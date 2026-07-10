#!/usr/bin/env python3
"""Atomic application of all 3 fixes - NO patch tool used"""
import py_compile

# ======== Fix 1: auto_detect_paths cache in ld_instance_manager.py ========
path1 = r"E:\模拟器定时关闭工具\ld_instance_manager.py"
with open(path1, "r", encoding="utf-8") as f:
    text = f.read()

# Insert cache variables before the function
text = text.replace(
    "def auto_detect_paths():",
    "# 自动探测结果缓存（模块级），避免重复全盘扫描\n"
    "_AUTO_DETECT_CACHE = None\n"
    "_AUTO_DETECT_CACHE_TIME = 0.0\n"
    "_AUTO_DETECT_CACHE_TTL = 300  # 5 分钟过期\n\n"
    "def auto_detect_paths(force=False):"
)

# Insert cache read after docstring, before 'result = {'
text = text.replace(
    '    result = {',
    '    global _AUTO_DETECT_CACHE, _AUTO_DETECT_CACHE_TIME\n'
    '    now = time.time()\n'
    '    if not force and _AUTO_DETECT_CACHE is not None and (now - _AUTO_DETECT_CACHE_TIME) < _AUTO_DETECT_CACHE_TTL:\n'
    '        return dict(_AUTO_DETECT_CACHE)\n\n'
    '    result = {',
    1  # only first occurrence
)

# Insert cache write before 'return result'
text = text.replace(
    '    return result',
    '    # 写入缓存\n'
    '    _AUTO_DETECT_CACHE = result\n'
    '    _AUTO_DETECT_CACHE_TIME = time.time()\n'
    '    return result',
    1  # only first occurrence
)

with open(path1, "w", encoding="utf-8") as f:
    f.write(text)
py_compile.compile(path1, doraise=True)
print("Fix 1 (ld_instance_manager.py): OK")


# ======== Fix 2: MuMu interval unification in 模拟器管理工具.pyw ========
path2 = r"E:\模拟器定时关闭工具\模拟器管理工具.pyw"
with open(path2, "r", encoding="utf-8") as f:
    lines = f.readlines()

# Find the _work() function inside _time_up and modify it
# Strategy: read as lines, find the exact block, replace with correct indentation
S20 = "                    "  # 20 spaces
S24 = "                        "  # 24 spaces
S28 = "                            "  # 28 spaces
S32 = "                                "  # 32 spaces
S36 = "                                    "  # 36 spaces
S40 = "                                        "  # 40 spaces
S44 = "                                            "  # 44 spaces

# Find the section: 'ok = 0' through 'time.sleep(5)' in MuMu section
# and replace with unified interval version
target_text = (
    S20 + "ok = 0\n"
    "\n"
    S20 + "# LDPlayer\n"
    S20 + "if ld_names:\n"
    S24 + "_interval = 5\n"
    S24 + "try:\n"
    S28 + "_interval = int(self.launch_interval_var.get())\n"
    S24 + "except (ValueError, TypeError):\n"
    S28 + "pass\n"
    S24 + "results = staggered_launch(\n"
    S28 + "dnconsole, ld_names, interval_seconds=_interval,\n"
    S28 + "on_status=lambda s: self.root.after(0, lambda: t[\"vars\"][\"st_lbl\"].config(text=s[:30], fg=YELLOW)),\n"
    S24 + ")\n"
    S24 + "ok += sum(1 for _, s, _ in results if s)\n"
    "\n"
    S20 + "# MuMu\n"
    S20 + "if mumu_keys and mumu_path:\n"
    S24 + "from ld_instance_manager import launch_mumu_instance, start_mumu_health_monitor\n"
    S24 + "for k in mumu_keys:\n"
    S28 + "idx = int(k.replace(\"mumu_\", \"\"))\n"
    S28 + "try:\n"
    S32 + "succ_mu, _ = launch_mumu_instance(mumu_path, idx)\n"
    S32 + "if succ_mu:\n"
    S36 + "ok += 1\n"
    S36 + "# 启动成功后开启健康巡检\n"
    S36 + "if self._mumu_health_check_enabled:\n"
    S40 + "monitor = start_mumu_health_monitor(\n"
    S44 + "mumu_path, idx,\n"
    S44 + "check_interval=self._mumu_health_interval * 60)\n"
    S40 + "with self._mumu_lock:\n"
    S44 + "self._mumu_monitors[idx] = monitor\n"
    S40 + "_log_info(f\"_time_up MuMu {idx} 健康巡检已启动\")\n"
    S32 + "time.sleep(5)\n"
    S28 + "except Exception as _e_mu:\n"
    S32 + "_log_error(f\"_time_up _work: MuMu {idx} 启动失败: {_e_mu}\")\n"
)

