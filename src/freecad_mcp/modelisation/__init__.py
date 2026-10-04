"""Parametric modelling tools: PartDesign, Sketcher and Spreadsheet.

The tool modules are vendored from spkane/freecad-addon-robust-mcp-server
(MIT, see LICENSE-spkane), fixed for FreeCAD 1.1, and run on this addon's
execute_code through :class:`ExecuteCodeBridge`. Every feature tool checks
that the solid changed as expected and undoes the feature otherwise.
"""

import functools
from typing import Any, Callable

from .bridge import ExecuteCodeBridge
from .partdesign import register_partdesign_tools
from .spreadsheet import register_spreadsheet_tools

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

    def __init__(self, mcp: Any) -> None:
        self._mcp = mcp

    def tool(self, *args: Any, **kwargs: Any) -> Callable[[Callable[..., Any]], Any]:
        register = self._mcp.tool(*args, **kwargs)

        def decorate(fn: Callable[..., Any]) -> Any:
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


def register_modelling_tools(mcp: Any, get_connection: Callable[[], Any]) -> None:
    """Register the modelling tools on ``mcp``, talking to FreeCAD via ``get_connection``."""
    bridge = ExecuteCodeBridge(get_connection)

    async def get_bridge() -> ExecuteCodeBridge:
        return bridge

    register_partdesign_tools(_ReportErrors(mcp), get_bridge)
    register_spreadsheet_tools(_ReportErrors(mcp), get_bridge)
