"""
稳健的 Windows 文件/目录删除工具
—— 替代 git-bash rm -rf，避免 MSYS2 仿真层卡死

用法:
  python _robust_cleanup.py <path> [path...]

原理:
  - os.remove() / shutil.rmtree() 直接调用 Windows API
  - 无 MSYS2 路径转换, 无 fork 开销, 无 AV 仿真层拦截
  - 文件被占用则提示检查占用进程
"""

import os
import shutil
import subprocess
import sys


def find_locking_processes(path: str) -> list:
    """用 PowerShell 查占用指定路径的进程 (仅提示, 不自动杀)"""
    try:
        basename = os.path.basename(path)
        ps_cmd = (
            f"Get-Process | Where-Object {{ "
            f"$_.Modules | Where-Object {{ $_.FileName -like '*{basename}*' }} "
            f"}} | Select-Object Id,ProcessName"
        )
        result = subprocess.run(  # noqa: S603
            ['powershell', '-Command', ps_cmd],  # noqa: S607
            capture_output=True, text=True, timeout=10
        )
        lines = [line for line in result.stdout.splitlines() if line.strip() and '---' not in line]
        return lines if lines else ['  (未查到占用进程)']
    except Exception as e:
        return [f'  (查询失败: {e})']


def remove_path(path: str) -> bool:
    """删除文件或目录, 返回是否成功"""
    abs_path = os.path.abspath(path)
    if not os.path.exists(abs_path):
        print(f'\u23ed \u4e0d\u5b58\u5728: {abs_path}')
        return True

    try:
        if os.path.isfile(abs_path) or os.path.islink(abs_path):
            os.remove(abs_path)
            print(f'\u2705 \u6587\u4ef6\u5df2\u5220\u9664: {abs_path}')
            return True
        elif os.path.isdir(abs_path):
            shutil.rmtree(abs_path, onerror=lambda fn, p, ei: print(
                f'  ⚠ 删除 {p} 失败: {ei[1]}'))
            if not os.path.exists(abs_path):
                print(f'\u2705 \u76ee\u5f55\u5df2\u5220\u9664: {abs_path}')
                return True
            else:
                print(f'\u274c \u76ee\u5f55\u5220\u9664\u4e0d\u5b8c\u6574: {abs_path}')
                return False
    except PermissionError as e:
        print(f'\u274c \u6743\u9650\u62d2\u7edd (\u6587\u4ef6\u88ab\u5360\u7528): {abs_path}')
        print(f'   \u9519\u8bef: {e}')
        print('   占用进程查询:')
        for p in find_locking_processes(abs_path):
            print(f'     {p}')
        return False
    except Exception as e:
        print(f'\u274c \u5220\u9664\u5931\u8d25: {abs_path}')
        print(f'   \u9519\u8bef: {type(e).__name__}: {e}')
        return False


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    all_ok = True
    for p in sys.argv[1:]:
        if not remove_path(p):
            all_ok = False

    if all_ok:
        print('\n\u2705 \u5168\u90e8\u6e05\u7406\u5b8c\u6210')
    else:
        print('\n\u26a0\ufe0f \u90e8\u5206\u6e05\u7406\u5931\u8d25, \u8bf7\u68c0\u67e5\u4e0a\u9762\u7684\u9519\u8bef\u4fe1\u606f')
        sys.exit(1)


if __name__ == '__main__':
    main()
