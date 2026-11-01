"""
py++ 主驱动 (pypp)
整条编译/执行流水线：
  源码 -> [拼写修复] -> [词法] -> [语法] -> 尝试 gcc -O2 编译执行
                                        -> 若无 gcc，降级到内置 VM 解释执行

无论哪条路径，遇到"小错误"都自动修复并友好提示。
"""

import os
import re
import sys
import subprocess
import shutil
import tempfile
import threading
import time
import hashlib

# 保证能 import 同目录模块
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _force_utf8_io():
    """把本进程的 stdout/stderr 钉成 UTF-8。

    中文 Windows 的默认编码是 GBK。父进程（runner.py / IDE / 运行器）
    通常按 UTF-8 解码读我们，若我们按 GBK 输出就会**整段乱码**。
    反过来，控制台代码页是 GBK 时写 UTF-8 也会花屏 —— 所以两件事都要做：
      ① 交互式控制台：SetConsoleOutputCP(65001) 把代码页切到 UTF-8
      ② Python 层：reconfigure 成 UTF-8（覆盖 locale 默认）

    非 tty（管道/IDE）场景只做 ②：代码页属于宿主控制台，别去改人家的。
    errors='replace' 兜底，宁可个别字符变问号，也不要整段抛异常丢输出。
    """
    interactive = False
    try:
        interactive = sys.stdout.isatty()
    except Exception:
        pass

    if os.name == 'nt' and interactive:
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        except Exception:
            pass

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass

    # 让以后 spawn 的子进程也走 UTF-8（gcc 诊断、原生 exe 输出）
    os.environ.setdefault('PYTHONIOENCODING', 'utf-8')
    os.environ.setdefault('PYTHONUTF8', '1')


_force_utf8_io()

from lexer import Lexer, LexerError
from parser import Parser, ParseError
from codegen import CGenerator, NeedsVMError
from interpreter import Interpreter
from repair import (RepairReport, fix_source, format_welcome_fixed_msg)
import repair as _repair


VERSION = "1.1.0"

# ---- 编译器探测缓存 ----
_gcc_info_cache = None


def _probe_toolchain(gcc_path):
    """
    给定 gcc 路径，返回可用的工具链信息 dict，或 None。
    Windows/MinGW 的关键点（实测得出）：
      1. gcc 的 cc1.exe 需要同目录 DLL，必须把 bin 放进 PATH，否则静默失败
      2. ld 需要在启动文件目录找 default-manifest.o，必须用 -B 指定前缀 lib 目录
    """
    bin_dir = os.path.dirname(os.path.abspath(gcc_path))
    prefix = os.path.dirname(bin_dir)          # 例如 .../mingw64
    info = {'path': gcc_path, 'bin': bin_dir, 'prefix': prefix, 'extra': []}

    # 推理启动文件目录（-B 目标）
    import glob as _glob
    lib_candidates = [
        os.path.join(prefix, 'x86_64-w64-mingw32', 'lib'),
        os.path.join(prefix, 'lib'),
    ]
    for lc in lib_candidates:
        if os.path.isdir(lc):
            info['extra'].append(f'-B{lc}{os.sep}')
            info['startlib'] = lc
            break

    # 实测验证：能否成功编译一个最小程序
    if not _verify_toolchain(info):
        return None
    return info


def _verify_toolchain(info):
    """
    验证工具链是否可用 —— 只跑 gcc --version，不编译程序。

    性能考量（实测）：
      - gcc --version            ~13 ms
      - 编译一个 hello 并运行     ~289 ms   ← 原先的做法，纯浪费
    每次运行都多做一次编译，只为"提前确认工具链能用"，代价太高。
    而真正的编译失败本来就有降级 VM 的兜底，不需要预先体检。
    """
    try:
        env = _toolchain_env(info)
        r = subprocess.run([info['path'], '--version'],
                           capture_output=True, text=True, timeout=15, env=env)
        return r.returncode == 0
    except Exception:
        return False


def _toolchain_env(info):
    """构造包含工具链 bin 的环境变量（Windows 下 DLL 查找必需）。"""
    env = dict(os.environ)
    if os.name == 'nt':
        sep = ';'
        env['PATH'] = info['bin'] + sep + env.get('PATH', '')
    else:
        sep = ':'
        env['PATH'] = info['bin'] + sep + env.get('PATH', '')
    return env


def find_gcc():
    """返回可用的 gcc 可执行文件路径，或 None。"""
    info = find_toolchain()
    return info['path'] if info else None


def find_toolchain():
    """寻找并验证完整可用的 C 工具链，返回 info dict 或 None。"""
    global _gcc_info_cache
    if _gcc_info_cache is not None:
        return _gcc_info_cache or None

    import glob as _glob
    candidates = []
    # 1. PATH 中的
    for name in ('gcc', 'clang'):
        p = shutil.which(name)
        if p:
            candidates.append(p)
    # 2. 项目自带的便携工具链（优先，最可靠）
    here = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(here)
    bundled = os.path.join(project_root, 'tools', 'mingw64', 'mingw64', 'bin', 'gcc.exe')
    if os.path.isfile(bundled):
        candidates.insert(0, bundled)
    # 3. 常见安装位置
    patterns = [
        r"C:\mingw64\bin\gcc.exe",
        r"C:\MinGW\bin\gcc.exe",
        r"C:\msys64\mingw64\bin\gcc.exe",
        r"C:\Program Files\mingw64\bin\gcc.exe",
        r"C:\Program Files (x86)\mingw-w64\**\bin\gcc.exe",
        os.path.expanduser(r"~\AppData\Local\Microsoft\WinGet\Packages\**\mingw64\bin\gcc.exe"),
        os.path.expanduser(r"~\AppData\Local\Microsoft\WinGet\Packages\**\bin\gcc.exe"),
        r"C:\ProgramData\mingw64\bin\gcc.exe",
    ]
    for pat in patterns:
        for hit in _glob.glob(pat, recursive=True):
            if os.path.isfile(hit):
                candidates.append(hit)

    for c in candidates:
        info = _probe_toolchain(c)
        if info:
            _gcc_info_cache = info
            return info
    _gcc_info_cache = False
    return None


