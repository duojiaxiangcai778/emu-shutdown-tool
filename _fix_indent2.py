#!/usr/bin/env python3
"""Fix indentation in _save_tasks_config_inner"""
import py_compile

path = r"E:\模拟器定时关闭工具\模拟器管理工具.pyw"
with open(path, 'r', encoding='utf-8') as f:
    lines = f.readlines()

# The section starts at the "config = load_tool_config()" line
# Find the exact range to replace
start = None
for i, line in enumerate(lines):
    if "def _save_tasks_config_inner" in line:
        start = i
        break

print(f"Function starts at line {start+1}")

# Let's check lines 2590-2630
for i in range(2589, 2632):
    if i < len(lines):
        s = lines[i]
        print(f"  L{i+1}: |{s.rstrip()}|")

# Specifically fix lines 2610 and 2631
lines[2609] = "                except Exception as e:\n"
lines[2610] = "                    _log_error(f'_save_tasks_config 关闭任务异常: task_id={t.get(\"id\")} {type(e).__name__}: {e}')\n"

lines[2629] = "                except Exception as e:\n"
lines[2630] = "                    _log_error(f'_save_tasks_config 启动任务异常: task_id={t.get(\"id\")} {type(e).__name__}: {e}')\n"

with open(path, 'w', encoding='utf-8') as f:
    f.writelines(lines)

py_compile.compile(path, doraise=True)
print("\nSYNTAX OK")