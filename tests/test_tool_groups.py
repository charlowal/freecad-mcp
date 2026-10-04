"""--tools / FREECAD_MCP_TOOLS keeps only the named tool groups.

keep_tool_groups removes tools from the module-level server, so each check
runs in its own interpreter and leaves the other tests' server intact.
"""

import json
import subprocess
import sys

PROBE = """
import asyncio, json, sys
from freecad_mcp import server
before = [t.name for t in asyncio.run(server.mcp.list_tools())]
try:
    kept = server.keep_tool_groups(sys.argv[1])
except ValueError as e:
    print(json.dumps({"error": str(e)}))
    raise SystemExit(0)
after = [t.name for t in asyncio.run(server.mcp.list_tools())]
print(json.dumps({"before": before, "kept": kept, "after": after, "groups": server.MODELLING_GROUPS}))
"""


def probe(spec: str) -> dict:
    out = subprocess.run([sys.executable, "-c", PROBE, spec], capture_output=True, text=True, check=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_base_and_one_group_keep_exactly_those_tools() -> None:
    r = probe("base, Drawing")
    drawing = set(r["groups"]["drawing"])
    modelling = {n for names in r["groups"].values() for n in names}
    base = {n for n in r["before"] if n not in modelling}
    assert "execute_code" in base and "get_view" in base
    assert set(r["after"]) == base | drawing == set(r["kept"])
    assert "pad_sketch" not in r["after"]


def test_every_group_is_named_after_its_module_and_all_groups_keep_everything() -> None:
    r = probe("base,partdesign,spreadsheet,export,validation,files,inspection,drawing,sheetmetal")
    assert set(r["groups"]) == {"partdesign", "spreadsheet", "export", "validation", "files", "inspection", "drawing", "sheetmetal"}
    assert sorted(r["after"]) == sorted(r["before"])


def test_an_unknown_group_is_refused_with_the_valid_names() -> None:
    r = probe("base,dessin")
    assert "dessin" in r["error"] and "drawing" in r["error"] and "base" in r["error"]


def test_the_command_line_refuses_an_unknown_group() -> None:
    out = subprocess.run([sys.executable, "-c", "import sys; from freecad_mcp.server import main; sys.argv = ['freecad-mcp', '--tools', 'nope']; main()"],
                         capture_output=True, text=True)
    assert out.returncode == 2 and "Unknown tool group(s) nope" in out.stderr
