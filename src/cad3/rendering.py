"""Headless shaded rendering of FreeCAD geometry for CAD3.

Runs INSIDE the FreeCAD worker process (``freecadcmd``, ``App.GuiUp == 0``).
Importing this module on the host must not require FreeCAD or VTK: both are
imported lazily inside :func:`render_objects`.

Pipeline (measured working on FreeCAD 1.1.3 / macOS with the VTK 9.3.1 that
ships inside the FreeCAD bundle):

    native BRep shape -> per-solid shape.tessellate(tolerance) -> vtkPolyData
    -> vtkPolyDataNormals -> per-object vtkActor -> offscreen
    vtkRenderWindow -> vtkWindowToImageFilter -> PNG

The result is programmatic offscreen shading.  It is explicitly NOT a native
FreeCAD GUI viewport screenshot: there is no ViewObject, no selection state and
no GUI annotation, because the process runs headless.

Crash safety (a native C++ crash cannot be caught in Python, so it is avoided):

* every object is tessellated **solid by solid**; a solid that raises is skipped
  and reported instead of failing the whole object;
* the resulting triangle count is *verified* before VTK consumes the mesh (a
  wrong ``vtkCellArray`` construction silently produced 14 cells for a
  24-triangle mesh and segfaulted ``vtkPolyDataNormals``);
* objects whose mesh construction fails, whose bounds are not finite, or whose
  triangle count exceeds :data:`MAX_TRIANGLES_PER_OBJECT` are skipped with an
  actionable reason in the returned metadata;
* the remaining C++ risk is process-level, so a caller that must never lose a
  whole run should render one view per worker subprocess (that isolation belongs
  to the worker, not to this module).
"""

from __future__ import annotations

import math
import os
from pathlib import Path
from typing import Any, Sequence

__all__ = ['SCHEMA', 'RenderError', 'DEFAULT_VIEWS', 'VIEW_NAMES', 'render_objects']

SCHEMA = 'cad3.rendering/v1'

DEFAULT_VIEWS = ('iso', 'top', 'front', 'right')

