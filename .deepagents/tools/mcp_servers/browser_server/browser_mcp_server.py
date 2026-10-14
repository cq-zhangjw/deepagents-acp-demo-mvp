import asyncio
import json
import os
import time
import traceback
from fastmcp import FastMCP

from playwright.async_api import BrowserContext, Page, Playwright, async_playwright
script_path = os.path.dirname(__file__)

browser: BrowserContext = None
playwright: Playwright = None

GLOBAL_TIMEOUT = 5 * 60 * 1000
storage = {}

# JavaScript that renders a simplified XML element tree of the page for
# locating elements. Each node carries its key attributes plus a stable
# `css` selector path so the model can feed it straight into click/fill.
_XML_TREE_JS = r"""
(function(){
  function esc(s){return String(s==null?'':s).replace(/[<>&'"]/g,function(c){return {'<':'&lt;','>':'&gt;','&':'&amp;',"'":'&apos;','"':'&quot;'}[c];});}
  function cssEsc(s){return s.replace(/[^a-zA-Z0-9_\-]/g,function(c){return '\\'+c;});}
  function cssPath(el){
    var parts=[], cur=el;
    while(cur && cur.nodeType===1){
      var part=cur.tagName.toLowerCase();
      if(cur.id){ part+='#'+cssEsc(cur.id); parts.unshift(part); break; }
      var cls=[];
      for(var i=0;i<cur.classList.length;i++){var c=cur.classList[i]; if(c && !/^[a-f0-9]{6,}$/i.test(c)) cls.push(c); if(cls.length>=2) break;}
      if(cls.length) part+='.'+cls.map(cssEsc).join('.');
      var p=cur.parentElement;
      if(p){
        var same=Array.prototype.filter.call(p.children,function(x){return x.tagName===cur.tagName;});
        if(same.length>1) part+=':nth-of-type('+(same.indexOf(cur)+1)+')';
      }
      parts.unshift(part);
      cur=p;
      if(parts.length>10) break;
    }
    return parts.join(' > ');
  }
  var SKIP={SCRIPT:1,STYLE:1,NOSCRIPT:1,TEMPLATE:1,HEAD:1,LINK:1,META:1};
  var ATTRS=['id','name','placeholder','aria-label','data-testid','role','href','type','alt','title','for'];
  function attrs(el){
    var out=[];
    for(var i=0;i<ATTRS.length;i++){var v=el.getAttribute(ATTRS[i]); if(v!==null && v!==''){out.push(ATTRS[i]+'="'+esc(String(v).slice(0,80))+'"');}}
    var cls=[];
    for(var j=0;j<el.classList.length;j++){var c=el.classList[j]; if(c && !/^[a-f0-9]{6,}$/i.test(c)) cls.push(c); if(cls.length>=2) break;}
    if(cls.length) out.push('class="'+esc(cls.join(' '))+'"');
    var tag=el.tagName.toLowerCase();
    if(tag==='input'||tag==='textarea'||tag==='select'){var v=el.value; if(v!==null && v!==''){out.push('value="'+esc(String(v).slice(0,60))+'"');}}
    return out;
  }
  var MAXN=window.__PW_MAX_NODES||300, MAXD=window.__PW_MAX_DEPTH||8;
  var out=[], nodes=0, truncated=false;
  function walk(el, depth){
    if(nodes>=MAXN){ if(!truncated){out.push('<!-- truncated -->'); truncated=true;} return; }
    if(!el || el.nodeType!==1) return;
    if(SKIP[el.tagName]) return;
    var tag=el.tagName.toLowerCase();
    var a=attrs(el), css=cssPath(el);
    out.push('<'+tag+(a.length?' '+a.join(' '):'')+(css?' css="'+esc(css)+'"':'')+'>');
    nodes++;
    if(depth<MAXD){
      for(var i=0;i<el.children.length;i++){walk(el.children[i], depth+1); if(nodes>=MAXN) break;}
    } else if(el.children.length){ out.push('...'); }
    out.push('</'+tag+'>');
  }
  walk(document.body||document.documentElement, 0);
  return out.join('');
})()
"""


