import json
import tempfile
import unittest
from pathlib import Path

from cad3 import mates, runtime
from cad3.project import Project


def port(pid, ptype, origin=(0, 0, 0), axis=(1, 0, 0), **dims):
    return {"id": pid, "type": ptype, "origin": list(origin), "axis": list(axis), **dims}


def decl(parts, mate_list, **extra):
    data = {"schema": mates.SCHEMA, "parts": {k: {"ports": v} for k, v in parts.items()},
            "mates": [{"a": a, "b": b} for a, b in mate_list]}
    data.update(extra)
    return mates.normalize(data)


class PairRuleTests(unittest.TestCase):
    def check(self, a, b):
        d = decl({"A": [a], "B": [b]}, [("A.%s" % a["id"], "B.%s" % b["id"])])
        return mates.check_declarations(d)["mates"][0]

    def test_nominal_fit_passes(self):
        r = self.check(port("s", "cylindrical_surface", diameter=8), port("b", "cylindrical_bore", diameter=8))
        self.assertEqual(r["verdict"], mates.PASS)

    def test_shaft_too_big_fails_either_order(self):
        for a, b in ((port("s", "cylindrical_surface", diameter=10), port("b", "cylindrical_bore", diameter=8)),
                     (port("b", "cylindrical_bore", diameter=8), port("s", "cylindrical_surface", diameter=10))):
            self.assertEqual(self.check(a, b)["verdict"], mates.FAIL)

    def test_offset_axes_fail_even_if_diameters_match(self):
        r = self.check(port("s", "cylindrical_surface", (0, 0, 0), diameter=8),
                       port("b", "cylindrical_bore", (0, 3, 0), diameter=8))
        self.assertEqual(r["verdict"], mates.FAIL)
        self.assertIn("offset", r["reason"])

    def test_crossed_axes_fail(self):
        r = self.check(port("s", "cylindrical_surface", axis=(1, 0, 0), diameter=8),
                       port("b", "cylindrical_bore", axis=(0, 1, 0), diameter=8))
        self.assertEqual(r["verdict"], mates.FAIL)

    def test_planes_must_oppose_and_touch(self):
        ok = self.check(port("t", "planar_face", (0, 0, 10), (0, 0, 1)),
                        port("u", "planar_face", (5, 5, 10), (0, 0, -1)))
        self.assertEqual(ok["verdict"], mates.PASS)
        gap = self.check(port("t", "planar_face", (0, 0, 10), (0, 0, 1)),
                         port("u", "planar_face", (0, 0, 12), (0, 0, -1)))
        self.assertEqual(gap["verdict"], mates.FAIL)

    def test_unknown_pairing_is_not_pass(self):
        r = self.check(port("t", "planar_face", axis=(0, 0, 1)), port("b", "cylindrical_bore", diameter=8))
        self.assertEqual(r["verdict"], mates.UNKNOWN)


