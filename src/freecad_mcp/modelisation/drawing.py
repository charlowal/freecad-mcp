"""Drawing tools: TechDraw sheets, views, dual dimensions, annotations, checks.

Written for mechanical drawings in dual units, mm [in], third-angle
projection and AWS A2.4 weld symbols, the choices this fork was made for;
each is a parameter. TechDraw has no second unit: a dual dimension keeps
its measured value associative and shows both units as its text
(``Arbitrary``). ``refresh_dual_dimensions`` rewrites the texts after the
model changes, and ``check_drawing`` recomputes every one of them from the
geometry.

Nothing here declares conformity to a standard. The checker answers PASS,
FAIL, NON_VERIFIE or NON_APPLICABLE with its evidence, and the drawing's
status stays "À VÉRIFIER": a person checks and approves it. Unknown
title-block fields are written "À RENSEIGNER", never invented.

Same conventions as the other modules: a script goes through
``bridge.execute_python`` and leaves its answer in ``_result_``. Arguments
travel as JSON, so the scripts are plain strings, not templates; the
formatting functions below run both here (tests) and inside FreeCAD.
"""

import asyncio
import inspect
import json
import math
import os
import re
import shutil
import subprocess
from collections.abc import Awaitable, Callable
from typing import Any
from xml.sax.saxutils import escape

from .export import _path_code

PLACEHOLDER = "À RENSEIGNER"
TO_CHECK = "À VÉRIFIER"
VERDICTS = ("PASS", "FAIL", "NON_VERIFIE", "NON_APPLICABLE")
STANDARD_SCALES = (10.0, 5.0, 4.0, 2.0, 1.0, 0.5, 0.25, 0.2, 0.1, 0.05, 0.02, 0.01)
PROJECTIONS = ("Front", "Left", "Right", "Rear", "Top", "Bottom",
               "FrontTopLeft", "FrontTopRight", "FrontBottomLeft", "FrontBottomRight")

# Title-block fields by the names the tools use, then the template field each
# one is called in FreeCAD's ASME templates: ANSI A and B first, ANSI C, D and E second
TITLE_FIELDS = {
    "title": ["DrawingTitle1", "Title"],
    "title_2": ["DrawingTitle2", "Subtitle"],
    "title_3": ["DrawingTitle3"],
    "drawing_number": ["drawing_number", "DrawingNumber"],
    "revision": ["revision_index", "Revision"],
    "drawn_by": ["DrawnBy", "AuthorName"],
    "checked_by": ["CheckedBy", "SupervisorName"],
    "approved_1": ["Approved1"],
    "approved_2": ["Approved2"],
    "company": ["CompanyName", "Company_name"],
    "company_address": ["CompanyAddress"],
    "code": ["Code"],
    "weight": ["Weight"],
    "scale": ["scale", "Scale"],
    "sheet": ["Sheet", "SheetNumber"],
    "date": ["CreationDate", "Date"],
}
# Unknown values of these read "À RENSEIGNER"; other fields stay empty
_IDENTITY_FIELDS = ("title", "drawing_number", "drawn_by", "company", "weight")

# ---------------------------------------------------------------------------
# Shared functions: tested here, and sent to FreeCAD inside every script.
# They may only use math, re and json.
# ---------------------------------------------------------------------------


def _decimals(value, most):
    """Fewest decimals (up to ``most``) that write ``value`` exactly."""
    for d in range(most + 1):
        if abs(round(value, d) - value) < 5e-7:
            return d
    return most


def _fmt(value, decimals, leading_zero=True):
    """Fixed decimals; inch values drop the leading zero (.433)."""
    text = ("%." + str(decimals) + "f") % value
    if text in ("-0", "-0." + "0" * decimals):
        text = text[1:]
    if not leading_zero:
        if text.startswith("0."):
            text = text[1:]
        elif text.startswith("-0."):
            text = "-" + text[2:]
    return text


def _tolerance_text(spec, decimals=None):
    """Metric deviations: " ±0.1", " +0.2/-0.1", " +0.3/0", or "" without tolerance.

    ASME Y14.5 in mm: both deviations carry the same decimals; a nil deviation
    is a single 0 without sign; the dimension itself need not match them
    ("12 ±0.1", not "12.0 ±0.1").
    """
    plus, minus = spec.get("plus"), spec.get("minus")
    if plus is None and minus is None:
        return ""
    plus = plus or 0.0
    minus = minus or 0.0
    if decimals is None:
        decimals = max(_decimals(abs(plus), 3), _decimals(abs(minus), 3))
    if abs(plus + minus) < 1e-12:
        return " ±" + _fmt(abs(plus), decimals)

    def deviation(value, sign):
        if abs(value) < 1e-12:
            return "0"
        return sign + _fmt(abs(value), decimals)

    up = deviation(plus, "+" if plus >= 0 else "-")
    down = deviation(minus, "-" if minus <= 0 else "+")
    return " " + up + "/" + down


def _inch_limits(low, high, decimals):
    """Limits in mm converted to inches and rounded inward: the inch range
    never reaches outside the mm range (lower limit up, upper limit down).
    Narrow ranges get a decimal more until the rounded limits stay in order."""
    for d in range(decimals, 7):
        q = 10 ** d
        lo = math.ceil(low / 25.4 * q - 1e-9) / q
        hi = math.floor(high / 25.4 * q + 1e-9) / q
        if lo <= hi + 1e-12:
            return _fmt(lo, d, False) + "–" + _fmt(hi, d, False)
    raise ValueError("The range %s–%s mm is too narrow to write in inches" % (low, high))


def _dual_text(spec, value):
    """The text of a dimension of ``value`` mm: "120 [4.724]", "⌀11 ±0.1 [.429–.437]".

    mm keep the fewest decimals that write the value (no trailing zero).
    With a tolerance, the inch text is the pair of limits, converted and
    rounded inward, so the reference inches never widen the mm tolerance;
    3 inch decimals for deviations of 0.1 mm and more, 4 below. ``limits``
    writes the mm as limits too ("20.02–20.04"). ``dual`` False keeps mm only.
    """
    decimals_in = spec.get("decimals_in", 3)
    decimals_mm = spec.get("decimals_mm")
    plus, minus = spec.get("plus"), spec.get("minus")
    toleranced = plus is not None or minus is not None
    if toleranced:
        low = value + min(plus or 0.0, minus or 0.0)
        high = value + max(plus or 0.0, minus or 0.0)
        if high - low < 1e-9:
            raise ValueError("A tolerance needs two different limits")
    if toleranced and spec.get("limits"):
        d = decimals_mm if decimals_mm is not None else max(_decimals(low, 3), _decimals(high, 3))
        text = _fmt(low, d) + "–" + _fmt(high, d)
    else:
        d = decimals_mm if decimals_mm is not None else _decimals(value, 2)
        text = _fmt(value, d) + _tolerance_text(spec)
    if spec.get("dual", True):
        if toleranced:
            smallest = min(abs(t) for t in (plus, minus) if t)
            text += " [" + _inch_limits(low, high, max(decimals_in, 3 if smallest >= 0.1 - 1e-9 else 4)) + "]"
        else:
            text += " [" + _fmt(value / 25.4, decimals_in, False) + "]"
    return spec.get("prefix", "") + text + spec.get("suffix", "")


def _hole_text(hole, count, spec):
    """Hole callout, one line per stage: "2X ⌀11 [.433] THRU" then ⌴ or ⌵, then any note.

    ``thread`` ("M5×0.8-6H") replaces the drill size on the first line, with
    THRU or the full-thread depth ``thread_depth``; ``plus``/``minus``/``limits``
    tolerate the hole's size only, never its depths.
    """
    plain = dict(spec)
    for key in ("prefix", "suffix", "plus", "minus", "decimals_mm", "limits"):
        plain.pop(key, None)

    def size(value):
        return _dual_text(plain, value)

    sized = dict(plain, plus=spec.get("plus"), minus=spec.get("minus"), limits=spec.get("limits"))
    count_text = "%dX " % count if count > 1 else ""
    if spec.get("thread"):
        first = count_text + spec["thread"]
        if hole["through"]:
            first += " THRU"
        elif spec.get("thread_depth"):
            first += " ↧ " + size(spec["thread_depth"])
        else:
            raise ValueError("A blind thread needs its full-thread depth (thread_depth)")
    else:
        first = count_text + "⌀" + _dual_text(sized, hole["diameter"])
        first += " THRU" if hole["through"] else " ↧ " + size(hole["depth"])
    lines = [first]
    if hole.get("cbore_diameter"):
        lines.append("⌴ ⌀" + size(hole["cbore_diameter"]) + " ↧ " + size(hole["cbore_depth"]))
    if hole.get("csink_diameter"):
        angle = hole["csink_angle"]
        lines.append("⌵ ⌀" + size(hole["csink_diameter"]) + " X " + _fmt(angle, _decimals(angle, 1)) + "°")
    if spec.get("note"):
        lines.append(spec["note"])
    return "\n".join(lines)


def _template_areas(svg, width, height):
    """Inner frame and title block of a template, in page mm (y up).

    The frame is the innermost of the long border lines drawn near the
    sheet's edges, whatever draws them (rect, line or path, inside any
    transformed group). The title block is the box, closed by drawn lines,
    around the template's editable texts in the frame's lower half. A
    template with neither gets 10 mm margins and no title block.
    """
    import xml.etree.ElementTree as ET

    def mul(a, b):
        return (a[0]*b[0] + a[2]*b[1], a[1]*b[0] + a[3]*b[1], a[0]*b[2] + a[2]*b[3],
                a[1]*b[2] + a[3]*b[3], a[0]*b[4] + a[2]*b[5] + a[4], a[1]*b[4] + a[3]*b[5] + a[5])

    def matrix(text):
        m = (1, 0, 0, 1, 0, 0)
        for kind, args in re.findall(r"(matrix|translate|scale|rotate)\s*\(([^)]*)\)", text or ""):
            v = [float(x) for x in re.split(r"[\s,]+", args.strip()) if x]
            if kind == "matrix" and len(v) == 6:
                t = tuple(v)
            elif kind == "translate":
                t = (1, 0, 0, 1, v[0], v[1] if len(v) > 1 else 0)
            elif kind == "scale":
                t = (v[0], 0, 0, v[-1], 0, 0)
            else:
                a = math.radians(v[0]); c, s = math.cos(a), math.sin(a)
                t = (c, s, -s, c, 0, 0)
                if len(v) == 3:
                    t = mul(mul((1, 0, 0, 1, v[1], v[2]), t), (1, 0, 0, 1, -v[1], -v[2]))
            m = mul(m, t)
        return m

    def apply(m, x, y):
        return m[0]*x + m[2]*y + m[4], m[1]*x + m[3]*y + m[5]

    try:
        root = ET.fromstring(svg)
    except Exception:
        return dict(frame=[10.0, 10.0, width - 10.0, height - 10.0], title_block=None, source="default margins")
    vb = [float(v) for v in re.split(r"[\s,]+", (root.get("viewBox") or "").strip()) if v]
    base = (width / vb[2], 0, 0, height / vb[3], -vb[0] * width / vb[2], -vb[1] * height / vb[3]) if len(vb) == 4 and vb[2] and vb[3] else (1, 0, 0, 1, 0, 0)
    segments, texts = [], []

    def walk(node, m):
        m = mul(m, matrix(node.get("transform")))
        tag = node.tag.split("}")[-1]
        if tag == "rect":
            try:
                x, y, w, h = (float(node.get(k)) for k in ("x", "y", "width", "height"))
                pts = [apply(m, *p) for p in ((x, y), (x + w, y), (x + w, y + h), (x, y + h), (x, y))]
                segments.extend(zip(pts, pts[1:]))
            except (TypeError, ValueError):
                pass
        elif tag == "line":
            try:
                segments.append((apply(m, float(node.get("x1")), float(node.get("y1"))),
                                 apply(m, float(node.get("x2")), float(node.get("y2")))))
            except (TypeError, ValueError):
                pass
        elif tag == "path":
            x = y = sx = sy = 0.0
            for cmd, args in re.findall(r"([MmLlHhVvZzCcSsQqTtAa])([^MmLlHhVvZzCcSsQqTtAa]*)", node.get("d") or ""):
                v = [float(n) for n in re.findall(r"-?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?", args)]
                rel = cmd.islower()
                c = cmd.upper()
                if c == "Z":
                    segments.append((apply(m, x, y), apply(m, sx, sy)))
                    x, y = sx, sy
                    continue
                step = {"M": 2, "L": 2, "H": 1, "V": 1, "C": 6, "S": 4, "Q": 4, "T": 2, "A": 7}[c]
                for i in range(0, len(v) - step + 1, step):
                    a = v[i:i + step]
                    if c == "H":
                        nx, ny = (x + a[0] if rel else a[0]), y
                    elif c == "V":
                        nx, ny = x, (y + a[0] if rel else a[0])
                    else:
                        nx, ny = (x + a[-2], y + a[-1]) if rel else (a[-2], a[-1])
                    if c == "M" and i == 0:
                        sx, sy = nx, ny
                    elif c in ("L", "H", "V") or (c == "M" and i > 0):
                        segments.append((apply(m, x, y), apply(m, nx, ny)))
                    x, y = nx, ny
        elif tag == "text" and any(k.endswith("editable") for k in node.attrib):
            try:
                texts.append(apply(m, float(node.get("x", 0)), float(node.get("y", 0))))
            except ValueError:
                pass
        for child in node:
            walk(child, m)

    walk(root, base)
    horiz = [(min(a[0], b[0]), max(a[0], b[0]), a[1]) for a, b in segments
             if abs(a[1] - b[1]) < 0.05 and abs(a[0] - b[0]) > 0.6 * width]
    vert = [(min(a[1], b[1]), max(a[1], b[1]), a[0]) for a, b in segments
            if abs(a[0] - b[0]) < 0.05 and abs(a[1] - b[1]) > 0.6 * height]
    # Borders run close to the sheet's edges; a title block's top line is further in
    tops = [s[2] for s in horiz if s[2] < 0.12 * height]
    bottoms = [s[2] for s in horiz if s[2] > 0.88 * height]
    lefts = [s[2] for s in vert if s[2] < 0.12 * width]
    rights = [s[2] for s in vert if s[2] > 0.88 * width]
    if not (tops and bottoms and lefts and rights):
        return dict(frame=[10.0, 10.0, width - 10.0, height - 10.0], title_block=None, source="default margins")
    # SVG y runs down: the inner frame is the innermost of the nested borders
    fx0, fx1, fy_top, fy_bottom = max(lefts), min(rights), max(tops), min(bottoms)
    frame = [fx0, height - fy_bottom, fx1, height - fy_top]
    block = None
    inside = [(x, y) for x, y in texts if fx0 < x < fx1 and (fy_top + fy_bottom) / 2 < y < fy_bottom]
    if inside:
        # Snap the box around the editable texts to the drawn lines that close it
        tx0 = min(x for x, _ in inside); ty0 = min(y for _, y in inside)

        def covers(lo, hi, a, b, share):
            return min(hi, max(a, b)) - max(lo, min(a, b)) >= share * (hi - lo)

        lefts_b = [a[0] for a, b in segments if abs(a[0] - b[0]) < 0.05 and fx0 + 1 < a[0] < tx0 - 0.5
                   and covers(ty0, fy_bottom, a[1], b[1], 0.5)]
        tops_b = [a[1] for a, b in segments if abs(a[1] - b[1]) < 0.05 and fy_top + 1 < a[1] < ty0 - 0.5
                  and covers(tx0, fx1, a[0], b[0], 0.5)]
        bx0 = min(lefts_b) if lefts_b else tx0 - 5
        by_top = max(tops_b) if tops_b else ty0 - 8
        block = [bx0, height - fy_bottom, fx1, height - by_top]
    return dict(frame=[round(v, 3) for v in frame], title_block=block and [round(v, 3) for v in block],
                source="template frame")


def _scale_text(scale):
    """1:1, 1:2, 2:1."""
    if scale >= 1:
        return _fmt(scale, _decimals(scale, 2)) + ":1"
    return "1:" + _fmt(1 / scale, _decimals(1 / scale, 2))


_SHARED = (_decimals, _fmt, _tolerance_text, _inch_limits, _dual_text, _hole_text, _template_areas, _scale_text)

# ---------------------------------------------------------------------------
# Helpers that only exist inside FreeCAD
# ---------------------------------------------------------------------------

