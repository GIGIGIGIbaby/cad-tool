"""Storage owner for an ordinary cad3 project folder.

A cad3 project is a plain directory that contains ``cad3.json`` plus a real
Git repository.  There is no custom commit database and no compatibility with
the retired ``.456d`` package format: if there is no ``cad3.json`` with the
expected schema, this is not a cad3 project.

Host Python >= 3.11, standard library only.

Safety rules implemented here:

* every write is anchored to one explicit project root;
* :meth:`Project.init` refuses an existing non-empty directory, refuses to be
  created inside somebody else's Git work tree, and never adopts a parent
  repository;
* :meth:`Project.resolve` refuses absolute paths, ``..`` traversal and symlink
  parents that leave the project root;
* :func:`project_lock` serialises mutations, is reentrant inside a thread so
  nested callers cannot deadlock, is released on ordinary termination (also on
  exceptions) and only reclaims stale locks conservatively.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = [
    "Cad3Error",
    "Project",
    "project_lock",
    "SCHEMA",
    "CONFIG_FILENAME",
    "INTERNAL_DIRNAME",
    "DEFAULT_DIRECTORIES",
    "DEFAULT_DOCUMENT",
    "DEFAULT_ENTRYPOINT",
]

SCHEMA = "cad3.project/v1"
CONFIG_FILENAME = "cad3.json"
INTERNAL_DIRNAME = ".cad3"
DEFAULT_DIRECTORIES: tuple[str, ...] = ("src", "inputs", "models", "artifacts", "notes")
DEFAULT_DOCUMENT = "models/main.FCStd"
DEFAULT_ENTRYPOINT = "src/main.py"

LOCK_FILENAME = "lock"
LOCK_TIMEOUT_SECONDS = 30.0
LOCK_POLL_SECONDS = 0.05
#: A lock whose owner cannot be proven dead is never stolen.  When the owner
#: *is* provably gone (same host, dead pid) or the lock file is corrupt, the
#: file must still be at least this old before it is reclaimed.
LOCK_GRACE_SECONDS = 10.0
LOCK_CORRUPT_GRACE_SECONDS = 30.0

GITIGNORE_TEXT = """\
# cad3 internal state: run dirs, isolated profiles, locks (never committed)
.cad3/

# FreeCAD transient / lock files
*.FCStd1
*.FCStd.lock
.~lock.*#
*.FCBak
"""

ENTRYPOINT_TEMPLATE = '''\
"""cad3 entrypoint - a native FreeCAD script.

`cad3 run` executes this file inside FreeCAD.  The runtime has already opened
this project's configured document, so the script only ever modifies the
provided `doc`.  It must not create, open, or save a document itself: the
runtime recomputes, inspects and saves the candidate document after the script
returns.

Namespace provided by the runtime:

    App          FreeCAD application module
    doc          the live document for this project
    PROJECT_DIR  Path of the project root
    RUN_DIR      Path of this run's scratch directory
    __file__     this file, and __name__ == "__main__"

