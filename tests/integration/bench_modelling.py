"""Live bench for the modelling, file and inspection tools.

Needs a running FreeCAD with the addon's RPC server; not collected by pytest.

    FREECAD_MCP_TOKEN=... python tests/integration/bench_modelling.py

Every tool is called as an MCP client would call it, then judged by a
measurement read directly from FreeCAD, independent of the tool's answer:

    OK             the expected effect is measured
    ERROR          the tool raised where it should have worked
    FALSE_SUCCESS  the tool reported success but the measurement disagrees

A case that must be refused (a pocket into empty space) is OK only if the
tool raises with a usable hint and leaves the model unchanged. The bench
opens and closes its own documents (Bench*) without saving them.
"""

import asyncio
import json
import math
import os
import sys
from collections import Counter
from typing import Any

from freecad_mcp.freecad_client import FreeCADConnection
from freecad_mcp.modelisation import register_tools
from freecad_mcp.modelisation.bridge import ExecuteCodeBridge

CONNECTION = FreeCADConnection(host="localhost", port=9875, token=os.environ.get("FREECAD_MCP_TOKEN"))
SHARED_DIR = os.environ.get("BENCH_SHARED_DIR", os.path.expanduser("~/snap/freecad/common/mcp-headless"))
RESULTS: list[tuple[str, str, str, str]] = []
PI = math.pi


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
        raise RuntimeError(text[-400:])
    return json.loads(text.split("__Q__", 1)[1].splitlines()[0])


async def call(_tool: str, **kwargs: Any) -> tuple[bool, Any]:
    try:
        return True, await T[_tool](**kwargs)
    except Exception as e:  # noqa: BLE001
        lines = [line for line in str(e).splitlines() if line.strip()]
        return False, (lines[-1] if lines else repr(e))[:220]


def note(group: str, case: str, verdict: str, detail: str = "") -> None:
    RESULTS.append((group, case, verdict, detail))
    print(f"{verdict:13} {group:9} {case:34} {detail}"[:240], flush=True)


def judge(group: str, case: str, ok: bool, reply: Any, condition: bool, detail_ok: str, detail_ko: str) -> None:
    if not ok:
        note(group, case, "ERROR", str(reply))
    else:
        note(group, case, "OK" if condition else "FALSE_SUCCESS", detail_ok if condition else detail_ko)


# --------------------------------------------------------------------------- FreeCAD reads

def sketch_state(doc: str, sk: str) -> dict:
    return q(f"""
_s = App.getDocument({doc!r}).getObject({sk!r}); _s.recompute()
R = {{"geo": _s.GeometryCount, "cons": _s.ConstraintCount, "solve": _s.solve(), "ext": len(_s.ExternalGeo) - 2,
     "constr": [_s.getConstruction(i) for i in range(_s.GeometryCount)],
     "types": [c.Type for c in _s.Constraints], "g": []}}
for _g in _s.Geometry:
    _d = {{"t": type(_g).__name__}}
    if type(_g).__name__ == "LineSegment":
        _d.update(x1=_g.StartPoint.x, y1=_g.StartPoint.y, x2=_g.EndPoint.x, y2=_g.EndPoint.y)
    if hasattr(_g, "Radius"):
        _d.update(r=_g.Radius, cx=_g.Center.x, cy=_g.Center.y)
    R["g"].append(_d)
""")


def solid(doc: str, obj: str) -> dict:
    return q(f"""
_d = App.getDocument({doc!r}); _d.recompute(); _o = _d.getObject({obj!r}); _sh = _o.Shape
R = {{"vol": round(_sh.Volume, 2) if not _sh.isNull() else None, "valid": (not _sh.isNull()) and _sh.isValid(),
     "faces": len(_sh.Faces) if not _sh.isNull() else 0,
     "zmin": round(_sh.BoundBox.ZMin, 3) if not _sh.isNull() else None, "zmax": round(_sh.BoundBox.ZMax, 3) if not _sh.isNull() else None}}
""")


def body_state(doc: str, body: str) -> dict:
    return q(f"""
_d = App.getDocument({doc!r}); _d.recompute(); _b = _d.getObject({body!r})
R = {{"tip": _b.Tip.Name if _b.Tip else None, "vol": round(_b.Shape.Volume, 2) if not _b.Shape.isNull() else 0,
     "features": [o.Name for o in _b.Group if o.TypeId.startswith("PartDesign::") and o.TypeId not in ("PartDesign::Plane", "PartDesign::Line", "PartDesign::Point")]}}
""")


def close(doc: str) -> None:
    q(f"R = [App.closeDocument(_d) for _d in list(App.listDocuments()) if _d == {doc!r}]")


async def new_body(doc: str) -> str:
    close(doc)
    q(f"App.newDocument({doc!r}); R = True")
    return (await T["create_partdesign_body"](doc_name=doc))["name"]


async def sketch(doc: str, body: str, plane: str = "XY_Plane", offset: float = 0.0) -> str:
    return (await T["create_sketch"](body_name=body, plane=plane, offset=offset, doc_name=doc))["name"]


async def plate(doc: str, body: str, x: float, y: float, w: float, h: float, t: float, reversed: bool = False) -> str:
    sk = await sketch(doc, body)
    await T["add_sketch_rectangle"](sketch_name=sk, x=x, y=y, width=w, height=h, doc_name=doc)
    return (await T["pad_sketch"](sketch_name=sk, length=t, reversed=reversed, doc_name=doc))["name"]


def top_face(doc: str, obj: str) -> str:
    return q(f"""
_sh = App.getDocument({doc!r}).getObject({obj!r}).Shape
R = "Face" + str(max(range(len(_sh.Faces)), key=lambda i: _sh.Faces[i].CenterOfMass.z) + 1)
""")


# --------------------------------------------------------------------------- 1. sketch geometry

async def bench_geometry() -> None:
    G, D = "sketch", "BenchSketch"
    b = await new_body(D)
    s = await sketch(D, b)
    cases = [
        ("add_sketch_line", dict(x1=0, y1=0, x2=10, y2=0), 1),
        ("add_sketch_arc", dict(center_x=0, center_y=20, radius=5, start_angle=0, end_angle=90), 1),
        ("add_sketch_point", dict(x=3, y=3), 1),
        ("add_sketch_circle", dict(center_x=30, center_y=0, radius=4), 1),
        ("add_sketch_ellipse", dict(center_x=50, center_y=0, major_radius=6, minor_radius=3), 1),
        ("add_sketch_polygon", dict(center_x=70, center_y=0, radius=5, sides=6), 6),
        ("add_sketch_slot", dict(center1_x=0, center1_y=40, center2_x=20, center2_y=40, radius=3), 4),
        ("add_sketch_bspline", dict(points=[[0, 60], [10, 65], [20, 60], [30, 66]]), 1),
        ("add_sketch_rectangle", dict(x=40, y=40, width=10, height=5), 4),
    ]
    for tool, kwargs, expected in cases:
        before = sketch_state(D, s)["geo"]
        ok, reply = await call(tool, sketch_name=s, doc_name=D, **kwargs)
        added = sketch_state(D, s)["geo"] - before
        cond = added >= expected if tool == "add_sketch_polygon" else added == expected
        judge(G, tool, ok, reply, cond, f"+{added} geometries", f"+{added} instead of +{expected}")
    before = sketch_state(D, s)["geo"]
    ok, reply = await call("add_sketch_line", sketch_name=s, x1=0, y1=-10, x2=10, y2=-10, construction=True, doc_name=D)
    st = sketch_state(D, s)
    judge(G, "add_sketch_line (construction)", ok, reply, st["geo"] == before + 1 and st["constr"][-1] is True,
          "construction line", f"construction={st['constr'][-1]}")
    ok, reply = await call("get_sketch_info", sketch_name=s, doc_name=D)
    st = sketch_state(D, s)
    seen = json.dumps(reply, default=str) if ok else ""
    judge(G, "get_sketch_info", ok, reply, str(st["geo"]) in seen and str(st["cons"]) in seen,
          f"reports {st['geo']} geometries / {st['cons']} constraints", f"reply lacks {st['geo']}/{st['cons']}: {seen[:90]}")
    before = sketch_state(D, s)["constr"][0]
    ok, reply = await call("toggle_construction", sketch_name=s, geometry_index=0, doc_name=D)
    after = sketch_state(D, s)["constr"][0]
    judge(G, "toggle_construction", ok, reply, after != before, f"{before} -> {after}", f"stays {after}")
    before = sketch_state(D, s)["geo"]
    ok, reply = await call("delete_sketch_geometry", sketch_name=s, geometry_index=before - 1, doc_name=D)
    after = sketch_state(D, s)["geo"]
    judge(G, "delete_sketch_geometry", ok, reply, after == before - 1, f"{before} -> {after}", f"{before} -> {after}")
    close(D)


