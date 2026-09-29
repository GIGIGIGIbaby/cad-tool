"""EVAL-002/003 independent probe. Runs INSIDE FreeCAD via `cad3 run` on a scratch
judge project whose models/main.FCStd is a byte copy of a committed blob.

Does not trust the agent's own report: finds the Stroke/CarriagePos aliases,
drives them, recomputes and checks pairwise interference, envelope and travel.
Writes PROJECT_DIR/judge_out.json.

Axes default to [("CarriagePos", "Stroke")]; an optional PROJECT_DIR/judge_cfg.json
{"axes": [["PosX", "StrokeX"], ["PosY", "StrokeY"]]} checks every corner.
"""
import itertools
import json

import Part

OVERLAP_TOL = 1e-3  # mm^3


CFG = PROJECT_DIR / "judge_cfg.json"
AXES = json.loads(CFG.read_text())["axes"] if CFG.exists() else [["CarriagePos", "Stroke"]]


def find_sheet():
    for o in doc.Objects:
        if o.TypeId == "Spreadsheet::Sheet":
            try:
                if all(o.getCellFromAlias(a) for pair in AXES for a in pair):
                    return o
            except Exception:
                pass
    return None


SKIP_TYPES = ("Spreadsheet::Sheet", "App::Origin", "App::Line", "App::Plane",
              "App::Part", "App::DocumentObjectGroup")
CONTAINERS = ("App::Part", "App::DocumentObjectGroup")


def is_output(o):
    t = o.TypeId
    if t in SKIP_TYPES or t.startswith("Sketcher::"):
        return False
    if t.startswith("PartDesign::") and t != "PartDesign::Body":
        return False
    try:
        parent = o.getParentGeoFeatureGroup()
    except Exception:
        parent = None
    if parent is not None and parent.TypeId == "PartDesign::Body":
        return False
    is_link = t.startswith("App::Link")
    if not is_link and not hasattr(o, "Shape"):
        return False
    for p in o.InList:  # consumed by another geometry feature -> intermediate
        if p.TypeId in CONTAINERS or p.TypeId == "Spreadsheet::Sheet":
            continue
        if p.TypeId.startswith("App::Link"):
            continue
        if hasattr(p, "Shape"):
            return False
    return True


def global_shape(o):
    # own placement via Part.getShape(transform=True); then containers upward
    s = Part.getShape(o, "", needSubElement=False, transform=True).copy()
    pl = App.Placement()
    parent = o.getParentGeoFeatureGroup()
    while parent is not None:
        pl = parent.Placement.multiply(pl)
        parent = parent.getParentGeoFeatureGroup()
    if not pl.isIdentity():
        s.transformShape(pl.toMatrix())
    return s


def collect():
    parts = {}
    for o in doc.Objects:
        if not is_output(o):
            continue
        s = global_shape(o)
        if s.isNull() or not s.Solids:
            continue
        parts[o.Name] = (o.Label, s)
    return parts


def state_errors():
    bad = []
    for o in doc.Objects:
        st = list(o.State)
        if "Invalid" in st or "Error" in st:
            bad.append({"name": o.Name, "state": st})
    return bad


def snapshot(sheet, positions):
    for (pos_alias, _), value in zip(AXES, positions):
        sheet.set(sheet.getCellFromAlias(pos_alias), str(value))
    doc.recompute()
    parts = collect()
    bb = None
    for _, (_, s) in parts.items():
        bb = s.BoundBox if bb is None else bb.united(s.BoundBox)
    clashes = []
    names = sorted(parts)
    for a, b in itertools.combinations(names, 2):
        sa, sb = parts[a][1], parts[b][1]
        if not sa.BoundBox.intersect(sb.BoundBox):
            continue
        try:
            v = sa.common(sb).Volume
        except Exception as e:  # noqa: BLE001
            clashes.append({"a": a, "b": b, "error": str(e)})
            continue
        if v > OVERLAP_TOL:
            clashes.append({"a": a, "b": b, "overlap_mm3": round(v, 3)})
    centers = {n: tuple(round(c, 4) for c in parts[n][1].BoundBox.Center) for n in names}
    return {
        "pos": list(positions),
        "errors": state_errors(),
        "n_parts": len(names),
        "parts": {n: {"label": parts[n][0], "solids": len(parts[n][1].Solids),
                      "valid": parts[n][1].isValid(), "volume": round(parts[n][1].Volume, 2)} for n in names},
        "envelope": None if bb is None else [round(bb.XLength, 2), round(bb.YLength, 2), round(bb.ZLength, 2)],
        "bbox": None if bb is None else [round(v, 2) for v in (bb.XMin, bb.YMin, bb.ZMin, bb.XMax, bb.YMax, bb.ZMax)],
        "clashes": clashes,
        "_centers": centers,
    }


def moved(a, b):
    out = {}
    for n, c0 in a["_centers"].items():
        c1 = b["_centers"].get(n)
        if c1 is None:
            continue
        d = [round(y - x, 3) for x, y in zip(c0, c1)]
        if max(abs(v) for v in d) > 1e-3:
            out[n] = d
    return out


out = {"axes": AXES}
doc.recompute()
sheet = find_sheet()
if sheet is None:
    out["error"] = "no Spreadsheet with aliases %s" % AXES
else:
    strokes = [float(sheet.get(stroke_alias)) for _, stroke_alias in AXES]
    out["strokes"] = strokes
    out["stroke"] = strokes[0]
    out["errors_as_committed"] = state_errors()
    corners = list(itertools.product(*[(0.0, st) for st in strokes]))
    snaps = [snapshot(sheet, c) for c in corners]
    base = snaps[0]
    by_axis = {}
    for k, (pos_alias, _) in enumerate(AXES):
        target = tuple(strokes[i] if i == k else 0.0 for i in range(len(AXES)))
        by_axis[pos_alias] = moved(base, snaps[corners.index(target)])
    out["moving_by_axis"] = by_axis
    # EVAL-002 compatible summary: displacement magnitude along the first axis
    out["moving_parts_displacement"] = {
        n: round(sum(v * v for v in d) ** 0.5, 3) for n, d in by_axis[AXES[0][0]].items()}
    bbs = [s["bbox"] for s in snaps if s["bbox"]]
    if bbs:
        lo = [min(b[i] for b in bbs) for i in range(3)]
        hi = [max(b[i + 3] for b in bbs) for i in range(3)]
        out["swept_envelope"] = [round(hi[i] - lo[i], 2) for i in range(3)]
    for s in snaps:
        s.pop("_centers")
    out["corners"] = snaps
    if len(AXES) == 1:
        out["pos0"], out["posStroke"] = snaps[0], snaps[1]

(PROJECT_DIR / "judge_out.json").write_text(json.dumps(out, indent=1, ensure_ascii=False))
print(json.dumps({k: v for k, v in out.items() if k not in ("pos0", "posStroke", "corners")}, ensure_ascii=False))
