"""Interface declarations and mate checks — a type checker for assemblies.

The model declares, per part, the interfaces it *intends* (shaft journals,
bores, mounting faces) and which interfaces meet.  Two independent checks
follow, both reporting and neither deciding design quality:

1. **Declarations only** (host, no FreeCAD, may run before any geometry):
   pairwise rules ported from V2 ``cadparts.mates`` (bore vs shaft, threads,
   clearance holes, opposed planes) plus declared coaxiality/coplanarity,
   *unmated interfaces* and *parts with no mate at all*.  An interface that no
   mate references must be listed in ``unmated_ok`` with a reason, otherwise it
   is reported.  This is where a lead screw whose far journal touches nothing
   shows up.
2. **Declarations vs geometry** (inside FreeCAD): every cylindrical/planar
   interface must exist on the named object's real shape at the declared
   place, with the declared diameter, axis and material side.  When it does
   not, same-diameter cylinders found elsewhere on the part are listed — a
   bore drilled along the wrong axis shows up here.

Verdicts: PASS / WARN / FAIL / UNKNOWN; UNKNOWN is never silently PASS.
Coordinates are global millimetres in the document's current state.
"""
from __future__ import annotations

import json
import math
from math import isclose
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

SCHEMA = "cad3.interfaces/v1"
REPORT_SCHEMA = "cad3.mates/v1"
DEFAULT_FILE = "notes/interfaces.json"

PASS, WARN, FAIL, UNKNOWN = "PASS", "WARN", "FAIL", "UNKNOWN"
NOTHING = "NOTHING_TO_CHECK"
_SEVERITY = (PASS, UNKNOWN, WARN, FAIL)

_NOMINAL_TOL = 1e-9
POS_TOL = 0.05       # mm, geometric position/diameter tolerance
DIR_TOL = 1e-4       # 1 - |cos| for "parallel"

CYLINDRICAL_OUTER = {"cylindrical_surface", "male_shaft", "male_thread_envelope"}
CYLINDRICAL_INNER = {"cylindrical_bore", "clearance_hole", "female_thread_envelope"}
PLANAR = {"planar_face"}
KNOWN_TYPES = CYLINDRICAL_OUTER | CYLINDRICAL_INNER | PLANAR | {"axis"}

FORMAT_HELP = {
    "file": DEFAULT_FILE,
    "example": {
        "schema": SCHEMA,
        "ground": "BasePlate",
        "parts": {
            "LeadScrew": {"designation": "Tr8x8 lead screw", "ports": [
                {"id": "journal_motor", "type": "cylindrical_surface", "origin": [0, 30, 20],
                 "axis": [1, 0, 0], "diameter": 8}]},
            "BearingA": {"object": "Bearing608_A", "designation": "608ZZ", "ports": [
                {"id": "bore", "type": "cylindrical_bore", "origin": [5, 30, 20],
                 "axis": [1, 0, 0], "diameter": 8}]},
        },
        "mates": [{"a": "LeadScrew.journal_motor", "b": "BearingA.bore", "why": "radial support"}],
        "unmated_ok": {"Carriage.top_face": "customer mounting surface"},
    },
    "types": sorted(KNOWN_TYPES),
    "notes": [
        "origin: any point on the axis (cylinders) or on the face (planes), global mm",
        "axis: cylinder axis direction, or OUTWARD normal of a planar face",
        "object: FreeCAD object name when it differs from the part key",
        "every interface must be mated or listed in unmated_ok with a reason",
    ],
}


class DeclarationError(ValueError):
    """The interface file cannot be read as cad3.interfaces/v1."""


