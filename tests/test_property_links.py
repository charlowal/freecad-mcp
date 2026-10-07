"""Link properties accept object names (create_object / edit_object).

A client such as a local model gives names; FreeCAD's link properties want
the objects. The property's own link type decides how a name is turned
into one (issue seen on 2026-10-06: Part::MultiFuse "Shapes").
"""

import importlib.util
from pathlib import Path
import sys
import types

import pytest

MAPPER = Path(__file__).resolve().parents[1] / "addon" / "FreeCADMCP" / "rpc_server" / "property_mapper.py"


class Obj:
    def __init__(self, name, label=None, kinds=None):
        self.Name = name
        self.Label = label or name
        self._kinds = kinds or {}
        self.PropertiesList = list(self._kinds) + ["Label"]
        for prop in self._kinds:
            setattr(self, prop, None)  # a real object always has its properties

    def getTypeIdOfProperty(self, prop):
        return self._kinds.get(prop, "App::PropertyString")


class Doc:
    Name = "Support_Laptop"

    def __init__(self, *objs):
        self.Objects = list(objs)

    def getObject(self, name):
        return next((o for o in self.Objects if o.Name == name), None)

    def getObjectsByLabel(self, label):
        return [o for o in self.Objects if o.Label == label]


@pytest.fixture()
def mapper(monkeypatch):
    printed = []
    class Vector:
        def __init__(self, *a):
            self.a = a

    fake = types.SimpleNamespace(
        Vector=Vector, Placement=lambda *a: ("P",) + a, Rotation=lambda *a: ("R",) + a,
        Document=object, DocumentObject=object,
        Console=types.SimpleNamespace(PrintError=printed.append, PrintMessage=printed.append),
    )
    monkeypatch.setitem(sys.modules, "FreeCAD", fake)
    spec = importlib.util.spec_from_file_location("property_mapper_under_test", MAPPER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.printed = printed
    return module


def test_link_list_takes_names(mapper):
    a, b = Obj("PlaqueTops"), Obj("PlaqueFront")
    fuse = Obj("Fusion", kinds={"Shapes": "App::PropertyLinkList"})
    mapper.set_object_property(Doc(a, b, fuse), fuse, {"Shapes": ["PlaqueTops", "PlaqueFront"]})
    assert fuse.Shapes == [a, b]


def test_link_list_takes_a_single_name(mapper):
    part = Obj("Bracket")
    view = Obj("View", kinds={"Source": "App::PropertyLinkList"})
    mapper.set_object_property(Doc(part, view), view, {"Source": "Bracket"})
    assert view.Source == [part]


def test_link_takes_a_name_or_a_label(mapper):
    base = Obj("Box001", label="Enveloppe")
    cut = Obj("Coque", kinds={"Base": "App::PropertyLink", "Tool": "App::PropertyLink"})
    tool = Obj("Box002")
    mapper.set_object_property(Doc(base, tool, cut), cut, {"Base": "Enveloppe", "Tool": "Box002"})
    assert cut.Base is base and cut.Tool is tool


def test_link_sub_takes_name_pair_or_dict(mapper):
    pad = Obj("Pad")
    sketch = Obj("Sketch", kinds={"Support": "App::PropertyLinkSub", "AttachmentSupport": "App::PropertyLinkSubList"})
    doc = Doc(pad, sketch)
    mapper.set_object_property(doc, sketch, {"Support": ["Pad", "Face6"]})
    assert sketch.Support == (pad, ["Face6"])
    mapper.set_object_property(doc, sketch, {"Support": "Pad"})
    assert sketch.Support == (pad, [])
    mapper.set_object_property(doc, sketch, {"AttachmentSupport": [{"object_name": "Pad", "face": "Face3"}]})
    assert sketch.AttachmentSupport == [(pad, "Face3")]


def test_unknown_name_lists_the_objects(mapper):
    fuse = Obj("Fusion", kinds={"Shapes": "App::PropertyLinkList"})
    with pytest.raises(ValueError, match="objects in Support_Laptop: .*Fusion"):
        mapper.set_object_property(Doc(Obj("PlaqueTops"), fuse), fuse, {"Shapes": ["PlaqueTops", "Nope"]})


def test_plain_properties_keep_their_strings(mapper):
    box = Obj("Box", kinds={"Label2": "App::PropertyString"})
    mapper.set_object_property(Doc(box), box, {"Label2": "PlaqueTops"})
    assert box.Label2 == "PlaqueTops"


def test_objects_passed_as_is_still_work(mapper):
    a = Obj("A")
    fuse = Obj("Fusion", kinds={"Shapes": "App::PropertyLinkList"})
    mapper.set_object_property(Doc(a, fuse), fuse, {"Shapes": [a]})
    assert fuse.Shapes == [a]
