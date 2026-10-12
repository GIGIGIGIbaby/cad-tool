# CAD 工具 V3

**原生 FreeCAD + 模型可读的反馈 + Git 设计快照。**

这是一个全新的小项目，不兼容旧 `.456d`，不依赖 build123d，不接管用户已打开的 FreeCAD GUI。模型直接写 FreeCAD Python；工具负责运行、观察、保存证据和版本恢复，而不是再造一套 CAD API。

## 需要什么

- 主机 Python **3.11+**（主机代码只有标准库依赖）。
- 已安装 **Git**。
- 已安装 **FreeCAD**。本机开发基线为 macOS arm64 的 FreeCAD 1.1.3；其它平台路径有探测逻辑，但不能据此视为已经实测。
- 着色预览需要 FreeCAD 运行环境中的 VTK。本机应用已自带；缺少时工具会报告，不会擅自安装。

无需安装本 Python 包即可使用：

```bash
python3 cad3.py --help
python3 cad3.py doctor
```

若需安装命令，先在自己选择的 Python 虚拟环境中执行：

```bash
python -m pip install -e .
cad3 --help
```

FreeCAD 在它自己的进程和 Python 中运行，不需要把 FreeCAD、Qt、OCCT 安装进主机 Python。可通过 `--freecad /path/to/freecadcmd` 或 `CAD3_FREECAD` 指定入口。

## 五分钟试用

以下命令在本目录执行；目标目录必须不存在或为空。不会安装插件或修改全局 Git 配置。

```bash
python3 cad3.py init /tmp/cad3-demo --name "带孔板试验"
cp examples/build_plate.py /tmp/cad3-demo/src/main.py
cp examples/edit_plate.py /tmp/cad3-demo/src/edit_plate.py
cp examples/inspect_custom.py /tmp/cad3-demo/src/inspect_custom.py
cp examples/fail.py /tmp/cad3-demo/src/fail.py

python3 cad3.py run -p /tmp/cad3-demo --step
python3 cad3.py inspect -p /tmp/cad3-demo --format code
python3 cad3.py render -p /tmp/cad3-demo --views iso,top
python3 cad3.py render -p /tmp/cad3-demo --section y=10   # 剖面：切掉朝向相机一半，零件重叠处标红
python3 cad3.py commit -p /tmp/cad3-demo -m "带孔板初版"

python3 cad3.py run src/edit_plate.py -p /tmp/cad3-demo --step
python3 cad3.py commit -p /tmp/cad3-demo -m "长度改成100毫米"
python3 cad3.py diff HEAD~1 -p /tmp/cad3-demo
python3 cad3.py log -p /tmp/cad3-demo

# 工作区干净时恢复旧文件；HEAD不移动，回退本身可以再次提交。
python3 cad3.py restore HEAD~1 -p /tmp/cad3-demo
python3 cad3.py inspect -p /tmp/cad3-demo --object Body --detail
python3 cad3.py status -p /tmp/cad3-demo
```

所有命令默认输出 JSON，只有显式 `inspect --format code` 输出只读的代码式观察视图。`ok=true` 表示该命令的执行协议成功，**不是设计、强度或制造合格证明**。原生错误/警告、断言与观察结果需要另行阅读。

## 工程是什么

一个普通目录：

```text
cad3.json                 项目配置
src/                      原生 FreeCAD Python 脚本和模块
inputs/                   显式输入依赖
models/                   当前 FCStd 和可选 STEP
artifacts/latest/         对应模型的状态、报告、日志和可选预览
notes/                    需求、设计说明等
.cad3/                    被 Git 忽略的运行现场、独立配置和锁
.git/                     真正的 Git 仓库
```

工具创建独立 Git 仓库，不悄悄使用父目录的源码仓库。提交是当前文件的快照，不会重新建模。失败运行的诊断保留在返回的 `run_dir`；成功运行的重要证据进入可版本化的 `artifacts/latest/`。

## 模型怎样工作

脚本获得这些名字：

- `App`：FreeCAD 原生模块。
- `doc`：当前工程文档；已有文档则打开，没有则新建。不会每次自动推倒重建。
- `PROJECT_DIR`、`RUN_DIR`：工程目录与本次运行目录（Path）。
- `__file__`、`__name__`：标准脚本上下文。

示意：

