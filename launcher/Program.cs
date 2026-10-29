using System.IO;
using System.Net;
using System.Text;
using Photino.NET;

namespace DeepAgentsLauncher;

/// <summary>
/// Photino launcher: opens a native window hosting the OS WebView, shows a loading
/// page while preparing the Python env + starting app.py, then navigates to the gateway.
/// </summary>
internal static class Program
{
    private const int Port = 8000;
    private const string PythonDownloadUrl = "https://www.python.org/downloads/release/python-3128/";

    private static PhotinoWindow _window = null!;
    private static EnvRunner _runner = null!;
    private static readonly CancellationTokenSource Cts = new();
    private static readonly StringBuilder LogBuffer = new();
    private static readonly object LogLock = new();
    private static readonly TaskCompletionSource WebReady = new(TaskCreationOptions.RunContinuationsAsynchronously);
    private static bool _navigatedToApp;
    private static DateTime _lastRender = DateTime.MinValue;

    [STAThread]
    private static void Main()
    {
        string root = AppContext.BaseDirectory.TrimEnd(Path.DirectorySeparatorChar);
        _runner = new EnvRunner(root, Port) { Log = PushLog };

        lock (LogLock)
        {
            LogBuffer.AppendLine("正在准备运行环境 ...");
        }

        string iconPath = Path.Combine(AppContext.BaseDirectory, "app.ico");
        var builder = new PhotinoWindow()
            .SetTitle("ACPAgent");
        if (File.Exists(iconPath))
        {
            builder = builder.SetIconFile(iconPath);
        }

        _window = builder
            .SetUseOsDefaultSize(false)
            .SetSize(1280, 860)
            .SetMaximized(true)
            .SetResizable(true)
            .RegisterWebMessageReceivedHandler(OnWebMessage)
            .RegisterWindowCreatedHandler(OnWindowCreated)
            .RegisterWindowClosingHandler(OnWindowClosing)
            .LoadRawString(RenderLoadingPage());

        _window.WaitForClose();
    }

    private static void OnWebMessage(object? sender, string message)
    {
        // The loading page posts "ready" once its DOM is parsed and the JS bridge works.
        if (message == "ready")
        {
            WebReady.TrySetResult();
        }
        else if (message.StartsWith("open:", StringComparison.Ordinal))
        {
            OpenInDefaultBrowser(message["open:".Length..]);
        }
    }

    private static void OpenInDefaultBrowser(string url)
    {
        try
        {
            System.Diagnostics.Process.Start(new System.Diagnostics.ProcessStartInfo(url) { UseShellExecute = true });
        }
        catch
        {
            // best-effort; ignore if no default browser is configured
        }
    }

    private static void OnWindowCreated(object? sender, EventArgs e)
    {
        Task.Run(async () =>
        {
            try
            {
                // Wait for the real "WebView ready" signal; fall back to a timeout so a
                // missed message never hangs startup. This avoids the native AccessViolation
                // that happens if we reload/navigate before the WebView's first frame.
                await Task.WhenAny(WebReady.Task, Task.Delay(5000, Cts.Token)).ConfigureAwait(false);
                _window.Invoke(() => _window.LoadRawString(RenderLoadingPage()));

                await _runner.PrepareAndStartAsync(Cts.Token);
                _navigatedToApp = true;
                _window.Invoke(() => _window.Load(_runner.ServiceUrl));
            }
            catch (OperationCanceledException)
            {
                // window closing; ignore
            }
            catch (PythonNotFoundException)
            {
                _window.Invoke(() => _window.LoadRawString(RenderPythonMissingPage()));
            }
            catch (Exception ex)
            {
                PushLog(string.Empty);
                PushLog($"[错误] {ex.Message}");
            }
        });
    }

    private static bool OnWindowClosing(object? sender, EventArgs e)
    {
        Cts.Cancel();
        _runner.StopApp();
        return false; // allow close
    }

