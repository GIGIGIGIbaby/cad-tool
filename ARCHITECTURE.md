# CAD Tools V3 — implementation contract

Fresh project; no `.456d` or build123d compatibility. Standard-library-only host Python >=3.11; FreeCAD runs in its own installed interpreter/process. Do not modify files outside cad-v3 during development, except isolated test tempdirs. Do not install plugins, change user preferences, or attach to a running GUI.

## Product scope

An ordinary project folder, a small JSON CLI (`cad3`), native FreeCAD Python scripts, model observation, and Git-backed snapshots. Do not replicate CAD operations as hundreds of commands, invent a CAD DSL, or build a GUI. Skills provide a short entry point and on-demand references.

## Project layout

```
cad3.json                 # schema cad3.project/v1; name; document=models/main.FCStd; entrypoint=src/main.py
src/                     # native FreeCAD scripts/modules
inputs/                  # explicit input dependencies
models/                  # native FCStd and optional STEP output
artifacts/latest/        # latest successful execution evidence, state and logs
notes/                   # intent/design notes
.cad3/                   # ignored: run dirs, isolated profiles, locks
.git/                    # real Git, not a new custom version-control engine
```

Project init only accepts an absent or empty target directory; never silently adopts a parent Git repository. Input script is a trusted local program, not sandboxed code. Process/profile isolation is NOT a malicious-code sandbox.

## Shared Python contracts (must remain compatible)

`cad3.project` (storage owner):
- `class Cad3Error(Exception)` common user-facing error.
- `class Project`: `.root: Path`, `.config: dict`, `.document_path: Path`, `.entrypoint_path: Path`, `.internal_dir: Path`; `Project.load(path: Path) -> Project`; `Project.init(path: Path, name: str|None=None) -> Project`; `.resolve(relative: str) -> Path` refuses paths escaping root including symlink parents.
- `project_lock(project)` context manager: serialize V3 mutations, avoid stale locks on ordinary termination; do not claim it locks manual GUI edits.

`cad3.versions` (storage owner; API functions receive Project):
- `status(project) -> dict`: HEAD, branch/detached, changes.
- `commit(project, message: str) -> dict`: snapshot actual files, NEVER rerun modeling. Refuse no changes with a clear outcome; return commit hash. Stage intended project files only. No pushes/global Git config.
- `log(project, limit: int=10) -> dict`: history (empty when no commits).
- `diff(project, revision: str|None=None) -> dict`: Git text/binary diff of actual files; do not claim binary diff is semantic geometry diff.
- `restore(project, revision: str) -> dict`: refuse dirty tracked/staged or untracked project content; restore committed files from verified revision WITHOUT moving HEAD. Next commit records rollback. No force flag needed: users can commit WIP first. Restore must remove tracked files absent from target and not delete unrelated/ignored files. Test untracked collisions and inherited Git repository rejection.
- Git identity: if none configured, use command-scoped local CAD3 identity; never modify user's global identity. Disable hooks for tool-generated commits/restores.

`cad3.runtime` (runtime owner):
- `discover_freecad(explicit: str|None=None) -> Path`: explicit -> CAD3_FREECAD -> common app/platform paths/PATH; never import FreeCAD on host or install it.
- `doctor(executable: str|None=None, timeout: float=15) -> dict`: safe isolated capability/version probe.
- `execute(project, script: Path|None=None, *, executable=None, timeout=120.0, export_step=False) -> dict`
- `inspect(project, *, executable=None, timeout=60.0, object_name=None, detail=False) -> dict`
- `render(project, *, executable=None, timeout=90.0, views=('iso','top','front','right'), width=960, height=720) -> dict`
- All functions return JSON-ready dict with `ok` flag, run id/path, errors/warnings, and action results. CLI exits nonzero when ok=false.
- Runtime handles its own project_lock for mutating execute/render; CLI must not nest this lock.
- Separate process invokes packaged `worker.py` with a JSON request file. Per-run FREECAD_USER_HOME + --user-cfg/--system-cfg, HOME, PYTHONIOENCODING; remove host PYTHONHOME/PYTHONPATH/VIRTUAL_ENV. Verify actual config paths are inside run dir before executing.
- Capture stdout/stderr to files. Explicit per-run completion record is mandatory: zero process exit alone is NOT success; missing/mismatched record, script exception/SystemExit, signal or timeout means failure. Catch BaseException and traceback. Host kill only its own process group on timeout. A genuine C++ crash cannot be caught in Python; report it.
- Script namespace: `App`, `doc`, `PROJECT_DIR` (Path), `RUN_DIR` (Path), `__file__`, `__name__='__main__'`; script may import native FreeCAD modules and package-local modules; prefer modifying provided doc (or assign another live native doc back to `doc`). Optional `cad_outputs` list of object names/objects selects exports/render outputs.
- Default doc: open current project's configured FCStd if present, otherwise create Model. Do not recreate from scratch on every run. Script chooses what to change.
- After execution: recompute, inspect state, save candidate FCStd, optionally export STEP, write observation and run evidence. Publish current model/evidence only after protocol success, verify input FCStd hash did not change externally. Errors must not overwrite prior model. Preserve dependency lookup relative to project; document limits of external links.
- Computation warnings/invalid native state must be visible; `ok` means execution/protocol success, NOT design/manufacturing correctness. Do not silently accept native Invalid/Error objects: report them prominently; do not invent universal solid-count, hole-count or volume gates. Assertions are authored per task.
- Commit latest relevant report + stdout/stderr + state under artifacts/latest so saved evidence is not only in ignored .cad3. Failed run logs stay in .cad3 and are returned.

