#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
雷电模拟器实例管理模块
- 路径自动探测
- 实例扫描与设置读写
- 配置快照保存/恢复
- 间隔启动
"""

import os
import sys
import json
import time
import shutil
import subprocess
import threading
import winreg
import ctypes
import tempfile
from datetime import datetime
from pathlib import Path


# ============================================================
# 路径自动探测
# ============================================================

def auto_detect_paths():
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
    result = {
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

    # 策略2: 检查 pathconfig.ini（多开器配置）
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

    return result


def _find_ld_from_process():
    """从运行中的 LDPlayer 进程获取安装路径"""
    try:
        r = subprocess.run(
            ['wmic', 'process', 'where',
             "name='dnplayer.exe' or name='ldplayer.exe' or name='dnconsole.exe'",
             'get', 'ExecutablePath'],
            capture_output=True, text=True, timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        for line in r.stdout.strip().split('\n'):
            line = line.strip()
            if line.lower().endswith(('.exe',)) and os.path.isfile(line):
                return os.path.dirname(line)
    except Exception:
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
    except Exception:
        pass
    return None


def _find_multiplayer_from_pathconfig():
    """从 pathconfig.ini 获取多开器路径"""
    for base in _get_all_drives():
        for name in ['ldmutiplayer', 'LDPlayer', 'ldplayer']:
            for sub in ['', name]:
                pf = os.path.join(base, name, sub, 'pathconfig.ini') if sub else os.path.join(base, name, 'pathconfig.ini')
                pf = os.path.normpath(pf)
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
                    except Exception:
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
    ]
    for hkey, subkey in reg_paths:
        try:
            key = winreg.OpenKey(hkey, subkey, 0, winreg.KEY_READ)
        except Exception:
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
            except Exception:
                pass
    return None


def _scan_common_paths():
    """扫描常见安装路径"""
    candidates = []
    for drive in _get_all_drives():
        for name in ['LDPlayer9', 'LDPlayer8', 'LDPlayer']:
            candidates.append(os.path.join(drive, name))
            candidates.append(os.path.join(drive, 'Program Files', name))
            candidates.append(os.path.join(drive, 'Program Files (x86)', name))
            candidates.append(os.path.join(drive, 'E', name))
            candidates.append(os.path.join(drive, 'Software', name))
            # 多开器同目录
            for mp in ['ldmutiplayer', 'LDPlayer', 'ldplayer']:
                candidates.append(os.path.join(drive, mp))
                candidates.append(os.path.join(drive, 'E', mp))

    for path in candidates:
        path = os.path.normpath(path)
        if os.path.isfile(os.path.join(path, 'dnconsole.exe')):
            return path
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
        except Exception:
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
    except Exception:
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
    except Exception:
        pass

    # 按编号排序
    instances.sort(key=lambda x: x['name'])
    return instances


def check_running_instances(instances, dnconsole_path=None):
    """检查哪些实例正在运行
    优先使用 dnconsole.exe list2 / runninglist 获得准确结果
    回退到 wmic 方式
    """
    running_names = set()

    if dnconsole_path and os.path.isfile(dnconsole_path):
        try:
            # 方式1: 使用 dnconsole.exe list2
            r = subprocess.run(
                [dnconsole_path, 'list2'],
                capture_output=True, text=True, timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW
            )
            if r.returncode == 0 and r.stdout.strip():
                for line in r.stdout.strip().splitlines():
                    parts = line.split(',')
                    if len(parts) >= 6:
                        idx = parts[0].strip()
                        name = parts[1].strip()
                        # 第6列(索引5)表示运行状态: 1=运行中
                        status = parts[5].strip()
                        if status == '1' and name.startswith('leidian'):
                            running_names.add(name)
                if running_names:
                    for inst in instances:
                        inst['running'] = inst['name'] in running_names
                    return

            # 方式2: 使用 dnconsole.exe runninglist
            r2 = subprocess.run(
                [dnconsole_path, 'runninglist'],
                capture_output=True, text=True, timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW
            )
            if r2.returncode == 0 and r2.stdout.strip():
                for line in r2.stdout.strip().splitlines():
                    line = line.strip()
                    if line.startswith('leidian'):
                        running_names.add(line)
                if running_names:
                    for inst in instances:
                        inst['running'] = inst['name'] in running_names
                    return
        except Exception:
            pass

    # 回退方式: wmic 通过命令行检测
    import re
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
    except Exception:
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
        return None, "未找到实例配置目录"

    timestamp = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
    snap_dir = os.path.join(snapshot_base_dir, timestamp)
    _log_error(f"[SNAP] save_snapshot 开始: vms_config_dir={vms_config_dir}, mumu_vms_dir={mumu_vms_dir}")
    try:
        os.makedirs(snap_dir, exist_ok=True)

        # ---- 备份 LDPlayer 实例配置 ----
        count = 0
        for fname in os.listdir(vms_config_dir):
            if fname.endswith('.config'):
                src = os.path.join(vms_config_dir, fname)
                shutil.copy2(src, os.path.join(snap_dir, fname))
                count += 1
        _log_error(f"[SNAP] LDPlayer config 文件找到 {count} 个")
        if count == 0:
            _log_error(f"[SNAP] vms_config_dir 内容: {os.listdir(vms_config_dir)}")

        # 复制全局配置（多开器的 leidians.config）
        if multiplayer_config_dir and os.path.isdir(multiplayer_config_dir):
            global_cfg = os.path.join(multiplayer_config_dir, 'leidians.config')
            if os.path.isfile(global_cfg):
                shutil.copy2(global_cfg, os.path.join(snap_dir, 'leidians.config'))

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
                    except Exception:
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

    # 恢复 LDPlayer 实例配置
    for fname in os.listdir(snapshot_dir):
        if fname.endswith('.config') and fname.startswith('leidian'):
            src = os.path.join(snapshot_dir, fname)
            dst = os.path.join(vms_config_dir, fname)
            try:
                shutil.copy2(src, dst)
                restored += 1
            except Exception as e:
                errors.append(f"{fname}: {e}")

    # 恢复全局配置
    global_cfg = os.path.join(snapshot_dir, 'leidians.config')
    if os.path.isfile(global_cfg) and multiplayer_config_dir and os.path.isdir(multiplayer_config_dir):
        dst = os.path.join(multiplayer_config_dir, 'leidians.config')
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
                except Exception:
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
    import re
    match = re.search(r'(\d+)$', instance_name)
    if match:
        return int(match.group(1))
    return None


def launch_instance(dnconsole_path, instance_name, timeout=30):
    """
    启动单个实例（ShellExecuteW 提权方式）
    注意: LDPlayer 9.5.13.0 中 launch --name 参数无效，必须使用 --index
    返回: (成功, 消息)
    """
    if not os.path.isfile(dnconsole_path):
        return False, f"dnconsole.exe 不存在: {dnconsole_path}"

    # 从实例名中提取索引
    index = _extract_index(instance_name)
    if index is not None:
        params = f'launch --index {index}'
        launch_args = ['launch', '--index', str(index)]
    else:
        # 如果提取失败（实例名不规范），回退到 --name 方式
        params = f'launch --name {instance_name}'
        launch_args = ['launch', '--name', instance_name]

    shell32 = ctypes.windll.shell32
    # 明确声明 argtypes 确保 64 位 Python 正确传参
    shell32.ShellExecuteW.argtypes = [
        ctypes.c_void_p,   # HWND hwnd
        ctypes.c_wchar_p,  # LPCWSTR lpOperation
        ctypes.c_wchar_p,  # LPCWSTR lpFile
        ctypes.c_wchar_p,  # LPCWSTR lpParameters
        ctypes.c_wchar_p,  # LPCWSTR lpDirectory
        ctypes.c_int,      # INT nShowCmd
    ]
    shell32.ShellExecuteW.restype = ctypes.c_void_p

    directory = os.path.dirname(dnconsole_path)
    err_code = 0

    # 尝试方式1: ShellExecuteW (runas 提权)
    try:
        h_instance = shell32.ShellExecuteW(
            None, "runas", dnconsole_path, params, directory, 0  # SW_HIDE
        )
        # 返回值 > 32 表示成功 (HINSTANCE 是指针大小，直接用 .value 避免 64 位截断)
        h_val = h_instance.value if h_instance else 0
        if h_val and h_val > 32:
            time.sleep(3)
            return True, f"{instance_name} 启动成功"
        err_code = h_val or 0
        # 尝试方式2: subprocess.run (对已提权的程序有效)
        try:
            r = subprocess.run(
                [dnconsole_path] + launch_args,
                capture_output=True, text=True, timeout=timeout,
                creationflags=subprocess.CREATE_NO_WINDOW
            )
            if r.returncode == 0:
                time.sleep(3)
                return True, f"{instance_name} 启动成功"
            raw = (r.stdout.strip() or r.stderr.strip() or "")
            # 如果输出包含帮助文本（启动参数错误），给出友好提示
            if "Usage:" in raw or "Commands :" in raw or "dnconsole <command>" in raw.lower():
                msg = f"启动失败，dnconsole 返回帮助信息（--index 参数可能不被支持，实例: {instance_name}）"
            else:
                msg = raw or f"ShellExecuteW返回{err_code}"
        except Exception:
            msg = f"启动失败 (err={err_code})"
        return False, f"{instance_name} 失败: {msg}"
    except Exception as e:
        # 最终回退: subprocess.run
        try:
            r = subprocess.run(
                [dnconsole_path] + launch_args,
                capture_output=True, text=True, timeout=timeout,
                creationflags=subprocess.CREATE_NO_WINDOW
            )
            if r.returncode == 0:
                time.sleep(3)
                return True, f"{instance_name} 启动成功"
            raw2 = (r.stdout.strip() or r.stderr.strip() or str(e))
            # 同上，过滤帮助文本
            if "Usage:" in raw2 or "Commands :" in raw2 or "dnconsole <command>" in raw2.lower():
                msg = f"启动失败，dnconsole 返回帮助信息（--index 参数可能不被支持，实例: {instance_name}）"
            else:
                msg = raw2
        except subprocess.TimeoutExpired:
            msg = f"启动超时 ({timeout}s)"
        except Exception as e2:
            msg = str(e2)
        return False, f"{instance_name} 失败: {msg}"


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
    except Exception:
        pass

_ensure_snapshot_dir()


def load_tool_config():
    """加载工具配置"""
    try:
        if os.path.isfile(TOOL_CONFIG_FILE):
            with open(TOOL_CONFIG_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def save_tool_config(config):
    """保存工具配置"""
    try:
        with open(TOOL_CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
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
    except Exception:
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
    except Exception:
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
    except Exception:
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
    else:
        # 进一步检查配置文件
        ld_config = os.path.join(found_dir, "ld_config.ini")
        if os.path.isfile(ld_config):
            try:
                with open(ld_config, "r", encoding="utf-8") as f:
                    content = f.read()
                if "hyperv" in content.lower():
                    return {"version": "hyperv", "detail": "Hyper-V 兼容版（配置含 hyperv 标识）"}
            except Exception:
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
        log_path = os.path.join(_dir, "emu_tool.log")
        if exc_info is None:
            exc_info = traceback.format_exc()
        elif isinstance(exc_info, BaseException):
            exc_info = f"{type(exc_info).__name__}: {exc_info}"
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(log_path, 'a', encoding='utf-8') as f:
            f.write(f"[{ts}] [{context}]\n{exc_info}\n---\n")
    except Exception:
        pass

# 常见 MuMu 安装路径
MUMU_PATHS_CANDIDATES = [
    r"C:\Program Files\Netease\MuMuPlayer-12.0\shell\MuMuManager.exe",
    r"C:\Program Files\Netease\MuMuPlayer-12.0\MuMuManager.exe",
    r"C:\Program Files\MuMuPlayer-12.0\shell\MuMuManager.exe",
    r"C:\Program Files (x86)\Netease\MuMuPlayer-12.0\shell\MuMuManager.exe",
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

    # 2. 在 MuMuPlayer 常见路径深度搜索（含子目录）
    search_roots = []
    for drive in "CDEFGHIJKLMNOPQRSTUVWXYZ":
        base = f"{drive}:\\"
        if not os.path.exists(base):
            continue
        # 盘符根目录
        search_roots.append(base)
        # 常见子目录
        for sub in ["E", "Program Files", "Program Files (x86)", "Programs",
                     "Software", "Games", "Tools"]:
            sp = os.path.join(base, sub)
            if os.path.isdir(sp):
                search_roots.append(sp)

    for search_root in search_roots:
        for name in ["MuMu Player 12", "MuMuPlayer-12.0", "MuMuPlayer", "Netease"]:
            candidate = os.path.join(search_root, name)
            if os.path.isdir(candidate):
                for root, dirs, files in os.walk(candidate):
                    if "MuMuManager.exe" in files:
                        fp = os.path.join(root, "MuMuManager.exe")
                        result["manager_path"] = fp
                        result["install_dir"] = candidate
                        result["found"] = True
                        cli = os.path.join(os.path.dirname(root), "mumu-cli.exe")
                        if os.path.isfile(cli):
                            result["cli_path"] = cli
                        return result
                    if root.count(os.sep) - candidate.count(os.sep) >= 3:
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
            )
            raw = (r.stdout or b"").decode('utf-8', errors='replace').strip()
            if raw:
                return json.loads(raw)
        except Exception:
            pass

        # 尝试 B：cmd /c 中转
        try:
            r = subprocess.run(
                ["cmd.exe", "/c", f'"{mumu_manager_path}" info --vmindex all'],
                capture_output=True, timeout=15, cwd=mgr_dir,
            )
            raw = (r.stdout or b"").decode('utf-8', errors='replace').strip()
            if raw:
                return json.loads(raw)
        except Exception:
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
        except Exception:
            pass
        finally:
            try:
                if os.path.isfile(bat_path): os.unlink(bat_path)
            except Exception:
                pass
            try:
                if os.path.isfile(out_file): os.unlink(out_file)
            except Exception:
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
                        except Exception:
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
            except Exception:
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
                except Exception:
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
    except Exception:
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
    4. %ANDROID_HOME%\platform-tools\adb.exe
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


def check_mumu_adb_connection(index, timeout=10):
    """
    检查 MuMu 实例是否可以通过 ADB 连接。
    用 adb connect 127.0.0.1:<port> 尝试连接。
    返回: True=连接成功, False=失败或超时
    """
    adb = _find_adb_path()
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


def check_mumu_boot_completed(index, timeout=30):
    """
    检查 MuMu Android 系统是否启动完成。
    通过 ADB 执行 getprop sys.boot_completed，返回 "1" 表示启动完成。
    支持重试直到超时。
    """
    adb = _find_adb_path()
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
        except Exception:
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
    adb = _find_adb_path()
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
            import re
            idx_str = str(index)
            for line in r.stdout.splitlines():
                if re.search(r'(?<![a-zA-Z0-9])' + re.escape(idx_str) + r'(?![a-zA-Z0-9])', line):
                    process_found = True
                    break
        except Exception:
            pass

        if not process_found:
            stage = "process_not_found"
            continue

        stage = "adb_connecting"

        # 检查 ADB 连接
        if adb and check_mumu_adb_connection(index, timeout=5):
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
            except Exception:
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
        _log_error(f"[MUMU_HEALTH] 实例 {index} 第 {attempt + 1} 次启动尝试 (timeout={current_timeout}s)")

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
        _log_error(f"[MUMU_HEALTH] 实例 {index} 启动失败: {last_result['message']}，准备重试")
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

        healthy = running == adb_connected == boot_completed
        # 运行中但 ADB 不通 或 boot 未完成 = 不健康
        if running and (not adb_connected or not boot_completed):
            healthy = False

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
    # 将路径列表转为 PowerShell 数组
    items = "; ".join(f"'{p}'" for p in lnk_paths)
    cmd = (
        "$s = New-Object -ComObject WScript.Shell; "
        f"$paths = @({items}); "
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
    except Exception:
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
        for root, dirs, files in os.walk(scan_dir):
            for fname in files:
                if not fname.lower().endswith(".lnk"):
                    continue
                fname_lower = fname.lower()
                if any(kw in fname_lower for kw in _LNK_NAME_KEYWORDS):
                    candidate_lnks.append(os.path.join(root, fname))

    if candidate_lnks:
        targets = _resolve_lnk_targets_batch(candidate_lnks)
        for lnk_path, target in targets.items():
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

def start_mumu_health_monitor(mumu_manager_path, index, check_interval=1200, shutdown_time=None, on_status=None):
    """
    启动 MuMu 实例的定时健康巡检线程。

    参数:
        mumu_manager_path: MuMuManager.exe 路径
        index: 实例索引
        check_interval: 巡检间隔（秒），默认 1200（20 分钟）
        shutdown_time: 可选，计划关机时间戳（time.time()），到达此时间后停止巡检
        on_status: 可选回调函数，接收 (index, healthy, message) 参数

    返回: {"thread": threading.Thread, "stop_event": threading.Event}
    """
    stop_event = threading.Event()

    def _monitor_loop():
        # 第一次巡检：确保启动成功
        if on_status:
            on_status(index, False, "正在启动实例...")
        launch_result = launch_mumu_with_health_check(mumu_manager_path, index)
        if not launch_result["success"]:
            if on_status:
                on_status(index, False, f"启动失败: {launch_result['message']}")
            return
        if on_status:
            on_status(index, True, "启动成功，开始定时巡检")

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

            # 执行健康检查
            adb_ok = check_mumu_adb_connection(index, timeout=10)
            if not adb_ok:
                if on_status:
                    on_status(index, False, "ADB 断连，准备重启...")
                # 关闭后重新启动
                shutdown_mumu_instance(mumu_manager_path, index)
                time.sleep(3)
                launch_result = launch_mumu_with_health_check(mumu_manager_path, index)
                if launch_result["success"]:
                    if on_status:
                        on_status(index, True, f"重启成功（第 {launch_result['retries'] + 1} 次尝试）")
                else:
                    if on_status:
                        on_status(index, False, f"重启失败: {launch_result['message']}")
                continue

            boot_ok = check_mumu_boot_completed(index, timeout=15)
            if not boot_ok:
                if on_status:
                    on_status(index, False, "boot_completed 丢失，准备重启...")
                shutdown_mumu_instance(mumu_manager_path, index)
                time.sleep(3)
                launch_result = launch_mumu_with_health_check(mumu_manager_path, index)
                if launch_result["success"]:
                    if on_status:
                        on_status(index, True, f"重启成功（第 {launch_result['retries'] + 1} 次尝试）")
                else:
                    if on_status:
                        on_status(index, False, f"重启失败: {launch_result['message']}")
                continue

            # 健康
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
        thread.join(timeout=5)



