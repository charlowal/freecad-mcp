"""Modelling, file and inspection tools.

partdesign, spreadsheet, export and validation are vendored from
spkane/freecad-addon-robust-mcp-server (MIT, see LICENSE-spkane) and fixed
for FreeCAD 1.1; files and inspection are written here. All run on this
addon's execute_code through :class:`ExecuteCodeBridge`. Every feature tool
checks that the solid changed as expected and undoes the feature otherwise;
every export reads its file back.
"""

import functools
from typing import Any, Callable

from .bridge import ExecuteCodeBridge
from .export import register_export_tools
from .files import register_file_tools
from .inspection import register_inspection_tools
from .partdesign import register_partdesign_tools
from .spreadsheet import register_spreadsheet_tools
from .validation import register_validation_tools

# Vendored tools left out: undo and execute_code already cover them.
SKIPPED_TOOLS = frozenset({"undo_if_invalid", "safe_execute"})

try:
    # mcp 2.x: only a ToolError's message reaches the client
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:
    from mcp.server.fastmcp.exceptions import ToolError


class _ReportErrors:
    """Wrap ``mcp`` so a tool's exception reaches the client as a ToolError.

    The tools raise ValueError with a hint ("retry with reversed=True");
    the MCP server would otherwise replace it with "Error executing tool".
    """

    def __init__(self, mcp: Any, skip: frozenset[str] = frozenset()) -> None:
        self._mcp = mcp
        self._skip = skip

    def tool(self, *args: Any, **kwargs: Any) -> Callable[[Callable[..., Any]], Any]:
        register = self._mcp.tool(*args, **kwargs)

        def decorate(fn: Callable[..., Any]) -> Any:
            if fn.__name__ in self._skip:
                return fn
            @functools.wraps(fn)
            async def wrapper(*fn_args: Any, **fn_kwargs: Any) -> Any:
                try:
                    return await fn(*fn_args, **fn_kwargs)
                except ToolError:
                    raise
                except Exception as exc:
                    raise ToolError(str(exc)) from exc

            return register(wrapper)

        return decorate


TOOL_MODULES = (
    register_partdesign_tools,
    register_spreadsheet_tools,
    register_export_tools,
    register_validation_tools,
    register_file_tools,
    register_inspection_tools,
)


def register_tools(mcp: Any, get_bridge: Callable[[], Any]) -> None:
    """Register every tool module on ``mcp`` with the given bridge."""
    registry = _ReportErrors(mcp, skip=SKIPPED_TOOLS)
    for register_module in TOOL_MODULES:
        register_module(registry, get_bridge)


def register_modelling_tools(mcp: Any, get_connection: Callable[[], Any]) -> None:
    """Register the tools on ``mcp``, talking to FreeCAD via ``get_connection``."""
    bridge = ExecuteCodeBridge(get_connection)

    async def get_bridge() -> ExecuteCodeBridge:
        return bridge

    register_tools(mcp, get_bridge)
