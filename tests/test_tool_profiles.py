"""Tool profiles: "@name" in --tools / FREECAD_MCP_TOOLS stands for a list of groups.

Each profile's tool list is frozen here, so adding a tool to a group or a group
to a profile must be done on purpose. Like test_tool_groups, each check runs in
its own interpreter because keep_tool_groups removes tools from the module-level
server.
"""

import json
import subprocess
import sys

CODE = {"execute_code", "execute_code_async", "execute_code_headless"}

PROBE = """
import asyncio, json, sys
from freecad_mcp import server
try:
    kept = server.keep_tool_groups(sys.argv[1])
except ValueError as e:
    print(json.dumps({"error": str(e)}))
    raise SystemExit(0)
after = [t.name for t in asyncio.run(server.mcp.list_tools())]
print(json.dumps({"kept": kept, "after": after, "groups": server.MODELLING_GROUPS, "profiles": list(server.TOOL_PROFILES)}))
"""


def probe(spec: str) -> dict:
    out = subprocess.run([sys.executable, "-c", PROBE, spec], capture_output=True, text=True, check=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


MODELISATION = (
    "add_external_geometry", "add_sketch_arc", "add_sketch_bspline", "add_sketch_circle", "add_sketch_constraint",
    "add_sketch_ellipse", "add_sketch_line", "add_sketch_point", "add_sketch_polygon", "add_sketch_rectangle",
    "add_sketch_slot", "chamfer_edges", "check_interference", "close_document", "constrain_angle",
    "constrain_coincident", "constrain_distance", "constrain_distance_x", "constrain_distance_y",
    "constrain_equal", "constrain_fix", "constrain_horizontal", "constrain_parallel", "constrain_perpendicular",
    "constrain_radius", "constrain_tangent", "constrain_vertical", "create_datum_line", "create_datum_plane",
    "create_datum_point", "create_document", "create_hole", "create_object", "create_partdesign_body",
    "create_sketch", "delete_object", "delete_sketch_constraint", "delete_sketch_geometry", "draft_feature",
    "edit_object", "export_dxf", "fillet_edges", "get_async_status", "get_object", "get_objects",
    "get_parts_list", "get_rpc_status", "get_sketch_info", "get_topology", "get_view", "groove_sketch",
    "insert_part_from_library", "linear_pattern", "list_documents", "loft_sketches", "mass_properties",
    "measure_angle", "measure_distance", "mirrored_feature", "open_document", "pad_sketch", "pocket_sketch",
    "polar_pattern", "recompute_document", "redo", "reload_document", "revolution_sketch", "run_fem_analysis",
    "save_document", "sheetmetal_unfold", "spreadsheet_bind_property", "spreadsheet_clear_cell",
    "spreadsheet_create", "spreadsheet_export_csv", "spreadsheet_get_aliases", "spreadsheet_get_cell",
    "spreadsheet_get_cell_range", "spreadsheet_import_csv", "spreadsheet_set_alias", "spreadsheet_set_cell",
    "subtractive_loft", "subtractive_pipe", "sweep_sketch", "thickness_feature", "toggle_construction", "undo",
    "validate_document", "validate_object",
)

MISE_EN_PLAN = (
    "add_datum_symbol", "add_dimension", "add_drawing_views", "add_gdt_frame", "add_hole_callouts",
    "add_hole_table", "add_parts_list", "add_revision_table", "add_section_view", "add_surface_texture",
    "add_weld_symbol", "apply_line_types", "check_drawing", "check_interference", "close_document",
    "create_document", "create_drawing", "create_drawing_page", "create_object", "delete_object", "edit_object",
    "export_drawing", "export_dxf", "fill_title_block", "get_async_status", "get_object", "get_objects",
    "get_parts_list", "get_rpc_status", "get_topology", "get_view", "insert_part_from_library", "list_documents",
    "mass_properties", "measure_angle", "measure_distance", "open_document", "recompute_document", "redo",
    "refresh_dual_dimensions", "reload_document", "run_fem_analysis", "save_document", "undo",
)

INSPECTION = (
    "check_interference", "close_document", "create_document", "create_object", "delete_object", "edit_object",
    "export_3mf", "export_dxf", "export_iges", "export_obj", "export_step", "export_stl", "get_async_status",
    "get_object", "get_objects", "get_parts_list", "get_rpc_status", "get_topology", "get_view", "import_step",
    "import_stl", "insert_part_from_library", "list_documents", "mass_properties", "measure_angle",
    "measure_distance", "open_document", "recompute_document", "redo", "reload_document", "run_fem_analysis",
    "save_document", "undo", "validate_document", "validate_object",
)

EXPECTED = {"modelisation": MODELISATION, "mise-en-plan": MISE_EN_PLAN, "inspection": INSPECTION}


def test_each_profile_exposes_exactly_its_tools() -> None:
    for name, expected in EXPECTED.items():
        r = probe(f"@{name}")
        assert sorted(r["after"]) == sorted(r["kept"]) == list(expected), name
        assert len(expected) == len(set(expected))


def test_the_profiles_are_the_three_documented_ones() -> None:
    assert probe("@inspection")["profiles"] == ["modelisation", "mise-en-plan", "inspection"]


def test_no_profile_lets_a_model_run_code() -> None:
    for name in EXPECTED:
        assert not CODE & set(probe(f"@{name}")["after"]), name


def test_a_profile_is_case_insensitive_and_code_can_be_added_on_purpose() -> None:
    r = probe("@Inspection, code")
    assert set(r["after"]) == set(INSPECTION) | CODE


def test_a_profile_combines_with_groups_and_minus() -> None:
    r = probe("@modelisation,-spreadsheet")
    spreadsheet = set(r["groups"]["spreadsheet"])
    assert len(spreadsheet) == 10
    assert set(r["after"]) == set(MODELISATION) - spreadsheet
    r = probe("@mise-en-plan,partdesign")
    assert set(r["after"]) == set(MISE_EN_PLAN) | set(r["groups"]["partdesign"])
    r = probe("@modelisation,-@inspection")
    assert set(r["after"]) == set(MODELISATION) - set(INSPECTION)


def test_a_profile_alone_taken_out_of_nothing_is_refused() -> None:
    assert "none given" in probe("-@inspection")["error"]


def test_an_unknown_profile_is_refused_with_the_valid_names() -> None:
    r = probe("@dessin")
    for word in ("@dessin", "@modelisation", "@mise-en-plan", "@inspection", "drawing", "base", "core"):
        assert word in r["error"], word
    assert "@dessin" in probe("base,-@dessin")["error"]


def test_the_command_line_refuses_an_unknown_profile_at_startup() -> None:
    for argv in ("['freecad-mcp', '--tools', '@nope']", "['freecad-mcp']"):
        env = {"FREECAD_MCP_TOOLS": "@nope", "PATH": ""} if argv == "['freecad-mcp']" else None
        out = subprocess.run([sys.executable, "-c", f"import sys; from freecad_mcp.server import main; sys.argv = {argv}; main()"],
                             capture_output=True, text=True, env=env)
        assert out.returncode == 2 and "@nope" in out.stderr and "@mise-en-plan" in out.stderr, argv
