"""示例：在注入的 doc 上建 80x50x6、两个 Ø8 通孔的板（纯原生 FreeCAD 树）。

由 cad-v3 运行时执行，命名空间注入 App / doc / PROJECT_DIR / RUN_DIR。
树结构：Spreadsheet(Params) → Body → BaseSketch → Pad → HoleSketch → Pocket
所有尺寸具名，并全部由 Params 表达式驱动；只修改传入的 doc，不新建文档。

用法：复制进工程的 src/main.py（或作为它的起点），在空工程上运行：
    cad3 run src/main.py
需要空文档：已有 Params/Body 时用 examples/edit_plate.py 改现有模型。
"""
import json
import math

import Part
import Sketcher
import PartDesign  # 导入以加载原生 PartDesign 对象类型

# === 具名尺寸（本文件唯一真值源，单位 mm）===
PLATE_LENGTH_MM = 80.0
PLATE_WIDTH_MM = 50.0
PLATE_THICK_MM = 6.0
HOLE_DIA_MM = 8.0
HOLE_SPACING_MM = 30.0
VOLUME_TOL_MM3 = 1e-3
LENGTH_TOL_MM = 1e-6

PARAMS = (
    ("PlateLength", PLATE_LENGTH_MM),
    ("PlateWidth", PLATE_WIDTH_MM),
    ("PlateThick", PLATE_THICK_MM),
    ("HoleDia", HOLE_DIA_MM),
    ("HoleSpacing", HOLE_SPACING_MM),
)
OUTPUT_OBJECT = "Body"
XY_PLANE = "XY_Plane"


def analytic_volume(length_mm, width_mm, thick_mm, hole_dia_mm):
    """独立解析值：矩形板减两个贯通圆柱孔。"""
    return length_mm * width_mm * thick_mm - 2.0 * math.pi * (hole_dia_mm / 2.0) ** 2 * thick_mm


def require_empty_document():
    reserved_names = ("Params", "Body")
    occupied = [name for name in reserved_names if doc.getObject(name) is not None]
    if occupied:
        raise RuntimeError(
            "build_plate 需要空文档，已存在 %s；请在空工程运行，或用 examples/edit_plate.py 修改现有模型。"
            % "、".join(occupied)
        )


def find_origin_plane(body, plane_name):
    for feature in body.Origin.OriginFeatures:
        if feature.Name == plane_name or feature.Label == plane_name:
            return feature
    raise RuntimeError("Body.Origin 中找不到基准面 %s" % plane_name)


def attach_flat(sketch, body, plane_name):
    sketch.AttachmentSupport = (find_origin_plane(body, plane_name), [""])
    sketch.MapMode = "FlatFace"


def add_params():
    """建参数表：每个别名一行，值是具名尺寸。"""
    sheet = doc.addObject("Spreadsheet::Sheet", "Params")
    sheet.Label = "Params"
    for row, (alias, value_mm) in enumerate(PARAMS, start=1):
        cell = "B%d" % row
        sheet.set(cell, "%s mm" % value_mm)
        sheet.setAlias(cell, alias)
    return sheet


def add_base_sketch(body):
    """矩形基体草图：几何 + 约束全部具名，尺寸由 Params 表达式驱动。"""
    sketch = body.newObject("Sketcher::SketchObject", "BaseSketch")
    sketch.Label = "BaseSketch"
    attach_flat(sketch, body, XY_PLANE)

    half_l, half_w = PLATE_LENGTH_MM / 2.0, PLATE_WIDTH_MM / 2.0
    corners = [
        App.Vector(-half_l, -half_w, 0), App.Vector(half_l, -half_w, 0),
        App.Vector(half_l, half_w, 0), App.Vector(-half_l, half_w, 0),
    ]
    for i in range(4):
        sketch.addGeometry(Part.LineSegment(corners[i], corners[(i + 1) % 4]), False)

    for i in range(4):
        sketch.addConstraint(Sketcher.Constraint("Coincident", i, 2, (i + 1) % 4, 1))
    for i in (0, 2):
        sketch.addConstraint(Sketcher.Constraint("Horizontal", i))
    for i in (1, 3):
        sketch.addConstraint(Sketcher.Constraint("Vertical", i))

    named = {
        "PlateLength": sketch.addConstraint(Sketcher.Constraint("Distance", 0, 1, 0, 2, PLATE_LENGTH_MM)),
        "PlateWidth": sketch.addConstraint(Sketcher.Constraint("Distance", 1, 1, 1, 2, PLATE_WIDTH_MM)),
        "LeftX": sketch.addConstraint(Sketcher.Constraint("DistanceX", 0, 1, -half_l)),
        "BottomY": sketch.addConstraint(Sketcher.Constraint("DistanceY", 0, 1, -half_w)),
    }
    for alias, index in named.items():
        sketch.renameConstraint(index, alias)
    for alias in ("PlateLength", "PlateWidth"):
        sketch.setExpression("Constraints.%s" % alias, "Params.%s" % alias)
    sketch.setExpression("Constraints.LeftX", "-(Params.PlateLength / 2)")
    sketch.setExpression("Constraints.BottomY", "-(Params.PlateWidth / 2)")
    return sketch


