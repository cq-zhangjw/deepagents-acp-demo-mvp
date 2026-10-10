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
    import sys
    sys.stdin = open(sys.stdin.fileno(), "r", encoding="utf-8", closefd=False)
    sys.stdout = open(sys.stdout.fileno(), "w", encoding="utf-8", closefd=False)

    mcp.run(transport="stdio")
