"""示例：在现有 doc 上把 Params.PlateLength 从 80 改成 100，重算后做独立解析断言。

由 cad-v3 运行时执行，命名空间注入 App / doc / PROJECT_DIR / RUN_DIR。
前提：doc 里已有 examples/build_plate.py 建出的树（Params 参数表 + Body 实体）。
本脚本只改参数源，不重建几何；断言值由声明尺寸独立算出，不从几何反推。

用法：
    cad3 run examples/edit_plate.py
"""
import json
import math

# === 具名目标值（单位 mm）===
NEW_PLATE_LENGTH_MM = 100.0
EXPECTED_OLD_PLATE_LENGTH_MM = 80.0
VOLUME_TOL_MM3 = 1e-3
TARGET_OBJECT = "Body"
ALIAS_CELL_SCAN_ROWS = 20


def find_cell(sheet, alias):
    """按别名找单元格：Spreadsheet 只提供 cell→alias，反向要自己扫。"""
    for row in range(1, ALIAS_CELL_SCAN_ROWS + 1):
        cell = "B%d" % row
        if sheet.getAlias(cell) == alias:
            return cell
    raise RuntimeError("参数表里找不到别名 %s" % alias)


def read_params(sheet):
    """读声明值（求值后的 Quantity），而不是从几何反推。"""
    return {alias: sheet.get(find_cell(sheet, alias)).Value
            for alias in ("PlateLength", "PlateWidth", "PlateThick", "HoleDia", "HoleSpacing")}


def named_constraint_value(sketch, name):
    """没有 getConstraintByName；具名约束只能遍历 Constraints 找 .Name。"""
    for constraint in sketch.Constraints:
        if constraint.Name == name:
            return constraint.Value
    raise RuntimeError("草图里找不到具名约束 %s" % name)


def analytic_volume(params):
    length, width, thick, hole_dia = (params["PlateLength"], params["PlateWidth"],
                                      params["PlateThick"], params["HoleDia"])
    return length * width * thick - 2.0 * math.pi * (hole_dia / 2.0) ** 2 * thick


sheet = doc.getObject("Params")
body = doc.getObject(TARGET_OBJECT)
if sheet is None or body is None:
    raise RuntimeError("找不到 Params / %s；请先运行 examples/build_plate.py 建出模型。" % TARGET_OBJECT)

before_params = read_params(sheet)
before_volume = body.Shape.Volume

# === 只改参数源，再重算 ===
length_cell = find_cell(sheet, "PlateLength")
sheet.set(length_cell, "%s mm" % NEW_PLATE_LENGTH_MM)
doc.recompute()

after_params = read_params(sheet)
after_shape = body.Shape
base_sketch = doc.getObject("BaseSketch")
expected_volume = analytic_volume(after_params)

checks = {
    # 参数是否真的生效（catch 静默 no-op）
    "param_source_updated": abs(after_params["PlateLength"] - NEW_PLATE_LENGTH_MM) <= VOLUME_TOL_MM3,
    "param_changed_from_old": abs(before_params["PlateLength"] - EXPECTED_OLD_PLATE_LENGTH_MM) <= VOLUME_TOL_MM3,
    # 参数是否传到约束（要用约束的具名值，不能用几何反推）
    "constraint_follows_param": abs(
        named_constraint_value(base_sketch, "PlateLength") - NEW_PLATE_LENGTH_MM) <= VOLUME_TOL_MM3,
    # 几何是否与独立解析值一致
    "volume_matches_analytic": abs(after_shape.Volume - expected_volume) <= VOLUME_TOL_MM3,
    "bbox_x_follows_param": abs(after_shape.BoundBox.XLength - NEW_PLATE_LENGTH_MM) <= VOLUME_TOL_MM3,
    "volume_increased": after_shape.Volume > before_volume,
}

report = {
    "object": TARGET_OBJECT,
    "plate_length_mm": {"before": before_params["PlateLength"], "after": after_params["PlateLength"]},
    "volume_mm3": {"before": round(before_volume, 6), "after": round(after_shape.Volume, 6),
                   "analytic_after": round(expected_volume, 6)},
    "bbox_x_mm": round(after_shape.BoundBox.XLength, 6),
    "expression_chain": [list(pair) for pair in doc.getObject("Plate").ExpressionEngine],
    "checks": checks,
}
(RUN_DIR / "edit_plate_checks.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(report, ensure_ascii=False, indent=2))

failed = [name for name, passed in checks.items() if not passed]
if failed:
    raise RuntimeError("edit_plate 断言失败：%s" % "、".join(failed))

cad_outputs = [TARGET_OBJECT]