# ---------------------------------------------------------------------------
# CPython 开发环境探测
# ---------------------------------------------------------------------------
# 目的：程序里写了 import 时，原生模式要把 CPython 解释器嵌进可执行文件，
#       因此需要 include/Python.h + 导入库(python3XX.lib) + 共享库(python3XX.dll)。
#       三者齐全才可用；缺任何一样就退回 VM —— VM 本身就跑在 Python 里，
#       import 功能完全一致，只是慢一点。所以 import 永远是"能用"的。
# ---------------------------------------------------------------------------

_pydev_cache = None

# ---------------------------------------------------------------------------
# C 库链接指令
# ---------------------------------------------------------------------------
# 源码里写 `#link z` 或 `#link -lz -L/path`，编译时自动追加给 gcc。
# 这是 py++ 调用 C 库的"接线"部分：extern 声明负责让编译器认识函数，
# #link 负责让链接器找到实现。
# ---------------------------------------------------------------------------

_LINK_RE = re.compile(r'^[ \t]*#[ \t]*link[ \t]+(.+?)[ \t]*$', re.MULTILINE)


def _collect_link_flags(source: str):
    """扫描 #link 指令，返回要追加给 gcc 的链接参数。"""
    flags = []
    if not source:
        return flags
    for m in _LINK_RE.finditer(source):
        for tok in m.group(1).split():
            f = tok if tok.startswith('-') else ('-l' + tok)
            if f not in flags:
                flags.append(f)
    return flags


# 常见 C 库 → 头文件。用于 extern 签名校验。
# 只收"头文件名可确定"的常见库；查不到就跳过校验（宁可漏报，不可误报）。
_LIB_HEADERS = {
    'z': 'zlib.h',
    'zlib': 'zlib.h',
    'm': 'math.h',
    'pthread': 'pthread.h',
    'curl': 'curl/curl.h',
    'sqlite3': 'sqlite3.h',
    'png': 'png.h',
    'jpeg': 'jpeglib.h',
    'ssl': 'openssl/ssl.h',
    'crypto': 'openssl/crypto.h',
}


def _find_include_dirs(toolchain):
    """给出 gcc 的头文件搜索目录（用于找 C 标准/第三方头文件）。"""
    dirs = []
    if not toolchain:
        return dirs
    prefix = toolchain.get('prefix')
    if prefix:
        dirs.append(os.path.join(prefix, 'include'))
        dirs.append(os.path.join(prefix, 'x86_64-w64-mingw32', 'include'))
    return [d for d in dirs if os.path.isdir(d)]


def _strip_c_comments(text):
    text = re.sub(r'/\*.*?\*/', ' ', text, flags=re.S)
    return re.sub(r'//[^\n]*', ' ', text)


