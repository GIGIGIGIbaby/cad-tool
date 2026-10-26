# Changelog

## 3.0.0-alpha.2 (2026-10-26)

- 修复发布物污染：CI 测试生成的 `__pycache__/*.pyc` 不再进入 tarball
  （包内 `.npmignore` + 工作流 `PYTHONDONTWRITEBYTECODE=1`）。工具与协议行为不变。

## 3.0.0-alpha.1

首个 FreeCAD 原生 V3 插件版，取代 V2 的 build123d / `.456d` 插件。

- Host 工具从 V2 的 17 个改为 13 个，映射到 `cad3` CLI：
  `cad3_doctor`、`cad3_init`、`cad3_run`、`cad3_inspect`、`cad3_render`、
  `cad3_mates`、`cad3_catalog`、`cad3_status`、`cad3_log`、`cad3_diff`、
  `cad3_commit`、`cad3_restore`，以及兜底 `cad3_cli`。
- 内嵌 Python CLI 只依赖标准库；不再需要创建 build123d 虚拟环境，
  FreeCAD 由用户安装，插件用 `cad3_doctor` 探测。
- 渲染结果卡展示离屏 PNG 预览（iso/top/front/right 与轴向剖面）。
- 随包安装 `freecad-v3` skill；工具名与运行约定写进 skill 头部。
- 发布产物仍是 `dsh-cad-studio.tgz`，因此插件市场的既有安装入口与条目无需改名。
