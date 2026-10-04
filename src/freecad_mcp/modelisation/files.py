"""Document and file tools: open, save, close, recompute, undo, redo, DXF.

Same conventions as the vendored modules: each tool sends a script through
``bridge.execute_python`` and reads ``_result_`` back. Paths go through
``_path_code`` so ~ means the user's home even in FreeCAD's snap.
"""

from collections.abc import Awaitable, Callable
from typing import Any

from .export import _path_code


def _document_code(doc_name: str | None) -> str:
    return f"""
doc = FreeCAD.ActiveDocument if {doc_name!r} is None else FreeCAD.getDocument({doc_name!r})
if doc is None:
    raise ValueError("No document found")

def _mark_saved(saved_doc):
    # A save through the App API leaves the GUI's Modified flag set
    try:
        import FreeCADGui
        FreeCADGui.getDocument(saved_doc.Name).Modified = False
    except Exception:
        pass
"""


def register_file_tools(mcp: Any, get_bridge: Callable[[], Awaitable[Any]]) -> None:
    """Register the document and DXF tools."""

    async def run(code: str, failure: str) -> dict[str, Any]:
        result = await (await get_bridge()).execute_python(code)
        if result.success:
            return result.result
        raise ValueError(result.error_traceback or failure)

    @mcp.tool()
    async def open_document(file_path: str) -> dict[str, Any]:
        """Open a FreeCAD document (.FCStd).

        Args:
            file_path: Absolute path, or one starting with ~.

        Returns:
            name, label, path and object_count of the opened document.
        """
        code = f"""
{_path_code(file_path, must_exist=True)}
doc = FreeCAD.openDocument(path)
_result_ = {{"name": doc.Name, "label": doc.Label, "path": doc.FileName, "object_count": len(doc.Objects)}}
"""
        return await run(code, "Open document failed")

    @mcp.tool()
    async def save_document(doc_name: str | None = None, file_path: str | None = None) -> dict[str, Any]:
        """Save a document, or save it under a new path.

        Args:
            doc_name: Document to save. Uses active document if None.
            file_path: New path (.FCStd). Required for a document never saved.

        Returns:
            name, path and bytes written.
        """
        new_path = _path_code(file_path) if file_path else "path = None\n"
        code = f"""
import os
{_document_code(doc_name)}
{new_path}
if path:
    if not path.lower().endswith(".fcstd"):
        path += ".FCStd"
    doc.saveAs(path)
elif doc.FileName:
    doc.save()
else:
    raise ValueError("This document was never saved: give file_path")
if not os.path.exists(doc.FileName):
    raise ValueError("FreeCAD reported a save but wrote no file at " + doc.FileName)
_mark_saved(doc)
_result_ = {{"name": doc.Name, "path": doc.FileName, "bytes": os.path.getsize(doc.FileName)}}
"""
        return await run(code, "Save document failed")

    @mcp.tool()
    async def close_document(
        doc_name: str | None = None, save: bool = False, discard_changes: bool = False
    ) -> dict[str, Any]:
        """Close a document.

        A document with unsaved changes is not closed unless ``save`` or
        ``discard_changes`` says what to do with them.

        Args:
            doc_name: Document to close. Uses active document if None.
            save: Save before closing (the document must have a path).
            discard_changes: Close even if unsaved changes would be lost.

        Returns:
            name and whether it was saved.
        """
        code = f"""
{_document_code(doc_name)}
_name = doc.Name
# Unsaved changes: the GUI's Modified flag; App's isTouched only means "needs a recompute"
_unsaved = bool(doc.Objects) and not doc.FileName
if not _unsaved:
    try:
        import FreeCADGui
        _unsaved = bool(FreeCADGui.getDocument(_name).Modified)
    except Exception:
        _unsaved = doc.isTouched() or any("Touched" in o.State for o in doc.Objects)
if {save!r}:
    if not doc.FileName:
        raise ValueError("Document " + _name + " was never saved: call save_document with a file_path first")
    doc.save()
    _mark_saved(doc)
elif _unsaved and not {discard_changes!r}:
    raise ValueError("Document " + _name + " has unsaved changes: pass save=True, or discard_changes=True to lose them")
FreeCAD.closeDocument(_name)
_result_ = {{"name": _name, "saved": {save!r}}}
"""
        return await run(code, "Close document failed")

    @mcp.tool()
    async def recompute_document(doc_name: str | None = None) -> dict[str, Any]:
        """Recompute a document and report the objects left in error.

        Args:
            doc_name: Document to recompute. Uses active document if None.

        Returns:
            recomputed count and the objects in error with their state.
        """
        code = f"""
{_document_code(doc_name)}
_count = doc.recompute()
_errors = [dict(name=o.Name, label=o.Label, state=list(o.State)) for o in doc.Objects
           if "Invalid" in o.State or "Error" in o.State]
_result_ = {{"name": doc.Name, "recomputed": _count, "objects_in_error": _errors}}
"""
        return await run(code, "Recompute failed")

    @mcp.tool()
    async def undo(doc_name: str | None = None, steps: int = 1) -> dict[str, Any]:
        """Undo the last operations of a document.

        Args:
            doc_name: Document. Uses active document if None.
            steps: Number of operations to undo.

        Returns:
            The undone operation names and what is left to undo and redo.
        """
        code = f"""
{_document_code(doc_name)}
if {steps} < 1 or doc.UndoCount < {steps}:
    raise ValueError("Cannot undo {steps} step(s): " + str(doc.UndoCount) + " available " + str(list(doc.UndoNames)))
_done = list(doc.UndoNames)[:{steps}]
for _i in range({steps}):
    doc.undo()
doc.recompute()
_result_ = {{"undone": _done, "undo_left": list(doc.UndoNames), "redo_available": list(doc.RedoNames)}}
"""
        return await run(code, "Undo failed")

    @mcp.tool()
    async def redo(doc_name: str | None = None, steps: int = 1) -> dict[str, Any]:
        """Redo operations undone in a document.

        Args:
            doc_name: Document. Uses active document if None.
            steps: Number of operations to redo.

        Returns:
            The redone operation names and what is left to undo and redo.
        """
        code = f"""
{_document_code(doc_name)}
if {steps} < 1 or doc.RedoCount < {steps}:
    raise ValueError("Cannot redo {steps} step(s): " + str(doc.RedoCount) + " available " + str(list(doc.RedoNames)))
_done = list(doc.RedoNames)[:{steps}]
for _i in range({steps}):
    doc.redo()
doc.recompute()
_result_ = {{"redone": _done, "undo_available": list(doc.UndoNames), "redo_left": list(doc.RedoNames)}}
"""
        return await run(code, "Redo failed")

    @mcp.tool()
    async def export_dxf(
        file_path: str,
        face: str | None = None,
        object_names: list[str] | None = None,
        doc_name: str | None = None,
    ) -> dict[str, Any]:
        """Export a flat profile to DXF, e.g. for laser or waterjet cutting.

        Give either a planar face, which is laid flat in XY with its outline
        and holes (the usual input for cutting a plate), or 2D objects such
        as sketches.

        Args:
            file_path: Path for the .dxf file (absolute, or starting with ~).
            face: A planar face as "Feature:FaceN", e.g. "Pad:Face6".
            object_names: 2D objects (sketches, Draft shapes) to export as they are.
            doc_name: Document. Uses active document if None.

        Returns:
            path, the DXF entities written (counted from the file) and, for a
            face, the flat size and area.
        """
        code = f"""
import Part
import importDXF
{_document_code(doc_name)}
{_path_code(file_path)}
_face_ref = {face!r}
_names = {object_names!r}
if bool(_face_ref) == bool(_names):
    raise ValueError("Give either face or object_names")
_temporary = None
_flat_info = None
if _face_ref:
    if ":" not in _face_ref:
        raise ValueError("face must look like Feature:FaceN, e.g. Pad:Face6")
    _owner_name, _sub = _face_ref.split(":", 1)
    _owner = doc.getObject(_owner_name)
    if _owner is None:
        raise ValueError("Object not found: " + _owner_name)
    _f = Part.getShape(_owner, _sub, needSubElement=True)
    if _f.ShapeType != "Face" or type(_f.Surface).__name__ != "Plane":
        raise ValueError(_face_ref + " is not a planar face")
    # Turn the face so its normal points along +Z, then move it to the origin
    _normal = _f.normalAt(0, 0)
    _flat = _f.copy()
    _flat.Placement = FreeCAD.Placement(FreeCAD.Vector(), FreeCAD.Rotation(_normal, FreeCAD.Vector(0, 0, 1))).multiply(_flat.Placement)
    _box = _flat.BoundBox
    _flat.translate(FreeCAD.Vector(-_box.XMin, -_box.YMin, -_box.ZMin))
    _box = _flat.BoundBox
    if _box.ZLength > 1e-6:
        raise ValueError("Could not lay " + _face_ref + " flat (thickness " + str(_box.ZLength) + ")")
    _temporary = doc.addObject("Part::Feature", "DxfFlatProfile")
    _temporary.Shape = _flat
    _objects = [_temporary]
    _flat_info = dict(width=round(_box.XLength, 6), height=round(_box.YLength, 6), area=round(_flat.Area, 6),
                      holes=len(_flat.Wires) - 1)
else:
    _objects = [doc.getObject(n) for n in _names]
    _missing = [n for n, o in zip(_names, _objects) if o is None]
    if _missing:
        raise ValueError("Objects not found: " + ", ".join(_missing))
try:
    importDXF.export(_objects, path)
finally:
    if _temporary is not None:
        doc.removeObject(_temporary.Name)
if not os.path.exists(path) or os.path.getsize(path) == 0:
    raise ValueError("Export wrote no DXF at " + path + "." + _snap_hint)
_lines = open(path, encoding="utf-8", errors="replace").read().splitlines()
_start = _lines.index("ENTITIES") if "ENTITIES" in _lines else -1
_end = _lines.index("ENDSEC", _start) if _start >= 0 else -1
_entities = dict()
for _k in range(_start, _end):
    if _lines[_k].strip() == "0" and _k + 1 < _end:
        _entities[_lines[_k + 1].strip()] = _entities.get(_lines[_k + 1].strip(), 0) + 1
if not _entities:
    raise ValueError("The DXF holds no entity: " + path)
_result_ = {{"path": path, "bytes": os.path.getsize(path), "entities": _entities, "flat": _flat_info}}
"""
        return await run(code, "DXF export failed")