# --------------------------------------------------------------------------- 2. constraints

def length(g: dict) -> float:
    return math.hypot(g["x2"] - g["x1"], g["y2"] - g["y1"])


def heading(g: dict) -> float:
    return math.degrees(math.atan2(g["y2"] - g["y1"], g["x2"] - g["x1"]))


async def bench_constraints() -> None:
    G, D = "constraint", "BenchConstraints"
    b = await new_body(D)
    L, C = "add_sketch_line", "add_sketch_circle"

    async def fresh(*geometry):
        s = await sketch(D, b)
        for kind, kwargs in geometry:
            await T[kind](sketch_name=s, doc_name=D, **kwargs)
        return s

    cross = lambda e: (e["g"][0]["x2"] - e["g"][0]["x1"]) * (e["g"][1]["y2"] - e["g"][1]["y1"]) - (e["g"][0]["y2"] - e["g"][0]["y1"]) * (e["g"][1]["x2"] - e["g"][1]["x1"])  # noqa: E731
    dot = lambda e: (e["g"][0]["x2"] - e["g"][0]["x1"]) * (e["g"][1]["x2"] - e["g"][1]["x1"]) + (e["g"][0]["y2"] - e["g"][0]["y1"]) * (e["g"][1]["y2"] - e["g"][1]["y1"])  # noqa: E731
    cases = [
        ("constrain_horizontal", [(L, dict(x1=0, y1=0, x2=10, y2=3))], dict(geometry_index=0), lambda e: abs(e["g"][0]["y1"] - e["g"][0]["y2"]) < 1e-6, "y1 = y2"),
        ("constrain_vertical", [(L, dict(x1=0, y1=0, x2=3, y2=10))], dict(geometry_index=0), lambda e: abs(e["g"][0]["x1"] - e["g"][0]["x2"]) < 1e-6, "x1 = x2"),
        ("constrain_coincident", [(L, dict(x1=0, y1=0, x2=10, y2=0)), (L, dict(x1=10.5, y1=0.5, x2=15, y2=8))], dict(geometry1=0, point1=2, geometry2=1, point2=1),
         lambda e: math.hypot(e["g"][0]["x2"] - e["g"][1]["x1"], e["g"][0]["y2"] - e["g"][1]["y1"]) < 1e-6, "end of 0 = start of 1"),
        ("constrain_parallel", [(L, dict(x1=0, y1=0, x2=10, y2=0)), (L, dict(x1=0, y1=5, x2=10, y2=7))], dict(geometry1=0, geometry2=1), lambda e: abs(cross(e)) < 1e-6, "cross product 0"),
        ("constrain_perpendicular", [(L, dict(x1=0, y1=0, x2=10, y2=0)), (L, dict(x1=0, y1=5, x2=2, y2=15))], dict(geometry1=0, geometry2=1), lambda e: abs(dot(e)) < 1e-6, "dot product 0"),
        ("constrain_tangent", [(L, dict(x1=0, y1=0, x2=20, y2=0)), (C, dict(center_x=10, center_y=5, radius=3))], dict(geometry1=0, geometry2=1),
         lambda e: abs(e["g"][0]["y1"] - e["g"][0]["y2"]) < 1e-9 and abs(abs(e["g"][1]["cy"] - e["g"][0]["y1"]) - e["g"][1]["r"]) < 1e-6, "centre-line distance = radius"),
        ("constrain_equal", [(L, dict(x1=0, y1=0, x2=10, y2=0)), (L, dict(x1=0, y1=5, x2=7, y2=5))], dict(geometry1=0, geometry2=1), lambda e: abs(length(e["g"][0]) - length(e["g"][1])) < 1e-6, "equal lengths"),
        ("constrain_distance", [(L, dict(x1=0, y1=0, x2=10, y2=0))], dict(geometry1=0, distance=15), lambda e: abs(length(e["g"][0]) - 15) < 1e-6, "length 15"),
        ("constrain_distance_x", [(L, dict(x1=1, y1=1, x2=10, y2=2))], dict(geometry=0, point=1, distance=7), lambda e: abs(e["g"][0]["x1"] - 7) < 1e-6, "start x = 7"),
        ("constrain_distance_y", [(L, dict(x1=1, y1=1, x2=10, y2=2))], dict(geometry=0, point=1, distance=4), lambda e: abs(e["g"][0]["y1"] - 4) < 1e-6, "start y = 4"),
        ("constrain_radius", [(C, dict(center_x=0, center_y=0, radius=3))], dict(geometry_index=0, radius=6), lambda e: abs(e["g"][0]["r"] - 6) < 1e-6, "radius 6"),
        ("constrain_angle", [(L, dict(x1=0, y1=0, x2=10, y2=2))], dict(geometry1=0, angle=30), lambda e: abs(heading(e["g"][0]) - 30) < 1e-4, "angle 30 deg"),
        ("constrain_fix", [(L, dict(x1=0, y1=0, x2=10, y2=0))], dict(geometry_index=0), lambda e: "Block" in e["types"], "Block constraint"),
        ("add_sketch_constraint", [(L, dict(x1=0, y1=0, x2=10, y2=0))], dict(constraint_type="Distance", geometry1=0, value=12), lambda e: abs(length(e["g"][0]) - 12) < 1e-6, "Distance 12 -> length 12"),
    ]
    for tool, geometry, kwargs, test, what in cases:
        s = await fresh(*geometry)
        before = sketch_state(D, s)["cons"]
        ok, reply = await call(tool, sketch_name=s, doc_name=D, **kwargs)
        st = sketch_state(D, s)
        effect = bool(st["g"]) and test(st)
        judge(G, tool, ok, reply, effect and st["cons"] == before + 1 and st["solve"] == 0,
              f"{what} (solver={st['solve']}, +{st['cons'] - before} constraint)",
              f"{what} not reached: {json.dumps(st['g'])[:110]} solver={st['solve']} constraints {before}->{st['cons']}")
    s = await fresh((L, dict(x1=0, y1=0, x2=10, y2=0)))
    await T["constrain_horizontal"](sketch_name=s, geometry_index=0, doc_name=D)
    before = sketch_state(D, s)["cons"]
    ok, reply = await call("delete_sketch_constraint", sketch_name=s, constraint_index=0, doc_name=D)
    after = sketch_state(D, s)["cons"]
    judge(G, "delete_sketch_constraint", ok, reply, after == before - 1, f"{before} -> {after}", f"{before} -> {after}")
    close(D)