_HELPERS = r'''
import math
import os
import re
import FreeCAD
import Part

V = FreeCAD.Vector
GUI = bool(getattr(FreeCAD, "GuiUp", False))
PLACEHOLDER = "__PLACEHOLDER__"
TO_CHECK = "__TO_CHECK__"
TECHDRAW = os.path.join(FreeCAD.getResourceDir(), "Mod", "TechDraw")

doc = FreeCAD.ActiveDocument if _args.get("doc_name") is None else FreeCAD.getDocument(_args["doc_name"])
if doc is None:
    raise ValueError("No document found")


def _num(value):
    return float(getattr(value, "Value", value))


def _obj(name, type_id=None, what=None):
    found = doc.getObject(name)
    if found is None:
        raise ValueError((what or "Object") + " not found: " + str(name))
    if type_id and not found.isDerivedFrom(type_id):
        raise ValueError(str(name) + " is a " + found.TypeId + ", not a " + (what or type_id))
    return found


def _page(name):
    return _obj(name, "TechDraw::DrawPage", "drawing page")


def _part_view(name):
    view = _obj(name, "TechDraw::DrawViewPart", "part view")
    if abs(_num(view.Rotation)) > 1e-9:
        raise ValueError(view.Label + " is rotated; these tools place annotations on unrotated views only")
    return view


def _pump(seconds):
    if not GUI:
        return
    import time
    from PySide import QtGui
    end = time.time() + seconds
    while True:
        QtGui.QApplication.processEvents()
        if time.time() >= end:
            return
        time.sleep(0.05)


def _elements(view, kind):
    getter = view.getVertexByIndex if kind == "Vertex" else view.getEdgeByIndex
    found = []
    while True:
        try:
            item = getter(len(found))
        except Exception:
            return found
        if item is None:
            return found
        found.append(item)


def _settle(views, timeout=20.0):
    # With the GUI up, TechDraw projects in a worker thread: wait for every view's lines
    import time
    doc.recompute()
    end = time.time() + timeout
    last = None
    while True:
        counts = [len(_elements(v, "Edge")) for v in views]
        if all(counts) and counts == last:
            return counts
        if time.time() > end:
            empty = [v.Label for v, c in zip(views, counts) if not c]
            if empty:
                raise ValueError("TechDraw drew no line in " + ", ".join(empty) + "; does the source have a solid shape?")
            return counts
        last = counts
        _pump(0.2)


def _group(view):
    for parent in view.InList:
        if parent.isDerivedFrom("TechDraw::DrawProjGroup"):
            return parent
    return None


def _page_of(view):
    holder = _group(view) or view
    for parent in holder.InList:
        if parent.isDerivedFrom("TechDraw::DrawPage"):
            return parent
    raise ValueError(view.Label + " is on no drawing page")


def _origin(view):
    x, y = _num(view.X), _num(view.Y)
    group = _group(view)
    if group is not None:
        x += _num(group.X)
        y += _num(group.Y)
    return x, y


def _sources(view):
    found = list(view.Source) + list(getattr(view, "XSource", []) or [])
    if not found and view.isDerivedFrom("TechDraw::DrawViewSection"):
        found = list(view.BaseView.Source)
    return found


_OFFSETS = dict()


def _offset(view):
    # TechDraw centres each view; find the shift from projectPoint() to the
    # view's own coordinates by letting projected source vertices vote.
    if view.Name in _OFFSETS:
        return _OFFSETS[view.Name]
    marks = [vx.Point for vx in _elements(view, "Vertex")]
    if not marks:
        raise ValueError(view.Label + " has no vertex yet: recompute the document")
    votes = dict()
    for source in _sources(view):
        for vx in Part.getShape(source).Vertexes[:80]:
            p = view.projectPoint(vx.Point)
            for q in marks:
                key = (round(p.x - q.x, 3), round(p.y - q.y, 3))
                votes[key] = votes.get(key, 0) + 1
    if not votes:
        raise ValueError(view.Label + " shows no vertex of its source")
    _OFFSETS[view.Name] = max(votes.items(), key=lambda kv: kv[1])[0]
    return _OFFSETS[view.Name]


def _to_view(view, point):
    p = view.projectPoint(V(*point))
    dx, dy = _offset(view)
    return p.x - dx, p.y - dy


def _to_page(view, xy):
    ox, oy = _origin(view)
    s = view.getScale()
    return ox + s * xy[0], oy + s * xy[1]


def _view_box(view):
    xs, ys = [], []
    for e in _elements(view, "Edge"):
        b = e.BoundBox
        xs += [b.XMin, b.XMax]
        ys += [b.YMin, b.YMax]
    if xs and view.isDerivedFrom("TechDraw::DrawViewSection"):
        # The cut faces' outlines are not among the view's edges: add the slice
        n = V(view.SectionNormal)
        n.normalize()
        for source in _sources(view):
            for wire in Part.getShape(source).slice(n, V(view.SectionOrigin).dot(n)):
                for vx in wire.Vertexes:
                    x, y = _to_view(view, vx.Point)
                    xs.append(x)
                    ys.append(y)
    if not xs:
        raise ValueError(view.Label + " has no line yet: recompute the document")
    return min(xs), min(ys), max(xs), max(ys)


def _page_box(view):
    x0, y0, x1, y1 = _view_box(view)
    ax, ay = _to_page(view, (x0, y0))
    bx, by = _to_page(view, (x1, y1))
    return [round(ax, 3), round(ay, 3), round(bx, 3), round(by, 3)]


_PX = 25.4 / 96


def _svg_size(view):
    svg = view.Symbol or ""
    found = re.search(r'<svg[^>]*\swidth="([\d.]+)mm"[^>]*\sheight="([\d.]+)mm"', svg)
    if found:
        w, h = float(found.group(1)), float(found.group(2))
    else:
        # Spreadsheet tables draw in pixels
        w = h = 0.0
        for r in re.finditer(r'<rect[^>]*\sx="([-\d.]+)"[^>]*\sy="([-\d.]+)"[^>]*\swidth="([\d.]+)"[^>]*\sheight="([\d.]+)"', svg):
            w = max(w, float(r.group(1)) + float(r.group(3)))
            h = max(h, float(r.group(2)) + float(r.group(4)))
        w *= _PX
        h *= _PX
    s = _num(view.Scale)
    return w * s, h * s


def _symbol_box(view):
    w, h = _svg_size(view)
    x, y = _num(view.X), _num(view.Y)
    return [round(x - w / 2, 3), round(y - h / 2, 3), round(x + w / 2, 3), round(y + h / 2, 3)]


def _sheet(page):
    template = page.Template
    if template is None:
        raise ValueError(page.Label + " has no template")
    width, height = _num(template.Width), _num(template.Height)
    svg = ""
    for path in (getattr(template, "Template", ""), getattr(template, "PageResult", "")):
        if path and os.path.exists(path):
            svg = open(path, encoding="utf-8", errors="replace").read()
            break
    areas = _template_areas(svg, width, height)
    areas.update(width=width, height=height)
    return areas


def _boxes(page, skip=()):
    found = []
    for v in page.Views:
        if v.Name in skip:
            continue
        try:
            if v.isDerivedFrom("TechDraw::DrawProjGroup"):
                found += [(item.Name, _page_box(item)) for item in v.Views if item.Name not in skip]
            elif v.isDerivedFrom("TechDraw::DrawViewPart"):
                found.append((v.Name, _page_box(v)))
            elif v.isDerivedFrom("TechDraw::DrawViewSymbol"):
                found.append((v.Name, _symbol_box(v)))
        except ValueError:
            pass
    return found


def _overlap(a, b, gap=0.0):
    return not (a[2] + gap <= b[0] or b[2] + gap <= a[0] or a[3] + gap <= b[1] or b[3] + gap <= a[1])


def _inside(box, area, margin=0.0):
    return (box[0] >= area[0] + margin - 1e-6 and box[1] >= area[1] + margin - 1e-6
            and box[2] <= area[2] - margin + 1e-6 and box[3] <= area[3] - margin + 1e-6)


def _free_spot(page, w, h, gap=10.0, skip=()):
    # First place, from the top-right corner, where a w x h box clears every view
    sheet = _sheet(page)
    f = sheet["frame"]
    taken = [b for _, b in _boxes(page, skip)] + [b for n, b in _note_boxes(page) if n.split(":")[0] not in skip]
    if sheet["title_block"]:
        taken.append(sheet["title_block"])
    y = f[3] - 4 - h / 2
    while y - h / 2 >= f[1] + 4:
        x = f[2] - 4 - w / 2
        while x - w / 2 >= f[0] + 4:
            box = [x - w / 2, y - h / 2, x + w / 2, y + h / 2]
            if not any(_overlap(box, t, gap) for t in taken):
                return x, y
            x -= 4
        y -= 4
    return None


def _vertex_at(view, point, tol=0.01):
    x, y = _to_view(view, point)
    best = None
    for i, vx in enumerate(_elements(view, "Vertex")):
        d = math.hypot(vx.Point.x - x, vx.Point.y - y)
        if best is None or d < best[0]:
            best = (d, i)
    if best is None or best[0] > tol:
        raise ValueError("No visible vertex of " + view.Label + " at the projection of " + str(list(point))
                         + ("" if best is None else " (nearest is " + str(round(best[0], 3)) + " mm away)")
                         + ". Give a corner that is drawn in this view, or use elements=[\"VertexN\"]")
    return "Vertex" + str(best[1])


def _circle_at(view, point, radius=None, tol=0.01):
    x, y = _to_view(view, point)
    found = []
    for i, e in enumerate(_elements(view, "Edge")):
        curve = e.Curve
        if type(curve).__name__ != "Circle":
            continue
        if math.hypot(curve.Center.x - x, curve.Center.y - y) > tol:
            continue
        if radius is not None and abs(curve.Radius - radius) > tol:
            continue
        found.append((curve.Radius, i))
    if not found:
        raise ValueError("No circle of " + view.Label + " centred on the projection of " + str(list(point))
                         + ("" if radius is None else " with radius " + str(radius))
                         + ": the axis must point at the viewer in this view")
    found.sort()
    return "Edge" + str(found[0][1]), found[0][0]


TEXT_HEIGHT = 3.5


def _note_rise(lines):
    # How far above its Y TechDraw centres a note of so many lines, in text heights
    return 1.35 + 0.274 * (lines - 1)


def _text_box(text, x, y, size=TEXT_HEIGHT):
    # Measured on TechDraw's own render: 0.66 size per character, lines 2.13 sizes apart
    lines = text.split("\n")
    w = 0.66 * size * max(len(line) for line in lines)
    h = (1.6 + 2.13 * (len(lines) - 1)) * size
    return [x - w / 2, y - h / 2, x + w / 2, y + h / 2]


def _exit(view, xy, ux, uy):
    # Unscaled distance from xy to the view's outline along (ux, uy)
    x0, y0, x1, y1 = _view_box(view)
    ts = []
    if ux > 1e-9:
        ts.append((x1 - xy[0]) / ux)
    if ux < -1e-9:
        ts.append((x0 - xy[0]) / ux)
    if uy > 1e-9:
        ts.append((y1 - xy[1]) / uy)
    if uy < -1e-9:
        ts.append((y0 - xy[1]) / uy)
    return max(0.0, min(ts)) if ts else 0.0


def _leader_out(view, xy, w, h, tail, gap=6.0):
    # A leader from xy that leaves the view diagonally and ends where a w x h
    # box (on the far side of a horizontal tail) clears the frame and every view.
    page = _page_of(view)
    sheet = _sheet(page)
    taken = [b for _, b in _boxes(page)] + [b for _, b in _note_boxes(page)]
    if sheet["title_block"]:
        taken.append(sheet["title_block"])
    s = view.getScale()
    sx0, sy0 = _to_page(view, xy)
    first = None
    for ux, uy in ((1, 1), (-1, 1), (1, -1), (-1, -1)):
        n = math.sqrt(2)
        reach = _exit(view, xy, ux / n, uy / n) * s + gap
        for extra in (0.0, 8.0, 16.0, 24.0):
            d = (reach + extra) / n
            dx, dy = ux * d, uy * d
            end = sx0 + dx + (tail if ux > 0 else -tail)
            box = [end, sy0 + dy - h / 2, end + w, sy0 + dy + h / 2] if ux > 0 else [end - w, sy0 + dy - h / 2, end, sy0 + dy + h / 2]
            if first is None:
                first = (dx, dy, box)
            if _inside(box, sheet["frame"], 2.0) and not any(_overlap(box, t, 2.0) for t in taken):
                return dx, dy, box
    return first


_DIAGONALS = dict(up_right=(1, 1), up_left=(-1, 1), down_left=(-1, -1), down_right=(1, -1))


def _note_spot(view, xy, text, first, gap, skip=()):
    # Text centre (relative to the view) for a leadered note on xy: outside the
    # view, inside the frame, clear of views, tables and other notes.
    page = _page_of(view)
    sheet = _sheet(page)
    taken = [b for _, b in _boxes(page)] + [b for n, b in _note_boxes(page) if n.split(":")[0] not in skip]
    if sheet["title_block"]:
        taken.append(sheet["title_block"])
    s = view.getScale()
    ox, oy = _origin(view)
    half = _text_box(text, 0, 0)[2]
    order = [first] + [k for k in _DIAGONALS if k != first]
    best, fallback = None, None
    for extra in (0.0, 8.0, 16.0, 24.0, 32.0):
        for key in order:
            ux, uy = [c / math.sqrt(2) for c in _DIAGONALS[key]]
            reach = _exit(view, xy, ux, uy) * s + gap + extra
            x = xy[0] * s + reach * ux + (half if ux > 0 else -half)
            y = xy[1] * s + reach * uy
            box = _text_box(text, ox + x, oy + y)
            if fallback is None:
                fallback = (x, y, float("inf"))
            fits = _inside(box, sheet["frame"], 2.0) and not any(_overlap(box, t, 1.0) for t in taken)
            if fits and (best is None or reach < best[2] - 1e-9):
                best = (x, y, reach)
        if best is not None:
            return best
    return fallback


def _note_boxes(page):
    # Dimensions, balloons and notes, as boxes on the sheet
    found = []
    for v in page.Views:
        try:
            if v.isDerivedFrom("TechDraw::DrawViewDimension") and v.References2D:
                parent = v.References2D[0][0]
                ox, oy = _origin(parent)
                x, y = ox + _num(v.X), oy + _num(v.Y)
                found.append((v.Name, _text_box(v.FormatSpec if v.Arbitrary else v.getText(), x, y)))
                if v.Type in ("DistanceX", "DistanceY"):
                    a, b = v.getLinearPoints()  # already scaled, unlike the view's vertices
                    if v.Type == "DistanceY":
                        found.append((v.Name + ":line", [x - 1, oy + min(a.y, b.y), x + 1, oy + max(a.y, b.y)]))
                    else:
                        found.append((v.Name + ":line", [ox + min(a.x, b.x), y - 1, ox + max(a.x, b.x), y + 1]))
            elif v.isDerivedFrom("TechDraw::DrawViewBalloon") and v.SourceView is not None:
                ox, oy = _origin(v.SourceView)
                s = v.SourceView.getScale()
                x, y = ox + s * _num(v.X), oy + s * _num(v.Y)
                found.append((v.Name, [x - 4.5, y - 4.5, x + 4.5, y + 4.5]))
            elif v.isDerivedFrom("TechDraw::DrawViewAnnotation"):
                size = _num(v.TextSize)
                found.append((v.Name, _text_box("\n".join(v.Text), _num(v.X), _num(v.Y) + _note_rise(len(v.Text)) * size, size)))
            elif v.isDerivedFrom("TechDraw::DrawLeaderLine") and v.LeaderParent is not None and v.WayPoints:
                # Start in unscaled view units, waypoints in sheet mm counted downwards
                ox, oy = _origin(v.LeaderParent)
                s = v.LeaderParent.getScale()
                sx, sy = ox + s * _num(v.X), oy + s * _num(v.Y)
                pts = [(sx + w.x, sy - w.y) for w in v.WayPoints]
                welded = any(o.isDerivedFrom("TechDraw::DrawWeldSymbol") for o in v.InList)
                (ax, ay), (bx, by) = pts[-2], pts[-1]
                tall = 9.0 if welded else 1.0
                found.append((v.Name, [min(ax, bx), min(ay, by) - tall, max(ax, bx), max(ay, by) + tall]))
        except Exception:
            pass
    return found


def _field(texts, key, aliases):
    # The template's own name for a title-block field, or None
    for name in aliases.get(key, [key]):
        if name in texts:
            return name
    return None


def _resolve_fields(texts, wanted, aliases):
    # {template field: value} for values given by short or template names
    out, unknown = dict(), []
    for key, value in wanted.items():
        name = key if key in texts else _field(texts, key, aliases)
        if name is None:
            unknown.append(key)
        else:
            out[name] = value
    return out, unknown


def _store_spec(obj, spec):
    if "DualSpec" not in obj.PropertiesList:
        obj.addProperty("App::PropertyString", "DualSpec", "DualUnits", "How the dimension text is rebuilt from the geometry")
    obj.DualSpec = json.dumps(spec, sort_keys=True)


def _style(dim, referencing):
    vo = getattr(dim, "ViewObject", None) if GUI else None
    if vo is not None and "StandardAndStyle" in vo.PropertiesList:
        vo.StandardAndStyle = "ASME Referencing" if referencing else "ASME Inlined"
    if vo is not None and "Fontsize" in vo.PropertiesList:
        vo.Fontsize = TEXT_HEIGHT


def _holes(objects, direction=None):
    # Concave full cylinders, grouped by axis: drill, counterbore, countersink
    holes = []
    for source in objects:
        shape = Part.getShape(source)
        if shape.isNull() or not shape.Solids:
            continue
        stacks = dict()
        for f in shape.Faces:
            surface = f.Surface
            kind = type(surface).__name__
            if kind not in ("Cylinder", "Cone"):
                continue
            a = V(surface.Axis)
            a.normalize()
            if direction is not None and abs(a.dot(direction)) < 0.9999:
                continue
            if a.x < -1e-9 or (abs(a.x) < 1e-9 and (a.y < -1e-9 or (abs(a.y) < 1e-9 and a.z < 0))):
                a = a * -1
            c = V(surface.Center) if kind == "Cylinder" else V(surface.Apex)
            foot = c - a * c.dot(a)
            u0, u1, v0, v1 = f.ParameterRange
            um, vm = (u0 + u1) / 2, (v0 + v1) / 2
            p, n = f.valueAt(um, vm), f.normalAt(um, vm)
            radial = p - (c + a * (p - c).dot(a))
            if radial.Length < 1e-9 or n.dot(radial) >= 0:
                continue  # convex: a boss or a shaft
            ends = [f.valueAt(um, v0), f.valueAt(um, v1)]
            ts = [(q - foot).dot(a) for q in ends]
            radii = [(q - (foot + a * t)).Length for q, t in zip(ends, ts)]
            key = (round(foot.x, 4), round(foot.y, 4), round(foot.z, 4), round(a.x, 4), round(a.y, 4), round(a.z, 4))
            stacks.setdefault(key, dict(axis=a, foot=foot, faces=[]))["faces"].append(
                dict(kind=kind, span=u1 - u0, t0=min(ts), t1=max(ts), r=max(radii),
                     semi=getattr(surface, "SemiAngle", 0.0)))
        clusters = []
        for stack in stacks.values():
            # Holes on one axis through separate walls are separate holes: split where material ends
            faces = sorted(stack["faces"], key=lambda f: f["t0"])
            group, end = [], None
            for face in faces:
                if group and face["t0"] > end + 1e-4:
                    clusters.append(dict(stack, faces=group))
                    group = []
                group.append(face)
                end = face["t1"] if len(group) == 1 else max(end, face["t1"])
            if group:
                clusters.append(dict(stack, faces=group))
        for stack in clusters:
            a, foot, faces = stack["axis"], stack["foot"], stack["faces"]
            cylinders = dict()
            for face in faces:
                if face["kind"] == "Cylinder":
                    k = (round(face["r"], 5), round(face["t0"], 5), round(face["t1"], 5))
                    cylinders[k] = cylinders.get(k, 0.0) + face["span"]
            full = sorted(k for k, span in cylinders.items() if span >= 2 * math.pi - 1e-6)
            if not full:
                continue  # a fillet or a slot, not a hole
            drill = full[0]
            t_lo = min(face["t0"] for face in faces)
            t_hi = max(face["t1"] for face in faces)
            open_lo = not shape.isInside(foot + a * (t_lo - 0.02), 1e-7, True)
            open_hi = not shape.isInside(foot + a * (t_hi + 0.02), 1e-7, True)
            if not (open_lo or open_hi):
                continue  # an inner void
            through = open_lo and open_hi
            bigger = [k for k in full[1:] if k[0] > drill[0] + 1e-6]
            cones = [face for face in faces if face["kind"] == "Cone"]
            # The entry is the open end that holds a counterbore or countersink
            if not through:
                entry_hi = open_hi
            else:
                stages = [k for k in bigger] + [(face["r"], face["t0"], face["t1"]) for face in cones]
                entry_hi = (sum(1 for k in stages if (k[1] + k[2]) / 2 > (drill[1] + drill[2]) / 2) >= 1) if stages else (direction is None or a.dot(direction) > 0)
            entry_t = t_hi if entry_hi else t_lo
            hole = dict(object=source.Name, center=[round(x, 6) for x in foot + a * entry_t],
                        axis=[round(x, 6) for x in (a if entry_hi else a * -1)], diameter=round(2 * drill[0], 6),
                        through=through, depth=None)
            if not through:
                far = drill[1] if entry_hi else drill[2]
                hole["depth"] = round(abs(entry_t - far), 6)
            if bigger:
                k = bigger[0]
                hole["cbore_diameter"] = round(2 * k[0], 6)
                hole["cbore_depth"] = round(k[2] - k[1], 6)
            entry_cones = [face for face in cones if face["r"] > drill[0] + 1e-6]
            if entry_cones:
                cone = entry_cones[0]
                hole["csink_diameter"] = round(2 * cone["r"], 6)
                hole["csink_angle"] = round(2 * abs(math.degrees(cone["semi"])), 6)
            holes.append(hole)
    return holes


def _visible_point(view, part):
    # Centre of the largest face of part that faces the viewer and that no
    # solid of the view hides (a ray towards the viewer meets nothing)
    d = V(*view.Direction)
    d.normalize()
    blockers = [Part.getShape(o) for o in _sources(view)]
    found = []
    for f in Part.getShape(part).Faces:
        u0, u1, v0, v1 = f.ParameterRange
        um, vm = (u0 + u1) / 2, (v0 + v1) / 2
        if f.normalAt(um, vm).dot(d) <= 1e-6:
            continue
        p = f.CenterOfMass
        if not f.isInside(p, 1e-6, True):
            p = f.valueAt(um, vm)
        found.append((f.Area, p))
    found.sort(key=lambda c: -c[0])
    for _, p in found:
        ray = Part.LineSegment(p + d * 0.05, p + d * 1e4).toShape()
        if all(b.common(ray).Length < 1e-6 for b in blockers):
            return p, True
    return (found[0][1] if found else Part.getShape(part).CenterOfMass), False


def _hole_signature(hole):
    keys = ("diameter", "through", "depth", "cbore_diameter", "cbore_depth", "csink_diameter", "csink_angle")
    return tuple((k, round(hole[k], 4) if isinstance(hole.get(k), float) else hole.get(k)) for k in keys)


def _remove(*names):
    # By name: deleting a page or a group also deletes what it holds
    for name in names:
        if doc.getObject(name) is not None:
            doc.removeObject(name)


def _place(view, x, y):
    # Put the centre of what the view draws at (x, y): its origin is not that centre
    x0, y0, x1, y1 = _view_box(view)
    s = view.getScale()
    view.X, view.Y = x - s * (x0 + x1) / 2, y - s * (y0 + y1) / 2
'''