replacement = (
    S20 + "ok = 0\n"
    "\n"
    S20 + "# 读取启动间隔（LD 和 MuMu 共用）\n"
    S20 + "_interval = 5\n"
    S20 + "try:\n"
    S24 + "_interval = int(self.launch_interval_var.get())\n"
    S20 + "except (ValueError, TypeError):\n"
    S24 + "pass\n"
    "\n"
    S20 + "# LDPlayer\n"
    S20 + "if ld_names:\n"
    S24 + "results = staggered_launch(\n"
    S28 + "dnconsole, ld_names, interval_seconds=_interval,\n"
    S28 + "on_status=lambda s: self.root.after(0, lambda: t[\"vars\"][\"st_lbl\"].config(text=s[:30], fg=YELLOW)),\n"
    S24 + ")\n"
    S24 + "ok += sum(1 for _, s, _ in results if s)\n"
    "\n"
    S20 + "# MuMu\n"
    S20 + "if mumu_keys and mumu_path:\n"
    S24 + "from ld_instance_manager import launch_mumu_instance, start_mumu_health_monitor\n"
    S24 + "for k in mumu_keys:\n"
    S28 + "idx = int(k.replace(\"mumu_\", \"\"))\n"
    S28 + "try:\n"
    S32 + "succ_mu, _ = launch_mumu_instance(mumu_path, idx)\n"
    S32 + "if succ_mu:\n"
    S36 + "ok += 1\n"
    S36 + "# 启动成功后开启健康巡检\n"
    S36 + "if self._mumu_health_check_enabled:\n"
    S40 + "monitor = start_mumu_health_monitor(\n"
    S44 + "mumu_path, idx,\n"
    S44 + "check_interval=self._mumu_health_interval * 60)\n"
    S40 + "with self._mumu_lock:\n"
    S44 + "self._mumu_monitors[idx] = monitor\n"
    S40 + "_log_info(f\"_time_up MuMu {idx} 健康巡检已启动\")\n"
    S32 + "time.sleep(_interval)\n"
    S28 + "except Exception as _e_mu:\n"
    S32 + "_log_error(f\"_time_up _work: MuMu {idx} 启动失败: {_e_mu}\")\n"
)

if target_text in text:
    text = text.replace(target_text, replacement)
    print("Fix 2 (_time_up interval): OK")
else:
    print("Fix 2: target_text not found!")
    print("Expected pattern start:")
    print(repr(target_text[:200]))

with open(path2, "w", encoding="utf-8") as f:
    f.write(text)

py_compile.compile(path2, doraise=True)
print("Post Fix 2 syntax: OK")


# ======== Fix 3: _save_tasks_config_inner fallback ========
with open(path2, "r", encoding="utf-8") as f:
    text = f.read()

# Replace the shutdown and launch data collection to add fallback
S12 = "            "
S16 = "                "
S20 = "                    "
S24 = "                        "
S28 = "                            "

old_shutdown = (
    S12 + "shutdown_data = []\n"
    S12 + "for t in self.shutdown_tasks:\n"
    S16 + "try:\n"
    S20 + "shutdown_data.append({\n"
    S24 + '"mode": t["mode"],\n'
    S24 + '"hour": int(float(t["vars"]["h_spin"].get())),\n'
    S24 + '"minute": int(float(t["vars"]["m_spin"].get())),\n'
    S24 + '"countdown_min": int(float(t["vars"]["cd_spin"].get())),\n'
    S24 + '"enabled": t["en_var"].get(),\n'
    S20 + "})\n"
    S16 + "except Exception as e:\n"
    S20 + '_log_error(f"_save_tasks_config 关闭任务异常: task_id={t.get(\'id\')} {type(e).__name__}: {e}")\n'
)

