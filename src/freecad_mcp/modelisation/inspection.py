"""Inspection tools: topology, measurements, mass properties, interference.

They read the model and never change it. Elements are named the way the
feature tools expect them ("Face6", "Edge3" of a given object), so an
agent can find the edges to fillet or the face to sketch on instead of
guessing. Same conventions as the vendored modules: a script goes through
``bridge.execute_python`` and leaves its answer in ``_result_``.
"""

from collections.abc import Awaitable, Callable
from typing import Any

from .export import _build_object_selection_code

# Template helpers, defined once per script. A reference is "Object",
# "Object:FaceN", "Object:EdgeN", "Object:VertexN", or a point [x, y, z].
_HELPERS = """
import math
import Part

def _v(vector):
    return [round(vector.x, 6), round(vector.y, 6), round(vector.z, 6)]

def _object(name):
    obj = doc.getObject(name)
    if obj is None:
        raise ValueError("Object not found: " + str(name))
    return obj

def _shape_of(ref):
    if isinstance(ref, (list, tuple)):
        if len(ref) != 3:
            raise ValueError("A point is [x, y, z]: " + str(ref))
        return Part.Vertex(FreeCAD.Vector(*ref))
    name, _, sub = str(ref).partition(":")
    obj = _object(name)
    if sub:
        return Part.getShape(obj, sub, needSubElement=True)
    return Part.getShape(obj)

def _direction(shape, ref):
    # The direction that defines an angle: plane normal, line direction, cylinder axis
    if shape.ShapeType == "Face":
        surface = shape.Surface
        if type(surface).__name__ == "Plane":
            return shape.normalAt(0, 0), "plane"
        if hasattr(surface, "Axis"):
            return surface.Axis, "axis"
    if shape.ShapeType == "Edge" and type(shape.Curve).__name__ in ("Line", "LineSegment"):
        return (shape.Vertexes[-1].Point - shape.Vertexes[0].Point).normalize(), "line"
    raise ValueError(str(ref) + " has no direction: use a planar face, a straight edge or a cylindrical face")
"""