def add_pad(body, base_sketch):
    pad = body.newObject("PartDesign::Pad", "Plate")
    pad.Label = "Plate"
    pad.Profile = base_sketch
    pad.SideType = "Symmetric"
    pad.setExpression("Length", "Params.PlateThick")
    return pad


def add_hole_sketch(body):
    """两个 Ø8 通孔的草图：圆心位置与直径也全部由 Params 驱动。"""
    sketch = body.newObject("Sketcher::SketchObject", "HoleSketch")
    sketch.Label = "HoleSketch"
    attach_flat(sketch, body, XY_PLANE)

    radius = HOLE_DIA_MM / 2.0
    half_span = HOLE_SPACING_MM / 2.0
    for center_x in (-half_span, half_span):
        sketch.addGeometry(Part.Circle(App.Vector(center_x, 0, 0), App.Vector(0, 0, 1), radius), False)

    named = {
        "HoleDiaA": sketch.addConstraint(Sketcher.Constraint("Diameter", 0, HOLE_DIA_MM)),
        "HoleDiaB": sketch.addConstraint(Sketcher.Constraint("Diameter", 1, HOLE_DIA_MM)),
        "HoleX0": sketch.addConstraint(Sketcher.Constraint("DistanceX", 0, 3, -half_span)),
        "HoleX1": sketch.addConstraint(Sketcher.Constraint("DistanceX", 1, 3, half_span)),
        "HoleY0": sketch.addConstraint(Sketcher.Constraint("DistanceY", 0, 3, 0.0)),
        "HoleY1": sketch.addConstraint(Sketcher.Constraint("DistanceY", 1, 3, 0.0)),
    }
    for alias, index in named.items():
        sketch.renameConstraint(index, alias)
    for alias in ("HoleDiaA", "HoleDiaB"):
        sketch.setExpression("Constraints.%s" % alias, "Params.HoleDia")
    sketch.setExpression("Constraints.HoleX0", "-(Params.HoleSpacing / 2)")
    sketch.setExpression("Constraints.HoleX1", "Params.HoleSpacing / 2")
    return sketch


def add_pocket(body, hole_sketch):
    pocket = body.newObject("PartDesign::Pocket", "Holes")
    pocket.Label = "Holes"
    pocket.Profile = hole_sketch
    pocket.Type = "ThroughAll"
    pocket.SideType = "Symmetric"
    return pocket


def inspect_and_assert(body):
    """本任务自己的断言：解析体积、单一实体、边界框、两个圆柱面候选。"""
    doc.recompute()
    shape = body.Shape
    expected = analytic_volume(PLATE_LENGTH_MM, PLATE_WIDTH_MM, PLATE_THICK_MM, HOLE_DIA_MM)
    bbox = shape.BoundBox
    cylinders = [f for f in shape.Faces if f.Surface.TypeId == "Part::GeomCylinder"]

    checks = {
        "volume_matches_analytic": abs(shape.Volume - expected) <= VOLUME_TOL_MM3,
        "single_solid": len(shape.Solids) == 1,
        "brep_valid": shape.isValid(),
        "bbox_is_80x50x6": all(abs(got - want) < LENGTH_TOL_MM for got, want in
                               ((bbox.XLength, PLATE_LENGTH_MM), (bbox.YLength, PLATE_WIDTH_MM),
                                (bbox.ZLength, PLATE_THICK_MM))),
        # 本件没有凸台/沉头/圆角，所以圆柱面恰好 2 个；这不是通用孔数判据。
        "two_cylindrical_faces_with_hole_radius": len(cylinders) == 2 and all(
            abs(f.Surface.Radius - HOLE_DIA_MM / 2.0) <= LENGTH_TOL_MM for f in cylinders),
    }
    report = {
        "object": body.Name,
        "volume_mm3": round(shape.Volume, 6),
        "analytic_volume_mm3": round(expected, 6),
        "solids": len(shape.Solids),
        "faces": len(shape.Faces),
        "bbox_mm": [round(bbox.XLength, 6), round(bbox.YLength, 6), round(bbox.ZLength, 6)],
        "cylindrical_face_radii_mm": sorted(round(f.Surface.Radius, 6) for f in cylinders),
        "checks": checks,
    }
    (RUN_DIR / "build_plate_checks.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        raise RuntimeError("build_plate 断言失败：%s" % "、".join(failed))
    return body


require_empty_document()
param_sheet = add_params()
model_body = doc.addObject("PartDesign::Body", "Body")
model_body.Label = "PlateBody"
base_sketch = add_base_sketch(model_body)
pad = add_pad(model_body, base_sketch)
hole_sketch = add_hole_sketch(model_body)
pocket = add_pocket(model_body, hole_sketch)
inspect_and_assert(model_body)

print("参数表原始式：%s" % {alias: param_sheet.getContents("B%d" % (i + 1)) for i, (alias, _) in enumerate(PARAMS)})
print("草图诊断：BaseSketch solve=%s DoF=%s FullyConstrained=%s；HoleSketch solve=%s DoF=%s FullyConstrained=%s"
      % (base_sketch.solve(), base_sketch.DoF, base_sketch.FullyConstrained,
         hole_sketch.solve(), hole_sketch.DoF, hole_sketch.FullyConstrained))

# 运行时据此选择导出/渲染对象（Body 的最终 Tip 结果）
cad_outputs = [OUTPUT_OBJECT]