# --------------------------------------------------------------------------- 3. features

async def expect_volume(case: str, tool: str, doc: str, base: str, expected: float | None, tol: float, **kwargs: Any) -> str | None:
    """Run a feature tool; OK when the volume matches (or changes, if expected is None)."""
    v0 = solid(doc, base)["vol"]
    ok, reply = await call(tool, doc_name=doc, **kwargs)
    if not ok:
        note("feature", case, "ERROR", str(reply))
        return None
    st = solid(doc, reply["name"])
    reported = (reply.get("verification") or {}).get("volume_after")
    honest = reported is not None and abs(reported - st["vol"]) < 0.01
    if expected is None:
        cond, what = st["valid"] and st["vol"] != v0, f"volume {v0} -> {st['vol']}"
    else:
        cond, what = st["valid"] and abs(st["vol"] - expected) <= tol, f"volume {st['vol']} (expected {round(expected, 2)})"
    cond = cond and honest
    note("feature", case, "OK" if cond else "FALSE_SUCCESS", what + ("" if honest else f" ; reported volume_after={reported}"))
    return reply["name"]


async def expect_refusal(case: str, tool: str, doc: str, body: str, hint: str, **kwargs: Any) -> None:
    """OK when the tool raises with ``hint`` and the body is left as it was."""
    before = body_state(doc, body)
    ok, reply = await call(tool, doc_name=doc, **kwargs)
    after = body_state(doc, body)
    unchanged = after == before
    if ok:
        note("feature", case, "FALSE_SUCCESS", f"accepted: {str(reply)[:120]}")
    elif hint in str(reply) and unchanged:
        note("feature", case, "OK", f"refused, model unchanged ({after['vol']} mm3, tip {after['tip']})")
    else:
        note("feature", case, "ERROR", f"hint={hint in str(reply)} unchanged={unchanged}: {str(reply)[:120]}")


