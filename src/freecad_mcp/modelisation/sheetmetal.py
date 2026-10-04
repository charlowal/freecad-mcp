"""Sheet metal tools, on top of the SheetMetal workbench (shaise/FreeCAD_SheetMetal).

The workbench is a FreeCAD addon, installed separately (Addon Manager). A
tool finds it in FreeCAD's Mod folder even when it was installed after
FreeCAD started. Same conventions as the other modules: a script goes
through ``bridge.execute_python`` and leaves its answer in ``_result_``.
"""

from collections.abc import Awaitable, Callable
from typing import Any

_LOAD_WORKBENCH = """
import os
import sys
try:
    import SheetMetalUnfoldCmd
except ImportError:
    # Installed after FreeCAD started: its folder is not on sys.path yet
    _folder = os.path.join(FreeCAD.getUserAppDataDir(), "Mod", "SheetMetal")
    if not os.path.isdir(_folder):
        raise ValueError("The SheetMetal workbench is not installed: add it with FreeCAD's Addon Manager (Tools > Addon manager > SheetMetal)")
    sys.path.append(_folder)
    import SheetMetalUnfoldCmd
"""


def register_sheetmetal_tools(mcp: Any, get_bridge: Callable[[], Awaitable[Any]]) -> None:
    """Register the sheet metal tools."""

    @mcp.tool()
    async def sheetmetal_unfold(
        object_name: str,
        face: str | None = None,
        k_factor: float = 0.4,
        k_factor_standard: str = "ansi",
        generate_sketches: bool = True,
        doc_name: str | None = None,
    ) -> dict[str, Any]:
        """Unfold a sheet metal part (constant thickness with bends) to its flat pattern.

        Works on any solid of constant thickness whose bends are cylindrical,
        e.g. a PartDesign body built from a profile with inner and outer bend
        radii, or a part made with the SheetMetal workbench.

        Args:
            object_name: Body or feature holding the folded part.
            face: Planar face that stays fixed, e.g. "Face3". The largest
                planar face if None.
            k_factor: Position of the neutral fibre, 0 to 2.
            k_factor_standard: "ansi" (neutral fibre at K x thickness from
                the inner surface) or "din" (at K x thickness / 2).
            generate_sketches: Also make sketches of the outline, holes and
                bend lines, ready for export_dxf(object_names=...).
            doc_name: Document. Uses active document if None.

        Returns:
            The unfold object, the fixed face, thickness, the flat face to
            pass to export_dxf, its size and area, the bend count and the
            generated sketch names.
        """
        code = f"""
import Part
{_LOAD_WORKBENCH}
doc = FreeCAD.ActiveDocument if {doc_name is None} else FreeCAD.getDocument({doc_name!r})
if doc is None:
    raise ValueError("No document found")
_base = doc.getObject({object_name!r})
if _base is None:
    raise ValueError("Object not found: " + {object_name!r})
_shape = _base.Shape
if _shape.isNull() or len(_shape.Solids) != 1:
    raise ValueError({object_name!r} + " must hold exactly one solid to unfold")
if {k_factor_standard!r} not in ("ansi", "din"):
    raise ValueError("k_factor_standard is ansi or din")
if not 0 <= {k_factor} <= 2:
    raise ValueError("k_factor must be between 0 and 2")
_planes = [(i, f) for i, f in enumerate(_shape.Faces) if type(f.Surface).__name__ == "Plane"]
if {face!r}:
    _root = {face!r}
    _index = int(_root[4:]) - 1 if _root.startswith("Face") and _root[4:].isdigit() else -1
    if not 0 <= _index < len(_shape.Faces) or type(_shape.Faces[_index].Surface).__name__ != "Plane":
        raise ValueError(_root + " is not a planar face of " + {object_name!r})
else:
    _root = "Face" + str(max(_planes, key=lambda p: p[1].Area)[0] + 1)
_bends = sum(1 for f in _shape.Faces if type(f.Surface).__name__ == "Cylinder") // 2

doc.openTransaction("Unfold " + _base.Name)
try:
    _unfold = doc.addObject("Part::FeaturePython", _base.Name + "_Unfold")
    SheetMetalUnfoldCmd.SMUnfold(_unfold, _base, [_root])
    _unfold.KFactor = {k_factor}
    _unfold.KFactorStandard = {k_factor_standard!r}
    _unfold.GenerateSketch = {generate_sketches}
    if FreeCAD.GuiUp:
        SheetMetalUnfoldCmd.SMUnfoldViewProvider(_unfold.ViewObject)
    doc.recompute()
    _flat = _unfold.Shape
    if _flat.isNull() or "Invalid" in _unfold.State or len(_flat.Solids) != 1:
        raise ValueError("SheetMetal could not unfold " + _base.Name + " from " + _root + " (state " + str(list(_unfold.State)) + "): check that the part has a constant thickness and cylindrical bends")
    _big = max(range(len(_flat.Faces)), key=lambda i: _flat.Faces[i].Area)
    _face = _flat.Faces[_big]
    _thickness = _flat.Volume / _face.Area
    _laid = _face.copy()
    _laid.Placement = FreeCAD.Placement(FreeCAD.Vector(), FreeCAD.Rotation(_face.normalAt(0, 0), FreeCAD.Vector(0, 0, 1))).multiply(_laid.Placement)
    _box = _laid.BoundBox
    doc.commitTransaction()
except Exception:
    doc.abortTransaction()
    for _name in [o.Name for o in doc.Objects if o.Name.startswith(_base.Name + "_Unfold")]:
        try:
            doc.removeObject(_name)
        except Exception:
            pass
    raise
_result_ = {{
    "name": _unfold.Name,
    "fixed_face": _root,
    "k_factor": _unfold.KFactor,
    "k_factor_standard": _unfold.KFactorStandard,
    "bends": _bends,
    "thickness": round(_thickness, 6),
    "flat_face": _unfold.Name + ":Face" + str(_big + 1),
    "flat_size": [round(_box.XLength, 6), round(_box.YLength, 6)],
    "flat_area": round(_face.Area, 6),
    "holes": len(_face.Wires) - 1,
    "sketches": list(getattr(_unfold, "UnfoldSketches", []) or []),
    "next": "export_dxf(face=flat_face) writes the outline and holes; export_dxf(object_names=sketches) adds the bend lines",
}}
"""
        result = await (await get_bridge()).execute_python(code)
        if result.success:
            return result.result
        raise ValueError(result.error_traceback or "Unfold failed")
