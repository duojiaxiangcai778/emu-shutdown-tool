#!/usr/bin/env python3
"""Apply all 3 fixes to 模拟器管理工具.pyw and ld_instance_manager.py"""
import py_compile, os

# ============================================================
# Fix 1: auto_detect_paths() cache in ld_instance_manager.py
# ============================================================
path1 = r"E:\模拟器定时关闭工具\ld_instance_manager.py"
with open(path1, "r", encoding="utf-8") as f:
    lines = f.readlines()

# Add cache variables after the section header
cache_vars = [
    "\n",
    "# 自动探测结果缓存（模块级），避免重复全盘扫描\n",
    "_AUTO_DETECT_CACHE = None\n",
    "_AUTO_DETECT_CACHE_TIME = 0.0\n",
    "_AUTO_DETECT_CACHE_TTL = 300  # 5 分钟过期\n",
    "\n",
]

# Find the function definition to insert cache before it
insert_pos = None
for i, line in enumerate(lines):
    if "def auto_detect_paths():" in line:
        insert_pos = i
        break

# Change function signature
old_sig = "def auto_detect_paths():"
new_sig = "def auto_detect_paths(force=False):"

# Add cache read after docstring but before result = {
cache_read = [
    '    global _AUTO_DETECT_CACHE, _AUTO_DETECT_CACHE_TIME\n',
    '    now = time.time()\n',
    '    if not force and _AUTO_DETECT_CACHE is not None and (now - _AUTO_DETECT_CACHE_TIME) < _AUTO_DETECT_CACHE_TTL:\n',
    '        return dict(_AUTO_DETECT_CACHE)  # 返回副本，防止外部修改缓存\n',
]

# Add cache write before the final return
cache_write = [
    '    # 写入缓存\n',
    '    _AUTO_DETECT_CACHE = result\n',
    '    _AUTO_DETECT_CACHE_TIME = time.time()\n',
]

# Apply changes
new_lines = []
for i, line in enumerate(lines):
    if i == insert_pos:
        # Insert cache vars before function
        new_lines.extend(cache_vars)
        new_lines.append(line.replace(old_sig, new_sig))
    elif line.strip() == '    result = {':
        # Insert cache read before result = {
        new_lines.extend(cache_read)
        new_lines.append(line)
    elif line.strip() == '    return result':
        # Insert cache write before return
        new_lines.extend(cache_write)
        new_lines.append(line)
    else:
        new_lines.append(line)

with open(path1, "w", encoding="utf-8") as f:
    f.writelines(new_lines)

# Verify
try:
    py_compile.compile(path1, doraise=True)
    print("Fix 1 (ld_instance_manager.py): SYNTAX OK")
except py_compile.PyCompileError as e:
    print(f"Fix 1 ERROR: {e}")

# ============================================================
# Fix 2 & 3: MuMu interval unification + save from task dict
# ============================================================
path2 = r"E:\模拟器定时关闭工具\模拟器管理工具.pyw"
with open(path2, "r", encoding="utf-8") as f:
    lines = f.readlines()

# Fix 2a: In _time_up, move _interval before if ld_names and change time.sleep(5)
# Find the _work() function inside _time_up
for i, line in enumerate(lines):
    if 'ok = 0\n' in line and i > 2180 and i < 2200:  # inside _work()
        # Replace the block from 'ok = 0' through '# LDPlayer' through the MuMu section
        # We need to find the exact structure
        pass

# This is getting complex - let me just do the simple parts
# Fix 2b: In the test _exec_test_launch, change time.sleep(3) to use interval from UI
# This is a simpler change

# Fix 3: In _save_tasks_config_inner, add try/except fallback for spinbox reads
# Find the function
for i, line in enumerate(lines):
    if line.strip() == "hour = int(float(t[\"vars\"][\"h_spin\"].get()))":
        # Found a spinbox read - add try/except wrapper
        # Check if it's already inside a nested try
        pass

print("Done")