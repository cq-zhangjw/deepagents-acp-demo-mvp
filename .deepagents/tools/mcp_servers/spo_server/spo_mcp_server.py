###
# Refrence site: https://shareplum.readthedocs.io/en/latest/files.html
###
import re
import sys

sys.stdin.reconfigure(encoding="utf-8")
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import datetime
import json
import os
import time
import traceback

import requests
from playwright.sync_api import sync_playwright
from fastmcp import FastMCP

from shareplum.site import Site, Version

mcp = FastMCP("Sharepoint MCP Server")

script_path = os.path.dirname(__file__)


def _has_spo_auth_cookie(browser_context):
    """检测 Playwright 上下文是否已持有 SharePoint 会话 cookie（FedAuth / rtFa）。
    这是判断登录完成的核心标志，与最终跳到哪条 URL 无关。
    失败时安全返回 False，避免阻塞主流程。
    """
    try:
        for c in browser_context.cookies():
            if c['name'] in ('FedAuth', 'rtFa'):
                return True
    except Exception:
        return False
    return False


def perform_login_attempt(spo_site="https://xxx.sharepoint.com", user_data_dir=None, headless=False,
                          cookie_json_file="cookie.json", timeout_ms=10000):
    """
    Attempts to log into a specified site using Playwright and extract authentication cookies.

    Args:
        spo_site(str): https://xxx.sharepoint.com
        user_data_dir (str): Path to the user data directory for the browser context.
                             This allows Playwright to reuse browser profiles and sessions.
        headless (bool): Whether to run the browser in headless mode (without a visible UI).
        cookie_json_file (str): Path to the JSON file where extracted cookies will be saved.
        timeout_ms (int): Maximum time in milliseconds to wait for page elements or navigation.
        spo_site (str): The URL of the target website for login.

    Returns:
        tuple[int, requests.cookies.RequestsCookieJar | str]:
            - If successful: (200, RequestsCookieJar containing FedAuth and rtFa cookies).
            - If failed: (500, traceback string of the exception).
    """
    if user_data_dir is not None:
        user_data_dir = os.path.abspath(user_data_dir)
    try:
        with sync_playwright() as playwright:
            browser_context = playwright.chromium.launch_persistent_context(
                user_data_dir=user_data_dir,
                accept_downloads=True,
                headless=headless,
                slow_mo=100 if not headless else 0,  # 无头模式下不需要slow_mo
                channel="msedge",
                args=[
                    '--start-maximized',
                    '--disable-blink-features=AutomationControlled',
                    '--disable-infobars',
                    '--disable-features=TranslateUI',
                ],
                ignore_default_args=['--enable-automation']
            )
            page = browser_context.new_page()
            # page.goto(spo_site, timeout=timeout_ms, wait_until="domcontentloaded")
            page.goto(spo_site, timeout=timeout_ms, wait_until="commit")
            # 登录完成后可能重定向到任意站点且 URL 通常不含 "/Home"，
            # 因此不再以 URL 作为登录判据，改为轮询检测会话 cookie 是否出现。

            deadline = time.time() + timeout_ms / 1000.0
            while time.time() < deadline:
                if _has_spo_auth_cookie(browser_context):
                    break
                time.sleep(1)

            # page.expect_request(f"{spo_site}/SitePages/Home.aspx", timeout=timeout_ms)

            cookies = browser_context.cookies()
            auth_cookie = requests.cookies.RequestsCookieJar()
            cookie_result = []

            for cookie in cookies:
                if cookie['name'] in ['FedAuth', 'rtFa']:  # SharePoint关键cookies
                    auth_cookie.set(cookie['name'], cookie['value'], domain='.sharepoint.com')
                    cookie_data = {"name": cookie['name'], "value": cookie['value']}
                    cookie_result.append(cookie_data)

            with open(cookie_json_file, mode="w", encoding="utf-8") as f:
                f.write(json.dumps(cookie_result, indent=2))

            return 200, auth_cookie
    except:
        return 500, traceback.format_exc()


