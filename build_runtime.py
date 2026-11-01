"""
构建「便携 Python 运行时」压缩包（供 exe 安装器内嵌）。

为什么需要它：
  py++ 编译器本身是 Python 写的（src/pypp.py）。用户装完 py++ 后要能直接
  运行 `pypp 程序.cpy`，所以目标机器上必须有 Python。
  但装 exe 的用户很可能没有 Python —— 于是安装器需要自带一份。

做法：从当前 Python 安装里抽取「能跑 py++ 的最小集合」：
  python.exe / pythonw.exe / python3XX.dll / DLLs / Lib（去测试与缓存）
精简后约 20~30MB，塞进 exe 后用户无需另装 Python。

用法：
    python build_runtime.py                    # 生成 dist/runtime/pypp-runtime.zip
    python build_runtime.py --from <Python根>  # 指定源 Python
    python build_runtime.py --check            # 只报告会装哪些文件
"""

import os
import shutil
import sys
import zipfile
import argparse

HERE = os.path.dirname(os.path.abspath(__file__))

# 必须带的顶层文件
CORE_FILES = [
    'python.exe', 'pythonw.exe',
    'python3.dll', 'python3*.dll',
    'LICENSE.txt', 'vcruntime*.dll',
]

# Lib/ 下要剔除的（体积大且 py++ 用不到）
LIB_SKIP_DIRS = {
    'test', 'tests', 'idlelib', 'tkinter/test', 'unittest/test',
    'lib2to3', 'ensurepip', 'distutils', 'site-packages',
    '__pycache__', 'turtledemo', 'sqlite3/test', 'ctypes/test',
}
LIB_SKIP_EXT = {'.pyc', '.pyo', '.pdb'}

# 需要保留的 Lib 子目录（其余按需保留 .py）
# tkinter 要留（安装器 GUI 需要，虽然已打包进 exe，
# 但用户可能想用 python 再跑别的）—— 这里保守保留。
KEEP_DIRS = {'tkinter', 'encodings', 'importlib', 'json', 'urllib',
             'xml', 'email', 'html', 'http', 'logging', 'collections',
             'concurrent', 'ctypes', 'asyncio', 'multiprocessing',
             'wsgiref', 'unittest', 'sqlite3', 'zipfile', 'zipapp'}


def human(n):
    return f"{n / 1048576:.1f} MB"


def collect_files(py_root, verbose=False):
    """收集便携运行时需要的文件，返回 [(绝对路径, zip 内相对路径)]。"""
    items = []
    import glob as _glob

    # 1) 顶层核心文件
    for pat in CORE_FILES:
        for hit in _glob.glob(os.path.join(py_root, pat)):
            if os.path.isfile(hit) and '__pycache__' not in hit:
                items.append((hit, os.path.basename(hit)))

    # 2) Lib/（精简）
    lib = os.path.join(py_root, 'Lib')
    if os.path.isdir(lib):
        for dp, dirs, files in os.walk(lib):
            rel_dir = os.path.relpath(dp, lib).replace('\\', '/')
            if rel_dir == '.':
                rel_dir = ''
            # 剪掉不要的目录
            top = rel_dir.split('/')[0] if rel_dir else ''
            if top in LIB_SKIP_DIRS:
                dirs[:] = []
                continue
            dirs[:] = [d for d in dirs
                       if d != '__pycache__' and
                       (not rel_dir or f'{rel_dir}/{d}' not in LIB_SKIP_DIRS)]
            for fn in files:
                ext = os.path.splitext(fn)[1].lower()
                if ext in LIB_SKIP_EXT:
                    continue
                src = os.path.join(dp, fn)
                dst = 'Lib/' + (f'{rel_dir}/{fn}' if rel_dir else fn)
                items.append((src, dst))

    # 3) DLLs/（含 tkinter 的 _tkinter.pyd 与 tcl/tk）
    dlls = os.path.join(py_root, 'DLLs')
    if os.path.isdir(dlls):
        for dp, dirs, files in os.walk(dlls):
            dirs[:] = [d for d in dirs if d != '__pycache__']
            rel_dir = os.path.relpath(dp, dlls).replace('\\', '/')
            if rel_dir == '.':
                rel_dir = ''
            for fn in files:
                if fn.endswith(('.pdb',)):
                    continue
                src = os.path.join(dp, fn)
                dst = 'DLLs/' + (f'{rel_dir}/{fn}' if rel_dir else fn)
                items.append((src, dst))

    # 去重（同一 zip 路径只留一个）
    # 注意 seen 是 {zip内路径: 源文件路径}，排序后要还原成 (src, dst)
    seen = {}
    for src, dst in items:
        if dst not in seen:
            seen[dst] = src
    pairs = [(seen[d], d) for d in sorted(seen)]
    if verbose:
        total = 0
        for s, _ in pairs:
            try:
                total += os.path.getsize(s)
            except OSError:
                pass
        print(f"  共 {len(pairs)} 个文件，{human(total)}")
    return pairs


def build(py_root, out_zip, check_only=False):
    print(f"源 Python：{py_root}")
    items = collect_files(py_root, verbose=True)
    total = sum(os.path.getsize(s) for s, _ in items)
    print(f"解压后体积：{human(total)}")

    if check_only:
        print("\n前 30 项：")
        for s, d in items[:30]:
            print(f"  {d}")
        return 0

    os.makedirs(os.path.dirname(out_zip), exist_ok=True)
    print(f"\n正在写入 {out_zip} …")
    done = 0
    with zipfile.ZipFile(out_zip, 'w', zipfile.ZIP_DEFLATED,
                         compresslevel=6) as z:
        for src, dst in items:
            try:
                z.write(src, dst)
                done += 1
                if done % 500 == 0:
                    print(f"  已打包 {done}/{len(items)} …")
            except OSError as e:
                print(f"  跳过 {dst}: {e}")
    size = os.path.getsize(out_zip)
    print("")
    print("=" * 60)
    print(f"便携运行时已生成：{out_zip}")
    print(f"压缩后：{human(size)}   解压后：{human(total)}   文件数：{done}")
    print("=" * 60)
    return 0


def main():
    ap = argparse.ArgumentParser(description='构建便携 Python 运行时')
    ap.add_argument('--from', dest='src', default=None,
                    help='源 Python 根目录（默认用当前解释器）')
    ap.add_argument('--out', default=os.path.join(
        HERE, 'dist', 'runtime', 'pypp-runtime.zip'))
    ap.add_argument('--check', action='store_true', help='只列出文件清单')
    args = ap.parse_args()

    py_root = args.src or sys.base_prefix
    if not os.path.isfile(os.path.join(py_root, 'python.exe')):
        print(f"错误：{py_root} 里找不到 python.exe")
        return 1
    return build(py_root, os.path.abspath(args.out), args.check)


if __name__ == '__main__':
    sys.exit(main())
