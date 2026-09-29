# CAD V3 多模型评测任务：NEMA 17 可调电机安装板

这是一项独立的 FreeCAD + CAD Tools V3 能力评测。请把自己当作真实任务执行者，完成建模、参数修改、观察、渲染、Git 提交和恢复。不要只给方案；必须在本机生成项目和证据。

## 工作范围

- CAD V3 根目录：

  `/Users/liwuzhan/Desktop/cad tools v3`

- 你的独立临时项目目录：请使用以下目录之一，按模型路由选择自己的目录，不要和其他代理共用：

  - `/tmp/cad3-eval-gpt6-astra`
  - `/tmp/cad3-eval-glm53`
  - `/tmp/cad3-eval-deepseek-flash`

- 只能写入你选择的 `/tmp` 项目目录；不要修改 CAD V3 源码、旧 V2、旧模型、用户 FreeCAD 配置或正在运行的 GUI。
- 不安装 FreeCAD 插件、Python 依赖或 MCP；不联网下载。
- 所有 FreeCAD 操作必须通过：

  ```bash
  python3 "/Users/liwuzhan/Desktop/cad tools v3/cad3.py" ...
  ```

  不要直接启动普通 `freecadcmd`。

## 真实零件

设计一个用于皮带直线轴的 **NEMA 17 步进电机可调安装板**。安装板通过沿 X 方向的两个长圆槽相对机架移动，用于皮带张紧调整。

这是一个概念机械零件评测。任务不要求强度、材料、公差、真实机床、CAM、FEM、GUI 或实际紧固件采购判断。

坐标约定：

- XY 是板平面，Z 是厚度方向；
- 板中心、中心定位孔中心和电机孔阵列中心均为 `(0, 0)`；
- 单位全部为 mm；
- 外缘、孔和槽保持尖角，不加圆角或倒角；
- `SlotLength` 明确定义为长圆槽沿 X 方向的总外廓长度；直段长度为 `SlotLength - SlotWidth`，不是行程长度，也不是端点中心距。

## 必须的原生 FreeCAD 结构

必须创建：

- `Spreadsheet::Sheet`，内部 Name 为 `Params`；
- `PartDesign::Body`，内部 Name 为 `MotorPlate`，Label 可以不同；
- 原生特征链：

  ```text
  BaseSketch -> BasePad -> MotorSketch -> MotorPocket -> SlotSketch -> SlotPocket
  ```

- `Body.Tip` 为最后的 `SlotPocket`；
- PartDesign 特征的 Profile 必须指向对应草图；
- 最终交付对象用 `cad_outputs = ["MotorPlate"]` 或传入实际 Body 对象；这是内部 Name；
- 不得用 `Part::Feature.Shape = ...` 的冻结 BRep 伪装原生特征树；
- 不得添加与 MotorPlate 无关的顶层实体。

所有尺寸必须在 Params 中建立具名 alias，并通过 ExpressionEngine 驱动草图约束或特征属性。允许使用第二个项目内脚本执行改版，但不能用它重建冻结 BRep。

## 参数表

| Alias | 初始值 | 含义 |
|---|---:|---|
| `PlateLength` | 100 | 板 X 总长 |
| `PlateWidth` | 80 | 板 Y 总宽 |
| `PlateThick` | 8 | 板 Z 厚度 |
| `MotorPitch` | 31 | 电机孔阵列正方形边长 |
| `MotorHoleDia` | 3.5 | 四个电机通孔直径 |
| `PilotDia` | 24 | 中心定位通孔直径 |
| `SlotWidth` | 7 | 两个长圆槽宽度 |
| `SlotLength` | 24 | 初始长圆槽沿 X 总外廓长度 |
| `SlotY` | 30 | 两个槽中心的 Y 绝对位置，分别为 ±30 |

## 初版几何验收目标

