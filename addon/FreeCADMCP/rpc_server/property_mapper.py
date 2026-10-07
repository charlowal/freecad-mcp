"""Property assignment from JSON-friendly dicts onto FreeCAD document objects."""

from dataclasses import dataclass, field
from typing import Any

import FreeCAD


@dataclass
class Object:
    name: str
    type: str | None = None
    analysis: str | None = None
    properties: dict[str, Any] = field(default_factory=dict)


def _to_shape_color(val: Any) -> tuple[float, float, float, float]:
    """Normalise a color to a 4-float RGBA tuple.

    Accepts RGB triples (alpha defaults to 1.0) and RGBA quads, matching what
    FreeCAD's ``ShapeColor`` accepts.
    """
    if not isinstance(val, (list, tuple)) or len(val) not in (3, 4):
        raise ValueError(
            f"ShapeColor must be an RGB or RGBA sequence, got {val!r}."
        )
    r, g, b = (float(val[0]), float(val[1]), float(val[2]))
    a = float(val[3]) if len(val) == 4 else 1.0
    return (r, g, b, a)


def parse_reference_entry(entry: Any) -> tuple[str, Any]:
    """Normalise a single ``References`` entry to ``(object_name, sub_element)``.

    Accepts both the documented dict form
    ``{"object_name": "Box", "face": "Face1"}`` and the legacy
    ``["Box", "Face1"]`` pair form.
    """
    if isinstance(entry, dict):
        ref_name = entry.get("object_name", entry.get("Object"))
        face = entry.get("face", entry.get("Face"))
        if ref_name is None:
            raise ValueError(
                f"Reference entry {entry!r} is missing an 'object_name' key."
            )
        return ref_name, face
    if isinstance(entry, (list, tuple)) and len(entry) == 2:
        return entry[0], entry[1]
    raise ValueError(
        f"Invalid reference entry {entry!r}; expected "
        "{'object_name': ..., 'face': ...} or [object_name, face]."
    )


_LINK_KINDS = ("", "Child", "Global", "Hidden")
_LINK = tuple("App::PropertyLink" + k for k in _LINK_KINDS)
_LINK_LIST = tuple("App::PropertyLinkList" + k for k in _LINK_KINDS)
_LINK_SUB = tuple("App::PropertyLinkSub" + k for k in _LINK_KINDS)
_LINK_SUB_LIST = tuple("App::PropertyLinkSubList" + k for k in _LINK_KINDS)
_NOT_A_LINK = object()


def find_object(doc: FreeCAD.Document, ref: Any) -> Any:
    """The object a client named: by name, else by a label only one object has.

    A document object passed as is is returned unchanged. An unknown name
    raises with the document's object names, so the caller can pick one.
    """
    if not isinstance(ref, str):
        return ref
    obj = doc.getObject(ref)
    if obj is None:
        by_label = doc.getObjectsByLabel(ref)
        if len(by_label) == 1:
            obj = by_label[0]
    if obj is None:
        names = sorted(o.Name for o in doc.Objects)
        listed = ", ".join(names[:40]) + (" ..." if len(names) > 40 else "")
        raise ValueError(f"Referenced object '{ref}' not found; objects in {doc.Name}: {listed}")
    return obj


def resolve_link_value(doc: FreeCAD.Document, obj: Any, prop: str, val: Any) -> Any:
    """``val`` with object names turned into objects, as the property's link type wants.

    PropertyLink takes a name; PropertyLinkList a name or a list of names;
    PropertyLinkSub a name, ``[name, sub]`` or ``{"object_name", "face"}``;
    PropertyLinkSubList a list of those. Any other property gives
    ``_NOT_A_LINK`` and is set as before.
    """
    try:
        kind = obj.getTypeIdOfProperty(prop)
    except Exception:
        return _NOT_A_LINK
    if kind in _LINK:
        return None if val in (None, "") else find_object(doc, val)
    if kind in _LINK_LIST:
        items = val if isinstance(val, (list, tuple)) else [val]
        return [find_object(doc, v) for v in items]
    if kind in _LINK_SUB:
        if val in (None, ""):
            return None
        if isinstance(val, str) or not isinstance(val, (list, tuple, dict)):
            return (find_object(doc, val), [])
        name, sub_names = parse_reference_entry(val)
        if sub_names is None:
            sub_names = []
        elif isinstance(sub_names, str):
            sub_names = [sub_names]
        return (find_object(doc, name), list(sub_names))
    if kind in _LINK_SUB_LIST:
        return [(find_object(doc, name), face) for name, face in (parse_reference_entry(e) for e in val)]
    return _NOT_A_LINK


