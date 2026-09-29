"""Native FreeCAD document observation for CAD3.

This module is imported INSIDE the FreeCAD worker process.  Importing it on the
host must not require FreeCAD: ``FreeCAD``/``Part``/``vtk`` are only touched
lazily, inside the public functions.

Everything here is read-only.  ``inspect_document`` never recomputes the
document and never calls ``sketch.solve()`` or ``sketch.autoRemoveRedundants()``:
the worker recomputes first and this module reports whatever the last recompute
left behind, including the availability of each diagnostic.

Native semantics encoded here were measured on FreeCAD 1.1.3 / macOS (isolated
``freecadcmd`` runs); where a behaviour was NOT verified it is reported as
unavailable instead of guessed.
"""

from __future__ import annotations

import math
import re
from typing import Any

__all__ = ['SCHEMA', 'ObserveError', 'select_output_objects', 'inspect_document', 'code_view']

SCHEMA = 'cad3.observation/v1'
GENERATOR = 'cad3.observe'

# --------------------------------------------------------------------------
# policy limits (explicit; every truncation is reported in the output)
# --------------------------------------------------------------------------
MAX_OBJECTS = 5000
MAX_PARAMETERS_PER_OBJECT = 200
MAX_CONSTRAINTS_PER_SKETCH = 500
MAX_SKETCH_GEOMETRY = 500
MAX_SHEET_CELLS = 400
MAX_RECURSIVE_NAMES = 300
MAX_NAMED_NAMES = 60
MAX_VIEW_OBJECTS = 400
MAX_VIEW_PARAMS = 40
MAX_VIEW_CONSTRAINTS = 40

#: Geometry magnitudes above this are treated as non-finite garbage.  Measured:
#: ``App::Plane.Shape.Volume`` on FreeCAD 1.1.3 returns e.g. -1.19e191 / 4.8e152.
GEOM_MAGNITUDE_LIMIT = 1e12
#: Bounds above this (mm) mean the shape is not a real bounded solid but an
#: infinite helper (measured: origin datum planes report 2e100).
BBOX_MAGNITUDE_LIMIT = 1e9

# --------------------------------------------------------------------------
# native type tables (measured, keep to what was verified)
# --------------------------------------------------------------------------
DATUM_TYPE_IDS = frozenset({
    'App::Plane', 'App::Line', 'App::Point', 'App::Origin',
    'App::CoordinateSystem', 'PartDesign::Plane', 'PartDesign::Line',
    'PartDesign::Point', 'PartDesign::CoordinateSystem',
})
CONTAINER_TYPE_IDS = frozenset({
    'App::Part', 'App::DocumentObjectGroup', 'PartDesign::Body',
    'App::GeoFeatureGroup', 'App::GeoFeatureGroupExtension',
    'Assembly::AssemblyObject', 'Assembly::JointGroup',
})
SKETCH_TYPE_PREFIX = 'Sketcher::'

LINK_TYPE_IDS = frozenset({
    'App::PropertyLink', 'App::PropertyLinkList', 'App::PropertyLinkSub',
    'App::PropertyLinkSubList', 'App::PropertyLinkChild',
    'App::PropertyLinkGlobal', 'App::PropertyLinkHidden',
    'App::PropertyLinkSubHidden',
})
#: Link properties that are not listed in ``PropertiesList`` (measured: the
#: ``Shape``/``LinkedObject`` of an ``App::Link`` and the aggregate ``Shape`` of
#: ``App::Part``/``App::DocumentObjectGroup`` are dynamic properties).
HIDDEN_LINK_PROPS = (
    'LinkedObject', 'Links', 'ElementList', 'Base', 'Tool', 'Profile',
    'BaseFeature', 'Support', 'AttachmentSupport', 'LinkCopyOnChangeSource',
    'LinkCopyOnChangeGroup',
)

QUANTITY_TYPES = frozenset({
    'App::PropertyLength', 'App::PropertyDistance', 'App::PropertyAngle',
    'App::PropertyArea', 'App::PropertyVolume', 'App::PropertyQuantity',
    'App::PropertyPercent', 'App::PropertySpeed', 'App::PropertyAcceleration',
    'App::PropertyForce', 'App::PropertyPressure', 'App::PropertyStiffness',
    'App::PropertyVacuumPermittivity', 'App::PropertyAmountOfSubstance',
    'App::PropertyElectricCurrent', 'App::PropertyElectricPotential',
    'App::PropertyCapacitance', 'App::PropertyCharge',
    'App::PropertyMagneticFlux', 'App::PropertyMagneticFieldStrength',
    'App::PropertyMagneticFluxDensity', 'App::PropertyInductance',
    'App::PropertyResistance', 'App::PropertyConductivity',
    'App::PropertyThermalConductivity', 'App::PropertySpecificEnergy',
    'App::PropertyThermalExpansionCoefficient', 'App::PropertyFrequency',
    'App::PropertyVelocity', 'App::PropertyWork', 'App::PropertyPower',
    'App::PropertyHeatFlux', 'App::PropertyEntropy',
    'App::PropertySpecificHeat', 'App::PropertyThermalResistance',
    'App::PropertyElectricalCapacitance',
})
SCALAR_TYPE_IDS = frozenset({
    'App::PropertyString', 'App::PropertyBool', 'App::PropertyBoolList',
    'App::PropertyInteger', 'App::PropertyIntegerConstraint',
    'App::PropertyFloat', 'App::PropertyFloatConstraint',
    'App::PropertyEnumeration', 'App::PropertyStringList',
})
VECTOR_TYPE_IDS = frozenset({
    'App::PropertyVector', 'App::PropertyVectorDistance',
    'App::PropertyPosition', 'App::PropertyDirection',
})
PLACEMENT_TYPE_IDS = frozenset({
    'App::PropertyPlacement', 'App::PropertyRotation',
})

#: Properties that carry no observation value (handled separately or opaque).
SKIP_PARAMS = frozenset({
    'ExpressionEngine', 'Label', 'Shape', 'Group', 'Profile', 'Base', 'Tool',
    'AttachmentSupport', 'Support', 'AttacherType', 'AttacherEngine',
    'MapPathParameter', 'Geometry', 'Constraints', 'ExternalGeometry',
    'Construction', 'Proxy', 'PreviewShape', 'AddSubShape', 'SuppressedShape',
    'BaseFeature', 'ReferenceAxis', 'UpToFace', 'UpToFace2', 'UpToShape',
    'UpToShape2', 'ShapeMaterial', 'Links', 'ElementList', 'LinkedObject',
    'LinkCopyOnChangeSource', 'LinkCopyOnChangeGroup', 'Origin',
})
#: Constraint types whose ``Value`` is a driving dimension.
DIMENSIONAL_CONSTRAINTS = frozenset({
    'Distance', 'DistanceX', 'DistanceY', 'Radius', 'Diameter', 'Angle',
    'Weight', 'SnellsLaw',
})
#: ``Sketcher`` constraint reference that means "not set".
GEO_UNDEF = -2000

_LIMITS = [
    'Read from the live in-process document; no recompute, no sketch solve and '
    'no autoRemoveRedundants call happens during inspection.',
    'Objects whose native State is Touched/Invalid may carry missing or stale '
    'shapes; geometry is reported only when the shape is present and non-null.',
    'Volume is reported only when the shape actually contains solids; infinite '
    'helper planes report garbage native volumes and are reported as unbounded.',
    'A Compound/Part/Group aggregate volume is the SUM of its solid volumes, '
    'not a boolean union volume.',
    'Face surface type counts (e.g. Cylinder) are surface counts, not hole, '
    'boss or fillet counts.',
    'Native Face/Edge element names are not guaranteed stable across geometry '
    'changes (measured on FreeCAD 1.1.3).',
    'FeaturePython implementations are not contained in this snapshot; only the '
    'proxy class/module reference is recorded.',
    'Sketcher conflict/redundant ids are reported as the solver numbers them '
    '(1-based); the 0-based Constraints[i] mapping is given next to them. The '
    '1-based basis of PartiallyRedundantConstraints was not verified, so its '
    'mapping is withheld when non-empty.',
    'ViewObject/GUI display state does not exist with GuiUp == 0 and is not '
    'part of this snapshot.',
]


class ObserveError(RuntimeError):
    """Raised when an observation request cannot be satisfied as asked."""