# --------------------------------------------------------------------------
# loading / normalisation
# --------------------------------------------------------------------------
def _vec(value: Any, label: str) -> tuple[float, float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise DeclarationError(f"{label} must be [x, y, z]")
    try:
        out = tuple(float(item) for item in value)
    except (TypeError, ValueError) as exc:
        raise DeclarationError(f"{label} must contain numbers") from exc
    if not all(math.isfinite(item) for item in out):
        raise DeclarationError(f"{label} must be finite")
    return out  # type: ignore[return-value]


def _unit(v: Sequence[float]) -> tuple[float, float, float] | None:
    n = math.sqrt(sum(c * c for c in v))
    if n < 1e-12:
        return None
    return (v[0] / n, v[1] / n, v[2] / n)


def normalize(data: Mapping[str, Any]) -> dict:
    """Validate and return {ground, parts{name:{object, designation, ports{id:port}}}, mates, unmated_ok}."""
    if not isinstance(data, Mapping):
        raise DeclarationError("interface file must be a JSON object")
    schema = data.get("schema")
    if schema not in (None, SCHEMA):
        raise DeclarationError(f"unsupported schema {schema!r}; expected {SCHEMA}")
    raw_parts = data.get("parts")
    if not isinstance(raw_parts, Mapping) or not raw_parts:
        raise DeclarationError("'parts' must be a non-empty object {part_name: {ports: [...]}}")
    parts: dict = {}
    for name, spec in raw_parts.items():
        if not isinstance(spec, Mapping):
            raise DeclarationError(f"part {name!r} must be an object")
        ports: dict = {}
        for index, port in enumerate(spec.get("ports") or []):
            label = f"{name}.ports[{index}]"
            if not isinstance(port, Mapping) or not port.get("id"):
                raise DeclarationError(f"{label} needs an 'id'")
            pid = str(port["id"])
            if pid in ports:
                raise DeclarationError(f"duplicate interface {name}.{pid}")
            ptype = str(port.get("type") or "")
            entry: dict = {"id": pid, "type": ptype}
            if ptype not in KNOWN_TYPES:
                entry["unknown_type"] = True
            if "origin" in port:
                entry["origin"] = _vec(port["origin"], f"{label}.origin")
            if "axis" in port:
                axis = _unit(_vec(port["axis"], f"{label}.axis"))
                if axis is None:
                    raise DeclarationError(f"{label}.axis must not be zero")
                entry["axis"] = axis
            dims = {}
            for key in ("diameter", "nominal_diameter", "pitch", "length"):
                if key in port and port[key] is not None:
                    try:
                        dims[key] = float(port[key])
                    except (TypeError, ValueError) as exc:
                        raise DeclarationError(f"{label}.{key} must be a number") from exc
            entry["dims"] = dims
            if port.get("role"):
                entry["role"] = str(port["role"])
            ports[pid] = entry
        parts[str(name)] = {
            "object": str(spec.get("object") or name),
            "designation": spec.get("designation"),
            "ports": ports,
        }
    mates = []
    for index, mate in enumerate(data.get("mates") or []):
        if not isinstance(mate, Mapping) or "a" not in mate or "b" not in mate:
            raise DeclarationError(f"mates[{index}] must be {{'a': 'Part.port', 'b': 'Part.port'}}")
        mates.append({"a": str(mate["a"]), "b": str(mate["b"]), "why": mate.get("why")})
    unmated_ok = data.get("unmated_ok") or {}
    if not isinstance(unmated_ok, Mapping):
        raise DeclarationError("'unmated_ok' must be {'Part.port': 'reason'}")
    ground = data.get("ground")
    if ground is not None and str(ground) not in parts:
        raise DeclarationError(f"ground {ground!r} is not a declared part")
    return {"ground": ground, "parts": parts, "mates": mates,
            "unmated_ok": {str(k): str(v) for k, v in unmated_ok.items()}}


def load(path: Path) -> dict:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise DeclarationError(f"cannot read {path}: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DeclarationError(f"{path} is not valid JSON: {exc}") from exc
    return normalize(data)


# --------------------------------------------------------------------------
# pair rules (ported from V2 cadparts.mates, order independent)
# --------------------------------------------------------------------------
def _split(a, b, ta, tb):
    if a["type"] == ta and b["type"] == tb:
        return a, b
    if a["type"] == tb and b["type"] == ta:
        return b, a
    return None


def _d(port, key="diameter"):
    return port["dims"].get(key)


def _bore_and_shaft(a, b):
    bore, shaft = _split(a, b, "cylindrical_bore", "cylindrical_surface") or (None, None)
    if bore is None:
        bore, shaft = _split(a, b, "cylindrical_bore", "male_shaft") or (None, None)
    bd, sd = _d(bore), _d(shaft)
    if bd is None or sd is None:
        return UNKNOWN, "bore or shaft has no diameter"
    if sd > bd + _NOMINAL_TOL:
        return FAIL, f"shaft Ø{sd:g} larger than bore Ø{bd:g}: cannot assemble"
    if bd > sd + _NOMINAL_TOL:
        return WARN, (f"bore Ø{bd:g} larger than shaft Ø{sd:g}: assembles but nominal sizes differ "
                      "(deliberate clearance, or the wrong part?)")
    return PASS, "nominal fit"


def _thread_pair(a, b):
    male, female = _split(a, b, "male_thread_envelope", "female_thread_envelope")
    dm, df = _d(male, "nominal_diameter"), _d(female, "nominal_diameter")
    pm, pf = _d(male, "pitch"), _d(female, "pitch")
    if None in (dm, df, pm, pf):
        return UNKNOWN, "thread needs nominal_diameter and pitch"
    if not isclose(dm, df, abs_tol=_NOMINAL_TOL):
        return FAIL, f"thread diameters differ: {dm:g} vs {df:g}"
    if not isclose(pm, pf, abs_tol=_NOMINAL_TOL):
        return FAIL, f"thread pitches differ: {pm:g} vs {pf:g}"
    return PASS, "thread match"


def _clearance(a, b):
    pair = (_split(a, b, "clearance_hole", "male_thread_envelope")
            or _split(a, b, "clearance_hole", "male_shaft")
            or _split(a, b, "clearance_hole", "cylindrical_surface"))
    hole, shank = pair
    hd = _d(hole)
    sd = _d(shank, "nominal_diameter") or _d(shank)
    if hd is None or sd is None:
        return UNKNOWN, "hole or shank has no diameter"
    if sd > hd + _NOMINAL_TOL:
        return FAIL, f"shank Ø{sd:g} larger than clearance hole Ø{hd:g}"
    return PASS, "passes through"


def _planar(a, b):
    if "axis" not in a or "axis" not in b:
        return UNKNOWN, "planar face needs an outward normal 'axis'"
    dot = sum(x * y for x, y in zip(a["axis"], b["axis"]))
    if not isclose(dot, -1.0, abs_tol=1e-6):
        return FAIL, "outward normals are not opposed: the faces cannot sit on each other"
    return PASS, "opposed faces"


_RULES = {
    frozenset({"cylindrical_bore", "cylindrical_surface"}): _bore_and_shaft,
    frozenset({"cylindrical_bore", "male_shaft"}): _bore_and_shaft,
    frozenset({"male_thread_envelope", "female_thread_envelope"}): _thread_pair,
    frozenset({"clearance_hole", "male_thread_envelope"}): _clearance,
    frozenset({"clearance_hole", "male_shaft"}): _clearance,
    frozenset({"clearance_hole", "cylindrical_surface"}): _clearance,
    frozenset({"planar_face"}): _planar,
}


def _placement_check(a, b) -> tuple[str, str] | None:
    """Declared frames must agree: cylinders coaxial, planes coplanar."""
    if "origin" not in a or "origin" not in b or "axis" not in a or "axis" not in b:
        return None
    cyl = CYLINDRICAL_INNER | CYLINDRICAL_OUTER | {"axis"}
    if a["type"] in cyl and b["type"] in cyl:
        dot = abs(sum(x * y for x, y in zip(a["axis"], b["axis"])))
        if 1 - dot > DIR_TOL:
            angle = math.degrees(math.acos(min(1.0, dot)))
            return FAIL, f"declared axes are not parallel ({angle:.2f}°)"
        off = [q - p for p, q in zip(a["origin"], b["origin"])]
        along = sum(o * c for o, c in zip(off, a["axis"]))
        perp = math.sqrt(max(0.0, sum(o * o for o in off) - along * along))
        if perp > POS_TOL:
            return FAIL, f"declared axes are parallel but offset by {perp:.3f} mm"
        return None
    if a["type"] in PLANAR and b["type"] in PLANAR:
        off = [q - p for p, q in zip(a["origin"], b["origin"])]
        gap = abs(sum(o * c for o, c in zip(off, a["axis"])))
        if gap > POS_TOL:
            return FAIL, f"declared faces are {gap:.3f} mm apart, not touching"
    return None


def evaluate_pair(a: Mapping, b: Mapping) -> tuple[str, str]:
    rule = _RULES.get(frozenset({a["type"], b["type"]}))
    if rule is None:
        return UNKNOWN, f"no rule for {a['type']} ↔ {b['type']}"
    verdict, reason = rule(a, b)
    placed = _placement_check(a, b)
    if placed is not None and _SEVERITY.index(placed[0]) > _SEVERITY.index(verdict):
        return placed
    return verdict, reason


# --------------------------------------------------------------------------
# declaration-level graph check
# --------------------------------------------------------------------------
def _lookup(decl, ref):
    if "." not in ref:
        return None, f"{ref!r} is not 'Part.port'"
    part, pid = ref.split(".", 1)
    if part not in decl["parts"]:
        return None, f"no part {part!r}; known: {sorted(decl['parts'])}"
    port = decl["parts"][part]["ports"].get(pid)
    if port is None:
        return None, f"part {part!r} has no interface {pid!r}; known: {sorted(decl['parts'][part]['ports'])}"
    return (part, pid, port), None


def check_declarations(decl: Mapping) -> dict:
    verdicts = []
    mated: set[str] = set()
    edges: dict[str, set[str]] = {name: set() for name in decl["parts"]}
    for index, mate in enumerate(decl["mates"]):
        entry = {"index": index, "a": mate["a"], "b": mate["b"]}
        if mate.get("why"):
            entry["why"] = mate["why"]
        fa, ea = _lookup(decl, mate["a"])
        fb, eb = _lookup(decl, mate["b"])
        if ea or eb:
            entry.update(verdict=FAIL, reason=ea or eb)
            verdicts.append(entry)
            continue
        mated.add(mate["a"])
        mated.add(mate["b"])
        edges[fa[0]].add(fb[0])
        edges[fb[0]].add(fa[0])
        verdict, reason = evaluate_pair(fa[2], fb[2])
        entry.update(verdict=verdict, reason=reason, types=[fa[2]["type"], fb[2]["type"]])
        verdicts.append(entry)

    declared = {f"{p}.{i}" for p, spec in decl["parts"].items() for i in spec["ports"]}
    unmated = sorted(declared - mated)
    excused = {ref: decl["unmated_ok"][ref] for ref in unmated if ref in decl["unmated_ok"]}
    dangling = [ref for ref in unmated if ref not in excused]
    stale_excuses = sorted(set(decl["unmated_ok"]) - declared)

    isolated = sorted(p for p, n in edges.items() if not n)
    disconnected: list[str] = []
    if decl.get("ground"):
        seen, stack = {decl["ground"]}, [decl["ground"]]
        while stack:
            for nxt in edges[stack.pop()]:
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        disconnected = sorted(set(decl["parts"]) - seen - set(isolated))

    findings = []
    for ref in dangling:
        findings.append({"verdict": WARN, "kind": "unmated_interface", "ref": ref,
                         "reason": "declared interface touches nothing; mate it or list it in "
                                   "unmated_ok with the reason it is free"})
    for part in isolated:
        findings.append({"verdict": WARN, "kind": "part_without_mates", "ref": part,
                         "reason": "part has no declared mate: nothing is said to hold it"})
    for part in disconnected:
        findings.append({"verdict": WARN, "kind": "not_connected_to_ground", "ref": part,
                         "reason": f"no chain of mates leads from this part to ground {decl['ground']!r}"})
    for ref in stale_excuses:
        findings.append({"verdict": WARN, "kind": "stale_unmated_ok", "ref": ref,
                         "reason": "unmated_ok names an interface that is not declared"})
    for p, spec in decl["parts"].items():
        for pid, port in spec["ports"].items():
            if port.get("unknown_type"):
                findings.append({"verdict": UNKNOWN, "kind": "unknown_type", "ref": f"{p}.{pid}",
                                 "reason": f"type {port['type']!r} has no rules; known: {sorted(KNOWN_TYPES)}"})
    return {
        "mates": verdicts,
        "findings": findings,
        "coverage": {"parts": len(decl["parts"]), "interfaces": len(declared),
                     "mated": len(mated), "unmated_ok": excused},
    }


# --------------------------------------------------------------------------
# declaration vs FreeCAD geometry (worker side)
# --------------------------------------------------------------------------
def _global_shape(obj):
    import FreeCAD as App  # noqa: N813
    import Part
    shape = Part.getShape(obj, "", needSubElement=False, transform=True).copy()
    pl = App.Placement()
    parent = obj.getParentGeoFeatureGroup() if hasattr(obj, "getParentGeoFeatureGroup") else None
    while parent is not None:
        pl = parent.Placement.multiply(pl)
        parent = parent.getParentGeoFeatureGroup()
    if not pl.isIdentity():
        shape.transformShape(pl.toMatrix())
    return shape


def _cylinders(shape):
    out = []
    for index, face in enumerate(shape.Faces):
        surf = face.Surface
        if type(surf).__name__ != "Cylinder":
            continue
        axis = _unit((surf.Axis.x, surf.Axis.y, surf.Axis.z))
        centre = (surf.Center.x, surf.Center.y, surf.Center.z)
        try:
            u0, u1, v0, v1 = face.ParameterRange
            u, v = (u0 + u1) / 2, (v0 + v1) / 2
            p = face.valueAt(u, v)
            n = face.normalAt(u, v)
            off = (p.x - centre[0], p.y - centre[1], p.z - centre[2])
            along = sum(o * a for o, a in zip(off, axis))
            radial = [o - along * a for o, a in zip(off, axis)]
            outer = sum(r * c for r, c in zip(radial, (n.x, n.y, n.z))) > 0
        except Exception:
            outer = None
        bb = face.BoundBox
        out.append({"face": f"Face{index + 1}", "radius": float(surf.Radius), "axis": axis,
                    "centre": centre, "outer": outer,
                    "span": [round(v, 3) for v in (bb.XMin, bb.YMin, bb.ZMin, bb.XMax, bb.YMax, bb.ZMax)]})
    return out


def _on_axis(point, centre, axis):
    off = [p - c for p, c in zip(point, centre)]
    along = sum(o * a for o, a in zip(off, axis))
    return math.sqrt(max(0.0, sum(o * o for o in off) - along * along))


def _check_cylinder(port, cylinders):
    d = port["dims"].get("diameter") or port["dims"].get("nominal_diameter")
    if d is None or "axis" not in port or "origin" not in port:
        return UNKNOWN, "needs origin, axis and diameter to locate on geometry", []
    want_outer = port["type"] in CYLINDRICAL_OUTER
    same_d = [c for c in cylinders if abs(2 * c["radius"] - d) <= POS_TOL]
    for c in same_d:
        parallel = 1 - abs(sum(x * y for x, y in zip(c["axis"], port["axis"]))) <= DIR_TOL
        if parallel and _on_axis(port["origin"], c["centre"], c["axis"]) <= POS_TOL:
            if c["outer"] is not None and c["outer"] != want_outer:
                side = "outer surface (a shaft)" if c["outer"] else "hole (a bore)"
                return FAIL, (f"found Ø{d:g} on the declared axis ({c['face']}) but it is a {side}, "
                              f"not a {'shaft' if want_outer else 'bore'}"), []
            return PASS, f"Ø{d:g} found on the declared axis ({c['face']})", []
    hints = [{"face": c["face"], "diameter": round(2 * c["radius"], 4),
              "axis": [round(v, 4) for v in c["axis"]],
              "point_on_axis": [round(v, 3) for v in c["centre"]],
              "kind": "outer" if c["outer"] else "hole" if c["outer"] is False else "?"}
             for c in (same_d or cylinders)[:4]]
    if same_d:
        return FAIL, (f"no Ø{d:g} cylinder on the declared axis; the part has Ø{d:g} cylinder(s) "
                      "elsewhere (see found) — wrong axis or position?"), hints
    return FAIL, f"no Ø{d:g} cylinder on this part at all", hints


def _check_plane(port, shape):
    import FreeCAD as App  # noqa: N813
    import Part
    if "axis" not in port or "origin" not in port:
        return UNKNOWN, "needs origin and outward normal axis", []
    vertex = Part.Vertex(App.Vector(*port["origin"]))
    candidates = []
    for index, face in enumerate(shape.Faces):
        if type(face.Surface).__name__ != "Plane":
            continue
        try:
            u0, u1, v0, v1 = face.ParameterRange
            n = face.normalAt((u0 + u1) / 2, (v0 + v1) / 2)
        except Exception:
            continue
        normal = (n.x, n.y, n.z)
        dot = sum(x * y for x, y in zip(normal, port["axis"]))
        if abs(abs(dot) - 1.0) > DIR_TOL:
            continue
        dist = face.distToShape(vertex)[0]
        candidates.append((dist, dot, f"Face{index + 1}"))
    if not candidates:
        return FAIL, "no planar face with that normal on this part", []
    candidates.sort()
    dist, dot, name = candidates[0]
    if dist > POS_TOL:
        return FAIL, f"nearest face with that normal ({name}) is {dist:.3f} mm from the declared origin", []
    if dot < 0:
        return FAIL, f"{name} is at the declared place but its outward normal is reversed", []
    return PASS, f"planar face found ({name})", []


def check_geometry(doc, decl: Mapping) -> dict:
    results = []
    for part, spec in decl["parts"].items():
        obj = doc.getObject(spec["object"])
        if obj is None:
            matches = [o for o in doc.Objects if o.Label == spec["object"]]
            obj = matches[0] if len(matches) == 1 else None
        if obj is None:
            results.append({"ref": part, "verdict": FAIL,
                            "reason": f"no FreeCAD object named {spec['object']!r}"})
            continue
        try:
            shape = _global_shape(obj)
        except Exception as exc:  # noqa: BLE001
            results.append({"ref": part, "verdict": FAIL, "reason": f"cannot read shape: {exc}"})
            continue
        cylinders = _cylinders(shape)
        for pid, port in spec["ports"].items():
            ref = f"{part}.{pid}"
            if port["type"] in CYLINDRICAL_INNER | CYLINDRICAL_OUTER:
                if port["type"].endswith("thread_envelope") and port["type"].startswith("female"):
                    results.append({"ref": ref, "verdict": UNKNOWN,
                                    "reason": "internal threads are usually modelled as a tap drill; not located"})
                    continue
                verdict, reason, found = _check_cylinder(port, cylinders)
            elif port["type"] in PLANAR:
                verdict, reason, found = _check_plane(port, shape)
            else:
                verdict, reason, found = UNKNOWN, f"type {port['type']!r} is not located on geometry", []
            item = {"ref": ref, "object": obj.Name, "verdict": verdict, "reason": reason}
            if found:
                item["found"] = found
            results.append(item)
    return {"interfaces": results,
            "note": "geometry checked in the document's current state (current positions)"}


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------
def _worst(verdicts: Iterable[str]) -> str:
    worst = None
    for v in verdicts:
        if worst is None or _SEVERITY.index(v) > _SEVERITY.index(worst):
            worst = v
    return worst or NOTHING


def report(decl: Mapping, geometry: Mapping | None = None, *, geometry_skipped: str | None = None) -> dict:
    declared = check_declarations(decl)
    all_v = [m["verdict"] for m in declared["mates"]] + [f["verdict"] for f in declared["findings"]]
    if geometry is not None:
        all_v += [g["verdict"] for g in geometry["interfaces"]]
    counts = {k: all_v.count(k) for k in _SEVERITY}
    problems = ([m for m in declared["mates"] if m["verdict"] != PASS]
                + declared["findings"]
                + [g for g in (geometry or {}).get("interfaces", []) if g["verdict"] != PASS])
    return {
        "schema": REPORT_SCHEMA,
        "overall": _worst(all_v) if all_v else NOTHING,
        "counts": counts,
        "problems": problems,
        "declarations": declared,
        "geometry": geometry if geometry is not None else {"skipped": geometry_skipped},
        "boundary": ("checks declared interfaces against each other and against the shape; "
                     "not strength, tolerance class, assembly access or motion between states"),
    }


__all__ = ["SCHEMA", "REPORT_SCHEMA", "DEFAULT_FILE", "FORMAT_HELP", "DeclarationError",
           "normalize", "load", "evaluate_pair", "check_declarations", "check_geometry", "report"]
