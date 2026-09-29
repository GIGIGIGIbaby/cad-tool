"""Version snapshots for a cad3 project - real Git, not a private database.

Every function takes a :class:`cad3.project.Project` and drives the real ``git``
executable rooted at the project.  There is no custom commit store, no legacy
``.456d`` compatibility layer, and no push/global-config side effect.

Safety properties implemented here:

* every Git call is anchored with ``-C <project root>`` and the enclosing work
  tree must be the project root itself, so a parent repository is never used;
* hooks and the global attributes file are disabled for tool-generated
  commands, so committing/restoring cannot run a user hook or filter;
* revisions are validated (rejecting option-like strings) and resolved with
  ``--end-of-options`` before use;
* :func:`commit` snapshots the current files, never executes the project
  script, stages only the configured project scope, and refuses to absorb files
  that were already staged outside that scope;
* :func:`restore` refuses a dirty/staged/untracked project, moves neither HEAD
  nor the index, removes tracked files absent from the verified revision and
  never deletes unrelated or ignored files.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any, Iterable, Sequence

try:  # normal package import
    from cad3.project import (
        CONFIG_FILENAME,
        DEFAULT_DIRECTORIES,
        DEFAULT_DOCUMENT,
        DEFAULT_ENTRYPOINT,
        Cad3Error,
        Project,
        _is_within,
        project_lock,
    )
except ImportError:  # pragma: no cover - allows loading this file directly
    import importlib.util
    import sys

    _spec = importlib.util.spec_from_file_location(
        "cad3.project", Path(__file__).resolve().parent / "project.py"
    )
    if _spec is None or _spec.loader is None:  # pragma: no cover
        raise
    _project_module = importlib.util.module_from_spec(_spec)
    sys.modules.setdefault("cad3.project", _project_module)
    _spec.loader.exec_module(_project_module)
    CONFIG_FILENAME = _project_module.CONFIG_FILENAME
    DEFAULT_DIRECTORIES = _project_module.DEFAULT_DIRECTORIES
    DEFAULT_DOCUMENT = _project_module.DEFAULT_DOCUMENT
    DEFAULT_ENTRYPOINT = _project_module.DEFAULT_ENTRYPOINT
    Cad3Error = _project_module.Cad3Error
    Project = _project_module.Project
    _is_within = _project_module._is_within
    project_lock = _project_module.project_lock

__all__ = ["status", "commit", "log", "diff", "restore"]

GIT_TIMEOUT_SECONDS = 120.0
DIFF_TEXT_LIMIT = 200_000
MAX_REV_LENGTH = 200
PREVIEW_LIMIT = 5

CAD3_IDENTITY_NAME = "cad3"
CAD3_IDENTITY_EMAIL = "cad3@localhost"

HOOKS_DIRNAME = "hooks-disabled"
ATTRIBUTES_FILENAME = "no-attributes"

_GITIGNORE = ".gitignore"
_REV_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@{}^~+=-]*$")


# --------------------------------------------------------------------- plumbing

def _require_project(project: Any) -> Project:
    if not isinstance(project, Project):
        raise Cad3Error("this operation requires a cad3.project.Project instance")
    return project


def _git_env(overrides: dict[str, str] | None = None) -> dict[str, str]:
    env = os.environ.copy()
    # A caller-supplied GIT_DIR/GIT_WORK_TREE must never retarget our commands.
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
    env["GIT_ATTR_NOSYSTEM"] = "1"
    env["LC_ALL"] = "C"
    if overrides:
        env.update(overrides)
    return env


def _base_command(project: Project, extra_config: Sequence[str] = ()) -> list[str]:
    internal = project.internal_dir
    configs = [
        f"core.hooksPath={internal / HOOKS_DIRNAME}",
        f"core.attributesFile={internal / ATTRIBUTES_FILENAME}",
        "core.pager=cat",
        "advice.detachedHead=false",
        *extra_config,
    ]
    command = ["git", "-C", str(project.root), "--no-pager"]
    for item in configs:
        command += ["-c", item]
    return command


def _format_failure(args: Sequence[str], completed: "subprocess.CompletedProcess[str]") -> str:
    detail = (completed.stderr or completed.stdout or "").strip()
    if len(detail) > 800:
        detail = detail[:800] + " ..."
    subcommand = args[0] if args else "?"
    return f"git {subcommand} failed (exit {completed.returncode}): {detail}"


def _run_git(
    project: Project,
    args: Sequence[str],
    *,
    check: bool = True,
    config: Sequence[str] = (),
    env: dict[str, str] | None = None,
) -> "subprocess.CompletedProcess[str]":
    command = _base_command(project, config) + [str(item) for item in args]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="surrogateescape",
            cwd=str(project.root),
            env=_git_env(env),
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as exc:
        raise Cad3Error(
            "the git executable was not found on PATH; cad3 requires a real Git installation"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise Cad3Error(
            f"git {args[0] if args else '?'} timed out after {GIT_TIMEOUT_SECONDS:.0f}s"
        ) from exc
    except OSError as exc:
        raise Cad3Error(f"cannot run git: {exc}") from exc
    if check and completed.returncode != 0:
        raise Cad3Error(_format_failure(args, completed))
    return completed


def _ensure_own_repository(project: Project) -> None:
    """The project must own a repository rooted at the project directory."""
    completed = _run_git(project, ["rev-parse", "--show-toplevel"], check=False)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise Cad3Error(
            f"{project.root} is not usable as a Git repository ({detail}); a cad3 project "
            "owns its own repository - re-create it with cad3 init in an empty directory"
        )
    toplevel = Path(os.path.realpath(completed.stdout.strip()))
    if toplevel != project.root:
        raise Cad3Error(
            "refusing to use an inherited Git repository: Git resolves "
            f"{project.root} to the work tree {toplevel}; cad3 only manages a repository "
            "rooted at the project directory itself"
        )
    git_dir = project.root / ".git"
    if not git_dir.is_dir():
        raise Cad3Error(
            "refusing to use the project's repository: .git is not a directory (linked "
            "work tree or submodule); cad3 requires a repository whose .git lives inside "
            "the project root"
        )


def _head(project: Project) -> str | None:
    completed = _run_git(
        project, ["rev-parse", "--verify", "--quiet", "--end-of-options", "HEAD^{commit}"], check=False
    )
    value = completed.stdout.strip()
    return value or None


def _branch(project: Project) -> str | None:
    completed = _run_git(project, ["symbolic-ref", "--quiet", "--short", "HEAD"], check=False)
    value = completed.stdout.strip()
    return value or None


def _preview(items: Iterable[str], limit: int = PREVIEW_LIMIT) -> str:
    values = list(items)
    shown = ", ".join(values[:limit])
    if len(values) > limit:
        shown += f", ... (+{len(values) - limit} more)"
    return shown or "none"


# ------------------------------------------------------------------ revisions

def _check_revision(revision: Any, *, allow_range: bool = False) -> str:
    """Validate an untrusted revision string before it reaches Git."""
    if not isinstance(revision, str):
        raise Cad3Error(f"a revision string is required, got {type(revision).__name__}")
    value = revision.strip()
    if not value:
        raise Cad3Error("a revision is required")
    if len(value) > MAX_REV_LENGTH:
        raise Cad3Error(f"revision is too long ({len(value)} > {MAX_REV_LENGTH} characters)")
    candidates = [value]
    if allow_range and ("..." in value or ".." in value):
        separator = "..." if "..." in value else ".."
        candidates = value.split(separator)
        if len(candidates) != 2 or not all(part.strip() for part in candidates):
            raise Cad3Error(f"unusable revision range: {value!r}")
    for candidate in candidates:
        if candidate.startswith("-"):
            raise Cad3Error(
                f"refusing a revision that looks like a command-line option: {candidate!r}"
            )
        if not _REV_PATTERN.match(candidate):
            raise Cad3Error(f"refusing an unsafe revision: {candidate!r}")
    return value


def _verify_revision(project: Project, revision: str) -> str:
    completed = _run_git(
        project,
        ["rev-parse", "--verify", "--quiet", "--end-of-options", f"{revision}^{{commit}}"],
        check=False,
    )
    resolved = completed.stdout.strip()
    if completed.returncode != 0 or not resolved:
        raise Cad3Error(f"unknown revision: {revision!r} (not a commit in this project's history)")
    return resolved


def _tree_names(project: Project, revision: str) -> list[str]:
    completed = _run_git(
        project, ["ls-tree", "-r", "-z", "--name-only", "--full-tree", revision]
    )
    return [name for name in completed.stdout.split("\0") if name]


def _index_names(project: Project) -> list[str]:
    completed = _run_git(project, ["ls-files", "-z", "--cached", "--full-name"])
    return [name for name in completed.stdout.split("\0") if name]


# --------------------------------------------------------------------- status

def _parse_porcelain(text: str) -> list[dict[str, Any]]:
    fields = text.split("\0")
    entries: list[dict[str, Any]] = []
    index = 0
    while index < len(fields):
        record = fields[index]
        index += 1
        if not record:
            continue
        if len(record) < 3 or record[2] != " ":
            raise Cad3Error(f"unexpected git status entry: {record!r}")
        index_status = record[0]
        worktree_status = record[1]
        path = record[3:]
        original: str | None = None
        if index_status in ("R", "C") and index < len(fields):
            original = fields[index]
            index += 1
        untracked = index_status == "?" and worktree_status == "?"
        ignored = index_status == "!" and worktree_status == "!"
        conflicted = (
            index_status == "U"
            or worktree_status == "U"
            or (index_status, worktree_status) in {("A", "A"), ("D", "D")}
        )
        entries.append(
            {
                "path": path,
                "original_path": original,
                "index_status": index_status,
                "worktree_status": worktree_status,
                "staged": (not untracked) and (not ignored) and index_status != " ",
                "modified": (not untracked) and (not ignored) and worktree_status != " ",
                "deleted": index_status == "D" or worktree_status == "D",
                "untracked": untracked,
                "ignored": ignored,
                "conflicted": conflicted,
                "status": f"{index_status}{worktree_status}",
            }
        )
    return entries


def _porcelain(project: Project, include_ignored: bool) -> list[dict[str, Any]]:
    args = ["status", "--porcelain=v1", "-z", "--untracked-files=all"]
    args.append("--ignored=matching" if include_ignored else "--ignored=no")
    completed = _run_git(project, args)
    return _parse_porcelain(completed.stdout)


def status(project: Project, *, include_ignored: bool = False) -> dict[str, Any]:
    """Current HEAD/branch plus the working-tree changes of actual files."""
    _require_project(project)
    _ensure_own_repository(project)
    head = _head(project)
    branch = _branch(project)
    entries = _porcelain(project, include_ignored)
    changes = [entry for entry in entries if not entry["ignored"]]
    ignored = [entry for entry in entries if entry["ignored"]]
    counts = {
        "staged": sum(1 for entry in changes if entry["staged"]),
        "modified": sum(1 for entry in changes if entry["modified"] and not entry["staged"]),
        "untracked": sum(1 for entry in changes if entry["untracked"]),
        "conflicted": sum(1 for entry in changes if entry["conflicted"]),
        "ignored": len(ignored),
        "total": len(changes),
    }
    return {
        "ok": True,
        "root": str(project.root),
        "head": head,
        "short_head": head[:7] if head else None,
        "branch": branch,
        "detached": bool(head and not branch),
        "unborn": head is None,
        "clean": not changes,
        "changes": changes,
        "ignored": ignored,
        "counts": counts,
    }


# -------------------------------------------------------------------- staging

def _scope_paths(project: Project) -> list[str]:
    """Relative paths that a cad3 commit is allowed to snapshot."""
    raw: list[str] = [CONFIG_FILENAME, _GITIGNORE]
    for key, default in (("document", DEFAULT_DOCUMENT), ("entrypoint", DEFAULT_ENTRYPOINT)):
        value = project.config.get(key) or default
        raw.append(str(value))
    directories = project.config.get("directories")
    if not isinstance(directories, list) or not directories:
        directories = list(DEFAULT_DIRECTORIES)
    raw.extend(str(item) for item in directories)
    scope: list[str] = []
    for item in raw:
        resolved = project.resolve(item)  # refuses escapes and symlink escapes
        relative = resolved.relative_to(project.root).as_posix()
        if relative and relative not in scope:
            scope.append(relative)
    return scope


def _in_scope(path: str, scope: Sequence[str]) -> bool:
    return any(path == entry or path.startswith(entry + "/") for entry in scope)


def _staged_outside_scope(project: Project) -> list[str]:
    head = _head(project)
    args = ["diff", "--cached", "--name-only", "-z", "--no-renames"]
    if head:
        args.append(head)
    completed = _run_git(project, args)
    staged = [name for name in completed.stdout.split("\0") if name]
    scope = _scope_paths(project)
    return [name for name in staged if not _in_scope(name, scope)]


def _index_is_clean(project: Project) -> bool:
    head = _head(project)
    args = ["diff", "--cached", "--quiet"]
    if head:
        args.append(head)
    completed = _run_git(project, args, check=False)
    if completed.returncode == 0:
        return True
    if completed.returncode == 1:
        return False
    raise Cad3Error(_format_failure(["diff", "--cached"], completed))


def _staging_pathspecs(project: Project, scope: Sequence[str]) -> list[str]:
    """Keep scope entries that exist on disk or match something already tracked."""
    tracked: set[str] = set()
    for name in _index_names(project):
        tracked.add(name)
    head = _head(project)
    if head:
        tracked.update(_tree_names(project, head))
    keep: list[str] = []
    for entry in scope:
        candidate = project.root / entry
        if os.path.lexists(candidate):
            keep.append(entry)
            continue
        if any(name == entry or name.startswith(entry + "/") for name in tracked):
            keep.append(entry)
    return keep


def _git_config_value(project: Project, key: str) -> str | None:
    completed = _run_git(project, ["config", "--get", key], check=False)
    if completed.returncode != 0:
        return None
    value = completed.stdout.strip()
    return value or None


def _resolve_identity(project: Project) -> tuple[tuple[str, str], bool]:
    """Return ((name, email), needs_command_scoped_config).

    Nothing is ever written to any Git config.  When the repository/global
    configuration lacks an identity (or the CAD3_GIT_NAME/CAD3_GIT_EMAIL
    overrides are used), the effective identity is applied to this single
    command with ``-c``.
    """
    configured_name = _git_config_value(project, "user.name")
    configured_email = _git_config_value(project, "user.email")
    if configured_name and configured_email:
        return (configured_name, configured_email), False
    name = os.environ.get("CAD3_GIT_NAME") or configured_name or CAD3_IDENTITY_NAME
    email = os.environ.get("CAD3_GIT_EMAIL") or configured_email or CAD3_IDENTITY_EMAIL
    return (name, email), True


def _commit_files(project: Project, revision: str) -> list[dict[str, str]]:
    completed = _run_git(
        project,
        [
            "diff-tree",
            "--root",
            "--no-commit-id",
            "--name-status",
            "-r",
            "-z",
            "--no-renames",
            revision,
        ],
    )
    fields = [item for item in completed.stdout.split("\0")]
    files: list[dict[str, str]] = []
    index = 0
    while index + 1 < len(fields):
        code = fields[index]
        path = fields[index + 1]
        index += 2
        if not code or not path:
            continue
        files.append({"status": code, "path": path})
    return files


def commit(project: Project, message: str) -> dict[str, Any]:
    """Snapshot the actual project files.  Never re-runs the modeling script."""
    _require_project(project)
    if not isinstance(message, str) or not message.strip():
        raise Cad3Error("a non-empty commit message is required")
    if "\x00" in message:
        raise Cad3Error("the commit message must not contain NUL characters")
    _ensure_own_repository(project)
    with project_lock(project):
        _ensure_own_repository(project)  # re-check under the lock: no .git swap race
        if not (project.root / CONFIG_FILENAME).is_file():
            raise Cad3Error(
                f"refusing to commit: the project manifest {CONFIG_FILENAME} is missing; "
                "restore or re-create it first so the committed revision stays a cad3 project"
            )
        head_before = _head(project)
        outside = _staged_outside_scope(project)
        if outside:
            raise Cad3Error(
                "refusing to commit: the Git index already contains staged paths outside the "
                f"cad3 project scope ({_preview(outside)}); unstage them first "
                "(git reset -- <path>) - cad3 never absorbs unknown staged files"
            )
        scope = _scope_paths(project)
        pathspecs = _staging_pathspecs(project, scope)
        if pathspecs:
            _run_git(
                project,
                ["add", "-A", "--"]
                + [f":(literal,top){entry}" for entry in pathspecs],
            )
        if _index_is_clean(project):
            if head_before:
                outcome = f"nothing to commit: the project files already match HEAD ({head_before[:7]})"
            else:
                outcome = "nothing to commit: there are no commits yet and no project files to snapshot"
            return {
                "ok": False,
                "committed": False,
                "no_changes": True,
                "root": str(project.root),
                "head": head_before,
                "branch": _branch(project),
                "scope": scope,
                "message": outcome,
            }
        (name, email), used_fallback = _resolve_identity(project)
        config: list[str] = []
        if used_fallback:
            config = [f"user.name={name}", f"user.email={email}"]
        _run_git(project, ["commit", "--no-verify", "--quiet", "-m", message], config=config)
        head_after = _head(project)
        if not head_after:
            raise Cad3Error("git reported success but the project has no HEAD commit")
        files = _commit_files(project, head_after)
        return {
            "ok": True,
            "committed": True,
            "no_changes": False,
            "root": str(project.root),
            "hash": head_after,
            "short_hash": head_after[:7],
            "head": head_after,
            "previous_head": head_before,
            "branch": _branch(project),
            "files": files,
            "file_count": len(files),
            "scope": scope,
            "identity": {
                "name": name,
                "email": email,
                "command_scoped": used_fallback,
                "source": "command-scoped cad3 identity" if used_fallback else "configured git identity",
            },
            "message": f"committed {len(files)} file(s) as {head_after[:7]}",
        }


# ------------------------------------------------------------------------ log

_LOG_FORMAT = "%H%x1f%h%x1f%an%x1f%ae%x1f%aI%x1f%P%x1f%s"


def log(project: Project, limit: int = 10) -> dict[str, Any]:
    """Commit history, newest first; empty history is a normal outcome."""
    _require_project(project)
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise Cad3Error("limit must be an integer")
    if limit < 1 or limit > 10_000:
        raise Cad3Error("limit must be between 1 and 10000")
    _ensure_own_repository(project)
    head = _head(project)
    branch = _branch(project)
    if head is None:
        return {
            "ok": True,
            "root": str(project.root),
            "head": None,
            "branch": branch,
            "unborn": True,
            "count": 0,
            "commits": [],
            "message": "no commits yet",
        }
    completed = _run_git(
        project,
        ["log", f"--max-count={limit}", "-z", f"--format={_LOG_FORMAT}"],
    )
    commits: list[dict[str, Any]] = []
    for record in completed.stdout.split("\0"):
        if not record:
            continue
        parts = record.split("\x1f")
        if len(parts) < 7:
            continue
        parents = [item for item in parts[5].split(" ") if item]
        commits.append(
            {
                "hash": parts[0],
                "short_hash": parts[1],
                "author_name": parts[2],
                "author_email": parts[3],
                "authored_at": parts[4],
                "parents": parents,
                "subject": "\x1f".join(parts[6:]).strip(),
                "root": not parents,
            }
        )
    return {
        "ok": True,
        "root": str(project.root),
        "head": head,
        "short_head": head[:7],
        "branch": branch,
        "unborn": False,
        "count": len(commits),
        "commits": commits,
    }


# ----------------------------------------------------------------------- diff

def _parse_numstat(text: str) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    for record in text.split("\0"):
        if not record:
            continue
        parts = record.split("\t", 2)
        if len(parts) != 3:
            continue
        added, removed, path = parts
        binary = added == "-" or removed == "-"
        files.append(
            {
                "path": path,
                "additions": None if binary else int(added),
                "deletions": None if binary else int(removed),
                "binary": binary,
            }
        )
    return files


def diff(project: Project, revision: str | None = None) -> dict[str, Any]:
    """Text/binary diff of the actual files.  Not a semantic geometry diff."""
    _require_project(project)
    _ensure_own_repository(project)
    head = _head(project)
    if revision is None:
        if head is None:
            selector: list[str] = ["--cached"]
            target = "INDEX"
            note = "no commits yet: showing the index against the empty tree"
        else:
            selector = [head]
            target = "WORKTREE"
            note = "working tree against HEAD"
    else:
        value = _check_revision(revision, allow_range=True)
        selector = [value]
        target = value
        note = f"working tree against {value}"
    common = ["--no-color", "--no-ext-diff", "--no-textconv", "--no-renames"]
    numstat = _run_git(project, ["diff", "--numstat", "-z", *common, *selector])
    files = _parse_numstat(numstat.stdout)
    text_diff = _run_git(project, ["diff", *common, *selector])
    body = text_diff.stdout
    truncated = len(body) > DIFF_TEXT_LIMIT
    if truncated:
        body = body[:DIFF_TEXT_LIMIT]
    binary_files = [entry["path"] for entry in files if entry["binary"]]

    # Untracked project files are not part of a Git text diff, but a cad3 commit
    # would snapshot the ones inside the project scope: surface them explicitly.
    scope = _scope_paths(project)
    untracked_in_scope: list[str] = []
    untracked_out_of_scope: list[str] = []
    for entry in _porcelain(project, False):
        if not entry["untracked"]:
            continue
        if _in_scope(entry["path"], scope):
            untracked_in_scope.append(entry["path"])
        else:
            untracked_out_of_scope.append(entry["path"])
    if untracked_in_scope:
        note = (
            f"{note}; {len(untracked_in_scope)} untracked project file(s) are not in the Git "
            f"text diff but a cad3 commit would snapshot them ({_preview(untracked_in_scope)})"
        )
    return {
        "ok": True,
        "root": str(project.root),
        "revision": revision,
        "target": target,
        "head": head,
        "clean": not files and not untracked_in_scope,
        "files": files,
        "file_count": len(files),
        "binary_files": binary_files,
        "text": body,
        "truncated": truncated,
        "untracked": untracked_in_scope,
        "untracked_out_of_scope": untracked_out_of_scope,
        "note": note,
        "binary_note": (
            "Binary content is compared byte-wise by Git; a binary diff is not a semantic "
            "geometry diff."
        ),
    }


# -------------------------------------------------------------------- restore

def _object_format(project: Project) -> str:
    completed = _run_git(project, ["rev-parse", "--show-object-format"], check=False)
    value = completed.stdout.strip()
    if completed.returncode == 0 and value in ("sha1", "sha256"):
        return value
    return "sha1"


def _ls_tree_entries(project: Project, revision: str) -> list[tuple[str, str, str]]:
    """(mode, object id, path) for every entry of *revision*'s tree."""
    completed = _run_git(project, ["ls-tree", "-r", "-z", revision])
    entries: list[tuple[str, str, str]] = []
    for record in completed.stdout.split("\0"):
        if not record:
            continue
        try:
            meta, path = record.split("\t", 1)
        except ValueError:
            continue
        fields = meta.split()
        if len(fields) != 3 or not path:
            continue
        entries.append((fields[0], fields[2], path))
    return entries