```python
import Part

box = doc.getObject("Box") or doc.addObject("Part::Box", "Box")
box.Length = 40
box.Width = 20
box.Height = 10
cad_outputs = ["Box"]        # 可选：明确哪些对象是交付实体
# worker会在脚本完成后重算、观察和保存；脚本中测量前仍应显式recompute。
doc.recompute()
assert abs(box.Shape.Volume - 8000) < 1e-6
```

更完整的原生草图、参数、Pad/Pocket 示例见 [build_plate.py](examples/build_plate.py)。增量修改见 [edit_plate.py](examples/edit_plate.py)；直接使用原生测量见 [inspect_custom.py](examples/inspect_custom.py)。

`cad_outputs` 可以指定对象名或对象；未提供时工具尝试选择 Body/独立结果实体，避免把中间特征再次计入。复杂装配应显式指定输出对象并检查结果，不能把启发式选择当作设计语义推理。

## 观察，而不是反编译

- `inspect`：紧凑对象结构、依赖、原生状态、输出几何。
- `inspect --object 名称 --detail`：查看指定对象的更多属性、表达式与草图诊断。
- `inspect --format code`：便于模型阅读的**只读**表达，不是完整重建脚本。
- `render`：真实几何的 VTK 着色图；**不是原生 GUI 截图**，不承诺保留界面标注或选中态。
- `render --section AXIS=MM`（可重复）：轴向剖面。看不见的内部（轴穿孔、轴承座、螺母）靠它看；零件之间的体积重叠画成红色，并在 JSON 的 `overlaps` 里列出。
- `mates`：读取 `notes/interfaces.json`，先检查声明的接口、配合关系和装配连通性；有模型时再在 FreeCAD 进程中核对圆柱面/平面是否真实存在。结果是 `PASS/WARN/FAIL/UNKNOWN` 的设计反馈，命令的顶层 `ok` 仍只表示运行协议成功，不是机械验收。
- 该检查器的接口格式、轴承示例、V2 标准件目录迁移边界和分阶段路线见 [声明式机械接口设计](docs/interfaces-design.md)。
- 其他测量/求解工作流仍由模型通过原生 Python API 组织，不要求固定建模顺序。

## 执行与恢复边界

- 每次调用使用独立 FreeCAD 进程和配置目录，不使用用户的正在运行的 GUI。
- 退出码为0不是成功的充分条件；运行ID、显式完成记录、结果与退出状态必须吻合。
- Python异常、进程崩溃、超时或缺少完成记录时，不发布候选模型。
- 正常工具写入串行化；发布前检查原模型是否被外部修改。**这不是针对用户任意脚本的安全沙箱**，脚本具有当前用户文件访问权限，也不锁定人工GUI编辑。
- `restore` 拒绝脏工作区和可能冲突的未跟踪工程内容。先提交WIP或自行使用Git整理工作区；工具不提供静默强制覆盖。
- Git diff 是文件差异：JSON/脚本可读，FCStd/STEP可能是二进制差异。没有承诺任意CAD工程的语义合并。
- 工程依赖应放入 `src/`、`inputs/` 并明确记录。任意外部链接或第三方FeaturePython插件不能只靠复制一个FCStd保证可重现。
- 不打开来源不明的工程；FreeCAD安装版本的安全更新由用户管理，本工具不会自动下载或升级应用。

## 给模型的 Skill

入口是 [freecad-v3 Skill](.agents/skills/freecad-v3/SKILL.md)。它是一份短说明和按需索引，不把整个FreeCAD文档灌入上下文，也不强制每个特征走固定检查流程。执行路径和API注意事项分别保存在 `references/` 中。

安装Skill只需要让使用的客户端发现该目录；本项目不自动修改任何客户端配置，也不依赖某一家模型平台的专有PTC功能。

## 测试

单元测试（不需要FreeCAD）及本机真实集成测试的入口：

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
CAD3_TEST_FREECAD=/Applications/FreeCAD.app/Contents/Resources/bin/freecadcmd \
  PYTHONPATH=src python3 -m unittest discover -s tests -v
```

集成测试使用临时新工程，不改当前源码仓库的模型。它们检查参数化构建、重新打开、编辑、版本恢复、失败保护和图像产生；不代替机械工程验收。

## 暂不做

旧`.456d`或build123d脚本自动迁移、GUI/MCP会话桥接、CAM/FEM专用封装、自动安装标准件插件、通用几何语义diff/merge、制造合格认证。STEP仍可作为交换格式，但导入STEP不会凭空恢复原设计的草图和特征历史。

实现契约见 [ARCHITECTURE.md](ARCHITECTURE.md)，反馈含义见 [反馈文档](docs/feedback.md)。
