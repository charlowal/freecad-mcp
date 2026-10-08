"""Drawing tools: the parts that run without FreeCAD.

The formatting functions are the same source that runs inside FreeCAD, so
what is asserted here is what the sheet shows.
"""

import asyncio
import re
import xml.dom.minidom

import pytest

from freecad_mcp.modelisation import drawing
from freecad_mcp.modelisation.drawing import (
    _decimals,
    _dual_text,
    _fmt,
    _hole_text,
    _scale_text,
    _template_areas,
    check_gdt,
    datum_symbol_svg,
    gdt_frame_svg,
)
from freecad_mcp.modelisation import ToolError
from test_modelisation import RecordingBridge, _modelling_tools


@pytest.mark.parametrize(
    ("value", "spec", "text"),
    [
        (120.0, {}, "120 [4.724]"),
        (11.0, {"prefix": "⌀"}, "⌀11 [.433]"),
        (5.5, {"prefix": "R"}, "R5.5 [.217]"),
        (0.5, {}, "0.5 [.020]"),
        (25.4, {}, "25.4 [1.000]"),
        (120.0, {"plus": 0.1, "minus": -0.1}, "120 ±0.1 [4.721–4.728]"),
        (50.0, {"plus": 0.2, "minus": -0.1}, "50 +0.2/-0.1 [1.965–1.976]"),
        (50.0, {"plus": 0.0, "minus": -0.05}, "50 0/-0.05 [1.9666–1.9685]"),
        (34.8, {"plus": 0.3, "minus": 0.0}, "34.8 +0.3/0 [1.371–1.381]"),
        (12.0, {"plus": 0.1, "minus": -0.1}, "12 ±0.1 [.469–.476]"),
        (20.03, {"plus": 0.01, "minus": -0.01, "limits": True, "prefix": "⌀"}, "⌀20.02–20.04 [.7882–.7889]"),
        (6.7, {"plus": 0.1, "minus": -0.1, "limits": True}, "6.6–6.8 [.260–.267]"),
        (16.0, {"plus": 0.2, "minus": -0.2}, "16 ±0.2 [.623–.637]"),
        (120.0, {"dual": False}, "120"),
        (120.0, {"decimals_mm": 2, "decimals_in": 4}, "120.00 [4.7244]"),
        (11.0, {"prefix": "2X ⌀", "suffix": " THRU"}, "2X ⌀11 [.433] THRU"),
    ],
)
def test_dual_text(value: float, spec: dict, text: str) -> None:
    assert _dual_text(spec, value) == text


def test_inch_values_drop_the_leading_zero_and_mm_keep_it() -> None:
    assert _fmt(0.433, 3, leading_zero=False) == ".433"
    assert _fmt(-0.25, 2, leading_zero=False) == "-.25"
    assert _fmt(0.5, 1) == "0.5"
    assert _fmt(-0.0001, 2) == "0.00"


def test_decimals_ignore_float_noise() -> None:
    assert _decimals(119.99999999997, 2) == 0
    assert _decimals(5.5, 2) == 1
    assert _decimals(1 / 3, 2) == 2


@pytest.mark.parametrize(
    ("hole", "count", "text"),
    [
        ({"diameter": 11.0, "through": True}, 2, "2X ⌀11 [.433] THRU"),
        ({"diameter": 6.6, "through": False, "depth": 12.0}, 1, "⌀6.6 [.260] ↧ 12 [.472]"),
        ({"diameter": 6.6, "through": True, "cbore_diameter": 11.0, "cbore_depth": 6.4}, 4,
         "4X ⌀6.6 [.260] THRU\n⌴ ⌀11 [.433] ↧ 6.4 [.252]"),
        ({"diameter": 6.6, "through": True, "csink_diameter": 13.0, "csink_angle": 90.0}, 1,
         "⌀6.6 [.260] THRU\n⌵ ⌀13 [.512] X 90°"),
    ],
)
def test_hole_callout_text(hole: dict, count: int, text: str) -> None:
    assert _hole_text(hole, count, {"dual": True, "decimals_in": 3}) == text