def register_inspection_tools(mcp: Any, get_bridge: Callable[[], Awaitable[Any]]) -> None:
    """Register the inspection tools."""

    async def run(code: str, failure: str) -> dict[str, Any]:
        result = await (await get_bridge()).execute_python(code)
        if result.success:
            return result.result
        raise ValueError(result.error_traceback or failure)

    def document(doc_name: str | None) -> str:
        return f"""
doc = FreeCAD.ActiveDocument if {doc_name!r} is None else FreeCAD.getDocument({doc_name!r})
if doc is None:
    raise ValueError("No document found")
{_HELPERS}
"""

    @mcp.tool()
    async def get_topology(
        object_name: str,
        kind: str = "all",
        element_type: str | None = None,
        on_face: str | None = None,
        near: list[float] | None = None,
        limit: int = 100,
        doc_name: str | None = None,
    ) -> dict[str, Any]:
        """List the faces and edges of an object with their geometry.

        Use it before fillet_edges, chamfer_edges, draft_feature,
        thickness_feature or create_sketch(plane="Feature:FaceN") to pick
        elements by position instead of guessing their names. Faces give
        their type, area, centre and normal (planes) or axis and radius
        (cylinders, cones); edges give their type, length, end points,
        radius (circles) and the two faces they join.

        Args:
            object_name: Feature or body whose shape is listed.
            kind: "faces", "edges" or "all".
            element_type: Keep one type only, e.g. "Plane", "Cylinder",
                "Line", "Circle".
            on_face: Keep the edges that bound this face, e.g. "Face6".
            near: Sort by distance from this point [x, y, z], nearest first.
            limit: Maximum number of faces and of edges returned.
            doc_name: Document. Uses active document if None.

        Returns:
            Counts, volume, bounding box, and the faces and edges kept.
        """
        code = f"""
{document(doc_name)}
_obj = _object({object_name!r})
shape = Part.getShape(_obj)
if shape.isNull():
    raise ValueError({object_name!r} + " has no shape")
_kind, _type, _on_face, _near, _limit = {kind!r}, {element_type!r}, {on_face!r}, {near!r}, {limit}
if _kind not in ("faces", "edges", "all"):
    raise ValueError("kind is faces, edges or all")
_origin = FreeCAD.Vector(*_near) if _near else None

# Which faces each edge joins (an edge shared by two faces has one hash)
_owners = dict()
for _i, _f in enumerate(shape.Faces):
    for _e in _f.Edges:
        _owners.setdefault(_e.hashCode(), set()).add(_i + 1)

_faces = []
if _kind in ("faces", "all"):
    for _i, _f in enumerate(shape.Faces):
        _s = _f.Surface
        _t = type(_s).__name__
        _d = dict(name="Face" + str(_i + 1), type=_t, area=round(_f.Area, 6), center=_v(_f.CenterOfMass))
        if _t == "Plane":
            _d["normal"] = _v(_f.normalAt(0, 0))
        if hasattr(_s, "Axis") and _t != "Plane":
            _d["axis"] = _v(_s.Axis)
        if hasattr(_s, "Radius"):
            _d["radius"] = round(_s.Radius, 6)
        if _t in ("Cylinder", "Sphere") and hasattr(_s, "Center"):
            _d["axis_point" if _t == "Cylinder" else "sphere_center"] = _v(_s.Center)
        _d["_at"] = _f.CenterOfMass
        _faces.append(_d)

_edges = []
_bounding = None
if _on_face:
    if not _on_face.startswith("Face") or not _on_face[4:].isdigit() or not 0 < int(_on_face[4:]) <= len(shape.Faces):
        raise ValueError("on_face must be a face of " + {object_name!r} + ", Face1 to Face" + str(len(shape.Faces)))
    _bounding = set(_e.hashCode() for _e in shape.Faces[int(_on_face[4:]) - 1].Edges)
if _kind in ("edges", "all"):
    for _j, _e in enumerate(shape.Edges):
        if _bounding is not None and _e.hashCode() not in _bounding:
            continue
        _c = _e.Curve
        _t = type(_c).__name__
        _d = dict(name="Edge" + str(_j + 1), type=_t, length=round(_e.Length, 6),
                  start=_v(_e.Vertexes[0].Point), end=_v(_e.Vertexes[-1].Point),
                  faces=["Face" + str(_k) for _k in sorted(_owners.get(_e.hashCode(), ()))])
        if hasattr(_c, "Radius"):
            _d["radius"] = round(_c.Radius, 6)
            _d["center"] = _v(_c.Center)
            _d["axis"] = _v(_c.Axis)
        elif _t in ("Line", "LineSegment"):
            _d["direction"] = _v((_e.Vertexes[-1].Point - _e.Vertexes[0].Point).normalize())
        _d["_at"] = _e.valueAt((_e.FirstParameter + _e.LastParameter) / 2)
        _edges.append(_d)

def _keep(items):
    if _type:
        items = [x for x in items if x["type"] == _type]
    if _origin is not None:
        for x in items:
            x["distance"] = round((x["_at"] - _origin).Length, 6)
        items.sort(key=lambda x: x["distance"])
    for x in items:
        del x["_at"]
    return items[:_limit], len(items)

_faces, _faces_matching = _keep(_faces)
_edges, _edges_matching = _keep(_edges)
_box = shape.BoundBox
_result_ = {{
    "object": _obj.Name,
    "solids": len(shape.Solids),
    "volume": round(shape.Volume, 6) if shape.Solids else None,
    "bounding_box": dict(min=[round(_box.XMin, 6), round(_box.YMin, 6), round(_box.ZMin, 6)],
                         max=[round(_box.XMax, 6), round(_box.YMax, 6), round(_box.ZMax, 6)]),
    "face_count": len(shape.Faces),
    "edge_count": len(shape.Edges),
    "faces": _faces,
    "edges": _edges,
    "faces_matching": _faces_matching,
    "edges_matching": _edges_matching,
}}
"""
        return await run(code, "get_topology failed")

    @mcp.tool()
    async def measure_distance(
        element_a: str | list[float], element_b: str | list[float], doc_name: str | None = None
    ) -> dict[str, Any]:
        """Measure the minimum distance between two elements.

        Args:
            element_a: "Object", "Object:FaceN", "Object:EdgeN",
                "Object:VertexN" or a point [x, y, z].
            element_b: Same forms as element_a.
            doc_name: Document. Uses active document if None.

        Returns:
            distance (mm) and the closest point on each element.
        """
        code = f"""
{document(doc_name)}
_a = _shape_of({element_a!r})
_b = _shape_of({element_b!r})
_distance, _pairs, _info = _a.distToShape(_b)
_result_ = {{"distance": round(_distance, 6), "point_a": _v(_pairs[0][0]), "point_b": _v(_pairs[0][1])}}
"""
        return await run(code, "measure_distance failed")

    @mcp.tool()
    async def measure_angle(element_a: str, element_b: str, doc_name: str | None = None) -> dict[str, Any]:
        """Measure the angle between two planar faces, straight edges or axes.

        For a face and an edge, the angle is between the edge and the plane.

        Args:
            element_a: "Object:FaceN" (planar or cylindrical) or "Object:EdgeN" (straight).
            element_b: Same forms as element_a.
            doc_name: Document. Uses active document if None.

        Returns:
            angle in degrees (0 to 90, the acute angle between the two
            elements) and the angle between their oriented directions (0 to 180).
        """
        code = f"""
{document(doc_name)}
_da, _ka = _direction(_shape_of({element_a!r}), {element_a!r})
_db, _kb = _direction(_shape_of({element_b!r}), {element_b!r})
_oriented = math.degrees(_da.getAngle(_db))
_acute = min(_oriented, 180 - _oriented)
if (_ka == "plane") != (_kb == "plane"):
    _acute = 90 - _acute  # a line against a plane: angle to the plane, not to its normal
_result_ = {{"angle": round(_acute, 6), "oriented_angle": round(_oriented, 6), "kinds": [_ka, _kb]}}
"""
        return await run(code, "measure_angle failed")

    @mcp.tool()
    async def mass_properties(
        object_name: str,
        material: str | None = None,
        density: float | None = None,
        doc_name: str | None = None,
    ) -> dict[str, Any]:
        """Volume, centre of mass, mass and inertia of a solid.

        The density comes from ``density`` (kg/m3), else the FreeCAD material
        card named ``material`` (e.g. "Steel-Generic", "Aluminum-6061-T6",
        "PLA-Generic"), else the material assigned to the object. Without
        one, mass and inertia are given per unit density and mass is None.

        Args:
            object_name: Body or solid feature.
            material: Name of a FreeCAD material card.
            density: Density in kg/m3; overrides material.
            doc_name: Document. Uses active document if None.

        Returns:
            volume (mm3), area (mm2), center_of_mass (mm), mass (kg),
            density and its source, inertia about the centre of mass
            (kg.mm2, or mm5 per unit density) and principal moments and axes.
        """
        code = f"""
{document(doc_name)}
_obj = _object({object_name!r})
shape = Part.getShape(_obj)
_solids = shape.Solids
if not _solids:
    raise ValueError({object_name!r} + " has no solid")

_density, _source = {density!r}, "argument"
if _density is None and {material!r}:
    import Materials
    _cards = [m for m in Materials.MaterialManager().Materials.values() if m.Name.lower() == {material!r}.lower()]
    _cards = [m for m in _cards if m.hasPhysicalProperty("Density")]
    if not _cards:
        _known = sorted(m.Name for m in Materials.MaterialManager().Materials.values()
                        if m.hasPhysicalProperty("Density") and {material!r}.lower().split("-")[0] in m.Name.lower())
        raise ValueError("No material card with a density named " + {material!r} + "; close names: " + ", ".join(_known[:12]))
    _q = _cards[0].getPhysicalValue("Density").getValueAs("kg/m^3")
    _density, _source = float(getattr(_q, "Value", _q)), "material " + _cards[0].Name
if _density is None and hasattr(_obj, "ShapeMaterial") and _obj.ShapeMaterial.Name != "Default":
    try:
        _q = _obj.ShapeMaterial.getPhysicalValue("Density").getValueAs("kg/m^3")
        _density = float(getattr(_q, "Value", _q))
        _source = "assigned material " + _obj.ShapeMaterial.Name
    except Exception:
        _density = None
if _density is not None and _density <= 0:
    raise ValueError("density must be positive")

# Combine the solids about the common centre of mass (parallel axis theorem)
_volume = sum(s.Volume for s in _solids)
_com = FreeCAD.Vector()
for s in _solids:
    _com = _com + s.CenterOfMass * (s.Volume / _volume)
_inertia = [[0.0] * 3 for _ in range(3)]
for s in _solids:
    _m = s.MatrixOfInertia
    _local = [[_m.A11, _m.A12, _m.A13], [_m.A21, _m.A22, _m.A23], [_m.A31, _m.A32, _m.A33]]
    _d = s.CenterOfMass - _com
    _dv = [_d.x, _d.y, _d.z]
    for r in range(3):
        for c in range(3):
            _shift = s.Volume * ((_d.Length ** 2 if r == c else 0.0) - _dv[r] * _dv[c])
            _inertia[r][c] += _local[r][c] + _shift

import numpy
_scale = _density * 1e-9 if _density is not None else 1.0  # kg/mm3, or per unit density
_tensor = numpy.array(_inertia) * _scale
_moments, _axes = numpy.linalg.eigh(_tensor)
_box = shape.BoundBox
_result_ = {{
    "object": _obj.Name,
    "solids": len(_solids),
    "volume": round(_volume, 6),
    "area": round(shape.Area, 6),
    "center_of_mass": _v(_com),
    "bounding_box_size": [round(_box.XLength, 6), round(_box.YLength, 6), round(_box.ZLength, 6)],
    "density": _density,
    "density_source": _source if _density is not None else None,
    "mass": round(_volume * 1e-9 * _density, 9) if _density is not None else None,
    "inertia_unit": "kg.mm2" if _density is not None else "mm5 (per unit density)",
    "inertia_about_center_of_mass": [[round(float(x), 6) for x in row] for row in _tensor],
    "principal_moments": [round(float(x), 6) for x in _moments],
    "principal_axes": [[round(float(x), 6) for x in _axes[:, k]] for k in range(3)],
    "note": None if _density is not None else "No density: pass density (kg/m3) or material for a mass",
}}
"""
        return await run(code, "mass_properties failed")

    @mcp.tool()
    async def check_interference(
        object_names: list[str] | None = None, doc_name: str | None = None
    ) -> dict[str, Any]:
        """Find solids that overlap or touch.

        Args:
            object_names: Solids to check. Checks the finished solids of the
                document (bodies and standalone solids) if None.
            doc_name: Document. Uses active document if None.

        Returns:
            The pairs that overlap (with the shared volume) and the pairs
            that touch without overlapping.
        """
        code = f"""
{document(doc_name)}
{_build_object_selection_code(object_names)}
_overlaps, _touching = [], []
for _i in range(len(objects)):
    for _j in range(_i + 1, len(objects)):
        _a, _b = shapes[_i], shapes[_j]
        if not _a.BoundBox.intersect(_b.BoundBox):
            continue
        _shared = _a.common(_b).Volume
        if _shared > 1e-6:
            _overlaps.append(dict(objects=[objects[_i].Name, objects[_j].Name], shared_volume=round(_shared, 6)))
        elif _a.distToShape(_b)[0] < 1e-6:
            _touching.append([objects[_i].Name, objects[_j].Name])
_result_ = {{"checked": [o.Name for o in objects], "overlaps": _overlaps, "touching": _touching}}
"""
        return await run(code, "check_interference failed")
