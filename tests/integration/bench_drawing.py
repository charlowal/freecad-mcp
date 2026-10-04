"""Live bench for the drawing tools.

Needs a running FreeCAD with the addon's RPC server; not collected by pytest.

    FREECAD_MCP_TOKEN=... python tests/integration/bench_drawing.py [output folder]

Each tool is called as an MCP client would call it, then judged by what
FreeCAD itself holds afterwards (the dimension's measured value and text,
the template fields, the files written), not by the tool's answer:

    OK             the expected effect is measured
    ERROR          the tool raised where it should have worked
    FALSE_SUCCESS  the tool reported success but the measurement disagrees

A case that must be refused is OK only if the tool raises with a usable
hint and leaves the sheet unchanged. Two counter-tests break a sheet on
purpose (a wrong dimension text, a view pushed off the frame) and expect
check_drawing to FAIL them. The bench closes its documents (BenchDraw*)
without saving them; the PDF and PNG go to the output folder.
"""

import asyncio
import json
import math
import os
import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from typing import Any

from freecad_mcp.freecad_client import FreeCADConnection
from freecad_mcp.modelisation import register_tools
from freecad_mcp.modelisation.bridge import ExecuteCodeBridge

CONNECTION = FreeCADConnection(host="localhost", port=9875, token=os.environ.get("FREECAD_MCP_TOKEN"))
OUT = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser("~/snap/freecad/common/mcp-headless"))
RESULTS: list[tuple[str, str, str, str]] = []
# Where the tools say they drew things, checked later on TechDraw's own SVG
EXPECT: dict[str, Any] = {"balloons": [], "kinks": []}
AWS = "/snap/freecad/current/usr/share/Mod/TechDraw/Symbols/Welding/AWS/"


class _Registry:
    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self, *args: Any, **kwargs: Any):
        def register(fn):
            self.tools[fn.__name__] = fn
            return fn

        return register


def _load_tools() -> dict[str, Any]:
    registry, bridge = _Registry(), ExecuteCodeBridge(lambda: CONNECTION)

    async def get_bridge():
        return bridge

    register_tools(registry, get_bridge)
    return registry.tools


T = _load_tools()


def q(code: str) -> Any:
    """Run a check in FreeCAD; the code leaves its answer in R."""
    reply = CONNECTION.execute_code("import json as _j\nR = None\n" + code + "\nprint('__Q__' + _j.dumps(R, default=str))")
    text = reply.get("message", "") + str(reply.get("error", ""))
    if "__Q__" not in text:
        raise RuntimeError(text[-600:])
    return json.loads(text.split("__Q__", 1)[1].splitlines()[0])


async def call(_tool: str, **kwargs: Any) -> tuple[bool, Any]:
    try:
        return True, await T[_tool](**kwargs)
    except Exception as e:  # noqa: BLE001
        lines = [line for line in str(e).splitlines() if line.strip()]
        return False, (lines[-1] if lines else repr(e))[:300]


def note(group: str, case: str, verdict: str, detail: str = "") -> None:
    RESULTS.append((group, case, verdict, detail))
    print(f"{verdict:13} {group:8} {case:38} {detail}"[:260], flush=True)


def judge(group: str, case: str, ok: bool, reply: Any, condition: bool, detail_ok: str, detail_ko: str) -> None:
    if not ok:
        note(group, case, "ERROR", str(reply))
    else:
        note(group, case, "OK" if condition else "FALSE_SUCCESS", detail_ok if condition else detail_ko)


def refused(group: str, case: str, ok: bool, reply: Any, hint: str, unchanged: bool) -> None:
    if ok:
        note(group, case, "FALSE_SUCCESS", "accepted: " + str(reply)[:160])
    elif hint.lower() not in str(reply).lower():
        note(group, case, "ERROR", "refused without the hint '" + hint + "': " + str(reply))
    elif not unchanged:
        note(group, case, "FALSE_SUCCESS", "refused but left something behind")
    else:
        note(group, case, "OK", "refused: " + str(reply)[:150])


def count_views(doc: str, page: str) -> int:
    return q(f"R = len(App.getDocument({doc!r}).getObject({page!r}).Views)")


def dim_state(doc: str, name: str) -> dict:
    return q(f"""
_d = App.getDocument({doc!r}); _d.recompute(); _o = _d.getObject({name!r})
R = dict(raw=_o.getRawValue(), text=_o.FormatSpec, arbitrary=_o.Arbitrary, basic=_o.TheoreticalExact,
         spec="DualSpec" in _o.PropertiesList, refs=[str(r[1]) for r in _o.References2D])
""")


def close(doc: str) -> None:
    q(f"R = [App.closeDocument(_d) for _d in list(App.listDocuments()) if _d == {doc!r}]")