# --------------------------------------------------------------------------
# lazy native access
# --------------------------------------------------------------------------
def _app():
    try:                                    # pragma: no cover - host side
        import FreeCAD as App           # type: ignore
        return App
    except ImportError as exc:              # pragma: no cover - host side
        raise ObserveError(
            'cad3.observe must run inside the FreeCAD worker process; '
            'FreeCAD is not importable here'
        ) from exc


# --------------------------------------------------------------------------
# small helpers (pure python, testable on the host)
# --------------------------------------------------------------------------
def _finite(value: Any, *, limit: float | None = None) -> float | None:
    """Return a finite float or None (rejects NaN/Inf and absurd magnitudes)."""
    if isinstance(value, bool):
        return float(value)
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    if limit is not None and abs(number) > limit:
        return None
    return number


def _num(value: Any, *, limit: float | None = None, digits: int = 6) -> float | None:
    number = _finite(value, limit=limit)
    return None if number is None else round(number, digits)


def _names_of(value: Any, *, top: bool = True) -> list[str]:
    """Collect DocumentObject names out of any native link value shape.

    Measured native shapes: ``PropertyLink`` -> obj|None, ``PropertyLinkSub`` ->
    flat ``(obj, [subs])``, ``PropertyLinkSubList`` -> ``[(obj, [subs]), ...]``.
    Sub-element name strings are NOT object names and are dropped.
    """
    out: list[str] = []
    if value is None:
        return out
    if hasattr(value, 'Name'):
        return [str(value.Name)]
    if isinstance(value, str):
        return [value] if top else []
    if isinstance(value, (list, tuple)):
        for item in value:
            out.extend(_names_of(item, top=False))
    return out


def _dedup_sorted(names: Any) -> list[str]:
    seen = set()
    out = []
    for name in names:
        text = str(name)
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return sorted(out)


def _capped(names: list[str], limit: int) -> tuple[list[str], bool]:
    if len(names) <= limit:
        return names, False
    return names[:limit], True


def _unit_of(quantity: Any) -> str | None:
    """Unit string of a native Quantity (regex on str(); measured stable)."""
    try:
        match = re.search(r'Unit:\s*(\S+)', str(quantity.Unit))
    except Exception:
        return None
    if match:
        return match.group(1)
    try:
        return str(quantity.Unit)
    except Exception:
        return None


def _expression_map(obj: Any) -> dict[str, str]:
    """Object-level expressions: {property: expression source}."""
    out: dict[str, str] = {}
    try:
        engine = list(obj.ExpressionEngine)
    except Exception:
        return out
    for entry in engine:
        try:
            prop, expression = entry[0], entry[1]
        except Exception:
            continue
        out[str(prop)] = str(expression)
    return out


def _expression_object_refs(expressions: dict[str, str]) -> set[str]:
    """Object names referenced by expression sources (measured form ``Obj.Prop``)."""
    refs: set[str] = set()
    for source in expressions.values():
        refs.update(re.findall(r'([A-Za-z_][A-Za-z0-9_]*)\.', source))
    return refs


def _group_names(obj: Any) -> list[str]:
    if not hasattr(obj, 'Group'):
        return []
    try:
        return [str(child.Name) for child in obj.Group]
    except Exception:
        return []


def _link_names(obj: Any) -> list[str]:
    """Direct link/containment targets of an object (geometry inputs)."""
    names: list[str] = []
    for targets in _link_property_map(obj).values():
        names.extend(targets)
    return _dedup_sorted(names)


def _link_property_map(obj: Any) -> dict[str, list[str]]:
    """Named link properties -> referenced object names.

    Includes link properties that FreeCAD keeps out of ``PropertiesList``
    (measured: ``App::Link.LinkedObject``).
    """
    out: dict[str, list[str]] = {}
    try:
        props = list(obj.PropertiesList)
    except Exception:
        props = []
    for prop in props:
        if prop.startswith('_'):
            # measured: PartDesign features expose `_Body` as a hidden BACK
            # reference to their Body.  Treating it as a geometry input would
            # make every Body look "consumed" by its own feature.
            continue
        try:
            type_id = obj.getTypeIdOfProperty(prop)
        except Exception:
            continue
        if type_id not in LINK_TYPE_IDS:
            continue
        try:
            names = _dedup_sorted(_names_of(obj.getPropertyByName(prop)))
        except Exception:
            continue
        if names:
            out[prop] = _capped(names, MAX_NAMED_NAMES)[0]
    for prop in HIDDEN_LINK_PROPS:
        if prop in props or not hasattr(obj, prop):
            continue
        try:
            names = _dedup_sorted(_names_of(getattr(obj, prop)))
        except Exception:
            continue
        if names:
            out[prop] = _capped(names, MAX_NAMED_NAMES)[0]
    return out


def _out_list(obj: Any) -> list[str]:
    try:
        return _dedup_sorted([child.Name for child in obj.OutList])
    except Exception:
        return []


def _in_list(obj: Any) -> list[str]:
    try:
        return _dedup_sorted([parent.Name for parent in obj.InList])
    except Exception:
        return []


def _recursive_names(obj: Any, attribute: str, *, limit: int = MAX_RECURSIVE_NAMES):
    if not hasattr(obj, attribute):
        return [], False
    try:
        raw = getattr(obj, attribute)
        names = _dedup_sorted([item.Name for item in raw])
    except Exception:
        return [], False
    return _capped(names, limit)


def _state_of(obj: Any) -> list[str]:
    try:
        return [str(item) for item in obj.State]
    except Exception:
        return []


def _needs_recompute(state: list[str]) -> bool:
    return any(flag in state for flag in ('Touched', 'Recompute'))


def _is_invalid(state: list[str]) -> bool:
    return any(flag in state for flag in ('Invalid', 'Error'))


def _visibility_of(obj: Any):
    try:
        return bool(obj.Visibility)
    except Exception:
        return None


def _object_name(obj: Any) -> str:
    try:
        return str(obj.Name)
    except Exception:
        return repr(obj)


def _object_type(obj: Any) -> str:
    try:
        return str(obj.TypeId)
    except Exception:
        return 'Unknown'


def _object_label(obj: Any) -> str:
    try:
        return str(obj.Label)
    except Exception:
        return ''


def _doc_objects(doc: Any) -> list[Any]:
    try:
        return list(doc.Objects)
    except Exception as exc:
        raise ObserveError(f'cannot list document objects: {exc}') from exc


# --------------------------------------------------------------------------
# shape inspection
# --------------------------------------------------------------------------
def _shape_of(obj: Any):
    """The native Shape or None.  ``ShapeType`` must never be read on a null
    shape (measured: ``FreeCADError: cannot determine type of null shape``)."""
    if not hasattr(obj, 'Shape'):
        return None
    try:
        shape = obj.Shape
    except Exception:
        return None
    if shape is None:
        return None
    try:
        if shape.isNull():
            return None
    except Exception:
        return None
    return shape


def _bbox_fields(shape: Any, *, digits: int = 6):
    """Return (bbox dict | None, bounded flag).  None means unbounded/unknown."""
    try:
        box = shape.BoundBox
        raw = [box.XMin, box.YMin, box.ZMin, box.XMax, box.YMax, box.ZMax]
    except Exception:
        return None, False
    values = [_num(value, limit=BBOX_MAGNITUDE_LIMIT, digits=digits) for value in raw]
    if any(value is None for value in values):
        return None, False
    xmin, ymin, zmin, xmax, ymax, zmax = values
    return {
        'min': [xmin, ymin, zmin],
        'max': [xmax, ymax, zmax],
        'size': [round(xmax - xmin, digits), round(ymax - ymin, digits),
                 round(zmax - zmin, digits)],
    }, True


def _count(shape: Any, attribute: str) -> int | None:
    try:
        return len(getattr(shape, attribute))
    except Exception:
        return None


def _surface_type_counts(shape: Any, *, limit: int = 2000):
    """Count faces per surface class.  NOT a feature/hole count."""
    counts: dict[str, int] = {}
    try:
        faces = list(shape.Faces)
    except Exception:
        return counts, False
    truncated = False
    for index, face in enumerate(faces):
        if index >= limit:
            truncated = True
            break
        try:
            name = type(face.Surface).__name__
        except Exception:
            name = 'Unknown'
        counts[name] = counts.get(name, 0) + 1
    return dict(sorted(counts.items())), truncated