def login_with_retry(
        spo_site,
        user_data_dir=None,
        cookie_json_file="cookie.json",
        max_retries=1,
        base_timeout=60):
    """
    Main login function with retry logic.

    Args:
        spo_site(str): https://xxx.sharepoint.com
        user_data_dir (str): Path to the user data directory.
        cookie_json_file (str): Path to save cookies.
        initial_headless (bool): Whether the first attempt should use headless mode.
        max_retries (int): Number of retries after the initial attempt fails.
        site_url (str): The URL of the target website for login.

    Returns:
        tuple[int, requests.cookies.RequestsCookieJar | str]:
            - If successful: (200, RequestsCookieJar containing FedAuth and rtFa cookies).
            - If failed after all retries: (error_code, traceback string).
    """
    timeout = base_timeout * 1000
    code, auth_cookie = perform_login_attempt(spo_site, user_data_dir, False, cookie_json_file,
                                              timeout_ms=timeout)  # 首次尝试超时1分钟

    if code == 200:
        return code, auth_cookie
    else:
        for i in range(max_retries):
            code, auth_cookie = perform_login_attempt(spo_site, user_data_dir, headless=False,
                                                      cookie_json_file=cookie_json_file,
                                                      timeout_ms=timeout * 5)
            if code == 200:
                return code, auth_cookie
            else:
                if i == max_retries - 1:
                    return code, auth_cookie


def init_site(site_url, user_data_dir=None, headless=True):
    """
        Initialize and return a SharePoint Site object, using a cached auth cookie
        if available, or performing an interactive login to obtain a new cookie.

        Args:
            site_url (str):
                The full URL of the SharePoint site to connect to.
                Example: "https://<tenant>.sharepoint.com/sites/<site_name>".
            user_data_dir (str | None, optional):
                Path to a persistent browser user data directory used during
                interactive login (via Playwright or similar). When provided,
                the browser session (cookies, login state, etc.) can be reused
                across runs to reduce repeated logins. If None, a temporary
                browser profile is used.
            headless (bool, optional):
                Whether to run the browser in headless mode during interactive
                login. If True (default), the browser window is not shown.
                If False, a visible browser window is opened for manual login.

        Returns:
            Site:
                An initialized SharePoint Site object authenticated with a valid
                auth cookie, ready for further SharePoint operations.

        Raises:
            Exception:
                - If no valid auth cookie can be obtained from the local cache
                  or via interactive login.
                - If creating the Site object fails after obtaining an auth cookie.
    """
    cookie_json_file = f"{script_path}/cookie.json"
    find_str = "sharepoint.com"
    spo_site = site_url[0: site_url.find(find_str) + len(find_str)]
    auth_cookie = None
    if os.path.exists(cookie_json_file):
        cookies = json.load(open(cookie_json_file))
        auth_cookie = requests.cookies.RequestsCookieJar()
        for cookie in cookies:
            auth_cookie.set(cookie['name'], cookie['value'], domain='.sharepoint.com')
    try:
        if auth_cookie is None:
            raise Exception("Error: No auth_cookie was found!")
        site = Site(site_url, authcookie=auth_cookie, version=Version.v2016)
        return site
    except Exception as e:
        if os.path.exists(cookie_json_file):
            os.remove(cookie_json_file)
        code, auth_cookie = login_with_retry(
            spo_site,
            user_data_dir=user_data_dir,
            cookie_json_file=cookie_json_file,
            max_retries=1,
            base_timeout=60
        )
        if code != 200:
            raise Exception(f"Error: Get the auth cookie failed!!!{auth_cookie}")
        try:
            site = Site(site_url, authcookie=auth_cookie, version=Version.v2016)
            return site
        except:
            raise Exception(f"Error: Get the auth cookie failed!!! {traceback.format_exc()}")


