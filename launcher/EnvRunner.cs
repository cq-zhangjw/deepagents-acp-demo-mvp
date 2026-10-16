using System.Diagnostics;
using System.IO;
using System.Net.Http;

namespace DeepAgentsLauncher;

/// <summary>Raised when no suitable system Python 3.12 can be found to create the venv.</summary>
internal sealed class PythonNotFoundException(string message) : Exception(message);

/// <summary>
/// Prepares the Python environment (.venv 3.12 + requirements.txt) and starts app.py.
/// Progress is reported through the <see cref="Log"/> callback so the UI can display it.
/// </summary>
internal sealed class EnvRunner
{
    private const string VenvDir = ".venv";
    private const string Requirements = "requirements.txt";
    private const string AppEntry = "app.py";
    private const string TargetVersionPrefix = "3.12.";
    private const string PipIndexUrl = "https://pypi.tuna.tsinghua.edu.cn/simple";

    private readonly string _root;
    private readonly int _port;

    public EnvRunner(string root, int port)
    {
        _root = root;
        _port = port;
    }

    /// <summary>Called with each human-readable progress line.</summary>
    public Action<string>? Log { get; set; }

    /// <summary>The running app.py process, available after <see cref="PrepareAndStartAsync"/>.</summary>
    public Process? AppProcess { get; private set; }

    public string ServiceUrl => $"http://127.0.0.1:{_port}";

    /// <summary>
    /// Ensures the venv + deps exist, launches app.py, and waits until the HTTP
    /// service answers. Throws on any unrecoverable failure.
    /// </summary>
    public async Task PrepareAndStartAsync(CancellationToken ct)
    {
        Report($"工作目录: {_root}");
        string venvPython = Path.Combine(_root, VenvDir, "Scripts", "python.exe");

        if (!IsVenvReady(venvPython))
        {
            string? systemPython = FindSystemPython312();
            if (systemPython is null)
            {
                throw new PythonNotFoundException("未找到系统 Python 3.12.x，请先安装 Python 3.12 并加入 PATH。");
            }

            Report($"使用系统 Python 创建虚拟环境: {systemPython}");
            await RunAsync(systemPython, "-m", "venv", "--copies", VenvDir).ConfigureAwait(false);

            Report("升级 pip ...");
            await RunAsync(venvPython, "-m", "pip", "install", "--upgrade", "pip", "-i", PipIndexUrl).ConfigureAwait(false);

            Report("安装 requirements.txt（首次较慢，请稍候）...");
            await RunAsync(venvPython, "-m", "pip", "install", "-r", Requirements, "-i", PipIndexUrl).ConfigureAwait(false);
        }
        else
        {
            Report("检测到可用的虚拟环境 (.venv, Python 3.12.x)。");
        }

        Report("正在启动服务 (app.py) ...");
        AppProcess = StartApp(venvPython);

        Report("等待服务就绪 ...");
        await WaitForServiceAsync(ct).ConfigureAwait(false);
        Report("服务已就绪，正在打开页面。");
    }

    public void StopApp()
    {
        try
        {
            if (AppProcess is { HasExited: false })
            {
                AppProcess.Kill(entireProcessTree: true);
            }
        }
        catch
        {
            // best-effort cleanup
        }
    }

    private bool IsVenvReady(string venvPython)
    {
        if (!File.Exists(venvPython))
        {
            return false;
        }

        string? version = GetPythonVersion(venvPython);
        return version is not null && version.StartsWith(TargetVersionPrefix, StringComparison.Ordinal);
    }

    private static string? FindSystemPython312()
    {
        if (CaptureOutput("py", "-3.12", "-c", "import sys; print(sys.executable)") is { } viaLauncher
            && File.Exists(viaLauncher) && IsVersion312(viaLauncher))
        {
            return viaLauncher;
        }

        if (CaptureOutput("python", "-c", "import sys; print(sys.executable)") is { } viaPath
            && File.Exists(viaPath) && IsVersion312(viaPath))
        {
            return viaPath;
        }

        string localAppData = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        string[] candidates =
        [
            Path.Combine(localAppData, "Programs", "Python", "Python312", "python.exe"),
            @"C:\Program Files\Python312\python.exe",
            @"C:\Program Files\Python\python3120\python.exe",
            @"C:\Program Files\Python\python3120\tools\python.exe",
        ];

        foreach (string candidate in candidates)
        {
            if (File.Exists(candidate) && IsVersion312(candidate))
            {
                return candidate;
            }
        }

        return null;
    }

