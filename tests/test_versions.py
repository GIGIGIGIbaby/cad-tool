"""Unit tests for cad3.project (storage/lock/path safety) and cad3.versions.

Standard library only, ``unittest``, real Git, and every project lives in a
fresh ``tempfile`` directory so no repository inside the source checkout is ever
touched.

Run with::

    python -m unittest discover -s tests -v
    python -m pytest tests/test_versions.py -q
"""

from __future__ import annotations

import importlib.util
import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path

HERE = Path(__file__).resolve()
SRC = HERE.parents[1] / "src"


def _load_modules() -> tuple[types.ModuleType, types.ModuleType]:
    """Import cad3.project/cad3.versions, with a direct-file fallback."""
    if str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))
    try:
        from cad3 import project as project_module  # type: ignore
        from cad3 import versions as versions_module  # type: ignore

        return project_module, versions_module
    except Exception:  # pragma: no cover - package scaffolding may be incomplete
        package = types.ModuleType("cad3")
        package.__path__ = [str(SRC / "cad3")]  # type: ignore[attr-defined]
        sys.modules.setdefault("cad3", package)
        loaded: dict[str, types.ModuleType] = {}
        for name in ("project", "versions"):
            location = SRC / "cad3" / f"{name}.py"
            spec = importlib.util.spec_from_file_location(f"cad3.{name}", location)
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            sys.modules[f"cad3.{name}"] = module
            spec.loader.exec_module(module)
            loaded[name] = module
        return loaded["project"], loaded["versions"]


cad3_project, cad3_versions = _load_modules()
Cad3Error = cad3_project.Cad3Error
Project = cad3_project.Project
project_lock = cad3_project.project_lock
status = cad3_versions.status
commit = cad3_versions.commit
log = cad3_versions.log
diff = cad3_versions.diff
restore = cad3_versions.restore

FAKE_FCSTD_V1 = b"\x00\x01FCStd\x00fake-v1\x00\xff\xfe" + bytes(range(256))
FAKE_FCSTD_V2 = b"\x00\x01FCStd\x00fake-v2\x00\x00\x01\x02" + bytes(range(255, -1, -1))


def _dead_pid() -> int:
    """A pid that is guaranteed not to be running any more."""
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()
    return process.pid


