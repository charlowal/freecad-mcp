import asyncio
import inspect
from typing import Any

import pytest

from freecad_mcp.modelisation import bridge as bridge_module
from freecad_mcp.modelisation.bridge import RESULT_MARK, ExecuteCodeBridge, ExecutionResult, find_result, wrap
from freecad_mcp.modelisation import SKIPPED_TOOLS, register_tools
from freecad_mcp.modelisation.partdesign import _check_solid


class FakeMCP:
    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self, *args: Any, **kwargs: Any):
        def register(fn):
            self.tools[fn.__name__] = fn
            return fn

        return register


class RecordingBridge:
    """Answers every script with a plausible result and keeps the scripts."""

    def __init__(self) -> None:
        self.scripts: list[str] = []

    async def execute_python(self, code: str, timeout_ms: int | None = None) -> ExecutionResult:
        self.scripts.append(code)
        return ExecutionResult(success=True, result={"name": "Obj", "label": "Obj", "type_id": "T"})


def _modelling_tools(bridge: Any) -> dict[str, Any]:
    mcp = FakeMCP()

    async def get_bridge():
        return bridge

    register_tools(mcp, get_bridge)
    return mcp.tools


def _sample(annotation: Any) -> Any:
    text = str(annotation)
    if "list[list" in text:
        return [[0.0, 0.0], [10.0, 5.0], [20.0, 0.0]]
    if "list[float]" in text:
        return [1.0, 2.0, 3.0]
    if "list[str]" in text:
        return ["Sketch", "Sketch001"]
    if annotation is bool:
        return False
    if annotation is int:
        return 1
    if annotation is float or "float" in text:
        return 2.5
    return "Obj"


def _required_args(fn: Any) -> dict[str, Any]:
    return {
        name: _sample(p.annotation)
        for name, p in inspect.signature(fn).parameters.items()
        if p.default is inspect.Parameter.empty
    }


def test_registers_80_tools_and_skips_the_redundant_ones() -> None:
    tools = _modelling_tools(RecordingBridge())
    assert len(tools) == 80
    assert not SKIPPED_TOOLS & set(tools)


def test_server_registers_modelling_tools_beside_the_originals() -> None:
    from freecad_mcp import server

    names = [t.name for t in asyncio.run(server.mcp.list_tools())]
    assert len(names) == len(set(names)) == 97
    assert {"execute_code", "get_view", "pad_sketch", "constrain_angle", "spreadsheet_bind_property",
            "export_step", "export_dxf", "get_topology", "mass_properties", "undo"} <= set(names)


def test_server_exposes_the_input_schema_of_the_wrapped_tools() -> None:
    from freecad_mcp import server

    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    schema = tools["pocket_sketch"].input_schema
    assert {"sketch_name", "length", "reversed"} <= set(schema["properties"])
    assert set(schema["required"]) == {"sketch_name", "length"}


def test_a_tool_error_reaches_the_client_with_its_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    from freecad_mcp import server

    hint = "Pocket removed no material; it was undone. Retry with reversed=True"
    monkeypatch.setattr(server.state, "freecad_connection", FakeConnection([{"success": False, "error": "ValueError: " + hint}]))
    with pytest.raises(Exception) as raised:
        asyncio.run(server.mcp.call_tool("pocket_sketch", {"sketch_name": "Sketch001", "length": 2}))
    assert "reversed=True" in str(raised.value)
    assert type(raised.value).__name__ == "ToolError"  # not the generic UnexpectedToolError


@pytest.mark.parametrize("name", sorted(_modelling_tools(RecordingBridge())))
def test_every_tool_script_compiles(name: str) -> None:
    bridge = RecordingBridge()
    fn = _modelling_tools(bridge)[name]
    try:
        asyncio.run(fn(**_required_args(fn)))
    except (KeyError, TypeError, ValueError):
        pass  # the canned result does not fit every tool; the scripts are what we test
    assert bridge.scripts, f"{name} sent no script"
    for script in bridge.scripts:
        compile(wrap(script), name, "exec")


@pytest.mark.parametrize("indent", [4, 8])
def test_check_snippet_nests_in_a_transaction_block(indent: int) -> None:
    opener = "    if body:\n" if indent == 8 else ""
    code = (
        "try:\n"
        + opener
        + " " * indent + "pad = make()\n"
        + _check_solid("pad", "add", "hint", indent=indent)
        + "    doc.commitTransaction()\n"
        "except Exception:\n"
        "    raise\n"
    )
    compile(code, "snippet", "exec")


def test_find_result_reads_the_marked_line() -> None:
    assert find_result(f"Python code executed successfully.\nOutput: {RESULT_MARK}" + '{"a": 1}\n') == (True, {"a": 1})
    assert find_result("Output: nothing") == (False, None)


class FakeConnection:
    def __init__(self, replies: list[dict[str, Any]]) -> None:
        self.replies = replies
        self.calls: list[str] = []

    def execute_code(self, code: str, timeout: float | None = None) -> dict[str, Any]:
        self.calls.append(code)
        return self.replies.pop(0)


def _run(connection: FakeConnection) -> ExecutionResult:
    return asyncio.run(ExecuteCodeBridge(lambda: connection).execute_python("_result_ = 1"))


def test_bridge_returns_the_result() -> None:
    conn = FakeConnection([{"success": True, "message": f"Output: {RESULT_MARK}" + '{"name": "Pad"}'}])
    res = _run(conn)
    assert res.success and res.result == {"name": "Pad"} and len(conn.calls) == 1


def test_bridge_fetches_a_lost_result_without_rerunning_the_tool() -> None:
    conn = FakeConnection([
        {"success": True, "message": "Output: "},
        {"success": True, "message": f"Output: {RESULT_MARK}" + '{"name": "Pad"}'},
    ])
    res = _run(conn)
    assert res.success and res.result == {"name": "Pad"}
    assert bridge_module.RESULT_VARIABLE in conn.calls[1] and "_result_ = 1" not in conn.calls[1]


def test_bridge_gives_up_after_the_refetches() -> None:
    conn = FakeConnection([{"success": True, "message": "Output: "}] * (1 + bridge_module.REFETCH_ATTEMPTS))
    res = _run(conn)
    assert not res.success and "result was lost" in res.error_traceback


def test_bridge_reports_the_addon_error() -> None:
    conn = FakeConnection([{"success": False, "error": "ValueError: Pocket removed no material"}])
    res = _run(conn)
    assert not res.success and "removed no material" in res.error_traceback