def _matrix(text: str | None) -> tuple:
    if not text:
        return (1, 0, 0, 1, 0, 0)
    out = (1, 0, 0, 1, 0, 0)
    for kind, args in re.findall(r"(matrix|translate|scale)\(([^)]*)\)", text):
        v = [float(x) for x in re.split(r"[ ,]+", args.strip()) if x]
        m = {"matrix": tuple(v), "translate": (1, 0, 0, 1, v[0], v[1] if len(v) > 1 else 0),
             "scale": (v[0], 0, 0, v[-1], 0, 0)}[kind]
        out = _mul(out, m)
    return out


def _mul(a: tuple, b: tuple) -> tuple:
    return (a[0] * b[0] + a[2] * b[1], a[1] * b[0] + a[3] * b[1], a[0] * b[2] + a[2] * b[3],
            a[1] * b[2] + a[3] * b[3], a[0] * b[4] + a[2] * b[5] + a[4], a[1] * b[4] + a[3] * b[5] + a[5])


def svg_marks(path: str) -> tuple[list, list]:
    """Texts and path points of a TechDraw SVG export, in sheet mm with y up."""
    root = ET.parse(path).getroot()
    vb = [float(v) for v in root.get("viewBox").split()]
    k = float(root.get("width")[:-2]) / vb[2]
    height = vb[3] * k
    texts, points = [], []

    def walk(node, m):
        m = _mul(m, _matrix(node.get("transform")))
        tag = node.tag.split("}")[-1]
        if tag == "text" and (node.text or "").strip():
            # x, y is the start of the baseline: move to the middle of the glyphs
            size = float(node.get("font-size", 0)) * abs(m[0] * m[3] - m[1] * m[2]) ** 0.5 * k
            x, y = float(node.get("x", 0)), float(node.get("y", 0))
            label = node.text.strip()
            texts.append((label, (m[0] * x + m[2] * y + m[4]) * k + 0.28 * size * len(label),
                          height - (m[1] * x + m[3] * y + m[5]) * k + 0.36 * size))
        if tag == "path":
            nums = [float(v) for v in re.findall(r"-?\d+(?:\.\d+)?(?:e-?\d+)?", node.get("d", ""))]
            for x, y in zip(nums[0::2], nums[1::2]):
                points.append(((m[0] * x + m[2] * y + m[4]) * k, height - (m[1] * x + m[3] * y + m[5]) * k))
        for child in node:
            walk(child, m)

    walk(root, (1, 0, 0, 1, 0, 0))
    return texts, points


def check_of(report: dict, name: str) -> str:
    return next((c["verdict"] for c in report["checks"] if c["check"] == name), "missing")


# --------------------------------------------------------------------------- models

def make_bracket(doc: str) -> None:
    close(doc)
    q(f"""
import Part
_d = App.newDocument({doc!r}); V = App.Vector
base = Part.makeBox(120, 80, 10)
for x in (25, 95):
    base = base.cut(Part.makeCylinder(5.5, 10, V(x, 30, 0)))
ame = Part.makeBox(120, 10, 90, V(0, 70, 10))
gousset = Part.Face(Part.makePolygon([V(55, 70, 10), V(55, 10, 10), V(55, 70, 70), V(55, 70, 10)])).extrude(V(10, 0, 0))
for n, s in (("BasePlate", base), ("Web", ame), ("Gusset", gousset)):
    _d.addObject("Part::Feature", n).Shape = s
_d.recompute(); R = True
""")


def make_hole_plate(doc: str) -> None:
    close(doc)
    q(f"""
import Part, math
_d = App.newDocument({doc!r}); V = App.Vector
p = Part.makeBox(80, 50, 12)
for x in (15, 65):
    p = p.cut(Part.makeCylinder(5.5, 12, V(x, 15, 0)))
p = p.cut(Part.makeCylinder(3.3, 12, V(40, 35, 0))).cut(Part.makeCylinder(5.5, 6.4, V(40, 35, 12 - 6.4)))
p = p.cut(Part.makeCylinder(3.3, 8, V(15, 38, 12 - 8)))
p = p.cut(Part.makeCylinder(3.3, 12, V(65, 38, 0))).cut(Part.makeCone(6.5, 3.3, 3.2, V(65, 38, 12), V(0, 0, -1)))
_d.addObject("Part::Feature", "HolePlate").Shape = p
_d.recompute(); R = round(p.Volume, 3)
""")


# --------------------------------------------------------------------------- cases

