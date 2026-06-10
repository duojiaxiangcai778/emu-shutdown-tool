# 模拟器定时关闭工具 / Emulator Shutdown Timer

一个基于 tkinter 的 Windows 桌面工具，支持定时关闭 **雷电模拟器** 和 **MuMu模拟器**。

A tkinter-based Windows desktop tool for scheduling emulator shutdowns. Supports **LDPlayer** and **MuMu Emulator**.

---

## 功能 / Features

- ⏰ **定点定时** — 设置每天固定时间自动关闭模拟器（如每天 23:00）
- ⏳ **倒计时定时** — 设定倒计时（分钟），到时间自动关闭
- 🔁 **自动重启** — 定点任务关闭后自动重置，第二天同一时间再次触发
- 📋 **多任务支持** — 可添加/删除多个定时任务，每个独立启停
- 🗂️ **状态持久化** — 任务配置自动保存，开机自动加载并启动
- 🔍 **模拟器检测** — 自动扫描雷电 / MuMu 模拟器进程并显示列表
- ⚡ **一键关闭** — 支持关闭单个或全部模拟器进程
- 🪟 **最小化到任务栏** — 后台继续运行定时任务
- 🚀 **开机自启** — 支持设置开机自动启动

---

## 截图 / Screenshot

![主界面](screenshot.png)

---

## 下载 / Download

从 [Releases](https://github.com/duojiaxiangcai778/emu-shutdown-tool/releases) 页面下载最新版 `模拟器定时关闭工具.exe`。

Download the latest `模拟器定时关闭工具.exe` from the [Releases](https://github.com/duojiaxiangcai778/emu-shutdown-tool/releases) page.

---

## 使用说明 / Usage

1. 打开软件，点击 **+ 添加任务** 创建定时任务
2. 选择 **定点**（每天固定时间）或 **倒计时** 模式
3. 点击 **▶** 启动任务
4. 任务到时间后自动关闭检测到的模拟器
5. 点击 **— 最小化** 缩小到任务栏后台运行
6. 点击 **开机自启** 可设置开机自动启动

---

## 构建 / Build

```bash
# 安装依赖
pip install pyinstaller

# 打包 exe
pyinstaller 模拟器定时关闭工具.spec --clean --noconfirm

# 输出在 dist/ 目录
```

---

## 技术栈 / Tech Stack

- **Python 3.11+** — tkinter GUI
- **PyInstaller** — 打包为独立 exe
- **ctypes** — Windows API 调用（托盘图标）
- **WMIC / tasklist** — 进程扫描
- **taskkill / wmic** — 进程关闭

---

## 开源协议 / License

MIT