async def bench_features() -> None:
    disc = PI * 3 ** 2

    D = "BenchPad"
    b = await new_body(D)
    s = await sketch(D, b)
    await T["add_sketch_rectangle"](sketch_name=s, x=0, y=0, width=40, height=20, doc_name=D)
    ok, reply = await call("pad_sketch", sketch_name=s, length=3, symmetric=True, doc_name=D)
    st = solid(D, reply["name"]) if ok else {}
    judge("feature", "pad_sketch symmetric", ok, reply, ok and abs(st["vol"] - 2400) < 0.01 and abs(st["zmin"] + 1.5) < 1e-3,
          f"volume {st.get('vol')}, z {st.get('zmin')}..{st.get('zmax')}", f"volume {st.get('vol')}, z {st.get('zmin')}..{st.get('zmax')}")
    close(D)

    for case, tool, kwargs in (
        ("pocket into empty space", "pocket_sketch", dict(length=2)),
        ("hole into empty space", "create_hole", dict(diameter=6, depth=10)),
    ):
        D = "BenchRefusal"
        b = await new_body(D)
        p = await plate(D, b, 0, 0, 40, 20, 3)
        s = await sketch(D, b)
        await T["add_sketch_circle"](sketch_name=s, center_x=20, center_y=10, radius=3, doc_name=D)
        await expect_refusal(case, tool, D, b, "reversed=True", sketch_name=s, **kwargs)
        depth = 2 if tool == "pocket_sketch" else 3  # the 10 mm hole goes through the 3 mm plate
        await expect_volume(case.split()[0] + " reversed=True", tool, D, p, 2400 - disc * depth, 0.5,
                            sketch_name=s, reversed=True, **kwargs)
        close(D)

    D = "BenchPocket"
    b = await new_body(D)
    p = await plate(D, b, 0, 0, 40, 20, 3)
    s = await sketch(D, b, plane=f"{p}:{top_face(D, p)}")
    await T["add_sketch_circle"](sketch_name=s, center_x=20, center_y=10, radius=3, doc_name=D)
    await expect_volume("pocket from the top face", "pocket_sketch", D, p, 2400 - disc * 2, 0.05, sketch_name=s, length=2)
    close(D)
    D = "BenchPocket"
    b = await new_body(D)
    p = await plate(D, b, 0, 0, 40, 20, 3, reversed=True)
    s = await sketch(D, b)
    await T["add_sketch_circle"](sketch_name=s, center_x=20, center_y=10, radius=3, doc_name=D)
    await expect_volume("pocket ThroughAll", "pocket_sketch", D, p, 2400 - disc * 3, 0.05, sketch_name=s, length=1, type="ThroughAll")
    close(D)

    D = "BenchPattern"
    b = await new_body(D)
    await plate(D, b, 0, 0, 60, 20, 3, reversed=True)
    s = await sketch(D, b)
    await T["add_sketch_circle"](sketch_name=s, center_x=10, center_y=10, radius=3, doc_name=D)
    pocket = (await T["pocket_sketch"](sketch_name=s, length=1, type="ThroughAll", doc_name=D))["name"]
    await expect_volume("linear_pattern", "linear_pattern", D, pocket, 3600 - 3 * disc * 3, 0.1, feature_name=pocket, direction="X", length=40, occurrences=3)
    close(D)
    D = "BenchPolar"
    b = await new_body(D)
    s = await sketch(D, b)
    await T["add_sketch_circle"](sketch_name=s, center_x=0, center_y=0, radius=20, doc_name=D)
    await T["pad_sketch"](sketch_name=s, length=3, reversed=True, doc_name=D)
    s = await sketch(D, b)
    await T["add_sketch_circle"](sketch_name=s, center_x=12, center_y=0, radius=2, doc_name=D)
    pocket = (await T["pocket_sketch"](sketch_name=s, length=1, type="ThroughAll", doc_name=D))["name"]
    await expect_volume("polar_pattern", "polar_pattern", D, pocket, PI * 400 * 3 - 6 * PI * 4 * 3, 0.1, feature_name=pocket, axis="Z", occurrences=6)
    close(D)
    D = "BenchMirror"
    b = await new_body(D)
    await plate(D, b, -20, -10, 40, 20, 3, reversed=True)
    s = await sketch(D, b)
    await T["add_sketch_circle"](sketch_name=s, center_x=10, center_y=0, radius=3, doc_name=D)
    pocket = (await T["pocket_sketch"](sketch_name=s, length=1, type="ThroughAll", doc_name=D))["name"]
    await expect_volume("mirrored_feature", "mirrored_feature", D, pocket, 2400 - 2 * disc * 3, 0.1, feature_name=pocket, plane="YZ")
    close(D)

    for case, tool, kwargs in (
        ("fillet (2 edges)", "fillet_edges", dict(radius=1, edges=["Edge1", "Edge3"])),
        ("fillet (all edges)", "fillet_edges", dict(radius=1)),
        ("chamfer (2 edges)", "chamfer_edges", dict(size=1, edges=["Edge1", "Edge3"])),
        ("chamfer (all edges)", "chamfer_edges", dict(size=1)),
    ):
        D = "BenchEdges"
        b = await new_body(D)
        p = await plate(D, b, 0, 0, 40, 20, 3)
        await expect_volume(case, tool, D, p, None, 0, object_name=p, **kwargs)
        close(D)

    D = "BenchShell"
    b = await new_body(D)
    p = await plate(D, b, 0, 0, 20, 20, 10)
    await expect_volume("thickness_feature", "thickness_feature", D, p, 4000 - 18 * 18 * 9, 1.0, object_name=p, thickness=1, faces_to_remove=[top_face(D, p)])
    close(D)
    D = "BenchDraft"
    b = await new_body(D)
    p = await plate(D, b, 0, 0, 20, 20, 10)
    await expect_volume("draft_feature (no faces given)", "draft_feature", D, p, None, 0, object_name=p, angle=5)
    close(D)

    D = "BenchRevolve"
    b = await new_body(D)
    s = await sketch(D, b, "XZ_Plane")
    await T["add_sketch_rectangle"](sketch_name=s, x=5, y=0, width=5, height=10, doc_name=D)
    ring = PI * (100 - 25) * 10
    ok, reply = await call("revolution_sketch", sketch_name=s, axis="Base_Z", doc_name=D)
    st = solid(D, reply["name"]) if ok else {}
    judge("feature", "revolution_sketch", ok, reply, ok and abs(st["vol"] - ring) < 0.1, f"volume {st.get('vol')} (expected {round(ring, 2)})", f"volume {st.get('vol')}")
    if ok:
        s = await sketch(D, b, "XZ_Plane")
        await T["add_sketch_rectangle"](sketch_name=s, x=8, y=4, width=4, height=2, doc_name=D)
        await expect_volume("groove_sketch", "groove_sketch", D, reply["name"], ring - PI * (100 - 64) * 2, 0.1, sketch_name=s, axis="Base_Z")
    close(D)

    D = "BenchLoft"
    b = await new_body(D)
    s1 = await sketch(D, b)
    await T["add_sketch_rectangle"](sketch_name=s1, x=-5, y=-5, width=10, height=10, doc_name=D)
    s2 = await sketch(D, b, offset=10)
    await T["add_sketch_circle"](sketch_name=s2, center_x=0, center_y=0, radius=4, doc_name=D)
    ok, reply = await call("loft_sketches", sketch_names=[s1, s2], doc_name=D)
    st = solid(D, reply["name"]) if ok else {}
    judge("feature", "loft_sketches (offset sketch)", ok, reply, ok and st["valid"] and 500 < st["vol"] < 1000, f"volume {st.get('vol')}", f"volume {st.get('vol')}")
    close(D)
    D = "BenchLoftCut"
    b = await new_body(D)
    p = await plate(D, b, -15, -15, 30, 30, 20)
    s1 = await sketch(D, b)
    await T["add_sketch_rectangle"](sketch_name=s1, x=-5, y=-5, width=10, height=10, doc_name=D)
    dp = (await T["create_datum_plane"](body_name=b, offset=10, doc_name=D))["name"]
    s2 = await sketch(D, b, plane=dp)
    await T["add_sketch_circle"](sketch_name=s2, center_x=0, center_y=0, radius=4, doc_name=D)
    await expect_volume("subtractive_loft (datum sketch)", "subtractive_loft", D, p, None, 0, sketch_names=[s1, s2])
    close(D)
    for tool, on_solid in (("sweep_sketch", False), ("subtractive_pipe", True)):
        D = "BenchSweep"
        b = await new_body(D)
        base = await plate(D, b, -10, -10, 20, 20, 30) if on_solid else None
        prof = await sketch(D, b)
        await T["add_sketch_circle"](sketch_name=prof, center_x=0, center_y=0, radius=2, doc_name=D)
        spine = await sketch(D, b, "XZ_Plane")
        await T["add_sketch_line"](sketch_name=spine, x1=0, y1=0, x2=0, y2=20, doc_name=D)
        tube = PI * 4 * 20
        if on_solid:
            await expect_volume(tool, tool, D, base, 12000 - tube, 0.5, profile_sketch=prof, spine_sketch=spine)
        else:
            ok, reply = await call(tool, profile_sketch=prof, spine_sketch=spine, doc_name=D)
            st = solid(D, reply["name"]) if ok else {}
            judge("feature", tool, ok, reply, ok and abs(st["vol"] - tube) < 0.5, f"volume {st.get('vol')} (expected {round(tube, 2)})", f"volume {st.get('vol')}")
        close(D)

    D = "BenchDatum"
    b = await new_body(D)
    ok, reply = await call("create_datum_plane", body_name=b, offset=10, doc_name=D)
    z = q(f"R = App.getDocument({D!r}).getObject({reply['name']!r}).Placement.Base.z") if ok else None
    judge("feature", "create_datum_plane", ok, reply, ok and abs(z - 10) < 1e-6, "plane at z = 10", f"z = {z}")
    for axis, want in (("X_Axis", [1, 0, 0]), ("Y_Axis", [0, 1, 0])):
        ok, reply = await call("create_datum_line", body_name=b, base_axis=axis, doc_name=D)
        d = q(f"R = [round(v, 6) for v in App.getDocument({D!r}).getObject({reply['name']!r}).Shape.Edges[0].Curve.Direction]") if ok else None
        judge("feature", f"create_datum_line {axis}", ok, reply, ok and [abs(v) for v in d] == want, f"direction {d}", f"direction {d}")
    ok, reply = await call("create_datum_point", body_name=b, position=[1, 2, 3], doc_name=D)
    pt = q(f"_p = App.getDocument({D!r}).getObject({reply['name']!r}).Placement.Base; R = [_p.x, _p.y, _p.z]") if ok else None
    judge("feature", "create_datum_point", ok, reply, ok and pt == [1, 2, 3], f"point {pt}", f"point {pt}")
    p = await plate(D, b, 0, 0, 40, 20, 3)
    s = await sketch(D, b)
    before = sketch_state(D, s)["ext"]
    ok, reply = await call("add_external_geometry", sketch_name=s, object_name=p, element="Edge1", doc_name=D)
    after = sketch_state(D, s)["ext"]
    judge("feature", "add_external_geometry", ok, reply, after == before + 1 and reply.get("external_geometry_count") == after,
          f"external {before} -> {after}", f"external {before} -> {after}, reported {reply.get('external_geometry_count') if ok else None}")
    close(D)


# --------------------------------------------------------------------------- 4. spreadsheet

