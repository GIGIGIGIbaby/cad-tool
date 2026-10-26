# dsh-cad-studio

FreeCAD 原生 CAD 的 DSH 插件。一次安装提供：

- **13 个 Host 工具**：FreeCAD 探测、工程初始化、脚本执行、只读观察、离屏渲染、
  装配接口核对、标准件目录查询、Git 设计快照（status/log/diff/commit/restore），
  以及一个原样转发 argv 的 `cad3_cli` 兜底；
- **浏览器结果卡**：渲染图预览、错误、提交哈希；
- **内嵌的 cad3 Python CLI**：宿主 Python 3.11+ 即可运行，不安装 build123d，
  不创建 300 MB 依赖环境；FreeCAD 由用户自己安装，插件只探测；
- **freecad-v3 skill**：让模型知道什么时候建模、观察、留档，以及 DSH 里该调哪些工具。

## 需要什么

- DSH（Web 或 Desktop）**0.2.1-rc.1 或更新**（插件用 `engines.dsh` 声明版本门禁）。
- 主机 Python **3.11+**（主机代码只有标准库依赖）。
- 已安装 **FreeCAD**（本机基线 FreeCAD 1.1.3；其它平台靠 `cad3 doctor` 探测）。
- Git。

插件不接管用户已打开的 FreeCAD GUI；所有几何执行都在独立的 `freecadcmd` 进程里完成。

## 安装

从 GitHub Release 安装最新包：

```bash
# macOS / Linux
curl -fL https://github.com/liwuzhan/cad-tool/releases/latest/download/dsh-cad-studio.tgz \
  -o dsh-cad-studio.tgz
dsh plugin --profile <profile> add ./dsh-cad-studio.tgz
```

```powershell
# Windows PowerShell
Invoke-WebRequest `
  -Uri https://github.com/liwuzhan/cad-tool/releases/latest/download/dsh-cad-studio.tgz `
  -OutFile dsh-cad-studio.tgz
dsh plugin --profile <profile> add ./dsh-cad-studio.tgz
```

也可以直接从仓库子目录安装（不跑构建，包内已提交内嵌的 Python 源码与 skill）：

```bash
dsh plugin --profile <profile> add "github:liwuzhan/cad-tool#path:/packages/dsh-cad-studio"
```

安装后开新会话，先调 `cad3_doctor` 确认 FreeCAD 可见。首次运行模型前不需要 bootstrap。

## 工具

| 工具 | 说明 |
|---|---|
| `cad3_doctor` | 探测 FreeCAD 与版本，纯读取 |
| `cad3_init` | 在空目录创建工程（含自己的 Git 仓库与 `cad3.json`） |
| `cad3_run` | 执行工程脚本；`step=true` 同时导出 STEP |
| `cad3_inspect` | 只读观察结构/参数/表达式/几何；`format=code` 返回代码式描述 |
| `cad3_render` | 离屏渲染 iso/top/front/right 或轴向剖面，返回 PNG 预览 |
| `cad3_mates` | 读取 `notes/interfaces.json`，核对声明的接口与几何 |
| `cad3_catalog` | 内置标准件目录 search/describe/compare/instantiate |
| `cad3_status` / `cad3_log` / `cad3_diff` | Git 状态、历史、文件差异 |
| `cad3_commit` / `cad3_restore` | 快照当前文件 / 恢复旧版本（不移动 HEAD） |
| `cad3_cli` | 兜底：把任意 cad3 argv 原样转发 |

工程目录用 `project` 传入；不传默认会话工作区。顶层 `ok` 只表示命令执行协议成功，
**不是设计、强度或制造合格证明**；原生错误/警告/观察结果要另行阅读。

## 包结构

- `lib/index.js` — Host 插件与 13 个 `cad3_*` 工具。
- `lib/client.js` — 浏览器结果卡。
- `python/` — 内嵌的 cad3 CLI（由 `packages/sync-vendor.mjs` 从仓库根同步）。
- `skills/freecad-v3/` — 从 `.agents/skills` 同步的 skill；Host 半件用 `ctx.skills.register()` 自带注册。
- `cordis.patch.yml` — 一行 profile bundle patch。

## 开发

```bash
# 修改仓库根的 src/cad3 或 skill 后，同步进插件包并提交
node packages/sync-vendor.mjs
node packages/sync-vendor.mjs --check   # CI 校验，不一致则失败
node packages/dsh-cad-studio/test/register.test.mjs
```

发布：给匹配 package 版本的 tag（如 `v3.0.0-alpha.1`）推送到 GitHub，
release workflow 会同步校验、测试、打 `dsh-cad-studio.tgz` 并挂到 Release。
