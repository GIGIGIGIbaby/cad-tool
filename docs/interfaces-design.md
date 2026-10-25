# 声明式机械接口与类型检查器

## 结论

V3 不应把 Build123D 的标准件生成器直接移植成 FreeCAD `FeaturePython`。FreeCAD 重开文档时可能阻止工程内 Python 模块恢复，导致代理失效；而脚本写入的 `Part::Feature` 也是冻结形状。适合迁移的是 V2 的**标准件目录和接口元数据**，不是其运行时代理模型。

建议把机械装配看成三种彼此独立的证据：

1. **目录契约**：标准件族说明有哪些参数、标准依据、名义尺寸和连接接口。
2. **装配声明**：当前工程声明哪些对象代表哪些零件、每个接口在哪里、接口之间如何连接。
3. **几何核对**：FreeCAD 重算后测量真实 BRep，核对声明的接口是否确实存在。

检查结果是设计反馈，不是制造认证。`PASS` 表示目前的声明和测量相互一致；`WARN` 表示需要模型判断或存在遗漏风险；`FAIL` 表示声明之间或声明与几何直接矛盾；`UNKNOWN` 表示工具没有足够证据，不能默认为通过。

## 当前 V3 原型

入口为：

```bash
python3 cad3.py mates -p /path/to/project
python3 cad3.py mates -p /path/to/project --no-geometry
```

默认读取 `notes/interfaces.json`。`--no-geometry` 只做声明层检查，因此模型尚未建出时也可以先发现轴端没有支撑、零件没有配合、接口尺寸不匹配等问题。存在 `models/main.FCStd` 时，第二阶段在隔离 FreeCAD 进程中检查圆柱面和基准平面。

当前已经覆盖：

- 轴/孔名义直径关系，以及轴径大于孔径的矛盾；
- 轴线平行、同轴和偏移检查；
- 螺纹包络的公称直径与螺距；
- 通孔与轴/螺杆的通过关系；
- 互相接触的平面外法向和位置；
- 未配对接口、完全没有配合的零件、没有连到基准件的零件；
- 声明的圆柱接口是否在指定 FreeCAD 对象、指定位置、指定轴线和内外侧真实存在；
- 找到同直径但错误轴线的圆柱时给出诊断提示。

## 声明示例

下面是“轴两端由两个 608 轴承支撑”的最小意图。坐标为文档全局毫米；圆柱接口的 `origin` 可以是轴线上任意一点，平面接口的 `origin` 是面上一点。

```json
{
  "schema": "cad3.interfaces/v1",
  "ground": "BasePlate",
  "parts": {
    "BasePlate": {
      "object": "BasePlate",
      "ports": [
        {"id": "left_bearing_hole", "type": "cylindrical_bore",
         "origin": [10, 30, 20], "axis": [1, 0, 0], "diameter": 22},
        {"id": "right_bearing_hole", "type": "cylindrical_bore",
         "origin": [190, 30, 20], "axis": [1, 0, 0], "diameter": 22}
      ]
    },
    "LeadScrew": {
      "object": "LeadScrew",
      "designation": "Tr8x8",
      "ports": [
        {"id": "journal_left", "type": "cylindrical_surface",
         "origin": [10, 30, 20], "axis": [1, 0, 0], "diameter": 8},
        {"id": "journal_right", "type": "cylindrical_surface",
         "origin": [190, 30, 20], "axis": [1, 0, 0], "diameter": 8},
        {"id": "threaded_section", "type": "male_thread_envelope",
         "origin": [100, 30, 20], "axis": [1, 0, 0], "nominal_diameter": 8, "pitch": 2}
      ]
    },
    "BearingLeft": {
      "object": "BearingLeft",
      "designation": "608ZZ",
      "ports": [
        {"id": "shaft_bore", "type": "cylindrical_bore",
         "origin": [10, 30, 20], "axis": [1, 0, 0], "diameter": 8},
        {"id": "housing_seat", "type": "cylindrical_surface",
         "origin": [10, 30, 20], "axis": [1, 0, 0], "diameter": 22},
        {"id": "axial_face", "type": "planar_face",
         "origin": [10, 30, 20], "axis": [-1, 0, 0]}
      ]
    },
    "BearingRight": {
      "object": "BearingRight",
      "designation": "608ZZ",
      "ports": [
        {"id": "shaft_bore", "type": "cylindrical_bore",
         "origin": [190, 30, 20], "axis": [1, 0, 0], "diameter": 8},
        {"id": "housing_seat", "type": "cylindrical_surface",
         "origin": [190, 30, 20], "axis": [1, 0, 0], "diameter": 22},
        {"id": "axial_face", "type": "planar_face",
         "origin": [190, 30, 20], "axis": [1, 0, 0]}
      ]
    }
  },
  "mates": [
    {"a": "LeadScrew.journal_left", "b": "BearingLeft.shaft_bore", "why": "left radial support"},
    {"a": "LeadScrew.journal_right", "b": "BearingRight.shaft_bore", "why": "right radial support"},
    {"a": "BearingLeft.housing_seat", "b": "BasePlate.left_bearing_hole", "why": "left housing seat"},
    {"a": "BearingRight.housing_seat", "b": "BasePlate.right_bearing_hole", "why": "right housing seat"}
  ],
  "unmated_ok": {
    "LeadScrew.threaded_section": "the nut rides here; deliberately outside this subassembly",
    "BearingLeft.axial_face": "axial location comes from the housing shoulder in the plate",
    "BearingRight.axial_face": "axial location comes from the housing shoulder in the plate"
  }
}
```