def test_hole_callout_note_is_its_last_line() -> None:
    spec = {"dual": True, "decimals_in": 3, "note": "REAM 3/16 IN"}
    assert _hole_text({"diameter": 4.9, "through": True}, 4, spec) == "4X ⌀4.9 [.193] THRU\nREAM 3/16 IN"


def test_hole_callout_ignores_a_dimension_prefix_but_tolerates_the_size() -> None:
    spec = {"dual": True, "decimals_in": 3, "prefix": "X", "plus": 0.1, "minus": -0.1}
    assert _hole_text({"diameter": 11.0, "through": True}, 1, spec) == "⌀11 ±0.1 [.430–.437] THRU"
    spec["limits"] = True
    assert _hole_text({"diameter": 6.7, "through": True}, 4, spec) == "4X ⌀6.6–6.8 [.260–.267] THRU"


def test_hole_callout_depths_stay_untoleranced() -> None:
    spec = {"dual": True, "decimals_in": 3, "plus": 0.1, "minus": -0.1}
    assert _hole_text({"diameter": 6.6, "through": False, "depth": 12.0}, 1, spec) == "⌀6.6 ±0.1 [.256–.263] ↧ 12 [.472]"


def test_thread_callout_replaces_the_drill_size() -> None:
    spec = {"dual": True, "decimals_in": 3, "thread": "M5×0.8-6H"}
    assert _hole_text({"diameter": 4.2, "through": True}, 2, spec) == "2X M5×0.8-6H THRU"
    spec["thread_depth"] = 10.0
    assert _hole_text({"diameter": 4.2, "through": False, "depth": 13.0}, 2, spec) == "2X M5×0.8-6H ↧ 10 [.394]"
    del spec["thread_depth"]
    with pytest.raises(ValueError, match="full-thread depth"):
        _hole_text({"diameter": 4.2, "through": False, "depth": 13.0}, 2, spec)


@pytest.mark.parametrize(("low", "high"), [(19.9, 20.1), (6.6, 6.8), (20.0, 20.021), (49.95, 50.0), (0.05, 0.06),
                                           (119.5, 120.5), (3.0, 3.001)])
def test_inch_limits_never_leave_the_mm_range(low: float, high: float) -> None:
    from freecad_mcp.modelisation.drawing import _inch_limits
    lo, hi = (float("0" + t) for t in _inch_limits(low, high, 3).split("–"))
    assert low <= lo * 25.4 + 1e-9 and hi * 25.4 <= high + 1e-9 and lo <= hi


def test_a_tolerance_needs_two_limits() -> None:
    with pytest.raises(ValueError, match="two different limits"):
        _dual_text({"plus": 0.0, "minus": 0.0}, 10.0)


@pytest.mark.parametrize(("scale", "text"), [(1.0, "1:1"), (0.5, "1:2"), (0.25, "1:4"), (2.0, "2:1"), (0.1, "1:10")])
def test_scale_text(scale: float, text: str) -> None:
    assert _scale_text(scale) == text


ANSI_B = """<svg xmlns="http://www.w3.org/2000/svg" xmlns:freecad="https://www.freecad.org/wiki/index.php?title=Svg_Namespace"
 width="431.8mm" height="279.4mm" viewBox="0 0 431.8 279.4">
<rect id="rectOutline" x="12.5" y="6.6" width="406.8" height="266.2"/>
<rect id="frame" x="19.979" y="20.179" width="391.84" height="239.04"/>
<rect id="block" x="264.98" y="210.95" width="146.66" height="48.074"/>
<rect id="cell" x="264.98" y="250" width="40" height="9.019"/>
<text freecad:editable="DrawingTitle1" x="300" y="230">TITLE</text>
<text freecad:editable="DrawnBy" x="270" y="255">ME</text>
</svg>"""

# The larger ASME sheets draw their borders as paths inside a transformed group
ANSI_D_PATHS = """<svg xmlns="http://www.w3.org/2000/svg" xmlns:freecad="https://www.freecad.org/wiki/index.php?title=Svg_Namespace"
 width="864" height="559" viewBox="0 0 432 279.5">
<g transform="scale(0.5)">
<path d="M10 10 H854 V549 H10 Z"/>
<path d="m20 20h824v519h-824z"/>
<path d="M690 470 V539 M690 470 H844"/>
<text freecad:editable="DrawingTitle1" x="760" y="500">TITLE</text>
</g>
</svg>"""


