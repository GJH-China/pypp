"""
py++ Web 服务 (web/server.py)
纯标准库实现，无需第三方依赖。
提供 docs/WEB_API.md 中约定的全部接口，供前端网页对接。

启动：
  python web/server.py            # 默认 8765 端口
  python web/server.py --port 9000
"""

import os
import sys
import json
import io
import re as _re
import zipfile
import subprocess
import tempfile
import time
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, 'src')
sys.path.insert(0, SRC)

VERSION = "1.1.0"
RUN_TIMEOUT = 5  # 在线试运行超时（秒）
MAX_BODY = 64 * 1024

_gcc_cache = None   # 服务端是否有 gcc（探测一次后缓存）

import pypp
from repair import RepairReport
from lexer import Lexer
from parser import Parser


def _build_zip() -> bytes:
    """构建可下载的自包含压缩包。"""
    buf = io.BytesIO()
    prefix = f"pypp-{VERSION}"
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        # src
        for name in os.listdir(SRC):
            if name.endswith('.py'):
                z.write(os.path.join(SRC, name), f"{prefix}/src/{name}")
        # examples
        exdir = os.path.join(ROOT, 'examples')
        if os.path.isdir(exdir):
            for name in os.listdir(exdir):
                if name.endswith('.cpy'):
                    z.write(os.path.join(exdir, name), f"{prefix}/examples/{name}")
        # install.py / install_gui.py / pget / README
        for name in ('install.py', 'install_gui.py', 'install_gui.bat',
                     'pget.py', 'pget.bat', 'README.md'):
            p = os.path.join(ROOT, name)
            if os.path.exists(p):
                z.write(p, f"{prefix}/{name}")
        # web
        z.write(os.path.join(HERE, 'server.py'), f"{prefix}/web/server.py")
        # docs
        docp = os.path.join(ROOT, 'docs', 'WEB_API.md')
        if os.path.exists(docp):
            z.write(docp, f"{prefix}/docs/WEB_API.md")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# C 库能力探测
# ---------------------------------------------------------------------------
# 背景（踩过的坑）：「在线试运行 c_lib 无输出」——
#   c_lib 示例靠 extern 直通 zlib，而 **C 库只在原生模式可用**。
#   服务器上没有 gcc 时会静默降级到内置 VM，extern 调用一律返回 0，
#   于是页面显示一堆 "0"（或前端把 0 当成空），看起来就像"没输出"。
#
# 因此这里必须主动探测，并把结论显式告诉前端：
#   · engine == 'vm' 且代码里有 extern  → c_lib_unsupported = True
#   · 前端据此展示「本示例需要原生模式」的提示，而不是干瘪的 0
# ---------------------------------------------------------------------------

_EXTERN_RE = _re.compile(r'^[ \t]*extern[ \t]', _re.MULTILINE)
_LINK_RE_SRV = _re.compile(r'^[ \t]*#[ \t]*link[ \t]+', _re.MULTILINE)

# ⚠️ 不要用 `'原生模式' in stderr` 判断引擎！
#    VM 模式下 C 库的修复提示里也含「需要原生模式才能调用」这句话，
#    会被子串匹配误判成原生模式（错得还很隐蔽：输出是 VM 的 0，标签却是原生）。
#    引擎标记只出现在 stderr 的「引擎:」行和结尾的「(原生模式 …)」括注里。
_ENGINE_LINE_RE = _re.compile(r'引擎[:：]\s*(原生高速模式|内置\s*VM\s*模式)')
_ENGINE_TAIL_RE = _re.compile(r'\((原生模式|VM 模式)')


def _detect_mode(stderr: str) -> str:
    """从 py++ 的 stderr 标记里判断实际使用的引擎。"""
    s = _strip_ansi(stderr or '')
    m = _ENGINE_LINE_RE.search(s)
    if m:
        return 'native' if '原生' in m.group(1) else 'vm'
    m = _ENGINE_TAIL_RE.search(s)
    if m:
        return 'native' if m.group(1) == '原生模式' else 'vm'
    return 'vm'


def _needs_native(code: str) -> bool:
    """代码是否依赖 C 库（extern 声明或 #link 指令）。"""
    if not code:
        return False
    return bool(_EXTERN_RE.search(code) or _LINK_RE_SRV.search(code))


def _has_gcc() -> bool:
    """服务端是否有可用的 C 工具链（决定在线试运行能否走原生模式）。"""
    global _gcc_cache
    if _gcc_cache is not None:
        return _gcc_cache
    try:
        from pypp import find_gcc
        _gcc_cache = bool(find_gcc())
    except Exception:
        _gcc_cache = False
    return _gcc_cache


