# AGENT-FEEDBACK-001：RV63 建模会话中的 cad-v3 工具使用反馈

- 状态：**开放**（待更强模型评估必要性/优先级）
- 来源：2026-10-20，RV63 / NMRV063 整机建模会话；**agent 作为 cad-v3 的用户**
- 性质：使用反馈与需求草案，不是产品承诺，也不是模型合格判定
- 影响面：参考/检索链路、特征观察、检查与导出、输出/视图、生成器工作流、提交、标准件
- 关联：[HUMAN-FEEDBACK-001](HUMAN-FEEDBACK-001-feature-addressability.md)（可寻址性）、
  [feedback.md](feedback.md)（既有实测边界）、[CATALOG.md](../CATALOG.md)、
  [.agents/skills/freecad-v3/SKILL.md](../.agents/skills/freecad-v3/SKILL.md)
- 本次模型工程：`/Users/liwuzhan/Desktop/CAD ToolS测试/rv63-worm-reducer`
  （15 个零件、21 项自检、105 对干涉、STEP 往返无 invalid）

**一句话结论：建模能力不是这次的主要瓶颈；最大瓶颈是“参考图纸/照片 → 可追溯参数”的
输入链路，其次是“几何 → 可读证据”的输出链路。**

> 阅读提示：本文中的“我”是建模 agent。凡属 agent 自身使用不当的（如没先用 catalog、
> 没先用 mates），会单独标注，避免把工作流问题误记成工具缺陷。

## 1. 本次会话基线

### 1.1 任务与产物

| 项 | 值 |
|---|---|
| 任务 | RV63 / NMRV063 蜗轮蜗杆减速机整机（壳体、蜗杆、30 齿蜗轮、输出轴、轴承、油封、后盖、油塞、螺钉） |
| 最终规模 | 15 个具名 `Part::Feature`；21 项自检；105 对 `common()` 干涉 |
| 关键尺寸 | 中心距 63；轴高 72；144×174 外形；地脚 85×100；输出 4×M8（95 方阵）+ 8×M6 盖螺钉；输入 4×M8@φ95、φ80 凹止口、φ19×40；i=30 |
| 最终自检 | 21/21 全过；105 对干涉 0；6 相位啮合 0；STEP 重导入无 invalid；估算质量 9.04 kg |
| 版本 | `1cbbeaa`（模型修订）、`8268d24`（README 补充）；工作区 clean |
| 参考来源 | artesk、诺广、格鲁夫、上海科麦 RV 目录页、驱能实物图、津驰、中国传动网、val-nmrv、fl-motor-nmrv |
| 主要迭代 | 约 15 次 `cad3 run` / `--step`；多次 `render`、自定义相机、剖面；一轮用户评审后的孔位/圆角/多面孔修订 |

### 1.2 顺手、值得保留的部分

1. **`cad3 run` 的隔离与速度**：单次 10–15 s，`freecadcmd` 在工程内跑，candidate 模型不污染
   `models/`；失败时 exit code 实测为 **1**。skill 提醒“不能只看 exit 0”（失败记录也会写出），
   这点仍然成立；两者不矛盾，值得在更高版本再核一次。
2. **`render --section x=0` / `y=0`**：这是本次最有用的观察通道。蜗杆/蜗轮啮合、键槽、
   轴承座、油封、干涉红块，基本都靠剖面看出来；`overlaps` 为空/非空的语义也清楚。
3. **`inspect` 的只读性与 native issues**：对象、依赖、几何、`invalid` 列表、`Face`/`Edge`
   警告，足够建立“模型现在是什么”的基线；`--format code` 适合快速浏览。
4. **`commit/log/restore`**：文件级快照可靠；每次修订能回看。
5. **`catalog` 不是空的**：`cad3 catalog search 6205` 能返回 `bearing.deep_groove.6205`
   （d25 / D52 / B15）。**这次我自己没先用它**，手搓了带滚道槽的简化环——这是我的流程
   问题，但也说明“标准件怎么插进自定义装配”的路径不够显眼/不够轻。
6. **skill 里的既有实测**（loft 用 ruled、圆柱面数≠孔数、section 只查摆放位置）确实省了
   时间；本次又新增了几条坑（见 R11）。

### 1.3 时间与上下文消耗（粗略）