async def bench_spreadsheet() -> None:
    G, D = "sheet", "BenchSheet"
    close(D)
    q(f"App.newDocument({D!r}); R = True")
    ok, reply = await call("spreadsheet_create", name="Params", doc_name=D)
    kind = q(f"_o = App.getDocument({D!r}).getObject('Params'); R = _o.TypeId if _o else None")
    judge(G, "spreadsheet_create", ok, reply, kind == "Spreadsheet::Sheet", "sheet created", f"type {kind}")
    ok, reply = await call("spreadsheet_set_cell", spreadsheet_name="Params", cell="A1", value=12, doc_name=D)
    v = q(f"App.getDocument({D!r}).recompute(); R = App.getDocument({D!r}).getObject('Params').get('A1')")
    judge(G, "spreadsheet_set_cell", ok, reply, v == 12, "A1 = 12", f"A1 = {v}")
    ok, reply = await call("spreadsheet_set_alias", spreadsheet_name="Params", cell="A1", alias="length", doc_name=D)
    alias = q(f"R = App.getDocument({D!r}).getObject('Params').getAlias('A1')")
    judge(G, "spreadsheet_set_alias", ok, reply, alias == "length", "alias length", f"alias {alias}")
    ok, reply = await call("spreadsheet_get_cell", spreadsheet_name="Params", cell="A1", doc_name=D)
    judge(G, "spreadsheet_get_cell", ok, reply, ok and "12" in json.dumps(reply) and reply.get("alias") == "length", "12, alias length", f"{reply}")
    ok, reply = await call("spreadsheet_get_aliases", spreadsheet_name="Params", doc_name=D)
    judge(G, "spreadsheet_get_aliases", ok, reply, ok and reply.get("aliases") == {"length": "A1"}, "length -> A1", f"{reply}")
    await T["spreadsheet_set_cell"](spreadsheet_name="Params", cell="B2", value=3, doc_name=D)
    ok, reply = await call("spreadsheet_get_cell_range", spreadsheet_name="Params", start_cell="A1", end_cell="B2", doc_name=D)
    seen = json.dumps(reply) if ok else ""
    judge(G, "spreadsheet_get_cell_range", ok, reply, "12" in seen and "3" in seen, "A1 and B2 read", seen[:100])
    ok, reply = await call("spreadsheet_clear_cell", spreadsheet_name="Params", cell="B2", doc_name=D)
    left = q(f"R = App.getDocument({D!r}).getObject('Params').getContents('B2')")
    judge(G, "spreadsheet_clear_cell", ok, reply, left in ("", None), "B2 cleared", f"B2 = {left!r}")
    q(f"App.getDocument({D!r}).addObject('Part::Box', 'Box'); App.getDocument({D!r}).recompute(); R = True")
    ok, reply = await call("spreadsheet_bind_property", spreadsheet_name="Params", alias="length", target_object="Box", target_property="Length", doc_name=D)
    box_length = q(f"App.getDocument({D!r}).recompute(); R = App.getDocument({D!r}).getObject('Box').Length.Value")
    judge(G, "spreadsheet_bind_property", ok, reply, box_length == 12, "Box.Length = 12", f"Length = {box_length}")
    csv_path = os.path.join(SHARED_DIR, "bench_sheet.csv")
    ok, reply = await call("spreadsheet_export_csv", spreadsheet_name="Params", file_path=csv_path, doc_name=D)
    content = open(csv_path, encoding="utf-8").read() if os.path.exists(csv_path) else ""
    judge(G, "spreadsheet_export_csv", ok, reply, "12" in content, f"{len(content)} bytes", f"file: {content[:60]!r}")
    await T["spreadsheet_create"](name="Import", doc_name=D)
    with open(csv_path, "w", encoding="utf-8") as f:
        f.write("7,8\n9,10\n")
    ok, reply = await call("spreadsheet_import_csv", spreadsheet_name="Import", file_path=csv_path, doc_name=D)
    values = q(f"_s = App.getDocument({D!r}).getObject('Import'); R = [_s.getContents(c) for c in ('A1', 'B1', 'A2', 'B2')]")
    judge(G, "spreadsheet_import_csv", ok, reply, [str(x).lstrip("=") for x in values] == ["7", "8", "9", "10"], f"read {values}", f"read {values}")
    os.remove(csv_path)
    close(D)


# --------------------------------------------------------------------------- 5. files

HOLE = PI * 9 * 3  # the 6 mm hole through the 3 mm plate
PLATE = 2400 - HOLE
SHARED_TILDE = "~/" + os.path.relpath(SHARED_DIR, os.path.expanduser("~"))  # exercises ~ resolution


async def holed_plate(doc: str) -> tuple[str, str, str]:
    """A 40 x 20 x 3 plate with a 6 mm hole through it; returns body, pad, pocket."""
    b = await new_body(doc)
    p = await plate(doc, b, 0, 0, 40, 20, 3)
    s = await sketch(doc, b, plane=f"{p}:{top_face(doc, p)}")
    await T["add_sketch_circle"](sketch_name=s, center_x=20, center_y=10, radius=3, doc_name=doc)
    pocket = (await T["pocket_sketch"](sketch_name=s, length=1, type="ThroughAll", doc_name=doc))["name"]
    return b, p, pocket


def read_brep(path: str) -> list:
    return q(f"import Part\n_s = Part.Shape(); _s.read({path!r}); R = [len(_s.Solids), round(_s.Volume, 2), len(_s.Faces), round(_s.Area, 2)]")


def read_mesh(path: str) -> list:
    return q(f"import Mesh\n_m = Mesh.Mesh({path!r}); R = [_m.CountFacets, round(_m.Volume, 2)]")