def test_template_areas_find_the_frame_and_the_title_block() -> None:
    areas = _template_areas(ANSI_B, 431.8, 279.4)
    assert areas["source"] == "template frame"
    assert areas["frame"] == [19.979, 20.181, 411.819, 259.221]
    assert areas["title_block"] == [264.98, 20.181, 411.819, 68.45]


def test_template_areas_read_paths_in_transformed_groups() -> None:
    areas = _template_areas(ANSI_D_PATHS, 864.0, 559.0)
    assert areas["frame"] == [20.0, 20.0, 844.0, 539.0]
    assert areas["title_block"] == [690.0, 20.0, 844.0, 89.0]


@pytest.mark.parametrize("name", ["ANSIA_Landscape", "ANSIB_Landscape", "ANSIC_Landscape", "ANSID_Landscape",
                                  "ANSIE_Landscape", "ANSIB_Portrait", "ANSID_Portrait"])
def test_every_asme_template_gives_a_frame_and_a_title_block(name: str) -> None:
    import os
    path = f"/snap/freecad/current/usr/share/Mod/TechDraw/Templates/ASME/{name}.svg"
    if not os.path.exists(path):
        pytest.skip("FreeCAD's templates are not installed here")
    svg = open(path, encoding="utf-8").read()
    w = float(re.search(r'<svg[^>]*\swidth="([\d.]+)', svg).group(1))
    h = float(re.search(r'<svg[^>]*\sheight="([\d.]+)', svg).group(1))
    areas = _template_areas(svg, w, h)
    f, b = areas["frame"], areas["title_block"]
    assert areas["source"] == "template frame"
    assert 5 < f[0] < 0.1 * w and f[2] > 0.9 * w and 5 < f[1] < 0.1 * h and f[3] > 0.9 * h
    # The title block sits in the frame's bottom-right corner
    assert b and b[1] == f[1] and b[2] == f[2] and b[0] > f[0] + 40 and 30 < b[3] - b[1] < 90


