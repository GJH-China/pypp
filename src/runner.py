"""
py++ 运行器 UI —— 介于 CLI 与 TUI 之间的命令行界面

双击 .cpy 文件时由它接手（pypprun.bat → python runner.py <文件>）：
  · 头部信息框：py++ 版本、文件名、引擎模式
  · 程序输出**实时透传**到控制台（stream 模式：
    input() 的提示语当场可见，用户能直接输入）
  · 尾部状态框：引擎模式、编译/运行耗时、退出码（绿=0 / 红=非0）
  · 按任意键关闭窗口（仅交互式控制台，管道模式自动跳过）

为什么不是 curses：只依赖 ANSI 颜色 + 制表符边框，cmd / PowerShell /
Windows Terminal 全兼容，零依赖，也不会因为终端不支持全屏而花屏。

为什么必须 stream：pypp.py 以前 capture 子进程输出、跑完才打印，
程序里 input() 的提示语用户根本看不到（只能盲打，像假死）。
stream=True 让子进程 stdio 直连控制台，提示与输出实时可见。
"""

import os
import re
import sys
import time
import unicodedata

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from assoc import RUNNER_NAME
from pypp import VERSION, compile_and_run, find_toolchain

# ---- ANSI 颜色（终端 UI 的"TUI 感"来源）----------------------------------
_RESET = '\033[0m'
_BOLD = '\033[1m'
_DIM = '\033[2m'
_GREEN = '\033[32m'
_RED = '\033[31m'
_YELLOW = '\033[33m'
_CYAN = '\033[36m'
_WHITE = '\033[97m'

_BOX_W = 64          # 框内文本宽度（等宽显示单元）


_ANSI_RE = re.compile(r'\033\[[0-9;]*m')


def _plain(s):
    """去掉 ANSI 颜色码（算宽度时颜色码不占地方）。"""
    return _ANSI_RE.sub('', s)