def resolve_references(doc: FreeCAD.Document, val: Any) -> list[tuple[Any, Any]]:
    """Resolve a ``References`` list into ``(DocumentObject, sub_element)`` tuples."""
    refs = []
    for entry in val:
        ref_name, face = parse_reference_entry(entry)
        ref_obj = doc.getObject(ref_name)
        if ref_obj is None:
            raise ValueError(f"Referenced object '{ref_name}' not found.")
        refs.append((ref_obj, face))
    return refs


def set_object_property(
    doc: FreeCAD.Document, obj: FreeCAD.DocumentObject, properties: dict[str, Any]
):
    failures = []
    for prop, val in properties.items():
        try:
            if prop in obj.PropertiesList:
                if prop == "Placement" and isinstance(val, dict):
                    if "Base" in val:
                        pos = val["Base"]
                    elif "Position" in val:
                        pos = val["Position"]
                    else:
                        pos = {}
                    rot = val.get("Rotation", {})
                    placement = FreeCAD.Placement(
                        FreeCAD.Vector(
                            pos.get("x", 0),
                            pos.get("y", 0),
                            pos.get("z", 0),
                        ),
                        FreeCAD.Rotation(
                            FreeCAD.Vector(
                                rot.get("Axis", {}).get("x", 0),
                                rot.get("Axis", {}).get("y", 0),
                                rot.get("Axis", {}).get("z", 1),
                            ),
                            rot.get("Angle", 0),
                        ),
                    )
                    setattr(obj, prop, placement)

                elif isinstance(getattr(obj, prop), FreeCAD.Vector) and isinstance(
                    val, dict
                ):
                    vector = FreeCAD.Vector(
                        val.get("x", 0), val.get("y", 0), val.get("z", 0)
                    )
                    setattr(obj, prop, vector)

                elif (resolved := resolve_link_value(doc, obj, prop, val)) is not _NOT_A_LINK:
                    # Link properties take objects; clients give their names
                    setattr(obj, prop, resolved)

                elif prop in ["Base", "Tool", "Source", "Profile"] and isinstance(
                    val, str
                ):
                    ref_obj = doc.getObject(val)
                    if ref_obj:
                        setattr(obj, prop, ref_obj)
                    else:
                        raise ValueError(f"Referenced object '{val}' not found.")

                elif prop == "References" and isinstance(val, list):
                    setattr(obj, prop, resolve_references(doc, val))

                else:
                    setattr(obj, prop, val)
            # ShapeColor is a property of the ViewObject
            elif prop == "ShapeColor" and isinstance(val, (list, tuple)):
                setattr(obj.ViewObject, prop, _to_shape_color(val))

            elif prop == "ViewObject" and isinstance(val, dict):
                for k, v in val.items():
                    if k == "ShapeColor":
                        setattr(obj.ViewObject, k, _to_shape_color(v))
                    else:
                        setattr(obj.ViewObject, k, v)

            else:
                setattr(obj, prop, val)

        except Exception as e:
            FreeCAD.Console.PrintError(f"Property '{prop}' assignment error: {e}\n")
            failures.append(f"{prop}: {e}")

    if failures:
        raise ValueError(
            "Failed to set propert" + ("y" if len(failures) == 1 else "ies")
            + ": " + "; ".join(failures)
        )