mcp = FastMCP("browser mcp server")
SCRIPT_PATH = os.path.dirname(__file__)


async def init_browser(
        user_data_dir=None,
        headless=False
):
    """Launch a persistent Microsoft Edge browser context (async).

    Args:
        user_data_dir: Optional directory for the persistent browser profile.
        headless: Whether to run the browser without a visible window.

    Returns:
        None. The context and Playwright instance are stored globally.
    """
    global browser, playwright
    if user_data_dir is not None:
        user_data_dir = os.path.abspath(user_data_dir)
    playwright = await async_playwright().start()
    try:
        browser = await playwright.chromium.launch_persistent_context(
            user_data_dir=user_data_dir,
            accept_downloads=True,
            headless=headless,
            slow_mo=100 if not headless else 0,
            channel="msedge",
            # Keep the OS sandbox on: without it Edge shows the
            # "unsupported command-line flag --no-sandbox" infobar.
            chromium_sandbox=True,
            args=[
                '--start-maximized',
                # Suppress the "unsupported command-line flag" infobar for the
                # automation flags below (Chromium skips it when --test-type is set).
                '--test-type',
                '--disable-blink-features=AutomationControlled',
                '--disable-infobars',
                '--disable-features=TranslateUI',
            ],
            ignore_default_args=['--enable-automation'],
            no_viewport=True
        )
    except Exception:
        # Always stop the driver subprocess, otherwise its transport is
        # finalized after the loop closes and emits a scary-but-harmless
        # "Event loop is closed" warning on shutdown.
        try:
            await playwright.stop()
        except Exception:  # noqa: BLE001 - best effort cleanup
            pass
        raise
    return browser


async def close_browser():
    """Close the global browser context and stop the Playwright instance.

    Returns:
        None.
    """
    global playwright, browser
    if browser:
        try:
            await browser.close()
        except Exception:  # noqa: BLE001 - best effort
            pass
    if playwright:
        try:
            await playwright.stop()
        except Exception:  # noqa: BLE001 - best effort
            pass

    # 重置全局变量
    playwright = browser = None


async def _ensure_browser(headless=False):
    """Lazily initialise the persistent browser context if not running."""
    global browser, playwright
    if playwright is None or browser is None:
        await init_browser(headless=headless)
    return browser


async def _active_page(page_index=-1):
    """Return the currently usable page (default: the most recently opened)."""
    global browser
    await _ensure_browser()
    if browser is None or not browser.pages:
        return None
    return browser.pages[page_index]


async def go_to_url(url: str, css_flag: str = None, max_retry: int = 1) -> Page:
    """Open a URL in a new page and optionally wait for a SharePoint element.

    Args:
        url: URL to navigate to.
        css_flag: When provided, wait for the SharePoint suite branding element.
        max_retry: Maximum number of navigation attempts.

    Returns:
        The page that was opened and navigated.

    Raises:
        Exception: If navigation fails after the configured attempts.
    """
    global browser, playwright
    if playwright is None and browser is None:
        await init_browser()
    count = 1
    while True:
        page = await browser.new_page()

        try:
            await page.goto(url, wait_until="networkidle", timeout=GLOBAL_TIMEOUT)

            if css_flag is not None:
                await page.wait_for_selector("#O365_SuiteBranding_container", timeout=GLOBAL_TIMEOUT)
            break
        except:
            count += 1
            if count > max_retry:
                raise Exception(f"Error: go to url failed! url: {url}. Tracback: {traceback.format_exc()}")
    return page


def get_element(page, selector, iframe_selector=None):
    """Get the first matching element, optionally from an iframe.

    Args:
        page: Playwright page to search.
        selector: CSS or Playwright selector for the target element.
        iframe_selector: Optional selector for the containing iframe.

    Returns:
        A locator for the first matching element.
    """
    if iframe_selector:
        frame = page.frame_locator(iframe_selector)
        return frame.locator(selector).first
    else:
        return page.locator(selector).first