def _shape_probe(obj: Any) -> dict | None:
    """Cheap shape facts used for output selection (no deep metric building)."""
    shape = _shape_of(obj)
    if shape is None:
        return None
    try:
        shape_type = str(shape.ShapeType)
    except Exception:
        return None
    solids: int | None
    has_faces: bool
    if shape_type in ('Solid',):
        solids, has_faces = 1, True
    elif shape_type in ('CompSolid',):
        solids = _count(shape, 'Solids') or 1
        has_faces = True
    elif shape_type == 'Compound':
        solids = _count(shape, 'Solids') or 0
        has_faces = bool(solids) or bool(_count(shape, 'Faces') or 0)
    elif shape_type in ('Shell', 'Face', 'Unknown'):
        solids, has_faces = 0, bool(_count(shape, 'Faces') or 0)
    else:                                   # Wire / Edge / Vertex
        solids, has_faces = 0, False
    _, bounded = _bbox_fields(shape)
    return {'shape': shape, 'shape_type': shape_type, 'solids': solids,
            'has_faces': has_faces, 'bounded': bounded}


def shape_geometry(obj: Any, *, detail: bool = False) -> dict | None:
    """JSON-safe geometry summary or None when the object has no live shape."""
    probe = _shape_probe(obj)
    if probe is None:
        return None
    shape = probe['shape']
    unbounded = not probe['bounded']
    geometry: dict[str, Any] = {
        'shape_type': probe['shape_type'],
        'valid': None,
        'unbounded': unbounded,
        'solids': probe['solids'],
    }
    try:
        geometry['valid'] = bool(shape.isValid())
    except Exception:
        geometry['valid'] = None
    solids = probe['solids'] or 0
    geometry['volume_applicable'] = solids > 0
    if unbounded:
        geometry.update({
            'volume_mm3': None, 'area_mm2': None, 'bbox_mm': None,
            'faces': None, 'edges': None, 'vertices': None, 'shells': None,
            'note': ('native bounds are not finite (infinite datum/helper shape); '
                     'volume, area and bounds are withheld'),
        })
        if detail:
            geometry['solid_volumes_mm3'] = []
            geometry['surface_types'] = {}
        return geometry
    bbox, _ = _bbox_fields(shape)
    geometry['bbox_mm'] = list(bbox['size']) if bbox else None
    geometry['faces'] = _count(shape, 'Faces')
    geometry['edges'] = _count(shape, 'Edges')
    geometry['vertices'] = _count(shape, 'Vertexes')
    geometry['shells'] = _count(shape, 'Shells')
    volume = None
    if solids > 0:
        volume = _num(shape.Volume, limit=GEOM_MAGNITUDE_LIMIT)
    geometry['volume_mm3'] = volume
    area = _num(shape.Area, limit=GEOM_MAGNITUDE_LIMIT)
    geometry['area_mm2'] = area
    if solids > 0 and volume is None:
        geometry['note'] = ('native volume is missing or non-finite; not reported '
                            'as a measurement')
    elif solids == 0:
        geometry['note'] = ('shape contains no solid (shell/face/wire); volume is '
                            'not meaningful here')
    elif (geometry['shape_type'] == 'Compound' and solids > 1):
        geometry['note'] = ('aggregate of %d solids: volume is the sum of solid '
                            'volumes, not a boolean union volume' % solids)
    if detail:
        solid_volumes = []
        try:
            for index, solid in enumerate(shape.Solids):
                if index >= 200:
                    break
                value = _num(solid.Volume, limit=GEOM_MAGNITUDE_LIMIT)
                solid_volumes.append(value)
        except Exception:
            solid_volumes = []
        geometry['solid_volumes_mm3'] = solid_volumes
        geometry['bbox'] = bbox
        surface_types, surface_truncated = _surface_type_counts(shape)
        geometry['surface_types'] = surface_types
        geometry['surface_types_note'] = ('face counts by surface class; a cylinder '
                                         'face can be a hole, a boss or a fillet '
                                         'and is NOT a hole count')
        if surface_truncated:
            geometry['surface_types_truncated'] = True
        try:
            geometry['element_map_size'] = len(shape.ElementMap)
            geometry['element_map_version'] = str(shape.ElementMapVersion)
        except Exception:
            pass
    return geometry


def _is_shape_candidate(probe: dict | None) -> bool:
    if probe is None:
        return False
    return bool(probe['has_faces']) and bool(probe['bounded'])


# --------------------------------------------------------------------------
# output selection
# --------------------------------------------------------------------------
def _explicit_objects(doc: Any, objects: list[Any], names: Any) -> list[Any]:
    if names is None:
        return []
    if isinstance(names, (str,)) or hasattr(names, 'Name'):
        names = [names]
    if not isinstance(names, (list, tuple, set)):
        raise ObserveError('names must be an object name, a DocumentObject or a '
                           'sequence of them')
    by_name = {}
    for obj in objects:
        by_name[_object_name(obj)] = obj
    resolve: dict[str, Any] = {}
    unknown: list[str] = []
    for entry in names:
        if hasattr(entry, 'Name'):
            name = _object_name(entry)
            if name in by_name:
                resolve.setdefault(name, by_name[name])
            else:                            # object from another document
                resolve.setdefault(name, entry)
            continue
        if not isinstance(entry, str):
            unknown.append(repr(entry))
            continue
        target = None
        if hasattr(doc, 'getObject'):
            try:
                target = doc.getObject(entry)
            except Exception:
                target = None
        if target is None:
            unknown.append(entry)
        else:
            resolve.setdefault(entry, target)
    if unknown:
        available, _ = _capped(sorted(by_name), MAX_NAMED_NAMES)
        raise ObserveError(
            'unknown object name(s): %s; available: %s'
            % (', '.join(unknown), ', '.join(available) or '(none)'))
    ordered = []
    seen = set()
    for entry in names:
        key = _object_name(entry) if hasattr(entry, 'Name') else str(entry)
        if key in seen:
            continue
        seen.add(key)
        ordered.append(resolve[key])
    return ordered


def _consumed_output_names(doc: Any, candidates: list[Any]) -> set[str]:
    """Names of candidates that another candidate consumes.

    An edge owner -> input counts as consumption when the input is a link
    property value, a ``Group`` member, or an ``OutList`` edge that is not
    explainable as an expression-only reference.  Measured: expression
    dependencies DO appear in ``OutList`` (E2.Length = E1.Length adds E1), so a
    naive OutList closure would hide independent solids.
    """
    by_name = {_object_name(obj): obj for obj in _doc_objects(doc)}
    candidate_names = {_object_name(obj) for obj in candidates}
    consumed: set[str] = set()
    stack = list(candidate_names)
    while stack:
        name = stack.pop()
        owner = by_name.get(name)
        if owner is None:
            continue
        targets = set(_link_names(owner))
        targets.update(_group_names(owner))
        expressions = _expression_map(owner)
        expression_refs = _expression_object_refs(expressions)
        for child in _out_list(owner):
            if child in expression_refs and child not in targets:
                continue
            targets.add(child)
        for child in targets:
            if child in candidate_names and child not in consumed:
                consumed.add(child)
                stack.append(child)
    return consumed


def select_output_objects(doc: Any, names: Any = None) -> list[Any]:
    """Select the objects that represent this document's output geometry.

    ``names`` (str / DocumentObject / sequence of them) is authoritative: those
    objects are returned in the given order, deduplicated, and unknown names
    raise :class:`ObserveError` instead of being silently dropped.

    Without ``names``, shape-bearing result objects are returned in creation
    order, excluding datum planes/axes/points, sketches, infinite helper shapes,
    every feature contained in a ``PartDesign::Body`` (the Body is the design
    result; a broken intermediate feature is reported as a native error instead
    of being rendered/exported), and every object that another selected candidate
    consumes (Body vs. its Tip, boolean inputs, linked sources, Part/Group
    members).  Containers such as ``App::Part``/``App::DocumentObjectGroup`` ARE
    selected when they expose an aggregate shape, which is what FreeCAD 1.1.3
    does; a container without an aggregate shape falls back to its children.
    """
    if doc is None:
        raise ObserveError('select_output_objects requires a live FreeCAD document')
    objects = _doc_objects(doc)
    if not objects:
        return []
    if isinstance(names, (list, tuple, set)) and not names:
        # an empty sequence means "not supplied": fall back to the default set
        names = None
    if names is not None:
        return _explicit_objects(doc, objects, names)
    # Features that live inside a PartDesign::Body are never outputs, even when
    # the Body itself is broken and its own Shape is missing: the Body is the
    # design result and the failure of an intermediate feature belongs in the
    # native error report, not in the render/export selection.
    body_children = _body_contained_names(objects)
    candidates = []
    for obj in objects:
        name = _object_name(obj)
        type_id = _object_type(obj)
        if name in body_children:
            continue
        if type_id in DATUM_TYPE_IDS or type_id.startswith(SKETCH_TYPE_PREFIX):
            continue
        probe = _shape_probe(obj)
        if not _is_shape_candidate(probe):
            continue
        candidates.append(obj)
    if not candidates:
        return []
    consumed = _consumed_output_names(doc, candidates)
    return [obj for obj in candidates if _object_name(obj) not in consumed]