#: Stable view names -> (camera direction from the model centre, camera up).
#: Names are a fixed whitelist so a view name can never escape the output dir.
VIEW_DIRECTIONS: dict[str, tuple[tuple[float, float, float], tuple[float, float, float]]] = {
    'iso': ((1.0, -1.0, 0.8), (0.0, 0.0, 1.0)),
    'front': ((0.0, -1.0, 0.0), (0.0, 0.0, 1.0)),
    'back': ((0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
    'right': ((1.0, 0.0, 0.0), (0.0, 0.0, 1.0)),
    'left': ((-1.0, 0.0, 0.0), (0.0, 0.0, 1.0)),
    'top': ((0.0, 0.0, 1.0), (0.0, 1.0, 0.0)),
    'bottom': ((0.0, 0.0, -1.0), (0.0, -1.0, 0.0)),
}
VIEW_NAMES = tuple(VIEW_DIRECTIONS)

#: Object colours (RGB 0..1), assigned in render order.
PALETTE = (
    (0.46, 0.63, 0.78), (0.80, 0.62, 0.42), (0.52, 0.74, 0.55),
    (0.78, 0.50, 0.55), (0.60, 0.56, 0.80), (0.85, 0.74, 0.40),
    (0.45, 0.70, 0.72), (0.72, 0.58, 0.68), (0.55, 0.65, 0.45),
    (0.70, 0.70, 0.70),
)

MIN_SIZE = 64
MAX_SIZE = 8192
#: Deflection relative to the model diagonal, clamped to a sane band (mm).
MIN_TOLERANCE = 0.01
MAX_TOLERANCE = 1.0
CAMERA_DISTANCE_FACTOR = 2.5
CAMERA_FIT_MARGIN = 1.15

#: Per-object triangle budget.  A native shape far above this is not rendered:
#: meshing it can exhaust memory (SIGBUS/SIGKILL) and a C++ crash cannot be
#: caught in Python.  The deflection is retried coarser first.
MAX_TRIANGLES_PER_OBJECT = 1_500_000
COARSEN_FACTOR = 4.0
MAX_COARSEN_ATTEMPTS = 3

STATEMENT = ('programmatic offscreen shading of FreeCAD BRep tessellation '
             '(bundled VTK); NOT a native FreeCAD GUI viewport screenshot')


class RenderError(RuntimeError):
    """Raised when a render request cannot be satisfied as asked."""


# --------------------------------------------------------------------------
# lazy native access
# --------------------------------------------------------------------------
def _vtk():
    try:
        import vtk                       # type: ignore
        return vtk
    except ImportError as exc:                # pragma: no cover - depends on runtime
        raise RenderError(
            'VTK is not importable in this process; cad3.rendering needs the VTK '
            'that ships with the FreeCAD installation (it must run inside the '
            'FreeCAD worker, not the host interpreter)'
        ) from exc


def _gui_up():
    try:
        import FreeCAD as App             # type: ignore
        return bool(App.GuiUp)
    except Exception:
        return None


# --------------------------------------------------------------------------
# validation / normalisation helpers
# --------------------------------------------------------------------------
def _finite(value: Any, *, limit: float | None = None) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    if limit is not None and abs(number) > limit:
        return None
    return number


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


def _normalize_views(views: Any) -> list[str]:
    if isinstance(views, str):
        views = [item for item in views.replace(';', ',').split(',')]
    if not isinstance(views, (list, tuple)):
        raise RenderError('views must be a view name or a sequence of view names; '
                          'valid views: %s' % ', '.join(VIEW_NAMES))
    result: list[str] = []
    unknown: list[str] = []
    for item in views:
        name = str(item).strip().lower()
        if not name:
            continue
        if name not in VIEW_DIRECTIONS:
            unknown.append(name)
            continue
        if name not in result:
            result.append(name)
    if unknown:
        raise RenderError('unknown view name(s): %s; valid views: %s'
                          % (', '.join(unknown), ', '.join(VIEW_NAMES)))
    if not result:
        raise RenderError('no views requested; valid views: %s' % ', '.join(VIEW_NAMES))
    return result


def _normalize_size(value: Any, label: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise RenderError(f'{label} must be an integer pixel count')
    if number < MIN_SIZE or number > MAX_SIZE:
        raise RenderError(f'{label}={number} is outside the supported range '
                          f'[{MIN_SIZE}, {MAX_SIZE}]')
    return number


def _normalize_objects(objects: Any) -> list[Any]:
    if objects is None:
        raise RenderError('render_objects requires at least one DocumentObject')
    if hasattr(objects, 'Shape') or hasattr(objects, 'Name'):
        entries = [objects]
    elif isinstance(objects, (list, tuple, set)):
        entries = list(objects)
    else:
        raise RenderError('objects must be a DocumentObject or a sequence of them')
    if not entries:
        raise RenderError('render_objects requires at least one DocumentObject')

    doc = None
    for entry in entries:
        candidate = getattr(entry, 'Document', None)
        if candidate is not None:
            doc = candidate
            break

    resolved: list[Any] = []
    seen: set[str] = set()
    unresolved: list[str] = []
    for entry in entries:
        if isinstance(entry, str):
            target = None
            if doc is not None and hasattr(doc, 'getObject'):
                try:
                    target = doc.getObject(entry)
                except Exception:
                    target = None
            if target is None:
                unresolved.append(entry)
                continue
            entry = target
        if not hasattr(entry, 'Shape') and not hasattr(entry, 'Name'):
            unresolved.append(repr(entry))
            continue
        name = _object_name(entry)
        if name in seen:
            continue
        seen.add(name)
        resolved.append(entry)
    if unresolved:
        raise RenderError('unresolved render object(s): %s'
                          % ', '.join(unresolved[:20]))
    if not resolved:
        raise RenderError('no renderable objects given')
    return resolved


def _prepare_output_dir(output_dir: Any) -> Path:
    if output_dir is None:
        raise RenderError('output_dir is required')
    try:
        directory = Path(output_dir)
    except TypeError as exc:
        raise RenderError(f'output_dir is not a path: {output_dir!r}') from exc
    if directory.exists() and not directory.is_dir():
        raise RenderError(f'output_dir exists and is not a directory: {directory}')
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RenderError(f'cannot create output_dir {directory}: {exc}') from exc
    try:
        return directory.resolve()
    except OSError:
        return directory


def _target_path(directory: Path, view: str) -> Path:
    # view names come from the whitelist, so this can never traverse
    path = (directory / f'render_{view}.png')
    try:
        if path.resolve().parent != directory:
            raise RenderError(f'refusing to write outside the output dir: {path}')
    except OSError:
        pass
    return path


# --------------------------------------------------------------------------
# geometry -> VTK
# --------------------------------------------------------------------------
def _shape_of(obj: Any):
    """Native Shape or None.  ``isNull()`` is checked before any type access."""
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


def _bbox_of(shape: Any):
    try:
        box = shape.BoundBox
        values = [box.XMin, box.YMin, box.ZMin, box.XMax, box.YMax, box.ZMax]
    except Exception:
        return None
    numbers = [_finite(value, limit=1e9) for value in values]
    if any(number is None for number in numbers):
        return None
    return numbers


def _face_count(shape: Any) -> int:
    try:
        return len(shape.Faces)
    except Exception:
        return 0


def _shape_type(shape: Any) -> str:
    try:
        return str(shape.ShapeType)
    except Exception:
        return 'Unknown'


def _tolerance_for(diagonal: float) -> float:
    if diagonal is None or not math.isfinite(diagonal) or diagonal <= 0:
        return 0.1
    return max(MIN_TOLERANCE, min(MAX_TOLERANCE, diagonal / 1000.0))


def _shape_solids(shape: Any) -> list:
    try:
        return list(shape.Solids)
    except Exception:
        return []


def _tessellate_shape(shape: Any, tolerance: float):
    """Tessellate a shape SOLID BY SOLID (falling back to the whole shape).

    Measured on FreeCAD 1.1.3: ``Compound`` results (``Part::MultiFuse``,
    ``Part::Cut``, ``App::Part``/``App::DocumentObjectGroup`` aggregates) can hold
    many solids.  Meshing every solid separately keeps each native call on one
    simple, valid shape, lets a single bad solid be skipped instead of failing
    the whole object, and still merges into one mesh for rendering.

    Returns ``(points, cells, parts, failures)`` with plain python data only.
    """
    solids = _shape_solids(shape)
    targets = solids if solids else [shape]
    points: list = []
    cells: list = []
    parts: list = []
    failures: list = []
    for index, target in enumerate(targets):
        label = index if solids else None
        try:
            vertices, triangles = target.tessellate(tolerance)
        except Exception as exc:
            failures.append({'solid': label,
                             'reason': '%s: %s' % (type(exc).__name__, exc)})
            continue
        base = len(points) // 3
        local_points = []
        for vertex in vertices:
            try:
                local_points.extend((_finite(vertex.x), _finite(vertex.y),
                                     _finite(vertex.z)))
            except Exception:
                local_points = []
                break
        if not local_points or any(component is None for component in local_points):
            failures.append({'solid': label,
                             'reason': 'non-finite vertex coordinates'})
            continue
        local_cells = []
        for triangle in triangles:
            try:
                indices = [int(item) for item in triangle]
            except Exception:
                continue
            if len(indices) == 3 and all(0 <= item < len(vertices)
                                         for item in indices):
                local_cells.extend((base + indices[0], base + indices[1],
                                    base + indices[2]))
        if not local_cells:
            failures.append({'solid': label,
                             'reason': 'no triangles (no shaded surface)'})
            continue
        points.extend(local_points)
        cells.extend(local_cells)
        parts.append({'solid': label, 'vertices': len(vertices),
                      'triangles': len(local_cells) // 3})
    return points, cells, parts, failures


def _collect_meshes(objects: list[Any]):
    """Tessellate every object; return (meshes, skipped).

    A mesh entry carries plain python geometry only (no native objects)::

        {'object', 'name', 'typeId', 'shape_type', 'vertices', 'triangles',
         'points', 'cells', 'bbox', 'size', 'tolerance', 'solids',
         'parts', 'failed_solids'}

    Guards applied per object: null/missing shape, non-finite (infinite datum)
    bounds, no faces, python-level tessellation errors, non-finite vertices, and
    a triangle budget (coarser deflection retries, then an actionable skip) so a
    huge native shape cannot exhaust memory.
    """
    meshes: list[dict] = []
    skipped: list[dict] = []
    for obj in objects:
        name = _object_name(obj)
        shape = _shape_of(obj)
        if shape is None:
            skipped.append({'object': name,
                            'reason': 'no shape (missing or null native Shape)'})
            continue
        bbox = _bbox_of(shape)
        if bbox is None:
            skipped.append({'object': name, 'reason': (
                'non-finite or absurd bounds (infinite datum/helper shape); '
                'cannot be rendered')})
            continue
        size = (bbox[3] - bbox[0], bbox[4] - bbox[1], bbox[5] - bbox[2])
        diagonal = math.sqrt(sum(component * component for component in size))
        faces = _face_count(shape)
        if faces <= 0:
            skipped.append({'object': name, 'reason': (
                'shape has no faces to shade (ShapeType=%s)' % _shape_type(shape))})
            continue
        solids = _shape_solids(shape)
        tolerance = _tolerance_for(diagonal)
        points: list = []
        cells: list = []
        parts: list = []
        failures: list = []
        for attempt in range(MAX_COARSEN_ATTEMPTS):
            points, cells, parts, failures = _tessellate_shape(shape, tolerance)
            if len(cells) // 3 <= MAX_TRIANGLES_PER_OBJECT:
                break
            tolerance = min(tolerance * COARSEN_FACTOR, MAX_TOLERANCE * 10)
        triangles = len(cells) // 3
        if triangles <= 0:
            if failures:
                reason = 'native tessellation failed (%s)' % '; '.join(
                    'solid %s: %s' % (item['solid'], item['reason'])
                    for item in failures[:3])
            else:
                reason = ('tessellation produced no triangles (no shaded surface '
                          'in %s)' % _shape_type(shape))
            skipped.append({'object': name, 'reason': reason})
            continue
        if triangles > MAX_TRIANGLES_PER_OBJECT:
            skipped.append({'object': name, 'reason': (
                'geometry too dense: %d triangles exceeds the per-object budget of '
                '%d; render a sub-part (use cad_outputs) or simplify the model'
                % (triangles, MAX_TRIANGLES_PER_OBJECT))})
            continue
        meshes.append({
            'object': obj, 'name': name, 'typeId': _object_type(obj),
            'shape_type': _shape_type(shape), 'vertices': len(points) // 3,
            'triangles': triangles, 'points': points, 'cells': cells,
            'bbox': bbox,
            'size': [round(component, 6) for component in size],
            'tolerance': round(tolerance, 6),
            'solids': len(solids) if solids else 1,
            'parts': parts,
            'failed_solids': failures,
        })
    return meshes, skipped


def _build_points(vtk: Any, flat: list):
    """vtkPoints from a flat [x,y,z,...] list; bulk numpy path when available."""
    try:
        import numpy
        from vtk.util import numpy_support      # type: ignore
        array = numpy.asarray(flat, dtype=numpy.float64).reshape(-1, 3)
        vtk_array = numpy_support.numpy_to_vtk(array, deep=True)
        points = vtk.vtkPoints()
        points.SetData(vtk_array)
        if points.GetNumberOfPoints() == len(flat) // 3:
            return points
    except Exception:
        pass
    points = vtk.vtkPoints()
    count = len(flat) // 3
    points.SetNumberOfPoints(count)
    for index in range(count):
        points.SetPoint(index, flat[index * 3], flat[index * 3 + 1],
                        flat[index * 3 + 2])
    return points


def _apply_triangles(vtk: Any, polydata: Any, mesh: dict) -> bool:
    """Attach triangles to a polydata and VERIFY the resulting cell count.

    Measured on FreeCAD 1.1.3 / VTK 9.3.1: handing a plain concatenated index
    list to ``vtkCellArray.SetCells(ncells, array)`` is wrong -- that API wants
    the legacy size-prefixed format.  It silently produced 14 cells for a
    24-triangle mesh and then SEGFAULTED inside ``vtkPolyDataNormals``, killing
    the whole worker process (no completion record).  Every strategy below is
    therefore verified against the resulting cell count before the mesh is used.
    """
    expected = mesh['triangles']
    flat = mesh['cells']

    def _modern():
        import numpy
        from vtk.util import numpy_support      # type: ignore
        offsets = list(range(0, len(flat) + 3, 3))
        cells = vtk.vtkCellArray()
        cells.SetData(
            numpy_support.numpy_to_vtkIdTypeArray(
                numpy.asarray(offsets, dtype=numpy.int64), deep=True),
            numpy_support.numpy_to_vtkIdTypeArray(
                numpy.asarray(flat, dtype=numpy.int64), deep=True))
        return cells

    def _legacy():
        import numpy
        from vtk.util import numpy_support      # type: ignore
        prefixed = []
        for index in range(0, len(flat), 3):
            prefixed.extend((3, flat[index], flat[index + 1], flat[index + 2]))
        array = numpy_support.numpy_to_vtkIdTypeArray(
            numpy.asarray(prefixed, dtype=numpy.int64), deep=True)
        cells = vtk.vtkCellArray()
        cells.SetCells(expected, array)
        return cells

    def _loop():
        cells = vtk.vtkCellArray()
        for index in range(0, len(flat), 3):
            cells.InsertNextCell(3)
            cells.InsertCellPoint(flat[index])
            cells.InsertCellPoint(flat[index + 1])
            cells.InsertCellPoint(flat[index + 2])
        return cells

    for builder in (_modern, _legacy, _loop):
        try:
            polydata.SetPolys(builder())
            if polydata.GetNumberOfCells() == expected:
                return True
        except Exception:
            continue
    return False


def _build_actor(vtk: Any, mesh: dict, color):
    polydata = vtk.vtkPolyData()
    polydata.SetPoints(_build_points(vtk, mesh['points']))
    if not _apply_triangles(vtk, polydata, mesh):
        raise RenderError(
            'safe triangle construction failed for %s (%d triangles): refusing to '
            'hand an unverified mesh to VTK' % (mesh['name'], mesh['triangles']))
    normals = vtk.vtkPolyDataNormals()
    normals.SetInputData(polydata)
    normals.ComputePointNormalsOn()
    normals.ConsistencyOn()
    normals.SplittingOn()
    normals.SetFeatureAngle(30.0)
    normals.Update()
    mapper = vtk.vtkPolyDataMapper()
    mapper.SetInputConnection(normals.GetOutputPort())
    mapper.ScalarVisibilityOff()
    actor = vtk.vtkActor()
    actor.SetMapper(mapper)
    actor_property = actor.GetProperty()
    actor_property.SetColor(color[0], color[1], color[2])
    actor_property.SetAmbient(0.25)
    actor_property.SetDiffuse(0.75)
    actor_property.SetSpecular(0.2)
    actor_property.SetSpecularPower(20.0)
    actor_property.SetInterpolationToGouraud()
    return actor


def _combined_bbox(meshes: list[dict]):
    minima = [min(mesh['bbox'][index] for mesh in meshes) for index in range(3)]
    maxima = [max(mesh['bbox'][index + 3] for mesh in meshes) for index in range(3)]
    return minima, maxima


def _write_png(vtk: Any, window: Any, path: Path) -> int:
    capture = vtk.vtkWindowToImageFilter()
    capture.SetInput(window)
    capture.SetInputBufferTypeToRGB()
    capture.ReadFrontBufferOff()
    capture.Update()
    writer = vtk.vtkPNGWriter()
    temporary = path.with_name(path.name + '.part')
    writer.SetFileName(str(temporary))
    writer.SetInputConnection(capture.GetOutputPort())
    writer.Write()
    if not temporary.exists() or temporary.stat().st_size <= 0:
        raise RenderError(f'PNG writer produced no data for {path}')
    os.replace(temporary, path)
    return path.stat().st_size


def render_objects(objects: Any, output_dir: Any,
                   views: Sequence[str] = DEFAULT_VIEWS,
                   width: int = 960, height: int = 720) -> list:
    """Render objects to shaded PNGs in ``output_dir``; one dict per view.

    Raises :class:`RenderError` with an actionable message when VTK is missing
    or when nothing renderable was supplied.  Per-view metadata carries
    ``source`` (never a GUI screenshot), ``camera``, ``dimensions`` and
    ``objects``; anything that could not be rendered (no shape, infinite datum
    bounds, wire-only geometry, tessellation failure, individual failed solids,
    over-budget triangle count) is listed under ``skipped``/``warnings`` instead
    of disappearing silently.
    """
    view_names = _normalize_views(views)
    image_width = _normalize_size(width, 'width')
    image_height = _normalize_size(height, 'height')
    selected = _normalize_objects(objects)
    directory = _prepare_output_dir(output_dir)

    vtk = _vtk()
    meshes, skipped = _collect_meshes(selected)

    # Build the VTK side FIRST, with per-object failure containment: a mesh that
    # cannot be safely handed to VTK must become a reported skip, never a crash.
    actors = []
    for index, mesh in enumerate(meshes):
        try:
            actor = _build_actor(vtk, mesh, PALETTE[index % len(PALETTE)])
        except Exception as exc:
            skipped.append({'object': mesh['name'], 'reason': (
                'VTK mesh construction failed: %s: %s'
                % (type(exc).__name__, exc))})
            continue
        actors.append((mesh, actor, index))
    if not actors:
        reasons = '; '.join(
            '%s: %s' % (item['object'], item['reason']) for item in skipped[:10])
        raise RenderError(
            'no renderable geometry among %d object(s)%s'
            % (len(selected), f' ({reasons})' if reasons else ''))
    render_meshes = [mesh for mesh, _actor, _index in actors]

    minima, maxima = _combined_bbox(render_meshes)
    centre = [(minima[index] + maxima[index]) / 2.0 for index in range(3)]
    sizes = [maxima[index] - minima[index] for index in range(3)]
    diagonal = math.sqrt(sum(component * component for component in sizes))
    if not math.isfinite(diagonal) or diagonal <= 0:
        raise RenderError('rendered geometry has a degenerate bounding box '
                          '(diagonal=%r)' % diagonal)
    camera_distance = max(diagonal * CAMERA_DISTANCE_FACTOR, 1.0)

    try:
        vtk_version = str(vtk.vtkVersion.GetVTKVersion())
    except Exception:
        vtk_version = None
    gui_up = _gui_up()

    source = {
        'geometry': ('FreeCAD Part.Shape.tessellate solid by solid '
                     '(native BRep meshing)'),
        'renderer': 'VTK%s offscreen (bundled with FreeCAD)'
                    % (f' {vtk_version}' if vtk_version else ''),
        'vtk_version': vtk_version,
        'gui_screenshot': False,
        'native_viewport': False,
        'headless': True,
        'gui_up': gui_up,
        'statement': STATEMENT,
        'tessellation_note': ('mesh deflection is derived from the model diagonal '
                              '(diagonal/1000, clamped to [%s, %s] mm)'
                              % (MIN_TOLERANCE, MAX_TOLERANCE)),
        'safety': {
            'per_solid_meshing': True,
            'cell_count_verified': True,
            'max_triangles_per_object': MAX_TRIANGLES_PER_OBJECT,
            'note': ('each object is tessellated solid by solid, the resulting '
                     'triangle count is verified before VTK sees the mesh, and '
                     'objects over the triangle budget are skipped with a reason '
                     'instead of being meshed into a possible memory crash. '
                     'A native C++ crash cannot be caught in Python: run one '
                     'render view per worker subprocess if a single view must '
                     'never take down a whole run.'),
        },
    }
    object_metadata = [
        {'name': mesh['name'], 'type': mesh['typeId'],
         'shape_type': mesh['shape_type'], 'vertices': mesh['vertices'],
         'triangles': mesh['triangles'],
         'solids': mesh.get('solids'),
         'tessellation_tolerance': mesh['tolerance'],
         'bbox_mm': mesh['size'],
         'color_rgb': list(PALETTE[index % len(PALETTE)])}
        for mesh, _actor, index in actors
    ]
    dimensions = {
        'unit': 'mm',
        'bbox_min': [round(value, 6) for value in minima],
        'bbox_max': [round(value, 6) for value in maxima],
        'bbox_size': [round(value, 6) for value in sizes],
        'diagonal_mm': round(diagonal, 6),
        'objects_rendered': len(render_meshes),
        'vertices': sum(mesh['vertices'] for mesh in render_meshes),
        'triangles': sum(mesh['triangles'] for mesh in render_meshes),
        'max_triangles_per_object': MAX_TRIANGLES_PER_OBJECT,
        'solids': sum(mesh.get('solids') or 0 for mesh in render_meshes),
    }
    warnings: list[str] = []
    if skipped:
        warnings.append('%d object(s) were not rendered: %s'
                        % (len(skipped),
                           ', '.join(item['object'] for item in skipped[:10])))
    partial = [(mesh['name'], mesh['failed_solids']) for mesh in render_meshes
               if mesh.get('failed_solids')]
    for name, failures in partial:
        warnings.append(
            '%s: %d solid(s) could not be tessellated and are missing from the '
            'image (%s)' % (name, len(failures),
                            '; '.join('solid %s: %s' % (item['solid'], item['reason'])
                                      for item in failures[:3])))
    if gui_up:
        warnings.append('GuiUp is true in this process; rendering is still done '
                        'offscreen and is not a GUI screenshot')

    window = vtk.vtkRenderWindow()
    window.SetOffScreenRendering(1)
    window.SetSize(image_width, image_height)
    try:
        window.SetMultiSamples(4)
    except Exception:
        pass
    renderer = vtk.vtkRenderer()
    renderer.SetBackground(1.0, 1.0, 1.0)
    for _mesh, actor, _index in actors:
        renderer.AddActor(actor)
    try:
        renderer.AutomaticLightCreationOn()
    except Exception:
        pass
    window.AddRenderer(renderer)

    results: list = []
    try:
        for view in view_names:
            direction, up = VIEW_DIRECTIONS[view]
            length = math.sqrt(sum(component * component for component in direction))
            unit_direction = [component / length for component in direction]
            camera = renderer.GetActiveCamera()
            camera.SetFocalPoint(*centre)
            camera.SetPosition(*[centre[index] + unit_direction[index] * camera_distance
                                 for index in range(3)])
            camera.SetViewUp(*up)
            camera.ParallelProjectionOn()
            renderer.ResetCamera()
            try:
                camera.SetParallelScale(camera.GetParallelScale() * CAMERA_FIT_MARGIN)
            except Exception:
                pass
            renderer.ResetCameraClippingRange()
            window.Render()
            path = _target_path(directory, view)
            size_bytes = _write_png(vtk, window, path)
            results.append({
                'view': view,
                'path': str(path),
                'ok': True,
                'width': image_width,
                'height': image_height,
                'size_bytes': size_bytes,
                'source': dict(source),
                'camera': {
                    'position': [round(value, 6) for value in camera.GetPosition()],
                    'focal_point': [round(value, 6)
                                    for value in camera.GetFocalPoint()],
                    'view_up': [round(value, 6) for value in camera.GetViewUp()],
                    'direction': [round(value, 6) for value in unit_direction],
                    'parallel_projection': True,
                    'parallel_scale': round(float(camera.GetParallelScale()), 6),
                    'clipping_range': [round(float(value), 6)
                                       for value in camera.GetClippingRange()],
                },
                'dimensions': dict(dimensions),
                'objects': [dict(item) for item in object_metadata],
                'skipped': [dict(item) for item in skipped],
                'warnings': list(warnings),
            })
    finally:
        try:
            window.Finalize()
        except Exception:
            pass
    return results