async def wait_for_element(page, selector, iframe_selector=None, timeout=GLOBAL_TIMEOUT, state="visible"):
    """Wait for an element to reach the requested state.

    Args:
        page: Playwright page to search.
        selector: Selector for the target element. If None, return immediately.
        iframe_selector: Optional selector for the containing iframe.
        timeout: Maximum wait time in milliseconds.
        state: Playwright locator state to wait for, such as ``visible``.

    Returns:
        None.
    """
    if selector is None:
        return

    # logger.info(f"🔍 [wait element] -> {selector} (iframe: {iframe_selector or 'main'})")

    if iframe_selector:
        frame = page.frame_locator(iframe_selector)
        await frame.locator(selector).wait_for(state=state, timeout=timeout)
    else:
        await page.locator(selector).wait_for(state=state, timeout=timeout)


async def run_automation(steps: list, wait_user: bool=True):
    """Run a sequence of browser actions in a new page.

    Each step is a dictionary that may contain ``action``, ``element_selector``,
    ``element_iframe``, ``wait_selector``, ``wait_iframe``, ``value``, ``timeout``,
    ``max_retry``, and ``save_as``. Supported actions are ``goto``, ``click``,
    ``fill``, ``wait``, ``extract``, ``screenshot``, ``hover``, ``select``, and
    ``press``.

    Args:
        steps: Ordered list of action dictionaries to execute.
        wait_user: If True, launch the browser visibly; otherwise use headless mode.

    Returns:
        The Playwright page, extracted text, or an error message.

    Raises:
        Exception: If the step list is empty or browser initialization fails.
    """
    global storage, browser

    if steps is None or len(steps) == 0:
        raise Exception("Error: please input your steps!")

    headless = False if wait_user else True
    if browser is None:
        await init_browser(headless=headless)

    def on_page_close(page_obj: Page):
        pass
    page = await browser.new_page()
    page.on("close", on_page_close)

    # logger.info(f"📋 Start action，total steps: {len(steps)} ...")

    for index, step in enumerate(steps):
        try:
            action = step.get("action")
            element_selector = step.get("element_selector")
            element_iframe = step.get("element_iframe")  # element 所在的 iframe
            wait_selector = step.get("wait_selector")
            wait_iframe = step.get("wait_iframe")  # wait 所在的 iframe
            value = step.get("value")
            timeout = step.get("timeout") if step.get("timeout") is not None else GLOBAL_TIMEOUT
            max_retry = step.get("max_retry") if step.get("max_retry") is not None else 0
            save_as = step.get("save_as", None)

        except KeyError as e:
            # return f"Error: ❌ Every step must include the required fields: action, element_selector, wait_selector, value, timeout, max_retry. You can set them to None."
            return f"Error: ❌ Every step must include the required fields: action, element_selector, wait_selector, value, timeout, max_retry. You can set them to None. Missing field: {str(e)}"
        # logger.info(f"\n📍 [Step {index + 1}/{len(steps)}] action: {action or 'None'} value: {value}")

        resolved_value = value

        attempt = 0
        while attempt <= max_retry:
            try:
                if attempt > 0:
                    # logger.info(f"⚠️ retrying {attempt}/{max_retry} ...")
                    pass

                # 等待 wait_selector 出现（如果配置了）
                if wait_selector is not None:
                    await wait_for_element(page, wait_selector, wait_iframe, timeout)

                if action is not None:
                    if action == "goto":
                        await page.goto(resolved_value, wait_until="networkidle", timeout=timeout)

                    elif action == "click":
                        if element_selector:
                            element = get_element(page, element_selector, element_iframe)
                            if element:
                                await element.click(timeout=timeout)

                    elif action == "fill":
                        if not element_selector:
                            # raise ValueError("❌ action 'fill', must set the 'element_selector'")
                            return f"Error: ❌ action 'fill', must set the 'element_selector'"
                        element = get_element(page, element_selector, element_iframe)
                        if element:
                            await element.fill(resolved_value, timeout=timeout)

                    elif action == "wait":
                        if resolved_value:
                            await page.wait_for_timeout(float(resolved_value))
                        elif wait_selector:
                            # 如果 wait 动作没有指定 value，但有 wait_selector，则等待该元素
                            await wait_for_element(page, wait_selector, wait_iframe, timeout)

                    elif action == "extract":
                        if not element_selector:
                            # raise ValueError(
                            #     "❌ action 'extract', must set the 'element_selector' as extraction source!")
                            return f"Error: ❌ action 'extract', must set the 'element_selector' as extraction source!"
                        element = get_element(page, element_selector, element_iframe)
                        if element:
                            text = await element.inner_text()
                            if save_as:
                                storage[save_as] = text.strip()
                                # logger.info(f"💾 extract success: {save_as} = '{storage[save_as]}'")
                                return f"Extracted and saved as '{save_as}': {storage[save_as]}"

                    elif action == "screenshot":
                        path = resolved_value if resolved_value else f"screenshot_{int(time.time())}.png"
                        await page.screenshot(path=path)

                    elif action == "hover":
                        if element_selector:
                            element = get_element(page, element_selector, element_iframe)
                            if element:
                                await element.hover(timeout=timeout)

                    elif action == "select":
                        if not element_selector:
                            # raise ValueError("❌ action 'select', must set the 'element_selector'")
                            return f"Error: ❌ action 'select', must set the 'element_selector'"
                        element = get_element(page, element_selector, element_iframe)
                        if element:
                            await element.select_option(resolved_value, timeout=timeout)

                    elif action == "press":
                        if not element_selector:
                            await page.keyboard.press(resolved_value)
                        else:
                            element = get_element(page, element_selector, element_iframe)
                            if element:
                                await element.press(resolved_value)

                    else:
                        # raise ValueError(f"❌ Unknown action type: {action}")
                        return f"Error: ❌ Unknown action type: {action}"
                break

            except Exception as e:
                attempt += 1
                if attempt > max_retry:
                    error_img = f"{SCRIPT_PATH}/error_step_{index + 1}_{int(time.time())}.png"
                    await page.screenshot(path=error_img)
                    # logger.info(f"❌ Step {index + 1} failed after {max_retry} retries. Screenshot saved: {error_img}")
                    # raise e
                    return f"Error: ❌ Step {index + 1} failed after {max_retry} retries. Screenshot saved: {error_img}. Traceback: {traceback.format_exc()}"
                else:
                    # logger.info(f"⏳ Retry {attempt}/{max_retry} after error: {str(e)}")
                    await asyncio.sleep(2)

    return page