- 从第一次检索到最终提交约 1 小时；`cad3 run` 的纯执行时间合计约 3–4 分钟。
- 大头在：找图、交叉核对图纸/目录/照片、读图、改孔位/圆角、写检查、读大 JSON。
- 单次 `cad3 run` 输出日志约 **24 KB**（`/tmp/run_final3.log`）；四视图 `render` 输出约
  **80 KB**（`/tmp/rh3.log`）。我每次都 `tail`/`grep`，而不是完整读。

## 2. 反馈总表

| ID | 主题 | 类型 | 一句话 | 建议优先级 |
|---|---|---|---|---|
| R1 | 参考图纸/照片没有入口 | 工具缺口 | 不能 `ref add/pdf/sheet/crop/overlay`，检索结果无法沉淀/对齐 | **P0** |
| R2 | 参数没有出处/冲突记录 | 工具缺口 | 参数散在对话和手工表里；`8-M8` vs `4×M8` 冲突只能靠照片裁决 | **P0** |
| R3 | 特征/孔表不可读 | 工具缺口 | `inspect` 只到几何，没有按轴线/半径去重的孔表/孔组 | **P0** |
| R4 | 干涉/质量/检查靠手写 | 工具缺口 | 105 对 `common()`、21 项断言、密度表都写在 `rv63.py` | **P0** |
| R5 | 输出太大 | 体验/上下文 | run 24 KB、render 80 KB；没有 `--brief` | **P0（快赢）** |
| R6 | 视图/相机/标注不够 | 工具缺口 | 固定 iso 在 −Y 侧；+Y 输入面要 import 私有渲染函数 | P1 |
| R7 | 导出没有回读验证 | 工具缺口 | 轴承在 FCStd valid、STEP 重导入 invalid，靠手动发现 | **P0/P1** |
| R8 | 生成器工作流没定位 | 工具/文档 | 复用 doc 会出 `Housing001`；没有 `--fresh`/`--set`/参数表 | P1 |
| R9 | `commit` 不含根文件 | 行为缺陷 | `README.md` 在根目录，commit 只 stage 配置目录 | **P1（快赢）** |
| R10 | 标准件/接口插入偏重 | 工具/文档 | 6205 在 catalog 里，但 `add/declaration/mates` 对简单件偏重 | P2 |
| R11 | 文档/示例/坑 | 文档 | 没有 assembly/checks/gear/ref 例子；新坑散落 | P2 |

> R1/R2 是用户已指出的“检索问题”的工具侧根因；R3/R4 是“模型能力变强后，工具没有同步
> 变强”的直接表现；R5/R9 是低成本快赢。

## 3. 详细条目

### R1 参考图纸/照片没有入口（P0）

**现象**

- 现在只能靠 `web_search` / `web_fetch`。`web_fetch` 对 PDF 直接返回
  `Error: unsupported content type "application/pdf"`；我改用 `curl` + `pypdf`，但**没有
  页面渲染**，无法看图纸页。
- 图片要自己下载、PIL 裁剪、放大；没有 `refs/` 目录，没有“这张图对应哪个尺寸”的记录。
- 没有把参考图叠到模型视图上的手段；孔位/圆角只能靠肉眼对照图片和渲染。

**本次证据**

- PDF 目录被拒后，最终可用的 RV 目录页是上海科麦页面里的一张 JPEG；063 图纸是 artesk
  的 GIF；实物照片来自中国制造网/津驰/传动网。整个链路跨了 5+ 个站点、十几次抓取。
- 输出面 `8-M8×14`（artesk 063 图纸）与 `PE=4×M8×14(n=4)`（RV 目录页）冲突；最后靠
  实物照片（4 个角孔 + 8 个轴承盖螺钉）定案。
- 输入法兰孔从 4×M6@φ100 改为 4×M8@φ95；依据是目录 `H=95` + 实物照片的 PAM 法兰螺钉。
- 圆角 R12 → R2.5、多面固定孔，也都是照片给出的信息，图纸没直接写。

**影响**

- 检索质量决定参数质量；参数错了，后面的几何/检查再漂亮也没用。
- 参考图不可回看、不可引用，用户评审时只能听我复述“照片里是……”。
- 无法做“图纸/照片 ↔ 模型”的比例对齐，孔位/轮廓的小偏差发现得晚。

**建议**

新增 `cad3 ref` 子命令与 `refs/` 目录：

