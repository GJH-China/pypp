"""
py++ vs C++ 性能对比脚本
用法：python bench/run_bench.py
需要系统有 gcc。会编译 bench.cpy（经 py++）和 bench.cpp（直接 gcc），
各跑多次取中位数，最后给出倍率。
"""

import os
import sys
import time
import subprocess
import statistics
import tempfile
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, 'src')
sys.path.insert(0, SRC)

from pypp import find_gcc, compile_and_run

RUNS = 5
# 运行时输入：让循环规模在运行期才确定，避免 gcc 把循环常量折叠掉
BENCH_STDIN = "0\n"


def time_cmd(cmd, stdin=None, env=None):
    times = []
    out = ""
    for _ in range(RUNS):
        t0 = time.perf_counter()
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600,
                           input=stdin, env=env)
        times.append(time.perf_counter() - t0)
        out = r.stdout
    return statistics.median(times), out


def main():
    from pypp import find_toolchain, _toolchain_env
    tool = find_toolchain()
    if not tool:
        print("未找到可用的 C 工具链，无法进行原生性能对比。")
        print("py++ 会使用内置 VM 引擎（速度不具可比性）。")
        return 1

    gcc = tool['path']
    extra = tool.get('extra', [])
    env = _toolchain_env(tool)
    print(f"编译器: {gcc}\n")
    tmp = tempfile.mkdtemp(prefix="pypp_bench_")

    # ---- C++ 基线 ----
    print("编译 C++ 基线 ...")
    cpp_exe = os.path.join(tmp, "bench_cpp.exe" if os.name == 'nt' else "bench_cpp")
    cmd = [gcc, os.path.join(HERE, "bench.cpp"), "-O2", "-o", cpp_exe] + extra
    if os.name != 'nt':
        cmd.append('-lstdc++')
    r = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if r.returncode != 0:
        print("C++ 基线编译失败：", (r.stderr or "")[:600])
        return 1

    t_cpp, out_cpp = time_cmd([cpp_exe], stdin=BENCH_STDIN, env=env)
    print(f"C++   用时 {t_cpp:.3f}s")
    print(f"C++   输出: {out_cpp.strip()}\n")

    # ---- py++ 原生模式 ----
    print("编译 py++ 程序（原生模式）...")
    src = open(os.path.join(HERE, "bench.cpy"), encoding='utf-8').read()
    from lexer import Lexer
    from parser import Parser
    from codegen import CGenerator
    prog = Parser(Lexer(src, repair_log=[]).tokenize(), repair_log=[]).parse()
    c_code = CGenerator().generate(prog)
    cfile = os.path.join(tmp, "bench_pypp.c")
    with open(cfile, 'w', encoding='utf-8') as f:
        f.write(c_code)
    pypp_exe = os.path.join(tmp, "bench_pypp.exe" if os.name == 'nt' else "bench_pypp")
    cmd = [gcc, cfile, "-O2", "-o", pypp_exe] + extra
    if os.name != 'nt':
        cmd.append('-lm')
    r = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if r.returncode != 0:
        print("py++ 编译失败：", (r.stderr or "")[:600])
        return 1

    t_pypp, out_pypp = time_cmd([pypp_exe], stdin=BENCH_STDIN, env=env)
    print(f"py++  用时 {t_pypp:.3f}s")
    print(f"py++  输出: {out_pypp.strip()}\n")

    # ---- 结果 ----
    ratio = t_pypp / t_cpp if t_cpp > 0 else float('inf')
    print("=" * 46)
    print(f"  C++   基线     : {t_cpp:.3f}s")
    print(f"  py++  原生模式 : {t_pypp:.3f}s")
    print(f"  倍率 (py++/C++) : {ratio:.3f}x")
    if ratio <= 1.3:
        print("  ✓ 达到目标：在 C++ 的 1.3 倍以内（即 ±0.3 倍）")
    else:
        print(f"  ! 超出目标，需进一步优化")
    print("=" * 46)

    shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