async def bench_sheet(doc: str) -> dict:
    make_bracket(doc)
    ok, r = await call("create_drawing_page", template="ANSIB_Landscape", title="WELDED BRACKET",
                       drawing_number="BENCH-001", drawn_by="Claude", doc_name=doc)
    page = r["page"] if ok else None
    if ok:
        got = q(f"""
_p = App.getDocument({doc!r}).getObject({page!r}); _t = _p.Template
R = dict(w=_t.Width.Value, h=_t.Height.Value, f=dict(_t.EditableTexts), tpl=_t.Template)
""")
        f = got["f"]
        cond = (got["w"] == 431.8 and got["h"] == 279.4 and f["DrawingTitle1"] == "WELDED BRACKET"
                and f["drawing_number"] == "BENCH-001" and f["CompanyName"] == "À RENSEIGNER"
                and f["Approved1"] == "" and f["CheckedBy"] == "À VÉRIFIER"
                and all(abs(a - b) < 0.01 for a, b in zip(r["frame"], [19.979, 20.181, 411.819, 259.221]))
                and r["title_block"] is not None and abs(r["title_block"][3] - 68.45) < 0.01)
        judge("sheet", "page, template, title block", ok, r, cond,
              f"ANSI B {got['w']}x{got['h']}, frame {r['frame']}, block {r['title_block']}", json.dumps(got)[:200])
    else:
        note("sheet", "page, template, title block", "ERROR", r)
    before = q(f"R = len(App.getDocument({doc!r}).Objects)")
    ok2, r2 = await call("create_drawing_page", template="ANSIZ_Landscape", doc_name=doc)
    refused("sheet", "unknown template refused", ok2, r2, "ANSIB_Landscape", before == q(f"R = len(App.getDocument({doc!r}).Objects)"))
    ok3, r3 = await call("create_drawing_page", fields={"Colour": "red"}, doc_name=doc)
    refused("sheet", "unknown field refused", ok3, r3, "Its fields", before == q(f"R = len(App.getDocument({doc!r}).Objects)"))
    ok4, r4 = await call("fill_title_block", page_name=page, fields={"weight": "1.9 kg", "company": "Bench"}, doc_name=doc)
    got = q(f"R = dict(App.getDocument({doc!r}).getObject({page!r}).Template.EditableTexts)")
    judge("sheet", "fill_title_block", ok4, r4, got["Weight"] == "1.9 kg" and got["CompanyName"] == "Bench",
          "Weight and CompanyName written", json.dumps(got)[:200])
    return {"page": page}


async def bench_views(doc: str, page: str) -> dict:
    n0 = count_views(doc, page)
    ok, r = await call("add_drawing_views", page_name=page, object_names=["BasePlate", "Web", "Gusset"], doc_name=doc)
    if not ok:
        note("views", "auto scale and layout", "ERROR", r)
        return {}
    # Independent read: every view's drawn lines, scaled and placed, against the frame and title block
    got = q(f"""
_d = App.getDocument({doc!r}); _p = _d.getObject({page!r})
def _edges(v):
    out, i = [], 0
    while True:
        try:
            e = v.getEdgeByIndex(i)
        except Exception:
            return out
        out.append(e); i += 1
boxes = dict()
for v in _d.Objects:
    if v.isDerivedFrom("TechDraw::DrawViewPart"):
        g = [p for p in v.InList if p.isDerivedFrom("TechDraw::DrawProjGroup")]
        ox = v.X.Value + (g[0].X.Value if g else 0); oy = v.Y.Value + (g[0].Y.Value if g else 0)
        s = v.getScale()
        es = _edges(v)
        boxes[v.Name] = [ox + s * min(e.BoundBox.XMin for e in es), oy + s * min(e.BoundBox.YMin for e in es),
                         ox + s * max(e.BoundBox.XMax for e in es), oy + s * max(e.BoundBox.YMax for e in es)]
R = dict(boxes=boxes, scale_field=_p.Template.EditableTexts["scale"], types=[v.Type for v in _d.getObject({r['group']!r}).Views])
""")
    frame, block = [19.979, 20.181, 411.819, 259.221], [264.98, 20.376, 411.64, 68.45]
    inside = all(b[0] >= frame[0] and b[1] >= frame[1] and b[2] <= frame[2] and b[3] <= frame[3] for b in got["boxes"].values())
    clear = all(b[2] <= block[0] or b[1] >= block[3] or b[0] >= block[2] for b in got["boxes"].values())
    names = list(got["boxes"])
    apart = all(not (got["boxes"][a][0] < got["boxes"][c][2] and got["boxes"][c][0] < got["boxes"][a][2]
                     and got["boxes"][a][1] < got["boxes"][c][3] and got["boxes"][c][1] < got["boxes"][a][3])
                for i, a in enumerate(names) for c in names[i + 1:])
    cond = inside and clear and apart and got["scale_field"] == r["scale_text"] and sorted(got["types"]) == ["Front", "Right", "Top"]
    judge("views", "auto scale and layout", ok, r, cond,
          f"scale {r['scale_text']}, iso {r['isometric_scale']}, {len(names)} views inside, off the title block, apart",
          f"inside={inside} clear={clear} apart={apart} field={got['scale_field']} {got['boxes']}")
    n1 = count_views(doc, page)
    ok2, r2 = await call("add_drawing_views", page_name=page, object_names=["BasePlate"], scale=10, isometric=False, doc_name=doc)
    refused("views", "10:1 that cannot fit refused", ok2, r2, "do not fit", count_views(doc, page) == n1)
    return r