new_shutdown = (
    S12 + "shutdown_data = []\n"
    S12 + "for t in self.shutdown_tasks:\n"
    S16 + "try:\n"
    S20 + "try:\n"
    S24 + 'hour = int(float(t["vars"]["h_spin"].get()))\n'
    S24 + 'minute = int(float(t["vars"]["m_spin"].get()))\n'
    S24 + 'cd = int(float(t["vars"]["cd_spin"].get()))\n'
    S20 + "except Exception:\n"
    S24 + 'hour = t.get("hour", 0)\n'
    S24 + 'minute = t.get("minute", 0)\n'
    S24 + 'cd = t.get("cd_min", 30)\n'
    S20 + "shutdown_data.append({\n"
    S24 + '"mode": t["mode"],\n'
    S24 + '"hour": hour,\n'
    S24 + '"minute": minute,\n'
    S24 + '"countdown_min": cd,\n'
    S24 + '"enabled": t["en_var"].get(),\n'
    S20 + "})\n"
    S16 + "except Exception as e:\n"
    S20 + '_log_error(f"_save_tasks_config 关闭任务异常: task_id={t.get(\'id\')} {type(e).__name__}: {e}")\n'
)

old_launch = (
    S12 + "launch_data = []\n"
    S12 + "for t in self.launch_tasks:\n"
    S16 + "try:\n"
    S20 + "launch_data.append({\n"
    S24 + '"mode": t["mode"],\n'
    S24 + '"hour": int(float(t["vars"]["h_spin"].get())),\n'
    S24 + '"minute": int(float(t["vars"]["m_spin"].get())),\n'
    S24 + '"countdown_min": int(float(t["vars"]["cd_spin"].get())),\n'
    S24 + '"enabled": t["en_var"].get(),\n'
    S24 + '"instances": list(t.get("instances", [])),\n'
    S20 + "})\n"
    S16 + "except Exception as e:\n"
    S20 + '_log_error(f"_save_tasks_config 启动任务异常: task_id={t.get(\'id\')} {type(e).__name__}: {e}")\n'
)

new_launch = (
    S12 + "launch_data = []\n"
    S12 + "for t in self.launch_tasks:\n"
    S16 + "try:\n"
    S20 + "try:\n"
    S24 + 'hour = int(float(t["vars"]["h_spin"].get()))\n'
    S24 + 'minute = int(float(t["vars"]["m_spin"].get()))\n'
    S24 + 'cd = int(float(t["vars"]["cd_spin"].get()))\n'
    S20 + "except Exception:\n"
    S24 + 'hour = t.get("hour", 8)\n'
    S24 + 'minute = t.get("minute", 0)\n'
    S24 + 'cd = t.get("cd_min", 30)\n'
    S20 + "launch_data.append({\n"
    S24 + '"mode": t["mode"],\n'
    S24 + '"hour": hour,\n'
    S24 + '"minute": minute,\n'
    S24 + '"countdown_min": cd,\n'
    S24 + '"enabled": t["en_var"].get(),\n'
    S24 + '"instances": list(t.get("instances", [])),\n'
    S20 + "})\n"
    S16 + "except Exception as e:\n"
    S20 + '_log_error(f"_save_tasks_config 启动任务异常: task_id={t.get(\'id\')} {type(e).__name__}: {e}")\n'
)

if old_shutdown in text and old_launch in text:
    text = text.replace(old_shutdown, new_shutdown)
    text = text.replace(old_launch, new_launch)
    print("Fix 3 (save fallback): OK")
else:
    print("Fix 3: old pattern not found!")
    if old_shutdown not in text:
        print("  shutdown pattern missing!")
    if old_launch not in text:
        print("  launch pattern missing!")

with open(path2, "w", encoding="utf-8") as f:
    f.write(text)

py_compile.compile(path2, doraise=True)
print("Post Fix 3 syntax: OK")

print("\nAll 3 fixes applied successfully!")
print(f"\nFiles modified:\n  {path1}\n  {path2}")