async def bench_files() -> None:
    G, D = "files", "BenchFiles"
    b, p, pocket = await holed_plate(D)
    real = lambda name: os.path.join(SHARED_DIR, name)  # noqa: E731
    tilde = lambda name: SHARED_TILDE + "/" + name  # noqa: E731

    ok, reply = await call("export_step", file_path=tilde("bench.step"), doc_name=D)
    back = read_brep(real("bench.step")) if ok and os.path.exists(real("bench.step")) else None
    judge(G, "export_step (default selection)", ok, reply, back is not None and back[0] == 1 and abs(back[1] - PLATE) < 0.01 and reply["objects"] == [b],
          f"file holds 1 solid of {back and back[1]} mm3, objects {reply.get('objects') if ok else None}", f"file {back}, objects {reply.get('objects') if ok else None}")
    ok, reply = await call("export_step", file_path=tilde("bench_pad.step"), object_names=[p], doc_name=D)
    back = read_brep(real("bench_pad.step")) if ok else None
    judge(G, "export_step (named feature)", ok, reply, back is not None and back[0] == 1 and abs(back[1] - 2400) < 0.01, f"1 solid of {back and back[1]} mm3", f"file {back}")
    ok, reply = await call("export_iges", file_path=tilde("bench.igs"), doc_name=D)
    back = read_brep(real("bench.igs")) if ok else None
    area = q(f"R = round(App.getDocument({D!r}).getObject({b!r}).Shape.Area, 2)")
    judge(G, "export_iges", ok, reply, back is not None and back[2] == 7 and abs(back[3] - area) < 0.1, f"{back and back[2]} faces, area {back and back[3]}", f"file {back}, expected area {area}")
    for tool, ext in (("export_stl", "stl"), ("export_3mf", "3mf"), ("export_obj", "obj")):
        ok, reply = await call(tool, file_path=tilde(f"bench.{ext}"), doc_name=D)
        back = read_mesh(real(f"bench.{ext}")) if ok else None
        judge(G, tool, ok, reply, back is not None and back[0] > 0 and abs(back[1] - PLATE) < 0.02 * PLATE,
              f"{back and back[0]} facets, mesh volume {back and back[1]}", f"file {back}")
    ok, reply = await call("export_step", file_path="/tmp/bench.step", doc_name=D)
    judge(G, "export_step to /tmp refused", not ok, reply, "home" in str(reply), "refused with the snap hint", f"{reply}")
    ok, reply = await call("export_dxf", file_path=tilde("bench.dxf"), face=f"{pocket}:{top_face(D, pocket)}", doc_name=D)
    ent = reply.get("entities") if ok else None
    flat = reply.get("flat") if ok else None
    judge(G, "export_dxf (top face)", ok, reply, ent == {"LINE": 4, "CIRCLE": 1} and flat["width"] == 40 and flat["height"] == 20 and flat["holes"] == 1,
          f"entities {ent}, flat {flat}", f"entities {ent}, flat {flat}")
    side = q(f"""_sh = App.getDocument({D!r}).getObject({pocket!r}).Shape
R = "Face" + str(min(range(len(_sh.Faces)), key=lambda i: _sh.Faces[i].CenterOfMass.y if type(_sh.Faces[i].Surface).__name__ == "Plane" else 1e9) + 1)""")
    ok, reply = await call("export_dxf", file_path=tilde("bench_side.dxf"), face=f"{pocket}:{side}", doc_name=D)
    flat = reply.get("flat") if ok else None
    judge(G, "export_dxf (side face, laid flat)", ok, reply, flat is not None and sorted([flat["width"], flat["height"]]) == [3, 40] and reply["entities"].get("LINE") == 4,
          f"flat {flat}, entities {reply.get('entities') if ok else None}", f"flat {flat}")
    q(f"App.getDocument({D!r}).getObject({pocket!r}).Profile[0].Visibility = True; R = True")
    sk_name = q(f"R = App.getDocument({D!r}).getObject({pocket!r}).Profile[0].Name")
    ok, reply = await call("export_dxf", file_path=tilde("bench_sketch.dxf"), object_names=[sk_name], doc_name=D)
    judge(G, "export_dxf (sketch)", ok, reply, ok and reply["entities"].get("CIRCLE") == 1, f"entities {reply.get('entities') if ok else None}", f"{reply}")
    # a plate whose outline is cut in pieces, as an unfolder leaves it at the
    # bend lines: long sides in 3 lines, a rounded corner in 2 arcs, a hole in
    # 2 half circles (12 edges); the DXF must hold one entity per side, arc and
    # hole (6 edges)
    # (its own document, so the undo cases below still see the pocket last)
    split = "BenchSplit"
    close(split)
    area = q(f"""import Part, math
_d = App.newDocument({split!r})
_v = App.Vector
_c = Part.Circle(_v(35, 15, 0), _v(0, 0, 1), 5)
_outer = Part.Wire([Part.LineSegment(_v(0, 0, 0), _v(10, 0, 0)).toShape(), Part.LineSegment(_v(10, 0, 0), _v(25, 0, 0)).toShape(),
                    Part.LineSegment(_v(25, 0, 0), _v(40, 0, 0)).toShape(), Part.LineSegment(_v(40, 0, 0), _v(40, 15, 0)).toShape(),
                    Part.Edge(_c, 0, math.pi / 4), Part.Edge(_c, math.pi / 4, math.pi / 2),
                    Part.LineSegment(_v(35, 20, 0), _v(25, 20, 0)).toShape(), Part.LineSegment(_v(25, 20, 0), _v(10, 20, 0)).toShape(),
                    Part.LineSegment(_v(10, 20, 0), _v(0, 20, 0)).toShape(), Part.LineSegment(_v(0, 20, 0), _v(0, 0, 0)).toShape()])
_h = Part.Circle(_v(15, 10, 0), _v(0, 0, 1), 4)
_hole = Part.Wire([Part.Edge(_h, 0, math.pi), Part.Edge(_h, math.pi, 2 * math.pi)])
_o = _d.addObject("Part::Feature", "SplitPlate"); _o.Shape = Part.makeFace([_outer, _hole], "Part::FaceMakerBullseye")
_d.recompute(); R = round(_o.Shape.Area, 6)""")
    ok, reply = await call("export_dxf", file_path=tilde("bench_split.dxf"), face="SplitPlate:Face1", doc_name=split)
    flat = reply.get("flat") if ok else None
    judge(G, "export_dxf joins split edges", ok, reply,
          reply["entities"] == {"LINE": 4, "ARC": 1, "CIRCLE": 1} and flat["joined_edges"] == 6 and abs(flat["area"] - area) < 1e-6 and flat["holes"] == 1,
          f"entities {reply.get('entities') if ok else None}, {flat and flat['joined_edges']} edges joined, area kept",
          f"entities {reply.get('entities') if ok else None}, flat {flat}, area before {area}")
    close(split)

    # undo / redo on the pocket
    before = body_state(D, b)["vol"]
    ok, reply = await call("undo", doc_name=D)
    undone = body_state(D, b)["vol"]
    ok2, reply2 = await call("redo", doc_name=D)
    redone = body_state(D, b)["vol"]
    judge(G, "undo then redo", ok and ok2, reply if not ok else reply2, abs(undone - 2400) < 0.01 and abs(redone - before) < 0.01,
          f"{before} -> undo {undone} -> redo {redone}", f"{before} -> undo {undone} -> redo {redone} ({reply} / {reply2})")
    ok, reply = await call("undo", doc_name=D, steps=1000)
    judge(G, "undo beyond history refused", not ok, reply, "available" in str(reply), "refused, history listed", str(reply))

    # save, close, open
    ok, reply = await call("close_document", doc_name=D)
    still = D in q("R = list(App.listDocuments())")
    judge(G, "close refuses unsaved changes", not ok, reply, still and "unsaved changes" in str(reply), "refused, document still open", f"{reply}, open={still}")
    ok, reply = await call("save_document", doc_name=D, file_path=tilde("bench_doc"))
    saved = real("bench_doc.FCStd")
    judge(G, "save_document", ok, reply, os.path.exists(saved) and reply["path"] == saved, f"{reply.get('bytes') if ok else None} bytes at {saved}", f"{reply}")
    ok, reply = await call("close_document", doc_name=D)
    judge(G, "close_document after save", ok, reply, D not in q("R = list(App.listDocuments())"), "closed", f"{reply}")
    ok, reply = await call("open_document", file_path=tilde("bench_doc.FCStd"))
    D = reply["name"] if ok else D  # a reopened document is named after its file
    vol = body_state(D, b)["vol"] if ok else None
    judge(G, "open_document", ok, reply, ok and abs(vol - PLATE) < 0.01, f"{D}: {reply.get('object_count') if ok else None} objects, body {vol} mm3", f"{reply}, body {vol}")
    ok, reply = await call("recompute_document", doc_name=D)
    judge(G, "recompute_document", ok, reply, ok and reply["objects_in_error"] == [], f"{reply.get('recomputed') if ok else None} recomputed, no error", f"{reply}")
    q(f"App.getDocument({D!r}).addObject('Part::Box', 'Extra'); App.getDocument({D!r}).recompute(); R = True")
    ok, reply = await call("close_document", doc_name=D)
    still = D in q("R = list(App.listDocuments())")
    judge(G, "close refuses changes made after a save", not ok, reply, still and "unsaved changes" in str(reply), "refused, document still open", f"{reply}, open={still}")
    ok, reply = await call("close_document", doc_name=D, discard_changes=True)
    judge(G, "close_document discard_changes", ok, reply, D not in q("R = list(App.listDocuments())"), "closed, change discarded", f"{reply}")

    # imports
    q("R = [App.closeDocument(d) for d in list(App.listDocuments()) if d == 'BenchImport']; App.newDocument('BenchImport'); R = True")
    ok, reply = await call("import_step", file_path=tilde("bench.step"), doc_name="BenchImport")
    judge(G, "import_step", ok, reply, ok and reply["solids"] == 1 and abs(reply["volume"] - PLATE) < 0.01, f"{reply.get('solids') if ok else None} solid, {reply.get('volume') if ok else None} mm3", f"{reply}")
    ok, reply = await call("import_stl", file_path=tilde("bench.stl"), doc_name="BenchImport")
    facets = q(f"R = App.getDocument('BenchImport').getObject({reply['object']!r}).Mesh.CountFacets") if ok else None
    judge(G, "import_stl", ok, reply, ok and facets and facets == reply["facets"], f"{facets} facets", f"{reply}")
    ok, reply = await call("import_step", file_path=tilde("missing.step"), doc_name="BenchImport")
    judge(G, "import of a missing file refused", not ok, reply, "not found" in str(reply), "refused", str(reply))
    close("BenchImport")
    for name in ("bench.step", "bench_pad.step", "bench.igs", "bench.stl", "bench.3mf", "bench.obj", "bench.dxf", "bench_side.dxf", "bench_sketch.dxf", "bench_split.dxf", "bench_doc.FCStd"):
        if os.path.exists(real(name)):
            os.remove(real(name))
    for leftover in os.listdir(SHARED_DIR):
        if leftover.startswith("bench_doc"):
            os.remove(real(leftover))


