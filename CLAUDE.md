# CAD Tools V3 项目指令

本项目是 FreeCAD V3：原生 FreeCAD Python + 只读观察 + Git 快照。与旧 V2（build123d / `.456d` / Checkpoint）无关，不要使用旧流程或旧 `cad` 命令。

- 建模任务：先读 [skills/freecad-v3/SKILL.md](skills/freecad-v3/SKILL.md)，按需读其 `references/`。
- 命令入口：`python3 cad3.py ...`（或已安装的 `cad3`）。
- 实现契约：[ARCHITECTURE.md](ARCHITECTURE.md)；反馈含义与已被实测修正的判断：[docs/feedback.md](docs/feedback.md)。
- 测试：`PYTHONPATH=src python3 -m unittest discover -s tests -v`。
- 旧 V2 仓库封存于 `../cad tools v2`（标签 `v2-final`），只读参考。