def _is_body_type(type_id: str) -> bool:
    return type_id == 'PartDesign::Body' or type_id.startswith('PartDesign::Body')


def _body_contained_names(objects: list[Any]) -> set[str]:
    """Names of objects reachable through any ``PartDesign::Body`` Group."""
    contained: set[str] = set()
    stack: list[str] = []
    for obj in objects:
        if not _is_body_type(_object_type(obj)):
            continue
        for child in _group_names(obj):
            if child not in contained:
                contained.add(child)
                stack.append(child)
    if stack:
        by_name = {_object_name(obj): obj for obj in objects}
        while stack:
            name = stack.pop()
            owner = by_name.get(name)
            if owner is None:
                continue
            for child in _group_names(owner):
                if child not in contained:
                    contained.add(child)
                    stack.append(child)
    return contained


# --------------------------------------------------------------------------
# per-object records
# --------------------------------------------------------------------------
def _placement_fields(placement: Any) -> dict | None:
    out: dict[str, Any] = {}
    try:
        base = placement.Base
        out['base_mm'] = [_num(base.x), _num(base.y), _num(base.z)]
    except Exception:
        pass
    try:
        rotation = placement.Rotation
        axis = rotation.Axis
        angle = _finite(rotation.Angle)
        out['rotation'] = {
            'axis': [_num(axis.x), _num(axis.y), _num(axis.z)],
            'angle_deg': None if angle is None else round(math.degrees(angle), 6),
            'angle_rad': None if angle is None else round(angle, 9),
        }
    except Exception:
        pass
    try:
        out['repr'] = str(placement)
    except Exception:
        pass
    return out or None


def _parameter_value(obj: Any, prop: str, type_id: str) -> dict:
    record: dict[str, Any] = {'type_id': type_id}
    try:
        raw = obj.getPropertyByName(prop)
    except Exception as exc:
        record['omitted'] = True
        record['reason'] = f'read failed: {type(exc).__name__}'
        return record
    if raw is None:
        record['value'] = None
        return record
    if type_id in LINK_TYPE_IDS:
        record['links'] = _capped(_dedup_sorted(_names_of(raw)), MAX_NAMED_NAMES)[0]
        return record
    if type_id in PLACEMENT_TYPE_IDS:
        placement = _placement_fields(raw)
        if placement is None:
            record['omitted'] = True
            record['reason'] = 'placement could not be read'
        else:
            record.update(placement)
        return record
    if type_id in VECTOR_TYPE_IDS:
        try:
            record.update({'x': _num(raw.x), 'y': _num(raw.y), 'z': _num(raw.z)})
        except Exception:
            record['omitted'] = True
            record['reason'] = 'vector could not be read'
        return record
    if hasattr(raw, 'UserString'):
        record['value'] = _num(raw.Value, limit=GEOM_MAGNITUDE_LIMIT)
        try:
            record['user_string'] = str(raw.UserString)
        except Exception:
            record['user_string'] = None
        record['unit'] = _unit_of(raw)
        return record
    if type_id in QUANTITY_TYPES:
        record['value'] = _num(getattr(raw, 'Value', raw), limit=GEOM_MAGNITUDE_LIMIT)
        return record
    if type_id in SCALAR_TYPE_IDS:
        if isinstance(raw, (bool, int, float)):
            record['value'] = _num(raw, limit=GEOM_MAGNITUDE_LIMIT) if isinstance(
                raw, float) else raw
        else:
            record['value'] = str(raw)
        if type_id == 'App::PropertyEnumeration':
            try:
                record['enum'] = [str(item) for item in
                                  obj.getEnumerationsOfProperty(prop)]
            except Exception:
                pass
        return record
    record['omitted'] = True
    record['reason'] = 'native property type is not serialized by cad3.observe'
    return record


def _parameters(obj: Any, *, limit: int = MAX_PARAMETERS_PER_OBJECT):
    params: dict[str, Any] = {}
    truncated = False
    try:
        props = list(obj.PropertiesList)
    except Exception:
        return params, False
    for prop in props:
        if prop in SKIP_PARAMS or prop.startswith('_'):
            continue
        try:
            type_id = str(obj.getTypeIdOfProperty(prop))
        except Exception:
            continue
        if len(params) >= limit:
            truncated = True
            break
        record = _parameter_value(obj, prop, type_id)
        if record.get('omitted'):
            # keep opaque native types visible but never serialize their value
            params[prop] = {'type_id': type_id, 'omitted': True,
                            'reason': record.get('reason', 'not serialized')}
            continue
        try:
            group = obj.getGroupOfProperty(prop)
        except Exception:
            group = None
        if group:
            record['group'] = str(group)
        params[prop] = record
    return params, truncated


def _link_summary(obj: Any) -> dict | None:
    type_id = _object_type(obj)
    if not (type_id == 'App::Link' or type_id.startswith('App::Link')):
        return None
    summary: dict[str, Any] = {}
    for prop, key in (('LinkedObject', 'linked_object'),
                      ('LinkTransform', 'link_transform'),
                      ('LinkCopyOnChange', 'link_copy_on_change'),
                      ('LinkClaimChild', 'claims_child'),
                      ('Scale', 'scale')):
        if not hasattr(obj, prop):
            continue
        try:
            value = getattr(obj, prop)
        except Exception:
            continue
        if hasattr(value, 'Name'):
            summary[key] = str(value.Name)
        elif isinstance(value, (bool, int, float, str)):
            summary[key] = value
    targets = _link_names(obj)
    if targets:
        summary['link_inputs'] = _capped(targets, MAX_NAMED_NAMES)[0]
    linked_document = None
    try:
        if obj.LinkedObject is not None:
            linked_document = str(obj.LinkedObject.Document.Name)
    except Exception:
        linked_document = None
    if linked_document:
        summary['linked_document'] = linked_document
    return summary or None


def _proxy_summary(obj: Any) -> dict | None:
    if not hasattr(obj, 'Proxy'):
        return None
    try:
        proxy = obj.Proxy
    except Exception:
        return None
    module = type(proxy).__module__ if proxy is not None else None
    summary = {
        'is_set': proxy is not None,
        'class': f'{module}.{type(proxy).__qualname__}' if proxy is not None else None,
        'implementation_in_snapshot': False,
        'requirement': ('record the proxy module (and its version) as an external '
                        'dependency; a snapshot cannot carry the implementation'),
    }
    if module:
        import sys
        module_obj = sys.modules.get(module)
        summary['module_file'] = getattr(module_obj, '__file__', None)
        summary['module_version'] = getattr(module_obj, '__version__', None)
    return summary


def _attachment_summary(obj: Any) -> dict | None:
    if not hasattr(obj, 'AttachmentSupport'):
        return None
    summary: dict[str, Any] = {}
    try:
        summary['support'] = _capped(
            _dedup_sorted(_names_of(obj.AttachmentSupport)), MAX_NAMED_NAMES)[0]
    except Exception:
        summary['support'] = None
    for prop, key in (('MapMode', 'map_mode'), ('AttacherType', 'attacher_type')):
        if hasattr(obj, prop):
            try:
                summary[key] = str(getattr(obj, prop))
            except Exception:
                pass
    return summary