class ProjectTestCase(unittest.TestCase):
    """Base fixture: isolated temp tree, no global Git config, no identity."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="cad3-unit-")
        self.base = Path(self._tmp.name).resolve()
        self.home = self.base / "home"
        self.home.mkdir()
        self._env_backup = dict(os.environ)
        os.environ["HOME"] = str(self.home)
        os.environ["XDG_CONFIG_HOME"] = str(self.home / "xdg")
        os.environ["GIT_CONFIG_GLOBAL"] = str(self.home / ".gitconfig")
        os.environ["GIT_CONFIG_SYSTEM"] = os.devnull
        os.environ["GIT_CONFIG_NOSYSTEM"] = "1"
        for variable in ("CAD3_GIT_NAME", "CAD3_GIT_EMAIL", "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
            os.environ.pop(variable, None)
        self.project_dir = self.base / "project"

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env_backup)
        self._tmp.cleanup()

    # ---------------------------------------------------------------- helpers

    def git(
        self, *args: str, cwd: Path | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(cwd or self.project_dir), *args],
            capture_output=True,
            text=True,
            env=dict(os.environ),
        )

    def git_ok(self, *args: str, cwd: Path | None = None) -> str:
        completed = self.git(*args, cwd=cwd)
        self.assertEqual(
            completed.returncode, 0, f"git {' '.join(args)} failed: {completed.stderr}"
        )
        return completed.stdout

    def make_project(self, name: str = "demo", *, initial_commit: bool = False) -> Project:
        project = Project.init(self.project_dir, name=name)
        if initial_commit:
            result = commit(project, "initial")
            self.assertTrue(result["ok"], result)
        return project

    def write(self, project: Project, relative: str, data: bytes | str) -> Path:
        path = project.resolve(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, bytes):
            path.write_bytes(data)
        else:
            path.write_text(data, encoding="utf-8")
        return path

    def paths(self, *args: str) -> set[str]:
        output = self.git_ok("ls-files", "-z", *args)
        return {item for item in output.split("\0") if item}

    def tree_paths(self, revision: str = "HEAD") -> set[str]:
        """Paths committed in *revision* (not merely staged in the index)."""
        output = self.git_ok("ls-tree", "-r", "-z", "--name-only", revision)
        return {item for item in output.split("\0") if item}

    def status_paths(self) -> set[str]:
        output = self.git_ok("status", "--porcelain=v1", "-z")
        return {item[3:] for item in output.split("\0") if item}

    def head_hash(self, cwd: Path | None = None) -> str:
        return self.git_ok("rev-parse", "HEAD", cwd=cwd).strip()

    def change_map(self, project: Project) -> dict[str, dict[str, object]]:
        return {change["path"]: change for change in status(project)["changes"]}


# =============================================================== init / layout

class InitTests(ProjectTestCase):
    def test_init_creates_layout_manifest_and_own_repository(self) -> None:
        project = Project.init(self.project_dir, name="demo part")
        root = project.root
        self.assertEqual(root, Path(os.path.realpath(self.project_dir)))
        self.assertEqual(project.name, "demo part")

        config = json.loads((root / "cad3.json").read_text(encoding="utf-8"))
        self.assertEqual(config["schema"], "cad3.project/v1")
        self.assertEqual(config["name"], "demo part")
        self.assertEqual(config["entrypoint"], "src/main.py")
        self.assertEqual(config["document"], "models/main.FCStd")

        for directory in ("src", "inputs", "models", "artifacts", "artifacts/latest", "notes"):
            self.assertTrue((root / directory).is_dir(), directory)
        self.assertTrue((root / "src" / "main.py").is_file())
        self.assertEqual(project.entrypoint_path, root / "src" / "main.py")
        self.assertEqual(project.document_path, root / "models" / "main.FCStd")
        self.assertEqual(project.internal_dir, root / ".cad3")

        # The entrypoint template is native FreeCAD, never build123d/456d.
        template = (root / "src" / "main.py").read_text(encoding="utf-8")
        self.assertIn("import FreeCAD", template)
        self.assertNotIn("build123d", template)
        self.assertNotIn(".456d", template)

        # The internal dir is ignored, the manifest is not.
        gitignore = (root / ".gitignore").read_text(encoding="utf-8")
        self.assertIn(".cad3/", gitignore.split())
        ignored = self.git("check-ignore", "-q", ".cad3/runlog/x.json")
        self.assertEqual(ignored.returncode, 0, "the .cad3 dir must be Git-ignored")

        # It owns a real repository rooted exactly at the project.
        self.assertTrue((root / ".git").is_dir())
        self.assertEqual(self.git_ok("rev-parse", "--show-toplevel").strip(), str(root))

        # init creates no commit and stages nothing.
        self.assertEqual(self.git_ok("diff", "--cached", "--name-only"), "")
        self.assertEqual(log(project)["count"], 0)
        self.assertIsNone(status(project)["head"])

    def test_init_refuses_non_empty_directory_and_announces_it(self) -> None:
        self.project_dir.mkdir(parents=True)
        (self.project_dir / "keep.txt").write_text("keep", encoding="utf-8")
        with self.assertRaises(Cad3Error) as caught:
            Project.init(self.project_dir)
        self.assertIn("non-empty", str(caught.exception))
        self.assertTrue((self.project_dir / "keep.txt").is_file())
        self.assertFalse((self.project_dir / "cad3.json").exists())
        self.assertFalse((self.project_dir / ".git").exists())

        file_target = self.base / "plain-file"
        file_target.write_text("x", encoding="utf-8")
        with self.assertRaises(Cad3Error):
            Project.init(file_target)

    def test_init_accepts_an_existing_empty_directory(self) -> None:
        self.project_dir.mkdir(parents=True)
        project = Project.init(self.project_dir)
        self.assertEqual(project.root, Path(os.path.realpath(self.project_dir)))
        self.assertEqual(project.name, "project")  # falls back to the directory name

    def test_init_refuses_a_symlinked_target(self) -> None:
        real = self.base / "real"
        real.mkdir()
        link = self.base / "link"
        link.symlink_to(real, target_is_directory=True)
        with self.assertRaises(Cad3Error):
            Project.init(link)
        self.assertEqual(list(real.iterdir()), [])

    def test_init_refuses_to_inherit_a_parent_repository(self) -> None:
        outer = self.base / "outer"
        outer.mkdir()
        self.git_ok("init", "--quiet", "--initial-branch=main", cwd=outer)
        (outer / "readme.md").write_text("outer\n", encoding="utf-8")
        self.git_ok("add", "-A", "--", ".", cwd=outer)
        self.git_ok(
            "-c", "user.name=outer", "-c", "user.email=outer@example.test",
            "commit", "--quiet", "-m", "outer base", cwd=outer,
        )
        head_before = self.head_hash(cwd=outer)
        status_before = self.git_ok("status", "--porcelain=v1", cwd=outer)
        tracked_before = self.git_ok("ls-files", "-z", cwd=outer)

        nested = outer / "nested" / "project"
        with self.assertRaises(Cad3Error) as caught:
            Project.init(nested)
        self.assertIn("Git repository", str(caught.exception))
        self.assertFalse((outer / "nested").exists(), "init must not create anything")

        self.assertEqual(self.head_hash(cwd=outer), head_before)
        self.assertEqual(self.git_ok("status", "--porcelain=v1", cwd=outer), status_before)
        self.assertEqual(self.git_ok("ls-files", "-z", cwd=outer), tracked_before)


# ============================================================ load and resolve

class LoadAndResolveTests(ProjectTestCase):
    def test_load_finds_the_project_from_nested_paths_and_the_manifest(self) -> None:
        project = self.make_project(name="loaded")
        deep = project.root / "src" / "deep"
        deep.mkdir(parents=True)
        self.assertEqual(Project.load(deep).root, project.root)
        self.assertEqual(Project.load(project.root / "cad3.json").root, project.root)
        self.assertEqual(Project.load(project.root).name, "loaded")
        self.assertEqual(Project.load(str(project.root)).root, project.root)

    def test_load_rejects_missing_and_legacy_manifests(self) -> None:
        plain = self.base / "plain"
        plain.mkdir()
        with self.assertRaises(Cad3Error) as caught:
            Project.load(plain)
        self.assertIn("cad3.json", str(caught.exception))

        legacy = self.base / "part.456d"
        legacy.mkdir()
        (legacy / "manifest.json").write_text("{}", encoding="utf-8")
        (legacy / "design.md").write_text("legacy", encoding="utf-8")
        with self.assertRaises(Cad3Error):
            Project.load(legacy)

    def test_load_rejects_unknown_schema_and_broken_json(self) -> None:
        project = self.make_project()
        manifest = project.root / "cad3.json"
        manifest.write_text(json.dumps({"schema": "cad3.project/v0"}), encoding="utf-8")
        with self.assertRaises(Cad3Error) as caught:
            Project.load(project.root)
        self.assertIn("schema", str(caught.exception))
        manifest.write_text("{not json", encoding="utf-8")
        with self.assertRaises(Cad3Error):
            Project.load(project.root)

    def test_load_rejects_a_manifest_symlink_leaving_the_root(self) -> None:
        outside = self.base / "outside.json"
        outside.write_text(json.dumps({"schema": "cad3.project/v1"}), encoding="utf-8")
        project_dir = self.base / "symlinked"
        project_dir.mkdir()
        (project_dir / "cad3.json").symlink_to(outside)
        with self.assertRaises(Cad3Error):
            Project.load(project_dir)

    def test_load_rejects_traversing_configured_paths(self) -> None:
        project = self.make_project()
        manifest = project.root / "cad3.json"
        baseline = json.loads(manifest.read_text(encoding="utf-8"))
        variants = (
            {"document": "../../etc/passwd"},
            {"entrypoint": "/etc/passwd"},
            {"directories": ["src", "../../outside"]},
            {"directories": ["src", "/tmp"]},
            {"document": "models/../../outside/x.FCStd"},
        )
        for override in variants:
            with self.subTest(override=override):
                config = dict(baseline)
                config.update(override)
                manifest.write_text(json.dumps(config), encoding="utf-8")
                with self.assertRaises(Cad3Error):
                    Project.load(project.root)
        manifest.write_text(json.dumps(baseline), encoding="utf-8")
        self.assertEqual(Project.load(project.root).root, project.root)

    def test_resolve_accepts_plain_paths_and_rejects_escapes(self) -> None:
        project = self.make_project()
        self.assertEqual(
            project.resolve("models/main.FCStd"), project.root / "models" / "main.FCStd"
        )
        self.assertEqual(
            project.resolve("artifacts/latest/state.json"),
            project.root / "artifacts" / "latest" / "state.json",
        )

        outside = self.base / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("secret", encoding="utf-8")
        (project.root / "escape").symlink_to(outside, target_is_directory=True)

        bad = (
            "../outside/secret.txt",
            "src/../../outside/secret.txt",
            "..",
            ".",
            "",
            "/etc/passwd",
            "/tmp",
            "escape/secret.txt",
            "escape",
            "src/./../../x",
            "models/\x00bad",
        )
        for relative in bad:
            with self.subTest(relative=relative):
                with self.assertRaises(Cad3Error):
                    project.resolve(relative)
        with self.assertRaises(Cad3Error):
            project.resolve(None)  # type: ignore[arg-type]

    def test_resolve_allows_a_symlink_that_stays_inside_the_root(self) -> None:
        project = self.make_project()
        (project.root / "alias").symlink_to(project.root / "models", target_is_directory=True)
        resolved = project.resolve("alias/out.step")
        self.assertEqual(resolved, project.root / "alias" / "out.step")

    def test_versions_refuse_an_escaping_internal_dir(self) -> None:
        project = self.make_project(initial_commit=True)
        outside = self.base / "elsewhere"
        outside.mkdir()
        os.rename(project.internal_dir, outside / "cad3-internal")
        project.internal_dir.symlink_to(outside / "cad3-internal", target_is_directory=True)
        with self.assertRaises(Cad3Error):
            status(project)


# ====================================================================== locks

class LockTests(ProjectTestCase):
    def lock_path(self, project: Project) -> Path:
        return project.internal_dir / "lock"

    def test_lock_is_reentrant_and_released_on_exit_and_exception(self) -> None:
        project = self.make_project()
        lock_path = self.lock_path(project)
        with project_lock(project):
            self.assertTrue(lock_path.exists())
            with project_lock(project):  # nested acquisition must not deadlock
                self.assertTrue(lock_path.exists())
            self.assertTrue(lock_path.exists())
        self.assertFalse(lock_path.exists())

        with self.assertRaises(RuntimeError):
            with project_lock(project):
                raise RuntimeError("boom")
        self.assertFalse(lock_path.exists(), "ordinary exceptions must release the lock")

    def test_lock_is_shared_between_threads_without_deadlock(self) -> None:
        project = self.make_project()
        observed: list[str] = []
        started = threading.Event()
        release = threading.Event()

        def first() -> None:
            with project_lock(project, timeout=5.0):
                observed.append("first-in")
                started.set()
                release.wait(5.0)
                observed.append("first-out")

        def second() -> None:
            started.wait(5.0)
            with project_lock(project, timeout=5.0):
                observed.append("second-in")

        one = threading.Thread(target=first)
        two = threading.Thread(target=second)
        one.start()
        two.start()
        started.wait(5.0)
        time.sleep(0.2)
        self.assertEqual(observed, ["first-in"], "the second thread must wait")
        release.set()
        one.join(5.0)
        two.join(5.0)
        self.assertEqual(observed, ["first-in", "first-out", "second-in"])
        self.assertFalse(self.lock_path(project).exists())

    def test_live_lock_is_never_stolen_and_times_out(self) -> None:
        project = self.make_project()
        lock_path = self.lock_path(project)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            lock_path.write_text(
                json.dumps(
                    {
                        "token": "someone-else",
                        "pid": holder.pid,
                        "host": socket.gethostname(),
                        "created": "now",
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(Cad3Error) as caught:
                with project_lock(project, timeout=0.3, grace=0.0):
                    self.fail("a live lock must not be acquired")
            self.assertIn("timed out", str(caught.exception))
            self.assertTrue(lock_path.exists(), "a live lock must never be removed")
            with self.assertRaises(Cad3Error):
                with project_lock(project, timeout=0.3):
                    self.fail("the lock still belongs to another owner")
            self.assertTrue(lock_path.exists())
        finally:
            holder.kill()
            holder.wait()

    def test_lock_serialises_across_processes(self) -> None:
        project = self.make_project()
        worker = self.base / "lock_worker.py"
        worker.write_text(
            "import sys, time\n"
            f"sys.path.insert(0, {str(SRC)!r})\n"
            "from cad3.project import Cad3Error, Project, project_lock\n"
            "project = Project.load(sys.argv[1])\n"
            "mode = sys.argv[2]\n"
            "if mode == 'hold':\n"
            "    with project_lock(project, timeout=10):\n"
            "        print('held', flush=True)\n"
            "        time.sleep(2.5)\n"
            "    print('released', flush=True)\n"
            "else:\n"
            "    try:\n"
            "        with project_lock(project, timeout=0.4):\n"
            "            print('acquired', flush=True)\n"
            "    except Cad3Error:\n"
            "        print('blocked', flush=True)\n",
            encoding="utf-8",
        )
        env = dict(os.environ)
        holder = subprocess.Popen(
            [sys.executable, str(worker), str(project.root), "hold"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        try:
            self.assertEqual(holder.stdout.readline().strip(), "held")
            blocked = subprocess.run(
                [sys.executable, str(worker), str(project.root), "try"],
                capture_output=True,
                text=True,
                env=env,
                timeout=60,
            )
            self.assertEqual(blocked.stdout.strip(), "blocked", blocked.stderr)
            self.assertEqual(holder.stdout.readline().strip(), "released")
            holder.wait(timeout=60)
            after = subprocess.run(
                [sys.executable, str(worker), str(project.root), "try"],
                capture_output=True,
                text=True,
                env=env,
                timeout=60,
            )
            self.assertEqual(after.stdout.strip(), "acquired", after.stderr)
        finally:
            if holder.poll() is None:
                holder.kill()
                holder.wait()
            holder.stdout.close()
            holder.stderr.close()
        self.assertFalse((project.internal_dir / "lock").exists())

    def test_stale_lock_of_a_dead_owner_is_reclaimed_only_after_the_grace(self) -> None:
        project = self.make_project()
        lock_path = self.lock_path(project)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text(
            json.dumps(
                {
                    "token": "dead-owner",
                    "pid": _dead_pid(),
                    "host": socket.gethostname(),
                    "created": "long ago",
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaises(Cad3Error):
            with project_lock(project, timeout=0.2, grace=3600.0):
                self.fail("a fresh lock must not be reclaimed even if the pid is gone")
        self.assertTrue(lock_path.exists())

        old = time.time() - 7200.0
        os.utime(lock_path, (old, old))
        with project_lock(project, timeout=5.0, grace=10.0):
            self.assertTrue(lock_path.exists())
        self.assertFalse(lock_path.exists())

    def test_lock_of_a_foreign_host_is_never_reclaimed(self) -> None:
        project = self.make_project()
        lock_path = self.lock_path(project)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text(
            json.dumps({"token": "remote", "pid": 1, "host": "another-host.invalid"}),
            encoding="utf-8",
        )
        old = time.time() - 7200.0
        os.utime(lock_path, (old, old))
        with self.assertRaises(Cad3Error):
            with project_lock(project, timeout=0.2, grace=1.0):
                self.fail("a remote owner can never be proven dead")
        self.assertTrue(lock_path.exists())

    def test_lock_rejects_bad_arguments(self) -> None:
        project = self.make_project()
        with self.assertRaises(Cad3Error):
            project_lock("not a project")  # type: ignore[arg-type]
        with self.assertRaises(Cad3Error):
            project_lock(project, timeout=0)
        with self.assertRaises(Cad3Error):
            project_lock(project, timeout="soon")  # type: ignore[arg-type]


# ================================================== status / log / diff / locks

class StatusLogDiffTests(ProjectTestCase):
    def test_status_reports_unborn_repository_and_then_clean_head(self) -> None:
        project = self.make_project()
        before = status(project)
        self.assertTrue(before["ok"])
        self.assertTrue(before["unborn"])
        self.assertIsNone(before["head"])
        self.assertIsNone(before["short_head"])
        self.assertFalse(before["clean"])
        paths = {change["path"] for change in before["changes"]}
        self.assertIn("cad3.json", paths)
        self.assertIn("src/main.py", paths)
        self.assertTrue(all(change["untracked"] for change in before["changes"]))

        result = commit(project, "first commit")
        after = status(project)
        self.assertFalse(after["unborn"])
        self.assertEqual(after["head"], result["hash"])
        self.assertEqual(after["short_head"], result["hash"][:7])
        self.assertEqual(after["branch"], "main")
        self.assertFalse(after["detached"])
        self.assertTrue(after["clean"])
        self.assertEqual(after["counts"]["total"], 0)
        self.assertEqual(after["changes"], [])

    def test_ignored_internal_state_never_dirties_the_project(self) -> None:
        project = self.make_project(initial_commit=True)
        (project.internal_dir / "runlog" / "run-0001").mkdir(parents=True)
        (project.internal_dir / "runlog" / "run-0001" / "stdout.txt").write_text(
            "log", encoding="utf-8"
        )
        (project.internal_dir / "profiles" / "run-0001").mkdir(parents=True)
        (project.internal_dir / "profiles" / "run-0001" / "user.cfg").write_text(
            "cfg", encoding="utf-8"
        )

        current = status(project)
        self.assertTrue(current["clean"], current["changes"])
        self.assertEqual(current["counts"]["total"], 0)

        with_ignored = status(project, include_ignored=True)
        self.assertTrue(with_ignored["clean"])
        self.assertTrue(with_ignored["ignored"], "ignored state must be visible on demand")
        self.assertTrue(
            any(entry["path"].startswith(".cad3") for entry in with_ignored["ignored"])
        )
        self.assertEqual(with_ignored["counts"]["ignored"], len(with_ignored["ignored"]))

        result = commit(project, "should be a no-op")
        self.assertFalse(result["ok"])
        self.assertFalse(result["committed"])
        self.assertTrue(result["no_changes"])
        self.assertIn("nothing to commit", result["message"])
        self.assertNotIn(".cad3", self.paths())

    def test_log_is_empty_without_commits_and_ordered_newest_first(self) -> None:
        project = self.make_project()
        empty = log(project)
        self.assertTrue(empty["ok"])
        self.assertEqual(empty["count"], 0)
        self.assertEqual(empty["commits"], [])
        self.assertIsNone(empty["head"])
        self.assertEqual(empty["message"], "no commits yet")

        first = commit(project, "first")
        self.write(project, "src/app.py", "print('v2')\n")
        second = commit(project, "second")

        history = log(project)
        self.assertEqual(history["count"], 2)
        self.assertEqual([item["hash"] for item in history["commits"]], [second["hash"], first["hash"]])
        self.assertEqual(history["commits"][0]["subject"], "second")
        self.assertEqual(history["commits"][1]["subject"], "first")
        self.assertTrue(history["commits"][1]["root"])
        self.assertEqual(history["commits"][0]["parents"], [first["hash"]])
        self.assertEqual(history["commits"][0]["author_name"], "cad3")
        self.assertEqual(history["commits"][0]["author_email"], "cad3@localhost")

        limited = log(project, limit=1)
        self.assertEqual(limited["count"], 1)
        self.assertEqual(limited["commits"][0]["subject"], "second")

        for bad in (0, -1, "2", None, True, 10001):
            with self.subTest(limit=bad):
                with self.assertRaises(Cad3Error):
                    log(project, limit=bad)  # type: ignore[arg-type]

    def test_diff_reports_text_and_binary_changes_without_geometry_claims(self) -> None:
        project = self.make_project()
        self.write(project, "src/app.py", "value = 1\n")
        self.write(project, "models/main.FCStd", FAKE_FCSTD_V1)
        commit(project, "first")

        result = diff(project)
        self.assertTrue(result["ok"])
        self.assertTrue(result["clean"])
        self.assertEqual(result["files"], [])
        self.assertEqual(result["text"], "")

        self.write(project, "src/app.py", "value = 2\n")
        self.write(project, "models/main.FCStd", FAKE_FCSTD_V2)
        changed = diff(project)
        self.assertFalse(changed["clean"])
        self.assertIn("src/app.py", [item["path"] for item in changed["files"]])
        self.assertIn("models/main.FCStd", changed["binary_files"])
        self.assertNotIn("models/main.FCStd", result["binary_files"])
        self.assertIn("-value = 1", changed["text"])
        self.assertIn("+value = 2", changed["text"])
        self.assertIn("not a semantic geometry diff", changed["binary_note"])
        self.assertFalse(changed["truncated"])

        text_entry = next(item for item in changed["files"] if item["path"] == "src/app.py")
        self.assertEqual(text_entry["additions"], 1)
        self.assertEqual(text_entry["deletions"], 1)
        binary_entry = next(item for item in changed["files"] if item["path"] == "models/main.FCStd")
        self.assertTrue(binary_entry["binary"])
        self.assertIsNone(binary_entry["additions"])

        head = self.head_hash()
        revision = diff(project, head)
        self.assertEqual(revision["target"], head)
        self.assertFalse(revision["clean"])
        with self.assertRaises(Cad3Error):
            diff(project, "--upload-pack=evil")

    def test_diff_before_the_first_commit_uses_the_index(self) -> None:
        project = self.make_project()
        self.write(project, "src/app.py", "value = 1\n")
        result = diff(project)
        self.assertTrue(result["ok"])
        self.assertEqual(result["target"], "INDEX")
        self.assertIn("no commits yet", result["note"])

    def test_diff_surfaces_untracked_project_files_separately(self) -> None:
        project = self.make_project(initial_commit=True)
        self.assertTrue(diff(project)["clean"])

        self.write(project, "artifacts/latest/state.json", '{"outputs": []}\n')
        (project.root / "scratch.dat").write_text("unrelated", encoding="utf-8")
        result = diff(project)
        self.assertFalse(result["clean"], "an in-scope untracked file changes the next commit")
        self.assertEqual(result["files"], [], "Git's text diff cannot show untracked files")
        self.assertEqual(result["text"], "")
        self.assertEqual(result["untracked"], ["artifacts/latest/state.json"])
        self.assertEqual(result["untracked_out_of_scope"], ["scratch.dat"])
        self.assertIn("untracked project file", result["note"])

        commit(project, "evidence")
        self.assertTrue(diff(project)["clean"])
        self.assertEqual(diff(project)["untracked_out_of_scope"], ["scratch.dat"])


# ===================================================================== commit

class CommitTests(ProjectTestCase):
    def test_commit_snapshots_files_and_never_executes_the_entrypoint(self) -> None:
        project = self.make_project()
        sentinel = project.root / "entrypoint-ran.txt"
        self.write(
            project,
            "src/main.py",
            "from pathlib import Path\n"
            f"Path({str(sentinel)!r}).write_text('executed')\n",
        )
        self.write(project, "models/main.FCStd", FAKE_FCSTD_V1)
        self.write(project, "artifacts/latest/state.json", json.dumps({"ok": True}) + "\n")

        result = commit(project, "snapshot only")
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["committed"])
        self.assertFalse(sentinel.exists(), "commit must never run the modeling script")

        tracked = self.paths()
        for expected in (
            "cad3.json",
            ".gitignore",
            "src/main.py",
            "models/main.FCStd",
            "artifacts/latest/state.json",
        ):
            self.assertIn(expected, tracked)
        self.assertIn("models", result["scope"])
        self.assertIn("artifacts", result["scope"])
        self.assertIn("cad3.json", result["scope"])
        self.assertNotIn(".cad3", " ".join(tracked))

        committed_bytes = subprocess.run(
            ["git", "-C", str(project.root), "cat-file", "blob", f"{result['hash']}:models/main.FCStd"],
            capture_output=True,
            env=dict(os.environ),
        ).stdout
        self.assertEqual(committed_bytes, FAKE_FCSTD_V1)
        self.assertEqual(result["identity"]["name"], "cad3")
        self.assertTrue(result["identity"]["command_scoped"])

    def test_commit_keeps_artifacts_latest_evidence_under_version_control(self) -> None:
        project = self.make_project(initial_commit=True)
        self.write(project, "artifacts/latest/observation.json", '{"outputs": []}\n')
        self.write(project, "artifacts/latest/stderr.txt", "warning: nothing\n")
        result = commit(project, "publish evidence")
        self.assertTrue(result["committed"])
        self.assertIn("artifacts/latest/observation.json", self.paths())
        self.assertIn("artifacts/latest/stderr.txt", self.paths())
        self.assertEqual(
            {item["path"] for item in result["files"]},
            {"artifacts/latest/observation.json", "artifacts/latest/stderr.txt"},
        )

    def test_commit_refuses_when_there_is_nothing_to_do(self) -> None:
        project = self.make_project()
        first = commit(project, "first")
        self.assertTrue(first["committed"])
        again = commit(project, "again")
        self.assertFalse(again["ok"])
        self.assertFalse(again["committed"])
        self.assertTrue(again["no_changes"])
        self.assertEqual(again["head"], first["hash"])
        self.assertIn("nothing to commit", again["message"])
        self.assertIn(first["hash"][:7], again["message"])

    def test_commit_ignores_out_of_scope_untracked_files(self) -> None:
        project = self.make_project()
        scratch = project.root / "scratch.dat"
        scratch.write_text("not project content", encoding="utf-8")
        result = commit(project, "first")
        self.assertTrue(result["committed"])
        self.assertNotIn("scratch.dat", self.paths())
        self.assertIn("scratch.dat", self.status_paths())
        self.assertTrue(scratch.is_file())

    def test_commit_refuses_unknown_files_that_were_already_staged(self) -> None:
        project = self.make_project(initial_commit=True)
        head_before = self.head_hash()
        scratch = project.root / "scratch.dat"
        scratch.write_text("staged behind cad3's back", encoding="utf-8")
        self.git_ok("add", "--", "scratch.dat")

        with self.assertRaises(Cad3Error) as caught:
            commit(project, "must be refused")
        self.assertIn("outside the cad3 project scope", str(caught.exception))
        self.assertEqual(self.head_hash(), head_before, "HEAD must not move")
        self.assertIn("scratch.dat", self.git_ok("diff", "--cached", "--name-only"))
        self.assertNotIn("scratch.dat", self.tree_paths(), "it must not enter the commit")

    def test_commit_uses_a_command_scoped_identity_without_global_config(self) -> None:
        project = self.make_project()
        self.assertFalse((self.home / ".gitconfig").exists())
        result = commit(project, "identity")
        self.assertTrue(result["committed"])
        self.assertFalse((self.home / ".gitconfig").exists(), "global git config must stay absent")
        local = self.git("config", "--local", "--get", "user.name")
        self.assertNotEqual(local.returncode, 0, "identity must not be written to .git/config")
        entry = log(project)["commits"][0]
        self.assertEqual(entry["author_name"], "cad3")
        self.assertEqual(entry["author_email"], "cad3@localhost")

        os.environ["CAD3_GIT_NAME"] = "Env Person"
        os.environ["CAD3_GIT_EMAIL"] = "env@example.test"
        self.write(project, "src/app.py", "value = 1\n")
        second = commit(project, "env identity")
        self.assertEqual(second["identity"]["name"], "Env Person")
        self.assertTrue(second["identity"]["command_scoped"])
        self.assertEqual(log(project)["commits"][0]["author_name"], "Env Person")
        self.assertFalse((self.home / ".gitconfig").exists())
        self.assertNotEqual(
            self.git("config", "--local", "--get", "user.name").returncode, 0
        )

    def test_commit_does_not_execute_user_hooks(self) -> None:
        project = self.make_project()
        sentinel = self.base / "hook-ran.txt"
        hooks = project.root / ".git" / "hooks"
        hooks.mkdir(parents=True, exist_ok=True)
        for name in ("pre-commit", "post-commit", "commit-msg"):
            hook = hooks / name
            hook.write_text(
                "#!/bin/sh\n"
                f"echo ran > {sentinel}\n"
                "exit 1\n",
                encoding="utf-8",
            )
            hook.chmod(hook.stat().st_mode | stat.S_IXUSR)
        result = commit(project, "hooks must not run")
        self.assertTrue(result["committed"], result)
        self.assertFalse(sentinel.exists(), "user hooks must be disabled for cad3 commits")

    def test_commit_requires_a_usable_message(self) -> None:
        project = self.make_project()
        for bad in ("", "   ", None, 5, b"bytes"):
            with self.subTest(message=bad):
                with self.assertRaises(Cad3Error):
                    commit(project, bad)  # type: ignore[arg-type]
        with self.assertRaises(Cad3Error):
            commit(project, "nul\x00byte")
        self.assertEqual(log(project)["count"], 0)

    def test_commit_requires_a_project_instance(self) -> None:
        with self.assertRaises(Cad3Error):
            commit("not a project", "message")  # type: ignore[arg-type]


# ==================================================================== restore

class RestoreTests(ProjectTestCase):
    def _binary_history(self) -> tuple[Project, str, str]:
        project = self.make_project()
        self.write(project, "models/main.FCStd", FAKE_FCSTD_V1)
        self.write(project, "src/legacy.py", "legacy = 1\n")
        first = commit(project, "v1")
        self.assertTrue(first["committed"], first)

        self.write(project, "models/main.FCStd", FAKE_FCSTD_V2)
        self.write(project, "src/added_later.py", "added = 2\n")
        self.write(project, "src/legacy.py", "legacy = 2\n")
        second = commit(project, "v2")
        self.assertTrue(second["committed"], second)
        return project, first["hash"], second["hash"]

    def test_restore_is_byte_exact_and_removes_files_absent_from_the_target(self) -> None:
        project, first, second = self._binary_history()
        result = restore(project, first)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["target"], first)
        self.assertEqual(result["restored_from"], second)
        self.assertEqual(result["head"], second)
        self.assertFalse(result["head_moved"])
        self.assertEqual(result["verify_files"], [])

        self.assertEqual((project.root / "models" / "main.FCStd").read_bytes(), FAKE_FCSTD_V1)
        self.assertEqual((project.root / "src" / "legacy.py").read_text(), "legacy = 1\n")
        self.assertFalse((project.root / "src" / "added_later.py").exists())
        self.assertIn("src/added_later.py", result["removed"])

        # HEAD and the index are untouched: this is a worktree-only rollback.
        self.assertEqual(self.head_hash(), second)
        self.assertEqual(self.git_ok("diff", "--cached", "--name-only"), "")

        after = status(project)
        paths = {change["path"]: change for change in after["changes"]}
        self.assertIn("models/main.FCStd", paths)
        self.assertIn("src/legacy.py", paths)
        self.assertIn("src/added_later.py", paths)
        self.assertTrue(paths["src/added_later.py"]["deleted"])

        # The next commit records the rollback.
        rollback = commit(project, "roll back to v1")
        self.assertTrue(rollback["committed"], rollback)
        self.assertNotEqual(rollback["hash"], second)
        self.assertEqual(log(project)["count"], 3)
        self.assertEqual(log(project)["commits"][0]["subject"], "roll back to v1")
        self.assertEqual((project.root / "models" / "main.FCStd").read_bytes(), FAKE_FCSTD_V1)
        self.assertNotIn("src/added_later.py", self.paths())

    def test_restore_recreates_a_file_that_only_exists_in_the_target(self) -> None:
        project = self.make_project()
        self.write(project, "src/legacy.py", "legacy = 1\n")
        first = commit(project, "v1")
        self.write(project, "src/removed_later.py", "x = 1\n")
        self.git_ok("rm", "--quiet", "--", "src/legacy.py")
        second = commit(project, "v2")
        self.assertFalse((project.root / "src" / "legacy.py").exists())

        result = restore(project, first["hash"])
        self.assertTrue(result["ok"], result)
        self.assertEqual((project.root / "src" / "legacy.py").read_text(), "legacy = 1\n")
        self.assertFalse((project.root / "src" / "removed_later.py").exists())
        self.assertIn("src/removed_later.py", result["removed"])
        self.assertEqual(self.head_hash(), second["hash"])

    def test_restore_refuses_dirty_staged_and_untracked_project_content(self) -> None:
        project = self.make_project()
        self.write(project, "src/app.py", "value = 1\n")
        first = commit(project, "v1")
        self.write(project, "src/app.py", "value = 2\n")
        second = commit(project, "v2")

        # (a) modified tracked file
        self.write(project, "src/app.py", "value = 3\n")
        with self.assertRaises(Cad3Error) as caught:
            restore(project, first["hash"])
        self.assertIn("refusing to restore", str(caught.exception))
        self.assertEqual(self.head_hash(), second["hash"])
        self.assertEqual((project.root / "src" / "app.py").read_text(), "value = 3\n")

        # (b) staged change
        self.write(project, "src/app.py", "value = 3\n")
        self.git_ok("add", "--", "src/app.py")
        with self.assertRaises(Cad3Error):
            restore(project, first["hash"])
        self.assertEqual(self.head_hash(), second["hash"])
        self.assertIn("src/app.py", self.git_ok("diff", "--cached", "--name-only"))

        # (c) untracked project content, on an otherwise clean tree
        self.git_ok("reset", "--hard", "--quiet")
        self.assertEqual(status(project)["clean"], True)
        self.write(project, "src/brand_new.py", "new = True\n")
        with self.assertRaises(Cad3Error) as caught:
            restore(project, first["hash"])
        self.assertIn("src/brand_new.py", str(caught.exception))
        self.assertTrue((project.root / "src" / "brand_new.py").is_file())
        self.assertEqual(self.head_hash(), second["hash"])

    def test_restore_never_deletes_ignored_or_unrelated_content(self) -> None:
        project = self.make_project()
        self.write(project, "src/app.py", "value = 1\n")
        self.write(project, "models/main.FCStd", FAKE_FCSTD_V1)
        first = commit(project, "v1")
        self.write(project, "models/main.FCStd", FAKE_FCSTD_V2)
        second = commit(project, "v2")

        log_file = project.internal_dir / "runlog" / "run-0001" / "stdout.txt"
        log_file.parent.mkdir(parents=True, exist_ok=True)
        log_file.write_text("kept", encoding="utf-8")
        backup = project.root / "models" / "main.FCStd1"  # matched by .gitignore
        backup.write_bytes(b"freecad backup")
        outside = self.base / "unrelated.txt"
        outside.write_text("outside the project", encoding="utf-8")

        self.assertTrue(status(project)["clean"], status(project)["changes"])
        result = restore(project, first["hash"])
        self.assertTrue(result["ok"], result)
        self.assertTrue(log_file.is_file(), "ignored .cad3 run logs must survive a restore")
        self.assertTrue(backup.is_file(), "ignored FreeCAD backups must survive a restore")
        self.assertEqual(backup.read_bytes(), b"freecad backup")
        self.assertTrue(outside.is_file())
        self.assertEqual((project.root / "models" / "main.FCStd").read_bytes(), FAKE_FCSTD_V1)
        self.assertEqual(self.head_hash(), second["hash"])

    def test_restore_validates_the_revision_before_touching_anything(self) -> None:
        project = self.make_project(initial_commit=True)
        head = self.head_hash()
        bad = (
            "-D",
            "--force",
            "--hard",
            "HEAD; rm -rf .",
            "HEAD && echo pwned",
            "a b",
            "",
            "   ",
            "nope-deadbeef",
            "*",
            ":/etc",
            "x" * 400,
            None,
            5,
        )
        for revision in bad:
            with self.subTest(revision=revision):
                with self.assertRaises(Cad3Error):
                    restore(project, revision)  # type: ignore[arg-type]
        self.assertEqual(self.head_hash(), head)
        self.assertTrue(status(project)["clean"])

    def test_restore_to_head_is_a_clean_no_op(self) -> None:
        project = self.make_project(initial_commit=True)
        head = self.head_hash()
        result = restore(project, head[:7])
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["removed"], [])
        self.assertEqual(result["changed"], [])
        self.assertIn("nothing to restore", result["message"])
        self.assertEqual(self.head_hash(), head)

    def test_restore_refuses_a_committed_symlink_that_escapes_the_project(self) -> None:
        project = self.make_project()
        escape = project.root / "src" / "escape_link"
        escape.symlink_to("/etc")
        first = commit(project, "with escaping symlink")
        self.assertTrue(first["committed"], first)
        self.git_ok("rm", "--quiet", "--", "src/escape_link")
        second = commit(project, "without the symlink")

        with self.assertRaises(Cad3Error) as caught:
            restore(project, first["hash"])
        self.assertIn("src/escape_link", str(caught.exception))
        self.assertIn("outside the project root", str(caught.exception))
        self.assertFalse(
            os.path.lexists(escape), "an escaping symlink must not be materialised"
        )
        self.assertEqual(self.head_hash(), second["hash"])
        self.assertTrue(status(project)["clean"], "a refused restore must not touch the worktree")

    def test_commit_refuses_paths_the_project_ignores(self) -> None:
        project = self.make_project()
        self.write(project, "models/main.FCStd", FAKE_FCSTD_V1)
        gitignore = project.root / ".gitignore"
        gitignore.write_text(
            gitignore.read_text(encoding="utf-8") + "models/\n", encoding="utf-8"
        )
        with self.assertRaises(Cad3Error) as caught:
            commit(project, "cannot snapshot an ignored document")
        self.assertIn("models", str(caught.exception))
        self.assertEqual(log(project)["count"], 0)
        self.assertTrue((project.root / "models" / "main.FCStd").is_file())

    def test_commit_refuses_a_missing_manifest(self) -> None:
        project = self.make_project(initial_commit=True)
        (project.root / "cad3.json").unlink()
        with self.assertRaises(Cad3Error) as caught:
            commit(project, "would drop the manifest")
        self.assertIn("cad3.json", str(caught.exception))
        self.assertEqual(log(project)["count"], 1)

    def test_restore_requires_a_clean_project_instance(self) -> None:
        with self.assertRaises(Cad3Error):
            restore("not a project", "HEAD")  # type: ignore[arg-type]


# ================================================== repository ownership rules

class RepositoryOwnershipTests(ProjectTestCase):
    def test_version_operations_refuse_an_inherited_repository(self) -> None:
        outer = self.base / "host-repo"
        nested = outer / "nested"
        nested.mkdir(parents=True)
        (nested / "cad3.json").write_text(
            json.dumps(
                {
                    "schema": "cad3.project/v1",
                    "name": "nested",
                    "entrypoint": "src/main.py",
                    "document": "models/main.FCStd",
                    "directories": ["src", "models"],
                }
            ),
            encoding="utf-8",
        )
        (nested / ".gitignore").write_text(".cad3/\n", encoding="utf-8")
        (nested / "src").mkdir()
        (nested / "src" / "main.py").write_text("# native FreeCAD script\n", encoding="utf-8")
        (outer / "readme.md").write_text("host\n", encoding="utf-8")

        self.git_ok("init", "--quiet", "--initial-branch=main", cwd=outer)
        self.git_ok("add", "-A", "--", ".", cwd=outer)
        self.git_ok(
            "-c", "user.name=host", "-c", "user.email=host@example.test",
            "commit", "--quiet", "-m", "host base", cwd=outer,
        )
        head_before = self.head_hash(cwd=outer)
        count_before = self.git_ok("rev-list", "--count", "HEAD", cwd=outer).strip()
        status_before = self.git_ok("status", "--porcelain=v1", cwd=outer)
        self.assertEqual(status_before, "")

        project = Project.load(nested)
        self.assertEqual(project.root, Path(os.path.realpath(nested)))
        self.assertFalse((nested / ".git").exists())

        operations = (
            lambda: status(project),
            lambda: commit(project, "must not land in the host repo"),
            lambda: log(project),
            lambda: diff(project),
            lambda: restore(project, "HEAD"),
        )
        for operation in operations:
            with self.subTest(operation=operation):
                with self.assertRaises(Cad3Error) as caught:
                    operation()
                self.assertIn("inherited", str(caught.exception))

        self.assertEqual(self.head_hash(cwd=outer), head_before)
        self.assertEqual(self.git_ok("rev-list", "--count", "HEAD", cwd=outer).strip(), count_before)
        self.assertEqual(self.git_ok("status", "--porcelain=v1", cwd=outer), "")
        self.assertEqual(self.git_ok("ls-files", "-z", cwd=outer).count("cad3"), 1)

    def test_project_operations_leave_an_unrelated_repository_untouched(self) -> None:
        other = self.base / "other-repo"
        other.mkdir()
        self.git_ok("init", "--quiet", "--initial-branch=main", cwd=other)
        (other / "data.txt").write_text("other\n", encoding="utf-8")
        self.git_ok("add", "-A", "--", ".", cwd=other)
        self.git_ok(
            "-c", "user.name=other", "-c", "user.email=other@example.test",
            "commit", "--quiet", "-m", "other base", cwd=other,
        )
        other_head = self.head_hash(cwd=other)
        other_status = self.git_ok("status", "--porcelain=v1", cwd=other)
        other_tracked = self.git_ok("ls-files", "-z", cwd=other)
        other_config = (other / ".git" / "config").read_text(encoding="utf-8")

        project = self.make_project()
        self.write(project, "models/main.FCStd", FAKE_FCSTD_V1)
        first = commit(project, "first")
        self.write(project, "models/main.FCStd", FAKE_FCSTD_V2)
        self.assertFalse(diff(project)["clean"])
        commit(project, "second")
        self.assertEqual(log(project)["count"], 2)
        self.assertTrue(restore(project, first["hash"])["ok"], "restore must succeed")
        with project_lock(project):
            pass

        self.assertEqual(self.head_hash(cwd=other), other_head)
        self.assertEqual(self.git_ok("status", "--porcelain=v1", cwd=other), other_status)
        self.assertEqual(self.git_ok("ls-files", "-z", cwd=other), other_tracked)
        self.assertEqual((other / ".git" / "config").read_text(encoding="utf-8"), other_config)
        self.assertNotEqual(self.head_hash(), other_head)

    def test_a_project_without_its_repository_fails_clearly(self) -> None:
        project = self.make_project()
        import shutil

        shutil.rmtree(project.root / ".git")
        for operation in (
            lambda: status(project),
            lambda: commit(project, "no repo"),
            lambda: log(project),
            lambda: diff(project),
        ):
            with self.subTest(operation=operation):
                with self.assertRaises(Cad3Error) as caught:
                    operation()
                self.assertIn("not usable as a Git repository", str(caught.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
