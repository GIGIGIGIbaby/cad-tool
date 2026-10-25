"""Insert standard assembly proxies into a native FreeCAD document.

Only call add() inside FreeCAD. Importing this module needs no CAD kernel.
Saved proxies have no Python Proxy and reopen without this library installed.
"""

from __future__ import annotations
import json
from copy import deepcopy
from . import catalog


def _shape(spec, App, Part):
    d = spec["dimensions_mm"]
    f = spec["family"]

    def cylinder(diameter, length, z=0):
        return Part.makeCylinder(diameter / 2, length, App.Vector(0, 0, z))

    def ring(outside, bore, length, z=0):
        shape = cylinder(outside, length, z)
        return shape.cut(cylinder(bore, length + 2, z - 1)) if bore > 0 else shape

    def hexagon(flats, height):
        import math

        r = flats / math.sqrt(3)
        pts = [
            App.Vector(
                r * math.cos(math.radians(30 + 60 * i)),
                r * math.sin(math.radians(30 + 60 * i)),
                0,
            )
            for i in range(6)
        ]
        return Part.Face(Part.makePolygon(pts + [pts[0]])).extrude(
            App.Vector(0, 0, height)
        )

    if f == "bearing.deep_groove":
        return ring(d["outside_diameter"], d["bore"], d["width"])
    if f == "fastener.hex_bolt_metric":
        return (
            hexagon(d["across_flats"], d["head_height"])
            .fuse(cylinder(d["diameter"], d["length"], d["head_height"]))
            .removeSplitter()
        )
    if f == "fastener.hex_nut_metric":
        return hexagon(d["across_flats"], d["nut_height"]).cut(
            cylinder(d["diameter"], d["nut_height"] + 2, -1)
        )
    if f == "fastener.plain_washer_metric":
        return ring(d["washer_outside"], d["washer_inside"], d["washer_thickness"])
    if f == "motor.stepper.square":
        face = d["face"]
        shape = Part.makeBox(
            face, face, d["body_length"], App.Vector(-face / 2, -face / 2, 0)
        )
        for x in (-1, 1):
            for y in (-1, 1):
                tool = cylinder(d["hole"], min(8, d["body_length"]) + 1, -1)
                tool.translate(App.Vector(x * d["pitch"] / 2, y * d["pitch"] / 2, 0))
                shape = shape.cut(tool)
        shape = shape.fuse(cylinder(d["pilot"], d["pilot_depth"], -d["pilot_depth"]))
        return shape.fuse(
            cylinder(d["shaft"], d["shaft_length"], -d["shaft_length"])
        ).removeSplitter()
    if f == "linear.bushing.ball":
        shape = cylinder(d["outside"], d["length"])
        if d["style"] in ("LMF", "LMK"):
            flange = (
                cylinder(d["flange"], d["flange_thickness"])
                if d["style"] == "LMF"
                else Part.makeBox(
                    d["flange"],
                    d["flange"],
                    d["flange_thickness"],
                    App.Vector(-d["flange"] / 2, -d["flange"] / 2, 0),
                )
            )
            shape = shape.fuse(flange)
            # LMF uses a circular bolt pitch; LMK uses a square pattern.
            coords = (
                [
                    (d["flange_pitch"] / 2, 0),
                    (0, d["flange_pitch"] / 2),
                    (-d["flange_pitch"] / 2, 0),
                    (0, -d["flange_pitch"] / 2),
                ]
                if d["style"] == "LMF"
                else [
                    (x * d["flange_pitch"] / 2, y * d["flange_pitch"] / 2)
                    for x in (-1, 1)
                    for y in (-1, 1)
                ]
            )
            for x, y in coords:
                tool = cylinder(d["flange_hole"], d["flange_thickness"] + 2, -1)
                tool.translate(App.Vector(x, y, 0))
                shape = shape.cut(tool)
        return shape.cut(cylinder(d["bore"], d["length"] + 2, -1)).removeSplitter()
    if f == "drive.timing_pulley":
        if not d["flange"]:
            return ring(d["outside_diameter"], d["bore"], d["length"])
        return (
            ring(d["flange_diameter"], d["bore"], 1)
            .fuse(ring(d["outside_diameter"], d["bore"], d["belt_width"], 1))
            .fuse(ring(d["flange_diameter"], d["bore"], 1, d["belt_width"] + 1))
            .removeSplitter()
        )
    if f == "coupling.flexible":
        h = d["hub_length"]
        gap = d["gap"]
        shape = ring(d["diameter"], d["bore_a"], h)
        # Flexible centre is an annulus, preserving both bore passages.
        shape = shape.fuse(
            ring(d["diameter"] * 0.86, max(d["bore_a"], d["bore_b"]), gap, h)
        )
        return shape.fuse(ring(d["diameter"], d["bore_b"], h, h + gap)).removeSplitter()
    if f == "profile.round_rod":
        return cylinder(d["diameter"], d["length"])
    raise catalog.CatalogError(f"no native proxy for {f}")


def add(
    document, identifier, *, name, parameters=None, selections=None, placement=None
):
    """Create/update one Part::Feature and store its complete instance contract.

    Naming is explicit and idempotent. Updating a part keeps its Placement
    (including expressions) unless a replacement Placement is supplied.
    """
    import FreeCAD as App
    import Part

    spec = catalog.instantiate(identifier, parameters=parameters, selections=selections)
    shape = _shape(spec, App, Part)
    if not shape.isValid() or len(shape.Solids) != 1:
        raise catalog.CatalogError(
            f"{identifier}: invalid or disconnected native proxy"
        )
    obj = document.getObject(name)
    if obj is not None and (
        obj.TypeId != "Part::Feature" or "Cad3Part" not in obj.PropertiesList
    ):
        raise catalog.CatalogError(f"{name} exists and is not a library part")
    if obj is None:
        obj = document.addObject("Part::Feature", name)
        obj.addProperty(
            "App::PropertyString",
            "Cad3Part",
            "Standard part",
            "Reusable local catalog contract",
        )
    obj.Shape = shape
    obj.Cad3Part = json.dumps(spec, ensure_ascii=False, allow_nan=False)
    obj.Label = spec["purchase"].get(
        "order_code", spec["purchase"].get("query", identifier)
    )
    if placement is not None:
        obj.Placement = placement
    return obj


def contract(obj):
    """Read the local part contract, including after reopening an FCStd."""
    return json.loads(obj.Cad3Part)


def declaration(obj, *, ports=None):
    """Expand current Placement to world-space ports for cad3 mates.

    Recompute the document first. Optional ports selects interfaces involved
    in this assembly; selection of absent ports is an error.
    """
    import FreeCAD as App

    spec = contract(obj)
    local = spec["ports"]
    selected = None if ports is None else set(ports)
    if selected is not None and selected - set(p["id"] for p in local):
        raise catalog.CatalogError(
            f'unknown ports {sorted(selected-set(p["id"] for p in local))}'
        )
    placement = obj.getGlobalPlacement()
    result = []
    for p in local:
        if selected is not None and p["id"] not in selected:
            continue
        out = deepcopy(p)
        point = placement.multVec(App.Vector(*p["origin"]))
        axis = placement.Rotation.multVec(App.Vector(*p["axis"]))
        out.update(origin=[point.x, point.y, point.z], axis=[axis.x, axis.y, axis.z])
        result.append(out)
    return dict(
        object=obj.Name,
        designation=spec.get("purchase", {}).get(
            "order_code", spec.get("catalog_id", obj.Label)
        ),
        catalog_id=spec.get("catalog_id", "custom." + obj.Name),
        library_version=spec.get("library_version"),
        ports=result,
    )
