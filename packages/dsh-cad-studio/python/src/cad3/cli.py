"""Small JSON CLI; modeling remains native FreeCAD Python, not a new CAD language."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import sys

from . import __version__
from . import catalog
from .project import Cad3Error, Project
from . import runtime, versions


def positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if number <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def positive_float(value: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be finite and positive")
    return number


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="cad3",
        description="FreeCAD execution, observation and Git-backed design snapshots",
    )
    p.add_argument("--version", action="version", version=f"cad3 {__version__}")
    commands = p.add_subparsers(dest="command", required=True)
    doctor = commands.add_parser(
        "doctor", help="discover and probe an installed FreeCAD in an isolated process"
    )
    doctor.add_argument("--freecad", metavar="PATH")
    doctor.add_argument("--timeout", type=positive_float, default=15.0)
    init = commands.add_parser(
        "init", help="create an ordinary new project with its own Git repository"
    )
    init.add_argument("path", type=Path)
    init.add_argument("--name")

    def project_command(name: str, help_text: str, freecad: bool = False):
        cmd = commands.add_parser(name, help=help_text)
        cmd.add_argument(
            "-p",
            "--project",
            type=Path,
            default=Path.cwd(),
            help="project directory (default: current directory)",
        )
        if freecad:
            cmd.add_argument("--freecad", metavar="PATH")
        return cmd

    run = project_command(
        "run", "execute trusted Python against the current native document", True
    )
    run.add_argument(
        "script",
        nargs="?",
        help="script relative to project, or absolute path inside it",
    )
    run.add_argument("--timeout", type=positive_float, default=120.0)
    run.add_argument(
        "--step",
        action="store_true",
        help="also export selected result objects as STEP",
    )
    inspect = project_command(
        "inspect",
        "observe structure, parameters, expressions and native geometry",
        True,
    )
    inspect.add_argument("--object", dest="object_name")
    inspect.add_argument("--detail", action="store_true")
    inspect.add_argument("--format", choices=("json", "code"), default="json")
    inspect.add_argument("--timeout", type=positive_float, default=60.0)
    render = project_command(
        "render", "render geometry with bundled VTK (not a GUI screenshot)", True
    )
    render.add_argument(
        "--views",
        default=None,
        help="comma list of iso,top,front,right (default all four, or none when --section is given)",
    )
    render.add_argument(
        "--section",
        action="append",
        default=[],
        metavar="AXIS=MM",
        help="axis-aligned section view, e.g. --section y=12.5 (repeatable); overlaps between objects are drawn red",
    )
    render.add_argument("--width", type=positive_int, default=960)
    render.add_argument("--height", type=positive_int, default=720)
    render.add_argument("--timeout", type=positive_float, default=90.0)
    mates_cmd = project_command(
        "mates",
        "check declared part interfaces against each other and the geometry",
        True,
    )
    mates_cmd.add_argument(
        "--file",
        default=None,
        help="interface declaration (default notes/interfaces.json)",
    )
    mates_cmd.add_argument(
        "--no-geometry",
        action="store_true",
        help="declarations only; do not start FreeCAD",
    )
    mates_cmd.add_argument("--timeout", type=positive_float, default=120.0)
    catalog_cmd = commands.add_parser(
        "catalog", help="search and inspect the bundled standard-parts catalog"
    )
    catalog_sub = catalog_cmd.add_subparsers(dest="catalog_command", required=True)
    search_cmd = catalog_sub.add_parser(
        "search", help="rank compact family/item candidates"
    )
    search_cmd.add_argument("query", nargs="?", default="")
    search_cmd.add_argument("--category")
    search_cmd.add_argument("--limit", type=positive_int, default=8)
    describe_cmd = catalog_sub.add_parser(
        "describe", help="show one family or catalog item contract"
    )
    describe_cmd.add_argument("identifier")
    compare_cmd = catalog_sub.add_parser(
        "compare", help="compare two or more catalog candidates"
    )
    compare_cmd.add_argument("identifiers", nargs="+", metavar="ID")
    instance_cmd = catalog_sub.add_parser(
        "instantiate", help="return a geometry-free instance contract"
    )
    instance_cmd.add_argument("identifier")
    instance_cmd.add_argument(
        "--selection",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="purchase selection; may be repeated",
    )
    instance_cmd.add_argument(
        "--parameter",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="instance parameter override; may be repeated",
    )
    project_command("status", "show project Git status")
    history = project_command("log", "show committed design snapshots")
    history.add_argument("--limit", type=positive_int, default=10)
    diff = project_command(
        "diff", "show file changes (not automatic CAD semantic diff)"
    )
    diff.add_argument("revision", nargs="?")
    commit = project_command(
        "commit", "snapshot existing design files without rerunning the model"
    )
    commit.add_argument("-m", "--message", required=True)
    restore = project_command(
        "restore",
        "restore a committed design into a clean worktree, without moving HEAD",
    )
    restore.add_argument("revision")
    return p


def _project(args) -> Project:
    return Project.load(args.project)


def _script(project: Project, value: str | None) -> Path | None:
    if value is None:
        return None
    path = Path(value)
    if path.is_absolute():
        try:
            path = path.resolve().relative_to(project.root.resolve())
        except ValueError as exc:
            raise Cad3Error(
                "Scripts must be inside the project so their source can be versioned"
            ) from exc
    return project.resolve(str(path))


def _key_values(values: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        key, separator, item = value.partition("=")
        if not separator or not key.strip():
            raise Cad3Error(f"expected KEY=VALUE, got {value!r}")
        if key.strip() in result:
            raise Cad3Error(f"duplicate catalog key {key.strip()!r}")
        text = item.strip()
        if text.casefold() in {"true", "false"}:
            parsed = text.casefold() == "true"
        else:
            try:
                parsed = (
                    float(text) if any(char in text for char in ".eE") else int(text)
                )
            except ValueError:
                parsed = text
        result[key.strip()] = parsed
    return result


def dispatch(args) -> dict | str:
    command = args.command
    if command == "doctor":
        return runtime.doctor(executable=args.freecad, timeout=args.timeout)
    if command == "init":
        project = Project.init(args.path, name=args.name)
        return {"ok": True, "project": str(project.root), "config": project.config}
    if command == "catalog":
        if args.catalog_command == "search":
            return {
                "catalog": catalog.search(
                    args.query, category=args.category, limit=args.limit
                )
            }
        if args.catalog_command == "describe":
            return catalog.describe(args.identifier)
        if args.catalog_command == "compare":
            return catalog.compare(*args.identifiers)
        if args.catalog_command == "instantiate":
            return catalog.instantiate(
                args.identifier,
                selections=_key_values(args.selection),
                parameters=_key_values(args.parameter),
            )
        raise Cad3Error(f"Unknown catalog command: {args.catalog_command}")
    project = _project(args)
    if command == "run":
        return runtime.execute(
            project,
            _script(project, args.script),
            executable=args.freecad,
            timeout=args.timeout,
            export_step=args.step,
        )
    if command == "inspect":
        result = runtime.inspect(
            project,
            executable=args.freecad,
            timeout=args.timeout,
            object_name=args.object_name,
            detail=args.detail,
        )
        if args.format == "code" and result.get("ok"):
            from .observe import code_view

            return code_view(result["state"])
        return result
    if command == "render":
        if args.views is None:
            views = () if args.section else ("iso", "top", "front", "right")
        else:
            views = tuple(
                item.strip() for item in args.views.split(",") if item.strip()
            )
        if (not views and not args.section) or any(
            item not in {"iso", "top", "front", "right"} for item in views
        ):
            raise Cad3Error(
                "views must be a nonempty comma-separated list of iso,top,front,right"
            )
        if len(set(views)) != len(views):
            raise Cad3Error("duplicate render views are not allowed")
        if max(args.width, args.height) > 4096:
            raise Cad3Error("render width and height must not exceed 4096 pixels")
        return runtime.render(
            project,
            executable=args.freecad,
            timeout=args.timeout,
            views=views,
            sections=args.section,
            width=args.width,
            height=args.height,
        )
    if command == "mates":
        return runtime.mates(
            project,
            file=args.file,
            geometry=not args.no_geometry,
            executable=args.freecad,
            timeout=args.timeout,
        )
    if command == "status":
        return versions.status(project)
    if command == "log":
        return versions.log(project, limit=args.limit)
    if command == "diff":
        return versions.diff(project, revision=args.revision)
    if command == "commit":
        if not args.message.strip():
            raise Cad3Error("commit message must not be empty")
        return versions.commit(project, args.message)
    if command == "restore":
        return versions.restore(project, args.revision)
    raise Cad3Error(f"Unknown command: {command}")


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        result = dispatch(args)
        if isinstance(result, str):
            print(result)
            return 0
        if not isinstance(result, dict):
            raise TypeError("command returned a non-object result")
        result = {"ok": True, "command": args.command, **result}
        encoded = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)
        print(encoded)
        return 0 if result.get("ok") else 1
    except KeyboardInterrupt:
        print(
            json.dumps(
                {
                    "ok": False,
                    "command": args.command,
                    "error": {
                        "type": "Interrupted",
                        "message": "Operation interrupted",
                    },
                }
            )
        )
        return 130
    except Exception as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "command": args.command,
                    "error": {"type": type(exc).__name__, "message": str(exc)},
                },
                ensure_ascii=False,
            )
        )
        return 1


def entrypoint() -> None:
    raise SystemExit(main())
