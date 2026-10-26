# 接口声明与 `cad3 mates`

`notes/interfaces.json` 写"每个零件对外有哪些接口、哪个接口接哪个"。`cad3 mates` 做两件事，都只报告、不替你设计：

1. **声明自洽**（不需要几何，`--no-geometry` 可在建模前跑）：配合对是否说得通（孔径 vs 轴径、螺纹、通孔、平面朝向）、声明的轴线是否同轴/平面是否贴合；**哪些接口什么都没接**；哪些零件没有任何配合；哪些零件连不回 `ground`。
2. **声明 vs 几何**：每个圆柱/平面接口是否真的出现在该对象的形状上——同一位置、同一直径、同一轴向、同一材料侧（孔还是轴）。找不到时列出这个零件上同直径的圆柱在哪、朝哪。

## 怎么写才有用

- **外购件按真实件声明它全部的标准接口**，不只是你用到的那几个。一只 608 轴承有内孔、外圈座和两个端面；一根丝杆有两端轴颈和螺纹段；一台 NEMA17 有止口、安装面、四个螺孔和输出轴。自制件声明它承担的功能面。
- 每个接口要么出现在某条 `mates` 里，要么写进 `unmated_ok` 并**写明为什么空着**。写不出理由的，就是要回去看的地方。
- 坐标用全局毫米、文档当前状态；`origin` 是轴线上任一点或面上一点，`axis` 是圆柱轴向或平面**外法线**；`object` 是 FreeCAD 对象名（与键名不同时填）。
- 运动件的声明按保存时的位置写；需要时在其它位置另存一份声明再跑。

```json
{
  "schema": "cad3.interfaces/v1",
  "ground": "BasePlate",
  "parts": {
    "BasePlate": {"ports": [
      {"id": "housing_bore", "type": "cylindrical_bore", "origin": [5, 30, 20], "axis": [1, 0, 0], "diameter": 22}]},
    "LeadScrew": {"designation": "Tr8x8", "ports": [
      {"id": "journal_motor", "type": "cylindrical_surface", "origin": [0, 30, 20], "axis": [1, 0, 0], "diameter": 8},
      {"id": "journal_idler", "type": "cylindrical_surface", "origin": [300, 30, 20], "axis": [1, 0, 0], "diameter": 8}]},
    "Bearing608_A": {"designation": "608ZZ", "ports": [
      {"id": "bore", "type": "cylindrical_bore", "origin": [5, 30, 20], "axis": [1, 0, 0], "diameter": 8},
      {"id": "outer", "type": "cylindrical_surface", "origin": [5, 30, 20], "axis": [1, 0, 0], "diameter": 22}]}
  },
  "mates": [
    {"a": "LeadScrew.journal_motor", "b": "Bearing608_A.bore", "why": "radial support"},
    {"a": "Bearing608_A.outer", "b": "BasePlate.housing_bore", "why": "housing seat"}
  ]
}
```

上面这份会报一条 `unmated_interface`：`LeadScrew.journal_idler` 没有接任何东西——这就是"丝杆远端没有支撑"在声明里的样子。

类型：`cylindrical_surface`（轴/外圆）、`male_shaft`、`cylindrical_bore`（孔）、`clearance_hole`、`male_thread_envelope` / `female_thread_envelope`（需 `nominal_diameter` 与 `pitch`）、`planar_face`、`axis`。

## 读结果

- `overall`：PASS / UNKNOWN / WARN / FAIL（取最差）。`problems` 列出所有非 PASS 项。
- `FAIL` 且带 `found`：声明的孔/轴不在几何上，`found` 给出同直径圆柱的实际轴向和位置——多半是打错轴向或位置。
- `unmated_interface` / `part_without_mates` / `not_connected_to_ground`：声明本身在说"这个东西没被什么支撑或连接"。先想清楚是声明漏了，还是设计漏了。
- 通过只说明声明自洽且与几何一致；不涉及强度、公差等级、装配可达性或运动过程。声明是你写的，漏声明就查不到。