```bash
cad3 ref add <url|file> --name rv063_catalog
cad3 ref pdf refs/rv063.pdf                 # PDF -> PNG（页面级）
cad3 ref sheet refs/                        # 带标签/来源的 contact sheet
cad3 ref crop refs/photo.jpg --box x,y,w,h --zoom 2 --out refs/crop.png
cad3 ref overlay refs/front.png --view front \
    --scale 0.72 --offset 12,-4 --opacity 0.35
```

`ref overlay` 的最小闭环：正交渲染当前模型 → 按已知尺寸（如 144 宽）标定参考图比例 →
叠加半透明参考图 → 可选把 `inspect --features` 的孔心打点 → 输出 comparison PNG。
它只作为观察辅助，不作合格判定。

**验收标准**

1. 给定一张 063 正视参考图，`ref overlay --view front` 能按 144 mm 宽对齐，输出叠加图；
2. 叠加图上能看出输出 4 角孔 + 8 盖螺钉、输入 4 孔是否与模型一致；
3. PDF 目录页能转 PNG 并在 `refs/` 里带来源/日期；
4. 不引入 GUI 桥接、不改用户 FreeCAD 偏好；
5. 不把“叠加对齐”写成“尺寸合格”。

**备注**

- 这不是“在 cad3 里做 web 搜索”；搜索仍由 agent 的 web 工具完成。cad3 负责**下载后的
  落地、裁剪、比例对齐、对比**。
- 校准方式可以是：已知两孔中心距 / 已知外形宽 / 手动两点标定；需要定义清楚误差来源。

### R2 参数没有出处、没有冲突记录（P0）

**现象**

- `P` 是 Python dict，只有值；`notes/design.md` 的来源表是事后手写的。
- 同一个尺寸在不同来源冲突时，没有结构化的“值 / 来源 / 置信度 / 备注”。
- 检查只验证“几何等于脚本里的值”，不验证“脚本里的值等于来源”。

**本次证据**

- `8-M8×14` vs `4×M8×14(n=4)`；输入 `4×M6@φ100` vs `4×M8@φ95`；圆角 R12 vs 照片方箱；
  多面孔是否每面都有——都需要人工在对话里裁决。
- 最终 `design.md` 的 §2 表是手工整理的；`notes/verification.json` 只有 checks，没有 source。

**建议**

- `params.json` / `params.csv`：每条 `alias, value, unit, source, confidence, note`；
  模型脚本读它，或至少由 `cad3 params --check` 对比脚本里的 `P`。
- `refs/manifest.json`：`source_id → url/file, fetched_at, used_for`。
- `cad3 params --table` 生成 Markdown 设计表；`--conflicts` 列出同尺寸多来源不一致。
- 报告中带 `source` 引用（例如“4×M8@φ95 ← 目录 H=95 + 照片 PAM 法兰”）。

**验收标准**

1. 每个关键尺寸都有 source；缺 source 时 `--check` 给出警告；
2. 两个来源冲突时能被列出（不自动裁决）；
3. `--table` 生成的表可贴进 design.md；
4. 模型脚本与 params 不一致时能报出来（或明确声明“脚本是唯一真值源”）。

**备注**

- 这不一定要求改运行协议；可以先做“旁路清单 + 检查”，再决定是否让脚本读 params。
- 与 HUMAN-FEEDBACK-001 的“稳定编号”不同：这里是**参数级 provenance**，不是几何元素编号。

### R3 特征/孔表不可读（P0）

**现象**

- `inspect` 能给出对象、面/边、几何，但没有“孔/凸台/孔组”的语义层。
- 我自己写了 `hole_axes` / `hole_axes_axial`（`src/rv63.py:621–665`）按 `(radius, axis line)`
  去重；写了 4 角孔、8 盖螺钉、输入 4 孔、顶/背/侧/底、地脚的分类。这些逻辑每个模型都要重写。
- 盲孔如果从面内 0.01 mm 起切，几何上会留一层表皮：`inspect` 数得到圆柱面，`render` 却
  看不到孔；我调试了一轮才把 `add_tap` 改成从面外 1 mm 起切。

**建议**

```bash
cad3 inspect --features          # 圆柱/平面/圆角候选，按轴线和半径分组
cad3 inspect --holes             # 孔表：轴线、半径、深度、是否贯通、所属面
cad3 inspect --patterns          # bolt circle / square / 阵列识别
```