# --------------------------------------------------------------------------- 6. inspection

async def bench_inspection() -> None:
    G, D = "inspect", "BenchInspect"
    b, p, pocket = await holed_plate(D)
    ok, reply = await call("get_topology", object_name=pocket, doc_name=D)
    faces = {f["name"]: f for f in reply["faces"]} if ok else {}
    top = [f for f in faces.values() if f["type"] == "Plane" and f.get("normal") == [0.0, 0.0, 1.0]]
    cyl = [f for f in faces.values() if f["type"] == "Cylinder"]
    # every edge joins two faces, except the hole's seam, which runs along the cylinder only
    single = [e for e in reply["edges"] if len(e["faces"]) != 2] if ok else []
    lines_ok = ok and len(single) == 1 and cyl and single[0]["faces"] == [cyl[0]["name"]]
    judge(G, "get_topology", ok, reply, ok and reply["face_count"] == 7 and reply["edge_count"] == 15 and len(top) == 1
          and abs(top[0]["center"][2] - 3) < 1e-6 and len(cyl) == 1 and cyl[0]["radius"] == 3 and lines_ok,
          f"{reply.get('face_count') if ok else None} faces, {reply.get('edge_count') if ok else None} edges, top {top and top[0]['name']}, hole r={cyl and cyl[0]['radius']}, edges join 2 faces but the seam",
          f"faces {reply.get('face_count') if ok else None}, top {top}, cyl {cyl}, edges joined={lines_ok}")
    top_name = top[0]["name"] if top else "Face1"
    ok, reply = await call("get_topology", object_name=pocket, kind="edges", on_face=top_name, doc_name=D)
    kinds = sorted(e["type"] for e in reply["edges"]) if ok else None
    judge(G, "get_topology on_face", ok, reply, kinds is not None and kinds.count("Line") == 4 and kinds.count("Circle") == 1, f"edges of {top_name}: {kinds}", f"{kinds}")
    ok, reply = await call("get_topology", object_name=pocket, kind="edges", near=[0, 0, 3], limit=3, doc_name=D)
    dist = [e["distance"] for e in reply["edges"]] if ok else None
    judge(G, "get_topology near", ok, reply, dist is not None and len(dist) == 3 and dist == sorted(dist) and dist[0] < 10.01,
          f"3 nearest edges at {dist}", f"{dist}")

    # the workflow topology -> fillet: round the four vertical edges
    ok, reply = await call("get_topology", object_name=pocket, kind="edges", element_type="Line", doc_name=D)
    vertical = [e["name"] for e in reply["edges"]
                if e.get("direction") and abs(abs(e["direction"][2]) - 1) < 1e-9 and len(e["faces"]) == 2] if ok else []
    v0 = solid(D, pocket)["vol"]
    ok, reply = await call("fillet_edges", object_name=pocket, radius=2, edges=vertical, doc_name=D)
    v1 = solid(D, reply["name"])["vol"] if ok else None
    expected = v0 - 4 * (4 - PI) * 3
    judge(G, "fillet the edges found by topology", ok, reply, len(vertical) == 4 and v1 is not None and abs(v1 - expected) < 0.01,
          f"{vertical}: {v0} -> {v1} (expected {round(expected, 2)})", f"vertical={vertical}, volume {v1}")
    q(f"App.getDocument({D!r}).undo(); App.getDocument({D!r}).recompute(); R = True")

    bottom = [f["name"] for f in faces.values() if f["type"] == "Plane" and f.get("normal") == [0.0, 0.0, -1.0]]
    side = [f["name"] for f in faces.values() if f["type"] == "Plane" and abs(f.get("normal", [0, 0, 1])[2]) < 1e-9]
    ok, reply = await call("measure_distance", element_a=f"{pocket}:{top_name}", element_b=f"{pocket}:{bottom[0]}", doc_name=D)
    judge(G, "measure_distance face-face", ok, reply, ok and abs(reply["distance"] - 3) < 1e-6, f"{reply.get('distance') if ok else None} mm", f"{reply}")
    ok, reply = await call("measure_distance", element_a=[20, 10, 10], element_b=f"{pocket}:{top_name}", doc_name=D)
    rim = math.sqrt(7 ** 2 + 3 ** 2)  # the hole is under the point: the nearest point is on its rim
    judge(G, "measure_distance point-face", ok, reply, ok and abs(reply["distance"] - rim) < 1e-6,
          f"{reply.get('distance') if ok else None} mm to {reply.get('point_b') if ok else None} (expected {round(rim, 6)}, on the rim)", f"{reply}")
    ok, reply = await call("measure_angle", element_a=f"{pocket}:{top_name}", element_b=f"{pocket}:{side[0]}", doc_name=D)
    judge(G, "measure_angle face-face", ok, reply, ok and abs(reply["angle"] - 90) < 1e-6, f"{reply.get('angle') if ok else None} deg", f"{reply}")
    ok, reply = await call("measure_angle", element_a=f"{pocket}:{vertical[0]}", element_b=f"{pocket}:{top_name}", doc_name=D)
    judge(G, "measure_angle edge-plane", ok, reply, ok and abs(reply["angle"] - 90) < 1e-6, f"{reply.get('angle') if ok else None} deg", f"{reply}")

    ok, reply = await call("mass_properties", object_name=b, density=7850, doc_name=D)
    izz = (400000 - HOLE * 4.5) * 7850e-9  # box (a2+b2)/12 minus the hole r2/2, unit density x rho
    judge(G, "mass_properties density", ok, reply, ok and abs(reply["mass"] - PLATE * 7850e-9) < 1e-9 and reply["center_of_mass"] == [20.0, 10.0, 1.5]
          and abs(max(reply["principal_moments"]) - izz) < 1e-6,
          f"{reply.get('mass') if ok else None} kg at {reply.get('center_of_mass') if ok else None}, Izz {max(reply['principal_moments']) if ok else None} (expected {round(izz, 6)})", f"{reply}")
    ok, reply = await call("mass_properties", object_name=b, material="Aluminum-6061-T6", doc_name=D)
    judge(G, "mass_properties material card", ok, reply, ok and abs(reply["density"] - 2700) < 1e-6 and abs(reply["mass"] - PLATE * 2.7e-6) < 1e-9,
          f"{reply.get('density') if ok else None} kg/m3 from {reply.get('density_source') if ok else None}", f"{reply}")
    ok, reply = await call("mass_properties", object_name=b, doc_name=D)
    judge(G, "mass_properties without density", ok, reply, ok and reply["mass"] is None and reply["note"], "mass None with a note", f"{reply}")
    ok, reply = await call("mass_properties", object_name=b, material="Unobtainium", doc_name=D)
    judge(G, "mass_properties unknown material", not ok, reply, "No material card" in str(reply), "refused", str(reply))
    ok, reply = await call("validate_object", object_name=b, doc_name=D)
    judge(G, "validate_object", ok, reply, ok and reply.get("valid") is True, "valid", f"{reply}")
    ok, reply = await call("validate_document", doc_name=D)
    judge(G, "validate_document", ok, reply, ok and reply.get("valid") is True, "valid", f"{str(reply)[:150]}")
    close(D)

    D = "BenchClash"
    close(D)
    q(f"""_d = App.newDocument({D!r})
for _n, _x, _y in (("A", 0, 0), ("B", 5, 0), ("C", 0, 10), ("Far", 100, 0)):
    _o = _d.addObject("Part::Box", _n); _o.Placement.Base = App.Vector(_x, _y, 0)
_d.recompute(); R = True""")
    ok, reply = await call("check_interference", doc_name=D)
    overlaps = sorted((tuple(o["objects"]), round(o["shared_volume"], 3)) for o in reply["overlaps"]) if ok else None
    touching = sorted(tuple(t) for t in reply["touching"]) if ok else None
    judge(G, "check_interference", ok, reply, overlaps == [(("A", "B"), 500.0)] and touching == [("A", "C"), ("B", "C")],
          f"overlaps {overlaps}, touching {touching}", f"overlaps {overlaps}, touching {touching}")
    close(D)


