# 测量数值与无头视觉观察

来源：FreeCAD 1.1.3 / macOS arm64 / `freecadcmd`（`GuiUp=0`）隔离实测，含主代理独立复核的 15 项解析对照。
数值通过**不等于**图像正确，图像好看也**不等于**数值正确——两者必须交叉核对。

## 1. 测量前先解决"新鲜度"

- 状态源必须是**已 recompute 的 Tip 形状**：先 `doc.recompute()`，再读几何，并用**驱动参数**反证。
- 未 recompute 时：`Params.State` 变 `Touched`，但 `Body.State` 仍是 `Up-to-date`、`Body.Shape.Volume` 与 `BoundBox` 全是旧值；`Spreadsheet.get(alias)` 也返回上一次求值结果。**`obj.State` 不能当新鲜度判据。**
- `doc.recompute()` 返回 `int`（实测 `6`），**不是失败对象列表**，不能当成功判据。
- 测谁：`Body`（`PartDesign::Body`，Compound，等于 Tip 结果）或 `Body.Tip`；不要测中间特征 `Pad`。
  `Body.Shape.isSame/isEqual(Tip.Shape)` 与 `hashCode()` 在这里全为假/不等，但几何量相等——**要比就比数值**。

## 2. Shape 指标与坑

`Volume`(mm³) / `Area`(mm²) / `Length`(mm) / `BoundBox`(mm) / `CenterOfMass` 或 `CenterOfGravity`(App.Vector) 都是普通 float，**不带单位对象**。

- **Compound 上没有 `CenterOfMass`**，用 `CenterOfGravity`。
- **开口壳照样报出像样的体积**（5/6 面开口壳 `Volume=800`、`isValid=True`）。体积数字不能证明闭合实体。
- **不要用壳数等于实体数当门控**：10³ 减中心 6³ 的合法实体是 1 Solid / 2 Shells（体积 784）。装配各 Solid 体积之和也不等于其并集体积（需要先 `fuse()` 或扣重叠）。
- `isValid()`/`check()` 只管 BRep 拓扑：健康时 `check()` 返回 `None`，不健康时**抛** `ValueError("No error")`；空 `Part.Shape()` 是 NULL 对象，各属性抛不同异常。

## 3. 距离 / 接触 / 干涉

`dist, vectors, infos = A.distToShape(B)`，同时 `A.common(B).Volume`：

```
dist > 形状容差              → 间隙 GAP
dist == 0 且 overlap == 0    → 接触 CONTACT（面/边/点贴合，common 是空集）
dist == 0 且 overlap > 0     → 干涉或完全内含
overlap == min(VolA, VolB)   → 完全内含（内含只出现在 overlap>0 分支）
```

- **`dist == 0` 只表示"低于本形状容差分辨率"**：1e-8 mm 间隙实测返回 `0.0`，1e-4 mm 能分辨。亚容差判定要换方法。
- **接触时 `common` 是空集**（0 面/0 边/0 顶点，`Area=0`），所以无法用 `common` 区分面/边/点接触。
- `infos` 的元素索引是 **0-based**，`+1` 才对应 `Face<N>` / `Edge<N>` 名称；`vectors` 给出成对最近点。
- `Shape.proximity(other)` 在未三角化时直接抛 `OCCError`，不是即用接口。
- 交叉校验：`VolA + VolB − fuse.Volume` 与 `common.Volume` 逐位一致；但两者共享同一 BRep/OCCT 内核，只能降低、不能排除共模误差。

## 4. 圆柱特征：列候选，不要数孔

同一个圆柱面的多条读数路径（`Face.Surface.Radius`、`Edge.Curve.Radius`、`Edge.Length/π`、`Face.Area/(2πh)`、面 `BoundBox` 跨度）在本样本上互相一致，但它们**同源**。

- **圆柱面数 ≠ 孔数**：沉头孔 1 个孔 2 个圆柱面；普通通孔 1:1；圆柱凸台（加料）0 个孔 1 个面；圆角块 0 个孔 1 个面。
- **半径也不能区分圆角与孔**：实测圆角半径 4.0 与 Ø8 孔完全相同。
- 正确做法：按**轴线 + 半径 + 与相邻面的拓扑关系/内外法向 + 是否材料侧**判定，输出"圆柱候选 + 判据"，而不是把面数当孔数。
- `Surface.Axis` 的方向符号是任意的，不可断言；`Surface.Center` 只是轴线上一点。

