import unittest
import tempfile
from pathlib import Path

from cad3 import catalog


class CatalogTests(unittest.TestCase):
    def test_index_has_v2_families_without_importing_cad_kernel(self):
        index = catalog.load_catalog_index()
        self.assertEqual(index["schema"], catalog.INDEX_SCHEMA)
        self.assertGreaterEqual(len(index["entries"]), 324)
        self.assertEqual(
            catalog.search("6204", limit=1)[0]["id"], "bearing.deep_groove.6204"
        )

    def test_search_supports_chinese_and_category(self):
        results = catalog.search("NEMA17", category="motor", limit=3)
        self.assertEqual(results[0]["id"], "motor.stepper.square.42")
        self.assertTrue(catalog.search("直线轴承", category="linear"))

    def test_describe_is_progressive_and_instance_has_interfaces(self):
        described = catalog.describe("6204")
        self.assertEqual(described["entry"]["id"], "bearing.deep_groove.6204")
        self.assertNotIn("items", described["family"])
        instance = catalog.instantiate(
            "6204", selections={"closure": "2RS", "clearance": "C3"}
        )
        self.assertEqual(instance["schema"], catalog.INSTANCE_SCHEMA)
        bore = next(item for item in instance["ports"] if item["id"] == "shaft_bore")
        self.assertEqual(bore["diameter"], 20)
        self.assertEqual(instance["selection"]["closure"], "2RS")

    def test_unknown_and_pinned_override_are_explicit(self):
        with self.assertRaises(catalog.CatalogError):
            catalog.describe("invented-bearing")
        with self.assertRaises(catalog.CatalogError):
            catalog.instantiate("6204", parameters={"code": "6205"})

    def test_compare_is_compact_json(self):
        result = catalog.compare("6204", "6304")
        self.assertEqual(result["schema"], "cad3.catalog-comparison/v1")
        self.assertEqual(
            [item["id"] for item in result["candidates"]],
            ["bearing.deep_groove.6204", "bearing.deep_groove.6304"],
        )

    def test_native_proxy_contracts_cover_common_assembly_parts(self):
        examples = [
            ("608ZZ", {}, 4),
            ("fastener.hex_bolt_metric", {"size": "M6", "length": 20}, 2),
            ("fastener.hex_nut_metric", {"size": "M6"}, 3),
            ("fastener.plain_washer_metric", {"size": "M6"}, 3),
            ("NEMA17", {}, 7),
            ("LMF20", {}, 8),
            ("GT2-20T-5", {}, 4),
            (
                "jaw_coupling",
                {"kind": "jaw", "size": "19", "bore_a": 8, "bore_b": 14},
                4,
            ),
            ("profile.round_rod", {"diameter": 8, "length": 100}, 3),
        ]
        for identifier, parameters, port_count in examples:
            with self.subTest(identifier=identifier):
                result = catalog.instantiate(identifier, parameters=parameters)
                self.assertEqual(len(result["ports"]), port_count)
                self.assertTrue(
                    all("origin" in port and "axis" in port for port in result["ports"])
                )
                self.assertTrue(result["geometry"]["omitted"])

    def test_catalog_only_part_and_unverified_family_are_not_generated(self):
        with self.assertRaisesRegex(catalog.CatalogError, "catalog contract"):
            catalog.instantiate(
                "gear.spur",
                parameters={"module": 2, "teeth": 20, "bore": 8, "face_width": 10},
            )
        with self.assertRaisesRegex(catalog.CatalogError, "LME metric/imperial"):
            catalog.instantiate("LME20")

    def test_cli_catalog_commands_do_not_need_project_or_freecad(self):
        from cad3 import cli
        import contextlib
        import io
        import json

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = cli.main(
                [
                    "catalog",
                    "instantiate",
                    "fastener.hex_bolt_metric",
                    "--parameter",
                    "size=M6",
                    "--parameter",
                    "length=20",
                ]
            )
        self.assertEqual(code, 0)
        data = json.loads(output.getvalue())
        self.assertEqual(data["dimensions_mm"]["length"], 20)

    def test_native_parts_survive_reopen_and_ports_follow_placement(self):
        from cad3.project import Project
        from cad3 import runtime

        with tempfile.TemporaryDirectory(prefix="cad3-parts-") as directory:
            project = Project.init(Path(directory) / "project", name="catalog smoke")
            first = project.root / "src" / "main.py"
            first.write_text(
                """from cad3.parts import add
parts = [
    add(doc, '608ZZ', name='Bearing'),
    add(doc, 'fastener.hex_bolt_metric', name='Bolt', parameters={'size':'M6','length':20}),
    add(doc, 'NEMA17', name='Motor'),
    add(doc, 'LMF20', name='Bushing'),
    add(doc, 'GT2-20T-5', name='Pulley'),
    add(doc, 'jaw_coupling', name='Coupling', parameters={'kind':'jaw','size':'19','bore_a':8,'bore_b':14}),
]
cad_outputs = parts
""",
                encoding="utf-8",
            )
            built = runtime.execute(
                project, executable=runtime.discover_freecad(), timeout=45
            )
            self.assertTrue(built["ok"], built.get("errors"))
            reopen = project.root / "src" / "verify.py"
            reopen.write_text(
                """import json
from cad3.parts import contract, declaration
from cad3.mates import normalize
obj = doc.getObject('Bearing')
assert obj is not None and getattr(obj, 'Proxy', None) is None
saved = contract(obj)
assert saved['catalog_id'] == 'bearing.deep_groove.608'
assert obj.Shape.isValid() and len(obj.Shape.Solids) == 1
ports = declaration(obj)['ports']
normalized = normalize({'parts': {'Bearing': {'object': obj.Name, 'ports': ports}}})
assert normalized['parts']['Bearing']['ports']['shaft_bore']['dims']['diameter'] == 8
obj.Placement.Base = App.Vector(10, 20, 30)
port = next(p for p in declaration(obj)['ports'] if p['id'] == 'shaft_bore')
assert port['origin'] == [10.0, 20.0, 30.0], port
""",
                encoding="utf-8",
            )
            checked = runtime.execute(
                project, reopen, executable=runtime.discover_freecad(), timeout=45
            )
            self.assertTrue(checked["ok"], checked.get("errors"))


if __name__ == "__main__":
    unittest.main()
