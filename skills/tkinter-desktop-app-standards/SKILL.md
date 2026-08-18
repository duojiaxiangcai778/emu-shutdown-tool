# Tkinter Desktop App Standards

适用于本项目的 Tkinter 开发与维护规范，源自 Hermes Desktop 的桌面应用经验并已移除平台专属指令。

## 核心规则

- 所有耗时的模拟器探测、进程扫描、PowerShell/DISM 调用和启动/关闭命令必须放到后台线程。
- 后台线程不得直接操作 Tk 控件，统一使用 `root.after(0, callback)` 回到主线程。
- UI 回调和 daemon 线程必须捕获 `Exception`，并通过项目现有日志函数记录。
- 用户修改任务、勾选实例或改变开关后立即保存配置，关闭窗口前再次保存。
- 配置加载完成前不要允许保存操作覆盖磁盘文件。
- 定时器循环必须在异常时将任务恢复为非运行状态并记录日志。
- 保持 `shutdown_tasks_frame`、`launch_tasks_frame`、`inst_rows_frame` 和任务 `vars` 字典等现有业务契约。

## UI 约束

- 主界面使用 Notebook 页面分流，不重新引入嵌套 Canvas 滚动区。
- 新增控件优先使用现有颜色、字体和 `RoundedButton`，避免另起一套主题。
- 路径输入必须支持回车和失焦验证，并在无效时恢复可用状态。
- 不要在主线程周期性调用可能阻塞的 MuMu 扫描。

## 验证

```powershell
.venv\Scripts\python.exe -m py_compile 模拟器管理工具.pyw ld_instance_manager.py
```

完成 UI 改动后至少验证：启动、标签页切换、创建任务、关闭窗口和配置保存。
