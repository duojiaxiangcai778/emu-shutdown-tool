# 模拟器管理工具 Bug 检测报告

## 已修复 (3个)

| # | 文件 | 行号 | 问题 | 状态 |
|---|------|------|------|------|
| 5原 | pyw | 58,83,92,103 | `_LOG_BUFFER` 多线程读写无锁保护 | ✅ 已加 `threading.Lock` |
| 4原 | pyw | 102 | `_flush_log` 每次写入都带重复文件头 | ✅ 仅空文件写 header |
| 3原 | pyw | 75-96 | `_log_error`/`_log_info` 写入加锁 | ✅ 已加锁 |

---

## 未修复 P0 — 致命

### Bug 1: `_scanning` 标志异常后永不重置
- **文件**: `pyw:2849-3046`
- **现象**: `_scan_and_display_instances` 在第2852行设 `self._scanning = True`，如果中间代码抛异常，第3046行的 `self._scanning = False` 永远不会执行，之后所有扫描请求被第2849行跳过
- **修复**: 第2852行之后加 `try:`，方法末尾加 `finally: self._scanning = False`
```
 2852:     self._scanning = True
+2853:     try:
          ... 所有现有代码保持不变 ...
+3046:     finally:
+3047:         self._scanning = False
-3046:     self._scanning = False
```

### Bug 2: `_force_shutdown_windows` 方法2-4是死代码
- **文件**: `pyw:188-266`
- **现象**: 方法1（shutdown /f）在第213行无条件 `return True`。只要 `subprocess.run` 不抛异常（几乎不会），方法2-4永远不执行。若shutdown被拦截，程序误判成功
- **修复**: 移除第213行 `return True`，改为检查returncode
```python
# 第213行改为:
        result = subprocess.run(...)
        if result.returncode == 0:
            return True  # 真正成功才返回
        # returncode非0 → 继续尝试后续方法
```

---

## 未修复 P1 — 严重

### Bug 3: `_log_error` 被大量滥用为 debug 日志
- **文件**: `pyw:2664,2672,2686,2688,2702,2705,2707,2713,2721,2727,2729,2735,2738,2740,2755,2769,2850,2853,2861,2878,2879,2883,2885,2889,2896,2898`等数十处
- **问题**: 验证路径、扫描实例中的debug输出全部用 `_log_error`，同时多处将普通字符串作为第二个参数传入 `_log_error(context, "普通消息")`，格式化后带多余 `---` 分隔线
- **修复**: 所有 `_log_error("[DEBUG]...")` 改为 `_log_info(...)`；传字符串消息的 `_log_error` 改为 `_log_info`

### Bug 4: 禁用任务后 auto_reset 仍可重启任务
- **文件**: `pyw:1843-1856`
- **问题**: `_on_en_toggle_generic` 设 `running=False`，但不取消 `auto_reset_id` 和 `update_id` 的 pending after回调。auto_reset回调触发时会重新设 `running=True`
- **修复**: 禁用时增加 cancel 逻辑（模仿 `_stop_task` 第1776-1780行）
```python
# 在 _on_en_toggle_generic 的 if not task["enabled"] 分支中添加:
for k in ("update_id", "auto_reset_id"):
    if task.get(k):
        try: self.root.after_cancel(task[k])
        except Exception: pass
        task[k] = None
```

### Bug 5: `_on_close` 未停止 launch_tasks 回调线程
- **文件**: `pyw:4156-4157`
- **问题**: 关闭窗口时只遍历 `shutdown_tasks` 设 `running=False`，遗漏 `launch_tasks`
- **修复**: 在第4156行之后添加对 launch_tasks 的同样处理
```python
for t in self.launch_tasks:
    t["running"] = False
```

### Bug 6: 开机自动启动与实例扫描竞态
- **文件**: `pyw:3043-3044`
- **问题**: `_auto_launch_on_startup` 通过 `root.after(500)` 触发，扫描是异步的。500ms内扫描未完成则 `_instances` 为空，自动启动静默失败
- **修复**: 将第3043-3044行改为直接调用（此时扫描已完成、UI已构建完毕）
```python
# 替换:
if not self._startup_launch_done:
    self.root.after(500, self._auto_launch_on_startup)
# 为:
if not self._startup_launch_done:
    self._auto_launch_on_startup()
```

### Bug 7: `graceful_kill_async` 回调在窗口销毁后调用 root.after
- **文件**: `pyw:2253-2255`
- **问题**: `on_done` lambda 调用 `self.root.after(0, ...)`，但 `_on_close` 已 `destroy()`
- **修复**: lambda 中加守护条件
```python
on_done=lambda ...: None if self._destroyed else self.root.after(0, lambda: self._on_kill_done(...))
```

### Bug 8: `_save_all_paths` 覆盖整个 paths 字典
- **文件**: `pyw:2654-2659`
- **问题**: `config["paths"] = self._ld_paths` 完全替换，丢失用户手动添加的其他路径键值
- **修复**: 改为合并
```python
if self._ld_paths:
    config.setdefault("paths", {}).update(self._ld_paths)
```