@mcp.tool()
async def browser_screenshot(page_index: int = -1, full_page: bool = True) -> str:
    """Capture a screenshot of the current (most recently opened) page.

    Args:
        page_index: Which page to capture. -1 means the most recent page.
        full_page: Whether to capture the full scrollable page (True) or only
            the visible viewport (False).

    Returns:
        The absolute path of the saved PNG. Use a file-read tool to view it.
    """
    try:
        page = await _active_page(page_index)
        if page is None:
            return "Error: no page is open. Please run do_automation_by_script or open a URL first."
        shot_dir = os.path.join(SCRIPT_PATH, "screenshots")
        os.makedirs(shot_dir, exist_ok=True)
        name = f"shot_{time.strftime('%Y%m%d_%H%M%S')}_{int(time.time() * 1000) % 1000}.png"
        path = os.path.join(shot_dir, name)
        await page.screenshot(path=path, full_page=full_page)
        return f"Screenshot saved: {os.path.abspath(path)}"
    except Exception:
        return f"Error: {traceback.format_exc()}"


@mcp.tool()
async def browser_get_content(
    format: str = "text",
    max_length: int = 20000,
    max_depth: int = 8,
    max_nodes: int = 300,
    page_index: int = -1,
) -> str:
    """Extract the current page content in a model-friendly form.

    Args:
        format: One of "text" (readable page text), "html" (raw page HTML), or
            "xml" (a simplified XML element tree). The XML tree annotates every
            element with its key attributes and a `css="..."` selector path so
            it can be used directly to locate elements for click/fill actions.
        max_length: Maximum characters returned for text/html output.
        max_depth: Maximum DOM depth in the XML element tree.
        max_nodes: Maximum number of elements in the XML element tree.
        page_index: Which page to read. -1 means the most recent page.

    Returns:
        The extracted content (truncated with a marker when over the limit).
    """
    try:
        page = await _active_page(page_index)
        if page is None:
            return "Error: no page is open. Please run do_automation_by_script or open a URL first."
        fmt = (format or "text").strip().lower()
        if fmt == "html":
            html = await page.content()
            if len(html) > max_length:
                return html[:max_length] + "\n...(truncated)"
            return html
        if fmt == "xml":
            code = (
                f"window.__PW_MAX_NODES={int(max_nodes)};"
                f"window.__PW_MAX_DEPTH={int(max_depth)};"
                + _XML_TREE_JS
            )
            tree = await page.evaluate(code)
            if len(tree) > max_length:
                return tree[:max_length] + "\n...(truncated)"
            return tree
        # default: readable text
        text = await page.locator("body").inner_text(timeout=GLOBAL_TIMEOUT)
        if len(text) > max_length:
            return text[:max_length] + "\n...(truncated)"
        return text
    except Exception:
        return f"Error: {traceback.format_exc()}"


