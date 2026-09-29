"""示例：故意失败 —— 用于验证"失败运行不发布、不覆盖已有模型"。

由 cad-v3 运行时执行，命名空间注入 App / doc / PROJECT_DIR / RUN_DIR。
它会先做**可观察的改动**（改参数源 + 新增一个探针盒子），然后抛异常；
正确行为：本次运行 ok=false，`models/` 与 `artifacts/latest/` 保持上一次成功的样子。

实测提醒（FreeCAD 1.1.3）：异常被 worker 捕获后，**freecadcmd 进程本身可能仍以 0 退出**，
失败只体现在完成记录里的 exception 与 CLI 的非零退出上——这正是"退出码 0 不能单独当成功"的实例。

预期非成功——不要放在正常流程里，只在回滚/失败协议验证时手动运行：
    cad3 run examples/fail.py      # 期望：失败，进程非零退出，完成记录为异常
"""
import json

# === 具名参数 ===
PROBE_OBJECT = "RollbackProbe"
PROBE_SIZE_MM = 12.0
PARAM_ALIAS = "PlateLength"
ALIAS_CELL_SCAN_ROWS = 20
BAIT_VALUE_MM = 999.0
FAIL_MESSAGE = "fail.py: 故意异常（预期失败，用于验证失败不发布模型）"


def find_cell(sheet, alias):
    for row in range(1, ALIAS_CELL_SCAN_ROWS + 1):
        cell = "B%d" % row
        if sheet.getAlias(cell) == alias:
            return cell
    return None


params = doc.getObject("Params")
cell = find_cell(params, PARAM_ALIAS) if params is not None else None

# 可观察改动 1：改参数源（若本工程有该参数表）
if cell is not None:
    params.set(cell, "%s mm" % BAIT_VALUE_MM)
else:
    print("未找到 Params.%s；跳过参数改动，仍继续制造失败。" % PARAM_ALIAS)

# 可观察改动 2：新增一个探针对象
probe = doc.addObject("Part::Box", PROBE_OBJECT)
probe.Label = PROBE_OBJECT
probe.Length = PROBE_SIZE_MM
probe.Width = PROBE_SIZE_MM
probe.Height = PROBE_SIZE_MM
doc.recompute()

print(json.dumps({
    "changed": bool(cell),
    "param_cell": cell,
    "param_value": params.get(cell).UserString if cell is not None else None,
    "added_object": probe.Name,
    "note": "改动已提交给内存中的候选文档；随后抛异常，运行时应判定失败且不发布。",
}, ensure_ascii=False, indent=2))

raise RuntimeError(FAIL_MESSAGE)