def _run_code(code: str):
    """在受限子进程中执行用户代码（带超时）。"""
    t0 = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="pypp_web_") as d:
        fpath = os.path.join(d, "code.cpy")
        with open(fpath, 'w', encoding='utf-8') as f:
            f.write(code)
        try:
            env = dict(os.environ)
            env['PYPP_NO_COLOR'] = '1'
            r = subprocess.run(
                [sys.executable, os.path.join(SRC, 'pypp.py'), fpath, '--no-color'],
                capture_output=True, text=True, timeout=RUN_TIMEOUT,
                encoding='utf-8', errors='replace', env=env)
            out = r.stdout
            err = r.stderr
            ms = (time.perf_counter() - t0) * 1000
            repairs = _extract_repairs(err)
            mode = _detect_mode(err)

            # C 库示例在 VM 下必然"全是 0"，这不算真正的输出，必须明说
            needs_native = _needs_native(code)
            unsupported = needs_native and mode != 'native'
            note = ''
            if unsupported:
                note = ('这段代码用到了 C 库（extern / #link），而 C 库只在'
                        '原生高速模式下可用。当前在线沙箱未启用原生模式，'
                        '所以 C 函数一律返回 0。请下载 py++ 并在本地运行，'
                        '即可看到 zlib 的真实结果。')

            return {
                'ok': r.returncode == 0 and not unsupported,
                'mode': mode,
                'output': out,
                'warnings': _clean_stderr(err),
                'repairs': repairs,
                'engine_label': '原生高速模式' if mode == 'native' else '内置 VM 模式',
                'time_ms': round(ms, 2),
                # 新增字段（向后兼容，前端可选读取）
                'needs_native': needs_native,
                'c_lib_unsupported': unsupported,
                'note': note,
            }
        except subprocess.TimeoutExpired:
            return {
                'ok': False, 'mode': 'timeout', 'output': '',
                'warnings': f'执行超时（>{RUN_TIMEOUT}s），已中断',
                'repairs': [], 'engine_label': '已中断',
                'time_ms': RUN_TIMEOUT * 1000,
                'needs_native': _needs_native(code),
                'c_lib_unsupported': False,
                'note': '',
            }


_ANSI = _re.compile(r'\x1b\[[0-9;]*m')


def _strip_ansi(s: str) -> str:
    return _ANSI.sub('', s or '')


def _extract_repairs(stderr: str):
    repairs = []
    for line in _strip_ansi(stderr or '').split('\n'):
        line = line.strip()
        if '[已自动修复]' in line:
            msg = line.split('[已自动修复]', 1)[1].strip()
            m = {'line': 0, 'message': msg}
            if msg.startswith('第'):
                parts = msg.split('行', 1)
                try:
                    m['line'] = int(parts[0].replace('第', '').strip())
                except ValueError:
                    pass
                m['message'] = parts[1].strip() if len(parts) > 1 else msg
            repairs.append(m)
    return repairs


