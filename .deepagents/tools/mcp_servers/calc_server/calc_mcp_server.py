import sys
import traceback
from fastmcp import FastMCP

mcp = FastMCP("Calculator Server")


@mcp.tool()
def calculate(expression: str) -> str:
    """
    calc the expression str

    Args:
        expression: expression str，egg: "2 + 3 * 4"

    Returns:
        the result of expression
    """
    try:
        result = eval(expression)
        return f"{result}"
    except:
        raise Exception(traceback.format_exc())


if __name__ == "__main__":
    # Belt-and-suspenders on top of the PYTHONIOENCODING=utf-8 env injected by
    # the parent: on Windows the default console encoding may be GBK/CP932,
    # which corrupts JSON-RPC over stdio. reconfigure() re-encodes the existing
    # streams in place (safer than re-wrapping fileno with open()).
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError, OSError):
            pass
    mcp.run(transport="stdio")