_HELPERS = _HELPERS.replace("__PLACEHOLDER__", PLACEHOLDER).replace("__TO_CHECK__", TO_CHECK)
_PRELUDE = "import json\n" + _HELPERS + "\n\n" + "\n\n".join(inspect.getsource(fn) for fn in _SHARED)


def _script(body: str, **args: Any) -> str:
    """A FreeCAD script: the arguments as JSON, the helpers, then ``body``."""
    return "import json\n_args = json.loads(" + repr(json.dumps(args)) + ")\n" + _PRELUDE + "\n" + body


# ---------------------------------------------------------------------------
# GD&T and datum symbols, drawn as SVG paths (no font holds them all)
# ---------------------------------------------------------------------------

_GDT_SYMBOLS = {
    "straightness": '<path d="M1.4 4 H6.6"/>',
    "flatness": '<path d="M1.2 5.6 H5.3 L6.8 2.4 H2.7 Z"/>',
    "circularity": '<circle cx="4" cy="4" r="2.6"/>',
    "cylindricity": '<circle cx="4" cy="4" r="2.1"/><path d="M1.2 6.9 L4.6 0.9 M3.4 7.1 L6.8 1.1"/>',
    "profile_of_a_line": '<path d="M1.3 5.5 A2.7 2.7 0 0 1 6.7 5.5"/>',
    "profile_of_a_surface": '<path d="M1.3 5.5 A2.7 2.7 0 0 1 6.7 5.5 Z"/>',
    "angularity": '<path d="M6.8 6.2 H1.4 L5.8 1.6"/>',
    "perpendicularity": '<path d="M1.4 6.4 H6.6 M4 6.4 V1.4"/>',
    "parallelism": '<path d="M1.6 6.6 L4.4 1.4 M3.6 6.6 L6.4 1.4"/>',
    "position": '<circle cx="4" cy="4" r="2"/><path d="M4 0.8 V7.2 M0.8 4 H7.2"/>',
    "concentricity": '<circle cx="4" cy="4" r="2.8"/><circle cx="4" cy="4" r="1.5"/>',
    "symmetry": '<path d="M0.9 4 H7.1 M2.2 2.4 H5.8 M2.2 5.6 H5.8"/>',
    "circular_runout": '<path d="M2 6.8 L5.4 2.4"/><path d="M6.4 1.2 L4.3 2.3 L5.8 3.5 Z" fill="black"/>',
    "total_runout": ('<path d="M1.2 6.6 H5.4 M1.6 6.6 L4.2 2.6 M3.8 6.6 L6.4 2.6"/>'
                     '<path d="M5 1.2 L3.3 2.4 L4.7 3.4 Z M7.2 1.2 L5.5 2.4 L6.9 3.4 Z" fill="black"/>'),
}
_FORM = {"straightness", "flatness", "circularity", "cylindricity"}
_NEEDS_DATUM = {"angularity", "perpendicularity", "parallelism", "concentricity", "symmetry",
                "circular_runout", "total_runout"}
_DIAMETER_ZONE = {"straightness", "position", "perpendicularity", "parallelism", "angularity", "concentricity"}
_NO_MODIFIER = {"circularity", "cylindricity", "profile_of_a_line", "profile_of_a_surface",
                "circular_runout", "total_runout", "concentricity", "symmetry"}
_DATUM_LETTER = re.compile(r"^(?![IOQ]$)[A-HJ-NPR-Z]{1,2}$")
_FRAME_HEIGHT = 8.0
_TEXT = 3.5


def _text_width(text: str, size: float = _TEXT) -> float:
    return 0.62 * size * len(text)


def _modifier_svg(x: float, letter: str) -> str:
    return (f'<circle cx="{x:.2f}" cy="4" r="1.9" fill="none" stroke="black" stroke-width="0.3"/>'
            f'<text x="{x:.2f}" y="5.0" font-family="osifont" font-size="2.7" text-anchor="middle">{letter}</text>')


def _split_datum(ref: str) -> tuple[str, str | None]:
    found = re.fullmatch(r"\s*([A-Z]{1,2})\s*(?:\(([MLml])\))?\s*", ref)
    if not found:
        raise ValueError(f"Datum reference {ref!r}: give a letter, optionally with (M) or (L), e.g. 'B(M)'")
    letter, modifier = found.group(1), found.group(2)
    if not _DATUM_LETTER.match(letter):
        raise ValueError(f"Datum letter {letter!r}: I, O and Q are not used as datum letters")
    return letter, modifier.upper() if modifier else None


def _deviations(tolerance, upper, lower):
    """(plus, minus) deviations in mm from a ± tolerance or from upper and lower; (None, None) without."""
    if tolerance is not None and (upper is not None or lower is not None):
        raise ValueError("Give either tolerance (±) or upper and lower")
    if tolerance is not None:
        if not tolerance:
            raise ValueError("A ± tolerance must not be zero")
        return abs(tolerance), -abs(tolerance)
    if upper is not None or lower is not None:
        if upper is None or lower is None:
            raise ValueError("Give both upper and lower deviations")
        if upper <= lower:
            raise ValueError("upper must be above lower")
        return upper, lower
    return None, None


def check_gdt(characteristic: str, diameter_zone: bool, material_condition: str | None,
              datums: list[str]) -> list[str]:
    """Refuse a feature control frame that cannot be right; return warnings."""
    if characteristic not in _GDT_SYMBOLS:
        raise ValueError(f"Unknown characteristic {characteristic!r}; use one of {', '.join(sorted(_GDT_SYMBOLS))}")
    if characteristic in _FORM and datums:
        raise ValueError(f"{characteristic} is a form tolerance and takes no datum reference")
    if characteristic in _NEEDS_DATUM and not datums:
        raise ValueError(f"{characteristic} needs at least one datum reference")
    if diameter_zone and characteristic not in _DIAMETER_ZONE:
        raise ValueError(f"{characteristic} has no cylindrical tolerance zone: drop diameter_zone")
    if material_condition and characteristic in _NO_MODIFIER:
        raise ValueError(f"{characteristic} takes no material condition modifier (M or L)")
    if material_condition and material_condition.upper() not in ("M", "L"):
        raise ValueError("material_condition is 'M' (maximum) or 'L' (least)")
    if len(datums) > 3:
        raise ValueError("A frame holds at most three datum references (primary, secondary, tertiary)")
    letters = [_split_datum(d)[0] for d in datums]
    if len(set(letters)) != len(letters):
        raise ValueError("Each datum letter appears once in a frame")
    warnings = []
    if characteristic in ("concentricity", "symmetry"):
        warnings.append(f"{characteristic}: to verify, our notes say ASME Y14.5-2018 no longer lists it")
    if characteristic == "position" and not datums:
        warnings.append("position without datum reference: to verify, it is only meant for special cases")
    return warnings


def gdt_frame_svg(characteristic: str, tolerance: str, diameter_zone: bool = False,
                  material_condition: str | None = None, datums: list[str] | None = None,
                  projected: str | None = None) -> tuple[str, float]:
    """SVG of a feature control frame, and its width in mm (height 8 mm).

    ``projected`` is the height of a projected tolerance zone, written after a
    circled P: "⌀0.2 Ⓟ 6".
    """
    datums = datums or []
    h = _FRAME_HEIGHT
    tol_text = ("⌀" if diameter_zone else "") + tolerance
    extra = (4.6 if material_condition else 0.0) + ((5.2 + _text_width(projected)) if projected else 0.0)
    cells = [8.0, 3.0 + _text_width(tol_text) + extra]
    parsed = [_split_datum(d) for d in datums]
    cells += [3.0 + _text_width(letter) + (4.6 if mod else 0.0) for letter, mod in parsed]
    width = sum(cells)
    parts = [f'<rect x="0.175" y="0.175" width="{width - 0.35:.2f}" height="{h - 0.35:.2f}"/>']
    x = 0.0
    for cell in cells[:-1]:
        x += cell
        parts.append(f'<path d="M{x:.2f} 0 V{h:.2f}"/>')
    texts = [f'<g fill="none" stroke="black" stroke-width="0.3" stroke-linejoin="round">{_GDT_SYMBOLS[characteristic]}</g>']
    x = cells[0] + 1.5
    texts.append(f'<text x="{x:.2f}" y="5.25" font-family="osifont" font-size="{_TEXT}">{escape(tol_text)}</text>')
    after = cells[0] + 1.5 + _text_width(tol_text)
    if material_condition:
        texts.append(_modifier_svg(after + 2.3, material_condition.upper()))
        after += 4.6
    if projected:
        texts.append(_modifier_svg(after + 2.3, "P"))
        texts.append(f'<text x="{after + 5.2:.2f}" y="5.25" font-family="osifont" font-size="{_TEXT}">{escape(projected)}</text>')
    x = cells[0] + cells[1]
    for (letter, mod), cell in zip(parsed, cells[2:]):
        texts.append(f'<text x="{x + 1.5:.2f}" y="5.25" font-family="osifont" font-size="{_TEXT}">{letter}</text>')
        if mod:
            texts.append(_modifier_svg(x + cell - 2.8, mod))
        x += cell
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{width:.2f}mm" height="{h:.2f}mm" '
           f'viewBox="0 0 {width:.2f} {h:.2f}">'
           f'<g fill="none" stroke="black" stroke-width="0.35">{"".join(parts)}</g>{"".join(texts)}</svg>')
    return svg, width


def datum_symbol_svg(letter: str, side: str = "up", stem: float = 4.0) -> tuple[str, tuple[float, float]]:
    """SVG of a datum feature symbol, and where its triangle's base centre sits.

    ``side`` says where the letter box stands from the feature. The offset
    is from the symbol's centre to the base centre, in page mm (y up), so the
    symbol goes at ``feature_point - offset``.
    """
    letter, _ = _split_datum(letter)
    box = max(7.0, 3.0 + _text_width(letter))
    tri = 3.2
    if side in ("up", "down"):
        w, h = box, box + stem + tri
        bx, by = 0.0, (0.0 if side == "up" else stem + tri)
        if side == "up":
            line = f"M{w / 2:.2f} {box:.2f} V{box + stem:.2f}"
            triangle = f"M{w / 2 - 2.2:.2f} {h:.2f} H{w / 2 + 2.2:.2f} L{w / 2:.2f} {h - tri:.2f} Z"
            base = (w / 2, h)
        else:
            line = f"M{w / 2:.2f} {tri:.2f} V{tri + stem:.2f}"
            triangle = f"M{w / 2 - 2.2:.2f} 0 H{w / 2 + 2.2:.2f} L{w / 2:.2f} {tri:.2f} Z"
            base = (w / 2, 0.0)
    elif side in ("left", "right"):
        w, h = box + stem + tri, box
        bx, by = (0.0 if side == "left" else stem + tri), 0.0
        if side == "left":
            line = f"M{box:.2f} {h / 2:.2f} H{box + stem:.2f}"
            triangle = f"M{w:.2f} {h / 2 - 2.2:.2f} V{h / 2 + 2.2:.2f} L{w - tri:.2f} {h / 2:.2f} Z"
            base = (w, h / 2)
        else:
            line = f"M{tri:.2f} {h / 2:.2f} H{tri + stem:.2f}"
            triangle = f"M0 {h / 2 - 2.2:.2f} V{h / 2 + 2.2:.2f} L{tri:.2f} {h / 2:.2f} Z"
            base = (0.0, h / 2)
    else:
        raise ValueError("side is up, down, left or right: where the letter box stands from the feature")
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w:.2f}mm" height="{h:.2f}mm" viewBox="0 0 {w:.2f} {h:.2f}">'
           f'<rect x="{bx + 0.175:.2f}" y="{by + 0.175:.2f}" width="{box - 0.35:.2f}" height="{box - 0.35:.2f}" '
           f'fill="none" stroke="black" stroke-width="0.35"/>'
           f'<path d="{line}" fill="none" stroke="black" stroke-width="0.35"/>'
           f'<path d="{triangle}" fill="black" stroke="black" stroke-width="0.2"/>'
           f'<text x="{bx + box / 2:.2f}" y="{by + box / 2 + 1.25:.2f}" font-family="osifont" font-size="{_TEXT}" '
           f'text-anchor="middle">{letter}</text></svg>')
    # SVG y runs down: turn the base point into an offset from the centre, y up
    return svg, (base[0] - w / 2, h / 2 - base[1])