async def bench_dimensions(doc: str, page: str, views: dict) -> list[str]:
    front, top = views["views"]["Front"], views["views"]["Top"]
    made = []
    cases = [
        ("front width 120", dict(view_name=front, kind="horizontal", points=[[0, 0, 0], [120, 0, 0]]), 120.0, "120 [4.724]"),
        ("front height 100", dict(view_name=front, kind="vertical", points=[[120, 0, 0], [120, 0, 100]]), 100.0, "100 [3.937]"),
        ("hole diameter 11", dict(view_name=top, kind="diameter", center=[25, 30, 10]), 11.0, "⌀11 [.433]"),
        ("hole radius 5.5", dict(view_name=top, kind="radius", center=[95, 30, 10], side="down_right"), 5.5, "R5.5 [.217]"),
        ("depth 80 ±0.1", dict(view_name=top, kind="vertical", points=[[120, 0, 10], [120, 80, 10]], tolerance=0.1),
         80.0, "80.0 ±0.1 [3.150 ±.004]"),
    ]
    for case, kwargs, value, text in cases:
        ok, r = await call("add_dimension", doc_name=doc, **kwargs)
        if not ok:
            note("dims", case, "ERROR", r)
            continue
        got = dim_state(doc, r["name"])
        cond = abs(got["raw"] - value) < 1e-6 and got["text"] == text and got["arbitrary"] and got["spec"]
        judge("dims", case, ok, r, cond, f"TechDraw measures {got['raw']}, shows {got['text']!r}", json.dumps(got))
        made.append(r["name"])
    # Basic: from the plate's corner to the gusset's corner
    ok, r = await call("add_dimension", view_name=front, kind="horizontal", points=[[0, 0, 0], [55, 0, 10]], basic=True,
                       side="above", doc_name=doc)
    if ok:
        got = dim_state(doc, r["name"])
        judge("dims", "basic 55 boxed", ok, r, got["basic"] and got["text"] == "55 [2.165]" and abs(got["raw"] - 55) < 1e-6,
              f"TheoreticalExact, {got['text']!r}", json.dumps(got))
        made.append(r["name"])
    else:
        note("dims", "basic 55 boxed", "ERROR", r)
    n = count_views(doc, page)
    ok, r = await call("add_dimension", view_name=front, kind="horizontal", points=[[0, 0, 0], [50, 50, 50]], doc_name=doc)
    refused("dims", "point not drawn refused", ok, r, "No visible vertex", count_views(doc, page) == n)
    ok, r = await call("add_dimension", view_name=front, kind="horizontal", points=[[0, 0, 0], [120, 0, 0]], basic=True,
                       tolerance=0.1, doc_name=doc)
    refused("dims", "basic with tolerance refused", ok, r, "basic dimension carries no tolerance", count_views(doc, page) == n)
    ok, r = await call("add_dimension", view_name=top, kind="diameter", center=[60, 40, 10], doc_name=doc)
    refused("dims", "no circle there refused", ok, r, "No circle", count_views(doc, page) == n)
    return made