- 外包围盒 `100 x 80 x 8`；
- 一个有效、闭合的最终 Solid；
- 中心通孔 `Ø24`，中心 `(0, 0)`；
- 四个电机通孔 `Ø3.5`，中心为 `(±MotorPitch/2, ±MotorPitch/2)`，即 `(±15.5, ±15.5)`；
- 两个水平长圆槽，中心为 `(0, ±SlotY)`，宽 `7`，总 X 长 `24`；
- 初版精确体积：

  `57553.257022909158 mm^3`

  公式：

  ```text
  PlateLength * PlateWidth * PlateThick
  - pi * (PilotDia / 2)^2 * PlateThick
  - 4 * pi * (MotorHoleDia / 2)^2 * PlateThick
  - 2 * (SlotWidth * (SlotLength - SlotWidth)
         + pi * (SlotWidth / 2)^2) * PlateThick
  ```

- 容差采用 `max(1e-6 mm^3, 1e-9 * expected_volume)`；不要把显示四舍五入值作为独立真值。
- `BaseSketch`、`MotorSketch`、`SlotSketch` 均应为 `DoF == 0`，且没有已报告的冲突或冗余约束。`solve() == 0` 单独不构成完全约束证明。

孔的验收不能用圆柱面总数代替。顶面和底面的平面内环应分别包含七个开口：中心孔、四个电机孔、两个槽。应按内环面积、中心和包围盒辨认它们。

## 改版任务

初版提交后，只改变参数层的 `Params.SlotLength`：

```text
24 -> 30
```

其余八个参数、BaseSketch 和 MotorSketch 的定义、外包围盒、中心孔、电机孔、SlotWidth 和 SlotY 均保持不变。文件层允许新增改版脚本、模型和证据；“只改 SlotLength”指参数／约束定义层，而不是只允许一个文件 diff。

改版精确体积：

```text
56881.257022909158 mm^3
```

体积下降必须为精确的 `672 mm^3`：

```text
2 * SlotWidth * (30 - 24) * PlateThick = 672
```

改版后两个槽的总 X 长为 `30`，宽仍为 `7`；电机孔和中心孔不变。

## 必须执行的 V3 流程

1. 阅读 CAD V3 的 README、Skill 和 references；
2. `cad3 init` 创建独立项目；
3. 创建 `notes/design.md`，写清功能、参数和解析公式；
4. 复制或编写项目内的原生 FreeCAD 建模脚本；
5. 初版执行：

   ```bash
   cad3 run ... --step
   cad3 inspect --detail
   cad3 inspect --format code
   cad3 render --views iso,top,front,right
   cad3 commit -m "initial NEMA17 adjustable motor plate"
   ```

6. 执行 `SlotLength 24 -> 30` 的真实增量改版；显式 `doc.recompute()`，读回实际参数和几何；再次 inspect、render、commit；
7. 展示 `cad3 log` 和 `cad3 diff`；注意 `diff` 是文件级差异，不能把它当作 CAD 语义差异证明；
8. 在工作区干净时执行 `cad3 restore <初版commit>`；不要重建模型，不要再次提交；读取恢复后的 FCStd 和参数，确认恢复为初版值和初版体积，且 HEAD 不移动；
9. 将所有可复核证据留在项目目录中，包括运行 JSON、观察 JSON、代码式观察、四视图、初版/改版断言和失败修复记录。

## 诚实报告

最终报告必须包含：

- 使用的模型路由和推理预算；
- 项目路径；
- 初版和改版 commit hash 及父子关系；
- 初版、改版、恢复后的实际参数和体积；
- 原生特征树、Profile、Tip、ExpressionEngine 和草图诊断证据；
- 渲染路径；
- 失败尝试、FreeCAD 版本特定坑和 V3 阻碍；
- 哪些检查由模型自己做，哪些没有独立验证；
- 不要把 `ok=true`、BRep valid、脚本断言通过或截图好看写成制造合格证明；
- 不要修改 V3 源码以迁就本任务。

任何失败都保留在项目报告中，不能删除后只报告最终成功。