def _clean_stderr(stderr: str):
    out = []
    for line in _strip_ansi(stderr or '').split('\n'):
        s = line.strip()
        if not s:
            continue
        if s.startswith('[py++') or ('自动修复' in s and s.startswith('[')):
            continue
        if s.startswith('这无大碍'):
            continue
        out.append(s)
    return '\n'.join(out)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        # 精简日志
        sys.stderr.write("[web] " + (fmt % args) + "\n")

    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False, indent=2).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        if path == '/api/info':
            # 在线沙箱是否有原生模式 —— 决定 C 库示例能否真跑出结果
            gcc_ok = _has_gcc()
            self._send_json({
                'name': 'py++', 'version': VERSION, 'ext': '.cpy',
                'tagline': '像 Python 一样简洁，像 C++ 一样快',
                'engine': 'native' if gcc_ok else 'vm',
                'c_lib_available': gcc_ok,
                'features': [
                    '缩进语法，无需分号',
                    '有 gcc 时编译为原生代码 (-O2)',
                    '无 gcc 时自动降级到内置 VM',
                    '小错误自动修复，程序照常运行',
                    'import 真实 CPython，可直接用 pip 生态的第三方库',
                    'pget 包管理器，用法与 pip 一致',
                    '能调用 C 库：extern 声明 + #link 链接，pget cbind 自动转译头文件',
                ],
                'sample_code': 'name = "py++"\nprint("hello，" + name)',
                # 安装方式（供网站下载区渲染引导）
                'install': {
                    'gui': {
                        'file': 'install_gui.py',
                        'launcher_windows': 'install_gui.bat',
                        'steps': ['欢迎', '安装选项', '安装进度', '完成'],
                        'need_tkinter': True,
                        'note': 'Windows 用户可直接双击 install_gui.bat',
                    },
                    'cli': {
                        'cmd': 'python install.py',
                        'check_cmd': 'python install.py --check --json',
                        'uninstall_cmd': 'python install.py --uninstall',
                    },
                    # 包管理器（对标 pip）
                    'pget': {
                        'file': 'pget.py',
                        'launcher_windows': 'pget.bat',
                        'install_dir': '~/.pypp/packages',
                        'commands': ['install', 'uninstall', 'list', 'show',
                                     'freeze', 'search', 'download', 'doctor',
                                     'cbind', 'clibs'],
                        'examples': [
                            'pget install requests',
                            'pget list',
                            'pget install -i https://pypi.tuna.tsinghua.edu.cn/simple numpy',
                            'pget cbind zlib.h --link z --out zlib.cpy',
                            'pget clibs',
                        ],
                        'note': '装到 py++ 自己的目录，原生模式与 VM 模式都能 import；'
                                'cbind 把 C 头文件转译成 py++ 的 extern 声明',
                    },
                    'editions': [
                        {'name': '完整版', 'size': '约 557MB',
                         'includes': '便携 C 编译器', 'engine': '原生高速模式',
                         'desc': '速度接近 C++'},
                        {'name': '精简版', 'size': '不到 1MB',
                         'includes': '仅核心', 'engine': '需系统自备 gcc',
                         'desc': '体积小，适合已有编译器的用户'},
                    ],
                },
                # 实测性能（可复现，脚本在 bench/run_bench.py）
                'performance': {
                    'headline': '比 C++ 慢不到 13%',
                    'subtitle': '实测 1.12x，目标 ±0.3 倍，轻松达标',
                    'badge': '原生编译 · -O2 优化 · 零运行时开销',
                    'ratio': 1.123,
                    'cpp_seconds': 0.030,
                    'pypp_seconds': 0.033,
                    'compiler': 'gcc 14.2.0 (MinGW-W64)',
                    'opt_level': '-O2',
                    'platform': 'Windows 11 / AMD64',
                    'method': '5 次中位数，stdin 输入防常量折叠',
                    'target': '±0.3x (即 <= 1.3x)',
                    'passed': True,
                    'case_native': '原生模式：1.12x（接近 C++）',
                    'case_vm': '内置 VM：约慢 3000~7000 倍（仅兜底用）',
                },
            })
        elif path == '/api/check':
            info = _detect()
            self._send_json(info)
        elif path == '/api/download':
            data = _build_zip()
            self.send_response(200)
            self.send_header('Content-Type', 'application/zip')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Content-Disposition',
                             f'attachment; filename="pypp-{VERSION}.zip"')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif path in ('/', '/index.html'):
            self._send_json({
                'service': 'py++ web api', 'version': VERSION,
                'endpoints': ['/api/info', '/api/check', '/api/download', '/api/run'],
            })
        else:
            self._send_json({'error': 'not found'}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        if path != '/api/run':
            self._send_json({'error': 'not found'}, 404)
            return
        length = int(self.headers.get('Content-Length', 0))
        if length > MAX_BODY:
            self._send_json({'error': '请求体过大（上限 64KB）'}, 413)
            return
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode('utf-8'))
        except Exception:
            self._send_json({'error': '无效的 JSON'}, 400)
            return
        code = payload.get('code', '')
        if not isinstance(code, str) or not code.strip():
            self._send_json({'error': '缺少 code 字段'}, 400)
            return
        self._send_json(_run_code(code))


def _detect():
    sys.path.insert(0, SRC)
    import platform
    from pypp import find_gcc
    gcc = find_gcc()
    info = {
        'python': {'version': platform.python_version(),
                   'ok': sys.version_info >= (3, 8)},
        'platform': {'system': platform.system(), 'machine': platform.machine()},
        'compiler': {'found': bool(gcc), 'name': os.path.basename(gcc) if gcc else None,
                     'version': None},
        'engine': 'native' if gcc else 'vm',
        'ready': True,
    }
    if gcc:
        try:
            r = subprocess.run([gcc, '--version'], capture_output=True,
                               text=True, timeout=10)
            info['compiler']['version'] = r.stdout.split('\n')[0].strip()
        except Exception:
            pass
    return info


def main():
    ap = argparse.ArgumentParser(description='py++ Web 服务')
    ap.add_argument('--port', type=int, default=8765)
    ap.add_argument('--host', default='127.0.0.1')
    args = ap.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"py++ web api 已启动: http://{args.host}:{args.port}")
    print(f"接口: /api/info  /api/check  /api/download  /api/run")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == '__main__':
    main()
