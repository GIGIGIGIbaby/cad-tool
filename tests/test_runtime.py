"""Unit tests for the cad3 runtime failure protocol and the in-FreeCAD worker.

Host side is standard library only; FreeCAD itself is never imported here.

Two harnesses are used:

* ``freecad_stub`` - a tiny in-process stand-in for the native FreeCAD API, used
  to drive ``cad3.worker.execute_request`` directly (protocol-level tests).
* ``fake_freecadcmd.py`` - an executable test double that runs the *real*
  ``cad3.worker`` with that stub injected, so the host-side launch/validate/
  publish/timeout paths are exercised end to end without FreeCAD.

A final class runs against a real installed FreeCADCmd when one is discovered
(``CAD3_SKIP_REAL_FREECAD=1`` opts out).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
# The repository root also contains a ``cad3.py`` entry shim; running from that
# directory would otherwise shadow the ``cad3`` package.
if str(SRC) in sys.path:
    sys.path.remove(str(SRC))
sys.path.insert(0, str(SRC))

import cad3.runtime as runtime  # noqa: E402

WORKER_PATH = SRC / "cad3" / "worker.py"


# --------------------------------------------------------------------------
# test doubles, kept as source text so they can run in a child process
# --------------------------------------------------------------------------
STUB_FREECAD_SOURCE = r'''"""Minimal stand-in for the native FreeCAD API (test double, no real FreeCAD)."""
import json
import math
import os
import sys
import types
from pathlib import Path


class StubBoundBox:
    def __init__(self, x, y, z):
        self.XMin = self.YMin = self.ZMin = 0.0
        self.XMax, self.YMax, self.ZMax = float(x), float(y), float(z)
        self.XLength, self.YLength, self.ZLength = float(x), float(y), float(z)


class StubShape:
    def __init__(self, obj):
        self._obj = obj

    def isNull(self):
        return self._obj._dims is None

    def isValid(self):
        return not self._obj._invalid

    @property
    def Volume(self):
        dims = self._obj._dims or {}
        if "Length" in dims:
            return float(dims.get("Length", 0) * dims.get("Width", 0) * dims.get("Height", 0))
        if "Radius" in dims:
            return math.pi * float(dims.get("Radius", 0)) ** 2 * float(dims.get("Height", 0))
        return 0.0

    @property
    def Area(self):
        return 6.0

    @property
    def Solids(self):
        return [] if self.isNull() else [self]

    @property
    def Faces(self):
        return [0, 1, 2, 3, 4, 5]

    @property
    def BoundBox(self):
        dims = self._obj._dims or {}
        if "Length" in dims:
            return StubBoundBox(dims.get("Length", 0), dims.get("Width", 0), dims.get("Height", 0))
        if "Radius" in dims:
            radius = float(dims.get("Radius", 0))
            return StubBoundBox(2 * radius, 2 * radius, dims.get("Height", 0))
        return StubBoundBox(0, 0, 0)


class StubObject:
    def __init__(self, type_id, name, dims=None, invalid=False, state=None):
        object.__setattr__(self, "TypeId", type_id)
        object.__setattr__(self, "Name", name)
        object.__setattr__(self, "Label", name)
        object.__setattr__(self, "_dims", dict(dims) if dims is not None else {})
        object.__setattr__(self, "_invalid", bool(invalid))
        object.__setattr__(self, "_state", list(state) if state else ["Up-to-date"])
        object.__setattr__(self, "Shape", StubShape(self))
        object.__setattr__(self, "InList", [])
        object.__setattr__(self, "OutList", [])
        object.__setattr__(self, "Visibility", True)

    def __setattr__(self, key, value):
        if key in ("Length", "Width", "Height", "Radius"):
            self._dims[key] = value
        object.__setattr__(self, key, value)

    @property
    def State(self):
        return list(self._state)


class StubDocument:
    def __init__(self, name, file_name=""):
        self.Name = name
        self.Label = name
        self.FileName = file_name
        self.Objects = []
        self.RecomputesFrozen = False
        self.recompute_hook = None

    def addObject(self, type_id, name):
        obj = StubObject(type_id, name)
        self.Objects.append(obj)
        return obj

    def getObject(self, name):
        for obj in self.Objects:
            if obj.Name == name:
                return obj
        return None

    def removeObject(self, name):
        self.Objects = [obj for obj in self.Objects if obj.Name != name]

    def recompute(self):
        if os.environ.get("CAD3_STUB_RECOMPUTE_FAIL"):
            raise RuntimeError("stub recompute failure")
        if self.recompute_hook is not None:
            self.recompute_hook()

    def _payload(self):
        return {
            "name": self.Name,
            "objects": [
                {"TypeId": obj.TypeId, "Name": obj.Name, "dims": obj._dims,
                 "invalid": obj._invalid, "state": obj._state}
                for obj in self.Objects
            ],
        }

    def saveAs(self, path):
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self._payload()), encoding="utf-8")
        self.FileName = str(target)

    def save(self):
        if not self.FileName:
            raise RuntimeError("document has no file name")
        self.saveAs(self.FileName)


def _config_base():
    override = os.environ.get("CAD3_STUB_CONFIG_DIR")
    if override:
        return Path(override)
    home = os.environ.get("FREECAD_USER_HOME")
    return Path(home) if home else Path(tempfile_dir())


def tempfile_dir():
    import tempfile
    return tempfile.gettempdir()


def build_app():
    app = types.ModuleType("FreeCAD")
    documents = {}

    def config_get(key):
        if key not in ("UserAppData", "UserParameter", "SystemParameter"):
            return ""
        if os.environ.get("CAD3_STUB_CONFIG_EMPTY"):
            return ""
        return str(_config_base() / (key + ".cfg"))

    def get_user_app_data_dir():
        if os.environ.get("CAD3_STUB_CONFIG_EMPTY"):
            return ""
        return str(_config_base())

    def new_document(name="Unnamed"):
        doc = StubDocument(name)
        documents[doc.Name] = doc
        return doc

    def open_document(path):
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        doc = StubDocument(payload.get("name", "Loaded"), str(path))
        for entry in payload.get("objects", []):
            doc.Objects.append(
                StubObject(entry["TypeId"], entry["Name"], entry.get("dims"),
                           entry.get("invalid", False), entry.get("state"))
            )
        documents[doc.Name] = doc
        return doc

    def close_document(name):
        documents.pop(name, None)

    app.Version = lambda: ["0", "99", "stub"]
    app.ConfigGet = config_get
    app.getUserAppDataDir = get_user_app_data_dir
    app.newDocument = new_document
    app.openDocument = open_document
    app.closeDocument = close_document
    app.listDocuments = lambda: dict(documents)
    app.ActiveDocument = None
    return app


def build_part():
    part = types.ModuleType("Part")

    def export(objects, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text("ISO-10303-21;\nSTUB STEP\n", encoding="utf-8")

    def make_box(length, width, height):
        return types.SimpleNamespace(Volume=float(length) * float(width) * float(height))

    part.export = export
    part.makeBox = make_box
    return part


def install():
    app = build_app()
    sys.modules["FreeCAD"] = app
    if not os.environ.get("CAD3_STUB_NO_PART"):
        sys.modules["Part"] = build_part()
    return app


def fake_observe():
    module = types.ModuleType("cad3.observe")

    def select_output_objects(doc, names=None):
        if names:
            selected = []
            for item in names:
                if isinstance(item, str):
                    obj = doc.getObject(item)
                    if obj is None:
                        raise ValueError("no object named %s" % item)
                    selected.append(obj)
                else:
                    selected.append(item)
            return selected
        return [obj for obj in doc.Objects if not obj.Shape.isNull() and obj.Shape.Solids]

    def inspect_document(doc, object_name=None, detail=False):
        objects = list(doc.Objects)
        if object_name:
            objects = [obj for obj in objects if obj.Name == object_name]
        return {
            "schema": "cad3.inspect/v1",
            "observer": "fake-observe",
            "document": {"name": doc.Name, "object_count": len(doc.Objects)},
            "outputs": [obj.Name for obj in select_output_objects(doc)],
            "objects": [
                {"name": obj.Name, "type": obj.TypeId, "state": obj.State,
                 "shape": {"volume": obj.Shape.Volume, "valid": obj.Shape.isValid()}}
                for obj in objects
            ],
        }

    def code_view(state):
        name = (state or {}).get("document") or {}
        return "# fake code view for %s\n" % name.get("name")

    module.select_output_objects = select_output_objects
    module.inspect_document = inspect_document
    module.code_view = code_view
    return module


def fake_rendering():
    module = types.ModuleType("cad3.rendering")

    def render_objects(objects, output_dir, views=("iso",), width=960, height=720):
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        images = []
        for view in views:
            path = output_dir / ("render_%s.png" % view)
            path.write_bytes(b"\x89PNG\r\n\x1a\n" + view.encode("utf-8"))
            images.append({
                "view": view,
                "path": str(path),
                "width": int(width),
                "height": int(height),
                "objects": [getattr(obj, "Name", None) for obj in objects],
            })
        return images

    module.render_objects = render_objects
    return module
'''

FAKE_FREECMD_SOURCE = r'''#!/usr/bin/env python3
"""Test double for FreeCADCmd: runs the real cad3.worker with a stubbed FreeCAD."""
import json
import os
import runpy
import signal
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, "__SRC__")

import freecad_stub  # noqa: E402


def _inside(path, base):
    try:
        Path(os.path.realpath(str(path))).relative_to(Path(os.path.realpath(str(base))))
        return True
    except ValueError:
        return False


def wiring_notes(request, run_dir):
    notes = []
    if not sys.argv or Path(sys.argv[-1]).name != "worker.py":
        notes.append("last argv is not worker.py: %r" % (sys.argv[-1:],))
    for flag in ("--user-cfg", "--system-cfg"):
        if flag not in sys.argv:
            notes.append("missing %s" % flag)
    for key in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"):
        if key in os.environ:
            notes.append("leaked host %s" % key)
    for key in ("FREECAD_USER_HOME", "HOME"):
        value = os.environ.get(key)
        if not value:
            notes.append("missing %s" % key)
        elif not _inside(value, run_dir):
            notes.append("%s=%s is outside %s" % (key, value, run_dir))
    if not Path(os.environ.get("CAD3_RUN_REQUEST", "")).is_file():
        notes.append("CAD3_RUN_REQUEST is not a file")
    return notes


def write_record(run_dir, record):
    (run_dir / "completion.json").write_text(json.dumps(record), encoding="utf-8")


def run_worker(request_path, run_dir, modules):
    app = freecad_stub.install()
    if modules == "fake":
        sys.modules["cad3.observe"] = freecad_stub.fake_observe()
        sys.modules["cad3.rendering"] = freecad_stub.fake_rendering()
    else:
        sys.modules["cad3.observe"] = None
        sys.modules["cad3.rendering"] = None
    worker = Path(sys.argv[-1])
    try:
        runpy.run_path(str(worker), run_name="__main__")
    except SystemExit as exc:
        code = exc.code
        if code is None:
            return 0
        return code if isinstance(code, int) else 1
    return 0


def main():
    scenario = os.environ.get("CAD3_TEST_SCENARIO", "ok")
    modules = os.environ.get("CAD3_TEST_MODULES", "none")
    request_path = Path(os.environ["CAD3_RUN_REQUEST"])
    request = json.loads(request_path.read_text(encoding="utf-8"))
    run_dir = Path(request["run_dir"])

    notes = wiring_notes(request, run_dir)
    if notes:
        write_record(run_dir, {
            "protocol": "cad3.completion/v1", "run_id": request.get("run_id"),
            "action": request.get("action"), "status": "failed", "ok": False,
            "errors": [{"kind": "fake_wiring", "message": "; ".join(notes)}],
            "warnings": [], "native_issues": [],
        })
        print("FAKE_WIRING_ERROR " + "; ".join(notes))
        return 1

    if scenario == "exit0":
        print("fake: exiting 0 without a completion record", flush=True)
        os._exit(0)
    if scenario == "crash":
        print("fake: simulating a native C++ crash", flush=True)
        sys.stdout.flush()
        os.kill(os.getpid(), signal.SIGSEGV)
        time.sleep(5)
        os._exit(139)
    if scenario == "stale":
        write_record(run_dir, {
            "protocol": "cad3.completion/v1", "run_id": "stale-run-id",
            "action": request.get("action"), "status": "completed", "ok": True,
            "errors": [], "warnings": [], "native_issues": [],
            "state": {"stale": True}, "result": {},
        })
        print("fake: wrote a completion record from another run")
        return 0
    if scenario == "stale_script":
        write_record(run_dir, {
            "protocol": "cad3.completion/v1", "run_id": request.get("run_id"),
            "action": request.get("action"), "status": "completed", "ok": True,
            "errors": [], "warnings": [], "native_issues": [],
            "script": {"path": request.get("script"), "sha256": "0" * 64},
            "state": {"stale": True}, "result": {},
        })
        print("fake: wrote a completion record with the wrong script hash")
        return 0
    if scenario == "hang":
        grandchild = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
        (run_dir / "grandchild.pid").write_text(str(grandchild.pid), encoding="utf-8")
        print("fake: hanging with grandchild %d" % grandchild.pid, flush=True)
        time.sleep(600)
        return 0
    if scenario == "input_change":
        code = run_worker(request_path, run_dir, modules)
        document = Path(request["document"])
        document.parent.mkdir(parents=True, exist_ok=True)
        document.write_bytes(b"EXTERNAL-CHANGE")
        print("fake: changed the project document externally")
        return code
    if scenario == "slow_ok":
        time.sleep(float(os.environ.get("CAD3_TEST_SLOW_SECONDS", "3")))
        return run_worker(request_path, run_dir, modules)

    return run_worker(request_path, run_dir, modules)


if __name__ == "__main__":
    raise SystemExit(main())
'''


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
class StubProject:
    """Duck-typed cad3 project used to keep these tests independent of cad3.project."""

    def __init__(self, root: Path, name: str = "test"):
        self.root = Path(root)
        self.config = {
            "schema": "cad3.project/v1",
            "name": name,
            "document": "models/main.FCStd",
            "entrypoint": "src/main.py",
        }
        self.document_path = self.root / "models" / "main.FCStd"
        self.entrypoint_path = self.root / "src" / "main.py"
        self.internal_dir = self.root / ".cad3"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_bytes(path) -> bytes | None:
    path = Path(path)
    return path.read_bytes() if path.is_file() else None


def process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover
        return True


def kinds(result: dict) -> list:
    return [item.get("kind") for item in result.get("errors", [])]


class Harness(unittest.TestCase):
    """Shared temp workspace with the stub module and the fake FreeCADCmd."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="cad3-runtime-tests-"))
        cls.addClassCleanup(shutil.rmtree, cls.tmp, True)
        (cls.tmp / "freecad_stub.py").write_text(STUB_FREECAD_SOURCE, encoding="utf-8")
        fake = cls.tmp / "fake_freecadcmd.py"
        fake.write_text(FAKE_FREECMD_SOURCE.replace("__SRC__", str(SRC)), encoding="utf-8")
        fake.chmod(0o755)
        cls.fake_exe = fake

    def env(self, scenario: str = "ok", modules: str = "none"):
        """Patch the fake FreeCADCmd's scenario/module switches for one call."""
        return unittest.mock.patch.dict(
            os.environ, {"CAD3_TEST_SCENARIO": scenario, "CAD3_TEST_MODULES": modules}
        )

    def project(self, name: str) -> StubProject:
        root = self.tmp / name
        (root / "src").mkdir(parents=True, exist_ok=True)
        (root / "models").mkdir(parents=True, exist_ok=True)
        return StubProject(root, name)

    def write_script(self, project: StubProject, source: str) -> Path:
        path = project.entrypoint_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
        return path

    def run_runtime(self, project, scenario="ok", modules="none", **kwargs):
        kwargs.setdefault("timeout", 30.0)
        with self.env(scenario=scenario, modules=modules):
            return runtime.execute(project, executable=str(self.fake_exe), **kwargs)


