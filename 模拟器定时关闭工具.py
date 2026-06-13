#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模拟器定时关闭工具 v4.0
- 保留全部原有功能：定时任务、模拟器检测、一键关闭
- 新增优化：优雅关闭（WM_CLOSE → 超时 → 强制）、自动关机、配置备份
- 新增：雷电模拟器实例管理（自动探测路径、设置编辑、间隔启动、配置快照）
- 粉紫主题
"""

import sys
import os
import time
import json
import threading
import subprocess
import winreg
import ctypes
import shutil
from datetime import datetime, timedelta
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from tkinter.font import Font

try:
    import psutil
except ImportError:
    psutil = None

from ld_instance_manager import (
    auto_detect_paths, scan_instances, check_running_instances,
    read_instance_config, write_instance_config, get_instance_summary,
    save_snapshot, restore_snapshot, list_snapshots,
    launch_instance, staggered_launch,
    load_tool_config, save_tool_config,
    get_saved_paths, save_paths,
    TOOL_CONFIG_FILE, SNAPSHOT_DIR,
)


# ============================================================
# Windows API 常量 & 辅助
# ============================================================
WM_CLOSE = 0x0010

# ExitWindowsEx / InitiateShutdown 常量
EWX_SHUTDOWN   = 0x00000001
EWX_REBOOT     = 0x00000002
EWX_FORCE      = 0x00000004
EWX_POWEROFF   = 0x00000008
EWX_FORCEIFHUNG = 0x00000010

SHUTDOWN_FORCE_OTHERS = 0x00000001
SHUTDOWN_FORCE_SELF   = 0x00000002
SHUTDOWN_RESTART      = 0x00000004
SHUTDOWN_POWEROFF     = 0x00000008
SHUTDOWN_HYBRID       = 0x00000200

# 特权常量
SE_SHUTDOWN_NAME = "SeShutdownPrivilege"
TOKEN_ADJUST_PRIVILEGES = 0x0020
TOKEN_QUERY = 0x0008
SE_PRIVILEGE_ENABLED = 0x00000002

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
advapi32 = ctypes.windll.advapi32
ntdll = ctypes.windll.ntdll

WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_int, ctypes.c_int)


class LUID(ctypes.Structure):
    _fields_ = [("LowPart", ctypes.c_ulong), ("HighPart", ctypes.c_long)]


class TOKEN_PRIVILEGES(ctypes.Structure):
    _fields_ = [
        ("PrivilegeCount", ctypes.c_ulong),
        ("Luid", LUID),
        ("Attributes", ctypes.c_ulong),
    ]


def _enable_shutdown_privilege():
    """获取关机特权（SeShutdownPrivilege）— 调用关机 API 前必须拥有"""
    try:
        h_token = ctypes.c_void_p()
        # 打开当前进程的访问令牌
        ok = advapi32.OpenProcessToken(
            kernel32.GetCurrentProcess(),
            TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY,
            ctypes.byref(h_token)
        )
        if not ok:
            return False

        luid = LUID()
        ok = advapi32.LookupPrivilegeValueW(None, SE_SHUTDOWN_NAME, ctypes.byref(luid))
        if not ok:
            kernel32.CloseHandle(h_token)
            return False

        tp = TOKEN_PRIVILEGES()
        tp.PrivilegeCount = 1
        tp.Luid = luid
        tp.Attributes = SE_PRIVILEGE_ENABLED

        advapi32.AdjustTokenPrivileges(h_token, False, ctypes.byref(tp), 0, None, None)
        kernel32.CloseHandle(h_token)
        return True
    except Exception:
        return False


def _force_shutdown_windows(should_restart):
    """
    多级强制关机/重启 — 确保真正关机，不被任何软件拦截，不进入休眠/睡眠
    
    尝试顺序：
      1. shutdown /f（强制关闭应用）
      2. ExitWindowsEx API（绕过 UI 拦截）
      3. InitiateShutdown API（最底层，Vista+）
      4. wmic os call（最后手段）
    """
    # 先获取关机特权
    _enable_shutdown_privilege()

    # ----- 方法1: shutdown.exe /f -----
    flag = '/r' if should_restart else '/s'
    try:
        subprocess.run(
            ['shutdown', flag, '/t', '0', '/f'],
            capture_output=True, timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        # 如果成功，等待一下让命令生效
        time.sleep(2)
        # 检查是否还在运行（shutdown /a 可以取消说明还没执行）
        # 实际上 shutdown 命令调度的关机无法被本进程阻塞，这里直接返回
        return True
    except Exception:
        pass

    # ----- 方法2: ExitWindowsEx API (EWX_FORCE) -----
    # EWX_FORCE | EWX_SHUTDOWN | EWX_POWEROFF = 0x0D
    # EWX_FORCE | EWX_REBOOT = 0x06
    try:
        if should_restart:
            flags = EWX_REBOOT | EWX_FORCE
        else:
            flags = EWX_SHUTDOWN | EWX_FORCE | EWX_POWEROFF
        result = user32.ExitWindowsEx(flags, 0)
        if result:
            time.sleep(3)
            return True
    except Exception:
        pass

    # ----- 方法3: InitiateShutdown API (Windows Vista+) -----
    # 更底层，能绕过更多拦截
    try:
        if should_restart:
            dw_flags = SHUTDOWN_FORCE_OTHERS | SHUTDOWN_FORCE_SELF | SHUTDOWN_RESTART
        else:
            dw_flags = SHUTDOWN_FORCE_OTHERS | SHUTDOWN_FORCE_SELF | SHUTDOWN_POWEROFF
        result = kernel32.InitiateShutdownW(None, None, 0, dw_flags, 0)
        if result:
            time.sleep(3)
            return True
    except Exception:
        pass

    # ----- 方法4: wmic os call -----
    try:
        cmd = 'shutdown' if not should_restart else 'reboot'
        subprocess.run(
            ['wmic', 'os', 'where', 'Primary=True', 'call', cmd],
            capture_output=True, timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        time.sleep(3)
        return True
    except Exception:
        pass

    return False


def get_process_windows(pid):
    """获取指定 PID 关联的所有可见顶层窗口句柄"""
    hwnds = []
    @WNDENUMPROC
    def callback(hwnd, _):
        w_pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(w_pid))
        if w_pid.value == pid and user32.IsWindowVisible(hwnd):
            hwnds.append(hwnd)
        return True
    user32.EnumWindows(callback, 0)
    return hwnds


# ============================================================
# 内嵌图标（base64 编码的粉紫圆形 32x32 ICO）
# ============================================================
_APP_ICON_B64 = (
    "AAABAAEAICAAAAEAIACKAwAAFgAAAIlQTkcNChoKAAAADUlIRFIAAAAgAAAAIAgGAAAAc3p69AAAAARzQklUCAgICHwIZIgAAAAJcEhZcwAA"
    "AN0AAADdAXBTogcAAAAZdEVYdFNvZnR3YXJlAHd3dy5pbmtzY2FwZS5vcmeb7jwaAAADB0lEQVRYha3XS2hdVRQG4G/ZtDZUqBERqaSJ"
    "aB+O2g7UXBCiHTnXsWZQQS1YRxVHKShUHenMIGpHDhx0WNSJWATFtuBAtNBSGom1Riv4KA1SWA7OvuY0OfdyTrwLFneftf/973+v/byR"
    "mbpaREzhVSyW0BSOZ+bi4FbNdlvHjiciYh6fYjNeKr4Zn0XEfERMdOEc6wLGPPbjN7yFf0r8bezBE5jAy60ZM7OVYwt+wmwtdgzHat/P"
    "4SK2tOXtMgVP4RpOD8Fcx82CbWVdBKzg5yxDjYitGMd4KcvMj1QLc6U1a4cp6OFC7XumdLSCmVr8AnptebsswnusbjuZ+XVEfNEv13CL"
    "BdvKugg4gwMREbg7M3/FVw24AwU7WgGZeSUilvAGHsDT2Navj4h3VAtwKTOvjFxAsWewgD/K98Fa3R7cWTCtLboexRExrkr9Ntwo4XHV"
    "Fuxl5o1BbUcioCbkQdWJSLUmLm6Ep9NdsMZ6OF+8t2GWtvu1ts8ncQrn8FjxcyU22ZmvQ8eBF7CMVzCHq8XnSmwZLypTOzIB2KW6A05j"
    "tpaBfcX7GZit4Xb9bwHYhKNlZIfL6PoZGKvhxmoZOFx8ubTdtCEBZWRnG0a2u9TvwJvFd5TY7oZMncW+TgJwRHX1PlvLwC1zi5N4vfjJ"
    "NWuln6mjheMajnQR8Brex1IZxc4GzCXVW3AKlxrqd5a2S/hQ9WZsLWABf1a7dOAUHcKPxQ8NwWXhWmiqbzwJI2I7bscvmRkRsRf3luqr"
    "mXm+4O5Sqfy9fK/DRUTiPtzMzOV1fTUJqAnJImAF/Tt/JjO3DsCvw/U5BvbRUsB/JLXYw1av4+uZeWYAbqiA1tdxRDy+JvSl1QdJTzVl"
    "TbjhvC0zcALTJXw5M+cGjHYortEaVu1krTxsF+QGcOsuq7EygoN4HtvxSER8g7+6pHKYRcTHDdwfZOap/nvgBL7Ho7i//H43KgEDuN+j"
    "Ojb34gfV/7o78Hft9/MSb7J6XRtcE/dDgXfxJC43NN6PbwcQT9fa1MttOabxyb+yK7vHd3uPnAAAAABJRU5ErkJggg=="
)


def _get_icon_path():
    """将内嵌图标解压到临时目录，返回路径"""
    import tempfile
    path = os.path.join(tempfile.gettempdir(), "emu_shutdown_icon.ico")
    if not os.path.exists(path):
        try:
            with open(path, "wb") as f:
                f.write(__import__("base64").b64decode(_APP_ICON_B64))
        except Exception:
            return None
    return path


# ============================================================
# 配色方案 — 粉紫主题
# ============================================================
PRIMARY     = "#ff6b9d"   # 主色：热粉色
ACCENT      = "#b088f9"   # 强调色：淡紫色
BG          = "#F5F0F8"   # 极浅紫底
CARD        = "#FFFFFF"   # 纯白卡片
TEXT        = "#2D2538"   # 深紫黑文字
TEXT_SUB    = "#9B8EAA"   # 次要紫色文字
TEXT_LIGHT  = "#CBBFD6"   # 浅紫占位
BORDER      = "#E6DEF0"   # 浅紫边框
GREEN       = "#00B42A"
RED         = "#FF4757"
YELLOW      = "#FFA502"   # 暖琥珀色
ORANGE      = "#ff6b9d"   # 兼容旧变量
BG_LIGHT    = "#EDE6F4"   # 浅紫背景

# 粉紫扩展色
PINK_LIGHT  = "#FF8DB8"
PINK_DARK   = "#E55D83"
PURPLE_LIGHT = "#C9A8FF"
PURPLE_DARK  = "#9068D8"

# 兼容旧变量名
MI_ORANGE     = PRIMARY
MI_ORANGE_LT  = PINK_LIGHT
MI_ORANGE_DK  = PINK_DARK
MI_BG         = BG
MI_CARD       = CARD
MI_TEXT       = TEXT
MI_TEXT_SUB   = TEXT_SUB
MI_TEXT_LIGHT = TEXT_LIGHT
MI_BORDER     = BORDER
MI_GREEN      = GREEN
MI_RED        = RED
MI_YELLOW     = YELLOW


# ============================================================
# 进程检测 — 完全保留原有逻辑（雷电 + MuMu）
# ============================================================

LD_PROCESS_KEYWORDS = [
    "dnplayer", "dnmultiplayerex", "dnmultiplayer",
    "dnconsole", "dnmemu", "雷电模拟器", "leidian",
    "ldnews", "ldbox", "ldplayer"
]

MUMU_PROCESS_KEYWORDS = [
    "memevey", "mumuplayer", "mumuvmmgr", "mumugame",
    "nemuplayer", "nemu", "nemuservice", "mumu",
    "nemuheadless", "nemumultiplayer",
    "mumuemu", "mumuservice",
]

IGNORED_PROCESS_KEYWORDS = [
    "mumuremote",
]


def scan_emulators_in_background(callback):
    """在后台线程中扫描模拟器，完成后通过 callback 传回结果"""
    def _scan():
        try:
            procs = _do_scan()
            callback(procs)
        except Exception:
            callback([])
    threading.Thread(target=_scan, daemon=True).start()


def _do_scan():
    """实际执行扫描（可能阻塞）"""
    if psutil is not None:
        return _scan_psutil()
    procs = _scan_wmic()
    if procs:
        return procs
    return _scan_tasklist()


def _scan_psutil():
    processes = []
    for proc in psutil.process_iter(['pid', 'name', 'exe', 'cmdline']):
        try:
            pinfo = proc.info
            name = (pinfo['name'] or '').lower()
            exe = (pinfo['exe'] or '').lower()
            cmdline = ' '.join([str(a).lower() for a in (pinfo['cmdline'] or [])])
            combined = f"{name} {exe} {cmdline}"

            if any(kw in combined for kw in IGNORED_PROCESS_KEYWORDS):
                continue

            matched_type = None
            for kw in LD_PROCESS_KEYWORDS:
                if kw in combined:
                    matched_type = 'ld'
                    break
            if matched_type is None:
                for kw in MUMU_PROCESS_KEYWORDS:
                    if kw in combined:
                        matched_type = 'mumu'
                        break

            if matched_type:
                processes.append({
                    'pid': pinfo['pid'],
                    'name': pinfo['name'] or 'Unknown',
                    'type': matched_type,
                })
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    return processes


def _scan_wmic():
    processes = []
    try:
        result = subprocess.run(
            ['wmic', 'process', 'get', 'ProcessId,Name,ExecutablePath', '/format:csv'],
            capture_output=True, text=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW
        )
        for line in result.stdout.strip().split('\n')[1:]:
            if not line.strip():
                continue
            parts = line.split(',')
            if len(parts) >= 3:
                name = parts[-2].lower()
                exe = parts[-1].lower()
                pid_str = parts[-3] if len(parts) >= 4 else ''
                combined = f"{name} {exe}"

                if any(kw in combined for kw in IGNORED_PROCESS_KEYWORDS):
                    continue

                matched_type = None
                for kw in LD_PROCESS_KEYWORDS:
                    if kw in combined:
                        matched_type = 'ld'
                        break
                if matched_type is None:
                    for kw in MUMU_PROCESS_KEYWORDS:
                        if kw in combined:
                            matched_type = 'mumu'
                            break

                if matched_type and pid_str.isdigit():
                    processes.append({
                        'pid': int(pid_str),
                        'name': name,
                        'type': matched_type,
                    })
    except Exception:
        pass
    return processes


def _scan_tasklist():
    """使用 tasklist 扫描（WMIC 不可用时的备选方案）"""
    processes = []
    try:
        result = subprocess.run(
            ['tasklist', '/FO', 'CSV', '/NH'],
            capture_output=True, text=True, timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        for line in result.stdout.strip().split('\n'):
            if not line.strip():
                continue
            parts = line.split(',')
            if len(parts) >= 2:
                name = parts[0].strip('"').lower()
                pid_str = parts[1].strip('"') if len(parts) >= 2 else ''
                combined = name

                if any(kw in combined for kw in IGNORED_PROCESS_KEYWORDS):
                    continue

                matched_type = None
                for kw in LD_PROCESS_KEYWORDS:
                    if kw in combined:
                        matched_type = 'ld'
                        break
                if matched_type is None:
                    for kw in MUMU_PROCESS_KEYWORDS:
                        if kw in combined:
                            matched_type = 'mumu'
                            break

                if matched_type and pid_str.isdigit():
                    processes.append({
                        'pid': int(pid_str),
                        'name': name,
                        'type': matched_type,
                    })
    except Exception:
        pass
    return processes


# ============================================================
# 优雅关闭引擎（替代原 kill_emulators_async / _kill_single_process）
# ============================================================

GRACEFUL_TIMEOUT = 90       # 优雅关闭等待最大秒数（一级）
GRACEFUL_TIMEOUT_2 = 40     # 二次等待秒数（dnconsole quit 后）
ADB_SHUTDOWN_WAIT = 15      # ADB 关机等待秒数
POST_FORCE_WAIT = 5         # 强制关闭后等待秒数
SHUTDOWN_CANCEL_WAIT = 5    # 关机前取消倒计时秒数
BACKUP_KEEP_COUNT = 20      # 备份保留最大份数


def find_ldplayer_install_path(procs):
    """尝试找到雷电模拟器安装路径（用于 dnconsole quitall）"""
    candidates = [
        os.path.expandvars(r'%ProgramFiles%\LDPlayer\LDPlayer9'),
        os.path.expandvars(r'%ProgramFiles(x86)%\LDPlayer\LDPlayer9'),
        os.path.expandvars(r'%ProgramFiles%\LDPlayer\LDPlayer8'),
        os.path.expandvars(r'%ProgramFiles(x86)%\LDPlayer\LDPlayer8'),
        r'C:\LDPlayer\LDPlayer9', r'C:\LDPlayer\LDPlayer8',
        r'D:\LDPlayer\LDPlayer9', r'D:\LDPlayer\LDPlayer8',
    ]
    # 先从运行中的进程获取真实路径
    for proc in procs:
        if proc.get('name', '').lower() in ('ldplayer.exe', 'dnplayer.exe'):
            try:
                r = subprocess.run(
                    ['wmic', 'process', 'where', f'ProcessId={proc["pid"]}', 'get', 'ExecutablePath'],
                    capture_output=True, text=True, timeout=5,
                    creationflags=subprocess.CREATE_NO_WINDOW
                )
                for line in r.stdout.strip().split('\n'):
                    line = line.strip()
                    if line.lower().endswith(('ldplayer.exe', 'dnplayer.exe')) and os.path.exists(line):
                        return os.path.dirname(line)
            except Exception:
                pass
    for path in candidates:
        if os.path.exists(os.path.join(path, 'dnconsole.exe')):
            return path
    return None


def find_vms_config_dir():
    """获取 LDPlayer VM 配置目录"""
    appdata = os.environ.get('APPDATA', '')
    if not appdata:
        return None
    for ver in ['LDPlayer9', 'LDPlayer8', 'LDPlayer']:
        d = os.path.join(appdata, 'LDPlayer', ver, 'vms')
        if os.path.exists(d):
            return d
    roaming = os.path.join(appdata, 'LDPlayer')
    if os.path.exists(roaming):
        for item in os.listdir(roaming):
            d = os.path.join(roaming, item, 'vms')
            if os.path.exists(d):
                return d
    return None


def backup_config_files(backup_root):
    """备份雷电模拟器配置，返回 (备份目录路径, 消息)"""
    vms_dir = find_vms_config_dir()
    if not vms_dir or not os.path.exists(vms_dir):
        return None, "未找到雷电模拟器配置目录，跳过备份"
    timestamp = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
    backup_dir = os.path.join(backup_root, f'雷电配置备份_{timestamp}')
    try:
        os.makedirs(backup_dir, exist_ok=True)
        # 直接复制整个配置目录（包含 .config 实例设置和 .cfg 等其他配置）
        count = 0
        for item in os.listdir(vms_dir):
            s = os.path.join(vms_dir, item)
            d = os.path.join(backup_dir, item)
            if os.path.isdir(s):
                shutil.copytree(s, d, dirs_exist_ok=True)
            else:
                shutil.copy2(s, d)
            count += 1
        return backup_dir, f"已备份 {count} 项配置到：{backup_dir}"
    except Exception as e:
        return None, f"备份失败：{str(e)}"


def graceful_kill_async(on_done, on_status=None, on_progress=None, 
                         do_backup=False, do_shutdown=False, should_restart=False):
    """
    后台优雅关闭模拟器进程，替代原 kill_emulators_async
    
    Args:
        on_done: 完成后回调 fn(count, success, fail, failed_names, backup_msg, shutdown_executed)
        on_status: 状态更新回调 fn(text) （可选）
        on_progress: 进度回调 fn(current, total) （可选）
        do_backup: 是否备份配置
        do_shutdown: 是否关机/重启
        should_restart: True=重启 False=关机
    """
    TOTAL = 60  # 总进度

    def _status(t):
        if on_status:
            try:
                on_status(t)
            except Exception:
                pass

    def _progress(c, t=TOTAL):
        if on_progress:
            try:
                on_progress(c, t)
            except Exception:
                pass

    def _work():
        start_ts = time.time()
        backup_msg = ""
        shutdown_executed = False
        cancelled = False

        try:
            # ---- 扫描 ----
            _status("正在扫描模拟器进程...")
            _progress(2)
            procs = _do_scan()
            count = len(procs)
            success = 0

            if not procs:
                _status("未检测到模拟器进程")
            else:
                _status(f"发现 {count} 个模拟器进程，准备安全关闭")
                _progress(5)

            # ---- 备份配置 ----
            if do_backup:
                _status("正在备份雷电模拟器配置...")
                _progress(8)
                backup_dir = os.path.join(_config_dir_abs(), '备份')
                _, backup_msg = backup_config_files(backup_dir)
                _cleanup_old_backups(backup_dir)
                _status(backup_msg)

            if not procs:
                elapsed = int(time.time() - start_ts)
                _progress(elapsed)
                if do_shutdown:
                    _do_shutdown_countdown(should_restart, _status, _progress, TOTAL, start_ts)
                    shutdown_executed = True
                on_done(0, 0, 0, [], backup_msg, shutdown_executed)
                return

            # ---- 阶段1：dnconsole quitall（最优雅，所有实例一起关）----
            ld_path = find_ldplayer_install_path(procs)
            dnconsole_path = None
            if ld_path:
                dnconsole_path = os.path.join(ld_path, 'dnconsole.exe')
                if os.path.exists(dnconsole_path):
                    _status("通过 dnconsole quitall 优雅关闭所有实例...")
                    _progress(10)
                    try:
                        subprocess.run([dnconsole_path, 'quitall'], capture_output=True,
                                       text=True, timeout=15,
                                       creationflags=subprocess.CREATE_NO_WINDOW)
                    except Exception:
                        pass

            # ---- 等待阶段1完成（60秒）----
            _status("等待实例保存数据并退出...")
            deadline = time.time() + GRACEFUL_TIMEOUT
            while time.time() < deadline:
                remaining = _do_scan()
                exited_count = count - len(remaining)
                if not remaining:
                    _status(f"所有进程已安全关闭 ({count}/{count}) ✓")
                    success = count
                    break
                elapsed = int(time.time() - start_ts)
                _progress(min(elapsed + 5, 40))
                still = ', '.join(f"{p['name']}({p['pid']})" for p in remaining[:3])
                if len(remaining) > 3:
                    still += f"... 共{len(remaining)}个"
                _status(f"等待退出 ({exited_count}/{count})  剩余: {still}")
                time.sleep(1)
            else:
                remaining = _do_scan()
                _status(f"阶段1完成，剩余 {len(remaining)} 个进程，进入阶段2")

            # ---- 阶段2：逐个 dnconsole quit --index N（针对每个剩余实例）----
            remaining = _do_scan()
            if remaining and dnconsole_path and os.path.exists(dnconsole_path):
                _status("逐个发送 quit 指令到剩余实例...")
                _progress(42)
                for proc in remaining:
                    # 尝试从进程命令行提取实例名
                    try:
                        r = subprocess.run(
                            ['wmic', 'process', 'where', f'ProcessId={proc["pid"]}',
                             'get', 'CommandLine', '/format:csv'],
                            capture_output=True, text=True, timeout=5,
                            creationflags=subprocess.CREATE_NO_WINDOW
                        )
                        for line in r.stdout.splitlines():
                            if 'leidian' in line.lower() or 'dnplayer' in line.lower():
                                parts = line.split(',')
                                cmdline = parts[-1].lower() if len(parts) > 1 else ''
                                # 提取 --name 或 --index 参数
                                for marker in ['--name ', '--index ']:
                                    if marker in cmdline:
                                        val = cmdline.split(marker)[1].split()[0] if marker in cmdline else ''
                                        if val:
                                            idx = val
                                            subprocess.run(
                                                [dnconsole_path, 'quit', '--index', idx],
                                                capture_output=True, timeout=8,
                                                creationflags=subprocess.CREATE_NO_WINDOW
                                            )
                                            break
                    except Exception:
                        pass

                # 等待第二阶段完成（40秒）
                _status("等待实例响应 quit 指令...")
                deadline2 = time.time() + GRACEFUL_TIMEOUT_2
                while time.time() < deadline2:
                    remaining = _do_scan()
                    exited = count - len(remaining)
                    if not remaining:
                        _status(f"所有进程已关闭 ({count}/{count}) ✓")
                        success = count
                        break
                    _progress(min(42 + int(time.time() - start_ts), 65))
                    _status(f"等待响应 ({exited}/{count})...")
                    time.sleep(1)
                else:
                    remaining = _do_scan()
                    _status(f"阶段2完成，剩余 {len(remaining)} 个进程")

            # ---- 阶段3：ADB 关机（让 Android 系统安全停止）----
            remaining = _do_scan()
            if remaining and dnconsole_path and os.path.exists(dnconsole_path):
                _status("通过 ADB 发送关机指令到 Android 系统...")
                _progress(65)
                for proc in remaining:
                    try:
                        # 先获取 adb 端口
                        r = subprocess.run(
                            [dnconsole_path, 'adb', '--name', f'leidian{proc["pid"] % 100}',
                             '--command', 'shell reboot -p'],
                            capture_output=True, text=True, timeout=10,
                            creationflags=subprocess.CREATE_NO_WINDOW
                        )
                    except Exception:
                        pass
                _status(f"等待 ADB 关机生效（{ADB_SHUTDOWN_WAIT} 秒）...")
                for i in range(ADB_SHUTDOWN_WAIT):
                    remaining = _do_scan()
                    if not remaining:
                        success = count
                        break
                    time.sleep(1)

            # ---- 阶段4：最后手段 — 非强制 taskkill（不带 /F）----
            remaining = _do_scan()
            if remaining:
                _status(f"发送关闭信号到 {len(remaining)} 个剩余进程...")
                _progress(80)
                for proc in remaining:
                    # 先发 WM_CLOSE
                    hwnds = get_process_windows(proc['pid'])
                    for hw in hwnds:
                        user32.SendMessageW(hw, WM_CLOSE, 0, 0)
                    # 再发 taskkill（不带 /F，相当于再发一次 WM_CLOSE）
                    try:
                        subprocess.run(['taskkill', '/PID', str(proc['pid'])],
                                       capture_output=True, timeout=5,
                                       creationflags=subprocess.CREATE_NO_WINDOW)
                    except Exception:
                        pass

                # 等待进程响应
                _status("等待进程响应关闭信号...")
                for i in range(20):
                    remaining = _do_scan()
                    if not remaining:
                        success = count
                        break
                    time.sleep(1)
                else:
                    remaining = _do_scan()
                    _status(f"仍有 {len(remaining)} 个进程未响应")

            # ---- 阶段5：最终强制关闭（仅清理僵尸进程）----
            remaining = _do_scan()
            if remaining:
                # 检查是否只剩僵死的 adb/exe 辅助进程，不是模拟器核心进程
                core_remaining = [p for p in remaining if 'adb' not in p['name'].lower()
                                 and 'dnconsole' not in p['name'].lower()]
                if core_remaining:
                    _status(f"⚠ 强制关闭 {len(core_remaining)} 个顽固进程（可能丢失数据）...")
                    _progress(88)
                    for proc in core_remaining:
                        try:
                            subprocess.run(['taskkill', '/F', '/PID', str(proc['pid'])],
                                           capture_output=True, timeout=8,
                                           creationflags=subprocess.CREATE_NO_WINDOW)
                            success += 1
                        except Exception:
                            pass
                # 辅助进程也清理
                for proc in remaining:
                    if 'adb' in proc['name'].lower() or 'dnconsole' in proc['name'].lower():
                        try:
                            subprocess.run(['taskkill', '/F', '/PID', str(proc['pid'])],
                                           capture_output=True, timeout=5,
                                           creationflags=subprocess.CREATE_NO_WINDOW)
                        except Exception:
                            pass
            else:
                if success == 0:
                    success = count

            # ---- 等待清理 ----
            _status("等待系统清理完成...")
            for i in range(POST_FORCE_WAIT):
                time.sleep(1)
                _progress(min(int(time.time() - start_ts) + 2, TOTAL - 8))

            # ---- 关机/重启 ----
            if do_shutdown:
                _do_shutdown_countdown(should_restart, _status, _progress, TOTAL, start_ts)
                shutdown_executed = True
            else:
                elapsed = int(time.time() - start_ts)
                _progress(elapsed)
                _status(f"操作完成 — 已关闭 {success}/{count} 个进程")

            failed_count = count - success
            failed_names = []
            if failed_count > 0:
                final = _do_scan()
                failed_names = [f"{p['name']}(PID:{p['pid']})" for p in final]
            on_done(count, success, failed_count, failed_names, backup_msg, shutdown_executed)

        except Exception as e:
            _status(f"出错：{str(e)}")
            on_done(0, 0, 1, [str(e)], backup_msg, shutdown_executed)

    threading.Thread(target=_work, daemon=True).start()


def _cleanup_old_backups(backup_root, keep_count=BACKUP_KEEP_COUNT):
    """清理超出保留数量的旧备份目录，只保留最新的 keep_count 个"""
    if not os.path.isdir(backup_root):
        return
    try:
        entries = []
        for name in os.listdir(backup_root):
            d = os.path.join(backup_root, name)
            if os.path.isdir(d) and name.startswith("雷电配置备份_"):
                entries.append((d, name))
        if len(entries) > keep_count:
            # 按时间排序（名称含时间戳），删除最旧的
            entries.sort(key=lambda x: x[1])
            for d, name in entries[:-keep_count]:
                shutil.rmtree(d, ignore_errors=True)
    except Exception:
        pass


def _config_dir_abs():
    """获取配置/备份目录（exe所在目录）"""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _do_shutdown_countdown(should_restart, _status, _progress, TOTAL, start_ts):
    """关机倒计时并执行 — 使用多级强制关机确保不被拦截"""
    for i in range(SHUTDOWN_CANCEL_WAIT, 0, -1):
        _status(f"即将{'重启' if should_restart else '关机'} ({i} 秒后可取消)")
        _progress(TOTAL - i + 1, TOTAL)
        time.sleep(1)
    _status(f"正在{'重启' if should_restart else '关机'}...")
    _progress(TOTAL, TOTAL)
    # 使用多级强制关机
    ok = _force_shutdown_windows(should_restart)
    if ok:
        # 关机命令已发出，等待系统执行
        _status(f"{'重启' if should_restart else '关机'}命令已执行")
    else:
        _status(f"❌ 所有关机方法均失败，请手动{'重启' if should_restart else '关机'}")
        _progress(0, TOTAL)


# ============================================================
# 开机自启动管理（保留原样）
# ============================================================

REG_KEY_NAME = r"Software\Microsoft\Windows\CurrentVersion\Run"
REG_ENTRY_NAME = "模拟器定时关闭工具"


def is_auto_start_enabled():
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_KEY_NAME, 0, winreg.KEY_READ)
        winreg.QueryValueEx(key, REG_ENTRY_NAME)
        winreg.CloseKey(key)
        return True
    except Exception:
        return False


def set_auto_start(enable):
    exe_path = os.path.abspath(sys.executable if getattr(sys, 'frozen', False) else sys.argv[0])
    if not exe_path.lower().endswith('.exe'):
        return False, "当前运行的不是 exe 程序，请使用打包后的 exe 文件。"
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_KEY_NAME, 0, winreg.KEY_SET_VALUE)
        if enable:
            winreg.SetValueEx(key, REG_ENTRY_NAME, 0, winreg.REG_SZ, f'"{exe_path}"')
        else:
            try:
                winreg.DeleteValue(key, REG_ENTRY_NAME)
            except FileNotFoundError:
                pass
        winreg.CloseKey(key)
        return True, ""
    except Exception as e:
        return False, str(e)


# ============================================================
# 配置持久化（保留原样）
# ============================================================

def _config_dir():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))

CONFIG_FILE = os.path.join(_config_dir(), "tasks_config.json")


def load_tasks_config():
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception:
        pass
    return []


def save_tasks_config(tasks_data):
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(tasks_data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# ============================================================
# GUI — RoundedButton（保留原样，颜色引用已更新为粉紫）
# ============================================================

class RoundedButton(tk.Frame):
    """自绘按钮（Frame + Label，带字体缓存和悬停效果）"""

    _font_cache = {}

    def __init__(self, parent, text, command=None, bg=MI_ORANGE, fg="white",
                 font=None, padx=16, pady=6, hover_bg=None, **kwargs):
        self._cmd = command
        self._bg = bg
        self._fg = fg
        self._hover_bg = hover_bg or self._default_hover(bg)
        self._text = text
        self._font = font or ("Microsoft YaHei", 10)

        cache_key = str(self._font)
        if cache_key not in self._font_cache:
            try:
                _f = Font(font=self._font)
                tw = _f.measure("A" * max(len(text), 10))
                th = _f.metrics("linespace")
                _f.destroy()
                self._font_cache[cache_key] = (tw, th)
            except Exception:
                self._font_cache[cache_key] = (60, 20)
        tw, th = self._font_cache[cache_key]
        try:
            _f = Font(font=self._font)
            tw = _f.measure(text)
            _f.destroy()
        except Exception:
            pass

        w = tw + padx * 2 + 10
        h = th + pady * 2 + 6

        super().__init__(parent, width=w, height=h, bg=bg,
                         highlightthickness=1, highlightbackground=bg,
                         bd=0, cursor="hand2", **kwargs)
        self.pack_propagate(False)

        self._label = tk.Label(self, text=text, font=self._font,
                               bg=bg, fg=fg, cursor="hand2")
        self._label.place(relx=0.5, rely=0.5, anchor="center")

        self.bind("<Button-1>", self._on_click)
        self._label.bind("<Button-1>", self._on_click)
        self.bind("<Enter>", self._on_enter)
        self._label.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self._label.bind("<Leave>", self._on_leave)

    @staticmethod
    def _default_hover(bg_color):
        """根据按钮背景色自动推断合适的悬停颜色"""
        light_map = {
            "#ff6b9d": "#FF8DB8",
            "#b088f9": "#C9A8FF",
            "#00B42A": "#30C94E",
            "#FF4757": "#FF6B78",
            "#FFA502": "#FFB933",
            "#CBBFD6": "#D8CCE2",
        }
        return light_map.get(bg_color, "#FFFFFF")

    def _on_click(self, event):
        if self._cmd:
            self._cmd()

    def _on_enter(self, event):
        self.configure(bg=self._hover_bg, highlightbackground=self._hover_bg)
        self._label.configure(bg=self._hover_bg)

    def _on_leave(self, event):
        self.configure(bg=self._bg, highlightbackground=self._bg)
        self._label.configure(bg=self._bg)

    def set_text(self, text):
        self._text = text
        self._label.configure(text=text)

    def config_bg(self, color):
        self._bg = color
        self._hover_bg = self._default_hover(color)
        self.configure(bg=color, highlightbackground=color)
        self._label.configure(bg=color)


# ============================================================
# 主界面 — 保留原有全部功能 + 新增安全关机
# ============================================================

class EmulatorShutdownApp:
    def __init__(self, root):
        self.root = root
        self.root.title("模拟器管理工具 v4.1")
        try:
            ico = _get_icon_path()
            if ico:
                self.root.iconbitmap(ico)
        except Exception:
            pass
        self.root.geometry("880x800")
        self.root.minsize(820, 700)
        self.root.configure(bg=BG)

        # 窗口居中
        self.root.update_idletasks()
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x = (sw - 880) // 2
        y = (sh - 800) // 2
        self.root.geometry(f"880x800+{x}+{y}")

        # 关闭任务
        self.shutdown_tasks: list = []
        self.next_shutdown_id = 1
        # 启动任务
        self.launch_tasks: list = []
        self.next_launch_id = 1
        # 通用
        self.scan_timer_id = None
        self._emu_procs_cache = []
        self._emu_scan_pending = False

        # 关机相关状态
        self._shutdown_running = False

        # 实例管理
        self._ld_paths = {}
        self._instances = []
        self._inst_vars = []

        self.auto_start_var = tk.BooleanVar(value=is_auto_start_enabled())
        self.auto_launch_var = tk.BooleanVar(value=False)
        self._auto_launch_instances = set()
        self._startup_launch_done = False

        self._destroyed = False
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._build_ui()
        self.root.after_idle(self._lazy_init)

    def _lazy_init(self):
        """UI 显示后的延迟初始化"""
        self._load_tasks_config()
        self._start_scan_loop()
        self._init_instance_manager()

    # ---------- UI 构建 ----------

    def _build_ui(self):
        root = self.root

        self.f_title  = Font(family="Microsoft YaHei", size=14, weight="bold")
        self.f_sec    = Font(family="Microsoft YaHei", size=10, weight="bold")
        self.f_body   = Font(family="Microsoft YaHei", size=9)
        self.f_small  = Font(family="Microsoft YaHei", size=8)

        # ===== 顶栏 — 粉紫风：纯白 + 粉紫底线 =====
        header = tk.Frame(root, bg=CARD, height=48)
        header.pack(fill="x")
        header.pack_propagate(False)
        h_row = tk.Frame(header, bg=CARD)
        h_row.pack(expand=True, fill="x", padx=20)
        tk.Label(h_row, text="模拟器管理", font=self.f_title,
                 bg=CARD, fg=TEXT).pack(side="left")
        tk.Label(h_row, text="v4.1", font=self.f_small,
                 bg=CARD, fg=TEXT_SUB, padx=6).pack(side="left")
        # 粉色强调底线，替代原来的灰色
        tk.Frame(header, bg=PRIMARY, height=2).pack(side="bottom", fill="x")

        # ===== 主内容区（可滚动） =====
        main_canvas = tk.Canvas(root, bg=BG, highlightthickness=0)
        main_scrollbar = ttk.Scrollbar(root, orient="vertical", command=main_canvas.yview)
        main_frame = tk.Frame(main_canvas, bg=BG, padx=16, pady=12)
        main_frame.bind("<Configure>", lambda e: main_canvas.configure(scrollregion=main_canvas.bbox("all")))
        main_canvas.create_window((0, 0), window=main_frame, anchor="nw", tags="main_inner")
        main_canvas.configure(yscrollcommand=main_scrollbar.set)
        main_scrollbar.pack(side="right", fill="y")
        main_canvas.pack(side="left", fill="both", expand=True)

        def _on_mf_cfg(event):
            main_canvas.itemconfig("main_inner", width=event.width)
        main_canvas.bind("<Configure>", _on_mf_cfg)
        def _on_mw(event):
            main_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        main_canvas.bind_all("<MouseWheel>", _on_mw)

        # ---------- 卡片工厂 ----------
        def make_card(parent, padding=14, accent_color=ACCENT):
            # 外层容器：白色卡片 + 紫色左边框装饰
            card = tk.Frame(parent, bg=CARD, bd=0, highlightthickness=1,
                           highlightbackground=BORDER, highlightcolor=BORDER)
            card.pack(fill="x", pady=(0, 8))
            # 左侧紫色装饰条
            accent_bar = tk.Frame(card, bg=accent_color, width=3)
            accent_bar.pack(side="left", fill="y")
            inner = tk.Frame(card, bg=CARD, padx=padding, pady=padding)
            inner.pack(fill="both", expand=True)
            return card, inner

        def section_title(parent, text, color=PRIMARY):
            lbl = tk.Label(parent, text=text, font=self.f_sec,
                     bg=CARD, fg=color)
            lbl.pack(anchor="w", pady=(0, 6))
            # 标题下加一条细分割线
            tk.Frame(parent, bg=BORDER, height=1).pack(fill="x", pady=(0, 4))

        # ============================================================
        # 卡片1：定时关闭实例 + 开机自启
        # ============================================================
        card1, c1 = make_card(main_frame)

        section_title(c1, "■  定时关闭实例", RED)

        shutdown_scroll_f = tk.Frame(c1, bg=CARD)
        shutdown_scroll_f.pack(fill="both", expand=True)
        shutdown_canvas = tk.Canvas(shutdown_scroll_f, bg=CARD, highlightthickness=0, height=100)
        shutdown_sb = ttk.Scrollbar(shutdown_scroll_f, orient="vertical", command=shutdown_canvas.yview)
        self.shutdown_tasks_frame = tk.Frame(shutdown_canvas, bg=CARD)
        self.shutdown_tasks_frame.bind("<Configure>",
            lambda e: shutdown_canvas.configure(scrollregion=shutdown_canvas.bbox("all")))
        shutdown_canvas.create_window((0, 0), window=self.shutdown_tasks_frame, anchor="nw", tags="inner")
        shutdown_canvas.configure(yscrollcommand=shutdown_sb.set)
        shutdown_canvas.pack(side="left", fill="both", expand=True)
        shutdown_sb.pack(side="right", fill="y")
        self._shutdown_canvas = shutdown_canvas

        rbtn = tk.Frame(c1, bg=CARD)
        rbtn.pack(fill="x", pady=(6, 0))
        RoundedButton(rbtn, text="+", command=lambda: self._add_task("shutdown"),
                      bg=RED, fg="white", font=("Consolas", 10, "bold"),
                      padx=8, pady=1).pack(side="left", padx=(0, 4))
        RoundedButton(rbtn, text="全部启动", command=lambda: self._start_all("shutdown"),
                      bg=ORANGE, fg="white", font=self.f_body, padx=8).pack(side="left", padx=(0, 4))
        RoundedButton(rbtn, text="停止", command=lambda: self._stop_all("shutdown"),
                      bg=RED, fg="white", font=self.f_body, padx=8).pack(side="left")

        # 开机自启实例
        auto_f = tk.Frame(c1, bg=CARD)
        auto_f.pack(fill="x", pady=(6, 0))
        tk.Checkbutton(auto_f, text="开机自启勾选的实例（启动时自动恢复配置并启动）",
                       variable=self.auto_launch_var,
                       font=self.f_body, bg=CARD, fg=TEXT_SUB,
                       selectcolor=CARD, activebackground=CARD,
                       command=self._save_tasks_config).pack(side="left")

        # ===== 定时启动实例 =====
        tk.Frame(c1, bg=BORDER, height=1).pack(fill="x", pady=(6, 4))
        section_title(c1, "■  定时启动实例", GREEN)

        launch_scroll_f = tk.Frame(c1, bg=CARD)
        launch_scroll_f.pack(fill="both", expand=True)
        launch_canvas = tk.Canvas(launch_scroll_f, bg=CARD, highlightthickness=0, height=100)
        launch_sb = ttk.Scrollbar(launch_scroll_f, orient="vertical", command=launch_canvas.yview)
        self.launch_tasks_frame = tk.Frame(launch_canvas, bg=CARD)
        self.launch_tasks_frame.bind("<Configure>",
            lambda e: launch_canvas.configure(scrollregion=launch_canvas.bbox("all")))
        launch_canvas.create_window((0, 0), window=self.launch_tasks_frame, anchor="nw", tags="inner")
        launch_canvas.configure(yscrollcommand=launch_sb.set)
        launch_canvas.pack(side="left", fill="both", expand=True)
        launch_sb.pack(side="right", fill="y")
        self._launch_canvas = launch_canvas

        lbtn = tk.Frame(c1, bg=CARD)
        lbtn.pack(fill="x", pady=(6, 0))
        RoundedButton(lbtn, text="+", command=self._add_launch_task,
                      bg=GREEN, fg="white", font=("Consolas", 10, "bold"),
                      padx=8, pady=1).pack(side="left", padx=(0, 4))
        RoundedButton(lbtn, text="全部启动", command=lambda: self._start_all_launch(),
                      bg=GREEN, fg="white", font=self.f_body, padx=8).pack(side="left", padx=(0, 4))
        RoundedButton(lbtn, text="停止", command=lambda: self._stop_all_launch(),
                      bg=TEXT_SUB, fg="white", font=self.f_body, padx=8).pack(side="left")

        # ============================================================
        # 卡片2：实例管理
        # ============================================================
        card2, c2 = make_card(main_frame)

        tk.Label(c2, text="雷电模拟器实例", font=self.f_sec,
                 bg=CARD, fg=PRIMARY).pack(anchor="w", pady=(0, 4))

        self.ld_path_var = tk.StringVar(value="检测中...")
        tk.Label(c2, textvariable=self.ld_path_var,
                 font=self.f_small, bg=CARD, fg=TEXT_SUB,
                 anchor="w").pack(fill="x", pady=(0, 4))

        # 实例表头
        ih = tk.Frame(c2, bg=CARD)
        ih.pack(fill="x")
        for txt, w in [("", 3), ("实例", 10), ("设置", 26), ("状态", 8)]:
            tk.Label(ih, text=txt, font=("Microsoft YaHei", 8, "bold"),
                     bg=CARD, fg=TEXT_SUB, width=w, anchor="w").pack(side="left")
        tk.Frame(c2, bg=BORDER, height=1).pack(fill="x", pady=(2, 0))

        self.inst_rows_frame = tk.Frame(c2, bg=CARD)
        self.inst_rows_frame.pack(fill="x")

        # 按钮行
        ib = tk.Frame(c2, bg=CARD)
        ib.pack(fill="x", pady=(6, 0))
        RoundedButton(ib, text="⟳ 扫描", command=self._refresh_instances,
                      bg=PRIMARY, fg="white", font=self.f_body, padx=8).pack(side="left", padx=(0, 4))
        RoundedButton(ib, text="编辑", command=self._edit_instance_settings,
                      bg=TEXT_SUB, fg="white", font=self.f_body, padx=8).pack(side="left", padx=(0, 4))
        RoundedButton(ib, text="保存快照", command=self._save_snapshot,
                      bg=GREEN, fg="white", font=self.f_body, padx=8).pack(side="left", padx=(0, 4))
        RoundedButton(ib, text="恢复快照", command=self._restore_snapshot,
                      bg=YELLOW, fg="white", font=self.f_body, padx=8).pack(side="left", padx=(0, 8))
        # 间隔
        tk.Label(ib, text="间隔", font=self.f_body, bg=CARD, fg=TEXT_SUB).pack(side="left")
        self.launch_interval_var = tk.StringVar(value="5")
        ttk.Spinbox(ib, from_=1, to=60, width=2,
                     textvariable=self.launch_interval_var,
                     font=("Consolas", 9)).pack(side="left", padx=(3, 1))
        tk.Label(ib, text="秒", font=self.f_body, bg=CARD, fg=TEXT_SUB).pack(side="left")
        self.launch_btn = RoundedButton(ib, text="启动", command=self._on_staggered_launch,
                                        bg=GREEN, fg="white", font=self.f_body, padx=8)
        self.launch_btn.pack(side="right")
        self.launch_status_var = tk.StringVar(value="")
        tk.Label(ib, textvariable=self.launch_status_var,
                 font=self.f_small, bg=CARD, fg=TEXT_SUB).pack(side="right", padx=(0, 6))

        # 第二行：备份管理按钮
        bb = tk.Frame(c2, bg=CARD)
        bb.pack(fill="x", pady=(4, 0))
        tk.Label(bb, text="备份管理", font=("Microsoft YaHei", 8, "bold"),
                 bg=CARD, fg=TEXT_SUB).pack(side="left", padx=(0, 8))
        RoundedButton(bb, text="📋 备份列表", command=self._show_backup_list,
                      bg=PURPLE_LIGHT, fg="white", font=self.f_small, padx=8, pady=2).pack(side="left", padx=(0, 4))
        RoundedButton(bb, text="📸 快照列表", command=self._show_snapshot_list,
                      bg=PINK_LIGHT, fg="white", font=self.f_small, padx=8, pady=2).pack(side="left")

        # ============================================================
        # 卡片3：关机设置 & 操作按钮
        # ============================================================
        card3, c3 = make_card(main_frame)

        tk.Label(c3, text="操作", font=self.f_sec,
                 bg=CARD, fg=TEXT).pack(anchor="w", pady=(0, 4))

        # 操作按钮行
        op_row = tk.Frame(c3, bg=CARD)
        op_row.pack(fill="x", pady=(0, 6))
        RoundedButton(op_row, text="关闭所有模拟器", command=self._on_kill_now,
                      bg=RED, fg="white", font=self.f_body, padx=10).pack(side="left", padx=(0, 10))

        # 关机设置（嵌入操作区）
        tk.Label(c3, text="关闭后操作", font=("Microsoft YaHei", 8, "bold"),
                 bg=CARD, fg=TEXT_SUB).pack(anchor="w")
        sf = tk.Frame(c3, bg=CARD)
        sf.pack(fill="x")
        self.shutdown_var = tk.BooleanVar(value=True)
        tk.Checkbutton(sf, text="关机", variable=self.shutdown_var,
                       font=self.f_body, bg=CARD, fg=RED,
                       selectcolor=CARD, activebackground=CARD).pack(side="left", padx=(0, 16))
        self.restart_var = tk.BooleanVar(value=False)
        tk.Checkbutton(sf, text="重启（替代关机）", variable=self.restart_var,
                       font=self.f_body, bg=CARD, fg=RED,
                       selectcolor=CARD, activebackground=CARD).pack(side="left")
        tk.Label(sf, text="（关闭模拟器后自动执行）", font=("Microsoft YaHei", 7),
                 bg=CARD, fg=TEXT_LIGHT).pack(side="left", padx=(8, 0))

        # ============================================================
        # 底部
        # ============================================================
        bt = tk.Frame(main_frame, bg=BG)
        bt.pack(fill="x", pady=(4, 0))
        tk.Checkbutton(bt, text="开机自启工具", variable=self.auto_start_var,
                       font=self.f_body, bg=BG, fg=TEXT_SUB,
                       selectcolor=CARD, activebackground=BG,
                       command=self._on_auto_start_toggle).pack(side="left")
        RoundedButton(bt, text="最小化", command=self._minimize_to_tray,
                      bg=TEXT_LIGHT, fg="white", font=self.f_small, padx=8).pack(side="right")

        # 底栏
        ft = tk.Frame(root, bg=BG, height=20)
        ft.pack(fill="x")
        tk.Label(ft, text="实例管理 · 定时启停 · 强制关机 · 配置快照",
                 font=("Microsoft YaHei", 7), bg=BG, fg=TEXT_LIGHT).pack(expand=True)

    # ---------- 任务组件（完全保留原有逻辑） ----------

    def _make_task_row(self, parent, task_id, data, task_type):
        """构建单个任务行"""
        mode = data.get("mode", "fixed")
        hour = data.get("hour", 22)
        minute = data.get("minute", 0)
        cd_min = data.get("countdown_min", 30)
        enabled = data.get("enabled", True)

        color = RED

        task = {
            "id": task_id, "type": task_type,
            "running": False, "thread": None,
            "remaining": 0, "target_ts": 0, "enabled": enabled,
            "mode": mode, "hour": hour, "minute": minute, "cd_min": cd_min,
            "update_id": None, "auto_reset_id": None, "_pending_update": False,
        }

        frame = tk.Frame(parent, bg=CARD, bd=0)
        frame.pack(fill="x")
        tk.Frame(frame, bg=BORDER, height=1).pack(fill="x", side="bottom")

        row = tk.Frame(frame, bg=CARD)
        row.pack(fill="x", pady=4)

        # 启用勾选
        en_var = tk.BooleanVar(value=enabled)
        tk.Checkbutton(row, variable=en_var, bg=CARD,
                       activebackground=CARD, selectcolor=CARD,
                       command=lambda: self._on_en_toggle(task, en_var)).pack(side="left", padx=(0, 3))
        task["en_var"] = en_var

        # 模式
        mode_var = tk.StringVar(value="定点" if mode == "fixed" else "倒计时")
        mode_combo = ttk.Combobox(row, textvariable=mode_var,
                                   values=["定点", "倒计时"], width=4,
                                   state="readonly", font=("Microsoft YaHei", 8))
        mode_combo.pack(side="left", padx=(0, 3))

        # 时间输入
        tf = tk.Frame(row, bg=CARD)
        tf.pack(side="left")
        ff = tk.Frame(tf, bg=CARD)
        h_spin = ttk.Spinbox(ff, from_=0, to=23, width=2,
                              font=("Consolas", 8), format="%02.0f")
        h_spin.pack(side="left")
        h_spin.set(f"{hour:02d}")
        tk.Label(ff, text=":", font=("Consolas", 8), bg=CARD, fg=TEXT).pack(side="left")
        m_spin = ttk.Spinbox(ff, from_=0, to=59, width=2,
                              font=("Consolas", 8), format="%02.0f")
        m_spin.pack(side="left")
        m_spin.set(f"{minute:02d}")

        cf = tk.Frame(tf, bg=CARD)
        cd_spin = ttk.Spinbox(cf, from_=1, to=999, width=3, font=("Consolas", 8))
        cd_spin.pack(side="left")
        cd_spin.set(str(cd_min))
        tk.Label(cf, text="分", font=("Microsoft YaHei", 8),
                 bg=CARD, fg=TEXT_SUB).pack(side="left", padx=1)

        def _switch_mode():
            m = mode_var.get()
            task["mode"] = "countdown" if m == "倒计时" else "fixed"
            if task["mode"] == "fixed":
                cf.pack_forget(); ff.pack(side="left")
            else:
                ff.pack_forget(); cf.pack(side="left")
            self._save_tasks_config()

        mode_combo.bind("<<ComboboxSelected>>", lambda e: _switch_mode())
        if mode == "fixed":
            ff.pack(side="left"); cf.pack_forget()
        else:
            ff.pack_forget(); cf.pack(side="left")

        # 状态
        st_lbl = tk.Label(row, text="待启动", font=("Microsoft YaHei", 8),
                          fg=TEXT_LIGHT, bg=CARD, anchor="w")
        st_lbl.pack(side="left", padx=(6, 0))

        # 按钮
        act_btn = RoundedButton(row, text="▶", command=lambda: _toggle(),
                                 bg=color, fg="white",
                                 font=("Consolas", 8, "bold"), padx=6, pady=0)
        act_btn.pack(side="right", padx=(2, 0))
        RoundedButton(row, text="×", command=lambda: _delete(),
                      bg=TEXT_LIGHT, fg="white",
                      font=("Consolas", 8, "bold"), padx=4, pady=0).pack(side="right", padx=(2, 0))

        def _toggle():
            _stop() if task["running"] else _start()

        def _calc_ts():
            try:
                if task["mode"] == "fixed":
                    h, m = int(h_spin.get()), int(m_spin.get())
                    t = datetime.now().replace(hour=h, minute=m, second=0, microsecond=0)
                    if t <= datetime.now():
                        t += timedelta(days=1)
                    return t.timestamp()
                else:
                    return (datetime.now() + timedelta(minutes=int(cd_spin.get()))).timestamp()
            except ValueError:
                return None

        def _start():
            if task["running"] or not task["enabled"]:
                return
            ts = _calc_ts()
            if ts is None:
                return
            task["running"] = True
            task["target_ts"] = ts
            task["thread"] = threading.Thread(target=_loop, daemon=True)
            task["thread"].start()
            act_btn.config_bg(TEXT_LIGHT); act_btn.set_text("■")
            _update_status()
            self._save_tasks_config()

        def _stop():
            task["running"] = False
            for k in ("update_id", "auto_reset_id"):
                if task.get(k):
                    try: self.root.after_cancel(task[k])
                    except: pass
                    task[k] = None
            task["thread"] = None
            act_btn.config_bg(color); act_btn.set_text("▶")
            st_lbl.config(text="待启动", fg=TEXT_LIGHT)
            self._save_tasks_config()

        def _loop():
            while task["running"]:
                rem = int(task["target_ts"] - time.time())
                if rem <= 0:
                    self.root.after(0, _time_up); break
                task["remaining"] = rem
                if not task["_pending_update"]:
                    task["_pending_update"] = True
                    self.root.after(0, _update_status)
                time.sleep(0.5)

        def _time_up():
            if not task["running"]:
                return
            task["running"] = False
            act_btn.config_bg(color); act_btn.set_text("▶")
            st_lbl.config(text="执行中…", fg=YELLOW)

            should_shutdown = self.shutdown_var.get()
            def _on_done(count, success, fail_count, failed_names, _, __):
                st_lbl.config(text=f"完成 {success}/{count}", fg=GREEN if fail_count == 0 else YELLOW)
                if should_shutdown and success > 0:
                    st_lbl.config(text=f"关机中…", fg=RED)
                    self.root.update()
                    time.sleep(0.5)
                    _force_shutdown_windows(self.restart_var.get())
                elif task["mode"] == "fixed":
                    task["auto_reset_id"] = self.root.after(2000, lambda: self._auto_reset_task(task))
                self._save_tasks_config()
            graceful_kill_async(on_done=_on_done, do_backup=True, do_shutdown=should_shutdown,
                               should_restart=self.restart_var.get())

        def _auto_reset_task_local():
            task["auto_reset_id"] = None
            if not task["enabled"]:
                return
            h, m = int(h_spin.get()), int(m_spin.get())
            target = datetime.now().replace(hour=h, minute=m, second=0, microsecond=0) + timedelta(days=1)
            task["target_ts"] = target.timestamp()
            task["running"] = True
            task["thread"] = threading.Thread(target=_loop, daemon=True)
            task["thread"].start()
            act_btn.config_bg(TEXT_LIGHT); act_btn.set_text("■")
            st_lbl.config(text=f"明天 {h:02d}:{m:02d}", fg=PRIMARY)
            self._save_tasks_config()

        def _update_status():
            task["_pending_update"] = False
            if not task["enabled"]:
                st_lbl.config(text="已禁用", fg=TEXT_LIGHT); return
            if task["running"]:
                rem = task["remaining"]
                h, m, s = rem // 3600, (rem % 3600) // 60, rem % 60
                if task["mode"] == "fixed":
                    ts = datetime.fromtimestamp(task["target_ts"]).strftime("%H:%M")
                    st_lbl.config(text=f"{ts} {h:02d}:{m:02d}:{s:02d}", fg=YELLOW)
                else:
                    st_lbl.config(text=f"{h:02d}:{m:02d}:{s:02d}", fg=YELLOW)
            else:
                st_lbl.config(text="待启动", fg=TEXT_LIGHT)

        def _delete():
            if task["running"]:
                _stop()
            frame.destroy()
            for i, t in enumerate(self.shutdown_tasks):
                if t["id"] == task_id:
                    self.shutdown_tasks.pop(i); break
            self._save_tasks_config()

        # 绑定 auto_reset
        task["_auto_reset_fn"] = _auto_reset_task_local

        task["vars"] = {
            "h_spin": h_spin, "m_spin": m_spin, "cd_spin": cd_spin,
            "st_lbl": st_lbl, "act_btn": act_btn,
        }
        return task

    def _on_en_toggle(self, task, en_var):
        task["enabled"] = en_var.get()
        if not task["enabled"] and task["running"]:
            task["running"] = False
            task["vars"]["act_btn"].config_bg(RED)
            task["vars"]["act_btn"].set_text("▶")
            task["vars"]["st_lbl"].config(text="已禁用", fg=TEXT_LIGHT)
        self._save_tasks_config()

    def _auto_reset_task(self, t):
        fn = t.get("_auto_reset_fn")
        if fn:
            fn()

    def _renumber(self, tasks_list):
        for i, t in enumerate(tasks_list):
            if "set_idx" in t:
                t["set_idx"](i + 1)

    # ---------- 任务管理 ----------

    def _add_task(self, task_type, data=None):
        if data is None:
            data = {}
        tid = self.next_shutdown_id; self.next_shutdown_id += 1
        parent = self.shutdown_tasks_frame; tasks_list = self.shutdown_tasks
        widget = self._make_task_row(parent, tid, data, task_type)
        tasks_list.append(widget)
        self._save_tasks_config()

    def _start_all(self, task_type):
        for t in self.shutdown_tasks:
            if t["enabled"] and not t["running"]:
                self._inline_start(t)

    def _inline_start(self, t):
        if t["running"] or not t["enabled"]:
            return
        try:
            if t["mode"] == "fixed":
                h = int(t["vars"]["h_spin"].get())
                m = int(t["vars"]["m_spin"].get())
                target = datetime.now().replace(hour=h, minute=m, second=0, microsecond=0)
                if target <= datetime.now():
                    target += timedelta(days=1)
                ts = target.timestamp()
            else:
                ts = (datetime.now() + timedelta(minutes=int(t["vars"]["cd_spin"].get()))).timestamp()
        except ValueError:
            return
        t["running"] = True; t["target_ts"] = ts
        t["thread"] = threading.Thread(target=self._make_loop_fn(t), daemon=True)
        t["thread"].start()
        t["vars"]["act_btn"].config_bg(TEXT_LIGHT)
        t["vars"]["act_btn"].set_text("■")
        self._update_task_status(t)
        self._save_tasks_config()

    def _make_loop_fn(self, t):
        def _loop():
            while t["running"]:
                rem = int(t["target_ts"] - time.time())
                if rem <= 0:
                    self.root.after(0, lambda: self._task_time_up(t)); break
                t["remaining"] = rem
                if not t.get("_pending_update"):
                    t["_pending_update"] = True
                    self.root.after(0, lambda: self._update_task_status(t))
                time.sleep(0.5)
        return _loop

    def _task_time_up(self, t):
        if not t["running"]:
            return
        t["running"] = False
        t["vars"]["act_btn"].config_bg(RED)
        t["vars"]["act_btn"].set_text("▶")
        t["vars"]["st_lbl"].config(text="执行中…", fg=YELLOW)

        should_shutdown = self.shutdown_var.get()
        def _on_done(count, success, fail_count, failed_names, _, __):
            t["vars"]["st_lbl"].config(
                text=f"完成 {success}/{count}" if count > 0 else "无进程",
                fg=GREEN if fail_count == 0 else YELLOW)
            if should_shutdown and success > 0:
                t["vars"]["st_lbl"].config(text="关机中…", fg=RED)
                self.root.update()
                time.sleep(0.5)
                _force_shutdown_windows(self.restart_var.get())
            elif t["mode"] == "fixed":
                t["auto_reset_id"] = self.root.after(2000, lambda: self._auto_reset_task(t))
            self._save_tasks_config()
        graceful_kill_async(on_done=_on_done, do_backup=True, do_shutdown=should_shutdown,
                           should_restart=self.restart_var.get())

    def _update_task_status(self, t):
        t["_pending_update"] = False
        if not t["enabled"]:
            t["vars"]["st_lbl"].config(text="已禁用", fg=TEXT_LIGHT); return
        if t["running"]:
            rem = t["remaining"]
            h, m, s = rem // 3600, (rem % 3600) // 60, rem % 60
            if t["mode"] == "fixed":
                ts = datetime.fromtimestamp(t["target_ts"]).strftime("%H:%M")
                t["vars"]["st_lbl"].config(text=f"{ts} {h:02d}:{m:02d}:{s:02d}", fg=YELLOW)
            else:
                t["vars"]["st_lbl"].config(text=f"{h:02d}:{m:02d}:{s:02d}", fg=YELLOW)
        else:
            t["vars"]["st_lbl"].config(text="待启动", fg=TEXT_LIGHT)

    def _stop_all(self, task_type):
        for t in self.shutdown_tasks:
            if t.get("auto_reset_id"):
                try: self.root.after_cancel(t["auto_reset_id"])
                except: pass
                t["auto_reset_id"] = None
            if t["running"]:
                t["running"] = False
                t["vars"]["act_btn"].config_bg(RED)
                t["vars"]["act_btn"].set_text("▶")
                t["vars"]["st_lbl"].config(text="已停止", fg=TEXT_LIGHT)
        self._save_tasks_config()

    # ---------- 定时启动任务 ----------

    def _add_launch_task(self, data=None):
        """添加一个定时启动任务"""
        if data is None:
            data = {}
        tid = self.next_launch_id
        self.next_launch_id += 1
        widget = self._make_launch_task_row(tid, data)
        self.launch_tasks.append(widget)
        self._save_tasks_config()

    def _make_launch_task_row(self, task_id, data):
        """构建定时启动任务行"""
        mode = data.get("mode", "fixed")
        hour = data.get("hour", 8)
        minute = data.get("minute", 0)
        cd_min = data.get("countdown_min", 30)
        enabled = data.get("enabled", True)
        inst_names = data.get("instances", [])

        color = GREEN

        task = {
            "id": task_id, "type": "launch",
            "running": False, "thread": None,
            "remaining": 0, "target_ts": 0, "enabled": enabled,
            "mode": mode, "hour": hour, "minute": minute, "cd_min": cd_min,
            "instances": list(inst_names),
            "update_id": None, "auto_reset_id": None, "_pending_update": False,
        }

        frame = tk.Frame(self.launch_tasks_frame, bg=CARD, bd=0)
        frame.pack(fill="x")
        tk.Frame(frame, bg=BORDER, height=1).pack(fill="x", side="bottom")

        row = tk.Frame(frame, bg=CARD)
        row.pack(fill="x", pady=4)

        # 启用勾选
        en_var = tk.BooleanVar(value=enabled)
        tk.Checkbutton(row, variable=en_var, bg=CARD,
                       activebackground=CARD, selectcolor=CARD,
                       command=lambda: self._on_launch_en_toggle(task, en_var)).pack(side="left", padx=(0, 3))
        task["en_var"] = en_var

        # 模式
        mode_var = tk.StringVar(value="定点" if mode == "fixed" else "倒计时")
        mode_combo = ttk.Combobox(row, textvariable=mode_var,
                                   values=["定点", "倒计时"], width=4,
                                   state="readonly", font=("Microsoft YaHei", 8))
        mode_combo.pack(side="left", padx=(0, 3))

        # 时间输入
        tf = tk.Frame(row, bg=CARD)
        tf.pack(side="left")
        ff = tk.Frame(tf, bg=CARD)
        h_spin = ttk.Spinbox(ff, from_=0, to=23, width=2,
                              font=("Consolas", 8), format="%02.0f")
        h_spin.pack(side="left")
        h_spin.set(f"{hour:02d}")
        tk.Label(ff, text=":", font=("Consolas", 8), bg=CARD, fg=TEXT).pack(side="left")
        m_spin = ttk.Spinbox(ff, from_=0, to=59, width=2,
                              font=("Consolas", 8), format="%02.0f")
        m_spin.pack(side="left")
        m_spin.set(f"{minute:02d}")

        cf = tk.Frame(tf, bg=CARD)
        cd_spin = ttk.Spinbox(cf, from_=1, to=999, width=3, font=("Consolas", 8))
        cd_spin.pack(side="left")
        cd_spin.set(str(cd_min))
        tk.Label(cf, text="分", font=("Microsoft YaHei", 8),
                 bg=CARD, fg=TEXT_SUB).pack(side="left", padx=1)

        def _switch_mode():
            m = mode_var.get()
            task["mode"] = "countdown" if m == "倒计时" else "fixed"
            if task["mode"] == "fixed":
                cf.pack_forget(); ff.pack(side="left")
            else:
                ff.pack_forget(); cf.pack(side="left")
            self._save_tasks_config()

        mode_combo.bind("<<ComboboxSelected>>", lambda e: _switch_mode())
        if mode == "fixed":
            ff.pack(side="left"); cf.pack_forget()
        else:
            ff.pack_forget(); cf.pack(side="left")

        # 实例选择
        inst_btn = RoundedButton(row, text=f"选择实例 ({len(inst_names)})",
                                 command=lambda: self._pick_instances(task, inst_btn),
                                 bg=GREEN, fg="white", font=("Microsoft YaHei", 7),
                                 padx=6, pady=1)
        inst_btn.pack(side="left", padx=(4, 0))

        # 状态
        st_lbl = tk.Label(row, text="待启动", font=("Microsoft YaHei", 8),
                          fg=TEXT_LIGHT, bg=CARD, anchor="w")
        st_lbl.pack(side="left", padx=(6, 0))

        # 按钮
        act_btn = RoundedButton(row, text="▶", command=lambda: _toggle(),
                                 bg=color, fg="white",
                                 font=("Consolas", 8, "bold"), padx=6, pady=0)
        act_btn.pack(side="right", padx=(2, 0))
        RoundedButton(row, text="×", command=lambda: _delete(),
                      bg=TEXT_LIGHT, fg="white",
                      font=("Consolas", 8, "bold"), padx=4, pady=0).pack(side="right", padx=(2, 0))

        def _toggle():
            _stop() if task["running"] else _start()

        def _calc_ts():
            try:
                if task["mode"] == "fixed":
                    h, m = int(h_spin.get()), int(m_spin.get())
                    t = datetime.now().replace(hour=h, minute=m, second=0, microsecond=0)
                    if t <= datetime.now():
                        t += timedelta(days=1)
                    return t.timestamp()
                else:
                    return (datetime.now() + timedelta(minutes=int(cd_spin.get()))).timestamp()
            except ValueError:
                return None

        def _start():
            if task["running"] or not task["enabled"]:
                return
            if not task["instances"]:
                messagebox.showinfo("提示", "请先选择要启动的实例")
                return
            ts = _calc_ts()
            if ts is None:
                return
            task["running"] = True
            task["target_ts"] = ts
            task["thread"] = threading.Thread(target=_loop, daemon=True)
            task["thread"].start()
            act_btn.config_bg(TEXT_LIGHT); act_btn.set_text("■")
            _update_status()
            self._save_tasks_config()

        def _stop():
            task["running"] = False
            for k in ("update_id", "auto_reset_id"):
                if task.get(k):
                    try: self.root.after_cancel(task[k])
                    except: pass
                    task[k] = None
            task["thread"] = None
            act_btn.config_bg(color); act_btn.set_text("▶")
            st_lbl.config(text="待启动", fg=TEXT_LIGHT)
            self._save_tasks_config()

        def _loop():
            while task["running"]:
                rem = int(task["target_ts"] - time.time())
                if rem <= 0:
                    self.root.after(0, _time_up); break
                task["remaining"] = rem
                if not task["_pending_update"]:
                    task["_pending_update"] = True
                    self.root.after(0, _update_status)
                time.sleep(0.5)

        def _time_up():
            if not task["running"]:
                return
            task["running"] = False
            act_btn.config_bg(color); act_btn.set_text("▶")
            st_lbl.config(text="启动中…", fg=YELLOW)

            dnconsole = self._ld_paths.get("dnconsole")
            if not dnconsole or not os.path.isfile(dnconsole):
                st_lbl.config(text="未找到 dnconsole", fg=RED)
                return

            instances = task["instances"][:]
            if not instances:
                st_lbl.config(text="无实例可选", fg=TEXT_LIGHT)
                return

            def _work():
                results = staggered_launch(
                    dnconsole, instances, interval_seconds=5,
                    on_status=lambda t: self.root.after(0, lambda: st_lbl.config(text=t[:30], fg=YELLOW)),
                )
                ok = sum(1 for _, s, _ in results if s)
                self.root.after(0, lambda: st_lbl.config(
                    text=f"完成 {ok}/{len(results)}", fg=GREEN if ok == len(results) else YELLOW))
                self.root.after(0, self._scan_and_display_instances)
                if task["mode"] == "fixed":
                    task["auto_reset_id"] = self.root.after(2000, lambda: self._autoreset_launch(task))
                self._save_tasks_config()

            threading.Thread(target=_work, daemon=True).start()

        def _auto_reset_local():
            task["auto_reset_id"] = None
            if not task["enabled"]:
                return
            h, m = int(h_spin.get()), int(m_spin.get())
            target = datetime.now().replace(hour=h, minute=m, second=0, microsecond=0) + timedelta(days=1)
            task["target_ts"] = target.timestamp()
            task["running"] = True
            task["thread"] = threading.Thread(target=_loop, daemon=True)
            task["thread"].start()
            act_btn.config_bg(TEXT_LIGHT); act_btn.set_text("■")
            st_lbl.config(text=f"明天 {h:02d}:{m:02d}", fg=GREEN)
            self._save_tasks_config()

        def _update_status():
            task["_pending_update"] = False
            if not task["enabled"]:
                st_lbl.config(text="已禁用", fg=TEXT_LIGHT); return
            if task["running"]:
                rem = task["remaining"]
                h, m, s = rem // 3600, (rem % 3600) // 60, rem % 60
                if task["mode"] == "fixed":
                    ts = datetime.fromtimestamp(task["target_ts"]).strftime("%H:%M")
                    st_lbl.config(text=f"{ts} {h:02d}:{m:02d}:{s:02d}", fg=YELLOW)
                else:
                    st_lbl.config(text=f"{h:02d}:{m:02d}:{s:02d}", fg=YELLOW)
            else:
                st_lbl.config(text="待启动", fg=TEXT_LIGHT)

        def _delete():
            if task["running"]:
                _stop()
            frame.destroy()
            for i, t in enumerate(self.launch_tasks):
                if t["id"] == task_id:
                    self.launch_tasks.pop(i); break
            self._save_tasks_config()

        task["_auto_reset_fn"] = _auto_reset_local
        task["vars"] = {
            "h_spin": h_spin, "m_spin": m_spin, "cd_spin": cd_spin,
            "st_lbl": st_lbl, "act_btn": act_btn, "inst_btn": inst_btn,
        }
        return task

    def _on_launch_en_toggle(self, task, en_var):
        task["enabled"] = en_var.get()
        if not task["enabled"] and task["running"]:
            task["running"] = False
            task["vars"]["act_btn"].config_bg(GREEN)
            task["vars"]["act_btn"].set_text("▶")
            task["vars"]["st_lbl"].config(text="已禁用", fg=TEXT_LIGHT)
        self._save_tasks_config()

    def _pick_instances(self, task, btn):
        """弹出实例选择对话框"""
        if not self._instances:
            messagebox.showinfo("提示", "未检测到实例，请先扫描")
            return

        win = tk.Toplevel(self.root)
        win.title("选择实例")
        win.geometry("300x350")
        win.configure(bg=BG)
        win.transient(self.root)
        win.grab_set()

        tk.Label(win, text="勾选要启动的实例:", font=self.f_sec,
                 bg=BG, fg=TEXT).pack(pady=(8, 4))

        f = tk.Frame(win, bg=BG)
        f.pack(fill="both", expand=True, padx=12)

        canvas = tk.Canvas(f, bg=CARD, highlightthickness=0)
        sb = ttk.Scrollbar(f, orient="vertical", command=canvas.yview)
        inner = tk.Frame(canvas, bg=CARD)
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=inner, anchor="nw", tags="inner")
        canvas.configure(yscrollcommand=sb.set)
        canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        vars_dict = {}
        for inst in self._instances:
            checked = inst['name'] in task['instances']
            var = tk.BooleanVar(value=checked)
            cb = tk.Checkbutton(inner, text=inst['name'], variable=var,
                                bg=CARD, fg=TEXT, activebackground=CARD,
                                selectcolor=CARD, anchor="w")
            cb.pack(fill="x", padx=8, pady=1)
            vars_dict[inst['name']] = var

        def _confirm():
            selected = [name for name, var in vars_dict.items() if var.get()]
            if not selected:
                messagebox.showinfo("提示", "请至少选一个实例")
                return
            task['instances'] = selected
            btn.set_text(f"选择实例 ({len(selected)})")
            self._save_tasks_config()
            win.destroy()

        RoundedButton(win, text="确定", command=_confirm,
                      bg=GREEN, fg="white", font=("Microsoft YaHei", 9),
                      padx=16).pack(pady=(6, 10))

    def _start_all_launch(self):
        """全部启动所有定时启动任务"""
        for t in self.launch_tasks:
            if t["enabled"] and not t["running"]:
                self._inline_start_launch(t)

    def _inline_start_launch(self, t):
        if t["running"] or not t["enabled"]:
            return
        if not t["instances"]:
            return
        try:
            if t["mode"] == "fixed":
                h = int(t["vars"]["h_spin"].get())
                m = int(t["vars"]["m_spin"].get())
                target = datetime.now().replace(hour=h, minute=m, second=0, microsecond=0)
                if target <= datetime.now():
                    target += timedelta(days=1)
                ts = target.timestamp()
            else:
                ts = (datetime.now() + timedelta(minutes=int(t["vars"]["cd_spin"].get()))).timestamp()
        except ValueError:
            return
        t["running"] = True; t["target_ts"] = ts
        t["thread"] = threading.Thread(target=self._make_launch_loop_fn(t), daemon=True)
        t["thread"].start()
        t["vars"]["act_btn"].config_bg(TEXT_LIGHT)
        t["vars"]["act_btn"].set_text("■")
        self._update_launch_status(t)
        self._save_tasks_config()

    def _make_launch_loop_fn(self, t):
        def _loop():
            while t["running"]:
                rem = int(t["target_ts"] - time.time())
                if rem <= 0:
                    self.root.after(0, lambda: self._launch_task_time_up(t)); break
                t["remaining"] = rem
                if not t.get("_pending_update"):
                    t["_pending_update"] = True
                    self.root.after(0, lambda: self._update_launch_status(t))
                time.sleep(0.5)
        return _loop

    def _launch_task_time_up(self, t):
        if not t["running"]:
            return
        t["running"] = False
        t["vars"]["act_btn"].config_bg(GREEN)
        t["vars"]["act_btn"].set_text("▶")
        t["vars"]["st_lbl"].config(text="启动中…", fg=YELLOW)

        dnconsole = self._ld_paths.get("dnconsole")
        if not dnconsole or not os.path.isfile(dnconsole):
            t["vars"]["st_lbl"].config(text="未找到 dnconsole", fg=RED)
            return

        instances = t["instances"][:]
        if not instances:
            t["vars"]["st_lbl"].config(text="无实例可选", fg=TEXT_LIGHT)
            return

        def _work():
            results = staggered_launch(
                dnconsole, instances, interval_seconds=5,
                on_status=lambda s: self.root.after(0, lambda: t["vars"]["st_lbl"].config(text=s[:30], fg=YELLOW)),
            )
            ok = sum(1 for _, s, _ in results if s)
            self.root.after(0, lambda: t["vars"]["st_lbl"].config(
                text=f"完成 {ok}/{len(results)}", fg=GREEN if ok == len(results) else YELLOW))
            self.root.after(0, self._scan_and_display_instances)
            if t["mode"] == "fixed":
                t["auto_reset_id"] = self.root.after(2000, lambda: self._autoreset_launch(t))
            self._save_tasks_config()

        threading.Thread(target=_work, daemon=True).start()

    def _autoreset_launch(self, t):
        t["auto_reset_id"] = None
        if not t["enabled"]:
            return
        h, m = int(t["vars"]["h_spin"].get()), int(t["vars"]["m_spin"].get())
        target = datetime.now().replace(hour=h, minute=m, second=0, microsecond=0) + timedelta(days=1)
        t["target_ts"] = target.timestamp()
        t["running"] = True
        t["thread"] = threading.Thread(target=self._make_launch_loop_fn(t), daemon=True)
        t["thread"].start()
        t["vars"]["act_btn"].config_bg(TEXT_LIGHT)
        t["vars"]["act_btn"].set_text("■")
        t["vars"]["st_lbl"].config(text=f"明天 {h:02d}:{m:02d}", fg=GREEN)
        self._save_tasks_config()

    def _update_launch_status(self, t):
        t["_pending_update"] = False
        if not t["enabled"]:
            t["vars"]["st_lbl"].config(text="已禁用", fg=TEXT_LIGHT); return
        if t["running"]:
            rem = t["remaining"]
            h, m, s = rem // 3600, (rem % 3600) // 60, rem % 60
            if t["mode"] == "fixed":
                ts = datetime.fromtimestamp(t["target_ts"]).strftime("%H:%M")
                t["vars"]["st_lbl"].config(text=f"{ts} {h:02d}:{m:02d}:{s:02d}", fg=YELLOW)
            else:
                t["vars"]["st_lbl"].config(text=f"{h:02d}:{m:02d}:{s:02d}", fg=YELLOW)
        else:
            t["vars"]["st_lbl"].config(text="待启动", fg=TEXT_LIGHT)

    def _stop_all_launch(self):
        """停止所有定时启动任务"""
        for t in self.launch_tasks:
            if t.get("auto_reset_id"):
                try: self.root.after_cancel(t["auto_reset_id"])
                except: pass
                t["auto_reset_id"] = None
            if t["running"]:
                t["running"] = False
                t["vars"]["act_btn"].config_bg(GREEN)
                t["vars"]["act_btn"].set_text("▶")
                t["vars"]["st_lbl"].config(text="已停止", fg=TEXT_LIGHT)
        self._save_tasks_config()

    # ---------- 立即关闭（原有按钮，使用优雅关闭） ----------

    def _on_kill_now(self):
        if not self._emu_procs_cache:
            messagebox.showinfo("提示", "未检测到模拟器进程。")
            return

        should_shutdown = self.shutdown_var.get()
        action_text = "并关机" if should_shutdown else ""
        if should_shutdown and self.restart_var.get():
            action_text = "并重启"

        t_labels = {'ld': '雷电', 'mumu': 'MuMu'}
        details = "\n".join([
            f"  [{t_labels.get(p['type'], '?')}] PID:{p['pid']}  {p['name']}"
            for p in self._emu_procs_cache
        ])
        if not messagebox.askyesno("确认操作",
                                    f"即将关闭以下 {len(self._emu_procs_cache)} 个模拟器{action_text}：\n\n{details}\n\n确定继续？",
                                    icon="warning"):
            return

        def _on_done(count, success, fail_count, failed_names, backup_msg, shutdown_exec):
            if count == 0:
                msg = "未检测到模拟器进程"
            else:
                msg = f"已关闭 {success}/{count} 个模拟器"
            if failed_names:
                msg += "\n\n无法关闭：\n" + "\n".join(f"  {f}" for f in failed_names)
            if should_shutdown and success > 0:
                msg += f"\n\n即将{'重启' if self.restart_var.get() else '关机'}..."
            messagebox.showinfo("操作完成", msg)

        graceful_kill_async(on_done=_on_done, do_backup=True, do_shutdown=should_shutdown,
                           should_restart=self.restart_var.get())

    def _on_auto_start_toggle(self):
        enable = self.auto_start_var.get()
        ok, err = set_auto_start(enable)
        if not ok:
            messagebox.showerror("设置失败", f"开机自启动设置失败：\n{err}")
            self.auto_start_var.set(not enable)

    # ---------- 模拟器扫描 ----------

    def _start_scan_loop(self):
        self._trigger_scan()
        self.scan_timer_id = self.root.after(3000, self._start_scan_loop)

    def _trigger_scan(self):
        if self._destroyed or self._emu_scan_pending:
            return
        self._emu_scan_pending = True
        scan_emulators_in_background(self._on_scan_result)

    def _on_scan_result(self, procs):
        if self._destroyed:
            return
        self._emu_scan_pending = False
        self._emu_procs_cache = procs

    def _refresh_emu(self):
        self._emu_scan_pending = False
        self._trigger_scan()

    # ---------- 关机由任务触发，无需独立方法 ----------

    # ---------- 配置持久化 ----------

    def _save_tasks_config(self):
        config = load_tool_config()
        shutdown_data = []
        for t in self.shutdown_tasks:
            try:
                shutdown_data.append({
                    "mode": t["mode"],
                    "hour": int(t["vars"]["h_spin"].get()),
                    "minute": int(t["vars"]["m_spin"].get()),
                    "countdown_min": int(t["vars"]["cd_spin"].get()),
                    "enabled": t["en_var"].get(),
                })
            except (KeyError, ValueError):
                pass
        config["shutdown_tasks"] = shutdown_data

        launch_data = []
        for t in self.launch_tasks:
            try:
                launch_data.append({
                    "mode": t["mode"],
                    "hour": int(t["vars"]["h_spin"].get()),
                    "minute": int(t["vars"]["m_spin"].get()),
                    "countdown_min": int(t["vars"]["cd_spin"].get()),
                    "enabled": t["en_var"].get(),
                    "instances": list(t.get("instances", [])),
                })
            except (KeyError, ValueError):
                pass
        config["launch_tasks"] = launch_data

        config["auto_launch"] = self.auto_launch_var.get()
        config["auto_launch_instances"] = list(self._auto_launch_instances)
        config["shutdown_always"] = self.shutdown_var.get()
        config["restart_always"] = self.restart_var.get()
        save_tool_config(config)

    def _load_tasks_config(self):
        config = load_tool_config()
        shutdown_data = config.get("shutdown_tasks", None)
        if shutdown_data is not None:
            for td in shutdown_data:
                self._add_task("shutdown", td)
        launch_data = config.get("launch_tasks", None)
        if launch_data is not None:
            for td in launch_data:
                self._add_launch_task(td)
        self.auto_launch_var.set(config.get("auto_launch", False))
        self.shutdown_var.set(config.get("shutdown_always", True))
        self.restart_var.set(config.get("restart_always", False))
        for t in self.shutdown_tasks:
            if t["enabled"] and t["mode"] == "fixed":
                self._inline_start(t)
        for t in self.launch_tasks:
            if t["enabled"] and t["mode"] == "fixed" and t.get("instances"):
                self._inline_start_launch(t)

    # ---------- 实例管理 ----------

    def _init_instance_manager(self):
        """初始化实例管理器：探测路径、扫描实例"""
        self._load_auto_launch_instances()
        saved = get_saved_paths()

        # 尝试使用保存的路径
        if saved.get("ld_path") and os.path.isfile(saved.get("dnconsole", "")):
            self._ld_paths = saved
            self.ld_path_var.set(f"LDPlayer: {saved['ld_path']}")
            self._scan_and_display_instances()
            return

        # 自动探测
        self.ld_path_var.set("正在自动搜索 LDPlayer...")
        threading.Thread(target=self._auto_detect_thread, daemon=True).start()

    def _auto_detect_thread(self):
        """后台自动探测路径"""
        paths = auto_detect_paths()
        self._ld_paths = paths
        self.root.after(0, lambda: self._on_detect_done(paths))

    def _on_detect_done(self, paths):
        if paths.get("ld_path"):
            save_paths(paths)
            self.ld_path_var.set(f"LDPlayer: {paths['ld_path']}")
            self._scan_and_display_instances()
        else:
            self.ld_path_var.set("未检测到 LDPlayer，点击刷新手动搜索")
            if messagebox.askyesno("未检测到 LDPlayer",
                                   "未在常见路径中找到 LDPlayer。\n"
                                   "是否手动选择安装目录？"):
                self._manual_select_ld_path()

    def _manual_select_ld_path(self):
        """手动选择 LDPlayer 安装目录"""
        path = filedialog.askdirectory(title="选择 LDPlayer 安装目录")
        if path and os.path.isfile(os.path.join(path, 'dnconsole.exe')):
            paths = {
                "ld_path": path,
                "dnconsole": os.path.join(path, "dnconsole.exe"),
                "multiplayer_path": None,
                "dnmultiplayerex": None,
                "vms_config_dir": None,
            }
            # 尝试找 vms config
            vms_cfg = os.path.join(path, "vms", "config")
            if os.path.isdir(vms_cfg):
                paths["vms_config_dir"] = vms_cfg
            save_paths(paths)
            self._ld_paths = paths
            self.ld_path_var.set(f"LDPlayer: {path}")
            self._scan_and_display_instances()
        elif path:
            messagebox.showerror("错误", "所选目录中未找到 dnconsole.exe")

    def _scan_and_display_instances(self):
        """扫描并显示实例列表"""
        vms_cfg = self._ld_paths.get("vms_config_dir")
        if not vms_cfg:
            # 尝试在多开器目录找
            mp = self._ld_paths.get("multiplayer_path")
            if mp:
                vms_cfg = os.path.join(mp, "vms", "config")
                if not os.path.isdir(vms_cfg):
                    vms_cfg = None
        if not vms_cfg:
            self.ld_path_var.set(f"LDPlayer: {self._ld_paths.get('ld_path', '?')} | 未找到实例配置")
            return

        instances = scan_instances(vms_cfg)
        dnconsole = self._ld_paths.get("dnconsole")
        check_running_instances(instances, dnconsole)
        self._instances = instances

        # 清空旧行
        for w in self.inst_rows_frame.winfo_children():
            w.destroy()

        if not instances:
            tk.Label(self.inst_rows_frame, text="  未找到实例",
                     font=("Microsoft YaHei", 9), bg=CARD, fg=TEXT_SUB).pack(fill="x")
            return

        self._inst_vars = []
        for inst in instances:
            row = tk.Frame(self.inst_rows_frame, bg=CARD)
            row.pack(fill="x", pady=1)

            checked = inst['name'] in self._auto_launch_instances
            var = tk.BooleanVar(value=checked)
            cb = tk.Checkbutton(row, variable=var, bg=CARD,
                                activebackground=CARD, selectcolor=CARD, width=4,
                                command=self._save_auto_launch_instances)
            cb.pack(side="left")
            self._inst_vars.append((var, inst))

            summary = get_instance_summary(inst['settings'])
            root_mark = "✓" if summary['root'] else "✗"
            set_text = f"{summary['cpu']}核 {summary['memory']}M Root:{root_mark}"

            # 显示实例名 + 自定义名字
            display_name = inst['name']
            if summary['name']:
                display_name = f"{inst['name']} ({summary['name']})"
            tk.Label(row, text=display_name, font=("Microsoft YaHei", 9, "bold"),
                     bg=CARD, fg=TEXT, width=18, anchor="w").pack(side="left")
            tk.Label(row, text=set_text, font=("Microsoft YaHei", 9),
                     bg=CARD, fg=TEXT, width=30, anchor="w").pack(side="left")

            status = "运行中" if inst['running'] else "已停止"
            color = GREEN if inst['running'] else TEXT_LIGHT
            tk.Label(row, text=status, font=("Microsoft YaHei", 9),
                     bg=CARD, fg=color, width=10, anchor="w").pack(side="left")

        self.ld_path_var.set(
            f"LDPlayer: {self._ld_paths.get('ld_path', '?')} | "
            f"多开器: {self._ld_paths.get('multiplayer_path', '未找到')} | "
            f"实例: {len(instances)} 个"
        )

        if not self._startup_launch_done:
            self.root.after(500, self._auto_launch_on_startup)

    def _refresh_instances(self):
        """刷新实例列表"""
        self.ld_path_var.set("正在搜索...")
        threading.Thread(target=self._auto_detect_thread, daemon=True).start()

    def _get_selected_instances(self):
        """获取勾选的实例名列表"""
        if not hasattr(self, '_inst_vars') or not self._inst_vars:
            return []
        return [inst['name'] for var, inst in self._inst_vars if var.get()]

    def _save_auto_launch_instances(self):
        """保存自启动实例列表"""
        self._auto_launch_instances = set(self._get_selected_instances())
        config = load_tool_config()
        config["auto_launch_instances"] = list(self._auto_launch_instances)
        save_tool_config(config)

    def _load_auto_launch_instances(self):
        """加载自启动实例列表"""
        config = load_tool_config()
        self._auto_launch_instances = set(config.get("auto_launch_instances", []))
        return self._auto_launch_instances

    def _auto_launch_on_startup(self):
        """软件启动时自动启动勾选的实例（先恢复配置，再启动）"""
        if self._startup_launch_done:
            return
        self._startup_launch_done = True  # 无论是否启用，只执行一次
        if not self.auto_launch_var.get():
            return
        if not self._auto_launch_instances:
            return
        dnconsole = self._ld_paths.get("dnconsole")
        if not dnconsole or not os.path.isfile(dnconsole):
            return

        self._startup_launch_done = True
        selected = [name for name in self._auto_launch_instances
                    if any(inst['name'] == name for inst in self._instances)]
        if not selected:
            return

        try:
            interval = int(self.launch_interval_var.get())
        except ValueError:
            interval = 5

        def _work():
            self.root.after(0, lambda: self.launch_status_var.set("正在恢复配置..."))
            vms_cfg = self._ld_paths.get("vms_config_dir")
            mp_cfg = None
            mp = self._ld_paths.get("multiplayer_path")
            if mp:
                mp_cfg = os.path.join(mp, "vms", "config")

            snapshots = list_snapshots(SNAPSHOT_DIR)
            if snapshots:
                latest = snapshots[0]
                count, msg = restore_snapshot(latest['path'], vms_cfg, mp_cfg)
                self.root.after(0, lambda: self.launch_status_var.set(f"已恢复配置: {msg}"))
                time.sleep(1)
            else:
                self.root.after(0, lambda: self.launch_status_var.set("无快照，跳过恢复"))

            self.root.after(0, lambda: self.launch_status_var.set(f"正在启动 {len(selected)} 个实例..."))
            results = staggered_launch(
                dnconsole, selected, interval,
                on_status=lambda t: self.root.after(0, lambda: self.launch_status_var.set(t)),
            )
            ok = sum(1 for _, s, _ in results if s)
            self.root.after(0, lambda: self.launch_status_var.set(f"自启动完成 {ok}/{len(results)}"))
            self.root.after(0, self._scan_and_display_instances)

        threading.Thread(target=_work, daemon=True).start()

    def _edit_instance_settings(self):
        """编辑选中实例的设置"""
        selected = self._get_selected_instances()
        if not selected:
            messagebox.showinfo("提示", "请先勾选要编辑的实例")
            return
        if len(selected) > 1:
            messagebox.showinfo("提示", "请只勾选一个实例进行编辑")
            return

        inst_name = selected[0]
        inst = None
        for i in self._instances:
            if i['name'] == inst_name:
                inst = i
                break
        if not inst:
            return

        self._open_settings_editor(inst)

    def _open_settings_editor(self, inst):
        """打开设置编辑窗口"""
        win = tk.Toplevel(self.root)
        win.title(f"编辑 - {inst['name']}")
        win.geometry("420x480")
        win.configure(bg=BG)
        win.transient(self.root)
        win.grab_set()

        settings = inst['settings']
        summary = get_instance_summary(settings)

        tk.Label(win, text=f"实例: {inst['name']}", font=self.f_sec,
                 bg=BG, fg=TEXT).pack(pady=(12, 8))

        form = tk.Frame(win, bg=BG, padx=20)
        form.pack(fill="x")

        fields = {}

        # 分辨率
        tk.Label(form, text="分辨率:", font=("Microsoft YaHei", 10),
                 bg=BG, fg=TEXT, width=10, anchor="w").grid(row=0, column=0, pady=4, sticky="w")
        res_frame = tk.Frame(form, bg=BG)
        res_frame.grid(row=0, column=1, pady=4, sticky="w")
        w_var = tk.StringVar(value=str(summary['resolution'].split('x')[0]))
        h_var = tk.StringVar(value=str(summary['resolution'].split('x')[1]))
        tk.Entry(res_frame, textvariable=w_var, width=6, font=("Consolas", 10)).pack(side="left")
        tk.Label(res_frame, text="x", bg=BG, fg=TEXT).pack(side="left", padx=2)
        tk.Entry(res_frame, textvariable=h_var, width=6, font=("Consolas", 10)).pack(side="left")
        fields['resolution'] = (w_var, h_var)

        # DPI
        tk.Label(form, text="DPI:", font=("Microsoft YaHei", 10),
                 bg=BG, fg=TEXT, width=10, anchor="w").grid(row=1, column=0, pady=4, sticky="w")
        dpi_var = tk.StringVar(value=str(settings.get('advancedSettings.resolutionDpi', 240)))
        tk.Entry(form, textvariable=dpi_var, width=10, font=("Consolas", 10)).grid(
            row=1, column=1, pady=4, sticky="w")
        fields['dpi'] = dpi_var

        # CPU
        tk.Label(form, text="CPU核心:", font=("Microsoft YaHei", 10),
                 bg=BG, fg=TEXT, width=10, anchor="w").grid(row=2, column=0, pady=4, sticky="w")
        cpu_var = tk.StringVar(value=str(summary['cpu']))
        tk.Spinbox(form, from_=1, to=8, textvariable=cpu_var, width=8,
                   font=("Consolas", 10)).grid(row=2, column=1, pady=4, sticky="w")
        fields['cpu'] = cpu_var

        # 内存
        tk.Label(form, text="内存(MB):", font=("Microsoft YaHei", 10),
                 bg=BG, fg=TEXT, width=10, anchor="w").grid(row=3, column=0, pady=4, sticky="w")
        mem_var = tk.StringVar(value=str(summary['memory']))
        tk.Spinbox(form, from_=256, to=8192, increment=256, textvariable=mem_var, width=8,
                   font=("Consolas", 10)).grid(row=3, column=1, pady=4, sticky="w")
        fields['memory'] = mem_var

        # Root
        root_var = tk.BooleanVar(value=summary['root'])
        tk.Checkbutton(form, text="Root 权限", variable=root_var,
                       font=("Microsoft YaHei", 10), bg=BG, fg=TEXT,
                       selectcolor=CARD, activebackground=BG
                       ).grid(row=4, column=0, columnspan=2, pady=4, sticky="w")
        fields['root'] = root_var

        # 自动启动
        autorun_var = tk.BooleanVar(value=summary['auto_run'])
        tk.Checkbutton(form, text="开机自动启动", variable=autorun_var,
                       font=("Microsoft YaHei", 10), bg=BG, fg=TEXT,
                       selectcolor=CARD, activebackground=BG
                       ).grid(row=5, column=0, columnspan=2, pady=4, sticky="w")
        fields['auto_run'] = autorun_var

        # 帧率
        tk.Label(form, text="帧率:", font=("Microsoft YaHei", 10),
                 bg=BG, fg=TEXT, width=10, anchor="w").grid(row=6, column=0, pady=4, sticky="w")
        fps_var = tk.StringVar(value=str(summary['fps']))
        ttk.Combobox(form, textvariable=fps_var, values=["20", "30", "60", "120"],
                     width=8, font=("Consolas", 10), state="readonly").grid(
            row=6, column=1, pady=4, sticky="w")
        fields['fps'] = fps_var

        # 设备名
        tk.Label(form, text="设备名:", font=("Microsoft YaHei", 10),
                 bg=BG, fg=TEXT, width=10, anchor="w").grid(row=7, column=0, pady=4, sticky="w")
        name_var = tk.StringVar(value=summary['name'])
        tk.Entry(form, textvariable=name_var, width=20, font=("Consolas", 10)).grid(
            row=7, column=1, pady=4, sticky="w")
        fields['dev_name'] = name_var

        def _save():
            if inst['running']:
                messagebox.showwarning("警告", "实例正在运行中，请先关闭再修改设置")
                return
            try:
                new_settings = dict(settings)
                new_settings['advancedSettings.resolution'] = {
                    "width": int(fields['resolution'][0].get()),
                    "height": int(fields['resolution'][1].get()),
                }
                new_settings['basicSettings.width'] = int(fields['resolution'][0].get())
                new_settings['basicSettings.height'] = int(fields['resolution'][1].get())
                new_settings['advancedSettings.resolutionDpi'] = int(fields['dpi'].get())
                new_settings['advancedSettings.cpuCount'] = int(fields['cpu'].get())
                new_settings['advancedSettings.memorySize'] = int(fields['memory'].get())
                new_settings['basicSettings.rootMode'] = fields['root'].get()
                new_settings['basicSettings.autoRun'] = fields['auto_run'].get()
                new_settings['basicSettings.fps'] = int(fields['fps'].get())
                new_settings['statusSettings.playerName'] = fields['dev_name'].get()

                ok, msg = write_instance_config(inst['config_path'], new_settings)
                if ok:
                    messagebox.showinfo("成功", f"{inst['name']} 设置已保存")
                    win.destroy()
                    self._scan_and_display_instances()
                else:
                    messagebox.showerror("失败", msg)
            except Exception as e:
                messagebox.showerror("错误", f"保存失败: {str(e)}")

        btn_frame = tk.Frame(win, bg=BG)
        btn_frame.pack(fill="x", padx=20, pady=12)
        RoundedButton(btn_frame, text="保存", command=_save,
                      bg=GREEN, fg="white", font=("Microsoft YaHei", 10, "bold"),
                      padx=16).pack(side="left", padx=(0, 8))
        RoundedButton(btn_frame, text="取消", command=win.destroy,
                      bg=TEXT_LIGHT, fg="white", font=("Microsoft YaHei", 10),
                      padx=16).pack(side="left")

    def _save_snapshot(self):
        """保存配置快照"""
        vms_cfg = self._ld_paths.get("vms_config_dir")
        mp_cfg = None
        mp = self._ld_paths.get("multiplayer_path")
        if mp:
            mp_cfg = os.path.join(mp, "vms", "config")

        snap_dir, msg = save_snapshot(vms_cfg, mp_cfg, SNAPSHOT_DIR)
        if snap_dir:
            messagebox.showinfo("保存成功", msg)
        else:
            messagebox.showerror("保存失败", msg)

    def _restore_snapshot(self):
        """恢复配置快照"""
        snapshots = list_snapshots(SNAPSHOT_DIR)
        if not snapshots:
            messagebox.showinfo("提示", "没有可用的快照")
            return

        # 选择快照
        win = tk.Toplevel(self.root)
        win.title("选择快照")
        win.geometry("360x300")
        win.configure(bg=BG)
        win.transient(self.root)
        win.grab_set()

        tk.Label(win, text="选择要恢复的快照:", font=self.f_sec,
                 bg=BG, fg=TEXT).pack(pady=(12, 8))

        listbox = tk.Listbox(win, font=("Consolas", 10), bg=CARD, fg=TEXT,
                             selectbackground=ACCENT, selectforeground="white",
                             relief="flat", bd=0, highlightthickness=0)
        listbox.pack(fill="both", expand=True, padx=20, pady=(0, 8))

        for snap in snapshots:
            listbox.insert(tk.END, f"  {snap['name']}  ({snap['instance_count']}个实例)")

        def _do_restore():
            sel = listbox.curselection()
            if not sel:
                messagebox.showinfo("提示", "请选一个快照")
                return
            snap = snapshots[sel[0]]
            vms_cfg = self._ld_paths.get("vms_config_dir")
            mp_cfg = None
            mp = self._ld_paths.get("multiplayer_path")
            if mp:
                mp_cfg = os.path.join(mp, "vms", "config")
            count, msg = restore_snapshot(snap['path'], vms_cfg, mp_cfg)
            messagebox.showinfo("恢复完成", msg)
            win.destroy()
            self._scan_and_display_instances()

        RoundedButton(win, text="恢复", command=_do_restore,
                      bg=GREEN, fg="white", font=("Microsoft YaHei", 10, "bold"),
                      padx=16).pack(pady=(0, 12))

    def _show_backup_list(self, restore_geometry=None):
        """显示自动备份列表（扫描多个可能的位置）"""
        # ==== 恢复上次窗口位置 ====
        if restore_geometry is None:
            try:
                cfg = load_tool_config()
                restore_geometry = cfg.get('dialog_geometry', {}).get('backup_list')
            except Exception:
                pass

        # 扫描多个可能的备份位置
        possible_dirs = []
        script_dir = os.path.dirname(os.path.abspath(__file__)) if not getattr(sys, 'frozen', False) else None
        if script_dir:
            possible_dirs.append(os.path.join(script_dir, '备份'))
        if getattr(sys, 'frozen', False):
            exe_dir = os.path.dirname(os.path.abspath(sys.executable))
            possible_dirs.append(os.path.join(exe_dir, '备份'))
            parent = os.path.dirname(exe_dir)
            if parent != exe_dir:
                possible_dirs.append(os.path.join(parent, '备份'))

        unique_dirs = []
        for d in possible_dirs:
            n = os.path.normpath(d)
            if n not in unique_dirs:
                unique_dirs.append(n)

        all_entries = []
        seen_names = set()
        for backup_root in unique_dirs:
            if not os.path.isdir(backup_root):
                continue
            for name in os.listdir(backup_root):
                d = os.path.join(backup_root, name)
                if os.path.isdir(d) and name.startswith("雷电配置备份_") and name not in seen_names:
                    seen_names.add(name)
                    fcount = 0
                    for root, _, files in os.walk(d):
                        fcount += len(files)
                    all_entries.append((d, name, fcount, backup_root))

        if not all_entries:
            messagebox.showinfo("备份列表", "暂无备份记录")
            return

        all_entries.sort(key=lambda x: x[1], reverse=True)

        try:
            win = tk.Toplevel(self.root)
            win.title("自动备份列表")
            win.geometry(restore_geometry or "560x400")
            win.configure(bg=BG)
            win.transient(self.root)
            win.grab_set()

            tk.Label(win, text="自动备份（每次关闭时生成）", font=self.f_sec,
                     bg=BG, fg=PRIMARY).pack(pady=(10, 4))
            tk.Label(win, text=f"共 {len(all_entries)} 份，自动保留最新 20 份，支持多选（Ctrl/Shift+点击）",
                     font=self.f_small, bg=BG, fg=TEXT_SUB).pack(pady=(0, 6))

            lb_frame = tk.Frame(win, bg=BG)
            lb_frame.pack(fill="both", expand=True, padx=12, pady=(0, 4))
            listbox = tk.Listbox(lb_frame, font=("Consolas", 10), bg=CARD, fg=TEXT,
                                 selectbackground=ACCENT, selectforeground="white",
                                 selectmode=tk.EXTENDED,
                                 relief="flat", bd=0, highlightthickness=0)
            scrollbar = ttk.Scrollbar(lb_frame, orient="vertical", command=listbox.yview)
            listbox.configure(yscrollcommand=scrollbar.set)
            listbox.pack(side="left", fill="both", expand=True)
            scrollbar.pack(side="right", fill="y")

            for d, name, fcount, _ in all_entries:
                ts = name.replace("雷电配置备份_", "")
                listbox.insert(tk.END, f"  {ts}  |  {fcount} 个文件")

            btn_row = tk.Frame(win, bg=BG)
            btn_row.pack(fill="x", padx=12, pady=(0, 10))

            # 关闭时保存位置
            def _on_close():
                _save_geometry(win.geometry())
                win.destroy()

            def _save_geometry(geo):
                try:
                    cfg = load_tool_config()
                    if 'dialog_geometry' not in cfg:
                        cfg['dialog_geometry'] = {}
                    cfg['dialog_geometry']['backup_list'] = geo
                    save_tool_config(cfg)
                except Exception:
                    pass

            def on_delete():
                sel = listbox.curselection()
                if not sel:
                    messagebox.showinfo("提示", "请先选中要删除的备份")
                    return
                geo = win.geometry()
                names = [all_entries[i][1] for i in sel]
                if messagebox.askyesno("确认删除", f"删除选中的 {len(sel)} 个备份？\n\n" + "\n".join(f"  {n}" for n in names)):
                    for i in reversed(sel):
                        shutil.rmtree(all_entries[i][0], ignore_errors=True)
                    win.destroy()
                    self._show_backup_list(geo)

            win.protocol("WM_DELETE_WINDOW", _on_close)
            RoundedButton(btn_row, text="删除选中", command=on_delete,
                          bg=RED, fg="white", font=("Microsoft YaHei", 9),
                          padx=10).pack(side="left", padx=(0, 6))
            RoundedButton(btn_row, text="关闭", command=_on_close,
                          bg=TEXT_LIGHT, fg="white", font=("Microsoft YaHei", 9),
                          padx=10).pack(side="right")
        except Exception as e:
            import traceback
            err_msg = f"弹窗异常: {e}\n\n{traceback.format_exc()}"
            exe_dir = os.path.dirname(os.path.abspath(sys.executable)) if getattr(sys, 'frozen', False) else os.path.dirname(os.path.abspath(__file__))
            try:
                with open(os.path.join(exe_dir, "_backup_error.log"), 'w', encoding='utf-8') as f:
                    f.write(err_msg)
            except Exception:
                pass
            messagebox.showerror("备份列表异常",
                f"发生了未预期的错误，已记录到日志。\n\n{err_msg}")

    def _show_snapshot_list(self, restore_geometry=None):
        """显示手动快照列表"""
        # ==== 恢复上次窗口位置 ====
        if restore_geometry is None:
            try:
                cfg = load_tool_config()
                restore_geometry = cfg.get('dialog_geometry', {}).get('snapshot_list')
            except Exception:
                pass

        # ==== 扫描 ====
        exe_dir = os.path.dirname(os.path.abspath(sys.executable)) if getattr(sys, 'frozen', False) else os.path.dirname(os.path.abspath(__file__))
        scan_paths = set()
        scan_paths.add(os.path.normpath(os.path.join(exe_dir, '快照')))
        scan_paths.add(os.path.normpath(os.path.join(os.getcwd(), '快照')))

        all_snapshots = []
        seen = set()
        for d in scan_paths:
            if not os.path.isdir(d):
                continue
            for name in sorted(os.listdir(d), reverse=True):
                snap_dir = os.path.join(d, name)
                if not os.path.isdir(snap_dir) or name in seen:
                    continue
                seen.add(name)
                meta_path = os.path.join(snap_dir, 'snapshot_meta.json')
                meta = {}
                if os.path.isfile(meta_path):
                    try:
                        with open(meta_path, 'r', encoding='utf-8') as f:
                            meta = json.load(f)
                    except Exception:
                        pass
                all_snapshots.append({
                    "name": name,
                    "path": snap_dir,
                    "instance_count": meta.get("instance_count", 0),
                    "timestamp": meta.get("timestamp", name),
                })

        if not all_snapshots:
            messagebox.showinfo("快照列表", "暂无手动快照\n请在调整好实例后点击「保存快照」创建")
            return

        # ==== 弹窗 ====
        try:
            win = tk.Toplevel(self.root)
            win.title("手动快照列表")
            win.geometry(restore_geometry or "560x400")
            win.configure(bg=BG)
            win.transient(self.root)
            win.grab_set()

            # 关闭时保存位置
            def _on_close():
                _save_geometry(win.geometry())
                win.destroy()

            tk.Label(win, text="手动快照（保存的黄金配置）", font=self.f_sec,
                     bg=BG, fg=PURPLE_DARK).pack(pady=(10, 4))
            tk.Label(win, text=f"共 {len(all_snapshots)} 份，支持多选（Ctrl/Shift+点击）",
                     font=self.f_small, bg=BG, fg=TEXT_SUB).pack(pady=(0, 6))

            lb_frame = tk.Frame(win, bg=BG)
            lb_frame.pack(fill="both", expand=True, padx=12, pady=(0, 4))
            listbox = tk.Listbox(lb_frame, font=("Consolas", 10), bg=CARD, fg=TEXT,
                                 selectbackground=ACCENT, selectforeground="white",
                                 selectmode=tk.EXTENDED,
                                 relief="flat", bd=0, highlightthickness=0)
            scrollbar = ttk.Scrollbar(lb_frame, orient="vertical", command=listbox.yview)
            listbox.configure(yscrollcommand=scrollbar.set)
            listbox.pack(side="left", fill="both", expand=True)
            scrollbar.pack(side="right", fill="y")

            for snap in all_snapshots:
                listbox.insert(tk.END, f"  {snap['timestamp']}  |  {snap['instance_count']} 个实例")

            btn_row = tk.Frame(win, bg=BG)
            btn_row.pack(fill="x", padx=12, pady=(0, 10))

            def _save_geometry(geo):
                try:
                    cfg = load_tool_config()
                    if 'dialog_geometry' not in cfg:
                        cfg['dialog_geometry'] = {}
                    cfg['dialog_geometry']['snapshot_list'] = geo
                    save_tool_config(cfg)
                except Exception:
                    pass

            def on_select():
                sel = listbox.curselection()
                if not sel:
                    return
                _save_geometry(win.geometry())
                snap = all_snapshots[sel[0]]
                win.destroy()
                self._do_restore_snapshot(snap)

            def on_delete():
                sel = listbox.curselection()
                if not sel:
                    messagebox.showinfo("提示", "请先选中要删除的快照")
                    return
                geo = win.geometry()
                names = [all_snapshots[i]['name'] for i in sel]
                if messagebox.askyesno("确认删除", f"删除选中的 {len(sel)} 个快照？\n\n" + "\n".join(f"  {n}" for n in names)):
                    for i in reversed(sel):
                        shutil.rmtree(all_snapshots[i]['path'], ignore_errors=True)
                    win.destroy()
                    self._show_snapshot_list(geo)

            win.protocol("WM_DELETE_WINDOW", _on_close)
            RoundedButton(btn_row, text="恢复选中（首个）", command=on_select,
                          bg=GREEN, fg="white", font=("Microsoft YaHei", 9),
                          padx=10).pack(side="left", padx=(0, 6))
            RoundedButton(btn_row, text="删除选中", command=on_delete,
                          bg=RED, fg="white", font=("Microsoft YaHei", 9),
                          padx=10).pack(side="left")
            RoundedButton(btn_row, text="关闭", command=_on_close,
                          bg=TEXT_LIGHT, fg="white", font=("Microsoft YaHei", 9),
                          padx=10).pack(side="right")
        except Exception as e:
            import traceback
            err_msg = f"弹窗异常: {e}\n\n{traceback.format_exc()}"
            try:
                with open(os.path.join(exe_dir, "_snapshot_error.log"), 'w', encoding='utf-8') as f:
                    f.write(err_msg)
            except Exception:
                pass
            messagebox.showerror("快照列表异常",
                f"发生了未预期的错误，已记录到日志。\n\n{err_msg}")

    def _do_restore_snapshot(self, snap):
        """从快照恢复配置"""
        vms_cfg = self._ld_paths.get("vms_config_dir")
        mp_cfg = None
        mp = self._ld_paths.get("multiplayer_path")
        if mp:
            mp_cfg = os.path.join(mp, "vms", "config")
        count, msg = restore_snapshot(snap['path'], vms_cfg, mp_cfg)
        messagebox.showinfo("恢复完成", msg)
        self._scan_and_display_instances()

    def _on_staggered_launch(self):
        """间隔启动选中的实例"""
        selected = self._get_selected_instances()
        if not selected:
            messagebox.showinfo("提示", "请先勾选要启动的实例")
            return

        dnconsole = self._ld_paths.get("dnconsole")
        if not dnconsole or not os.path.isfile(dnconsole):
            messagebox.showerror("错误", "未找到 dnconsole.exe")
            return

        try:
            interval = int(self.launch_interval_var.get())
        except ValueError:
            interval = 5

        self.launch_btn.set_text("启动中...")
        self.launch_btn.config_bg(TEXT_LIGHT)

        def _work():
            results = staggered_launch(
                dnconsole, selected, interval,
                on_status=lambda t: self.root.after(0, lambda: self.launch_status_var.set(t)),
            )
            self.root.after(0, lambda: self._on_launch_done(results))

        threading.Thread(target=_work, daemon=True).start()

    def _on_launch_done(self, results):
        self.launch_btn.set_text("启动")
        self.launch_btn.config_bg(GREEN)
        success = sum(1 for _, ok, _ in results if ok)
        total = len(results)
        details = "\n".join(f"  {name}: {'成功' if ok else msg}" for name, ok, msg in results)
        messagebox.showinfo("启动完成", f"成功 {success}/{total}\n{details}")
        self.launch_status_var.set(f"完成 {success}/{total}")
        self._scan_and_display_instances()

    # ---------- 窗口关闭 ----------

    def _on_close(self):
        running = sum(1 for t in self.shutdown_tasks if t["running"])
        if running > 0:
            if not messagebox.askyesno("确认退出",
                                       f"有 {running} 个定时任务正在运行，确认退出？"):
                return
        self._destroyed = True
        for t in self.shutdown_tasks:
            t["running"] = False
        if self.scan_timer_id:
            try: self.root.after_cancel(self.scan_timer_id)
            except: pass
            self.scan_timer_id = None
        self.root.destroy()

    def _minimize_to_tray(self):
        self.root.iconify()


# ============================================================
# 入口（保留原样）
# ============================================================

SINGLE_INSTANCE_MUTEX_NAME = "Local\\EmulatorShutdownTool_v3"
_single_instance_mutex = None


def ensure_single_instance():
    global _single_instance_mutex
    try:
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        _single_instance_mutex = kernel32.CreateMutexW(None, False, SINGLE_INSTANCE_MUTEX_NAME)
        if not _single_instance_mutex:
            return True
        err = ctypes.get_last_error()
        if err == 183:
            return False
        return True
    except Exception:
        return True


def main():
    try:
        if not ensure_single_instance():
            messagebox.showwarning("警告", "程序已经在运行中！\n请勿重复打开。")
            sys.exit(0)

        if psutil is None and not getattr(sys, 'frozen', False):
            try:
                subprocess.run(
                    [sys.executable, "-m", "pip", "install", "psutil", "-q"],
                    timeout=30, creationflags=subprocess.CREATE_NO_WINDOW
                )
                import psutil as psutil_module
                globals()['psutil'] = psutil_module
            except Exception:
                pass

        root = tk.Tk()
        root.withdraw()
        root.configure(bg=BG)
        app = EmulatorShutdownApp(root)
        root.deiconify()
        root.mainloop()
    except Exception:
        import traceback
        try:
            _dir = _config_dir()
            with open(os.path.join(_dir, 'crash.log'), 'w', encoding='utf-8') as f:
                f.write(f"模拟器管理工具 v4.0 崩溃日志\n")
                f.write(f"时间: {datetime.now()}\n")
                f.write(f"Python: {sys.version}\n")
                f.write(f"Frozen: {getattr(sys, 'frozen', False)}\n")
                f.write(f"可执行文件: {sys.executable}\n")
                f.write(f"工作目录: {os.getcwd()}\n")
                f.write("-" * 60 + "\n")
                traceback.print_exc(file=f)
        except Exception:
            pass
        raise


if __name__ == "__main__":
    main()
