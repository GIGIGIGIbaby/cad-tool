#!/usr/bin/env python3
"""EVAL-002 host driver.

For each commit of an agent project, extract the committed models/main.FCStd
blob into a fresh scratch judge project and run eval002_probe.py inside an
isolated FreeCAD process through cad3. The agent project is never modified.

usage: python3 evaluate_eval002.py PROJECT [out.json] [--axes PosX:StrokeX,PosY:StrokeY]
"""
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
CAD3 = HERE.parents[1] / "cad3.py"
PROBE = HERE / "eval002_probe.py"


def git(project, *args, binary=False):
    r = subprocess.run(["git", "-C", str(project), *args], capture_output=True, check=True)
    return r.stdout if binary else r.stdout.decode()


AXES = None


def judge_commit(project, commit, workroot):
    blob = git(project, "show", f"{commit}:models/main.FCStd", binary=True)
    jp = workroot / commit[:10]
    subprocess.run([sys.executable, str(CAD3), "init", str(jp), "--name", "judge"],
                   check=True, capture_output=True)
    (jp / "models").mkdir(exist_ok=True)
    (jp / "models" / "main.FCStd").write_bytes(blob)
    shutil.copy(PROBE, jp / "src" / "probe.py")
    if AXES:
        (jp / "judge_cfg.json").write_text(json.dumps({"axes": AXES}))
    r = subprocess.run([sys.executable, str(CAD3), "run", "src/probe.py", "-p", str(jp),
                        "--timeout", "900"], capture_output=True, text=True)
    out = jp / "judge_out.json"
    if not out.exists():
        return {"commit": commit, "error": "probe produced no output",
                "cad3": r.stdout[-3000:], "stderr": r.stderr[-2000:]}
    res = json.loads(out.read_text())
    res["commit"] = commit
    res["cad3_ok"] = r.returncode == 0
    return res


def main():
    global AXES
    argv = list(sys.argv[1:])
    if "--axes" in argv:
        i = argv.index("--axes")
        AXES = [pair.split(":") for pair in argv[i + 1].split(",")]
        del argv[i:i + 2]
    sys.argv[1:] = argv
    project = Path(sys.argv[1]).resolve()
    commits = git(project, "log", "--reverse", "--format=%H").split()
    workroot = Path(tempfile.mkdtemp(prefix=f"cad3-judge-{project.name}-"))
    results = {"project": str(project), "commits": [], "workroot": str(workroot)}
    for c in commits:
        try:
            git(project, "cat-file", "-e", f"{c}:models/main.FCStd")
        except subprocess.CalledProcessError:
            results["commits"].append({"commit": c, "error": "no models/main.FCStd"})
            continue
        subject = git(project, "log", "-1", "--format=%s", c).strip()
        res = judge_commit(project, c, workroot)
        res["subject"] = subject
        results["commits"].append(res)
    text = json.dumps(results, indent=1, ensure_ascii=False)
    if len(sys.argv) > 2:
        Path(sys.argv[2]).write_text(text)
    print(text)


if __name__ == "__main__":
    main()