def test_template_without_frame_gets_default_margins() -> None:
    areas = _template_areas("<svg></svg>", 279.4, 215.9)
    assert areas == {"frame": [10.0, 10.0, 269.4, 205.9], "title_block": None, "source": "default margins"}


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (("flatness", False, None, ["A"]), "form tolerance"),
        (("perpendicularity", False, None, []), "at least one datum"),
        (("flatness", True, None, []), "cylindrical tolerance zone"),
        (("profile_of_a_surface", False, "M", ["A"]), "no material condition"),
        (("position", True, "X", ["A"]), "maximum"),
        (("position", True, "M", ["A", "B", "C", "D"]), "at most three"),
        (("position", True, "M", ["A", "A"]), "once"),
        (("position", True, "M", ["I"]), "I, O and Q"),
        (("roundness", False, None, []), "Unknown characteristic"),
    ],
)
def test_impossible_frames_are_refused(args: tuple, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        check_gdt(*args)


def test_valid_frames_pass_with_warnings_where_due() -> None:
    assert check_gdt("position", True, "M", ["A", "B(M)", "C"]) == []
    assert check_gdt("flatness", False, None, []) == []
    assert "Y14.5-2018" in check_gdt("symmetry", False, None, ["A"])[0]
    assert check_gdt("position", True, None, [])


def test_frame_svg_is_wellformed_and_sized() -> None:
    svg, width = gdt_frame_svg("position", "0.2", True, "M", ["A", "B(M)", "C"])
    root = xml.dom.minidom.parseString(svg).documentElement
    assert root.getAttribute("height") == "8.00mm"
    assert float(root.getAttribute("width")[:-2]) == pytest.approx(width, abs=0.01)
    texts = [t.firstChild.data for t in root.getElementsByTagName("text")]
    assert texts == ["⌀0.2", "M", "A", "B", "M", "C"]
    dividers = [p for p in root.getElementsByTagName("path") if re.fullmatch(r"M[\d.]+ 0 V8\.00", p.getAttribute("d"))]
    assert len(dividers) == 4  # symbol | tolerance | A | B | C


@pytest.mark.parametrize("characteristic", sorted(drawing._GDT_SYMBOLS))
def test_every_characteristic_draws(characteristic: str) -> None:
    datums = [] if characteristic in drawing._FORM else ["A"]
    svg, _ = gdt_frame_svg(characteristic, "0.1", False, None, datums)
    xml.dom.minidom.parseString(svg)


@pytest.mark.parametrize(
    ("side", "offset"),
    [("up", (0.0, -1.0)), ("down", (0.0, 1.0)), ("left", (1.0, 0.0)), ("right", (-1.0, 0.0))],
)
def test_datum_symbol_puts_the_triangle_on_the_feature(side: str, offset: tuple) -> None:
    svg, (dx, dy) = datum_symbol_svg("A", side)
    root = xml.dom.minidom.parseString(svg).documentElement
    w, h = float(root.getAttribute("width")[:-2]), float(root.getAttribute("height")[:-2])
    # The triangle's base is on the symbol's edge facing the feature
    assert (dx, dy) == pytest.approx((offset[0] * w / 2, offset[1] * h / 2), abs=0.01)


def test_datum_symbol_refuses_I_O_Q() -> None:
    with pytest.raises(ValueError, match="I, O and Q"):
        datum_symbol_svg("O")


def _run(name: str, **kwargs):
    bridge = RecordingBridge()
    fn = _modelling_tools(bridge)[name]
    return bridge, asyncio.run(fn(**kwargs))


def test_add_dimension_refuses_mixed_tolerances_before_reaching_freecad() -> None:
    bridge = RecordingBridge()
    fn = _modelling_tools(bridge)["add_dimension"]
    with pytest.raises(ToolError, match="either tolerance"):
        asyncio.run(fn(view_name="V", kind="horizontal", points=[[0, 0, 0], [1, 0, 0]], tolerance=0.1, upper=0.1))
    with pytest.raises(ToolError, match="both"):
        asyncio.run(fn(view_name="V", kind="horizontal", points=[[0, 0, 0], [1, 0, 0]], upper=0.1))
    assert not bridge.scripts


def test_arguments_travel_as_json_not_as_code() -> None:
    bridge, _ = _run("fill_title_block", page_name="Sheet", fields={"title": 'Bracket "A" {x}\n'})
    script = bridge.scripts[0]
    assert '_args = json.loads(' in script
    assert "{x}" in script  # braces reach FreeCAD as data, no template ate them
    compile(script, "fill_title_block", "exec")


def test_add_views_validates_projections_and_scale() -> None:
    bridge = RecordingBridge()
    fn = _modelling_tools(bridge)["add_drawing_views"]
    with pytest.raises(ToolError, match="Unknown projections"):
        asyncio.run(fn(page_name="Sheet", object_names=["Box"], views=["Side"]))
    with pytest.raises(ToolError, match="positive"):
        asyncio.run(fn(page_name="Sheet", object_names=["Box"], scale=0))
    with pytest.raises(ToolError, match="Third angle"):
        asyncio.run(fn(page_name="Sheet", object_names=["Box"], projection="American"))
    assert not bridge.scripts


def test_weld_symbol_maps_sides_to_aws_files() -> None:
    bridge, _ = _run("add_weld_symbol", view_name="V", point=[0, 0, 0], arrow_side="fillet", other_side="v",
                     arrow_size="6")
    script = bridge.scripts[0]
    assert '"file": "filletDown.svg"' in script and '"file": "VUp.svg"' in script
    with pytest.raises(ToolError, match="Unknown weld"):
        _run("add_weld_symbol", view_name="V", point=[0, 0, 0], arrow_side="J")


def test_a_mouth_chamfer_can_leave_the_countersink_line_out() -> None:
    hole = {"diameter": 20.03, "through": True, "csink_diameter": 21.03, "csink_angle": 90.0}
    spec = {"dual": True, "decimals_in": 3, "plus": 0.01, "minus": -0.01, "limits": True}
    assert _hole_text(hole, 1, spec) == "⌀20.02–20.04 [.7882–.7889] THRU\n⌵ ⌀21.03 [.828] X 90°"
    spec["csink"] = False
    assert _hole_text(hole, 1, spec) == "⌀20.02–20.04 [.7882–.7889] THRU"