输出至少包含：`radius, axis, center, axial_range, open_at, face_ref, kind(hole|boss)`,
以及按 `(radius, axis)` 去重后的计数。分类要用“材料侧/法向/拓扑”判候选，不能只数面
（与 feedback.md 第 4.7 条一致）。

**验收标准**

1. RV63 模型能报告：输出 4×M8（95 方阵）+ 8×M6@φ95、输入 4×M8@φ95、顶/背/侧/底各 4×M8、
   地脚 4×φ8.5；计数与 `verification.json` 一致；
2. 能标出“盲孔/未贯通”的孔（如早期 `add_tap` 的表皮）；
3. 输出既有人类可读表，也有 JSON；支持 `--brief`；
4. 不把“圆柱候选”直接叫“孔”，要带判据。

### R4 干涉/质量/检查靠手写（P0）

**现象**

- 105 对 `common()`、21 项断言、密度表都写在 `src/rv63.py` 的 `inspect_and_assert`
  （`src/rv63.py:666–836`，约 170 行）。
- `mates` 是接口声明级，不是“任意零件两两干涉”；这次没有装配接口声明，所以没用上。
- 没有 `cad3 check`；失败只靠脚本 `raise RuntimeError`，结果只在我的 `rv63_checks.json`。

**建议**

- `cad3 inspect --interference`：全组合 `common()`，输出 `[a,b,volume]`，支持 `--tolerance`,
  `--ignore`（接触面/配合对），默认 0 容差；
- `cad3 inspect --mass materials.json`：按对象/材料算质量；
- `cad3 check checks.py`：一个轻量 harness，提供
  `expect_bbox`, `expect_holes`, `expect_no_overlap`, `expect_mesh_clear`, `expect_mass`
  等 helper；输出 compact pass/fail 表 + `checks.json` + 非零退出码；
- 保留“任务自定义断言”，不发明通用几何门控（与 feedback.md §5 一致）。

**验收标准**

1. RV63 的 21 项检查可用 `cad3 check` 跑出同样的 21/21；
2. 全组合干涉表为空/非空能直接看到，不用读脚本；
3. 质量表能按材料算；
4. 失败时 exit code 非零，且报告可被 CI/下一轮 agent 读取。

### R5 输出太大、迭代时全是噪音（P0，快赢）

**现象**

- `cad3 run` 一次约 24 KB；四视图 `render` 约 80 KB；`inspect --detail` 更大。
- 我每次都用 `tail`/`grep` 找 `error`/`checks`，完整读会吃掉大量上下文。
- JSON 里很多是完整文档状态/每个视图的 objects 列表，不是“这次成功没有、错在哪、图在哪”。

**建议**

- `run --brief`：一行 `status` + `error` + `checks` 摘要 + `outputs/step/model` 路径；
- `render --brief`：只给 `path + overlaps + warnings`；
- `inspect --brief`：每对象 `name/type/valid/solids/volume/bbox`；
- `--json` 保留完整输出；`--errors-only` 用于 CI/循环；
- 在 JSON 顶部加 `summary`（status/errors/check 计数/paths），即使不用 brief 也能先看结论。

**验收标准**

1. `run --brief` ≤2 KB，包含成功/失败、错误首行、checks 计数、产物路径；
2. `render --brief` 只列路径/overlaps/warnings；
3. 完整 JSON 仍写入 run 目录，不丢信息。

**备注**

- exit code 我实测是 1，这点不用改；需要的是“人能一眼看到”。

### R6 视图/相机/标注不够（P1）

**现象**

- `render` 固定 `iso/top/front/right`；`iso` 方向是 `(+X,−Y,+Z)`
  （`src/cad3/rendering.py:47–56`），**看不到 +Y 输入面**。
- 我写了 `src/render_input.py`（75 行），直接 import `cad3.rendering` 的私有
  `_collect_meshes / _build_actor / _fit_camera / _combined_bbox / _write_png / _vtk`，
  才能出输入侧 3/4 和背面 3/4。私有 API 依赖是维护风险。
- palette 按对象顺序分配；密封圈/油塞拿到偏红的颜色，和 section 的干涉红容易混。
- 没有标注/孔位编号/尺寸；HUMAN-FEEDBACK-001 已经提出过编号图，这里补充“相机自由度”
  和“参考图叠加”。

**建议**