_WELD_FILES = {
    "fillet": ("filletDown.svg", "filletUp.svg"),
    "square": ("SquareDown.svg", "SquareUp.svg"),
    "v": ("VDown.svg", "VUp.svg"),
    "bead": ("beadDown.svg", "beadUp.svg"),
    "plug": ("plug.svg", "plug.svg"),
}


def _pdf_media_box(path: str) -> list[float] | None:
    data = open(path, "rb").read()
    found = re.search(rb"/MediaBox\s*\[\s*([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s*\]", data)
    return [float(v) for v in found.groups()] if found else None


def _report_markdown(report: dict[str, Any]) -> str:
    lines = [f"# Contrôle de la mise en plan {report['page']}", "",
             f"Statut : **{report['status']}** — {report['summary']}", "",
             "| Contrôle | Verdict | Preuve |", "| --- | --- | --- |"]
    for item in report["checks"]:
        evidence = str(item["evidence"]).replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {item['check']} | {item['verdict']} | {evidence} |")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# FreeCAD scripts
# ---------------------------------------------------------------------------

_CREATE_PAGE = r'''
name = _args["template"]
path = name
if not os.path.isabs(path):
    found = None
    for folder in ("ASME", ""):
        candidate = os.path.join(TECHDRAW, "Templates", folder, name if name.endswith(".svg") else name + ".svg")
        if os.path.exists(candidate):
            found = candidate
            break
    if found is None:
        asme = sorted(f[:-4] for f in os.listdir(os.path.join(TECHDRAW, "Templates", "ASME")) if f.endswith(".svg"))
        raise ValueError("Template not found: " + name + ". ASME templates: " + ", ".join(asme))
    path = found
if not os.path.exists(path):
    raise ValueError("Template not found: " + path)
page = doc.addObject("TechDraw::DrawPage", _args.get("page_name") or "Sheet")
template = doc.addObject("TechDraw::DrawSVGTemplate", page.Name + "_Template")
template.Template = path
page.Template = template
texts = dict(template.EditableTexts)
aliases = _args["aliases"]
fields, unknown = _resolve_fields(texts, _args["fields"], aliases)
if unknown:
    _remove(page.Name, template.Name)
    raise ValueError("Fields not in this template: " + ", ".join(sorted(unknown)) + ". Its fields: " + ", ".join(sorted(texts)))
defaults = dict()
for key in _args["identity"]:
    name = _field(texts, key, aliases)
    if name:
        defaults[name] = PLACEHOLDER
for key, value in (("checked_by", TO_CHECK), ("approved_1", ""), ("approved_2", ""), ("sheet", "1 / 1"),
                   ("revision", "?")):  # "?": the revision box is too narrow for the placeholder
    name = _field(texts, key, aliases)
    if name:
        defaults[name] = value
for key in texts:
    texts[key] = fields.get(key, defaults.get(key, ""))
template.EditableTexts = texts
doc.recompute()
sheet = _sheet(page)
_result_ = dict(page=page.Name, template=path, size=[sheet["width"], sheet["height"]], frame=sheet["frame"],
                title_block=sheet["title_block"], area_source=sheet["source"], fields=dict(template.EditableTexts))
'''

_FILL_TITLE_BLOCK = r'''
page = _page(_args["page_name"])
template = page.Template
texts = dict(template.EditableTexts)
fields, unknown = _resolve_fields(texts, _args["fields"], _args["aliases"])
if unknown and not _args.get("lenient"):
    raise ValueError("Fields not in this template: " + ", ".join(sorted(unknown)) + ". Its fields: " + ", ".join(sorted(texts)))
texts.update(fields)
template.EditableTexts = texts
doc.recompute()
after = dict(template.EditableTexts)
wrong = [k for k in fields if after.get(k) != fields[k]]
if wrong:
    raise ValueError("FreeCAD did not keep: " + ", ".join(wrong))
_result_ = dict(page=page.Name, fields=after)
'''

_ADD_VIEWS = r'''
page = _page(_args["page_name"])
sheet = _sheet(page)
frame, block = sheet["frame"], sheet["title_block"]
objects = [_obj(n) for n in _args["object_names"]]
for o in objects:
    if Part.getShape(o).isNull():
        raise ValueError(o.Name + " has no shape to draw")
d = V(*_args["front_direction"])
up = V(*_args["up_direction"])
if d.Length < 1e-9 or up.Length < 1e-9 or abs(d.normalize().dot(up.normalize())) > 1e-6:
    raise ValueError("front_direction and up_direction must be perpendicular, non-zero vectors")
right = up.cross(d)
group = doc.addObject("TechDraw::DrawProjGroup", "Projections")
page.addView(group)
group.Source = objects
group.ProjectionType = _args["projection"]
group.addProjection("Front")
group.Anchor.Direction = d
group.Anchor.XDirection = right
for name in _args["views"]:
    if name != "Front":
        group.addProjection(name)
group.ScaleType = "Custom"
group.Scale = 1.0
group.spacingX = group.spacingY = _args["spacing"]
iso = None
if _args["isometric"]:
    iso = doc.addObject("TechDraw::DrawViewPart", "Isometric")
    page.addView(iso)
    iso.Source = objects
    iso.Direction = d + up + right
    iso.XDirection = right - d
    iso.ScaleType = "Custom"
    iso.Scale = 1.0
items = list(group.Views)
for item in items:
    item.HardHidden = bool(_args["hidden_lines"])
_settle(items + ([iso] if iso else []))
sizes = dict()
for item in items + ([iso] if iso else []):
    x0, y0, x1, y1 = _view_box(item)
    sizes[item.Name] = (x1 - x0, y1 - y0)
margin = _args["margin"]


def _layout(s):
    # Place the group at s; True when every view clears the frame and the title block
    group.Scale = s
    group.X, group.Y = 0, 0
    _settle(items)
    boxes = [_page_box(i) for i in items]
    gx0, gy0 = min(b[0] for b in boxes), min(b[1] for b in boxes)
    gx1, gy1 = max(b[2] for b in boxes), max(b[3] for b in boxes)
    w, h = gx1 - gx0, gy1 - gy0
    rooms = [[frame[0], frame[1], block[0], frame[3]], [frame[0], block[3], frame[2], frame[3]]] if block else [list(frame)]
    for room in rooms:
        free_w = room[2] - room[0] - 2 * margin - w
        free_h = room[3] - room[1] - 2 * margin - h
        if free_w < 0 or free_h < 0:
            continue
        # Centred, a little left so the isometric finds the top-right corner
        sx = room[0] + margin + free_w * 0.4
        sy = room[1] + margin + free_h * 0.5
        group.X, group.Y = sx - gx0, sy - gy0
        return True
    return False


wanted = _args.get("scale")
scales = [wanted] if wanted else [s for s in _args["scales"]]
if not wanted:
    # Start from the largest standard scale the bare views could fit at
    fw = sum(sizes[i.Name][0] for i in items) + _args["spacing"] * (len(items) - 1)
    fh = sum(sizes[i.Name][1] for i in items) + _args["spacing"] * (len(items) - 1)
    room_w = frame[2] - frame[0] - 2 * margin
    room_h = frame[3] - frame[1] - 2 * margin
    guess = min(room_w / max(fw, 1e-9), room_h / max(fh, 1e-9)) * 2
    scales = [s for s in scales if s <= guess] or [scales[-1]]
placed = None
for s in scales:
    if _layout(s):
        placed = s
        break
if placed is None:
    leftovers = [i.Name for i in items] + ([iso.Name] if iso else [])
    group.purgeProjections()  # items first: deleting the anchor under its group corrupts the group
    _remove(group.Name, *leftovers)
    raise ValueError("The views do not fit on " + page.Label + " at " + ", ".join(_scale_text(s) for s in scales)
                     + ": use a larger template, fewer views, or a smaller scale")
_settle(items)
iso_scale = None
if iso:
    for s in [x for x in _args["scales"] if x <= placed]:
        iso.Scale = s
        w, h = sizes[iso.Name][0] * s, sizes[iso.Name][1] * s
        spot = _free_spot(page, w, h, gap=_args["spacing"] / 2, skip=(iso.Name,))
        if spot:
            _settle([iso])
            _place(iso, *spot)
            iso_scale = s
            break
    if iso_scale is None:
        _remove(iso.Name)
        iso = None
    else:
        _settle([iso])
texts = dict(page.Template.EditableTexts)
if "scale" in texts:
    texts["scale"] = _scale_text(placed)
    page.Template.EditableTexts = texts
doc.recompute()
boxes = dict((i.Type, _page_box(i)) for i in items)
if iso:
    boxes["Isometric"] = _page_box(iso)
outside = [k for k, b in boxes.items() if not _inside(b, frame)]
clash = [k for k, b in boxes.items() if block and _overlap(b, block)]
names = list(boxes)
crossing = [a + "/" + b for i, a in enumerate(names) for b in names[i + 1:] if _overlap(boxes[a], boxes[b])]
_verification = dict(outside_frame=outside, on_title_block=clash, overlapping=crossing)
if outside or clash or crossing:
    raise ValueError("Views placed badly (outside " + str(outside) + ", on title block " + str(clash)
                     + ", overlapping " + str(crossing) + "): give a smaller scale")
_result_ = dict(group=group.Name, views=dict((i.Type, i.Name) for i in items), isometric=iso.Name if iso else None,
                scale=placed, scale_text=_scale_text(placed), isometric_scale=iso_scale, boxes=boxes,
                projection=group.ProjectionType)
'''

_ADD_SECTION = r'''
base = _part_view(_args["base_view"])
page = _page_of(base)
normal = V(*_args["normal"])
if normal.Length < 1e-9:
    raise ValueError("normal must be a non-zero vector")
normal.normalize()
d = base.Direction
if abs(normal.dot(d)) > 1e-6:
    raise ValueError("The cutting plane must be seen edge-on in " + base.Label + ": normal must be perpendicular to its direction " + str(list(d)))
point = V(*_args["point"])
shapes = [Part.getShape(o) for o in _sources(base)]
cut_area = 0.0
for shape in shapes:
    for piece in shape.slice(normal, point.dot(normal)):
        try:
            cut_area += Part.Face(piece).Area
        except Exception:
            pass
if cut_area <= 1e-9:
    raise ValueError("The cutting plane through " + str(_args["point"]) + " misses the part")
section = doc.addObject("TechDraw::DrawViewSection", "Section" + _args["symbol"])
page.addView(section)
section.BaseView = base
section.Source = base.Source
section.SectionNormal = normal
section.Direction = normal
section.SectionOrigin = point
section.SectionSymbol = _args["symbol"]
section.ScaleType = "Custom"
section.Scale = _args.get("scale") or base.getScale()
section.Label = (_args.get("caption") or "SECTION") + " " + _args["symbol"] + "-" + _args["symbol"]
_settle([section])
x0, y0, x1, y1 = _view_box(section)
w, h = (x1 - x0) * section.getScale(), (y1 - y0) * section.getScale()
if _args.get("position"):
    section.X, section.Y = _args["position"]
else:
    spot = _free_spot(page, w, h + 8, gap=12, skip=(section.Name,))
    if spot is None:
        _remove(section.Name)
        raise ValueError("No free room for section " + _args["symbol"] + "-" + _args["symbol"] + " at " + _scale_text(section.getScale())
                         + ": give a position or a smaller scale")
    _place(section, spot[0], spot[1] + 4)
doc.recompute()
box = _page_box(section)
caption = doc.addObject("TechDraw::DrawViewAnnotation", "SectionCaption")
page.addView(caption)
caption.Text = [section.Label] + ([_scale_text(section.getScale())] if abs(section.getScale() - base.getScale()) > 1e-9 else [])
caption.TextSize = TEXT_HEIGHT
caption.X, caption.Y = (box[0] + box[2]) / 2, box[1] - 4 - _note_rise(len(caption.Text)) * TEXT_HEIGHT
doc.recompute()
_verification = dict(cut_area=round(cut_area, 6), lines=len(_elements(section, "Edge")))
_result_ = dict(name=section.Name, caption=caption.Name, label=section.Label, box=box,
                scale=section.getScale(), cut_area=round(cut_area, 6))
'''

_ADD_DIMENSION = r'''
view = _part_view(_args["view_name"])
page = _page_of(view)
kind = _args["kind"]
types = dict(horizontal="DistanceX", vertical="DistanceY", aligned="Distance", diameter="Diameter", radius="Radius")
refs = []
expected = None
if kind in ("horizontal", "vertical", "aligned"):
    if _args.get("elements"):
        refs = list(_args["elements"])
        if len(refs) != 2 or not all(r.startswith("Vertex") for r in refs):
            raise ValueError(kind + " dimensions take two vertices, e.g. elements=[\"Vertex3\", \"Vertex7\"]")
        a, b = [_elements(view, "Vertex")[int(r[6:])].Point for r in refs]
    else:
        points = _args.get("points") or []
        if len(points) != 2:
            raise ValueError(kind + " dimensions take two 3D points (model mm), e.g. points=[[0,0,0],[120,0,0]]")
        refs = [_vertex_at(view, p) for p in points]
        a, b = [V(*_to_view(view, p), 0) for p in points]
    expected = dict(horizontal=abs(a.x - b.x), vertical=abs(a.y - b.y), aligned=math.hypot(a.x - b.x, a.y - b.y))[kind]
elif kind in ("diameter", "radius"):
    if _args.get("elements"):
        refs = list(_args["elements"])
        edge = _elements(view, "Edge")[int(refs[0][4:])]
        if type(edge.Curve).__name__ != "Circle":
            raise ValueError(refs[0] + " is not a circle or arc")
        radius = edge.Curve.Radius
        centre = (edge.Curve.Center.x, edge.Curve.Center.y)
    else:
        if not _args.get("center"):
            raise ValueError(kind + " dimensions take the circle's centre as a 3D point: center=[x, y, z]")
        ref, radius = _circle_at(view, _args["center"], _args.get("radius"))
        refs = [ref]
        centre = _to_view(view, _args["center"])
    expected = 2 * radius if kind == "diameter" else radius
else:
    raise ValueError("kind is horizontal, vertical, aligned, diameter or radius")
if expected <= 1e-9:
    raise ValueError("The two points coincide in " + view.Label + " along that direction: nothing to dimension")
if _args.get("basic") and (_args.get("plus") is not None or _args.get("minus") is not None):
    raise ValueError("A basic dimension carries no tolerance")
dim = doc.addObject("TechDraw::DrawViewDimension", "Dimension")
dim.Type = types[kind]
dim.MeasureType = "Projected"
dim.References2D = [(view, r) for r in refs]
page.addView(dim)
doc.recompute()
raw = dim.getRawValue()
if abs(raw - expected) > 1e-6 * max(1.0, expected):
    _remove(dim.Name)
    raise ValueError("TechDraw measured " + str(raw) + " where the geometry gives " + str(expected) + "; dimension removed")
spec = dict(dual=_args["dual"], decimals_in=_args["decimals_in"], decimals_mm=_args.get("decimals_mm"),
            plus=_args.get("plus"), minus=_args.get("minus"), limits=bool(_args.get("limits")),
            suffix=_args.get("suffix") or "",
            prefix=(_args.get("prefix") or "") + dict(diameter="⌀", radius="R").get(kind, ""), kind=kind)
text = _dual_text(spec, raw)
_store_spec(dim, spec)
dim.Arbitrary = True
dim.FormatSpec = text
dim.TheoreticalExact = bool(_args.get("basic"))
_style(dim, kind in ("diameter", "radius"))
# Place it outside the view, stacked after the dimensions already on that side
s = view.getScale()
x0, y0, x1, y1 = _view_box(view)
side = _args.get("side") or dict(horizontal="below", vertical="right", aligned="above").get(kind, "up_right")
stack = 0
for other in page.Views:
    if other is not dim and other.isDerivedFrom("TechDraw::DrawViewDimension") and "DualSpec" in other.PropertiesList:
        if other.References2D and other.References2D[0][0] == view and json.loads(other.DualSpec).get("side") == side:
            stack += 1
gap = _args["offset"] + 8.0 * stack
half_text = (_text_box(text, 0, 0)[2])
if kind in ("diameter", "radius"):
    if side not in _DIAGONALS:
        raise ValueError("side is up_right, up_left, down_left or down_right for circles")
    dim.X, dim.Y, _ = _note_spot(view, centre, text, side, _args["offset"], skip=(dim.Name,))
else:
    mid = ((a.x + b.x) / 2 * s, (a.y + b.y) / 2 * s)

    def _linear(where):
        if where == "below":
            return mid[0], y0 * s - gap
        if where == "above":
            return mid[0], y1 * s + gap
        if where == "left":
            return x0 * s - gap - half_text, mid[1]
        if where == "right":
            return x1 * s + gap + half_text, mid[1]
        raise ValueError("side is below, above, left or right for linear dimensions")

    def _blocked(xy):
        ox, oy = _origin(view)
        box = _text_box(text, ox + xy[0], oy + xy[1])
        sheet = _sheet(page)
        others = [b for n, b in _boxes(page) if n != view.Name]
        if sheet["title_block"]:
            others.append(sheet["title_block"])
        return not _inside(box, sheet["frame"], 2.0) or any(_overlap(box, b, 1.0) for b in others)

    spot = _linear(side)
    if not _args.get("side") and _blocked(spot):
        other = dict(below="above", above="below", left="right", right="left")[side]
        if not _blocked(_linear(other)):
            side = other
            spot = _linear(side)
    # A text wider than the gap between extension lines goes beyond them,
    # on the side where it touches no other note
    span = expected * s
    outside = []
    if kind == "horizontal" and 2 * half_text + 8 > span:
        outside = [(mid[0] + d * (span / 2 + 4 + half_text), spot[1]) for d in (1, -1)]
    elif kind == "vertical" and 1.6 * TEXT_HEIGHT + 8 > span:
        outside = [(spot[0], mid[1] + d * (span / 2 + 4 + 0.8 * TEXT_HEIGHT)) for d in (1, -1)]
    if outside:
        ox_, oy_ = _origin(view)
        others = [b for n, b in _note_boxes(page) if n.split(":")[0] != dim.Name] + [b for n, b in _boxes(page) if n != view.Name]
        clear = [c for c in outside if not any(_overlap(_text_box(text, ox_ + c[0], oy_ + c[1]), b, 1.0) for b in others)]
        spot = (clear or outside)[0]
    dim.X, dim.Y = spot
spec["side"] = side
_store_spec(dim, spec)
doc.recompute()
_verification = dict(measured=raw, geometry=expected)
_result_ = dict(name=dim.Name, value_mm=round(raw, 6), text=dim.FormatSpec, references=refs, basic=dim.TheoreticalExact)
'''