# --------------------------------------------------------------------------- 7. sheet metal

async def bench_sheetmetal() -> None:
    G, D = "sheet-metal", "BenchSheetMetal"
    close(D)
    # an L bracket: 2 mm thick, 2 mm inner bend radius, legs 30 and 20 mm, 25 mm wide
    q(f"""import Part
_d = App.newDocument({D!r})
_t, _r, _w = 2.0, 2.0, 25.0
_ro = _r + _t
_h = Part.makeBox(30 - _ro, _w, _t, App.Vector(_ro, 0, 0))
_v = Part.makeBox(_t, _w, 20 - _ro, App.Vector(0, 0, _ro))
_ring = Part.makeCylinder(_ro, _w, App.Vector(_ro, 0, _ro), App.Vector(0, 1, 0)).cut(Part.makeCylinder(_r, _w, App.Vector(_ro, 0, _ro), App.Vector(0, 1, 0)))
_o = _d.addObject("Part::Feature", "Bracket"); _o.Shape = _h.fuse([_v, _ring.common(Part.makeBox(_ro, _w, _ro))]).removeSplitter()
_d.recompute(); R = True""")
    ansi = 26 + 16 + PI / 2 * (2 + 0.4 * 2)  # straight legs + bend allowance on the neutral fibre
    ok, reply = await call("sheetmetal_unfold", object_name="Bracket", doc_name=D)
    size = sorted(reply["flat_size"]) if ok else None
    judge(G, "sheetmetal_unfold ansi", ok, reply, ok and abs(size[1] - ansi) < 1e-3 and abs(size[0] - 25) < 1e-6 and reply["bends"] == 1 and abs(reply["thickness"] - 2) < 1e-6,
          f"flat {size}, expected {round(ansi, 3)} x 25", f"{reply}")
    unfold = reply if ok else None
    if unfold:
        # one sketch per layer, and no text (lines and arcs) among the cut lines
        layers = unfold["sketches"]
        kinds = q(f"_d = App.getDocument({D!r}); R = {{k: sorted(set(type(g).__name__ for g in _d.getObject(n).Geometry)) for k, n in {layers!r}.items()}}")
        judge(G, "sheetmetal_unfold sketch layers", True, unfold,
              {"outline", "bend_lines"} <= set(layers) and kinds["outline"] == ["LineSegment"] and kinds["bend_lines"] == ["LineSegment"],
              f"layers {kinds}", f"layers {kinds}")
    din = 26 + 16 + PI / 2 * (2 + 0.4 * 2 / 2)
    ok, reply = await call("sheetmetal_unfold", object_name="Bracket", k_factor_standard="din", generate_sketches=False, doc_name=D)
    size = sorted(reply["flat_size"]) if ok else None
    judge(G, "sheetmetal_unfold din", ok, reply, ok and abs(size[1] - din) < 1e-3, f"flat {size}, expected {round(din, 3)} x 25", f"{reply}")
    curved = q(f"_s = App.getDocument({D!r}).getObject('Bracket').Shape; R = 'Face' + str([i for i, f in enumerate(_s.Faces) if type(f.Surface).__name__ == 'Cylinder'][0] + 1)")
    ok, reply = await call("sheetmetal_unfold", object_name="Bracket", face=curved, doc_name=D)
    judge(G, "unfold from a curved face refused", not ok, reply, "not a planar face" in str(reply), "refused", str(reply))
    if unfold:
        real = os.path.join(SHARED_DIR, "bench_flat.dxf")
        ok, reply = await call("export_dxf", file_path=SHARED_TILDE + "/bench_flat.dxf", face=unfold["flat_face"], doc_name=D)
        flat = reply.get("flat") if ok else None
        judge(G, "export_dxf flat pattern", ok, reply, ok and reply["entities"] == {"LINE": 4} and abs(max(flat["width"], flat["height"]) - ansi) < 1e-3,
              f"entities {reply.get('entities') if ok else None}, flat {flat}", f"{reply}")
        ok, reply = await call("export_dxf", file_path=SHARED_TILDE + "/bench_bends.dxf", object_names=[unfold["sketches"]["bend_lines"]], doc_name=D)
        ent = reply["entities"] if ok else {}
        judge(G, "export_dxf bend lines alone", ok, reply, ok and set(ent) == {"LINE"} and ent["LINE"] >= 1,
              f"entities {ent} (bend lines only, no text)", f"{reply}")
        for name in ("bench_flat.dxf", "bench_bends.dxf"):
            if os.path.exists(os.path.join(SHARED_DIR, name)):
                os.remove(os.path.join(SHARED_DIR, name))
    close(D)


REPORT_VIEW = """
from PySide import QtGui
_views = [w for w in Gui.getMainWindow().findChildren(QtGui.QTextEdit) if w.objectName() == "Report view"]
R = _views[0].toPlainText() if _views else None
"""


async def main() -> int:
    report_before = q(REPORT_VIEW)
    for group in (bench_geometry, bench_constraints, bench_features, bench_spreadsheet, bench_files, bench_inspection, bench_sheetmetal):
        try:
            await group()
        except Exception as e:  # noqa: BLE001
            note(group.__name__, "(bench)", "BENCH_ERROR", repr(e)[:200])
    # The tools must not litter FreeCAD's Report view: SyntaxWarnings from the
    # scripts or deprecated properties show up there on every call.
    report_after = q(REPORT_VIEW)
    if report_before is not None and report_after is not None:
        added = report_after[len(report_before):] if report_after.startswith(report_before[:200]) else report_after
        # The bench's deliberate refusals must each show as one "MCP tool
        # refused" line, never as a traceback with the whole script.
        noise = ("SyntaxWarning", "eprecated", "Traceback", "--- code ---")
        noisy = [line for line in added.splitlines() if any(n in line for n in noise)]
        refusals = sum("MCP tool refused:" in line for line in added.splitlines())
        note("report", "no warning in FreeCAD's Report view", "OK" if not noisy and refusals else "FALSE_SUCCESS",
             f"{len(added.splitlines())} lines added: {refusals} one-line refusals, no SyntaxWarning, deprecation, traceback or script"
             if not noisy and refusals else f"{len(noisy)} noisy lines (e.g. {noisy[0][:120] if noisy else '-'}), {refusals} refusals")
    q("R = [App.closeDocument(_d) for _d in list(App.listDocuments()) if _d.startswith('Bench')]")
    totals = Counter(r[2] for r in RESULTS)
    print(f"\nTOTAL: {dict(totals)} over {len(RESULTS)} cases")
    out = os.environ.get("BENCH_RESULTS")
    if out:
        with open(out, "w", encoding="utf-8") as f:
            json.dump(RESULTS, f, ensure_ascii=False, indent=1)
    return 0 if totals.get("OK", 0) == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
