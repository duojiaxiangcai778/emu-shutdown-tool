# 模拟器管理工具 Bug 检测报告（基于最新代码）

## 已修复 (13个)

| # | 问题 | 修复位置 |
|---|------|---------|
| 1 | `_LOG_BUFFER` 多线程读写无锁 | pyw:59 `_LOG_BUFFER_LOCK` |
| 2 | `_flush_log` 每次写入重复文件头 | pyw:110 仅空文件写 header |
| 3 | `_log_error`/`_log_info` 写入加锁 | pyw:84,94 |
| 4 | `_force_shutdown_windows` 方法2-4死代码 | pyw:208 移除 `return True` |
| 5 | `_on_close` 未停止 launch_tasks | pyw:4172-4173 |
| 6 | 禁用任务后 auto_reset 仍可重启 | pyw:1855-1859 cancel pending after |
| 7 | `_refresh_log_display` 读 `_LOG_BUFFER` 未加锁 | pyw:3935 `with _LOG_BUFFER_LOCK` |
| 8 | `_refresh_log_display` 定时器未取消 | pyw:3944 `_log_refresh_id` + pyw:4178-4181 |
| 9 | `_save_all_paths` 覆盖 paths 字典 | pyw:2668-2670 `setdefault().update()` |
| 10 | `_find_ld_from_process` 只返回第一个 | ld_instance_manager.py:117-127 收集候选优先 vms/config |
| 11 | `_on_ld_focusout` 占位文本触发验证 | pyw:1422 `if not self._ld_placeholder` |
| 12 | `on_done` 回调窗口销毁后崩溃 | pyw:2266 `None if self._destroyed else ...` |
| 13 | `save_tool_config` 写入不原子 | ld_instance_manager.py:811-815 先写 .tmp 再 `os.replace` |
| 14 | `_auto_reset_task` 未取消旧 pending after | pyw:1830-1832 先 cancel 旧 ID |
| 15 | `_save_tasks_config_inner` 迭代未快照 | pyw:2341 逐个 try/except（已有） |

---

## 未修复 — P0 致命

### Bug A: `_scanning` 异常后永不重置
- **位置**: pyw:2860-3054
- **现状**: `_scan_and_display_instances` 没有 try/finally 保护。中间任何异常（如 `scan_instances`、`scan_mumu_instances`、UI 创建等）抛出后，`_scanning` 永远为 True，之后所有扫描请求被跳过
- **影响**: 实例列表永远无法刷新
- **修复**: 方法体包裹 try/finally，finally 中 `self._scanning = False`

### Bug B: 开机自动启动与实例扫描竞态
- **位置**: pyw:3052-3053
- **现状**: 仍用 `root.after(500, self._auto_launch_on_startup)`
- **影响**: 扫描异步，500ms 内未完成则 `_instances` 为空，自动启动静默失败
- **修复**: 改为直接调用 `self._auto_launch_on_startup()`

---

## 未修复 — P1 严重

### Bug C: `_log_error` 被大量滥用为 debug 日志
- **位置**: pyw:2662,2670,2678,2688,2698,2705,2707,2722,2729,2735,2740,2755,2769,2862,2870,2878,2881,2887,2888,2893,2898 等数十处
- **问题**: 验证路径、扫描实例中的 debug 输出全部用 `_log_error`，多处将普通字符串作为第二个参数
- **修复**: 所有 `_log_error("[DEBUG]...")` 改为 `_log_info(...)`

### Bug D: `_on_mumu_focusout` 无条件触发验证
- **位置**: pyw:1452-1453
- **现状**: 占位文本时也调用 `_on_mumu_path_enter()`，触发无意义路径验证和日志输出
- **修复**: 加 `if not self._mumu_placeholder:` 守护（与 LD 行一致）

### Bug E: `_save_snapshot` 主线程阻塞
- **位置**: pyw:3336
- **现状**: `_auto_detect_paths()` 在主线程执行 wmic + 文件扫描 + 注册表查询
- **影响**: GUI 冻结 5-30 秒
- **修复**: 改为后台线程，完成后 `root.after()` 回调

---

## 未修复 — P2 中等

### Bug F: `success[0] += 1` 多线程竞态
- **位置**: pyw:3980-4002, 4043-4069
- **问题**: 多线程 `success[0] += 1` 非原子操作
- **修复**: 用 `threading.Lock` 或收集结果后求和

### Bug G: `_mumu_health_var` 等属性初始化过晚
- **位置**: pyw:1212-1217
- **问题**: UI 在 `__init__` 已构建，用户快速点击触发 `AttributeError`
- **修复**: 将初始化移到 `__init__` 中

### Bug H: MuMu 健康巡检 `on_status` 回调线程安全
- **位置**: ld_instance_manager.py:1985-2046
- **问题**: 当前 `on_status` 未传入不触发，但启用时有风险
- **修复**: 回调内用 `root.after(0, lambda: ...)` 调度到主线程

---

## 统计

| 优先级 | 总数 | 已修复 | 未修复 |
|--------|------|--------|--------|
| P0 | 2 | 0 | 2 |
| P1 | 3 | 0 | 3 |
| P2 | 3 | 0 | 3 |
| P3 | 1 | 1 | 0 |
| **合计** | **9** | **1** | **8** |

> 注：与上一版报告相比，Bug 5(关闭launch线程)、Bug 4(禁用任务重启)、Bug 10(日志锁)、Bug 15(定时器取消)、Bug 8(paths合并)、Bug 19(多LD)、Bug 7(on_done崩溃)、Bug 13(auto_reset重复)、Bug 17(原子写入)、Bug 11(focusout验证)、Bug 2(死代码) 均已修复。
