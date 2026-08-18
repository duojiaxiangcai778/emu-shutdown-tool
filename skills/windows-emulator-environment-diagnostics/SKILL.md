# Windows Emulator Environment Diagnostics

本项目的 LDPlayer/MuMu 环境诊断指南，改写自 Hermes Desktop 的模拟器诊断经验。

## 诊断顺序

1. 检查 LDPlayer/MuMu 安装路径和命令行工具是否存在。
2. 检查 Hyper-V、Windows Hypervisor Platform、Virtual Machine Platform、VBS 和 CPU 虚拟化状态。
3. 先收集完整报告，再根据报告决定是否提供一键修复。
4. 修复后重新检测，不用单次命令返回码推断最终状态。

## 兼容性

- LDPlayer 新版本可能使用 `ldconsole.exe`，旧版本可能使用 `dnconsole.exe`，路径探测必须兼容两者。
- LDPlayer 实例配置可能在 `vms/config` 文件或实例子目录中，扫描应有降级策略。
- MuMu 的 `MuMuManager.exe info --vmindex all` 输出通常按 UTF-8 JSON 处理，不要依赖 Windows 默认代码页。
- MuMu 的实例状态、启动和关闭命令不要与 LDPlayer 命令混用。

## 线程与权限

- PowerShell、DISM、bcdedit 和模拟器 CLI 调用不能阻塞 Tkinter 主线程。
- 一键修复必须明确提示管理员权限和重启要求。
- 环境检测失败时显示可诊断的状态，不要静默清空之前的结果。
