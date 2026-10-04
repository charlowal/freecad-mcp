"""Run the vendored modelling tools on this addon's execute_code.

The PartDesign and Spreadsheet tools build a Python script that stores its
answer in ``_result_`` and pass it to ``bridge.execute_python``. This bridge
sends the script to the addon's ``execute_code`` and reads ``_result_`` back
from the printed output. The JSON is also kept in FreeCAD's script namespace,
so it can be fetched again when the printed output is lost (issue #55)
instead of running the tool a second time.
"""

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Callable

RESULT_MARK = "__freecad_mcp_result__"
RESULT_VARIABLE = "_freecad_mcp_result"
REFETCH_ATTEMPTS = 2


@dataclass
class ExecutionResult:
    success: bool
    result: Any = None
    stdout: str = ""
    stderr: str = ""
    execution_time_ms: float = 0.0
    error_type: str | None = None
    error_traceback: str | None = None


def wrap(code: str) -> str:
    """Surround a tool script so it reports ``_result_`` and any verification."""
    return (
        "_result_ = None\n"
        "_verification = None\n"
        + code
        + "\nimport json as _json\n"
        "if isinstance(_result_, dict) and _verification is not None:\n"
        "    _result_['verification'] = _verification\n"
        f"{RESULT_VARIABLE} = _json.dumps(_result_, default=str)\n"
        f"print({RESULT_MARK!r} + {RESULT_VARIABLE})\n"
    )


def find_result(output: str) -> tuple[bool, Any]:
    """Return ``(found, value)`` for the marked JSON line in ``output``."""
    for line in output.splitlines():
        if RESULT_MARK in line:
            return True, json.loads(line.split(RESULT_MARK, 1)[1])
    return False, None


class ExecuteCodeBridge:
    """The ``execute_python`` interface of the vendored tools, on execute_code."""

    def __init__(self, get_connection: Callable[[], Any]) -> None:
        self._get_connection = get_connection

    async def execute_python(self, code: str, timeout_ms: int | None = None) -> ExecutionResult:
        connection = self._get_connection()
        # Without a timeout the addon's own default budget (90 s) applies.
        timeout = timeout_ms / 1000 if timeout_ms else None
        reply = await asyncio.to_thread(connection.execute_code, wrap(code), timeout)
        if not reply.get("success"):
            error = str(reply.get("error") or reply.get("message") or "execute_code failed")
            return ExecutionResult(
                success=False, stderr=error, error_type="ScriptError", error_traceback=error
            )
        output = reply.get("message", "")
        found, value = find_result(output)
        attempts = 0
        while not found and attempts < REFETCH_ATTEMPTS:
            attempts += 1
            again = await asyncio.to_thread(
                connection.execute_code, f"print({RESULT_MARK!r} + {RESULT_VARIABLE})"
            )
            found, value = find_result(again.get("message", "")) if again.get("success") else (False, None)
        if not found:
            error = (
                "FreeCAD ran the tool but its result was lost; check the model "
                "with get_objects before retrying"
            )
            return ExecutionResult(success=False, stdout=output, stderr=error, error_traceback=error)
        return ExecutionResult(success=True, result=value, stdout=output)