_REFRESH_DUAL = r'''
page = _page(_args["page_name"])
doc.recompute()
changed, kept, broken = [], [], []
for dim in page.Views:
    if not dim.isDerivedFrom("TechDraw::DrawViewDimension") or "DualSpec" not in dim.PropertiesList:
        continue
    spec = json.loads(dim.DualSpec)
    try:
        if spec.get("kind") == "hole":
            view = dim.References2D[0][0]
            matches = [h for h in _holes([_obj(spec["object"])], V(*view.Direction))
                       if math.dist(h["center"], spec["center"]) < 1e-3 or _hole_signature(h) == tuple(tuple(x) for x in spec["signature"])]
            if not matches:
                raise ValueError("hole not found")
            count = sum(1 for h in _holes([_obj(o) for o in spec["objects"]], V(*view.Direction)) if _hole_signature(h) == _hole_signature(matches[0]))
            text = _hole_text(matches[0], count, spec)
        else:
            raw = dim.getRawValue()
            if raw <= 1e-9:
                raise ValueError("measures nothing")
            text = _dual_text(spec, raw)
    except Exception as error:
        broken.append(dict(name=dim.Name, error=str(error)))
        continue
    if dim.FormatSpec != text:
        changed.append(dict(name=dim.Name, before=dim.FormatSpec, after=text))
        dim.FormatSpec = text
    else:
        kept.append(dim.Name)
doc.recompute()
_result_ = dict(changed=changed, unchanged=kept, broken=broken)
'''

_ADD_HOLE_CALLOUTS = r'''
view = _part_view(_args["view_name"])
page = _page_of(view)
objects = [_obj(n) for n in _args["object_names"]] if _args.get("object_names") else _sources(view)
d = V(*view.Direction)
holes = _holes(objects, d)
groups = []
for h in holes:
    sig = _hole_signature(h)
    for g in groups:
        if g["signature"] == sig:
            g["holes"].append(h)
            break
    else:
        groups.append(dict(signature=sig, holes=[h]))
if _args.get("diameter") is not None:
    wanted = [g for g in groups if abs(g["holes"][0]["diameter"] - _args["diameter"]) < 1e-3]
    if not wanted:
        raise ValueError("No hole of ⌀" + str(_args["diameter"]) + " in this view; diameters found: "
                         + ", ".join(sorted(set("%g" % g["holes"][0]["diameter"] for g in groups))))
    groups = wanted
spec = dict(dual=_args["dual"], decimals_in=_args["decimals_in"], kind="hole", note=_args.get("note") or "")
for key in ("thread", "thread_depth", "plus", "minus", "limits"):
    if _args.get(key) is not None and _args.get(key) is not False:
        spec[key] = _args[key]
made, missed = [], []
s = view.getScale()
for g in groups:
    shown = []
    for h in g["holes"]:
        try:
            edge, radius = _circle_at(view, h["center"], h["diameter"] / 2)
            shown.append((h, edge, radius))
        except ValueError:
            continue
    if not shown:
        missed.append(dict(holes=len(g["holes"]), diameter=g["holes"][0]["diameter"], reason="no circle in this view"))
        continue
    text = _hole_text(shown[0][0], len(g["holes"]), spec)
    # The hole of the group whose leader is shortest carries the callout
    spots = [(_note_spot(view, _to_view(view, h["center"]), text, "up_left", _args["offset"]), h, edge, radius)
             for h, edge, radius in shown]
    spot, h, edge, radius = min(spots, key=lambda c: c[0][2])
    dim = doc.addObject("TechDraw::DrawViewDimension", "HoleCallout")
    dim.Type = "Diameter"
    dim.MeasureType = "Projected"
    dim.References2D = [(view, edge)]
    page.addView(dim)
    doc.recompute()
    raw = dim.getRawValue()
    if abs(raw - h["diameter"]) > 1e-6:
        _remove(dim.Name)
        raise ValueError("TechDraw measured " + str(raw) + " for a hole of " + str(h["diameter"]))
    entry = dict(spec, object=h["object"], objects=[o.Name for o in objects], center=h["center"],
                 signature=[list(x) for x in g["signature"]], side="up_left")
    _store_spec(dim, entry)
    dim.Arbitrary = True
    dim.FormatSpec = text
    _style(dim, True)
    dim.X, dim.Y = spot[0], spot[1]
    made.append(dict(name=dim.Name, text=text, holes=len(g["holes"]), diameter=h["diameter"], through=h["through"]))
doc.recompute()
_result_ = dict(callouts=made, not_shown=missed, holes_found=len(holes))
'''

_ADD_GDT = r'''
page = _page(_args["page_name"]) if _args.get("page_name") else None
leader = None
if _args.get("view_name"):
    view = _part_view(_args["view_name"])
    page = page or _page_of(view)
    point = _args.get("point")
    if not point:
        raise ValueError("With view_name, give the feature's 3D point (model mm) the leader points at")
    xy = _to_view(view, point)
    s = view.getScale()
    sx, sy = _to_page(view, xy)
    if _args.get("leader"):
        dx, dy = _args["leader"]
    else:
        dx, dy, _ = _leader_out(view, xy, _args["width"], 8.0, 4.0)
    tail = 4 if dx >= 0 else -4
    leader = doc.addObject("TechDraw::DrawLeaderLine", "GdtLeader")
    leader.LeaderParent = view
    leader.X, leader.Y = xy  # unscaled: TechDraw applies the view's scale
    leader.WayPoints = [V(0, 0, 0), V(dx, -dy, 0), V(dx + tail, -dy, 0)]  # waypoints count y downwards
    leader.StartSymbol = 0
    page.addView(leader)
    end_x = sx + dx + tail
    centre = (end_x + (_args["width"] / 2 if dx >= 0 else -_args["width"] / 2), sy + dy)
    kink = [round(sx + dx, 3), round(sy + dy, 3)]
elif _args.get("position"):
    if page is None:
        raise ValueError("Give page_name with a position")
    centre = tuple(_args["position"])
else:
    raise ValueError("Give either view_name and point (with a leader) or page_name and position")
symbol = doc.addObject("TechDraw::DrawViewSymbol", _args["kind"])
symbol.Symbol = _args["svg"]
page.addView(symbol)
symbol.X, symbol.Y = centre
for key, value in _args["properties"].items():
    symbol.addProperty("App::PropertyString", key, "GD&T", "Feature control data, read by check_drawing")
    setattr(symbol, key, value)
doc.recompute()
_result_ = dict(name=symbol.Name, leader=leader.Name if leader else None, center=[round(c, 3) for c in centre],
                box=_symbol_box(symbol), kink_at=kink if leader else None)
'''

_ADD_WELD = r'''
view = _part_view(_args["view_name"])
page = _page_of(view)
xy = _to_view(view, _args["point"])
s = view.getScale()
if _args.get("leader"):
    dx, dy = _args["leader"]
else:
    dx, dy, _ = _leader_out(view, xy, _args["reference_length"], 18.0, 0.0)
reference = _args["reference_length"] if dx >= 0 else -_args["reference_length"]
leader = doc.addObject("TechDraw::DrawLeaderLine", "WeldLeader")
leader.LeaderParent = view
leader.X, leader.Y = xy  # unscaled: TechDraw applies the view's scale
leader.WayPoints = [V(0, 0, 0), V(dx, -dy, 0), V(dx + reference, -dy, 0)]  # waypoints count y downwards
page.addView(leader)
weld = doc.addObject("TechDraw::DrawWeldSymbol", "WeldSymbol")
weld.Leader = leader
page.addView(weld)
weld.AllAround = bool(_args["all_around"])
weld.FieldWeld = bool(_args["field_weld"])
weld.TailText = _args.get("tail") or ""
doc.recompute()
tiles = dict((t.TileRow, t) for t in weld.InList if t.isDerivedFrom("TechDraw::DrawTileWeld"))
for row in (0, -1):
    if row not in tiles:
        t = doc.addObject("TechDraw::DrawTileWeld", "TileWeld")
        t.TileParent = weld
        t.TileRow = row
        tiles[row] = t
folder = os.path.join(TECHDRAW, "Symbols", "Welding", "AWS")
filled = []
for row, side in ((0, "arrow"), (-1, "other")):
    data = _args[side]
    if not data:
        continue
    path = os.path.join(folder, data["file"])
    if not os.path.exists(path):
        raise ValueError("Weld symbol file not found: " + path)
    tile = tiles[row]
    tile.SymbolFile = path
    tile.SymbolIncluded = path  # setting SymbolFile alone leaves the blank tile in place
    tile.LeftText = data.get("size") or ""
    tile.RightText = data.get("length") or ""
    filled.append((tile, path, side))
doc.recompute()
wrong = [side for tile, path, side in filled if open(tile.SymbolIncluded, "rb").read() != open(path, "rb").read()]
if wrong:
    raise ValueError("The weld symbol kept a blank tile on the " + ", ".join(wrong) + " side")
start = _to_page(view, xy)
_result_ = dict(name=weld.Name, leader=leader.Name, arrow_at=[round(c, 3) for c in start],
                kink_at=[round(start[0] + dx, 3), round(start[1] + dy, 3)],
                tiles=dict((side, os.path.basename(path)) for tile, path, side in filled))
'''

_ADD_TABLE = r'''
page = _page(_args["page_name"])
sheet = _sheet(page)
frame, block = sheet["frame"], sheet["title_block"]
rows = _args["rows"]
columns = _args["columns"]
name = _args["sheet_name"]
old = doc.getObject(name)
if old is not None and old.isDerivedFrom("Spreadsheet::Sheet"):
    old.clearAll()
    table = old
else:
    table = doc.addObject("Spreadsheet::Sheet", name)
letters = [chr(ord("A") + i) for i in range(len(columns))]
for letter, (title, width) in zip(letters, columns):
    table.set(letter + "1", str(title))
    table.setColumnWidth(letter, int(width))
for r, row in enumerate(rows, start=2):
    for letter, value in zip(letters, row):
        text = str(value)
        table.set(letter + str(r), "'" + text if text[:1] in ("=", "+", "-") else text)
table.setStyle("A1:" + letters[-1] + "1", "bold")
doc.recompute()
view = None
for v in page.Views:
    if v.isDerivedFrom("TechDraw::DrawViewSpreadsheet") and v.Source == table:
        view = v
if view is None:
    view = doc.addObject("TechDraw::DrawViewSpreadsheet", name + "View")
    page.addView(view)
    view.Source = table
view.CellStart = "A1"
view.CellEnd = letters[-1] + str(len(rows) + 1)
doc.recompute()
w, h = _svg_size(view)
where = _args["where"]
if where == "top_right":
    view.X, view.Y = frame[2] - w / 2, frame[3] - h / 2
elif where == "above_title_block":
    top = block[3] if block else frame[1]
    view.X, view.Y = frame[2] - w / 2, top + h / 2
else:
    view.X, view.Y = where
doc.recompute()
box = _symbol_box(view)
crossing = [n for n, b in _boxes(page, skip=(view.Name,)) if _overlap(box, b)]
if not _inside(box, frame) or crossing:
    spot = _free_spot(page, w, h, gap=4, skip=(view.Name,))
    if spot is None:
        raise ValueError("No free room for the " + name + " table (" + str(round(w)) + " x " + str(round(h)) + " mm)")
    view.X, view.Y = spot
    doc.recompute()
    box = _symbol_box(view)
_result_ = dict(sheet=table.Name, view=view.Name, box=box, rows=len(rows))
'''

_BOM_ITEMS = r'''
objects = [_obj(n) for n in _args["object_names"]]
items = []
for o in objects:
    shape = Part.getShape(o)
    if shape.isNull() or not shape.Solids:
        continue
    b = shape.BoundBox
    sig = (round(shape.Volume, 3), round(shape.Area, 3), tuple(sorted((round(b.XLength, 3), round(b.YLength, 3), round(b.ZLength, 3)))))
    for item in items:
        if item["signature"] == sig:
            item["objects"].append(o.Name)
            break
    else:
        material = getattr(getattr(o, "ShapeMaterial", None), "Name", "") or ""
        items.append(dict(signature=sig, objects=[o.Name], label=o.Label,
                          description=o.Label2 or PLACEHOLDER,
                          material=material if material and material != "Default" else PLACEHOLDER))
for number, item in enumerate(items, start=1):
    item["item"] = number
    item.pop("signature")
_result_ = dict(items=items)
'''

_ADD_BALLOONS = r'''
view = _part_view(_args["view_name"])
page = _page_of(view)
s = view.getScale()
x0, y0, x1, y1 = _view_box(view)
cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
d = V(*view.Direction)
made = []
sheet = _sheet(page)
gx, gy = _origin(view)
taken = [b for _, b in _boxes(page)] + [b for _, b in _note_boxes(page)]
if sheet["title_block"]:
    taken.append(sheet["title_block"])
plan = []
for item in _args["items"]:
    o = _obj(item["objects"][0])
    target, seen = _visible_point(view, o)
    ox, oy = _to_view(view, target)
    start = math.atan2(oy - cy, ox - cx) if math.hypot(ox - cx, oy - cy) > 1e-6 else math.pi / 4
    half = math.hypot(x1 - x0, y1 - y0) / 2 * s
    spot = None
    for ring in (0.0, 10.0, 20.0, 30.0):
        for k in (0, 1, -1, 2, -2, 3, -3, 4, -4, 5, -5, 6, -6):
            a = start + k * math.radians(15)
            r = half + _args["offset"] + ring
            px, py = gx + cx * s + r * math.cos(a), gy + cy * s + r * math.sin(a)
            box = [px - 5, py - 5, px + 5, py + 5]
            if _inside(box, sheet["frame"], 2.0) and not any(_overlap(box, t, 1.0) for t in taken):
                spot = (px, py)
                break
        if spot:
            break
    if spot is None:
        f = sheet["frame"]
        spot = (min(max(gx + cx * s + half * math.cos(start), f[0] + 7), f[2] - 7),
                min(max(gy + cy * s + half * math.sin(start), f[1] + 7), f[3] - 7))
    taken.append([spot[0] - 5, spot[1] - 5, spot[0] + 5, spot[1] + 5])
    plan.append(dict(item=item, obj=o, origin=(ox, oy), arrow=(gx + ox * s, gy + oy * s), spot=spot, seen=seen))


def _cross(p1, p2, p3, p4):
    def side(a, b, c):
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
    return side(p3, p4, p1) * side(p3, p4, p2) < 0 and side(p1, p2, p3) * side(p1, p2, p4) < 0


# Two crossing leaders swap their bubbles; each swap shortens the total, so it ends
for _ in range(len(plan) * len(plan)):
    swapped = False
    for i in range(len(plan)):
        for j in range(i + 1, len(plan)):
            a, b = plan[i], plan[j]
            if _cross(a["arrow"], a["spot"], b["arrow"], b["spot"]):
                a["spot"], b["spot"] = b["spot"], a["spot"]
                swapped = True
    if not swapped:
        break
crossings = sum(1 for i in range(len(plan)) for j in range(i + 1, len(plan))
                if _cross(plan[i]["arrow"], plan[i]["spot"], plan[j]["arrow"], plan[j]["spot"]))
for entry in plan:
    ox, oy = entry["origin"]
    spot = entry["spot"]
    balloon = doc.addObject("TechDraw::DrawViewBalloon", "Balloon")
    balloon.SourceView = view
    # Unscaled view coordinates: TechDraw applies the view's scale
    balloon.OriginX, balloon.OriginY = ox, oy
    balloon.X, balloon.Y = (spot[0] - gx) / s, (spot[1] - gy) / s
    balloon.Text = str(entry["item"]["item"])
    page.addView(balloon)
    made.append(dict(name=balloon.Name, item=entry["item"]["item"], object=entry["obj"].Name,
                     points_at_visible_face=entry["seen"], bubble_at=[round(spot[0], 3), round(spot[1], 3)],
                     arrow_at=[round(entry["arrow"][0], 3), round(entry["arrow"][1], 3)]))
doc.recompute()
_result_ = dict(balloons=made, crossing_leaders=crossings)
'''

