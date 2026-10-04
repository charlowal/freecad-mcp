"""Export tools for FreeCAD Robust MCP Server.

This module provides tools for exporting FreeCAD documents and objects
to various file formats: STEP, STL, 3MF, OBJ, IGES, and FreeCAD native.

Vendored from spkane/freecad-addon-robust-mcp-server (MIT, see
LICENSE-spkane) and adapted to run on this addon's execute_code. Changes:
by default the finished solids are exported (bodies and standalone solids,
with their global placement), not every visible object, which doubled a
body's solid and added origin planes and sketches; paths starting with ~
resolve to the user's home even in FreeCAD's snap; every export reads its
file back and reports what it holds, and a STEP or IGES whose solids do not
match the source is an error.
"""

from collections.abc import Awaitable, Callable
from typing import Any


def _build_object_selection_code(object_names: list[str] | None) -> str:
    """Generate Python code that selects the objects to export.

    Named objects are exported as given. Otherwise the finished solids are
    taken: PartDesign bodies and standalone solid features, but not the
    features inside a body (the body holds its tip), not origin planes,
    sketches or datums, and not inputs consumed by another feature.
    Shapes are taken with their global placement (Part.getShape).
    """
    return f"""
import Part
if {object_names is not None}:
    objects = [doc.getObject(n) for n in {object_names!r}]
    missing = [n for n, o in zip({object_names!r}, objects) if o is None]
    if missing:
        raise ValueError("Objects not found: " + ", ".join(missing))
else:
    def _exportable(obj):
        if not hasattr(obj, "Shape") or obj.Shape.isNull() or not obj.Shape.Solids:
            return False
        if obj.TypeId.startswith(("App::", "Sketcher::")):
            return False
        group = obj.getParentGeoFeatureGroup()
        if group is not None and group.TypeId == "PartDesign::Body":
            return False
        return not any(hasattr(p, "Shape") and p.TypeId != "PartDesign::Body" and not p.TypeId.startswith("App::") for p in obj.InList)
    objects = [obj for obj in doc.Objects if _exportable(obj)]
if not objects:
    raise ValueError("No solid to export; pass object_names")
shapes = [Part.getShape(obj) for obj in objects]
"""


def _path_code(file_path: str, must_exist: bool = False) -> str:
    """Generate Python code that sets ``path`` from ``file_path``.

    A path starting with ~ resolves to the user's home: FreeCAD installed as
    a snap has its own HOME, and SNAP_REAL_HOME is the user's.
    """
    return f"""
import os
path = {file_path!r}
if path.startswith("~"):
    path = os.environ.get("SNAP_REAL_HOME", os.path.expanduser("~")) + path[1:]
_snap_hint = (" FreeCAD installed as a snap only reaches your home folder, outside hidden folders (no /tmp, no ~/.cache)."
              if "SNAP" in os.environ else "")
if not os.path.isabs(path):
    raise ValueError("Give an absolute path, or one starting with ~: " + path)
if "SNAP" in os.environ and os.path.realpath(path).startswith(("/tmp/", "/var/tmp/")):
    raise ValueError("In FreeCAD's snap, " + path + " lands in a /tmp private to FreeCAD, where you would not find it; write under your home folder")
if {must_exist!r} and not os.path.exists(path):
    raise FileNotFoundError("File not found: " + path + "." + _snap_hint)
if not {must_exist!r} and not os.path.isdir(os.path.dirname(path)):
    raise FileNotFoundError("Folder not found: " + os.path.dirname(path) + "." + _snap_hint)
"""


def _read_back_code(kind: str) -> str:
    """Generate Python code that reads the written file back into ``_file_check``."""
    return f"""
if not os.path.exists(path) or os.path.getsize(path) == 0:
    raise ValueError("Export wrote no file at " + path + "." + _snap_hint)
if {kind!r} == "iges":
    # IGES usually keeps the faces but not the solids: compare the area
    _back = Part.Shape()
    _back.read(path)
    _source_area = sum(s.Area for s in shapes)
    _file_check = dict(bytes=os.path.getsize(path), faces=len(_back.Faces), solids=len(_back.Solids),
                       area=round(_back.Area, 6), source_area=round(_source_area, 6))
    if not _back.Faces or abs(_back.Area - _source_area) > 1e-3 * max(1.0, _source_area):
        raise ValueError("The exported IGES does not hold the source faces (area " + str(round(_back.Area, 3)) + " instead of " + str(round(_source_area, 3)) + "): " + path)
elif {kind!r} == "brep":
    _back = Part.Shape()
    _back.read(path)
    _source_solids = sum(len(s.Solids) for s in shapes)
    _file_check = dict(bytes=os.path.getsize(path), solids=len(_back.Solids), volume=round(_back.Volume, 6),
                       source_solids=_source_solids, source_volume=round(sum(s.Volume for s in shapes), 6))
    if _file_check["solids"] != _source_solids:
        raise ValueError("The exported file holds " + str(_file_check["solids"]) + " solids instead of " + str(_source_solids) + ": " + path)
else:
    import Mesh as _Mesh
    _back = _Mesh.Mesh(path)
    _file_check = dict(bytes=os.path.getsize(path), facets=_back.CountFacets, closed=_back.isSolid())
    if _back.CountFacets == 0:
        raise ValueError("The exported mesh is empty: " + path)
"""