`cad3.observe` (observation owner, imported INSIDE FreeCAD only; host import must not require FreeCAD):
- `select_output_objects(doc, names=None) -> list`: explicit object names/objects if supplied; otherwise Body tips/independent result solids without double counting hidden/intermediate inputs. Do not include infinite datum planes, sketches, origins as volume-bearing output. Return [] for empty doc.
- `inspect_document(doc, *, object_name=None, detail=False) -> dict`: JSON-safe schema, document, outputs, objects, containment/dependencies, parameters and expressions, sketch DoF/solve diagnostics, native errors/warnings and meaningful geometry metrics. Compact defaults; detail on demand; units and truncation explicit. Never serialize opaque C++ objects/Measure registry, unbounded shape/Proxy data, or NaN/Infinity. Do not call solve on arbitrary sketches just for inspection: read diagnostics after recompute and report availability.
- `code_view(state: dict) -> str`: compact READ-ONLY descriptive view, explicitly not executable rebuild code.

`cad3.rendering` (observation owner, FreeCAD worker context):
- `render_sections(objects, output_dir, sections, width, height) -> list[dict]`: axis-aligned half-space sections (`'y=12.5'`), camera facing the cut, pairwise overlaps of cut objects drawn red and listed; observation only, not an interference gate.
- `render_objects(objects, output_dir: Path, *, views=('iso','top','front','right'), width=960, height=720) -> list[dict]`: VTK imported lazily from bundled runtime, FreeCAD tessellation -> shaded PNG, metadata per view (camera, dimensions, source), explicitly NOT native GUI screenshot. Stable valid view names; detect no geometry/missing VTK as actionable error. Keep target paths inside supplied output dir.

## CLI (parent owns cli.py and package scaffold)

`cad3 doctor [--freecad PATH]`
`cad3 init PATH [--name NAME]`
`cad3 run [SCRIPT] [-p PROJECT] [--freecad PATH] [--timeout SECONDS] [--step]`
`cad3 inspect [-p PROJECT] [--object NAME] [--detail] [--format json|code] [--freecad PATH]`
`cad3 render [-p PROJECT] [--views iso,top,front,right] [--section AXIS=MM ...] [--width N] [--height N] [--freecad PATH]` (with only `--section`, the default views are skipped)
`cad3 status|log|diff|commit|restore [-p PROJECT]` (`commit -m MESSAGE`; `restore REV`; `diff [REV]`; `log --limit N`)
Project defaults to current directory (or nearest parent containing cad3.json; never legacy manifests). JSON output for all commands except explicit `--format code`. argparse errors still exit nonzero. `python cad3.py ...` works from a fresh checkout without pip installation. `python -m cad3` and installed `cad3` also supported.

## Definition of done

Unit tests: project/path safety/Git restore/dirty refusal and runtime failure protocol. Real installed FreeCAD integration: doctor; native Sketcher->Pad->Pocket; run->inspect->render->commit; parameter edit persists on reopen; second commit; diff; restore previous model/parameters; Python exception and timeout preserve prior model; missing completion detected. Rendered images actually inspected. User preferences content hashes unchanged; V2 Git diff unchanged. Examples use native FreeCAD, not build123d Checkpoint. Evidence and limits documented; no claim of arbitrary CAD semantic merge or automatic import of old projects.
