#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模拟器管理工具 v4.2
- 保留全部原有功能：定时任务、模拟器检测、一键关闭
- 新增优化：优雅关闭（WM_CLOSE → 超时 → 强制）、自动关机、配置备份
- 新增：雷电模拟器实例管理（自动探测路径、设置编辑、间隔启动、配置快照）
- 新增：环境检测模块（Hyper-V/VMP/VBS 检测 + 一键修复）
- 暗色主题（Dark Theme）— 靛蓝/紫色主色调
"""

import ctypes
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
import winreg
from datetime import datetime, timedelta
from tkinter import filedialog, messagebox, ttk
from tkinter.font import Font

try:
    import psutil
except ImportError:
    psutil = None

from ld_instance_manager import (
    SNAPSHOT_DIR,
    apply_all_fixes,
    auto_detect_mumu,
    check_running_instances,
    find_emulator_from_shortcuts,
    get_emulator_environment_report,
    get_instance_summary,
    launch_instance,
    list_snapshots,
    load_tool_config,
    restore_snapshot,
    save_snapshot,
    save_tool_config,
    scan_instances,
    scan_mumu_instances,
    shutdown_mumu_instance,
    staggered_launch,
    write_instance_config,
)

# ============================================================
# Windows API 常量 & 辅助
# ============================================================
WM_CLOSE = 0x0010

# -------------------- 日志 --------------------
import traceback as _traceback

_LOG_FILE = None
_LOG_BUFFER = []   # 内存缓冲，关闭时一次性写入
_LOG_BUFFER_LOCK = threading.Lock()

def _get_log_path():
    global _LOG_FILE
    if _LOG_FILE:
        return _LOG_FILE
    try:
        if getattr(sys, 'frozen', False):
            _dir = os.path.dirname(os.path.abspath(sys.executable))
        else:
            _dir = os.path.dirname(os.path.abspath(__file__))
        _LOG_FILE = os.path.join(_dir, "模拟器管理工具_运行日志.txt")
    except Exception:
        _LOG_FILE = "模拟器管理工具_运行日志.txt"
    return _LOG_FILE


def _log_error(context, exc_info=None):
    """记录错误到内存缓冲，关闭时写入日志文件"""
    try:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if exc_info is None:
            exc_info = _traceback.format_exc()
            if not exc_info or exc_info.strip() == "NoneType: None":
                exc_info = ""  # 无异常时不要垃圾
        elif isinstance(exc_info, BaseException):
            exc_info = f"{type(exc_info).__name__}: {exc_info}"
        with _LOG_BUFFER_LOCK:
            if exc_info:
                _LOG_BUFFER.append(f"[{ts}] [{context}]\n{exc_info}\n---\n")
            else:
                _LOG_BUFFER.append(f"[{ts}] [{context}]\n")
    except Exception as _e:
        pass


def _log_info(msg):
    """记录一般信息到内存缓冲"""
    try:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with _LOG_BUFFER_LOCK:
            _LOG_BUFFER.append(f"[{ts}] {msg}\n")
    except Exception as _e:
        pass


def _log_warn(msg):
    """记录警告信息（不致命但需关注）"""
    try:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with _LOG_BUFFER_LOCK:
            _LOG_BUFFER.append(f"[{ts}] ⚠ {msg}\n")
    except Exception:
        pass


def _flush_log():
    """将缓冲写入日志文件（追加模式），自动控制日志文件大小"""
    with _LOG_BUFFER_LOCK:
        if not _LOG_BUFFER:
            return
        content = "".join(_LOG_BUFFER)
    try:
        log_path = _get_log_path()
        # 仅在文件不存在或为空时写入 header
        need_header = not os.path.isfile(log_path) or os.path.getsize(log_path) == 0
        header = f"\n模拟器管理工具 v4.2 运行日志\n{'=' * 50}\n" if need_header else ""
        with open(log_path, 'a', encoding='utf-8') as f:
            f.write(header + content)
        # 再检查文件大小，超过 1MB 时截断保留后半段
        if os.path.isfile(log_path) and os.path.getsize(log_path) > 1_048_576:
            with open(log_path, 'r', encoding='utf-8') as f:
                old = f.read()
            with open(log_path, 'w', encoding='utf-8') as f:
                f.write(old[-524288:])  # 保留最后 500KB
    except Exception as _e:
        pass

# -------------------- 关机常量 --------------------

# ExitWindowsEx / InitiateShutdown 常量
EWX_SHUTDOWN   = 0x00000001
EWX_REBOOT     = 0x00000002
EWX_FORCE      = 0x00000004
EWX_POWEROFF   = 0x00000008

SHUTDOWN_FORCE_OTHERS = 0x00000001
SHUTDOWN_FORCE_SELF   = 0x00000002
SHUTDOWN_RESTART      = 0x00000004
SHUTDOWN_POWEROFF     = 0x00000008

# 特权常量
SE_SHUTDOWN_NAME = "SeShutdownPrivilege"
TOKEN_ADJUST_PRIVILEGES = 0x0020
TOKEN_QUERY = 0x0008
SE_PRIVILEGE_ENABLED = 0x00000002

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
advapi32 = ctypes.windll.advapi32
ntdll = ctypes.windll.ntdll

WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)


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
    # 方法1只是启动关机命令，不保证生效，继续尝试后续方法确保真正关机
    try:
        subprocess.run(
            ['shutdown', flag, '/t', '0', '/f'],
            capture_output=True, timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW
        )
    except Exception as _e:
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
    except Exception as _e:
        pass

    # ----- 方法3: InitiateShutdown API (Windows Vista+) -----
    # 更底层，能绕过更多拦截
    try:
        if should_restart:
            dw_flags = SHUTDOWN_FORCE_OTHERS | SHUTDOWN_FORCE_SELF | SHUTDOWN_RESTART
        else:
            dw_flags = SHUTDOWN_FORCE_OTHERS | SHUTDOWN_FORCE_SELF | SHUTDOWN_POWEROFF
        result = advapi32.InitiateShutdownW(None, None, 0, dw_flags, 0)
        if result:
            time.sleep(3)
            return True
    except Exception as _e:
        pass

    # ----- 方法4: wmic os call -----
    try:
        if should_restart:
            # wmic os call reboot 无效，回退到 shutdown /r
            subprocess.run(
                ['shutdown', '/r', '/t', '0', '/f'],
                capture_output=True, timeout=15,
                creationflags=subprocess.CREATE_NO_WINDOW
            )
        else:
            subprocess.run(
                ['wmic', 'os', 'where', 'Primary=True', 'call', 'Shutdown'],
                capture_output=True, timeout=15,
                creationflags=subprocess.CREATE_NO_WINDOW
            )
        time.sleep(3)
        return True
    except Exception as _e:
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
# 图标
# ============================================================

def _get_icon_path():
    """从 exe 同级目录加载图标，找不到则返回 None"""
    try:
        if getattr(sys, 'frozen', False):
            _dir = os.path.dirname(os.path.abspath(sys.executable))
        else:
            _dir = os.path.dirname(os.path.abspath(__file__))
        ico = os.path.join(_dir, "app_icon.ico")
        if os.path.isfile(ico):
            return ico
    except Exception as _e:
        pass
    return None


# ============================================================
# 配色方案 — 暗色主题（Dark Theme）
# ============================================================
PRIMARY     = "#6366F1"   # 靛蓝/紫色（品牌色）
ACCENT      = "#818CF8"   # 浅靛蓝（强调/悬停）
BG          = "#1E1E2E"   # 深色主背景
CARD        = "#2B2B3D"   # 卡片背景
TEXT        = "#E0E0E0"   # 主文字（浅色）
TEXT_SUB    = "#A0A0A8"   # 次要文字
TEXT_LIGHT  = "#6B7280"   # 浅灰（占位/装饰）
BORDER      = "#3A3A52"   # 深色边框
GREEN       = "#22C55E"   # 成功绿
RED         = "#EF4444"   # 错误红
YELLOW      = "#F59E0B"   # 警告琥珀
BG_LIGHT    = "#363650"   # 浅色背景（输入框等）

# 扩展色
ORANGE_LIGHT  = "#818CF8"
ORANGE_DARK   = "#4F46E5"

# 兼容旧变量名
MI_ORANGE     = PRIMARY
MI_ORANGE_LT  = ORANGE_LIGHT
MI_ORANGE_DK  = ORANGE_DARK


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
    "mumuemu", "mumuservice", "mumumanager",
    "mumu12", "mumu6",
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
                # WMIC CSV 列顺序（字母序）: Node, ExecutablePath, Name, ProcessId
                name = parts[-2].lower()
                exe = parts[-3].lower() if len(parts) >= 4 else name
                pid_str = parts[-1].strip()
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
    except Exception as _e:
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
    except Exception as _e:
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


def _find_ldconsole(ld_path):
    """查找 LDPlayer 的命令行工具（新版叫 ldconsole.exe，旧版叫 dnconsole.exe）"""
    if not ld_path:
        return None
    for name in ['ldconsole.exe', 'dnconsole.exe']:
        fp = os.path.join(ld_path, name)
        if os.path.isfile(fp):
            return fp
    return None


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
            except Exception as _e:
                pass
    for path in candidates:
        if _find_ldconsole(path):
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


def _find_mumu_manager():
    """查找 MuMuManager.exe 路径（含自动检测的路径）"""
    candidates = [
        r'C:\Program Files\Netease\MuMuPlayer-12.0\shell\MuMuManager.exe',
        r'C:\Program Files\Netease\MuMuPlayer-12.0\MuMuManager.exe',
        r'C:\Program Files\MuMuPlayer-12.0\shell\MuMuManager.exe',
        r'C:\Program Files (x86)\Netease\MuMuPlayer-12.0\shell\MuMuManager.exe',
    ]
    for p in candidates:
        if os.path.isfile(p):
            return p
    try:
        info = auto_detect_mumu()
        if info.get("manager_path"):
            return info["manager_path"]
    except Exception as _e:
        pass
    return None


def backup_config_files(backup_root, vms_dir=None):
    """备份雷电模拟器配置，返回 (备份目录路径, 消息)"""
    if not vms_dir:
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
                         do_backup=False, do_shutdown=False, should_restart=False,
                         mumu_vms_dir=None, vms_config_dir=None, multiplayer_config_dir=None):
    """
    后台优雅关闭模拟器进程，替代原 kill_emulators_async
    
    Args:
        on_done: 完成后回调 fn(count, success, fail, failed_names, backup_msg, shutdown_executed)
        on_status: 状态更新回调 fn(text) （可选）
        on_progress: 进度回调 fn(current, total) （可选）
        do_backup: 是否备份配置
        do_shutdown: 是否关机/重启
        should_restart: True=重启 False=关机
        vms_config_dir: LDPlayer 实例配置目录（用户配置的路径，优先于自动检测）
        multiplayer_config_dir: 多开器配置目录
    """
    TOTAL = 60  # 总进度

    def _status(t):
        if on_status:
            try:
                on_status(t)
            except Exception as _e:
                pass

    def _progress(c, t=TOTAL):
        if on_progress:
            try:
                on_progress(c, t)
            except Exception as _e:
                pass

    def _work():
        start_ts = time.time()
        backup_msg = ""
        shutdown_executed = False

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
                backup_dir = os.path.join(_config_dir(), '备份')
                _, backup_msg = backup_config_files(backup_dir, vms_dir=vms_config_dir)
                _cleanup_old_backups(backup_dir)
                _status(backup_msg)

                # 也保存一份到快照目录，作为启动时的恢复点
                cfg_vms = vms_config_dir or find_vms_config_dir()
                if cfg_vms:
                    save_snapshot(cfg_vms, multiplayer_config_dir, SNAPSHOT_DIR, mumu_vms_dir=mumu_vms_dir)

            if not procs:
                _log_info("gka: 无进程, 直接走关机路径")
                elapsed = int(time.time() - start_ts)
                _progress(elapsed)
                if do_shutdown:
                    _log_info("gka: 开始关机倒计时...")
                    _do_shutdown_countdown(should_restart, _status, _progress, TOTAL, start_ts)
                    shutdown_executed = True
                    _log_info(f"gka: 关机倒计时结束, shutdown_executed={shutdown_executed}")
                _log_info("gka: 调用 on_done (count=0 success=0)")
                on_done(0, 0, 0, [], backup_msg, shutdown_executed)
                _log_info("gka: on_done 返回")
                return

            # ---- 阶段1：dnconsole quitall（LDPlayer 优雅关闭）----
            ld_path = find_ldplayer_install_path(procs)
            dnconsole_path = None
            if ld_path:
                dnconsole_path = _find_ldconsole(ld_path)
                if dnconsole_path and os.path.exists(dnconsole_path):
                    _status("通过 dnconsole quitall 优雅关闭 LDPlayer 实例...")
                    _progress(10)
                    try:
                        subprocess.run([dnconsole_path, 'quitall'], capture_output=True,
                                       text=True, timeout=15,
                                       creationflags=subprocess.CREATE_NO_WINDOW)
                    except Exception as _e:
                        pass

            # ---- 阶段1b：MuMuManager（MuMu 优雅关闭）----
            has_mumu = any(p.get('type') == 'mumu' for p in procs)
            if has_mumu:
                # 搜索 MuMuManager.exe（含自动检测的路径）
                mumu_mgr = _find_mumu_manager()
                if not mumu_mgr:
                    # 从进程路径搜索
                    for proc in procs:
                        if proc.get('type') == 'mumu':
                            try:
                                r = subprocess.run(
                                    ['wmic', 'process', 'where', f'ProcessId={proc["pid"]}', 'get', 'ExecutablePath'],
                                    capture_output=True, text=True, timeout=5,
                                    creationflags=subprocess.CREATE_NO_WINDOW
                                )
                                for line in r.stdout.strip().split('\n'):
                                    line = line.strip()
                                    if 'MuMuManager' in line and os.path.isfile(line):
                                        mumu_mgr = line
                                        break
                            except Exception as _e:
                                pass
                if mumu_mgr:
                    _status("通过 MuMuManager 优雅关闭 MuMu 模拟器...")
                    try:
                        subprocess.run([mumu_mgr, 'shutdown', '-n', 'all'],
                                       capture_output=True, text=True, timeout=15,
                                       cwd=os.path.dirname(mumu_mgr),
                                       creationflags=subprocess.CREATE_NO_WINDOW)
                    except Exception as _e:
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
                    except Exception as _e:
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
                # 获取实际运行中的实例名称列表
                running_names = []
                try:
                    r_list = subprocess.run(
                        [dnconsole_path, 'runninglist'],
                        capture_output=True, text=True, timeout=10,
                        creationflags=subprocess.CREATE_NO_WINDOW
                    )
                    if r_list.returncode == 0:
                        for line in r_list.stdout.strip().splitlines():
                            line = line.strip()
                            if line.startswith('leidian'):
                                running_names.append(line)
                except Exception as _e:
                    pass
                for inst_name in running_names:
                    try:
                        r = subprocess.run(
                            [dnconsole_path, 'adb', '--name', inst_name,
                             '--command', 'shell reboot -p'],
                            capture_output=True, text=True, timeout=10,
                            creationflags=subprocess.CREATE_NO_WINDOW
                        )
                    except Exception as _e:
                        pass
                _status(f"等待 ADB 关机生效（{ADB_SHUTDOWN_WAIT} 秒）...")
                for _ in range(ADB_SHUTDOWN_WAIT):
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
                    except Exception as _e:
                        pass

                # 等待进程响应
                _status("等待进程响应关闭信号...")
                for _ in range(20):
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
                        except Exception as _e:
                            pass
                # 辅助进程也清理
                for proc in remaining:
                    if 'adb' in proc['name'].lower() or 'dnconsole' in proc['name'].lower():
                        try:
                            subprocess.run(['taskkill', '/F', '/PID', str(proc['pid'])],
                                           capture_output=True, timeout=5,
                                           creationflags=subprocess.CREATE_NO_WINDOW)
                            success += 1
                        except Exception as _e:
                            pass
            else:
                if success == 0:
                    success = count

            # ---- 等待清理 ----
            _status("等待系统清理完成...")
            for _ in range(POST_FORCE_WAIT):
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
            try:
                on_done(count, success, failed_count, failed_names, backup_msg, shutdown_executed)
            except Exception as cb_err:
                _log_error(f"graceful_kill_async on_done 回调异常: {cb_err}")

        except Exception as e:
            _status(f"出错：{str(e)}")
            try:
                on_done(0, 0, 1, [str(e)], backup_msg, shutdown_executed)
            except Exception as _e:
                pass

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
            for d, _ in entries[:-keep_count]:
                shutil.rmtree(d, ignore_errors=True)
    except Exception as _e:
        pass


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
            winreg.SetValueEx(key, REG_ENTRY_NAME, 0, winreg.REG_SZ, f'"{exe_path}" --nowindow')
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


# ============================================================
# GUI — RoundedButton（自绘按钮，带悬停效果）
# ============================================================

def _lighten_color(hex_color, factor=0.15):
    """将十六进制颜色调亮 factor（0-1），找不到映射时的兜底"""
    try:
        r = int(hex_color[1:3], 16)
        g = int(hex_color[3:5], 16)
        b = int(hex_color[5:7], 16)
        r = min(255, int(r + (255 - r) * factor))
        g = min(255, int(g + (255 - g) * factor))
        b = min(255, int(b + (255 - b) * factor))
        return f"#{r:02x}{g:02x}{b:02x}"
    except Exception:
        return hex_color


class RoundedButton(tk.Frame):
    """自绘按钮（Frame + Label，带字体缓存和悬停效果）"""

    _font_cache = {}     # cache_key -> (linespace,)
    _shared_fonts = {}   # cache_key -> tkinter Font object (persistent)
    _text_width_cache = {}  # (cache_key, text) -> pixel width

    @classmethod
    def _get_shared_font(cls, font_spec):
        cache_key = str(font_spec)
        if cache_key not in cls._shared_fonts:
            cls._shared_fonts[cache_key] = Font(font=font_spec)
        return cls._shared_fonts[cache_key]

    def __init__(self, parent, text, command=None, bg=MI_ORANGE, fg="white",
                 font=None, padx=16, pady=6, hover_bg=None, **kwargs):
        self._cmd = command
        self._bg = bg
        self._fg = fg
        self._hover_bg = hover_bg or self._default_hover(bg)
        self._text = text
        self._font = font or ("Microsoft YaHei", 10)

        cache_key = str(self._font)
        # 缓存行高（只需一次 Font 查询）
        if cache_key not in self._font_cache:
            try:
                _f = self._get_shared_font(self._font)
                self._font_cache[cache_key] = (_f.metrics("linespace"),)
            except Exception:
                self._font_cache[cache_key] = (20,)
        th = self._font_cache[cache_key][0]

        # 缓存按钮文本像素宽度（避免重复创建 Font）
        tw_cache_key = (cache_key, text)
        if tw_cache_key not in self._text_width_cache:
            try:
                _f = self._get_shared_font(self._font)
                self._text_width_cache[tw_cache_key] = _f.measure(text)
            except Exception:
                self._text_width_cache[tw_cache_key] = 60
        tw = self._text_width_cache[tw_cache_key]

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
            "#6366F1": "#818CF8",  # 靛蓝/紫色
            "#818CF8": "#A5B4FC",  # 浅靛蓝
            "#22C55E": "#4ADE80",  # 绿
            "#EF4444": "#F87171",  # 红
            "#F59E0B": "#FBBF24",  # 黄
            "#6B7280": "#9CA3AF",  # 灰
            "#4F46E5": "#6366F1",  # 深靛蓝
        }
        return light_map.get(bg_color, _lighten_color(bg_color))

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

    def config(self, **kwargs):
        """支持 config(command=...) 等标准接口"""
        if 'command' in kwargs:
            self._cmd = kwargs.pop('command')
        if kwargs:
            super().config(**kwargs)


# ============================================================
# 主界面 — 保留原有全部功能 + 新增安全关机
# ============================================================

class EmulatorShutdownApp:
    def __init__(self, root):
        self.root = root
        self.root.title("模拟器管理工具 v4.2")
        try:
            ico = _get_icon_path()
            if ico:
                self.root.iconbitmap(ico)
        except Exception as _e:
            pass
        self.root.geometry("880x850")
        self.root.minsize(820, 700)
        self.root.configure(bg=BG)

        # 窗口居中
        self.root.update_idletasks()
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x = (sw - 880) // 2
        y = (sh - 850) // 2
        self.root.geometry(f"880x850+{x}+{y}")

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
        self._kill_status_var = tk.StringVar(value="")

        # 实例管理
        self._ld_paths = {}
        self._instances = []
        self._inst_vars = []
        # MuMu 相关
        self._mumu_path = None
        self._mumu_instances = []
        # 路径搜索锁（防止重复启动全盘搜索）
        self._path_search_running = False

        self.auto_start_var = tk.BooleanVar(value=is_auto_start_enabled())
        self.auto_launch_var = tk.BooleanVar(value=False)
        self._auto_launch_instances = set()
        self._startup_launch_done = False

        self._destroyed = False
        self._config_loaded = False
        self._config_save_lock = threading.Lock()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._build_ui()
        self.root.after_idle(self._lazy_init)

    def _lazy_init(self):
        """UI 显示后的延迟初始化"""
        # 线程安全事件处理器：后台线程用 event_generate 通知主线程执行 GUI 操作
        def _on_timer_expired(event):
            for tlist in [self.shutdown_tasks, self.launch_tasks]:
                for t in tlist:
                    fn = t.pop('_time_up_fn', None)
                    if fn:
                        fn(t)
        def _on_timer_update(event):
            for tlist in [self.shutdown_tasks, self.launch_tasks]:
                for t in tlist:
                    fn = t.pop('_update_fn', None)
                    if fn:
                        t["_pending_update"] = False
                        fn(t)
        self.root.bind_all('<<TimerExpired>>', _on_timer_expired, add='+')
        self.root.bind_all('<<TimerUpdate>>', _on_timer_update, add='+')
        try:
            from ld_instance_manager import TOOL_CONFIG_FILE as _CFG_PATH
            _log_info("程序启动，开始加载配置...")
            # 启动时打印实际读到的配置文件路径和内容快照
            _raw_cfg = load_tool_config()
            _raw_launch = _raw_cfg.get("launch_tasks", [])
            _raw_shutdown = _raw_cfg.get("shutdown_tasks", [])
            _log_info(f"配置来源: {_CFG_PATH}")
            _log_info(f"配置文件原始内容 — 关闭任务: {len(_raw_shutdown)}个, 启动任务: {len(_raw_launch)}个")
            if _raw_launch:
                _log_info(f"启动任务详情: {json.dumps(_raw_launch, ensure_ascii=False)[:200]}")
            _flush_log()

            self._init_instance_manager()
            self._load_tasks_config()
            self._config_loaded = True
            cfg = load_tool_config()
            self._log_refresh_id = None
            _log_info(f"配置加载完成：关闭任务 {len(self.shutdown_tasks)} 个，启动任务 {len(self.launch_tasks)} 个")

            # 开机自动恢复最新快照（后台线程，不阻塞主线程）
            def _bg_restore():
                try:
                    from ld_instance_manager import list_snapshots, restore_snapshot
                    snaps = list_snapshots(SNAPSHOT_DIR)
                    if not snaps:
                        return
                    latest = snaps[0]
                    _log_info(f"发现 {len(snaps)} 个快照，自动恢复最新: {latest['timestamp']}")
                    vms = self._ld_paths.get("vms_config_dir")
                    mp = self._ld_paths.get("multiplayer_path")
                    if not vms or not mp:
                        from ld_instance_manager import auto_detect_paths
                        detected = auto_detect_paths()
                        if not vms and detected.get("vms_config_dir"):
                            vms = detected["vms_config_dir"]
                        if not mp and detected.get("multiplayer_path"):
                            mp = detected["multiplayer_path"]
                    mp_cfg = os.path.join(mp, "vms", "config") if mp and os.path.isdir(os.path.join(mp, "vms", "config")) else None
                    count, msg = restore_snapshot(latest["path"], vms, mp_cfg, mumu_vms_dir=self._get_mumu_vms_dir())
                    if count > 0:
                        _log_info(f"快照恢复成功: {msg}")
                        self.root.after(0, self._scan_and_display_instances)
                except Exception as e:
                    _log_error(f"开机自动恢复快照失败: {e}")
            threading.Thread(target=_bg_restore, daemon=True).start()
            self._start_scan_loop()
            # 立即触发首次实例扫描，不依赖后台线程完成
            self.root.after(0, self._scan_and_display_instances)
            self.root.after(5000, self._refresh_log_display)
            _flush_log()
        except Exception as _e:
            _log_error("_lazy_init初始化异常", _e)
            _flush_log()

    # ---------- UI 构建 ----------

    def _build_ui(self):
        root = self.root

        self.f_title  = Font(family="Microsoft YaHei", size=13, weight="bold")
        self.f_sec    = Font(family="Microsoft YaHei", size=10, weight="bold")
        self.f_body   = Font(family="Microsoft YaHei", size=10)
        self.f_small  = Font(family="Microsoft YaHei", size=8)

        # ===== 顶栏 — 暗色主题 =====
        header = tk.Frame(root, bg=CARD, height=48)
        header.pack(fill="x")
        header.pack_propagate(False)
        h_row = tk.Frame(header, bg=CARD)
        h_row.pack(expand=True, fill="x", padx=20)
        tk.Label(h_row, text="模拟器管理", font=self.f_title,
                 bg=CARD, fg=TEXT).pack(side="left")
        # 运行状态点
        status_dot = tk.Frame(h_row, bg=GREEN, width=8, height=8,
                              highlightthickness=0, bd=0)
        status_dot.pack(side="left", padx=(8, 0))
        status_dot.pack_propagate(False)
        self._status_dot = status_dot
        tk.Label(h_row, text="v4.2", font=self.f_small,
                 bg=CARD, fg=TEXT_LIGHT, padx=6).pack(side="right")
        # 底部强调线
        tk.Frame(header, bg=PRIMARY, height=2).pack(side="bottom", fill="x")

        # ===== 主内容区（左右分栏） =====
        paned = tk.PanedWindow(root, bg=BG, orient="horizontal", sashwidth=4, sashrelief="ridge")
        paned.pack(fill="both", expand=True)

        # ---- 左侧：现有内容（可滚动） ----
        left_frame = tk.Frame(paned, bg=BG)
        main_canvas = tk.Canvas(left_frame, bg=BG, highlightthickness=0)
        main_scrollbar = ttk.Scrollbar(left_frame, orient="vertical", command=main_canvas.yview)
        main_frame = tk.Frame(main_canvas, bg=BG, padx=16, pady=12)
        def _set_scrollregion(e, c=main_canvas):
            try:
                b = c.bbox("all")
                if b:
                    c.configure(scrollregion=b)
            except Exception:
                pass
        main_frame.bind("<Configure>", _set_scrollregion)
        main_canvas.create_window((0, 0), window=main_frame, anchor="nw", tags="main_inner")
        main_canvas.configure(yscrollcommand=main_scrollbar.set)
        main_scrollbar.pack(side="right", fill="y")
        main_canvas.pack(side="left", fill="both", expand=True)
        paned.add(left_frame, stretch="always")

        # ---- 右侧：实时日志面板 ----
        right_frame = tk.Frame(paned, bg=CARD, width=280)
        right_frame.pack_propagate(False)
        # 标题 + 操作按钮
        log_header = tk.Frame(right_frame, bg=CARD)
        log_header.pack(fill="x", padx=6, pady=(6, 2))
        tk.Label(log_header, text="运行日志", font=("Microsoft YaHei", 9, "bold"),
                 bg=CARD, fg=TEXT).pack(side="left")
        # 复制按钮
        tk.Button(log_header, text="复制", font=("Microsoft YaHei", 7),
                  bg="#444", fg=TEXT_LIGHT, bd=0, padx=4, pady=0,
                  command=self._copy_log).pack(side="right", padx=(2, 0))
        # 清空按钮
        tk.Button(log_header, text="清空", font=("Microsoft YaHei", 7),
                  bg="#444", fg=TEXT_LIGHT, bd=0, padx=4, pady=0,
                  command=self._clear_log_display).pack(side="right")
        # 日志文本框
        log_frame = tk.Frame(right_frame, bg=CARD)
        log_frame.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        self._log_text = tk.Text(log_frame, bg="#1a1a2e", fg="#a0a0c0",
                                 font=("Consolas", 8), bd=0, wrap="none",
                                 state="disabled", relief="flat")
        log_sb = ttk.Scrollbar(log_frame, orient="vertical", command=self._log_text.yview)
        self._log_text.configure(yscrollcommand=log_sb.set)
        log_sb.pack(side="right", fill="y")
        self._log_text.pack(fill="both", expand=True)
        paned.add(right_frame, width=280, stretch="never")

        # 防抖：拖拽 PanedWindow 时连续 Configure 事件只响应最后一次
        main_cfg_after_id = [None]
        def _on_mf_cfg(event):
            nonlocal main_cfg_after_id
            # 取消上一次防抖
            if main_cfg_after_id[0] is not None:
                try:
                    root.after_cancel(main_cfg_after_id[0])
                except Exception:
                    pass
                main_cfg_after_id[0] = None
            # 延迟执行，让连续事件只触发一次 itemconfig
            def _do_resize():
                try:
                    cur = main_canvas.itemcget("main_inner", "width")
                    if str(cur) != str(event.width):
                        main_canvas.itemconfig("main_inner", width=event.width)
                except tk.TclError:
                    main_canvas.itemconfig("main_inner", width=event.width)
            main_cfg_after_id[0] = root.after(10, _do_resize)
        main_canvas.bind("<Configure>", _on_mf_cfg)

        # ---------- 卡片工厂 ----------
        def make_card(parent, padding=16, accent_color=ACCENT):
            # 外层容器：暗色卡片 + 左边框装饰
            card = tk.Frame(parent, bg=CARD, bd=0, highlightthickness=1,
                           highlightbackground=BORDER, highlightcolor=BORDER)
            card.pack(fill="x", pady=(0, 12))
            # 左侧装饰条
            accent_bar = tk.Frame(card, bg=accent_color, width=3)
            accent_bar.pack(side="left", fill="y")
            inner = tk.Frame(card, bg=CARD, padx=padding, pady=padding)
            inner.pack(fill="both", expand=True)
            return card, inner

        def section_title(parent, text, color=PRIMARY):
            lbl = tk.Label(parent, text=text, font=self.f_sec,
                     bg=CARD, fg=color)
            lbl.pack(anchor="w", pady=(0, 6))
            tk.Frame(parent, bg=BORDER, height=1).pack(fill="x", pady=(0, 4))

        # ============================================================
        # 卡片1：环境检测 — 最优先，诊断入口
        # ============================================================
        env_card, env_inner = make_card(main_frame, accent_color=PRIMARY)

        env_header = tk.Frame(env_inner, bg=CARD)
        env_header.pack(fill="x")
        tk.Label(env_header, text="环境检测", font=self.f_sec,
                 bg=CARD, fg=PRIMARY).pack(side="left")
        self.env_score_var = tk.StringVar(value="")
        self.env_score_lbl = tk.Label(env_header, textvariable=self.env_score_var,
                                       font=self.f_body,
                                       bg=CARD, fg=TEXT_LIGHT)
        self.env_score_lbl.pack(side="right")

        # 环境状态表格
        self.env_frame = tk.Frame(env_inner, bg=CARD)
        self.env_frame.pack(fill="x")
        self._rebuild_env_ui([])

        # 操作行
        env_btn_row = tk.Frame(env_inner, bg=CARD)
        env_btn_row.pack(fill="x", pady=(6, 0))
        self.env_scan_btn = RoundedButton(env_btn_row, text="一键检测",
                                           command=self._on_env_scan,
                                           bg=PRIMARY, fg="white",
                                           font=self.f_body, padx=12)
        self.env_scan_btn.pack(side="left", padx=(0, 6))
        self.env_fix_btn = RoundedButton(env_btn_row, text="一键修复",
                                          command=self._on_env_fix,
                                          bg=RED, fg="white",
                                          font=self.f_body, padx=12)
        self.env_fix_btn.pack(side="left", padx=(0, 6))
        self.env_fix_btn.pack_forget()
        self.env_status_var = tk.StringVar(value="点击「一键检测」开始检查")
        tk.Label(env_btn_row, textvariable=self.env_status_var,
                 font=self.f_small, bg=CARD, fg=TEXT_LIGHT).pack(side="left", padx=(8, 0))

        # 详情区
        tk.Frame(env_inner, bg=BORDER, height=1).pack(fill="x", pady=(6, 0))
        self.env_detail_text = tk.Text(env_inner, font=("Consolas", 9),
                                        bg=BG_LIGHT, fg=TEXT, height=3,
                                        wrap="word", relief="flat", bd=0,
                                        highlightthickness=0)
        self.env_detail_text.pack(fill="x", pady=(4, 0))
        self.env_detail_text.insert("1.0", "检测结果详情将在此显示...")
        self.env_detail_text.config(state="disabled")

        # ============================================================
        # 卡片2：实例管理
        # ============================================================
        card2, c2 = make_card(main_frame, accent_color=ACCENT)

        tk.Label(c2, text="模拟器管理", font=self.f_sec,
                 bg=CARD, fg=TEXT).pack(anchor="w", pady=(0, 2))

        # ---- LDPlayer 路径选择行 ----
        ld_row = tk.Frame(c2, bg=CARD)
        ld_row.pack(fill="x", pady=(2, 0))
        tk.Label(ld_row, text="雷电", font=self.f_small, bg=CARD, fg=TEXT_SUB,
                 width=4, anchor="w").pack(side="left")
        self.ld_path_var = tk.StringVar(value="")
        self.ld_path_entry = tk.Entry(ld_row, textvariable=self.ld_path_var,
                                       font=("Consolas", 9), bg=BG_LIGHT, fg=TEXT_LIGHT,
                                       relief="flat", bd=2, highlightthickness=0)
        self.ld_path_entry.pack(side="left", fill="x", expand=True, ipady=2)
        self.ld_path_entry.insert(0, "输入路径回车，或点浏览")
        self._ld_placeholder = True
        def _on_ld_focusin(_):
            if self._ld_placeholder:
                self.ld_path_var.set("")
                self.ld_path_entry.config(fg=TEXT)
                self._ld_placeholder = False
        def _on_ld_focusout(_):
            if not self._ld_placeholder:
                self._on_ld_path_enter()
            if not self.ld_path_var.get().strip():
                self.ld_path_var.set("")
                self.ld_path_entry.insert(0, "输入路径回车，或点浏览")
                self.ld_path_entry.config(fg=TEXT_LIGHT)
                self._ld_placeholder = True
        self.ld_path_entry.bind("<FocusIn>", _on_ld_focusin)
        self.ld_path_entry.bind("<Return>", lambda e: self._on_ld_path_enter())
        self.ld_path_entry.bind("<FocusOut>", _on_ld_focusout)
        RoundedButton(ld_row, text="...", command=self._manual_select_ld_path,
                      bg=PRIMARY, fg="white", font=("Consolas", 9), padx=6, pady=0).pack(side="right", padx=(4, 0))

        # ---- MuMu 路径选择行 ----
        mm_row = tk.Frame(c2, bg=CARD)
        mm_row.pack(fill="x", pady=(2, 4))
        tk.Label(mm_row, text="MuMu", font=self.f_small, bg=CARD, fg=ACCENT,
                 width=4, anchor="w").pack(side="left")
        self.mumu_path_var = tk.StringVar(value="")
        self.mumu_path_entry = tk.Entry(mm_row, textvariable=self.mumu_path_var,
                                         font=("Consolas", 9), bg=BG_LIGHT, fg=TEXT_LIGHT,
                                         relief="flat", bd=2, highlightthickness=0)
        self.mumu_path_entry.pack(side="left", fill="x", expand=True, ipady=2)
        self.mumu_path_entry.insert(0, "输入路径回车，或点浏览")
        self._mumu_placeholder = True
        def _on_mumu_focusin(_):
            if self._mumu_placeholder:
                self.mumu_path_var.set("")
                self.mumu_path_entry.config(fg=TEXT)
                self._mumu_placeholder = False
        def _on_mumu_focusout(_):
            if not self._mumu_placeholder:
                self._on_mumu_path_enter()
            if not self.mumu_path_var.get().strip():
                self.mumu_path_var.set("")
                self.mumu_path_entry.insert(0, "输入路径回车，或点浏览")
                self.mumu_path_entry.config(fg=TEXT_LIGHT)
                self._mumu_placeholder = True
        self.mumu_path_entry.bind("<FocusIn>", _on_mumu_focusin)
        self.mumu_path_entry.bind("<Return>", lambda e: self._on_mumu_path_enter())
        self.mumu_path_entry.bind("<FocusOut>", _on_mumu_focusout)
        RoundedButton(mm_row, text="...", command=self._manual_select_mumu_path,
                      bg=ORANGE_DARK, fg="white", font=("Consolas", 9), padx=6, pady=0).pack(side="right", padx=(4, 0))

        # 实例列表表头
        ih = tk.Frame(c2, bg=CARD)
        ih.pack(fill="x")
        for txt, w in [("", 3), ("实例", 10), ("设置", 22), ("状态", 8)]:
            tk.Label(ih, text=txt, font=("Microsoft YaHei", 8, "bold"),
                     bg=CARD, fg=TEXT_SUB, width=w, anchor="w").pack(side="left")
        tk.Frame(c2, bg=BORDER, height=1).pack(fill="x", pady=(2, 0))

        self.inst_rows_frame = tk.Frame(c2, bg=CARD)
        self.inst_rows_frame.pack(fill="x")

        # 操作按钮行 1：管理操作
        ib = tk.Frame(c2, bg=CARD)
        ib.pack(fill="x", pady=(6, 0))
        RoundedButton(ib, text="扫描", command=self._refresh_instances,
                      bg=PRIMARY, fg="white", font=self.f_small, padx=8).pack(side="left", padx=(0, 4))
        RoundedButton(ib, text="编辑", command=self._edit_instance_settings,
                      bg=TEXT_SUB, fg="white", font=self.f_small, padx=8).pack(side="left", padx=(0, 4))
        RoundedButton(ib, text="保存快照", command=self._save_snapshot,
                      bg=GREEN, fg="white", font=self.f_small, padx=8).pack(side="left", padx=(0, 4))
        RoundedButton(ib, text="恢复", command=self._restore_snapshot,
                      bg=YELLOW, fg="white", font=self.f_small, padx=8).pack(side="left", padx=(0, 8))
        RoundedButton(ib, text="关闭所有", command=self._on_kill_now,
                      bg=RED, fg="white", font=self.f_small, padx=8).pack(side="left", padx=(0, 8))
        tk.Label(ib, textvariable=self._kill_status_var,
                 font=self.f_small, bg=CARD, fg=TEXT_SUB).pack(side="left", padx=(4, 0))

        # 间隔启动控件
        tk.Label(ib, text="间隔", font=self.f_small, bg=CARD, fg=TEXT_SUB).pack(side="left")
        self.launch_interval_var = tk.StringVar(value="5")
        ttk.Spinbox(ib, from_=1, to=60, width=2,
                     textvariable=self.launch_interval_var,
                     font=("Consolas", 9)).pack(side="left", padx=(2, 1))
        tk.Label(ib, text="秒", font=self.f_small, bg=CARD, fg=TEXT_SUB).pack(side="left")
        self.launch_btn = RoundedButton(ib, text="启动", command=self._on_staggered_launch,
                                         bg=GREEN, fg="white", font=self.f_small, padx=8)
        self.launch_btn.pack(side="right")
        self.launch_status_var = tk.StringVar(value="")
        tk.Label(ib, textvariable=self.launch_status_var,
                 font=self.f_small, bg=CARD, fg=TEXT_SUB).pack(side="right", padx=(0, 6))

        # 操作按钮行 2：快照管理
        bb = tk.Frame(c2, bg=CARD)
        bb.pack(fill="x", pady=(4, 0))
        tk.Label(bb, text="快照", font=self.f_small, bg=CARD, fg=TEXT_SUB).pack(side="left", padx=(0, 6))
        RoundedButton(bb, text="快照列表", command=self._show_snapshot_list,
                      bg=ORANGE_LIGHT, fg="white", font=self.f_small, padx=6, pady=2).pack(side="left")

        # ============================================================
        # 卡片3：定时任务
        # ============================================================
        card1, c1 = make_card(main_frame, accent_color=RED)

        section_title(c1, "定时关闭", RED)

        shutdown_scroll_f = tk.Frame(c1, bg=CARD)
        shutdown_scroll_f.pack(fill="both", expand=True)
        shutdown_canvas = tk.Canvas(shutdown_scroll_f, bg=CARD, highlightthickness=0, height=100)
        shutdown_sb = ttk.Scrollbar(shutdown_scroll_f, orient="vertical", command=shutdown_canvas.yview)
        self.shutdown_tasks_frame = tk.Frame(shutdown_canvas, bg=CARD)
        def _set_shutdown_sr(e, c=shutdown_canvas):
            try:
                b = c.bbox("all")
                if b:
                    c.configure(scrollregion=b)
            except Exception:
                pass
        self.shutdown_tasks_frame.bind("<Configure>", _set_shutdown_sr)
        shutdown_canvas.create_window((0, 0), window=self.shutdown_tasks_frame, anchor="nw", tags="inner")
        shutdown_canvas.configure(yscrollcommand=shutdown_sb.set)
        shutdown_canvas.pack(side="left", fill="both", expand=True)
        shutdown_sb.pack(side="right", fill="y")
        self._shutdown_canvas = shutdown_canvas

        rbtn = tk.Frame(c1, bg=CARD)
        rbtn.pack(fill="x", pady=(4, 0))
        RoundedButton(rbtn, text="新建", command=lambda: self._add_task("shutdown"),
                      bg=RED, fg="white", font=self.f_body, padx=8, pady=1).pack(side="left", padx=(0, 4))
        RoundedButton(rbtn, text="全部启动", command=lambda: self._start_all("shutdown"),
                      bg=PRIMARY, fg="white", font=self.f_small, padx=8).pack(side="left", padx=(0, 4))
        RoundedButton(rbtn, text="停止", command=lambda: self._stop_all("shutdown"),
                      bg=TEXT_SUB, fg="white", font=self.f_small, padx=8).pack(side="left")

        # 关闭后操作选项（同时作用于手动关闭和定时关闭）
        shutdown_opts = tk.Frame(c1, bg=CARD)
        shutdown_opts.pack(fill="x", pady=(6, 0))
        self.shutdown_var = tk.BooleanVar(value=True)
        tk.Checkbutton(shutdown_opts, text="关闭后关机", variable=self.shutdown_var,
                       font=self.f_small, bg=CARD, fg=RED,
                       selectcolor=CARD, activebackground=CARD,
                       command=self._save_tasks_config).pack(side="left", padx=(0, 8))
        self.restart_var = tk.BooleanVar(value=False)
        tk.Checkbutton(shutdown_opts, text="替代为重启", variable=self.restart_var,
                       font=self.f_small, bg=CARD, fg=TEXT_SUB,
                       selectcolor=CARD, activebackground=CARD,
                       command=self._save_tasks_config).pack(side="left", padx=(0, 8))
        tk.Label(shutdown_opts, text="（关闭模拟器后自动执行）", font=("Microsoft YaHei", 8),
                 bg=CARD, fg=TEXT_LIGHT).pack(side="left")

        # 开机自启选项
        auto_f = tk.Frame(c1, bg=CARD)
        auto_f.pack(fill="x", pady=(4, 0))
        tk.Checkbutton(auto_f, text="开机自启勾选的实例", variable=self.auto_launch_var,
                       font=self.f_small, bg=CARD, fg=TEXT_SUB,
                       selectcolor=CARD, activebackground=CARD,
                       command=self._save_tasks_config).pack(side="left")

        # 分割 + 定时启动
        tk.Frame(c1, bg=BORDER, height=1).pack(fill="x", pady=(4, 4))
        section_title(c1, "定时启动", GREEN)

        launch_scroll_f = tk.Frame(c1, bg=CARD)
        launch_scroll_f.pack(fill="both", expand=True)
        launch_canvas = tk.Canvas(launch_scroll_f, bg=CARD, highlightthickness=0, height=100)
        launch_sb = ttk.Scrollbar(launch_scroll_f, orient="vertical", command=launch_canvas.yview)
        self.launch_tasks_frame = tk.Frame(launch_canvas, bg=CARD)
        def _set_launch_sr(e, c=launch_canvas):
            try:
                b = c.bbox("all")
                if b:
                    c.configure(scrollregion=b)
            except Exception:
                pass
        self.launch_tasks_frame.bind("<Configure>", _set_launch_sr)
        launch_canvas.create_window((0, 0), window=self.launch_tasks_frame, anchor="nw", tags="inner")
        launch_canvas.configure(yscrollcommand=launch_sb.set)
        launch_canvas.pack(side="left", fill="both", expand=True)
        launch_sb.pack(side="right", fill="y")
        self._launch_canvas = launch_canvas

        lbtn = tk.Frame(c1, bg=CARD)
        lbtn.pack(fill="x", pady=(4, 0))
        RoundedButton(lbtn, text="新建", command=self._add_launch_task,
                      bg=GREEN, fg="white", font=self.f_body, padx=8, pady=1).pack(side="left", padx=(0, 4))
        RoundedButton(lbtn, text="全部启动", command=lambda: self._start_all_launch(),
                      bg=GREEN, fg="white", font=self.f_small, padx=8).pack(side="left", padx=(0, 4))
        RoundedButton(lbtn, text="停止", command=lambda: self._stop_all_launch(),
                      bg=TEXT_SUB, fg="white", font=self.f_small, padx=8).pack(side="left")

        # ============================================================
        # 底部栏
        # ============================================================
        bt = tk.Frame(main_frame, bg=BG)
        bt.pack(fill="x", pady=(4, 0))
        tk.Checkbutton(bt, text="开机自启", variable=self.auto_start_var,
                       font=self.f_small, bg=BG, fg=TEXT_SUB,
                       selectcolor=CARD, activebackground=BG,
                       command=self._on_auto_start_toggle).pack(side="left")
        RoundedButton(bt, text="最小化", command=self._minimize_to_tray,
                      bg=TEXT_LIGHT, fg="white", font=self.f_small, padx=10).pack(side="right")

        # 底栏
        ft = tk.Frame(root, bg=BG, height=16)
        ft.pack(fill="x")
        tk.Label(ft, text="v4.2 · 环境检测 · 模拟器管理 · 定时任务 · 关机联动",
                 font=("Microsoft YaHei", 7), bg=BG, fg=TEXT_LIGHT).pack(expand=True)

        # 统一滚轮处理：所有 widget 已创建完毕，可以安全引用所有变量
        self._canvas_scroll_map = {
            id(main_canvas): main_canvas,
            id(shutdown_canvas): shutdown_canvas,
            id(launch_canvas): launch_canvas,
            id(shutdown_sb): shutdown_canvas,
            id(launch_sb): launch_canvas,
            id(main_scrollbar): main_canvas,
        }
        def _on_mw(event, canvas=None):
            if canvas is not None:
                canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
                return
            w = event.widget
            while w:
                wid = id(w)
                if wid in self._canvas_scroll_map:
                    self._canvas_scroll_map[wid].yview_scroll(
                        int(-1 * (event.delta / 120)), "units")
                    return
                w = getattr(w, "master", None)

        # 直接绑定到每个 Canvas
        main_canvas.bind("<MouseWheel>", lambda e: _on_mw(e, main_canvas))
        shutdown_canvas.bind("<MouseWheel>", lambda e: _on_mw(e, shutdown_canvas))
        launch_canvas.bind("<MouseWheel>", lambda e: _on_mw(e, launch_canvas))

    # ---------- 任务共享逻辑 ----------

    def _create_task_ui(self, parent, task, data, color, on_en_toggle, on_delete, make_extra_widget=None):
        """构建任务行的通用 UI 组件（复用于定时关闭和定时启动）
        返回: {"frame", "row", "h_spin", "m_spin", "cd_spin", "st_lbl", "act_btn", "en_var", "mode_var"}
        """
        mode = data.get("mode", "fixed")
        hour = data.get("hour", 22)
        minute = data.get("minute", 0)
        cd_min = data.get("countdown_min", 30)
        enabled = data.get("enabled", True)

        frame = tk.Frame(parent, bg=CARD, bd=0)
        frame.pack(fill="x")
        tk.Frame(frame, bg=BORDER, height=1).pack(fill="x", side="bottom")

        row = tk.Frame(frame, bg=CARD)
        row.pack(fill="x", pady=4)

        # 启用勾选
        en_var = tk.BooleanVar(value=enabled)
        tk.Checkbutton(row, variable=en_var, bg=CARD,
                       activebackground=CARD, selectcolor=CARD,
                       command=lambda: on_en_toggle(task, en_var)).pack(side="left", padx=(0, 3))

        # 模式
        mode_var = tk.StringVar(value="定点" if mode == "fixed" else "倒计时")
        mode_combo = ttk.Combobox(row, textvariable=mode_var,
                                   values=["定点", "倒计时"], width=4,
                                   state="readonly", font=("Microsoft YaHei", 9))
        mode_combo.pack(side="left", padx=(0, 3))

        # 时间输入
        tf = tk.Frame(row, bg=CARD)
        tf.pack(side="left")
        ff = tk.Frame(tf, bg=CARD)
        h_spin = ttk.Spinbox(ff, from_=0, to=23, width=2,
                              font=("Consolas", 9), format="%02.0f")
        h_spin.pack(side="left")
        h_spin.set(f"{hour:02d}")
        tk.Label(ff, text=":", font=("Consolas", 9), bg=CARD, fg=TEXT).pack(side="left")
        m_spin = ttk.Spinbox(ff, from_=0, to=59, width=2,
                              font=("Consolas", 9), format="%02.0f")
        m_spin.pack(side="left")
        m_spin.set(f"{minute:02d}")

        cf = tk.Frame(tf, bg=CARD)
        cd_spin = ttk.Spinbox(cf, from_=1, to=999, width=3, font=("Consolas", 9))
        cd_spin.pack(side="left")
        cd_spin.set(str(cd_min))
        tk.Label(cf, text="分", font=("Microsoft YaHei", 9),
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

        if make_extra_widget is not None:
            make_extra_widget(row)

        # 状态
        st_lbl = tk.Label(row, text="待启动", font=("Microsoft YaHei", 9),
                          fg=TEXT_LIGHT, bg=CARD, anchor="w")
        st_lbl.pack(side="left", padx=(6, 0))

        # 按钮（command 稍后由调用方绑定）
        act_btn = RoundedButton(row, text="▶",
                                 bg=color, fg="white",
                                 font=("Consolas", 9, "bold"), padx=6, pady=0)
        act_btn.pack(side="right", padx=(2, 0))
        RoundedButton(row, text="x", command=lambda: on_delete(),
                      bg=TEXT_LIGHT, fg="white",
                      font=("Consolas", 9, "bold"), padx=4, pady=0).pack(side="right", padx=(2, 0))

        task["en_var"] = en_var
        task["frame"] = frame
        return {
            "frame": frame, "row": row,
            "h_spin": h_spin, "m_spin": m_spin, "cd_spin": cd_spin,
            "st_lbl": st_lbl, "act_btn": act_btn,
            "en_var": en_var, "mode_var": mode_var,
        }

    @staticmethod
    def _calc_timestamp(mode, h_spin, m_spin, cd_spin):
        """计算任务目标时间戳（定点/倒计时通用）"""
        try:
            if mode == "fixed":
                h, m = int(float(h_spin.get())), int(float(m_spin.get()))
                t = datetime.now().replace(hour=h, minute=m, second=0, microsecond=0)
                if t <= datetime.now():
                    t += timedelta(days=1)
                return t.timestamp()
            else:
                return (datetime.now() + timedelta(minutes=int(float(cd_spin.get())))).timestamp()
        except (ValueError, TypeError):
            return None

    def _make_loop_fn(self, t, time_up_fn, update_fn):
        """创建任务倒计时循环（通用）"""
        def _loop():
            try:
                while t["running"]:
                    rem = int(t["target_ts"] - time.time())
                    if rem <= 0:
                        _log_info(f"计时器到期: id={t.get('id')} type={t.get('type')} target_ts={t.get('target_ts')}")
                        _flush_log()
                        t["_time_up_fn"] = time_up_fn
                        self.root.event_generate('<<TimerExpired>>', when='tail')
                        break
                    t["remaining"] = rem
                    if not t.get("_pending_update"):
                        t["_pending_update"] = True
                        t["_update_fn"] = update_fn
                        self.root.event_generate('<<TimerUpdate>>', when='tail')
                    time.sleep(0.5)
            except Exception as _e:
                _log_error(f"定时器循环异常 id={t.get('id')}", _e)
                _flush_log()
                t["running"] = False
        return _loop
    def _start_task(self, t, color, time_up_fn, update_fn):
        """启动任务（通用逻辑）"""
        if t["running"] or not t["enabled"]:
            return
        ts = self._calc_timestamp(t["mode"], t["vars"]["h_spin"], t["vars"]["m_spin"], t["vars"]["cd_spin"])
        if ts is None:
            return
        t["running"] = True
        t["target_ts"] = ts
        t["remaining"] = int(ts - time.time())
        t["thread"] = threading.Thread(target=self._make_loop_fn(t, time_up_fn, update_fn), daemon=True)
        t["thread"].start()
        t["vars"]["act_btn"].config_bg(TEXT_LIGHT); t["vars"]["act_btn"].set_text("||")
        update_fn(t)
        self._save_tasks_config()

    def _stop_task(self, t, color):
        """停止任务（通用逻辑）"""
        t["running"] = False
        t["_executing"] = False
        for k in ("update_id", "auto_reset_id"):
            if t.get(k):
                try: self.root.after_cancel(t[k])
                except Exception as _e:
                    pass
                t[k] = None
        t["thread"] = None
        t["vars"]["act_btn"].config_bg(color); t["vars"]["act_btn"].set_text("▶")
        t["vars"]["st_lbl"].config(text="待启动", fg=TEXT_LIGHT)
        self._save_tasks_config()

    def _inline_start_task(self, t, color, time_up_fn, update_fn):
        """外部启动任务（从配置加载时调用，通用逻辑）"""
        if t["running"] or not t["enabled"]:
            return
        # 确保回调不为空（从 task dict 回退取）
        if time_up_fn is None:
            time_up_fn = t.get("_time_up_fn")
        if update_fn is None:
            update_fn = t.get("_update_fn")
        if update_fn is None or time_up_fn is None:
            _log_error("_inline_start_task", "回调函数为空，无法启动任务")
            return
        try:
            if t["mode"] == "fixed":
                h = int(float(t["vars"]["h_spin"].get()))
                m = int(float(t["vars"]["m_spin"].get()))
                target = datetime.now().replace(hour=h, minute=m, second=0, microsecond=0)
                if target <= datetime.now():
                    target += timedelta(days=1)
                ts = target.timestamp()
            else:
                ts = (datetime.now() + timedelta(minutes=int(float(t["vars"]["cd_spin"].get())))).timestamp()
        except (ValueError, TypeError) as e:
            _log_error("_inline_start_task", f"时间解析失败: {e}")
            return
        t["running"] = True; t["target_ts"] = ts
        t["remaining"] = int(ts - time.time())  # 预计算剩余时间，让 update_fn 立刻显示正确倒计时
        _log_info(f"任务启动: id={t.get('id')} 模式={t['mode']} 类型={t.get('type')} target_ts={ts} remaining={t['remaining']}")
        _flush_log()
                # 用局部变量捕获回调，避免闭包问题
        _tu_fn = time_up_fn
        _upd_fn = update_fn
        t["thread"] = threading.Thread(target=self._make_loop_fn(t, _tu_fn, _upd_fn), daemon=True)
        t["thread"].start()
        t["vars"]["act_btn"].config_bg(TEXT_LIGHT)
        t["vars"]["act_btn"].set_text("||")
        _upd_fn(t)
        self._save_tasks_config()

    def _auto_reset_task(self, t, color, status_fg):
        """定时定点模式自动重置到明天（通用逻辑）"""
        # 先取消旧的 pending after 回调
        if t.get("auto_reset_id"):
            try: self.root.after_cancel(t["auto_reset_id"])
            except Exception as _e:
                pass
        t["auto_reset_id"] = None
        if not t["enabled"]:
            return
        try:
            h, m = int(float(t["vars"]["h_spin"].get())), int(float(t["vars"]["m_spin"].get()))
        except (ValueError, TypeError):
            return
        target = datetime.now().replace(hour=h, minute=m, second=0, microsecond=0) + timedelta(days=1)
        t["target_ts"] = target.timestamp()
        t["running"] = True
        t["thread"] = threading.Thread(target=self._make_loop_fn(t, t["_time_up_fn"], t["_update_fn"]), daemon=True)
        t["thread"].start()
        t["vars"]["act_btn"].config_bg(TEXT_LIGHT); t["vars"]["act_btn"].set_text("||")
        t["vars"]["st_lbl"].config(text=f"明天 {h:02d}:{m:02d}", fg=status_fg)
        self._save_tasks_config()

    def _on_en_toggle_generic(self, task, en_var, color):
        """启用/禁用切换（通用逻辑）"""
        task["enabled"] = en_var.get()
        if not task["enabled"] and task["running"]:
            task["running"] = False
            # 取消 pending after 回调，防止禁用后仍自动重启
            for k in ("update_id", "auto_reset_id"):
                if task.get(k):
                    try: self.root.after_cancel(task[k])
                    except Exception as _e:
                        pass
                    task[k] = None
            task["vars"]["act_btn"].config_bg(color)
            task["vars"]["act_btn"].set_text("▶")
            task["vars"]["st_lbl"].config(text="已禁用", fg=TEXT_LIGHT)

        self._save_tasks_config()

    def _stop_all_generic(self, tasks_list, color):
        """停止指定列表中所有任务（通用逻辑）"""
        for t in tasks_list:
            if t.get("auto_reset_id"):
                try: self.root.after_cancel(t["auto_reset_id"])
                except Exception as _e:
                    pass
                t["auto_reset_id"] = None
            if t["running"]:
                t["running"] = False
                t["vars"]["act_btn"].config_bg(color)
                t["vars"]["act_btn"].set_text("▶")
                t["vars"]["st_lbl"].config(text="已停止", fg=TEXT_LIGHT)

        self._save_tasks_config()

    # ---------- 任务组件（使用共享逻辑） ----------

    def _make_task_row(self, parent, task_id, data, task_type):
        """构建单个定时关闭任务行"""
        task = {
            "id": task_id, "type": task_type,
            "running": False, "thread": None,
            "remaining": 0, "target_ts": 0, "enabled": data.get("enabled", True),
            "mode": data.get("mode", "fixed"), "hour": data.get("hour", 22),
            "minute": data.get("minute", 0), "cd_min": data.get("countdown_min", 30),
            "update_id": None, "auto_reset_id": None, "_pending_update": False,
        }
        color = RED

        def _update_status(t):
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

        def _time_up(t):
            if not t["running"]:
                return
            _log_info(f"定时执行: id={t.get('id')} 类型={t.get('type')}")
            t["running"] = False
            t["_executing"] = True
            t["vars"]["act_btn"].set_text("…")
            t["vars"]["st_lbl"].config(text="执行中...", fg=YELLOW)
            self._save_tasks_config()

            should_shutdown = self.shutdown_var.get()
            _log_info(f"定时执行: should_shutdown={should_shutdown}")
            def _on_done(count, success, fail_count, failed_names, _, shutdown_executed):
                _log_info(f"定时执行完成: id={t.get('id')} count={count} success={success} shutdown_executed={shutdown_executed}")
                def _ui():
                    t["_executing"] = False
                    t["vars"]["st_lbl"].config(
                        text=f"完成 {success}/{count}" if count > 0 else "无进程",
                        fg=GREEN if fail_count == 0 else YELLOW)
                    if shutdown_executed:
                        t["vars"]["st_lbl"].config(text="关机中...", fg=RED)
                        t["vars"]["act_btn"].config_bg(color); t["vars"]["act_btn"].set_text("▶")
                    elif t["mode"] == "fixed":
                        t["auto_reset_id"] = self.root.after(2000, lambda: self._auto_reset_task(t, RED, PRIMARY))
                    self._save_tasks_config()
                self.root.after(0, _ui)
            graceful_kill_async(on_done=_on_done, do_backup=True, do_shutdown=should_shutdown,
                               should_restart=self.restart_var.get(),
                               mumu_vms_dir=self._get_mumu_vms_dir())

        # 存储回调供 _auto_reset_task 使用
        task["_time_up_fn"] = _time_up
        task["_update_fn"] = _update_status

        def _toggle():
            if task.get("_executing"):
                return
            _stop() if task["running"] else _start()

        def _start():
            self._start_task(task, color, _time_up, _update_status)

        def _stop():
            self._stop_task(task, color)

        def _delete():
            if task["running"]:
                _stop()
            task["frame"].destroy()
            for i, t in enumerate(self.shutdown_tasks):
                if t["id"] == task_id:
                    self.shutdown_tasks.pop(i); break
            self._save_tasks_config()

        ui = self._create_task_ui(parent, task, data, color,
                                   on_en_toggle=lambda t, v: self._on_en_toggle_generic(t, v, RED),
                                   on_delete=_delete)
        ui["act_btn"].config(command=_toggle)
        task["vars"] = ui

        # 5秒测试按钮（完全独立，不碰原任务状态）
        def _test_5s():
            self._toast("测试-关闭任务", "5秒后执行优雅关闭", 3000)
            self.root.after(5000, lambda: graceful_kill_async(
                on_done=lambda c, s, fc, fn, _, se:
                    self._toast("测试完成", f"进程: {s}/{c} {'关机执行' if se else '未关机'}", 5000),
                do_backup=False,
                do_shutdown=self.shutdown_var.get(),
                should_restart=self.restart_var.get(),
                mumu_vms_dir=self._get_mumu_vms_dir(),
            ))
        RoundedButton(ui["row"], text="5s", command=_test_5s,
                      bg=ACCENT, fg="white",
                      font=("Consolas", 9), padx=4, pady=0).pack(side="right", padx=(2, 0))
        return task

    # ---------- 任务管理 ----------

    def _add_task(self, task_type, data=None):
        if data is None:
            data = {}
        tid = self.next_shutdown_id; self.next_shutdown_id += 1
        widget = self._make_task_row(self.shutdown_tasks_frame, tid, data, task_type)
        self.shutdown_tasks.append(widget)
        self._save_tasks_config()

    def _start_all(self, task_type):
        for t in self.shutdown_tasks:
            if t["enabled"] and not t["running"]:
                self._inline_start(t)

    def _inline_start(self, t):
        self._inline_start_task(t, RED, t.get("_time_up_fn"), t.get("_update_fn"))

    def _stop_all(self, task_type):
        self._stop_all_generic(self.shutdown_tasks, RED)

    # ---------- 定时启动任务 ----------

    def _add_launch_task(self, data=None):
        """添加一个定时启动任务"""
        if data is None:
            data = {}
        tid = self.next_launch_id
        self.next_launch_id += 1
        _log_info(f"_add_launch_task: 开始创建 tid={tid}")
        try:
            widget = self._make_launch_task_row(tid, data)
            _log_info("_add_launch_task: _make_launch_task_row 成功")
        except Exception as e:
            _log_error(f"_add_launch_task _make_launch_task_row 异常: {type(e).__name__}: {e}")
            return
        self.launch_tasks.append(widget)
        _log_info(f"_add_launch_task: append 后 launch_tasks 数量={len(self.launch_tasks)}")
        self._save_tasks_config()

    def _make_launch_task_row(self, task_id, data):
        """构建定时启动任务行"""
        inst_names = data.get("instances", [])
        color = GREEN

        task = {
            "id": task_id, "type": "launch",
            "running": False, "thread": None,
            "remaining": 0, "target_ts": 0, "enabled": data.get("enabled", True),
            "mode": data.get("mode", "fixed"), "hour": data.get("hour", 8),
            "minute": data.get("minute", 0), "cd_min": data.get("countdown_min", 30),
            "instances": list(inst_names),
            "update_id": None, "auto_reset_id": None, "_pending_update": False,
        }

        def _update_status(t):
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

        def _time_up(t):
            if not t["running"]:
                _log_info(f"_time_up 跳过: id={t.get('id')} running=False")
                return
            _log_info(f"定时执行(启动): id={t.get('id')} 实例数={len(t.get('instances',[]))}")
            t["running"] = False
            t["_executing"] = True
            t["vars"]["act_btn"].set_text("…")
            t["vars"]["st_lbl"].config(text="启动中...", fg=YELLOW)
            self._save_tasks_config()

            # 读路径：自动检测（不依赖配置文件，跨电脑可用）
            dnconsole = self._ld_paths.get("dnconsole") if self._ld_paths else None
            mumu_path = self._mumu_path
            if not dnconsole or not os.path.isfile(dnconsole):
                from ld_instance_manager import auto_detect_paths, auto_detect_mumu
                _detected = auto_detect_paths()
                dnconsole = _detected.get("dnconsole", "") or dnconsole
                if not mumu_path or not os.path.isfile(mumu_path):
                    mumu_path = _detected.get("mumu_manager_path", "")
                if not mumu_path or not os.path.isfile(mumu_path):
                    info = auto_detect_mumu()
                    if info.get("manager_path"):
                        mumu_path = info["manager_path"]
                    _log_info(f"_time_up: 自动探测 dnconsole={dnconsole}, mumu_path={mumu_path}")

            instances = t["instances"][:]
            if not instances:
                _log_error(f"_time_up: 无实例 task instances={t.get('instances')}")
                t["vars"]["st_lbl"].config(text="无实例可选", fg=TEXT_LIGHT)
                t["_executing"] = False
                return

            # 区分 LD 和 MuMu 实例
            ld_names = [n for n in instances if not n.startswith("mumu_")]
            mumu_keys = [n for n in instances if n.startswith("mumu_")]

            # 仅在有 LD 实例时才需要 dnconsole
            if ld_names:
                if dnconsole and os.path.isfile(dnconsole):
                    self._ld_paths["dnconsole"] = dnconsole
                    _log_info(f"_time_up: 路径就绪 dnconsole={dnconsole}")
                else:
                    _log_error(f"_time_up: 有 LD 实例({len(ld_names)}个)但 dnconsole 不可用，跳过启动")
                    t["vars"]["st_lbl"].config(text="未找到 dnconsole", fg=RED)
                    t["_executing"] = False
                    return
            if mumu_path:
                self._mumu_path = mumu_path
            _log_info(f"_time_up: 路径确认 ld_names={len(ld_names)}个 mumu_keys={len(mumu_keys)}个 dnconsole={bool(dnconsole)} mumu_path={bool(mumu_path)}")

            def _work():
                try:
                    total = len(instances)
                    ok = 0

                    # 读取启动间隔（LD 和 MuMu 共用）
                    _interval = 5
                    try:
                        _interval = int(self.launch_interval_var.get())
                    except (ValueError, TypeError):
                        pass

# LDPlayer
                    if ld_names:
                        results = staggered_launch(
                            dnconsole, ld_names, interval_seconds=_interval,
                            on_status=lambda s: self.root.after(0, lambda: t["vars"]["st_lbl"].config(text=s[:30], fg=YELLOW)),
                        )
                        ok += sum(1 for _, s, _ in results if s)

                    # MuMu
                    if mumu_keys and mumu_path:
                        from ld_instance_manager import launch_mumu_instance
                        for k in mumu_keys:
                            idx = int(k.replace("mumu_", ""))
                            try:
                                succ_mu, _ = launch_mumu_instance(mumu_path, idx)
                                if succ_mu:
                                    ok += 1

                                time.sleep(_interval)
                            except Exception as _e_mu:
                                _log_error(f"_time_up _work: MuMu {idx} 启动失败: {_e_mu}")

                    def _ui():
                        t["_executing"] = False
                        t["vars"]["st_lbl"].config(
                            text=f"完成 {ok}/{total}", fg=GREEN if ok == total else YELLOW)
                        self._scan_and_display_instances()
                        if t["mode"] == "fixed":
                            t["auto_reset_id"] = self.root.after(2000, lambda: self._autoreset_launch(t))
                        self._save_tasks_config()
                    self.root.after(0, _ui)
                except Exception as _e_w:
                    _log_error(f"_time_up _work 异常: {_e_w}")
                    _flush_log()
                    self.root.after(0, lambda: t["vars"]["st_lbl"].config(text="启动异常", fg=RED))
                    self.root.after(0, lambda: t.__setitem__("_executing", False))

            threading.Thread(target=_work, daemon=True).start()

        # 存储回调供 _autoreset_launch 使用
        task["_time_up_fn"] = _time_up
        task["_update_fn"] = _update_status

        def _toggle():
            if task.get("_executing"):
                return
            _stop() if task["running"] else _start()

        def _start():
            if task["running"] or not task["enabled"]:
                return
            if not task["instances"]:
                self._toast("定时启动", "请先点击「选择实例」勾选要启动的实例")
                return
            self._start_task(task, color, _time_up, _update_status)

        def _stop():
            self._stop_task(task, color)

        def _delete():
            if task["running"]:
                _stop()

            task["frame"].destroy()
            for i, t in enumerate(self.launch_tasks):
                if t["id"] == task_id:
                    self.launch_tasks.pop(i); break
            self._save_tasks_config()

        _inst_btn_ref = [None]

        def _make_extra(row):
            inst_btn = RoundedButton(row, text=f"选择实例 ({len(task['instances'])})",
                                     command=lambda: self._pick_instances(task, inst_btn),
                                     bg=GREEN, fg="white", font=self.f_small,
                                     padx=6, pady=1)
            inst_btn.pack(side="left", padx=(4, 0))
            _inst_btn_ref[0] = inst_btn
            return inst_btn

        ui = self._create_task_ui(self.launch_tasks_frame, task, data, color,
                                   on_en_toggle=lambda t, v: self._on_en_toggle_generic(t, v, GREEN),
                                   on_delete=_delete,
                                   make_extra_widget=_make_extra)
        ui["act_btn"].config(command=_toggle)
        task["vars"] = ui
        if not inst_names:
            task["vars"]["st_lbl"].config(text="需要实例", fg=YELLOW)
        if _inst_btn_ref[0]:
            task["vars"]["inst_btn"] = _inst_btn_ref[0]

        # 5秒测试按钮（完全独立，不碰原任务状态）
        def _test_5s():
            instances = task.get("instances", [])
            if not instances:
                self._toast("测试-启动", "请先选择实例", 3000)
                return
            self._toast("测试-启动", f"5秒后启动 {len(instances)} 个实例", 3000)
            self.root.after(5000, lambda: self._exec_test_launch(instances))
        RoundedButton(ui["row"], text="5s", command=_test_5s,
                      bg=ACCENT, fg="white",
                      font=("Consolas", 9), padx=4, pady=0).pack(side="right", padx=(2, 0))
        return task

    def _pick_instances(self, task, btn):
        """弹出实例选择对话框（含 LDPlayer + MuMu）"""
        if not self._instances and not self._mumu_instances:
            messagebox.showinfo("提示", "未检测到实例，请先扫描")
            return

        total = len(self._instances or []) + len(self._mumu_instances or [])
        # 窗口高度按实例数自适应，最小 300，最大 520
        height = min(max(300, total * 44 + 120), 520)

        win = tk.Toplevel(self.root)
        win.title("选择实例")
        win.geometry(f"380x{height}")
        win.minsize(380, 300)
        win.configure(bg=BG)
        win.transient(self.root)
        win.grab_set()

        tk.Label(win, text="勾选要启动的实例:", font=self.f_sec,
                 bg=BG, fg=TEXT).pack(pady=(8, 4))

        f = tk.Frame(win, bg=BG)
        f.pack(fill="both", expand=True, padx=12)

        # Canvas + 滚动条
        canvas = tk.Canvas(f, bg=CARD, highlightthickness=1, highlightbackground=BORDER)
        sb = tk.Scrollbar(f, orient="vertical", command=canvas.yview,
                          width=16, bg=BORDER, troughcolor=BG)
        inner = tk.Frame(canvas, bg=CARD)
        def _set_dialog_sr(e, c=canvas):
            try:
                b = c.bbox("all")
                if b:
                    c.configure(scrollregion=b)
            except Exception:
                pass
        inner.bind("<Configure>", _set_dialog_sr)
        canvas.create_window((0, 0), window=inner, anchor="nw", tags="inner")
        canvas.configure(yscrollcommand=sb.set)

        canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        # 鼠标滚轮支持
        def _on_mousewheel(event):
            canvas.yview_scroll(-1 * (event.delta // 120), "units")
        inner.bind("<MouseWheel>", _on_mousewheel)
        canvas.bind("<MouseWheel>", _on_mousewheel)

        vars_dict = {}

        # LDPlayer 实例
        if self._instances:
            tk.Label(inner, text="  雷电模拟器", font=("Microsoft YaHei", 9, "bold"),
                     bg=CARD, fg=PRIMARY, anchor="w").pack(fill="x", padx=8, pady=(4, 1))
            for inst in self._instances:
                checked = inst['name'] in task['instances']
                var = tk.BooleanVar(value=checked)
                display = inst['name']
                summary = get_instance_summary(inst.get('settings', {}))
                if summary.get('name'):
                    display = f"{inst['name']} ({summary['name']})"
                cb = tk.Checkbutton(inner, text=display, variable=var,
                                    bg=CARD, fg=TEXT, activebackground=CARD,
                                    selectcolor=CARD, anchor="w")
                cb.pack(fill="x", padx=8, pady=1)
                vars_dict[inst['name']] = var

        # MuMu 实例
        if self._mumu_instances:
            tk.Frame(inner, bg=BORDER, height=1).pack(fill="x", padx=8, pady=2)
            tk.Label(inner, text="  MuMu 模拟器", font=("Microsoft YaHei", 9, "bold"),
                     bg=CARD, fg=ACCENT, anchor="w").pack(fill="x", padx=8, pady=(2, 1))
            for inst in self._mumu_instances:
                inst_key = f"mumu_{inst['index']}"
                checked = inst_key in task['instances']
                var = tk.BooleanVar(value=checked)
                display = f"MuMu-{inst['index']} {inst['name']}"
                cb = tk.Checkbutton(inner, text=display, variable=var,
                                    bg=CARD, fg=TEXT, activebackground=CARD,
                                    selectcolor=CARD, anchor="w")
                cb.pack(fill="x", padx=8, pady=1)
                vars_dict[inst_key] = var

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
        started = 0
        for t in self.launch_tasks:
            if t["enabled"] and not t["running"]:
                if not t.get("instances"):
                    _log_info(f"全部启动：任务 id={t.get('id')} 无实例，跳过")
                    continue
                self._inline_start_launch(t)
                started += 1
                _log_info(f"全部启动：任务 id={t.get('id')} 已调用启动")
        if started == 0:
            _log_info("全部启动：没有可启动的任务（全部无实例或已在运行）")

    def _inline_start_launch(self, t):
        if t["running"] or not t["enabled"]:
            return
        if not t.get("instances"):
            return  # 无实例时静默跳过，不弹窗阻塞
        self._inline_start_task(t, GREEN, t.get("_time_up_fn"), t.get("_update_fn"))
    def _autoreset_launch(self, t):
        self._auto_reset_task(t, GREEN, YELLOW)

    def _exec_test_launch(self, instances):
        """5s测试启动：直接启动选中的实例（LDPlayer + MuMu）"""
        import threading as _th
        def _work():
            try:
                ld_names = [n for n in instances if not n.startswith("mumu_")]
                mumu_keys = [n for n in instances if n.startswith("mumu_")]
                total = len(instances)
                succ = 0

                # ---- 自动探测路径（与 _time_up 一致，不依赖后台线程是否完成）----
                dnconsole = self._ld_paths.get("dnconsole") if self._ld_paths else None
                mumu_path = self._mumu_path
                if (not dnconsole or not os.path.isfile(dnconsole)) \
                   or (mumu_keys and (not mumu_path or not os.path.isfile(mumu_path))):
                    from ld_instance_manager import auto_detect_paths, auto_detect_mumu
                    _detected = auto_detect_paths()
                    if not dnconsole or not os.path.isfile(dnconsole):
                        dnconsole = _detected.get("dnconsole", "") or dnconsole
                    if mumu_keys and (not mumu_path or not os.path.isfile(mumu_path)):
                        mumu_path = _detected.get("mumu_manager_path", "") or mumu_path
                    if mumu_keys and (not mumu_path or not os.path.isfile(mumu_path)):
                        info = auto_detect_mumu()
                        if info.get("manager_path"):
                            mumu_path = info["manager_path"]

                # LDPlayer
                if ld_names:
                    from ld_instance_manager import staggered_launch
                    if dnconsole and os.path.isfile(dnconsole):
                        self._ld_paths["dnconsole"] = dnconsole
                        results = staggered_launch(dnconsole, ld_names, interval_seconds=5)
                        succ += sum(1 for _, ok, _ in results if ok)
                    else:
                        for _ in ld_names:
                            _log_error("测试启动: dnconsole 不可用，跳过 LDPlayer 实例")

                # MuMu
                if mumu_keys and mumu_path:
                    self._mumu_path = mumu_path
                    from ld_instance_manager import launch_mumu_instance
                    for k in mumu_keys:
                        idx = int(k.replace("mumu_", ""))
                        try:
                            ok, msg = launch_mumu_instance(mumu_path, idx)
                            if ok:
                                succ += 1

                            time.sleep(3)
                        except Exception as e:
                            _log_error(f"测试启动 MuMu {idx}: {e}")

                fail = total - succ
                self.root.after(0, lambda: self._toast(
                    "测试完成", f"启动: {succ}/{total} 个实例" + (f" 失败{fail}" if fail else ""), 5000))
            except Exception as e:
                _log_error(f"测试启动异常: {e}")
                self.root.after(0, lambda e=e: self._toast("测试失败", str(e)[:60], 5000))
        _th.Thread(target=_work, daemon=True).start()

    def _stop_all_launch(self):
        """停止所有定时启动任务"""
        self._stop_all_generic(self.launch_tasks, GREEN)

    # ---------- 立即关闭（原有按钮，使用优雅关闭） ----------

    def _on_kill_now(self):
        procs = list(self._emu_procs_cache)
        if not procs:
            messagebox.showinfo("提示", "未检测到模拟器进程。")
            return

        # 立即设标记，防止快速双击绕过守卫
        if self._shutdown_running:
            self._kill_status_var.set("关闭中，请勿重复点击")
            return
        self._shutdown_running = True

        should_shutdown = self.shutdown_var.get()
        # 直接执行，不再弹确认框

        graceful_kill_async(
            on_done=lambda count, success, fail_count, failed_names, backup_msg, shutdown_executed: None if self._destroyed else self.root.after(0, lambda: self._on_kill_done(
                count, success, fail_count, failed_names, backup_msg, shutdown_executed)),
            do_backup=True,
            do_shutdown=should_shutdown,
            should_restart=self.restart_var.get(),
            mumu_vms_dir=self._get_mumu_vms_dir(),
        )

    def _on_kill_done(self, count, success, fail_count, failed_names, backup_msg, shutdown_executed):
        """关闭模拟器完成后的回调"""
        self._shutdown_running = False
        if count == 0:
            return
        msg = f"已关闭 {success}/{count} 个模拟器"
        if fail_count > 0:
            msg += f"\n{fail_count} 个失败: {', '.join(failed_names[:5])}"
        if backup_msg:
            msg += f"\n{backup_msg}"
        self._kill_status_var.set(msg)

    def _on_auto_start_toggle(self):
        enable = self.auto_start_var.get()
        ok, err = set_auto_start(enable)
        if not ok:
            messagebox.showerror("设置失败", f"开机自启动设置失败：\n{err}")
            self.auto_start_var.set(not enable)

    # ---------- 模拟器扫描 ----------

    def _start_scan_loop(self):
        self._trigger_scan()
        # 定期扫描改为后台执行，不阻塞主线程
        # 取消旧 timer 后再注册新 timer，防止叠加
        if hasattr(self, 'scan_timer_id') and self.scan_timer_id:
            try:
                self.root.after_cancel(self.scan_timer_id)
            except Exception as _e:
                pass
        self.scan_timer_id = self.root.after(30000, self._start_scan_loop)

    def _trigger_scan(self):
        if self._destroyed or self._emu_scan_pending:
            return
        self._emu_scan_pending = True
        scan_emulators_in_background(self._on_scan_result)

    def _on_scan_result(self, procs):
        if self._destroyed:
            return
        # 调度到主线程赋值，避免后台线程写共享变量
        self.root.after(0, lambda p=procs: self._on_scan_result_ui(p))

    def _on_scan_result_ui(self, procs):
        """在主线程接收扫描结果"""
        try:
            self._emu_procs_cache = procs
        except Exception as _e:
            pass
        finally:
            self._emu_scan_pending = False

    # ---------- 关机由任务触发，无需独立方法 ----------

    # ---------- 配置持久化 ----------

    def _save_tasks_config(self):
        # 加载保护：初始化完成前不保存，防止覆盖已有配置
        if not getattr(self, '_config_loaded', False):
            return
        with self._config_save_lock:
            self._save_tasks_config_inner()

    def _save_tasks_config_inner(self):
        try:
            config = load_tool_config()
            shutdown_data = []
            for t in self.shutdown_tasks:
                try:
                    try:
                        hour = int(float(t["vars"]["h_spin"].get()))
                        minute = int(float(t["vars"]["m_spin"].get()))
                        cd = int(float(t["vars"]["cd_spin"].get()))
                    except Exception:
                        hour = t.get("hour", 0)
                        minute = t.get("minute", 0)
                        cd = t.get("cd_min", 30)
                    shutdown_data.append({
                        "mode": t["mode"],
                        "hour": hour,
                        "minute": minute,
                        "countdown_min": cd,
                        "enabled": t["en_var"].get(),
                    })
                except Exception as e:
                    _log_error(f"_save_tasks_config 关闭任务异常: task_id={t.get('id')} {type(e).__name__}: {e}")
            launch_data = []
            for t in self.launch_tasks:
                try:
                    try:
                        hour = int(float(t["vars"]["h_spin"].get()))
                        minute = int(float(t["vars"]["m_spin"].get()))
                        cd = int(float(t["vars"]["cd_spin"].get()))
                    except Exception:
                        hour = t.get("hour", 8)
                        minute = t.get("minute", 0)
                        cd = t.get("cd_min", 30)
                    launch_data.append({
                        "mode": t["mode"],
                        "hour": hour,
                        "minute": minute,
                        "countdown_min": cd,
                        "enabled": t["en_var"].get(),
                        "instances": list(t.get("instances", [])),
                    })
                except Exception as e:
                    _log_error(f"_save_tasks_config 启动任务异常: task_id={t.get('id')} {type(e).__name__}: {e}")
            # 始终写入任务数据
            config["shutdown_tasks"] = shutdown_data
            config["launch_tasks"] = launch_data
            config["auto_launch"] = self.auto_launch_var.get()
            config["auto_launch_instances"] = list(self._auto_launch_instances)
            config["shutdown_always"] = self.shutdown_var.get()
            config["restart_always"] = self.restart_var.get()
            _log_info(f"_save_tasks_config: 关闭={len(shutdown_data)}个 启动={len(launch_data)}个")

            # 保护：保存时确保 paths 不被擦除（按钮路径用 self._ld_paths，定时器读 config）
            if self._ld_paths:
                config.setdefault("paths", {})
                config["paths"].update(self._ld_paths)
            if self._mumu_path:
                config["mumu_manager_path"] = self._mumu_path
            save_tool_config(config)
        except Exception as e:
            _log_error(f"_save_tasks_config_inner 整体异常: {type(e).__name__}: {e}")

    def _load_tasks_config(self):
        config = load_tool_config()
        # 注意：这里不能设 _config_loaded = True，否则加载过程中的
        # _save_tasks_config 会用空数据覆盖配置文件
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

    def _toast(self, title, message, duration=3000):
        """右下角自动消失的通知（不阻塞操作）"""
        try:
            win = tk.Toplevel(self.root)
            win.overrideredirect(True)
            win.attributes("-topmost", True)
            win.configure(bg="#333")
            # 标题
            tk.Label(win, text=title, font=("Microsoft YaHei", 10, "bold"),
                     bg="#444", fg="white", padx=12, pady=4).pack(fill="x")
            # 内容
            tk.Label(win, text=message, font=("Microsoft YaHei", 9),
                     bg="#333", fg="#ddd", padx=12, pady=8).pack()
            # 定位到右下角
            win.update_idletasks()
            sw = win.winfo_screenwidth()
            sh = win.winfo_screenheight()
            ww = win.winfo_reqwidth()
            wh = win.winfo_reqheight()
            win.geometry(f"+{sw-ww-20}+{sh-wh-60}")
            win.after(duration, win.destroy)
        except Exception as _e:
            _log_error("[TOAST]", _e)  # toast 失败不影响主流程

    def _init_instance_manager(self):
        """初始化实例管理器：加载保存路径，缺失时自动搜索"""
        self._load_auto_launch_instances()
        saved = load_tool_config()
        ld_paths = saved.get("paths", {})
        mumu_path = saved.get("mumu_manager_path", "")

        # 配置迁移：旧版 dnconsole 路径不存在时自动找 ldconsole
        if ld_paths.get("ld_path"):
            old_console = ld_paths.get("dnconsole", "")
            if old_console and not os.path.isfile(old_console):
                new_console = _find_ldconsole(ld_paths["ld_path"])
                if new_console:
                    ld_paths["dnconsole"] = new_console
                    saved["paths"] = ld_paths
                    save_tool_config(saved)

        # 检查保存的路径是否有效
        has_ld = ld_paths.get("ld_path") and bool(_find_ldconsole(ld_paths["ld_path"]))
        has_mumu = mumu_path and os.path.isfile(mumu_path)

        if has_ld:
            self._ld_paths = ld_paths
            self.ld_path_var.set(ld_paths["ld_path"])
            self.ld_path_entry.config(fg=TEXT)
        if has_mumu:
            self._mumu_path = mumu_path
            self.mumu_path_var.set(mumu_path)
            self.mumu_path_entry.config(fg=TEXT)

        # 路径完整则延迟扫描（窗口先显示，实例列表后加载，秒开不卡主线程）
        if has_ld and has_mumu:
            self.root.after(50, self._scan_and_display_instances)
        else:
            def _work():
                try:
                    # 统一调用 auto_detect_paths()（进程→注册表→快捷方式→磁盘遍历）
                    from ld_instance_manager import auto_detect_paths
                    detected = auto_detect_paths()
                    self._apply_detected_paths(detected)
                except Exception as _e:
                    pass
                self.root.after(0, self._save_all_paths)
                self.root.after(0, self._scan_and_display_instances)
            threading.Thread(target=_work, daemon=True).start()

    def _save_all_paths(self):
        """保存 LDPlayer 和 MuMu 路径到内存（不写入配置文件，跨电脑可用）"""
        # 路径仅在内存中，不写入 config JSON
        _log_info(f"路径已保存到内存: LDPlayer路径={bool(self._ld_paths)}, MuMu路径={bool(self._mumu_path)}")

    def _validate_ld_path(self, raw_path):
        """验证并保存 LDPlayer 路径"""
        path = raw_path.strip().strip('"').strip("'")
        _log_info(f"[DEBUG] _validate_ld_path 收到: [{raw_path}] => [{path}]")
        if not path:
            _log_info("[DEBUG] 路径为空")
            return False
        if not os.path.isdir(path):
            _log_error(f"[DEBUG] 目录不存在: {path}")
            return False
        console = _find_ldconsole(path)
        _log_info(f"[DEBUG] _find_ldconsole 返回: {console}")
        if not console:
            _log_info("[DEBUG] 没找到命令行工具")
            return False
        paths = {
            "ld_path": path,
            "dnconsole": console,
            "multiplayer_path": None,
            "dnmultiplayerex": None,
            "vms_config_dir": None,
        }
        vms = os.path.join(path, "vms", "config")
        if os.path.isdir(vms):
            paths["vms_config_dir"] = vms
            _log_info(f"[DEBUG] vms_config_dir = {vms}")
        else:
            _log_error(f"[DEBUG] vms/config 目录不存在: {vms}")
        mp = os.path.join(os.path.dirname(path), "ldmutiplayer")
        if os.path.isdir(mp):
            paths["multiplayer_path"] = mp
            paths["dnmultiplayerex"] = os.path.join(mp, "dnmultiplayerex.exe")
            # 只有 LDPlayer 目录下没有 vms/config 才用多开器的
            if not paths.get("vms_config_dir"):
                vms2 = os.path.join(mp, "vms", "config")
                if os.path.isdir(vms2):
                    paths["vms_config_dir"] = vms2
        self._ld_paths = paths
        self.ld_path_var.set(path)
        self.ld_path_entry.config(fg=TEXT)
        self._save_all_paths()
        _log_info("[DEBUG] 路径保存成功，准备刷新实例")
        try:
            self._scan_and_display_instances()
            _log_info("[DEBUG] 刷新实例完成")
        except Exception as e:
            _log_error(f"[DEBUG] 刷新实例异常: {e}")
        return True

    def _validate_mumu_path(self, raw_path):
        """验证并保存 MuMuManager.exe 路径"""
        path = raw_path.strip().strip('"').strip("'").strip()
        _log_error(f"[MUMU] 收到路径: [{raw_path}] => [{path}]")
        if not path:
            _log_error("[MUMU] 路径为空")
            return False
        if os.path.isdir(path):
            for sub in ["", "nx_main"]:
                candidate = os.path.join(path, sub, "MuMuManager.exe")
                exists = os.path.isfile(candidate)
                _log_error(f"[MUMU] 尝试 {candidate}: {'找到' if exists else '不存在'}")
                if exists:
                    path = candidate
                    break
        else:
            _log_error(f"[MUMU] 不是目录，当文件处理: {path}")
        _log_error(f"[MUMU] 最终路径: {path}, 存在={os.path.isfile(path) if path else False}")
        if not os.path.isfile(path) or "MuMuManager" not in os.path.basename(path):
            _log_error("[MUMU] 验证失败: 不存在或文件名不含 MuMuManager")
            return False
        self._mumu_path = path
        self.mumu_path_var.set(path)
        self.mumu_path_entry.config(fg=TEXT)
        self._save_all_paths()
        _log_error("[MUMU] 保存成功，准备刷新实例")
        try:
            self._scan_and_display_instances()
            _log_error("[MUMU] 刷新完成")
        except Exception as e:
            _log_error(f"[MUMU] 刷新异常: {e}")
        return True

    def _on_ld_path_enter(self):
        """LDPlayer 路径输入框回车/失焦处理"""
        raw = self.ld_path_var.get()
        ok = self._validate_ld_path(raw)
        if ok:
            self.ld_path_entry.config(fg=TEXT)
        else:
            if self._ld_paths.get("ld_path"):
                self.ld_path_var.set(self._ld_paths["ld_path"])
            else:
                self.ld_path_var.set("")
                self.ld_path_entry.config(fg=TEXT_LIGHT)
            _log_error(f"LD路径验证失败: [{raw}]")

    def _on_mumu_path_enter(self):
        """MuMu 路径输入框回车/失焦处理"""
        raw = self.mumu_path_var.get()
        ok = self._validate_mumu_path(raw)
        if ok:
            self.mumu_path_entry.config(fg=TEXT)
        else:
            if self._mumu_path:
                self.mumu_path_var.set(self._mumu_path)
            else:
                self.mumu_path_var.set("")
                self.mumu_path_entry.config(fg=TEXT_LIGHT)
            _log_error(f"MuMu路径验证失败: [{raw}]")

    def _manual_select_ld_path(self):
        """手动选择 LDPlayer 安装目录"""
        path = filedialog.askdirectory(title="选择 LDPlayer 安装目录")
        if not path:
            return
        console = _find_ldconsole(path)
        if not console:
            messagebox.showerror("错误", "所选目录中未找到 ldconsole.exe 或 dnconsole.exe\n请选择 LDPlayer 安装目录（如 D:\\E\\LDPlayer9）")
            return
        paths = {
            "ld_path": path,
            "dnconsole": console,
            "multiplayer_path": None,
            "dnmultiplayerex": None,
            "vms_config_dir": None,
        }
        vms_cfg = os.path.join(path, "vms", "config")
        if os.path.isdir(vms_cfg):
            paths["vms_config_dir"] = vms_cfg
        else:
            # 也可能是多开器模式
            mp = os.path.join(os.path.dirname(path), "ldmutiplayer")
            if os.path.isdir(mp):
                paths["multiplayer_path"] = mp
                paths["dnmultiplayerex"] = os.path.join(mp, "dnmultiplayerex.exe")
                vms_cfg2 = os.path.join(mp, "vms", "config")
                if os.path.isdir(vms_cfg2):
                    paths["vms_config_dir"] = vms_cfg2
        self._ld_paths = paths
        self.ld_path_var.set(path)
        self.ld_path_entry.config(fg=TEXT)
        self._save_all_paths()
        self._scan_and_display_instances()

    def _manual_select_mumu_path(self):
        """手动选择 MuMuManager.exe 路径"""
        path = filedialog.askopenfilename(
            title="选择 MuMuManager.exe",
            filetypes=[("可执行文件", "MuMuManager.exe"), ("所有文件", "*.*")]
        )
        if not path:
            return
        if not os.path.isfile(path) or "MuMuManager" not in os.path.basename(path):
            messagebox.showerror("错误", "请选择 MuMuManager.exe 文件")
            return
        self._mumu_path = path
        self.mumu_path_var.set(path)
        self.mumu_path_entry.config(fg=TEXT)
        self._save_all_paths()
        self._scan_and_display_instances()

    def _get_mumu_vms_dir(self):
        """从 _mumu_path 计算 MuMu vms 目录"""
        if not self._mumu_path:
            return None
        mgr_dir = os.path.dirname(self._mumu_path)
        install_dir = os.path.dirname(mgr_dir)
        vms = os.path.join(install_dir, "vms")
        return vms if os.path.isdir(vms) else None

    def _refresh_instances(self):
        """刷新实例列表：重新扫描实例 + 尝试补全缺失路径"""
        # 如果 MuMu 路径未设置，再次检测
        if not self._mumu_path or not os.path.isfile(self._mumu_path):
            try:
                sc = find_emulator_from_shortcuts()
                if sc.get("mumu_manager") and os.path.isfile(sc["mumu_manager"]):
                    self._mumu_path = sc["mumu_manager"]
                    self.mumu_path_var.set(sc["mumu_manager"])
                    self._save_all_paths()
                    self.mumu_path_entry.config(fg=TEXT)
            except Exception as _e:
                pass
        self._scan_and_display_instances()

    def _scan_and_display_instances(self):
        """扫描并同时显示 LDPlayer + MuMu 实例"""
        # 防重入：如果正在扫描中，跳过本次调用
        if getattr(self, '_scanning_instances', False):
            return
        self._scanning_instances = True
        try:
                # ---- 扫描 LDPlayer 实例 ----
            vms_cfg = self._ld_paths.get("vms_config_dir")
            # 路径缺失 → 异步检测（不阻塞主线程）
            if not vms_cfg or not self._mumu_path:
                if not self._path_search_running:
                    self._path_search_running = True
                    def _search():
                        try:
                            from ld_instance_manager import auto_detect_paths
                            detected = auto_detect_paths()
                            self.root.after(0, lambda: self._apply_detected_paths(detected))
                        finally:
                            self._path_search_running = False
                    threading.Thread(target=_search, daemon=True).start()
                # 用现有路径继续往下（可能为 None），不阻塞主线程等异步结果

            ld_instances = []
            if vms_cfg:
                ld_instances = scan_instances(vms_cfg)
                dnconsole = self._ld_paths.get("dnconsole")
                check_running_instances(ld_instances, dnconsole)
            self._instances = ld_instances

            # ---- 扫描 MuMu 实例 ----
            mumu_instances = []
            if self._mumu_path and os.path.isfile(self._mumu_path):
                mumu_instances = scan_mumu_instances(self._mumu_path)
            self._mumu_instances = mumu_instances

            # ---- 无闪烁重建：隐藏→重建→显示 ----
            self.inst_rows_frame.pack_forget()
            for w in self.inst_rows_frame.winfo_children():
                w.destroy()

            self._inst_vars = []
            has_any = bool(ld_instances or mumu_instances)

            if not has_any:
                tk.Label(self.inst_rows_frame, text="  未找到任何实例",
                         font=("Microsoft YaHei", 9), bg=CARD, fg=TEXT_SUB).pack(fill="x")
            else:
                # >>> LDPlayer 实例 <<<
                if ld_instances:
                    # 子标题
                    ld_hdr = tk.Frame(self.inst_rows_frame, bg=BG_LIGHT)
                    ld_hdr.pack(fill="x", pady=(0, 2))
                    tk.Label(ld_hdr, text="  雷电模拟器", font=self.f_small,
                             bg=BG_LIGHT, fg=PRIMARY).pack(side="left")
                    RoundedButton(ld_hdr, text="启动全部", command=self._on_ld_launch_all,
                                  bg=GREEN, fg="white", font=self.f_small, padx=6, pady=1).pack(side="right", padx=(0, 4))
                    RoundedButton(ld_hdr, text="关闭全部", command=self._on_ld_shutdown_all,
                                  bg=RED, fg="white", font=self.f_small, padx=6, pady=1).pack(side="right", padx=(0, 4))

                    for inst in ld_instances:
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
                        root_mark = "Y" if summary['root'] else "N"
                        set_text = f"{summary['cpu']}核 {summary['memory']}M Root:{root_mark}"

                        display_name = inst['name']
                        if summary['name']:
                            display_name = f"{inst['name']} ({summary['name']})"
                        tk.Label(row, text=display_name, font=("Microsoft YaHei", 9, "bold"),
                                 bg=CARD, fg=TEXT, width=18, anchor="w").pack(side="left")
                        tk.Label(row, text=set_text, font=("Consolas", 9),
                                 bg=CARD, fg=TEXT_SUB, width=30, anchor="w").pack(side="left")

                        status = "运行中" if inst['running'] else "已停止"
                        color = GREEN if inst['running'] else TEXT_LIGHT
                        tk.Label(row, text=status, font=("Microsoft YaHei", 9),
                                 bg=CARD, fg=color, width=6, anchor="w").pack(side="left")

                        # 单个启动/关闭按钮
                        inst_name = inst['name']
                        RoundedButton(row, text="▶", command=lambda n=inst_name: self._on_ld_launch_one(n),
                                      bg=GREEN, fg="white", font=("Consolas", 8, "bold"),
                                      padx=4, pady=0).pack(side="right", padx=(1, 0))
                        RoundedButton(row, text="■", command=lambda n=inst_name: self._on_ld_stop_one(n),
                                      bg=RED, fg="white", font=("Consolas", 8, "bold"),
                                      padx=4, pady=0).pack(side="right", padx=(1, 0))

                # >>> MuMu 实例 <<<
                if mumu_instances:
                    # 分隔
                    tk.Frame(self.inst_rows_frame, bg=BORDER, height=1).pack(fill="x", pady=(4, 2))
                    # 子标题
                    mm_hdr = tk.Frame(self.inst_rows_frame, bg=BG_LIGHT)
                    mm_hdr.pack(fill="x", pady=(0, 2))
                    tk.Label(mm_hdr, text="  MuMu 模拟器", font=self.f_small,
                             bg=BG_LIGHT, fg=ACCENT).pack(side="left")
                    # 操作按钮
                    RoundedButton(mm_hdr, text="启动全部", command=self._on_mumu_launch_all,
                                  bg=GREEN, fg="white", font=self.f_small, padx=6, pady=1).pack(side="right", padx=(0, 4))
                    RoundedButton(mm_hdr, text="关闭全部", command=self._on_mumu_shutdown_all,
                                  bg=RED, fg="white", font=self.f_small, padx=6, pady=1).pack(side="right", padx=(0, 4))

                    # 诊断按钮
                    RoundedButton(mm_hdr, text="诊断", command=self._mumu_diagnose,
                                  bg=ORANGE_LIGHT, fg="white", font=("Microsoft YaHei", 8),
                                  padx=4, pady=0).pack(side="left", padx=(2, 0))

                    for inst in mumu_instances:
                        row = tk.Frame(self.inst_rows_frame, bg=CARD)
                        row.pack(fill="x", pady=1)

                        idx = inst['index']
                        name = inst['name']
                        running = inst['running']

                        display_name = f"MuMu-{idx} {name}"

                        # 实例信息
                        cpu = inst.get('cpu', '?')
                        mem = inst.get('memory', '?')
                        root = inst.get('root', False)
                        root_mark = "Y" if root else "N"
                        try:
                            mem_int = int(float(mem))
                            mem_str = f"{mem_int}M"
                        except (ValueError, TypeError):
                            mem_str = f"{mem}M"
                        info_text = f"{cpu}核 {mem_str} Root:{root_mark}"

                        var = tk.BooleanVar(value=inst['name'] in self._auto_launch_instances)
                        cb = tk.Checkbutton(row, variable=var, bg=CARD,
                                            activebackground=CARD, selectcolor=CARD, width=4,
                                            command=self._save_auto_launch_instances)
                        cb.pack(side="left")
                        self._inst_vars.append((var, inst))

                        tk.Label(row, text=display_name, font=("Microsoft YaHei", 9, "bold"),
                                 bg=CARD, fg=TEXT, width=18, anchor="w").pack(side="left")
                        tk.Label(row, text=info_text, font=("Consolas", 9),
                                 bg=CARD, fg=TEXT_SUB, width=22, anchor="w").pack(side="left")

                        status = "运行中" if running else "已停止"
                        color = GREEN if running else TEXT_LIGHT
                        tk.Label(row, text=status, font=("Microsoft YaHei", 9),
                                 bg=CARD, fg=color, width=6, anchor="w").pack(side="left")

                        # 单个启动/关闭按钮
                        RoundedButton(row, text="▶", command=lambda i=idx: self._on_mumu_launch_one(i),
                                      bg=GREEN, fg="white", font=("Consolas", 8, "bold"),
                                      padx=4, pady=0).pack(side="right", padx=(1, 0))
                        RoundedButton(row, text="■", command=lambda i=idx: self._on_mumu_shutdown_one(i),
                                      bg=RED, fg="white", font=("Consolas", 8, "bold"),
                                      padx=4, pady=0).pack(side="right", padx=(1, 0))

                # 更新路径显示 — 状态信息直接看实例列表，不额外显示
                pass

            self.inst_rows_frame.pack(fill="x")



            if not self._startup_launch_done:
                self.root.after(500, self._auto_launch_on_startup)
        finally:
            self._scanning_instances = False

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
        """软件启动时自动启动勾选的实例
        策略：仅当实例配置文件丢失或损坏时从快照恢复，配置正常则直接启动
        关闭前自动保存"最后状态"快照，确保有关机恢复点
        """
        if self._startup_launch_done:
            return
        self._startup_launch_done = True
        if not self.auto_launch_var.get():
            return
        if not self._auto_launch_instances:
            return

        # 分离 LDPlayer 和 MuMu 实例
        selected_ld = [name for name in self._auto_launch_instances
                       if any(inst['name'] == name for inst in self._instances)]
        selected_mumu = [name for name in self._auto_launch_instances
                         if any(inst['name'] == name for inst in self._mumu_instances)]

        if not selected_ld and not selected_mumu:
            return

        try:
            interval = int(self.launch_interval_var.get())
        except ValueError:
            interval = 5

        def _work():
            # ---- LDPlayer 自启动 ----
            if selected_ld:
                dnconsole = self._ld_paths.get("dnconsole")
                if dnconsole and os.path.isfile(dnconsole):
                    vms_cfg = self._ld_paths.get("vms_config_dir")
                    if vms_cfg and os.path.isdir(vms_cfg):
                        # 检查所有勾选实例的配置文件是否完好
                        config_ok = True
                        for name in selected_ld:
                            cfg_path = os.path.join(vms_cfg, f"{name}.config")
                            if not os.path.isfile(cfg_path):
                                config_ok = False
                                break
                            try:
                                with open(cfg_path, 'r', encoding='utf-8') as f:
                                    cfg = json.load(f)
                                if not cfg or not isinstance(cfg, dict):
                                    config_ok = False
                                    break
                            except Exception:
                                config_ok = False
                                break

                        if not config_ok:
                            self.root.after(0, lambda: self.launch_status_var.set("配置异常，正在从快照恢复..."))
                            mp_cfg = None
                            mp = self._ld_paths.get("multiplayer_path")
                            if mp:
                                mp_cfg = os.path.join(mp, "vms", "config")
                            snapshots = list_snapshots(SNAPSHOT_DIR)
                            if snapshots:
                                latest = snapshots[0]
                                count, msg = restore_snapshot(latest['path'], vms_cfg, mp_cfg, mumu_vms_dir=self._get_mumu_vms_dir())
                                self.root.after(0, lambda s=msg: self.launch_status_var.set(f"已恢复: {s}"))
                                time.sleep(1)
                        else:
                            self.root.after(0, lambda: self.launch_status_var.set("配置正常，跳过恢复"))

                    self.root.after(0, lambda: self.launch_status_var.set(f"正在启动 {len(selected_ld)} 个实例..."))
                    results = staggered_launch(
                        dnconsole, selected_ld, interval,
                        on_status=lambda t: self.root.after(0, lambda: self.launch_status_var.set(t)),
                    )
                    ok = sum(1 for _, s, _ in results if s)
                    self.root.after(0, lambda: self.launch_status_var.set(f"自启动完成 {ok}/{len(results)}"))

            # ---- MuMu 自启动 ----
            if selected_mumu:
                from ld_instance_manager import launch_mumu_instance
                self.root.after(0, lambda: self.launch_status_var.set(f"正在启动 {len(selected_mumu)} 个 MuMu 实例..."))
                ok = 0
                for name in selected_mumu:
                    for inst in self._mumu_instances:
                        if inst.get('name') == name:
                            idx = inst['index']
                            result = launch_mumu_instance(self._mumu_path, idx)
                            if result:
                                ok += 1

                            break
                    time.sleep(interval)
                self.root.after(0, lambda: self.launch_status_var.set(f"MuMu 自启动完成 {ok}/{len(selected_mumu)}"))

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
        # 先查 LDPlayer 实例
        for i in self._instances:
            if i['name'] == inst_name:
                inst = i
                break
        # 没找到则查 MuMu 实例（按 index 匹配）
        if not inst:
            for i in self._mumu_instances:
                if str(i.get('index', '')) == inst_name or i.get('name') == inst_name:
                    inst = i
                    break
        if not inst:
            return

        self._open_settings_editor(inst)

    def _open_settings_editor(self, inst):
        """打开设置编辑窗口"""
        # MuMu 实例：从 vm_config.json 读取/写入
        is_mumu = ('settings' not in inst or not inst.get('settings')
                   or 'config_path' not in inst or not inst.get('config_path'))
        if is_mumu:
            # 定位实例的 configs 目录
            cfg_dir = None
            if self._mumu_path:
                mgr_dir = os.path.dirname(self._mumu_path)
                install_dir = os.path.dirname(mgr_dir)
                ud = os.path.expanduser("~\\Documents")
                for d in [os.path.join(install_dir, "vms"),
                          os.path.join(ud, "MuMu12", "vms"),
                          os.path.join(ud, "MuMuPlayer-12.0", "vms")]:
                    p = os.path.join(d, f"MuMuPlayer-12.0-{inst['index']}", "configs")
                    if os.path.isdir(p):
                        cfg_dir = p; break
            if not cfg_dir:
                messagebox.showinfo("提示", "找不到 MuMu 实例配置目录")
                return

            # 读取各配置文件
            vm_path = os.path.join(cfg_dir, "vm_config.json")
            shell_path = os.path.join(cfg_dir, "shell_config.json")
            extra_path = os.path.join(cfg_dir, "extra_config.json")

            vm = {}
            shell = {}
            extra = {}
            try:
                with open(vm_path, 'r', encoding='utf-8') as f:
                    vm = json.load(f).get("vm", {})
            except Exception:
                pass
            try:
                with open(shell_path, 'r', encoding='utf-8') as f:
                    shell = json.load(f)
            except Exception:
                pass
            try:
                with open(extra_path, 'r', encoding='utf-8') as f:
                    extra = json.load(f)
            except Exception:
                pass

            # 从 shell_config 读取分辨率/帧率/DPI
            renderer = shell.get("renderer", {})
            shell_res = renderer.get("resolution", {})
            res_w = str(int(float(shell_res.get("width", 1920))))
            res_h = str(int(float(shell_res.get("height", 1080))))
            res_dpi = str(int(float(shell_res.get("dpi", 280))))
            fps_val = str(int(float(renderer.get("fps_limit", renderer.get("fps_limit_real", 60)))))

            # 从 extra_config 读取设备名
            dev_name = extra.get("playerName", inst.get('name', ''))

            # 从 vm_config 读取 CPU/内存/Root
            cpu_val = str(vm.get("cpu", inst.get('cpu', "4")))
            mem_gb = vm.get("memory", inst.get('memory', "4"))
            try:
                mem_mb = int(float(str(mem_gb)) * 1024)
            except (ValueError, TypeError):
                mem_mb = 4096
            root_val = str(vm.get("root", "false")).lower() == "true"

            win = tk.Toplevel(self.root)
            win.title(f"MuMu-{inst['index']} {inst['name']}")
            win.geometry("440x500")
            win.configure(bg=BG)
            win.transient(self.root)
            win.grab_set()

            tk.Label(win, text=f"MuMu-{inst['index']} {inst['name']}", font=self.f_sec,
                     bg=BG, fg=TEXT).pack(pady=(12, 8))

            form = tk.Frame(win, bg=BG, padx=20)
            form.pack(fill="x")

            fields = {}

            # 分辨率
            tk.Label(form, text="分辨率:", font=("Microsoft YaHei", 10),
                     bg=BG, fg=TEXT, width=10, anchor="w").grid(row=0, column=0, pady=4, sticky="w")
            res_frame = tk.Frame(form, bg=BG)
            res_frame.grid(row=0, column=1, pady=4, sticky="w")
            w_var = tk.StringVar(value=res_w)
            h_var = tk.StringVar(value=res_h)
            tk.Entry(res_frame, textvariable=w_var, width=6, font=("Consolas", 10),
                     bg=BG_LIGHT, fg=TEXT, relief="flat").pack(side="left")
            tk.Label(res_frame, text="x", bg=BG, fg=TEXT).pack(side="left", padx=2)
            tk.Entry(res_frame, textvariable=h_var, width=6, font=("Consolas", 10),
                     bg=BG_LIGHT, fg=TEXT, relief="flat").pack(side="left")
            fields['resolution'] = (w_var, h_var)

            # DPI
            tk.Label(form, text="DPI:", font=("Microsoft YaHei", 10),
                     bg=BG, fg=TEXT, width=10, anchor="w").grid(row=1, column=0, pady=4, sticky="w")
            dpi_var = tk.StringVar(value=res_dpi)
            tk.Entry(form, textvariable=dpi_var, width=10, font=("Consolas", 10),
                     bg=BG_LIGHT, fg=TEXT, relief="flat").grid(
                row=1, column=1, pady=4, sticky="w")
            fields['dpi'] = dpi_var

            # CPU
            row = 2
            tk.Label(form, text="CPU核心:", font=("Microsoft YaHei", 10),
                     bg=BG, fg=TEXT, width=10, anchor="w").grid(row=row, column=0, pady=4, sticky="w")
            cpu_var = tk.StringVar(value=cpu_val)
            tk.Spinbox(form, from_=1, to=16, textvariable=cpu_var, width=8,
                       font=("Consolas", 10), bg=BG_LIGHT, fg=TEXT).grid(row=row, column=1, pady=4, sticky="w")
            fields['cpu'] = cpu_var

            # 内存：vm_config.json 存的是 GB（如 "6.000000"），显示为 MB
            row = 3
            tk.Label(form, text="内存(MB):", font=("Microsoft YaHei", 10),
                     bg=BG, fg=TEXT, width=10, anchor="w").grid(row=row, column=0, pady=4, sticky="w")
            mem_var = tk.StringVar(value=str(mem_mb))
            tk.Spinbox(form, from_=256, to=16384, increment=256, textvariable=mem_var, width=8,
                       font=("Consolas", 10), bg=BG_LIGHT, fg=TEXT).grid(row=row, column=1, pady=4, sticky="w")
            fields['memory'] = mem_var

            # Root
            row = 4
            root_var = tk.BooleanVar(value=root_val)
            tk.Checkbutton(form, text="Root 权限", variable=root_var,
                           font=("Microsoft YaHei", 10), bg=BG, fg=TEXT,
                           selectcolor=CARD, activebackground=BG
                           ).grid(row=row, column=0, columnspan=2, pady=4, sticky="w")
            fields['root'] = root_var

            # 帧率
            row = 6
            tk.Label(form, text="帧率:", font=("Microsoft YaHei", 10),
                     bg=BG, fg=TEXT, width=10, anchor="w").grid(row=row, column=0, pady=4, sticky="w")
            fps_var = tk.StringVar(value=fps_val)
            ttk.Combobox(form, textvariable=fps_var, values=["20","30","60","72","120","144","240"],
                         width=8, font=("Consolas", 10), state="readonly").grid(
                row=row, column=1, pady=4, sticky="w")
            fields['fps'] = fps_var

            # 设备名
            row = 7
            tk.Label(form, text="设备名:", font=("Microsoft YaHei", 10),
                     bg=BG, fg=TEXT, width=10, anchor="w").grid(row=row, column=0, pady=4, sticky="w")
            name_var = tk.StringVar(value=dev_name)
            tk.Entry(form, textvariable=name_var, width=20, font=("Consolas", 10),
                     bg=BG_LIGHT, fg=TEXT, relief="flat").grid(
                row=row, column=1, pady=4, sticky="w")
            fields['dev_name'] = name_var

            # ADB 端口（只读）
            row = 8
            tk.Label(form, text="ADB端口:", font=("Microsoft YaHei", 10),
                     bg=BG, fg=TEXT, width=10, anchor="w").grid(row=row, column=0, pady=4, sticky="w")
            from ld_instance_manager import get_mumu_adb_port
            adb_port = get_mumu_adb_port(inst['index'])
            tk.Label(form, text=str(adb_port), font=("Consolas", 10),
                     bg=BG_LIGHT, fg=TEXT_SUB, anchor="w", padx=4).grid(
                row=row, column=1, pady=4, sticky="w")

            def _save_mumu():
                if inst['running']:
                    messagebox.showwarning("警告", "实例正在运行，请先关闭再修改")
                    return
                try:
                    # 保存 vm_config.json（CPU/内存/Root）
                    with open(vm_path, 'r', encoding='utf-8') as f:
                        cfg = json.load(f)
                    vm_new = cfg.get("vm", {})
                    vm_new["cpu"] = int(fields['cpu'].get())
                    vm_new["memory"] = f"{float(fields['memory'].get()) / 1024:.6f}"
                    vm_new["root"] = "true" if fields['root'].get() else "false"
                    cfg["vm"] = vm_new
                    with open(vm_path, 'w', encoding='utf-8') as f:
                        json.dump(cfg, f, ensure_ascii=False, indent=2)
                    # 保存 shell_config.json（分辨率/DPI/帧率）
                    with open(shell_path, 'r', encoding='utf-8') as f:
                        shell_cfg = json.load(f)
                    renderer = shell_cfg.get("renderer", {})
                    res = renderer.get("resolution", {})
                    res["width"] = f"{float(fields['resolution'][0].get()):.6f}"
                    res["height"] = f"{float(fields['resolution'][1].get()):.6f}"
                    res["dpi"] = f"{float(fields['dpi'].get()):.6f}"
                    renderer["resolution"] = res
                    renderer["fps_limit"] = fields['fps'].get()
                    renderer["fps_limit_real"] = fields['fps'].get()
                    shell_cfg["renderer"] = renderer
                    with open(shell_path, 'w', encoding='utf-8') as f:
                        json.dump(shell_cfg, f, ensure_ascii=False, indent=2)
                    # 保存 extra_config.json（设备名）
                    with open(extra_path, 'r', encoding='utf-8') as f:
                        extra_cfg = json.load(f)
                    extra_cfg["playerName"] = fields['dev_name'].get()
                    with open(extra_path, 'w', encoding='utf-8') as f:
                        json.dump(extra_cfg, f, ensure_ascii=False, indent=2)
                    messagebox.showinfo("成功", f"MuMu-{inst['index']} 设置已保存")
                    win.destroy()
                    self._scan_and_display_instances()
                except Exception as e:
                    messagebox.showerror("错误", f"保存失败: {e}")

            btn_frame = tk.Frame(win, bg=BG)
            btn_frame.pack(fill="x", padx=20, pady=12)
            RoundedButton(btn_frame, text="保存", command=_save_mumu,
                          bg=GREEN, fg="white", font=("Microsoft YaHei", 10, "bold"),
                          padx=16).pack(side="left", padx=(0, 8))
            RoundedButton(btn_frame, text="取消", command=win.destroy,
                          bg=TEXT_LIGHT, fg="white", font=("Microsoft YaHei", 10),
                          padx=16).pack(side="left")
            return

        # LDPlayer 实例编辑（原有逻辑）
        win = tk.Toplevel(self.root)
        win.title(f"编辑 - {inst['name']}")
        win.geometry("440x500")
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
        res_str = summary.get('resolution', '') or ''
        if 'x' in res_str and res_str not in ('?x?', 'x', ''):
            res_parts = res_str.split('x')
            w_val, h_val = res_parts[0], res_parts[1]
        else:
            w_val, h_val = '720', '1280'
        w_var = tk.StringVar(value=str(w_val))
        h_var = tk.StringVar(value=str(h_val))
        tk.Entry(res_frame, textvariable=w_var, width=6, font=("Consolas", 10),
                 bg=BG_LIGHT, fg=TEXT, relief="flat").pack(side="left")
        tk.Label(res_frame, text="x", bg=BG, fg=TEXT).pack(side="left", padx=2)
        tk.Entry(res_frame, textvariable=h_var, width=6, font=("Consolas", 10),
                 bg=BG_LIGHT, fg=TEXT, relief="flat").pack(side="left")
        fields['resolution'] = (w_var, h_var)

        # DPI
        tk.Label(form, text="DPI:", font=("Microsoft YaHei", 10),
                 bg=BG, fg=TEXT, width=10, anchor="w").grid(row=1, column=0, pady=4, sticky="w")
        dpi_var = tk.StringVar(value=str(settings.get('advancedSettings.resolutionDpi', 240)))
        tk.Entry(form, textvariable=dpi_var, width=10, font=("Consolas", 10),
                 bg=BG_LIGHT, fg=TEXT, relief="flat").grid(
            row=1, column=1, pady=4, sticky="w")
        fields['dpi'] = dpi_var

        # CPU
        tk.Label(form, text="CPU核心:", font=("Microsoft YaHei", 10),
                 bg=BG, fg=TEXT, width=10, anchor="w").grid(row=2, column=0, pady=4, sticky="w")
        cpu_var = tk.StringVar(value=str(summary['cpu']))
        tk.Spinbox(form, from_=1, to=8, textvariable=cpu_var, width=8,
                   font=("Consolas", 10), bg=BG_LIGHT, fg=TEXT).grid(row=2, column=1, pady=4, sticky="w")
        fields['cpu'] = cpu_var

        # 内存
        tk.Label(form, text="内存(MB):", font=("Microsoft YaHei", 10),
                 bg=BG, fg=TEXT, width=10, anchor="w").grid(row=3, column=0, pady=4, sticky="w")
        mem_var = tk.StringVar(value=str(summary['memory']))
        tk.Spinbox(form, from_=256, to=8192, increment=256, textvariable=mem_var, width=8,
                   font=("Consolas", 10), bg=BG_LIGHT, fg=TEXT).grid(row=3, column=1, pady=4, sticky="w")
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
        tk.Entry(form, textvariable=name_var, width=20, font=("Consolas", 10),
                 bg=BG_LIGHT, fg=TEXT, relief="flat").grid(
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
        try:
            vms_cfg = self._ld_paths.get("vms_config_dir")
            mp_cfg = None
            mp = self._ld_paths.get("multiplayer_path")
            if not vms_cfg or not mp:
                from ld_instance_manager import auto_detect_paths
                detected = auto_detect_paths()
                if not vms_cfg and detected.get("vms_config_dir"):
                    vms_cfg = detected["vms_config_dir"]
                if not mp and detected.get("multiplayer_path"):
                    mp = detected["multiplayer_path"]
                if not self._mumu_path and detected.get("mumu_manager_path"):
                    self._mumu_path = detected["mumu_manager_path"]
            if mp:
                mp_cfg = os.path.join(mp, "vms", "config")

            snap_dir, msg = save_snapshot(vms_cfg, mp_cfg, SNAPSHOT_DIR, mumu_vms_dir=self._get_mumu_vms_dir())
            if snap_dir:
                _log_info(f"保存快照成功: {msg}")
                self._toast("保存成功", msg)
            else:
                _log_error(f"保存快照失败: vms_cfg={vms_cfg} mp_cfg={mp_cfg} msg={msg}")
                self._toast("保存失败", msg, 4000)
        except Exception as e:
            _log_error(f"保存快照异常: {e}")
            messagebox.showerror("保存失败", f"保存快照时发生异常:\n{e}")

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
            try:
                sel = listbox.curselection()
                if not sel:
                    messagebox.showinfo("提示", "请选一个快照")
                    return
                snap = snapshots[sel[0]]

                # 路径自动检测（不依赖配置文件，跨电脑可用）
                vms_cfg = self._ld_paths.get("vms_config_dir")
                mp = self._ld_paths.get("multiplayer_path")
                if not vms_cfg or not mp:
                    from ld_instance_manager import auto_detect_paths
                    detected = auto_detect_paths()
                    if not vms_cfg and detected.get("vms_config_dir"):
                        vms_cfg = detected["vms_config_dir"]
                    if not mp and detected.get("multiplayer_path"):
                        mp = detected["multiplayer_path"]
                mp_cfg = os.path.join(mp, "vms", "config") if mp else None

                count, msg = restore_snapshot(snap['path'], vms_cfg, mp_cfg, mumu_vms_dir=self._get_mumu_vms_dir())
                _log_info(f"恢复快照: {msg}（快照={snap['name']}）")
                messagebox.showinfo("恢复完成", msg)
                win.destroy()
                self._scan_and_display_instances()
            except Exception as e:
                _log_error(f"恢复快照异常: {e}")
                messagebox.showerror("恢复失败", f"恢复快照时发生异常:\n{e}")

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
            except Exception as _e:
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
                    for _, _, files in os.walk(d):
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

            for _, name, fcount, _ in all_entries:
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
                except Exception as _e:
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
            err_msg = f"弹窗异常: {e}\n\n{_traceback.format_exc()}"
            exe_dir = os.path.dirname(os.path.abspath(sys.executable)) if getattr(sys, 'frozen', False) else os.path.dirname(os.path.abspath(__file__))
            try:
                with open(os.path.join(exe_dir, "_backup_error.log"), 'w', encoding='utf-8') as f:
                    f.write(err_msg)
            except Exception as _e:
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
            except Exception as _e:
                pass

        # ==== 扫描 ====
        all_snapshots = list_snapshots(SNAPSHOT_DIR)

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

            tk.Label(win, text="手动快照（保存的配置快照）", font=self.f_sec,
                     bg=BG, fg=PRIMARY).pack(pady=(10, 4))
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
                ld = snap.get('ldplayer_count', 0)
                mm = snap.get('mumu_count', 0)
                listbox.insert(tk.END, f"  {snap['timestamp']}  |  {snap['instance_count']} 个实例 (LD:{ld} MuMu:{mm})")

            btn_row = tk.Frame(win, bg=BG)
            btn_row.pack(fill="x", padx=12, pady=(0, 10))

            def _save_geometry(geo):
                try:
                    cfg = load_tool_config()
                    if 'dialog_geometry' not in cfg:
                        cfg['dialog_geometry'] = {}
                    cfg['dialog_geometry']['snapshot_list'] = geo
                    save_tool_config(cfg)
                except Exception as _e:
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
            err_msg = f"弹窗异常: {e}\n\n{_traceback.format_exc()}"
            try:
                log_dir = os.path.dirname(os.path.abspath(sys.executable)) if getattr(sys, 'frozen', False) else os.path.dirname(os.path.abspath(__file__))
                with open(os.path.join(log_dir, "_snapshot_error.log"), 'w', encoding='utf-8') as f:
                    f.write(err_msg)
            except Exception as _e:
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
        count, msg = restore_snapshot(snap['path'], vms_cfg, mp_cfg, mumu_vms_dir=self._get_mumu_vms_dir())
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
            messagebox.showerror("错误", "未找到 LDPlayer 命令行工具")
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



    def _mumu_diagnose(self):
        """MuMu 诊断：扫描弹窗 + ADB 状态，输出到日志和 toast"""
        def _work():
            lines = ["===== MuMu 诊断 ====="]
            # 1. 扫描 Windows 窗口
            from ld_instance_manager import _find_mumu_error_dialog
            dialogs = _find_mumu_error_dialog()
            if dialogs:
                lines.append(f"发现 {len(dialogs)} 个相关弹窗:")
                for hwnd, title in dialogs:
                    lines.append(f"  HWND={hwnd} 标题={title}")
            else:
                lines.append("未发现 MuMu 错误弹窗")

            # 2. ADB 状态
            if self._mumu_instances:
                from ld_instance_manager import (
                    check_mumu_adb_connection,
                    check_mumu_boot_completed,
                    get_mumu_adb_port,
                )
                for inst in self._mumu_instances:
                    idx = inst['index']
                    port = get_mumu_adb_port(idx)
                    adb = check_mumu_adb_connection(idx, timeout=3)
                    boot = "?"
                    if adb:
                        boot = check_mumu_boot_completed(idx, timeout=3)
                    boot_txt = "完" if boot is True else ("等" if boot == "?" else "?")
                    lines.append(f"  实例{idx}({inst['name']}) ADB端口={port} ADB={'连' if adb else '断'} Boot={boot_txt}")

            # 3. 输出
            msg = "\n".join(lines)
            _log_info(msg)
            self.root.after(0, lambda: self._toast("MuMu 诊断", "完成，详情见日志", 4000))
        threading.Thread(target=_work, daemon=True).start()

    # ---------- 日志面板 ----------

    def _find_config_file(self):
        """搜索 instance_config.json"""
        candidates = []

        # 1. 从 ld_instance_manager 的 TOOL_CONFIG_FILE
        try:
            from ld_instance_manager import TOOL_CONFIG_FILE
            candidates.append(TOOL_CONFIG_FILE)
        except Exception as _e:
            pass

        # 2. 从 sys.argv[0]（启动路径）
        try:
            argv_dir = os.path.dirname(os.path.abspath(sys.argv[0]))
            candidates.append(os.path.join(argv_dir, "instance_config.json"))
        except Exception as _e:
            pass

        # 3. 从当前工作目录
        try:
            candidates.append(os.path.join(os.getcwd(), "instance_config.json"))
        except Exception as _e:
            pass

        for path in candidates:
            if path and os.path.isfile(path):
                return path
        return None

    def _apply_detected_paths(self, detected):
        """应用自动搜索到的模拟器路径到实例变量"""
        if detected.get("ld_path"):
            self._ld_paths = detected
            self.root.after(0, lambda: self.ld_path_var.set(detected["ld_path"]))
            self.root.after(0, lambda: self.ld_path_entry.config(fg=TEXT))
            _log_info(f"[PATH] 自动搜索到 LDPlayer: {detected['ld_path']}")
        if not self._mumu_path and detected.get("mumu_manager_path"):
            self._mumu_path = detected["mumu_manager_path"]
            self.root.after(0, lambda: self.mumu_path_var.set(detected["mumu_manager_path"]))
            self.root.after(0, lambda: self.mumu_path_entry.config(fg=TEXT))
            _log_info(f"[PATH] 自动搜索到 MuMu: {detected['mumu_manager_path']}")
        # 触发一次实例扫描
        self.root.after(100, self._scan_and_display_instances)

    def _clear_log_display(self):
        """清空日志面板"""
        if hasattr(self, '_log_text'):
            self._log_text.configure(state="normal")
            self._log_text.delete("1.0", "end")
            self._log_text.configure(state="disabled")

    def _copy_log(self):
        """复制日志内容到剪贴板"""
        if not hasattr(self, '_log_text'):
            return
        try:
            content = self._log_text.get("1.0", "end-1c")
            self.root.clipboard_clear()
            self.root.clipboard_append(content)
            self._toast("已复制", "日志内容已复制到剪贴板")
        except Exception as _e:
            pass

    def _refresh_log_display(self):
        """刷新右侧日志面板（每30秒追加新行）"""
        if not hasattr(self, '_log_text') or not self._log_text.winfo_exists():
            self._log_refresh_id = None
            return
        try:
            self._log_text.configure(state="normal")
            with _LOG_BUFFER_LOCK:
                n = len(_LOG_BUFFER)
                idx = getattr(self, '_log_displayed_count', 0)
                if n > idx:
                    self._log_text.insert("end", "".join(_LOG_BUFFER[idx:]))
                    self._log_displayed_count = n
            self._log_text.see("end")
            self._log_text.configure(state="disabled")
        except Exception as _e:
            pass
        # 每 10 秒写入一次文件，避免崩溃丢日志
        self._log_flush_counter = getattr(self, '_log_flush_counter', 0) + 1
        if self._log_flush_counter >= 10:
            self._log_flush_counter = 0
            _flush_log()
        self._log_refresh_id = self.root.after(5000, self._refresh_log_display)

    # ---------- MuMu 控制 ----------

    def _on_mumu_launch_one(self, index):
        """启动单个 MuMu 实例"""
        def _work():
            from ld_instance_manager import launch_mumu_instance
            ok, msg = launch_mumu_instance(self._mumu_path, index)
            result = {"success": ok, "message": msg}
            self.root.after(0, lambda: self._toast(
                "启动结果" if result["success"] else "启动失败", result["message"]))
            self.root.after(500, self._scan_and_display_instances)
        threading.Thread(target=_work, daemon=True).start()

    def _on_mumu_shutdown_one(self, index):
        """关闭单个 MuMu 实例"""
        def _work():

            ok, msg = shutdown_mumu_instance(self._mumu_path, index)
            self.root.after(0, lambda succ=ok, m=msg: self._toast(
                "关闭结果" if succ else "关闭失败", m, 4000))
            self.root.after(500, self._scan_and_display_instances)
        threading.Thread(target=_work, daemon=True).start()

    def _on_mumu_launch_all(self):
        """启动所有 MuMu 实例（后台线程启动，不阻塞 UI）"""
        if not self._mumu_instances:
            return
        total = len(self._mumu_instances)
        success = [0]
        success_lock = threading.Lock()
        threads = []

        def _launch_one(idx):
                    from ld_instance_manager import launch_mumu_instance
                    r_ok, r_msg = launch_mumu_instance(self._mumu_path, idx)
                    if r_ok:
                        with success_lock:
                            success[0] += 1

        def _work():
            for inst in self._mumu_instances:
                t = threading.Thread(target=_launch_one, args=(inst['index'],), daemon=True)
                t.start()
                threads.append(t)
                try:
                    delay = int(self.launch_interval_var.get())
                except (ValueError, TypeError):
                    delay = 5
                time.sleep(delay)
            for t in threads:
                t.join(timeout=600)
            self.root.after(0, lambda: self._toast("启动完成", f"成功 {success[0]}/{total}"))
            self.root.after(500, self._scan_and_display_instances)

        threading.Thread(target=_work, daemon=True).start()

    def _on_mumu_shutdown_all(self):
        """关闭所有 MuMu 实例"""
        if not self._mumu_instances:
            return
        def _work():

            total = len(self._mumu_instances)
            success = 0
            for inst in self._mumu_instances:
                ok, msg = shutdown_mumu_instance(self._mumu_path, inst['index'])
                if ok:
                    success += 1
            self.root.after(0, lambda: self._toast(
                "关闭完成", f"成功 {success}/{total}", 4000))
            self.root.after(500, self._scan_and_display_instances)
        threading.Thread(target=_work, daemon=True).start()

    # ---------- LDPlayer 全部启动/关闭 ----------

    def _on_ld_launch_all(self):
        """启动所有 LDPlayer 实例"""
        if not self._instances:
            return
        dnconsole = self._ld_paths.get("dnconsole")
        if not dnconsole or not os.path.isfile(dnconsole):
            messagebox.showerror("错误", "未找到 LDPlayer 命令行工具")
            return
        def _work():
            total = len(self._instances)
            success = [0]
            success_lock = threading.Lock()
            threads = []
            for inst in self._instances:
                t = threading.Thread(target=lambda n=inst['name']: _launch_one(n, dnconsole, success, success_lock), daemon=True)
                t.start()
                threads.append(t)
                try:
                    delay = int(self.launch_interval_var.get())
                except (ValueError, TypeError):
                    delay = 5
                time.sleep(delay)  # 在后台线程 sleep，不阻塞 UI
            for t in threads:
                t.join(timeout=600)
            self.root.after(0, lambda: self._toast(
                "启动完成", f"成功 {success[0]}/{total}", 4000))
            self.root.after(500, self._scan_and_display_instances)

        def _launch_one(name, dnconsole, success, lock):
            ok, msg = launch_instance(dnconsole, name)
            if ok:
                with lock:
                    success[0] += 1

        threading.Thread(target=_work, daemon=True).start()

    def _on_ld_shutdown_all(self):
        """关闭所有 LDPlayer 实例"""
        if not self._instances:
            return
        dnconsole = self._ld_paths.get("dnconsole")
        if not dnconsole or not os.path.isfile(dnconsole):
            messagebox.showerror("错误", "未找到 LDPlayer 命令行工具")
            return
        def _work():
            # 优先使用 quitall
            try:
                subprocess.run([dnconsole, 'quitall'], capture_output=True, timeout=30,
                               creationflags=subprocess.CREATE_NO_WINDOW)
            except Exception as _e:
                pass
            time.sleep(5)
            self.root.after(0, lambda: self._toast("关闭完成", "已发送关闭指令", 4000))
            self.root.after(500, self._scan_and_display_instances)
        threading.Thread(target=_work, daemon=True).start()

    # ---------- LDPlayer 单实例控制 ----------

    def _on_ld_launch_one(self, inst_name):
        """启动单个 LDPlayer 实例"""
        dnconsole = self._ld_paths.get("dnconsole")
        if not dnconsole or not os.path.isfile(dnconsole):
            messagebox.showerror("错误", "未找到 LDPlayer 命令行工具")
            return
        def _work():
            ok, msg = launch_instance(dnconsole, inst_name)
            self.root.after(0, lambda succ=ok, m=msg: self._toast(
                "启动结果" if succ else "启动失败", m, 4000))
            self.root.after(500, self._scan_and_display_instances)
        threading.Thread(target=_work, daemon=True).start()

    def _on_ld_stop_one(self, inst_name):
        """关闭单个 LDPlayer 实例"""
        dnconsole = self._ld_paths.get("dnconsole")
        if not dnconsole or not os.path.isfile(dnconsole):
            messagebox.showerror("错误", "未找到 LDPlayer 命令行工具")
            return
        # 从实例名提取索引 (leidian0 → 0)
        match = re.search(r'(\d+)$', inst_name)
        if not match:
            messagebox.showerror("错误", f"无法解析实例索引: {inst_name}")
            return
        idx = match.group(1)
        def _work():
            try:
                r = subprocess.run(
                    [dnconsole, 'quit', '--index', idx],
                    capture_output=True, text=True, timeout=15,
                    creationflags=subprocess.CREATE_NO_WINDOW
                )
                ok = r.returncode == 0
                msg = f"{inst_name} 已关闭" if ok else f"关闭失败: {r.stderr or r.stdout or ''}"
            except Exception as e:
                ok = False
                msg = f"关闭异常: {e}"
            self.root.after(0, lambda succ=ok, m=msg: self._toast(
                "关闭结果" if succ else "关闭失败", m, 4000))
            self.root.after(500, self._scan_and_display_instances)
        threading.Thread(target=_work, daemon=True).start()

    # ---------- 窗口关闭 ----------

    def _on_close(self):
        running = sum(1 for t in self.shutdown_tasks if t["running"])
        launch_running = sum(1 for t in self.launch_tasks if t["running"])
        _log_info(f"程序关闭（定时关闭运行中: {running}，定时启动运行中: {launch_running}）")
        if running > 0 or launch_running > 0:
            total = running + launch_running
            if not messagebox.askyesno("确认退出",
                                       f"有 {total} 个定时任务正在运行，确认退出？"):
                return
        # 保存当前配置和所有勾选状态
        self._save_tasks_config()

        self._destroyed = True
        for t in self.shutdown_tasks:
            t["running"] = False
        for t in self.launch_tasks:
            t["running"] = False
        if self.scan_timer_id:
            try: self.root.after_cancel(self.scan_timer_id)
            except Exception as _e:
                pass
            self.scan_timer_id = None
        if self._log_refresh_id:
            try: self.root.after_cancel(self._log_refresh_id)
            except Exception as _e:
                pass
            self._log_refresh_id = None
        _flush_log()
        self.root.destroy()

    def _minimize_to_tray(self):
        self.root.iconify()

    # ---------- 环境检测 ----------

    def _rebuild_env_ui(self, features):
        """重建环境状态行"""
        for w in self.env_frame.winfo_children():
            w.destroy()

        if not features:
            no_data = tk.Label(self.env_frame, text="点击「一键检测」开始检查",
                                font=("Microsoft YaHei", 9),
                                bg=CARD, fg=TEXT_LIGHT, anchor="w")
            no_data.pack(fill="x", pady=4)
            return

        # 表头
        hdr = tk.Frame(self.env_frame, bg=CARD)
        hdr.pack(fill="x")
        for txt, w in [("状态", 5), ("检测项", 24), ("当前状态", 20)]:
            tk.Label(hdr, text=txt, font=("Microsoft YaHei", 8, "bold"),
                     bg=CARD, fg=TEXT_SUB, width=w, anchor="w").pack(side="left")
        tk.Frame(self.env_frame, bg=BORDER, height=1).pack(fill="x")

        for ft in features:
            row = tk.Frame(self.env_frame, bg=CARD)
            row.pack(fill="x", pady=1)

            enabled = ft.get("enabled")
            name = ft.get("name", "")
            label = ft.get("label", name)
            raw = ft.get("raw", "")

            if enabled is True:
                dot_color = RED
                status_txt = "已启用"
            elif enabled is False:
                dot_color = GREEN
                status_txt = "已关闭"
            else:
                dot_color = TEXT_LIGHT
                status_txt = "未知"

            # 状态圆点
            dot = tk.Frame(row, bg=dot_color, width=8, height=8,
                           highlightthickness=0, bd=0)
            dot.pack(side="left", padx=(4, 6))
            dot.pack_propagate(False)

            # 检测项名称
            tk.Label(row, text=label, font=("Microsoft YaHei", 9),
                     bg=CARD, fg=TEXT, width=24, anchor="w").pack(side="left")

            # 状态值
            tk.Label(row, text=raw or status_txt, font=("Consolas", 9),
                     bg=CARD, fg=dot_color, width=20, anchor="w").pack(side="left")

    def _on_env_scan(self):
        """一键检测 — 在后台线程执行"""
        self.env_scan_btn.set_text("检测中...")
        self.env_scan_btn.config_bg(TEXT_LIGHT)
        self.env_status_var.set("正在检测系统环境...")
        self.env_fix_btn.pack_forget()

        def _work():
            try:
                report = get_emulator_environment_report()
                self.root.after(0, lambda r=report: self._update_env_display(r))
            except Exception as e:
                err_msg = f"检测失败: {str(e)}"
                self.root.after(0, lambda m=err_msg: self.env_status_var.set(m))
                self.root.after(0, lambda: self.env_scan_btn.set_text("一键检测"))
                self.root.after(0, lambda: self.env_scan_btn.config_bg(PRIMARY))

        threading.Thread(target=_work, daemon=True).start()

    def _update_env_display(self, report):
        """更新环境检测 UI"""
        self.env_scan_btn.set_text("一键检测")
        self.env_scan_btn.config_bg(PRIMARY)

        features = report.get("features", [])
        issues = report.get("issues", [])
        score = report.get("overall_score", 100)
        ld_info = report.get("ldplayer", {})
        vt_info = report.get("cpu_vt", {})

        # 更新状态行
        self._rebuild_env_ui(features)

        # 额外添加上LDPlayer版本和CPU虚拟化
        extra_frame = tk.Frame(self.env_frame, bg=CARD)
        extra_frame.pack(fill="x", pady=(2, 0))
        # LDPlayer 信息
        ld_label = ld_info.get("detail", "未检测到")
        tk.Label(extra_frame, text=ld_label, font=("Microsoft YaHei", 9),
                 bg=CARD, fg=TEXT_SUB, anchor="w").pack(side="left", padx=(20, 0))
        # VT-x 信息
        vt_enabled = vt_info.get("enabled")
        if vt_enabled is True:
            vt_txt = "VT-x/AMD-V 已开启"
            vt_color = GREEN
        elif vt_enabled is False:
            vt_txt = "VT-x/AMD-V 未开启! 请进BIOS开启"
            vt_color = RED
        else:
            vt_txt = "VT-x/AMD-V 状态未知"
            vt_color = TEXT_LIGHT
        tk.Label(extra_frame, text=vt_txt, font=("Microsoft YaHei", 9),
                 bg=CARD, fg=vt_color, anchor="w").pack(side="left", padx=(20, 0))

        # 评分
        if score >= 80:
            score_color = GREEN
            score_text = f"环境评分: {score}/100 OK"
        elif score >= 50:
            score_color = YELLOW
            score_text = f"环境评分: {score}/100 WARNING"
        else:
            score_color = RED
            score_text = f"环境评分: {score}/100 FAIL"
        self.env_score_var.set(score_text)
        self.env_score_lbl.config(fg=score_color)

        # 详情文本
        detail_lines = []
        if issues:
            for _, issue in enumerate(issues, 1):
                icon = {"critical": "[!]", "warning": "[~]", "info": "[i]"}.get(issue["severity"], "-")
                detail_lines.append(f"{icon} [{issue['severity'].upper()}] {issue['message']}")
        else:
            detail_lines.append("环境正常，无冲突问题")

        self.env_detail_text.config(state="normal")
        self.env_detail_text.delete("1.0", tk.END)
        self.env_detail_text.insert("1.0", "\n".join(detail_lines))
        self.env_detail_text.config(state="disabled")

        # 显示/隐藏修复按钮
        can_fix = report.get("can_auto_fix", False)
        if can_fix:
            self.env_fix_btn.pack(side="left", padx=(0, 6))
            self.env_status_var.set(f"发现 {len(issues)} 个问题，点击「一键修复」")
        else:
            self.env_fix_btn.pack_forget()
            if not issues:
                self.env_status_var.set("环境正常 ✓")
            else:
                self.env_status_var.set(f"{len(issues)} 个问题，需手动处理")

    def _on_env_fix(self):
        """一键修复 — 关闭所有冲突功能"""
        if not messagebox.askyesno("一键修复",
                                    "将自动关闭以下功能（需要管理员权限）：\n"
                                    "  • Hyper-V\n"
                                    "  • Windows Hypervisor Platform\n"
                                    "  • Virtual Machine Platform\n"
                                    "  • VBS（基于虚拟化的安全）— 通过 bcdedit 禁用\n\n"
                                    "⚠ 部分功能关闭后需要「完全关机」（不是重启）才能生效。\n"
                                    "继续吗？"):
            return

        self.env_fix_btn.set_text("修复中...")
        self.env_fix_btn.config_bg(TEXT_LIGHT)
        self.env_status_var.set("正在关闭冲突功能...")

        def _work():
            try:
                results = apply_all_fixes()
                success_count = sum(1 for _, ok, _ in results if ok)
                total = len(results)

                lines = [f"修复完成：成功 {success_count}/{total}"]
                for fn, ok, msg in results:
                    lines.append(f"  {'✓' if ok else '✗'} {fn}: {msg}")

                status_msg = f"修复完成 ({success_count}/{total})，请手动重启电脑"
                detail_text = "\n".join(lines)
                self.root.after(0, lambda m=status_msg: self.env_status_var.set(m))
                self.root.after(0, lambda: self.env_fix_btn.set_text("一键修复"))
                self.root.after(0, lambda: self.env_fix_btn.config_bg(RED))
                self.root.after(0, lambda: self.env_detail_text.config(state="normal"))
                self.root.after(0, lambda: self.env_detail_text.delete("1.0", tk.END))
                self.root.after(0, lambda t=detail_text: self.env_detail_text.insert("1.0", t))
                self.root.after(0, lambda: self.env_detail_text.config(state="disabled"))
                # 重新检测
                self.root.after(1000, self._on_env_scan)
            except Exception as e:
                err_msg = f"修复失败: {str(e)}"
                self.root.after(0, lambda m=err_msg: self.env_status_var.set(m))
                self.root.after(0, lambda: self.env_fix_btn.set_text("一键修复"))
                self.root.after(0, lambda: self.env_fix_btn.config_bg(RED))

        threading.Thread(target=_work, daemon=True).start()


# ============================================================
# 入口（保留原样）
# ============================================================

SINGLE_INSTANCE_MUTEX_NAME = "Local\\EmulatorShutdownTool_v3"
_single_instance_mutex = None


def ensure_single_instance():
    global _single_instance_mutex
    try:
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

        no_window = '--nowindow' in sys.argv

        if psutil is None and not getattr(sys, 'frozen', False):
            def _install_psutil():
                try:
                    subprocess.run(
                        [sys.executable, "-m", "pip", "install", "psutil", "-q"],
                        timeout=30, creationflags=subprocess.CREATE_NO_WINDOW
                    )
                    import psutil as psutil_module
                    globals()['psutil'] = psutil_module
                except Exception as _e:
                    pass
            threading.Thread(target=_install_psutil, daemon=True).start()

        root = tk.Tk()
        # 先设背景色再隐藏，避免白屏闪烁
        root.configure(bg=BG)
        root.withdraw()
        # 确保窗口已完全初始化
        root.update_idletasks()
        _app = EmulatorShutdownApp(root)
        # 确保所有 UI 渲染完成后再显示
        root.update_idletasks()
        if no_window:
            root.iconify()
        else:
            root.deiconify()
        root.mainloop()
    except Exception:
        try:
            _log_info("程序异常崩溃")
            tb = _traceback.format_exc()
            _LOG_BUFFER.append(f"\n{tb}\n")
            _flush_log()
        except Exception as _e:
            pass
        raise


if __name__ == "__main__":
    main()