def _sketch_record(obj: Any, *, detail: bool):
    """Sketch geometry, constraints (0-based index + native 1-based id) and
    read-only solver diagnostics.  ``solve()`` is never called."""
    warnings: list[str] = []
    truncated: list[str] = []
    record: dict[str, Any] = {}
    try:
        record['geometry_count'] = int(obj.GeometryCount)
    except Exception:
        record['geometry_count'] = None
    try:
        record['constraint_count'] = int(obj.ConstraintCount)
    except Exception:
        record['constraint_count'] = None

    diagnostics: dict[str, Any] = {}
    unavailable: list[str] = []
    for prop, key in (('DoF', 'dof'), ('FullyConstrained', 'fully_constrained')):
        if not hasattr(obj, prop):
            unavailable.append(prop)
            diagnostics[key] = None
            continue
        try:
            value = getattr(obj, prop)
            diagnostics[key] = bool(value) if prop == 'FullyConstrained' else int(value)
        except Exception:
            unavailable.append(prop)
            diagnostics[key] = None
    native_lists = {}
    for prop, key in (('ConflictingConstraints', 'conflicting_constraint_ids'),
                      ('RedundantConstraints', 'redundant_constraint_ids'),
                      ('PartiallyRedundantConstraints',
                       'partially_redundant_constraint_ids')):
        if not hasattr(obj, prop):
            unavailable.append(prop)
            native_lists[key] = None
            continue
        try:
            native_lists[key] = sorted(int(item) for item in getattr(obj, prop))
        except Exception:
            unavailable.append(prop)
            native_lists[key] = None
    diagnostics.update(native_lists)

    def _to_zero_based(native):
        if native is None:
            return None
        return [item - 1 for item in native]

    diagnostics['conflicting_indices_0based'] = _to_zero_based(
        native_lists.get('conflicting_constraint_ids'))
    diagnostics['redundant_indices_0based'] = _to_zero_based(
        native_lists.get('redundant_constraint_ids'))
    partial = native_lists.get('partially_redundant_constraint_ids')
    if partial:
        # measured: the list is exposed but its 1-based basis was NOT verified
        diagnostics['partially_redundant_indices_0based'] = None
        diagnostics['partially_redundant_basis'] = (
            'unverified: native list returned non-empty but its numbering basis '
            'was not measured; the 0-based mapping is withheld rather than guessed')
    else:
        diagnostics['partially_redundant_indices_0based'] = []
    diagnostics['id_basis'] = {
        'native_ids': '1-based, as reported by the Sketcher solver',
        'constraint_index': '0-based, Constraints[i] as used by the Python API',
    }
    diagnostics['solve_called'] = False
    diagnostics['solve_code'] = None
    diagnostics['note'] = ('inspection is read-only: solve() and '
                           'autoRemoveRedundants() are not called; the values are '
                           'whatever the last recompute left behind')
    diagnostics['unavailable'] = unavailable
    record['diagnostics'] = diagnostics

    if detail:
        geometry = []
        try:
            raw_geometry = list(obj.Geometry)
        except Exception:
            raw_geometry = []
        if len(raw_geometry) > MAX_SKETCH_GEOMETRY:
            truncated.append('geometry')
            warnings.append('sketch geometry truncated to %d of %d elements'
                            % (MAX_SKETCH_GEOMETRY, len(raw_geometry)))
        for index, item in enumerate(raw_geometry[:MAX_SKETCH_GEOMETRY]):
            entry: dict[str, Any] = {'index': index,
                                     'type': type(item).__name__}
            try:
                entry['construction'] = bool(obj.getConstruction(index))
            except Exception:
                entry['construction'] = None
            for attr in ('StartPoint', 'EndPoint', 'Center', 'Location'):
                if not hasattr(item, attr):
                    continue
                value = getattr(item, attr)
                if hasattr(value, 'x'):
                    entry[attr] = [_num(value.x), _num(value.y), _num(value.z)]
            for attr in ('Radius', 'MajorRadius', 'MinorRadius', 'StartAngle',
                         'EndAngle'):
                if hasattr(item, attr):
                    entry[attr] = _num(getattr(item, attr))
            geometry.append(entry)
        record['geometry'] = geometry

        expressions = _expression_map(obj)
        constraints = []
        try:
            raw_constraints = list(obj.Constraints)
        except Exception:
            raw_constraints = []
        if len(raw_constraints) > MAX_CONSTRAINTS_PER_SKETCH:
            truncated.append('constraints')
            warnings.append('sketch constraints truncated to %d of %d entries'
                            % (MAX_CONSTRAINTS_PER_SKETCH, len(raw_constraints)))
        conflicting = set(native_lists.get('conflicting_constraint_ids') or [])
        redundant = set(native_lists.get('redundant_constraint_ids') or [])
        partial_ids = set(partial or [])
        for index, constraint in enumerate(raw_constraints[:MAX_CONSTRAINTS_PER_SKETCH]):
            native_id = index + 1
            entry = {'index': index, 'native_id': native_id}
            for attr, key in (('Type', 'type'), ('Name', 'name'),
                              ('Value', 'value'), ('First', 'first'),
                              ('FirstPos', 'first_pos'), ('Second', 'second'),
                              ('SecondPos', 'second_pos'), ('Third', 'third'),
                              ('ThirdPos', 'third_pos'), ('Driving', 'driving'),
                              ('IsActive', 'is_active'),
                              ('InVirtualSpace', 'in_virtual_space'),
                              ('LabelPosition', 'label_position')):
                if not hasattr(constraint, attr):
                    continue
                try:
                    value = getattr(constraint, attr)
                except Exception:
                    continue
                if key == 'value':
                    entry[key] = _num(value)
                elif key in ('first', 'first_pos', 'second', 'second_pos',
                             'third', 'third_pos'):
                    try:
                        entry[key] = int(value)
                    except Exception:
                        continue
                elif key == 'type':
                    entry[key] = str(value)
                elif key == 'name':
                    entry[key] = str(value)
                else:
                    entry[key] = bool(value)
            constraint_type = entry.get('type')
            entry['dimensional'] = constraint_type in DIMENSIONAL_CONSTRAINTS
            entry['is_conflicting'] = native_id in conflicting
            entry['is_redundant'] = native_id in redundant
            entry['is_partially_redundant'] = native_id in partial_ids
            expression = expressions.get(f'.Constraints.{entry.get("name") or ""}')
            if expression and entry.get('name'):
                entry['expression'] = expression
            constraints.append(entry)
        record['constraints'] = constraints
        record['expressions'] = expressions
        external = []
        if hasattr(obj, 'ExternalGeometry'):
            try:
                external = _capped(_dedup_sorted(_names_of(obj.ExternalGeometry)),
                                   MAX_NAMED_NAMES)[0]
            except Exception:
                external = []
        record['external_geometry'] = external
        record['attachment'] = _attachment_summary(obj)
    if truncated:
        record['truncated'] = truncated
    if warnings:
        record['warnings'] = warnings
    return record


def _spreadsheet_record(obj: Any):
    truncated: list[str] = []
    cells: dict[str, Any] = {}
    addresses: list[str] = []
    try:
        if hasattr(obj, 'getUsedCells'):
            addresses = [str(item) for item in obj.getUsedCells()]
        elif hasattr(obj, 'getUsedRange'):
            bounds = list(obj.getUsedRange())
            if len(bounds) == 2:
                first, last = bounds
                first_col, first_row = re.match(r'([A-Z]+)(\d+)', first).groups()  # type: ignore[union-attr]
                last_col, last_row = re.match(r'([A-Z]+)(\d+)', last).groups()     # type: ignore[union-attr]
                for col in range(ord(first_col), ord(last_col) + 1):
                    for row in range(int(first_row), int(last_row) + 1):
                        addresses.append(f'{chr(col)}{row}')
    except Exception:
        addresses = []
    if not addresses:
        for col in 'ABCDEFGH':
            for row in range(1, 41):
                addresses.append(f'{col}{row}')
    aliases: dict[str, Any] = {}
    for index, address in enumerate(addresses):
        if len(cells) >= MAX_SHEET_CELLS:
            truncated.append('cells')
            break
        try:
            raw = obj.getContents(address)
        except Exception:
            continue
        try:
            alias = obj.getAlias(address)
        except Exception:
            alias = None
        if not raw and not alias:
            continue
        entry: dict[str, Any] = {'raw': None if raw is None else str(raw)}
        try:
            entry['value'] = str(obj.get(address))
        except Exception as exc:
            entry['value'] = None
            entry['value_unavailable'] = (
                f'{type(exc).__name__}: cell value requires a recompute')
        if alias:
            entry['alias'] = str(alias)
            aliases[str(alias)] = {'cell': address, 'raw': entry['raw'],
                                   'value': entry['value']}
        cells[address] = entry
    record: dict[str, Any] = {'cells': cells, 'aliases': aliases}
    if truncated:
        record['truncated'] = truncated
    return record


