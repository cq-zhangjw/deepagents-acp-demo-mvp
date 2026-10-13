"""实测 get_list_data（cqbfs tenant, 全字段读取）。入口=server.py"""
import asyncio, os, sys, time
from fastmcp import Client
from fastmcp.client.transports.stdio import PythonStdioTransport

SERVER_PATH = r"E:\my_projects\deepagents_acp\.deepagents\tools\mcp_servers\spo_server\spo_mcp_server.py"
SITE_URL = "https://cqbfs.sharepoint.com/sites/MyDevepoler981"
LIST_ID = "9c105529-0e90-4e19-9a50-d4d61c22fb5c"

async def main():
    client = Client(PythonStdioTransport(
        script_path=SERVER_PATH,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    ))
    import signal
    import threading

    def _overall_timeout():
        for _ in range(300):  # ~5 分钟
            time.sleep(1)
    t = threading.Thread(target=_overall_timeout, daemon=True); t.start()

    await client.__aenter__()
    try:
        print("=== calling get_list_data (cqbfs) ===")
        call = await client.call_tool("get_list_data", {
            "site_url": SITE_URL,   # str
            "list_id": LIST_ID,     # str
            # fields=[] 全字段；query=None op 默认
        })
        data = getattr(call, "data", None)
        if data is not None:
            print("RESULT:", data)
        else:
            for block in getattr(call, "content", None) or []:
                txt = getattr(block, "text", None)
                if txt:
                    print(txt)
    except Exception as e:
        import traceback; print("=== ERROR ==="); traceback.print_exc()
    finally:
        await client.close()
    print("=== done ===")

asyncio.run(main())