def register_export_tools(mcp: Any, get_bridge: Callable[[], Awaitable[Any]]) -> None:
    """Register export-related tools with the Robust MCP Server.

    Args:
        mcp: The FastMCP (Robust MCP Server) instance (Any due to lack of stubs).
        get_bridge: Async function returning the active bridge connection.
    """

    @mcp.tool()
    async def export_step(
        file_path: str,
        object_names: list[str] | None = None,
        doc_name: str | None = None,
    ) -> dict[str, Any]:
        """Export objects to STEP format.

        STEP (Standard for the Exchange of Product Data) is an ISO standard
        for CAD data exchange, widely supported by CAD software.

        Args:
            file_path: Path for the output .step file.
            object_names: List of object names to export. Exports the finished
                solids (bodies and standalone solids) if None.
            doc_name: Document to export from. Uses active document if None.

        Returns:
            Dictionary with export result:
                - success: Whether export was successful
                - path: Path to exported file
                - object_count: Number of objects exported
        """
        bridge = await get_bridge()

        code = f"""
import Part

doc = FreeCAD.ActiveDocument if {doc_name is None} else FreeCAD.getDocument({doc_name!r})
if doc is None:
    raise ValueError("No document found")
{_path_code(file_path)}{_build_object_selection_code(object_names)}
# Combine shapes
if len(shapes) == 1:
    shape = shapes[0]
else:
    shape = Part.makeCompound(shapes)

shape.exportStep(path)
{_read_back_code('brep')}
_result_ = {{
    "success": True,
    "path": path,
    "object_count": len(objects),
    "objects": [obj.Name for obj in objects],
    "check": _file_check,
}}
"""
        result = await bridge.execute_python(code)
        if result.success:
            return result.result
        raise ValueError(result.error_traceback or "STEP export failed")

    @mcp.tool()
    async def export_stl(
        file_path: str,
        object_names: list[str] | None = None,
        doc_name: str | None = None,
        mesh_tolerance: float = 0.1,
    ) -> dict[str, Any]:
        """Export objects to STL format.

        STL (Stereolithography) is commonly used for 3D printing and
        rapid prototyping. It represents surfaces as triangular meshes.

        Args:
            file_path: Path for the output .stl file.
            object_names: List of object names to export. Exports the finished
                solids (bodies and standalone solids) if None.
            doc_name: Document to export from. Uses active document if None.
            mesh_tolerance: Mesh approximation tolerance. Lower = finer mesh.

        Returns:
            Dictionary with export result:
                - success: Whether export was successful
                - path: Path to exported file
                - object_count: Number of objects exported
        """
        bridge = await get_bridge()

        code = f"""
import Mesh
import MeshPart
import Part

doc = FreeCAD.ActiveDocument if {doc_name is None} else FreeCAD.getDocument({doc_name!r})
if doc is None:
    raise ValueError("No document found")
{_path_code(file_path)}{_build_object_selection_code(object_names)}
# Create mesh from shapes using MeshPart (more reliable than manual tessellation)
meshes = []
for shape in shapes:
    mesh = MeshPart.meshFromShape(shape, LinearDeflection={mesh_tolerance})
    meshes.append(mesh)

# Combine meshes
if len(meshes) == 1:
    final_mesh = meshes[0]
else:
    final_mesh = Mesh.Mesh()
    for m in meshes:
        final_mesh.addMesh(m)

final_mesh.write(path)
{_read_back_code('mesh')}
_result_ = {{
    "success": True,
    "path": path,
    "object_count": len(objects),
    "objects": [obj.Name for obj in objects],
    "check": _file_check,
}}
"""
        result = await bridge.execute_python(code)
        if result.success:
            return result.result
        raise ValueError(result.error_traceback or "STL export failed")

    @mcp.tool()
    async def export_3mf(
        file_path: str,
        object_names: list[str] | None = None,
        doc_name: str | None = None,
        mesh_tolerance: float = 0.1,
    ) -> dict[str, Any]:
        """Export objects to 3MF format.

        3MF (3D Manufacturing Format) is a modern 3D printing format that
        supports richer data than STL, including colors, materials, and
        print settings. It is increasingly preferred over STL for 3D printing.

        Args:
            file_path: Path for the output .3mf file.
            object_names: List of object names to export. Exports the finished
                solids (bodies and standalone solids) if None.
            doc_name: Document to export from. Uses active document if None.
            mesh_tolerance: Mesh approximation tolerance. Lower = finer mesh.

        Returns:
            Dictionary with export result:
                - success: Whether export was successful
                - path: Path to exported file
                - object_count: Number of objects exported
        """
        bridge = await get_bridge()

        code = f"""
import Mesh
import MeshPart
import Part

doc = FreeCAD.ActiveDocument if {doc_name is None} else FreeCAD.getDocument({doc_name!r})
if doc is None:
    raise ValueError("No document found")
{_path_code(file_path)}{_build_object_selection_code(object_names)}
# Create mesh from shapes using MeshPart (more reliable than manual tessellation)
meshes = []
for shape in shapes:
    mesh = MeshPart.meshFromShape(shape, LinearDeflection={mesh_tolerance})
    meshes.append(mesh)

# Combine meshes
if len(meshes) == 1:
    final_mesh = meshes[0]
else:
    final_mesh = Mesh.Mesh()
    for m in meshes:
        final_mesh.addMesh(m)

# Export to 3MF format
final_mesh.write(path)
{_read_back_code('mesh')}
_result_ = {{
    "success": True,
    "path": path,
    "object_count": len(objects),
    "objects": [obj.Name for obj in objects],
    "check": _file_check,
}}
"""
        result = await bridge.execute_python(code)
        if result.success:
            return result.result
        raise ValueError(result.error_traceback or "3MF export failed")

    @mcp.tool()
    async def export_obj(
        file_path: str,
        object_names: list[str] | None = None,
        doc_name: str | None = None,
        mesh_tolerance: float = 0.1,
    ) -> dict[str, Any]:
        """Export objects to OBJ format.

        OBJ (Wavefront) is a common 3D model format supported by many
        3D graphics applications and game engines.

        Args:
            file_path: Path for the output .obj file.
            object_names: List of object names to export. Exports the finished
                solids (bodies and standalone solids) if None.
            doc_name: Document to export from. Uses active document if None.
            mesh_tolerance: Mesh approximation tolerance. Lower = finer mesh.

        Returns:
            Dictionary with export result:
                - success: Whether export was successful
                - path: Path to exported file
                - object_count: Number of objects exported
        """
        bridge = await get_bridge()

        code = f"""
import Mesh
import MeshPart

doc = FreeCAD.ActiveDocument if {doc_name is None} else FreeCAD.getDocument({doc_name!r})
if doc is None:
    raise ValueError("No document found")
{_path_code(file_path)}{_build_object_selection_code(object_names)}
# Create mesh from shapes using MeshPart (more reliable than manual tessellation)
meshes = []
for shape in shapes:
    mesh = MeshPart.meshFromShape(shape, LinearDeflection={mesh_tolerance})
    meshes.append(mesh)

# Combine meshes
if len(meshes) == 1:
    final_mesh = meshes[0]
else:
    final_mesh = Mesh.Mesh()
    for m in meshes:
        final_mesh.addMesh(m)

final_mesh.write(path)
{_read_back_code('mesh')}
_result_ = {{
    "success": True,
    "path": path,
    "object_count": len(objects),
    "objects": [obj.Name for obj in objects],
    "check": _file_check,
}}
"""
        result = await bridge.execute_python(code)
        if result.success:
            return result.result
        raise ValueError(result.error_traceback or "OBJ export failed")

    @mcp.tool()
    async def export_iges(
        file_path: str,
        object_names: list[str] | None = None,
        doc_name: str | None = None,
    ) -> dict[str, Any]:
        """Export objects to IGES format.

        IGES (Initial Graphics Exchange Specification) is an older but still
        widely supported CAD data exchange format.

        Args:
            file_path: Path for the output .iges file.
            object_names: List of object names to export. Exports the finished
                solids (bodies and standalone solids) if None.
            doc_name: Document to export from. Uses active document if None.

        Returns:
            Dictionary with export result:
                - success: Whether export was successful
                - path: Path to exported file
                - object_count: Number of objects exported
        """
        bridge = await get_bridge()

        code = f"""
import Part

doc = FreeCAD.ActiveDocument if {doc_name is None} else FreeCAD.getDocument({doc_name!r})
if doc is None:
    raise ValueError("No document found")
{_path_code(file_path)}{_build_object_selection_code(object_names)}
# Combine shapes
if len(shapes) == 1:
    shape = shapes[0]
else:
    shape = Part.makeCompound(shapes)

shape.exportIges(path)
{_read_back_code('iges')}
_result_ = {{
    "success": True,
    "path": path,
    "object_count": len(objects),
    "objects": [obj.Name for obj in objects],
    "check": _file_check,
}}
"""
        result = await bridge.execute_python(code)
        if result.success:
            return result.result
        raise ValueError(result.error_traceback or "IGES export failed")

    @mcp.tool()
    async def import_step(
        file_path: str,
        doc_name: str | None = None,
    ) -> dict[str, Any]:
        """Import a STEP file into FreeCAD.

        Args:
            file_path: Path to the .step file to import.
            doc_name: Document to import into. Creates new if None.

        Returns:
            Dictionary with import result:
                - success: Whether import was successful
                - document: Name of document containing imported objects
                - objects: List of imported object names
        """
        bridge = await get_bridge()

        code = f"""
import Part
import os

{_path_code(file_path, must_exist=True)}
doc = FreeCAD.ActiveDocument if {doc_name is None} else FreeCAD.getDocument({doc_name!r})
if doc is None:
    doc = FreeCAD.newDocument("Imported")

# Get object count before import
before_count = len(doc.Objects)

try:
    import ImportGui as _importer  # keeps names and colours; Part.insert is deprecated in 1.1
except ImportError:
    import Import as _importer
_importer.insert(path, doc.Name)
doc.recompute()

# Get new objects
new_objects = [obj.Name for obj in doc.Objects[before_count:]]

if not new_objects:
    raise ValueError("The STEP file added no object: " + path)
# Count the leaf shapes only: an assembly comes in as App::Part containers
# whose shape repeats their children's.
_leaves = [o for o in (doc.getObject(n) for n in new_objects)
           if hasattr(o, "Shape") and not o.Shape.isNull()
           and not o.hasExtension("App::GroupExtension") and not o.hasExtension("App::GeoFeatureGroupExtension")]
_result_ = {{
    "success": True,
    "document": doc.Name,
    "objects": new_objects,
    "solids": sum(len(o.Shape.Solids) for o in _leaves),
    "volume": round(sum(o.Shape.Volume for o in _leaves if o.Shape.Solids), 6),
}}
"""
        result = await bridge.execute_python(code)
        if result.success:
            return result.result
        raise ValueError(result.error_traceback or "STEP import failed")

    @mcp.tool()
    async def import_stl(
        file_path: str,
        doc_name: str | None = None,
    ) -> dict[str, Any]:
        """Import an STL file into FreeCAD.

        Args:
            file_path: Path to the .stl file to import.
            doc_name: Document to import into. Creates new if None.

        Returns:
            Dictionary with import result:
                - success: Whether import was successful
                - document: Name of document containing imported object
                - object: Name of imported mesh object
        """
        bridge = await get_bridge()

        code = f"""
import Mesh
import os

{_path_code(file_path, must_exist=True)}
doc = FreeCAD.ActiveDocument if {doc_name is None} else FreeCAD.getDocument({doc_name!r})
if doc is None:
    doc = FreeCAD.newDocument("Imported")

Mesh.insert(path, doc.Name)
doc.recompute()

# Get the last added object (the imported mesh)
mesh_obj = doc.Objects[-1]

if mesh_obj.TypeId != "Mesh::Feature" or mesh_obj.Mesh.CountFacets == 0:
    raise ValueError("The STL file added no mesh: " + path)
_result_ = {{
    "success": True,
    "document": doc.Name,
    "object": mesh_obj.Name,
    "facets": mesh_obj.Mesh.CountFacets,
}}
"""
        result = await bridge.execute_python(code)
        if result.success:
            return result.result
        raise ValueError(result.error_traceback or "STL import failed")