async def bench_holes(doc: str) -> None:
    make_hole_plate(doc)
    ok, page = await call("create_drawing_page", template="ANSIB_Landscape", doc_name=doc)
    ok, views = await call("add_drawing_views", page_name=page["page"], object_names=["HolePlate"], doc_name=doc)
    if not ok:
        note("holes", "views of the hole plate", "ERROR", views)
        return
    top = views["views"]["Top"]
    ok, r = await call("add_hole_callouts", view_name=top, doc_name=doc)
    want = {
        "2X ⌀11 [.433] THRU",
        "⌀6.6 [.260] THRU\n⌴ ⌀11 [.433] ↧ 6.4 [.252]",
        "⌀6.6 [.260] ↧ 8 [.315]",
        "⌀6.6 [.260] THRU\n⌵ ⌀13 [.512] X 90°",
    }
    if ok:
        got = q(f"""
_d = App.getDocument({doc!r}); _d.recompute()
R = sorted([o.FormatSpec, o.getRawValue()] for o in _d.Objects if o.isDerivedFrom("TechDraw::DrawViewDimension"))
""")
        texts = {t for t, _ in got}
        raws = sorted(round(v, 6) for _, v in got)
        judge("holes", "callouts THRU, ⌴, ↧, ⌵, 2X", ok, r, texts == want and raws == [6.6, 6.6, 6.6, 11.0],
              f"{len(texts)} callouts, measured {raws}", f"got {sorted(texts)} {raws}")
    else:
        note("holes", "callouts THRU, ⌴, ↧, ⌵, 2X", "ERROR", r)
    ok, r = await call("add_hole_table", page_name=page["page"], view_name=top, doc_name=doc)
    if ok:
        got = q(f"""
_d = App.getDocument({doc!r})
R = dict(tags=sorted(o.Text[0] for o in _d.Objects if o.isDerivedFrom("TechDraw::DrawViewAnnotation")),
         a2=_d.getObject("HoleTable").getContents("A2").lstrip("'"), rows=[_d.getObject("HoleTable").getContents("D" + str(i)).lstrip("'") for i in range(2, 7)])
""")
        judge("holes", "hole table, 5 holes tagged", ok, r, got["tags"] == ["A1", "A2", "A3", "A4", "A5"] and got["a2"] == "A1",
              f"tags {got['tags']}, first rows {got['rows'][:2]}", json.dumps(got))
    else:
        note("holes", "hole table, 5 holes tagged", "ERROR", r)
    rep = await call("check_drawing", page_name=page["page"], doc_name=doc)
    if rep[0]:
        judge("holes", "check: every hole called out", True, rep[1], check_of(rep[1], "percages_cotes") == "PASS",
              "percages_cotes PASS", json.dumps([c for c in rep[1]["checks"] if c["check"] == "percages_cotes"]))
    close(doc)


async def bench_refresh(doc: str) -> None:
    close(doc)
    q(f"_d = App.newDocument({doc!r}); _b = _d.addObject('Part::Box', 'Block'); _b.Length = 60; _b.Width = 40; _b.Height = 10; _d.recompute(); R = True")
    ok, page = await call("create_drawing_page", doc_name=doc)
    ok, views = await call("add_drawing_views", page_name=page["page"], object_names=["Block"], isometric=False, doc_name=doc)
    ok, dim = await call("add_dimension", view_name=views["views"]["Front"], kind="horizontal",
                         points=[[0, 0, 0], [60, 0, 0]], doc_name=doc)
    if not ok:
        note("refresh", "dimension on a box", "ERROR", dim)
        return
    q(f"_d = App.getDocument({doc!r}); _d.Block.Length = 75; _d.recompute(); R = True")
    q("import time; from PySide import QtGui\nfor _i in range(10):\n    QtGui.QApplication.processEvents(); time.sleep(0.1)\nR = True")
    stale = await call("check_drawing", page_name=page["page"], doc_name=doc)
    judge("refresh", "counter-test: stale text FAILs", stale[0], stale[1],
          stale[0] and check_of(stale[1], "valeurs_recalculees") == "FAIL", "check_drawing FAILs the 60 left on a 75 box",
          "check did not notice: " + check_of(stale[1], "valeurs_recalculees") if stale[0] else "")
    ok, r = await call("refresh_dual_dimensions", page_name=page["page"], doc_name=doc)
    got = dim_state(doc, dim["name"])
    judge("refresh", "refresh rewrites 60 -> 75", ok, r, got["text"] == "75 [2.953]" and abs(got["raw"] - 75) < 1e-6,
          f"{got['text']!r}, changed {[c['before'] + ' -> ' + c['after'] for c in r['changed']] if ok else ''}", json.dumps(got))
    after = await call("check_drawing", page_name=page["page"], doc_name=doc)
    judge("refresh", "check PASSes after refresh", after[0], after[1],
          after[0] and check_of(after[1], "valeurs_recalculees") == "PASS", "valeurs_recalculees PASS", str(after[1])[:200])
    close(doc)


