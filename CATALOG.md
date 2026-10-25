# CAD3 标准件库

这是面向 CAD 模型的渐进式零件目录。优先用 `cad3 catalog` 查库，选定后用
`cad3.parts.add` 在 FreeCAD 文档中放入标准件代理，并从代理声明中取得装配接口。
不要凭型号印象补尺寸；无精确候选时先报告候选与缺失字段。

## 模型调用流程

```bash
python3 cad3.py catalog search "6204 深沟球轴承"
python3 cad3.py catalog compare 6204 6304
python3 cad3.py catalog describe 6204
python3 cad3.py catalog instantiate 6204 --selection closure=2RS --selection clearance=C3
```

查询输出是 JSON。`search` 给出短候选；`describe` 只返回一个 family/item 契约；
`instantiate` 返回单位明确的参数、采购语义和本地坐标接口，不创建文件，也不启动 FreeCAD。
搜索结果的 `capabilities.native_proxy` 表示是否已有可执行的原生 FreeCAD 简化代理。
目录中的其它条目仍可搜索和比较；没有代理时不可假装它已能生成。

## 在 FreeCAD 建模脚本中使用

`cad3.parts` 和 `cad3.catalog` 不依赖 build123d。建模脚本直接使用本机随 cad3 安装的模块：

```python
from cad3.parts import add, declaration

motor = add(doc, "NEMA17", name="Stepper", placement=App.Placement(
    App.Vector(0, 0, 0), App.Rotation()))
bearing = add(doc, "608ZZ", name="BearingA")
ports = declaration(bearing)
print(ports)  # 世界坐标、cad3.mates 可读的接口
cad_outputs = [motor, bearing]
```

零件保存在普通 `Part::Feature` 中，完整 `cad3.part-instance/v1` 写入 `Cad3Part` 属性；
不保存工程 Python Proxy。文档重开后，B-rep 和采购/接口合同仍在。`Placement` 可单独编辑；
修改规格时再次运行 `add`。代理不是制造级模型，具体精度和省略项在实例的 `geometry` 字段里。

首批可直接实例化的族：深沟球轴承、六角螺栓/螺母/平垫圈、方形步进电机、LM/LMF/LMK
直线轴承、同步带轮、弹性联轴器和圆棒。LME 直线轴承、轴承 `rings` 细节及其他尚未实现的族
会明确拒绝原生实例化，但仍可查询其目录声明。LME 需要另外核实英制/公制系列的尺寸来源。

轴承目录包含 608 与 62/63 系列边界尺寸；`608ZZ`、`608-2RS` 等别名保留密封选项。
同一几何不同采购选项不复制几何型号。紧固件通过 family + 参数实例化，例如：

```bash
python3 cad3.py catalog instantiate fastener.hex_bolt_metric --parameter size=M6 --parameter length=20
```

## 非标件与装配

非标件仍由模型直接建模；供人继续设计和反馈的零件默认保留按功能命名的原生特征树，
并在 `notes/interfaces.json` 声明实际对象和关键接口。特征树与逐个孔的语义编号需要分别考虑，
具体取舍见 [建模指引](.agents/skills/freecad-v3/references/modeling-guidance.md)。
标准件的 `declaration(obj)` 根据当前 Placement 把目录局部接口变换到文档全局坐标，可直接
放入 `parts` 声明；配合关系仍由设计意图显式提供。接口是装配和检查证据，不是运动求解、
强度、公差或采购认证。

接口 JSON 是当前参数与位置的声明快照；修改参数或 Placement 后应重新生成声明，
再核对配合关系。编辑 FCStd 本身不会自动刷新这个外部文件。

来源目录沿用 V2 MIT 项目的 JSON 自声明，并附有来源清单和哈希。第一批原生代理参数表位于
`src/cad3/data/profiles.json`。新增或修正型号必须更新声明、接口测试和 `index.json`；不能只
改索引。跨厂商相似型号不自动视为互换。
