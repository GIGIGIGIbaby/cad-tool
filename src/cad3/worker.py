"""In-FreeCAD half of the cad3 run protocol.

This module is executed *inside* the isolated FreeCADCmd process by
``cad3.runtime`` and must never be imported by host-side code (it imports
FreeCAD).  It reads one JSON request file, performs exactly one action
(``doctor``/``execute``/``inspect``/``render``) and writes one explicit
completion record.

The completion record is the protocol's success token.  A zero process exit
code alone is never success: the record must exist, be readable, carry the
same ``run_id``/``action``/script hash the host requested, and report
``status == "completed"``.  Anything else (missing record, script exception,
``SystemExit``, timeout, signal, C++ crash) is reported as failure by the host.

Run manually for debugging::

    CAD3_RUN_REQUEST=/path/request.json freecadcmd <this file>
    freecadcmd <this file> /path/request.json
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

PROTOCOL = "cad3.run/v1"
COMPLETION_PROTOCOL = "cad3.completion/v1"
ACTIONS = ("doctor", "execute", "inspect", "render")

#: Printed on stdout so the captured run log carries the outcome as evidence.
RESULT_MARKER = "CAD3_WORKER_RESULT"

_MAX_DEPTH = 6
_MAX_ITEMS = 200
_MAX_STR = 4000


class ProtocolError(Exception):
    """The cad3 protocol (not the user script) could not be completed."""

    def __init__(self, message: str, kind: str = "protocol") -> None:
        super().__init__(message)
        self.kind = kind


class ScriptFailure(Exception):
    """The user script raised / exited; the record is filled by the handler."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------
