#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模拟器定时关闭工具 v3.0
- 小米设计美学，圆角卡片、橙色基调、干净留白
- 后台线程扫描模拟器，窗口拖动流畅不卡顿
- 支持多个定时任务（定点/倒计时），可自由添加/删除
- 开关机自启动设置
"""

import sys
import os
import time
import json
import threading
import subprocess
import winreg
import ctypes
from datetime import datetime, timedelta
import tkinter as tk
from tkinter import ttk, messagebox
from tkinter.font import Font

try:
    import psutil
except ImportError:
    psutil = None


# ============================================================
# 内嵌图标（base64 编码的橙色圆形 32x32 ICO）
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
# 配色方案 — 小米设计语言
# ============================================================
MI_ORANGE     = "#FF6900"
MI_ORANGE_LT  = "#FF8C38"
MI_ORANGE_DK  = "#E55D00"
MI_BG         = "#F5F5F5"
MI_CARD       = "#FFFFFF"
MI_TEXT       = "#1A1A1A"
MI_TEXT_SUB   = "#999999"
MI_TEXT_LIGHT = "#BFBFBF"
MI_BORDER     = "#E8E8E8"
MI_GREEN      = "#07C160"
MI_RED        = "#FA5151"
MI_YELLOW     = "#FFC300"


# ============================================================
# 进程检测 — 改为无阻塞设计，扫描在后台线程进行
# ============================================================

LD_PROCESS_KEYWORDS = [
    "dnplayer", "dnmultiplayerex", "dnmultiplayer",
    "dnconsole", "dnmemu", "雷电模拟器", "leidian",
    "ldnews", "ldbox", "ldplayer"
]

MUMU_PROCESS_KEYWORDS = [
    # MuMu 12 / 最新版
    "memevey", "mumuplayer", "mumuvmmgr", "mumugame",
    # MuMu 旧版
    "nemuplayer", "nemu", "nemuservice", "mumu",
    "nemuheadless", "nemumultiplayer",
    # MuMu 通用
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


def kill_emulators():
    """同步关闭所有模拟器进程（三层尝试：taskkill → taskkill /T → wmic delete）"""
    procs = _do_scan()
    if not procs:
        return 0, 0, []

    success = 0
    fail = 0
    for proc in procs:
        # 第一层：普通 taskkill /F
        try:
            r = subprocess.run(
                ['taskkill', '/PID', str(proc['pid']), '/F'],
                capture_output=True, timeout=5, creationflags=subprocess.CREATE_NO_WINDOW,
                check=True
            )
            success += 1
            continue
        except subprocess.CalledProcessError:
            pass
        except Exception:
            pass

        # 第二层：taskkill /F /T（杀进程树）
        try:
            r = subprocess.run(
                ['taskkill', '/PID', str(proc['pid']), '/F', '/T'],
                capture_output=True, timeout=5, creationflags=subprocess.CREATE_NO_WINDOW,
                check=True
            )
            success += 1
            continue
        except Exception:
            pass

        # 第三层：wmic delete（能杀掉部分 system 级进程）
        try:
            r = subprocess.run(
                ['wmic', 'process', 'where', f'ProcessId={proc["pid"]}', 'delete'],
                capture_output=True, timeout=5, creationflags=subprocess.CREATE_NO_WINDOW,
                check=True
            )
            success += 1
            continue
        except Exception:
            pass

        fail += 1

    return len(procs), success, procs


def _kill_single_process(pid, proc_name):
    """多方法尝试杀死单个进程，返回是否成功"""
    methods = [
        # 1-3: PID-based
        ['taskkill', '/F', '/PID', str(pid)],
        ['taskkill', '/F', '/T', '/PID', str(pid)],
        ['wmic', 'process', 'where', f'ProcessId={pid}', 'delete'],
        # 4: kill by image name (catch all instances)
        ['taskkill', '/F', '/IM', proc_name],
        ['taskkill', '/F', '/T', '/IM', proc_name],
        # 5: PowerShell (can handle some protected processes)
        ['powershell', '-Command', f'Stop-Process -Id {pid} -Force -ErrorAction SilentlyContinue'],
    ]
    for cmd in methods:
        try:
            subprocess.run(cmd, capture_output=True, timeout=8,
                           creationflags=subprocess.CREATE_NO_WINDOW, check=True)
            return True
        except Exception:
            continue
    return False


def kill_emulators_async(on_done):
    """后台异步关闭模拟器，完成后调用 on_done(count, success, fail, failed_names)"""
    def _work():
        try:
            procs = _do_scan()
            if not procs:
                on_done(0, 0, 0, [])
                return
            success = 0
            failed_names = []
            for proc in procs:
                name = (proc.get('name') or '?').lower()
                if _kill_single_process(proc['pid'], name):
                    success += 1
                else:
                    failed_names.append(f"{name}(PID:{proc['pid']})")
            on_done(len(procs), success, len(procs) - success, failed_names)
        except Exception:
            on_done(0, 0, 0, [])
    threading.Thread(target=_work, daemon=True).start()


# ============================================================
# 开机自启动管理
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
# 配置持久化
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
# GUI — 小米设计语言
# ============================================================

class RoundedButton(tk.Frame):
    """自绘按钮（Frame + Label，带字体缓存，启动更快）"""

    _font_cache = {}

    def __init__(self, parent, text, command=None, bg=MI_ORANGE, fg="white",
                 font=None, padx=16, pady=6, **kwargs):
        self._cmd = command
        self._bg = bg
        self._fg = fg
        self._text = text
        self._font = font or ("Microsoft YaHei", 10)

        # 字体度量缓存（避免反复创建 Font 对象）
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
        # 按实际文字重新计算宽度
        try:
            _f = Font(font=self._font)
            tw = _f.measure(text)
            _f.destroy()
        except Exception:
            pass

        w = tw + padx * 2 + 8
        h = th + pady * 2 + 4

        super().__init__(parent, width=w, height=h, bg=bg,
                         highlightthickness=0, bd=0, cursor="hand2", **kwargs)
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

    def _on_click(self, event):
        if self._cmd:
            self._cmd()

    def _on_enter(self, event):
        self.configure(bg=MI_ORANGE_LT)
        self._label.configure(bg=MI_ORANGE_LT)

    def _on_leave(self, event):
        self.configure(bg=self._bg)
        self._label.configure(bg=self._bg)

    def set_text(self, text):
        self._text = text
        self._label.configure(text=text)

    def config_bg(self, color):
        self._bg = color
        self.configure(bg=color)
        self._label.configure(bg=color)


class EmulatorShutdownApp:
    def __init__(self, root):
        self.root = root
        self.root.title("模拟器定时关闭工具")
        # 设置窗口图标
        try:
            ico = _get_icon_path()
            if ico:
                self.root.iconbitmap(ico)
        except Exception:
            pass
        self.root.geometry("640x680")
        self.root.minsize(600, 620)
        self.root.configure(bg=MI_BG)

        # 窗口居中
        self.root.update_idletasks()
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x = (sw - 640) // 2
        y = (sh - 680) // 2
        self.root.geometry(f"640x680+{x}+{y}")

        # 任务数据
        self.tasks: list = []
        self.next_task_id = 1
        self.scan_timer_id = None
        self._emu_procs_cache = []       # 缓存的最新模拟器列表
        self._emu_scan_pending = False   # 防止并发扫描

        self.auto_start_var = tk.BooleanVar(value=is_auto_start_enabled())

        self._destroyed = False
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._build_ui()
        # 延迟加载任务和启动扫描，让窗口先显示出来
        self.root.after_idle(self._lazy_init)

    def _lazy_init(self):
        """UI 显示后的延迟初始化"""
        self._load_tasks()
        self._start_scan_loop()

    # ---------- UI 构建 ----------

    def _build_ui(self):
        root = self.root

        # ===== 字体 =====
        f_title  = Font(family="Microsoft YaHei", size=16, weight="bold")
        f_sec    = Font(family="Microsoft YaHei", size=11, weight="bold")
        f_body   = Font(family="Microsoft YaHei", size=10)
        f_small  = Font(family="Microsoft YaHei", size=9)

        # ===== 顶栏 — 小米橙色 =====
        header = tk.Frame(root, bg=MI_ORANGE, height=56)
        header.pack(fill="x")
        header.pack_propagate(False)

        # 顶栏内容行
        h_row = tk.Frame(header, bg=MI_ORANGE)
        h_row.pack(expand=True, fill="x", padx=20)

        tk.Label(h_row, text="模拟器定时关闭", font=f_title,
                 bg=MI_ORANGE, fg="white").pack(side="left")

        tk.Label(h_row, text="v3.0", font=("Microsoft YaHei", 9),
                 bg=MI_ORANGE, fg="white", padx=8).pack(side="left")

        # ===== 主内容区 =====
        main = tk.Frame(root, bg=MI_BG, padx=20, pady=16)
        main.pack(fill="both", expand=True)

        # ---------- 卡片工厂 ----------
        def make_card(parent, padding=16):
            card = tk.Frame(parent, bg=MI_CARD, bd=1, relief="solid",
                            highlightbackground=MI_BORDER, highlightthickness=0)
            inner = tk.Frame(card, bg=MI_CARD, padx=padding, pady=padding)
            inner.pack(fill="both", expand=True)
            card.pack(fill="x", pady=(0, 12))
            return card, inner

        # ---------- 卡片：定时任务 ----------
        card1, c1_inner = make_card(main)

        # 卡片标题行
        c1_title = tk.Frame(c1_inner, bg=MI_CARD)
        c1_title.pack(fill="x", pady=(0, 8))

        tk.Label(c1_title, text="定时任务", font=f_sec,
                 bg=MI_CARD, fg=MI_TEXT).pack(side="left")
        tk.Label(c1_title, text="可添加多个", font=f_small,
                 bg=MI_CARD, fg=MI_TEXT_SUB).pack(side="left", padx=8)

        # 任务列表 — 滚动区域
        list_container = tk.Frame(c1_inner, bg=MI_CARD)
        list_container.pack(fill="both", expand=True)

        canvas = tk.Canvas(list_container, bg=MI_CARD, highlightthickness=0, height=200)
        scrollbar = ttk.Scrollbar(list_container, orient="vertical", command=canvas.yview)
        self.tasks_frame = tk.Frame(canvas, bg=MI_CARD)

        self.tasks_frame.bind("<Configure>",
                              lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=self.tasks_frame, anchor="nw", tags="inner")
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self._tasks_canvas = canvas

        # 按钮行
        btn_row = tk.Frame(c1_inner, bg=MI_CARD)
        btn_row.pack(fill="x", pady=(10, 0))

        self.add_btn = RoundedButton(btn_row, text="+ 添加任务",
                                      command=self._add_task,
                                      bg=MI_ORANGE, fg="white",
                                      font=("Microsoft YaHei", 10))
        self.add_btn.pack(side="left", padx=(0, 8))

        self.start_all_btn = RoundedButton(btn_row, text="全部启动",
                                            command=self._start_all,
                                            bg=MI_GREEN, fg="white",
                                            font=("Microsoft YaHei", 10),
                                            padx=14)
        self.start_all_btn.pack(side="left", padx=0)

        self.stop_all_btn = RoundedButton(btn_row, text="全部停止",
                                           command=self._stop_all,
                                           bg=MI_RED, fg="white",
                                           font=("Microsoft YaHei", 10),
                                           padx=14)
        self.stop_all_btn.pack(side="left", padx=(6, 0))

        # ---------- 卡片：模拟器状态 ----------
        card2, c2_inner = make_card(main)

        c2_title = tk.Frame(c2_inner, bg=MI_CARD)
        c2_title.pack(fill="x", pady=(0, 8))

        tk.Label(c2_title, text="模拟器状态", font=f_sec,
                 bg=MI_CARD, fg=MI_TEXT).pack(side="left")

        self.emu_count_label = tk.Label(c2_title, text="",
                                        font=f_small, bg=MI_CARD, fg=MI_TEXT_SUB)
        self.emu_count_label.pack(side="left", padx=8)

        # 模拟器列表
        self.emu_listbox = tk.Listbox(
            c2_inner, height=3, font=("Consolas", 10),
            bg="#FAFAFA", fg=MI_TEXT,
            selectbackground=MI_ORANGE_LT, selectforeground="white",
            relief="flat", bd=0, highlightthickness=0
        )
        self.emu_listbox.pack(fill="x")
        self.emu_listbox.insert(tk.END, "  正在扫描...")
        self.emu_listbox.bind("<Double-Button-1>", self._on_kill_selected)

        # 模拟器操作行
        emu_btn_row = tk.Frame(c2_inner, bg=MI_CARD)
        emu_btn_row.pack(fill="x", pady=(8, 0))

        self.kill_selected_btn = RoundedButton(emu_btn_row, text="关闭选中",
                                                command=self._on_kill_selected,
                                                bg=MI_RED, fg="white",
                                                font=("Microsoft YaHei", 9))
        self.kill_selected_btn.pack(side="left", padx=(0, 6))

        self.refresh_btn = RoundedButton(emu_btn_row, text="⟳ 刷新",
                                          command=self._refresh_emu,
                                          bg=MI_ORANGE, fg="white",
                                          font=("Microsoft YaHei", 9),
                                          padx=10)
        self.refresh_btn.pack(side="left")

        # ---------- 底部操作栏 ----------
        bottom = tk.Frame(main, bg=MI_BG)
        bottom.pack(fill="x", pady=(4, 0))

        self.kill_btn = RoundedButton(bottom, text="⚡ 立即关闭所有模拟器",
                                       command=self._on_kill_now,
                                       bg=MI_RED, fg="white",
                                       font=("Microsoft YaHei", 11, "bold"),
                                       padx=22, pady=8)
        self.kill_btn.pack(side="left", padx=(0, 16))

        # 开机自启动
        self.auto_cb = tk.Checkbutton(
            bottom, text="开机自启",
            variable=self.auto_start_var,
            font=("Microsoft YaHei", 10),
            bg=MI_BG, fg=MI_TEXT,
            selectcolor=MI_CARD, activebackground=MI_BG,
            command=self._on_auto_start_toggle
        )
        self.auto_cb.pack(side="left")

        # 最小化到系统托盘
        self.tray_btn = RoundedButton(bottom, text="— 最小化",
                                       command=self._minimize_to_tray,
                                       bg=MI_TEXT_LIGHT, fg="white",
                                       font=("Microsoft YaHei", 9),
                                       padx=8, pady=3)
        self.tray_btn.pack(side="right")

        # ---------- 底栏 ----------
        footer = tk.Frame(root, bg=MI_BG, height=28)
        footer.pack(fill="x")
        tk.Label(footer,
                 text="支持 雷电模拟器 / MuMu模拟器  ·  任务自动保存",
                 font=("Microsoft YaHei", 8), bg=MI_BG, fg=MI_TEXT_LIGHT).pack(expand=True)

    # ---------- 任务组件 ----------

    def _make_task_row(self, parent, task_id, data):
        """构建单个任务行"""
        mode = data.get("mode", "fixed")
        hour = data.get("hour", 22)
        minute = data.get("minute", 0)
        cd_min = data.get("countdown_min", 30)
        enabled = data.get("enabled", True)

        # 运行时状态
        task = {
            "id": task_id,
            "running": False,
            "thread": None,
            "remaining": 0,
            "target_ts": 0,
            "enabled": enabled,
            "mode": mode,
            "hour": hour,
            "minute": minute,
            "cd_min": cd_min,
            "update_id": None,
            "auto_reset_id": None,
            "_pending_update": False,
        }

        frame = tk.Frame(parent, bg=MI_CARD, bd=0)
        frame.pack(fill="x", pady=(0, 0))

        # 行间分隔线
        sep = tk.Frame(frame, bg=MI_BORDER, height=1)
        sep.pack(fill="x", side="bottom")

        # 行内容
        row = tk.Frame(frame, bg=MI_CARD)
        row.pack(fill="x", pady=5)

        # 序号
        idx_lbl = tk.Label(row, text=f"#{task_id}", font=("Consolas", 9, "bold"),
                           width=3, anchor="w", bg=MI_CARD, fg=MI_TEXT_SUB)
        idx_lbl.pack(side="left")

        def _set_idx(n):
            idx_lbl.config(text=f"#{n}")
        task["set_idx"] = _set_idx

        # 启用开关
        en_var = tk.BooleanVar(value=enabled)
        en_cb = tk.Checkbutton(row, variable=en_var, bg=MI_CARD,
                                activebackground=MI_CARD,
                                selectcolor=MI_CARD)

        def _on_en_toggle():
            task["enabled"] = en_var.get()
            if not task["enabled"] and task["running"]:
                _stop()
            _update_status()
            self._save_tasks()

        en_cb.config(command=_on_en_toggle)
        en_cb.pack(side="left", padx=(0, 4))
        task["en_var"] = en_var

        # 模式选择
        mode_var = tk.StringVar(value="定点" if mode == "fixed" else "倒计时")
        mode_combo = ttk.Combobox(row, textvariable=mode_var,
                                   values=["定点", "倒计时"], width=5,
                                   state="readonly", font=("Microsoft YaHei", 9))
        mode_combo.pack(side="left", padx=(0, 6))

        # 时间控件
        tf = tk.Frame(row, bg=MI_CARD)
        tf.pack(side="left")

        # 定点
        ff = tk.Frame(tf, bg=MI_CARD)
        h_spin = ttk.Spinbox(ff, from_=0, to=23, width=3,
                              font=("Consolas", 9), format="%02.0f")
        h_spin.pack(side="left")
        h_spin.set(f"{hour:02d}")
        tk.Label(ff, text=":", font=("Consolas", 9), bg=MI_CARD,
                 fg=MI_TEXT).pack(side="left")
        m_spin = ttk.Spinbox(ff, from_=0, to=59, width=3,
                              font=("Consolas", 9), format="%02.0f")
        m_spin.pack(side="left")
        m_spin.set(f"{minute:02d}")

        # 倒计时
        cf = tk.Frame(tf, bg=MI_CARD)
        cd_spin = ttk.Spinbox(cf, from_=1, to=999, width=4, font=("Consolas", 9))
        cd_spin.pack(side="left")
        cd_spin.set(str(cd_min))
        tk.Label(cf, text="分钟", font=("Microsoft YaHei", 9),
                 bg=MI_CARD, fg=MI_TEXT).pack(side="left", padx=2)

        def _switch_mode():
            m = mode_var.get()
            task["mode"] = "countdown" if m == "倒计时" else "fixed"
            if task["mode"] == "fixed":
                cf.pack_forget()
                ff.pack(side="left")
            else:
                ff.pack_forget()
                cf.pack(side="left")
            self._save_tasks()

        mode_combo.bind("<<ComboboxSelected>>", lambda e: _switch_mode())

        if mode == "fixed":
            ff.pack(side="left")
            cf.pack_forget()
        else:
            ff.pack_forget()
            cf.pack(side="left")

        # 状态标签
        st_lbl = tk.Label(row, text="● 待启动", font=("Microsoft YaHei", 8),
                          fg=MI_TEXT_LIGHT, bg=MI_CARD, width=18, anchor="w")
        st_lbl.pack(side="left", padx=(10, 0))

        # 操作按钮
        act_btn = RoundedButton(row, text="▶", command=lambda: _toggle(),
                                 bg=MI_ORANGE, fg="white",
                                 font=("Consolas", 9, "bold"),
                                 padx=8, pady=1)
        act_btn.pack(side="right", padx=(0, 2))

        del_btn = RoundedButton(row, text="✕", command=lambda: _delete(),
                                 bg=MI_RED, fg="white",
                                 font=("Consolas", 9, "bold"),
                                 padx=6, pady=1)
        del_btn.pack(side="right", padx=(0, 2))

        # ---------- 操作函数 ----------
        def _toggle():
            if task["running"]:
                _stop()
            else:
                _start()

        def _calc_ts():
            try:
                if task["mode"] == "fixed":
                    h = int(h_spin.get())
                    m = int(m_spin.get())
                    now = datetime.now()
                    t = now.replace(hour=h, minute=m, second=0, microsecond=0)
                    if t <= now:
                        t += timedelta(days=1)
                    return t.timestamp()
                else:
                    mins = int(cd_spin.get())
                    return (datetime.now() + timedelta(minutes=mins)).timestamp()
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
            act_btn.config_bg(MI_RED)
            act_btn.set_text("■")
            _update_status()
            self._save_tasks()

        def _stop():
            task["running"] = False
            if task["update_id"]:
                try:
                    self.root.after_cancel(task["update_id"])
                except Exception:
                    pass
                task["update_id"] = None
            if task["auto_reset_id"]:
                try:
                    self.root.after_cancel(task["auto_reset_id"])
                except Exception:
                    pass
                task["auto_reset_id"] = None
            task["thread"] = None
            act_btn.config_bg(MI_ORANGE)
            act_btn.set_text("▶")
            st_lbl.config(text="● 已停止", fg=MI_TEXT_LIGHT)
            self._save_tasks()

        def _loop():
            while task["running"]:
                now_ts = time.time()
                rem = int(task["target_ts"] - now_ts)
                if rem <= 0:
                    self.root.after(0, _time_up)
                    break
                task["remaining"] = rem
                if not task["_pending_update"]:
                    task["_pending_update"] = True
                    self.root.after(0, _update_status)
                time.sleep(0.5)

        def _time_up():
            if not task["running"]:
                return
            task["running"] = False
            act_btn.config_bg(MI_ORANGE)
            act_btn.set_text("▶")
            st_lbl.config(text="关闭中…", fg=MI_YELLOW)

            def _on_kill_done(count, success, fail_count, failed_names):
                if count == 0:
                    msg = f"任务 #{task_id}：未检测到模拟器进程。"
                elif fail_count == 0:
                    msg = f"任务 #{task_id}：已关闭 {success}/{count} 个模拟器"
                else:
                    t_labels = {'ld': '雷电', 'mumu': 'MuMu'}
                    details = "\n".join(f"  {f}" for f in failed_names)
                    msg = f"任务 #{task_id}：已关闭 {success}/{count} 个\n无法关闭：\n{details}"
                messagebox.showinfo("模拟器关闭", msg)
                if task["mode"] == "fixed":
                    task["auto_reset_id"] = self.root.after(2000, _auto_reset)
                self._save_tasks()

            kill_emulators_async(_on_kill_done)

        def _auto_reset():
            task["auto_reset_id"] = None
            if not task["enabled"]:
                return
            h = int(h_spin.get())
            m = int(m_spin.get())
            now = datetime.now()
            t = now.replace(hour=h, minute=m, second=0, microsecond=0) + timedelta(days=1)
            task["target_ts"] = t.timestamp()
            task["running"] = True
            task["thread"] = threading.Thread(target=_loop, daemon=True)
            task["thread"].start()
            act_btn.config_bg(MI_RED)
            act_btn.set_text("■")
            st_lbl.config(text=f"明天 {h:02d}:{m:02d}", fg=MI_ORANGE)
            self._save_tasks()

        def _update_status():
            task["_pending_update"] = False
            if not task["enabled"]:
                st_lbl.config(text="● 已禁用", fg=MI_TEXT_LIGHT)
                return
            if task["running"]:
                rem = task["remaining"]
                hrs = rem // 3600
                mins = (rem % 3600) // 60
                secs = rem % 60
                if task["mode"] == "fixed":
                    ts = datetime.fromtimestamp(task["target_ts"]).strftime("%H:%M")
                    st_lbl.config(text=f"{ts} ⏳{hrs:02d}:{mins:02d}:{secs:02d}",
                                  fg=MI_YELLOW)
                else:
                    st_lbl.config(text=f"⏳{hrs:02d}:{mins:02d}:{secs:02d}",
                                  fg=MI_YELLOW)
            else:
                st_lbl.config(text="● 待启动", fg=MI_TEXT_LIGHT)

        def _delete():
            if task["running"]:
                _stop()
            frame.destroy()
            for i, t in enumerate(self.tasks):
                if t["id"] == task_id:
                    self.tasks.pop(i)
                    break
            self._renumber()
            self._resize_canvas()
            self._save_tasks()

        task["vars"] = {
            "h_spin": h_spin, "m_spin": m_spin, "cd_spin": cd_spin,
            "st_lbl": st_lbl, "act_btn": act_btn,
        }
        return task

    def _renumber(self):
        for i, t in enumerate(self.tasks):
            if "set_idx" in t:
                t["set_idx"](i + 1)

    def _resize_canvas(self):
        cnt = len(self.tasks)
        self._tasks_canvas.config(height=min(46 * max(cnt, 1), 240))

    # ---------- 任务管理 ----------

    def _add_task(self, data=None):
        if data is None:
            data = {}
        widget = self._make_task_row(self.tasks_frame, self.next_task_id, data)
        self.tasks.append(widget)
        self.next_task_id += 1
        self._renumber()
        self._resize_canvas()
        self._save_tasks()

    def _start_all(self):
        for t in self.tasks:
            if t["enabled"] and not t["running"]:
                self._inline_start(t)

    def _inline_start(self, t):
        if t["running"] or not t["enabled"]:
            return
        try:
            if t["mode"] == "fixed":
                h = int(t["vars"]["h_spin"].get())
                m = int(t["vars"]["m_spin"].get())
                now = datetime.now()
                target = now.replace(hour=h, minute=m, second=0, microsecond=0)
                if target <= now:
                    target += timedelta(days=1)
                ts = target.timestamp()
            else:
                mins = int(t["vars"]["cd_spin"].get())
                ts = (datetime.now() + timedelta(minutes=mins)).timestamp()
        except ValueError:
            return

        t["running"] = True
        t["target_ts"] = ts
        t["thread"] = threading.Thread(target=self._make_loop_fn(t), daemon=True)
        t["thread"].start()
        t["vars"]["act_btn"].config_bg(MI_RED)
        t["vars"]["act_btn"].set_text("■")
        self._update_task_status(t)
        self._save_tasks()

    def _make_loop_fn(self, t):
        def _loop():
            while t["running"]:
                now_ts = time.time()
                rem = int(t["target_ts"] - now_ts)
                if rem <= 0:
                    self.root.after(0, lambda: self._task_time_up(t))
                    break
                t["remaining"] = rem
                if not t.get("_pending_update"):
                    t["_pending_update"] = True
                    self.root.after(0, lambda: self._update_task_status(t))
                time.sleep(0.5)
        return _loop

    def _task_time_up(self, t):
        if not t["running"]:
            return
        task_id = t["id"]
        t["running"] = False
        t["vars"]["act_btn"].config_bg(MI_ORANGE)
        t["vars"]["act_btn"].set_text("▶")
        t["vars"]["st_lbl"].config(text="关闭中…", fg=MI_YELLOW)

        def _on_kill_done(count, success, fail_count, failed_names):
            if count == 0:
                msg = f"任务 #{task_id}：未检测到模拟器进程。"
            elif fail_count == 0:
                msg = f"任务 #{task_id}：已关闭 {success}/{count} 个模拟器"
            else:
                details = "\n".join(f"  {f}" for f in failed_names)
                msg = f"任务 #{task_id}：已关闭 {success}/{count} 个\n无法关闭：\n{details}"
            messagebox.showinfo("模拟器关闭", msg)
            if t["mode"] == "fixed":
                t["auto_reset_id"] = self.root.after(2000, lambda: self._auto_reset_task(t))
            self._save_tasks()

        kill_emulators_async(_on_kill_done)

    def _auto_reset_task(self, t):
        t["auto_reset_id"] = None
        if not t["enabled"]:
            return
        h = int(t["vars"]["h_spin"].get())
        m = int(t["vars"]["m_spin"].get())
        now = datetime.now()
        target = now.replace(hour=h, minute=m, second=0, microsecond=0) + timedelta(days=1)
        t["target_ts"] = target.timestamp()
        t["running"] = True
        t["thread"] = threading.Thread(target=self._make_loop_fn(t), daemon=True)
        t["thread"].start()
        t["vars"]["act_btn"].config_bg(MI_RED)
        t["vars"]["act_btn"].set_text("■")
        t["vars"]["st_lbl"].config(text=f"明天 {h:02d}:{m:02d}", fg=MI_ORANGE)
        self._save_tasks()

    def _update_task_status(self, t):
        t["_pending_update"] = False
        if not t["enabled"]:
            t["vars"]["st_lbl"].config(text="● 已禁用", fg=MI_TEXT_LIGHT)
            return
        if t["running"]:
            rem = t["remaining"]
            hrs = rem // 3600
            mins = (rem % 3600) // 60
            secs = rem % 60
            if t["mode"] == "fixed":
                ts = datetime.fromtimestamp(t["target_ts"]).strftime("%H:%M")
                t["vars"]["st_lbl"].config(text=f"{ts} ⏳{hrs:02d}:{mins:02d}:{secs:02d}",
                                          fg=MI_YELLOW)
            else:
                t["vars"]["st_lbl"].config(text=f"⏳{hrs:02d}:{mins:02d}:{secs:02d}",
                                          fg=MI_YELLOW)
        else:
            t["vars"]["st_lbl"].config(text="● 待启动", fg=MI_TEXT_LIGHT)

    def _stop_all(self):
        for t in self.tasks:
            if t.get("auto_reset_id"):
                try:
                    self.root.after_cancel(t["auto_reset_id"])
                except Exception:
                    pass
                t["auto_reset_id"] = None
            if t["running"]:
                t["running"] = False
                if t.get("update_id"):
                    try:
                        self.root.after_cancel(t["update_id"])
                    except Exception:
                        pass
                t["vars"]["act_btn"].config_bg(MI_ORANGE)
                t["vars"]["act_btn"].set_text("▶")
                t["vars"]["st_lbl"].config(text="● 已停止", fg=MI_TEXT_LIGHT)
        self._save_tasks()

    def _on_kill_now(self):
        if not self._emu_procs_cache:
            messagebox.showinfo("提示", "未检测到运行的模拟器进程。")
            return
        t_labels = {'ld': '雷电', 'mumu': 'MuMu'}
        details = "\n".join([
            f"  [{t_labels.get(p['type'], '?')}] PID:{p['pid']}  {p['name']}"
            for p in self._emu_procs_cache
        ])
        if not messagebox.askyesno("⚠ 确认关闭所有模拟器",
                                    f"即将关闭以下 {len(self._emu_procs_cache)} 个模拟器：\n\n{details}\n\n确定继续？",
                                    icon="warning"):
            return
        self.kill_btn.set_text("关闭中…")
        self.kill_btn.config_bg(MI_YELLOW)

        def _on_done(count, success, fail_count, failed_names):
            self.kill_btn.config_bg(MI_RED)
            self.kill_btn.set_text("⚡ 立即关闭所有模拟器")
            msg = f"已关闭 {success}/{count} 个模拟器"
            if failed_names:
                msg += "\n\n无法关闭：\n" + "\n".join(f"  {f}" for f in failed_names)
            messagebox.showinfo("关闭完成", msg)

        kill_emulators_async(_on_done)

    def _kill_single_emulator(self, proc):
        """关闭单个模拟器进程（后台异步）"""
        def _work():
            ok = _kill_single_process(proc['pid'], proc.get('name', '').lower())
            self.root.after(0, lambda: self._on_single_kill_result(ok, proc))
        threading.Thread(target=_work, daemon=True).start()

    def _on_single_kill_result(self, ok, proc):
        if self._destroyed:
            return
        t_labels = {'ld': '雷电', 'mumu': 'MuMu'}
        emu_type = t_labels.get(proc['type'], '?')
        if ok:
            messagebox.showinfo("关闭完成", f"已关闭 [{emu_type}] PID:{proc['pid']}")
        else:
            messagebox.showerror("关闭失败", f"无法关闭 [{emu_type}] PID:{proc['pid']}\n请尝试以管理员身份运行本程序")

    def _on_kill_selected(self, event=None):
        """关闭列表框中选中的单个模拟器"""
        selection = self.emu_listbox.curselection()
        if not selection:
            messagebox.showinfo("提示", "请先在列表中选中一个模拟器。")
            return
        idx = selection[0]
        if idx < 0 or idx >= len(self._emu_procs_cache):
            return
        proc = self._emu_procs_cache[idx]
        t_labels = {'ld': '雷电', 'mumu': 'MuMu'}
        emu_type = t_labels.get(proc['type'], '?')
        if not messagebox.askyesno("⚠ 确认关闭",
                                    f"确定关闭以下模拟器？\n\n  [{emu_type}] PID:{proc['pid']}  {proc['name']}",
                                    icon="warning"):
            return
        self._kill_single_emulator(proc)

    def _refresh_emu(self):
        """手动刷新模拟器列表"""
        self._emu_scan_pending = False
        self._trigger_scan()

    def _on_auto_start_toggle(self):
        enable = self.auto_start_var.get()
        ok, err = set_auto_start(enable)
        if not ok:
            messagebox.showerror("设置失败", f"开机自启动设置失败：\n{err}")
            self.auto_start_var.set(not enable)

    # ---------- 模拟器扫描（后台线程） ----------

    def _start_scan_loop(self):
        """每 3 秒发起一次后台扫描"""
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
        try:
            self.root.after(0, self._update_emu_display)
        except Exception:
            pass

    def _update_emu_display(self):
        if self._destroyed:
            return
        self.emu_listbox.delete(0, tk.END)
        if not self._emu_procs_cache:
            self.emu_listbox.insert(tk.END, "  未检测到运行的模拟器")
            self.emu_count_label.config(text="(0)")
            return

        t_labels = {'ld': '雷电', 'mumu': 'MuMu'}
        self.emu_count_label.config(text=f"({len(self._emu_procs_cache)})")
        for proc in self._emu_procs_cache:
            emu_type = t_labels.get(proc['type'], '?')
            entry = f"  [{emu_type}]  PID:{proc['pid']}  {proc['name']}"
            self.emu_listbox.insert(tk.END, entry)

    # ---------- 配置持久化 ----------

    def _load_tasks(self):
        tasks_data = load_tasks_config()
        if tasks_data:
            for td in tasks_data:
                self._add_task(td)
        elif not os.path.exists(CONFIG_FILE):
            # 首次使用，创建一个默认任务
            self._add_task()
        # 开机后自动启动已启用的定点任务
        for t in self.tasks:
            if t["enabled"] and t["mode"] == "fixed":
                self._inline_start(t)

    def _save_tasks(self):
        data = []
        for t in self.tasks:
            try:
                data.append({
                    "mode": t["mode"],
                    "hour": int(t["vars"]["h_spin"].get()),
                    "minute": int(t["vars"]["m_spin"].get()),
                    "countdown_min": int(t["vars"]["cd_spin"].get()),
                    "enabled": t["en_var"].get(),
                })
            except (KeyError, ValueError):
                pass
        save_tasks_config(data)

    # ---------- 窗口关闭 ----------

    def _on_close(self):
        """点击 X 时真正退出"""
        running = sum(1 for t in self.tasks if t["running"])
        if running > 0:
            if not messagebox.askyesno("确认退出",
                                       f"有 {running} 个定时任务正在运行，确认退出？"):
                return
        self._destroyed = True
        for t in self.tasks:
            t["running"] = False
        if self.scan_timer_id:
            try:
                self.root.after_cancel(self.scan_timer_id)
            except Exception:
                pass
            self.scan_timer_id = None
        self.root.destroy()

    def _minimize_to_tray(self):
        """最小化到任务栏（后台继续运行）"""
        self.root.iconify()

# ============================================================
# 入口
# ============================================================

SINGLE_INSTANCE_MUTEX_NAME = "Local\\EmulatorShutdownTool_v3"
_single_instance_mutex = None


def ensure_single_instance():
    """确保只运行一个实例，防止无限打开"""
    global _single_instance_mutex
    try:
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        _single_instance_mutex = kernel32.CreateMutexW(None, False, SINGLE_INSTANCE_MUTEX_NAME)
        if not _single_instance_mutex:
            return True  # 创建失败仍允许运行（非致命）
        err = ctypes.get_last_error()
        if err == 183:  # ERROR_ALREADY_EXISTS
            return False
        return True
    except Exception:
        return True  # 如果互斥锁创建失败，仍允许运行


def main():
    try:
        if not ensure_single_instance():
            messagebox.showwarning(
                "警告",
                "程序已经在运行中！\n请勿重复打开。"
            )
            sys.exit(0)

        # frozen exe 下 sys.executable 是 exe 本身，无法 pip install
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
        root.withdraw()  # 隐藏窗口直到 UI 构建完毕，避免白屏
        root.configure(bg=MI_BG)
        app = EmulatorShutdownApp(root)
        root.deiconify()  # UI 就绪，显示窗口
        root.mainloop()
    except Exception:
        import traceback
        try:
            _dir = _config_dir()
            with open(os.path.join(_dir, 'crash.log'), 'w', encoding='utf-8') as f:
                f.write(f"模拟器定时关闭工具 v3.0 崩溃日志\n")
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