@mcp.tool()
def download_spo_file(site_url: str, spo_file_path: str):
    """
    Download a file from your sharepoint site.
    Args:
        site_url: a sharepoint, teams or ondrive site url
            egg1: https://xxx.sharepoint.com/sites/xxx
            egg2: https://xxx.sharepoint.com/teams/xxx
            egg3: https://xxx-my.sharepoint.com/my

        spo_file_path: should be the string between 'Shared%20Documents' and '?' of the file link.t
            egg:
                file link:
                    https://xxx.sharepoint.com/:x:/r/sites/CN-BFS-1st-SDD/Shared%20Documents/General/03_PJ%E8%B3%87%E7%94%A3/01_AI%E6%8A%80%E8%A1%93%E3%82%A2%E3%83%97%E3%83%AA%E5%8C%96/80_%E3%83%8E%E3%82%A6%E3%83%8F%E3%82%A6/03_AIAgent/Source/%E3%82%B9%E3%82%AF%E3%83%AA%E3%83%97%E3%83%88%E3%81%AE%E4%BD%9C%E6%88%90%E3%83%AB%E3%83%BC%E3%83%AB_NEW.xlsx?d=w099c7d79287a41528012a11757ff1de5&csf=1&web=1&e=RcLqkx
                spo_file_path:
                    Shared%20Documents/General/03_PJ%E8%B3%87%E7%94%A3/01_AI%E6%8A%80%E8%A1%93%E3%82%A2%E3%83%97%E3%83%AA%E5%8C%96/80_%E3%83%8E%E3%82%A6%E3%83%8F%E3%82%A6/03_AIAgent/Source/%E3%82%B9%E3%82%AF%E3%83%AA%E3%83%97%E3%83%88%E3%81%AE%E4%BD%9C%E6%88%90%E3%83%AB%E3%83%BC%E3%83%AB_NEW.xlsx
    Returns:
        The save file's absolute path
    """
    try:
        site = init_site(site_url)
    except:
        return traceback.format_exc()
    try:
        file_name = spo_file_path[spo_file_path.rfind("/") + 1:]
        folder_path = spo_file_path[0:spo_file_path.rfind("/") + 1]
        folder = site.Folder(folder_name=folder_path)
        content = folder.get_file(file_name)

        save_folder = f"{script_path}/tmp"
        os.makedirs(save_folder, exist_ok=True)

        save_file = f"{save_folder}/{file_name}"
        with open(save_file, mode="wb") as f:
            f.write(content)
        return {
            "save_file": save_file
        }
    except:
        return traceback.format_exc()


@mcp.tool()
def upload_file_to_spo(site_url: str, file_path: str, spo_path: str):
    """
    Uploads a local file to a specified path in SharePoint Online (SPO).

    Args:
        site_url (str): The URL of the SharePoint Online site, e.g., 'https://yourcompany.sharepoint.com/sites/YourSite'.
        file_path (str): The full path to the local file, e.g., 'C:/Users/user/Documents/report.pdf'.
        spo_path (str): The relative path to the target folder in SharePoint Online.
                        Example: 'Shared Documents/04_FRDC技術支援（AI関連）/08_2024/04_Traffic LLM datalabeling/99_納品'
                        This path is relative to the site's root or a document library.

    Returns:
        str: The upload result message.
    """
    try:
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"Local file '{file_path}' was not found.")

        site = init_site(site_url=site_url)
        folder = site.Folder(spo_path)
        with open(file_path, mode='rb') as file:
            file_content = file.read()
            folder.upload_file(file_content, os.path.basename(file_path))
        return "Upload successfully!"
    except:
        return traceback.format_exc()


def convert_datetime_values_to_isoformat(data):
    """
    Recursively convert all datetime objects in a dictionary or list to
    ISO 8601 formatted strings.

    Example format: "2026-02-09T17:32:30+00:00"

    Args:
        data (dict | list | any):
            The input data structure to process. Can be a dictionary, list,
            or any other type. Nested dictionaries and lists are traversed
            recursively.

    Returns:
        dict | list | any:
            The processed data structure where all datetime objects have been
            converted to ISO 8601 strings. Non-datetime values are returned
            unchanged.

    Raises:
        None
    """
    if isinstance(data, datetime.datetime):
        if data.tzinfo is None or data.tzinfo.utcoffset(data) is None:
            data = data.replace(tzinfo=datetime.timezone.utc)
        return data.isoformat(timespec='seconds')  # timespec='seconds' 确保没有微秒

    elif isinstance(data, dict):
        return {k: convert_datetime_values_to_isoformat(v) for k, v in data.items()}

    elif isinstance(data, list):
        return [convert_datetime_values_to_isoformat(elem) for elem in data]

    else:
        return data


