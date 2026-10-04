"""A base tool's failure reaches the client with its reason, not "Error executing tool"."""

import xmlrpc.client

import pytest

from freecad_mcp import server


class BusyFreeCAD:
    def list_documents(self):
        raise xmlrpc.client.Fault(1, "GUI_DISPATCH_FAILED: GUI dispatch gave up after 60.0s waiting for 'list_documents' to start")


def test_a_failing_base_tool_raises_a_tool_error_with_the_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "get_freecad_connection", lambda: BusyFreeCAD())
    with pytest.raises(server.ToolError) as caught:
        server.list_documents(None)
    assert "GUI_DISPATCH_FAILED" in str(caught.value) and "Fault" in str(caught.value)