def _worktree_matches_tree(project: Project, revision: str) -> list[str]:
    """Paths whose worktree bytes/type differ from *revision*'s tree.

    A byte-wise comparison against the committed blobs (the same precision Git
    uses internally), not a semantic CAD comparison; project-level checkout
    filters can therefore legitimately be reported as a mismatch.
    """
    import hashlib

    algorithm = _object_format(project)
    mismatches: list[str] = []
    for mode, expected, path in _ls_tree_entries(project, revision):
        if mode == "160000":  # gitlink/submodule: nothing to hash in the worktree
            continue
        if path.startswith("/") or ".." in path.split("/"):
            mismatches.append(path)
            continue
        candidate = project.root / path
        try:
            if mode == "120000":
                if not os.path.islink(candidate):
                    mismatches.append(path)
                    continue
                payload = os.fsencode(os.readlink(candidate))
                actual = hashlib.new(algorithm, b"blob %d\0" % len(payload) + payload).hexdigest()
            else:
                if os.path.islink(candidate) or not os.path.isfile(candidate):
                    mismatches.append(path)
                    continue
                digest = hashlib.new(algorithm)
                digest.update(b"blob %d\0" % os.path.getsize(candidate))
                with open(candidate, "rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
                actual = digest.hexdigest()
        except OSError:
            mismatches.append(path)
            continue
        if actual != expected:
            mismatches.append(path)
    return mismatches


def _escaping_symlinks(project: Project, revision: str | None) -> list[str]:
    """Tracked symlinks of *revision* whose target leaves the project root."""
    if not revision:
        return []
    root_real = os.path.realpath(project.root)
    unsafe: list[str] = []
    for mode, oid, path in _ls_tree_entries(project, revision):
        if mode != "120000":
            continue
        blob = _run_git(project, ["cat-file", "blob", oid], check=False)
        if blob.returncode != 0:
            continue
        link_target = blob.stdout
        if not link_target:
            continue
        if os.path.isabs(link_target):
            resolved = os.path.normpath(link_target)
        else:
            resolved = os.path.normpath(
                os.path.join(os.path.dirname(path), link_target)
            )
        candidate = resolved if os.path.isabs(resolved) else os.path.join(str(project.root), resolved)
        if not _is_within(candidate, root_real):
            unsafe.append(path)
    return sorted(unsafe)


def _present_escaping_symlinks(project: Project, paths: Iterable[str]) -> list[str]:
    """On-disk symlinks among *paths* that currently point outside the root."""
    root_real = os.path.realpath(project.root)
    unsafe: list[str] = []
    for name in paths:
        candidate = project.root / name
        if os.path.islink(candidate) and not _is_within(os.path.realpath(candidate), root_real):
            unsafe.append(name)
    return sorted(unsafe)


def restore(project: Project, revision: str) -> dict[str, Any]:
    """Restore committed files from a verified revision without moving HEAD."""
    _require_project(project)
    value = _check_revision(revision)
    _ensure_own_repository(project)
    with project_lock(project):
        _ensure_own_repository(project)  # re-check under the lock: no .git swap race
        target = _verify_revision(project, value)
        head_before = _head(project)
        current = status(project)
        blocking = [entry for entry in current["changes"] if not entry["ignored"]]
        if blocking:
            raise Cad3Error(
                "refusing to restore: the project has uncommitted content ("
                f"{_preview(entry['status'] + ' ' + entry['path'] for entry in blocking)}); "
                "commit it first (cad3 commit -m ...) and restore again"
            )
        # Refuse before writing anything when the requested revision (or the
        # current one) tracks a symlink that would leave the project root.
        escaping = sorted(
            set(_escaping_symlinks(project, target)) | set(_escaping_symlinks(project, head_before))
        )
        if escaping:
            raise Cad3Error(
                "refusing to restore: committed symlink(s) point outside the project root "
                f"({_preview(escaping)}); remove them from the history first - cad3 never "
                "materialises paths that escape the project"
            )
        current_names = set(_index_names(project))
        if head_before:
            current_names.update(_tree_names(project, head_before))
        target_names = _tree_names(project, target)
        target_set = set(target_names)
        if CONFIG_FILENAME not in target_set:
            raise Cad3Error(
                f"refusing to restore: revision {value!r} does not contain {CONFIG_FILENAME}, "
                "so it is not a cad3 project revision"
            )
        to_remove = sorted(name for name in current_names if name not in target_set)

        _run_git(project, ["restore", f"--source={target}", "--worktree", "--", ":(top)"])

        removed: list[str] = []
        leftovers: list[str] = []
        for name in to_remove:
            candidate = project.root / name
            if os.path.lexists(candidate):
                try:
                    os.unlink(candidate)
                except OSError as exc:
                    leftovers.append(f"{name} ({exc})")
                    continue
            removed.append(name)
        for directory in sorted({os.path.dirname(name) for name in removed}, reverse=True):
            _prune_empty_dirs(project.root, directory)

        verify_files = _worktree_matches_tree(project, target)
        if leftovers:
            verify_files.extend(sorted(set(leftovers)))
        unsafe_symlinks = _present_escaping_symlinks(project, target_names)
        final_status = status(project)

        result: dict[str, Any] = {
            "ok": not verify_files and not unsafe_symlinks,
            "root": str(project.root),
            "revision": revision,
            "target": target,
            "short_target": target[:7],
            "restored_from": head_before,
            "head": head_before,
            "head_moved": False,
            "branch": _branch(project),
            "restored": sorted(target_names),
            "restored_count": len(target_names),
            "removed": removed,
            "removed_count": len(removed),
            "changed": [
                {"status": entry["status"], "path": entry["path"]}
                for entry in final_status["changes"]
            ],
            "verify_files": verify_files,
            "unsafe_symlinks": unsafe_symlinks,
            "status": final_status,
            "notes": [
                "HEAD was not moved and the index was not staged: the next cad3 commit "
                "records this rollback.",
                "Ignored files (for example .cad3 run logs) and unrelated files were left "
                "untouched.",
                "There is no force option: commit your work first, then restore.",
            ],
        }
        if verify_files or unsafe_symlinks:
            result["message"] = (
                "restore did not fully verify: "
                + _preview(verify_files + unsafe_symlinks)
                + " (HEAD was not moved)"
            )
        elif not head_before:
            result["message"] = f"restored the working tree to {target[:7]}"
        elif head_before == target:
            result["message"] = f"already at {target[:7]}; nothing to restore"
        else:
            result["message"] = (
                f"restored the working tree to {target[:7]} (HEAD is still {head_before[:7]}); "
                "commit to record this rollback"
            )
        return result


def _prune_empty_dirs(root: Path, relative: str) -> None:
    """Remove directories emptied by the restore, never the project root."""
    if not relative:
        return
    root_real = os.path.realpath(root)
    current = root / relative
    while True:
        if os.path.realpath(current) == root_real:
            return
        try:
            os.rmdir(current)
        except OSError:
            return
        current = current.parent