```bash
cad3 render --direction 0.8,1.0,0.55 --up 0,0,1
cad3 render --azimuth 35 --elevation 25
cad3 render --views iso,front --section x=0 --keep +
cad3 render --colors materials.json          # 语义色，红色保留给 overlap
cad3 render --annotate holes,ids             # 孔位编号/引线
```

把 `render_objects` 的公开 API 暴露出来（direction/up/width），不要逼用户 import 私有函数。

**验收标准**

1. 纯 CLI 能出 `+Y` 输入侧视图和 `−X` 背面 3/4；
2. `views + section` 一次调用；
3. `--keep` 能选剖面保留侧；
4. 标注图上孔位可读，红色不与零件色混淆；
5. 不依赖私有 `cad3.rendering._*`。

### R7 导出没有回读验证（P0/P1）

**现象**

- `cad3 run --step` 只负责导出；没有回读校验。
- 这次“两个环 + 8 球”的轴承在 FCStd 里 valid、体积正常；STEP 重导入 FreeCAD 后
  `isValid=false`，体积也变了。我手动写了 `Import.insert` + `isValid`/Volume 才发现，
  最后改成带滚道槽的单实体环。
- 最终 STEP 28 个形状对象 / 24 个实体 / 无 invalid，是靠手写脚本核的。

**建议**

```bash
cad3 run src/main.py --step --validate
cad3 export --format step,stl --validate
```

回读后按对象/bbox/体积匹配，报告 `valid, volume_cmp, bbox_cmp, solids`；`--strict` 时
非零退出。匹配策略要允许 STEP 把 compound 拆成多个 product。

**验收标准**

1. 一个已知 invalid 的 fixture（如相切球/环融合）能被 `--validate` 报出；
2. RV63 的 STEP 能报 15 个逻辑零件（STEP 可能拆成 28 个 product）/全部 valid/体积一致；
3. 不把“回读 valid”写成“可制造”。

### R8 生成器工作流没有明确定位（P1）

**现象**

- `run` 复用传入的 doc；忘了清空就出现 `Housing001`、`Housing002`。我在 `build_all` 里
  手写了 `for old in list(doc.Objects): doc.removeObject(old.Name)`。
- skill 写的是“只修改传入的 doc，不要每次从零重建”——这对 PartDesign 编辑是对的，对
  **装配生成器**恰恰相反。
- `P` 是 Python dict；没有 `--set`/`params.json`；设计表手写。

**建议**

- `cad3 run --fresh`：在空 doc 上执行；成功后再替换项目模型；失败不污染；
- `cad3.parts.upsert(doc, name, shape, label)`：按名替换，避免 `001`；
- `run --set fillet_v=3 --set module=3.5`；`params.json`；
- `cad3 params --table` 生成 Markdown；
- 文档把“编辑模式 / 生成器模式”分开，给出各自的 scaffold。

**验收标准**

1. 生成器脚本连续跑两次，不出现 `001` 重名；
2. `--fresh` 不会在失败时覆盖 `models/`；
3. `--set` 改一个尺寸，`run --brief` 的 bbox/checks 跟着变；
4. 参数表可生成。

### R9 `commit` 不含根文件（P1，快赢）

**现象**

- `cad3 commit` 的 scope 来自 `cad3.json` 的 `directories`；根目录 `README.md` 不算，
  所以 commit 后 `status` 仍显示 `?? README.md`，我最后手动 `git add`。
- 这不是大问题，但“commit 后还 dirty”会让人怀疑快照完整性。

**证据**

- `src/cad3/versions.py:354 _scope_paths()` 只遍历 `project.config["directories"]`；
- 本次 `2008572` commit 后 README 未跟踪，`8268d24` 才补上。

**建议**

- 默认把根目录下未被 `.gitignore` 的普通文件（`README.md`、`*.md` 等）纳入 scope；
  或提供 `cad3 commit --all`；
- 仍要保留“不吸收 scope 外已 staged 文件”的安全检查。

**验收标准**

1. 根目录新增 `README.md` 后，`cad3 commit` 能一次纳入并变 clean；
2. 不会误吸收 `models/` 之外的临时文件；`--all` 行为有明确文档。

### R10 标准件/接口插入偏重（P2）

**现象**

- `cad3 catalog search 6205` 有 `bearing.deep_groove.6205`；但插入路径是
  `cad3.parts.add` + `declaration` + mates 接口，对“自定义装配里的一个轴承”偏重。