def _count_c_params(param_text):
    """数一个 C 参数列表里的参数个数（处理函数指针等嵌套括号）。

    返回 (个数, 是否含变参)。函数指针参数里的逗号不能算分隔符。
    """
    p = (param_text or '').strip()

    # ⚠️ 剥掉导出修饰宏：C 头文件里常见
    #      ZEXPORT / ZEXTERN / WINAPI / __cdecl / const
    #    实测 zlib.h 的 `zlibVersion(void)` 因为带 ZEXPORT 被数成 1 个参数，
    #    导致正确代码被误报为签名不符。这些宏不占参数位。
    p = re.sub(r'\b(ZEXPORT|ZEXTERN|ZEXPORTVA|WINAPI|APIENTRY|CALLBACK|'
               r'__cdecl|__stdcall|__fastcall|__declspec\s*\([^)]*\))\b',
               ' ', p)
    p = re.sub(r'\s+', ' ', p).strip()

    if not p or p == 'void':
        return 0, False

    parts, depth, buf = [], 0, []
    for ch in p:
        if ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
        if ch == ',' and depth == 0:
            parts.append(''.join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append(''.join(buf))

    has_varargs = any('...' in x for x in parts)
    # 只剩修饰符/空白的"参数"不算真参数
    real = [x for x in parts
            if x.strip() and x.strip() not in ('void', 'const', 'register')]
    return len(real), has_varargs


def _real_c_signature(name, link_flags, toolchain):
    """从真实头文件里找某个 C 函数的参数个数。

    返回 (参数个数, 头文件名)；查不到返回 None。
    ⚠️ 只做"个数"比对，不比对类型 —— 类型比对需要完整 typedef 展开，
       误报风险高；而参数个数不符是段错误的主因，先堵这个。
    """
    # #link z → 找 zlib.h
    header = None
    for f in link_flags:
        if not f.startswith('-l'):
            continue
        lib = f[2:]
        if lib in _LIB_HEADERS:
            header = _LIB_HEADERS[lib]
            break
    if header is None:
        return None

    incdirs = _find_include_dirs(toolchain)
    text = None
    for d in incdirs:
        p = os.path.join(d, *header.split('/'))
        if os.path.isfile(p):
            try:
                with open(p, 'r', encoding='utf-8', errors='replace') as fh:
                    text = fh.read()
                break
            except OSError:
                continue
    if text is None:
        return None

    text = _strip_c_comments(text)
    # ⚠️ 必须先去掉预处理行（#define / #include）。
    #    实测 zlib.h 里有 `#define zlib_version zlibVersion()`，
    #    不去掉的话正则先命中这个宏，抓出一大段跨行垃圾。
    text = re.sub(r'^[ \t]*#[^\n]*$', ' ', text, flags=re.M)
    text = re.sub(r'\s+', ' ', text)

    # 只认"返回类型 + 函数名 + 参数表 + 分号"这种标准声明：
    # 参数表里不允许出现嵌套括号之外的字母串过多（防止跨行吞掉别的声明）
    pat = re.compile(
        r'\b([A-Za-z_][A-Za-z0-9_ \t\*]*?)\b' + re.escape(name) +
        r'\s*\(([^;{}()]*)\)\s*;')
    m = pat.search(text)
    if not m:
        return None
    n, varargs = _count_c_params(m.group(2))
    if varargs:
        return None   # 变参函数无法校验个数
    return n, header


def _validate_externs(program, source=''):
    """校验源码里的 extern 声明与真实 C 签名是否一致。

    返回错误消息列表（空 = 通过）。
    """
    externs = {}
    for stmt in getattr(program, 'body', []) or []:
        if type(stmt).__name__ == 'ExternDecl' and getattr(stmt, 'name', None):
            externs[stmt.name] = stmt
    if not externs:
        return []

    # #link 不是 AST 节点，是源码指令 → 直接扫原文
    link_flags = _collect_link_flags(source)
    if not link_flags:
        return []

    toolchain = find_toolchain()
    problems = []
    for name, decl in externs.items():
        real = _real_c_signature(name, link_flags, toolchain)
        if real is None:
            continue
        real_n, header = real
        decl_n = len(getattr(decl, 'params', []) or [])
        if decl_n != real_n:
            problems.append(
                f"C 函数 {name}() 的实际签名有 {real_n} 个参数"
                f"（见 {header}），你声明了 {decl_n} 个。\n"
                f"  参数个数不符会让程序崩溃（不是小错误，无法自动修复）。\n"
                f"  请照着头文件改声明，例如：\n"
                f"    extern unsigned long crc32(unsigned long, const char*, unsigned int)"
            )
    return problems


def _packages_dir():
    """pget 安装的第三方包目录。"""
    override = os.environ.get('PYPP_PKG_DIR')
    if override:
        return override
    return os.path.join(os.path.expanduser('~'), '.pypp', 'packages')


# ---------------------------------------------------------------------------
# import 一个 pget 装好的 C 库
# ---------------------------------------------------------------------------
# `pget install zlib` 会生成 ~/.pypp/packages/_c/z.cpyi。用户在 .cpy 里写
# `import z` 时，py++ 发现存在这个声明文件，就把它的 #link 与 extern 声明
# 直接展开进源码 —— 于是"导入 C 库"和"导入 Python 库"用同一套语法，
# 而实际走的是零开销的编译期展开。
# ---------------------------------------------------------------------------

_IMPORT_LINE_RE = re.compile(
    # C++ 标准库叫 stdc++，所以名字里要允许 +（Python 的 import 不允许，
    # 但这一行本身就是被我们改写成 #link 的，不进 Python 导入器）
    r'^([ \t]*)import[ \t]+([A-Za-z_][A-Za-z0-9_.+\-]*)[ \t]*(?:as[ \t]+(\w+))?[ \t]*$',
    re.MULTILINE)


def _c_lib_dir():
    """cget / cpget 装好的声明文件目录（两个都扫）。"""
    pkg = _packages_dir()
    return [os.path.join(pkg, '_c'), os.path.join(pkg, '_cpp')]


def _find_c_decl(name):
    """找一个 C/C++ 库的 .cpyi 声明文件，返回路径或 None。"""
    safe = re.sub(r'[^A-Za-z0-9_.-]', '_', name)
    for d in _c_lib_dir():
        for cand in (name + '.cpyi', safe + '.cpyi'):
            p = os.path.join(d, cand)
            if os.path.isfile(p):
                return p
    return None


def _expand_c_imports(source):
    """
    把 `import <C库>` 展开成该库的 #link + extern 声明。

    返回 (新源码, 展开的库名列表)。只会替换真正找得到 .cpyi 的行，
    其余的 import 原样保留（交给正常的 Python 导入路径）。
    """
    if not source or 'import' not in source:
        return source, []

    expanded = []

    def _repl(m):
        indent, name, _alias = m.group(1), m.group(2), m.group(3)
        # 只认单段名字（C 库没有点分层级）
        if '.' in name:
            return m.group(0)
        decl = _find_c_decl(name)
        if not decl:
            return m.group(0)
        try:
            with open(decl, 'r', encoding='utf-8') as f:
                body = f.read()
        except OSError:
            return m.group(0)
        expanded.append(name)
        # 保留缩进（虽然 import 一般顶格），加注释说明来源
        pad = indent
        out = [f"{pad}# ↓ 由 import {name} 展开（pget 装好的 C 库声明）"]
        for ln in body.splitlines():
            out.append(pad + ln if ln.strip() else ln)
        return '\n'.join(out)

    return _IMPORT_LINE_RE.sub(_repl, source), expanded


def _ensure_vm_path():
    """
    把 pget 的包目录加进 VM 的 sys.path。
    这样"VM 用当前解释器、原生嵌入另一个 Python"时，两个引擎至少能看到
    同一批 pget 安装的第三方库（否则会出现"原生能跑、VM 找不到模块"）。
    """
    try:
        pkg = _packages_dir()
        if os.path.isdir(pkg) and pkg not in sys.path:
            sys.path.insert(0, pkg)
    except Exception:
        pass


def _probe_python_dev(root):
    """检查某个 Python 安装目录能否用于嵌入，返回 info dict 或 None。"""
    if not root or not os.path.isdir(root):
        return None
    inc = os.path.join(root, 'include')
    libs = os.path.join(root, 'libs')
    if not os.path.isfile(os.path.join(inc, 'Python.h')):
        return None

    info = {'root': os.path.normpath(root), 'include': inc,
            'libs': libs, 'libname': None, 'dll': None, 'version': None}

    if os.name == 'nt':
        # 导入库：libs/python313.lib
        if not os.path.isdir(libs):
            return None
        for f in sorted(os.listdir(libs)):
            m = re.match(r'(python\d)(\d+)\.lib$', f)
            if m:
                info['libname'] = f[:-4]          # python313
                info['version'] = m.group(1)[-1] + '.' + m.group(2)
                break
        if not info['libname']:
            return None
        dll = os.path.join(info['root'], info['libname'] + '.dll')
        if not os.path.isfile(dll):
            dll = None
            for f in os.listdir(info['root']):
                if f.lower().startswith('python3') and f.lower().endswith('.dll'):
                    dll = os.path.join(info['root'], f)
                    break
        if not dll:
            return None
        info['dll'] = dll
        return info

    # POSIX：libpython3.13.so
    import glob as _glob
    for pat in (os.path.join(root, 'libpython3*.so*'),
                os.path.join(root, 'lib', 'libpython3*.so*')):
        hits = [h for h in _glob.glob(pat) if not h.endswith('.a')]
        if hits:
            name = os.path.basename(hits[0])
            m = re.match(r'libpython(\d)\.(\d+)', name)
            if m:
                info['version'] = m.group(1) + '.' + m.group(2)
                info['libname'] = 'python' + m.group(1) + '.' + m.group(2)
                info['dll'] = hits[0]
                info['libs'] = os.path.dirname(hits[0])
                return info
    return None


def _score_python(root):
    """给候选 Python 打分：优先"用户自己的系统 Python"，避开 py++ 内部运行时。"""
    norm = root.replace('/', '\\').lower()
    score = 0
    if '.workbuddy-ai' in norm:                    # py++ 自带/内置的解释器
        score -= 100
    if 'programs\\python' in norm:                 # 官方安装器默认位置
        score += 60
    elif re.match(r'^[a-z]:\\python', norm):       # C:\Python313
        score += 50
    if 'program files' in norm:
        score += 30
    return score


def find_python_dev():
    """
    定位可用于嵌入的 CPython，返回 info dict 或 None。

    选择策略（重要）：
      · 优先用户自己的系统 Python —— 这样它已装的第三方库在原生模式下也能用；
      · 尽量避开 py++ 内部运行时（隔离目录，通常没有第三方库）；
      · 优先与"当前解释器同大版本"的 Python —— 因为 pget 装的 wheel 带
        cp313 之类的标签，版本必须对得上才能加载。
    """
    global _pydev_cache
    if _pydev_cache is not None:
        return _pydev_cache or None

    import glob as _glob
    cur = f"{sys.version_info[0]}.{sys.version_info[1]}"

    cands = []
    explicit = os.environ.get('PYPP_PYTHON')
    if explicit:
        cands.append(explicit)
    try:
        import sysconfig
        for k in ('installed_base', 'prefix', 'base'):
            v = sysconfig.get_config_var(k)
            if v:
                cands.append(v)
    except Exception:
        pass
    cands.append(sys.prefix)
    for pat in (r"C:\Python3*", r"C:\Program Files\Python3*",
                os.path.expanduser(r"~\AppData\Local\Programs\Python\Python3*"),
                r"C:\ProgramData\Python3*"):
        for hit in _glob.glob(pat):
            cands.append(hit)

    # 去重 → 打分排序 → 依次探测
    seen, uniq = set(), []
    for root in cands:
        if not root:
            continue
        key = os.path.normpath(root)
        if key in seen:
            continue
        seen.add(key)
        uniq.append(key)

    def rank(root):
        info_guess = _probe_python_dev(root)
        if not info_guess:
            return None
        s = _score_python(root)
        if info_guess['version'] == cur:
            s += 40                      # 与当前解释器同版本 → wheel 通用
        return (s, info_guess)

    best = None
    for root in uniq:
        r = rank(root)
        if r and (best is None or r[0] > best[0]):
            best = r
    if best:
        _pydev_cache = best[1]
        return best[1]
    _pydev_cache = False
    return None


class CompileResult:
    def __init__(self):
        self.mode = None          # 'native' | 'vm'
        self.success = False
        self.repairs = 0
        self.output = ""
        self.error = ""
        self.exit_code = None     # 子进程退出码（stream 模式下由子进程直接带走）
        self.compile_ms = 0
        self.run_ms = 0
        self.report = None        # RepairReport，供调用方读取修复明细
        self.from_cache = False   # 是否命中编译产物缓存


def compile_and_run(source, filename="<py++>", stdin_data=None,
                    force_vm=False, use_color=True, quiet=False,
                    stream=False, passthrough=None):
    """
    核心流水线。返回 CompileResult。

    stream=True：交互式控制台模式——原生路径不捕获子进程 stdio
    （stdout/stderr 继承，提示语实时可见、可当场输入），VM 路径
    的输出也实时写到控制台。仅用于真实终端/运行器；Web/测试用
    默认 False（捕获输出以便程序化消费）。
    """
    t_start = time.perf_counter()
    report = RepairReport()
    result = CompileResult()

    # 0. 预处理拼写修复
    fixed_source = fix_source(source, report)

    # 0.5 展开 `import <C库>`（pget 装好的 .cpyi → #link + extern）
    fixed_source, c_libs = _expand_c_imports(fixed_source)
    for _lib in c_libs:
        report.add('info', 0, f"已展开 C 库声明：{_lib}", "需要原生模式才能调用")

    # 1. 词法
    try:
        lexer = Lexer(fixed_source, repair_log=[])
        tokens = lexer.tokenize()
        for kind, line, col, msg in lexer.repair_log:
            report.add('repair', line, msg)
    except LexerError as e:
        report.add('error', e.line, e.msg)
        result.error = report.render(use_color)
        result.report = report
        return result

    # 2. 语法
    try:
        parser = Parser(tokens, repair_log=[])
        program = parser.parse()
        for kind, line, col, msg in parser.repair_log:
            report.add('repair', line, msg)
    except ParseError as e:
        report.add('error', e.line, e.msg)
        result.error = report.render(use_color)
        result.report = report
        return result

    # 3. 选择执行路径
    toolchain = None if force_vm else find_toolchain()
    result.repairs = sum(1 for lvl, *_ in report.items if lvl == 'repair')

    # 2.9 extern 签名校验（必须在生成 C 之前）
    #
    # ⚠️ 为什么需要：用户把 C 函数声明写错（比如 crc32 少写两个参数）时，
    #    py++ 会照着这个错误声明生成调用，实参个数与真实 C 函数不符 →
    #    寄存器错位 → 解引用野指针 → **段错误**。
    #    而崩溃后以前还报"成功"（退出码被吞），用户只看到空白输出。
    #    实测 crc32 少声明参数直接 139 崩溃。
    #    这里在编译期就用真实头文件签名核对，不一致直接报错，
    #    绝不生成注定崩溃的代码。
    #
    # ⚠️ force_vm（无 gcc / 强制 VM）时**跳过**：
    #    VM 根本不调用 C 函数，校验没有意义，还会把
    #    "需要原生模式才能调用 C 库"这条更准确的提示挡掉。
    if toolchain:
        _extern_problems = _validate_externs(program, fixed_source)
        if _extern_problems:
            for _msg in _extern_problems:
                report.add('error', 0, _msg)
            result.error = report.render(use_color)
            result.report = report
            return result

    if toolchain:
        try:
            result = _run_native(program, source, fixed_source, report, result,
                                 toolchain, stdin_data, use_color, quiet,
                                 stream=stream, passthrough=passthrough)
            result.report = report
            if result.success:
                return result

            # ⚠️ 关键区分：编译失败 vs 运行崩溃
            #
            # 编译失败 → 降级 VM 合理（VM 能跑出结果，这正是容错哲学）。
            # 运行崩溃 → **绝不能降级**！程序已经真的跑起来了，崩溃就是
            #   程序本身的问题（段错误 / 访问违规）。此时降级到 VM 会得到
            #   一个"看起来正常"的结果，把真实错误掩盖成假输出。
            #
            # 实测事故（2026-10-19）：extern 签名写错 → 原生段错误
            #   (exit=3221225477 = 0xC0000005) → 自动降级 VM → VM 打印 0
            #   → 用户看到 `crc32("abc")` 返回 0，以为函数坏了，
            #   实际是崩溃后的假结果。这比直接报错坏得多。
            if result.mode == 'native' and result.exit_code not in (None, 0):
                _crash = result.exit_code
                report.add('error', 0,
                           f"程序已编译成功，但运行期崩溃（退出码 {_crash}）。",
                           "这不是 py++ 的编译问题，是程序本身出错（如越界访问、"
                           "C 函数参数不匹配）。")
                result.error = report.render(use_color)
                result.success = False
                return result

            # native 编译失败 → 降级 VM（容错哲学）
            report.add('info', 0,
                       "原生编译未成功，已自动切换到 py++ 内置引擎继续运行",
                       "程序仍可正常执行，速度略低，结果一致")
        except Exception as e:
            report.add('info', 0,
                       f"原生编译异常，已降级到内置引擎（{type(e).__name__}）")

    # 4. VM 降级路径
    result.mode = 'vm'
    _ensure_vm_path()
    buf = []

    def _vm_out(s):
        # stream 模式实时写控制台；否则收集进 buf 由调用方统一处理
        if stream:
            sys.stdout.write(s)
            sys.stdout.flush()
        else:
            buf.append(s)

    interp = Interpreter(output=_vm_out, repair_report=report)
    try:
        interp.run(program)
        for w in interp.warn:
            report.add('repair', 0, w, "运行时小问题，已自动兜底处理")
        result.output = ''.join(buf)
        result.success = True
    except Exception as e:
        result.error = f"{_repair.RED}[运行时异常] {type(e).__name__}: {e}{_repair.RESET}"
        result.output = ''.join(buf)
    result.run_ms = (time.perf_counter() - t_start) * 1000

    # 输出提示
    result.report = report
    if not quiet:
        _emit_notices(report, result, use_color)
    return result


def _run_native(program, source, fixed_source, report, result, toolchain,
                stdin_data, use_color, quiet, stream=False, passthrough=False):
    """编译为 C 并执行（带编译产物缓存）。"""
    result.mode = 'native'
    t0 = time.perf_counter()
    try:
        gen = CGenerator()
        c_code = gen.generate(program)
    except NeedsVMError as e:
        report.add('info', 0,
                   f"这段代码需要内置引擎（{e}）",
                   "py++ 已自动切换到内置引擎，import 功能完全一致，速度略低")
        result.success = False
        return result
    except Exception as e:
        report.add('info', 0, f"代码生成阶段未能完成（{e}），切换内置引擎")
        result.success = False
        return result

    # 用到了 import → 需要把 CPython 嵌进可执行文件
    pydev = None
    if getattr(gen, 'uses_python', False):
        pydev = find_python_dev()
        if not pydev:
            report.add('info', 0,
                       "程序用到了 import，但本机没有找到可嵌入的 Python 开发环境"
                       "（缺 include/Python.h 或 libs/python3XX.lib）",
                       "已自动切换到 py++ 内置引擎，import 功能完全一致")
            result.success = False
            return result

    # 编译期补全过哪些未定义变量？如实告知用户（与 VM 模式保持一致）
    for name in getattr(gen, 'completed_vars', []):
        report.add('repair', 0,
                   f"未定义的变量 '{name}'，已按 0 处理",
                   "这无大碍，py++ 已在编译期自动补全")

    tool = toolchain if isinstance(toolchain, dict) else find_toolchain()
    if not tool:
        result.success = False
        return result
    exe_path = tool['path']
    extra = tool.get('extra', [])
    env = _toolchain_env(tool)

    # 嵌入 Python 时的额外编译参数与运行环境
    py_flags = []
    # ⚠️ 运行环境**必须**带上工具链 bin 目录（Windows DLL 查找）。
    #    原因：编译产物是动态链接的 mingw exe。只要源码用了 `#link z`
    #    之类的 C 库，运行时就要找 zlib1.dll；而 zlib1.dll 只存在于
    #    tools/mingw64/mingw64/bin，**不在 System32**。
    #    不把该目录塞进 PATH，Windows 就会「找不到 DLL → 进程凭空退出、
    #    stdout 为空、退出码 0」——看起来像「跑通了但没输出」。
    #    之前只有 pydev 分支才设 run_env，普通程序走 env=None（继承父进程
    #    PATH）——本地从 shell 跑常常侥幸命中，但被 Node/计划任务等
    #    PATH 干净的环境调用时必然复现。
    run_env = env
    pyfp = ''
    if pydev:
        py_flags = [f"-I{pydev['include']}", f"-L{pydev['libs']}",
                    f"-l{pydev['libname']}"]
        pyfp = f"{pydev['root']}|{pydev['version']}"
        run_env = _python_run_env(env, pydev)

    # 源码里的 #link 指令（调用 C 库）
    link_flags = _collect_link_flags(source)
    for f in _collect_link_flags(fixed_source):
        if f not in link_flags:
            link_flags.append(f)
    if link_flags:
        pyfp += '|link:' + ','.join(link_flags)

    # ---- 尝试复用上次的编译产物 ----
    # 缓存键基于"生成的 C 代码"而非原始源码 —— 这样容错修复、断裂合并
    # 等任何上游变化都会自动让缓存失效，比按源文件哈希更安全。
    cached_exe = _cache_lookup(c_code, tool, pyfp)
    if cached_exe:
        result.compile_ms = (time.perf_counter() - t0) * 1000
        result.from_cache = True
        return _exec_and_finish(cached_exe, result, report, stdin_data,
                                use_color, quiet, cleanup_dir=None,
                                run_env=run_env, stream=stream,
                                passthrough=passthrough)

    tmpdir = tempfile.mkdtemp(prefix="pypp_")
    c_file = os.path.join(tmpdir, "main.c")
    exe_file = os.path.join(tmpdir, "main.exe" if os.name == 'nt' else "main")
    try:
        with open(c_file, 'w', encoding='utf-8') as f:
            f.write(c_code)

        cmd = [exe_path, c_file, '-O2', '-o', exe_file] + extra + py_flags
        cmd.append('-lm')
        cmd.extend(link_flags)
        comp = subprocess.run(cmd, capture_output=True,
                              encoding='utf-8', errors='replace',
                              timeout=120, env=env)
    except Exception as e:
        report.add('info', 0, f"编译过程异常（{type(e).__name__}），切换内置引擎")
        result.success = False
        try:
            shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:
            pass
        return result

    result.compile_ms = (time.perf_counter() - t0) * 1000
    if comp.returncode != 0 or not os.path.exists(exe_file):
        report.add('info', 0, "C 后端编译未成功，切换内置引擎",
                   (comp.stderr or "")[:300])
        result.success = False
        try:
            shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:
            pass
        return result

    # 存入缓存，供下次快速复用
    _cache_store(c_code, tool, exe_file, pyfp)
    return _exec_and_finish(exe_file, result, report, stdin_data,
                            use_color, quiet, cleanup_dir=tmpdir,
                            run_env=run_env, stream=stream,
                            passthrough=passthrough)


def _python_run_env(base_env, pydev):
    """运行嵌入了 CPython 的可执行文件所需的环境（DLL 查找 + pget 包目录）。"""
    env = dict(base_env)
    prepend = [pydev['root']]
    dlls = os.path.join(pydev['root'], 'DLLs')
    if os.path.isdir(dlls):
        prepend.append(dlls)
    sep = ';' if os.name == 'nt' else ':'
    env['PATH'] = sep.join(prepend) + sep + env.get('PATH', '')
    env['PYPP_PYPATH'] = _packages_dir()
    return env


# ---------------------------------------------------------------------------
# 编译产物缓存
# ---------------------------------------------------------------------------
# 目的：改一次代码跑一次，不必每次都重新走 gcc。
#   实测：完整编译 ~437ms → 命中缓存 ~130ms。
#
# 缓存键 = SHA256(生成的 C 代码) + 编译器指纹 + py++ 版本
#   · 用"生成的 C 代码"而不是原始源码 → 容错修复的任何变化都会失效
#   · 编译器指纹（路径+版本）→ 换 gcc 会自动重编
#   · py++ 版本 → 升级后不会用到旧产物
#
# 存放位置：~/.pypp/cache（用户可写，不污染项目目录）
# ---------------------------------------------------------------------------

_CACHE_TTL_DAYS = 30       # 超过则视为过期（清理用）
_cache_dir_ready = False


def _cache_dir():
    # 允许用环境变量覆盖（测试隔离用）
    override = os.environ.get('PYPP_CACHE_DIR')
    if override:
        return override
    return os.path.join(os.path.expanduser('~'), '.pypp', 'cache')


def _cache_key(c_code: str, tool, extra_fp: str = '') -> str:
    """计算缓存键。extra_fp 用于区分嵌入 Python 时的目标解释器。"""
    h = hashlib.sha256()
    h.update(VERSION.encode())
    h.update(b'\x00')
    h.update(tool.get('path', '').encode())
    h.update(b'\x00')
    h.update((tool.get('version') or '').encode())
    h.update(b'\x00')
    h.update((extra_fp or '').encode())
    h.update(b'\x00')
    h.update(c_code.encode('utf-8'))
    return h.hexdigest()


def _cache_lookup(c_code: str, tool, extra_fp: str = ''):
    """命中则返回可执行的 exe 路径，否则 None。"""
    global _cache_dir_ready
    if os.environ.get('PYPP_NO_CACHE'):
        return None
    try:
        d = _cache_dir()
        if not os.path.isdir(d):
            return None
        key = _cache_key(c_code, tool, extra_fp)
        exe = os.path.join(d, key + ('.exe' if os.name == 'nt' else ''))
        if os.path.isfile(exe):
            # 校验可执行（防止残留的坏文件）
            return exe
    except Exception:
        return None
    return None


def _cache_store(c_code: str, tool, exe_file, extra_fp: str = ''):
    """把编译产物存进缓存。失败静默（缓存是加速手段，不该影响主流程）。"""
    global _cache_dir_ready
    if os.environ.get('PYPP_NO_CACHE'):
        return
    try:
        d = _cache_dir()
        if not _cache_dir_ready:
            os.makedirs(d, exist_ok=True)
            _cache_dir_ready = True
        key = _cache_key(c_code, tool, extra_fp)
        dst = os.path.join(d, key + ('.exe' if os.name == 'nt' else ''))
        if not os.path.exists(dst):
            shutil.copy2(exe_file, dst)
        _cache_gc(d)
    except Exception:
        pass


def _cache_gc(d):
    """轻量清理：条目过多或过旧时删掉一部分。"""
    try:
        entries = [os.path.join(d, f) for f in os.listdir(d)]
        files = [e for e in entries if os.path.isfile(e)]
        if len(files) < 200:
            return
        files.sort(key=lambda p: os.path.getmtime(p))
        for f in files[:len(files) // 2]:
            try:
                os.remove(f)
            except OSError:
                pass
    except Exception:
        pass


def _exec_and_finish(exe_file, result, report, stdin_data,
                     use_color, quiet, cleanup_dir=None, run_env=None,
                     stream=False, passthrough=False):
    """运行编译产物、收集输出、可选清理临时目录。

    stream=True（交互式控制台）：stdout/stderr **直接继承**，不捕获。
    这样程序里的 input() 提示语实时可见、用户能当场输入——
    否则提示语会进管道、跑完才打印，用户只能"盲打"。

    passthrough=True（IDE 用）：stdout/stderr 重定向到本进程的管道，
    **边读边写**到自己的 stdout/stderr，同时收集一份。
    与 stream 的区别：stream 让子进程直连控制台，父进程（IDE）什么都收不到；
    passthrough 让 IDE 既能**实时看到每一行**，又能拿到完整 output/exit_code。
    ⚠️ 一定要逐行 flush：否则父进程那边仍是块缓冲，看起来还是"跑完才出"。

    （踩过的坑：input() 无提示 + 输出被捕获 = 看起来像假死。）
    注意：stream 模式下 stdin_data 无意义（stdin 直接来自控制台）。
    """
    t1 = time.perf_counter()
    # 超时：默认不限时（None）。长跑程序（模拟、批量计算）不该被 CLI 掐断；
    # 需要防跑飞的场景可设 PYPP_RUN_TIMEOUT=<秒>，设 0 或留空 = 不限。
    _timeout_env = os.environ.get('PYPP_RUN_TIMEOUT', '').strip()
    try:
        _run_timeout = float(_timeout_env) if _timeout_env else None
        if _run_timeout is not None and _run_timeout <= 0:
            _run_timeout = None
    except ValueError:
        _run_timeout = None
    try:
        if stream:
            run = subprocess.run([exe_file], timeout=_run_timeout, env=run_env)
            result.run_ms = (time.perf_counter() - t1) * 1000
            result.exit_code = run.returncode
            result.output = ''      # 已经实时进控制台了，无处可捕
            result.success = run.returncode == 0
        elif passthrough:
            # 边跑边转发：逐行读子进程输出 → 立刻写自己的 stdout → flush
            proc = subprocess.Popen(
                [exe_file],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
                env=run_env, bufsize=0)
            out_chunks, err_chunks = [], []

            def _pump(src, dst, sink):
                # ⚠️ readline() 而不是 for-line：readline 一拿到行就返回，
                #    for-line 在无缓冲管道下会等到缓冲区攒够（或 EOF）。
                while True:
                    line = src.readline()
                    if not line:
                        break
                    sink.append(line)
                    try:
                        dst.write(line)
                        dst.flush()
                    except Exception:
                        pass

            th_out = threading.Thread(target=_pump, args=(proc.stdout, sys.stdout, out_chunks), daemon=True)
            th_err = threading.Thread(target=_pump, args=(proc.stderr, sys.stderr, err_chunks), daemon=True)
            th_out.start()
            th_err.start()

            if stdin_data is not None:
                try:
                    proc.stdin.write(stdin_data.encode('utf-8'))
                    proc.stdin.close()
                except Exception:
                    pass

            # 不设超时（None）时 wait() 会一直等；超时抛 TimeoutExpired
            try:
                proc.wait(timeout=_run_timeout)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except Exception:
                    pass
                proc.wait()

            th_out.join(timeout=5)
            th_err.join(timeout=5)

            result.run_ms = (time.perf_counter() - t1) * 1000
            result.exit_code = proc.returncode
            result.output = b''.join(out_chunks).decode('utf-8', 'replace')
            err_text = b''.join(err_chunks).decode('utf-8', 'replace')
            if proc.returncode != 0 and err_text:
                result.output += "\n" + err_text
            # ⚠️ 不能无条件 True：子进程崩溃（段错误 = 退出码 139 / 0xC0000005
            #    = 3221225477）时，以前也报"成功"，导致调用方完全无法察觉。
            result.success = proc.returncode == 0
        else:
            # ⚠️ 解码必须是 UTF-8：C 字符串字面量按 UTF-8 写进 C 源文件，
            #    程序输出就是 UTF-8。用 text=True 走 locale 默认（中文
            #    Windows 是 GBK）会在读取线程里 UnicodeDecodeError 崩溃，
            #    整个程序输出丢失。errors='replace' 防止二进制输出炸掉。
            run = subprocess.run([exe_file], capture_output=True,
                                 encoding='utf-8', errors='replace',
                                 input=stdin_data, timeout=_run_timeout, env=run_env)
            result.run_ms = (time.perf_counter() - t1) * 1000
            result.output = run.stdout
            result.exit_code = run.returncode
            if run.returncode != 0 and run.stderr:
                result.output += "\n" + run.stderr
            # ⚠️ 同上：崩溃就是崩溃，不能报成功。
            result.success = run.returncode == 0
    finally:
        result.report = report
        if not quiet:
            _emit_notices(report, result, use_color)
        if cleanup_dir:
            try:
                shutil.rmtree(cleanup_dir, ignore_errors=True)
            except Exception:
                pass
    return result


def _emit_notices(report, result, use_color=True):
    if report.has_repairs:
        repairs = [it for it in report.items if it[0] == 'repair']
        print(format_welcome_fixed_msg(len(repairs)) if use_color
              else f"[py++] 检测到 {len(repairs)} 处小错误，已兼容运行。",
              file=sys.stderr)
        # 详情
        detail = report.render(use_color)
        if detail:
            print(detail, file=sys.stderr)


def main():
    args = sys.argv[1:]
    if not args or args[0] in ('-h', '--help'):
        print_help()
        return 0
    if args[0] in ('-v', '--version'):
        print(f"py++ {VERSION}")
        return 0
    if args[0] == '--check':
        tool = find_toolchain()
        pydev = find_python_dev()
        print(f"py++ {VERSION} 环境检测")
        print(f"  Python: {sys.version.split()[0]}")
        if tool:
            print(f"  ✓ C 工具链: {tool['path']}")
            print(f"  执行引擎: 原生高速模式")
        else:
            print(f"  ! 未找到 C 工具链")
            print(f"  执行引擎: 内置 VM 模式")
        if pydev:
            print(f"  ✓ 可嵌入 CPython: {pydev['root']} ({pydev['version']})")
            print(f"  import 支持: 原生模式直接嵌入 CPython")
        else:
            print(f"  ! 未找到可嵌入的 CPython 开发环境")
            print(f"  import 支持: 降级到 VM（功能一致，速度略低）")
        print(f"  第三方包目录: {_packages_dir()}")
        return 0

    force_vm = False
    no_color = False
    quiet = False
    filepath = None
    for a in args:
        if a == '--vm':
            force_vm = True
        elif a == '--no-color':
            no_color = True
        elif a in ('--quiet', '-q'):
            quiet = True
        elif not a.startswith('-'):
            filepath = a

    if no_color:
        _repair.set_color(False)

    if not filepath:
        print(f"{_repair.RED}错误：请指定 py++ 源文件{_repair.RESET}")
        return 1
    if not os.path.exists(filepath):
        print(f"{_repair.RED}错误：找不到文件 {filepath}{_repair.RESET}")
        return 1

    with open(filepath, 'r', encoding='utf-8') as f:
        source = f.read()

    toolchain = find_toolchain() if not force_vm else None
    if not force_vm:
        eng = ('原生高速模式 (' + os.path.basename(toolchain['path']) + ')'
               if toolchain else '内置 VM 模式')
        print(f"{_repair.CYAN}[py++] 引擎: {eng}  |  "
              f"文件: {filepath}{_repair.RESET}", file=sys.stderr)
    # 交互式终端（stdin 是控制台）→ 子进程 stdio 直连控制台：
    # 程序里 input() 的提示语实时可见、用户能当场输入。
    # （曾经的坑：输出被 capture 进管道，提示语跑完才打印，
    #  用户对着"没有提示的等待"只能盲打，看起来像假死。）
    # 管道/CI 场景 stdin 不是 tty → 保持捕获，行为与历史一致。
    stream = False
    try:
        stream = bool(sys.stdin.isatty())
    except Exception:
        stream = False
    result = compile_and_run(source, filename=filepath, force_vm=force_vm,
                             stream=stream)

    if result.output:
        print(result.output, end='')
    if result.error:
        print(result.error, file=sys.stderr)

    # 性能信息
    if result.success:
        if result.mode == 'native':
            if getattr(result, 'from_cache', False):
                # 命中缓存：不必显示 0ms，直接说明"用了缓存"更清楚
                print(f"{_repair.GREEN}[py++] 完成 "
                      f"运行 {_fmt_ms(result.run_ms)}  "
                      f"(原生模式 · 复用上次编译结果){_repair.RESET}",
                      file=sys.stderr)
            else:
                print(f"{_repair.GREEN}[py++] 完成 "
                      f"编译 {_fmt_ms(result.compile_ms)} | "
                      f"运行 {_fmt_ms(result.run_ms)}  "
                      f"(原生模式){_repair.RESET}", file=sys.stderr)
        else:
            print(f"{_repair.CYAN}[py++] 完成 运行 {_fmt_ms(result.run_ms)}  "
                  f"(VM 模式){_repair.RESET}", file=sys.stderr)
    return 0


def _fmt_ms(ms: float) -> str:
    """把毫秒格式化成易读形式（去掉无意义的小数）。"""
    if ms < 1:
        return f"{ms:.2f}ms"
    if ms < 1000:
        return f"{ms:.0f}ms"
    return f"{ms/1000:.2f}s"


def print_help():
    print(f"""{_repair.BOLD}py++ {VERSION}{_repair.RESET} — 像 Python 一样简洁，像 C++ 一样快

{_repair.GREEN}用法:{_repair.RESET}
  pypp <文件.cpy>           编译并运行
  pypp <文件.cpy> --vm      强制使用内置 VM（不用 gcc）
  pypp --check              检查本机环境
  pypp -v                   查看版本

{_repair.GREEN}特性:{_repair.RESET}
  · 缩进语法，类 Python，无需分号
  · 有 gcc 时编译为原生代码 (-O2)，速度接近 C++
  · 无 gcc 时自动降级到内置 VM，保证能跑
  · 小错误自动修复，运行不受影响
  · import 真实 CPython，可直接用第三方库（配 pget 安装）

{_repair.GREEN}装库:{_repair.RESET}
  pget install requests     安装第三方库（对标 pip）
  pget list                 查看已安装
""")


if __name__ == '__main__':
    sys.exit(main())