每个接口要么出现在 `mates` 里，要么在 `unmated_ok` 里写明它为什么是自由的。写不出理由的接口，就是需要回去看的地方；`unmated_ok` 里写了不存在的接口会被报为过期条目。

这里的关键不是把所有面都声明出来，而是声明**设计意图中的接口**。轴承的内径对应 `shaft_bore`，外径对应 `housing_seat`，宽度和端面对应轴向定位接口；一个只声明“有一个轴承实体”的模型无法得到同样的检查能力。

## 为什么采用接口而不是自动猜语义

从 BRep 反推“这是轴、孔、轴承内圈还是装配接触面”不可靠：圆柱面数量不能等同于孔数量，沉头孔会有多个圆柱/圆锥面，圆角也可能产生同样半径。声明提供了模型已经知道的语义，几何检查只负责验证位置、方向、尺寸和内外侧。

声明和几何必须分开报告：

- 声明层可以在建模之前运行，反馈设计遗漏；
- 几何层只能判断当前文档状态，不能判断历史意图是否完整；
- 两层都通过，也不代表有足够轴承寿命、预紧、材料强度、制造公差、装配可达性或运动行程。

## 标准件目录迁移边界

V2 的目录数据值得迁移为版本化 JSON 或只读 Python 数据：

- `catalog_id`、类别、别名和标准编号；
- 参数和单位；
- 名义尺寸，例如轴承 `bore`、`outside_diameter`、`width`；
- 接口类型、角色和可派生的接口尺寸；
- 标准引用、精度/细节等级和已知省略项。

例如 6204 可产生如下契约（数值来自 V2 目录）：

```json
{
  "catalog_id": "bearing.deep_groove.6204",
  "designation": "6204",
  "dimensions_mm": {"bore": 20, "outside_diameter": 47, "width": 14},
  "interfaces": [
    {"id": "shaft_bore", "type": "cylindrical_bore", "diameter_from": "bore"},
    {"id": "housing_seat", "type": "cylindrical_surface", "diameter_from": "outside_diameter"},
    {"id": "axial_face_min", "type": "planar_face"},
    {"id": "axial_face_max", "type": "planar_face"}
  ]
}
```

目录数据不应自动修改 FreeCAD 文档，也不应依赖工程内的可执行代理恢复。实际几何可以由 `Part::Cylinder`、原生 PartDesign 特征、装配脚本或外部 STEP 提供；用户/模型通过 `object` 显式绑定对象，检查器只读当前形状。

### 当前落地状态

V3 已将 V2 MIT 目录声明复制到 `src/cad3/data/catalog/`，包含 34 个 family 和 288 个原有型号条目，并加入 608 与 LM8 两个高频补充项。只读 CLI 为 `cad3 catalog search|compare|describe|instantiate`。目录本身可以发现更多零件；`native_proxy` 能力字段只对已实现的族为真。

首批原生 `Part::Feature` 代理覆盖深沟球轴承、六角螺栓/螺母/垫圈、方形步进电机、LM/LMF/LMK 直线轴承、同步带轮、弹性联轴器和圆棒。完整实例契约写入对象属性，文档重开不需工程 Python Proxy；`cad3.parts.declaration(obj)` 会用对象当前 Placement 把目录端口变换到全局坐标，结果可交给 `mates`。这是固定 B-rep 简化代理，换型号需再次执行建模脚本。

LME 尺寸在 V2 目前复用了 LM 数据，本项目保留可检索声明但不生成代理。其余目录族同样保持“可检索”与“可生成”分开的状态；缺少核实的尺寸来源或接口实现时不得生成近似型号并当作指定件。

## 建议的实施顺序

### 阶段 1：把原型变成稳定检查契约

- 保持 `notes/interfaces.json` 为显式工程文件并纳入 Git；
- 为 `mates` 报告增加稳定的 `design_verdict`/`counts` 使用说明，顶层 `ok` 继续只表示运行协议成功；
- 把几何容差从隐含常量扩展为声明级别的 `tolerance`，默认值仍保持保守；
- 增加轴承、安装孔、平面座的端到端样例和错误样例；
- 把完整报告保存到 `artifacts/latest/mates.json`，供模型下一轮直接阅读。

### 阶段 2：标准件目录与原生代理（首批已完成）

- 已建立内置 JSON 声明、紧凑索引与免 FreeCAD 的查询 CLI；
- 已迁移 V2 catalog contracts，并覆盖第一批高频原生 FreeCAD 简化代理；
- 实例端口带具体数值坐标，库代理端口可转换为 `mates` 声明；
- 后续只按真实装配需求扩展原生代理，保持目录发现范围可大于代理生成范围。

### 阶段 3：更强的关系检查

- 给接口增加 `contact`、`clearance`、`axial_stop`、`rotational_drive` 等关系语义；
- 用局部几何测量验证轴向间隙、接触距离和包络，而不是仅验证单个面存在；
- 增加“同一轴线上的两个支撑”“轴向定位至少一端”“旋转自由度/锁定意图”等装配级启发式；
- 全部仍报告为警告或证据，不声称完成运动学、强度或制造验收。

## 不建议现在做的事

- 不要把所有 FreeCAD 对象自动分类成标准件；
- 不要用实体数、圆柱面数或总体积作为通用合格门槛；
- 不要把 `FAIL` 当作阻止脚本运行的编译错误；
- 不要在没有声明的情况下从几何猜测唯一装配意图；
- 不要为了“参数会更新”重新引入工程内 `FeaturePython` 代理。