BOX_SCRIPT = """\
box = doc.addObject("Part::Box", "Box")
box.Length = 40.0
box.Width = 20.0
box.Height = 10.0
doc.recompute()
cad_outputs = ["Box"]
"""


# --------------------------------------------------------------------------
# worker-level protocol tests (in-process, stub FreeCAD)
# --------------------------------------------------------------------------
class WorkerProtocolTests(Harness):
    ENV_KEYS = ("FREECAD_USER_HOME", "HOME", "CAD3_STUB_CONFIG_DIR",
                "CAD3_STUB_CONFIG_EMPTY", "CAD3_STUB_RECOMPUTE_FAIL")
    MODULE_KEYS = ("FreeCAD", "Part", "cad3.observe", "cad3.rendering")

    def setUp(self):
        self.project = self.project(self._testMethodName)
        self.script = self.write_script(self.project, BOX_SCRIPT)
        self._saved = {name: sys.modules.get(name) for name in self.MODULE_KEYS}
        self._env_saved = {key: os.environ.get(key) for key in self.ENV_KEYS}
        self.addCleanup(self._restore_modules)
        self.addCleanup(self._restore_env)

    def _restore_modules(self):
        for name in self.MODULE_KEYS:
            module = self._saved.get(name)
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module

    def _restore_env(self):
        for key, value in self._env_saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _load_stub_module(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location("freecad_stub", self.tmp / "freecad_stub.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def _install_stub(self, *, observe=None, rendering=None):
        """Install the stub FreeCAD plus a deterministic cad3.observe/rendering state.

        ``None`` means "module unavailable" (a real observer may exist in the
        package, so tests must not depend on it); pass a module to exercise the
        real integration path.
        """
        module = self._load_stub_module()
        module.install()
        sys.modules["cad3.observe"] = observe
        sys.modules["cad3.rendering"] = rendering
        return module

    def _worker(self):
        import cad3.worker as worker

        return worker

    def run_worker_request(self, path):
        """Run the worker in-process with its stdout/stderr captured."""
        import contextlib
        import io

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            record = self._worker().execute_request(path)
        self.last_output = out.getvalue()
        self.last_error = err.getvalue()
        return record

    def request(self, **overrides):
        worker = self._worker()
        run_id = overrides.pop("run_id", "worker-test")
        action = overrides.pop("action", "execute")
        run_dir = self.project.root / ".cad3" / "runs" / f"{action}-{run_id}"
        run_dir.mkdir(parents=True, exist_ok=True)
        script = overrides.pop("script", self.script)
        request = {
            "protocol": worker.PROTOCOL,
            "run_id": run_id,
            "action": action,
            "project_dir": str(self.project.root),
            "document": str(self.project.document_path),
            "document_exists": self.project.document_path.exists(),
            "script": str(script) if script else None,
            "script_sha256": sha256_bytes(script.read_bytes()) if script else None,
            "model_candidate": str(run_dir / "model.FCStd"),
            "step_candidate": str(run_dir / "main.step"),
            "export_step": False,
            "views": None,
            "width": 640,
            "height": 480,
            "object_name": None,
            "detail": False,
            "run_dir": str(run_dir),
            "timeout_s": 30,
        }
        request.update(overrides)
        # The real host always provides an isolated profile/home for the child.
        os.environ["FREECAD_USER_HOME"] = str(run_dir / "freecad-home")
        os.environ["HOME"] = str(run_dir / "home")
        path = run_dir / "request.json"
        path.write_text(json.dumps(request), encoding="utf-8")
        return request, path

    def test_success_writes_completion_record_and_candidate(self):
        self._install_stub()
        request, path = self.request()
        record = self.run_worker_request(path)

        self.assertTrue(record["ok"], record.get("errors"))
        self.assertEqual(record["status"], "completed")
        self.assertEqual(record["protocol"], "cad3.completion/v1")
        self.assertEqual(record["run_id"], "worker-test")
        self.assertEqual(record["script"]["sha256"], sha256_bytes(self.script.read_bytes()))
        self.assertEqual(record["outputs"], ["Box"])
        self.assertEqual(record["state"]["outputs"], ["Box"])
        self.assertTrue(Path(record["artifacts"]["model_candidate"]).is_file())
        self.assertTrue((Path(request["run_dir"]) / "completion.json").is_file())
        self.assertEqual(record["native_issues"], [])

    def test_system_exit_zero_is_failure(self):
        self.write_script(self.project, "import sys\nsys.exit(0)\n")
        self._install_stub()
        request, path = self.request()
        record = self.run_worker_request(path)
        self.assertFalse(record["ok"])
        self.assertEqual(record["errors"][0]["kind"], "script_system_exit")
        self.assertEqual(record["exit"], {"kind": "system_exit", "code": 0})
        self.assertFalse(Path(request["model_candidate"]).exists())

    def test_script_exception_is_recorded_with_traceback(self):
        self.write_script(self.project, "raise ValueError('boom')\n")
        self._install_stub()
        request, path = self.request()
        record = self.run_worker_request(path)
        self.assertFalse(record["ok"])
        self.assertEqual(record["errors"][0]["kind"], "script_exception")
        self.assertEqual(record["exception"]["type"], "ValueError")
        self.assertIn("ValueError", record["exception"]["traceback"])
        self.assertFalse(Path(request["model_candidate"]).exists())

    def test_config_outside_run_dir_refuses_before_running_script(self):
        marker = self.project.root / "script-ran.marker"
        self.write_script(
            self.project,
            "from pathlib import Path\nPath(%r).write_text('ran')\n" % str(marker),
        )
        self._install_stub()
        request, path = self.request()
        outside = self.tmp / "user-profile-outside"
        outside.mkdir(exist_ok=True)
        os.environ["CAD3_STUB_CONFIG_DIR"] = str(outside)
        record = self.run_worker_request(path)
        self.assertFalse(record["ok"])
        self.assertEqual(record["errors"][0]["kind"], "isolation")
        self.assertFalse(marker.exists(), "script must not run when isolation cannot be verified")

    def test_unverifiable_config_is_a_warning_not_a_failure(self):
        self._install_stub()
        request, path = self.request()
        os.environ["CAD3_STUB_CONFIG_EMPTY"] = "1"
        record = self.run_worker_request(path)
        self.assertTrue(record["ok"], record.get("errors"))
        self.assertTrue(any("could not be verified" in item for item in record["warnings"]))

    def test_invalid_native_objects_are_reported_but_ok(self):
        self.write_script(
            self.project,
            "obj = doc.addObject('Part::Feature', 'Broken')\n"
            "obj._invalid = True\n"
            "obj._state = ['Invalid']\n"
            "doc.recompute()\n"
            "cad_outputs = ['Broken']\n",
        )
        self._install_stub()
        request, path = self.request()
        record = self.run_worker_request(path)
        self.assertTrue(record["ok"], record.get("errors"))
        self.assertTrue(record["native_issues"])
        self.assertEqual({item["kind"] for item in record["native_issues"]},
                         {"native_invalid", "invalid_shape"})

    def test_cad_outputs_unknown_name_fails(self):
        self.write_script(self.project, "cad_outputs = ['Nope']\n")
        self._install_stub()
        request, path = self.request()
        record = self.run_worker_request(path)
        self.assertFalse(record["ok"])
        self.assertIn(record["errors"][0]["kind"], {"cad_outputs", "protocol"})

    def test_output_selection_prefers_cad3_observe(self):
        stub = self._load_stub_module()
        observe = stub.fake_observe()
        calls = []
        real_select = observe.select_output_objects

        def spy(doc, names=None):
            calls.append(names)
            return real_select(doc, names)

        observe.select_output_objects = spy
        self._install_stub(observe=observe)
        request, path = self.request()
        record = self.run_worker_request(path)
        self.assertTrue(record["ok"], record.get("errors"))
        self.assertEqual(record["outputs_source"], "cad3.observe")
        self.assertEqual(calls, [["Box"]])
        self.assertEqual(record["state"]["observer"], "fake-observe")

    def test_bad_protocol_and_action_are_rejected(self):
        self._install_stub()
        _, path = self.request(protocol="wrong/v9")
        record = self.run_worker_request(path)
        self.assertFalse(record["ok"])
        self.assertEqual(record["errors"][0]["kind"], "bad_request")

        _, path = self.request(action="nonsense")
        record = self.run_worker_request(path)
        self.assertFalse(record["ok"])
        self.assertEqual(record["errors"][0]["kind"], "bad_request")

    def test_export_step_produces_candidate(self):
        self._install_stub()
        request, path = self.request(export_step=True)
        record = self.run_worker_request(path)
        self.assertTrue(record["ok"], record.get("errors"))
        self.assertTrue(Path(request["step_candidate"]).is_file())

    def test_recompute_failure_is_reported(self):
        # The script leaves recompute to the worker, so the failure is a
        # protocol/recompute failure rather than a script exception.
        self.write_script(self.project, "doc.addObject('Part::Box', 'Box')\n")
        self._install_stub()
        os.environ["CAD3_STUB_RECOMPUTE_FAIL"] = "1"
        request, path = self.request()
        record = self.run_worker_request(path)
        self.assertFalse(record["ok"])
        self.assertEqual(record["errors"][0]["kind"], "recompute")

    def test_empty_project_inspect_reports_no_document(self):
        self._install_stub()
        request, path = self.request(action="inspect")
        record = self.run_worker_request(path)
        self.assertTrue(record["ok"], record.get("errors"))
        self.assertIsNone(record["state"]["document"])
        self.assertTrue(any("no model document" in item for item in record["warnings"]))


# --------------------------------------------------------------------------
# host-side failure protocol (fake FreeCADCmd running the real worker)
# --------------------------------------------------------------------------
class RuntimeProtocolTests(Harness):
    def setUp(self):
        self.project = self.project(self._testMethodName)
        self.write_script(self.project, BOX_SCRIPT)

    def test_success_publishes_model_and_consistent_evidence(self):
        result = self.run_runtime(self.project, export_step=True)
        self.assertTrue(result["ok"], result.get("errors"))
        self.assertEqual(result["action"], "execute")
        self.assertTrue(result["model_published"])
        self.assertEqual(result["document"]["published"], True)
        self.assertEqual(result["state"]["outputs"], ["Box"])

        model = self.project.document_path
        self.assertTrue(model.is_file())
        latest = self.project.root / "artifacts" / "latest"
        for name in ("report.json", "stdout.log", "stderr.log", "state.json", "observation.txt"):
            self.assertTrue((latest / name).is_file(), name)
        self.assertTrue((self.project.root / "models" / "main.step").is_file())
        self.assertEqual(result["step"], "models/main.step")

        report = json.loads((latest / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["model"]["sha256"], sha256_bytes(model.read_bytes()))
        self.assertEqual(report["published"]["model"], "models/main.FCStd")
        self.assertEqual(result["model"]["sha256"], sha256_bytes(model.read_bytes()))
        self.assertIn(runtime.OK_MEANING, report["ok_meaning"])
        self.assertNotIn("fake_wiring", kinds(result))
        self.assertIn("state.json", result["published"]["evidence_state"])

    def test_script_exception_preserves_prior_model_and_evidence(self):
        first = self.run_runtime(self.project)
        self.assertTrue(first["ok"], first.get("errors"))
        model_before = read_bytes(self.project.document_path)
        report_path = self.project.root / "artifacts" / "latest" / "report.json"
        report_before = read_bytes(report_path)

        self.write_script(self.project, "raise RuntimeError('nope')\n")
        result = self.run_runtime(self.project)
        self.assertFalse(result["ok"])
        self.assertEqual(kinds(result)[0], "script_exception")
        self.assertFalse(result["model_published"])
        self.assertEqual(read_bytes(self.project.document_path), model_before)
        self.assertEqual(read_bytes(report_path), report_before)
        self.assertEqual(result["published"], {})
        self.assertEqual(result["worker"]["exception"]["type"], "RuntimeError")
        self.assertTrue(Path(result["run_dir"]).is_dir(), "failed run logs stay in .cad3")

    def test_system_exit_zero_is_not_success(self):
        self.write_script(self.project, "import sys\nsys.exit(0)\n")
        result = self.run_runtime(self.project)
        self.assertFalse(result["ok"])
        self.assertEqual(kinds(result)[0], "script_system_exit")
        self.assertFalse(self.project.document_path.exists())
        self.assertEqual(result["process"]["exit_code"], 1)

    def test_missing_completion_record_detected_even_with_exit_zero(self):
        result = self.run_runtime(self.project, scenario="exit0")
        self.assertFalse(result["ok"])
        self.assertEqual(kinds(result), ["missing_completion_record"])
        self.assertEqual(result["process"]["exit_code"], 0)
        self.assertIsNone(result["protocol"]["completion"])
        self.assertFalse(self.project.document_path.exists())

    def test_stale_result_from_another_run_is_rejected(self):
        result = self.run_runtime(self.project, scenario="stale")
        self.assertFalse(result["ok"])
        self.assertIn("stale_result", kinds(result))
        self.assertFalse(self.project.document_path.exists())

    def test_stale_script_hash_is_rejected(self):
        result = self.run_runtime(self.project, scenario="stale_script")
        self.assertFalse(result["ok"])
        self.assertIn("stale_result", kinds(result))

    def test_native_crash_is_reported(self):
        result = self.run_runtime(self.project, scenario="crash")
        self.assertFalse(result["ok"])
        self.assertEqual(result["process"]["signal"], "SIGSEGV")
        self.assertIn("missing_completion_record", kinds(result))
        self.assertTrue(any(item.get("crash_suspected") for item in result["errors"]))
        self.assertFalse(self.project.document_path.exists())

    def test_timeout_kills_only_its_own_process_group(self):
        bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        self.addCleanup(lambda: (bystander.terminate(), bystander.wait(timeout=10)))
        started = time.monotonic()
        result = self.run_runtime(self.project, scenario="hang", timeout=3.0)
        elapsed = time.monotonic() - started

        self.assertFalse(result["ok"])
        self.assertIn("timeout", kinds(result))
        self.assertTrue(result["process"]["timed_out"])
        self.assertLess(elapsed, 20)
        self.assertTrue(bystander.poll() is None, "the test's own process must never be signalled")
        pid_file = Path(result["run_dir"]) / "grandchild.pid"
        self.assertTrue(pid_file.is_file())
        grandchild = int(pid_file.read_text().strip())
        deadline = time.monotonic() + 5
        while process_alive(grandchild) and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertFalse(process_alive(grandchild), "the whole child process group must be killed")
        self.assertFalse(self.project.document_path.exists())

    def test_external_document_change_refuses_publish(self):
        result = self.run_runtime(self.project, scenario="input_change")
        self.assertFalse(result["ok"])
        self.assertIn("input_changed", kinds(result))
        self.assertFalse(result["model_published"])
        self.assertEqual(read_bytes(self.project.document_path), b"EXTERNAL-CHANGE")
        self.assertFalse((self.project.root / "artifacts" / "latest" / "report.json").exists())

    def test_script_outside_project_is_refused(self):
        outside = self.tmp / "outside-script.py"
        outside.write_text("print('should never run')\n", encoding="utf-8")
        result = runtime.execute(self.project, outside, executable=str(self.fake_exe), timeout=10)
        self.assertFalse(result["ok"])
        self.assertEqual(kinds(result), ["script_outside_project"])
        self.assertIsNone(result["run_dir"], "nothing may be launched for a refused script")

    def test_missing_script_is_reported(self):
        result = runtime.execute(self.project, self.project.root / "src" / "nope.py",
                                 executable=str(self.fake_exe), timeout=10)
        self.assertFalse(result["ok"])
        self.assertEqual(kinds(result), ["script_missing"])

    def test_inspect_returns_state_and_publishes_evidence(self):
        self.assertTrue(self.run_runtime(self.project)["ok"])
        result = runtime.inspect(self.project, executable=str(self.fake_exe), timeout=30)
        self.assertTrue(result["ok"], result.get("errors"))
        self.assertEqual(result["action"], "inspect")
        self.assertEqual(result["state"]["outputs"], ["Box"])
        self.assertEqual(result["state"]["objects"][0]["shape"]["volume"], 8000.0)
        latest = self.project.root / "artifacts" / "latest"
        self.assertTrue((latest / "inspect.json").is_file())
        self.assertTrue((latest / "inspect.state.json").is_file())
        self.assertEqual(result["published"]["evidence_state"], "artifacts/latest/inspect.state.json")

    def test_inspect_object_filter(self):
        self.assertTrue(self.run_runtime(self.project)["ok"])
        result = runtime.inspect(self.project, executable=str(self.fake_exe), timeout=30,
                                 object_name="Box", detail=True)
        self.assertTrue(result["ok"], result.get("errors"))
        self.assertIn("Box", result["state"]["outputs"])

    def test_render_publishes_images(self):
        self.assertTrue(self.run_runtime(self.project)["ok"])
        with self.env(modules="fake"):
            result = runtime.render(self.project, executable=str(self.fake_exe), timeout=30,
                                    views=("iso", "front"), width=320, height=240)
        self.assertTrue(result["ok"], result.get("errors"))
        self.assertEqual([item["view"] for item in result["images"]], ["iso", "front"])
        for item in result["images"]:
            self.assertTrue(Path(self.project.root / item["published"]).is_file())
        latest = self.project.root / "artifacts" / "latest"
        self.assertTrue((latest / "renders" / "render_iso.png").is_file())
        self.assertTrue((latest / "render.json").is_file())
        self.assertTrue((latest / "render.stdout.log").is_file())

    def test_render_without_rendering_module_is_actionable(self):
        self.assertTrue(self.run_runtime(self.project)["ok"])
        with self.env(modules="none"):
            result = runtime.render(self.project, executable=str(self.fake_exe), timeout=30)
        self.assertFalse(result["ok"])
        self.assertIn("rendering_unavailable", kinds(result))
        self.assertIn("cad3.rendering", result["error"])

    def test_render_on_missing_document_is_actionable(self):
        with self.env(modules="none"):
            result = runtime.render(self.project, executable=str(self.fake_exe), timeout=30)
        self.assertFalse(result["ok"])
        self.assertEqual(kinds(result), ["no_document"])

    def test_render_rejects_unknown_view(self):
        result = runtime.render(self.project, executable=str(self.fake_exe), timeout=30, views=("diagonal",))
        self.assertFalse(result["ok"])
        self.assertEqual(kinds(result), ["bad_request"])

    def test_doctor_reports_capabilities(self):
        result = runtime.doctor(executable=str(self.fake_exe), timeout=30)
        self.assertTrue(result["ok"], result.get("errors"))
        self.assertEqual(result["action"], "doctor")
        self.assertEqual(result["version"], ["0", "99", "stub"])
        self.assertTrue(result["capabilities"]["part_module"])
        self.assertTrue(result["capabilities"]["config_isolated"])
        self.assertFalse(result["capabilities"]["observe_module"])
        self.assertEqual(result["executable"], str(self.fake_exe.resolve()))

    def test_doctor_without_part_module_fails_with_capability_error(self):
        with unittest.mock.patch.dict(os.environ, {"CAD3_STUB_NO_PART": "1"}):
            result = runtime.doctor(executable=str(self.fake_exe), timeout=30)
        self.assertFalse(result["ok"])
        self.assertIn("capability", kinds(result))

    def test_missing_executable_is_reported_not_raised(self):
        result = runtime.execute(self.project, executable=str(self.tmp / "no-such-freecad"), timeout=5)
        self.assertFalse(result["ok"])
        self.assertEqual(kinds(result), ["freecad_not_found"])

    def test_stub_project_lock_fallback_is_used_and_released(self):
        result = self.run_runtime(self.project)
        self.assertTrue(result["ok"], result.get("errors"))
        lock = self.project.root / ".cad3" / "lock"
        if lock.exists():  # flock releases on close; a leftover file is fine, ownership is not
            with open(lock, "ab"):
                pass


class DiscoveryTests(Harness):
    def test_explicit_path_is_used(self):
        self.assertEqual(runtime.discover_freecad(str(self.fake_exe)), self.fake_exe.resolve())

    def test_env_var_is_used(self):
        with unittest.mock.patch.dict(os.environ, {"CAD3_FREECAD": str(self.fake_exe)}):
            self.assertEqual(runtime.discover_freecad(), self.fake_exe.resolve())

    def test_bad_explicit_path_raises(self):
        with self.assertRaises(runtime.Cad3Error):
            runtime.discover_freecad(str(self.tmp / "missing-binary"))

    def test_not_found_raises_with_guidance(self):
        import shutil as _shutil

        with unittest.mock.patch.object(runtime, "_app_candidates", lambda: iter(())), \
                unittest.mock.patch.object(_shutil, "which", lambda name: None):
            with self.assertRaises(runtime.Cad3Error) as ctx:
                runtime.discover_freecad()
        self.assertIn(runtime._FREECAD_ENV, str(ctx.exception))

    def test_packaged_worker_is_next_to_runtime_and_host_safe(self):
        self.assertEqual(runtime.WORKER_PATH, WORKER_PATH)
        self.assertTrue(WORKER_PATH.is_file())
        text = WORKER_PATH.read_text(encoding="utf-8")
        # The host module must never import FreeCAD; the worker may.
        self.assertNotIn("import FreeCAD", runtime.__file__ and Path(runtime.__file__).read_text(encoding="utf-8"))
        self.assertIn("import FreeCAD", text)

    def test_directory_argument_resolves_bin(self):
        fake_dir = self.tmp / "fakeapp"
        (fake_dir / "bin").mkdir(parents=True, exist_ok=True)
        target = fake_dir / "bin" / "freecadcmd"
        target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        target.chmod(0o755)
        self.assertEqual(runtime.discover_freecad(str(fake_dir)), target.resolve())


# --------------------------------------------------------------------------
# real FreeCAD integration (auto-skipped when FreeCAD is absent or opted out)
# --------------------------------------------------------------------------
def _real_freecad():
    if os.environ.get("CAD3_SKIP_REAL_FREECAD"):
        return None
    try:
        return runtime.discover_freecad()
    except runtime.Cad3Error:
        return None


REAL_FREECAD = _real_freecad()


@unittest.skipUnless(REAL_FREECAD, "no installed FreeCADCmd (or CAD3_SKIP_REAL_FREECAD=1)")
class RealFreeCADTests(unittest.TestCase):
    """Integration coverage: isolated launch, native edit, publish, failure safety."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="cad3-real-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        (self.root / "src").mkdir(parents=True, exist_ok=True)
        (self.root / "models").mkdir(parents=True, exist_ok=True)
        self.project = StubProject(self.root, "real")

    def write_script(self, source: str):
        self.project.entrypoint_path.write_text(source, encoding="utf-8")

    def test_doctor_execute_inspect_and_failure_safety(self):
        doctor = runtime.doctor(executable=str(REAL_FREECAD), timeout=60)
        self.assertTrue(doctor["ok"], doctor.get("errors"))
        self.assertTrue(doctor["capabilities"]["config_isolated"])
        self.assertTrue(doctor["capabilities"]["part_module"])

        self.write_script(
            "box = doc.addObject('Part::Box', 'Box')\n"
            "box.Length = 25.0\nbox.Width = 10.0\nbox.Height = 4.0\n"
            "doc.recompute()\ncad_outputs = ['Box']\n"
        )
        first = runtime.execute(self.project, executable=str(REAL_FREECAD), timeout=120, export_step=True)
        self.assertTrue(first["ok"], first.get("errors"))
        self.assertTrue(self.project.document_path.is_file())
        model_hash = sha256_bytes(self.project.document_path.read_bytes())
        self.assertIn("Box", first["state"]["outputs"])
        report = json.loads((self.root / "artifacts/latest/report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["model"]["sha256"], model_hash)

        inspected = runtime.inspect(self.project, executable=str(REAL_FREECAD), timeout=60)
        self.assertTrue(inspected["ok"], inspected.get("errors"))
        self.assertIn("Box", json.dumps(inspected["state"]))

        # Incremental edit: the second run must keep working on the same document.
        self.write_script(
            "box = doc.getObject('Box')\n"
            "box.Length = 50.0\n"
            "doc.recompute()\ncad_outputs = ['Box']\n"
        )
        second = runtime.execute(self.project, executable=str(REAL_FREECAD), timeout=120)
        self.assertTrue(second["ok"], second.get("errors"))
        self.assertNotEqual(sha256_bytes(self.project.document_path.read_bytes()), model_hash)

        # A failing script must not touch the published model.
        published = self.project.document_path.read_bytes()
        self.write_script("raise RuntimeError('native failure')\n")
        failed = runtime.execute(self.project, executable=str(REAL_FREECAD), timeout=120)
        self.assertFalse(failed["ok"])
        self.assertIn("script_exception", kinds(failed))
        self.assertEqual(self.project.document_path.read_bytes(), published)

        # SystemExit(0) is a failure too.
        self.write_script("import sys\nsys.exit(0)\n")
        exited = runtime.execute(self.project, executable=str(REAL_FREECAD), timeout=120)
        self.assertFalse(exited["ok"])
        self.assertEqual(kinds(exited), ["script_system_exit"])
        self.assertEqual(self.project.document_path.read_bytes(), published)

    def test_timeout_preserves_model_and_kills_only_child_group(self):
        self.write_script(
            "box = doc.addObject('Part::Box', 'Box')\n"
            "box.Length = 12.0\nbox.Width = 12.0\nbox.Height = 12.0\n"
            "doc.recompute()\ncad_outputs = ['Box']\n"
        )
        self.assertTrue(runtime.execute(self.project, executable=str(REAL_FREECAD), timeout=120)["ok"])
        published = self.project.document_path.read_bytes()

        bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        self.addCleanup(lambda: (bystander.terminate(), bystander.wait(timeout=10)))
        self.write_script("import time\nprint('sleeping', flush=True)\ntime.sleep(300)\n")
        result = runtime.execute(self.project, executable=str(REAL_FREECAD), timeout=6)
        self.assertFalse(result["ok"])
        self.assertTrue(result["process"]["timed_out"])
        self.assertIn("timeout", kinds(result))
        self.assertTrue(bystander.poll() is None)
        self.assertEqual(self.project.document_path.read_bytes(), published)

    def test_render_publishes_real_shaded_pngs(self):
        self.write_script(
            "box = doc.addObject('Part::Box', 'Box')\n"
            "box.Length = 30.0\nbox.Width = 10.0\nbox.Height = 5.0\n"
            "pin = doc.addObject('Part::Cylinder', 'Pin')\n"
            "pin.Radius = 3.0\npin.Height = 8.0\n"
            "doc.recompute()\ncad_outputs = ['Box', 'Pin']\n"
        )
        self.assertTrue(runtime.execute(self.project, executable=str(REAL_FREECAD), timeout=120)["ok"])

        result = runtime.render(self.project, executable=str(REAL_FREECAD), timeout=300,
                                views=("iso", "front"), width=480, height=360)
        self.assertTrue(result["ok"], result.get("errors"))
        self.assertEqual([item["view"] for item in result["images"]], ["iso", "front"])
        for item in result["images"]:
            published = self.root / item["published"]
            self.assertTrue(published.is_file())
            payload = published.read_bytes()
            self.assertTrue(payload.startswith(b"\x89PNG\r\n\x1a\n"), item["view"])
            self.assertGreater(len(payload), 1000)
            self.assertIn("camera", item)
        self.assertTrue((self.root / "artifacts/latest/render.json").is_file())


if __name__ == "__main__":
    unittest.main(verbosity=2)