    private static bool IsVersion312(string pythonExe)
    {
        string? version = GetPythonVersion(pythonExe);
        return version is not null && version.StartsWith(TargetVersionPrefix, StringComparison.Ordinal);
    }

    private static string? GetPythonVersion(string pythonExe)
        => CaptureOutput(pythonExe, "-c", "import sys; print('.'.join(map(str, sys.version_info[:3])))");

    private static string? CaptureOutput(string fileName, params string[] args)
    {
        try
        {
            var psi = new ProcessStartInfo
            {
                FileName = fileName,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                UseShellExecute = false,
                CreateNoWindow = true,
            };
            foreach (string a in args)
            {
                psi.ArgumentList.Add(a);
            }

            using var proc = Process.Start(psi);
            if (proc is null)
            {
                return null;
            }

            string output = proc.StandardOutput.ReadToEnd();
            proc.StandardError.ReadToEnd();
            proc.WaitForExit();

            if (proc.ExitCode != 0)
            {
                return null;
            }

            string trimmed = output.Trim();
            return string.IsNullOrWhiteSpace(trimmed) ? null : trimmed;
        }
        catch
        {
            return null;
        }
    }

    /// <summary>Runs a child process, streaming its stdout/stderr to the log; throws on non-zero exit.</summary>
    private async Task RunAsync(string fileName, params string[] args)
    {
        var psi = new ProcessStartInfo
        {
            FileName = fileName,
            WorkingDirectory = _root,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            UseShellExecute = false,
            CreateNoWindow = true,
        };
        foreach (string a in args)
        {
            psi.ArgumentList.Add(a);
        }

        using var proc = new Process { StartInfo = psi, EnableRaisingEvents = true };
        proc.OutputDataReceived += (_, e) => { if (e.Data is not null) Report(e.Data); };
        proc.ErrorDataReceived += (_, e) => { if (e.Data is not null) Report(e.Data); };

        proc.Start();
        proc.BeginOutputReadLine();
        proc.BeginErrorReadLine();
        await proc.WaitForExitAsync().ConfigureAwait(false);

        if (proc.ExitCode != 0)
        {
            throw new InvalidOperationException($"命令失败 ({fileName})，退出码 {proc.ExitCode}。");
        }
    }

    private Process StartApp(string venvPython)
    {
        var psi = new ProcessStartInfo
        {
            FileName = venvPython,
            WorkingDirectory = _root,
            UseShellExecute = false,
            CreateNoWindow = true,
        };
        psi.ArgumentList.Add(AppEntry);

        var proc = Process.Start(psi) ?? throw new InvalidOperationException("无法启动 app.py 进程。");
        return proc;
    }

    private async Task WaitForServiceAsync(CancellationToken ct)
    {
        using var http = new HttpClient { Timeout = TimeSpan.FromSeconds(3) };
        var deadline = DateTime.UtcNow.AddSeconds(120);

        while (DateTime.UtcNow < deadline)
        {
            ct.ThrowIfCancellationRequested();

            if (AppProcess is { HasExited: true })
            {
                throw new InvalidOperationException("app.py 进程意外退出，请检查日志。");
            }

            try
            {
                using var resp = await http.GetAsync(ServiceUrl, ct).ConfigureAwait(false);
                return; // any HTTP response means the port is serving
            }
            catch
            {
                await Task.Delay(500, ct).ConfigureAwait(false);
            }
        }

        throw new TimeoutException("等待服务就绪超时（120 秒）。");
    }

    private void Report(string line) => Log?.Invoke(line);
}
