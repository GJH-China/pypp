#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pget —— py++ 的包管理器（对标 pip）

py++ 的 import 走的是真实 CPython：
  · 原生模式把 CPython 解释器嵌进可执行文件；
  · VM 模式本身就跑在 Python 里。
所以"给 py++ 装库"本质上就是"给 Python 装库"。pget 做的是把这件事
做得对、做得可控：装到 py++ 自己的目录，两个引擎都能看到。

安装位置：~/.pypp/packages （可用环境变量 PYPP_PKG_DIR 覆盖）
索引地址：默认 https://pypi.org （可用 -i 换镜像，如清华源）

命令：
  pget install <包>...     安装（自动解析并安装依赖）
  pget uninstall <包>...   卸载
  pget list                列出已安装
  pget show <包>           显示包信息
  pget freeze              输出可重装清单
  pget search <关键词>      在 PyPI 上搜索
  pget download <包>       只下载 wheel 不安装
  pget doctor              环境诊断（该装哪个版本、装到哪、能否用）
  pget --version / -h
"""

import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

VERSION = "1.1.0"

# ---------------------------------------------------------------------------
# 输出样式
# ---------------------------------------------------------------------------
_NO_COLOR = bool(os.environ.get('NO_COLOR')) or not sys.stdout.isatty()


def _c(code, s):
    return s if _NO_COLOR else f"\033[{code}m{s}\033[0m"


def green(s):  return _c('32', s)
def red(s):    return _c('31', s)
def yellow(s): return _c('33', s)
def cyan(s):   return _c('36', s)
def bold(s):   return _c('1', s)
def dim(s):    return _c('2', s)


_PROG = {'py': 'pyget', 'c': 'cget', 'cpp': 'cpget', 'auto': 'pget'}


def _prog():
    """当前是哪个命令在跑（pyget / cget / cpget / pget）。"""
    try:
        return _PROG.get(_tool_kind(), 'pget')
    except Exception:
        return 'pget'


def info(msg):  print(f"{cyan(_prog())} {msg}")
def ok(msg):    print(f"{green('✓')} {msg}")
def warn(msg):  print(f"{yellow('!')} {msg}")
def err(msg):   print(f"{red('✗')} {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# 目录与目标 Python
# ---------------------------------------------------------------------------
def packages_dir():
    """pget 的安装目录（与 pypp._packages_dir() 保持一致）。"""
    return os.environ.get('PYPP_PKG_DIR') or os.path.join(
        os.path.expanduser('~'), '.pypp', 'packages')


def cache_dir():
    return os.environ.get('PYPP_CACHE_DIR') or os.path.join(
        os.path.expanduser('~'), '.pypp', 'cache')


def target_python():
    """
    返回 (python_exe, "3.13")。
    优先使用 py++ 原生模式将要嵌入的那个 Python —— 因为 wheel 带
    cp313 这样的 ABI 标签，装错版本就加载不了。
    """
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        if here not in sys.path:
            sys.path.insert(0, here)
        sys.path.insert(0, os.path.join(here, 'src'))
        import pypp  # noqa
        d = pypp.find_python_dev()
        if d:
            exe = os.path.join(d['root'],
                               'python.exe' if os.name == 'nt' else 'bin/python3')
            if not os.path.isfile(exe):
                exe = os.path.join(d['root'], 'python.exe')
            if os.path.isfile(exe):
                return exe, d['version']
    except Exception:
        pass
    return sys.executable, f"{sys.version_info[0]}.{sys.version_info[1]}"


# ---------------------------------------------------------------------------
# 版本比较（PEP 440 的够用子集）
# ---------------------------------------------------------------------------
_REL_RE = re.compile(r'^(\d+(?:\.\d+)*)(.*)$')


def _ver_key(v):
    v = (v or '').strip().lstrip('vV')
    m = _REL_RE.match(v)
    if not m:
        return ((0,), (0, v))
    rel = tuple(int(x) for x in m.group(1).split('.'))
    rest = m.group(2)
    if not rest:
        return (rel, (1, ''))          # 正式版排在预发布之后
    return (rel, (0, rest))


def _ver_eq(a, b):
    ka, kb = _ver_key(a)[0], _ver_key(b)[0]
    n = max(len(ka), len(kb))
    ka = ka + (0,) * (n - len(ka))
    kb = kb + (0,) * (n - len(kb))
    return ka == kb


def parse_spec(text):
    """
    'requests>=2.0,<3' -> ('requests', [('>=','2.0'), ('<','3')])
    同时剥掉 extras：'requests[socks]' -> 'requests'
    """
    text = text.strip()
    if not text:
        return '', []
    text = re.sub(r'\[[^\]]*\]', '', text)
    m = re.match(r'^([A-Za-z0-9._-]+)\s*(.*)$', text)
    if not m:
        return text, []
    name, rest = m.group(1), m.group(2).strip()
    specs = []
    if rest:
        for part in rest.split(','):
            part = part.strip()
            mm = re.match(r'^(==|!=|>=|<=|~=|>|<)\s*(.+)$', part)
            if mm:
                specs.append((mm.group(1), mm.group(2).strip()))
            elif part:
                specs.append(('==', part))
    return name, specs


def satisfies(ver, specs):
    for op, want in specs:
        a, b = _ver_key(ver), _ver_key(want)
        if op == '==':
            if not _ver_eq(ver, want):
                return False
        elif op == '!=':
            if _ver_eq(ver, want):
                return False
        elif op == '>=':
            if a < b:
                return False
        elif op == '<=':
            if a > b:
                return False
        elif op == '>':
            if a <= b:
                return False
        elif op == '<':
            if a >= b:
                return False
        elif op == '~=':
            if a < b:
                return False
            base = want.split('.')
            if len(base) >= 2:
                prefix = '.'.join(base[:-1])
                if not (ver == prefix or ver.startswith(prefix + '.')):
                    return False
    return True


# ---------------------------------------------------------------------------
# 网络
# ---------------------------------------------------------------------------
_UA = f"pget/{VERSION} (py++ package manager)"


def _index_url():
    return os.environ.get('PYPP_INDEX') or 'https://pypi.org'


def _http_get(url, timeout=30):
    req = urllib.request.Request(url, headers={'User-Agent': _UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def pypi_json(name, index=None):
    base = (index or _index_url()).rstrip('/')
    url = f"{base}/pypi/{urllib.parse.quote(name)}/json"
    try:
        return json.loads(_http_get(url).decode('utf-8'))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    except Exception:
        return None


def download_file(url, dest):
    req = urllib.request.Request(url, headers={'User-Agent': _UA})
    with urllib.request.urlopen(req, timeout=120) as r, open(dest, 'wb') as f:
        shutil.copyfileobj(r, f)
    return dest


# ---------------------------------------------------------------------------
# wheel 选择
# ---------------------------------------------------------------------------
def platform_tags():
    if os.name == 'nt':
        return ['win_amd64'] if sys.maxsize > 2 ** 32 else ['win32']
    import platform
    mach = platform.machine().lower()
    if sys.platform == 'darwin':
        return ['macosx_11_0_arm64', 'macosx_10_9_x86_64', 'macosx_10_9_universal2']
    return ['manylinux_2_17_x86_64', 'manylinux2014_x86_64', 'manylinux1_x86_64',
            'linux_x86_64'] if mach in ('x86_64', 'amd64') else ['any']


def wheel_score(fname, pyver):
    """给 wheel 文件名打分；None 表示与目标 Python/平台不兼容。"""
    if not fname.endswith('.whl'):
        return None
    parts = fname[:-4].split('-')
    if len(parts) < 5:
        return None
    pytag, abitag, plattag = parts[-3], parts[-2], parts[-1]
    pyts = pytag.split('.')
    abis = abitag.split('.')

    plat_ok = plattag == 'any' or plattag in platform_tags()
    if not plat_ok:
        return None

    cp = 'cp' + pyver.replace('.', '')
    cpnum = int(pyver.replace('.', ''))
    score = 50 if plattag != 'any' else 1

    if cp in pyts:
        score += 100
    elif 'py3' in pyts:
        score += 40
    elif any(t.startswith('cp3') for t in pyts):
        if 'abi3' in abis:
            nums = [int(t[2:]) for t in pyts
                    if t.startswith('cp3') and t[2:].isdigit()]
            if nums and min(nums) <= cpnum:
                score += 60            # abi3 向后兼容
            else:
                return None
        else:
            return None
    else:
        return None

    if cp in abis:
        score += 30
    elif 'abi3' in abis:
        score += 20
    elif 'none' in abis:
        score += 10
    return score


def pick_wheel(files, pyver, want_version=None):
    """从 PyPI 的 urls 列表里挑最合适的 wheel；没有则返回 None。"""
    best, best_score = None, -1
    for f in files:
        if f.get('packagetype') != 'bdist_wheel':
            continue
        if f.get('yanked'):
            continue
        if want_version and f.get('version') != want_version:
            continue
        s = wheel_score(f['filename'], pyver)
        if s is None:
            continue
        if s > best_score:
            best, best_score = f, s
    return best


def _all_releases(data):
    """PyPI json 里的 releases：{版本: [文件...]}"""
    return data.get('releases') or {}


# ---------------------------------------------------------------------------
# 安装
# ---------------------------------------------------------------------------
def _read_metadata(dist_info):
    meta = os.path.join(dist_info, 'METADATA')
    if not os.path.isfile(meta):
        return {}
    out = {'requires': []}
    with open(meta, 'r', encoding='utf-8', errors='replace') as f:
        for line in f:
            if line.startswith('Name: ') and 'name' not in out:
                out['name'] = line[6:].strip()
            elif line.startswith('Version: ') and 'version' not in out:
                out['version'] = line[9:].strip()
            elif line.startswith('Summary: ') and 'summary' not in out:
                out['summary'] = line[9:].strip()
            elif line.startswith('Requires-Dist: '):
                out['requires'].append(line[15:].strip())
            elif line.strip() == '' and out.get('name'):
                pass
    return out


def _installed_map(pkgdir=None):
    """扫描安装目录，返回 {规范化名: (显示名, 版本, dist_info路径)}"""
    d = pkgdir or packages_dir()
    out = {}
    if not os.path.isdir(d):
        return out
    for entry in sorted(os.listdir(d)):
        if not entry.endswith('.dist-info'):
            continue
        p = os.path.join(d, entry)
        if not os.path.isdir(p):
            continue
        m = _read_metadata(p)
        if m.get('name'):
            out[_norm(m['name'])] = (m['name'], m.get('version', '?'), p)
    return out


def _norm(name):
    return re.sub(r'[-_.]+', '-', name).lower()


def _remove_installed(name, pkgdir):
    """按 RECORD 移除某个已安装包的全部文件。返回 (显示名, 版本) 或 None。"""
    inst = _installed_map(pkgdir)
    key = _norm(name)
    if key not in inst:
        return None
    disp, ver, di = inst[key]
    record = os.path.join(di, 'RECORD')
    if os.path.isfile(record):
        with open(record, 'r', encoding='utf-8', errors='replace') as f:
            for line in f:
                rel = line.split(',')[0].strip()
                if not rel:
                    continue
                p = os.path.normpath(os.path.join(pkgdir, rel))
                if p.startswith(os.path.normpath(pkgdir)) and os.path.isfile(p):
                    try:
                        os.remove(p)
                    except OSError:
                        pass
    shutil.rmtree(di, ignore_errors=True)
    return (disp, ver)


def install_wheel(whl_path, pkgdir):
    """把 wheel 解压到安装目录。"""
    with zipfile.ZipFile(whl_path) as z:
        for member in z.namelist():
            if member.endswith('/'):
                continue
            # 防止路径穿越
            target = os.path.normpath(os.path.join(pkgdir, member))
            if not target.startswith(os.path.normpath(pkgdir)):
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with z.open(member) as srcf, open(target, 'wb') as dstf:
                shutil.copyfileobj(srcf, dstf)


def _dist_info_of(whl_path):
    with zipfile.ZipFile(whl_path) as z:
        for n in z.namelist():
            if n.endswith('.dist-info/METADATA'):
                return n.rsplit('/', 1)[0]
    return None


def _whl_name(whl_path):
    """从 wheel 文件名里取包名（可能带下划线，_norm 会归一化）。"""
    base = os.path.basename(whl_path)
    if base.endswith('.whl'):
        base = base[:-4]
    parts = base.split('-')
    return parts[0] if parts else base


def _runtime_requires(reqs):
    """过滤掉 extras 与仅环境相关的依赖。"""
    out = []
    for r in reqs:
        if ';' in r:
            cond = r.split(';', 1)[1]
            # 只保留"无条件"或平台标记简单的依赖
            if 'extra' in cond:
                continue
            r = r.split(';', 1)[0]
        r = r.strip()
        if r:
            out.append(r)
    return out


# ---------------------------------------------------------------------------
# 统一安装：判断一个名字属于哪个生态（PyPI / C / C++）
# ---------------------------------------------------------------------------
# pget 的设计目标：不管是 pip 的包、C 的库还是 C++ 的库，
# 都用同一条命令 `pget install <名字>` 装上。这里负责"猜"它是哪一类。

# 已知的 C/C++ 库别名 → 实际链接名 / 头文件名
# 用户可能写 OpenSSL、openssl、libssl、ssl，都得能对上。
C_LIB_ALIASES = {
    # 压缩
    'z': ('z', 'zlib.h'), 'zlib': ('z', 'zlib.h'),
    'libz': ('z', 'zlib.h'),
    'bz2': ('bz2', 'bzlib.h'), 'bzip2': ('bz2', 'bzlib.h'),
    'libbz2': ('bz2', 'bzlib.h'),
    'lzma': ('lzma', 'lzma.h'), 'xz': ('lzma', 'lzma.h'),
    'zstd': ('zstd', 'zstd.h'), 'libzstd': ('zstd', 'zstd.h'),
    'lz4': ('lz4', 'lz4.h'),
    # 加密 / 哈希
    'ssl': ('ssl', 'openssl/ssl.h'), 'crypto': ('crypto', 'openssl/ssl.h'),
    'openssl': ('ssl', 'openssl/ssl.h'), 'libssl': ('ssl', 'openssl/ssl.h'),
    'libcrypto': ('crypto', 'openssl/ssl.h'),
    'sodium': ('sodium', 'sodium.h'), 'libsodium': ('sodium', 'sodium.h'),
    # 数据库
    'sqlite': ('sqlite3', 'sqlite3.h'), 'sqlite3': ('sqlite3', 'sqlite3.h'),
    'libsqlite3': ('sqlite3', 'sqlite3.h'),
    'mysql': ('mysqlclient', 'mysql/mysql.h'),
    'pq': ('pq', 'libpq-fe.h'), 'postgresql': ('pq', 'libpq-fe.h'),
    'libpq': ('pq', 'libpq-fe.h'),
    # 网络 / 序列化
    'curl': ('curl', 'curl/curl.h'), 'libcurl': ('curl', 'curl/curl.h'),
    'cjson': ('cjson', 'cjson/cJSON.h'), 'json': ('cjson', 'cjson/cJSON.h'),
    'jansson': ('jansson', 'jansson.h'),
    'yaml': ('yaml', 'yaml.h'), 'libyaml': ('yaml', 'yaml.h'),
    # 正则 / 字符
    'pcre': ('pcre', 'pcre.h'), 'pcre2': ('pcre2-8', 'pcre2.h'),
    'iconv': ('iconv', 'iconv.h'), 'libiconv': ('iconv', 'iconv.h'),
    'intl': ('intl', 'libintl.h'), 'gettext': ('intl', 'libintl.h'),
    # 图形（若工具链带）
    'png': ('png', 'png.h'), 'libpng': ('png', 'png.h'),
    'jpeg': ('jpeg', 'jpeglib.h'), 'libjpeg': ('jpeg', 'jpeglib.h'),
    'webp': ('webp', 'webp/decode.h'),
    # 数学 / 线程
    'm': ('m', 'math.h'), 'gmp': ('gmp', 'gmp.h'),
    'mpfr': ('mpfr', 'mpfr.h'),
    'winpthread': ('winpthread', 'pthread.h'), 'pthread': ('winpthread', 'pthread.h'),
}

# C++ 标准库没有"头文件"可转译，只能靠 #link 链接
CPP_STD_LIBS = {'stdc++', 'stdc++fs', 'c++', 'c++abi'}


def _c_lib_candidates(name):
    """把用户写的名字归一化成一堆候选 (链接名, 头文件名) 组合。"""
    key = name.strip().lower()
    out = []
    if key in C_LIB_ALIASES:
        out.append(C_LIB_ALIASES[key])
    # 去掉 lib 前缀再试一次
    if key.startswith('lib'):
        bare = key[3:]
        if bare in C_LIB_ALIASES:
            out.append(C_LIB_ALIASES[bare])
        out.append((bare, bare + '.h'))
        out.append((bare, bare + '/' + bare + '.h'))
    # 原样再兜一层（很多库名 = 头文件名）
    out.append((key, key + '.h'))
    out.append((key, key + '/' + key + '.h'))
    # 去重保序
    seen, uniq = set(), []
    for c in out:
        if c not in seen:
            seen.add(c)
            uniq.append(c)
    return uniq


def _find_c_header(header_rel):
    """在工具链 / 常见系统路径里找头文件，返回绝对路径或 None。"""
    if os.path.isabs(header_rel) and os.path.isfile(header_rel):
        return header_rel
    _libdirs, incdir = _toolchain_dirs()
    roots = []
    if incdir:
        roots.append(incdir)
    # vcpkg / 系统自带，给一点容错
    for env in ('VCPKG_ROOT', 'CPATH', 'C_INCLUDE_PATH'):
        v = os.environ.get(env)
        if v:
            for p in v.split(os.pathsep):
                if p:
                    roots.append(p)
                    if env == 'VCPKG_ROOT':
                        roots.append(os.path.join(p, 'installed', 'x64-windows', 'include'))
    for root in roots:
        if not root:
            continue
        cand = os.path.join(root, *header_rel.split('/'))
        if os.path.isfile(cand):
            return cand
    return None


def _has_c_library(linkname):
    """工具链里有没有 lib<name>.a / lib<name>.dll.a。"""
    libdirs, _inc = _toolchain_dirs()
    for d in libdirs:
        for pat in (f'lib{linkname}.a', f'lib{linkname}.dll.a',
                    f'{linkname}.lib', f'lib{linkname}.lib'):
            if os.path.isfile(os.path.join(d, pat)):
                return True
    return False


def detect_ecosystem(name):
    """
    判断一个名字属于哪个生态，返回 dict：
      {'kind': 'c'|'cpp'|'pypi', 'link': 链接名, 'header': 头文件绝对路径或None,
       'reason': 判断依据（给用户看的）}
    顺序很重要：先看是不是明显的 C 库，避免把 `json` 这类
    PyPI 上也有的名字误判 —— 只有当头文件真的存在时才认 C 库。
    """
    n = name.strip()
    if not n:
        return {'kind': 'pypi', 'link': None, 'header': None, 'reason': ''}

    # 1) 直接给的就是头文件路径
    if n.lower().endswith(('.h', '.hpp', '.hh', '.hxx')) or '/' in n or '\\' in n:
        h = _find_c_header(n)
        if h:
            base = os.path.basename(h).rsplit('.', 1)[0]
            return {'kind': 'c', 'link': base, 'header': h,
                    'reason': '看起来是 C/C++ 头文件路径'}

    # 2) C++ 标准库
    if n.lower() in CPP_STD_LIBS:
        return {'kind': 'cpp', 'link': n, 'header': None,
                'reason': 'C++ 标准库，只需 #link'}

    # 3) 已知别名 / 名字对应到头文件 → C 库
    for linkname, header_rel in _c_lib_candidates(n):
        h = _find_c_header(header_rel)
        if h:
            return {'kind': 'c', 'link': linkname, 'header': h,
                    'reason': f'匹配到 C 库头文件 {os.path.basename(h)}'}
        if _has_c_library(linkname):
            return {'kind': 'c', 'link': linkname, 'header': None,
                    'reason': f'工具链里有 lib{linkname}'}

    # 4) 兜底：PyPI
    reason = '按 PyPI 包处理'
    # 如果这个名字是"已知 C 库别名"但本机没有，提示一下，
    # 免得用户以为 pget 没认出来（很可能他就是想装那个 C 库）
    if n.strip().lower() in C_LIB_ALIASES:
        reason = (f'本机工具链没有 {n}（已按 PyPI 处理；'
                  f'若确实要 C 库，请先装好再重试）')
    return {'kind': 'pypi', 'link': None, 'header': None, 'reason': reason}


def _cbind_to_file(header, linkname, out_path, limit=0):
    """把头文件转译成 extern 声明写入 out_path，返回声明条数。"""
    with open(header, 'r', encoding='utf-8', errors='replace') as f:
        text = f.read()
    incdirs = [os.path.dirname(os.path.abspath(header))] + _toolchain_include_dirs()
    typedefs = _collect_typedefs(_gather_text(header, incdirs=incdirs))
    code, n = transpile_header(text, linkname, limit, typedefs)
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, 'w', encoding='utf-8', newline='\n') as f:
        f.write(code)
    return n, code


def cmd_install_c(name, spec=None, limit=0):
    """安装一个 C/C++ 库：转译头文件并生成 .cpyi 声明文件。"""
    spec = spec or detect_ecosystem(name)
    kind = spec['kind']
    link = spec['link']
    header = spec['header']

    info(f"识别为 {('C++' if kind == 'cpp' else 'C')} 库：{name}"
         + (f"  ({spec['reason']})" if spec.get('reason') else ''))

    subdir = '_cpp' if kind == 'cpp' else '_c'
    extdir = os.path.join(packages_dir(), subdir)
    os.makedirs(extdir, exist_ok=True)

    # ⚠️ 「文件名」与「import 名」要分开：
    #   C++ 标准库叫 stdc++，import 名里 + 是合法的，但 + 不能做文件名。
    #   所以文件存成 stdc__.cpyi，同时在文件里记下真正的 import 名。
    fname = re.sub(r'[^A-Za-z0-9_.-]', '_', link or name)
    out = os.path.join(extdir, fname + '.cpyi')
    # import 时写什么名字最自然（C++ 标准库要能 import stdc++）
    import_name = link if (kind == 'cpp' and link in CPP_STD_LIBS) else fname

    if header is None:
        # 只有库、没有头文件：生成一个只含 #link 的声明文件
        code = (f"# py++ {'C++' if kind == 'cpp' else 'C'} 库声明 —— 由 pget 自动生成\n"
                f"# 库名：{name}\n"
                f"# 未找到头文件，只生成链接指令。\n"
                f"# 用法：在你的 .cpy 里写  import {import_name}\n"
                f"#link {link}\n")
        with open(out, 'w', encoding='utf-8', newline='\n') as f:
            f.write(code)
        ok(f"已生成链接声明 → {out}")
        if kind == 'cpp':
            info(f"  C++ 标准库无需转译函数，直接 #link {link} 即可")
        else:
            warn(f"没找到 {name} 的头文件，无法自动转译函数声明。")
            info("  用 cget clibs 看看工具链里有哪些库；")
            info("  或指定头文件路径：cget install path/to/xxx.h")
        return 1, 0

    try:
        n, code = _cbind_to_file(header, link, out, limit)
    except Exception as e:
        err(f"转译失败：{type(e).__name__}: {e}")
        return 0, 1

    skipped = code.count('# 跳过')
    ok(f"已生成 {n} 条 extern 声明 → {out}")
    if skipped:
        info(f"  其中 {skipped} 个函数无法表达（变参/函数指针等），已注明原因")
    ok(f"完成：已安装 {'C++' if kind == 'cpp' else 'C'} 库 {link}")
    return 1, 0


def cmd_install(args, kind=None):
    """
    安装。kind 决定管哪一摊：
      'py'   → 只装 PyPI 包
      'c'    → 只装 C 库
      'cpp'  → 只装 C++ 库
      'auto' → 自动识别（pget 的兼容行为）
    """
    if kind is None:
        kind = _tool_kind()

    index = None
    pkgdir = packages_dir()
    reqs = []
    limit = 0
    force_link = None
    i = 0
    while i < len(args):
        a = args[i]
        if a in ('-i', '--index-url') and i + 1 < len(args):
            index = args[i + 1]; i += 2; continue
        if a in ('--target',) and i + 1 < len(args):
            pkgdir = args[i + 1]; i += 2; continue
        if a in ('-r', '--requirement') and i + 1 < len(args):
            reqs.extend(_read_requirements(args[i + 1])); i += 2; continue
        if a == '--limit' and i + 1 < len(args):
            limit = int(args[i + 1]); i += 2; continue
        if a in ('--link', '-l') and i + 1 < len(args):
            force_link = args[i + 1]; i += 2; continue
        if not a.startswith('-'):
            reqs.append(a)
        i += 1

    if not reqs:
        prog = {'py': 'pyget', 'c': 'cget', 'cpp': 'cpget', 'auto': 'pget'}[kind]
        hint = {'py': 'requests', 'c': 'zlib', 'cpp': 'stdc++',
                'auto': 'requests'}[kind]
        err(f"请指定要安装的库，例如：{prog} install {hint}")
        return 1

    okc = failc = 0

    # ---------------- 只装 C 库 ----------------
    if kind == 'c':
        for name in reqs:
            d = _force_c_spec(name, force_link, 'c')
            if d is None:
                failc += 1
                continue
            a, b = cmd_install_c(name, d, limit)
            okc += a; failc += b
        _print_ctail('_c')
        print()
        if failc:
            warn(f"完成：成功 {okc} 个，失败 {failc} 个")
            return 1
        ok(f"完成：共安装/确认 {okc} 个 C 库")
        return 0

    # ---------------- 只装 C++ 库 ----------------
    if kind == 'cpp':
        for name in reqs:
            d = _force_c_spec(name, force_link, 'cpp')
            if d is None:
                failc += 1
                continue
            a, b = cmd_install_c(name, d, limit)
            okc += a; failc += b
        _print_ctail('_cpp')
        print()
        if failc:
            warn(f"完成：成功 {okc} 个，失败 {failc} 个")
            return 1
        ok(f"完成：共安装/确认 {okc} 个 C++ 库")
        return 0

    # ---------------- 只装 Python 包 ----------------
    if kind == 'py':
        py_exe, pyver = target_python()
        info(f"目标 Python {pyver}  |  安装到 {pkgdir}")
        os.makedirs(pkgdir, exist_ok=True)
        done = set()
        for spec in reqs:
            r = _install_spec(spec, pkgdir, pyver, py_exe, index, done, depth=0)
            okc += r[0]; failc += r[1]
        print()
        if failc:
            warn(f"完成：成功 {okc} 个，失败 {failc} 个")
            return 1
        ok(f"完成：共安装/确认 {okc} 个 Python 包")
        return 0

    # ---------------- auto：自动识别（pget 兼容行为） ----------------
    py_specs, c_specs = [], []
    for r in reqs:
        d = detect_ecosystem(r)
        if d['kind'] in ('c', 'cpp'):
            c_specs.append((r, d))
        else:
            py_specs.append(r)

    if c_specs:
        for name, d in c_specs:
            a, b = cmd_install_c(name, d, limit)
            okc += a; failc += b
        _print_ctail('_c')

    if py_specs:
        py_exe, pyver = target_python()
        info(f"目标 Python {pyver}  |  安装到 {pkgdir}")
        for spec in py_specs:
            d = detect_ecosystem(spec)
            if '若确实要 C 库' in d.get('reason', ''):
                warn(f"{spec}：{d['reason']}")
        os.makedirs(pkgdir, exist_ok=True)
        done = set()
        for spec in py_specs:
            r = _install_spec(spec, pkgdir, pyver, py_exe, index, done, depth=0)
            okc += r[0]; failc += r[1]

    print()
    if failc:
        warn(f"完成：成功 {okc} 个，失败 {failc} 个")
        return 1
    ok(f"完成：共安装/确认 {okc} 个")
    return 0


def _print_ctail(subdir):
    """C/C++ 库装完后的统一提示。"""
    d = os.path.join(packages_dir(), subdir)
    print(dim(f"  声明文件目录：{d}"))
    if not os.path.isdir(d):
        return
    # import 名以声明文件里写的那行为准（文件名可能被净化过）
    names = []
    for fn in sorted(os.listdir(d)):
        if not fn.endswith('.cpyi'):
            continue
        listed = None
        try:
            with open(os.path.join(d, fn), 'r', encoding='utf-8') as f:
                for ln in f:
                    if ln.startswith('# 用法：') and 'import ' in ln:
                        listed = ln.split('import ', 1)[1].strip()
                        break
        except OSError:
            pass
        names.append(listed or os.path.splitext(fn)[0])
    if names:
        print(dim(f"  在 .cpy 里用：import {names[0]}"))


def _force_c_spec(name, force_link, kind):
    """
    在明确指定了工具身份时，构造库描述。
    找不到头文件不算失败 —— 会退化成"只生成 #link"。
    """
    if name.lower().endswith(('.h', '.hpp', '.hh', '.hxx')) or '/' in name or '\\' in name:
        h = _find_c_header(name)
        if h:
            base = force_link or os.path.basename(h).rsplit('.', 1)[0]
            return {'kind': kind, 'link': base, 'header': h,
                    'reason': '头文件路径'}
        warn(f"找不到头文件：{name}")
        return None
    if force_link:
        return {'kind': kind, 'link': force_link, 'header': None,
                'reason': '用户指定了 --link'}
    if kind == 'cpp' and name.lower() in CPP_STD_LIBS:
        return {'kind': 'cpp', 'link': name, 'header': None,
                'reason': 'C++ 标准库'}
    for linkname, header_rel in _c_lib_candidates(name):
        h = _find_c_header(header_rel)
        if h:
            return {'kind': kind, 'link': linkname, 'header': h,
                    'reason': f'匹配到 {os.path.basename(h)}'}
        if _has_c_library(linkname):
            return {'kind': kind, 'link': linkname, 'header': None,
                    'reason': f'工具链里有 lib{linkname}'}
    # 找不到：仍然按用户意图处理，只生成 #link
    return {'kind': kind, 'link': name, 'header': None,
            'reason': '未找到头文件，只生成链接指令'}


def _read_requirements(path):
    out = []
    if not os.path.isfile(path):
        warn(f"找不到依赖文件 {path}")
        return out
    with open(path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.split('#', 1)[0].strip()
            if line and not line.startswith('-'):
                out.append(line)
    return out


def _install_spec(spec, pkgdir, pyver, py_exe, index, done, depth=0):
    """返回 (成功数, 失败数)。"""
    # 本地 wheel
    if spec.lower().endswith('.whl') and os.path.isfile(spec):
        return _install_local_wheel(spec, pkgdir, pyver, py_exe, index, done, depth)

    name, specs = parse_spec(spec)
    if not name:
        return 0, 0
    key = _norm(name)
    if key in done:
        return 0, 0
    done.add(key)

    ind = "  " * depth
    data = pypi_json(name, index)
    if data is None:
        err(f"{ind}{name}: 在索引上找不到这个包（或网络不可用）")
        return 0, 1

    pyver_cur = pyver
    files = []
    chosen_ver = None

    # 从高到低找第一个"满足版本约束且有可用 wheel"的版本
    releases = _all_releases(data)
    cand_versions = sorted(releases.keys(), key=_ver_key, reverse=True)
    if not cand_versions:
        cand_versions = [data['info']['version']]
        releases = {data['info']['version']: data.get('urls', [])}

    for v in cand_versions:
        if specs and not satisfies(v, specs):
            continue
        w = pick_wheel(releases.get(v, []), pyver_cur)
        if w:
            files, chosen_ver = [w], v
            break

    if not chosen_ver:
        # 没有可用 wheel → 交给 pip（能处理 sdist 与本地编译）
        return _pip_fallback(spec, pkgdir, py_exe, ind, name)

    wheel = files[0]
    print(f"{ind}{bold(name)} {green(chosen_ver)}  "
          f"{dim(wheel['filename'])}")

    tmp = tempfile.mkdtemp(prefix="pget_")
    try:
        whl = os.path.join(tmp, wheel['filename'])
        download_file(wheel['url'], whl)
        old = _remove_installed(name, pkgdir)      # 升级：先清掉旧版本
        if old and old[1] != chosen_ver:
            print(dim(f"{ind}  （已移除旧版本 {old[1]}）"))
        install_wheel(whl, pkgdir)
        di = _dist_info_of(whl)
        meta = _read_metadata(os.path.join(pkgdir, di)) if di else {}
    except Exception as e:
        err(f"{ind}{name}: 安装失败（{type(e).__name__}: {e}）")
        shutil.rmtree(tmp, ignore_errors=True)
        return 0, 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    total_ok, total_fail = 1, 0

    # 依赖
    installed = _installed_map(pkgdir)
    for req in _runtime_requires(meta.get('requires', [])):
        dn, dspecs = parse_spec(req)
        if not dn:
            continue
        dk = _norm(dn)
        if dk in installed and satisfies(installed[dk][1], dspecs):
            continue
        if dk in done:
            continue
        sub_ok, sub_fail = _install_spec(req, pkgdir, pyver, py_exe, index,
                                         done, depth + 1)
        total_ok += sub_ok
        total_fail += sub_fail

    return total_ok, total_fail


def _install_local_wheel(whl_path, pkgdir, pyver, py_exe, index, done, depth):
    ind = "  " * depth
    try:
        di = _dist_info_of(whl_path)
        old = _remove_installed(_whl_name(whl_path), pkgdir)
        install_wheel(whl_path, pkgdir)
        meta = _read_metadata(os.path.join(pkgdir, di)) if di else {}
        extra = dim(f"  (本地文件{'; 替换 ' + old[1] if old else ''})")
        print(f"{ind}{bold(meta.get('name', os.path.basename(whl_path)))} "
              f"{green(meta.get('version', '?'))}{extra}")
    except Exception as e:
        err(f"{ind}本地 wheel 安装失败：{e}")
        return 0, 1

    total_ok, total_fail = 1, 0
    installed = _installed_map(pkgdir)
    for req in _runtime_requires(meta.get('requires', [])):
        dn, dspecs = parse_spec(req)
        if not dn:
            continue
        dk = _norm(dn)
        if dk in installed and satisfies(installed[dk][1], dspecs):
            continue
        if dk in done:
            continue
        s_ok, s_fail = _install_spec(req, pkgdir, pyver, py_exe, index,
                                     done, depth + 1)
        total_ok += s_ok
        total_fail += s_fail
    return total_ok, total_fail


def _pip_fallback(spec, pkgdir, py_exe, ind, name):
    """没有可用 wheel（只有源码包）时，交给 pip 处理。"""
    warn(f"{ind}{name}: 没有现成的 wheel，改用 pip 构建安装（可能较慢）")
    try:
        r = subprocess.run(
            [py_exe, '-m', 'pip', 'install', '--target', pkgdir,
             '--upgrade', '--no-warn-script-location', '--quiet', spec],
            capture_output=True, text=True, timeout=900)
    except Exception as e:
        err(f"{ind}{name}: pip 调用失败（{type(e).__name__}: {e}）")
        return 0, 1
    if r.returncode != 0:
        err(f"{ind}{name}: pip 安装失败")
        if r.stderr.strip():
            print(dim("      " + r.stderr.strip().splitlines()[-1]))
        return 0, 1
    ok(f"{ind}{name}: 已通过 pip 安装")
    return 1, 0


# ---------------------------------------------------------------------------
# 卸载 / 查询
# ---------------------------------------------------------------------------
def cmd_uninstall(args):
    names = [a for a in args if not a.startswith('-')]
    if not names:
        err("请指定要卸载的包，例如：pget uninstall requests")
        return 1
    pkgdir = packages_dir()
    installed = _installed_map(pkgdir)
    rc = 0
    for n in names:
        key = _norm(n)
        if key not in installed:
            err(f"未安装：{n}")
            rc = 1
            continue
        disp, ver, _di = installed[key]
        before = _count_files(pkgdir)
        _remove_installed(n, pkgdir)
        _prune_empty_dirs(pkgdir)
        removed = before - _count_files(pkgdir)
        ok(f"已卸载 {bold(disp)} {ver} {dim(f'({removed} 个文件)')}")
    return rc


def _count_files(root):
    n = 0
    for _cur, _dirs, files in os.walk(root):
        n += len(files)
    return n


def _prune_empty_dirs(root):
    for cur, dirs, files in os.walk(root, topdown=False):
        if cur == root:
            continue
        try:
            if not os.listdir(cur):
                os.rmdir(cur)
        except OSError:
            pass


def cmd_list(args):
    installed = _installed_map()
    if not installed:
        info(f"还没有安装任何第三方包（目录：{packages_dir()}）")
        k = _tool_kind()
        sample = {'py': ('pyget', 'requests'), 'c': ('cget', 'zlib'),
                  'cpp': ('cpget', 'stdc++'), 'auto': ('pget', 'requests')}[k]
        print(dim(f"  试试：{sample[0]} install {sample[1]}"))
        return 0
    rows = sorted(installed.values(), key=lambda x: x[0].lower())
    w = max(len(r[0]) for r in rows)
    print(bold(f"{'包名'.ljust(w)}  版本"))
    print(dim("-" * (w + 12)))
    for disp, ver, _ in rows:
        print(f"{disp.ljust(w)}  {ver}")
    print(dim(f"\n共 {len(rows)} 个包  |  {packages_dir()}"))
    return 0


def cmd_show(args):
    names = [a for a in args if not a.startswith('-')]
    if not names:
        err("请指定包名，例如：pget show requests")
        return 1
    installed = _installed_map()
    rc = 0
    for n in names:
        key = _norm(n)
        if key in installed:
            disp, ver, di = installed[key]
            m = _read_metadata(di)
            print(bold(f"{m.get('name', disp)} {ver}"))
            if m.get('summary'):
                print(f"  {m['summary']}")
            print(f"  位置: {di}")
            reqs = _runtime_requires(m.get('requires', []))
            if reqs:
                print("  依赖:")
                for r in reqs:
                    print(f"    - {r}")
            print()
            continue
        # 未安装 → 查索引
        data = pypi_json(n)
        if data:
            i = data['info']
            print(bold(f"{i.get('name')} {i.get('version')}") + dim("  (未安装)"))
            if i.get('summary'):
                print(f"  {i['summary']}")
            if i.get('home_page') or i.get('project_url'):
                print(f"  主页: {i.get('home_page') or i.get('project_url')}")
            print(dim(f"  安装：{_prog()} install {n}"))
            print()
        else:
            err(f"找不到包：{n}")
            rc = 1
    return rc


def cmd_freeze(args):
    installed = _installed_map()
    for disp, ver, _ in sorted(installed.values(), key=lambda x: x[0].lower()):
        print(f"{disp}=={ver}")
    return 0


_SIMPLE_CACHE = None


def cmd_search(args):
    kind = _tool_kind()
    # cget / cpget 的 search 直接查本机工具链里的 C 库，比 PyPI 有意义
    if kind in ('c', 'cpp') and not any(
            a in ('-i', '--index-url') for a in args):
        return cmd_clibs([a for a in args if not a.startswith('-')])
    kw = [a for a in args if not a.startswith('-')]
    if not kw:
        err(f"请给出关键词，例如：{_prog()} search json")
        return 1
    term = ' '.join(kw).lower()

    global _SIMPLE_CACHE
    cache_file = os.path.join(cache_dir(), 'simple-index.html')
    html = None
    if _SIMPLE_CACHE:
        html = _SIMPLE_CACHE
    elif os.path.isfile(cache_file) and time.time() - os.path.getmtime(cache_file) < 86400:
        try:
            with open(cache_file, 'r', encoding='utf-8', errors='replace') as f:
                html = f.read()
        except Exception:
            html = None

    if html is None:
        info("首次搜索需要拉取 PyPI 包名索引（约 20MB，之后会缓存 24 小时）…")
        base = _index_url().rstrip('/')
        try:
            raw = _http_get(base + '/simple/', timeout=180).decode('utf-8', 'replace')
        except Exception as e:
            err(f"无法获取包索引（{type(e).__name__}）")
            return 1
        try:
            os.makedirs(cache_dir(), exist_ok=True)
            with open(cache_file, 'w', encoding='utf-8') as f:
                f.write(raw)
        except Exception:
            pass
        html = raw
    _SIMPLE_CACHE = html

    names = re.findall(r'>([A-Za-z0-9._-]+)</a>', html)
    hits = [n for n in names if term in n.lower()]
    if not hits:
        warn(f"没有匹配 '{term}' 的包")
        return 0
    # 完全匹配 / 前缀匹配排前面
    hits.sort(key=lambda n: (n.lower() != term, not n.lower().startswith(term), len(n)))
    for n in hits[:40]:
        print(n)
    if len(hits) > 40:
        print(dim(f"… 共 {len(hits)} 个结果，只显示前 40 个"))
    return 0


def cmd_download(args):
    names = [a for a in args if not a.startswith('-')]
    if not names:
        err("请指定包名，例如：pget download requests")
        return 1
    _, pyver = target_python()
    out = os.getcwd()
    rc = 0
    for spec in names:
        name, _s = parse_spec(spec)
        data = pypi_json(name)
        if not data:
            err(f"找不到包：{name}"); rc = 1; continue
        w = pick_wheel(data.get('urls', []), pyver)
        if not w:
            err(f"{name}: 没有适合 Python {pyver} 的 wheel")
            rc = 1
            continue
        dest = os.path.join(out, w['filename'])
        info(f"下载 {w['filename']}")
        try:
            download_file(w['url'], dest)
            ok(f"已保存到 {dest}")
        except Exception as e:
            err(f"下载失败：{e}"); rc = 1
    return rc


def cmd_doctor(args):
    print(bold(f"pget {VERSION} 环境诊断"))
    print()
    py_exe, pyver = target_python()
    print(f"  目标 Python : {pyver}")
    print(f"  解释器路径  : {py_exe}")

    try:
        here = os.path.dirname(os.path.abspath(__file__))
        sys.path.insert(0, here)
        sys.path.insert(0, os.path.join(here, 'src'))
        import pypp
        d = pypp.find_python_dev()
        if d:
            print(f"  可嵌入      : {green('是')}  ({d['root']})")
            print(f"  头文件      : {d['include']}")
            print(f"  导入库      : {os.path.join(d['libs'], d['libname'])}")
            print(f"  共享库      : {d['dll']}")
        else:
            print(f"  可嵌入      : {yellow('否')}  "
                  f"{dim('（原生模式的 import 将退回 VM，功能一致）')}")
        tc = pypp.find_toolchain()
        print(f"  C 工具链    : {tc['path'] if tc else yellow('未找到')}")
    except Exception as e:
        print(f"  py++ 探测   : {yellow('不可用')} {dim(str(e))}")

    print(f"  安装目录    : {packages_dir()}")
    inst = _installed_map()
    print(f"  已装包      : {len(inst)} 个")
    print(f"  索引源      : {_index_url()}")

    # 版本一致性检查
    running = f"{sys.version_info[0]}.{sys.version_info[1]}"
    if running != pyver:
        print()
        warn(f"当前解释器是 {running}，但嵌入目标是 {pyver}。")
        print(dim("      pget 会按嵌入目标的版本挑 wheel；若两边大版本不同，"
                  "VM 模式下可能加载不了这些包。"))
    print()
    return 0


# ---------------------------------------------------------------------------
# C 库支持：头文件转译
# ---------------------------------------------------------------------------
_TYPE_WORDS = {
    'void', 'int', 'char', 'short', 'long', 'float', 'double',
    'unsigned', 'signed', 'const', 'volatile', 'size_t', 'ssize_t',
    'wchar_t', 'bool', 'intptr_t', 'uintptr_t',
    'int8_t', 'int16_t', 'int32_t', 'int64_t',
    'uint8_t', 'uint16_t', 'uint32_t', 'uint64_t',
}
_SKIP_NAMES = {'if', 'while', 'for', 'switch', 'return', 'sizeof',
               'defined', 'else', 'do', 'case'}


def _toolchain_include_dirs():
    """工具链的头文件搜索目录。"""
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        sys.path.insert(0, here)
        sys.path.insert(0, os.path.join(here, 'src'))
        import pypp
        tc = pypp.find_toolchain()
        if not tc:
            return []
        prefix = tc['prefix']
        return [d for d in (os.path.join(prefix, 'include'),
                            os.path.join(prefix, 'x86_64-w64-mingw32', 'include'))
                if os.path.isdir(d)]
    except Exception:
        return []


def _gather_text(path, depth=0, seen=None, incdirs=()):
    """
    读入头文件，并递归跟随 #include（限深度）。
    用途：把 uLong / z_streamp 这类定义在附属头文件里的 typedef 也收集到。
    """
    if seen is None:
        seen = set()
    p = os.path.normpath(os.path.abspath(path))
    if p in seen or depth > 3 or not os.path.isfile(p):
        return ''
    seen.add(p)
    try:
        with open(p, 'r', encoding='utf-8', errors='replace') as f:
            text = f.read()
    except Exception:
        return ''
    out = [text]
    base = os.path.dirname(p)
    for m in re.finditer(r'^[ \t]*#[ \t]*include[ \t]*([<"])([^>"]+)[>"]',
                         text, re.M):
        name = m.group(2)
        for cand in [os.path.join(base, name)] + \
                    [os.path.join(d, name) for d in incdirs]:
            if os.path.isfile(cand):
                out.append(_gather_text(cand, depth + 1, seen, incdirs))
                break
    return '\n'.join(out)


def _collect_typedefs(text):
    """从文本里收集 typedef 名 → 原始类型。"""
    body = re.sub(r'/\*.*?\*/', ' ', text, flags=re.S)
    body = re.sub(r'//[^\n]*', ' ', body)
    body = re.sub(r'^[ \t]*#[^\n]*', ' ', body, flags=re.M)
    body = re.sub(r'\s+', ' ', body)
    out = {}
    for m in re.finditer(
            r'typedef\s+([A-Za-z_][A-Za-z0-9_ \t\*]*?)\s+([A-Za-z_][A-Za-z0-9_]*)\s*;',
            body):
        out.setdefault(m.group(2), m.group(1).strip())
    return out


def _strip_export_macros(t: str) -> str:
    """
    去掉全大写的导出宏（ZEXTERN / ZEXPORT / MYAPI 之类）。
    这些是各库自己的"装饰"，不属于类型本身。
    """
    toks = [w for w in t.split() if not (w.isupper() and len(w) >= 3)]
    return ' '.join(toks)


def _c_type_to_pypp(t: str, typedefs=None, depth=0):
    """
    C 类型 → py++ extern 声明里用的类型名。

    数值类型**保留原始 C 写法**（unsigned long 就是 unsigned long），
    因为 C 的整数宽度与有无符号直接影响 ABI —— 塌缩成 int 会读到错值。
    py++ 侧统一当作整数处理，但声明必须精确。
    返回 '?' 表示无法识别（调用方跳过），'struct' 表示按值传结构体（跳过）。
    """
    s = ' '.join((t or '').replace('const', ' ').split()).strip()
    s = _strip_export_macros(s).strip()
    s = ' '.join(s.split())
    if not s:
        return 'int'
    if depth > 5:
        return '?'

    if '*' in s:
        base = ' '.join(s.replace('*', ' ').split())
        if typedefs and base in typedefs:
            return _c_type_to_pypp(typedefs[base] + ' *', typedefs, depth + 1)
        if base in ('char', 'unsigned char', 'signed char'):
            return 'str'
        return 'ptr'

    if typedefs and s in typedefs:
        return _c_type_to_pypp(typedefs[s], typedefs, depth + 1)

    if s == 'void':
        return 'void'
    if s in ('float', 'double', 'long double'):
        return 'double'
    if s.split()[0] in ('struct', 'union', 'enum'):
        return 'struct'          # 按值传结构体：py++ 表示不了
    if all(w in _TYPE_WORDS for w in s.split()):
        return s
    return '?'


def transpile_header(text: str, libname=None, limit=0, typedefs=None):
    """
    把 C 头文件里的函数声明转成 py++ 的 extern 声明。

    解析步骤：去注释 → 去预处理 → 收集 typedef → 正则找 `ret name(params);`。
    转译结果只是**起点**：C 的类型系统比 py++ 复杂（结构体、函数指针、
    变参…），无法识别的函数会被跳过并注明原因，请按实际头文件核对签名。
    """
    body = re.sub(r'/\*.*?\*/', ' ', text, flags=re.S)
    body = re.sub(r'//[^\n]*', ' ', body)
    body = re.sub(r'^[ \t]*#[^\n]*', ' ', body, flags=re.M)
    body = re.sub(r'__attribute__\s*\(\(.*?\)\)', ' ', body, flags=re.S)
    body = re.sub(r'\b(__cdecl|__stdcall|__fastcall|WINAPI|APIENTRY)\b', ' ', body)
    body = re.sub(r'\s+', ' ', body)

    # 先收集 typedef（含从 #include 头文件里带进来的），
    # 让 uLong / Bytef / z_streamp 这类名字能解析
    typedefs = dict(typedefs or {})
    for k, v in _collect_typedefs(text).items():
        typedefs.setdefault(k, v)

    decls, skipped, seen = [], [], set()
    for m in re.finditer(r'([A-Za-z_][A-Za-z0-9_]*)\s*\(([^()]*)\)\s*;', body):
        name = m.group(1)
        if name in _SKIP_NAMES or name in seen:
            continue
        seg = re.split(r'[;}{]', body[:m.start(1)])[-1].strip()
        ret_raw = _strip_export_macros(seg)
        if not ret_raw or 'typedef' in ret_raw or len(ret_raw) > 60:
            continue
        if not re.match(r'^[A-Za-z_][A-Za-z0-9_ \t\*]*$', ret_raw):
            continue
        if ret_raw.split()[0] in ('struct', 'union', 'enum'):
            continue

        params, bad = [], None
        praw = m.group(2).strip()
        if praw and praw != 'void':
            for p in praw.split(','):
                p = p.strip()
                if not p or p == '...':
                    continue
                toks = p.split()
                aname = ''
                if (len(toks) >= 2 and toks[-1] not in _TYPE_WORDS
                        and re.match(r'^[A-Za-z_][A-Za-z0-9_]*$', toks[-1])):
                    aname = toks.pop()
                ptype = _c_type_to_pypp(' '.join(toks), typedefs)
                if ptype in ('?', 'struct'):
                    bad = p
                    break
                params.append(f"{ptype} {aname}".strip())

        seen.add(name)
        if bad:
            skipped.append(f"# 跳过 {name}：参数类型无法转换（{bad}）")
            continue

        ret = _c_type_to_pypp(ret_raw, typedefs)
        if ret in ('?', 'struct'):
            skipped.append(f"# 跳过 {name}：返回类型无法转换（{ret_raw}）")
            continue

        sig = ', '.join(params)
        decls.append(f"extern {ret} {name}({sig})")
        if limit and len(decls) >= limit:
            break

    lines = ["# 由 pget cbind 从 C 头文件转译生成",
             "# 只是起点：请按实际头文件核对签名（类型宽度、指针、变参）",
             ""]
    if libname:
        lines.append(f"#link {libname}")
        lines.append("")
    lines.extend(decls)
    if skipped:
        lines.append("")
        lines.extend(skipped)
    return "\n".join(lines) + "\n", len(decls)


def cmd_cbind(args):
    """pget cbind <header.h> [--link 库名] [--out 文件] [--limit N]"""
    header = None
    libname = None
    out = None
    limit = 0
    i = 0
    while i < len(args):
        a = args[i]
        if a in ('--link', '-l') and i + 1 < len(args):
            libname = args[i + 1]; i += 2; continue
        if a in ('--out', '-o') and i + 1 < len(args):
            out = args[i + 1]; i += 2; continue
        if a == '--limit' and i + 1 < len(args):
            limit = int(args[i + 1]); i += 2; continue
        if not a.startswith('-'):
            header = a
        i += 1

    if not header:
        err(f"请指定 C 头文件，例如：{_prog()} cbind zlib.h --link z")
        return 1
    if not os.path.isfile(header):
        err(f"找不到头文件：{header}")
        return 1

    with open(header, 'r', encoding='utf-8', errors='replace') as f:
        text = f.read()

    # 跟随 #include 收集 typedef（uLong 等定义在 zconf.h 里）
    incdirs = [os.path.dirname(os.path.abspath(header))] + _toolchain_include_dirs()
    typedefs = _collect_typedefs(_gather_text(header, incdirs=incdirs))

    code, n = transpile_header(text, libname, limit, typedefs)
    if out:
        with open(out, 'w', encoding='utf-8', newline='\n') as f:
            f.write(code)
        ok(f"已生成 {n} 条 extern 声明 → {out}")
    else:
        sys.stdout.write(code)
    return 0


def _toolchain_dirs():
    """返回 (lib目录列表, include目录)。"""
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        sys.path.insert(0, here)
        sys.path.insert(0, os.path.join(here, 'src'))
        import pypp
        tc = pypp.find_toolchain()
        if not tc:
            return [], None
        prefix = tc['prefix']
        libs = [os.path.join(prefix, 'x86_64-w64-mingw32', 'lib'),
                os.path.join(prefix, 'lib')]
        return [d for d in libs if os.path.isdir(d)], os.path.join(prefix, 'include')
    except Exception:
        return [], None


def cmd_clibs(args):
    """列出工具链里可用的 C 库（有对应头文件的优先）。"""
    kw = [a for a in args if not a.startswith('-')]
    term = kw[0].lower() if kw else ''
    libdirs, incdir = _toolchain_dirs()
    if not libdirs:
        err("未找到 C 工具链（原生模式不可用）")
        return 1

    found = {}
    for d in libdirs:
        for fn in os.listdir(d):
            m = re.match(r'^lib([A-Za-z0-9_.+-]+?)\.(a|dll\.a)$', fn)
            if m:
                found.setdefault(m.group(1), d)

    rows = []
    for name in sorted(found):
        if term and term not in name.lower():
            continue
        has_h = bool(incdir) and (
            os.path.isfile(os.path.join(incdir, name + '.h'))
            or os.path.isdir(os.path.join(incdir, name)))
        rows.append((name, has_h))

    if not rows:
        warn("没有匹配的 C 库")
        return 0

    print(bold(f"{'库名'.ljust(28)} 头文件"))
    print(dim("-" * 40))
    for name, has_h in rows[:80]:
        mark = green("有") if has_h else dim("无")
        print(f"{name.ljust(28)} {mark}")
    print(dim(f"\n共 {len(rows)} 个  |  用 {_prog()} cbind <头文件> --link <库名> 生成声明"))
    if incdir:
        print(dim(f"  头文件目录: {incdir}"))
    return 0


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
# py++ 的包管理有三个平级命令，各管一摊：
#   pyget  → Python 库（PyPI）
#   cget   → C 库
#   cpget  → C++ 库
# 它们共用这一份实现，靠"以什么名字被调用"来决定管哪一摊。
# 老名字 pget 仍然可用，会自动识别（想明确指定时用 --py/--c/--cpp）。

TOOLS = {
    'pyget': 'py',      # Python / PyPI
    'cget':  'c',       # C
    'cpget': 'cpp',     # C++
    'pget':  'auto',    # 兼容：自动识别
}

TOOL_TITLE = {
    'py':   'Python 包管理器（PyPI）',
    'c':    'C 库管理器',
    'cpp':  'C++ 库管理器',
    'auto': '包管理器（PyPI / C / C++ 通用）',
}


def _tool_kind(argv0=None):
    """
    判断当前是哪个工具在跑，返回 'py' / 'c' / 'cpp' / 'auto'。
    优先看环境变量（启动器会设），其次看脚本名。
    """
    forced = os.environ.get('PYPP_PGET_TOOL')
    if forced in ('py', 'c', 'cpp', 'auto'):
        return forced
    name = os.path.basename(argv0 or sys.argv[0] or '')
    stem = os.path.splitext(name)[0].lower()
    # pyget.bat / cget.exe / cpget … 都能认出来
    for tool, kind in TOOLS.items():
        if stem == tool or stem.startswith(tool):
            return kind
    return 'auto'


def _build_help(kind):
    """按工具身份生成帮助文本。"""
    name = {'py': 'pyget', 'c': 'cget', 'cpp': 'cpget', 'auto': 'pget'}[kind]
    title = TOOL_TITLE[kind]

    if kind == 'py':
        sec = f"""{bold('pyget 管什么:')}
  只装 Python 库（PyPI）。装到 ~/.pypp/packages，{bold('import')} 直接用。
  原生模式与 VM 模式都能用；没有 wheel 时自动交给 pip 构建。

