"""Weigh the tool schemas of each group (and of any --tools spec) in tokens.

Lists the tools of the server object without starting it on stdio. A tool's
weight is its name + description + inputSchema as JSON; tokens are estimated
at 4 characters per token.

    python scripts/tool_weights.py                 # every group and profile
    python scripts/tool_weights.py "@modelisation,-spreadsheet"
"""

import asyncio
import json
import sys

from freecad_mcp import server


def weigh(tool) -> int:
    return len(tool.name) + len(tool.description or "") + len(json.dumps(getattr(tool, "inputSchema", None) or tool.input_schema, ensure_ascii=False))


def main() -> None:
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    weight = {n: weigh(t) for n, t in tools.items()}
    groups = server.tool_groups(list(tools))

    def line(label: str, names: list[str]) -> str:
        chars = sum(weight[n] for n in names)
        return f"{label:<16}{len(names):>4} tools{chars:>9} chars{round(chars / 4):>8} tokens"

    if len(sys.argv) > 1:
        kept = server.keep_tool_groups(sys.argv[1])
        print(line(sys.argv[1], kept))
        return
    for name, names in groups.items():
        print(line(name, names))
    print(line("total", list(tools)))
    for name in server.TOOL_PROFILES:
        names = server.resolve_tool_spec("@" + name, groups)
        print(line("@" + name, names))


if __name__ == "__main__":
    main()