@mcp.tool()
async def browser_execute_js(script: str, page_index: int = -1) -> str:
    """Execute JavaScript in the current page and return its result.

    Use this to verify elements before acting on them: check that a selector
    matches, read an element's state, or extract a value (e.g.
    ``document.querySelector('#submit') !== null``,
    ``document.querySelectorAll('.item').length``). The result is returned as
    JSON when it is structured, otherwise as its string form.

    Args:
        script: JavaScript expression to evaluate in the page's main frame.
            For multi-statement logic wrap it in an IIFE, e.g.
            ``(function(){ var el = document.querySelector('#x'); return el ? el.value : null; })()``.
        page_index: Which page to run on. -1 means the most recent page.

    Returns:
        The evaluated result as a string (JSON for dicts/lists).
    """
    try:
        page = await _active_page(page_index)
        if page is None:
            return "Error: no page is open. Please run do_automation_by_script or open a URL first."
        result = await page.evaluate(script)
        if isinstance(result, (dict, list)):
            return json.dumps(result, ensure_ascii=False, default=str)
        if isinstance(result, bool):
            return "true" if result else "false"
        if result is None:
            return "null"
        return str(result)
    except Exception:
        return f"Error: {traceback.format_exc()}"


@mcp.tool()
async def do_automation_by_script(script_param_file: str, wait_user: bool = True) -> str:
    """Execute browser automation steps from a JSON script file.

    The JSON file must contain a list of step lists. Each step list is passed to
    the browser automation runner, which supports navigation, interaction,
    waiting, text extraction, and screenshots. ``wait_user`` controls
    visibility: True shows the browser window, False runs it headless.

    The browser stays resident after the call on purpose, so later tool calls
    (browser_screenshot / browser_get_content / further automation) reuse the
    same instance instead of relaunching Edge. It is torn down when this MCP
    process exits.

    Args:
        script_param_file: Path to the UTF-8 encoded JSON automation script. Sample file placed in the ./samples folder.
        wait_user: Whether to show the browser window (True) or run headless (False).

    Returns:
        A success message, or an error message containing the traceback.
    """
    try:
        pages = []
        scripts_list = json.load(open(script_param_file, encoding="utf-8"))
        for steps in scripts_list:
            page = await run_automation(steps, wait_user=wait_user)
            pages.append(page)
        return f"Do automation completed. {len(pages)} page(s) open."
    except:
        return f"Error: {traceback.format_exc()}"


if __name__ == '__main__':
    import sys

    sys.stdin = open(sys.stdin.fileno(), "r", encoding="utf-8", closefd=False)
    sys.stdout = open(sys.stdout.fileno(), "w", encoding="utf-8", closefd=False)

    mcp.run(transport="stdio")
