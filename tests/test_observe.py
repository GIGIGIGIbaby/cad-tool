"""Tests for cad3.observe and cad3.rendering.

Two layers:

1. Host unit tests against a small fake of the *measured* FreeCAD object
   surface (no FreeCAD, no VTK, no GUI).  These run everywhere:
   ``python3 -m unittest tests.test_observe -v`` or ``pytest tests/test_observe.py``.
2. One env-gated integration test that runs a real, isolated ``freecadcmd``
   subprocess (own FREECAD_USER_HOME + --user-cfg/--system-cfg inside a fresh
   temp dir) and checks observation + rendering against real native geometry.
   Enable with ``CAD3_REAL_FREECAD_TEST=1`` (skipped by default).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / 'src'
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from cad3 import observe, rendering            # noqa: E402  (namespace package)


# ==========================================================================
# fake FreeCAD surface (only what cad3.observe/rendering actually reads)
# ==========================================================================
class FakeBoundBox:
    def __init__(self, minimum, maximum):
        self.XMin, self.YMin, self.ZMin = minimum
        self.XMax, self.YMax, self.ZMax = maximum

    @property
    def XLength(self):
        return self.XMax - self.XMin

    @property
    def YLength(self):
        return self.YMax - self.YMin

    @property
    def ZLength(self):
        return self.ZMax - self.ZMin

    @property
    def DiagonalLength(self):
        return (self.XLength ** 2 + self.YLength ** 2 + self.ZLength ** 2) ** 0.5


class FakeVector:
    def __init__(self, x=0.0, y=0.0, z=0.0):
        self.x, self.y, self.z = float(x), float(y), float(z)


def _fake_tessellation(tessellation, error):
    if error is not None:
        raise error
    if tessellation is not None:
        vertices, triangles = tessellation
        return [FakeVector(*point) for point in vertices], [tuple(t) for t in triangles]
    # a unit tetrahedron
    vertices = [(0, 0, 0), (10, 0, 0), (0, 10, 0), (0, 0, 10)]
    triangles = [(0, 1, 2), (0, 1, 3), (0, 2, 3), (1, 2, 3)]
    return [FakeVector(*point) for point in vertices], triangles


class FakeSolid:
    def __init__(self, volume, tessellation=None, tessellation_error=None):
        self.Volume = volume
        self._tessellation = tessellation
        self._tessellation_error = tessellation_error

    def tessellate(self, tolerance):            # noqa: ARG002 - native signature
        return _fake_tessellation(self._tessellation, self._tessellation_error)


class FakeFace:
    def __init__(self, surface_name):
        surface_class = type(surface_name, (), {})
        self.Surface = surface_class()


class FakeShape:
    """Mimics the native Part.Shape surface cad3 reads."""

    def __init__(self, shape_type='Solid', *, solid_volumes=(1000.0,), faces=None,
                 surface_types=None, area=600.0, bbox=((0.0, 0.0, 0.0), (10.0, 10.0, 10.0)),
                 valid=True, null=False, edges=None, vertices=None, shells=None,
                 element_map=None, element_map_version='1.0',
                 tessellation=None, tessellation_error=None,
                 solid_tessellations=None, solid_errors=None):
        self.ShapeType = shape_type
        self._null = null
        self._valid = valid
        self._solid_volumes = list(solid_volumes)
        if surface_types is None:
            surface_types = ['Plane'] * (faces if faces is not None else 6)
        self._faces = [FakeFace(name) for name in surface_types]
        self.Area = area
        self.BoundBox = FakeBoundBox(*bbox)
        self._edges = edges if edges is not None else len(self._faces)
        self._vertices = vertices if vertices is not None else len(self._faces) * 2
        self._shells = shells if shells is not None else len(self._solid_volumes)
        self.ElementMap = element_map if element_map is not None else {}
        self.ElementMapVersion = element_map_version
        self._tessellation = tessellation
        self._tessellation_error = tessellation_error
        self._solid_tessellations = list(solid_tessellations or [])
        self._solid_errors = list(solid_errors or [])

    def isNull(self):
        return self._null

    def isValid(self):
        return self._valid

    @property
    def Solids(self):
        solids = []
        for index, volume in enumerate(self._solid_volumes):
            tessellation = (self._solid_tessellations[index]
                            if index < len(self._solid_tessellations)
                            else self._tessellation)
            error = (self._solid_errors[index]
                     if index < len(self._solid_errors)
                     else self._tessellation_error)
            solids.append(FakeSolid(volume, tessellation, error))
        return solids

    @property
    def Faces(self):
        return list(self._faces)

    @property
    def Edges(self):
        return list(range(self._edges))

    @property
    def Vertexes(self):
        return list(range(self._vertices))

    @property
    def Shells(self):
        return list(range(self._shells))

    @property
    def Volume(self):
        return sum(self._solid_volumes)

    def tessellate(self, tolerance):            # noqa: ARG002 - native signature
        return _fake_tessellation(self._tessellation, self._tessellation_error)


LINK_TYPE_IDS = {
    'App::PropertyLink', 'App::PropertyLinkList', 'App::PropertyLinkSub',
    'App::PropertyLinkSubList', 'App::PropertyLinkChild', 'App::PropertyLinkGlobal',
    'App::PropertyLinkHidden', 'App::PropertyLinkSubHidden',
}


def _linked_objects(value):
    out = []
    if value is None:
        return out
    if hasattr(value, 'Name'):
        return [value]
    if isinstance(value, (list, tuple)):
        for item in value:
            out.extend(_linked_objects(item))
    return out


class FakeObject:
    """Base fake document object with native-shaped property access."""

    def __init__(self, name, type_id, *, label=None, state=('Up-to-date',), shape=None,
                 shape_in_props=True, props=None, expressions=None, group=None,
                 visibility=True, enums=None, document=None):
        self.Name = name
        self.Label = label or name
        self.TypeId = type_id
        self.State = list(state)
        self.Visibility = visibility
        self.Document = document
        self._props = dict(props or {})
        self._expressions = dict(expressions or {})
        self._enums = dict(enums or {})
        self._group = list(group or [])
        if shape is not None:
            self.Shape = shape
            if shape_in_props:
                self._props.setdefault('Shape', ('Part::PropertyPartShape', shape))
        self._props.setdefault('Label2', ('App::PropertyString', ''))
        self._props.setdefault('Visibility', ('App::PropertyBool', bool(visibility)))

    # -- native property API ------------------------------------------------
    @property
    def ExpressionEngine(self):
        return list(self._expressions.items())

    @property
    def PropertiesList(self):
        names = ['Label', 'Label2', 'ExpressionEngine'] + list(self._props)
        seen, out = set(), []
        for name in names:
            if name not in seen:
                seen.add(name)
                out.append(name)
        return out

    def getTypeIdOfProperty(self, prop):
        if prop == 'Label':
            return 'App::PropertyString'
        if prop == 'ExpressionEngine':
            return 'App::PropertyExpressionEngine'
        if prop == 'Group':
            return 'App::PropertyLinkList'
        if prop not in self._props:
            raise ValueError(f'no property {prop}')
        return self._props[prop][0]

    def getPropertyByName(self, prop):
        if prop == 'Label':
            return self.Label
        if prop == 'ExpressionEngine':
            return list(self._expressions.items())
        if prop == 'Group':
            return list(self._group)
        if prop not in self._props:
            raise ValueError(f'no property {prop}')
        return self._props[prop][1]

    def getGroupOfProperty(self, prop):         # noqa: ARG002
        return 'Base'

    def getEnumerationsOfProperty(self, prop):
        return list(self._enums.get(prop, []))

    # -- dependency surface -------------------------------------------------
    @property
    def Group(self):
        return list(self._group)

    @property
    def OutList(self):
        # measured native behaviour: duplicated and unstable, so duplicates are
        # returned on purpose to prove cad3 de-duplicates them
        found = []
        for prop, (type_id, value) in self._props.items():
            if prop.startswith('_'):
                # measured: FreeCAD does not list hidden back-references such as
                # PartDesign's `_Body` in OutList
                continue
            if type_id in LINK_TYPE_IDS:
                found.extend(_linked_objects(value))
        found.extend(self._group)
        doubled = []
        for item in found:
            doubled.extend([item, item])
        return doubled

    @property
    def InList(self):
        if self.Document is None:
            return []
        return [other for other in self.Document.Objects
                if any(item is self for item in other.OutList)]

    @property
    def OutListRecursive(self):
        seen, stack, out = {self.Name}, list(self.OutList), []
        while stack:
            item = stack.pop()
            if item.Name in seen:
                continue
            seen.add(item.Name)
            out.append(item)
            stack.extend(item.OutList)
        return out

    @property
    def InListRecursive(self):
        if self.Document is None:
            return []
        return [other for other in self.Document.Objects
                if self in other.OutListRecursive]

    def __repr__(self):
        return f'<{self.TypeId} {self.Name}>'


class FakeBody(FakeObject):
    def __init__(self, name, *, group=None, tip=None, shape=None, **kwargs):
        super().__init__(name, 'PartDesign::Body', group=group, shape=shape,
                         shape_in_props=False, **kwargs)
        self._tips = [tip] if tip is not None else []
        self._props.setdefault('Tip', ('App::PropertyLink',
                                       tip if tip is not None else None))

    @property
    def Tip(self):
        return self._tips[0] if self._tips else None


class FakePart(FakeObject):
    def __init__(self, name, **kwargs):
        super().__init__(name, 'App::Part', shape_in_props=False, **kwargs)


class FakeGroup(FakeObject):
    def __init__(self, name, **kwargs):
        super().__init__(name, 'App::DocumentObjectGroup', shape_in_props=False,
                         **kwargs)


_UNSET = object()


class FakeFeature(FakeObject):
    def __init__(self, name, type_id='Part::Feature', shape=_UNSET, **kwargs):
        if shape is _UNSET:
            shape = FakeShape()
        super().__init__(name, type_id, shape=shape, **kwargs)


class FakeSpreadsheet(FakeObject):
    def __init__(self, name, cells=None, *, recomputed=True, **kwargs):
        super().__init__(name, 'Spreadsheet::Sheet', **kwargs)
        self._cells = dict(cells or {})       # addr -> {'raw':..,'alias':..,'value':..}
        self._recomputed = recomputed

    def getUsedCells(self):
        return sorted(self._cells)

    def getContents(self, addr):
        return self._cells.get(addr, {}).get('raw', '')

    def getAlias(self, addr):
        return self._cells.get(addr, {}).get('alias')

    def get(self, addr):
        if addr not in self._cells:
            raise ValueError(f'Invalid cell address or property: {addr}')
        if not self._recomputed:
            raise ValueError('Invalid cell address or property: %s' % addr)
        return self._cells[addr].get('value', '')


class FakeConstraint:
    def __init__(self, type_, *, name='', value=0.0, first=0, first_pos=0,
                 second=-2000, second_pos=0, driving=True, active=True,
                 virtual=False):
        self.Type = type_
        self.Name = name
        self.Value = value
        self.First = first
        self.FirstPos = first_pos
        self.Second = second
        self.SecondPos = second_pos
        self.Third = -2000
        self.ThirdPos = 0
        self.Driving = driving
        self.IsActive = active
        self.InVirtualSpace = virtual


class FakeGeometry:
    def __init__(self, type_name, **values):
        self._type_name = type_name
        for key, value in values.items():
            setattr(self, key, value)

    def __repr__(self):
        return f'<{self._type_name}>'


class FakeSketch(FakeObject):
    def __init__(self, name, *, geometry=None, constraints=None, dof=0, fully=False,
                 conflicting=None, redundant=None, partially=None,
                 diagnostics=True, construction=None, external=None,
                 constraint_expressions=None, **kwargs):
        shape = kwargs.pop('shape', FakeShape(shape_type='Wire', solid_volumes=(),
                                              surface_types=[], area=0.0,
                                              bbox=((0, 0, 0), (10, 0, 0))))
        super().__init__(name, 'Sketcher::SketchObject', shape=shape, **kwargs)
        self.Geometry = list(geometry or [])
        self.Constraints = list(constraints or [])
        self.GeometryCount = len(self.Geometry)
        self.ConstraintCount = len(self.Constraints)
        self._construction = list(construction or [])
        self.ExternalGeometry = list(external or [])
        self._expressions.update(dict(constraint_expressions or {}))
        if diagnostics:
            self.DoF = dof
            self.FullyConstrained = fully
            self.ConflictingConstraints = list(conflicting or [])
            self.RedundantConstraints = list(redundant or [])
            self.PartiallyRedundantConstraints = list(partially or [])

    def getConstruction(self, index):
        if index < len(self._construction):
            return bool(self._construction[index])
        return False


class FakeDocument:
    def __init__(self, name='FakeDoc'):
        self.Name = name
        self.Label = name
        self.FileName = ''
        self.Objects = []

    def addObject(self, obj):
        obj.Document = self
        self.Objects.append(obj)
        return obj

    def getObject(self, name):
        for obj in self.Objects:
            if obj.Name == name:
                return obj
        return None

    @property
    def RootObjects(self):
        return [obj for obj in self.Objects if not obj.InList]


def install_fake_freecad():
    fake = types.ModuleType('FreeCAD')
    fake.Version = lambda: ['1', '1', '3', 'fake', 'Unknown', '2026/07/25',
                            'grafted', 'deadbeef']
    fake.GuiUp = 0
    previous = sys.modules.get('FreeCAD')
    sys.modules['FreeCAD'] = fake
    return previous


# ==========================================================================
# selection tests
# ==========================================================================
class SelectOutputObjectsTest(unittest.TestCase):
    def test_empty_document_returns_empty_list(self):
        self.assertEqual(observe.select_output_objects(FakeDocument()), [])

    def test_none_document_raises(self):
        with self.assertRaises(observe.ObserveError):
            observe.select_output_objects(None)

    def test_body_wins_over_its_tip_and_features(self):
        doc = FakeDocument('BodyDoc')
        sketch = FakeSketch('BaseSketch')
        pad = FakeFeature('Plate', 'PartDesign::Pad',
                          shape=FakeShape(solid_volumes=(100.0,)))
        pad._props['Profile'] = ('App::PropertyLinkSub', (sketch, []))
        tip = FakeFeature('Hole', 'PartDesign::Pocket',
                          shape=FakeShape(solid_volumes=(80.0,)))
        tip._props['Profile'] = ('App::PropertyLinkSub', (sketch, []))
        body = FakeBody('Body', group=[sketch, pad, tip], tip=tip,
                        shape=FakeShape(solid_volumes=(80.0,)))
        origin = FakeObject('Origin', 'App::Origin')
        plane = FakeObject('XY_Plane', 'App::Plane',
                           shape=FakeShape(shape_type='Face', solid_volumes=(),
                                           bbox=((0, 0, 0), (2e100, 2e100, 0)),
                                           area=8e100))
        for obj in (origin, plane):
            doc.addObject(obj)
        for obj in (body, sketch, pad, tip):
            doc.addObject(obj)
        self.assertEqual([obj.Name for obj in observe.select_output_objects(doc)],
                         ['Body'])

    def test_boolean_cut_keeps_result_and_drops_inputs(self):
        doc = FakeDocument('CutDoc')
        box = FakeFeature('Box', 'Part::Box', shape=FakeShape(solid_volumes=(1000.0,)))
        cyl = FakeFeature('Cyl', 'Part::Cylinder',
                          shape=FakeShape(solid_volumes=(31.4,)))
        cut = FakeFeature('Cut', 'Part::Cut',
                          shape=FakeShape(shape_type='Compound',
                                          solid_volumes=(968.6,)))
        cut._props['Base'] = ('App::PropertyLink', box)
        cut._props['Tool'] = ('App::PropertyLink', cyl)
        for obj in (box, cyl, cut):
            doc.addObject(obj)
        self.assertEqual([obj.Name for obj in observe.select_output_objects(doc)],
                         ['Cut'])

    def test_app_part_with_aggregate_shape_is_kept(self):
        doc = FakeDocument('PartDoc')
        body = FakeBody('Body', shape=FakeShape(shape_type='Compound',
                                                solid_volumes=(100.5,)))
        part = FakePart('Assembly', group=[body],
                        shape=FakeShape(shape_type='Compound', solid_volumes=(100.5,)))
        doc.addObject(body)
        doc.addObject(part)
        self.assertEqual([obj.Name for obj in observe.select_output_objects(doc)],
                         ['Assembly'])

    def test_container_without_shape_descends_to_children(self):
        doc = FakeDocument('NullPartDoc')
        body_one = FakeBody('Body1', shape=FakeShape())
        body_two = FakeBody('Body2', shape=FakeShape())
        part = FakePart('Assembly', group=[body_one, body_two], shape=None)
        doc.addObject(body_one)
        doc.addObject(body_two)
        doc.addObject(part)
        self.assertEqual([obj.Name for obj in observe.select_output_objects(doc)],
                         ['Body1', 'Body2'])

    def test_group_aggregate_shape_is_kept(self):
        doc = FakeDocument('GroupDoc')
        first = FakeFeature('G1', 'Part::Box')
        second = FakeFeature('G2', 'Part::Box')
        group = FakeGroup('Grp', group=[first, second],
                          shape=FakeShape(shape_type='Compound',
                                          solid_volumes=(1000.0, 1000.0)))
        for obj in (first, second, group):
            doc.addObject(obj)
        self.assertEqual([obj.Name for obj in observe.select_output_objects(doc)],
                         ['Grp'])

    def test_independent_solids_are_both_kept(self):
        doc = FakeDocument('TwoSolids')
        doc.addObject(FakeFeature('A', 'Part::Box'))
        doc.addObject(FakeFeature('B', 'Part::Box'))
        self.assertEqual([obj.Name for obj in observe.select_output_objects(doc)],
                         ['A', 'B'])

    def test_expression_only_dependency_does_not_hide_a_solid(self):
        doc = FakeDocument('ExprDoc')
        first = FakeFeature('E1', 'Part::Box')
        second = FakeFeature('E2', 'Part::Box',
                             expressions={'Length': 'E1.Length'})
        doc.addObject(first)
        doc.addObject(second)
        self.assertEqual([obj.Name for obj in observe.select_output_objects(doc)],
                         ['E1', 'E2'])

    def test_hidden_body_back_reference_does_not_consume_the_body(self):
        # measured on FreeCAD 1.1.3: PartDesign::Pad exposes `_Body` as a hidden
        # PropertyLinkHidden pointing back at its Body.  It must not count as
        # consumption, or every Body would disappear from the output.
        doc = FakeDocument('BackRef')
        sketch = FakeSketch('BaseSketch')
        pad = FakeFeature('Plate', 'PartDesign::Pad',
                          shape=FakeShape(solid_volumes=(100.0,)))
        pad._props['Profile'] = ('App::PropertyLinkSub', (sketch, []))
        body = FakeBody('Body', group=[sketch, pad], tip=pad,
                        shape=FakeShape(solid_volumes=(100.0,)))
        pad._props['_Body'] = ('App::PropertyLinkHidden', body)
        for obj in (sketch, pad, body):
            doc.addObject(obj)
        self.assertEqual([obj.Name for obj in observe.select_output_objects(doc)],
                         ['Body'])

    def test_feature_inside_a_broken_body_is_not_an_output(self):
        # measured: a failed Tip (Invalid) leaves Body.Shape null while the
        # earlier Pad still carries a cached valid shape.  The intermediate
        # feature must NOT become the render/export output of that Body.
        doc = FakeDocument('BrokenBody')
        sketch = FakeSketch('Sketch')
        plate = FakeFeature('Plate', 'PartDesign::Pad',
                            shape=FakeShape(solid_volumes=(100.0,)))
        plate._props['Profile'] = ('App::PropertyLinkSub', (sketch, []))
        bad = FakeFeature('BadFillet', 'PartDesign::Fillet', shape=None,
                          state=('Touched', 'Invalid'))
        bad._props['Base'] = ('App::PropertyLinkSub', (plate, ['Edge99']))
        body = FakeBody('Body', group=[sketch, plate, bad], tip=bad, shape=None,
                        state=('Touched', 'Invalid'))
        for obj in (sketch, plate, bad, body):
            doc.addObject(obj)
        self.assertEqual(observe.select_output_objects(doc), [])

    def test_link_consumes_its_linked_object(self):
        doc = FakeDocument('LinkDoc')
        body = FakeBody('Body', shape=FakeShape())
        link = FakeObject('Link', 'App::Link', shape=FakeShape(), shape_in_props=False)
        link._props['LinkedObject'] = ('App::PropertyLink', body)
        doc.addObject(body)
        doc.addObject(link)
        self.assertEqual([obj.Name for obj in observe.select_output_objects(doc)],
                         ['Link'])

    def test_sketch_and_datum_only_document_has_no_outputs(self):
        doc = FakeDocument('SketchOnly')
        doc.addObject(FakeSketch('Sketch'))
        doc.addObject(FakeObject('Origin', 'App::Origin'))
        doc.addObject(FakeSpreadsheet('Params'))
        self.assertEqual(observe.select_output_objects(doc), [])

    def test_explicit_names_preserve_order_and_deduplicate(self):
        doc = FakeDocument('ExplicitDoc')
        first = FakeFeature('A', 'Part::Box')
        second = FakeFeature('B', 'Part::Box')
        sketch = FakeSketch('Sketch')
        for obj in (first, second, sketch):
            doc.addObject(obj)
        picked = observe.select_output_objects(doc, ['B', 'A', 'B'])
        self.assertEqual([obj.Name for obj in picked], ['B', 'A'])
        by_object = observe.select_output_objects(doc, [sketch, 'A'])
        self.assertEqual([obj.Name for obj in by_object], ['Sketch', 'A'])
        single = observe.select_output_objects(doc, 'A')
        self.assertEqual([obj.Name for obj in single], ['A'])

    def test_explicit_unknown_name_raises_instead_of_dropping(self):
        doc = FakeDocument('UnknownDoc')
        doc.addObject(FakeFeature('A', 'Part::Box'))
        with self.assertRaises(observe.ObserveError) as ctx:
            observe.select_output_objects(doc, ['A', 'Nope'])
        self.assertIn('Nope', str(ctx.exception))
        self.assertIn('A', str(ctx.exception))

    def test_empty_name_sequence_means_default_selection(self):
        doc = FakeDocument('EmptyNames')
        doc.addObject(FakeFeature('A', 'Part::Box'))
        picked = observe.select_output_objects(doc, [])
        self.assertEqual([obj.Name for obj in picked], ['A'])


# ==========================================================================
# inspection tests
# ==========================================================================
class InspectDocumentTest(unittest.TestCase):
    def setUp(self):
        self.previous_freecad = install_fake_freecad()

    def tearDown(self):
        if self.previous_freecad is None:
            sys.modules.pop('FreeCAD', None)
        else:
            sys.modules['FreeCAD'] = self.previous_freecad

    def build_document(self):
        doc = FakeDocument('InspectDoc')
        sketch = FakeSketch('BaseSketch',
                            geometry=[FakeGeometry('Circle', Center=FakeVector(),
                                                   Radius=6.0)],
                            constraints=[FakeConstraint('Radius', name='R1', value=6.0)],
                            dof=0, fully=True)
        pad = FakeFeature('Plate', 'PartDesign::Pad', shape=FakeShape(
            solid_volumes=(1130.97,), bbox=((0, 0, 0), (12, 12, 10))))
        pad._props['Profile'] = ('App::PropertyLinkSub', (sketch, []))
        pad._props['Length'] = ('App::PropertyLength', FakeQuantity(10.0, '10.00 mm'))
        pad._props['Type'] = ('App::PropertyEnumeration', 'Length')
        pad._enums['Type'] = ['Length', 'UpToLast', 'ThroughAll']
        pad._expressions['Length'] = 'Params.PlateLength'
        tip = FakeFeature('Hole', 'PartDesign::Pocket', shape=FakeShape(
            solid_volumes=(1005.31,), surface_types=['Plane', 'Plane', 'Cylinder']))
        tip._props['Profile'] = ('App::PropertyLinkSub', (sketch, []))
        body = FakeBody('Body', group=[sketch, pad, tip], tip=tip,
                        shape=FakeShape(solid_volumes=(1005.31,),
                                        surface_types=['Plane'] * 3 + ['Cylinder'] * 2))
        plane = FakeObject('XY_Plane', 'App::Plane',
                           shape=FakeShape(shape_type='Face', solid_volumes=(),
                                           area=8e100,
                                           bbox=((0, 0, 0), (2e100, 2e100, 0))))
        badly = FakeFeature('BadFillet', 'PartDesign::Fillet', shape=None,
                            state=('Touched', 'Invalid'))
        sheet = FakeSpreadsheet('Params', cells={
            'B1': {'raw': '=40 mm', 'alias': 'PlateLength', 'value': '40.0 mm'},
        }, recomputed=True)
        for obj in (sketch, pad, tip, body, plane, badly, sheet):
            doc.addObject(obj)
        return doc

    def test_schema_document_objects_outputs_and_json_safety(self):
        state = observe.inspect_document(self.build_document())
        self.assertEqual(state['schema'], 'cad3.observation/v1')
        self.assertIn('name', state['document'])
        self.assertIsInstance(state['objects'], list)
        self.assertTrue(state['objects'])
        for record in state['objects']:
            for key in ('name', 'type', 'label', 'state', 'geometry'):
                self.assertIn(key, record)
        self.assertEqual(state['outputs'], ['Body'])
        self.assertEqual(state['document']['outputs'], ['Body'])
        text = json.dumps(state, allow_nan=False, ensure_ascii=False)
        self.assertNotIn('NaN', text)
        self.assertNotIn('Infinity', text)

    def test_compact_is_smaller_and_detail_expands_objects(self):
        compact = observe.inspect_document(self.build_document())
        detailed = observe.inspect_document(self.build_document(), detail=True)
        plate_compact = next(item for item in compact['objects']
                             if item['name'] == 'Plate')
        plate_detail = next(item for item in detailed['objects']
                            if item['name'] == 'Plate')
        self.assertNotIn('parameters', plate_compact)
        self.assertFalse(plate_compact['expanded'])
        self.assertTrue(plate_detail['expanded'])
        self.assertIn('parameters', plate_detail)
        self.assertEqual(plate_detail['parameters']['Length']['value'], 10.0)
        self.assertEqual(plate_detail['parameters']['Length']['unit'], 'mm')
        self.assertEqual(plate_detail['parameters']['Length']['user_string'], '10.00 mm')
        self.assertEqual(plate_detail['parameters']['Type']['value'], 'Length')
        self.assertEqual(plate_detail['parameters']['Type']['enum'],
                         ['Length', 'UpToLast', 'ThroughAll'])
        self.assertEqual(plate_detail['links']['Profile'], ['BaseSketch'])
        self.assertEqual(plate_detail['expressions']['Length'], 'Params.PlateLength')
        self.assertEqual(plate_detail['expression_object_refs'], ['Params'])
        self.assertLess(len(json.dumps(compact)), len(json.dumps(detailed)))

    def test_object_name_scopes_and_expands_one_object(self):
        state = observe.inspect_document(self.build_document(), object_name='Plate')
        self.assertEqual(len(state['objects']), 1)
        record = state['objects'][0]
        self.assertEqual(record['name'], 'Plate')
        self.assertTrue(record['expanded'])
        self.assertIn('parameters', record)
        self.assertEqual(state['document']['scoped_object'], 'Plate')
        self.assertFalse(record['is_output'])
        self.assertEqual(record['container'], 'Body')
        body_state = observe.inspect_document(self.build_document(), object_name='Body')
        container = body_state['objects'][0]
        self.assertTrue(container['is_output'])
        self.assertIsNone(container['container'])
        self.assertEqual(container['containment']['tip'], 'Hole')

    def test_unknown_object_name_raises_with_available_names(self):
        with self.assertRaises(observe.ObserveError) as ctx:
            observe.inspect_document(self.build_document(), object_name='Nope')
        self.assertIn('Nope', str(ctx.exception))
        self.assertIn('Body', str(ctx.exception))

    def test_infinite_datum_plane_reports_no_fake_volume(self):
        state = observe.inspect_document(self.build_document())
        plane = next(item for item in state['objects'] if item['name'] == 'XY_Plane')
        geometry = plane['geometry']
        self.assertTrue(geometry['unbounded'])
        self.assertIsNone(geometry['volume_mm3'])
        self.assertIsNone(geometry['area_mm2'])
        self.assertIsNone(geometry['bbox_mm'])
        self.assertFalse(geometry['volume_applicable'])
        self.assertEqual(plane['role'], 'datum')
        self.assertNotIn('XY_Plane', state['outputs'])
        self.assertTrue(any('non-finite' in warning for warning in state['warnings']))

    def test_nan_and_infinity_are_never_reported_as_measurements(self):
        doc = FakeDocument('NaN')
        shape = FakeShape(solid_volumes=(float('nan'),), area=float('inf'))
        doc.addObject(FakeFeature('Weird', 'Part::Feature', shape=shape))
        state = observe.inspect_document(doc, detail=True)
        geometry = state['objects'][0]['geometry']
        self.assertIsNone(geometry['volume_mm3'])
        self.assertIsNone(geometry['area_mm2'])
        self.assertIn('note', geometry)
        json.dumps(state, allow_nan=False)

    def test_multi_solid_is_legal_and_volume_semantics_are_stated(self):
        doc = FakeDocument('Multi')
        shape = FakeShape(shape_type='Compound', solid_volumes=(1000.0, 1000.0),
                          surface_types=['Plane'] * 12)
        doc.addObject(FakeFeature('Two', 'Part::Compound', shape=shape))
        state = observe.inspect_document(doc, detail=True)
        record = state['objects'][0]
        geometry = record['geometry']
        self.assertEqual(geometry['solids'], 2)
        self.assertEqual(len(geometry['solid_volumes_mm3']), 2)
        self.assertEqual(geometry['volume_mm3'], 2000.0)
        self.assertIn('sum', geometry['note'])
        self.assertEqual(geometry['surface_types'], {'Plane': 12})
        self.assertIn('NOT a hole count', geometry['surface_types_note'])
        self.assertEqual(state['outputs'], ['Two'])

    def test_non_solid_shape_never_reports_a_volume(self):
        doc = FakeDocument('Shell')
        doc.addObject(FakeFeature('Shell', 'Part::Feature', shape=FakeShape(
            shape_type='Shell', solid_volumes=(), surface_types=['Plane'])))
        state = observe.inspect_document(doc, detail=True)
        geometry = state['objects'][0]['geometry']
        self.assertEqual(geometry['solids'], 0)
        self.assertIsNone(geometry['volume_mm3'])
        self.assertFalse(geometry['volume_applicable'])
        # a bounded shell/face is a legitimate (non-solid) output: it is rendered
        # but never reported with a volume
        self.assertEqual(state['outputs'], ['Shell'])

    def test_sketch_diagnostics_map_native_ids_to_zero_based_indices(self):
        doc = FakeDocument('SketchDiag')
        sketch = FakeSketch('Diag',
                            geometry=[FakeGeometry('LineSegment')] * 3,
                            constraints=[
                                FakeConstraint('Horizontal'),
                                FakeConstraint('Distance', value=10.0),
                                FakeConstraint('Distance', value=20.0),
                            ],
                            dof=-1, fully=False, conflicting=[2, 3])
        doc.addObject(sketch)
        state = observe.inspect_document(doc, object_name='Diag')
        record = state['objects'][0]
        sketch_record = record['sketch']
        diagnostics = sketch_record['diagnostics']
        self.assertEqual(diagnostics['conflicting_constraint_ids'], [2, 3])
        self.assertEqual(diagnostics['conflicting_indices_0based'], [1, 2])
        self.assertEqual(diagnostics['dof'], -1)
        self.assertFalse(diagnostics['solve_called'])
        self.assertIsNone(diagnostics['solve_code'])
        self.assertIn('1-based', diagnostics['id_basis']['native_ids'])
        constraints = sketch_record['constraints']
        self.assertEqual([item['index'] for item in constraints], [0, 1, 2])
        self.assertEqual([item['native_id'] for item in constraints], [1, 2, 3])
        self.assertEqual([item['is_conflicting'] for item in constraints],
                         [False, True, True])
        self.assertEqual(constraints[1]['value'], 10.0)
        self.assertTrue(constraints[1]['dimensional'])
        self.assertFalse(constraints[0]['dimensional'])

    def test_unverified_partially_redundant_basis_withholds_mapping(self):
        doc = FakeDocument('Partial')
        sketch = FakeSketch('Sk', constraints=[FakeConstraint('Coincident')],
                            partially=[1], redundant=[])
        doc.addObject(sketch)
        state = observe.inspect_document(doc, object_name='Sk')
        diagnostics = state['objects'][0]['sketch']['diagnostics']
        self.assertEqual(diagnostics['partially_redundant_constraint_ids'], [1])
        self.assertIsNone(diagnostics['partially_redundant_indices_0based'])
        self.assertIn('unverified', diagnostics['partially_redundant_basis'])
        json.dumps(state, allow_nan=False)

    def test_missing_diagnostics_are_reported_as_unavailable(self):
        doc = FakeDocument('NoDiag')
        sketch = FakeSketch('Sk', constraints=[], diagnostics=False)
        doc.addObject(sketch)
        state = observe.inspect_document(doc, object_name='Sk')
        diagnostics = state['objects'][0]['sketch']['diagnostics']
        self.assertIsNone(diagnostics['dof'])
        self.assertIsNone(diagnostics['conflicting_constraint_ids'])
        self.assertIn('DoF', diagnostics['unavailable'])

    def test_native_invalid_state_is_reported_prominently(self):
        state = observe.inspect_document(self.build_document())
        self.assertTrue(state['errors'])
        entry = next(item for item in state['errors'] if item['object'] == 'BadFillet')
        self.assertIn('Invalid', entry['message'])
        self.assertIn('BadFillet', state['document']['recompute']['invalid'])
        self.assertTrue(any('Invalid' in warning for warning in state['warnings']))

    def test_broken_body_still_reports_native_errors_and_no_output(self):
        doc = FakeDocument('BrokenBody')
        sketch = FakeSketch('Sketch')
        plate = FakeFeature('Plate', 'PartDesign::Pad',
                            shape=FakeShape(solid_volumes=(100.0,)))
        plate._props['Profile'] = ('App::PropertyLinkSub', (sketch, []))
        bad = FakeFeature('BadFillet', 'PartDesign::Fillet', shape=None,
                          state=('Touched', 'Invalid'))
        bad._props['Base'] = ('App::PropertyLinkSub', (plate, ['Edge99']))
        body = FakeBody('Body', group=[sketch, plate, bad], tip=bad, shape=None,
                        state=('Touched', 'Invalid'))
        for obj in (sketch, plate, bad, body):
            doc.addObject(obj)
        state = observe.inspect_document(doc)
        self.assertEqual(state['outputs'], [])
        self.assertFalse(any(record['is_output'] for record in state['objects']))
        reported = [item['object'] for item in state['errors']]
        self.assertIn('Body', reported)
        self.assertIn('BadFillet', reported)
        self.assertIn('Body', state['document']['recompute']['invalid'])

    def test_touched_document_reports_needs_recompute(self):
        doc = FakeDocument('Touched')
        doc.addObject(FakeFeature('A', 'Part::Box', state=('Touched',)))
        state = observe.inspect_document(doc)
        self.assertTrue(state['document']['recompute']['needs_recompute'])
        self.assertEqual(state['document']['recompute']['touched'], ['A'])
        self.assertTrue(any('Touched' in warning for warning in state['warnings']))

    def test_no_output_geometry_produces_a_warning(self):
        doc = FakeDocument('Empty')
        doc.addObject(FakeObject('Origin', 'App::Origin'))
        state = observe.inspect_document(doc)
        self.assertEqual(state['outputs'], [])
        self.assertTrue(any('no output geometry' in warning
                            for warning in state['warnings']))

    def test_dependencies_are_deduplicated_and_sorted(self):
        state = observe.inspect_document(self.build_document())
        body = next(item for item in state['objects'] if item['name'] == 'Body')
        self.assertEqual(body['dependencies']['out'],
                         ['BaseSketch', 'Hole', 'Plate'])

    def test_spreadsheet_cells_reported_with_alias_and_recompute_caveat(self):
        state = observe.inspect_document(self.build_document(), object_name='Params')
        sheet = state['objects'][0]['spreadsheet']
        self.assertEqual(sheet['cells']['B1']['raw'], '=40 mm')
        self.assertEqual(sheet['cells']['B1']['value'], '40.0 mm')
        self.assertEqual(sheet['aliases']['PlateLength']['cell'], 'B1')

        stale_doc = FakeDocument('Stale')
        stale_doc.addObject(FakeSpreadsheet('Params', cells={
            'B1': {'raw': '=40 mm', 'alias': 'PlateLength', 'value': '40.0 mm'},
        }, recomputed=False))
        stale = observe.inspect_document(stale_doc, object_name='Params')
        cell = stale['objects'][0]['spreadsheet']['cells']['B1']
        self.assertIsNone(cell['value'])
        self.assertIn('recompute', cell['value_unavailable'])

    def test_limits_and_errors_and_warnings_are_explicit(self):
        state = observe.inspect_document(self.build_document())
        self.assertIsInstance(state['errors'], list)
        self.assertIsInstance(state['warnings'], list)
        self.assertTrue(state['limits'])
        self.assertTrue(any('hole' in limit for limit in state['limits']))
        self.assertTrue(any('FeaturePython' in limit for limit in state['limits']))

    def test_geometry_and_dependencies_present_for_compact_output(self):
        state = observe.inspect_document(self.build_document())
        body = next(item for item in state['objects'] if item['name'] == 'Body')
        self.assertTrue(body['is_output'])
        self.assertEqual(body['geometry']['volume_mm3'], 1005.31)
        self.assertEqual(body['role'], 'container')
        self.assertEqual(body['geometry']['faces'], 5)


class FakeQuantity:
    """Native Quantity double (UserString/Value/Unit)."""

    def __init__(self, value, user_string, unit='mm'):
        self.Value = value
        self.UserString = user_string
        self.Unit = f'Unit: {unit}'


# ==========================================================================
# code_view tests
# ==========================================================================
class CodeViewTest(unittest.TestCase):
    def setUp(self):
        self.previous_freecad = install_fake_freecad()

    def tearDown(self):
        if self.previous_freecad is None:
            sys.modules.pop('FreeCAD', None)
        else:
            sys.modules['FreeCAD'] = self.previous_freecad

    def _state(self):
        doc = FakeDocument('ViewDoc')
        sketch = FakeSketch('BaseSketch',
                            geometry=[FakeGeometry('Circle', Radius=6.0)],
                            constraints=[FakeConstraint('Radius', name='R1', value=6.0)],
                            conflicting=[1])
        pad = FakeFeature('Plate', 'PartDesign::Pad',
                          shape=FakeShape(solid_volumes=(1005.31,)))
        pad._props['Profile'] = ('App::PropertyLinkSub', (sketch, []))
        body = FakeBody('Body', group=[sketch, pad], tip=pad,
                        shape=FakeShape(solid_volumes=(1005.31,)))
        doc.addObject(sketch)
        doc.addObject(pad)
        doc.addObject(body)
        return observe.inspect_document(doc, detail=True)

    def test_view_is_marked_read_only_and_not_executable(self):
        text = observe.code_view(self._state())
        self.assertIn('READ-ONLY', text)
        self.assertIn('NOT AN EXECUTABLE REBUILDER', text)
        self.assertIn('cad3.observation/v1', text)

    def test_view_contains_structure_geometry_and_diagnostics(self):
        text = observe.code_view(self._state())
        self.assertIn('Body', text)
        self.assertIn('BaseSketch', text)
        self.assertIn('PartDesign::Pad', text)
        self.assertIn('volume_mm3=1005.31', text)
        self.assertIn('outputs', text)
        self.assertIn('conflicting_ids(1-based)=[1]', text)
        self.assertIn('indices(0-based)=[0]', text)
        self.assertIn('solve_called=False', text)
        self.assertIn('R1', text)

    def test_view_reports_errors_warnings_and_limits(self):
        state = self._state()
        state['errors'] = [{'object': 'Bad', 'state': ['Invalid'],
                            'message': 'native object state reports Invalid'}]
        state['warnings'] = ['something is stale']
        state['limits'] = ['a documented limit']
        text = observe.code_view(state)
        self.assertIn('# --- native errors', text)
        self.assertIn('native object state reports Invalid', text)
        self.assertIn('something is stale', text)
        self.assertIn('a documented limit', text)

    def test_view_tolerates_minimal_and_partial_state(self):
        self.assertIn('READ-ONLY', observe.code_view({}))
        partial = {'schema': 'cad3.observation/v1', 'document': {'name': 'D'},
                   'objects': [{'name': 'A'}], 'outputs': ['A']}
        text = observe.code_view(partial)
        self.assertIn('A', text)
        self.assertIn('geometry=None', text)

    def test_view_rejects_non_dict(self):
        with self.assertRaises(observe.ObserveError):
            observe.code_view(['not', 'a', 'state'])

    def test_view_truncates_long_object_lists_explicitly(self):
        state = self._state()
        template = dict(state['objects'][0])
        state['objects'] = [dict(template, name=f'Obj{index}')
                            for index in range(observe.MAX_VIEW_OBJECTS + 5)]
        text = observe.code_view(state)
        self.assertIn('5 more object record(s) omitted', text)


# ==========================================================================
# rendering tests (host: no VTK; the full pipeline runs in the integration test)
# ==========================================================================
class RenderingTest(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix='cad3-render-test-'))

    def tearDown(self):
        shutil.rmtree(self.directory, ignore_errors=True)

    def test_module_import_does_not_require_freecad_or_vtk(self):
        code = (
            'import sys;'
            f'sys.path.insert(0, {str(SRC_DIR)!r});'
            'import cad3.rendering as r;'
            f'assert {str(REPO_ROOT / "src")!r} in sys.path;'
            "assert 'vtk' not in sys.modules, 'vtk imported at module import';"
            "assert 'FreeCAD' not in sys.modules, 'FreeCAD imported at module import';"
            "assert r.SCHEMA == 'cad3.rendering/v1';"
            "print('ok')"
        )
        result = subprocess.run([sys.executable, '-c', code], capture_output=True,
                                text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('ok', result.stdout)

    def test_view_names_are_a_stable_whitelist(self):
        self.assertEqual(rendering.DEFAULT_VIEWS, ('iso', 'top', 'front', 'right'))
        for name in rendering.DEFAULT_VIEWS:
            self.assertIn(name, rendering.VIEW_NAMES)

    def test_unknown_or_empty_views_raise_actionable_error(self):
        with self.assertRaises(rendering.RenderError) as ctx:
            rendering._normalize_views(['iso', 'sideways'])
        message = str(ctx.exception)
        self.assertIn('sideways', message)
        self.assertIn('iso', message)
        with self.assertRaises(rendering.RenderError):
            rendering._normalize_views([])
        self.assertEqual(rendering._normalize_views('iso,top'), ['iso', 'top'])
        self.assertEqual(rendering._normalize_views(['TOP', 'top']), ['top'])

    def test_size_bounds_are_enforced(self):
        with self.assertRaises(rendering.RenderError):
            rendering._normalize_size(10, 'width')
        with self.assertRaises(rendering.RenderError):
            rendering._normalize_size('wide', 'width')
        self.assertEqual(rendering._normalize_size('640', 'height'), 640)

    def test_target_paths_stay_inside_the_output_directory(self):
        directory = rendering._prepare_output_dir(self.directory)
        for view in rendering.VIEW_NAMES:
            path = rendering._target_path(directory, view)
            self.assertEqual(path.parent, directory)
            self.assertEqual(path.name, f'render_{view}.png')

    def test_output_dir_cannot_be_a_file(self):
        target = self.directory / 'afile'
        target.write_text('x', encoding='utf-8')
        with self.assertRaises(rendering.RenderError):
            rendering._prepare_output_dir(target)

    def test_no_objects_is_an_actionable_error(self):
        with self.assertRaises(rendering.RenderError):
            rendering.render_objects([], self.directory)
        with self.assertRaises(rendering.RenderError):
            rendering.render_objects(None, self.directory)

    def test_unresolved_string_names_raise(self):
        doc = FakeDocument('RenderDoc')
        doc.addObject(FakeFeature('A', 'Part::Box'))
        with self.assertRaises(rendering.RenderError) as ctx:
            rendering.render_objects(['A', 'Missing'], self.directory)
        self.assertIn('Missing', str(ctx.exception))

    def test_shape_collection_skips_unrenderable_objects_with_reasons(self):
        good = FakeFeature('Good', 'Part::Box')
        no_shape = FakeFeature('NoShape', 'Part::Feature', shape=None)
        null_shape = FakeFeature('Null', 'Part::Feature',
                                 shape=FakeShape(null=True))
        infinite = FakeFeature('Plane', 'Part::Plane', shape=FakeShape(
            shape_type='Face', solid_volumes=(),
            bbox=((0, 0, 0), (2e100, 2e100, 0))))
        wire = FakeFeature('Sketch', 'Sketcher::SketchObject',
                           shape=FakeShape(shape_type='Wire', solid_volumes=(),
                                           surface_types=[]))
        broken = FakeFeature('Broken', 'Part::Feature', shape=FakeShape(
            tessellation_error=RuntimeError('tessellation exploded')))
        meshes, skipped = rendering._collect_meshes(
            [good, no_shape, null_shape, infinite, wire, broken])
        self.assertEqual([mesh['name'] for mesh in meshes], ['Good'])
        reasons = {item['object']: item['reason'] for item in skipped}
        self.assertIn('no shape', reasons['NoShape'])
        self.assertIn('no shape', reasons['Null'])
        self.assertIn('non-finite', reasons['Plane'])
        self.assertIn('no faces', reasons['Sketch'])
        self.assertIn('tessellation failed', reasons['Broken'])
        self.assertEqual(meshes[0]['triangles'], 4)
        self.assertEqual(meshes[0]['vertices'], 4)
        self.assertGreater(meshes[0]['tolerance'], 0)

    def test_non_finite_vertices_are_rejected(self):
        shape = FakeShape(tessellation=([(0, 0, 0), (float('nan'), 0, 0), (0, 1, 0)],
                                        [(0, 1, 2)]))
        meshes, skipped = rendering._collect_meshes(
            [FakeFeature('Bad', 'Part::Feature', shape=shape)])
        self.assertEqual(meshes, [])
        self.assertIn('non-finite', skipped[0]['reason'])

    def test_multi_solid_compound_is_meshed_per_solid(self):
        shape = FakeShape(shape_type='Compound', solid_volumes=(1000.0, 1000.0))
        meshes, skipped = rendering._collect_meshes(
            [FakeFeature('Two', 'Part::Compound', shape=shape)])
        self.assertEqual(skipped, [])
        self.assertEqual(meshes[0]['solids'], 2)
        self.assertEqual(len(meshes[0]['parts']), 2)
        self.assertEqual(meshes[0]['triangles'], 8)
        self.assertEqual(meshes[0]['vertices'], 8)
        self.assertEqual(meshes[0]['failed_solids'], [])

    def test_one_bad_solid_does_not_drop_the_whole_object(self):
        shape = FakeShape(shape_type='Compound', solid_volumes=(1000.0, 1000.0),
                          solid_errors=[None, RuntimeError('bad solid')])
        meshes, skipped = rendering._collect_meshes(
            [FakeFeature('Mixed', 'Part::Compound', shape=shape)])
        self.assertEqual(skipped, [])
        self.assertEqual(len(meshes[0]['parts']), 1)
        self.assertEqual(len(meshes[0]['failed_solids']), 1)
        self.assertIn('bad solid', meshes[0]['failed_solids'][0]['reason'])
        self.assertEqual(meshes[0]['triangles'], 4)

    def test_oversized_mesh_is_skipped_with_an_actionable_reason(self):
        original = rendering.MAX_TRIANGLES_PER_OBJECT
        rendering.MAX_TRIANGLES_PER_OBJECT = 2      # the fake tetra has 4
        try:
            meshes, skipped = rendering._collect_meshes(
                [FakeFeature('Dense', 'Part::Box')])
        finally:
            rendering.MAX_TRIANGLES_PER_OBJECT = original
        self.assertEqual(meshes, [])
        self.assertIn('too dense', skipped[0]['reason'])
        self.assertIn('cad_outputs', skipped[0]['reason'])

    def test_no_renderable_geometry_raises_with_reasons(self):
        original = rendering._vtk
        rendering._vtk = lambda: object()          # bypass the VTK import only
        try:
            with self.assertRaises(rendering.RenderError) as ctx:
                rendering.render_objects(
                    [FakeFeature('NoShape', 'Part::Feature', shape=None)],
                    self.directory)
        finally:
            rendering._vtk = original
        message = str(ctx.exception)
        self.assertIn('no renderable geometry', message)
        self.assertIn('NoShape', message)

    def test_missing_vtk_is_reported_as_runtime_error(self):
        try:
            import vtk                            # noqa: F401
            has_vtk = True
        except ImportError:
            has_vtk = False
        if has_vtk:
            self.skipTest('vtk importable on the host; covered by the FreeCAD test')
        with self.assertRaises(rendering.RenderError) as ctx:
            rendering.render_objects([FakeFeature('A', 'Part::Box')], self.directory)
        self.assertIn('VTK', str(ctx.exception))


# ==========================================================================
# interpreter compatibility (FreeCAD 1.1.3 bundles CPython 3.11)
# ==========================================================================
class InterpreterCompatibilityTest(unittest.TestCase):
    def test_modules_compile_under_the_freecad_interpreter(self):
        interpreter = _find_freecad_python()
        if interpreter is None:
            self.skipTest('FreeCAD bundled python not found (set CAD3_FREECAD)')
        result = subprocess.run(
            [interpreter, '-m', 'py_compile', observe.__file__, rendering.__file__],
            capture_output=True, text=True, timeout=180)
        self.assertEqual(
            result.returncode, 0,
            'cad3.observe/cad3.rendering must stay valid CPython 3.11 source '
            '(FreeCAD bundles 3.11):\n%s\n%s' % (result.stdout, result.stderr))

    def test_modules_have_no_python_312_only_fstring_constructs(self):
        # PEP 701 (multi-line / same-quote f-string expressions) is 3.12+ only;
        # it compiles on a newer host but breaks inside FreeCAD.
        for module in (observe, rendering):
            source = Path(module.__file__).read_text(encoding='utf-8')
            for number, line in enumerate(source.splitlines(), start=1):
                stripped = line.strip()
                if not stripped.startswith(('f"', "f'", 'parts.append(f')):
                    continue
                if stripped.count(stripped[stripped.index('f') + 1]) == 1:
                    self.fail('%s:%d looks like an unterminated f-string: %s'
                              % (module.__file__, number, stripped))


# ==========================================================================
# real FreeCAD integration (env gated, isolated)
# ==========================================================================
FREE_CANDIDATES = (
    '/Applications/FreeCAD.app/Contents/Resources/bin/freecadcmd',
    '/usr/lib/freecad/bin/FreeCADCmd',
    '/usr/bin/freecadcmd',
    '/usr/local/bin/freecadcmd',
)

RUNNER_SOURCE = '''\
"""Isolated FreeCAD launcher for the cad3 observation integration test."""
import json
import sys
import traceback
from pathlib import Path

import FreeCAD as App

SCRIPT = {script!r}
STATUS = {status!r}
ROOT = Path({root!r}).resolve()
record = {{'script': SCRIPT, 'version': list(App.Version()[:3]), 'ok': False}}
try:
    keys = ('UserAppData', 'UserParameter', 'SystemParameter')
    config = {{key: App.ConfigGet(key) for key in keys}}
    record['config'] = config
    record['user_app_data'] = App.getUserAppDataDir()
    for key, value in {{**config, 'getUserAppDataDir': record['user_app_data']}}.items():
        if value and not Path(value).resolve().is_relative_to(ROOT):
            raise RuntimeError('refuse non-isolated config path %s=%s' % (key, value))
    sys.path.insert(0, str(Path(SCRIPT).parent))
    namespace = {{'__file__': SCRIPT, '__name__': '__main__'}}
    exec(compile(Path(SCRIPT).read_text(encoding='utf-8'), SCRIPT, 'exec'), namespace)
    record['ok'] = True
except BaseException as exc:                     # noqa: BLE001
    record['exception'] = type(exc).__name__
    record['message'] = str(exc)
    record['traceback'] = traceback.format_exc()
    print(record['traceback'], file=sys.stderr, flush=True)
finally:
    Path(STATUS).write_text(json.dumps(record, ensure_ascii=False, indent=2),
                            encoding='utf-8')
'''

PROBE_SOURCE = '''\
"""Real FreeCAD observation/render probe; writes results.json for the host test."""
import json
import math
import sys
import traceback
from pathlib import Path

SRC = Path({src!r})
ROOT = Path({root!r}).resolve()
sys.path.insert(0, str(SRC))

import FreeCAD as App
import Part
import Sketcher
from cad3 import observe, rendering

results = {{}}
doc = App.newDocument('Integration')

# --- PartDesign Body: circle pad with a through pocket --------------------
body = doc.addObject('PartDesign::Body', 'Body')
base = body.newObject('Sketcher::SketchObject', 'BaseSketch')
base.addGeometry(Part.Circle(App.Vector(0, 0, 0), App.Vector(0, 0, 1), 6.0), False)
base.addConstraint(Sketcher.Constraint('Radius', 0, 6.0))
pad = body.newObject('PartDesign::Pad', 'Plate')
pad.Profile = base
pad.Length = 10.0
hole = body.newObject('Sketcher::SketchObject', 'HoleSketch')
hole.addGeometry(Part.Circle(App.Vector(0, 0, 0), App.Vector(0, 0, 1), 2.0), False)
hole.addConstraint(Sketcher.Constraint('Radius', 0, 2.0))
pocket = body.newObject('PartDesign::Pocket', 'Hole')
pocket.Profile = hole
pocket.Type = 'ThroughAll'
pocket.Midplane = True   # measured: without this the pocket extrudes away from
                         # the pad and removes nothing (test-model fix, not a
                         # cad3.observe issue)

# --- conflicting sketch for solver diagnostics ----------------------------
diag = doc.addObject('Sketcher::SketchObject', 'Diag')
diag.addGeometry(Part.LineSegment(App.Vector(0, 0, 0), App.Vector(10, 0, 0)), False)
diag.addConstraint(Sketcher.Constraint('Horizontal', 0))
diag.addConstraint(Sketcher.Constraint('Distance', 0, 10.0))
diag.addConstraint(Sketcher.Constraint('Distance', 0, 20.0))

# --- parameter sheet driving the pad --------------------------------------
sheet = doc.addObject('Spreadsheet::Sheet', 'Params')
sheet.set('B1', '10 mm')
sheet.setAlias('B1', 'PlateLength')
pad.setExpression('Length', 'Params.PlateLength')

# --- boolean cut ----------------------------------------------------------
box = doc.addObject('Part::Box', 'Box')
box.Length = box.Width = box.Height = 20.0
cyl = doc.addObject('Part::Cylinder', 'Cyl')
cyl.Radius = 3.0
cyl.Height = 20.0
cut = doc.addObject('Part::Cut', 'Cut')
cut.Base = box
cut.Tool = cyl

# --- App::Part containing a second body ----------------------------------
part = doc.addObject('App::Part', 'APart')
body2 = doc.addObject('PartDesign::Body', 'Body2')
part.addObject(body2)
s2 = body2.newObject('Sketcher::SketchObject', 'S2')
s2.addGeometry(Part.Circle(App.Vector(0, 0, 0), App.Vector(0, 0, 1), 4.0), False)
s2.addConstraint(Sketcher.Constraint('Radius', 0, 4.0))
p2 = body2.newObject('PartDesign::Pad', 'P2')
p2.Profile = s2
p2.Length = 2.0

# --- compound of two independent solids ----------------------------------
f1 = doc.addObject('Part::Feature', 'F1')
f1.Shape = Part.makeBox(5, 5, 5)
f2 = doc.addObject('Part::Feature', 'F2')
f2.Shape = Part.makeBox(5, 5, 5, App.Vector(10, 0, 0))
compound = doc.addObject('Part::Compound', 'Comp')
compound.Links = [f1, f2]

# --- a broken Body: failed Tip, earlier feature keeps a cached valid shape ---
broken = doc.addObject('PartDesign::Body', 'BrokenBody')
b_sketch = broken.newObject('Sketcher::SketchObject', 'BrokenSketch')
b_sketch.addGeometry(Part.Circle(App.Vector(0, 0, 0), App.Vector(0, 0, 1), 6.0), False)
b_sketch.addConstraint(Sketcher.Constraint('Radius', 0, 6.0))
b_pad = broken.newObject('PartDesign::Pad', 'BrokenPlate')
b_pad.Profile = b_sketch
b_pad.Length = 5.0
b_fillet = broken.newObject('PartDesign::Fillet', 'BrokenFillet')
b_fillet.Base = (b_pad, ['Edge99'])
b_fillet.Radius = 1.0

doc.recompute()

# --- selection ------------------------------------------------------------
selected = observe.select_output_objects(doc)
results['outputs'] = [obj.Name for obj in selected]
results['output_types'] = [obj.TypeId for obj in selected]
results['all_objects'] = [obj.Name for obj in doc.Objects]
results['body_volume'] = body.Shape.Volume
results['compound_solids'] = len(compound.Shape.Solids)
results['explicit'] = [obj.Name for obj in
                       observe.select_output_objects(doc, ['Box', 'Cut', 'Box'])]
results['broken_body'] = {{
    'state': [str(item) for item in broken.State],
    'shape_null': broken.Shape.isNull(),
    'pad_shape_null': b_pad.Shape.isNull(),
    'pad_volume': None if b_pad.Shape.isNull() else b_pad.Shape.Volume,
    'fillet_state': [str(item) for item in b_fillet.State],
    'fillet_shape_null': b_fillet.Shape.isNull(),
    'selected': [obj.Name for obj in observe.select_output_objects(doc)
                 if obj.Name in ('BrokenBody', 'BrokenPlate', 'BrokenFillet')],
}}

# --- inspection -----------------------------------------------------------
state = observe.inspect_document(doc)
results['schema'] = state['schema']
results['state_keys'] = sorted(state)
results['json_roundtrip'] = json.loads(json.dumps(state, allow_nan=False))['schema']
results['errors'] = state['errors']
results['warnings'] = state['warnings']
results['native_root_objects'] = state['document']['native_root_objects']
body_record = next(item for item in state['objects'] if item['name'] == 'Body')
results['body_record'] = {{
    'is_output': body_record['is_output'],
    'container': body_record['container'],
    'role': body_record['role'],
    'geometry': body_record['geometry'],
    'dependencies_out': body_record['dependencies']['out'],
    'links': body_record.get('links'),
}}
plane_record = next(item for item in state['objects'] if item['name'] == 'XY_Plane')
results['plane_geometry'] = {{
    'unbounded': plane_record['geometry']['unbounded'],
    'volume_mm3': plane_record['geometry']['volume_mm3'],
    'area_mm2': plane_record['geometry']['area_mm2'],
    'bbox_mm': plane_record['geometry']['bbox_mm'],
    'solids': plane_record['geometry']['solids'],
}}
sketch_record = next(item for item in state['objects'] if item['name'] == 'Diag')
diagnostics = sketch_record['sketch']['diagnostics']
results['sketch_diagnostics'] = {{
    'dof': diagnostics['dof'],
    'conflicting_ids': diagnostics['conflicting_constraint_ids'],
    'conflicting_indices': diagnostics['conflicting_indices_0based'],
    'redundant_ids': diagnostics['redundant_constraint_ids'],
    'partially_redundant_ids': diagnostics['partially_redundant_constraint_ids'],
    'solve_called': diagnostics['solve_called'],
    'unavailable': diagnostics['unavailable'],
    'constraint_count': sketch_record['sketch']['constraint_count'],
}}
body_detail = observe.inspect_document(doc, object_name='Body')
body_detail_record = body_detail['objects'][0]
geometry = body_detail_record['geometry']
results['body_surface_types'] = geometry.get('surface_types')
results['body_surface_note'] = geometry.get('surface_types_note')
results['body_placement'] = body_detail_record.get('placement') is not None
plate_detail = observe.inspect_document(doc, object_name='Plate')
plate_record = plate_detail['objects'][0]
results['plate_parameters_length'] = plate_record['parameters']['Length'].get(
    'user_string')
results['plate_expressions'] = plate_record['expressions']
results['plate_links'] = plate_record.get('links')
sheet_detail = observe.inspect_document(doc, object_name='Params')
results['sheet_cells'] = sheet_detail['objects'][0]['spreadsheet']['cells']
results['sheet_aliases'] = sorted(
    sheet_detail['objects'][0]['spreadsheet']['aliases'])

view = observe.code_view(state)
results['code_view_has_readonly'] = 'READ-ONLY' in view
results['code_view_has_executable_warning'] = 'NOT AN EXECUTABLE REBUILDER' in view
results['code_view_mentions_body'] = 'Body' in view
results['code_view_mentions_dof'] = 'solve_called=False' in view
results['code_view_lines'] = len(view.splitlines())

# --- rendering ------------------------------------------------------------
render_dir = ROOT / 'render'
rendered = rendering.render_objects(selected, render_dir, views=('iso', 'top', 'front', 'right'),
                                    width=640, height=480)
results['render'] = [{{
    'view': item['view'],
    'path': item['path'],
    'bytes': item['size_bytes'],
    'width': item['width'],
    'height': item['height'],
    'gui_screenshot': item['source']['gui_screenshot'],
    'statement': item['source']['statement'],
    'camera': item['camera'],
    'triangles': item['dimensions']['triangles'],
    'objects_rendered': item['dimensions']['objects_rendered'],
    'skipped': item['skipped'],
}} for item in rendered]
results['render_dir'] = str(render_dir)

(ROOT / 'results.json').write_text(json.dumps(results, indent=1, default=str),
                                   encoding='utf-8')
print('PROBE_OK')
'''


def _find_freecadcmd():
    explicit = os.environ.get('CAD3_FREECAD')
    if explicit and Path(explicit).exists():
        return explicit
    for candidate in FREE_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    found = shutil.which('freecadcmd') or shutil.which('FreeCADCmd')
    return found


def _find_freecad_python():
    explicit = os.environ.get('CAD3_FREECAD_PYTHON')
    if explicit and Path(explicit).exists():
        return explicit
    freecad = _find_freecadcmd()
    if freecad:
        candidate = Path(freecad).with_name('python')
        if candidate.exists():
            return str(candidate)
    return None


def _png_ihdr(path: Path):
    data = path.read_bytes()
    if data[:8] != b'\x89PNG\r\n\x1a\n':
        raise AssertionError(f'{path} is not a PNG file')
    width = int.from_bytes(data[16:20], 'big')
    height = int.from_bytes(data[20:24], 'big')
    return width, height


@unittest.skipUnless(os.environ.get('CAD3_REAL_FREECAD_TEST') == '1',
                     'set CAD3_REAL_FREECAD_TEST=1 to run the real FreeCAD test')
class RealFreeCADIntegrationTest(unittest.TestCase):
    """Runs a real isolated freecadcmd against real native geometry."""

    def test_observation_and_rendering_on_real_freecad(self):
        freecad = _find_freecadcmd()
        if freecad is None:
            self.skipTest('freecadcmd not found (set CAD3_FREECAD)')
        root = Path(tempfile.mkdtemp(prefix='cad3-observe-integration-'))
        try:
            profile = root / 'isolated-profile'
            home = root / 'isolated-home'
            profile.mkdir()
            home.mkdir()
            runner = root / 'runner.py'
            status = root / 'completion.json'
            probe = root / 'probe.py'
            runner.write_text(RUNNER_SOURCE.format(script=str(probe),
                                                  status=str(status), root=str(root)),
                              encoding='utf-8')
            probe.write_text(PROBE_SOURCE.format(src=str(SRC_DIR), root=str(root)),
                             encoding='utf-8')
            env = os.environ.copy()
            env.update(FREECAD_USER_HOME=str(profile), HOME=str(home),
                       PYTHONIOENCODING='utf-8', MPLBACKEND='Agg')
            for key in ('PYTHONHOME', 'PYTHONPATH', 'VIRTUAL_ENV'):
                env.pop(key, None)
            command = [freecad,
                       '--user-cfg', str(profile / 'user.cfg'),
                       '--system-cfg', str(profile / 'system.cfg'),
                       str(runner)]
            completed = subprocess.run(command, cwd=str(root), env=env,
                                       capture_output=True, text=True, timeout=300)
            record = json.loads(status.read_text(encoding='utf-8')) if status.exists() \
                else {'ok': False, 'error': 'missing completion record'}
            self.assertTrue(record.get('ok'),
                            f'freecad run failed: {record}\n'
                            f'stdout={completed.stdout[-2000:]}\n'
                            f'stderr={completed.stderr[-2000:]}')
            for key, value in record.get('config', {}).items():
                self.assertTrue(Path(value).resolve().is_relative_to(root.resolve()),
                                f'{key} escaped the isolated root: {value}')
            self.assertEqual(completed.returncode, 0)
            results = json.loads((root / 'results.json').read_text(encoding='utf-8'))

            # -- output selection ------------------------------------------
            self.assertEqual(results['schema'], 'cad3.observation/v1')
            outputs = results['outputs']
            self.assertIn('Body', outputs)
            self.assertIn('Cut', outputs)
            self.assertIn('APart', outputs)
            self.assertIn('Comp', outputs)
            self.assertIn('APart', outputs)
            for excluded in ('Plate', 'Hole', 'BaseSketch', 'HoleSketch', 'Origin',
                             'XY_Plane', 'Box', 'Cyl', 'Body2', 'Params', 'F1', 'F2'):
                self.assertNotIn(excluded, outputs,
                                 f'{excluded} must not be a duplicated output')
            self.assertEqual(results['explicit'], ['Box', 'Cut'])
            self.assertEqual(results['compound_solids'], 2)
            # a broken Body must not leak its cached intermediate feature
            broken = results['broken_body']
            self.assertTrue(broken['shape_null'])
            self.assertFalse(broken['pad_shape_null'],
                             'the cached Pad shape is exactly the trap this guards')
            self.assertEqual(broken['selected'], [])
            self.assertIn('Invalid', broken['fillet_state'])
            self.assertNotIn('BrokenBody', outputs)
            self.assertNotIn('BrokenPlate', outputs)
            self.assertTrue(any(item['object'] == 'BrokenFillet'
                                for item in results['errors']),
                            'the failed Tip must be reported as a native error')
            self.assertNotIn('Diag', outputs)
            self.assertTrue(any(item['object'] == 'Diag'
                                for item in results['errors']),
                            'the conflicting sketch must be reported as a native '
                            'error, not silently ignored')
            self.assertIsInstance(results['warnings'], list)

            # -- geometry --------------------------------------------------
            self.assertAlmostEqual(results['body_volume'], 1005.3096, places=3)
            body_geometry = results['body_record']['geometry']
            self.assertEqual(body_geometry['solids'], 1)
            self.assertTrue(body_geometry['volume_applicable'])
            self.assertIsNotNone(body_geometry['volume_mm3'])
            self.assertTrue(body_geometry['valid'])
            self.assertTrue(results['body_record']['is_output'])
            self.assertIsNone(results['body_record']['container'])
            self.assertEqual(results['body_record']['role'], 'container')
            self.assertIn('Cylinder', results['body_surface_types'])
            self.assertIn('NOT a hole count', results['body_surface_note'])

            # -- datum plane must not report garbage -----------------------
            plane = results['plane_geometry']
            self.assertTrue(plane['unbounded'])
            self.assertIsNone(plane['volume_mm3'])
            self.assertIsNone(plane['area_mm2'])
            self.assertIsNone(plane['bbox_mm'])
            self.assertEqual(plane['solids'], 0)

            # -- sketch diagnostics, native ids kept -----------------------
            diagnostics = results['sketch_diagnostics']
            self.assertEqual(diagnostics['unavailable'], [],
                             'all measured sketch diagnostics must be readable')
            self.assertEqual(diagnostics['solve_called'], False)
            self.assertEqual(diagnostics['constraint_count'], 3)
            self.assertEqual(diagnostics['conflicting_ids'], [2, 3])
            self.assertEqual(diagnostics['conflicting_indices'], [1, 2])
            self.assertEqual(diagnostics['redundant_ids'], [])
            self.assertEqual(diagnostics['partially_redundant_ids'], [])
            self.assertIsNotNone(diagnostics['dof'])

            # -- parameters / expressions / sheets -------------------------
            self.assertEqual(results['plate_parameters_length'], '10.00 mm')
            self.assertEqual(results['plate_expressions'],
                             {'Length': 'Params.PlateLength'})
            self.assertEqual(results['plate_links']['Profile'], ['BaseSketch'])
            self.assertTrue(results['body_placement'])
            self.assertEqual(results['sheet_cells']['B1']['value'], '10.0 mm')
            self.assertEqual(results['sheet_aliases'], ['PlateLength'])

            # -- code view -------------------------------------------------
            self.assertTrue(results['code_view_has_readonly'])
            self.assertTrue(results['code_view_has_executable_warning'])
            self.assertTrue(results['code_view_mentions_body'])
            self.assertTrue(results['code_view_mentions_dof'])

            # -- rendering -------------------------------------------------
            self.assertEqual([item['view'] for item in results['render']],
                             ['iso', 'top', 'front', 'right'])
            for item in results['render']:
                self.assertFalse(item['gui_screenshot'])
                self.assertIn('NOT a native FreeCAD GUI viewport screenshot',
                              item['statement'])
                self.assertEqual((item['width'], item['height']), (640, 480))
                self.assertGreater(item['triangles'], 0)
                self.assertGreater(item['bytes'], 1000)
                self.assertEqual(item['skipped'], [])
                self.assertIn('position', item['camera'])
                self.assertTrue(item['camera']['parallel_projection'])
                width, height = _png_ihdr(Path(item['path']))
                self.assertEqual((width, height), (640, 480))
            self.assertGreaterEqual(results['render'][0]['objects_rendered'],
                                    len(outputs))
        finally:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == '__main__':
    unittest.main(verbosity=2)
