---
name: freecad-v3
description: 在 cad-v3 工程里用原生 FreeCAD Python 建模、观察和留档。需要本机已安装 FreeCAD；适合编写/修改 src/main.py、执行 cad3 命令、按需读取 references。
---
<!-- 本段由 packages/sync-vendor.mjs 生成；不要手改，改 .agents/skills/freecad-v3/ 后重跑同步。 -->
## DSH 插件里的调用方式

这个 skill 随 `dsh-cad-studio` 插件安装。在 DSH 会话里用插件工具做 CAD，不要在 shell 里找
`cad3` 可执行文件（插件把 Python CLI 内嵌在自己的包目录里）：

- `cad3_doctor`：探测已安装的 FreeCAD（缺 FreeCAD 时先报告，不自动安装）。
- `cad3_init` / `cad3_run` / `cad3_inspect` / `cad3_render` / `cad3_mates` /
  `cad3_status` / `cad3_log` / `cad3_diff` / `cad3_commit` / `cad3_restore`：
  对应同名 CLI 子命令，常用参数与 CLI 一致。
- `cad3_catalog`：查询随插件内置的标准件目录（search / describe / compare / instantiate）。
- `cad3_cli`：兜底工具，把任意 `cad3` argv 原样转发，用于工具尚未覆盖的参数。
- 工程目录用 `project` 参数传入；不传时默认会话工作区。

下面是从仓库同步来的原 skill 正文。


# FreeCAD V3 建模工场

cad-v3 = 普通工程目录 + 小 JSON CLI + 原生 FreeCAD 脚本 + 只读观察 + Git 快照。
模型怎么建由你决定：直接用原生 `Part` / `PartDesign` / `Sketcher` 等 API 自由编排，没有强制的几何流程，也不是另一套 CAD DSL。

## 运行入口与短命令

```bash
cad3 doctor                     # 探测已安装的 FreeCAD 与版本（不改用户配置）
cad3 init PATH --name NAME      # 只在不存在/空的目录建工程
cad3 run src/main.py            # 执行脚本（先不发布）
cad3 run src/main.py --step     # 执行并导出 STEP
cad3 inspect --detail           # 观察当前模型（只读）
cad3 inspect --format code      # 只读的代码式描述，不是可执行重建脚本
cad3 render --views iso,top,front,right
cad3 render --section y=12.5     # 剖面：看内部配合；零件重叠处标红（可重复 --section）
cad3 status | log | diff        # Git 状态 / 历史 / 差异（diff 是文件差异，不是几何语义差异）
cad3 catalog search QUERY     # 搜索内置标准件目录
cad3 catalog instantiate ID   # 返回装配接口和原生代理契约
cad3 commit -m "MSG"            # 只快照当前文件，不重跑建模
cad3 restore REV                # 回到某次提交的内容（不动 HEAD，下次 commit 记录回滚）
```

- 工程根由 `cad3.json` 标记；默认从当前目录向上找，或用 `-p PROJECT` 指定。
- CLI 未安装时用 `python -m cad3 ...`；fresh checkout 中 `python cad3.py ...` 也可。
- `cad3` 只发现并调用**已安装**的 FreeCAD：不安装 FreeCAD、不装插件、不改用户偏好、不连接正在运行的 GUI。
  显式路径用 `--freecad PATH` 或环境变量 `CAD3_FREECAD`。
- 输入脚本是你写的可信本地程序，不是沙箱里的不可信代码。

## 脚本拿到什么

运行时在独立 FreeCAD 进程里执行脚本，命名空间注入：

- `App`（FreeCAD 模块）、`doc`（本工程的活文档）
- `PROJECT_DIR`、`RUN_DIR`（`pathlib.Path`）、`__file__`、`__name__ == '__main__'`
- 可 `import Part / Sketcher / PartDesign ...`，也可 import 工程内模块
- `from cad3.parts import add, declaration` 可插入支持的标准件代理，并将接口按 Placement 变换到全局坐标。完整目录流程见项目 [CATALOG.md](../../../CATALOG.md)。

默认打开 `models/main.FCStd`（存在时），否则新建 `Model`；**优先修改传入的 `doc`**，不要每次从零重建。
可选 `cad_outputs = ['Body']`（对象名或对象）指定导出/渲染对象；不给则由运行时挑 Body Tip / 独立结果实体，跳过草图与基准面。

## 执行成功 ≠ 制造正确

