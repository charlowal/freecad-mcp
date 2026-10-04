# Tools

[Back to README](../README.md) · [Configuration](configuration.md) · [Code execution](execution.md)

## Available tools

| Tool | Purpose |
| --- | --- |
| `create_document` | Create a new FreeCAD document. |
| `list_documents` | List open documents. |
| `reload_document` | Close and reopen a saved document to pick up external file changes, such as results from a headless script. |
| `create_object` | Create an object in a document. |
| `edit_object` | Edit an object's properties. |
| `delete_object` | Delete an object from a document. |
| `get_objects` | Get all objects in a document. |
| `get_object` | Get one object in a document. |
| `get_view` | Get a screenshot of the active view. |
| `execute_code` | Execute Python code on FreeCAD's GUI thread. |
| `execute_code_async` | Start a background computation and return its job ID; use `commit()` for document and view access. |
| `get_async_status` | Get background job state and failure tracebacks without using the GUI thread. |
| `execute_code_headless` | Run a script in a separate `freecadcmd` process and return its exit status and output. |
| `get_rpc_status` | Report RPC and GUI-dispatch health, addon version, and version check without using the GUI thread. |
| `insert_part_from_library` | Insert a part from the [FreeCAD parts library](https://github.com/FreeCAD/FreeCAD-library). |
| `get_parts_list` | List parts in the [FreeCAD parts library](https://github.com/FreeCAD/FreeCAD-library). |
| `run_fem_analysis` | Run CalculiX on an existing analysis and return summary results. |

See [code execution](execution.md) for execution modes, shared script state,
background job tracking, and timeout handling.

## Parametric modelling

59 more tools build parametric parts in a PartDesign body. They come from
[spkane/freecad-addon-robust-mcp-server](https://github.com/spkane/freecad-addon-robust-mcp-server)
(MIT, see `src/freecad_mcp/modelisation/LICENSE-spkane`), fixed for FreeCAD 1.1,
and run on this addon's `execute_code`; nothing changes in the addon.

| Group | Tools |
| --- | --- |
| Body and sketch | `create_partdesign_body`, `create_sketch`, `get_sketch_info`, `toggle_construction`, `delete_sketch_geometry`, `add_external_geometry` |
| Sketch geometry | `add_sketch_line`, `add_sketch_arc`, `add_sketch_circle`, `add_sketch_ellipse`, `add_sketch_point`, `add_sketch_polygon`, `add_sketch_slot`, `add_sketch_bspline`, `add_sketch_rectangle` |
| Constraints | `constrain_horizontal`, `constrain_vertical`, `constrain_coincident`, `constrain_parallel`, `constrain_perpendicular`, `constrain_tangent`, `constrain_equal`, `constrain_distance`, `constrain_distance_x`, `constrain_distance_y`, `constrain_radius`, `constrain_angle` (degrees), `constrain_fix`, `add_sketch_constraint`, `delete_sketch_constraint` |
| Features | `pad_sketch`, `pocket_sketch`, `revolution_sketch`, `groove_sketch`, `create_hole`, `fillet_edges`, `chamfer_edges`, `draft_feature`, `thickness_feature`, `linear_pattern`, `polar_pattern`, `mirrored_feature`, `loft_sketches`, `sweep_sketch`, `subtractive_loft`, `subtractive_pipe` |
| References | `create_datum_plane`, `create_datum_line`, `create_datum_point` |
| Spreadsheet | `spreadsheet_create`, `spreadsheet_set_cell`, `spreadsheet_get_cell`, `spreadsheet_set_alias`, `spreadsheet_get_aliases`, `spreadsheet_clear_cell`, `spreadsheet_get_cell_range`, `spreadsheet_bind_property`, `spreadsheet_import_csv`, `spreadsheet_export_csv` |

`create_sketch` attaches to `XY_Plane`, `XZ_Plane` or `YZ_Plane`, to a feature
face given as `"Pad:Face6"`, or to a datum plane, with an optional `offset`
along the sketch normal.

Every feature tool checks the solid before committing: an additive feature
must add material, a subtractive one remove it, and a dress-up or pattern
change it, with a valid result. Otherwise the feature is undone, the body is
left as it was, and the error says what to change; a pocket that cuts into
empty space, for instance, asks for `reversed=True`. A successful reply carries
`verification` with `volume_before` and `volume_after`.

## Drawings (TechDraw)

16 tools draft mechanical drawings on FreeCAD's ASME templates, in dual units
mm [in], third-angle projection and AWS weld symbols by default; each choice
is a parameter.

| Group | Tools |
| --- | --- |
| Sheet | `create_drawing_page`, `fill_title_block`, `add_revision_table`, `export_drawing` (PDF or SVG, plus a PNG) |
| Views | `add_drawing_views` (largest standard scale that fits, layout checked on the sheet), `add_section_view` |
| Dimensions | `add_dimension`, `refresh_dual_dimensions`, `add_hole_callouts`, `add_hole_table` |
| Annotations | `add_gdt_frame`, `add_datum_symbol`, `add_weld_symbol`, `add_parts_list` (with balloons) |
| Whole sheet | `create_drawing` (sheet, views, overall dimensions, hole callouts, parts list, then the check), `check_drawing` |

Dimensions are given by model points, `points=[[0, 0, 0], [120, 0, 0]]`, or by
a circle's centre; the tool finds the vertex or circle they project to in the
view and refuses a point that is not drawn there. TechDraw has no second unit,
so the text is written from TechDraw's own measurement, which must equal the
geometry's; `refresh_dual_dimensions` rewrites the texts after a model change.

`add_hole_callouts` finds holes in the solids themselves (drill, THRU or ↧
depth, ⌴ counterbore, ⌵ countersink; identical holes share "2X"), so imported
STEP parts work too. Threads are not recognised. `add_gdt_frame` refuses
frames that cannot be right, such as a datum on a form tolerance, an
orientation tolerance without datum, or a modifier where none applies.

`check_drawing` answers PASS, FAIL, NON_VERIFIE or NON_APPLICABLE per check,
with the evidence: views drawn, inside the frame, off the title block and
apart; dimensions and notes inside the frame; projection; scale field; title
block without template sample text; every dimension tied to geometry, in dual
units, its text recomputed from the geometry; decimal point; every hole called
out; GD&T datums shown; parts list against balloons. What only a person can
judge (complete dimensioning, conformity to a standard, whether a joint is
welded) is NON_VERIFIE, never PASS. Nothing is approved: unknown title-block
fields read "À RENSEIGNER", "Checked by" reads "À VÉRIFIER" and the approval
fields stay empty.

`export_drawing` needs the GUI: TechDraw renders a page only once it is shown,
so the tool opens it, exports, and brings the 3D view back to front, since
`get_view` fails while a sheet is in front.

## Files, documents and inspection

| Group | Tools |
| --- | --- |
| Documents | `open_document`, `save_document`, `close_document`, `recompute_document`, `undo`, `redo` |
| Export and import | `export_step`, `export_iges`, `export_stl`, `export_3mf`, `export_obj`, `export_dxf`, `import_step`, `import_stl` |
| Inspection | `get_topology`, `measure_distance`, `measure_angle`, `mass_properties`, `check_interference`, `validate_object`, `validate_document` |

The export tools (from spkane, see above) take the finished solids by default:
PartDesign bodies and standalone solids, with their global placement, never
the features inside a body, origin planes or sketches. Each export reads its
file back and reports what it holds; a STEP whose solids differ from the
source is an error. `export_dxf` lays a planar face flat in XY (outline and
holes) for laser or waterjet cutting, or writes 2D objects such as sketches.

A path may start with `~`. FreeCAD installed as a snap has its own HOME and a
private `/tmp`; `~` still means the user's home there, and a path under
`/tmp` is refused since the file would be out of reach.

`close_document` refuses to drop unsaved changes unless `save` or
`discard_changes` says what to do with them.

`get_topology` lists faces (type, area, centre, normal or axis and radius)
and edges (type, length, end points, radius, the two faces they join), with
filters by type, by bounding face and by distance to a point, so the names
passed to `fillet_edges`, `create_sketch(plane="Pad:Face6")` and the like
come from the geometry instead of guesses. `mass_properties` takes its
density from `density` (kg/m3), a FreeCAD material card such as
`Steel-Generic` or `Aluminum-6061-T6`, or the material assigned to the
object, and gives inertia about the centre of mass in kg.mm2.

`tests/integration/bench_modelling.py` runs every tool against a live FreeCAD
and judges each by a measurement read from FreeCAD, not by the tool's reply.

## Screenshot options

The following tools return optional screenshots: `create_object`, `edit_object`,
`delete_object`, `execute_code`, `insert_part_from_library`, `get_objects`,
`get_object`, and `run_fem_analysis`.

| Parameter | Default | Purpose |
| --- | --- | --- |
| `include_screenshot` | `true` | Set to `false` for text-only feedback, such as analytical scripts or intermediate steps. |
| `view_name` | `"Isometric"` | Orient the returned screenshot, for example `"Front"`, `"Top"`, or `"Right"`. |

The [`--only-text-feedback` flag](configuration.md#text-feedback-and-screenshots)
suppresses these optional screenshots regardless of `include_screenshot`.

Use `get_view` to request a screenshot explicitly; it is available even with
`--only-text-feedback`. It takes `view_name` and optional `width`, `height`, and
`focus_object` parameters. Supported views are `Isometric`, `Front`, `Top`,
`Right`, `Back`, `Left`, `Bottom`, `Dimetric`, and `Trimetric`.

## FEM analysis

`run_fem_analysis` runs the CalculiX solver on an existing `Fem::FemAnalysis`
container. It auto-creates a `SolverCcxTools` if the analysis has none and returns
max von Mises stress, max/min displacement, node count, and the solver's working
directory. The default `timeout` is 600 seconds.

The reply also lists the loads as the solver saw them (`applied_loads`: forces
in N with their faces and direction, pressures in MPa, fixed faces). Check them:
a plain number in `ConstraintForce.Force` is read as millinewtons, so
`Force = 1000` applies 1 N and the results come out 1000 times too low without
any error (#158); set it with its unit, e.g. `"1000 N"`. The force follows the
loaded face's normal unless `Direction` links an edge or face.

See [`examples/cantilever_fem.py`](../examples/cantilever_fem.py) for an end-to-end
example, including geometry, material, mesh, constraints, and an analytical
comparison. For long analyses, configure the client to allow the
[queue and execution timeout budgets](execution.md#gui-dispatch-timeouts).