async def bench_annotations(doc: str, page: str, views: dict) -> None:
    front, top = views["views"]["Front"], views["views"]["Top"]
    n = count_views(doc, page)
    ok, r = await call("add_section_view", base_view=top, point=[25, 30, 5], normal=[1, 0, 0], symbol="A", doc_name=doc)
    if ok:
        got = q(f"""
_s = App.getDocument({doc!r}).getObject({r['name']!r}); i = 0
while True:
    try:
        _s.getEdgeByIndex(i); i += 1
    except Exception:
        break
R = dict(label=_s.Label, edges=i, symbol=_s.SectionSymbol, origin=list(_s.SectionOrigin))
""")
        judge("annot", "section A-A through a hole", ok, r, got["edges"] > 0 and got["label"] == "SECTION A-A" and r["cut_area"] > 0,
              f"{got['edges']} lines, cut area {r['cut_area']}, box {r['box']}", json.dumps(got))
    else:
        note("annot", "section A-A through a hole", "ERROR", r)
    n = count_views(doc, page)
    ok, r = await call("add_section_view", base_view=top, point=[500, 30, 5], normal=[1, 0, 0], symbol="B", doc_name=doc)
    refused("annot", "section missing the part refused", ok, r, "misses the part", count_views(doc, page) == n)
    for letter, point, side in (("A", [60, 0, 0], "down"), ("B", [0, 0, 50], "left"), ("C", [120, 0, 50], "right")):
        ok, r = await call("add_datum_symbol", letter=letter, view_name=front, point=point, side=side, doc_name=doc)
        if not ok:
            note("annot", "datum " + letter, "ERROR", r)
            continue
        # The triangle's base must sit on the feature: the symbol's edge facing it passes through the point
        got = q(f"""
_d = App.getDocument({doc!r}); _v = _d.getObject({front!r}); _s = _d.getObject({r['name']!r})
g = [p for p in _v.InList if p.isDerivedFrom("TechDraw::DrawProjGroup")][0]
R = dict(datum=_s.GdtDatum, x=_s.X.Value, y=_s.Y.Value, gx=g.X.Value + _v.X.Value, gy=g.Y.Value + _v.Y.Value, s=_v.getScale())
""")
        px, py = r["feature_point"]
        b = r["box"]
        on_edge = {"down": abs(b[3] - py) < 0.01 and b[0] < px < b[2], "left": abs(b[2] - px) < 0.01 and b[1] < py < b[3],
                   "right": abs(b[0] - px) < 0.01 and b[1] < py < b[3]}[side]
        judge("annot", "datum " + letter + " on its feature", ok, r, got["datum"] == letter and on_edge,
              f"box {b}, feature at {r['feature_point']}", json.dumps(got))
    ok, r = await call("add_gdt_frame", characteristic="position", tolerance=0.2, diameter_zone=True, material_condition="M",
                       datums=["A", "B", "C"], view_name=top, point=[25, 30, 10], doc_name=doc)
    if ok:
        got = q(f"_s = App.getDocument({doc!r}).getObject({r['name']!r}); R = dict(c=_s.GdtCharacteristic, d=_s.GdtDatums, t=_s.GdtTolerance, svg=_s.Symbol)")
        judge("annot", "position ⌀0.2 Ⓜ A B C", ok, r, got["c"] == "position" and got["d"] == "A,B,C" and "⌀0.2" in got["svg"]
              and r["leader"], f"frame {r['box']}, leader {r['leader']}", json.dumps(got)[:200])
        EXPECT["kinks"].append(("frame leader", r["kink_at"]))
    else:
        note("annot", "position ⌀0.2 Ⓜ A B C", "ERROR", r)
    n = count_views(doc, page)
    ok, r = await call("add_gdt_frame", characteristic="flatness", tolerance=0.05, datums=["A"], page_name=page,
                       position=[200, 200], doc_name=doc)
    refused("annot", "flatness with a datum refused", ok, r, "form tolerance", count_views(doc, page) == n)
    ok, r = await call("add_weld_symbol", view_name=front, point=[55, 10, 10], arrow_side="fillet", other_side="fillet",
                       arrow_size="6", other_size="6", leader=[-20, 25], doc_name=doc)
    if ok:
        got = q(f"""
_d = App.getDocument({doc!r}); _w = _d.getObject({r['name']!r})
tiles = sorted((t.TileRow, open(t.SymbolIncluded, "rb").read() == open({AWS!r} + ("filletDown.svg" if t.TileRow == 0 else "filletUp.svg"), "rb").read(), t.LeftText)
               for t in _w.InList if t.isDerivedFrom("TechDraw::DrawTileWeld"))
R = dict(tiles=tiles, leader=_w.Leader.Name)
""")
        EXPECT["kinks"].append(("weld leader", r["kink_at"]))
        judge("annot", "AWS fillet both sides, size 6", ok, r,
              [t[1] for t in got["tiles"]] == [True, True] and [t[2] for t in got["tiles"]] == ["6", "6"],
              f"tiles {got['tiles']}", json.dumps(got))
    else:
        note("annot", "AWS fillet both sides, size 6", "ERROR", r)
    iso = views["isometric"]
    ok, r = await call("add_parts_list", page_name=page, object_names=["BasePlate", "Web", "Gusset"], view_name=iso, doc_name=doc)
    if ok:
        got = q(f"""
_d = App.getDocument({doc!r}); _t = _d.getObject("PartsList")
R = dict(rows=[[_t.getContents(c + str(i)).lstrip("'") for c in "ABCE"] for i in range(2, 5)],
         balloons=sorted(o.Text for o in _d.Objects if o.isDerivedFrom("TechDraw::DrawViewBalloon")))
""")
        cond = got["balloons"] == ["1", "2", "3"] and [row[1] for row in got["rows"]] == ["1", "1", "1"] \
            and [row[2] for row in got["rows"]] == ["BasePlate", "Web", "Gusset"] and all(row[3] == "À RENSEIGNER" for row in got["rows"])
        judge("annot", "parts list and balloons", ok, r, cond, f"rows {got['rows']}, balloons {got['balloons']}", json.dumps(got))
        EXPECT["balloons"] += [(str(b["item"]), b["bubble_at"]) for b in r["balloons"]]
    else:
        note("annot", "parts list and balloons", "ERROR", r)
    ok, r = await call("add_revision_table", page_name=page, revisions=[{"rev": "A", "description": "FIRST ISSUE", "date": "2026-10-04"}],
                       doc_name=doc)
    got = q(f"R = App.getDocument({doc!r}).getObject({page!r}).Template.EditableTexts['revision_index']")
    judge("annot", "revision block, title block rev", ok, r, got == "A", f"revision_index {got!r}, table {r.get('box') if ok else ''}", repr(got))


