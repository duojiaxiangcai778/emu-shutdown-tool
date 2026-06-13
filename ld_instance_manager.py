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
import winreg
import ctypes
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
            for val_name in ['InstallPath', 'Path', 'InstallDir', '']:
                try:
                    val, _ = winreg.QueryValueEx(key, val_name)
                    if val and os.path.isdir(val) and os.path.isfile(os.path.join(val, 'dnconsole.exe')):
                        winreg.CloseKey(key)
                        return val
                except FileNotFoundError:
                    continue
            winreg.CloseKey(key)
        except Exception:
            continue
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
    每个实例: {
        "name": "leidian0",
        "config_path": "D:\\...\\vms\\config\\leidian0.config",
        "settings": { ... },  # 解析后的设置
        "running": False,
    }
    """
    instances = []
    if not vms_config_dir or not os.path.isdir(vms_config_dir):
        return instances

    for fname in os.listdir(vms_config_dir):
        if fname.startswith('leidian') and fname.endswith('.config'):
            name = fname.replace('.config', '')
            # 跳过全局配置文件（leidians.config 不是实例）
            if name == 'leidians':
                continue
            config_path = os.path.join(vms_config_dir, fname)
            settings = read_instance_config(config_path)
            instances.append({
                "name": name,
                "config_path": config_path,
                "settings": settings,
                "running": False,
            })

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
                if inst['name'] in line:
                    running_names.add(inst['name'])
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

def save_snapshot(vms_config_dir, multiplayer_config_dir, snapshot_base_dir):
    """
    保存配置快照
    返回: (快照目录路径, 消息)
    """
    if not vms_config_dir or not os.path.isdir(vms_config_dir):
        return None, "未找到实例配置目录"

    timestamp = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
    snap_dir = os.path.join(snapshot_base_dir, timestamp)
    try:
        os.makedirs(snap_dir, exist_ok=True)

        # 复制实例配置
        count = 0
        for fname in os.listdir(vms_config_dir):
            if fname.endswith('.config'):
                src = os.path.join(vms_config_dir, fname)
                shutil.copy2(src, os.path.join(snap_dir, fname))
                count += 1

        # 复制全局配置（多开器的 leidians.config）
        if multiplayer_config_dir and os.path.isdir(multiplayer_config_dir):
            global_cfg = os.path.join(multiplayer_config_dir, 'leidians.config')
            if os.path.isfile(global_cfg):
                shutil.copy2(global_cfg, os.path.join(snap_dir, 'leidians.config'))

        # 写入元信息
        meta = {
            "timestamp": timestamp,
            "instance_count": count,
            "vms_config_dir": vms_config_dir,
            "multiplayer_config_dir": multiplayer_config_dir,
        }
        with open(os.path.join(snap_dir, 'snapshot_meta.json'), 'w', encoding='utf-8') as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

        return snap_dir, f"已保存 {count} 个实例配置到 {snap_dir}"
    except Exception as e:
        return None, f"保存失败: {str(e)}"


def restore_snapshot(snapshot_dir, vms_config_dir, multiplayer_config_dir):
    """
    恢复配置快照
    返回: (成功数, 消息)
    """
    if not os.path.isdir(snapshot_dir):
        return 0, "快照目录不存在"
    if not vms_config_dir or not os.path.isdir(vms_config_dir):
        return 0, "实例配置目录无效"

    restored = 0
    errors = []

    # 恢复实例配置
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

    msg = f"已恢复 {restored} 个实例配置"
    if errors:
        msg += f"，{len(errors)} 个失败: " + "; ".join(errors)
    return restored, msg


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
            snapshots.append({
                "name": name,
                "path": snap_dir,
                "instance_count": meta.get("instance_count", 0),
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

    # 尝试方式1: ShellExecuteW (runas 提权)
    try:
        h_instance = shell32.ShellExecuteW(
            None, "runas", dnconsole_path, params, directory, 0  # SW_HIDE
        )
        # 返回值 > 32 表示成功 (HINSTANCE cast to int)
        if ctypes.cast(h_instance, ctypes.c_int).value > 32:
            time.sleep(3)
            return True, f"{instance_name} 启动成功"
        err_code = ctypes.cast(h_instance, ctypes.c_int).value
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
                msg = f"启动失败，dnconsole 返回帮助信息（--index {index} 参数可能不被支持）"
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
                msg = f"启动失败，dnconsole 返回帮助信息（--index {index} 参数可能不被支持）"
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



