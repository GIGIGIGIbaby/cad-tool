# 特征树、参数链与草图求解诊断

来源：FreeCAD 1.1.3 / macOS arm64 / `freecadcmd` (`GuiUp=0`) 隔离实测，样本为
PartDesign Body + Pad + Pocket + 2 草图 + Spreadsheet（14 对象）。所有结论都有原始 JSON/日志留证；下面只写已被实测支持的部分。

## 1. 对象与结构（都无需解析 FCStd XML）

| 项 | API | 备注 |
|---|---|---|
| 标识 | `obj.Name` / `obj.Label` / `obj.TypeId` | 三者语义不同，一起输出；`TypeId` 精确到 `PartDesign::Pad` |
| Body | `Body.Group` / `Body.Tip` | 实测 `Tip='Holes'`；加 Fillet 后 `Tip` 变为 `EdgeFillet` |
| 依赖 | `obj.InList` / `OutList` / `OutListRecursive` | **OutList 有重复项、顺序不稳定**；消费前先去重+排序 |
| Profile | `pad.Profile` | `PropertyLinkSub`，读回**扁平元组** `(sketch, [])` |
| 附着 | `sk.AttachmentSupport` / `MapMode` | `PropertyLinkSubList`，读回 `[[plane, ['']]]`（list of tuples，形态与上一条不同） |
| Fillet 引用 | `fillet.Base` | 也是扁平元组 `(obj, ['Edge1'])`；按 list-of-tuples 赋值会抛类型错 |
| 参数单位 | `PropertyLength` 读回 `Quantity` | `'6.00 mm'`，`.UserString`/`.Value`/`.Unit`；`PropertyFloat/Integer` 无单位，用 `getTypeIdOfProperty()` 区分 |
| 结构图 | `doc.exportGraphviz()` | 内置 DOT 导出，含表达式虚线边；**是图不是可执行脚本** |
| 视图状态 | `obj.ViewObject` | 无 GUI 时属性存在但为 `None` |

**陷阱**：`App::Plane` 是无限面，`Shape.Volume` 会给垃圾大数（实测 `XZ_Plane = -1.19e191`）；判断"有体积"必须用 `len(Shape.Solids) > 0` 门控。

## 2. 参数链：Spreadsheet → 约束 → 几何 → 特征

三种语义要分开取，不能混成一个字段：

```python
sheet.getContents("B1")   # 原始式 '=80 mm'
sheet.get("B1")           # 求值  '80.0 mm'
sheet.getAlias("B1")      # 'PlateLength'
```

实测传播（改一处即可穿透到实体）：

- `Sheet.set("B1", "100 mm")` + `recompute` → 约束 `PlateLength` 80→100、`LeftX` −40→−50、Pad 24000→30000 mm³、bbox X 80→100。
- `HoleDia` 8→12 mm → 圆半径 4→6、孔特征体积随之变化。
- 新增 `PartDesign::Fillet` → `Body.Group` 增项且 `Body.Tip` 前移。

表达式读取：

```python
pad.ExpressionEngine          # [('Length', 'Params.PlateThick')]
sk.ExpressionEngine           # [('.Constraints.PlateLength', 'Params.PlateLength')]  ← 参数链的关键
```

改表达式的四条实测规则：

1. `setExpression` 后**不 recompute 不生效**（读旧值）。
2. `clearExpression` **不恢复原值**，保留最后一次求值。
3. 非法表达式（指向不存在的对象）**不报错**，错误推迟到重算。
4. 表达式驱动的量上 `setDatum('PlateWidth', 40mm)` → **调用成功但完全无效**（值仍 50，体积不变），重算后表达式静默胜出。**这是最危险的失败模式：静默 no-op。**
   要改就得改源（单元格/表达式），或先 `clearExpression` 再设值——但混用驱动会破坏对称（实测矩形由对称变成 y∈[−25,15]）。

## 3. 草图求解诊断

必须**同时**读四个量，任一单独读数都会误导：

