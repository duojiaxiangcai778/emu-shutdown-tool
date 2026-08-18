# AGENTS.md

## Project

This is a Windows-only Python 3.11+ Tkinter application for managing LDPlayer and MuMu emulator instances. The main UI, scheduling logic, and Windows integration live in `模拟器管理工具.pyw`; emulator discovery and instance operations live in `ld_instance_manager.py`.

## Development

- Use the local virtual environment when available: `.venv\Scripts\python.exe`.
- Run `python -m py_compile 模拟器管理工具.pyw ld_instance_manager.py` before packaging.
- Build with `模拟器管理工具.spec`.
- PyInstaller in the DeepSeek Harness may fail while creating isolated child-process pipes. In that environment use:

```powershell
.venv\Scripts\python.exe -c "import sys; sys._pyi_isolated_subprocess=True; from PyInstaller.__main__ import run; run(['--clean','--noconfirm','.\\模拟器管理工具.spec'])"
```

- The reproducible build output is `dist\模拟器管理工具.exe`.
- The root `模拟器管理工具.exe` is the tracked release artifact and should be replaced only after a successful build and launch smoke test.

## Safety

- Do not commit `instance_config.json`, `快照\`, runtime logs, `.venv\`, `build\`, or `dist\`.
- Do not call Windows power commands during tests.
- Keep long-running emulator discovery and process operations off the Tkinter main thread.
- UI callbacks and daemon threads must log unexpected exceptions.

## UI

The current UI is organized as four notebook pages: 实例管理, 自动化任务, 环境诊断, and 运行日志. Avoid reintroducing nested Canvas scroll regions. Preserve the existing task and instance widget attributes used by scheduling logic.