_ADD_HOLE_TAGS = r'''
view = _part_view(_args["view_name"])
page = _page_of(view)
objects = [_obj(n) for n in _args["object_names"]] if _args.get("object_names") else _sources(view)
d = V(*view.Direction)
holes = [h for h in _holes(objects, d)]
if not holes:
    raise ValueError("No hole seen end-on in " + view.Label)
origin = _args.get("origin")
if origin:
    ox, oy = _to_view(view, origin)
else:
    x0, y0, x1, y1 = _view_box(view)
    ox, oy = x0, y0
s = view.getScale()
rows = []
holes.sort(key=lambda h: (round(_to_view(view, h["center"])[1], 3) * -1, _to_view(view, h["center"])[0]))
groups = []
for h in holes:
    sig = _hole_signature(h)
    if sig not in groups:
        groups.append(sig)
counts = dict((g, sum(1 for h in holes if _hole_signature(h) == g)) for g in groups)
spec = dict(dual=_args["dual"], decimals_in=_args["decimals_in"])
for i, h in enumerate(holes, start=1):
    tag = "A" + str(i)
    hx, hy = _to_view(view, h["center"])
    rows.append([tag, _dual_text(spec, abs(hx - ox)), _dual_text(spec, abs(hy - oy)), _hole_text(h, 1, spec).replace("\n", " ")])
    note = doc.addObject("TechDraw::DrawViewAnnotation", "HoleTag")
    page.addView(note)
    note.Text = [tag]
    note.TextSize = 3.5
    px, py = _to_page(view, (hx, hy))
    r = h["diameter"] / 2 * s
    note.X, note.Y = px + r + 2.5, py + r + 2.5
doc.recompute()
_result_ = dict(rows=rows, origin_view=[round(ox, 6), round(oy, 6)])
'''

_EXPORT = r'''
page = _page(_args["page_name"])
if not GUI:
    raise ValueError("Exporting a sheet needs FreeCAD's GUI: TechDraw renders pages there")
import FreeCADGui
import TechDrawGui
from PySide import QtGui
mdi = FreeCADGui.getMainWindow().findChild(QtGui.QMdiArea)
before = mdi.activeSubWindow() if mdi else None
doc.recompute()
page.ViewObject.doubleClicked()  # the page must be drawn on screen before export
_pump(1.0)
ext = os.path.splitext(path)[1].lower()
if os.path.exists(path):
    os.remove(path)
if ext == ".pdf":
    TechDrawGui.exportPageAsPdf(page, path)
elif ext == ".svg":
    TechDrawGui.exportPageAsSvg(page, path)
else:
    raise ValueError("Export to .pdf or .svg")
# get_view crashes with a sheet in front (#154): bring a 3D view of the document back
views3d = [w for w in mdi.subWindowList() if w.windowTitle().startswith(doc.Label + " :")]
if views3d:
    mdi.setActiveSubWindow(views3d[0])
elif before is not None:
    mdi.setActiveSubWindow(before)
if not os.path.exists(path) or os.path.getsize(path) < 200:
    raise ValueError("TechDraw wrote no file at " + path + "." + _snap_hint)
sheet = _sheet(page)
_result_ = dict(path=path, bytes=os.path.getsize(path), size_mm=[sheet["width"], sheet["height"]])
'''

_CHECK = r'''
page = _page(_args["page_name"])
_drawn = []
for _v in page.Views:
    if _v.isDerivedFrom("TechDraw::DrawProjGroup"):
        _drawn += list(_v.Views)
    elif _v.isDerivedFrom("TechDraw::DrawViewPart"):
        _drawn.append(_v)
if _drawn:
    try:
        _settle(_drawn, timeout=10)
    except ValueError:
        pass  # an empty view is reported below
sheet = _sheet(page)
frame, block = sheet["frame"], sheet["title_block"]
checks = []


def verdict(check, value, evidence):
    checks.append(dict(check=check, verdict=value, evidence=evidence))


parts, groups, dims, symbols, balloons, welds, tables = [], [], [], [], [], [], []
for v in page.Views:
    if v.isDerivedFrom("TechDraw::DrawProjGroup"):
        groups.append(v)
        parts += list(v.Views)
    elif v.isDerivedFrom("TechDraw::DrawViewPart"):
        parts.append(v)
    elif v.isDerivedFrom("TechDraw::DrawViewDimension"):
        dims.append(v)
    elif v.isDerivedFrom("TechDraw::DrawViewSpreadsheet"):
        tables.append(v)
    elif v.isDerivedFrom("TechDraw::DrawViewSymbol"):
        symbols.append(v)
    elif v.isDerivedFrom("TechDraw::DrawViewBalloon"):
        balloons.append(v)
    elif v.isDerivedFrom("TechDraw::DrawWeldSymbol"):
        welds.append(v)

# 1. Views drawn
empty = [p.Label for p in parts if not _elements(p, "Edge")]
invalid = [p.Label for p in parts if not p.isValid()]
if not parts:
    verdict("vues_dessinees", "FAIL", "no part view on the sheet")
elif empty or invalid:
    verdict("vues_dessinees", "FAIL", "empty: " + str(empty) + ", invalid: " + str(invalid))
else:
    verdict("vues_dessinees", "PASS", str(len(parts)) + " views, each with lines: " + ", ".join(p.Label for p in parts))

# 2. Inside the frame, off the title block, no overlap
boxes = _boxes(page)
outside = [n for n, b in boxes if not _inside(b, frame)]
on_block = [n for n, b in boxes if block and _overlap(b, block)]
crossing = [a + "/" + c for i, (a, b) in enumerate(boxes) for c, d in boxes[i + 1:] if _overlap(b, d)]
verdict("dans_le_cadre", "FAIL" if outside else "PASS", ("outside: " + str(outside)) if outside else "frame " + str(frame) + " holds " + str(len(boxes)) + " views and tables")
if block:
    verdict("cartouche_libre", "FAIL" if on_block else "PASS", ("on title block: " + str(on_block)) if on_block else "title block " + str(block) + " clear")
else:
    verdict("cartouche_libre", "NON_VERIFIE", "title block not found in the template")
notes = _note_boxes(page)
lost = [n for n, b in notes if not _inside(b, frame)]
on_block_notes = [n for n, b in notes if block and _overlap(b, block)]
if not notes:
    verdict("annotations_dans_le_cadre", "NON_APPLICABLE", "no dimension, balloon or note")
else:
    verdict("annotations_dans_le_cadre", "FAIL" if lost or on_block_notes else "PASS",
            ("outside the frame: " + str(lost) + ", on the title block: " + str(on_block_notes)) if lost or on_block_notes
            else str(len(notes)) + " dimensions, balloons and notes inside the frame, off the title block")
verdict("sans_chevauchement", "FAIL" if crossing else "PASS", ("overlapping: " + str(crossing)) if crossing else "no two views or tables overlap")

# 3. Projection
wanted = _args["projection"]
bad = [g.Label + "=" + g.ProjectionType for g in groups if g.ProjectionType != wanted]
if not groups:
    verdict("projection", "NON_APPLICABLE", "no projection group")
else:
    verdict("projection", "FAIL" if bad else "PASS", ("expected " + wanted + ": " + str(bad)) if bad else "every group in " + wanted)

# 4. Scale field
texts = dict(page.Template.EditableTexts)
if groups and "scale" in texts:
    want = _scale_text(groups[0].Scale)
    verdict("echelle_cartouche", "PASS" if texts["scale"] == want else "FAIL", "title block says " + repr(texts["scale"]) + ", main views at " + want)
else:
    verdict("echelle_cartouche", "NON_VERIFIE", "no projection group or no scale field")

# 5. Title block filled, no invented data
template_defaults = dict()
try:
    raw = open(page.Template.Template, encoding="utf-8").read()
    for found in re.finditer(r'freecad:editable="([^"]+)"[^>]*>(?:\s*<tspan[^>]*>)?([^<]*)<', raw):
        template_defaults[found.group(1)] = found.group(2)
except Exception:
    pass
defaults = [k for k, v in texts.items() if template_defaults.get(k) and v == template_defaults[k] and v.strip()]
missing = [k for k, v in texts.items() if v.strip() in (PLACEHOLDER, "?")]
approved = [k for k in ("Approved1", "Approved2") if texts.get(k, "").strip()]
checker_field = next((k for k in ("CheckedBy", "SupervisorName") if k in texts), None)
checker = texts.get(checker_field, "").strip() if checker_field else ""
verdict("cartouche_sans_valeur_du_gabarit", "FAIL" if defaults else "PASS",
        ("template sample text left in: " + str(defaults)) if defaults else "no field keeps the template's sample text")
verdict("cartouche_complet", "NON_VERIFIE" if missing else "PASS",
        ("to fill in by a person: " + ", ".join(missing)) if missing else "every field holds a value")
verdict("approbation", "NON_VERIFIE" if approved else "PASS",
        ("approval fields filled (" + ", ".join(approved) + "): check that a person signed") if approved else "no approval: the drawing stays " + TO_CHECK)
if checker_field:
    named = checker not in ("", TO_CHECK, PLACEHOLDER, "?")
    verdict("verifie_par", "NON_VERIFIE" if named else "PASS",
            ("'Checked by' names " + repr(checker) + ": confirm that this person checked the drawing; a tool does not")
            if named else "'Checked by' still reads " + repr(checker))

# 6. Dimensions
linear = [d for d in dims if d.Type in ("Distance", "DistanceX", "DistanceY", "Diameter", "Radius")]
broken = []
for d in dims:
    try:
        if not d.isValid() or d.getRawValue() <= 1e-9:
            broken.append(d.Name)
    except Exception:
        broken.append(d.Name)
if not dims:
    verdict("cotes_rattachees", "NON_VERIFIE", "the sheet has no dimension")
else:
    verdict("cotes_rattachees", "FAIL" if broken else "PASS", ("measure nothing: " + str(broken)) if broken else str(len(dims)) + " dimensions measure their geometry")
if _args["dual_units"]:
    single = [d.Name for d in linear if "DualSpec" not in d.PropertiesList or not json.loads(d.DualSpec).get("dual", True)]
    if not linear:
        verdict("cotes_doubles", "NON_APPLICABLE", "no linear dimension")
    else:
        verdict("cotes_doubles", "FAIL" if single else "PASS", ("mm only: " + str(single)) if single else "every linear dimension shows mm [in]")
else:
    verdict("cotes_doubles", "NON_APPLICABLE", "dual units not asked")
stale, recomputed = [], 0
for d in dims:
    if "DualSpec" not in d.PropertiesList or d.Name in broken:
        continue
    spec = json.loads(d.DualSpec)
    try:
        if spec.get("kind") == "hole":
            view = d.References2D[0][0]
            here = _holes([_obj(o) for o in spec["objects"]], V(*view.Direction))
            match = [h for h in here if math.dist(h["center"], spec["center"]) < 1e-3]
            if not match:
                stale.append(d.Name + ": its hole is gone")
                continue
            count = sum(1 for h in here if _hole_signature(h) == _hole_signature(match[0]))
            want = _hole_text(match[0], count, spec)
        else:
            want = _dual_text(spec, d.getRawValue())
    except Exception as error:
        stale.append(d.Name + ": " + str(error))
        continue
    recomputed += 1
    shown = d.FormatSpec
    appended = spec.get("kind") == "hole" and shown.startswith(want) and shown[len(want):len(want) + 1] in (" ", "\n")
    if shown != want and not appended:
        stale.append(d.Name + " shows " + repr(d.FormatSpec) + ", geometry gives " + repr(want))
if recomputed or stale:
    verdict("valeurs_recalculees", "FAIL" if stale else "PASS", ("; ".join(stale)) if stale else str(recomputed) + " texts recomputed from the geometry, all equal")
else:
    verdict("valeurs_recalculees", "NON_APPLICABLE", "no dimension written by these tools")
commas = [d.Name for d in dims if re.search(r"\d,\d", d.getText() if hasattr(d, "getText") else d.FormatSpec)]
verdict("separateur_decimal", "FAIL" if commas else "PASS", ("decimal comma in: " + str(commas)) if commas else "decimal point everywhere")

# 7. Holes called out. An assembly is judged on its main view group, so the
# copies an exploded view is drawn from are not counted twice.
sources = []
for p in (list(groups[0].Views) if groups else parts):
    for o in _sources(p):
        if o not in sources:
            sources.append(o)
seen, callouts = dict(), set()
for p in parts:
    for h in _holes(sources, V(*p.Direction)):
        key = (h["object"], tuple(round(c, 3) for c in h["center"]))
        seen.setdefault(key, h)
for d in dims:
    if "DualSpec" in d.PropertiesList and json.loads(d.DualSpec).get("kind") == "hole":
        spec = json.loads(d.DualSpec)
        for key, h in seen.items():
            if _hole_signature(h) == tuple(tuple(x) for x in spec["signature"]):
                callouts.add(key)
    elif d.Type == "Diameter":
        try:
            for key, h in seen.items():
                if abs(d.getRawValue() - h["diameter"]) < 1e-6:
                    callouts.add(key)
        except Exception:
            pass
solid_count = sum(1 for o in sources if not Part.getShape(o).isNull() and Part.getShape(o).Solids)
assembly = solid_count > 1 and any(t.Source and t.Source.Name.startswith(_args["bom_sheet"]) for t in tables)
if assembly:
    verdict("percages_cotes", "NON_APPLICABLE", "assembly drawing with a parts list: holes are called out on the detail drawings")
elif not seen:
    verdict("percages_cotes", "NON_APPLICABLE", "no hole found in the parts")
else:
    left = sorted(set(seen) - callouts)
    verdict("percages_cotes", "FAIL" if left else "PASS",
            ("holes without callout: " + str([list(k[1]) for k in left])) if left else str(len(seen)) + " holes, each called out")

# 8. GD&T
frames = [s for s in symbols if "GdtCharacteristic" in s.PropertiesList]
datums = set(s.GdtDatum for s in symbols if "GdtDatum" in s.PropertiesList)
if not frames:
    verdict("gdt_references", "NON_APPLICABLE", "no feature control frame")
else:
    used = set()
    for f in frames:
        used |= set(x for x in f.GdtDatums.split(",") if x)
    missing_datums = sorted(used - datums)
    verdict("gdt_references", "FAIL" if missing_datums else "PASS",
            ("datums referenced but not shown: " + str(missing_datums)) if missing_datums else "frames use " + str(sorted(used)) + ", all shown")
verdict("gdt_semantique", "NON_VERIFIE", "whether each tolerance suits its function is an engineering decision")

# 9. Welds
solids = [o for o in sources if not Part.getShape(o).isNull()]
joints = 0
for i, a in enumerate(solids):
    for b in solids[i + 1:]:
        sa, sb = Part.getShape(a), Part.getShape(b)
        if sa.BoundBox.intersect(sb.BoundBox) and sa.distToShape(sb)[0] < 1e-6:
            joints += 1
if joints == 0:
    verdict("soudures", "NON_APPLICABLE", "no two touching parts")
else:
    verdict("soudures", "NON_VERIFIE", str(joints) + " joints between touching parts, " + str(len(welds)) + " weld symbols: whether each joint is welded is a design decision")

# 10. Parts list
if len(solids) < 2:
    verdict("nomenclature", "NON_APPLICABLE", "single part")
else:
    bom = [t for t in tables if t.Source and t.Source.Name.startswith(_args["bom_sheet"])]
    texts_b = set(b.Text for b in balloons)
    if not bom:
        verdict("nomenclature", "FAIL", str(len(solids)) + " parts and no parts list")
    else:
        sheet_obj = bom[0].Source
        numbers, total = [], 0
        r = 2
        while sheet_obj.getContents("A" + str(r)):
            numbers.append(sheet_obj.getContents("A" + str(r)).lstrip("'"))
            total += int(sheet_obj.getContents("B" + str(r)).lstrip("'") or 0)
            r += 1
        no_balloon = [n for n in numbers if n not in texts_b]
        problems = []
        if total != len(solids):
            problems.append("quantities add up to " + str(total) + " for " + str(len(solids)) + " parts")
        if no_balloon:
            problems.append("items without balloon: " + str(no_balloon))
        verdict("nomenclature", "FAIL" if problems else "PASS", "; ".join(problems) if problems else str(len(numbers)) + " items, quantities add up, each has a balloon")

# 11. What a tool cannot decide
verdict("cotation_complete", "NON_VERIFIE", "whether the part is fully and functionally dimensioned is for a person to judge")
verdict("conformite_norme", "NON_VERIFIE", "the standard's text is not available here; no conformity is declared")

counts = dict((k, sum(1 for c in checks if c["verdict"] == k)) for k in ("PASS", "FAIL", "NON_VERIFIE", "NON_APPLICABLE"))
_result_ = dict(page=page.Name, status=TO_CHECK, counts=counts, checks=checks,
                summary=", ".join(str(v) + " " + k for k, v in counts.items()))
'''