Prefer editing the provided document (or assigning another live native
document back to `doc` if you really need one).  Optional: append native
objects to `cad_outputs` to select what gets rendered/exported.
"""

import FreeCAD as App

BOX_LENGTH = 20.0
BOX_WIDTH = 20.0
BOX_HEIGHT = 20.0


def build(document):
    """Create a simple box in the provided document (idempotent by name)."""
    box = document.getObject("Box")
    if box is None:
        box = document.addObject("Part::Box", "Box")
    box.Label = "Box"
    box.Length = BOX_LENGTH
    box.Width = BOX_WIDTH
    box.Height = BOX_HEIGHT
    box.Placement.Base = App.Vector(0.0, 0.0, 0.0)
    document.recompute()
    return box


if __name__ == "__main__":
    _document = globals().get("doc")
    if _document is None:
        raise RuntimeError(
            "this script must be run by `cad3 run`: no document was provided "
            "in the namespace"
        )
    build(_document)
'''


class Cad3Error(Exception):
    """Common user-facing error for cad3 operations."""


def _is_within(path: str | os.PathLike[str], root: str | os.PathLike[str]) -> bool:
    """True when *path* is *root* or lives below it (both real paths)."""
    try:
        candidate = os.path.realpath(os.fspath(path))
        base = os.path.realpath(os.fspath(root))
    except (OSError, ValueError):
        return False
    if candidate == base:
        return True
    try:
        return os.path.commonpath([base, candidate]) == base
    except ValueError:
        return False


def _is_relative_lexically(relative: str) -> bool:
    """Reject absolute/UNC/drive paths and lexical ``..`` escapes."""
    if not relative or relative.strip() == "":
        return False
    if "\x00" in relative:
        return False
    path = Path(relative)
    if path.is_absolute() or path.drive:
        return False
    parts = Path(os.path.normpath(relative)).parts
    if not parts:
        return False
    if parts[0] == ".." or parts[0] in (os.sep, ".."):
        return False
    return True


def _escape_symlinks(candidate: Path, root: Path) -> str | None:
    """Return the offending path when a symlink component leaves *root*."""
    root_real = os.path.realpath(root)
    current = root
    for part in candidate.relative_to(root).parts:
        current = current / part
        if os.path.islink(current):
            real = os.path.realpath(current)
            if not _is_within(real, root_real):
                return str(current)
    return None


class Project:
    """An ordinary cad3 project folder."""

    def __init__(self, root: Path | str, config: dict[str, Any]) -> None:
        if not isinstance(config, dict):
            raise Cad3Error("project config must be a JSON object")
        self.root = Path(root)
        self.config: dict[str, Any] = dict(config)

    # ------------------------------------------------------------------ basic

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Project(root={str(self.root)!r}, name={self.name!r})"

    @property
    def name(self) -> str:
        value = self.config.get("name")
        if isinstance(value, str) and value.strip():
            return value.strip()
        return self.root.name

    @property
    def internal_dir(self) -> Path:
        """``<root>/.cad3`` - run dirs, isolated profiles, locks (ignored)."""
        return self.resolve(INTERNAL_DIRNAME)

    @property
    def document_path(self) -> Path:
        value = self.config.get("document")
        if not isinstance(value, str) or not value.strip():
            value = DEFAULT_DOCUMENT
        return self.resolve(value)

    @property
    def entrypoint_path(self) -> Path:
        value = self.config.get("entrypoint")
        if not isinstance(value, str) or not value.strip():
            value = DEFAULT_ENTRYPOINT
        return self.resolve(value)

    @property
    def directories(self) -> tuple[Path, ...]:
        raw = self.config.get("directories")
        if not isinstance(raw, list) or not raw:
            raw = list(DEFAULT_DIRECTORIES)
        resolved: list[Path] = []
        for entry in raw:
            if not isinstance(entry, str):
                raise Cad3Error(f"project directories must be strings, got {entry!r}")
            resolved.append(self.resolve(entry))
        return tuple(resolved)

    def ensure_internal_dir(self) -> Path:
        """Create ``.cad3`` (0700) and prove it really stays inside root."""
        internal = self.internal_dir
        if internal.exists() and not internal.is_dir():
            raise Cad3Error(f"internal path is not a directory: {internal}")
        try:
            internal.mkdir(mode=0o700, parents=False, exist_ok=True)
        except OSError as exc:
            raise Cad3Error(f"cannot create the project internal directory {internal}: {exc}") from exc
        if not _is_within(internal, self.root):
            raise Cad3Error(f"internal directory escapes the project root: {internal}")
        return internal

    # ---------------------------------------------------------------- resolve

    def resolve(self, relative: str) -> Path:
        """Resolve *relative* to an absolute path inside the project root.

        Refuses absolute paths, lexical ``..`` escapes and symlink parents that
        point outside the root.  The result need not exist yet.
        """
        if not isinstance(relative, str):
            raise Cad3Error(f"a relative project path (str) is required, got {type(relative).__name__}")
        if not _is_relative_lexically(relative):
            raise Cad3Error(f"refusing an unsafe project path: {relative!r}")
        root = self.root
        candidate = Path(os.path.normpath(os.path.join(str(root), relative)))
        try:
            candidate.relative_to(root)
        except ValueError:
            raise Cad3Error(f"refusing a project path that escapes {root}: {relative!r}") from None
        offending = _escape_symlinks(candidate, root)
        if offending is not None:
            raise Cad3Error(
                f"refusing a project path whose symlink parent leaves the project root: {offending}"
            )
        return candidate

    # ------------------------------------------------------------------- load

    @classmethod
    def load(cls, path: Path | str) -> "Project":
        """Load the nearest project at or above *path* (or from a file inside it)."""
        if path is None:
            raise Cad3Error("a project path is required")
        start = Path(os.fspath(path)).expanduser()
        if os.fspath(start) in ("", "."):
            start = Path.cwd()
        if not start.exists():
            raise Cad3Error(f"path does not exist: {start}")
        if start.is_file():
            if start.name != CONFIG_FILENAME:
                raise Cad3Error(
                    f"not a cad3 project file: {start} (expected a directory containing {CONFIG_FILENAME})"
                )
            start = start.parent
        try:
            start = Path(os.path.realpath(start))
        except OSError as exc:
            raise Cad3Error(f"cannot resolve project path {start}: {exc}") from exc
        if not start.is_dir():
            raise Cad3Error(f"not a directory: {start}")
        root: Path | None = None
        current = start
        while True:
            if (current / CONFIG_FILENAME).is_file():
                root = current
                break
            if current.parent == current:
                break
            current = current.parent
        if root is None:
            raise Cad3Error(
                f"no {CONFIG_FILENAME} found in {start} or any parent directory; "
                "this is not a cad3 project (legacy .456d packages are not supported)"
            )
        return cls._from_root(root)

    @classmethod
    def _from_root(cls, root: Path | str) -> "Project":
        root_path = Path(os.path.realpath(os.fspath(root)))
        if not root_path.is_dir():
            raise Cad3Error(f"project root is not a directory: {root_path}")
        config_path = root_path / CONFIG_FILENAME
        if os.path.islink(config_path) and not _is_within(os.path.realpath(config_path), root_path):
            raise Cad3Error(f"{CONFIG_FILENAME} is a symlink leaving the project root: {config_path}")
        if not config_path.is_file():
            raise Cad3Error(f"missing project manifest: {config_path}")
        try:
            text = config_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise Cad3Error(f"cannot read {config_path}: {exc}") from exc
        except UnicodeDecodeError as exc:
            raise Cad3Error(f"{config_path} is not valid UTF-8: {exc}") from exc
        try:
            config = json.loads(text)
        except json.JSONDecodeError as exc:
            raise Cad3Error(f"{config_path} is not valid JSON: {exc}") from exc
        if not isinstance(config, dict):
            raise Cad3Error(f"{config_path} must contain a JSON object")
        schema = config.get("schema")
        if schema != SCHEMA:
            raise Cad3Error(
                f"unsupported project schema in {config_path}: {schema!r} (expected {SCHEMA!r}); "
                "legacy .456d packages are not supported"
            )
        project = cls(root_path, config)
        # Validate configured paths eagerly so a bad manifest fails at load time.
        project.document_path  # noqa: B018 - validation
        project.entrypoint_path  # noqa: B018 - validation
        project.directories  # noqa: B018 - validation
        return project

    # ------------------------------------------------------------------- init

    @classmethod
    def init(cls, path: Path | str, name: str | None = None) -> "Project":
        """Create a new project in an absent or empty directory.

        Never initialises inside an existing Git work tree and never adopts a
        parent repository: the fresh project gets its own repository whose
        top-level directory is the project root.
        """
        if name is not None and (not isinstance(name, str) or not name.strip()):
            raise Cad3Error("project name must be a non-empty string")
        if path is None:
            raise Cad3Error("a target directory is required")
        raw = Path(os.fspath(path)).expanduser()
        if os.fspath(raw) in ("",):
            raise Cad3Error("a target directory is required")
        if raw.exists() and os.path.islink(raw):
            raise Cad3Error(f"refusing to initialise a project through a symlink: {raw}")
        target = Path(os.path.realpath(raw))

        if target.exists():
            if not target.is_dir():
                raise Cad3Error(f"target exists and is not a directory: {target}")
            entries = sorted(os.listdir(target))
            if entries:
                shown = ", ".join(entries[:5])
                more = "" if len(entries) <= 5 else f", ... (+{len(entries) - 5} more)"
                raise Cad3Error(
                    f"refusing to initialise in a non-empty directory: {target} "
                    f"(found {len(entries)} entries: {shown}{more})"
                )
        probe = target if target.exists() else _nearest_existing(target)
        if probe is None:  # pragma: no cover - defended by the parent walk
            raise Cad3Error(f"cannot find an existing parent directory for {target}")
        enclosing = _git_toplevel(probe)
        if enclosing is not None and Path(os.path.realpath(enclosing)) != target:
            raise Cad3Error(
                f"refusing to initialise a cad3 project inside an existing Git repository "
                f"({enclosing}); choose a directory outside that work tree so the project "
                "gets its own repository"
            )

        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise Cad3Error(f"cannot create project directory {target}: {exc}") from exc

        config: dict[str, Any] = {
            "schema": SCHEMA,
            "name": (name.strip() if isinstance(name, str) and name.strip() else target.name),
            "entrypoint": DEFAULT_ENTRYPOINT,
            "document": DEFAULT_DOCUMENT,
            "directories": list(DEFAULT_DIRECTORIES),
            "created": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        }
        _write_text(target / CONFIG_FILENAME, json.dumps(config, indent=2, ensure_ascii=False) + "\n")
        _write_text(target / ".gitignore", GITIGNORE_TEXT)
        entrypoint = target / DEFAULT_ENTRYPOINT
        entrypoint.parent.mkdir(parents=True, exist_ok=True)
        _write_text(entrypoint, ENTRYPOINT_TEMPLATE)
        for directory in DEFAULT_DIRECTORIES:
            if directory == "src":
                continue
            plain = target / directory
            plain.mkdir(parents=True, exist_ok=True)
            keep = plain / ".gitkeep"
            if not keep.exists():
                _write_text(keep, "")
        (target / "artifacts" / "latest").mkdir(parents=True, exist_ok=True)
        keep = target / "artifacts" / "latest" / ".gitkeep"
        if not keep.exists():
            _write_text(keep, "")

        _git_init(target)
        if not (target / ".git").is_dir():
            raise Cad3Error(f"git init did not create an own repository at {target}")
        toplevel = _git_toplevel(target)
        if toplevel is None or Path(os.path.realpath(toplevel)) != target:
            raise Cad3Error(
                f"refusing the new project at {target}: Git reports the work tree "
                f"{toplevel!r} instead of the project root"
            )
        return cls._from_root(target)


def _nearest_existing(path: Path) -> Path | None:
    current = path
    while True:
        if current.exists():
            return current
        if current.parent == current:
            return None
        current = current.parent


def _write_text(path: Path, text: str) -> None:
    try:
        path.write_text(text, encoding="utf-8")
    except OSError as exc:
        raise Cad3Error(f"cannot write {path}: {exc}") from exc


def _git_env() -> dict[str, str]:
    env = os.environ.copy()
    # Never let the caller's environment redirect Git at some other repository.
    for variable in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_NAMESPACE",
        "GIT_CEILING_DIRECTORIES",
    ):
        env.pop(variable, None)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_PAGER"] = "cat"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["LC_ALL"] = "C"
    return env


def _git_toplevel(directory: Path) -> str | None:
    """Return the enclosing Git work tree top level, or None when not in one."""
    try:
        completed = subprocess.run(
            ["git", "-C", str(directory), "--no-pager", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            env=_git_env(),
            timeout=60,
        )
    except FileNotFoundError as exc:
        raise Cad3Error(
            "the git executable was not found on PATH; cad3 requires a real Git installation"
        ) from exc
    except OSError as exc:
        raise Cad3Error(f"cannot run git: {exc}") from exc
    if completed.returncode != 0:
        return None
    out = completed.stdout.strip()
    return out or None


def _git_init(target: Path) -> None:
    attempts = (
        ["git", "-C", str(target), "init", "--quiet", "--initial-branch=main"],
        ["git", "-C", str(target), "init", "--quiet"],
    )
    last = None
    for command in attempts:
        try:
            completed = subprocess.run(
                command, capture_output=True, text=True, env=_git_env(), timeout=120
            )
        except FileNotFoundError as exc:
            raise Cad3Error(
                "the git executable was not found on PATH; cad3 requires a real Git installation"
            ) from exc
        except OSError as exc:
            raise Cad3Error(f"cannot run git: {exc}") from exc
        if completed.returncode == 0:
            return
        last = completed
    detail = (last.stderr or last.stdout or "").strip() if last is not None else ""
    raise Cad3Error(f"git init failed in {target}: {detail}")


# --------------------------------------------------------------------- locks

class _LockEntry:
    __slots__ = ("rlock", "owner", "depth", "token")

    def __init__(self) -> None:
        self.rlock = threading.RLock()
        self.owner: int | None = None
        self.depth = 0
        self.token: str | None = None


_STATE_GUARD = threading.Lock()
_STATE: dict[str, _LockEntry] = {}


def _lock_info(path: Path) -> dict[str, Any] | None:
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _lock_is_stale(path: Path, info: dict[str, Any] | None, grace: float) -> bool:
    """Conservative staleness test: never steal a lock we cannot prove dead."""
    try:
        age = time.time() - os.path.getmtime(path)
    except OSError:
        return False
    if info is None:
        # Unreadable/corrupt: only after a noticeably longer grace period.
        return age >= max(grace, LOCK_CORRUPT_GRACE_SECONDS)
    pid = info.get("pid")
    host = info.get("host")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return age >= max(grace, LOCK_CORRUPT_GRACE_SECONDS)
    if host != socket.gethostname():
        return False  # cannot prove a remote owner is gone
    if pid == os.getpid():
        return age >= grace  # a leftover from a crashed thread of this process
    if _pid_alive(pid):
        return False
    return age >= grace


def _describe_lock(info: dict[str, Any] | None) -> str:
    if not info:
        return "unreadable lock file"
    parts = []
    for key in ("pid", "host", "created", "thread"):
        if info.get(key) is not None:
            parts.append(f"{key}={info[key]}")
    return ", ".join(parts) or "unknown owner"


def _acquire_file_lock(path: Path, *, deadline: float, poll: float, grace: float) -> str:
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        raise Cad3Error(f"cannot create the lock directory {path.parent}: {exc}") from exc
    while True:
        token = uuid.uuid4().hex
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            info = _lock_info(path)
            if _lock_is_stale(path, info, grace):
                try:
                    os.unlink(path)
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    raise Cad3Error(f"cannot reclaim the stale project lock {path}: {exc}") from exc
                continue
            if time.monotonic() >= deadline:
                raise Cad3Error(
                    f"timed out waiting for the cad3 project lock {path} "
                    f"(held by {_describe_lock(info)}); another cad3 process is mutating "
                    "this project - wait for it or remove the lock file if you are sure "
                    "the owner is gone"
                )
            time.sleep(poll)
            continue
        except OSError as exc:
            raise Cad3Error(f"cannot create the project lock {path}: {exc}") from exc
        payload = json.dumps(
            {
                "token": token,
                "pid": os.getpid(),
                "host": socket.gethostname(),
                "thread": threading.get_ident(),
                "created": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            },
            sort_keys=True,
        ).encode("utf-8")
        try:
            os.write(fd, payload)
            os.fsync(fd)
        except OSError as exc:
            os.close(fd)
            try:
                os.unlink(path)
            except OSError:
                pass
            raise Cad3Error(f"cannot write the project lock {path}: {exc}") from exc
        os.close(fd)
        return token


def _release_file_lock(path: Path, token: str | None) -> None:
    if not token:
        return
    info = _lock_info(path)
    if info is not None and info.get("token") not in (None, token):
        return  # somebody else owns it now; leave their lock alone
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError:
        pass


class project_lock:
    """Context manager serialising mutations of one cad3 project.

    Shared contract for :mod:`cad3.versions` and the runtime.  Reentrant within
    the owning thread, so nested version operations cannot deadlock; other
    threads block and other processes wait on the lock file.  Released on
    ordinary exit, including exceptions.  It does not lock manual GUI edits.
    """

    def __init__(
        self,
        project: Project,
        *,
        timeout: float = LOCK_TIMEOUT_SECONDS,
        poll: float = LOCK_POLL_SECONDS,
        grace: float = LOCK_GRACE_SECONDS,
    ) -> None:
        if not isinstance(project, Project):
            raise Cad3Error("project_lock requires a Project instance")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise Cad3Error("lock timeout must be a positive number of seconds")
        self._project = project
        self._timeout = float(timeout)
        self._poll = max(0.001, float(poll))
        self._grace = max(0.0, float(grace))
        self._key: str | None = None
        self._entry: _LockEntry | None = None

    @property
    def path(self) -> Path:
        return self._project.internal_dir / LOCK_FILENAME

    def __enter__(self) -> "project_lock":
        internal = self._project.ensure_internal_dir()
        lock_path = internal / LOCK_FILENAME
        key = os.path.realpath(lock_path)
        me = threading.get_ident()
        with _STATE_GUARD:
            entry = _STATE.get(key)
            if entry is None:
                entry = _LockEntry()
                _STATE[key] = entry
            if entry.owner == me:
                entry.depth += 1
                self._key, self._entry = key, entry
                return self
        deadline = time.monotonic() + self._timeout
        if not entry.rlock.acquire(timeout=self._timeout):
            raise Cad3Error(
                f"timed out waiting for the cad3 project lock {lock_path} (another thread "
                "in this process holds it)"
            )
        try:
            token = _acquire_file_lock(
                lock_path,
                deadline=deadline,
                poll=self._poll,
                grace=self._grace,
            )
        except BaseException:
            entry.rlock.release()
            raise
        with _STATE_GUARD:
            entry.owner = me
            entry.depth = 1
            entry.token = token
        self._key, self._entry = key, entry
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        key, entry = self._key, self._entry
        self._key, self._entry = None, None
        if entry is None or key is None:
            return False
        with _STATE_GUARD:
            if entry.depth > 1:
                entry.depth -= 1
                return False
            token = entry.token
            entry.owner = None
            entry.depth = 0
            entry.token = None
        try:
            _release_file_lock(Path(key), token)
        finally:
            entry.rlock.release()
        return False