class GraphTests(unittest.TestCase):
    def test_dangling_interface_and_excuse(self):
        parts = {"Screw": [port("j1", "cylindrical_surface", diameter=8),
                           port("j2", "cylindrical_surface", (100, 0, 0), diameter=8)],
                 "Brg": [port("bore", "cylindrical_bore", diameter=8)]}
        d = decl(parts, [("Screw.j1", "Brg.bore")])
        refs = [f["ref"] for f in mates.check_declarations(d)["findings"]]
        self.assertIn("Screw.j2", refs)
        d2 = decl(parts, [("Screw.j1", "Brg.bore")], unmated_ok={"Screw.j2": "free end"})
        self.assertNotIn("Screw.j2", [f["ref"] for f in mates.check_declarations(d2)["findings"]])

    def test_ground_connectivity(self):
        parts = {"Base": [port("t", "planar_face", axis=(0, 0, 1))],
                 "A": [port("b", "planar_face", axis=(0, 0, -1)), port("s", "cylindrical_surface", diameter=5)],
                 "B": [port("h", "cylindrical_bore", diameter=5)],
                 "C": [port("x", "cylindrical_bore", diameter=5)],
                 "D": [port("y", "cylindrical_surface", diameter=5)]}
        d = decl(parts, [("Base.t", "A.b"), ("A.s", "B.h"), ("C.x", "D.y")], ground="Base")
        kinds = {(f["kind"], f["ref"]) for f in mates.check_declarations(d)["findings"]}
        self.assertIn(("not_connected_to_ground", "C"), kinds)
        self.assertIn(("not_connected_to_ground", "D"), kinds)
        self.assertNotIn(("not_connected_to_ground", "B"), kinds)

    def test_bad_reference_fails(self):
        d = decl({"A": [port("p", "planar_face")]}, [("A.p", "Z.q")])
        self.assertEqual(mates.check_declarations(d)["mates"][0]["verdict"], mates.FAIL)

    def test_report_overall_and_empty(self):
        d = decl({"A": [port("p", "planar_face")]}, [])
        r = mates.report(d, geometry_skipped="test")
        self.assertEqual(r["overall"], mates.WARN)


class NormalizeTests(unittest.TestCase):
    def test_rejects_bad_input(self):
        for bad in ({}, {"parts": {}}, {"parts": {"A": {"ports": [{"type": "planar_face"}]}}},
                    {"parts": {"A": {"ports": [{"id": "p", "axis": [0, 0, 0]}]}}},
                    {"schema": "other/v9", "parts": {"A": {"ports": []}}},
                    {"parts": {"A": {"ports": []}}, "ground": "Nope"}):
            with self.subTest(bad=bad), self.assertRaises(mates.DeclarationError):
                mates.normalize(bad)

    def test_axis_is_normalised(self):
        d = decl({"A": [port("p", "planar_face", axis=(0, 0, 5))]}, [])
        self.assertEqual(d["parts"]["A"]["ports"]["p"]["axis"], (0.0, 0.0, 1.0))


class RuntimeHostPathTests(unittest.TestCase):
    def test_missing_file_explains_format(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Project.init(Path(tmp) / "p", name="t")
            result = runtime.mates(project, geometry=False)
        self.assertFalse(result["ok"])
        self.assertIn("format", result)

    def test_no_geometry_runs_without_freecad(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Project.init(Path(tmp) / "p", name="t")
            data = {"parts": {"A": {"ports": [port("p", "planar_face")]}}}
            (project.root / "notes" / "interfaces.json").write_text(json.dumps(data))
            result = runtime.mates(project, geometry=True)  # no document yet -> host only
        self.assertTrue(result["ok"])
        self.assertEqual(result["mates"]["geometry"], {"skipped": "no model document yet"})


class DocumentationTests(unittest.TestCase):
    def _example(self, path, pattern=r"```json\n(.*?)```"):
        import re
        text = (Path(__file__).resolve().parents[1] / path).read_text(encoding="utf-8")
        block = re.search(pattern, text, re.S)
        self.assertIsNotNone(block, f"no example JSON block found in {path}")
        return mates.normalize(json.loads(block.group(1)))

    def test_design_doc_example_stays_valid(self):
        """The example in docs/interfaces-design.md must keep passing the checker."""
        decl = self._example("docs/interfaces-design.md", r"## 声明示例.*?```json\n(.*?)```")
        result = mates.report(decl, geometry_skipped="doc example")
        self.assertEqual(result["overall"], mates.PASS, result["problems"])

    def test_skill_reference_example_raises_one_dangling_interface(self):
        decl = self._example(".agents/skills/freecad-v3/references/interfaces.md")
        findings = mates.check_declarations(decl)["findings"]
        self.assertEqual([f["ref"] for f in findings], ["LeadScrew.journal_idler"])
        self.assertEqual(findings[0]["kind"], "unmated_interface")


if __name__ == "__main__":
    unittest.main()
