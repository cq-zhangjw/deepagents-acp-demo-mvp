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
    mcp.run(transport="stdio")