    private static void PushLog(string line)
    {
        if (_navigatedToApp)
        {
            return;
        }

        lock (LogLock)
        {
            LogBuffer.AppendLine(line);
        }

        if (!WebReady.Task.IsCompleted)
        {
            return;
        }

        // Throttle reloads so pip's chatty output doesn't hammer the native layer.
        if ((DateTime.UtcNow - _lastRender).TotalMilliseconds < 400)
        {
            return;
        }
        _lastRender = DateTime.UtcNow;

        _window.Invoke(() =>
        {
            if (!_navigatedToApp)
            {
                _window.LoadRawString(RenderLoadingPage());
            }
        });
    }

    private static string RenderLoadingPage()
    {
        string log;
        lock (LogLock)
        {
            log = LogBuffer.ToString();
        }

        string logHtml = WebUtility.HtmlEncode(log);
        return $$"""
        <!doctype html>
        <html lang="zh">
        <head>
          <meta charset="utf-8">
          <style>
            body { margin:0; font-family: "Segoe UI","Microsoft YaHei",sans-serif;
                   background:#0f172a; color:#e2e8f0; }
            .wrap { padding:28px 32px; }
            h1 { font-size:20px; font-weight:600; margin:0 0 6px; }
            .sub { color:#94a3b8; font-size:13px; margin-bottom:18px; }
            .spinner { display:inline-block; width:14px; height:14px; margin-right:8px;
                       border:2px solid #334155; border-top-color:#38bdf8; border-radius:50%;
                       animation:spin .8s linear infinite; vertical-align:middle; }
            @keyframes spin { to { transform:rotate(360deg); } }
            pre { background:#020617; border:1px solid #1e293b; border-radius:8px;
                  padding:14px 16px; font-size:12.5px; line-height:1.6;
                  white-space:pre-wrap; word-break:break-all; height:70vh; overflow:auto; margin:0; }
          </style>
        </head>
        <body>
          <div class="wrap">
            <h1><span class="spinner"></span>DeepAgents ACP 正在启动</h1>
            <div class="sub">首次运行会创建 .venv 并安装依赖，可能需要几分钟，请耐心等待。</div>
            <pre id="log">{{logHtml}}</pre>
          </div>
          <script>
            const el = document.getElementById('log');
            if (el) el.scrollTop = el.scrollHeight;
            // Tell C# the WebView + JS bridge are ready to receive reloads/navigation.
            window.external.sendMessage('ready');
          </script>
        </body>
        </html>
        """;
    }

    private static string RenderPythonMissingPage() => $$"""
    <!doctype html>
    <html lang="zh">
    <head>
      <meta charset="utf-8">
      <style>
        body { margin:0; font-family: "Segoe UI","Microsoft YaHei",sans-serif;
               background:#0f172a; color:#e2e8f0; display:flex; min-height:100vh;
               align-items:center; justify-content:center; }
        .card { max-width:560px; padding:36px 40px; background:#111827;
                border:1px solid #1e293b; border-radius:14px; text-align:center; }
        .icon { font-size:40px; margin-bottom:10px; }
        h1 { font-size:21px; margin:0 0 10px; }
        p { color:#94a3b8; font-size:14px; line-height:1.7; margin:0 0 22px; }
        a.btn { display:inline-block; background:#38bdf8; color:#04121f; font-weight:600;
                text-decoration:none; padding:11px 22px; border-radius:8px; font-size:14px; }
        a.btn:hover { background:#7dd3fc; }
        code { background:#020617; padding:2px 6px; border-radius:4px; color:#e2e8f0; }
      </style>
    </head>
    <body>
      <div class="card">
        <div class="icon">🐍</div>
        <h1>未检测到 Python 3.12</h1>
        <p>
          启动器需要系统已安装 <b>Python 3.12.x</b> 来创建虚拟环境。<br>
          请下载安装后重新运行本程序。<br>
          安装时请勾选 <code>Add python.exe to PATH</code>。
        </p>
        <a class="btn" href="{{PythonDownloadUrl}}" target="_blank" rel="noopener">下载 Python 3.12</a>
      </div>
      <script>
        // Open the download link in the user's default browser instead of the WebView.
        document.querySelector('a.btn').addEventListener('click', function (ev) {
          ev.preventDefault();
          window.external.sendMessage('open:' + this.href);
        });
      </script>
    </body>
    </html>
    """;
}