```python
sk.solve()                        # int 返回码
sk.DoF                            # 剩余自由度（属性名是 DoF，不是 DOF / dof_count）
sk.FullyConstrained               # bool，不可单独信任
sk.ConflictingConstraints         # list[int]，1-based
sk.RedundantConstraints           # list[int]，1-based
sk.PartiallyRedundantConstraints  # 本轮未构造出非空样例，编号基准未验证
```

`solve()` 码：`0` 成功、`-1` 求解器错误、`-2` 冗余、`-3` 冲突、`-4` 过度约束、`-5` 约束畸形（`-1`/`-5` 本轮未复现）。

已实测的关键事实：

- **`solve()==0` 不等于完全约束**：一条线只加 `Horizontal` → `solve=0`、`DoF=3`、`FullyConstrained=False`。
- **`FullyConstrained=True` 也不能单独信**：`solve=-2`（冗余）时它仍为 `True`，坐标也正确。
- **冲突时 `DoF` 不会变 0 或负数**：实测冲突例 `solve=-3`、`DoF=1`；过度约束例 `DoF=-1`。别用 `DoF` 判冲突。
- `State` / `getStatusString()` 是**几何对象有效性**通道（`Touched` → `Up-to-date`），不表达欠约束/冲突/冗余。
- **诊断编号是 1-based**，而 `Constraints[i]` / `delConstraint()` / `addConstraint()` 是 0-based：`python_index = reported_id - 1`。实测：把矛盾约束放在 Python 索引 0 与 3，报告值为 `[1, 4]` —— 统一 +1 平移。删之前仍要核对 `sk.Constraints[i].Type/.Value`，因为字面完全相同的重复项之间求解器可任选一条。
- **冗余有官方一键修复**：`sk.autoRemoveRedundants()`（实测 `-2`→`0`、列表清空、约束数 4→3）。**冲突没有一键修复**，删哪条是设计决策，必须由调用方决定。
- 参考/驱动尺寸：`sk.setDriving(idx, False)` 可读写回 `sk.Constraints[i].Driving`；参考尺寸不消自由度，但能消除"重复驱动尺寸"造成的冗余报告。
- 求解真的发生了的证据：相切例中圆心 `y=2` 是输入初始值，解出 `y=5.0`（脚本里只有半径 5），残差 `|dist−r|=0`，`DoF` 恰好降 1。对称例镜像残差精确为 0。
- **验证残差要用约束自己引用的几何**：一次"求解器违反对称约束"的假象，实际是参照物取错——轴自己移动了，求解器把对称建立在移动后的轴上。

## 4. 拓扑命名与扩展机制的限制

- `Shape.ElementMap` 是 `{历史键: 名称}`，名称为 `Face1/Edge1/...`，**顶点名可以一对多**。
- 纯重算（无参数改动）名称与历史键 0 变化；改尺寸后**名称保留但"名称→历史键"绑定会被重新生成**（实测 12/38 与 4/26 换键）。
  → 不能声称 Face/Edge 编号永远稳定，更不能只凭序号认定同一物理特征。
- FeaturePython（脚本对象）：快照只存 `<模块>.<类名>`，**装不下实现**；把 Proxy 类放进可导入模块后，另一进程重开可完整恢复行为（700→2000→6000 mm³ 含表达式）。`json.dumps(obj.Proxy)` 会抛 `TypeError`。
- 子元素引用（如 Fillet 的边）失败是**静默**的：伪造 `Edge99` 不抛异常，只得到 `State=['Touched','Invalid']`、`Shape=None`，stderr 才写 `Invalid edge link`。

## 5. 明确不声称

- 未找到内置的"任意文档 → 完整可执行 Python"出口：`exportPython/toPython/dumpPython/saveAsPython` 不存在，53 个导出过滤器里没有 Python（`meshpy` 是 FEM 网格数组，`csg` 实测是三角化顶点转储）。最接近的 `exportGraphviz`（结构）与 `csg/scad`（几何）都不是重建器。
- 本轮只证明该版本、该样本上的行为；未验证 TechDraw/Assembly 树、跨版本命名稳定性、含内部对齐约束草图的诊断编号基准。