def register_drawing_tools(mcp: Any, get_bridge: Callable[[], Awaitable[Any]]) -> None:
    """Register the TechDraw drawing tools."""

    async def run(body: str, failure: str, **args: Any) -> dict[str, Any]:
        result = await (await get_bridge()).execute_python(_script(body, **args))
        if result.success:
            return result.result
        raise ValueError(result.error_traceback or failure)

    @mcp.tool()
    async def create_drawing_page(
        template: str = "ANSIB_Landscape",
        page_name: str | None = None,
        title: str | None = None,
        drawing_number: str | None = None,
        revision: str | None = None,
        drawn_by: str | None = None,
        company: str | None = None,
        fields: dict[str, str] | None = None,
        doc_name: str | None = None,
    ) -> dict[str, Any]:
        """Create a TechDraw sheet on a template and fill its title block.

        Title, number, drafter, company and weight left out read
        "À RENSEIGNER" (to be filled in by a person); "Checked by" reads
        "À VÉRIFIER", approvals and other fields stay empty: a tool never
        invents, checks or approves. Short field names work on every ASME
        template, whose sizes A-B and C-E name their fields differently.

        Args:
            template: An ASME template name (ANSIA_Landscape, ANSIB_Landscape,
                ANSIC_Landscape, ANSID_Landscape, ANSIE_Landscape, ...) or an
                absolute path to an SVG template.
            page_name: Name of the page object; "Sheet" if None.
            title: Drawing title.
            drawing_number: Drawing number.
            revision: Revision letter.
            drawn_by: Drafter's name or initials.
            company: Company name.
            fields: Other title-block fields by their template name, e.g.
                {"Weight": "1.2 kg"}.
            doc_name: Document. Uses active document if None.

        Returns:
            The page name, its size, the inner frame and title block in page
            mm (y up), and the title-block fields as written.
        """
        values = dict(fields or {})
        for key, value in (("title", title), ("drawing_number", drawing_number), ("revision", revision),
                           ("drawn_by", drawn_by), ("company", company)):
            if value is not None:
                values[key] = value
        return await run(_CREATE_PAGE, "Creating the drawing page failed", template=template,
                         page_name=page_name, fields=values, aliases=TITLE_FIELDS, identity=list(_IDENTITY_FIELDS),
                         doc_name=doc_name)

    @mcp.tool()
    async def fill_title_block(page_name: str, fields: dict[str, str], doc_name: str | None = None) -> dict[str, Any]:
        """Write title-block fields of a sheet.

        Args:
            page_name: The drawing page.
            fields: Values by the short names title, title_2, drawing_number,
                revision, drawn_by, checked_by, company, weight, date, sheet,
                which find the field under each template's own name, or by
                template field name (DrawingTitle1, AuthorName, ...).
            doc_name: Document. Uses active document if None.

        Returns:
            Every field of the title block after the change.
        """
        return await run(_FILL_TITLE_BLOCK, "Filling the title block failed", page_name=page_name,
                         fields=dict(fields), aliases=TITLE_FIELDS, doc_name=doc_name)

    @mcp.tool()
    async def add_drawing_views(
        page_name: str,
        object_names: list[str],
        views: list[str] | None = None,
        isometric: bool = True,
        scale: float | None = None,
        projection: str = "Third angle",
        front_direction: list[float] | None = None,
        up_direction: list[float] | None = None,
        hidden_lines: bool = False,
        doc_name: str | None = None,
    ) -> dict[str, Any]:
        """Place orthographic views (and an isometric) of parts on a sheet.

        Picks the largest standard scale (10:1 ... 1:100) at which the views,
        with room for dimensions, fit in the frame without touching the
        title block, then checks the placement on the sheet itself.

        Args:
            page_name: The drawing page.
            object_names: Parts (solids, bodies) to draw together.
            views: Projections; default ["Front", "Top", "Right"]. Also Left,
                Rear, Bottom and the four FrontTop/FrontBottom corners.
            isometric: Add an isometric view in the free space, top right first.
            scale: A fixed scale (0.5 for 1:2) instead of the automatic one.
            projection: "Third angle" (ASME) or "First angle" (ISO).
            front_direction: Direction from the part to the viewer of the front
                view, default [0, -1, 0] (looking along +Y).
            up_direction: Model direction that points up in the front view,
                default [0, 0, 1].
            hidden_lines: Draw hidden lines dashed in every orthographic view;
                off by default, since they clutter all but simple parts.
            doc_name: Document. Uses active document if None.

        Returns:
            View names by projection, the scale, and each view's box on the
            sheet in mm.
        """
        views = views or ["Front", "Top", "Right"]
        bad = [v for v in views if v not in PROJECTIONS]
        if bad:
            raise ValueError(f"Unknown projections {bad}; use {', '.join(PROJECTIONS)}")
        if "Front" not in views:
            views = ["Front"] + views
        if projection not in ("Third angle", "First angle"):
            raise ValueError("projection is 'Third angle' (ASME) or 'First angle' (ISO)")
        if scale is not None and not (math.isfinite(scale) and scale > 0):
            raise ValueError("scale must be a positive number, e.g. 0.5 for 1:2")
        return await run(_ADD_VIEWS, "Adding the views failed", page_name=page_name, object_names=object_names,
                         views=views, isometric=isometric, scale=scale, projection=projection,
                         front_direction=front_direction or [0, -1, 0], up_direction=up_direction or [0, 0, 1],
                         hidden_lines=hidden_lines, scales=list(STANDARD_SCALES), margin=15.0, spacing=25.0,
                         doc_name=doc_name)

    @mcp.tool()
    async def add_section_view(
        base_view: str,
        point: list[float],
        normal: list[float],
        symbol: str = "A",
        scale: float | None = None,
        position: list[float] | None = None,
        caption: str = "SECTION",
        doc_name: str | None = None,
    ) -> dict[str, Any]:
        """Cut a section through a view and place it in the free space.

        Args:
            base_view: The view the cutting plane line is drawn on.
            point: A 3D point (model mm) the cutting plane passes through,
                e.g. a hole's centre.
            normal: The plane's normal, pointing at the viewer: the section
                shows the material on the other side, e.g. [1, 0, 0] shows
                what lies at x below the point. It must lie flat in the base view.
            symbol: Section letter: the view is captioned "SECTION A-A".
            scale: Scale of the section; the base view's if None.
            position: Centre on the sheet [x, y] in mm; free space if None.
            caption: Word before the letters, "SECTION" or "COUPE" for a
                drawing in French: "COUPE A-A".
            doc_name: Document. Uses active document if None.

        Returns:
            The section's name, caption, box on the sheet and the area cut.
        """
        if not re.fullmatch(r"[A-HJ-NPR-WYZ]{1,2}", symbol):
            raise ValueError("symbol is one or two capital letters, without I, O, Q or X")
        if not re.fullmatch(r"[A-ZÀ-Ü][A-ZÀ-Ü ]*", caption):
            raise ValueError("caption is a word in capitals, e.g. SECTION or COUPE")
        return await run(_ADD_SECTION, "Adding the section failed", base_view=base_view, point=point,
                         normal=normal, symbol=symbol, scale=scale, position=position, caption=caption,
                         doc_name=doc_name)

    @mcp.tool()
    async def add_dimension(
        view_name: str,
        kind: str,
        points: list[list[float]] | None = None,
        center: list[float] | None = None,
        radius: float | None = None,
        elements: list[str] | None = None,
        side: str | None = None,
        offset: float = 10.0,
        tolerance: float | None = None,
        upper: float | None = None,
        lower: float | None = None,
        basic: bool = False,
        limits: bool = False,
        dual: bool = True,
        decimals_mm: int | None = None,
        decimals_in: int = 3,
        prefix: str = "",
        suffix: str = "",
        doc_name: str | None = None,
    ) -> dict[str, Any]:
        """Dimension a view in mm [in], tied to the drawn geometry.

        Give model points, not view coordinates: the tool finds the vertex
        or circle they project to, and refuses a point that is not drawn
        there. The text is rebuilt from TechDraw's own measurement, which
        must equal the geometry's.

        Args:
            view_name: The view to dimension.
            kind: "horizontal", "vertical", "aligned", "diameter" or "radius".
            points: For linear kinds, two 3D points (model mm) on drawn corners.
            center: For diameter and radius, the circle's centre (3D).
            radius: Picks one circle when several share the centre.
            elements: View element names instead of points, e.g.
                ["Vertex3", "Vertex7"] or ["Edge4"].
            side: "below", "above", "left" or "right" for linear kinds;
                "up_right", "up_left", "down_left", "down_right" for circles.
            offset: Distance in sheet mm from the view to the dimension line;
                later dimensions on the same side stack 8 mm further out.
            tolerance: Symmetric tolerance in mm (±).
            upper: Upper deviation in mm, e.g. 0.2 (with lower).
            lower: Lower deviation in mm, e.g. -0.1 (with upper).
            basic: Basic (theoretically exact) dimension, drawn boxed.
            limits: Write the toleranced size as its limits, "20.02–20.04"
                (lower first), instead of nominal and deviations.
            dual: Show inches in brackets after mm; a toleranced dimension
                shows its inch limits, rounded inward ("[.7882–.7889]").
            decimals_mm: Fixed mm decimals; fewest exact decimals if None.
            decimals_in: Inch decimals (at least; 4 for deviations under 0.1 mm).
            prefix: Text before the value, e.g. "2X ".
            suffix: Text after it, e.g. " THRU".
            doc_name: Document. Uses active document if None.

        Returns:
            The dimension's name, measured value in mm and text.
        """
        plus, minus = _deviations(tolerance, upper, lower)
        if limits and plus is None:
            raise ValueError("limits needs a tolerance (tolerance, or upper and lower)")
        return await run(_ADD_DIMENSION, "Adding the dimension failed", view_name=view_name, kind=kind,
                         points=points, center=center, radius=radius, elements=elements, side=side,
                         offset=offset, plus=plus, minus=minus, basic=basic, limits=limits, dual=dual,
                         decimals_mm=decimals_mm, decimals_in=decimals_in, prefix=prefix, suffix=suffix,
                         doc_name=doc_name)

    @mcp.tool()
    async def refresh_dual_dimensions(page_name: str, doc_name: str | None = None) -> dict[str, Any]:
        """Rewrite every mm [in] text of a sheet from the current geometry.

        Run it after changing the model: TechDraw's measured values follow
        the model, the texts do not.

        Args:
            page_name: The drawing page.
            doc_name: Document. Uses active document if None.

        Returns:
            The texts changed (before and after), those already right, and
            the dimensions that lost their geometry.
        """
        return await run(_REFRESH_DUAL, "Refreshing the dimensions failed", page_name=page_name, doc_name=doc_name)

    @mcp.tool()
    async def add_hole_callouts(
        view_name: str,
        object_names: list[str] | None = None,
        diameter: float | None = None,
        thread: str | None = None,
        thread_depth: float | None = None,
        tolerance: float | None = None,
        upper: float | None = None,
        lower: float | None = None,
        limits: bool = False,
        dual: bool = True,
        decimals_in: int = 3,
        offset: float = 8.0,
        note: str | None = None,
        doc_name: str | None = None,
    ) -> dict[str, Any]:
        """Call out every hole seen end-on in a view, from the 3D geometry.

        Holes are found in the solids themselves, so imported STEP parts
        work too: drill diameter, THRU or depth (↧), counterbore (⌴) and
        countersink (⌵); identical holes share one callout "2X ...".
        Threads are not recognised in the solid: give ``thread`` for the
        group of tapped holes, modelled at their tap-drill diameter.

        Args:
            view_name: The view where the holes appear as circles.
            object_names: Parts to search; the view's sources if None.
            diameter: Only the holes of this modelled diameter (mm); all if None.
            thread: Thread designation that replaces the drill size, e.g.
                "M5×0.8-6H" or "1/4-20 UNC-2B" (needs ``diameter``).
            thread_depth: Full-thread depth in mm for a blind thread.
            tolerance: Symmetric tolerance of the hole size (±), in mm.
            upper: Upper deviation of the hole size (with lower).
            lower: Lower deviation of the hole size (with upper).
            limits: Write the size as its limits, "⌀6.6–6.8".
            dual: Show inches in brackets after mm.
            decimals_in: Inch decimals.
            offset: Leader length beyond the circle, in sheet mm.
            note: A last line under every callout, e.g. "REAM 3/16 IN"; the
                checker keeps it when it recomputes the callout.
            doc_name: Document. Uses active document if None.

        Returns:
            Each callout's name, text and hole count, and the hole groups
            this view cannot show.
        """
        plus, minus = _deviations(tolerance, upper, lower)
        if (thread or plus is not None) and diameter is None:
            raise ValueError("thread and size tolerances apply to one group of holes: give its diameter")
        if thread and plus is not None:
            raise ValueError("A thread designation carries its own tolerance class; give no size tolerance")
        if limits and plus is None:
            raise ValueError("limits needs a tolerance (tolerance, or upper and lower)")
        return await run(_ADD_HOLE_CALLOUTS, "Adding hole callouts failed", view_name=view_name,
                         object_names=object_names, diameter=diameter, thread=thread, thread_depth=thread_depth,
                         plus=plus, minus=minus, limits=limits, dual=dual, decimals_in=decimals_in,
                         offset=offset, note=note, doc_name=doc_name)

    @mcp.tool()
    async def add_hole_table(
        page_name: str,
        view_name: str,
        object_names: list[str] | None = None,
        origin: list[float] | None = None,
        dual: bool = True,
        decimals_in: int = 3,
        doc_name: str | None = None,
    ) -> dict[str, Any]:
        """Tag every hole of a view (A1, A2 ...) and list them in a table.

        Args:
            page_name: The drawing page that receives the table.
            view_name: The view where the holes appear as circles.
            object_names: Parts to search; the view's sources if None.
            origin: 3D point the X and Y columns are measured from; the
                view's lower-left corner if None.
            dual: Show inches in brackets after mm.
            decimals_in: Inch decimals.
            doc_name: Document. Uses active document if None.

        Returns:
            The table rows and where the table went.
        """
        tags = await run(_ADD_HOLE_TAGS, "Tagging the holes failed", view_name=view_name,
                         object_names=object_names, origin=origin, dual=dual, decimals_in=decimals_in,
                         doc_name=doc_name)
        table = await run(_ADD_TABLE, "Adding the hole table failed", page_name=page_name, rows=tags["rows"],
                          columns=[("TAG", 50), ("X", 110), ("Y", 110), ("SIZE", 260)], sheet_name="HoleTable",
                          where="top_right", doc_name=doc_name)
        return {**table, "holes": tags["rows"]}

    @mcp.tool()
    async def add_gdt_frame(
        characteristic: str,
        tolerance: float,
        datums: list[str] | None = None,
        diameter_zone: bool = False,
        material_condition: str | None = None,
        view_name: str | None = None,
        point: list[float] | None = None,
        leader: list[float] | None = None,
        page_name: str | None = None,
        position: list[float] | None = None,
        projected_height: float | None = None,
        dual: bool = False,
        doc_name: str | None = None,
    ) -> dict[str, Any]:
        """Add a feature control frame, with a leader to its feature.

        Refuses frames that cannot be right: a datum on a form tolerance,
        an orientation or runout tolerance without datum, a diameter zone
        or an M/L modifier where none applies, letters I, O and Q.

        Args:
            characteristic: straightness, flatness, circularity, cylindricity,
                profile_of_a_line, profile_of_a_surface, angularity,
                perpendicularity, parallelism, position, concentricity,
                symmetry, circular_runout or total_runout.
            tolerance: Tolerance zone size in mm.
            datums: Up to three references, primary first, e.g. ["A", "B(M)"].
            diameter_zone: Cylindrical zone (⌀ before the tolerance).
            material_condition: "M" (maximum) or "L" (least) after the tolerance.
            view_name: View holding the feature; a leader is drawn from point.
            point: The feature's 3D point (model mm) for the leader's arrow.
            leader: Leader offset [dx, dy] in sheet mm (y up); by default the
                leader leaves the view and the frame lands in free space.
            page_name: Sheet, when placing by position instead.
            position: Frame centre on the sheet [x, y], without leader.
            projected_height: Height in mm of a projected tolerance zone (Ⓟ),
                for tapped holes and pressed pins: at least the thickness of
                the mating part; position and orientation only.
            dual: Also write the tolerance in inches.
            doc_name: Document. Uses active document if None.

        Returns:
            The frame's name and box on the sheet, and any warning.
        """
        datums = datums or []
        warnings = check_gdt(characteristic, diameter_zone, material_condition, datums)
        if not (math.isfinite(tolerance) and tolerance > 0):
            raise ValueError("tolerance must be a positive size in mm")
        projected = None
        if projected_height is not None:
            if characteristic not in ("position", "perpendicularity", "parallelism", "angularity"):
                raise ValueError("A projected tolerance zone applies to position or orientation only")
            if not (math.isfinite(projected_height) and projected_height > 0):
                raise ValueError("projected_height must be a positive height in mm")
            projected = _fmt(projected_height, _decimals(projected_height, 2))
        spec = {"dual": dual, "decimals_in": 4}
        text = _dual_text(spec, tolerance)
        svg, width = gdt_frame_svg(characteristic, text, diameter_zone, material_condition, datums, projected)
        properties = {
            "GdtCharacteristic": characteristic,
            "GdtTolerance": text,
            "GdtDatums": ",".join(_split_datum(d)[0] for d in datums),
        }
        if projected:
            properties["GdtProjected"] = projected
        result = await run(_ADD_GDT, "Adding the feature control frame failed", kind="FeatureControlFrame", svg=svg,
                           width=width, properties=properties, view_name=view_name, point=point,
                           leader=leader, page_name=page_name, position=position, doc_name=doc_name)
        return {**result, "text": text, "warnings": warnings}

    @mcp.tool()
    async def add_datum_symbol(
        letter: str,
        view_name: str | None = None,
        point: list[float] | None = None,
        side: str = "up",
        page_name: str | None = None,
        position: list[float] | None = None,
        touching: list[str] | None = None,
        doc_name: str | None = None,
    ) -> dict[str, Any]:
        """Add a datum feature symbol whose triangle sits on a feature.

        A plane surface takes the symbol on its edge (view_name and point).
        A feature of size (hole, slot, pin) takes it on its feature control
        frame or in line with its size dimension: give page_name and the
        sheet position of the triangle's base instead.

        Args:
            letter: Datum letter, A to Z without I, O and Q.
            view_name: View holding the feature.
            point: 3D point (model mm) on the feature's edge in that view.
            side: Where the letter box stands from the feature: up, down,
                left or right.
            page_name: Sheet, when placing by position instead.
            position: Sheet point [x, y] in mm (y up) where the triangle's
                base sits, e.g. the bottom edge of a feature control frame.
            touching: Names of the objects the symbol is meant to touch (the
                frame it hangs from), left out of the overlap report.
            doc_name: Document. Uses active document if None.

        Returns:
            The symbol's name and box on the sheet.
        """
        svg, offset = datum_symbol_svg(letter, side)
        return await run(_ADD_DATUM, "Adding the datum symbol failed", svg=svg, offset=list(offset), letter=letter,
                         view_name=view_name, point=point, page_name=page_name, position=position,
                         touching=touching, doc_name=doc_name)

    @mcp.tool()
    async def add_weld_symbol(
        view_name: str,
        point: list[float],
        arrow_side: str | None = "fillet",
        other_side: str | None = None,
        arrow_size: str = "",
        other_size: str = "",
        arrow_length: str = "",
        other_length: str = "",
        all_around: bool = False,
        field_weld: bool = False,
        tail: str = "",
        leader: list[float] | None = None,
        reference_length: float = 30.0,
        doc_name: str | None = None,
    ) -> dict[str, Any]:
        """Add an AWS weld symbol with its arrow on a joint.

        Arrow-side symbols go below the reference line, other-side above.

        Args:
            view_name: View showing the joint.
            point: 3D point (model mm) of the joint for the arrow.
            arrow_side: fillet, square, v, bead, plug, or None.
            other_side: Same choices, or None.
            arrow_size: Weld size left of the arrow-side symbol, e.g. "6".
            other_size: Weld size on the other side.
            arrow_length: Length or length-pitch right of the symbol.
            other_length: Same for the other side.
            all_around: Weld all around (circle at the kink).
            field_weld: Field weld (flag at the kink).
            tail: Text in the tail, e.g. a process; no tail if empty.
            leader: Arrow-to-kink offset [dx, dy] in sheet mm (y up); by default
                the leader leaves the view and the symbol lands in free space.
            reference_length: Reference line length in sheet mm.
            doc_name: Document. Uses active document if None.

        Returns:
            The symbol's name, its leader, where the arrow points on the sheet,
            and the AWS symbol files used.
        """
        sides = {}
        for side, kind, size, length in (("arrow", arrow_side, arrow_size, arrow_length),
                                         ("other", other_side, other_size, other_length)):
            if kind is None:
                sides[side] = None
                continue
            if kind not in _WELD_FILES:
                raise ValueError(f"Unknown weld {kind!r}; use {', '.join(_WELD_FILES)}")
            sides[side] = {"file": _WELD_FILES[kind][0 if side == "arrow" else 1], "size": size, "length": length}
        if not any(sides.values()):
            raise ValueError("Give arrow_side, other_side or both")
        return await run(_ADD_WELD, "Adding the weld symbol failed", view_name=view_name, point=point,
                         arrow=sides["arrow"], other=sides["other"], all_around=all_around, field_weld=field_weld,
                         tail=tail, leader=leader, reference_length=reference_length,
                         doc_name=doc_name)

    async def bom_items(object_names: list[str], doc_name: str | None) -> list[dict[str, Any]]:
        return (await run(_BOM_ITEMS, "Listing the parts failed", object_names=object_names, doc_name=doc_name))["items"]

    @mcp.tool()
    async def add_parts_list(
        page_name: str,
        object_names: list[str],
        view_name: str | None = None,
        balloons: bool = True,
        doc_name: str | None = None,
    ) -> dict[str, Any]:
        """Add a parts list above the title block, and balloons that match it.

        Identical parts (same volume, area and size) share an item and add
        to its quantity. Description and material come from each part's
        Label2 and ShapeMaterial, else "À RENSEIGNER".

        Args:
            page_name: The drawing page.
            object_names: The parts to list.
            view_name: View that receives the balloons, e.g. the isometric.
            balloons: Add one balloon per item on view_name.
            doc_name: Document. Uses active document if None.

        Returns:
            The items (number, quantity, parts), the table, the balloons and
            how many of their leaders still cross (0 once untangled).
        """
        items = await bom_items(object_names, doc_name)
        if not items:
            raise ValueError("None of these objects is a solid part")
        rows = [[i["item"], len(i["objects"]), i["label"], i["description"], i["material"]] for i in items]
        table = await run(_ADD_TABLE, "Adding the parts list failed", page_name=page_name, rows=rows,
                          columns=[("ITEM", 50), ("QTY", 50), ("PART NUMBER", 150), ("DESCRIPTION", 190),
                                   ("MATERIAL", 150)],
                          sheet_name="PartsList", where="above_title_block", doc_name=doc_name)
        made, crossings = [], 0
        if balloons:
            if not view_name:
                raise ValueError("Give view_name for the balloons, or balloons=False")
            placed = await run(_ADD_BALLOONS, "Adding the balloons failed", view_name=view_name, items=items,
                               offset=12.0, doc_name=doc_name)
            made, crossings = placed["balloons"], placed["crossing_leaders"]
        return {"items": items, "table": table, "balloons": made, "crossing_leaders": crossings}

    @mcp.tool()
    async def add_revision_table(
        page_name: str,
        revisions: list[dict[str, str]],
        doc_name: str | None = None,
    ) -> dict[str, Any]:
        """Add the revision block in the sheet's top-right corner.

        Args:
            page_name: The drawing page.
            revisions: Rows oldest first, each with rev, description, date
                and optionally zone and approved; approved stays empty
                unless a person gave it.
            doc_name: Document. Uses active document if None.

        Returns:
            Where the table went; the title block's revision is set to the
            last row's.
        """
        if not revisions:
            raise ValueError("Give at least one revision row")
        missing = [i for i, r in enumerate(revisions) if not r.get("rev") or not r.get("description")]
        if missing:
            raise ValueError(f"Rows {missing} lack rev or description")
        rows = [[r.get("zone", ""), r["rev"], r["description"], r.get("date", PLACEHOLDER), r.get("approved", "")]
                for r in revisions]
        table = await run(_ADD_TABLE, "Adding the revision table failed", page_name=page_name, rows=rows,
                          columns=[("ZONE", 50), ("REV", 45), ("DESCRIPTION", 260), ("DATE", 90), ("APPROVED", 90)],
                          sheet_name="Revisions", where="top_right", doc_name=doc_name)
        await run(_FILL_TITLE_BLOCK, "Setting the revision failed", page_name=page_name,
                  fields={"revision": revisions[-1]["rev"]}, aliases=TITLE_FIELDS, lenient=True, doc_name=doc_name)
        return {**table, "revision": revisions[-1]["rev"]}

    @mcp.tool()
    async def export_drawing(page_name: str, file_path: str, png_dpi: int | None = 150,
                             doc_name: str | None = None) -> dict[str, Any]:
        """Export a sheet to PDF (or SVG) and render a PNG to look at.

        Needs FreeCAD's GUI. Brings the 3D view back to front afterwards,
        since get_view fails while a sheet is in front.

        Args:
            page_name: The drawing page.
            file_path: Absolute path (or ~/...) ending in .pdf or .svg.
            png_dpi: Resolution of the PNG rendered from the PDF with
                pdftoppm; None for no PNG.
            doc_name: Document. Uses active document if None.

        Returns:
            The file, its size, the PDF page size checked against the
            template, and the PNG paths.
        """
        result = await run(_path_code(file_path) + _EXPORT, "Exporting the drawing failed", page_name=page_name,
                           doc_name=doc_name)
        path = result["path"]
        if path.lower().endswith(".pdf"):
            box = _pdf_media_box(path)
            if box is None:
                raise ValueError(f"{path} has no readable page size")
            size = [round((box[2] - box[0]) * 25.4 / 72, 1), round((box[3] - box[1]) * 25.4 / 72, 1)]
            want = result["size_mm"]
            if abs(size[0] - want[0]) > 1 or abs(size[1] - want[1]) > 1:
                raise ValueError(f"The PDF page is {size} mm where the template is {want} mm")
            result["pdf_page_mm"] = size
            result["png"] = []
            if png_dpi:
                tool = shutil.which("pdftoppm")
                if tool is None:
                    result["png_note"] = "pdftoppm not found: no PNG"
                else:
                    stem = os.path.splitext(path)[0]
                    done = await asyncio.to_thread(subprocess.run, [tool, "-r", str(int(png_dpi)), "-png", path, stem],
                                                   capture_output=True, text=True, timeout=120)
                    if done.returncode != 0:
                        raise ValueError("pdftoppm failed: " + done.stderr[-300:])
                    folder = os.path.dirname(path) or "."
                    base = os.path.basename(stem)
                    result["png"] = sorted(os.path.join(folder, f) for f in os.listdir(folder)
                                           if re.fullmatch(re.escape(base) + r"-\d+\.png", f))
        return result

    @mcp.tool()
    async def check_drawing(
        page_name: str,
        dual_units: bool = True,
        projection: str = "Third angle",
        report_path: str | None = None,
        doc_name: str | None = None,
    ) -> dict[str, Any]:
        """Check a sheet and report each check as PASS, FAIL, NON_VERIFIE or NON_APPLICABLE.

        Views drawn and inside the frame, off the title block and apart;
        projection; scale field; title block without template sample text
        or invented values; every dimension tied to geometry, in dual units,
        its text recomputed from the geometry; decimal point; every hole
        called out; GD&T datums shown; parts list against balloons.
        What only a person can judge is NON_VERIFIE, never PASS, and the
        status stays "À VÉRIFIER".

        Args:
            page_name: The drawing page.
            dual_units: Require mm [in] on every linear dimension.
            projection: Expected projection of the view groups.
            report_path: Also write the report as Markdown here (absolute or ~).
            doc_name: Document. Uses active document if None.

        Returns:
            Status, counts per verdict, and every check with its evidence.
        """
        report = await run(_CHECK, "Checking the drawing failed", page_name=page_name, dual_units=dual_units,
                           projection=projection, bom_sheet="PartsList", doc_name=doc_name)
        if report_path:
            path = os.path.expanduser(report_path)
            if not os.path.isabs(path):
                raise ValueError("report_path must be absolute or start with ~")
            with open(path, "w", encoding="utf-8") as out:
                out.write(_report_markdown(report))
            report["report_path"] = path
        return report

    @mcp.tool()
    async def create_drawing(
        object_names: list[str],
        template: str = "ANSIB_Landscape",
        title: str | None = None,
        drawing_number: str | None = None,
        revision: str | None = None,
        drawn_by: str | None = None,
        company: str | None = None,
        views: list[str] | None = None,
        isometric: bool = True,
        scale: float | None = None,
        projection: str = "Third angle",
        overall_dimensions: bool = True,
        hole_callouts: bool = True,
        parts_list: bool | None = None,
        dual: bool = True,
        page_name: str | None = None,
        doc_name: str | None = None,
    ) -> dict[str, Any]:
        """Draft a whole sheet in one call, then check it.

        Sheet and title block, views at the largest standard scale that
        fits, overall dimensions on the front and top or right views, a
        callout on every hole seen end-on, a parts list with balloons when
        there are several parts, and check_drawing's report. The result is a
        first draft "À VÉRIFIER": functional dimensions, tolerances, GD&T
        and welds still come from the designer.

        Args:
            object_names: Parts to draw together.
            template: ASME template name or SVG path.
            title: Drawing title.
            drawing_number: Drawing number.
            revision: Revision letter.
            drawn_by: Drafter's name or initials.
            company: Company name.
            views: Projections; default ["Front", "Top", "Right"].
            isometric: Add an isometric view.
            scale: Fixed scale instead of the automatic one.
            projection: "Third angle" or "First angle".
            overall_dimensions: Width, height and depth over all.
            hole_callouts: Call out the holes.
            parts_list: Parts list and balloons; automatic (several parts) if None.
            dual: mm [in] dimensions.
            page_name: Name of the page object.
            doc_name: Document. Uses active document if None.

        Returns:
            What was made and the check report.
        """
        page = await create_drawing_page(template=template, page_name=page_name, title=title,
                                         drawing_number=drawing_number, revision=revision, drawn_by=drawn_by,
                                         company=company, doc_name=doc_name)
        placed = await add_drawing_views(page_name=page["page"], object_names=object_names, views=views,
                                         isometric=isometric, scale=scale, projection=projection, doc_name=doc_name)
        made: dict[str, Any] = {"page": page, "views": placed, "dimensions": [], "hole_callouts": [], "skipped": []}
        named = placed["views"]
        if overall_dimensions:
            plan = [("Front", "horizontal", "below"), ("Front", "vertical", "left")]
            plan.append(("Top", "vertical", "right") if "Top" in named else ("Right", "horizontal", "below"))
            for view_type, kind, side in plan:
                if view_type not in named:
                    continue
                extremes = await run(_EXTREMES, "Finding the extremes failed", view_name=named[view_type], kind=kind,
                                     doc_name=doc_name)
                if not extremes.get("elements"):
                    made["skipped"].append(f"{kind} overall on {view_type}: {extremes.get('reason')}")
                    continue
                made["dimensions"].append(await add_dimension(view_name=named[view_type], kind=kind,
                                                              elements=extremes["elements"], side=side, dual=dual,
                                                              doc_name=doc_name))
        if hole_callouts:
            for view_type in ("Top", "Front", "Right", "Left", "Bottom", "Rear"):
                if view_type in named:
                    found = await add_hole_callouts(view_name=named[view_type], dual=dual, doc_name=doc_name)
                    made["hole_callouts"] += found["callouts"]
            # One callout per hole group is enough: drop repeats found in later views
            made["hole_callouts"] = await run(_DEDUPE_CALLOUTS, "Removing repeated callouts failed",
                                              page_name=page["page"], doc_name=doc_name)
        solids = len(object_names)
        if parts_list or (parts_list is None and solids > 1):
            made["parts_list"] = await add_parts_list(page_name=page["page"], object_names=object_names,
                                                      view_name=placed["isometric"] or named["Front"],
                                                      doc_name=doc_name)
        made["check"] = await check_drawing(page_name=page["page"], dual_units=dual, projection=projection,
                                            doc_name=doc_name)
        return made