@mcp.tool()
def get_list_data(
        site_url: str,
        list_id: str = None,
        list_name: str = None,
        fields: list[str] = None,
        query: dict[str, str] = None,
        op: str = "And"):
    """
    Retrieves data from a specified SharePoint Online (SPO) list and saves it as a local JSON file.

    Args:
        site_url (str): The URL of the SharePoint Online site, e.g., 'https://yourcompany.sharepoint.com/sites/YourSite'.
        list_id (str, optional): The GUID (Globally Unique Identifier) of the SharePoint list.
                                 Example: '{{A1B2C3D4-E5F6-7890-1234-567890ABCDEF}}'.
                                 Mutually exclusive with `list_name`; only one should be provided.
        list_name (str, optional): The display name of the SharePoint list.
                                   Example: 'My Custom List'.
                                   Mutually exclusive with `list_id`; only one should be provided.
        fields (list[str], optional): A list of field names to retrieve. If None or empty, all fields are retrieved.
                                      Example: ['Title', 'Author', 'Created'].
        query (dict[str, str], optional): A dictionary representing the query conditions.
                                          Keys are field names, and values are the desired values to match.
                                          Example: {'Status': 'Completed', 'Priority': 'High'}.
                                          Note: This simple dictionary query currently only supports 'Eq' (equals) operations.
                                          For more complex queries, the internal logic would need to be expanded.
        op (str, optional): The logical operator to combine multiple query conditions.
                            Currently supports "And" or "Or". Defaults to "And".

    Returns:
        str: The full path to the temporary JSON file where the list data is saved.
    """
    try:
        if list_id and list_name:
            raise Exception("Error: Both 'list_id' and 'list_name' were provided. Please select only one! 🤔")
        if not list_id and not list_name:
            raise Exception("Error: Either 'list_id' or 'list_name' must be provided! 🧐")

        site = init_site(site_url=site_url)

        g_id = list_id if list_id else list_name
        try:
            sp_list = site.List(g_id)
        except Exception as e:
            raise Exception(
                f"Error: Cannot access SharePoint list '{g_id}'! "
                f"Please ensure 'list_id' or 'list_name' is correct and you have permission to access it. Details: {e} 😟"
            )

        show_fields = None
        if fields and len(fields) > 0:
            show_fields = fields

        condition_query = None
        if query:
            if op != "And" and op != "Or":
                raise Exception(f"The '{op} operation was not support!'")

            if len(query.keys()) >= 1:
                condition_query = {
                    'Where': []
                }
                if len(query.keys()) == 1:
                    for key in query.keys():
                        condition_query["Where"].append(
                            ('Eq', key, query[key])
                        )
                else:
                    tmp_query = []
                    for key in query.keys():
                        tmp_query.append(
                            ('Eq', key, query[key])
                        )
                    condition_query["Where"].append(
                        (op, tmp_query)
                    )

        data = sp_list.get_list_items(fields=show_fields, query=condition_query)

        data = convert_datetime_values_to_isoformat(data)
        os.makedirs(f"{script_path}/tmp", exist_ok=True)
        tmp_file = f"{script_path}/tmp/{int(time.time() * 1000)}.json"

        with open(tmp_file, mode="w", encoding="utf-8") as f:
            f.write(json.dumps(data, indent=2, ensure_ascii=False))
        return tmp_file
    except:
        return traceback.format_exc()


@mcp.tool()
def update_list(
        site_url: str,
        list_id: str = None,
        list_name: str = None,
        data: list[dict[str, str]] = None
):
    """
    Update data to a sharepoint list.
    Args:
        site_url: The URL of the SharePoint Online site, e.g., 'https://yourcompany.sharepoint.com/sites/YourSite'.
        list_id (str, optional): The GUID (Globally Unique Identifier) of the SharePoint list.
                                 Example: '{{A1B2C3D4-E5F6-7890-1234-567890ABCDEF}}'.
                                 Mutually exclusive with `list_name`; only one should be provided.
        list_name (str, optional): The display name of the SharePoint list.
                                   Example: 'My Custom List'.
                                   Mutually exclusive with `list_id`; only one should be provided.
        data: Provide data like so and the ID must be set:
            data = [
                {'ID': 23, 'Title': 'Updated Title'},
                {'ID': 28, 'Col1': 'Updated Value'}
            ]

    Returns:

    """
    try:
        if list_id and list_name:
            raise Exception("Error: Both 'list_id' and 'list_name' were provided. Please select only one! 🤔")
        if not list_id and not list_name:
            raise Exception("Error: Either 'list_id' or 'list_name' must be provided! 🧐")
        if data is None:
            raise Exception(f"Error: The 'data' parameter must be provided!")

        site = init_site(site_url=site_url)

        g_id = list_id if list_id else list_name
        try:
            sp_list = site.List(g_id)
        except Exception as e:
            raise Exception(
                f"Error: Cannot access SharePoint list '{g_id}'! "
                f"Please ensure 'list_id' or 'list_name' is correct and you have permission to access it. Details: {e} 😟"
            )
        sp_list.update_list_items(data=data, kind="Update", mutate_data=True)
        return f"Update list successfully!"
    except:
        return traceback.format_exc()


if __name__ == '__main__':
    mcp.run(transport="stdio")
