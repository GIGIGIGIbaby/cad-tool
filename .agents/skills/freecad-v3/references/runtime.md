# 运行协议、失败语义与隔离

来源：`cad-v3/ARCHITECTURE.md` 的运行契约 + 本轮 FreeCAD 1.1.3 macOS 隔离实测。
本文件只讲"脚本怎么被跑、什么算成功、边界在哪"。

## 1. 入口与工程布局

```bash
cad3 doctor [--freecad PATH]
cad3 init PATH [--name NAME]
cad3 run [SCRIPT] [-p PROJECT] [--freecad PATH] [--timeout SECONDS] [--step]
cad3 inspect [-p PROJECT] [--object NAME] [--detail] [--format json|code]
cad3 render [-p PROJECT] [--views iso,top,front,right] [--width N] [--height N]
cad3 status|log|diff|commit|restore        # commit -m MESSAGE；restore REV；diff [REV]；log --limit N
```

```
cad3.json                 # schema cad3.project/v1；name；document=models/main.FCStd；entrypoint=src/main.py
src/                      # 原生 FreeCAD 脚本与工程内模块
inputs/                   # 显式输入依赖
models/                   # 原生 FCStd，可选 STEP
artifacts/latest/         # 最近一次成功运行的证据、状态与日志
notes/                    # 意图/设计笔记
.cad3/                    # 忽略：运行目录、隔离配置、锁
.git/                     # 真正的 Git，不是自造版本引擎
```

- 除显式 `--format code` 外所有命令输出 JSON；CLI 在 `ok=false` 时退出非零。
- `init` 只接受不存在或空的目标目录，绝不静默接管父级 Git 仓库。
- `commit` 只快照真实文件，**不重跑建模**；无变化时明确拒绝。
- `restore` 拒绝脏工作区，从已核验版本恢复文件但不移动 HEAD，下次提交记录回滚。

## 2. 每次运行发生什么

1. 运行时定位 FreeCAD 可执行文件（explicit → `CAD3_FREECAD` → 常见安装路径/PATH）。**宿主 Python 绝不 import FreeCAD，也不安装它。**
2. 独立进程执行打包的 `worker.py`，通过 JSON 请求文件传参；每次运行有自己的 run 目录与日志文件。
3. 每次运行设置 `FREECAD_USER_HOME`、`--user-cfg`/`--system-cfg`、`HOME`、`PYTHONIOENCODING`，并清掉宿主的 `PYTHONHOME`/`PYTHONPATH`/`VIRTUAL_ENV`；执行前核验实际配置路径确实落在本次 run 目录内。
4. 打开工程配置的 FCStd（存在时），否则新建 `Model`；把 `App` / `doc` / `PROJECT_DIR` / `RUN_DIR` / `__file__` / `__name__='__main__'` 注入脚本命名空间后执行。
5. 脚本返回后：`recompute` → 观察状态 → 保存候选 FCStd → 可选导出 STEP → 写观察与运行证据。

**失败不发布**：任何一步不满足协议，候选模型不覆盖之前已发布的 `models/`；失败的运行日志留在 `.cad3` 并随返回值给出。

## 3. 成功判据（缺一不可）

| 判据 | 说明 |
|---|---|
| 进程退出码 0 | 非零、信号退出（如 SIGSEGV 的 -11）一律失败 |
| **完成记录 `status == "completed"`** | 记录协议 `cad3.completion/v1`，必须匹配本次 `run_id`/`action`/脚本哈希；**记录即使失败也会被写出（`status="failed"`），所以"文件存在"不等于成功**，退出码 0 也不等于成功 |
| 脚本无异常 / `SystemExit` | 运行时用 `catch BaseException` + traceback 兜底；`sys.exit(0)` 也**绝不**当成功 |
| 未超时 | 超时只杀自己进程组；其他进程不受影响 |

在实测中见过两次 SIGSEGV：C++ 层 `Unknown exception`，`try/except BaseException` 拦不住、`finally` 不执行、完成记录根本没写、stdout 甚至为 0 字节。
所以：**不能只看退出码，也不能只看完成记录是否存在；要同时看退出码、记录里的 status/run_id、以及 stdout/stderr 文件。**

## 4. 写模型时要记住的

- 先改 `doc`，再 `doc.recompute()`；表达式写入后不重算不生效。
- 脚本可以把另一个**活的原生文档**赋回 `doc` 作为本次结果（运行时会改用它并记一条 warning）；赋值非文档对象是协议错误。
- `sys.path` 会自动加入脚本目录、`<project>/src` 与工程根，所以工程内模块可以直接 import。
- `setExpression` 接受非法表达式**不报错**，错误推迟到重算；`clearExpression` 不会恢复表达式生效前的原值。
- 跨文档 `App::Link`：未保存属主文档时赋值会抛 `RuntimeError: Owner document not saved`；目标文件被移走后重开，`recompute` 不报错、`State` 仍 `Up-to-date`，但用的是**缓存旧形状**——最危险的一类静默过期。
- 输入 FCStd 在运行期间被外部改动，运行时会拒绝发布本次结果。

## 5. 输出对象怎么选（`cad_outputs`）

- 给了名字/对象就用给的；没给则取 Body 的 Tip 与独立结果实体，不重复计入中间/隐藏输入。
- 不把无限基准面、草图、Origin 当作有体积的输出；空文档返回 `[]`。
- STEP 导出与渲染都基于这组对象，所以选错对象会直接导致空图或空 STEP。

## 6. 隔离与边界（不要过度承诺）

- 进程与用户配置隔离是为了不污染用户环境，**不是恶意代码沙箱**：脚本是可信本地程序。
- 不安装插件、不改用户偏好、不接管正在运行的 GUI；无 GUI 时 `App.GuiUp == 0`、`obj.ViewObject is None`。
- 原生依赖：脚本只能使用 FreeCAD 自带模块与工程内可导入模块；宿主解释器里没有 `FreeCAD`。
- `ok` 表示"执行与协议成功"，**不表示几何有效或设计正确**。原生 Invalid/Error 对象与计算警告要显式报出来。
- 计算警告、无效原生状态必须可见；不要用统一门控（实体数、孔数、体积阈值）替代按任务的断言。

## 7. 观察命令的读法

- `inspect` 默认紧凑，`--detail` 展开；`--object NAME` 只看一个对象。字段含单位与截断标记；不序列化不透明的 C++ 对象、Measure 注册表、无界 Shape/Proxy 数据或 NaN/Infinity。
- **不要为了观察而对任意草图调 `solve()`**：`recompute` 后读诊断即可。
- `inspect --format code` 是紧凑只读描述，头部即声明"不是可执行重建代码"。
- `render` 的视图名固定为 `iso/top/front/right`；VTK 缺失或对象无几何是可操作错误，不是静默空图。
- `render --section y=12.5`（可重复）输出 `section_<n>_<axis>.png`；只给 `--section` 时不再渲染四个默认视图。每张图的元数据含 `objects_cut_by_plane` 与 `overlaps`（红色区域）。