async def bench_export_and_check(doc: str, page: str, views: dict) -> None:
    pdf = os.path.join(OUT, "bench-drawing.pdf")
    ok, r = await call("export_drawing", page_name=page, file_path=pdf, doc_name=doc)
    if ok:
        size = os.path.getsize(pdf) if os.path.exists(pdf) else 0
        cond = size > 10000 and r["pdf_page_mm"] == [431.8, 279.4] and r["png"] and all(os.path.getsize(p) > 10000 for p in r["png"])
        judge("export", "PDF ANSI B and PNG", ok, r, cond, f"{size} bytes, page {r['pdf_page_mm']} mm, {r['png']}", json.dumps(r))
    else:
        note("export", "PDF ANSI B and PNG", "ERROR", r)
    svg = os.path.join(OUT, "bench-drawing.svg")
    ok_svg, r_svg = await call("export_drawing", page_name=page, file_path=svg, png_dpi=None, doc_name=doc)
    if ok_svg:
        texts, points = svg_marks(svg)
        misses = []
        for label, (x, y) in EXPECT["balloons"]:
            near = [math.dist((tx, ty), (x, y)) for t, tx, ty in texts if t == label]
            if not near or min(near) > 1.5:
                misses.append(f"balloon {label} expected at {[x, y]}, drawn {min(near) if near else 'nowhere'} mm away")
        for label, (x, y) in EXPECT["kinks"]:
            d = min(math.dist(pt, (x, y)) for pt in points)
            if d > 1.5:
                misses.append(f"{label} kink expected at {[x, y]}, nearest drawn point {round(d, 2)} mm away")
        judge("export", "drawn where the tools say (SVG)", ok_svg, r_svg, not misses and len(EXPECT["balloons"]) == 3
              and len(EXPECT["kinks"]) == 2, f"{len(EXPECT['balloons'])} balloons and {len(EXPECT['kinks'])} leader kinks found on the render",
              "; ".join(misses))
    else:
        note("export", "drawn where the tools say (SVG)", "ERROR", r_svg)
    front_active = q("import FreeCADGui\nR = FreeCADGui.getMainWindow().findChild(__import__('PySide').QtGui.QMdiArea).activeSubWindow().windowTitle()")
    note("export", "3D view back in front", "OK" if "Sheet" not in front_active else "FALSE_SUCCESS", repr(front_active))
    report_path = os.path.join(OUT, "bench-drawing-check.md")
    ok, r = await call("check_drawing", page_name=page, report_path=report_path, doc_name=doc)
    if ok:
        fails = [c for c in r["checks"] if c["verdict"] == "FAIL"]
        never = [c for c in r["checks"] if c["check"] in ("cotation_complete", "conformite_norme", "gdt_semantique") and c["verdict"] != "NON_VERIFIE"]
        judge("check", "complete sheet: no FAIL", ok, r, not fails and not never and r["status"] == "À VÉRIFIER"
              and os.path.exists(report_path), r["summary"], json.dumps(fails)[:220])
    else:
        note("check", "complete sheet: no FAIL", "ERROR", r)
    # Counter-tests
    dim = q(f"""
_d = App.getDocument({doc!r})
R = [o.Name for o in _d.Objects if o.isDerivedFrom("TechDraw::DrawViewDimension") and "DualSpec" in o.PropertiesList and o.FormatSpec == "120 [4.724]"][0]
""")
    q(f"_o = App.getDocument({doc!r}).getObject({dim!r}); _o.FormatSpec = '125 [4.921]'; App.getDocument({doc!r}).recompute(); R = True")
    ok, r = await call("check_drawing", page_name=page, doc_name=doc)
    judge("check", "counter-test: wrong text FAILs", ok, r, ok and check_of(r, "valeurs_recalculees") == "FAIL",
          "valeurs_recalculees FAIL on 125 [4.921]", check_of(r, "valeurs_recalculees") if ok else "")
    q(f"_o = App.getDocument({doc!r}).getObject({dim!r}); _o.FormatSpec = '120 [4.724]'; App.getDocument({doc!r}).recompute(); R = True")
    iso = views["isometric"]
    old = q(f"_v = App.getDocument({doc!r}).getObject({iso!r}); R = [_v.X.Value, _v.Y.Value]")
    q(f"_v = App.getDocument({doc!r}).getObject({iso!r}); _v.X = 470; App.getDocument({doc!r}).recompute(); R = True")
    ok, r = await call("check_drawing", page_name=page, doc_name=doc)
    judge("check", "counter-test: view off the frame FAILs", ok, r, ok and check_of(r, "dans_le_cadre") == "FAIL",
          "dans_le_cadre FAIL with the isometric at x=470", check_of(r, "dans_le_cadre") if ok else "")
    q(f"_v = App.getDocument({doc!r}).getObject({iso!r}); _v.X = {old[0]}; _v.Y = {old[1]}; App.getDocument({doc!r}).recompute(); R = True")


