"""Concrete local interfaces for the first native standard-part families."""

from __future__ import annotations
import math
import json
from pathlib import Path

SUPPORTED = frozenset(
    {
        "bearing.deep_groove",
        "fastener.hex_bolt_metric",
        "fastener.hex_nut_metric",
        "fastener.plain_washer_metric",
        "motor.stepper.square",
        "linear.bushing.ball",
        "drive.timing_pulley",
        "coupling.flexible",
        "profile.round_rod",
    }
)


def resolve(family, p, dimensions):
    tables = json.loads((Path(__file__).parent / "data/profiles.json").read_text())
    d = dict(dimensions)
    ports = []

    def port(id, type, origin=(0, 0, 0), axis=(0, 0, 1), **dims):
        ports.append(
            dict(id=id, type=type, origin=list(origin), axis=list(axis), **dims)
        )

    def ends(length, first="axial_face_min", last="axial_face_max", point=(0, 0)):
        x, y = point
        port(first, "planar_face", (x, y, 0), axis=(0, 0, -1))
        port(last, "planar_face", (x, y, length), axis=(0, 0, 1))

    if family == "bearing.deep_groove":
        port("shaft_bore", "cylindrical_bore", diameter=d["bore"], length=d["width"])
        port(
            "housing_seat",
            "cylindrical_surface",
            diameter=d["outside_diameter"],
            length=d["width"],
        )
        ends(d["width"])
    elif family.startswith("fastener."):
        key = str(p["size"]).upper()
        key = key if key.startswith("M") else "M" + key
        if key not in tables["fasteners"]:
            raise ValueError(f"unsupported metric size {key}")
        p["size"] = key
        t = tables["fasteners"][key]
        d.update(t)
        if family == "fastener.hex_bolt_metric":
            length = p["length"]
            if length <= 0:
                raise ValueError("length must be positive")
            d["length"] = length
            port(
                "thread_axis",
                "male_thread_envelope",
                (0, 0, t["head_height"]),
                nominal_diameter=t["diameter"],
                pitch=t["coarse_pitch"],
                length=length,
            )
            port(
                "head_bearing_face",
                "planar_face",
                (t["across_flats"] / 2 - 0.5, 0, t["head_height"]),
                axis=(0, 0, 1),
            )
        elif family == "fastener.hex_nut_metric":
            port(
                "thread_bore",
                "female_thread_envelope",
                nominal_diameter=t["diameter"],
                pitch=t["coarse_pitch"],
                length=t["nut_height"],
            )
            ends(
                t["nut_height"],
                "bearing_face_min",
                "bearing_face_max",
                point=(0, 3.2),
            )
        else:
            port(
                "clearance_bore",
                "cylindrical_bore",
                diameter=t["washer_inside"],
                length=t["washer_thickness"],
            )
            ends(
                t["washer_thickness"],
                "bearing_face_min",
                "bearing_face_max",
                point=(4.0, 0),
            )
    elif family == "motor.stepper.square":
        t = tables["steppers"][str(p["frame"])]
        d.update(t)
        for name, target in [
            ("body_length", "body_length"),
            ("shaft_diameter", "shaft"),
            ("shaft_length", "shaft_length"),
        ]:
            if p[name] is not None:
                d[target] = p[name]
            if d[target] <= 0:
                raise ValueError(f"{name} must be positive")
        if d["shaft"] >= d["pilot"]:
            raise ValueError("shaft must be smaller than pilot")
        port("mounting_face", "planar_face", axis=(0, 0, -1))
        port(
            "pilot",
            "cylindrical_surface",
            axis=(0, 0, -1),
            diameter=d["pilot"],
            length=d["pilot_depth"],
        )
        port(
            "output_shaft",
            "male_shaft",
            axis=(0, 0, -1),
            diameter=d["shaft"],
            length=d["shaft_length"],
        )
        for x in (-1, 1):
            for y in (-1, 1):
                port(
                    f"mount_hole_{x}_{y}",
                    "clearance_hole",
                    (x * d["pitch"] / 2, y * d["pitch"] / 2, 0),
                    diameter=d["hole"],
                    length=min(8, d["body_length"]),
                )
    elif family == "linear.bushing.ball":
        if p["style"] == "LME":
            raise ValueError(
                "LME needs verified metric/imperial profile data; use a vendor model"
            )
        t = tables["bushings"][str(p["bore"])]
        d.update(t)
        d["style"] = p["style"]
        port("shaft_bore", "cylindrical_bore", diameter=d["bore"], length=d["length"])
        port(
            "housing_seat",
            "cylindrical_surface",
            (0, 0, d["flange_thickness"] if p["style"] in ("LMF", "LMK") else 0),
            diameter=d["outside"],
            length=d["length"],
        )
        face_point = (d["outside"] + d["bore"]) / 4
        port("axial_face_min", "planar_face", (face_point, 0, 0), axis=(0, 0, -1))
        port(
            "axial_face_max",
            "planar_face",
            (face_point, 0, d["length"]),
            axis=(0, 0, 1),
        )
        if p["style"] in ("LMF", "LMK"):
            coords = (
                [
                    (d["flange_pitch"] / 2, 0),
                    (0, d["flange_pitch"] / 2),
                    (-d["flange_pitch"] / 2, 0),
                    (0, -d["flange_pitch"] / 2),
                ]
                if p["style"] == "LMF"
                else [
                    (x * d["flange_pitch"] / 2, y * d["flange_pitch"] / 2)
                    for x in (-1, 1)
                    for y in (-1, 1)
                ]
            )
            for i, (x, y) in enumerate(coords):
                port(
                    f"flange_hole_{i}",
                    "clearance_hole",
                    (x, y, 0),
                    diameter=d["flange_hole"],
                    length=d["flange_thickness"],
                )
    elif family == "drive.timing_pulley":
        pitch, depth = {
            "GT2": (2, 0.75),
            "HTD3M": (3, 1.2),
            "HTD5M": (5, 2.1),
            "HTD8M": (8, 3.4),
        }[p["profile"]]
        outside = pitch * p["teeth"] / math.pi - 2 * depth * 0.35
        if p["belt_width"] <= 0 or p["bore"] >= outside * 0.72:
            raise ValueError("invalid belt width or bore too large")
        d.update(
            pitch=pitch,
            outside_diameter=outside,
            pitch_diameter=pitch * p["teeth"] / math.pi,
            flange_diameter=outside + max(4, pitch) if p["flange"] else outside,
            bore=p["bore"],
            belt_width=p["belt_width"],
            flange=p["flange"],
            length=p["belt_width"] + (2 if p["flange"] else 0),
        )
        if p["bore"] > 0:
            port(
                "shaft_bore", "cylindrical_bore", diameter=p["bore"], length=d["length"]
            )
        port("pitch_reference", "pitch_cylinder", diameter=d["pitch_diameter"])
        ends(d["length"])
    elif family == "coupling.flexible":
        t = tables["couplings"][p["kind"] + "/" + str(p["size"])]
        d.update(t)
        d["bore_a"] = (
            min(t["max_bore"], max(4, t["max_bore"] * 0.5))
            if p["bore_a"] is None
            else p["bore_a"]
        )
        d["bore_b"] = d["bore_a"] if p["bore_b"] is None else p["bore_b"]
        if (
            min(d["bore_a"], d["bore_b"]) <= 0
            or max(d["bore_a"], d["bore_b"]) > t["max_bore"]
        ):
            raise ValueError("coupling bores outside profile range")
        half = (t["length"] - t["gap"]) / 2
        d["hub_length"] = half
        port("shaft_bore_a", "cylindrical_bore", diameter=d["bore_a"], length=half)
        port(
            "shaft_bore_b",
            "cylindrical_bore",
            (0, 0, half + t["gap"]),
            diameter=d["bore_b"],
            length=half,
        )
        ends(t["length"], "shaft_end_a", "shaft_end_b")
    elif family == "profile.round_rod":
        if min(p["diameter"], p["length"]) <= 0:
            raise ValueError("diameter and length must be positive")
        d.update(p)
        port("shaft", "cylindrical_surface", diameter=p["diameter"], length=p["length"])
        ends(p["length"])
    else:
        raise ValueError(
            f"{family} is catalog-only; native generation is not implemented"
        )
    return d, ports