{bold('示例:')}
  pyget install requests
  pyget install "flask>=3.0"
  pyget install -i https://pypi.tuna.tsinghua.edu.cn/simple numpy
  pyget install -r requirements.txt
  pyget list
  pyget freeze > requirements.txt
  pyget uninstall requests

{bold('要装 C / C++ 库请用:')}
  cget install zlib        # C 库
  cpget install stdc++     # C++ 库"""
    elif kind == 'c':
        sec = f"""{bold('cget 管什么:')}
  只装 C 库。转译头文件生成 extern 声明，存到 ~/.pypp/packages/_c/。
  声明可用 {bold('import <库名>')} 引入，py++ 直接链接调用（仅原生模式）。

{bold('示例:')}
  cget install zlib                  # 按库名，自动找 zlib.h
  cget install sqlite3
  cget install ./inc/curl/curl.h     # 按头文件路径
  cget install zlib --link z         # 手动指定链接名
  cget list                          # 看装了哪些 C 库
  cget show zlib
  cget uninstall zlib
  cget search z                      # 列出名字含 z 的库
  cget clibs                         # 看工具链里有哪些库可用

{bold('用法（在你的 .cpy 里）:')}
  import z
  print(zlibVersion())"""
    elif kind == 'cpp':
        sec = f"""{bold('cpget 管什么:')}
  只装 C++ 库。和 cget 一样转译头文件，但按 C++ 的规则处理：
  · C++ 标准库（{cyan('stdc++')} 等）不需要头文件，直接生成 #link
  · C++ 头文件（.hpp/.hh/.hxx）按 C++ 语义转译
  声明存到 ~/.pypp/packages/_cpp/。

{bold('示例:')}
  cpget install stdc++               # C++ 标准库
  cpget install fmt                  # 按库名
  cpget install ./include/fmt/format.h   # 按头文件
  cpget list
  cpget show fmt
  cpget uninstall fmt
  cpget clibs                        # 看工具链里有哪些库可用

{bold('用法（在你的 .cpy 里）:')}
  import stdc++
  # 或者把声明直接复制进你的文件"""
    else:
        sec = f"""{bold('pget 管什么:')}
  通用入口，自动识别 PyPI / C / C++ 三种来源。

{bold('推荐用专用命令（更明确）:')}
  {cyan('pyget install requests')}    # Python 库
  {cyan('cget install zlib')}         # C 库
  {cyan('cpget install stdc++')}      # C++ 库

{bold('自动识别规则:')}
  1. 参数是头文件（.h/.hpp）      → C 库
  2. 参数是 C++ 标准库名          → C++ 库
  3. 工具链里有对应头文件/库       → C 库
  4. 都不是                       → PyPI 包

{bold('示例:')}
  pget install requests
  pget install zlib
  pget install stdc++
  pget install numpy --py            # 强制按 PyPI
  pget install zlib --c              # 强制按 C
  pget install stdc++ --cpp          # 强制按 C++"""

    return f"""{bold(name + ' ' + VERSION)} — {title}

{bold('用法:')}
  {name} <命令> [参数]

{bold('命令:')}
  install <库>...      安装
  uninstall <库>...    卸载
  list                 列出已安装
  show <库>            显示信息
  freeze               输出可重装清单
  search <关键词>       搜索
  download <库>        只下载不安装
  doctor               环境诊断

{sec}

{bold('通用参数:')}
  -i, --index-url URL  指定索引（PyPI，默认 https://pypi.org）
  --target DIR         指定安装目录（默认 ~/.pypp/packages）
  -r, --requirement F  从 requirements.txt 批量安装
  --link 库名          手动指定 C/C++ 链接名
  --limit N            最多转译 N 条声明

{bold('环境变量:')}
  PYPP_PKG_DIR    换包目录      PYPP_INDEX      换索引源
  PYPP_PYTHON     指定嵌入的 Python
"""


