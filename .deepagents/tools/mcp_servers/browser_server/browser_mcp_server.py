import json
import os
import time
import traceback
from fastmcp import FastMCP

from playwright.sync_api import sync_playwright, BrowserContext, Playwright, Page
script_path = os.path.dirname(__file__)

browser: BrowserContext = None
playwright: Playwright = None

GLOBAL_TIMEOUT = 5 * 60 * 1000
storage = {}


mcp = FastMCP("browser mcp server")
SCRIPT_PATH = os.path.dirname(__file__)

def init_browser(
        user_data_dir=None,
        headless=False
):
    """Launch a persistent Microsoft Edge browser context.

    Args:
        user_data_dir: Optional directory for the persistent browser profile.
        headless: Whether to run the browser without a visible window.

    Returns:
        None. The context and Playwright instance are stored globally.
    """
    global browser, playwright
    if user_data_dir is not None:
        user_data_dir = os.path.abspath(user_data_dir)
    playwright = sync_playwright().start()
    browser = playwright.chromium.launch_persistent_context(
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


def close_browser():
    """Close the global browser context and stop the Playwright instance.

    Returns:
        None.
    """
    global playwright, browser
    if browser:
        browser.close()
    if playwright:
        playwright.stop()

    # 重置全局变量
    playwright = browser = None


def go_to_url(url: str, css_flag: str = None, max_retry: int = 1) -> Page:
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
        init_browser()
    count = 1
    while True:
        page = browser.new_page()

        try:
            page.goto(url, wait_until="networkidle", timeout=GLOBAL_TIMEOUT)

            if css_flag is not None:
                page.wait_for_selector("#O365_SuiteBranding_container", timeout=GLOBAL_TIMEOUT)
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


def wait_for_element(page, selector, iframe_selector=None, timeout=GLOBAL_TIMEOUT, state="visible"):
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
        frame.locator(selector).wait_for(state=state, timeout=timeout)
    else:
        page.locator(selector).wait_for(state=state, timeout=timeout)


def run_automation(steps: list, wait_user: bool=True):
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
        init_browser(headless=headless)

    def on_page_close(page_obj: Page):
        pass
    page = browser.new_page()
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
                    wait_for_element(page, wait_selector, wait_iframe, timeout)

                if action is not None:
                    if action == "goto":
                        page.goto(resolved_value, wait_until="networkidle", timeout=timeout)

                    elif action == "click":
                        if element_selector:
                            element = get_element(page, element_selector, element_iframe)
                            if element:
                                element.click(timeout=timeout)

                    elif action == "fill":
                        if not element_selector:
                            # raise ValueError("❌ action 'fill', must set the 'element_selector'")
                            return f"Error: ❌ action 'fill', must set the 'element_selector'"
                        element = get_element(page, element_selector, element_iframe)
                        if element:
                            element.fill(resolved_value, timeout=timeout)

                    elif action == "wait":
                        if resolved_value:
                            page.wait_for_timeout(float(resolved_value))
                        elif wait_selector:
                            # 如果 wait 动作没有指定 value，但有 wait_selector，则等待该元素
                            wait_for_element(page, wait_selector, wait_iframe, timeout)

                    elif action == "extract":
                        if not element_selector:
                            # raise ValueError(
                            #     "❌ action 'extract', must set the 'element_selector' as extraction source!")
                            return f"Error: ❌ action 'extract', must set the 'element_selector' as extraction source!"
                        element = get_element(page, element_selector, element_iframe)
                        if element:
                            text = element.inner_text()
                            if save_as:
                                storage[save_as] = text.strip()
                                # logger.info(f"💾 extract success: {save_as} = '{storage[save_as]}'")
                                return f"Extracted and saved as '{save_as}': {storage[save_as]}"

                    elif action == "screenshot":
                        path = resolved_value if resolved_value else f"screenshot_{int(time.time())}.png"
                        page.screenshot(path=path)

                    elif action == "hover":
                        if element_selector:
                            element = get_element(page, element_selector, element_iframe)
                            if element:
                                element.hover(timeout=timeout)

                    elif action == "select":
                        if not element_selector:
                            # raise ValueError("❌ action 'select', must set the 'element_selector'")
                            return f"Error: ❌ action 'select', must set the 'element_selector'"
                        element = get_element(page, element_selector, element_iframe)
                        if element:
                            element.select_option(resolved_value, timeout=timeout)

                    elif action == "press":
                        if not element_selector:
                            page.keyboard.press(resolved_value)
                        else:
                            element = get_element(page, element_selector, element_iframe)
                            if element:
                                element.press(resolved_value)

                    else:
                        # raise ValueError(f"❌ Unknown action type: {action}")
                        return f"Error: ❌ Unknown action type: {action}"
                break

            except Exception as e:
                attempt += 1
                if attempt > max_retry:
                    error_img = f"{SCRIPT_PATH}/error_step_{index + 1}_{int(time.time())}.png"
                    page.screenshot(path=error_img)
                    # logger.info(f"❌ Step {index + 1} failed after {max_retry} retries. Screenshot saved: {error_img}")
                    # raise e
                    return f"Error: ❌ Step {index + 1} failed after {max_retry} retries. Screenshot saved: {error_img}. Traceback: {traceback.format_exc()}"
                else:
                    # logger.info(f"⏳ Retry {attempt}/{max_retry} after error: {str(e)}")
                    time.sleep(2)

    return page

@mcp.tool()
def do_automation_by_script(script_param_file: str, wait_user: bool = True) -> str:
    """Execute browser automation steps from a JSON script file.

    The JSON file must contain a list of step lists. Each step list is passed to
    the browser automation runner, which supports navigation, interaction,
    waiting, text extraction, and screenshots. When ``wait_user`` is True, the
    visible browser remains open until its context is closed; otherwise it runs
    headlessly.

    Args:
        script_param_file: Path to the UTF-8 encoded JSON automation script. Sample file placed in the ./samples folder.
        wait_user: Whether to show the browser and wait for the user to close it.

    Returns:
        A success message, or an error message containing the traceback.
    """
    try:
        pages = []
        scripts_list = json.load(open(script_param_file, encoding="utf-8"))
        for steps in scripts_list:
            page = run_automation(steps, wait_user=wait_user)
            pages.append(page)
        if wait_user:
            browser.wait_for_event("close", timeout=60 * 60 * 1000)
            # while not browser.is_closed():
            #     logger.info(f"Wait user confirm and close the browser...{browser.pages}")
            #     time.sleep(2)
        close_browser()
        return "Do automation competed."
    except:
        close_browser()
        return f"Error: {traceback.format_exc()}"


if __name__ == '__main__':
    import sys

    sys.stdin = open(sys.stdin.fileno(), "r", encoding="utf-8", closefd=False)
    sys.stdout = open(sys.stdout.fileno(), "w", encoding="utf-8", closefd=False)

    mcp.run(transport="stdio")