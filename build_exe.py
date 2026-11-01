"""
把 py++ 图形安装器打包成单文件 exe（PyInstaller）。

用法：
    python build_exe.py                # 构建 dist/installer/pypp-1.1.0-setup.exe
    python build_exe.py --onedir       # 目录版（启动更快，便于排查）
    python build_exe.py --clean        # 只清理构建产物

产出的 exe 把整个项目作为「资源」带在身上：
    src/ examples/ web/ docs/ README.md pget.py runtime/pypp-runtime.zip …
安装时再解压到用户选的目录，所以 exe 本身就相当于离线安装包。

⚠️ 关键设计（别踩）：
   py++ 编译器是 Python 写的，用户机器上很可能没有 Python。
   所以 exe 内嵌一份「便携 Python 运行时」（由 build_runtime.py 生成），
   安装时释放到 ~/.pypp/runtime/，pypp.bat 指向它。
   没有这一步的话，exe 产生的 pypp.bat 会指向 exe 自己，根本跑不起来。

⚠️ 单文件 exe 不含 557MB 的便携 gcc（体积原因）。
   安装器会在选项页让用户选择「在线下载」或「跳过」，
   详见 install_gui.py 的 gcc_mode。
"""

import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, 'src'))

try:
    from pypp import VERSION
except Exception:
    VERSION = '1.0.0'

APP_NAME = f"pypp-{VERSION}-setup"
DIST_DIR = os.path.join(HERE, 'dist', 'installer')
BUILD_DIR = os.path.join(HERE, 'build', 'installer')
SPEC_DIR = os.path.join(HERE, 'build')

# 便携运行时（build_runtime.py 的产物）
RUNTIME_ZIP = os.path.join(HERE, 'dist', 'runtime', 'pypp-runtime.zip')

# 要随 exe 一起分发的东西（相对项目根目录）
DATA_ITEMS = [
    ('src', 'src'),
    ('examples', 'examples'),
    ('web', 'web'),
    ('docs', 'docs'),
    ('bench', 'bench'),
]
# (源文件, 包内目标目录)。图标必须落到 assets/ 子目录 ——
# install_gui._step_launcher 是按 HERE/assets/<名字>.ico 找的。
DATA_FILES = [
    ('README.md', '.'),
    ('pget.py', '.'),
    ('pget.bat', '.'),
    ('pyget.bat', '.'),
    ('cget.bat', '.'),
    ('cpget.bat', '.'),
    ('docs/快速精通.md', 'docs'),
    ('assets/cpy.ico', 'assets'),
    ('assets/pypp.ico', 'assets'),
]

# exe 自身图标（PyInstaller --icon 只认 .ico）
EXE_ICON = os.path.join(HERE, 'assets', 'pypp.ico')


def clean():
    for d in (BUILD_DIR, DIST_DIR):
        if os.path.isdir(d):
            shutil.rmtree(d, ignore_errors=True)
            print(f"已清理 {d}")


# ---------------------------------------------------------------------------
# 构建期审计：src/ 里的标准库 import 必须"打包得进 exe"
# ---------------------------------------------------------------------------
# src/ 是用 --add-data 塞进 exe 的**数据文件**，PyInstaller 的静态分析
# 只扫入口脚本（install_gui.py），看不到 src/*.py 的 import。所以这里
# 一旦出现安装器自己没引用到的标准库，运行到那条 import 就会
# ModuleNotFoundError —— 而且是在用户机器上、安装到一半时崩。
#
# 已知由入口脚本（tkinter/urllib 那条链）间接带进包的白名单。
# 注：装上之后用的是便携 Python 运行时（自带全部标准库），所以白名单
# 只约束"exe 自己要 import 的那些"。
#
# ⚠️ threading：pypp.py 用它实现 passthrough（边运行边输出），
#    是**模块级** import —— 不加进来 exe 一导入就 ModuleNotFoundError。
#    （2026-10-19 被本审计拦下过，别用 --force 绕过。）
_EXE_SAFE_STDLIB = {
    'os', 'sys', 're', 'time', 'json', 'io', 'math',
    'shutil', 'subprocess', 'tempfile', 'hashlib', 'importlib',
    'unicodedata', 'glob', 'winreg', 'dataclasses', 'typing',
    'urllib', 'zipfile', 'textwrap', 'string', 'collections',
    'functools', 'itertools', 'warnings', 'traceback', 'stat',
    'platform', 'getpass', 'statistics', 'threading',
}


def _is_project_module(mod):
    """src/ 内部互引（lexer/parser/...）不算外部依赖。"""
    return os.path.isfile(os.path.join(HERE, 'src', mod + '.py'))