- 我这次手搓了简化环，**这是我的使用问题**；但也说明 catalog 的“先拿到精确外形”的
  轻量路径不够显眼。
- `mates` 没有完整装配示例；接口声明怎么写、和 `checks` 怎么分工，不清楚；本次没用。
- catalog 已覆盖轴承、紧固件（如 `fastener.hex_bolt_metric`）等，但没有明显的油封/平键/
  油塞/IEC/PAM 电机法兰；且搜索是模糊匹配——`cad3 catalog search seal` 返回直线轴承、
  `search key` 返回直线执行器，缺少 `--category`/`--exact`/别名，查标准件的信噪比低。

**建议**

- `cad3 catalog instantiate 6205 --frozen --at x,y,z --axis ...`：先给精确 d/D/B 实体；
- 标准件（seal/key/plug/screw/motor flange）补目录；
- `mates --auto`：同轴/贴合/孔对孔的候选提案，再人工确认；
- 一个完整的 assembly + mates 示例。

**验收标准**

1. 一条命令插入 6205，d/D/B 与目录一致；
2. 示例装配能 `mates` 全 PASS；
3. `--auto` 至少能提出正确的同轴候选。

### R11 文档/示例/坑（P2）

**现象**

- 没有 assembly / checks / gear / ref overlay 的完整例子；`examples/` 只有板件。
- 这次新踩的坑散在会话里：`Vector.normalized()` 不存在、`makeLoft` B 样条过冲、
  盲孔 0.01 mm 表皮、STEP 相切融合 invalid、`removeObject` 重跑、section keep 方向、
  `Shape.common` 对 compound、`Face.Surface.Center` 只是轴线上一点。
- `mates` 的接口声明没有“装配级”例子；`CATALOG.md` 偏目录，不偏装配。

**建议**

- `references/freecad-pitfalls.md`：每条“现象 / 最小复现 / 正确做法 / 实测版本”；
- `examples/assembly_generator.py`（reset/upsert + 命名对象 + 检查）；
- `examples/checks.py`；
- `examples/ref_overlay.py`；
- `cad3.compat`：`normalize_vector`, `loft_ruled`, `cut_open_hole`, `upsert_feature`。

**验收标准**

1. 新 agent 只看 examples 能在 30 分钟内复现一个多零件装配 + checks；
2. 本文新增的坑都进 pitfalls；
3. 不需要 import 私有 `cad3.rendering._*` 就能出自定义视图。

## 4. 优先级、依赖与成本判断

| 优先级 | 条目 | 理由 | 依赖 |
|---|---|---|---|
| **P0** | R1 ref 入口 + overlay | 用户已指出检索是主瓶颈；overlay 是“参考↔模型”唯一的比例证据 | 无（可先用 2D 合成） |
| **P0** | R2 参数出处/冲突 | 没有 provenance，检索结果无法复核；这次孔位/孔数冲突都靠人工裁决 | 可与 R1 同做 |
| **P0** | R3 features/holes | 孔表是这次重复劳动最大的一块；也是“盲孔表皮”这类问题的探针 | 无 |
| **P0** | R4 interference/mass/check | 105 对/21 项/密度表不该每个模型重写 | 可先做 helper，CLI 后做 |
| **P0** | R5 brief | 24 KB/80 KB 每次迭代；改动小、收益直接 | 无 |
| **P0/P1** | R7 export validate | 轴承 STEP invalid 是真实交付风险；回读脚本可复用 | 无 |
| **P1** | R6 camera/annotate/colors | +Y/背面视图目前靠私有 API；标注是 HUMAN-FEEDBACK-001 的延续 | 公开 render API |
| **P1** | R8 fresh/set/params | 生成器是这次的实际工作流；`001` 重名已发生 | 无 |
| **P1** | R9 commit 根文件 | 小 bug；commit 后 dirty 影响信任 | 无 |
| **P2** | R10 标准件/mates | catalog 已有 6205；插入路径和 seals/flanges 是增量 | R3/R4 |
| **P2** | R11 文档/示例/坑 | 低成本高杠杆，但不改变本次主链路 | 无 |

**如果只能先做一件：R1（ref overlay）**。它直接补“参考→参数”的输入链路，也是用户这次
评审中“R12 圆角、输入孔、多面固定孔”三类问题最可能被提前发现的工具。
**如果做三件：R1 + R2 + R3/R4 之一**；R5/R9 可以顺手带上。

## 5. 明确不建议做 / 边界

