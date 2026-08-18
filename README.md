# 模拟器管理工具

面向 Windows 的雷电模拟器与 MuMu 管理工具，提供实例扫描、批量启停、定时任务、配置快照、环境诊断，以及关闭后的关机/重启联动。

## 项目状态

当前发布版本为 `v4.3`。主界面采用四页工作台布局：实例管理、自动化任务、环境诊断、运行日志；主界面不使用嵌套滚轮区域。

## 主要能力

- **实例管理**：自动探测或手动指定 LDPlayer、MuMu 路径，展示运行状态、CPU、内存和 Root 配置。
- **批量控制**：支持启动、关闭、间隔启动和单实例操作，耗时命令在后台执行，避免卡住界面。
- **定时任务**：支持每天定点和倒计时两种模式，任务可单独启停，定点任务会自动滚动到下一天。
- **配置快照**：备份和恢复 LDPlayer、MuMu 实例配置，便于跨版本或异常后的回滚。
- **环境诊断**：检测 Hyper-V、VMP、VBS 和 CPU 虚拟化状态，并在需要管理员权限时提供修复入口。
- **关机联动**：模拟器关闭完成后可选择关机或重启，操作日志写入程序目录。

## 使用

1. 下载发布包中的 `模拟器管理工具.exe`。
2. 首次启动后等待自动探测；也可以在“模拟器管理”中手动选择路径。
3. 扫描实例，勾选需要管理的实例。
4. 在“定时关闭”或“定时启动”中新建任务并单独启动。
5. 需要修改 Windows 虚拟化功能时，请以管理员身份运行程序。

程序会在 exe 同目录保存 `instance_config.json`、`快照` 和 `模拟器管理工具_运行日志.txt`。这些文件属于本机运行数据，不应提交到公共仓库。

## 从源码构建

项目使用 Python 3.11+、Tkinter、psutil 和 PyInstaller。在 Windows PowerShell 中执行：

```powershell
.venv\\Scripts\\python.exe -m pip install -r requirements.txt
.venv\\Scripts\\python.exe -m PyInstaller --clean --noconfirm 模拟器管理工具.spec
```

DeepSeek Harness 对 PyInstaller 的隔离子进程管道有限制时，使用以下等价构建包装：

```powershell
.venv\\Scripts\\python.exe -c "import sys; sys._pyi_isolated_subprocess=True; from PyInstaller.__main__ import run; run(['--clean','--noconfirm','.\\模拟器管理工具.spec'])"
```

构建产物位于 `dist/模拟器管理工具.exe`。如果本地没有虚拟环境，可先安装依赖：

```powershell
python -m pip install pyinstaller psutil
```

## 目录说明

- `模拟器管理工具.pyw`：主界面和任务调度逻辑。
- `ld_instance_manager.py`：模拟器路径探测、实例扫描、配置快照和控制命令。
- `模拟器管理工具.spec`：PyInstaller 构建配置。
- `成品/`：本地发布目录，默认不提交到仓库。

## 仓库整理

- `模拟器管理工具.pyw`：主界面和任务调度逻辑。
- `ld_instance_manager.py`：模拟器探测、实例扫描、快照和控制命令。
- `模拟器管理工具.spec`：PyInstaller 构建配置。
- `app_icon.ico`：构建所需图标。
- `requirements.txt`：运行与构建依赖。
- `AGENTS.md`：面向代码代理的项目约束。
- `skills/`：从 Hermes Desktop 经验改写的本项目维护指南。
- 根目录 `模拟器管理工具.exe`：当前发布构建；`build/`、`dist/` 和本机运行数据不提交。

## 许可

暂未指定开源许可证。公开仓库仅代表代码可见，不等同于授予任意复制、修改和分发权利。