_ADD_DATUM = r'''
if _args.get("position"):
    # Triangle base at a sheet point: on a feature control frame or a dimension line
    if not _args.get("page_name"):
        raise ValueError("Give page_name with a position")
    page = _page(_args["page_name"])
    view = None
    px, py = _args["position"]
else:
    if not (_args.get("view_name") and _args.get("point")):
        raise ValueError("Give view_name and point (on a surface), or page_name and position (on a frame)")
    view = _part_view(_args["view_name"])
    page = _page_of(view)
    xy = _to_view(view, _args["point"])
    px, py = _to_page(view, xy)
symbol = doc.addObject("TechDraw::DrawViewSymbol", "Datum" + _args["letter"])
symbol.Symbol = _args["svg"]
page.addView(symbol)
symbol.X, symbol.Y = px - _args["offset"][0], py - _args["offset"][1]
symbol.addProperty("App::PropertyString", "GdtDatum", "GD&T", "Datum letter, read by check_drawing")
symbol.GdtDatum = _args["letter"]
doc.recompute()
box = _symbol_box(symbol)
# The triangle touches its own view by design; anything else under the symbol is reported
skip = set(_args.get("touching") or [])
if view is not None:
    skip.add(view.Name)
hits = sorted(set(n.split(":")[0] for n, b in _note_boxes(page) + _boxes(page, skip=(symbol.Name,))
                  if n.split(":")[0] not in skip and _overlap(box, b)))
_result_ = dict(name=symbol.Name, letter=_args["letter"], feature_point=[round(px, 3), round(py, 3)], box=box,
                overlaps=hits, warning=("covers " + ", ".join(hits) + ": try another side") if hits else None)
'''

_EXTREMES = r'''
view = _part_view(_args["view_name"])
marks = [(i, vx.Point) for i, vx in enumerate(_elements(view, "Vertex"))]
x0, y0, x1, y1 = _view_box(view)
if _args["kind"] == "horizontal":
    low = [m for m in marks if abs(m[1].x - x0) < 1e-6]
    high = [m for m in marks if abs(m[1].x - x1) < 1e-6]
    pick = lambda group: min(group, key=lambda m: m[1].y)
else:
    low = [m for m in marks if abs(m[1].y - y0) < 1e-6]
    high = [m for m in marks if abs(m[1].y - y1) < 1e-6]
    pick = lambda group: min(group, key=lambda m: m[1].x)
if not low or not high:
    _result_ = dict(elements=None, reason="the extreme of " + view.Label + " is a curve, not a corner")
else:
    _result_ = dict(elements=["Vertex" + str(pick(low)[0]), "Vertex" + str(pick(high)[0])])
'''

_DEDUPE_CALLOUTS = r'''
page = _page(_args["page_name"])
kept, seen = [], set()
for dim in list(page.Views):
    if dim.isDerivedFrom("TechDraw::DrawViewDimension") and "DualSpec" in dim.PropertiesList:
        spec = json.loads(dim.DualSpec)
        if spec.get("kind") != "hole":
            continue
        key = (spec["object"], tuple(tuple(x) for x in spec["signature"]))
        if key in seen:
            doc.removeObject(dim.Name)
            continue
        seen.add(key)
        kept.append(dict(name=dim.Name, text=dim.FormatSpec))
doc.recompute()
_result_ = kept
'''