1. **不做 GUI 桥接/点选**：本文的 ref overlay、camera、标注都是离屏/命令行，符合现有边界。
2. **不做 FCStd 语义合并、不做任意工程逆向**：与 feedback.md §5 一致。
3. **不发明通用几何门控**：`check` 只跑任务作者写的断言；不自动判定“孔数/体积合格”。
4. **不把 ref overlay 对齐当成尺寸合格**：它只是观察；误差来源（照片透视、图纸缩放、
   线宽）要标注。
5. **不在 cad3 里做通用 web 搜索**：搜索仍由 agent 的 web 工具完成；cad3 负责下载后的
   落地/裁剪/对齐/对比。
6. **不要求所有模型都交原生特征树**：generator 冻结形状 + 脚本重建是合法路径；但要在
   文档里明确“可编辑性”是任务取舍（HUMAN-FEEDBACK-001 的另一条线）。
7. **不把 STEP 回读 valid 当成可制造**：R7 只验证交换格式的拓扑/体积一致性。

## 6. 待更强模型评估的问题

1. **R1 的投入产出**：agent 已经能“看”图片；`ref overlay` 的增量是否足以抵消标定/
   透视误差的复杂度？是否只做“参考图 contact sheet + 坐标网格”就够？
2. **R2 的边界**：params 应该是脚本的输入，还是旁路 provenance？如果模型脚本继续用
   Python dict，`--check` 只做“值 vs 声明”的比对是否足够？
3. **R3 的语义**：`inspect --holes` 应输出“候选”还是“已分类孔”？孔/凸台/圆角的判定
   用材料侧、法向、射线还是拓扑？如何避免把圆角半径当孔？
4. **R4 的形态**：`check` 是 Python harness 还是声明式 JSON？和 `mates` 的边界怎么划？
   如何保持“任务自定义断言”，不变成通用门控？
5. **R5 的兼容性**：`--brief` 是默认还是 opt-in？现有脚本/CI 是否依赖完整 JSON 的 stdout？
6. **R7 的匹配策略**：STEP product 可能被拆分/改名；按 bbox+体积匹配的容差怎么定？
   `--strict` 的失败是否应阻断提交？
7. **R8 的默认值**：装配项目是否默认 `--fresh`？PartDesign 编辑工作流如何不被打扰？
   `--set` 与 Spreadsheet/表达式的关系？
8. **R9 的 scope**：根文件默认纳入是否安全？是否只纳入白名单（README/docs）？
9. **R10 的取舍**：标准件用 catalog proxy 还是自定义几何？接口声明是否强制？
10. **成功度量**：用什么指标证明这些改动有效——建模时间、迭代次数、上下文 token、
    发现的错孔/干涉数、用户评审返工次数？

## 7. 附录：本次会话证据

- 工程：`/Users/liwuzhan/Desktop/CAD ToolS测试/rv63-worm-reducer`
- 提交：`1cbbeaa`（模型修订）、`8268d24`（README 补充）
- 关键文件：`src/rv63.py`（836 行；`inspect_and_assert` 666–836；`hole_axes` 621–665；
  `make_bearing` 447；`add_tap` 151）、`src/render_input.py`（75 行，私有 API）、
  `notes/verification.json`（21 checks）、`notes/design.md`
- 输出大小：`/tmp/run_final3.log` 24,038 B；`/tmp/rh3.log` 80,059 B
- 失败探针：`src/_probe_fail.py` → `cad3 run` exit code **1**，`ok=false`，
  `errors=[script_exception: raised RuntimeError: probe failure]`
- catalog：`cad3 catalog search 6205` → `bearing.deep_groove.6205`（d25/D52/B15）
- PDF：`web_fetch` → `Error: unsupported content type "application/pdf"`
- 最终 STEP：28 个形状对象 / 24 个实体 / 无 invalid（STEP 把 compound 拆成多个 product；手写 `Import.insert` 回读）
- 最终模型：21/21 checks；105 对干涉 0；6 相位 0；质量 9.04 kg；包围盒 180×152×182

## 8. 变更记录

- 2026-10-20：初稿。来源：RV63/NMRV063 建模会话；记录 R1–R11、优先级建议、非目标与
  待评估问题；明确 R1/R2 为检索问题的工具侧根因，R3/R4 为模型能力提升后的检查缺口，
  R5/R9 为低成本快赢。
