"""EVAL-002 independent probe. Runs INSIDE FreeCAD via `cad3 run` on a scratch
judge project whose models/main.FCStd is a byte copy of a committed blob.

Does not trust the agent's own report: finds the Stroke/CarriagePos aliases,
drives them, recomputes and checks pairwise interference, envelope and travel.
Writes PROJECT_DIR/judge_out.json.
"""
import itertools
import json

import Part

OVERLAP_TOL = 1e-3  # mm^3


def find_sheet():
    for o in doc.Objects:
        if o.TypeId == "Spreadsheet::Sheet":
            try:
                if o.getCellFromAlias("Stroke") and o.getCellFromAlias("CarriagePos"):
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


def snapshot(sheet, pos_value):
    sheet.set(sheet.getCellFromAlias("CarriagePos"), str(pos_value))
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
        "pos": pos_value,
        "errors": state_errors(),
        "n_parts": len(names),
        "parts": {n: {"label": parts[n][0], "solids": len(parts[n][1].Solids),
                      "valid": parts[n][1].isValid(), "volume": round(parts[n][1].Volume, 2)} for n in names},
        "envelope": None if bb is None else [round(bb.XLength, 2), round(bb.YLength, 2), round(bb.ZLength, 2)],
        "clashes": clashes,
        "_centers": centers,
    }


out = {}
doc.recompute()
sheet = find_sheet()
if sheet is None:
    out["error"] = "no Spreadsheet with aliases Stroke and CarriagePos"
else:
    stroke = float(sheet.get("Stroke"))
    out["stroke"] = stroke
    out["carriagepos_committed"] = str(sheet.get("CarriagePos"))
    out["errors_as_committed"] = state_errors()
    lo = snapshot(sheet, 0)
    hi = snapshot(sheet, stroke)
    moved = {}
    for n, c0 in lo["_centers"].items():
        c1 = hi["_centers"].get(n)
        if c1 is None:
            continue
        d = sum((x - y) ** 2 for x, y in zip(c0, c1)) ** 0.5
        if d > 1e-3:
            moved[n] = round(d, 3)
    for s in (lo, hi):
        s.pop("_centers")
    out["pos0"] = lo
    out["posStroke"] = hi
    out["moving_parts_displacement"] = moved

(PROJECT_DIR / "judge_out.json").write_text(json.dumps(out, indent=1, ensure_ascii=False))
print(json.dumps({k: v for k, v in out.items() if k not in ("pos0", "posStroke")}, ensure_ascii=False))