def _object_record(obj: Any, *, expanded: bool, is_output: bool,
                   container: str | None) -> dict:
    type_id = _object_type(obj)
    state = _state_of(obj)
    geometry = shape_geometry(obj, detail=expanded)
    record: dict[str, Any] = {
        'name': _object_name(obj),
        'type': type_id,
        'label': _object_label(obj),
        'state': state,
        'visibility': _visibility_of(obj),
        'role': _role_of(type_id, geometry),
        'is_output': is_output,
        'container': container,
        'geometry': geometry,
        'dependencies': {'out': _out_list(obj), 'in': _in_list(obj)},
        'expanded': expanded,
    }
    links = _link_property_map(obj)
    if links:
        record['links'] = links
    if 'Python' in type_id or hasattr(obj, 'Proxy'):
        proxy = _proxy_summary(obj)
        if proxy:
            record['proxy'] = proxy
    if expanded:
        out_rec, out_truncated = _recursive_names(obj, 'OutListRecursive')
        in_rec, in_truncated = _recursive_names(obj, 'InListRecursive')
        record['dependencies_full'] = {
            'out_recursive': out_rec, 'in_recursive': in_rec,
            'truncated': out_truncated or in_truncated,
        }
        group = _group_names(obj)
        tip = None
        if hasattr(obj, 'Tip'):
            try:
                tip = str(obj.Tip.Name) if obj.Tip else None
            except Exception:
                tip = None
        record['containment'] = {
            'parent': container,
            'children': group,
            'tip': tip,
        }
        parameters, truncated = _parameters(obj)
        record['parameters'] = parameters
        if truncated:
            record['truncated'] = ['parameters']
        expressions = _expression_map(obj)
        record['expressions'] = expressions
        record['expression_object_refs'] = sorted(_expression_object_refs(expressions))
        record['attachment'] = _attachment_summary(obj)
        link = _link_summary(obj)
        if link:
            record['link'] = link
        if 'proxy' not in record:
            proxy = _proxy_summary(obj)
            if proxy:
                record['proxy'] = proxy
        if type_id.startswith(SKETCH_TYPE_PREFIX):
            record['sketch'] = _sketch_record(obj, detail=True)
        elif type_id == 'Spreadsheet::Sheet':
            record['spreadsheet'] = _spreadsheet_record(obj)
        placement = None
        try:
            if hasattr(obj, 'Placement'):
                placement = _placement_fields(obj.Placement)
        except Exception:
            placement = None
        if placement:
            record['placement'] = placement
    elif type_id.startswith(SKETCH_TYPE_PREFIX):
        # compact mode keeps the solver diagnostics (cheap and high value)
        record['sketch'] = _sketch_record(obj, detail=False)
    return record


def _role_of(type_id: str, geometry: dict | None) -> str:
    if type_id in DATUM_TYPE_IDS:
        return 'datum'
    if type_id.startswith(SKETCH_TYPE_PREFIX):
        return 'sketch'
    if type_id == 'App::Link' or type_id.startswith('App::Link'):
        return 'link'
    if type_id in CONTAINER_TYPE_IDS:
        return 'container'
    if geometry is None:
        return 'data'
    if (geometry.get('solids') or 0) > 0:
        return 'result'
    return 'geometry'