def audit_src_imports():
    """
    扫 src/*.py 的 import，找出 exe 里可能没有的模块。

    区分两级危险：
      · 模块级 import —— exe 导入该文件时立刻炸，**必须**处理
      · 函数内 import —— 只有那个函数在 exe 里被调用才炸，提示但不拦
    返回 (是否有模块级问题, 消息列表)。
    """
    import ast
    hard, soft = [], []

    for fn in sorted(os.listdir(os.path.join(HERE, 'src'))):
        if not fn.endswith('.py'):
            continue
        path = os.path.join(HERE, 'src', fn)
        try:
            with open(path, 'r', encoding='utf-8') as f:
                tree = ast.parse(f.read(), path)
        except SyntaxError as e:
            hard.append(f"{fn}: 语法错误 {e}")
            continue

        # 收集所有函数节点，用来判断 import 是在函数里还是模块级
        funcs = [n for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]

        def _owner(node):
            for f in funcs:
                if any(d is node for d in ast.walk(f)):
                    return f.name
            return None

        for node in ast.walk(tree):
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name.split('.')[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:          # 相对导入，src 内部互引
                    continue
                if node.module:
                    mods = [node.module.split('.')[0]]
            for mod in mods:
                if not mod or mod in _EXE_SAFE_STDLIB:
                    continue
                if _is_project_module(mod):
                    continue
                owner = _owner(node)
                line = getattr(node, 'lineno', '?')
                where = f"{fn}:{line} import {mod}"
                if owner:
                    soft.append(f"{where}  （函数 {owner}() 内，"
                                f"exe 不调用它就不炸）")
                else:
                    hard.append(where + "  （模块级，exe 导入即炸）")

    if soft:
        print("· 提示：src/ 里有函数内的白名单外 import（当前不影响 exe）：")
        for s in soft:
            print("    " + s)
    if hard:
        print("!" * 62)
        print("! 错误：src/ 有模块级 import 在 exe 白名单外")
        for h in hard:
            print("!   " + h)
        print("! 处置：改用白名单内的模块，或")
        print("!   1) 加进 _EXE_SAFE_STDLIB，2) 给 PyInstaller 传 --hidden-import")
        print("!" * 62)
        return False, hard
    print("构建期审计：src/ 的模块级 import 全部在 exe 安全白名单内")
    return True, []


def build(onedir=False):
    args = [
        sys.executable, '-m', 'PyInstaller',
        '--noconfirm',
        '--name', APP_NAME,
        '--windowed',                  # GUI 程序，不弹控制台
        '--onedir' if onedir else '--onefile',
        '--distpath', DIST_DIR,
        '--workpath', BUILD_DIR,
        '--specpath', SPEC_DIR,
        # tkinter 是 GUI 必需，PyInstaller 会自动带上；
        # 这里显式排除体积大又用不到的库，减小 exe
        '--exclude-module', 'numpy',
        '--exclude-module', 'matplotlib',
        '--exclude-module', 'PIL',
        '--exclude-module', 'pandas',
        '--exclude-module', 'scipy',
        '--exclude-module', 'pytest',
        '--exclude-module', 'setuptools',
        '--exclude-module', 'pip',
        '--exclude-module', 'unittest',
    ]
    # 图标（有就用）。assets/pypp.ico 由 build_icons.py 生成；
    # 以前指向 docs/images/pypp.ico，那个文件其实不存在，等于没有图标。
    ico = EXE_ICON
    if os.path.isfile(ico):
        args += ['--icon', ico]
        print(f"  exe 图标：{os.path.relpath(ico, HERE)}")
    else:
        print("  ⚠ 没有 assets/pypp.ico，exe 将用默认图标。"
              "请先运行：python build_icons.py")

    for src, dst in DATA_ITEMS:
        p = os.path.join(HERE, src)
        if os.path.isdir(p):
            args += ['--add-data', f'{p}{os.pathsep}{dst}']
        else:
            print(f"  跳过（不存在）: {src}")
    for rel, dst in DATA_FILES:
        p = os.path.join(HERE, rel)
        if os.path.isfile(p):
            args += ['--add-data', f'{p}{os.pathsep}{dst}']
        else:
            print(f"  跳过（不存在）: {rel}")

    # 便携 Python 运行时（必须有，否则装出来的 pypp 命令不可用）
    if os.path.isfile(RUNTIME_ZIP):
        args += ['--add-data', f'{RUNTIME_ZIP}{os.pathsep}runtime']
        print(f"  内嵌便携运行时：{RUNTIME_ZIP}")
    else:
        print("  ⚠ 未找到便携运行时，将不会内嵌。")
        print("    请先运行：  python build_runtime.py")

    args.append(os.path.join(HERE, 'install_gui.py'))

    print("执行 PyInstaller …")
    print("  " + " ".join(f'"{a}"' if ' ' in a else a for a in args))
    print()
    r = subprocess.run(args, cwd=HERE)
    if r.returncode != 0:
        print("\n构建失败。")
        return 1

    # 报告产物
    if onedir:
        exe = os.path.join(DIST_DIR, APP_NAME, APP_NAME + '.exe')
    else:
        exe = os.path.join(DIST_DIR, APP_NAME + '.exe')
    if os.path.isfile(exe):
        size = os.path.getsize(exe) / 1048576
        print("")
        print("=" * 60)
        print(f"构建成功：{exe}")
        print(f"体积：{size:.1f} MB")
        if onedir:
            total = sum(
                os.path.getsize(os.path.join(dp, f))
                for dp, _, fs in os.walk(os.path.dirname(exe))
                for f in fs)
            print(f"整个目录：{total / 1048576:.1f} MB")
        print("=" * 60)
        return 0
    print("构建结束，但没找到产物 exe。")
    return 1


def main():
    if '--clean' in sys.argv:
        clean()
        return 0
    # 先审计再打包：src/ 的 import 问题只在"运行 exe"时才暴露，
    # 而在用户机器上就是"装到一半崩"，必须挡在构建期。
    ok, _problems = audit_src_imports()
    if not ok:
        if '--force' in sys.argv:
            print("（--force：忽略审计结果继续打包）")
        else:
            print("已中止打包。确认无误后加 --force 重试。")
            return 1
    clean()
    return build(onedir='--onedir' in sys.argv)


if __name__ == '__main__':
    sys.exit(main())