def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _round(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return round(number, 6)


def _sha256_file(path: Path):
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _safe_json(value, depth: int = 0):
    """Convert arbitrary native objects into JSON-safe primitives."""
    if depth > _MAX_DEPTH:
        return "<truncated: depth>"
    if value is None or isinstance(value, (bool, int, str)):
        if isinstance(value, str) and len(value) > _MAX_STR:
            return value[:_MAX_STR] + "<truncated>"
        return value
    if isinstance(value, float):
        return None if value != value or value in (float("inf"), float("-inf")) else value
    if isinstance(value, dict):
        return {str(key): _safe_json(item, depth + 1) for key, item in list(value.items())[:_MAX_ITEMS]}
    if isinstance(value, (list, tuple, set)):
        return [_safe_json(item, depth + 1) for item in list(value)[:_MAX_ITEMS]]
    if isinstance(value, Path):
        return str(value)
    try:
        text = value.UserString  # FreeCAD Quantity
        if isinstance(text, str):
            return text
    except Exception:
        pass
    try:
        return repr(value)[:200]
    except Exception:
        return "<unrepresentable>"


def _dump_json(path: Path, payload) -> None:
    """Atomic JSON write (temp file + os.replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2, default=lambda obj: repr(obj)[:200])
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _real(path) -> Path:
    return Path(os.path.realpath(str(path)))


def _inside(base: Path, candidate: Path) -> bool:
    try:
        _real(candidate).relative_to(_real(base))
        return True
    except ValueError:
        return False


def _ensure_package_import_path() -> list:
    """Put the cad3 package's parent directory on sys.path.

    The host deliberately strips PYTHONPATH, so the sibling modules
    (``cad3.observe``/``cad3.rendering``) are only reachable if the worker adds
    its own package location.  Returns the paths it added.
    """
    added = []
    package_parent = Path(__file__).resolve().parent.parent
    if package_parent.is_dir() and str(package_parent) not in sys.path:
        sys.path.insert(0, str(package_parent))
        added.append(str(package_parent))
    return added


def _load_module(name: str, warnings=None):
    """Import ``name``; report (never hide) why it is unavailable."""
    try:
        return __import__(name, fromlist=["*"])
    except Exception as exc:
        message = f"{name} is not importable inside this FreeCAD runtime: {type(exc).__name__}: {exc}"
        if warnings is not None:
            warnings.append(message)
        return None


def _load_optional(module_name: str):
    """Import cad3.<name> when it exists; return None otherwise."""
    return _load_module(module_name)


# --------------------------------------------------------------------------
# isolation verification
# --------------------------------------------------------------------------
def _check_isolation(App, run_dir: Path) -> dict:
    """Verify the *actual* FreeCAD config paths live inside the run dir."""
    keys = ("UserAppData", "UserParameter", "SystemParameter")
    config: dict = {}
    problems: list = []
    unverified: list = []
    for key in keys:
        try:
            value = App.ConfigGet(key)
        except Exception as exc:  # pragma: no cover - depends on FreeCAD build
            config[key] = None
            unverified.append(f"ConfigGet({key}) failed: {type(exc).__name__}: {exc}")
            continue
        config[key] = str(value) if value else None
    try:
        user_app_data = App.getUserAppDataDir()
    except Exception as exc:  # pragma: no cover
        user_app_data = None
        unverified.append(f"getUserAppDataDir() failed: {type(exc).__name__}: {exc}")
    config["getUserAppDataDir"] = str(user_app_data) if user_app_data else None

    for key, value in config.items():
        if not value:
            continue
        if not _inside(run_dir, Path(value)):
            problems.append(f"{key}={value} is outside the run directory {run_dir}")

    for key in ("FREECAD_USER_HOME", "HOME"):
        value = os.environ.get(key)
        if value and not _inside(run_dir, Path(value)):
            problems.append(f"{key}={value} is outside the run directory {run_dir}")
    if not any(config.get(key) for key in keys) and not config.get("getUserAppDataDir"):
        unverified.append("FreeCAD reported no config paths; isolation could not be verified")

    return {"config": config, "problems": problems, "unverified": unverified}


# --------------------------------------------------------------------------
# document / object helpers
# --------------------------------------------------------------------------
def _is_document(candidate) -> bool:
    return candidate is not None and hasattr(candidate, "saveAs") and hasattr(candidate, "Objects")


def _open_or_create(App, document: Path, record: dict):
    existed = document.exists()
    if existed:
        try:
            doc = App.openDocument(str(document))
        except Exception as exc:
            raise ProtocolError(
                f"cannot open document {document}: {type(exc).__name__}: {exc}", kind="document_open"
            ) from exc
        if doc is None:
            raise ProtocolError(f"cannot open document {document}", kind="document_open")
    else:
        doc = App.newDocument("Model")
    record["document"] = {
        "path": str(document),
        "name": getattr(doc, "Name", None),
        "label": getattr(doc, "Label", None),
        "existed": existed,
    }
    return doc


def _recompute(doc) -> dict:
    info = {"ok": True, "message": None}
    try:
        doc.recompute()
    except Exception as exc:
        info["ok"] = False
        info["message"] = f"{type(exc).__name__}: {exc}"
    if getattr(doc, "RecomputesFrozen", False):
        info["frozen"] = True
    return info


def _object_state(obj):
    state = getattr(obj, "State", None)
    if state is None:
        return []
    try:
        return [str(item) for item in state]
    except TypeError:
        return [str(state)]


def _shape_metrics(shape):
    metrics: dict = {}
    try:
        if shape is None:
            return {"available": False}
        if shape.isNull():
            return {"available": True, "null": True}
        metrics["available"] = True
        metrics["valid"] = bool(shape.isValid())
        metrics["volume"] = _round(shape.Volume)
        metrics["area"] = _round(shape.Area)
        try:
            metrics["solids"] = len(shape.Solids)
            metrics["faces"] = len(shape.Faces)
        except Exception:
            pass
        bound = shape.BoundBox
        metrics["bbox"] = {
            "min": [_round(bound.XMin), _round(bound.YMin), _round(bound.ZMin)],
            "max": [_round(bound.XMax), _round(bound.YMax), _round(bound.ZMax)],
            "size": [_round(bound.XLength), _round(bound.YLength), _round(bound.ZLength)],
        }
    except Exception as exc:
        metrics["error"] = f"{type(exc).__name__}: {exc}"
    return metrics


def _fallback_outputs(doc, names=None):
    """Minimal stand-in used only when cad3.observe is unavailable."""
    if names:
        selected = []
        for item in names:
            if isinstance(item, str):
                obj = doc.getObject(item)
                if obj is None:
                    raise ProtocolError(f"cad_outputs: no object named {item!r}", kind="cad_outputs")
                selected.append(obj)
            else:
                selected.append(item)
        return selected
    chosen = []
    seen = set()
    for obj in list(getattr(doc, "Objects", [])):
        name = getattr(obj, "Name", None)
        if name in seen:
            continue
        shape = getattr(obj, "Shape", None)
        if shape is None:
            continue
        try:
            if shape.isNull() or not len(shape.Solids):
                continue
        except Exception:
            continue
        if getattr(obj, "TypeId", "") == "PartDesign::Body":
            tip = getattr(obj, "Tip", None)
            obj = tip if tip is not None and getattr(tip, "Shape", None) is not None else obj
            name = getattr(obj, "Name", name)
        chosen.append(obj)
        seen.add(name)
    return chosen


def _object_entry(obj, *, detail=False) -> dict:
    entry = {
        "name": getattr(obj, "Name", None),
        "label": getattr(obj, "Label", None),
        "type": getattr(obj, "TypeId", None),
        "state": _object_state(obj),
        "shape": _shape_metrics(getattr(obj, "Shape", None)),
    }
    if detail:
        entry["visibility"] = bool(getattr(obj, "Visibility", False))
        entry["in_list"] = [getattr(item, "Name", None) for item in list(getattr(obj, "InList", []) or [])]
        entry["out_list"] = [getattr(item, "Name", None) for item in list(getattr(obj, "OutList", []) or [])]
    return entry


def _fallback_inspect(doc, *, object_name=None, detail=False) -> dict:
    objects = list(getattr(doc, "Objects", []) or [])
    if object_name:
        objects = [obj for obj in objects if getattr(obj, "Name", None) == object_name]
        if not objects:
            raise ProtocolError(f"no object named {object_name!r} in {getattr(doc, 'Name', '?')}", kind="unknown_object")
    outputs = _fallback_outputs(doc, None)
    return {
        "schema": "cad3.inspect/v1",
        "observer": "worker-fallback (cad3.observe unavailable)",
        "document": {
            "name": getattr(doc, "Name", None),
            "label": getattr(doc, "Label", None),
            "file_name": getattr(doc, "FileName", None) or None,
            "object_count": len(list(getattr(doc, "Objects", []) or [])),
            "recomputes_frozen": bool(getattr(doc, "RecomputesFrozen", False)),
        },
        "outputs": [getattr(obj, "Name", None) for obj in outputs],
        "objects": [_object_entry(obj, detail=detail) for obj in objects],
        "units": {"length": "mm", "volume": "mm^3", "area": "mm^2"},
        "truncated": False,
    }


def _collect_native_issues(doc, outputs) -> tuple:
    """Report native Invalid/Error state prominently; never hide it."""
    issues: list = []
    warnings: list = []
    for obj in list(getattr(doc, "Objects", []) or []):
        name = getattr(obj, "Name", None)
        state = _object_state(obj)
        bad = [item for item in state if item in ("Invalid", "Error")]
        if bad:
            issues.append({"object": name, "state": bad, "kind": "native_invalid"})
        elif "Touched" in state:
            warnings.append(f"object {name} is still Touched after recompute")
        shape = getattr(obj, "Shape", None)
        if shape is None:
            continue
        try:
            if shape.isNull():
                continue
            if not shape.isValid():
                issues.append({"object": name, "kind": "invalid_shape", "message": "Shape.isValid() is False"})
            if not len(shape.Solids):
                warnings.append(f"object {name} has no solid (shell/compound only)")
        except Exception as exc:
            issues.append({"object": name, "kind": "shape_probe_failed", "message": f"{type(exc).__name__}: {exc}"})
    if not outputs and list(getattr(doc, "Objects", []) or []):
        warnings.append("no volume-bearing output object was selected")
    return issues, warnings


def _count_volume_objects(doc) -> int:
    """Cheap hint for error messages: how many objects carry solid geometry."""
    count = 0
    for obj in list(getattr(doc, "Objects", []) or []):
        shape = getattr(obj, "Shape", None)
        if shape is None:
            continue
        try:
            if not shape.isNull() and len(shape.Solids):
                count += 1
        except Exception:
            continue
    return count


def _select_outputs(doc, observe, cad_outputs, warnings) -> tuple:
    """Return (outputs, source). Falls back visibly if cad3.observe fails."""
    if observe is not None and hasattr(observe, "select_output_objects"):
        try:
            return list(observe.select_output_objects(doc, cad_outputs) or []), "cad3.observe"
        except Exception as exc:
            # An explicit object name is a caller contract.  Do not hide a
            # misspelling by rendering a different default object.
            observe_error = getattr(observe, "ObserveError", None)
            if cad_outputs is not None and observe_error is not None and isinstance(exc, observe_error):
                raise
            warnings.append(
                f"cad3.observe.select_output_objects failed ({type(exc).__name__}: {exc}); "
                "used the worker's built-in selection"
            )
    return _fallback_outputs(doc, cad_outputs), "worker-fallback"


def _observe_state(doc, observe, warnings, *, object_name=None, detail=False) -> tuple:
    """Return (state, source). Falls back visibly if cad3.observe fails."""
    if observe is not None and hasattr(observe, "inspect_document"):
        try:
            return observe.inspect_document(doc, object_name=object_name, detail=detail), "cad3.observe"
        except Exception as exc:
            observe_error = getattr(observe, "ObserveError", None)
            if object_name is not None and observe_error is not None and isinstance(exc, observe_error):
                raise
            warnings.append(
                f"cad3.observe.inspect_document failed ({type(exc).__name__}: {exc}); "
                "used the worker's built-in inspection"
            )
    return _fallback_inspect(doc, object_name=object_name, detail=detail), "worker-fallback"


def _code_view(observe, state) -> str:
    if observe is not None and hasattr(observe, "code_view"):
        try:
            return str(observe.code_view(state))
        except Exception as exc:  # pragma: no cover
            return f"code_view failed: {type(exc).__name__}: {exc}\n"
    return "code_view unavailable (cad3.observe not importable inside this FreeCAD runtime)\n"


# --------------------------------------------------------------------------
# actions
# --------------------------------------------------------------------------
def _do_doctor(App, request, record) -> None:
    run_dir = Path(request["run_dir"])
    isolation = _check_isolation(App, run_dir)
    record["config"] = isolation["config"]
    record["config_problems"] = isolation["problems"]
    record["warnings"].extend(isolation["unverified"])

    capabilities = {
        "freecad_import": True,
        "config_isolated": not isolation["problems"],
        "python": sys.version.split()[0],
        "python_executable": sys.executable,
        "run_dir_writable": os.access(run_dir, os.W_OK),
        "observe_module": _load_module("cad3.observe", record["warnings"]) is not None,
        "rendering_module": _load_module("cad3.rendering", record["warnings"]) is not None,
        "import_path": list(sys.path[:3]),
    }
    try:
        import Part  # noqa: F401  (native module availability probe)
        probe = Part.makeBox(1, 1, 1)
        capabilities["part_module"] = True
        capabilities["part_probe_volume"] = _round(probe.Volume)
    except Exception as exc:
        capabilities["part_module"] = False
        capabilities["part_probe_error"] = f"{type(exc).__name__}: {exc}"
    try:
        vtk = _load_optional("vtkmodules.all") or _load_optional("vtk")
        capabilities["vtk"] = vtk is not None
    except Exception:
        capabilities["vtk"] = False
    try:  # headless console build still ships the module on most installs
        gui = _load_optional("FreeCADGui")
        capabilities["gui_importable"] = gui is not None
    except Exception:
        capabilities["gui_importable"] = False

    document_ok = True
    document_error = None
    try:
        probe_doc = App.newDocument("_cad3_doctor")
        try:
            probe_doc.recompute()
        finally:
            App.closeDocument(probe_doc.Name)
    except Exception as exc:
        document_ok = False
        document_error = f"{type(exc).__name__}: {exc}"
    capabilities["document_roundtrip"] = document_ok
    if document_error:
        capabilities["document_error"] = document_error

    record["config_checked"] = not isolation["problems"]
    record["result"] = {"capabilities": capabilities}
    if not document_ok or not capabilities["part_module"]:
        raise ProtocolError(
            "FreeCAD runtime is not usable for modeling: "
            f"part_module={capabilities['part_module']} document_roundtrip={document_ok}"
            + (f" ({document_error})" if document_error else ""),
            kind="capability",
        )


def _do_execute(App, request, record) -> None:
    run_dir = Path(request["run_dir"])
    project_dir = Path(request["project_dir"])
    document = Path(request["document"])
    script = Path(request["script"]) if request.get("script") else None
    if script is None:
        raise ProtocolError("execute request carries no script", kind="bad_request")
    if not script.is_file():
        raise ProtocolError(f"script not found: {script}", kind="script_missing")

    isolation = _check_isolation(App, run_dir)
    record["config"] = isolation["config"]
    record["config_problems"] = isolation["problems"]
    record["warnings"].extend(isolation["unverified"])
    if isolation["problems"]:
        raise ProtocolError(
            "FreeCAD configuration is not isolated to the run directory: " + "; ".join(isolation["problems"]),
            kind="isolation",
        )

    source = script.read_text(encoding="utf-8")
    record["script"] = {"path": str(script), "sha256": hashlib.sha256(source.encode("utf-8")).hexdigest()}

    doc = _open_or_create(App, document, record)
    working_doc = doc

    for entry in (str(script.parent), str(project_dir / "src"), str(project_dir)):
        if entry not in sys.path:
            sys.path.insert(0, entry)

    namespace = {
        "__file__": str(script),
        "__name__": "__main__",
        "__builtins__": __builtins__,
        "App": App,
        "doc": doc,
        "PROJECT_DIR": project_dir,
        "RUN_DIR": run_dir,
    }
    started = time.monotonic()
    try:
        code = compile(source, str(script), "exec")
        exec(code, namespace)
    except SystemExit as exc:
        # sys.exit(0)/exit() must NEVER be mistaken for success.
        code_value = exc.code
        if code_value is None:
            code_value = 0
        if not isinstance(code_value, int):
            code_value = str(code_value)
        record["exit"] = {"kind": "system_exit", "code": code_value}
        raise ProtocolError(
            f"script called SystemExit({code_value!r}) before the run completed", kind="script_system_exit"
        ) from exc
    except BaseException as exc:
        record["exception"] = {
            "type": type(exc).__name__,
            "message": str(exc),
            "traceback": traceback.format_exc(),
        }
        print(record["exception"]["traceback"], file=sys.stderr, flush=True)
        raise ProtocolError(f"script raised {type(exc).__name__}: {exc}", kind="script_exception") from exc
    record["script_elapsed_s"] = round(time.monotonic() - started, 4)

    replaced = namespace.get("doc")
    if replaced is not working_doc:
        if not _is_document(replaced):
            raise ProtocolError(
                "script assigned a non-document value to 'doc'", kind="bad_document_handover"
            )
        working_doc = replaced
        record["warnings"].append(
            f"script replaced the working document with {getattr(replaced, 'Name', '?')!r}; using it as the result"
        )
    record["document"]["result_name"] = getattr(working_doc, "Name", None)

    cad_outputs = namespace.get("cad_outputs")
    if cad_outputs is not None and not isinstance(cad_outputs, (list, tuple)):
        raise ProtocolError("cad_outputs must be a list of object names or objects", kind="cad_outputs")
    record["cad_outputs"] = {
        "declared": cad_outputs is not None,
        "count": len(cad_outputs) if cad_outputs is not None else 0,
    }

    recompute = _recompute(working_doc)
    record["recompute"] = recompute
    if not recompute["ok"]:
        raise ProtocolError(f"document recompute failed: {recompute['message']}", kind="recompute")

    observe = _load_module("cad3.observe", record["warnings"])
    outputs, outputs_source = _select_outputs(working_doc, observe, cad_outputs, record["warnings"])
    record["outputs"] = [getattr(obj, "Name", None) for obj in outputs]
    record["outputs_source"] = outputs_source

    issues, warnings = _collect_native_issues(working_doc, outputs)
    record["native_issues"].extend(issues)
    record["warnings"].extend(warnings)

    state, state_source = _observe_state(working_doc, observe, record["warnings"])
    record["state_source"] = state_source
    record["state"] = _safe_json(state)
    state_path = run_dir / "state.json"
    _dump_json(state_path, record["state"])
    observation_path = run_dir / "observation.txt"
    observation_path.write_text(_code_view(observe, state), encoding="utf-8")
    record["artifacts"]["state_file"] = str(state_path)
    record["artifacts"]["observation_file"] = str(observation_path)

    candidate = Path(request["model_candidate"])
    candidate.parent.mkdir(parents=True, exist_ok=True)
    try:
        working_doc.saveAs(str(candidate))
    except Exception as exc:
        raise ProtocolError(f"cannot save model candidate: {type(exc).__name__}: {exc}", kind="save_candidate") from exc
    if not candidate.is_file() or candidate.stat().st_size == 0:
        raise ProtocolError("model candidate was not written by FreeCAD", kind="save_candidate")
    record["artifacts"]["model_candidate"] = str(candidate)
    record["artifacts"]["model_candidate_sha256"] = _sha256_file(candidate)

    step_path = None
    if request.get("export_step"):
        if not outputs:
            raise ProtocolError(
                "export_step requested but %s selected no output object (%d object(s), %d carry solid geometry); "
                "declare cad_outputs explicitly in the script"
                % (outputs_source, len(list(getattr(working_doc, "Objects", []) or [])),
                   _count_volume_objects(working_doc)),
                kind="export_step",
            )
        step_path = Path(request["step_candidate"])
        step_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            import Part

            Part.export(outputs, str(step_path))
        except Exception as exc:
            raise ProtocolError(f"STEP export failed: {type(exc).__name__}: {exc}", kind="export_step") from exc
        if not step_path.is_file() or step_path.stat().st_size == 0:
            raise ProtocolError("STEP export produced no file", kind="export_step")
        record["artifacts"]["step_candidate"] = str(step_path)

    record["result"] = {
        "outputs": record["outputs"],
        "outputs_source": record["outputs_source"],
        "documents": [str(getattr(working_doc, "Name", ""))],
        "model_candidate": str(candidate),
    }


def _do_inspect(App, request, record) -> None:
    document = Path(request["document"])
    object_name = request.get("object_name") or None
    detail = bool(request.get("detail"))
    isolation = _check_isolation(App, Path(request["run_dir"]))
    record["config"] = isolation["config"]
    record["config_problems"] = isolation["problems"]
    record["warnings"].extend(isolation["unverified"])
    if isolation["problems"]:
        raise ProtocolError(
            "FreeCAD configuration is not isolated to the run directory: " + "; ".join(isolation["problems"]),
            kind="isolation",
        )

    doc = None
    if document.exists():
        try:
            doc = App.openDocument(str(document))
        except Exception as exc:
            raise ProtocolError(
                f"cannot open document {document}: {type(exc).__name__}: {exc}", kind="document_open"
            ) from exc
    if doc is None:
        record["document"] = {"path": str(document), "existed": False, "name": None}
        record["warnings"].append("project has no model document yet; state is empty")
        state = {
            "schema": "cad3.inspect/v1",
            "document": None,
            "outputs": [],
            "objects": [],
            "units": {"length": "mm", "volume": "mm^3", "area": "mm^2"},
        }
    else:
        record["document"] = {
            "path": str(document),
            "existed": True,
            "name": getattr(doc, "Name", None),
        }
        recompute = _recompute(doc)
        record["recompute"] = recompute
        observe = _load_module("cad3.observe", record["warnings"])
        state, state_source = _observe_state(doc, observe, record["warnings"],
                                             object_name=object_name, detail=detail)
        record["state_source"] = state_source

    record["state"] = _safe_json(state)
    observe = _load_module("cad3.observe", record["warnings"])
    observation_path = Path(request["run_dir"]) / "observation.txt"
    observation_path.write_text(_code_view(observe, state), encoding="utf-8")
    record["artifacts"]["observation_file"] = str(observation_path)
    state_path = Path(request["run_dir"]) / "state.json"
    _dump_json(state_path, record["state"])
    record["artifacts"]["state_file"] = str(state_path)
    record["result"] = {"object_name": object_name, "detail": detail}


def _do_render(App, request, record) -> None:
    run_dir = Path(request["run_dir"])
    document = Path(request["document"])
    isolation = _check_isolation(App, run_dir)
    record["config"] = isolation["config"]
    record["config_problems"] = isolation["problems"]
    record["warnings"].extend(isolation["unverified"])
    if isolation["problems"]:
        raise ProtocolError(
            "FreeCAD configuration is not isolated to the run directory: " + "; ".join(isolation["problems"]),
            kind="isolation",
        )
    if not document.exists():
        raise ProtocolError(f"no model document to render yet: {document}", kind="no_document")

    try:
        doc = App.openDocument(str(document))
    except Exception as exc:
        raise ProtocolError(
            f"cannot open document {document}: {type(exc).__name__}: {exc}", kind="document_open"
        ) from exc
    record["document"] = {"path": str(document), "existed": True, "name": getattr(doc, "Name", None)}
    recompute = _recompute(doc)
    record["recompute"] = recompute

    observe = _load_module("cad3.observe", record["warnings"])
    rendering = _load_module("cad3.rendering", record["warnings"])
    outputs, outputs_source = _select_outputs(doc, observe, None, record["warnings"])
    record["outputs_source"] = outputs_source
    record["outputs"] = [getattr(obj, "Name", None) for obj in outputs]
    if not outputs:
        raise ProtocolError(
            "nothing to render: %s selected no output object (%d object(s) in %s, %d carry solid geometry); "
            "declare cad_outputs explicitly with a script run, or fix the observation selection"
            % (record.get("outputs_source") or "the observer", len(list(getattr(doc, "Objects", []) or [])),
               getattr(doc, "Name", "?"), _count_volume_objects(doc)),
            kind="no_geometry",
        )
    if rendering is None or not hasattr(rendering, "render_objects"):
        raise ProtocolError(
            "cad3.rendering is not importable inside this FreeCAD runtime; cannot render",
            kind="rendering_unavailable",
        )
    render_dir = run_dir / "renders"
    render_dir.mkdir(parents=True, exist_ok=True)
    try:
        images = rendering.render_objects(
            outputs,
            render_dir,
            views=tuple(request.get("views") or ("iso", "top", "front", "right")),
            width=int(request.get("width") or 960),
            height=int(request.get("height") or 720),
        )
    except ProtocolError:
        raise
    except Exception as exc:
        record["exception"] = {"type": type(exc).__name__, "message": str(exc), "traceback": traceback.format_exc()}
        raise ProtocolError(f"render failed: {type(exc).__name__}: {exc}", kind="render") from exc
    images = list(images or [])
    if not images:
        raise ProtocolError("renderer returned no images", kind="render")
    record["artifacts"]["renders"] = _safe_json(images)
    record["result"] = {"images": _safe_json(images), "views": list(request.get("views") or ())}
    state, state_source = _observe_state(doc, observe, record["warnings"])
    record["state_source"] = state_source
    record["state"] = _safe_json(state)


_HANDLERS = {
    "doctor": _do_doctor,
    "execute": _do_execute,
    "inspect": _do_inspect,
    "render": _do_render,
}


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------
def _validate_request(request: dict) -> None:
    if not isinstance(request, dict):
        raise ProtocolError("request must be a JSON object", kind="bad_request")
    if request.get("protocol") != PROTOCOL:
        raise ProtocolError(f"unsupported request protocol: {request.get('protocol')!r}", kind="bad_request")
    if request.get("action") not in ACTIONS:
        raise ProtocolError(f"unsupported action: {request.get('action')!r}", kind="bad_request")
    for key in ("run_id", "run_dir"):
        if not request.get(key):
            raise ProtocolError(f"request is missing {key!r}", kind="bad_request")


def execute_request(request_path) -> dict:
    """Run one request and always write a completion record. Returns the record."""
    request_path = Path(request_path)
    started = time.monotonic()
    request: dict = {}
    record: dict = {"protocol": COMPLETION_PROTOCOL, "status": "failed", "ok": False}
    run_dir = None
    try:
        request = json.loads(request_path.read_text(encoding="utf-8"))
        run_dir = Path(request["run_dir"])
    except Exception as exc:
        record["errors"] = [{"kind": "bad_request", "message": f"{type(exc).__name__}: {exc}"}]
        run_dir = request_path.parent
        record["run_id"] = None
        record["action"] = None
    else:
        record.update(
            {
                "run_id": request.get("run_id"),
                "action": request.get("action"),
                "started_at": _utc_now(),
                "pid": os.getpid(),
                "worker": str(Path(__file__).resolve()),
                "python": sys.version.split()[0],
                "errors": [],
                "warnings": [],
                "native_issues": [],
                "artifacts": {},
                "result": None,
                "config": {},
                "config_problems": [],
            }
        )
        try:
            _validate_request(request)
            record["import_path_added"] = _ensure_package_import_path()
            App = _import_freecad()
            record["freecad_version"] = list(App.Version())
            _HANDLERS[request["action"]](App, request, record)
            record["status"] = "completed"
            record["ok"] = True
        except ProtocolError as exc:
            record["errors"].append({"kind": exc.kind, "message": str(exc)})
        except BaseException as exc:
            record["errors"].append(
                {
                    "kind": "worker_error",
                    "message": f"{type(exc).__name__}: {exc}",
                    "traceback": traceback.format_exc(),
                }
            )
            print(traceback.format_exc(), file=sys.stderr, flush=True)

    record["finished_at"] = _utc_now()
    record["elapsed_s"] = round(time.monotonic() - started, 4)
    if not record.get("ok"):
        record["status"] = "failed"
    try:
        _dump_json(run_dir / "completion.json", record)
    except Exception as exc:  # pragma: no cover - last resort
        print(f"cad3 worker could not write completion.json: {exc}", file=sys.stderr, flush=True)
    summary = {
        "run_id": record.get("run_id"),
        "action": record.get("action"),
        "ok": record.get("ok"),
        "status": record.get("status"),
        "errors": record.get("errors"),
        "native_issues": record.get("native_issues"),
        "warnings": record.get("warnings"),
        "outputs": record.get("outputs"),
    }
    print(f"{RESULT_MARKER} " + json.dumps(summary, ensure_ascii=False, default=str), flush=True)
    return record


def _import_freecad():
    try:
        import FreeCAD as App
    except Exception as exc:  # pragma: no cover - only when mislaunched
        raise ProtocolError(f"cannot import FreeCAD inside the worker: {type(exc).__name__}: {exc}", kind="freecad_import")
    return App


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    request_path = os.environ.get("CAD3_RUN_REQUEST")
    if not request_path:
        for item in argv:
            if item.endswith(".json"):
                request_path = item
                break
    if not request_path:
        print("usage: CAD3_RUN_REQUEST=<request.json> freecadcmd worker.py [request.json]", file=sys.stderr)
        return 2
    record = execute_request(request_path)
    return 0 if record.get("ok") else 1


def _autorun() -> bool:
    """True when this file is the script FreeCADCmd was asked to execute.

    FreeCADCmd ``exec``s the entry script with ``__name__`` set to the file
    stem (not ``"__main__"``), so the usual guard is not enough.  Importing the
    module normally (``cad3.worker``) must never run the worker.
    """
    if __name__ == "__main__":
        return True
    if __name__ != Path(globals().get("__file__") or "").stem:
        return False
    if os.environ.get("CAD3_RUN_REQUEST"):
        return True
    return any(item.endswith(".json") for item in sys.argv[1:])


if __name__ == "__main__" or _autorun():
    raise SystemExit(main())
