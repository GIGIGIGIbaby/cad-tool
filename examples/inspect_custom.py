"""示例：只用原生 API 观察现有 doc —— 列圆柱候选 + 测距离（不改文档、不建几何）。

由 cad-v3 运行时执行，命名空间注入 App / doc / PROJECT_DIR / RUN_DIR。

- 圆柱候选：只列半径/轴线/面积与判据，**不把圆柱面数当孔数**（凸台、沉头孔、圆角都会产生圆柱面）。
- 距离：文档内有多个实体时两两测；只有一个时用一个"内存里的虚拟参考块"演示
  `distToShape` + `common.Volume` 的三态判据（虚拟块不进文档）。

用法：
    cad3 run examples/inspect_custom.py
"""
import json

import Part

# === 具名参数（单位 mm）===
TARGET_OBJECT = "Body"
REFERENCE_OBJECT = None      # 想固定测量对象就填它的 Name；None = 自动找第二个实体
CLEARANCE_MM = 3.0           # 虚拟参考块与目标包围盒之间的距离
TOUCH_TOL_MM = 1e-7          # 低于形状容差分辨率的距离会读成 0
RELATIVE_TOL = 1e-6


def solid_objects():
    """有体积的**独立输出**对象；跳过草图、基准面/轴，也跳过 Body 内部的中间特征。

    实测坑：PartDesign::Pad 这类中间特征自己也有 Shape 和体积，它们只是某个 Body 的 Group 成员，
    不能当第二个零件参与"零件间距离"。这里按"是否属于某个 Body"过滤。
    """
    found = []
    for obj in doc.Objects:
        shape = getattr(obj, "Shape", None)
        if shape is None or shape.isNull() or len(shape.Solids) == 0:
            continue
        if any(parent.TypeId == "PartDesign::Body" for parent in obj.InList):
            continue
        found.append(obj)
    return found


def cylinder_candidates(shape):
    """列出圆柱面候选；每行给出判定用的属性，而不是一个"孔数"。"""
    rows = []
    for index, face in enumerate(shape.Faces, start=1):
        surface = face.Surface
        if surface.TypeId != "Part::GeomCylinder":
            continue
        axis = surface.Axis
        center = surface.Center
        rows.append({
            "face": "Face%d" % index,
            "radius_mm": round(surface.Radius, 6),
            "diameter_mm": round(2.0 * surface.Radius, 6),
            "axis_direction": [round(v, 6) for v in (axis.x, axis.y, axis.z)],
            "axis_point_mm": [round(v, 6) for v in (center.x, center.y, center.z)],
            "area_mm2": round(face.Area, 6),
        })
    return rows


def classify_pair(shape_a, shape_b):
    """间隙 / 接触 / 干涉 / 内含：距离与重叠体积必须一起读。"""
    dist, vectors, infos = shape_a.distToShape(shape_b)
    overlap = shape_a.common(shape_b).Volume
    volume_a, volume_b = shape_a.Volume, shape_b.Volume
    if dist > TOUCH_TOL_MM:
        state = "GAP"
    elif overlap <= 0.0:
        state = "CONTACT"          # common 此时是空集，无法区分面/边/点接触
    elif abs(overlap - min(volume_a, volume_b)) <= RELATIVE_TOL * max(1.0, min(volume_a, volume_b)):
        state = "CONTAINMENT"
    else:
        state = "INTERFERENCE"
    return {
        "dist_mm": round(dist, 9),
        "overlap_mm3": round(overlap, 6),
        "state": state,
        "closest_point_pairs": len(vectors),
        "closest_element_records": len(infos),   # infos 里的索引是 0-based，+1 才是 Face/Edge 名
    }


def virtual_reference_box(target_shape):
    """按目标包围盒定尺寸的虚拟块，放在 X 正方向 CLEARANCE_MM 处；只存在于内存。"""
    bbox = target_shape.BoundBox
    size_y = bbox.YLength or 1.0
    size_z = bbox.ZLength or 1.0
    corner = App.Vector(bbox.XMax + CLEARANCE_MM, bbox.YMin, bbox.ZMin)
    return Part.makeBox(CLEARANCE_MM, size_y, size_z, corner)


target = doc.getObject(TARGET_OBJECT) if TARGET_OBJECT else None
objects_with_solids = solid_objects()
if target is None or getattr(target, "Shape", None) is None or target.Shape.isNull():
    raise RuntimeError("找不到带几何的对象 %s；先运行 examples/build_plate.py。" % TARGET_OBJECT)

target_shape = target.Shape
report = {
    "document": doc.Name,
    "objects_with_solids": [obj.Name for obj in objects_with_solids],
    "target": {
        "name": target.Name,
        "type_id": target.TypeId,
        "shape_type": target_shape.ShapeType,
        "volume_mm3": round(target_shape.Volume, 6),
        "area_mm2": round(target_shape.Area, 6),
        "solids": len(target_shape.Solids),
        "shells": len(target_shape.Shells),
        "bbox_mm": [round(target_shape.BoundBox.XLength, 6), round(target_shape.BoundBox.YLength, 6),
                    round(target_shape.BoundBox.ZLength, 6)],
        "is_valid": target_shape.isValid(),
    },
    "cylinder_candidates": cylinder_candidates(target_shape),
}

# 距离：优先用文档里真实的第二个实体，否则用虚拟参考块。
reference = doc.getObject(REFERENCE_OBJECT) if REFERENCE_OBJECT else next(
    (obj for obj in objects_with_solids if obj.Name != target.Name), None)
if reference is not None:
    report["distance"] = dict(classify_pair(target_shape, reference.Shape),
                              reference=reference.Name, reference_kind="document_object")
else:
    report["distance"] = dict(classify_pair(target_shape, virtual_reference_box(target_shape)),
                              reference="virtual_box(+%g mm in X)" % CLEARANCE_MM,
                              reference_kind="transient_shape_not_in_document")

report["notes"] = [
    "cylinder_candidates 是候选：圆柱面数量不等于孔数（凸台/沉头/圆角都会产生圆柱面），半径也无法区分圆角与孔。",
    "dist == 0 只表示距离低于本形状容差分辨率，不表示精确接触。",
    "objects_with_solids 只含独立输出；Body 内部的 Pad/Pocket 等中间特征被排除，避免把同一零件测成两个。",
    "本脚本只读：不 addObject、不 recompute 参数源、不写回文档。",
]

(RUN_DIR / "inspect_custom.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(report, ensure_ascii=False, indent=2))

# 若工程里真的实现了孔径/孔位要求，就在这里按任务写断言；不要用"圆柱面数 == 孔数"这种通用门控。