## 5. 截面、面积与角度

- `Shape.section(plane)` **只给截面曲线（边）**，不给填充面。要面积就与一个覆盖截面的平面面片求 `common`（精确），对可被孔分成多块的截面会返回 0 面，此时回退薄片求交，**必须在结果里标注 `method=approximate`**（实测薄片法 600.0016 vs 解析 600）。
- 环面积必须按导线**真实连接顺序**串联取点：OCC 会反向存储同一 wire 的部分边，逐边首尾拼接会得到自交环（300 mm² 被算成 150）。
- `slice` / `slices` 不是面积 API。
- 角度：原生 `Measure.Measurement.angle()` 对平面引用恒抛 `RuntimeError`；替代路径用 `Vector.getAngle`（**返回弧度**），并区分有向法向角与无向夹角、处理法向符号。
- 局部壁厚可用 `isInside` 射线二分估计；**本轮未找到通用全局最小壁厚一键查询**，局部数值不构成全局认证。

## 6. 无头视觉观察

| 能力 | 无头可用 | 说明 |
|---|---|---|
| **`cad3 render --section AXIS=MM`** | 是 | 半空间 `common()` 切开每个输出对象，正对切面出图；切后零件两两重叠画红并列在 `overlaps`。首选的"看内部"手段 |
| BRep 网格化 + VTK 离屏着色 PNG | 是 | 用安装包自带 VTK 9.3.1，`App.GuiUp=0`；**不是 GUI 截图**，不保留选中态/显示对象/界面标注 |
| 几何投影（可见 + 隐藏线） | 是 | `TechDraw.project()` 返回 4 组 `[可见边, 可见点, 隐藏边, 隐藏点]`（都是 `Part.Shape`）；`projectEx()` 返回 10 组 |
| 原生矢量投影 | 是 | `projectToSVG` / `exportSVGEdges` / `projectToDXF`；`projectToSVG` 有隐藏线虚线分组与线宽分级，但**无填充、无尺寸、无文字、无 viewBox** |
| 剖面 / 截面 | 是 | 半空间布尔 + 投影；切面面积与解析值精确一致（1300.0 / 1200.0），薄片回退标近似 |
| 原生 TechDraw 页面/视图/剖视/尺寸对象 | 可建可算可读边 | `DrawPage`+`DrawViewPart`+`DrawViewSection`+`DrawViewDimension` 在 `freecadcmd` 内建成功 |
| 原生页面导出 DXF | 是 | `writeDXFPage` / `writeDXFView` 实际写出文件 |
| 原生页面导出 SVG / PNG | **否** | 需要 `TechDrawGui`；控制台里 `obj.ViewObject is None`（不是抛异常），`saveImage()` 无从调用 |
| GUI 视口截图 | 未测 | 本轮未接管 GUI，不把"`freecadcmd` 没有 activeView"外推成"FreeCAD 不能截图" |

**图像来源必须标清楚**：`project()` 的几何由 FreeCAD 计算；若像素由适配层（matplotlib 等）栅格化，就不是原生视口帧。主代理实测抓到过"面积数值全对但填充画在轮廓外"的坐标系混用错误——**44 项数值通过没拦住它**。所以：

1. 每张图要能和数值对上（bbox、可见/隐藏边数、切面数与净面积）；
2. 用了回退路径（薄片、近似、自绘填充）就在代码与输出里写明；
3. 读图之后要用语言描述看到了什么，再去和数字比。

## 7. 明确不声称

- 不声称有"任意孔/有效壁厚"的通用自动识别接口。
- 不声称全局最小壁厚、连续运动碰撞、强度/工艺合格性。
- 不声称原生 GUI 截图、原生 TechDraw 页面 PNG/SVG 导出、GUI 选择态可无头复现。
- 相关性数值（多路径读出同一个 8.0）是**同源交叉校验**，独立效力只来自与设计声明值的比对。