HELP = _build_help('auto')


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    kind = _tool_kind()

    # 子命令别名：--py/--c/--cpp 可临时切换身份
    if args and args[0] in ('--py', '--pypi'):
        kind = 'py'; args = args[1:]
    elif args and args[0] == '--c':
        kind = 'c'; args = args[1:]
    elif args and args[0] == '--cpp':
        kind = 'cpp'; args = args[1:]

    prog = {'py': 'pyget', 'c': 'cget', 'cpp': 'cpget', 'auto': 'pget'}[kind]

    if not args or args[0] in ('-h', '--help', 'help'):
        print(_build_help(kind))
        return 0
    if args[0] in ('-v', '--version'):
        print(f"{prog} {VERSION}")
        return 0

    cmd, rest = args[0], args[1:]
    table = {
        'install': cmd_install,
        'uninstall': cmd_uninstall,
        'remove': cmd_uninstall,
        'list': cmd_list,
        'ls': cmd_list,
        'show': cmd_show,
        'info': cmd_show,
        'freeze': cmd_freeze,
        'search': cmd_search,
        'download': cmd_download,
        'doctor': cmd_doctor,
        'cbind': cmd_cbind,
        'clibs': cmd_clibs,
    }
    fn = table.get(cmd)
    if not fn:
        err(f"未知命令：{cmd}")
        print(dim(f"  运行 {_prog()} --help 查看用法"))
        return 2
    try:
        return fn(rest) or 0
    except KeyboardInterrupt:
        print()
        warn("已取消")
        return 130


if __name__ == '__main__':
    sys.exit(main())
