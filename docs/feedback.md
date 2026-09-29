# cad-v3 反馈能力与边界

本文说明 cad-v3 的"反馈回路"：一次运行给出什么证据、怎么读、哪些判断被实测修正过、哪些事现在不做。
它是 [ARCHITECTURE.md](../ARCHITECTURE.md) 运行契约的配套说明，配套技能入口见 [.agents/skills/freecad-v3/SKILL.md](../.agents/skills/freecad-v3/SKILL.md)。

## 1. 证据来源与范围

- 环境：**FreeCAD 1.1.3 / macOS arm64 / `freecadcmd`**（`App.GuiUp == 0`）。
- 方式：全部探针在**隔离临时目录**中执行，使用 `FREECAD_USER_HOME` + `--user-cfg/--system-cfg`，并核验实际配置路径落在运行目录内；未修改仓库模型、未安装依赖、未接管正在运行的 GUI。
- 偏好文件复核：真实用户的两份配置内容哈希在实验前后一致。
- 结论只描述所测版本与样本；不是整个 FreeCAD 生态的完备性证明，也不构成制造合格或任务成功率认证。
- 子报告的结论若有冲突，以**主报告复核后的修正结论**为准（本文第 4 节即这些修正）。

## 2. 反馈通道与契约

| 通道 | 产物 | 能回答 | 不能回答 |
|---|---|---|---|
| 运行证据 | run id / run 目录 / stdout / stderr / 完成记录 / `ok` | 本次脚本是否按协议跑完、失败在哪 | 设计是否正确 |
| 状态观察 | 对象、依赖、参数与表达式、草图求解诊断、原生错误/警告、几何指标 | 模型现在是什么、哪个参数驱动谁、草图还剩多少自由度 | 需求是否被满足 |
| 只读代码视图 | `inspect --format code` | 紧凑、可读的状态描述 | 不是可执行重建脚本，不能拿去"恢复"工程 |
| 视觉观察 | `render` 的 iso/top/front/right PNG、可选投影/剖面 | 形状、孔位、厚度、干涉的直观确认 | 不是 GUI 视口截图；无选中态、无界面标注 |
| 版本 | `status/log/diff/commit/restore` | 文件级历史与回滚 | `diff` 是文本/二进制文件差异，不是几何语义差异 |
| 导出 | STEP / STL | 交付与外部检查 | 不导出表达式、约束或特征语义 |

## 3. 怎么判断一次运行

1. **进程退出码**为 0，且不是信号退出（如 `-11`）。
2. **本次运行的完成记录 `status == "completed"`** 且匹配 `run_id`/脚本哈希——记录在失败时也会写出（`status="failed"`），所以"文件存在"和"退出码 0"都不构成成功。
3. 脚本无异常 / `SystemExit`、未超时。`sys.exit(0)` 是失败，不是成功。
4. 原生 Invalid/Error 对象与计算警告被显式列出。
5. 读一次 `inspect`（`--detail` 按需）确认参数、表达式、草图诊断是你预期的状态。
6. 有图就**看图**：写下每张图里看到了什么，再和数值比。
7. 只在以上都过之后 `commit`。

失败的运行不发布模型：候选 FCStd 不覆盖 `models/`，失败日志留在 `.cad3` 并随返回值给出。

## 4. 被实测修正过的判断（不要再用旧口径）

1. `solve() == 0` **不等于**完全约束：实测 `solve=0`、`DoF=3`、`FullyConstrained=False`。判完全约束用 `DoF == 0`，并同时看两个诊断列表。
2. 诊断编号不是"无法映射"：冲突/冗余列表是 **1-based**，`python_index = reported_id - 1`（把矛盾约束放在索引 0 与 3，报告 `[1, 4]`）。
3. "冲突/冗余只能读 stderr"不成立：`ConflictingConstraints` / `RedundantConstraints` 是结构化属性；只有"Invalid solution"这类无效解诊断仍只出现在 stderr。
4. "macOS 无法无头 3D 着色"不成立：BRep 网格化 + 安装包自带 VTK 离屏渲染成功；但它**不是**原生视口截图。
5. "数值全通过就说明图对"不成立：实测抓到过"面积正确但填充画在轮廓外"的坐标系混用错误；数字与图像必须交叉核对。
6. "一实体必须一壳"不成立：带封闭内腔的合法实体是 1 Solid / 2 Shells；**不要用壳数当实体门控**。
7. "圆柱面数 = 孔数"不成立：沉头孔 1 孔 2 面、圆柱凸台 0 孔 1 面、圆角半径与孔径相同。孔只能按轴线+半径+拓扑/法向判**候选**，不能计数。
8. 脚本对象（FeaturePython）重开不必然降级：把 Proxy 类放进**可导入模块**并在另一进程 `sys.path` 加上该目录，可恢复参数化行为；快照本身装不下实现，所以要登记模块与版本。

## 5. 现在明确不做的事

- **不做 GUI 桥接**：命令行不接管、不连接、不截取正在运行的 FreeCAD GUI；`ViewObject` 在无 GUI 时是 `None`。
- **不做 FCStd 的语义自动合并**：没有"几何语义 merge"，只有 Git 文件快照与回滚。
- **不做任意旧工程的完整迁移/逆向**：未找到"任意文档 → 完整可执行 Python"的内置出口；`exportGraphviz`（结构 DOT）与 `csg/scad`（几何顶点转储）都不是重建器。
- **不提供通用几何门控**：实体数、孔数、体积阈值都由任务作者自己断言，运行时不发明。
- **不承诺全局最小壁厚 / 碰撞 / 强度工艺认证**：局部射线或静态相交不构成这些结论。

## 6. 本地复核方式

用隔离启动器复跑，不要在宿主 Python 里 `import FreeCAD`，也不要直接用 `freecadcmd` 裸跑：

1. 写一个 bootstrap：在 FreeCAD 进程内 `exec` 目标脚本，`try/except BaseException` 写完成记录，`finally` 落盘；
2. 宿主侧用 `FREECAD_USER_HOME`、`--user-cfg`、`--system-cfg`、`HOME`、`PYTHONIOENCODING` 起子进程，清掉 `PYTHONHOME`/`PYTHONPATH`/`VIRTUAL_ENV`；
3. 校验配置路径都在运行目录内，收集 stdout/stderr/退出码/完成记录，四者一起判成败。

`examples/build_plate.py` → `examples/edit_plate.py` → `examples/inspect_custom.py` → `examples/fail.py` 按这个顺序覆盖：建树、参数传播与独立解析断言、只读测量、失败不发布。