def _dw(s):
    """显示宽度：CJK 记 2，其余记 1（否则框线对不齐）。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ('W', 'F') else 1
               for ch in _plain(s))


def _pad(s, width):
    return s + ' ' * max(0, width - _dw(s))


def _box(rows, accent=_CYAN):
    """画一个圆角信息框。rows: [(label, value)] 或 [纯文本]."""
    if not rows:
        return ''
    top = '╭' + '─' * (_BOX_W + 2) + '╮'
    bot = '╰' + '─' * (_BOX_W + 2) + '╯'
    out = [accent + top + _RESET]
    for row in rows:
        if isinstance(row, tuple):
            label, value = row
            text = f'{_BOLD}{label}{_RESET}  {value}'
            plain = f'{label}  {value}'
        else:
            text = row
            plain = row
        out.append(accent + '│ ' + _RESET + _pad(text, _BOX_W)
                   + ' ' + accent + '│' + _RESET)
    out.append(accent + bot + _RESET)
    return '\n'.join(out)


def _setup_console(title):
    """UTF-8 输出 + 控制台标题（双击场景窗口标题默认是 bat 名，太丑）。"""
    if os.name == 'nt':
        try:
            import ctypes
            # 仅交互式控制台切换代码页——管道/测试进程不碰，
            # 免得改掉宿主控制台的 CP 影响别的输出。
            try:
                interactive = sys.stdout.isatty()
            except Exception:
                interactive = False
            if interactive:
                ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleTitleW(title)
        except Exception:
            pass
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass


def wait_key():
    """交互式控制台才等待；管道模式直接返回（否则自动化会挂住）。"""
    try:
        if not sys.stdin.isatty():
            return
    except Exception:
        return
    try:
        if os.name == 'nt':
            import msvcrt
            msvcrt.getch()
        else:
            input()
    except Exception:
        pass


def _fmt_ms(ms):
    if ms is None:
        return '-'
    if ms < 1000:
        return f'{ms:.0f}ms'
    return f'{ms / 1000:.2f}s'


def _mode_label(mode):
    return {'native': '原生高速模式', 'vm': '内置 VM 模式'}.get(mode, mode or '未知')


def run_file(path, pause=True):
    """运行一个 .cpy 文件，带运行器 UI。返回退出码。"""
    if not os.path.isfile(path):
        print(_box([(_RED + '找不到文件' + _RESET, path)], accent=_RED))
        if pause:
            wait_key()
        return 1

    name = os.path.basename(path)
    folder = os.path.dirname(os.path.abspath(path))
    # 双击语义：切到脚本所在目录——脚本里 open("隔壁文件.txt")
    # 这类相对路径找得到旁边的东西（与旧版 pypprun.bat 的
    # pushd %~dp1 行为一致）。
    # 注意：命令行 pypp x.cpy 不切（保持 Python 习惯：
    # 相对路径相对于用户当前目录）；本运行器是双击专用 UI。
    try:
        os.chdir(folder or '.')
    except Exception:
        pass
    _setup_console(f'py++ · {name}')
    # 交互式控制台先清屏，给双击一个干净的画布
    try:
        if sys.stdout.isatty():
            os.system('cls' if os.name == 'nt' else 'clear')
    except Exception:
        pass

    # 引擎预判（find_toolchain 内部有缓存，成本 ~13ms）
    engine = '内置 VM 模式'
    try:
        engine = ('原生高速模式' if find_toolchain()
                  else '内置 VM 模式（未找到 C 编译器）')
    except Exception:
        pass

    print(_box([
        ('py++', f'运行器 v{VERSION}   {name}'),
        ('目录', folder),
        ('引擎', engine),
        ('开始', time.strftime('%H:%M:%S')),
    ]))
    print()
    # 必须 flush：Python stdout 在管道里是块缓冲，不刷的话子进程的
    # 输出会冲到信息框前面（管道场景顺序错乱）。
    sys.stdout.flush()

    # ---- 编译 + 运行（stream=True：子进程直连控制台，提示语实时可见）----
    try:
        with open(path, 'r', encoding='utf-8') as f:
            source = f.read()
    except Exception as e:
        print(_box([(_RED + '读取失败' + _RESET,
                     f'{type(e).__name__}: {e}')], accent=_RED))
        if pause:
            wait_key()
        return 1

    t0 = time.perf_counter()
    result = compile_and_run(source, filename=path, stream=True)
    total_ms = (time.perf_counter() - t0) * 1000

    # ---- 尾部状态框 ----
    sys.stdout.flush()
    if result.error:
        print()
        print(_box([(_RED + '运行出错' + _RESET, ''),
                    result.error.strip()], accent=_RED))
        code = result.exit_code if result.exit_code is not None else 1
    else:
        ok = result.success and (result.exit_code in (0, None))
        rows = [
            (_GREEN + '运行结束' + _RESET if ok else _RED + '运行结束' + _RESET,
             f'{_mode_label(result.mode)} · 编译 {_fmt_ms(result.compile_ms)}'
             f' · 运行 {_fmt_ms(result.run_ms)}'),
            ('退出码', str(result.exit_code if result.exit_code is not None
                          else (0 if ok else 1))),
        ]
        if result.repairs:
            rows.append(('小错误', f'已自动修复 {result.repairs} 处，不影响运行'))
        if result.from_cache:
            rows.append(('编译缓存', '复用上次结果'))
        print()
        print(_box(rows, accent=_GREEN if ok else _RED))
        code = (result.exit_code if result.exit_code is not None
                else (0 if result.success else 1))

    if pause:
        print()
        print(f'{_DIM}按任意键关闭窗口…{_RESET}')
        wait_key()
    else:
        print(f'{_DIM}总耗时 {_fmt_ms(total_ms)}{_RESET}')
    return code


def runner_bat_content(pyexe, entry):
    """生成 pypprun.bat 内容：双击时由它拉起 runner.py。

    由安装器生成（它才知道 pyexe 和安装位置）——注册表 assoc 只负责
    关联，不负责写文件。

    ⚠️ 本文件必须**纯 ASCII**：bat 是 cmd.exe 逐字节按 OEM 代码页
    （中文 Windows = GBK）解析的，chcp 65001 只影响之后的显示、
    不影响 cmd 对 bat 自身的解析。UTF-8 中文注释会字节错位，
    严重时把注释和下一行命令粘成一整行（实测：'...都在' 不是内部
    或外部命令，整个启动失败）。界面/提示的中文都在 runner.py 里。
    """
    return (
        '@echo off\r\n'
        'rem ============================================================\r\n'
        'rem  py++ double-click runner (generated, do not edit)\r\n'
        'rem  UI / live output / pause logic all live in runner.py.\r\n'
        'rem  This bat only sets UTF-8 console and launches it.\r\n'
        'rem ============================================================\r\n'
        'chcp 65001 >nul\r\n'
        f'"{pyexe}" "{entry}" "%~1"\r\n'
    )


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    args = [a for a in argv if not a.startswith('-')]
    pause = '--no-pause' not in argv
    if not args:
        print(_box([('用法', f'python runner.py <文件.cpy>'),
                    ('', '把 .cpy 文件拖到 pypprun.bat 上也可以')]))
        return 1
    return run_file(args[0], pause=pause)


if __name__ == '__main__':
    sys.exit(main())
