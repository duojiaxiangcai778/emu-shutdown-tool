#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
雷电模拟器实例管理模块
- 路径自动探测
- 实例扫描与设置读写
- 配置快照保存/恢复
- 间隔启动
"""

import ctypes
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import winreg
from datetime import datetime

# ============================================================
# 路径自动探测
# ============================================================

# 自动探测结果缓存（模块级），避免重复全盘扫描
_AUTO_DETECT_CACHE = None
_AUTO_DETECT_CACHE_TIME = 0.0
_AUTO_DETECT_CACHE_TTL = 300

def auto_detect_paths(force=False):
    """
    自动探测 LDPlayer 安装路径和多开器路径
    返回: {
        "ld_path": "D:\\E\\LDPlayer9",
        "multiplayer_path": "D:\\E\\ldmutiplayer",
        "vms_config_dir": "D:\\E\\LDPlayer9\\vms\\config",
        "dnconsole": "D:\\E\\LDPlayer9\\dnconsole.exe",
        "dnmultiplayerex": "D:\\E\\ldmutiplayer\\dnmultiplayerex.exe",
    }
    """
    global _AUTO_DETECT_CACHE, _AUTO_DETECT_CACHE_TIME
    now = time.time()
    if not force and _AUTO_DETECT_CACHE is not None and (now - _AUTO_DETECT_CACHE_TIME) < _AUTO_DETECT_CACHE_TTL:
        return dict(_AUTO_DETECT_CACHE)

    result = {
        "ld_path": None,
        "ld_path": None,
        "multiplayer_path": None,
        "vms_config_dir": None,
        "dnconsole": None,
        "dnmultiplayerex": None,
    }

    # 策略1: 从运行中的进程获取路径
    ld_from_proc = _find_ld_from_process()
    mp_from_proc = _find_multiplayer_from_process()

    if ld_from_proc:
        result["ld_path"] = ld_from_proc
        result["dnconsole"] = os.path.join(ld_from_proc, "dnconsole.exe")

    if mp_from_proc:
        result["multiplayer_path"] = mp_from_proc
        result["dnmultiplayerex"] = os.path.join(mp_from_proc, "dnmultiplayerex.exe")

    # 策略2: 从 pathconfig.ini 读取 LDPlayer 路径（多开器配置文件，全盘搜索）
    if not result["ld_path"]:
        ld_from_pc = _find_ld_from_pathconfig()
        if ld_from_pc:
            result["ld_path"] = ld_from_pc
            result["dnconsole"] = os.path.join(ld_from_pc, "dnconsole.exe")

    # 策略3: 检查 pathconfig.ini（多开器配置）
    if not result["multiplayer_path"]:
        mp = _find_multiplayer_from_pathconfig()
        if mp:
            result["multiplayer_path"] = mp
            result["dnmultiplayerex"] = os.path.join(mp, "dnmultiplayerex.exe")

    # 策略3: 检查注册表
    if not result["ld_path"]:
        reg_path = _find_ld_from_registry()
        if reg_path:
            result["ld_path"] = reg_path
            result["dnconsole"] = os.path.join(reg_path, "dnconsole.exe")

    # 策略4: 扫描常见路径
    if not result["ld_path"]:
        found = _scan_common_paths()
        if found:
            result["ld_path"] = found
            result["dnconsole"] = os.path.join(found, "dnconsole.exe")

    # 补充: 多开器路径如果没找到，尝试在 LDPlayer 同级目录搜索
    if not result["multiplayer_path"] and result["ld_path"]:
        parent = os.path.dirname(result["ld_path"])
        mp_siblings = _find_multiplayer_nearby(parent)
        if mp_siblings:
            result["multiplayer_path"] = mp_siblings
            result["dnmultiplayerex"] = os.path.join(mp_siblings, "dnmultiplayerex.exe")

    # 找 vms config 目录
    if result["ld_path"]:
        vms_cfg = os.path.join(result["ld_path"], "vms", "config")
        if os.path.isdir(vms_cfg):
            result["vms_config_dir"] = vms_cfg
        else:
            # 可能在多开器目录下
            for mp_base in [result["multiplayer_path"], result["ld_path"]]:
                if mp_base:
                    vms_cfg = os.path.join(mp_base, "vms", "config")
                    if os.path.isdir(vms_cfg):
                        result["vms_config_dir"] = vms_cfg
                        break

    # 补充 MuMu 路径检测（跨电脑兼容）
    mumu_info = auto_detect_mumu()
    if mumu_info.get("manager_path"):
        result["mumu_manager_path"] = mumu_info["manager_path"]

    _AUTO_DETECT_CACHE = result
    _AUTO_DETECT_CACHE_TIME = time.time()
    return result


def _find_ld_from_process():
    """从运行中的 LDPlayer 进程获取安装路径（优先选含 vms/config 的）"""
    try:
        # wmic 可能在新版 Windows 被废弃，用 PowerShell Get-CimInstance 作为主检测方式
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process | Where-Object { $_.Name -match 'dnplayer|ldplayer|dnconsole|ldconsole|ldplayerservice' } | Select-Object -ExpandProperty ExecutablePath"],
            capture_output=True, text=True, timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        candidates = []
        for line in r.stdout.strip().split('\n'):
            line = line.strip()
            if line.lower().endswith(('.exe',)) and os.path.isfile(line):
                candidates.append(os.path.dirname(line))
        # wmic 备用
        if not candidates:
            r2 = subprocess.run(
                ['wmic', 'process', 'where',
                 "name='dnplayer.exe' or name='ldplayer.exe' or name='dnconsole.exe' or name='ldplayerservice.exe'",
                 'get', 'ExecutablePath'],
                capture_output=True, text=True, timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW
            )
            for line in r2.stdout.strip().split('\n'):
                line = line.strip()
                if line.lower().endswith(('.exe',)) and os.path.isfile(line):
                    candidates.append(os.path.dirname(line))
        # 优先返回含 vms/config 的路径（正确安装），其次返回第一个
        for c in candidates:
            if os.path.isdir(os.path.join(c, "vms", "config")):
                return c
        if candidates:
            return candidates[0]
    except Exception as _e:
        pass
    return None


def _find_multiplayer_from_process():
    """从运行中的多开器进程获取路径"""
    try:
        r = subprocess.run(
            ['wmic', 'process', 'where', "name='dnmultiplayerex.exe'",
             'get', 'ExecutablePath'],
            capture_output=True, text=True, timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        for line in r.stdout.strip().split('\n'):
            line = line.strip()
            if line.lower().endswith(('.exe',)) and os.path.isfile(line):
                return os.path.dirname(line)
    except Exception as _e:
        pass
    return None


def _find_multiplayer_from_pathconfig():
    """从 pathconfig.ini 获取多开器路径"""
    for base in _get_all_drives():
        for name in ['ldmutiplayer', 'LDPlayer', 'ldplayer']:
            pf = os.path.normpath(os.path.join(base, name, 'pathconfig.ini'))
            if os.path.isfile(pf):
                try:
                    with open(pf, 'r', encoding='utf-8') as f:
                        content = f.read()
                    for line in content.splitlines():
                        if line.startswith('player9='):
                            val = line.split('=', 1)[1].strip()
                            if os.path.isdir(val):
                                # 多开器目录是 pathconfig.ini 所在目录
                                mp_dir = os.path.dirname(pf)
                                # 但 pathconfig.ini 可能在多开器子目录
                                # 检查当前目录是否有多开器
                                if os.path.isfile(os.path.join(mp_dir, 'dnmultiplayerex.exe')):
                                    return mp_dir
                                # 检查上一级
                                parent = os.path.dirname(mp_dir)
                                if os.path.isfile(os.path.join(parent, 'dnmultiplayerex.exe')):
                                    return parent
                                # 直接返回 LDPlayer 路径（多开器可能在同一个目录）
                                return val
                except Exception as _e:
                    pass
                    continue
    return None


def _find_ld_from_registry():
    """从 Windows 注册表查找 LDPlayer 安装路径"""
    reg_paths = [
        (winreg.HKEY_CURRENT_USER, r"Software\LDPlayer\LDPlayer9"),
        (winreg.HKEY_CURRENT_USER, r"Software\LDPlayer\LDPlayer8"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\LDPlayer\LDPlayer9"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\LDPlayer\LDPlayer8"),
        (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Uninstall\LDPlayer"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Uninstall\LDPlayer"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\LDPlayer"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\LDPlayer"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Changzhi\LDPlayer"),
    ]
    for hkey, subkey in reg_paths:
        try:
            key = winreg.OpenKey(hkey, subkey, 0, winreg.KEY_READ)
        except Exception as _e:
            pass
            continue
        try:
            for val_name in ['InstallPath', 'Path', 'InstallDir', '']:
                try:
                    val, _ = winreg.QueryValueEx(key, val_name)
                    if val and os.path.isdir(val) and os.path.isfile(os.path.join(val, 'dnconsole.exe')):
                        return val
                except FileNotFoundError:
                    continue
        finally:
            try:
                winreg.CloseKey(key)
            except Exception as _e:
                pass
    return None


def _find_ld_from_pathconfig():
    """全盘搜索 pathconfig.ini，读取 player9=xxx 获取 LDPlayer 安装路径"""
    for drive in _get_all_drives():
        for root, dirs, files in os.walk(drive):
            if 'pathconfig.ini' in files:
                try:
                    with open(os.path.join(root, 'pathconfig.ini'), 'r', encoding='utf-8') as f:
                        for line in f:
                            line = line.strip()
                            if line.startswith('player') and '=' in line:
                                path = line.split('=', 1)[1].strip().strip('"\'')
                                # 尝试拼接 dnconsole.exe 确认
                                for _ in [path, os.path.join(path, 'dnconsole.exe')]:
                                    if os.path.isfile(os.path.join(path, 'dnconsole.exe')):
                                        return os.path.normpath(path)
                except Exception as _e:
                    pass
            # 限制深度 5 层
            depth = root.replace(drive, "").count(os.sep)
            if depth >= 5:
                dirs.clear()
    return None


def _scan_common_paths():
    """遍历所有盘符搜索 dnconsole.exe/ldconsole.exe（不预设目录名，覆盖任意安装路径）"""
    for drive in _get_all_drives():
        for root, dirs, files in os.walk(drive):
            for name in ['dnconsole.exe', 'ldconsole.exe']:
                if name in files:
                    return os.path.dirname(os.path.join(root, name))
            # 限制深度 4 层
            depth = root.replace(drive, "").count(os.sep)
            if depth >= 4:
                dirs.clear()
    return None


def _find_multiplayer_nearby(parent_dir):
    """在父目录附近查找多开器"""
    names = ['ldmutiplayer', 'LDPlayer', 'ldplayer']
    for name in names:
        candidate = os.path.join(parent_dir, name)
        if os.path.isfile(os.path.join(candidate, 'dnmultiplayerex.exe')):
            return candidate
        # 搜索父目录下一层
        try:
            for item in os.listdir(parent_dir):
                sub = os.path.join(parent_dir, item)
                if os.path.isdir(sub) and os.path.isfile(os.path.join(sub, 'dnmultiplayerex.exe')):
                    return sub
        except Exception as _e:
            pass
    return None


def _get_all_drives():
    """获取所有可用盘符"""
    drives = []
    for letter in 'CDEFGHIJKLMNOPQRSTUVWXYZ':
        d = f"{letter}:\\"
        if os.path.exists(d):
            drives.append(d)
    return drives


# ============================================================
# 实例扫描
# ============================================================

def scan_instances(vms_config_dir):
    """
    扫描所有实例，返回实例列表
    支持两种目录结构：
      a) vms/config/leidian0.config  （旧版 .config 文件）
      b) vms/leidian0/leidian0.config 或 vms/leidian0/config.json （新版目录）
    每个实例: {
        "name": "leidian0",
        "config_path": "...",
        "settings": { ... },
        "running": False,
    }
    """
    instances = []
    if not vms_config_dir or not os.path.isdir(vms_config_dir):
        return instances

    # 策略1: 从 vms_config_dir 里找 leidian*.config 文件
    _scan_config_files(vms_config_dir, instances)

    # 策略2: 如果没找到，可能是新版目录结构
    # vms_config_dir 可能是 vms/config/，子实例可能在 vms/leidian0/
    if not instances:
        parent = os.path.dirname(vms_config_dir)  # vms/config -> vms
        if parent and os.path.isdir(parent):
            _scan_instance_dirs(parent, instances)

    # 策略3: 如果 vms_config_dir 自身就是 vms/（不是 vms/config/）
    if not instances:
        _scan_instance_dirs(vms_config_dir, instances)

    # 按编号排序
    instances.sort(key=lambda x: x['name'])
    return instances


def _scan_config_files(config_dir, instances):
    """从 config 目录扫描 leidian*.config 文件"""
    try:
        for fname in os.listdir(config_dir):
            if not fname.startswith('leidian') or not fname.endswith('.config'):
                continue
            name = fname.replace('.config', '')
            if name == 'leidians':
                continue
            config_path = os.path.join(config_dir, fname)
            if os.path.isfile(config_path):
                settings = read_instance_config(config_path)
                instances.append({
                    "name": name,
                    "config_path": config_path,
                    "settings": settings,
                    "running": False,
                })
    except Exception as _e:
        pass


def _scan_instance_dirs(vms_dir, instances):
    """从 vms 目录扫描 leidianN/ 子目录中的配置文件"""
    try:
        for entry in sorted(os.listdir(vms_dir)):
            if not entry.startswith('leidian'):
                continue
            sub = os.path.join(vms_dir, entry)
            if not os.path.isdir(sub):
                continue
            # 找子目录里的配置文件
            config_path = None
            for cfg_name in [f"{entry}.config", "config.json", "config.cfg"]:
                cp = os.path.join(sub, cfg_name)
                if os.path.isfile(cp):
                    config_path = cp
                    break
            if not config_path:
                # 子目录中任意 .config 文件
                for f in os.listdir(sub):
                    if f.endswith('.config'):
                        config_path = os.path.join(sub, f)
                        break
            if not config_path:
                continue
            settings = read_instance_config(config_path)
            instances.append({
                "name": entry,
                "config_path": config_path,
                "settings": settings,
                "running": False,
            })
    except Exception as _e:
        pass


def check_running_instances(instances, dnconsole_path=None):
    """检查哪些实例正在运行
    优先使用 dnconsole.exe list2 / runninglist 获得准确结果
    回退到 wmic 方式
    """
    running_names = set()

    if dnconsole_path and os.path.isfile(dnconsole_path):
        import concurrent.futures as _cf

        def _try_list2():
            r = subprocess.run(
                [dnconsole_path, 'list2'],
                capture_output=True, text=True, timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW
            )
            if r.returncode == 0 and r.stdout.strip():
                names = set()
                for line in r.stdout.strip().splitlines():
                    parts = line.split(',')
                    if len(parts) >= 6:
                        name = parts[1].strip()
                        status = parts[5].strip()
                        if status == '1' and name.startswith('leidian'):
                            names.add(name)
                return names
            return None

        def _try_runninglist():
            r = subprocess.run(
                [dnconsole_path, 'runninglist'],
                capture_output=True, text=True, timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW
            )
            if r.returncode == 0 and r.stdout.strip():
                names = set()
                for line in r.stdout.strip().splitlines():
                    line = line.strip()
                    if line.startswith('leidian'):
                        names.add(line)
                return names
            return None

        # 并行尝试 list2 和 runninglist，谁先返回有效数据用谁
        with _cf.ThreadPoolExecutor(max_workers=2) as pool:
            f1 = pool.submit(_try_list2)
            f2 = pool.submit(_try_runninglist)
            for f in _cf.as_completed([f1, f2], timeout=12):
                try:
                    result = f.result()
                    if result:
                        running_names = result
                        break
                except Exception:
                    continue  # noqa: S112 一个失败继续等另一个

        if running_names:
            for inst in instances:
                inst['running'] = inst['name'] in running_names
            return

    # 回退方式: wmic 通过命令行检测
    try:
        r = subprocess.run(
            ['wmic', 'process', 'where',
             "name like '%Headless%' or name='dnplayer.exe' or name='ldplayer.exe'",
             'get', 'CommandLine'],
            capture_output=True, text=True, timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        for line in r.stdout.splitlines():
            for inst in instances:
                name = inst['name']
                # 使用单词边界匹配，避免 "leidian1" 误匹配 "leidian10"
                if re.search(r'(?<![a-zA-Z0-9])' + re.escape(name) + r'(?![a-zA-Z0-9])', line):
                    running_names.add(name)
    except Exception as _e:
        pass

    for inst in instances:
        inst['running'] = inst['name'] in running_names


# ============================================================
# 配置读写
# ============================================================

def read_instance_config(config_path):
    """读取实例配置文件"""
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def write_instance_config(config_path, settings):
    """写入实例配置文件"""
    try:
        with open(config_path, 'w', encoding='utf-8') as f:
            json.dump(settings, f, ensure_ascii=False, indent=4)
        return True, ""
    except Exception as e:
        return False, str(e)


def get_instance_summary(settings):
    """从配置中提取摘要信息"""
    summary = {
        "resolution": f"{settings.get('advancedSettings.resolution', {}).get('width', '?')}x"
                      f"{settings.get('advancedSettings.resolution', {}).get('height', '?')}",
        "cpu": settings.get('advancedSettings.cpuCount', '?'),
        "memory": settings.get('advancedSettings.memorySize', '?'),
        "root": settings.get('basicSettings.rootMode', False),
        "auto_run": settings.get('basicSettings.autoRun', False),
        "fps": settings.get('basicSettings.fps', '?'),
        "name": settings.get('statusSettings.playerName', ''),
    }
    return summary


# ============================================================
# 配置快照
# ============================================================

def save_snapshot(vms_config_dir, multiplayer_config_dir, snapshot_base_dir, mumu_vms_dir=None):
    """
    保存配置快照（同时备份 LDPlayer + MuMu 配置）
    返回: (快照目录路径, 消息)
    """
    if not vms_config_dir or not os.path.isdir(vms_config_dir):
        # LDPlayer 不可用时只备份 MuMu
        vms_config_dir = None
        _log_error("[SNAP] vms_config_dir 不可用，仅备份 MuMu")

    timestamp = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
    snap_dir = os.path.join(snapshot_base_dir, timestamp)
    _log_error(f"[SNAP] save_snapshot 开始: vms_config_dir={vms_config_dir}, mumu_vms_dir={mumu_vms_dir}")
    try:
        os.makedirs(snap_dir, exist_ok=True)

        # ---- 备份 LDPlayer 实例配置 ----
        count = 0
        if vms_config_dir:
            for fname in os.listdir(vms_config_dir):
                if fname.endswith('.config') and fname != 'leidians.config':
                    src = os.path.join(vms_config_dir, fname)
                    shutil.copy2(src, os.path.join(snap_dir, fname))
                    count += 1
            # 也备份全局配置（不计入实例数）
            global_cfg = os.path.join(vms_config_dir, 'leidians.config')
            if os.path.isfile(global_cfg):
                shutil.copy2(global_cfg, os.path.join(snap_dir, 'leidians.config'))
        _log_error(f"[SNAP] LDPlayer 实例配置找到 {count} 个")
        if count == 0 and vms_config_dir:
            try:
                _log_error(f"[SNAP] vms_config_dir 内容: {os.listdir(vms_config_dir)}")
            except Exception as _e:
                pass

        # 全局配置已在上面备份，这里不再重复复制

        # ---- 备份 MuMu 实例配置 ----
        mumu_count = 0
        if mumu_vms_dir and os.path.isdir(mumu_vms_dir):
            # 在快照目录下建 mumu_configs/ 子目录
            mumu_snap = os.path.join(snap_dir, "mumu_configs")
            os.makedirs(mumu_snap, exist_ok=True)
            for entry in sorted(os.listdir(mumu_vms_dir)):
                if not entry.startswith("MuMuPlayer-12.0-"):
                    continue
                cfg_src = os.path.join(mumu_vms_dir, entry, "configs", "vm_config.json")
                if os.path.isfile(cfg_src):
                    try:
                        shutil.copy2(cfg_src, os.path.join(mumu_snap, f"{entry}.json"))
                        mumu_count += 1
                    except Exception as _e:
                        pass

        # 写入元信息
        meta = {
            "timestamp": timestamp,
            "ldplayer_count": count,
            "mumu_count": mumu_count,
            "vms_config_dir": vms_config_dir,
            "multiplayer_config_dir": multiplayer_config_dir,
            "mumu_vms_dir": mumu_vms_dir,
        }
        with open(os.path.join(snap_dir, 'snapshot_meta.json'), 'w', encoding='utf-8') as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

        parts = [f"已备份 LDPlayer {count} 个实例"]
        if mumu_count:
            parts.append(f"MuMu {mumu_count} 个实例")
        return snap_dir, "、".join(parts)
    except Exception as e:
        return None, f"保存失败: {str(e)}"


def restore_snapshot(snapshot_dir, vms_config_dir, multiplayer_config_dir, mumu_vms_dir=None):
    """
    恢复配置快照（同时恢复 LDPlayer + MuMu 配置）
    返回: (成功数, 消息)
    """
    if not os.path.isdir(snapshot_dir):
        return 0, "快照目录不存在"
    if not vms_config_dir or not os.path.isdir(vms_config_dir):
        return 0, "实例配置目录无效"

    restored = 0
    errors = []

    # 恢复 LDPlayer 实例配置（排除全局配置 leidians.config）
    for fname in os.listdir(snapshot_dir):
        if fname.endswith('.config') and fname.startswith('leidian') and fname != 'leidians.config':
            src = os.path.join(snapshot_dir, fname)
            dst = os.path.join(vms_config_dir, fname)
            try:
                shutil.copy2(src, dst)
                restored += 1
            except Exception as e:
                errors.append(f"{fname}: {e}")

    # 恢复全局配置到 vms_config_dir（leidians.config 在此目录）
    global_cfg = os.path.join(snapshot_dir, 'leidians.config')
    if os.path.isfile(global_cfg) and vms_config_dir and os.path.isdir(vms_config_dir):
        dst = os.path.join(vms_config_dir, 'leidians.config')
        try:
            shutil.copy2(global_cfg, dst)
        except Exception as e:
            errors.append(f"leidians.config: {e}")

    # 恢复 MuMu 实例配置
    mumu_restored = 0
    mumu_snap = os.path.join(snapshot_dir, "mumu_configs")
    if os.path.isdir(mumu_snap) and mumu_vms_dir and os.path.isdir(mumu_vms_dir):
        for fname in os.listdir(mumu_snap):
            if not fname.endswith(".json"):
                continue
            inst_name = fname.replace(".json", "")  # MuMuPlayer-12.0-0
            dst_dir = os.path.join(mumu_vms_dir, inst_name, "configs")
            dst = os.path.join(dst_dir, "vm_config.json")
            if not os.path.isdir(dst_dir):
                continue
            try:
                shutil.copy2(os.path.join(mumu_snap, fname), dst)
                mumu_restored += 1
            except Exception as e:
                errors.append(f"{fname}: {e}")

    parts = [f"已恢复 LDPlayer {restored} 个实例"]
    if mumu_restored:
        parts.append(f"MuMu {mumu_restored} 个实例")
    msg = "、".join(parts)
    if errors:
        msg += f"，{len(errors)} 个失败: " + "; ".join(errors)
    return restored + mumu_restored, msg


def list_snapshots(snapshot_base_dir):
    """列出所有快照"""
    snapshots = []
    if not os.path.isdir(snapshot_base_dir):
        return snapshots
    for name in sorted(os.listdir(snapshot_base_dir), reverse=True):
        snap_dir = os.path.join(snapshot_base_dir, name)
        if os.path.isdir(snap_dir):
            meta_path = os.path.join(snap_dir, 'snapshot_meta.json')
            meta = {}
            if os.path.isfile(meta_path):
                try:
                    with open(meta_path, 'r', encoding='utf-8') as f:
                        meta = json.load(f)
                except Exception as _e:
                    pass
            ld_cnt = meta.get("ldplayer_count")
            if ld_cnt is None:
                ld_cnt = meta.get("instance_count", 0)
            mm_cnt = meta.get("mumu_count", 0)
            try:
                total = int(ld_cnt or 0) + int(mm_cnt or 0)
            except (ValueError, TypeError):
                total = 0
            snapshots.append({
                "name": name,
                "path": snap_dir,
                "instance_count": total,
                "ldplayer_count": ld_cnt,
                "mumu_count": mm_cnt,
                "timestamp": meta.get("timestamp", name),
            })
    return snapshots


# ============================================================
# 间隔启动
# ============================================================

def _extract_index(instance_name):
    """
    从实例名中提取索引号
    实例命名规则: "leidian0", "leidian1", "leidian2"...
    索引就是末尾的数字
    返回 int 或 None（提取失败）
    """
    match = re.search(r'(\d+)$', instance_name)
    if match:
        return int(match.group(1))
    return None


def launch_instance(dnconsole_path, instance_name, timeout=30):
    """
    启动单个实例。
    优先用 subprocess.run 免 UAC，ShellExecuteW(runas) 仅做提权回退。
    启动后校验进程是否真正运行。
    返回: (成功, 消息)
    """
    if not os.path.isfile(dnconsole_path):
        _log_error("[LAUNCH]", f"dnconsole 不存在: {dnconsole_path}")
        return False, f"dnconsole.exe 不存在: {dnconsole_path}"

    # 从实例名中提取索引
    index = _extract_index(instance_name)
    if index is not None:
        params = f'launch --index {index}'
        launch_args = ['launch', '--index', str(index)]
    else:
        params = f'launch --name {instance_name}'
        launch_args = ['launch', '--name', instance_name]

    _log_error("[LAUNCH]", f"启动 {instance_name} (index={index})")
    directory = os.path.dirname(dnconsole_path)
    err_code = 0

    def _check_running():
        """启动后校验：调用 dnconsole list2 看实例是否在运行"""
        try:
            r = subprocess.run(
                [dnconsole_path, 'list2'],
                capture_output=True, text=True, timeout=8,
                creationflags=subprocess.CREATE_NO_WINDOW
            )
            # list2 输出格式：index,name,title,...,status(第6列=1运行中)
            for line in r.stdout.strip().split('\n'):
                if not line.strip():
                    continue
                parts = line.split(',')
                if len(parts) < 6:
                    continue
                if index is not None:
                    if parts[0].strip() != str(index):
                        continue
                else:
                    # 没有索引时按实例名匹配第2列
                    if parts[1].strip() != instance_name:
                        continue
                running = parts[5].strip() == '1'
                _log_error("[LAUNCH]", f"_check_running: {instance_name} running={running}")
                return running
            _log_error("[LAUNCH]", f"_check_running: {instance_name} 未在 list2 中找到")
            return False
        except Exception as e:
            _log_error("[LAUNCH]", f"_check_running 异常: {e}")
            return False

    # 尝试方式1: subprocess.run（免UAC，适合无人值守定时启动）
    try:
        _log_error("[LAUNCH]", f"方式1: subprocess.run {instance_name}")
        r = subprocess.run(
            [dnconsole_path] + launch_args,
            capture_output=True, text=True, timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        _log_error("[LAUNCH]", f"subprocess returncode={r.returncode}")
        if r.returncode == 0:
            time.sleep(3)
            if _check_running():
                _log_error("[LAUNCH]", f"{instance_name} 启动成功（方式1）")
                return True, f"{instance_name} 启动成功"
            else:
                # 进程未启动，等待再等几秒
                time.sleep(5)
                if _check_running():
                    _log_error("[LAUNCH]", f"{instance_name} 启动成功（方式1+延迟）")
                    return True, f"{instance_name} 启动成功（延迟）"
                _log_error("[LAUNCH]", f"{instance_name} 启动失败：dnconsole 返回0但进程未运行")
                return False, f"{instance_name} 启动失败：dnconsole 返回成功但进程未运行"
        raw = (r.stdout.strip() or r.stderr.strip() or "")
        _log_error("[LAUNCH]", f"subprocess 返回非0: {raw[:100]}")
        if "Usage:" in raw or "Commands :" in raw or "dnconsole <command>" in raw.lower():
            return False, f"启动失败，参数错误（实例: {instance_name}）"
    except Exception as e:
        _log_error("[LAUNCH]", f"方式1 异常: {e}")

    # 尝试方式2: ShellExecuteW (runas 提权，可能弹UAC)
    _log_error("[LAUNCH]", f"方式2: ShellExecuteW {instance_name}")
    try:
        shell32 = ctypes.windll.shell32
        shell32.ShellExecuteW.argtypes = [
            ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p,
            ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_int,
        ]
        shell32.ShellExecuteW.restype = ctypes.c_void_p
        h_instance = shell32.ShellExecuteW(
            None, "runas", dnconsole_path, params, directory, 0
        )
        h_val = h_instance.value if h_instance else 0
        _log_error("[LAUNCH]", f"ShellExecuteW 返回值={h_val}")
        if h_val and h_val > 32:
            time.sleep(3)
            if _check_running():
                _log_error("[LAUNCH]", f"{instance_name} 启动成功（方式2）")
                return True, f"{instance_name} 启动成功（提权）"
            time.sleep(5)
            if _check_running():
                _log_error("[LAUNCH]", f"{instance_name} 启动成功（方式2+延迟）")
                return True, f"{instance_name} 启动成功（提权+延迟）"
            _log_error("[LAUNCH]", f"{instance_name} 启动失败：ShellExecuteW >32 但进程未运行")
            return False, f"{instance_name} 启动失败：ShellExecuteW 成功但进程未运行"
        err_code = h_val or 0
    except Exception as e:
        _log_error("[LAUNCH]", f"方式2 异常: {e}")
        return False, f"{instance_name} 启动失败: {e}"

    _log_error("[LAUNCH]", f"{instance_name} 全部方式失败 (err={err_code})")
    return False, f"{instance_name} 失败 (err={err_code})"


def staggered_launch(dnconsole_path, instance_names, interval_seconds=5,
                     on_status=None, on_progress=None):
    """
    间隔启动多个实例（同步执行，会阻塞）
    on_status: fn(text) 状态回调
    on_progress: fn(current, total) 进度回调
    返回: [(实例名, 成功, 消息), ...]
    """
    results = []
    total = len(instance_names)
    for i, name in enumerate(instance_names):
        if on_status:
            on_status(f"正在启动 {name} ({i+1}/{total})...")
        if on_progress:
            on_progress(i, total)

        ok, msg = launch_instance(dnconsole_path, name)
        results.append((name, ok, msg))

        if on_status:
            on_status(msg)

        # 间隔等待（最后一个不等）
        if i < total - 1 and interval_seconds > 0:
            if on_status:
                on_status(f"等待 {interval_seconds} 秒后启动下一个...")
            for sec in range(interval_seconds, 0, -1):
                if on_status:
                    on_status(f"等待 {sec} 秒...")
                time.sleep(1)

    if on_progress:
        on_progress(total, total)
    return results


# ============================================================
# 配置持久化（工具自身的配置）
# ============================================================

def _tool_config_dir():
    """获取工具配置目录（exe所在目录）"""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


TOOL_CONFIG_FILE = os.path.join(_tool_config_dir(), "instance_config.json")
SNAPSHOT_DIR = os.path.join(_tool_config_dir(), "快照")

# 确保快照目录存在
def _ensure_snapshot_dir():
    """确保快照目录存在"""
    try:
        os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    except Exception as _e:
        pass

_ensure_snapshot_dir()


def load_tool_config():
    """加载工具配置"""
    try:
        if os.path.isfile(TOOL_CONFIG_FILE):
            with open(TOOL_CONFIG_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception as _e:
        pass
    return {}


def save_tool_config(config):
    """保存工具配置（原子写入：先写.tmp再rename）"""
    tmp = TOOL_CONFIG_FILE + ".tmp"
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
        os.replace(tmp, TOOL_CONFIG_FILE)
        return True
    except Exception:
        try:
            if os.path.isfile(tmp):
                os.remove(tmp)
        except Exception as _e:
            pass
        return False


def get_saved_paths():
    """获取保存的路径配置"""
    config = load_tool_config()
    return config.get("paths", {})


def save_paths(paths):
    """保存路径配置"""
    config = load_tool_config()
    config["paths"] = paths
    save_tool_config(config)


# ============================================================
# 模拟器环境检测模块
# ============================================================

# Windows 功能名称常量
FEATURE_HYPERV_ALL = "Microsoft-Hyper-V-All"
FEATURE_HYPERV_PLATFORM = "HypervisorPlatform"
FEATURE_VMP = "VirtualMachinePlatform"
FEATURE_WSL = "Microsoft-Windows-Subsystem-Linux"
FEATURE_CONTAINER = "Containers-DisposableClientVM"

FEATURE_LABELS = {
    FEATURE_HYPERV_ALL: "Hyper-V（完整虚拟化）",
    FEATURE_HYPERV_PLATFORM: "Windows Hypervisor Platform",
    FEATURE_VMP: "Virtual Machine Platform",
}

FEATURE_EMULATOR_IMPACT = {
    "冲突（必须关闭）": [FEATURE_HYPERV_ALL, FEATURE_HYPERV_PLATFORM],
    "可能冲突": [FEATURE_VMP],
    "推荐关闭运行时冲突": ["VirtualizationBasedSecurity"],
}


def _run_powershell(cmd, timeout=20):
    """运行 PowerShell 命令并返回 stdout"""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", cmd],
            capture_output=True, text=True, timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        return r.stdout.strip(), r.returncode
    except Exception:
        return "", -1


def check_windows_feature(feature_name):
    """检查 Windows 功能是否启用
    返回: {"name": ..., "enabled": bool, "raw": ...}
    """
    try:
        out, rc = _run_powershell(
            f"Get-WindowsOptionalFeature -Online -FeatureName {feature_name} | "
            f"Select-Object -ExpandProperty State"
        )
        if rc == 0 and out:
            enabled = "Enabled" in out
            return {
                "name": feature_name,
                "label": FEATURE_LABELS.get(feature_name, feature_name),
                "enabled": enabled,
                "raw": out,
            }
    except Exception as _e:
        pass
    return {"name": feature_name, "label": feature_name, "enabled": None, "raw": ""}


def check_all_windows_features():
    """检查所有与模拟器相关的 Windows 功能状态"""
    features = []
    for fn in [FEATURE_HYPERV_ALL, FEATURE_HYPERV_PLATFORM, FEATURE_VMP]:
        features.append(check_windows_feature(fn))

    # VBS / Credential Guard 特殊检测
    vbs_status = _check_virtualization_based_security()
    features.append(vbs_status)

    return features


def _check_virtualization_based_security():
    """检查 Virtualization-Based Security (VBS) 状态"""
    try:
        out, rc = _run_powershell(
            "Get-CimInstance -Namespace root\\Microsoft\\Windows\\DeviceGuard "
            "-Class Win32_DeviceGuard -ErrorAction SilentlyContinue | "
            "Select-Object -ExpandProperty VirtualizationBasedSecurityStatus"
        )
        if rc == 0 and out:
            try:
                status = int(out.strip())
                # 0=Off, 1=On but not running, 2=On and running
                enabled = status >= 1
                labels = {0: "未启用", 1: "已启用（未运行）", 2: "运行中"}
                return {
                    "name": "VirtualizationBasedSecurity",
                    "label": "基于虚拟化的安全 (VBS)",
                    "enabled": enabled,
                    "raw": labels.get(status, f"状态码 {status}"),
                }
            except ValueError:
                pass
    except Exception as _e:
        pass
    return {"name": "VirtualizationBasedSecurity", "label": "VBS", "enabled": None, "raw": ""}


def check_virtualization_cpu():
    """检查 CPU 是否开启硬件虚拟化 (VT-x/AMD-V)"""
    try:
        out, rc = _run_powershell(
            "Get-CimInstance Win32_Processor | "
            "Select-Object -ExpandProperty VirtualizationFirmwareEnabled"
        )
        if rc == 0 and out:
            enabled = "True" in out
            return {"name": "CPU_VTx", "label": "CPU 虚拟化 (VT-x/AMD-V)", "enabled": enabled, "raw": out.strip()}
    except Exception as _e:
        pass
    return {"name": "CPU_VTx", "label": "CPU 虚拟化 (VT-x/AMD-V)", "enabled": None, "raw": ""}


def check_ldplayer_hyperv_version():
    """检测已安装的 LDPlayer 是标准版还是 Hyper-V 版
    返回: {"version": "standard" / "hyperv" / "unknown", "detail": str}
    """
    # 通过检查 vbox64 目录存在与否来判断
    # 标准版有 vbox64/ 目录，Hyper-V 版没有
    found_dir = None
    for p in ["ld_path", "multiplayer_path"]:
        d = None
        config = load_tool_config()
        if "paths" in config:
            d = config["paths"].get(p)
        if d and os.path.isdir(d):
            found_dir = d
            break

    if not found_dir:
        return {"version": "unknown", "detail": "未检测到 LDPlayer 安装路径"}

    # 检查关键文件
    has_vbox = os.path.isdir(os.path.join(found_dir, "vbox64"))
    has_vbox_dll = os.path.isfile(os.path.join(found_dir, "vbox64", "VBoxRT.dll"))

    # 检查是否有 hyperv 相关文件
    hyperv_files = [
        "ldvirtuahyperv.dll", "ldvirtuahyperv64.dll",
    ]
    has_hyperv_dll = any(os.path.isfile(os.path.join(found_dir, f)) for f in hyperv_files)

    if has_vbox and has_vbox_dll:
        return {"version": "standard", "detail": "标准版（基于 VirtualBox，与 Hyper-V 冲突）"}
    elif has_hyperv_dll:
        return {"version": "hyperv", "detail": "Hyper-V 兼容版"}
    elif has_vbox and not has_vbox_dll:
        return {"version": "unknown", "detail": "vbox64 目录存在但 VBoxRT.dll 缺失，版本未知"}
    else:
        # 进一步检查配置文件
        ld_config = os.path.join(found_dir, "ld_config.ini")
        if os.path.isfile(ld_config):
            try:
                with open(ld_config, "r", encoding="utf-8") as f:
                    content = f.read()
                if "hyperv" in content.lower():
                    return {"version": "hyperv", "detail": "Hyper-V 兼容版（配置含 hyperv 标识）"}
            except Exception as _e:
                pass
        return {"version": "standard", "detail": "疑似标准版（默认判断）"}


def get_emulator_environment_report():
    """获取完整的模拟器环境检测报告"""
    report = {
        "features": check_all_windows_features(),
        "cpu_vt": check_virtualization_cpu(),
        "ldplayer": check_ldplayer_hyperv_version(),
        "issues": [],
        "can_auto_fix": False,
        "overall_score": 100,
    }

    deductions = 0
    ld_version = report["ldplayer"]["version"]

    for f in report["features"]:
        name = f["name"]
        enabled = f["enabled"]

        if enabled is None:
            continue

        # 冲突判断逻辑
        if name == FEATURE_HYPERV_ALL and enabled:
            if ld_version == "standard":
                report["issues"].append({
                    "feature": name,
                    "severity": "critical",
                    "message": "Hyper-V 已启用，与 LDPlayer 标准版冲突，需关闭",
                    "fix_action": "disable",
                })
                deductions += 40
            elif ld_version == "hyperv":
                report["issues"].append({
                    "feature": name,
                    "severity": "info",
                    "message": "Hyper-V 已启用（使用 Hyper-V 版 LDPlayer 时可以保持开启）",
                    "fix_action": None,
                })

        elif name == FEATURE_HYPERV_PLATFORM and enabled:
            if ld_version == "standard":
                report["issues"].append({
                    "feature": name,
                    "severity": "warning",
                    "message": "Windows Hypervisor Platform 已启用，与标准版 LDPlayer 冲突",
                    "fix_action": "disable",
                })
                deductions += 15
            else:
                report["issues"].append({
                    "feature": name,
                    "severity": "info",
                    "message": "Windows Hypervisor Platform 已启用（Hyper-V 版需要此功能）",
                    "fix_action": None,
                })

        elif name == FEATURE_VMP and enabled:
            if ld_version == "standard":
                report["issues"].append({
                    "feature": name,
                    "severity": "warning",
                    "message": "Virtual Machine Platform 已启用，建议关闭（与标准版 LDPlayer 冲突）",
                    "fix_action": "disable",
                })
                deductions += 10
            else:
                report["issues"].append({
                    "feature": name,
                    "severity": "info",
                    "message": "Virtual Machine Platform 已启用（Hyper-V 版需要此功能）",
                    "fix_action": None,
                })

        elif name == "VirtualizationBasedSecurity" and enabled:
            report["issues"].append({
                "feature": name,
                "severity": "warning",
                "message": "VBS 已启用，可能影响模拟器性能，建议关闭",
                "fix_action": "disable_vbs",
            })
            deductions += 5

    # CPU 虚拟化检查
    vt = report["cpu_vt"]
    if vt["enabled"] is False:
        report["issues"].append({
            "feature": "CPU_VTx",
            "severity": "critical",
            "message": "BIOS 中未开启 VT-x/AMD-V，模拟器无法使用硬件加速！请进入 BIOS 开启",
            "fix_action": None,  # BIOS 层面，无法自动修复
        })
        deductions += 50
    elif vt["enabled"] is True:
        pass  # 正常
    else:
        report["issues"].append({
            "feature": "CPU_VTx",
            "severity": "warning",
            "message": "无法检测 CPU 虚拟化状态",
            "fix_action": None,
        })
        deductions += 5

    # 计算是否有可自动修复的问题
    report["can_auto_fix"] = any(
        i.get("fix_action") == "disable" for i in report["issues"]
    )

    report["overall_score"] = max(0, 100 - deductions)
    return report


def apply_fix_disable_feature(feature_name):
    """关闭指定的 Windows 功能（需要管理员权限）
    返回: (success, message)
    """
    try:
        out, rc = _run_powershell(
            f"dism /Online /Disable-Feature /FeatureName:{feature_name} /NoRestart /Quiet",
            timeout=120
        )
        if rc == 0 or rc == 1:  # dism 退出码 1 也可能是成功
            # 验证是否真的是 Disabled
            check = check_windows_feature(feature_name)
            if check["enabled"] is False:
                return True, f"{feature_name} 已成功关闭"
            # 可能需要管理员权限
            return False, f"关闭失败，请以管理员身份运行此程序。输出: {out[:200]}"
        else:
            return False, f"dism 返回错误码 {rc}: {out[:200]}"
    except subprocess.TimeoutExpired:
        return False, "操作超时（2分钟）"
    except Exception as e:
        return False, f"执行失败: {str(e)}"


def disable_vbs():
    """通过 bcdedit 关闭 VBS（基于虚拟化的安全）
    需要管理员权限 + 完全关机（非重启）才能生效
    返回: (success, message)
    """
    try:
        # 方法1: bcdedit hypervisorlaunchtype off
        r = subprocess.run(
            ["bcdedit", "/set", "{current}", "hypervisorlaunchtype", "off"],
            capture_output=True, text=True, timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        if r.returncode == 0:
            return True, (
                "✅ VBS 已禁用（bcdedit）\n"
                "⚠ 重要：必须执行「完全关机」（不是重启）才能生效！\n"
                "   开始菜单 → 电源 → 关机，等几秒再开机"
            )
        # 可能提示权限不够
        err = r.stderr or r.stdout or ""
        return False, f"bcdedit 返回错误: {err[:200]}"
    except FileNotFoundError:
        return False, "未找到 bcdedit.exe（请以管理员身份运行）"
    except subprocess.TimeoutExpired:
        return False, "bcdedit 超时"
    except Exception as e:
        return False, f"执行失败: {str(e)}"


def apply_all_fixes():
    """一键关闭所有冲突功能
    返回: [(feature_name, success, message), ...]
    """
    report = get_emulator_environment_report()
    results = []
    has_vbs_fix = False
    for issue in report["issues"]:
        action = issue.get("fix_action")
        if action == "disable":
            fn = issue["feature"]
            ok, msg = apply_fix_disable_feature(fn)
            results.append((fn, ok, msg))
        elif action == "disable_vbs":
            has_vbs_fix = True

    # VBS 修复放在最后，独立执行
    if has_vbs_fix:
        ok, msg = disable_vbs()
        results.append(("VirtualizationBasedSecurity", ok, msg))

    return results


# ============================================================
# MuMu 模拟器检测与管理
# ============================================================

# 本地日志（兼容主文件的 _log_error，独立打包时也有）
def _log_error(context, exc_info=None):
    """写入错误日志到 exe 同级目录"""
    import traceback
    try:
        if getattr(sys, 'frozen', False):
            _dir = os.path.dirname(os.path.abspath(sys.executable))
        else:
            _dir = os.path.dirname(os.path.abspath(__file__))
        log_path = os.path.join(_dir, "模拟器管理工具_运行日志.txt")
        if exc_info is None:
            exc_info = traceback.format_exc()
        elif isinstance(exc_info, BaseException):
            exc_info = f"{type(exc_info).__name__}: {exc_info}"
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(log_path, 'a', encoding='utf-8') as f:
            f.write(f"[{ts}] [{context}]\n{exc_info}\n---\n")
    except Exception as _e:
        pass


def _log_info(msg):
    """记录一般信息"""
    try:
        if getattr(sys, 'frozen', False):
            _dir = os.path.dirname(os.path.abspath(sys.executable))
        else:
            _dir = os.path.dirname(os.path.abspath(__file__))
        log_path = os.path.join(_dir, "模拟器管理工具_运行日志.txt")
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(log_path, 'a', encoding='utf-8') as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass


# 常见 MuMu 安装路径
MUMU_PATHS_CANDIDATES = [
    r"C:\Program Files\Netease\MuMu\nx_main\MuMuManager.exe",
    r"C:\Program Files\Netease\MuMu\shell\MuMuManager.exe",
    r"C:\Program Files\Netease\MuMu\MuMuManager.exe",
    r"C:\Program Files\Netease\MuMuPlayer-12.0\nx_main\MuMuManager.exe",
    r"C:\Program Files\Netease\MuMuPlayer-12.0\shell\MuMuManager.exe",
    r"C:\Program Files\Netease\MuMuPlayer-12.0\MuMuManager.exe",
    r"C:\Program Files\MuMuPlayer-12.0\shell\MuMuManager.exe",
    r"C:\Program Files\MuMuPlayer-12.0\nx_main\MuMuManager.exe",
    r"C:\Program Files (x86)\Netease\MuMuPlayer-12.0\shell\MuMuManager.exe",
    r"C:\Program Files (x86)\Netease\MuMu\nx_main\MuMuManager.exe",
]


def auto_detect_mumu():
    """自动检测 MuMu 安装路径和 MuMuManager.exe
    返回: {"manager_path": str, "cli_path": str, "install_dir": str, "found": bool}
    """
    result = {"manager_path": None, "cli_path": None, "install_dir": None, "found": False}

    # 1. 搜索 MuMuManager.exe 在已知安装路径
    for p in MUMU_PATHS_CANDIDATES:
        if os.path.isfile(p):
            result["manager_path"] = p
            result["install_dir"] = os.path.dirname(os.path.dirname(p))
            result["found"] = True
            cli = os.path.join(result["install_dir"], "nx_main", "mumu-cli.exe")
            if os.path.isfile(cli):
                result["cli_path"] = cli
            return result

    # 1b. 从运行中的进程获取路径
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process | Where-Object { $_.Name -match 'MuMuManager' } | Select-Object -ExpandProperty ExecutablePath"],
            capture_output=True, text=True, timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        for line in r.stdout.strip().split('\n'):
            line = line.strip()
            if line.lower().endswith('mumumanager.exe') and os.path.isfile(line):
                result["manager_path"] = line
                result["install_dir"] = os.path.dirname(os.path.dirname(line))
                result["found"] = True
                return result
    except Exception as _e:
        pass
    # wmic 备用
    try:
        r = subprocess.run(
            ['wmic', 'process', 'where', "name='MuMuManager.exe'", 'get', 'ExecutablePath'],
            capture_output=True, text=True, timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        for line in r.stdout.strip().split('\n'):
            line = line.strip()
            if line.lower().endswith('mumumanager.exe') and os.path.isfile(line):
                result["manager_path"] = line
                result["install_dir"] = os.path.dirname(os.path.dirname(line))
                result["found"] = True
                return result
    except Exception as _e:
        pass

    # 1c. 从注册表搜索
    reg_paths = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Netease\MuMu"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Netease\MuMu"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Netease\MuMuPlayer-12.0"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Netease\MuMuPlayer-12.0"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Netease\MuMuPlayer-12.0"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\MuMuPlayer"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\MuMuPlayer"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\MuMu"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\MuMu"),
    ]
    for hkey, subkey in reg_paths:
        try:
            key = winreg.OpenKey(hkey, subkey, 0, winreg.KEY_READ)
        except Exception as _e:
            pass
            continue
        try:
            for val_name in ['InstallPath', 'Path', 'InstallDir', '']:
                try:
                    val, _ = winreg.QueryValueEx(key, val_name)
                    if val and os.path.isdir(val):
                        # 搜索 nx_main/MuMuManager.exe
                        for sub in ['', 'nx_main', 'shell']:
                            mgr = os.path.join(val, sub, 'MuMuManager.exe')
                            if os.path.isfile(mgr):
                                result["manager_path"] = mgr
                                result["install_dir"] = val
                                result["found"] = True
                                return result
                        # 搜索整个目录
                        for root, dirs, files in os.walk(val):
                            if "MuMuManager.exe" in files:
                                result["manager_path"] = os.path.join(root, "MuMuManager.exe")
                                result["install_dir"] = val
                                result["found"] = True
                                return result
                            # 限制深度为最多 3 级子目录（用 normpath 消除尾部 \\ 和 \\.\\ 的影响）
                            norm_root = os.path.normpath(root)
                            norm_val = os.path.normpath(val)
                            if norm_root.count(os.sep) - norm_val.count(os.sep) >= 3:
                                dirs.clear()
                except FileNotFoundError:
                    continue
        finally:
            try:
                winreg.CloseKey(key)
            except Exception as _e:
                pass

    # 2. 遍历所有盘符搜索 MuMuManager.exe（不预设目录名，覆盖任意安装路径）
    for drive in _get_all_drives():
        for root, dirs, files in os.walk(drive):
            if "MuMuManager.exe" in files:
                fp = os.path.join(root, "MuMuManager.exe")
                result["manager_path"] = fp
                # install_dir 是 nx_main 的上一级
                mgr_dir = os.path.dirname(root)
                if os.path.basename(root) == "nx_main":
                    result["install_dir"] = mgr_dir
                else:
                    result["install_dir"] = root
                result["found"] = True
                cli = os.path.join(result["install_dir"], "nx_main", "mumu-cli.exe")
                if not os.path.isfile(cli):
                    cli = os.path.join(root, "mumu-cli.exe")
                if os.path.isfile(cli):
                    result["cli_path"] = cli
                return result
            # 限制深度 5 层，避免扫到 system32 等深层目录
            depth = root.replace(drive, "").count(os.sep)
            if depth >= 5:
                dirs.clear()

    return result


def scan_mumu_instances(mumu_manager_path):
    """扫描 MuMu 模拟器实例
    优先用 MuMuManager.exe 获取（兼容各种环境），降级到磁盘扫描 vms/ 目录。
    搜索多个可能的 vms 位置，确保不同安装方式都能找到。
    返回: [{"index": "0", "name": "...", "running": bool, "hyperv": bool}, ...]
    """
    if not mumu_manager_path or not os.path.isfile(mumu_manager_path):
        return []

    mgr_dir = os.path.dirname(mumu_manager_path)          # .../nx_main
    install_dir = os.path.dirname(mgr_dir)                 # .../MuMu Player 12
    user_docs = os.path.expanduser("~\\Documents")

    # ========== 第1招：MuMuManager.exe 直调 ==========
    # 从 GUI 进程调控制台程序，各种环境都有可能抑制 stdout。
    # 试多种方式：直调、cmd /c、临时 bat，哪种能出数据就用哪种。
    def _try_mumumanager():
        """尝试用 MuMuManager 获取实例列表"""
        env = os.environ.copy()
        env["PATH"] = mgr_dir + os.pathsep + env.get("PATH", "")

        # 尝试 A：subprocess.run 直调
        try:
            r = subprocess.run(
                [mumu_manager_path, "info", "--vmindex", "all"],
                capture_output=True, timeout=10, cwd=mgr_dir, env=env,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            raw = (r.stdout or b"").decode('utf-8', errors='replace').strip()
            if raw:
                return json.loads(raw)
        except Exception:  # noqa: S110 — expected: fallback to next attempt
            pass  # fallback to B

        # 尝试 B：cmd /c 中转
        try:
            r = subprocess.run(
                ["cmd.exe", "/c", f'"{mumu_manager_path}" info --vmindex all'],
                capture_output=True, timeout=15, cwd=mgr_dir,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            raw = (r.stdout or b"").decode('utf-8', errors='replace').strip()
            if raw:
                return json.loads(raw)
        except Exception:  # noqa: S110 — expected: fallback to next attempt
            pass

        # 尝试 C：临时 bat 文件 > stdout 重定向
        out_file = os.path.join(tempfile.gettempdir(), f"_mumu_scan_{os.getpid()}.txt")
        bat_path = os.path.join(tempfile.gettempdir(), f"_mumu_scan_{os.getpid()}.bat")
        try:
            bat_content = f'@echo off\r\n"{mumu_manager_path}" info --vmindex all > "{out_file}"\r\n'
            with open(bat_path, 'w', encoding='utf-8') as f:
                f.write(bat_content)
            subprocess.run([bat_path], timeout=30, cwd=mgr_dir,
                           creationflags=subprocess.CREATE_NO_WINDOW)
            if os.path.isfile(out_file):
                with open(out_file, 'r', encoding='utf-8') as f:
                    raw = f.read().strip()
                if raw:
                    return json.loads(raw)
        except Exception:  # noqa: S110 — expected: all 3 attempts failed, disk scan fallback
            pass  # all 3 exhausted
        finally:
            try:
                if os.path.isfile(bat_path): os.unlink(bat_path)
            except Exception as _e:
                pass
            try:
                if os.path.isfile(out_file): os.unlink(out_file)
            except Exception as _e:
                pass

        return None

    try:
        data = _try_mumumanager()
        if data and isinstance(data, dict):
            instances = []
            for idx, info in data.items():
                instances.append({
                    "index": str(info.get("index", idx)),
                    "name": info.get("name", f"MuMu-{idx}"),
                    "running": info.get("is_process_started", False),
                    "android_running": info.get("is_android_started", False),
                    "hyperv": info.get("hyperv_enabled", False),
                    "disk_size": info.get("disk_size_bytes", 0),
                    "cpu": "?",
                    "memory": "?",
                    "root": False,
                })
            instances.sort(key=lambda x: int(x["index"]) if str(x["index"]).isdigit() else 0)
            # 补充 vm_config.json 里的 CPU/内存/Root
            for inst in instances:
                for dir_candidate in [os.path.join(install_dir, "vms"),
                                      os.path.join(user_docs, "MuMu12", "vms"),
                                      os.path.join(user_docs, "MuMuPlayer-12.0", "vms")]:
                    cfg = os.path.join(dir_candidate, f"MuMuPlayer-12.0-{inst['index']}", "configs", "vm_config.json")
                    if os.path.isfile(cfg):
                        try:
                            with open(cfg, 'r', encoding='utf-8') as f:
                                vm = json.load(f).get("vm", {})
                                inst["cpu"] = vm.get("cpu", "?")
                                inst["memory"] = vm.get("memory", "?")
                                inst["root"] = vm.get("root", "").lower() == "true"
                        except Exception as _e:
                            pass
                        break
            return instances
    except Exception as e:
        _log_error(f"[MUMU_SCAN] MuMuManager 整体异常: {e}")

    # ========== 第2招：磁盘扫描（MuMuManager 不可用时降级）==========
    vms_candidates = []
    # 安装目录下的 vms
    inst_vms = os.path.join(install_dir, "vms")
    if os.path.isdir(inst_vms):
        vms_candidates.append(inst_vms)
    # 用户文档下的 vms
    for sub in ["MuMu12", "MuMuPlayer-12.0"]:
        p = os.path.join(user_docs, sub, "vms")
        if os.path.isdir(p):
            vms_candidates.append(p)

    instances = []
    seen_indices = set()
    for vms_dir in vms_candidates:
        for entry in sorted(os.listdir(vms_dir)):
            if not entry.startswith("MuMuPlayer-12.0-"):
                continue
            inst_dir = os.path.join(vms_dir, entry)
            if not os.path.isdir(inst_dir):
                continue
            try:
                idx = entry.split("-")[-1]
            except Exception as _e:
                pass
                continue
            if idx in seen_indices:
                continue
            seen_indices.add(idx)

            vm_cfg = {}
            cfg_path = os.path.join(inst_dir, "configs", "vm_config.json")
            if os.path.isfile(cfg_path):
                try:
                    with open(cfg_path, 'r', encoding='utf-8') as f:
                        vm_cfg = json.load(f)
                except Exception as _e:
                    pass
            vm = vm_cfg.get("vm", {})
            instances.append({
                "index": idx,
                "name": f"MuMu-{idx}",
                "running": False,
                "android_running": False,
                "hyperv": vm.get("hyperv", "").lower() == "true",
                "disk_size": 0,
                "cpu": vm.get("cpu", "?"),
                "memory": vm.get("memory", "?"),
                "root": vm.get("root", "").lower() == "true",
            })

    _log_error(f"[MUMU_SCAN] 磁盘扫描发现 {len(instances)} 个实例")
    return instances


def launch_mumu_instance(mumu_manager_path, index):
    """启动单个 MuMu 实例
    返回: (success, message)
    """
    try:
        r = subprocess.run(
            [mumu_manager_path, "control", "--vmindex", str(index), "launch"],
            capture_output=True, text=True, timeout=30,
            cwd=os.path.dirname(mumu_manager_path),
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if r.returncode == 0:
            return True, f"MuMu 实例 {index} 启动成功"
        return False, f"启动失败: {r.stderr or r.stdout or '未知错误'}"
    except subprocess.TimeoutExpired:
        return False, "启动超时"
    except Exception as e:
        return False, f"启动异常: {str(e)}"


def shutdown_mumu_instance(mumu_manager_path, index):
    """关闭单个 MuMu 实例
    返回: (success, message)
    """
    try:
        r = subprocess.run(
            [mumu_manager_path, "control", "--vmindex", str(index), "shutdown"],
            capture_output=True, text=True, timeout=30,
            cwd=os.path.dirname(mumu_manager_path),
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if r.returncode == 0:
            return True, f"MuMu 实例 {index} 已关闭"
        return False, f"关闭失败: {r.stderr or r.stdout or '未知错误'}"
    except subprocess.TimeoutExpired:
        return False, "关闭超时"
    except Exception as e:
        return False, f"关闭异常: {str(e)}"


def _auto_detect_mumu_path():
    """快速检测 MuMu 路径（从进程或文件系统）
    返回: MuMuManager.exe 路径或 None
    """
    try:
        r = subprocess.run(
            ['wmic', 'process', 'where', "name='MuMuManager.exe'", 'get', 'ExecutablePath'],
            capture_output=True, text=True, timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        for line in r.stdout.strip().split('\n'):
            line = line.strip()
            if line.lower().endswith('mumumanager.exe') and os.path.isfile(line):
                return line
    except Exception as _e:
        pass
    info = auto_detect_mumu()
    return info.get("manager_path")


# ============================================================
# MuMu 模拟器 ADB 健康检测与自动恢复
# ============================================================

def _find_adb_path(mumu_manager_path=None):
    """
    自动查找 adb.exe 路径
    查找优先级:
    1. MuMuManager.exe 同目录下的 adb.exe (nx_main/adb.exe)
    2. MuMu 安装目录 shell/ 下的 adb.exe
    3. MuMu 安装目录 shell/adb.exe
    4. %ANDROID_HOME%\\platform-tools\adb.exe
    5. 环境变量 PATH 中的 adb
    返回: adb.exe 完整路径，找不到返回 None
    """
    # 1. MuMuManager.exe 同目录
    if mumu_manager_path and os.path.isfile(mumu_manager_path):
        mgr_dir = os.path.dirname(mumu_manager_path)  # nx_main/
        install_dir = os.path.dirname(mgr_dir)         # MuMu Player 12/
        candidates = [
            os.path.join(mgr_dir, "adb.exe"),              # nx_main/adb.exe
            os.path.join(install_dir, "shell", "adb.exe"), # shell/adb.exe
            os.path.join(install_dir, "adb.exe"),          # 根目录/adb.exe
        ]
        for c in candidates:
            if os.path.isfile(c):
                return c

    # 2. ANDROID_HOME
    android_home = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")
    if android_home:
        adb = os.path.join(android_home, "platform-tools", "adb.exe")
        if os.path.isfile(adb):
            return adb

    # 3. 环境变量 PATH
    which = shutil.which("adb")
    if which:
        return which

    return None


def get_mumu_adb_port(index):
    """
    返回指定 MuMu 12 实例的 ADB 端口号。
    MuMu 12 默认端口规则: 基址 16384 + (index * 32)
    实例 0 → 16384, 实例 1 → 16416, 实例 2 → 16448
    """
    try:
        idx = int(index)
    except (ValueError, TypeError):
        idx = 0
    return 16384 + (idx * 32)


def check_mumu_adb_connection(index, timeout=10, mumu_manager_path=None):
    """
    检查 MuMu 实例是否可以通过 ADB 连接。
    用 adb connect 127.0.0.1:<port> 尝试连接。
    返回: True=连接成功, False=失败或超时
    """
    adb = _find_adb_path(mumu_manager_path)
    if not adb:
        _log_error("[MUMU_ADB] 未找到 adb.exe")
        return False

    port = get_mumu_adb_port(index)
    target = f"127.0.0.1:{port}"

    try:
        # 先尝试 connect
        r = subprocess.run(
            [adb, "connect", target],
            capture_output=True, text=True, timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        output = (r.stdout or "").strip().lower()
        if "connected" in output and "cannot" not in output:
            return True
        return False
    except subprocess.TimeoutExpired:
        return False
    except Exception as e:
        _log_error(f"[MUMU_ADB] check_mumu_adb_connection 异常: {e}")
        return False


def check_mumu_boot_completed(index, timeout=30, mumu_manager_path=None):
    """
    检查 MuMu Android 系统是否启动完成。
    通过 ADB 执行 getprop sys.boot_completed，返回 "1" 表示启动完成。
    支持重试直到超时。
    """
    adb = _find_adb_path(mumu_manager_path)
    if not adb:
        _log_error("[MUMU_ADB] 未找到 adb.exe")
        return False

    port = get_mumu_adb_port(index)
    device = f"127.0.0.1:{port}"
    interval = 3
    elapsed = 0

    while elapsed < timeout:
        try:
            r = subprocess.run(
                [adb, "-s", device, "shell", "getprop", "sys.boot_completed"],
                capture_output=True, text=True, timeout=min(interval, timeout - elapsed),
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            result = (r.stdout or "").strip()
            if result == "1":
                return True
        except subprocess.TimeoutExpired:
            pass
        except Exception as _e:
            pass

        time.sleep(interval)
        elapsed += interval

    return False


def wait_mumu_ready(mumu_manager_path, index, timeout=180, check_interval=5):
    """
    等待 MuMu 实例完全就绪。
    流程: launch_mumu_instance → 轮询检查 ADB + boot_completed
    返回: {"success": bool, "message": str, "elapsed": float, "stage": str}
    stage: "launch_failed" / "process_not_found" / "adb_connecting" / "booting" / "ready" / "timeout"
    """
    start_time = time.time()

    # 第一步: 启动实例
    try:
        ok, msg = launch_mumu_instance(mumu_manager_path, index)
        if not ok:
            return {
                "success": False,
                "message": f"启动失败: {msg}",
                "elapsed": time.time() - start_time,
                "stage": "launch_failed",
            }
    except Exception as e:
        return {
            "success": False,
            "message": f"启动异常: {str(e)}",
            "elapsed": time.time() - start_time,
            "stage": "launch_failed",
        }

    # 第二步: 等待进程和 ADB 就绪
    port = get_mumu_adb_port(index)
    adb = _find_adb_path(mumu_manager_path)
    elapsed = 0.0
    stage = "process_not_found"

    while elapsed < timeout:
        time.sleep(check_interval)
        elapsed = time.time() - start_time

        # 检查 MuMuPlayer 进程
        process_found = False
        try:
            r = subprocess.run(
                ['wmic', 'process', 'where',
                 "name like '%MuMuPlayer%' or name='MuMuVMMHeadless.exe'",
                 'get', 'CommandLine'],
                capture_output=True, text=True, timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            idx_str = str(index)
            for line in r.stdout.splitlines():
                if re.search(r'(?<![a-zA-Z0-9])' + re.escape(idx_str) + r'(?![a-zA-Z0-9])', line):
                    process_found = True
                    break
        except Exception as _e:
            pass

        if not process_found:
            stage = "process_not_found"
            continue

        stage = "adb_connecting"

        # 检查 ADB 连接
        if adb and check_mumu_adb_connection(index, timeout=5, mumu_manager_path=mumu_manager_path):
            stage = "booting"

        # 检查 boot_completed
        try:
            device = f"127.0.0.1:{port}"
            r = subprocess.run(
                [adb, "-s", device, "shell", "getprop", "sys.boot_completed"],
                capture_output=True, text=True, timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            result = (r.stdout or "").strip()
            if result == "1":
                return {
                    "success": True,
                    "message": f"实例 {index} 已就绪",
                    "elapsed": elapsed,
                    "stage": "ready",
                }
        except Exception as _e:
            pass

    return {
        "success": False,
        "message": f"等待超时 ({timeout}s)，最后阶段: {stage}",
        "elapsed": elapsed,
        "stage": "timeout",
    }


def launch_mumu_with_health_check(mumu_manager_path, index, max_retries=3, timeout=180):
    """
    带健康检测的 MuMu 启动。
    - 调用 launch_mumu_instance 启动
    - 调用 wait_mumu_ready 等待就绪
    - 如果超时或失败，自动关闭实例并重试（最多 max_retries 次）
    - 每次重试递增 timeout
    返回: {"success": bool, "message": str, "retries": int}
    """
    last_result = None
    current_timeout = timeout

    for attempt in range(max_retries + 1):
        _log_info(f"[MUMU_HEALTH] 实例 {index} 第 {attempt + 1} 次启动尝试 (timeout={current_timeout}s)")

        last_result = wait_mumu_ready(mumu_manager_path, index, timeout=current_timeout, check_interval=5)

        if last_result["success"]:
            msg = f"实例 {index} 启动成功"
            if attempt > 0:
                msg += f"（第 {attempt + 1} 次尝试成功）"
            return {
                "success": True,
                "message": msg,
                "retries": attempt,
            }

        # 启动失败，关闭实例再重试
        _log_info(f"[MUMU_HEALTH] 实例 {index} 启动失败: {last_result['message']}，准备重试")
        try:
            shutdown_mumu_instance(mumu_manager_path, index)
            time.sleep(3)
        except Exception as e:
            _log_error(f"[MUMU_HEALTH] 关闭实例 {index} 失败: {e}")

        # 递增超时时间
        current_timeout = int(current_timeout * 1.5)
        time.sleep(2)

    return {
        "success": False,
        "message": f"实例 {index} 启动失败（已重试 {max_retries} 次）: {last_result['message'] if last_result else '未知错误'}",
        "retries": max_retries,
    }


def check_all_mumu_instances_health(mumu_manager_path, instances):
    """
    批量检查所有 MuMu 实例健康状态。
    对每个实例检测 ADB 连通性和 boot_completed。
    如果实例标记为运行中但 ADB 不可达，报告异常。
    返回: [{"index": str, "name": str, "running": bool,
             "adb_connected": bool, "boot_completed": bool, "healthy": bool}, ...]
    """
    results = []

    for inst in instances:
        idx = inst.get("index", "0")
        name = inst.get("name", f"MuMu-{idx}")
        running = inst.get("running", False)

        adb_connected = False
        boot_completed = False

        if running:
            adb_connected = check_mumu_adb_connection(idx, timeout=5)
            if adb_connected:
                boot_completed = check_mumu_boot_completed(idx, timeout=5)

        healthy = running and adb_connected and boot_completed

        results.append({
            "index": idx,
            "name": name,
            "running": running,
            "adb_connected": adb_connected,
            "boot_completed": boot_completed,
            "healthy": healthy,
        })

    return results


# ============================================================
# 桌面快捷方式检测（最快路径）
# ============================================================

def _resolve_lnk_targets_batch(lnk_paths):
    """批量解析 .lnk 快捷方式的 TargetPath（单次 PowerShell 调用）
    返回: {lnk_path: target_path, ...}
    """
    if not lnk_paths:
        return {}
    results = {}
    # 用 JSON 安全传递路径列表，避免单引号注入
    paths_json = json.dumps(lnk_paths, ensure_ascii=False)
    cmd = (
        "$s = New-Object -ComObject WScript.Shell; "
        f"$paths = {paths_json} | ConvertFrom-Json; "
        "foreach ($p in $paths) { try { $sc = $s.CreateShortcut($p); "
        "if ($sc.TargetPath) { Write-Output ($p + '|' + $sc.TargetPath) } } catch {} }"
    )
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", cmd],
            capture_output=True, text=True, timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        if r.returncode == 0:
            for line in r.stdout.strip().splitlines():
                if '|' in line:
                    lnk, target = line.split('|', 1)
                    lnk = lnk.strip()
                    target = target.strip()
                    if target:
                        results[lnk] = target
    except Exception as _e:
        pass
    return results


_LD_KEYWORDS = ["dnplayer", "dnconsole", "ldplayer", "leidian", "雷电"]
_MUMU_KEYWORDS = ["mumumanager", "mumuplayer", "mumu player", "mumu模拟器", "网易", "mumu"]
_LNK_NAME_KEYWORDS = [
    "ldplayer", "leidian", "雷电", "mumu", "dnplayer", "dnconsole",
    "模拟器", "emulator",
]


def find_emulator_from_shortcuts():
    """从桌面和开始菜单快捷方式查找 LDPlayer 和 MuMu 路径
    返回: {"ld_path": str|None, "mumu_manager": str|None}
    """
    result = {"ld_path": None, "mumu_manager": None}

    scan_dirs = set()
    user_desktop = os.path.expandvars(r"%USERPROFILE%\Desktop")
    if os.path.isdir(user_desktop):
        scan_dirs.add(user_desktop)
    public_desktop = os.path.expandvars(r"%PUBLIC%\Desktop")
    if os.path.isdir(public_desktop):
        scan_dirs.add(public_desktop)
    start_menu = os.path.expandvars(r"%APPDATA%\Microsoft\Windows\Start Menu")
    if os.path.isdir(start_menu):
        scan_dirs.add(start_menu)
    common_start = os.path.expandvars(r"%PROGRAMDATA%\Microsoft\Windows\Start Menu")
    if os.path.isdir(common_start):
        scan_dirs.add(common_start)

    candidate_lnks = []
    for scan_dir in scan_dirs:
        for root, _, files in os.walk(scan_dir):
            for fname in files:
                if not fname.lower().endswith(".lnk"):
                    continue
                fname_lower = fname.lower()
                if any(kw in fname_lower for kw in _LNK_NAME_KEYWORDS):
                    candidate_lnks.append(os.path.join(root, fname))

    if candidate_lnks:
        targets = _resolve_lnk_targets_batch(candidate_lnks)
        for _, target in targets.items():
            lower = target.lower()
            if not result["ld_path"]:
                if any(kw in lower for kw in _LD_KEYWORDS):
                    parent = os.path.dirname(target)
                    if os.path.isfile(os.path.join(parent, "dnconsole.exe")):
                        result["ld_path"] = parent
            if not result["mumu_manager"]:
                if "mumumanager" in lower:
                    if os.path.isfile(target) and "MuMuManager" in target:
                        result["mumu_manager"] = target
                    else:
                        parent = os.path.dirname(target)
                        mgr = os.path.join(parent, "MuMuManager.exe")
                        if os.path.isfile(mgr):
                            result["mumu_manager"] = mgr
                        nx_mgr = os.path.join(parent, "nx_main", "MuMuManager.exe")
                        if os.path.isfile(nx_mgr):
                            result["mumu_manager"] = nx_mgr
                elif any(kw in lower for kw in _MUMU_KEYWORDS):
                    parent = os.path.dirname(target)
                    for try_path in [
                        os.path.join(parent, "MuMuManager.exe"),
                        os.path.join(parent, "nx_main", "MuMuManager.exe"),
                    ]:
                        if os.path.isfile(try_path):
                            result["mumu_manager"] = try_path
                            break
            if result["ld_path"] and result["mumu_manager"]:
                return result

    return result


# ============================================================
# MuMu 定时健康巡检
# ============================================================

def _check_mumu_port_open(index, timeout=3):
    """检查 MuMu 实例的 ADB 端口是否已监听（不需要 adb.exe）。
    用 Python socket 直连 127.0.0.1:<port>，端口开放即认为实例存活。
    返回: True=端口开放, False=超时或不可达
    """
    port = get_mumu_adb_port(index)
    import socket as _socket
    try:
        s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        s.settimeout(timeout)
        result = s.connect_ex(("127.0.0.1", port))
        s.close()
        return result == 0
    except Exception:
        return False


def start_mumu_health_monitor(mumu_manager_path, index, check_interval=1200, shutdown_time=None, on_status=None, on_confirm_restart=None):
    """
    启动 MuMu 实例的定时健康巡检线程。

    参数:
        mumu_manager_path: MuMuManager.exe 路径
        index: 实例索引
        check_interval: 巡检间隔（秒），默认 1200（20 分钟）
        shutdown_time: 可选，计划关机时间戳（time.time()），到达此时间后停止巡检
        on_status: 可选回调函数，接收 (index, healthy, message) 参数
        on_confirm_restart: 可选回调函数，接收 (index) 参数，返回 True=确认重启，False=跳过

    返回: {"thread": threading.Thread, "stop_event": threading.Event}
    """
    stop_event = threading.Event()

    def _check_process():
        """检查 MuMu 进程是否存活（通过 wmic 命令行匹配索引号）"""
        try:
            r = subprocess.run(
                ['wmic', 'process', 'where',
                 "name like '%MuMuPlayer%' or name='MuMuVMMHeadless.exe'",
                 'get', 'CommandLine'],
                capture_output=True, text=True, timeout=8,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            idx_str = str(index)
            for line in r.stdout.splitlines():
                if re.search(r'(?<![a-zA-Z0-9])' + re.escape(idx_str) + r'(?![a-zA-Z0-9])', line):
                    return True
        except Exception:
            pass
        return False

    def _is_instance_alive():
        """综合判断实例是否存活：端口检测为主，进程检测为辅。"""
        # 先看端口：不需要 adb.exe，只要端口开放就认为存活
        if _check_mumu_port_open(index, timeout=3):
            return True
        # 端口不通时再看进程（启动初期端口可能还未绑定）
        return _check_process()

    def _monitor_loop():
        # 实例已由调用方启动，这里只做定时巡检
        if on_status:
            on_status(index, True, "开始定时巡检")

        # 重启控制：最多连续重启 3 次，每次间隔至少 60 秒
        restart_count = 0
        max_restarts = 3
        min_cooldown = 60
        last_restart_time = 0

        while not stop_event.is_set():
            # 检查是否到达关机时间（提前 1 分钟停止巡检）
            if shutdown_time is not None:
                remaining = shutdown_time - time.time()
                if remaining <= 60:
                    if on_status:
                        on_status(index, True, "接近关机时间，停止巡检")
                    return

            # 等待一个巡检周期（每秒检查 stop_event 以便及时响应停止信号）
            for _ in range(check_interval):
                if stop_event.is_set():
                    return
                time.sleep(1)

            # 自动点击 MuMu 错误弹窗的「重启」按钮
            auto_restart_mumu_on_error()

            # 执行健康检查：端口 + 进程双重判断，不需要 adb.exe
            alive = _is_instance_alive()
            if not alive:
                # 检查重启上限和冷却时间
                now = time.time()
                if restart_count >= max_restarts or (last_restart_time > 0 and now - last_restart_time < min_cooldown):
                    if on_status:
                        on_status(index, False, f"实例失联（已重启 {restart_count} 次，跳过本次）")
                    continue
                # 需要用户确认后才重启
                if on_confirm_restart is not None:
                    confirmed = on_confirm_restart(index)
                    if not confirmed:
                        if on_status:
                            on_status(index, False, f"实例失联（用户跳过，实例 {index}）")
                        continue
                restart_count += 1
                last_restart_time = now
                if on_status:
                    on_status(index, False, "实例失联，准备重启...")
                # 关闭后重新启动
                shutdown_mumu_instance(mumu_manager_path, index)
                time.sleep(3)
                launch_result = launch_mumu_with_health_check(mumu_manager_path, index)
                if launch_result["success"]:
                    restart_count = 0
                    last_restart_time = 0
                    if on_status:
                        on_status(index, True, f"重启成功（第 {launch_result['retries'] + 1} 次尝试）")
                else:
                    if on_status:
                        on_status(index, False, f"重启失败: {launch_result['message']}")
            else:
                # 健康
                restart_count = 0
                last_restart_time = 0
                if on_status:
                    on_status(index, True, "巡检正常")

    thread = threading.Thread(target=_monitor_loop, daemon=True, name=f"mumu-health-{index}")
    thread.start()

    return {"thread": thread, "stop_event": stop_event}


def stop_mumu_health_monitor(monitor):
    """
    停止健康巡检线程。
    设置 stop_event 并等待线程结束（超时 5 秒）。
    """
    if not monitor:
        return
    stop_event = monitor.get("stop_event")
    thread = monitor.get("thread")
    if stop_event:
        stop_event.set()
    if thread and thread.is_alive():
        thread.join(timeout=360)  # 等待最多 6 分钟（健康检测可能有长时间阻塞）


def _find_mumu_error_dialog():
    """查找 MuMu 模拟器的错误/卡启动弹窗窗口。
    扫描两类：1)标题匹配关键词的顶层窗口 2)标准对话框(#32770)含运行终止/重启文字
    返回: [(hwnd, title), ...] 或 None
    """
    user32 = ctypes.windll.user32
    result = []
    error_keywords = ["模拟器", "启动失败", "无响应", "重启", "连接超时",
                      "mumu", "emu", "failed", "timeout", "not responding",
                      "运行终止", "异常终止", "已停止"]

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def _enum_callback(hwnd, _):
        length = user32.GetWindowTextLengthW(hwnd)
        is_visible = user32.IsWindowVisible(hwnd)
        if not is_visible:
            return True

        # 方式1：检查窗口标题
        if length > 0:
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value.lower()
            has_mumu = any(kw in title for kw in ["mumu", "模拟器", "emu", "双开", "multi"])
            has_error = any(kw in title for kw in error_keywords)
            if has_mumu or has_error:
                result.append((hwnd, buf.value))
                return True

        # 方式2：检查标准对话框类（#32770）的子控件文字
        class_buf = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, class_buf, 64)
        class_name = class_buf.value
        if class_name == "#32770":  # 标准对话框
            # 枚举子控件找匹配文字
            child = user32.FindWindowExW(hwnd, None, "Static", None)
            while child:
                clen = user32.GetWindowTextLengthW(child)
                if clen > 0:
                    cbuf = ctypes.create_unicode_buffer(clen + 1)
                    user32.GetWindowTextW(child, cbuf, clen + 1)
                    ctext = cbuf.value.lower()
                    if any(kw in ctext for kw in ["运行终止", "重启", "启动失败", "显卡驱动"]):
                        result.append((hwnd, f"#32770: {cbuf.value[:60]}"))
                        return True
                child = user32.FindWindowExW(hwnd, child, "Static", None)

        return True

    try:
        user32.EnumWindows(_enum_callback, 0)
    except Exception as _e:
        pass
    return result if result else None


def _click_mumu_restart(dialog_hwnd):
    """在 MuMu 错误弹窗中查找并点击「重启」按钮。
    返回 True 表示已点击，False 表示没找到。
    """
    user32 = ctypes.windll.user32
    BM_CLICK = 0x00F5
    child = None
    while True:
        child = user32.FindWindowExW(dialog_hwnd, child, "Button", None)
        if not child:
            break
        clen = user32.GetWindowTextLengthW(child)
        if clen > 0:
            buf = ctypes.create_unicode_buffer(clen + 1)
            user32.GetWindowTextW(child, buf, clen + 1)
            if "重启" in buf.value:
                user32.SendMessageW(child, BM_CLICK, 0, 0)
                return True
    return False


_auto_click_restart_count = 0
_AUTO_CLICK_RESTART_MAX = 10


def auto_restart_mumu_on_error():
    """扫描 MuMu 错误弹窗，发现后自动点击重启。
    每个会话最多点击 _AUTO_CLICK_RESTART_MAX 次，防止无限循环。
    返回已处理的弹窗数。
    """
    global _auto_click_restart_count
    if _auto_click_restart_count >= _AUTO_CLICK_RESTART_MAX:
        return 0
    dialogs = _find_mumu_error_dialog()
    if not dialogs:
        return 0
    count = 0
    for hwnd, _ in dialogs:
        if _auto_click_restart_count >= _AUTO_CLICK_RESTART_MAX:
            break
        if _click_mumu_restart(hwnd):
            count += 1
            _auto_click_restart_count += 1
    return count


# deprecated: Windows 层弹窗经 EnumWindows 已覆盖，ADB uiautomator 对此场景无效
def adb_tap_restart_button(adb_port):
    """通过 ADB uiautomator 在 Android 界面查找并点击「立即重启」按钮。
    返回 True 表示已点击，False 表示没找到按钮。
    """
    adb_cmd = ["adb", "-s", f"127.0.0.1:{adb_port}"]
    try:
        subprocess.run(adb_cmd + ["shell", "uiautomator", "dump", "/sdcard/ui.xml"],
                       timeout=10, capture_output=True)
        r = subprocess.run(adb_cmd + ["shell", "cat", "/sdcard/ui.xml"],
                           timeout=10, capture_output=True)
        raw = r.stdout.decode('utf-8', errors='replace')
    except Exception:
        return False

    import re
    for m in re.finditer(r'text="([^"]*)"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', raw):
        text = m.group(1)
        if "\u91cd\u542f" in text:  # 重启
            x1, y1, x2, y2 = int(m.group(2)), int(m.group(3)), int(m.group(4)), int(m.group(5))
            cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
            try:
                subprocess.run(adb_cmd + ["shell", "input", "tap", str(cx), str(cy)],
                               timeout=5, capture_output=True)
                return True
            except Exception:
                return False
    return False


def adb_detect_and_restart(index):
    """检测 MuMu 实例是否有「运行终止」弹窗，有则自动点击重启。"""
    port = get_mumu_adb_port(index)
    if not port:
        return False
    return adb_tap_restart_button(port)

