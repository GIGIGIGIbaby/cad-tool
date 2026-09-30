"""Host-side runtime owner for CAD3.

Standard library only.  FreeCAD is never imported here; every modeling action
runs in a separately launched, profile-isolated FreeCADCmd process that is
driven by one JSON request file and must write one explicit completion record
(see ``cad3.worker``).

Public contract
---------------
``discover_freecad(explicit=None) -> Path``
    explicit -> ``CAD3_FREECAD`` -> well-known app/platform locations -> PATH.
    Raises ``Cad3Error`` when nothing usable is found.  Never installs or
    imports FreeCAD.

``doctor(executable=None, timeout=15) -> dict``
    Safe isolated capability probe; returns ``executable``/``version``/
    ``capabilities``.

``execute(project, script=None, *, executable=None, timeout=120.0, export_step=False) -> dict``
``inspect(project, *, executable=None, timeout=60.0, object_name=None, detail=False) -> dict``
``render(project, *, executable=None, timeout=90.0, views=('iso','top','front','right'), width=960, height=720) -> dict``
    All return a JSON-ready dict.  ``ok`` means the *execution protocol*
    succeeded; it does NOT mean the design is valid or manufacturable.  Native
    ``Invalid``/``Error`` objects are reported prominently under
    ``native_issues`` while ``ok`` may still be true.

Failure protocol
----------------
A completion record is mandatory and must match the requested ``run_id``,
``action`` and script hash.  A zero process exit code alone is never success:
missing/stale records, script exceptions, ``SystemExit``, signals, timeouts and
C++ crashes are all failures.  Failed runs publish nothing; their logs stay in
``<root>/.cad3/runs/<action>-<run_id>/`` and their paths are returned.
Successful runs atomically replace the model document (and optional STEP) and
refresh ``artifacts/latest``.

Result keys (shared by every entry point)
-----------------------------------------
``ok`` (protocol success only), ``action``, ``run_id``, ``run_dir`` (absolute)
and ``run_dir_rel``, ``errors`` (``[{kind, message, ...}]``) plus ``error``
(the first message), ``warnings``, ``native_issues``/``native_issue_count``,
``process`` (``exit_code``/``signal``/``timed_out``), ``elapsed_s``,
``timeout_s``, ``stdout``/``stderr`` (path + tail), ``input_hash``,
``protocol`` (request/completion paths and status), ``ok_meaning``.

Action specific:

* ``execute`` -> ``state``, ``document`` (path/abs/existed/name/published),
  ``step`` (project-relative STEP path or ``None``), ``model``
  (``path``/``sha256``/``abs``), ``model_published``, ``published``/``files``
  (label -> project-relative/absolute path), ``worker`` (completion-record
  details: outputs source, recompute, exception, config isolation).
* ``inspect`` -> ``state`` (``cad3.observe.inspect_document`` output; feed it to
  ``cad3.observe.code_view`` on the host), plus published
  ``inspect.state.json``/``inspect.observation.txt``/``inspect.json``.
* ``render`` -> ``images`` (per-view renderer metadata plus ``published``), and
  ``artifacts/latest/renders/*.png`` plus ``render.json``.
* ``doctor`` -> ``executable``, ``version``, ``capabilities`` (config
  isolation, native modules, VTK, Part probe, document round trip).

Limits
------
* Process/profile isolation keeps FreeCAD from reading or writing the user's
  real configuration, but the input script is a *trusted local program*: it is
  not a malicious-code sandbox.
* A script that itself writes to ``models/*.FCStd`` bypasses candidate
  publishing; the run records the external document hash change and refuses to
  publish, but the script's own write has already happened.
* Only the FreeCADCmd process group started by this module is ever signalled.
* A genuine C++ crash cannot be caught in Python; it is reported from the
  process exit status and the absence of a completion record.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

PROTOCOL = "cad3.run/v1"
COMPLETION_PROTOCOL = "cad3.completion/v1"
OK_MEANING = "execution/protocol success only; not design or manufacturing correctness"

#: The packaged worker is launched by the FreeCAD interpreter, not by the host.
WORKER_PATH = Path(__file__).resolve().with_name("worker.py")
_FREECAD_ENV = "CAD3_FREECAD"

_CRASH_SIGNALS = ("SIGSEGV", "SIGABRT", "SIGBUS", "SIGILL", "SIGFPE")
_CRASH_EXIT_CODES = (134, 139, 135, 136, 132, 245)


try:  # pragma: no cover - depends on sibling module availability
    from cad3.project import Cad3Error, project_lock  # type: ignore
except Exception:  # cad3.project is owned by another module; stay usable without it
    class Cad3Error(Exception):
        """Fallback mirror of ``cad3.project.Cad3Error`` (same name, same role)."""

    project_lock = None


# --------------------------------------------------------------------------
# FreeCAD discovery
# --------------------------------------------------------------------------
def _app_candidates():
    patterns = [
        "/Applications/FreeCAD*.app/Contents/Resources/bin/freecadcmd",
        "/Applications/FreeCAD*.app/Contents/Resources/bin/FreeCADCmd",
        "/Applications/FreeCAD*.app/Contents/MacOS/FreeCADCmd",
        str(Path.home() / "Applications/FreeCAD*.app/Contents/Resources/bin/freecadcmd"),
        "/usr/bin/freecadcmd",
        "/usr/local/bin/freecadcmd",
        "/snap/bin/freecad.cmd",
        "/opt/freecad/bin/freecadcmd",
        "/usr/lib/freecad/bin/freecadcmd",
        "/usr/lib/freecad-daily/bin/freecadcmd",
        r"C:\Program Files\FreeCAD*\bin\freecadcmd.exe",
        r"C:\Program Files (x86)\FreeCAD*\bin\freecadcmd.exe",
    ]
    for pattern in patterns:
        if any(char in pattern for char in "*?["):
            import glob

            yield from sorted(glob.glob(pattern))
        else:
            yield pattern


def _usable(path) -> Path | None:
    candidate = Path(path).expanduser()
    if candidate.is_dir():
        for suffix in (
            "bin/freecadcmd",
            "bin/FreeCADCmd",
            "Contents/Resources/bin/freecadcmd",
            "Contents/MacOS/FreeCADCmd",
            "bin/freecadcmd.exe",
        ):
            nested = candidate / suffix
            if nested.is_file():
                return nested.resolve()
        return None
    if candidate.is_file():
        return candidate.resolve()
    return None


def discover_freecad(explicit: str | None = None) -> Path:
    """Locate a FreeCADCmd executable. Never imports or installs FreeCAD."""
    if explicit:
        found = _usable(explicit)
        if found is None:
            raise Cad3Error(f"no usable FreeCADCmd executable at {explicit!r}")
        return found

    from_env = os.environ.get(_FREECAD_ENV)
    if from_env:
        found = _usable(from_env)
        if found is None:
            raise Cad3Error(f"{_FREECAD_ENV}={from_env!r} does not point to a usable FreeCADCmd executable")
        return found

    for raw in _app_candidates():
        found = _usable(raw)
        if found is not None:
            return found

    for name in ("freecadcmd", "FreeCADCmd", "freecad.cmd", "FreeCADCmd.exe"):
        found = shutil.which(name)
        if found:
            resolved = _usable(found)
            if resolved is not None:
                return resolved
    raise Cad3Error(
        "FreeCADCmd not found: pass --freecad PATH, set CAD3_FREECAD, or install FreeCAD "
        "(searched common application paths and PATH)"
    )


# --------------------------------------------------------------------------
# project accessors (duck-typed: cad3.project.Project is owned elsewhere)
# --------------------------------------------------------------------------
def _root(project) -> Path:
    root = getattr(project, "root", None)
    if root is None:
        raise Cad3Error("project object has no .root; pass a cad3.project.Project")
    return Path(root)


def _document_path(project) -> Path:
    configured = getattr(project, "document_path", None)
    if configured:
        return Path(configured)
    config = getattr(project, "config", None) or {}
    relative = config.get("document") or "models/main.FCStd"
    return _root(project) / relative


def _entrypoint_path(project) -> Path:
    configured = getattr(project, "entrypoint_path", None)
    if configured:
        return Path(configured)
    config = getattr(project, "config", None) or {}
    relative = config.get("entrypoint") or "src/main.py"
    return _root(project) / relative


def _artifacts_dir(project) -> Path:
    return _root(project) / "artifacts" / "latest"


def _runs_dir(project) -> Path:
    return _root(project) / ".cad3" / "runs"


# --------------------------------------------------------------------------
# small utilities
# --------------------------------------------------------------------------
def _sha256_file(path) -> str | None:
    path = Path(path)
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=lambda obj: repr(obj)[:200]),
        encoding="utf-8",
    )
    os.replace(tmp, path)


def _rel(path, root) -> str:
    try:
        return Path(path).resolve().relative_to(Path(root).resolve()).as_posix()
    except ValueError:
        return str(path)


def _inside(base, candidate) -> bool:
    try:
        Path(os.path.realpath(str(candidate))).relative_to(Path(os.path.realpath(str(base))))
        return True
    except ValueError:
        return False


def _tail(path, *, max_lines: int = 40, max_chars: int = 8000) -> str:
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    lines = text.splitlines()
    if len(lines) > max_lines:
        lines = lines[-max_lines:]
    joined = "\n".join(lines)
    if len(joined) > max_chars:
        joined = joined[-max_chars:]
    return joined


def _copy_atomic(src, dest: Path) -> Path:
    src = Path(src)
    if not src.is_file():
        raise FileNotFoundError(f"evidence source is missing: {src}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + f".tmp-{os.getpid()}")
    shutil.copyfile(src, tmp)
    os.replace(tmp, dest)
    return dest


def _new_run_id() -> str:
    return time.strftime("%Y%m%dT%H%M%S", time.gmtime()) + "-" + uuid.uuid4().hex[:8]


# --------------------------------------------------------------------------
# locking
# --------------------------------------------------------------------------
@contextmanager
def _fallback_lock(root: Path):
    """Serialize V3 mutations when cad3.project.project_lock is unavailable.

    Uses fcntl.flock, which the OS releases when the process dies, so ordinary
    termination never leaves a stale lock.
    """
    lock_dir = Path(root) / ".cad3"
    lock_path = lock_dir / "lock"
    lock_dir.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "a+b")
    try:
        try:
            import fcntl
        except ImportError:  # pragma: no cover - non-POSIX fallback
            fcntl = None
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        else:  # pragma: no cover - non-POSIX fallback
            deadline = time.monotonic() + 60.0
            while True:
                try:
                    fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                except FileExistsError:
                    if time.monotonic() > deadline:
                        raise Cad3Error(f"timed out waiting for project lock {lock_path}")
                    time.sleep(0.1)
                else:
                    os.close(fd)
                    break
            try:
                yield
            finally:
                try:
                    os.unlink(lock_path)
                except OSError:
                    pass
    finally:
        handle.close()


@contextmanager
def _null_lock():
    yield


def _lock_for(project):
    """Use cad3.project.project_lock for real Projects; fall back otherwise.

    ``project_lock.__init__`` rejects objects that are not ``Project`` instances,
    so a duck-typed project (tests, embedding) still gets a real OS-level lock
    instead of running unsynchronised.  A lock that is *held* raises on
    ``__enter__`` and is never swallowed.
    """
    if project is None:
        return _null_lock()
    if project_lock is not None:
        try:
            return project_lock(project)
        except Exception:
            return _fallback_lock(_root(project))
    return _fallback_lock(_root(project))


# --------------------------------------------------------------------------
# process launch
# --------------------------------------------------------------------------
def _child_env(run_dir: Path, request_path: Path) -> dict:
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV")
    }
    profile = run_dir / "freecad-home"
    home = run_dir / "home"
    profile.mkdir(parents=True, exist_ok=True)
    home.mkdir(parents=True, exist_ok=True)
    env.update(
        {
            "FREECAD_USER_HOME": str(profile),
            "HOME": str(home),
            "PYTHONIOENCODING": "utf-8",
            "MPLBACKEND": "Agg",
            "CAD3_RUN_REQUEST": str(request_path),
        }
    )
    return env


def _signal_name(returncode: int | None) -> str | None:
    if returncode is None:
        return None
    number = None
    if returncode < 0:
        number = -returncode
    elif returncode > 128:
        number = returncode - 128
    if number is None:
        return None
    try:
        return signal.Signals(number).name
    except ValueError:
        return f"signal-{number}"


def _kill_own_group(proc: subprocess.Popen, grace: float = 5.0) -> None:
    """Kill only the process group this module created - never ours, never a GUI."""
    if proc.poll() is not None:
        return
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        return
    own_group = os.getpgid(0)
    isolated = pgid != own_group and pgid == proc.pid

    def _terminate(sig) -> None:
        try:
            if isolated:
                os.killpg(pgid, sig)
            elif sig == signal.SIGTERM:
                proc.terminate()
            else:
                proc.kill()
        except (ProcessLookupError, PermissionError, OSError):
            pass

    _terminate(signal.SIGTERM)
    try:
        proc.wait(timeout=grace)
        return
    except subprocess.TimeoutExpired:
        pass
    _terminate(signal.SIGKILL)
    try:
        proc.wait(timeout=grace)
    except Exception:
        pass


def _launch(exe: Path, run_dir: Path, request_path: Path, *, timeout: float, cwd: Path) -> dict:
    profile = run_dir / "freecad-home"
    home = run_dir / "home"
    profile.mkdir(parents=True, exist_ok=True)
    home.mkdir(parents=True, exist_ok=True)
    user_cfg = profile / "user.cfg"
    system_cfg = profile / "system.cfg"
    # The requested config paths must already be inside the run dir before exec.
    for path in (user_cfg, system_cfg, profile, home):
        if not _inside(run_dir, path):
            raise Cad3Error(f"refusing to execute: config path {path} is outside the run dir {run_dir}")
    if not WORKER_PATH.is_file():
        raise Cad3Error(f"packaged worker is missing: {WORKER_PATH}")

    command = [
        str(exe),
        "--user-cfg",
        str(user_cfg),
        "--system-cfg",
        str(system_cfg),
        str(WORKER_PATH),
    ]
    env = _child_env(run_dir, request_path)
    stdout_path = run_dir / "stdout.log"
    stderr_path = run_dir / "stderr.log"
    started = time.monotonic()
    timed_out = False
    with open(stdout_path, "wb") as out, open(stderr_path, "wb") as err:
        proc = subprocess.Popen(
            command,
            cwd=str(cwd),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=err,
            start_new_session=True,
        )
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_own_group(proc)
    elapsed = round(time.monotonic() - started, 4)
    return {
        "command": command,
        "cwd": str(cwd),
        "stdout_path": stdout_path,
        "stderr_path": stderr_path,
        "exit_code": proc.returncode,
        "signal": _signal_name(proc.returncode),
        "timed_out": timed_out,
        "elapsed_s": elapsed,
    }


# --------------------------------------------------------------------------
# completion record
# --------------------------------------------------------------------------
def _read_completion(run_dir: Path):
    path = run_dir / "completion.json"
    if not path.is_file():
        return None, {"kind": "missing_completion_record", "message": f"no completion record at {path}"}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return None, {
            "kind": "unreadable_completion_record",
            "message": f"cannot parse {path}: {type(exc).__name__}: {exc}",
        }
    if not isinstance(payload, dict):
        return None, {"kind": "invalid_completion_record", "message": f"{path} is not a JSON object"}
    return payload, None


def _validate_completion(record, record_error, *, request: dict, proc: dict) -> list:
    errors: list = []
    stale = []
    if record_error is not None:
        errors.append(record_error)
    else:
        if record.get("protocol") != COMPLETION_PROTOCOL:
            stale.append(f"completion protocol {record.get('protocol')!r} != {COMPLETION_PROTOCOL!r}")
        if record.get("run_id") != request.get("run_id"):
            stale.append(f"run_id {record.get('run_id')!r} != requested {request.get('run_id')!r}")
        if record.get("action") != request.get("action"):
            stale.append(f"action {record.get('action')!r} != requested {request.get('action')!r}")
        expected_script = request.get("script_sha256")
        if expected_script:
            actual_script = (record.get("script") or {}).get("sha256")
            if actual_script != expected_script:
                stale.append("script hash does not match the requested script")
        if stale:
            errors.append(
                {
                    "kind": "stale_result",
                    "message": "completion record does not belong to this run: " + "; ".join(stale),
                }
            )
        for item in record.get("errors") or []:
            if isinstance(item, dict):
                errors.append({"kind": item.get("kind") or "worker_error", "message": item.get("message") or ""})
            else:
                errors.append({"kind": "worker_error", "message": str(item)})
        if record.get("status") != "completed" and not record.get("errors"):
            errors.append(
                {
                    "kind": "incomplete_status",
                    "message": f"completion record status is {record.get('status')!r}, expected 'completed'",
                }
            )
        if not record.get("ok") and not record.get("errors"):
            errors.append({"kind": "worker_not_ok", "message": "worker reported ok=false"})

    if proc["timed_out"]:
        errors.append(
            {
                "kind": "timeout",
                "message": f"FreeCADCmd exceeded {request.get('timeout_s')}s and its process group was killed",
            }
        )
    elif proc["signal"]:
        errors.append(
            {
                "kind": "signal",
                "message": f"FreeCADCmd was terminated by {proc['signal']} (exit code {proc['exit_code']})",
                "crash_suspected": proc["signal"] in _CRASH_SIGNALS,
            }
        )
    elif proc["exit_code"] != 0:
        entry = {
            "kind": "process_exit",
            "message": f"FreeCADCmd exited with code {proc['exit_code']}",
        }
        if record is None and proc["exit_code"] in _CRASH_EXIT_CODES:
            entry["crash_suspected"] = True
        # The record already explains the failure; keep the exit code visible in
        # result["process"] instead of adding a redundant error.
        if not errors:
            errors.append(entry)

    return errors


# --------------------------------------------------------------------------
# publishing
# --------------------------------------------------------------------------
def _publish_images(record: dict, run_dir: Path, artifacts: Path, root: Path) -> list:
    images = []
    entries = record.get("artifacts", {}).get("renders") or []
    render_dir = artifacts / "renders"
    for entry in entries:
        source = None
        meta = {}
        if isinstance(entry, str):
            source = Path(entry)
            meta = {"view": source.stem}
        elif isinstance(entry, dict):
            meta = dict(entry)
            for key in ("path", "file", "output", "filename"):
                value = meta.get(key)
                if value:
                    source = Path(value)
                    break
        if source is None:
            meta["publish_error"] = "renderer result has no file path"
            images.append(meta)
            continue
        if not _inside(run_dir, source):
            meta["publish_error"] = f"refused to publish image outside the run dir: {source}"
            images.append(meta)
            continue
        if not source.is_file():
            meta["publish_error"] = f"renderer reported a missing file: {source}"
            images.append(meta)
            continue
        dest = _copy_atomic(source, render_dir / source.name)
        meta["path"] = str(source)
        meta["published"] = _rel(dest, root)
        images.append(meta)
    return images


def _publish(project, action: str, run_dir: Path, record: dict, result: dict, document: Path, expected_hash) -> None:
    """Copy candidate artifacts into models/ and artifacts/latest/.

    Only called after protocol success.  Every source is pre-flighted before the
    first copy, and the model document itself is only ever touched by an atomic
    ``os.replace``.  ``artifacts/latest/<report>.json`` is written last and acts
    as the commit marker for the whole evidence set.
    """
    root = _root(project)
    artifacts = _artifacts_dir(project)
    artifacts.mkdir(parents=True, exist_ok=True)
    published: dict = {}  # label -> project-relative path
    files: dict = {}  # label -> absolute path
    plan: list = []  # (label, source, dest) in publish order
    artifacts_meta = record.get("artifacts") or {}
    candidate_sha = None

    if action == "execute":
        candidate = Path(artifacts_meta.get("model_candidate") or "")
        if not candidate.is_file():
            raise Cad3Error(f"worker reported no model candidate at {candidate}")
        if not _inside(run_dir, candidate):
            raise Cad3Error(f"refusing to publish a candidate outside the run dir: {candidate}")
        candidate_sha = artifacts_meta.get("model_candidate_sha256")
        if candidate_sha and _sha256_file(candidate) != candidate_sha:
            raise Cad3Error("model candidate hash changed after the worker saved it")
        # Concurrency guard: the input document must not have changed externally.
        current_hash = _sha256_file(document)
        if current_hash != expected_hash:
            raise Cad3Error(
                "refusing to publish: the model document changed on disk while the run was in progress "
                f"(expected {expected_hash}, found {current_hash})"
            )
        models = document.parent
        models.mkdir(parents=True, exist_ok=True)
        plan.extend(
            [
                ("evidence_stderr", run_dir / "stderr.log", artifacts / "stderr.log"),
                ("evidence_state", run_dir / "state.json", artifacts / "state.json"),
                ("evidence_observation", run_dir / "observation.txt", artifacts / "observation.txt"),
                ("evidence_stdout", run_dir / "stdout.log", artifacts / "stdout.log"),
            ]
        )
        step_candidate = artifacts_meta.get("step_candidate")
        if step_candidate:
            step_source = Path(step_candidate)
            if not _inside(run_dir, step_source):
                raise Cad3Error(f"refusing to publish a STEP file outside the run dir: {step_source}")
            plan.append(("step", step_source, models / (document.stem + ".step")))
        plan.append(("model", candidate, document))

    elif action == "render":
        images = _publish_images(record, run_dir, artifacts, root)
        result["images"] = images
        published["renders"] = [item["published"] for item in images if item.get("published")]
        plan.extend(
            [
                ("evidence_stderr", run_dir / "stderr.log", artifacts / "render.stderr.log"),
                ("evidence_stdout", run_dir / "stdout.log", artifacts / "render.stdout.log"),
            ]
        )

    elif action == "mates":
        plan.extend(
            [
                ("evidence_mates", run_dir / "mates.json", artifacts / "mates.json"),
                ("evidence_stderr", run_dir / "stderr.log", artifacts / "mates.stderr.log"),
                ("evidence_stdout", run_dir / "stdout.log", artifacts / "mates.stdout.log"),
            ]
        )

    elif action == "inspect":
        plan.extend(
            [
                ("evidence_state", run_dir / "state.json", artifacts / "inspect.state.json"),
                ("evidence_observation", run_dir / "observation.txt", artifacts / "inspect.observation.txt"),
                ("evidence_stderr", run_dir / "stderr.log", artifacts / "inspect.stderr.log"),
                ("evidence_stdout", run_dir / "stdout.log", artifacts / "inspect.stdout.log"),
            ]
        )

    missing = [str(source) for _label, source, _dest in plan if not Path(source).is_file()]
    if missing:
        raise Cad3Error("refusing to publish incomplete evidence: " + ", ".join(missing))

    for label, source, dest in plan:
        out = _copy_atomic(source, dest)
        published[label] = _rel(out, root)
        files[label] = str(out)
        if label == "model":
            model_sha = _sha256_file(out)
            result["model"] = {"path": published[label], "sha256": model_sha, "abs": str(out)}
            result["model_published"] = True
            if candidate_sha and model_sha != candidate_sha:
                raise Cad3Error(
                    f"published model hash {model_sha} does not match the verified candidate {candidate_sha}"
                )

    report_name = {"execute": "report.json", "render": "render.json", "inspect": "inspect.json",
                   "mates": "mates.report.json"}[action]
    report_path = artifacts / report_name
    result["published"] = published
    result["files"] = files
    if action != "execute":
        result.setdefault("model_published", False)
    _write_json(report_path, result)
    result["published"]["report"] = _rel(report_path, root)
    result["files"]["report"] = str(report_path)


# --------------------------------------------------------------------------
# the shared driver
# --------------------------------------------------------------------------
def _fail(result: dict, kind: str, message: str, **extra) -> dict:
    result["ok"] = False
    result["errors"].append({"kind": kind, "message": message})
    if extra:
        result.update(extra)
    result["error"] = message
    return result


def _invoke(
    *,
    action: str,
    root: Path,
    document: Path,
    project=None,
    script: Path | None = None,
    export_step: bool = False,
    object_name: str | None = None,
    detail: bool = False,
    views=None,
    sections=None,
    interfaces=None,
    width: int = 960,
    height: int = 720,
    executable=None,
    timeout: float = 120.0,
    publish: bool = True,
    step_path: Path | None = None,
) -> dict:
    run_id = _new_run_id()
    result: dict = {
        "ok": False,
        "action": action,
        "run_id": run_id,
        "run_dir": None,
        "ok_meaning": OK_MEANING,
        "errors": [],
        "warnings": [],
        "native_issues": [],
        "state": None,
        "document": {"path": _rel(document, root), "abs": str(document), "existed": document.exists()},
        "step": None,
        "images": [],
        "published": {},
        "files": {},
        "model_published": False,
    }

    try:
        exe = discover_freecad(executable)
    except Cad3Error as exc:
        return _fail(result, "freecad_not_found", str(exc))
    result["executable"] = str(exe)

    try:
        timeout = float(timeout)
    except (TypeError, ValueError):
        return _fail(result, "bad_request", f"invalid timeout: {timeout!r}")
    if timeout <= 0:
        return _fail(result, "bad_request", "timeout must be positive")
    result["timeout_s"] = timeout

    script_sha = None
    if action == "execute":
        if script is None:
            script = _entrypoint_path(project) if project is not None else None
        if script is None:
            return _fail(result, "bad_request", "no script given and the project has no entrypoint")
        script = Path(script).expanduser()
        if not script.is_absolute():
            script = root / script
        script = Path(os.path.realpath(str(script)))
        if not _inside(root, script):
            return _fail(
                result,
                "script_outside_project",
                f"refusing to execute a script outside the project: {script} (project root {root})",
            )
        if not script.is_file():
            return _fail(result, "script_missing", f"script not found: {script}")
        script_sha = _sha256_file(script)
    elif action == "render":
        sections = list(sections or [])
        views = tuple(views) if views is not None else (() if sections else ("iso", "top", "front", "right"))
        if not views and not sections:
            return _fail(result, "bad_request", "render needs at least one view or section")
        for item in sections:
            text = str(item).strip().lower().replace(" ", "")
            axis, _, value = text.partition("=")
            try:
                ok_value = axis in ("x", "y", "z") and float(value) == float(value) and abs(float(value)) < 1e9
            except ValueError:
                ok_value = False
            if not ok_value:
                return _fail(result, "bad_request", f"section must look like x=0, y=12.5 or z=-3: {item!r}")
        for name in views:
            if name not in ("iso", "top", "front", "right"):
                return _fail(result, "bad_request", f"unsupported view name: {name!r}")
        width, height = int(width), int(height)
        if width <= 0 or height <= 0:
            return _fail(result, "bad_request", "width and height must be positive")

    run_dir = (root / ".cad3" / "runs" / f"{action}-{run_id}") if project is not None else (root / "run")
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return _fail(result, "run_dir", f"cannot create run dir {run_dir}: {exc}")
    result["run_dir"] = str(run_dir)
    result["run_dir_rel"] = _rel(run_dir, root)

    request = {
        "protocol": PROTOCOL,
        "run_id": run_id,
        "action": action,
        "project_dir": str(root),
        "document": str(document),
        "document_exists": document.exists(),
        "script": str(script) if script is not None else None,
        "script_sha256": script_sha,
        "model_candidate": str(run_dir / "model.FCStd"),
        "step_candidate": str(step_path or (run_dir / (document.stem + ".step"))),
        "export_step": bool(export_step),
        "views": list(views) if views is not None else None,
        "sections": list(sections or []),
        "interfaces": interfaces,
        "width": width,
        "height": height,
        "object_name": object_name,
        "detail": bool(detail),
        "run_dir": str(run_dir),
        "timeout_s": timeout,
        "host_python": sys.version.split()[0],
        "host_worker": str(WORKER_PATH),
    }
    request_path = run_dir / "request.json"
    _write_json(request_path, request)

    input_hash_before = _sha256_file(document) if document.is_file() else None
    result["input_hash"] = {"before": input_hash_before, "after": None, "changed": None}

    lock = _lock_for(project) if (project is not None and action in ("execute", "render")) else _null_lock()
    with lock:
        try:
            proc = _launch(exe, run_dir, request_path, timeout=timeout, cwd=root)
        except Cad3Error as exc:
            return _fail(result, "launch", str(exc))
        except OSError as exc:
            return _fail(result, "launch", f"cannot start FreeCADCmd: {type(exc).__name__}: {exc}")

        record, record_error = _read_completion(run_dir)
        errors = _validate_completion(record, record_error, request=request, proc=proc)
        warnings: list = []
        if record:
            warnings.extend(str(item) for item in record.get("warnings") or [])
        if record_error is not None:
            warnings.append(
                "the worker wrote no usable completion record; a Python-level crash, os._exit, "
                "or a native C++ crash cannot be caught inside the worker"
            )

        input_hash_after = _sha256_file(document) if document.is_file() else None
        result["input_hash"] = {
            "before": input_hash_before,
            "after": input_hash_after,
            "changed": input_hash_before != input_hash_after,
        }
        if input_hash_before != input_hash_after:
            errors.append(
                {
                    "kind": "input_changed",
                    "message": "the model document changed on disk while the run was in progress; "
                    "refusing to publish this run",
                }
            )

        result.update(
            {
                "process": {
                    "exit_code": proc["exit_code"],
                    "signal": proc["signal"],
                    "timed_out": proc["timed_out"],
                },
                "elapsed_s": proc["elapsed_s"],
                "command": proc["command"],
                "protocol": {
                    "request": _rel(request_path, root),
                    "completion": _rel(run_dir / "completion.json", root)
                    if (run_dir / "completion.json").is_file()
                    else None,
                    "completion_protocol": (record or {}).get("protocol"),
                    "completion_status": (record or {}).get("status"),
                },
                "stdout": {
                    "path": _rel(proc["stdout_path"], root),
                    "abs": str(proc["stdout_path"]),
                    "tail": _tail(proc["stdout_path"]),
                },
                "stderr": {
                    "path": _rel(proc["stderr_path"], root),
                    "abs": str(proc["stderr_path"]),
                    "tail": _tail(proc["stderr_path"]),
                },
            }
        )
        if record:
            result["freecad_version"] = record.get("freecad_version")
            result["version"] = record.get("freecad_version")
            result["state"] = record.get("state")
            result["native_issues"] = list(record.get("native_issues") or [])
            result["warnings"].extend(warnings)
            result["worker"] = {
                "status": record.get("status"),
                "config_checked": record.get("config_checked"),
                "config": record.get("config"),
                "config_problems": record.get("config_problems") or [],
                "document": record.get("document"),
                "outputs": record.get("outputs"),
                "outputs_source": record.get("outputs_source"),
                "state_source": record.get("state_source"),
                "import_path_added": record.get("import_path_added"),
                "recompute": record.get("recompute"),
                "exception": record.get("exception"),
                "exit": record.get("exit"),
                "artifacts": record.get("artifacts"),
            }
            if action == "execute":
                result["document"] = {
                    "path": _rel(document, root),
                    "abs": str(document),
                    "existed": bool((record.get("document") or {}).get("existed", document.exists())),
                    "name": (record.get("document") or {}).get("name"),
                    "published": False,
                }
            if action == "doctor":
                result["capabilities"] = (record.get("result") or {}).get("capabilities") or {}

        if errors:
            result["ok"] = False
            result["errors"].extend(errors)
            result["error"] = errors[0]["message"]
            if result["native_issues"]:
                result["native_issue_count"] = len(result["native_issues"])
                result["error"] += f" | {len(result['native_issues'])} native issue(s) reported"
            return result

        result["ok"] = True
        result["protocol_ok"] = True
        if result["native_issues"]:
            result["native_issue_count"] = len(result["native_issues"])
            result["warnings"].append(
                f"FreeCAD reported {len(result['native_issues'])} native Invalid/Error issue(s); "
                "execution succeeded but the model state needs attention"
            )

        if action == "render":
            result["images"] = list((record.get("result") or {}).get("images") or [])
        if action == "mates":
            result["mates"] = record.get("result")
        if publish and project is not None:
            try:
                _publish(project, action, run_dir, record or {}, result, document, input_hash_before)
                if action == "execute":
                    result["document"]["published"] = True
                    if (record or {}).get("artifacts", {}).get("step_candidate"):
                        result["step"] = result["published"].get("step")
            except (Cad3Error, OSError, shutil.Error) as exc:
                result["ok"] = False
                message = f"{type(exc).__name__}: {exc}"
                if result.get("model_published"):
                    message += " (the model document was already replaced; the evidence set is incomplete)"
                result["errors"].append({"kind": "publish", "message": message})
                result["error"] = message
        return result


# --------------------------------------------------------------------------
# public entry points
# --------------------------------------------------------------------------
def doctor(executable: str | None = None, timeout: float = 15) -> dict:
    """Probe an isolated FreeCADCmd for version/config isolation/capabilities."""
    run_id = _new_run_id()
    result: dict = {
        "ok": False,
        "action": "doctor",
        "run_id": run_id,
        "run_dir": None,
        "ok_meaning": "capability probe only; not design correctness",
        "errors": [],
        "warnings": [],
        "native_issues": [],
        "state": None,
        "capabilities": {},
        "version": None,
    }
    try:
        exe = discover_freecad(executable)
    except Cad3Error as exc:
        return _fail(result, "freecad_not_found", str(exc))
    result["executable"] = str(exe)

    tmp_root = Path(tempfile.mkdtemp(prefix="cad3-doctor-"))
    try:
        probed = _invoke(
            action="doctor",
            root=tmp_root,
            document=tmp_root / "models" / "main.FCStd",
            executable=str(exe),
            timeout=timeout,
            publish=False,
        )
        probed["executable"] = str(exe)
        probed["run_dir_cleaned"] = True
        return probed
    except Exception as exc:  # pragma: no cover - defensive
        return _fail(result, "doctor", f"{type(exc).__name__}: {exc}")
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


def execute(
    project,
    script: Path | None = None,
    *,
    executable: str | None = None,
    timeout: float = 120.0,
    export_step: bool = False,
) -> dict:
    """Run the project script inside isolated FreeCADCmd and publish on success."""
    try:
        root = _root(project)
        document = _document_path(project)
    except Cad3Error as exc:
        return _fail(
            {
                "ok": False,
                "action": "execute",
                "run_id": None,
                "run_dir": None,
                "ok_meaning": OK_MEANING,
                "errors": [],
                "warnings": [],
                "native_issues": [],
            },
            "bad_project",
            str(exc),
        )
    return _invoke(
        action="execute",
        root=root,
        document=document,
        project=project,
        script=script,
        export_step=export_step,
        executable=executable,
        timeout=timeout,
    )


def inspect(
    project,
    *,
    executable: str | None = None,
    timeout: float = 60.0,
    object_name: str | None = None,
    detail: bool = False,
) -> dict:
    """Read-only observation of the current native document."""
    try:
        root = _root(project)
        document = _document_path(project)
    except Cad3Error as exc:
        return _fail(
            {
                "ok": False,
                "action": "inspect",
                "run_id": None,
                "run_dir": None,
                "ok_meaning": OK_MEANING,
                "errors": [],
                "warnings": [],
                "native_issues": [],
                "state": None,
            },
            "bad_project",
            str(exc),
        )
    return _invoke(
        action="inspect",
        root=root,
        document=document,
        project=project,
        object_name=object_name,
        detail=detail,
        executable=executable,
        timeout=timeout,
    )


def render(
    project,
    *,
    executable: str | None = None,
    timeout: float = 90.0,
    views=None,
    sections=None,
    width: int = 960,
    height: int = 720,
) -> dict:
    """Tessellate + shade the current document to PNGs via cad3.rendering.

    ``views`` defaults to iso/top/front/right unless only ``sections`` (e.g.
    ``["y=12.5"]``) are requested; each section is one extra PNG.
    """
    try:
        root = _root(project)
        document = _document_path(project)
    except Cad3Error as exc:
        return _fail(
            {
                "ok": False,
                "action": "render",
                "run_id": None,
                "run_dir": None,
                "ok_meaning": OK_MEANING,
                "errors": [],
                "warnings": [],
                "native_issues": [],
                "images": [],
            },
            "bad_project",
            str(exc),
        )
    return _invoke(
        action="render",
        root=root,
        document=document,
        project=project,
        views=views,
        sections=sections,
        width=width,
        height=height,
        executable=executable,
        timeout=timeout,
    )


def mates(
    project,
    *,
    file: str | None = None,
    geometry: bool = True,
    executable: str | None = None,
    timeout: float = 120.0,
) -> dict:
    """Check declared interfaces (``notes/interfaces.json``) and, when a model
    exists, locate every declared cylinder/plane on the real FreeCAD shapes.

    ``geometry=False`` runs on the host only, before any geometry exists.
    """
    from . import mates as mates_module

    base = {"ok": False, "action": "mates", "run_id": None, "run_dir": None, "ok_meaning": OK_MEANING,
            "errors": [], "warnings": [], "native_issues": []}
    try:
        root = _root(project)
        document = _document_path(project)
    except Cad3Error as exc:
        return _fail(base, "bad_project", str(exc))
    rel = file or mates_module.DEFAULT_FILE
    path = Path(rel)
    if not path.is_absolute():
        path = root / path
    path = Path(os.path.realpath(str(path)))
    if not _inside(root, path):
        return _fail(base, "bad_request", f"interface file must be inside the project: {path}")
    if not path.is_file():
        return _fail(base, "no_declaration",
                     f"no interface declaration at {_rel(path, root)}; write one first",
                     format=mates_module.FORMAT_HELP)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        decl = mates_module.normalize(raw)
    except (ValueError, mates_module.DeclarationError) as exc:
        return _fail(base, "bad_declaration", f"{_rel(path, root)}: {exc}", format=mates_module.FORMAT_HELP)
    if not geometry or not document.exists():
        reason = "--no-geometry requested" if not geometry else "no model document yet"
        base.update(ok=True, protocol_ok=True, file=_rel(path, root),
                    mates=mates_module.report(decl, geometry_skipped=reason))
        return base
    result = _invoke(
        action="mates",
        root=root,
        document=document,
        project=project,
        interfaces=raw,
        executable=executable,
        timeout=timeout,
    )
    result["file"] = _rel(path, root)
    return result