- `ok` / 退出码 0 只表示执行与协议成功，**不判断设计或制造是否正确**。
- 必须核对本次运行的完成记录：记录 `status` 要是 `completed` 且匹配本次 `run_id`；脚本异常或 `SystemExit`、信号退出、超时都算失败——**记录在失败时也会被写出，进程退出码为 0 同样不能单独信**。
- C++ 层崩溃（如 SIGSEGV）绕过 Python 异常处理，只能从进程退出码与日志看出来。
- 原生 `Invalid` / `Error` 对象和计算警告必须显式报告，不要静默接受，也不要另造通用门控。
- 数值断言按任务自己写；不要发明"通用实体数 / 孔数 / 体积门槛"。

## 选择建模表达

供人继续设计和反馈的非标件默认保留有意义命名的原生特征树；临时包络可以用冻结外形，标准件优先用库。建模前读 [references/modeling-guidance.md](references/modeling-guidance.md)，判断是否还需逐实例反馈。特征树本身不赋予阵列实例独立名称，`FaceN` / `EdgeN` 不是稳定 ID；逐个指认的孔/槽还需语义清单或编号视图，接口声明要随参数和 Placement 同步。

## 观察是只读

- `inspect` 读结构、参数与表达式、草图求解诊断、原生错误、几何指标；`--format code` 是**只读描述**。
- `render` 输出 BRep 网格化 + VTK 离屏着色 PNG，**不是原生 GUI 视口截图**，不保留选中态与界面标注。
- 数字与图像要交叉核对：数值全通过也拦不住坐标系/叠图错误；近似结果必须标注方法。
- 观察 ≠ 逆向：没有"把任意工程自动还原成完整可执行 Python"的内置出口。

## 参数修改要走依赖链

**要求"改表格就更新"的模型，只用原生对象 + 表达式**（`Part::Box/Cylinder/Cut/Fuse`、PartDesign 特征，尺寸与 `Placement` 挂表达式）。实测（FreeCAD 1.1.3）：

- 工程自带的 **FeaturePython 重开后失效**：恢复文档时只允许 FreeCAD/已装插件的模块，工程模块被拦截，Proxy 变 `None`，改表格几何不动。
- 脚本 `Part.makeBox(...)` 再塞进 `Part::Feature` 得到的是**冻结形状**；自定义属性挂表达式也不会重建几何。能让它随参数动的只有 `Placement` 表达式。
- 验证方式：保存后**重新打开**文件，只改表格单元格 + `recompute()`，确认几何真的变了（不要只在建模的同一进程里验证）。

- 参数放 `Spreadsheet`，用别名 + 表达式驱动约束与特征属性；改源单元格后 `doc.recompute()` 才传播。
- 表达式驱动的量：`setDatum` 会**静默 no-op**（调用成功但值不变）；应改源（单元格/表达式），或先 `clearExpression`（注意它不恢复原值）。
- 未 `recompute` 时旧 `Shape` 与 `State` 会给过期却看似正常的数字；先重算，再让几何与驱动参数交叉核对。

## 看内部与查干涉

- 外观图看不到轴穿孔、轴承座、螺母这类内部配合。沿关键轴线用 `cad3 render --section AXIS=MM` 切开看：切掉朝向相机的一半，零件间体积重叠画红，JSON 的 `overlaps` 同时列出。
- 装配干涉用 `A.common(B).Volume`（配合面建成等尺寸时为 0，属接触）。**结果"不合理"时先怀疑输入**：操作数是否有效单一实体（`isValid()`、`len(Solids)==1`）、孔/刀具体是否沿正确的轴、位置是否对——然后切剖面看。不要因为数字难看就改用更弱的判据。
- 红色/重叠为 0 只是观察，不是强度、公差或运动过程的证明；只查了你摆的那几个位置。

## 按需参考

| 何时读 | 文件 |
|---|---|
| 运行协议、失败判据、隔离、输出对象选择 | [references/runtime.md](references/runtime.md) |
| 特征树、参数/表达式链、草图求解诊断与编号基准 | [references/state-sketch.md](references/state-sketch.md) |
| 距离/截面/圆柱候选测量、无头图像、投影与剖面 | [references/measurement-visual.md](references/measurement-visual.md) |
| 可直接改写的脚本样例 | [build_plate.py](../../../examples/build_plate.py)、[edit_plate.py](../../../examples/edit_plate.py)、[inspect_custom.py](../../../examples/inspect_custom.py)、[fail.py](../../../examples/fail.py) |

实测证据与边界汇总见 [docs/feedback.md](../../../docs/feedback.md)（环境：FreeCAD 1.1.3 / macOS arm64 / `freecadcmd`）。

## 明确不声称

- 不做 GUI 桥接：命令行不接管、不截取原生视口。
- 不做 FCStd 的语义自动合并；`diff` 只是文件差异。
- 不承诺把任意既有工程完整迁移或逆向成参数化脚本。