### Bug 9: `_save_tasks_config_inner` 迭代中 UI 销毁崩溃
- **文件**: `pyw:2328-2338`
- **问题**: 遍历 tasks 访问 `t["vars"]["h_spin"].get()`，任务被删除后 frame 已 destroy，抛 TclError
- **修复**: 迭代前做快照，逐个 try/except
```python
for t in list(self.shutdown_tasks):  # 快照
    try:
        # ... 现有代码 ...
    except Exception as e:
        _log_error(...)
```

---

## 未修复 P2 — 中等

### Bug 10: `_refresh_log_display` 读 `_LOG_BUFFER` 未加锁
- **文件**: `pyw:3927`
- **问题**: `"".join(_LOG_BUFFER)` 未用 `_LOG_BUFFER_LOCK`，可能读到不一致状态
- **修复**:
```python
with _LOG_BUFFER_LOCK:
    lines = "".join(_LOG_BUFFER)
```

### Bug 11: `_on_ld_focusout`/`_on_mumu_focusout` 无条件触发验证
- **文件**: `pyw:1420-1426, 1450-1456`
- **问题**: FocusOut时即使显示占位文本也调用 `_on_ld_path_enter()`，触发无意义的路径验证和日志输出
- **修复**: 加守护条件
```python
def _on_ld_focusout(_):
    if not self._ld_placeholder:  # 添加这行
        self._on_ld_path_enter()
    ...
```

### Bug 12: `success[0] += 1` 多线程竞态
- **文件**: `pyw:3980-4002, 4043-4069`
- **问题**: 多线程同时 `success[0] += 1`（先读后写），CPython GIL下实际安全但理论上存在竞态
- **修复**: 用 `threading.Lock` 或改用 `threading.local` 收集结果

### Bug 13: `_auto_reset_task` 未取消旧的 pending after
- **文件**: `pyw:1825-1841`
- **问题**: 第1827行设 `auto_reset_id = None` 但没 cancel 旧的，旧回调仍会触发导致重复启动线程
- **修复**:
```python
if t.get("auto_reset_id"):
    try: self.root.after_cancel(t["auto_reset_id"])
    except Exception: pass
```

### Bug 14: `_mumu_health_var` 等属性在 `_lazy_init` 中创建太晚
- **文件**: `pyw:1214-1216`
- **问题**: UI 在 `__init__` 中已构建完毕，若用户在 `_lazy_init` 执行前点击 MuMu 健康检测按钮，触发 `AttributeError`
- **修复**: 将 `self._mumu_health_check_enabled`、`self._mumu_monitors`、`self._mumu_lock` 的初始化移到 `__init__` 中

### Bug 15: `_on_close` 不取消 `_refresh_log_display` 定时器
- **文件**: `pyw:4134-4163`
- **问题**: `_refresh_log_display` 用 `root.after(1000, ...)` 自调度，没有存储 timer ID，`destroy()` 后仍会触发 TclError
- **修复**: 存储 timer ID 并在 `_on_close` 中 cancel，或在方法开头检查 `self._destroyed`

### Bug 16: `_save_snapshot` 在主线程阻塞执行 `_auto_detect_paths`
- **文件**: `pyw:3324`
- **问题**: 该方法执行 wmic 子进程 + 文件系统扫描 + 注册表查询，可能阻塞 GUI 5-30秒
- **修复**: 改为后台线程执行路径检测，完成后通过 `root.after()` 回调继续保存快照

### Bug 17: `save_tool_config` 写入不原子
- **文件**: `ld_instance_manager.py:802-809`
- **问题**: 直接写入 config 文件，崩溃时文件可能被截断/损坏
- **修复**: 先写 `.tmp` 文件，再 `os.replace()` 覆盖

### Bug 18: 扫描可被并发触发导致重复UI行
- **文件**: `pyw:2874, 3896`
- **问题**: `_apply_detected_paths` → `root.after(100, _scan_and_display_instances)` 可能在第一次扫描的 widget 重建期间触发第二次扫描
- **修复**: `_scan_and_display_instances` 开头检查 `_scanning` 标志（Bug 1 修复后此问题自然解决）

---

## 未修复 P3 — 低

### Bug 19: `_find_ld_from_process` 只返回第一个匹配
- **文件**: `ld_instance_manager.py:107-123`
- **问题**: 多个 LDPlayer 安装时只返回第一个
- **修复**: 收集所有匹配路径，或优先返回含 vms/config 的路径

### Bug 20: MuMu 健康巡检 `on_status` 回调在后台线程直接调用
- **文件**: `ld_instance_manager.py:1985-2046`
- **问题**: `on_status(index, healthy, msg)` 由后台线程调用，未通过 `root.after()` 调度到主线程。当前代码中 `on_status` 未传入（第3947行），但未来启用时有线程安全风险
- **修复**: 回调内用 `root.after(0, lambda: ...)` 调度到主线程

---

## 统计

| 优先级 | 数量 | 已修复 | 未修复 |
|--------|------|--------|--------|
| P0 | 2 | 0 | 2 |
| P1 | 7 | 0 | 7 |
| P2 | 9 | 3 | 6 |
| P3 | 2 | 0 | 2 |
| **合计** | **20** | **3** | **17** |