async def bench_one_shot(doc: str) -> None:
    make_hole_plate(doc)
    ok, r = await call("create_drawing", object_names=["HolePlate"], title="HOLE PLATE", drawing_number="BENCH-002", doc_name=doc)
    if not ok:
        note("auto", "create_drawing on the hole plate", "ERROR", r)
        return
    fails = [c for c in r["check"]["checks"] if c["verdict"] == "FAIL"]
    texts = sorted(d["text"] for d in r["dimensions"])
    cond = not fails and texts == ["12 [.472]", "50 [1.969]", "80 [3.150]"] and len(r["hole_callouts"]) == 4
    judge("auto", "create_drawing on the hole plate", ok, r, cond,
          f"overall {texts}, {len(r['hole_callouts'])} callouts, check {r['check']['summary']}",
          f"overall {texts}, callouts {r['hole_callouts']}, fails {fails}")
    pdf = os.path.join(OUT, "bench-drawing-auto.pdf")
    await call("export_drawing", page_name=r["page"]["page"], file_path=pdf, doc_name=doc)
    close(doc)


REPORT = """
import FreeCADGui
from PySide import QtGui
_w = FreeCADGui.getMainWindow().findChild(QtGui.QTextEdit, "Report view")
R = _w.toPlainText() if _w else None
"""


def report_view_clean(before: str | None) -> None:
    """Warnings FreeCAD printed during the bench, other than the tools' deliberate refusals."""
    after = q(REPORT)
    if before is None or after is None:
        note("report", "Report view without warning", "ERROR", "Report view not found")
        return
    new = after[len(before):] if after.startswith(before) else after
    bad = [line for line in new.splitlines()
           if re.search(r"(?i)warning|corrupt|deprecat", line) and "ValueError" not in line]
    judge("report", "Report view without warning", True, None, not bad,
          f"{len(new.splitlines())} new lines, refusals only", " | ".join(bad)[:240])


async def main() -> None:
    os.makedirs(OUT, exist_ok=True)
    before = q(REPORT)
    doc = "BenchDrawBracket"
    sheet = await bench_sheet(doc)
    views = await bench_views(doc, sheet["page"])
    if views:
        await bench_dimensions(doc, sheet["page"], views)
        await bench_annotations(doc, sheet["page"], views)
        await bench_export_and_check(doc, sheet["page"], views)
    close(doc)
    await bench_holes("BenchDrawHoles")
    await bench_refresh("BenchDrawRefresh")
    await bench_one_shot("BenchDrawAuto")
    report_view_clean(before)
    counts = Counter(v for _, _, v, _ in RESULTS)
    print("\n" + ", ".join(f"{k} {n}" for k, n in sorted(counts.items())) + f" on {len(RESULTS)} cases")
    with open(os.path.join(OUT, "bench-drawing.json"), "w", encoding="utf-8") as out:
        json.dump([dict(group=g, case=c, verdict=v, detail=d) for g, c, v, d in RESULTS], out, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    asyncio.run(main())
