
"""Async Playwright helpers for the gateway's built-in browser.

The gateway starts a visible Edge window pointing at its own page once the
HTTP server is up (see app.py startup event). Everything here is async and
initialization is explicit (no module-level side effects): running
sync_playwright at import time would plant a live asyncio loop on the main
thread and break `uvicorn.run`'s `asyncio.run`.

The browser MCP server (.deepagents/tools/mcp_servers/browser_server) is a
separate stdio subprocess with its own sync Playwright instance and does not
use this module.
"""
import os

from playwright.async_api import BrowserContext, Page, Playwright, async_playwright

GLOBAL_BROWSER: BrowserContext = None
GLOBAL_PLAYWRIGHT: Playwright = None

SCRIPT_PATH = os.path.dirname(__file__)


async def init_browser(user_data_dir=None, headless=False):
    """Launch a persistent Edge browser context (async, call inside the app's loop).

    Args:
        user_data_dir: Optional directory for the persistent browser profile.
        headless: Whether to run the browser without a visible window.

    Returns:
        (context, playwright): the persistent context and Playwright instance.
    """
    if user_data_dir is not None:
        user_data_dir = os.path.abspath(user_data_dir)
    playwright = await async_playwright().start()
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
        no_viewport=True,
    )
    return browser, playwright


async def close_browser():
    """Close the global browser context and stop the Playwright instance."""
    global GLOBAL_PLAYWRIGHT, GLOBAL_BROWSER
    if GLOBAL_BROWSER:
        try:
            await GLOBAL_BROWSER.close()
        except Exception:  # noqa: BLE001 - shutdown best effort
            pass
    if GLOBAL_PLAYWRIGHT:
        try:
            await GLOBAL_PLAYWRIGHT.stop()
        except Exception:  # noqa: BLE001
            pass
    GLOBAL_PLAYWRIGHT = None
    GLOBAL_BROWSER = None