# --------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------
def _sanitize(value: Any) -> Any:
    """Final guarantee: JSON-safe, no NaN/Infinity, no opaque native objects."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(key): _sanitize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_sanitize(item) for item in value]
    if isinstance(value, (bytes, bytearray)):
        return f'<{len(value)} bytes>'
    return f'<omitted {type(value).__name__}>'


def inspect_document(doc: Any, object_name: str | None = None,
                     detail: bool = False) -> dict:
    """Inspect a live FreeCAD document and return a JSON-safe snapshot.

    ``object_name`` scopes ``objects`` to that single object and always expands
    it (asking for one object implies wanting its detail).  ``detail=True``
    expands every object.  The snapshot never recomputes and never solves.
    """
    if doc is None:
        raise ObserveError('inspect_document requires a live FreeCAD document')
    app = _app()
    objects = _doc_objects(doc)
    warnings: list[str] = []
    truncated: list[str] = []

    scoped = None
    if object_name is not None:
        if not hasattr(doc, 'getObject'):
            raise ObserveError('document does not support getObject()')
        scoped = doc.getObject(object_name)
        if scoped is None:
            available, _ = _capped(sorted(_object_name(item) for item in objects),
                                   MAX_NAMED_NAMES)
            raise ObserveError('unknown object %r; available: %s'
                               % (object_name, ', '.join(available) or '(none)'))
        selected = [scoped]
    else:
        selected = objects
        if len(selected) > MAX_OBJECTS:
            warnings.append('document has %d objects; only the first %d are '
                            'inspected' % (len(selected), MAX_OBJECTS))
            truncated.append('objects')
            selected = selected[:MAX_OBJECTS]

    try:
        outputs = select_output_objects(doc)
    except ObserveError as exc:
        outputs = []
        warnings.append(f'output selection failed: {exc}')
    output_names = [_object_name(item) for item in outputs]
    output_set = set(output_names)

    # containment map (App::Link mirrors its linked object's Group, so a Link is
    # never treated as the containment owner of another object's children)
    parents: dict[str, str] = {}
    for obj in objects:
        owner = _object_type(obj)
        if owner == 'App::Link' or owner.startswith('App::Link'):
            continue
        name = _object_name(obj)
        for child in _group_names(obj):
            parents.setdefault(child, name)

    records = []
    for obj in selected:
        name = _object_name(obj)
        expanded = bool(detail or (scoped is not None and obj is scoped))
        record = _object_record(obj, expanded=expanded, is_output=name in output_set,
                                container=parents.get(name))
        records.append(record)

    # document-wide native state facts (not limited to the inspected subset)
    errors: list[dict] = []
    touched: list[str] = []
    invalid: list[str] = []
    for obj in objects:
        name = _object_name(obj)
        state_flags = _state_of(obj)
        if _needs_recompute(state_flags) and name not in touched:
            touched.append(name)
        if _is_invalid(state_flags):
            invalid.append(name)
            errors.append({
                'object': name,
                'type': _object_type(obj),
                'state': state_flags,
                'message': ('native object state reports %s: its shape is missing or '
                            'not usable; fix the feature and recompute'
                            % '/'.join(flag for flag in state_flags
                                       if flag in ('Invalid', 'Error'))),
            })
    if touched:
        warnings.append('%d object(s) are Touched: the snapshot was taken without a '
                        'recompute, so shapes may be stale (%s%s)'
                        % (len(touched), ', '.join(touched[:10]),
                           '...' if len(touched) > 10 else ''))
    if invalid:
        warnings.append('%d object(s) report Invalid/Error state (%s)'
                        % (len(invalid), ', '.join(invalid[:10])))
    unbounded = [record['name'] for record in records
                 if (record.get('geometry') or {}).get('unbounded')]
    if unbounded:
        warnings.append('%d object(s) have non-finite bounds (infinite helper/datum '
                        'shapes): %s' % (len(unbounded), ', '.join(unbounded[:10])))
    if not output_names:
        warnings.append('no output geometry selected: the document has no '
                        'shape-bearing result objects')

    try:
        version = app.Version()
    except Exception:
        version = []
    document: dict[str, Any] = {
        'name': str(getattr(doc, 'Name', '')),
        'label': str(getattr(doc, 'Label', '')),
        'file_name': str(getattr(doc, 'FileName', '') or ''),
        'object_count': len(objects),
        'mode': 'detail' if detail else 'compact',
        'scoped_object': object_name,
        'outputs': output_names,
        'output_count': len(output_names),
        'native_root_objects': _capped(
            _dedup_sorted([item.Name for item in getattr(doc, 'RootObjects', [])]),
            MAX_NAMED_NAMES)[0],
        'native_root_objects_note': (
            'native Document.RootObjects; on FreeCAD 1.1.3 this means "no other '
            'object references it" (measured), NOT the GUI top-level object list'),
        'recompute': {
            'needs_recompute': bool(touched),
            'touched': _capped(touched, MAX_NAMED_NAMES)[0],
            'invalid': _capped(invalid, MAX_NAMED_NAMES)[0],
        },
    }

    state: dict[str, Any] = {
        'schema': SCHEMA,
        'generated_by': GENERATOR,
        'freecad': {
            'version': '.'.join(str(item) for item in list(version)[:3]) or None,
            'version_raw': [str(item) for item in version],
            'gui_up': bool(getattr(app, 'GuiUp', 0)),
            'headless': not bool(getattr(app, 'GuiUp', 0)),
        },
        'units': {
            'length': 'mm', 'area': 'mm^2', 'volume': 'mm^3', 'angle': 'deg',
            'note': ('numeric values are native FreeCAD quantities in these units; '
                     'quantity parameters also carry the native UserString'),
        },
        'document': document,
        'outputs': output_names,
        'objects': records,
        'errors': errors,
        'warnings': warnings,
        'limits': list(_LIMITS),
    }
    if truncated:
        state['truncated'] = truncated
    return _sanitize(state)


# --------------------------------------------------------------------------
# read-only code-style view
# --------------------------------------------------------------------------
_HEADER = '''\
# ===========================================================================
# READ-ONLY DESCRIPTIVE VIEW — schema {schema}
# Generated from an inspection snapshot of a REAL FreeCAD document. Every value
# below was read out of the live document; none of it is hand-written.
#
# THIS IS NOT AN EXECUTABLE REBUILDER AND IS NOT RUN BY ANYTHING. Measured
# reasons on FreeCAD 1.1.3:
#   * OCCT element names (Face1/Edge1/...) are rebuild-order dependent and their
#     name->history-key binding is regenerated when geometry changes.
#   * FeaturePython objects store only a <module>.<class> reference; the
#     implementation is not in the snapshot.
#   * With GuiUp == 0 there is no ViewObject, so display-only state is absent.
# Snapshot limits reported by the producer are listed at the end of this file.
# ===========================================================================
'''


def _fmt_number(value: Any, unit: str = '') -> str:
    if value is None:
        return 'n/a'
    if isinstance(value, bool):
        return 'True' if value else 'False'
    if isinstance(value, (int, float)):
        text = f'{value:g}'
        return f'{text} {unit}'.strip()
    return str(value)


def _geometry_value(geometry: dict, *keys):
    for key in keys:
        if geometry.get(key) is not None:
            return geometry[key]
    return None


def _geometry_bbox(geometry: dict):
    bbox = geometry.get('bbox_mm')
    if isinstance(bbox, (list, tuple)):
        return list(bbox)
    nested = geometry.get('bbox')
    if isinstance(nested, dict):
        size = nested.get('size')
        if isinstance(size, (list, tuple)):
            return list(size)
    return None


def _fmt_geometry(geometry: Any) -> str:
    """Render a geometry dict, tolerating the worker fallback schema too."""
    if not geometry:
        return 'geometry=None'
    if geometry.get('available') is False and 'volume_mm3' not in geometry:
        return 'geometry: unavailable (this object has no Shape)'
    if geometry.get('null'):
        return 'geometry: null native shape'
    parts = []
    shape_type = geometry.get('shape_type') or geometry.get('type')
    if shape_type:
        parts.append(f'type={shape_type}')
    parts.append(f"solids={_fmt_number(geometry.get('solids'))}")
    if geometry.get('unbounded'):
        parts.append('unbounded=True')
        parts.append('volume_mm3=n/a (infinite/non-finite shape)')
        return ' '.join(parts)
    volume = _geometry_value(geometry, 'volume_mm3', 'volume')
    parts.append(f"volume_mm3={_fmt_number(volume, 'mm^3')}")
    area = _geometry_value(geometry, 'area_mm2', 'area')
    parts.append(f"area_mm2={_fmt_number(area, 'mm^2')}")
    bbox = _geometry_bbox(geometry)
    if bbox:
        parts.append('bbox_mm=[' + ', '.join(_fmt_number(item) for item in bbox) + ']')
    else:
        parts.append('bbox_mm=n/a')
    parts.append(f"valid={geometry.get('valid')}")
    parts.append(f"faces={_fmt_number(geometry.get('faces'))}")
    if geometry.get('volume_applicable') is False:
        parts.append('volume_applicable=False')
    return ' '.join(parts)


def _fmt_links(links: Any) -> str:
    if not isinstance(links, list) or not links:
        return ''
    shown = links[:MAX_NAMED_NAMES]
    text = ', '.join(repr(item) for item in shown)
    if len(links) > len(shown):
        text += f", ... (+{len(links) - len(shown)} more)"
    return text


def code_view(state: Any) -> str:
    """Render an inspection snapshot as a compact read-only text view.

    The result is descriptive only: it is explicitly not rebuild code and must
    not be executed or evaluated as Python.
    """
    if not isinstance(state, dict):
        raise ObserveError('code_view expects the dict returned by inspect_document()')
    schema = state.get('schema') or SCHEMA
    lines: list[str] = [_HEADER.format(schema=schema)]
    document = state.get('document') or {}
    freecad = state.get('freecad') or {}
    outputs = state.get('outputs') or []
    objects = state.get('objects') or []

    lines.append('# --- document ---------------------------------------------------------')
    lines.append(f"document = {document.get('name')!r}   # Label="
                 f"{document.get('label')!r}")
    if document.get('file_name'):
        lines.append(f"source   = {document['file_name']!r}")
    version = freecad.get('version')
    gui_up = freecad.get('gui_up')
    lines.append(f'freecad  = {version}   gui_up={gui_up}   headless='
                 f'{freecad.get("headless")}')
    lines.append(f"objects  = {document.get('object_count')}   "
                 f"mode={document.get('mode')!r}   "
                 f"scoped_object={document.get('scoped_object')!r}")
    recompute = document.get('recompute') or {}
    lines.append(f"outputs  = {_fmt_links(outputs)}"
                 + (f"   # {len(outputs)} object(s)" if outputs
                    else '   # none selected'))
    lines.append(f"needs_recompute = {recompute.get('needs_recompute')}   "
                 f"touched={_fmt_links(recompute.get('touched')) or '[]'}   "
                 f"invalid={_fmt_links(recompute.get('invalid')) or '[]'}")
    if document.get('native_root_objects'):
        lines.append('native_root_objects = '
                     + _fmt_links(document.get('native_root_objects')))
    units = state.get('units') or {}
    if units:
        lines.append('units    = ' + ', '.join(
            f'{key}={value}' for key, value in units.items() if key != 'note'))
    lines.append('')

    lines.append('# --- objects in creation order ---------------------------------------')
    shown = objects[:MAX_VIEW_OBJECTS]
    output_lookup = set(outputs) if isinstance(outputs, (list, tuple)) else set()
    for record in shown:
        if not isinstance(record, dict):
            continue
        name = record.get('name')
        geometry = record.get('geometry')
        if geometry is None:
            geometry = record.get('shape')       # worker-fallback schema
        is_output = record.get('is_output')
        if is_output is None:
            is_output = name in output_lookup
        role = record.get('role') or 'unknown'
        header = (f"{name} = {record.get('type')}   # Label="
                  f"{record.get('label')!r} State={record.get('state')!r} "
                  f"role={role} "
                  f"output={'yes' if is_output else 'no'}")
        if record.get('container'):
            header += f" container={record['container']!r}"
        if not geometry:
            header += ' geometry=None'
        lines.append(header)
        if geometry:
            lines.append(f'    geometry: {_fmt_geometry(geometry)}')
        if record.get('visibility') is False:
            lines.append('    visibility: False')
        dependencies = record.get('dependencies') or {}
        out_names = dependencies.get('out') or []
        in_names = dependencies.get('in') or []
        if out_names or in_names:
            parts = []
            if out_names:
                parts.append(f'out=[{_fmt_links(out_names)}]')
            if in_names:
                parts.append(f'in=[{_fmt_links(in_names)}]')
            lines.append('    depends: ' + ' '.join(parts))
        links = record.get('links') or {}
        for prop, targets in links.items():
            lines.append(f"    {prop} -> {_fmt_links(targets) or '[]'}")
        containment = record.get('containment')
        if isinstance(containment, dict):
            if containment.get('tip'):
                lines.append(f"    tip -> {containment['tip']!r}")
            children = containment.get('children') or []
            if children:
                lines.append(f"    children: {_fmt_links(children)}")
        full = record.get('dependencies_full')
        if isinstance(full, dict):
            lines.append(f"    out_recursive: "
                         f"{_fmt_links(full.get('out_recursive')) or '[]'}"
                         f"   truncated={full.get('truncated')}")
        placement = record.get('placement')
        if isinstance(placement, dict):
            lines.append(f"    placement: {placement.get('repr')}")
        attachment = record.get('attachment')
        if isinstance(attachment, dict) and attachment.get('support'):
            lines.append(f"    attachment: support="
                         f"{_fmt_links(attachment.get('support'))} "
                         f"map_mode={attachment.get('map_mode')!r}")
        expressions = record.get('expressions') or {}
        if expressions:
            lines.append('    expressions:')
            for prop, source in list(expressions.items())[:MAX_VIEW_PARAMS]:
                lines.append(f'        {prop} <- {source!r}')
            if len(expressions) > MAX_VIEW_PARAMS:
                lines.append(f'        ... (+{len(expressions) - MAX_VIEW_PARAMS} more)')
        parameters = record.get('parameters') or {}
        if parameters:
            lines.append('    parameters:')
            items = list(parameters.items())
            for prop, info in items[:MAX_VIEW_PARAMS]:
                if not isinstance(info, dict):
                    continue
                if info.get('omitted'):
                    lines.append(f"        {prop} = <not serialized: "
                                 f"{info.get('type_id')}>")
                    continue
                if info.get('links') is not None:
                    shown_links = _fmt_links(info.get('links'))
                    lines.append(f"        {prop} = [{shown_links}]   # "
                                 f"{info.get('type_id')} (link)")
                    continue
                value = info.get('user_string')
                if value is None:
                    value = info.get('value')
                    unit = info.get('unit') or ''
                    if unit and unit != '(unitless)':
                        value = _fmt_number(value, unit)
                    else:
                        value = _fmt_number(value)
                else:
                    # native Quantity UserString already carries its unit
                    value = value
                extra = ''
                if info.get('enum'):
                    extra = f"   enum={info['enum']!r}"
                if info.get('group'):
                    extra += f"   group={info['group']!r}"
                lines.append(f"        {prop} = {value}   # {info.get('type_id')}{extra}")
            if len(items) > MAX_VIEW_PARAMS:
                lines.append(f'        ... (+{len(items) - MAX_VIEW_PARAMS} more)')
        sketch = record.get('sketch')
        if isinstance(sketch, dict):
            diagnostics = sketch.get('diagnostics') or {}
            lines.append(f"    sketch: geometry_count={sketch.get('geometry_count')} "
                         f"constraint_count={sketch.get('constraint_count')} "
                         f"DoF={diagnostics.get('dof')} "
                         f"fully_constrained={diagnostics.get('fully_constrained')}")
            conflicting = diagnostics.get('conflicting_constraint_ids')
            redundant = diagnostics.get('redundant_constraint_ids')
            if not conflicting and not redundant:
                lines.append('        conflicting/redundant: none')
            else:
                lines.append(f"        conflicting_ids(1-based)="
                             f"{conflicting} "
                             f"-> indices(0-based)="
                             f"{diagnostics.get('conflicting_indices_0based')}")
                lines.append(f"        redundant_ids(1-based)="
                             f"{redundant} "
                             f"-> indices(0-based)="
                             f"{diagnostics.get('redundant_indices_0based')}")
            if diagnostics.get('partially_redundant_constraint_ids'):
                lines.append(f"        partially_redundant_ids(1-based)="
                             f"{diagnostics.get('partially_redundant_constraint_ids')}"
                             f"  # 0-based mapping withheld (basis unverified)")
            if diagnostics.get('unavailable'):
                lines.append(f"        diagnostics unavailable: "
                             f"{diagnostics.get('unavailable')}")
            lines.append(f"        solve_called={diagnostics.get('solve_called')} "
                         f"(read-only inspection)")
            constraints = sketch.get('constraints')
            if isinstance(constraints, list) and constraints:
                lines.append('        constraints:')
                for item in constraints[:MAX_VIEW_CONSTRAINTS]:
                    flags = []
                    if item.get('is_conflicting'):
                        flags.append('CONFLICTING')
                    if item.get('is_redundant'):
                        flags.append('REDUNDANT')
                    if item.get('is_partially_redundant'):
                        flags.append('PARTIALLY_REDUNDANT')
                    if item.get('dimensional'):
                        flags.append('dimensional')
                    if item.get('driving') is False:
                        flags.append('reference')
                    expression = item.get('expression')
                    lines.append(
                        f"            [{item.get('index')}] id={item.get('native_id')} "
                        f"{item.get('type')} value={_fmt_number(item.get('value'))} "
                        f"name={item.get('name')!r} first={item.get('first')}:"
                        f"{item.get('first_pos')} second={item.get('second')}:"
                        f"{item.get('second_pos')}"
                        + (f" {' '.join(flags)}" if flags else '')
                        + (f" expr={expression!r}" if expression else ''))
                if len(constraints) > MAX_VIEW_CONSTRAINTS:
                    lines.append(f"            ... (+"
                                 f"{len(constraints) - MAX_VIEW_CONSTRAINTS} more)")
            geometry = sketch.get('geometry')
            if isinstance(geometry, list) and geometry:
                lines.append('        geometry:')
                for item in geometry[:MAX_VIEW_CONSTRAINTS]:
                    details = ' '.join(
                        f'{key}={value!r}' for key, value in item.items()
                        if key not in ('index', 'type'))
                    lines.append(f"            [{item.get('index')}] {item.get('type')}"
                                 + (f' {details}' if details else ''))
                if len(geometry) > MAX_VIEW_CONSTRAINTS:
                    lines.append(f"            ... (+"
                                 f"{len(geometry) - MAX_VIEW_CONSTRAINTS} more)")
        spreadsheet = record.get('spreadsheet')
        if isinstance(spreadsheet, dict):
            aliases = spreadsheet.get('aliases') or {}
            lines.append(f"    spreadsheet: {len(spreadsheet.get('cells') or {})} "
                         f"cell(s), {len(aliases)} alias(es)")
            for alias, info in list(aliases.items())[:MAX_VIEW_PARAMS]:
                if isinstance(info, dict):
                    lines.append(f"        {alias} = {info.get('value')!r}   # "
                                 f"{info.get('cell')} raw={info.get('raw')!r}")
            cells = spreadsheet.get('cells') or {}
            if len(cells) > MAX_VIEW_PARAMS * 2:
                lines.append(f"        ... (+{len(cells)} cells; see the JSON "
                             f"snapshot for all of them)")
        link = record.get('link')
        if isinstance(link, dict):
            lines.append(f"    link: linked_object={link.get('linked_object')!r} "
                         f"transform={link.get('link_transform')} "
                         f"copy_on_change={link.get('link_copy_on_change')!r}")
        proxy = record.get('proxy')
        if isinstance(proxy, dict):
            lines.append(f"    proxy: {proxy.get('class')!r} "
                         f"implementation_in_snapshot="
                         f"{proxy.get('implementation_in_snapshot')}")
        if record.get('truncated'):
            lines.append(f"    truncated: {record['truncated']}")
        if record.get('warnings'):
            for warning in record['warnings']:
                lines.append(f'    warning: {warning}')
        lines.append('')
    if len(objects) > len(shown):
        lines.append(f'# ... {len(objects) - len(shown)} more object record(s) '
                     f'omitted from this view (see the JSON snapshot)')
        lines.append('')

    errors = state.get('errors') or []
    if errors:
        lines.append('# --- native errors ---------------------------------------------------')
        for item in errors:
            if isinstance(item, dict):
                lines.append(f"# {item.get('object')}: {item.get('message')} "
                             f"state={item.get('state')!r}")
            else:
                lines.append(f'# {item}')
        lines.append('')
    warnings = state.get('warnings') or []
    if warnings:
        lines.append('# --- warnings --------------------------------------------------------')
        for warning in warnings:
            lines.append(f'# {warning}')
        lines.append('')
    limits = state.get('limits') or []
    if limits:
        lines.append('# --- limits of this snapshot -----------------------------------------')
        for limit in limits:
            lines.append(f'# - {limit}')
        lines.append('')
    lines.append('# --- end of read-only view -------------------------------------------')
    return '\n'.join(lines) + '\n'
