"""
py++ 图形化安装器 (install_gui.py)
仿 Python 官方安装器的四步向导：

    欢迎页 → 选项页 → 安装进度 → 完成页

与命令行版 install.py 共享同一套安装逻辑语义，但更适合双击运行。
命令行版仍然是网站调用的入口（--json），GUI 版是给人用的。

启动方式：
    python install_gui.py          # 需要带 tkinter 的 Python
    install_gui.bat                # Windows 一键启动（自动挑解释器）
"""

import os
import sys
import shutil
import subprocess
import threading
import traceback

def _resource_root():
    """定位随程序分发的资源目录（src / examples / tools / README …）。

    两种运行方式：
      · 源码运行        → __file__ 所在目录
      · PyInstaller exe → sys._MEIPASS（单文件模式解压出的临时目录）

    这里踩过一个坑：单文件 exe 的 `__file__` 指向临时解压目录，
    如果只靠它去找 tools/mingw64，会把"没带 gcc"误判成"路径写错"。
    所以统一走这个函数。
    """
    meipass = getattr(sys, '_MEIPASS', None)
    if meipass and os.path.isdir(meipass):
        return meipass
    return os.path.dirname(os.path.abspath(__file__))


def _is_frozen():
    """是否运行在 PyInstaller 打包的 exe 里。"""
    return bool(getattr(sys, 'frozen', False))


HERE = _resource_root()
SRC_DIR = os.path.join(HERE, 'src')
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

VERSION = "1.1.0"
PYPP_HOME = os.path.join(os.path.expanduser('~'), '.pypp')
BUNDLED_GCC = os.path.join(HERE, 'tools', 'mingw64', 'mingw64')

# 单文件 exe 里内嵌的便携 Python 运行时（含 python.exe + 标准库）。
# 源码运行时该目录不存在；exe 运行时指向 _MEIPASS 下的 runtime/。
BUNDLED_RUNTIME = os.path.join(HERE, 'runtime')

# 便携运行时安装到的位置
RUNTIME_DIR_NAME = 'runtime'


def _is_frozen_exe(path):
    """这个路径是不是"我们自己被打包成的 exe"（而非真正的 Python）。"""
    if not path:
        return False
    # 打包后 sys.executable 指向 exe 自己；用文件名粗判一下
    base = os.path.basename(path).lower()
    if base.startswith('python'):
        return False
    return base.endswith('.exe') and not base.startswith('py')


def _runtime_python(runtime_root):
    """在便携运行时目录里找 python.exe（Windows）或 python3（POSIX）。"""
    for rel in ('python.exe', 'pythonw.exe', 'python3', 'bin/python3'):
        p = os.path.join(runtime_root, *rel.split('/'))
        if os.path.isfile(p):
            return p
    return None

# 便携 C 编译器（MinGW-W64）的在线下载源。
# 用 winlibs 的稳定直链：由 GitHub Releases 提供，无需登录。
# 允许用环境变量覆盖（内网镜像 / 自建源 / 测试）。
#   PYPP_GCC_URL=https://your-mirror/mingw64.zip
GCC_DOWNLOAD_URL = os.environ.get(
    'PYPP_GCC_URL',
    'https://github.com/brechtsanders/winlibs_mingw/releases/download/'
    '15.2.0posix-13.0.0-ucrt-r2/'
    'winlibs-x86_64-posix-seh-gcc-15.2.0-mingw-w64ucrt-13.0.0-r2.zip')


# ---------------------------------------------------------------------------
# tkinter 可用性检查（必须在 import tkinter 之前）
# ---------------------------------------------------------------------------
def _tk_available():
    try:
        import tkinter  # noqa: F401
        return True, None
    except Exception as e:
        return False, str(e)


# ---------------------------------------------------------------------------
# 安装引擎（与界面解耦，方便单独测试）
# ---------------------------------------------------------------------------
class Installer:
    """执行真正的安装动作。所有方法都通过 log 回调向界面反馈进度。

    gcc_mode 三选一：
      'none'     不要编译器（最小安装，用 VM 模式）
      'download' 在线下载便携工具链（推荐，无需离线包）
      'bundled'  用安装包自带的工具链（仅完整离线包可用）
    """

    def __init__(self, target_dir, with_gcc=True, add_path=True,
                 install_examples=True, log=None, home_dir=None,
                 gcc_mode=None, gcc_url=None, assoc=False):
        self.target = os.path.abspath(target_dir)
        # 兼容旧的布尔参数：with_gcc=True 时按"包内有就用包内，否则下载"
        if gcc_mode is None:
            if not with_gcc:
                gcc_mode = 'none'
            elif os.path.isdir(BUNDLED_GCC):
                gcc_mode = 'bundled'
            else:
                gcc_mode = 'download'
        self.gcc_mode = gcc_mode
        self.with_gcc = gcc_mode != 'none'
        self.gcc_url = gcc_url or GCC_DOWNLOAD_URL
        self.add_path = add_path
        self.install_examples = install_examples
        # .cpy 双击运行（文件关联）。
        # ⚠️ 默认 False（opt-in）：它会写**真实注册表**（HKCU）。
        #    测试/脚本直接构造 Installer 时不应意外触碰用户环境；
        #    GUI 由复选框显式传入，install.py 由 --no-assoc 显式控制。
        #    （踩过的坑：默认 True 时 TestInstaller 不传该参数，
        #     跑完测试就把真实 .cpy 关联改写指向临时 home。）
        self.assoc = assoc
        self.assoc_status = 'skipped'   # ok / failed / skipped
        self.log = log or (lambda msg, level='info': None)
        self.home_dir = home_dir or PYPP_HOME   # 可覆盖，便于自检不污染真实目录
        self.cancel_flag = False
        self.gcc_status = 'skipped'   # ok / failed / skipped

    @property
    def bindir(self):
        """命令目录（pypp.bat / pypprun.bat 所在处）。"""
        return os.path.join(self.home_dir, 'bin')

    # -- 小工具 ------------------------------------------------------------
    def _copy_tree(self, src, dst, label):
        """带进度回调的目录复制。"""
        if not os.path.isdir(src):
            self.log(f"跳过 {label}（源目录不存在）", 'warn')
            return 0
        count = 0
        for root, dirs, files in os.walk(src):
            if self.cancel_flag:
                return count
            rel = os.path.relpath(root, src)
            tdir = dst if rel == '.' else os.path.join(dst, rel)
            os.makedirs(tdir, exist_ok=True)
            for fn in files:
                if self.cancel_flag:
                    return count
                shutil.copy2(os.path.join(root, fn), os.path.join(tdir, fn))
                count += 1
                if count % 200 == 0:
                    self.log(f"  正在复制 {label} … 已处理 {count} 个文件", 'progress')
        return count

    # -- 主流程 ------------------------------------------------------------
    def run(self):
        steps = [
            ("检查安装目录", self._step_prepare),
            ("部署 Python 运行时", self._step_runtime),
            ("复制 py++ 核心", self._step_core),
            ("生成 pypp 命令", self._step_launcher),
        ]
        if self.assoc:
            steps.append(("注册 .cpy 双击运行", self._step_assoc))
        if self.install_examples:
            steps.append(("安装示例程序", self._step_examples))
        if self.with_gcc:
            steps.append(("部署便携 C 编译器", self._step_gcc))
        if self.add_path:
            steps.append(("配置 PATH 环境变量", self._step_path))
        steps.append(("写入卸载程序", self._step_uninstaller))

        total = len(steps)
        for i, (name, fn) in enumerate(steps, 1):
            if self.cancel_flag:
                self.log("安装已取消。", 'warn')
                return False
            self.log(f"[{i}/{total}] {name}", 'step')
            try:
                fn()
            except Exception as e:
                self.log(f"  失败：{e}", 'error')
                self.log(traceback.format_exc(), 'debug')
                return False
        self.log("全部安装步骤已完成。", 'ok')
        return True

    # -- 各步骤 ------------------------------------------------------------
    def _step_prepare(self):
        os.makedirs(self.target, exist_ok=True)
        self.log(f"  安装目标：{self.target}", 'info')

    def _step_runtime(self):
        """部署便携 Python 运行时。

        为什么需要：py++ 编译器是 Python 写的，装完必须有个 Python 能跑它。
        · 源码安装（含运行时的完整包）→ 把 runtime.zip 解开放到 ~/.pypp/runtime
        · exe 安装               → 内嵌的 runtime.zip 同样解开
        · 系统已有 Python        → 也照样装一份，保证 pypp 命令始终可用
        若包里没带运行时，就退回"用系统 Python"。
        """
        self.runtime_root = os.path.join(self.home_dir, RUNTIME_DIR_NAME)

        # 已经装过就不重复解压（几百个文件，省时间）
        existed = _runtime_python(self.runtime_root)
        if existed:
            self.log(f"  Python 运行时已存在：{existed}", 'info')
            self.runtime_python = existed
            self.runtime_status = 'ok'
            return

        zip_path = os.path.join(BUNDLED_RUNTIME, 'pypp-runtime.zip')
        if not os.path.isfile(zip_path):
            # 没带运行时 —— 退回系统 Python
            sys_py = self._system_python()
            if sys_py:
                self.log(f"  安装包未附带运行时，将使用系统 Python：{sys_py}",
                         'warn')
                self.runtime_python = sys_py
                self.runtime_status = 'system'
            else:
                self.log("  未找到可用的 Python，pypp 命令可能无法运行。",
                         'error')
                self.runtime_python = None
                self.runtime_status = 'missing'
            return

        import zipfile
        self.log("  正在解压 Python 运行时（约 700 个文件）…", 'info')
        os.makedirs(self.runtime_root, exist_ok=True)
        try:
            with zipfile.ZipFile(zip_path) as z:
                names = z.namelist()
                for i, n in enumerate(names, 1):
                    if self.cancel_flag:
                        return
                    z.extract(n, self.runtime_root)
                    if i % 300 == 0:
                        self.log(f"  已解压 {i}/{len(names)} …", 'progress')
        except Exception as e:
            self.log(f"  解压运行时失败：{e}", 'error')
            self.runtime_python = self._system_python()
            self.runtime_status = 'failed'
            return

        found = _runtime_python(self.runtime_root)
        if found:
            self.runtime_python = found
            self.runtime_status = 'ok'
            self.log(f"  已部署 Python 运行时：{found}", 'ok')
        else:
            self.log("  解压后未找到 python.exe，改用系统 Python。", 'warn')
            self.runtime_python = self._system_python()
            self.runtime_status = 'failed'

    @staticmethod
    def _system_python():
        """系统里可用的 Python（用于没带运行时的情况）。"""
        import shutil as _sh
        for c in (sys.executable, _sh.which('python'), _sh.which('python3')):
            if c and os.path.isfile(c) and not _is_frozen_exe(c):
                return c
        return None

    def _step_core(self):
        n = self._copy_tree(SRC_DIR, os.path.join(self.target, 'src'), '核心源码')
        for extra in ('web', 'docs', 'bench'):
            src = os.path.join(HERE, extra)
            if os.path.isdir(src):
                n += self._copy_tree(src, os.path.join(self.target, extra), extra)
        for top in ('README.md', 'pget.py', 'pget.bat',
                    'pyget.bat', 'cget.bat', 'cpget.bat'):
            f = os.path.join(HERE, top)
            if os.path.isfile(f):
                shutil.copy2(f, os.path.join(self.target, top))
                n += 1
        # 「快速精通」指南单独放一份到安装根目录（docs/ 里已有一份），
        # 让用户装完第一眼就能找到它
        qs = os.path.join(HERE, 'docs', '快速精通.md')
        if os.path.isfile(qs):
            shutil.copy2(qs, os.path.join(self.target, '快速精通.md'))
            n += 1
        self.log(f"  已安装 {n} 个文件", 'info')

    def _step_launcher(self):
        bindir = os.path.join(self.home_dir, 'bin')
        os.makedirs(bindir, exist_ok=True)
        # 用便携运行时的 python.exe；没有才退回系统 Python。
        # ⚠️ 千万不能用裸 sys.executable —— exe 环境下它指向安装器自己，
        #    生成的 pypp.bat 会变成"调用安装器"，完全跑不通。
        pyexe = getattr(self, 'runtime_python', None) or self._system_python()
        if not pyexe:
            self.log("  找不到可用的 Python，pypp.bat 无法生成。", 'error')
            return
        # pypp 命令
        entry = os.path.join(self.target, 'src', 'pypp.py')
        content = f'@echo off\n"{pyexe}" "{entry}" %*\n'
        with open(os.path.join(bindir, 'pypp.bat'), 'w',
                  encoding='utf-8', newline='\n') as f:
            f.write(content)
        self.log(f"  已生成 {os.path.join(bindir, 'pypp.bat')}", 'info')
        self.log(f"    → 解释器：{pyexe}", 'info')
        # 双击运行器（pypprun.bat → python runner.py）：
        # bat 只负责拉起，界面/流式输出/暂停逻辑在 src/runner.py。
        from runner import runner_bat_content
        runner_entry = os.path.join(self.target, 'src', 'runner.py')
        with open(os.path.join(bindir, 'pypprun.bat'), 'w',
                  encoding='utf-8', newline='') as f:
            f.write(runner_bat_content(pyexe, runner_entry))
        self.log(f"  已生成 {os.path.join(bindir, 'pypprun.bat')}", 'info')
        # 包管理三兄弟 + 通用入口
        # PYPP_PGET_TOOL 决定各管哪一摊（py / c / cpp / auto）
        pget_entry = os.path.join(self.target, 'pget.py')
        if os.path.isfile(pget_entry):
            tools = [('pget', 'auto'), ('pyget', 'py'),
                     ('cget', 'c'), ('cpget', 'cpp')]
            for name, kind in tools:
                content = (f'@echo off\r\n'
                           f'set PYPP_PGET_TOOL={kind}\r\n'
                           f'"{pyexe}" "{pget_entry}" %*\r\n')
                with open(os.path.join(bindir, name + '.bat'), 'w',
                          encoding='utf-8', newline='') as f:
                    f.write(content)
            self.log(f"  已生成 pget / pyget / cget / cpget 命令", 'info')

        # 文件图标：复制到 bindir（与 pypprun.bat 同目录，注册表
        # DefaultIcon 就指向这里的 cpy.ico）
        for ico in ('cpy.ico', 'pypp.ico'):
            src = os.path.join(HERE, 'assets', ico)
            if os.path.isfile(src):
                shutil.copy2(src, os.path.join(bindir, ico))
                self.log(f"  已复制图标 {ico}", 'info')
            else:
                self.log(f"  安装包内没有 assets/{ico}，跳过图标", 'warn')

    def _step_assoc(self):
        """注册 .cpy 文件关联：双击 .cpy 直接运行，不必打包 exe。

        只写 HKCU（当前用户），不需要管理员权限；失败不阻断安装
        ——关联是便利功能，pypp 命令本身不依赖它。
        """
        try:
            from assoc import register as assoc_register
            r = assoc_register(self.bindir, log=lambda m: self.log(m, 'info'))
            if r.get('registered'):
                self.assoc_status = 'ok'
                self.log("  以后在文件资源管理器里双击任意 .cpy 文件"
                         "即可直接运行", 'ok')
            else:
                self.assoc_status = 'failed'
                self.log(f"  关联未注册：{r.get('skipped') or r.get('error')}",
                         'warn')
        except Exception as e:
            self.assoc_status = 'failed'
            self.log(f"  文件关联注册失败（不影响 py++ 使用）: {e}", 'warn')

    def _step_examples(self):
        n = self._copy_tree(os.path.join(HERE, 'examples'),
                            os.path.join(self.target, 'examples'), '示例')
        self.log(f"  已安装 {n} 个示例程序", 'info')

    def _step_gcc(self):
        dst = os.path.join(self.target, 'tools', 'mingw64')
        if os.path.isfile(os.path.join(dst, 'mingw64', 'bin', 'gcc.exe')):
            self.log("  便携 gcc 已存在，跳过。", 'info')
            self.gcc_status = 'ok'
            return

        if self.gcc_mode == 'bundled':
            if not os.path.isdir(BUNDLED_GCC):
                self.log("  安装包内没有编译器，改用在线下载。", 'warn')
                self.gcc_mode = 'download'
            else:
                self.log("  正在部署包内便携 gcc（约 557MB，可能几分钟）…", 'warn')
                n = self._copy_tree(BUNDLED_GCC,
                                    os.path.join(dst, 'mingw64'), 'gcc 工具链')
                self.log(f"  已部署 {n} 个编译器文件", 'info')
                self.gcc_status = 'ok'
                return

        if self.gcc_mode == 'download':
            try:
                self._download_gcc(dst)
                self.gcc_status = 'ok'
            except Exception as e:
                self.gcc_status = 'failed'
                self.log(f"  下载/解压失败：{type(e).__name__}: {e}", 'error')
                self.log("  py++ 本体已装好，将使用内置 VM 模式。", 'warn')
                self.log("  稍后可重新运行安装程序重试，"
                         "或让 py++ 自动识别系统已有的 gcc。", 'info')
            return

        # gcc_mode == 'none'
        self.log("  按要求跳过 C 编译器。", 'info')
        # 但仍尊重系统里已有的 gcc：装了也没坏处，且能白捡原生模式
        if self._system_gcc():
            self.log(f"  检测到系统已有 gcc：{self._system_gcc()}", 'info')
            self.log("  py++ 会自动使用它，无需额外配置。", 'ok')
            self.gcc_status = 'ok'
        else:
            self.log("  未检测到系统 gcc，将使用内置 VM 模式（较慢）。", 'info')

    @staticmethod
    def _system_gcc():
        """系统 PATH / 常见位置里有没有可用的 gcc。"""
        import shutil as _sh
        import glob as _glob
        for name in ('gcc', 'clang'):
            p = _sh.which(name)
            if p:
                return p
        for pat in (r'C:\mingw64\bin\gcc.exe', r'C:\MinGW\bin\gcc.exe',
                    r'C:\msys64\mingw64\bin\gcc.exe',
                    r'C:\ProgramData\mingw64\bin\gcc.exe'):
            if os.path.isfile(pat):
                return pat
        for pat in (r'C:\Program Files\mingw64\bin\gcc.exe',
                    r'C:\Program Files (x86)\mingw-w64\**\bin\gcc.exe'):
            for hit in _glob.glob(pat, recursive=True):
                if os.path.isfile(hit):
                    return hit
        return None

    def _download_gcc(self, dst):
        """下载并解压便携工具链到 <target>/tools/mingw64/mingw64。

        目标布局与包内自带的一致（…/mingw64/bin/gcc.exe），
        这样 find_toolchain() 不用区分来源。
        """
        import urllib.request
        import zipfile
        import tempfile

        url = self.gcc_url
        self.log(f"  下载源：{url}", 'info')
        self.log("  约 557MB，视网速可能需要几分钟…", 'warn')

        tmp = tempfile.mkdtemp(prefix='pypp_gcc_')
        zip_path = os.path.join(tmp, 'mingw64.zip')
        try:
            # 带进度反馈的下载
            last = {'pct': -1}

            def _hook(blocks, bs, total):
                if not total or self.cancel_flag:
                    return
                pct = int(blocks * bs * 100 / total)
                if pct >= last['pct'] + 5:
                    last['pct'] = pct
                    self.log(f"  下载中 … {pct}%  "
                             f"({blocks * bs // 1048576}MB / "
                             f"{total // 1048576}MB)", 'progress')

            req = urllib.request.Request(
                url, headers={'User-Agent': 'pypp-installer/1.0'})
            with urllib.request.urlopen(req, timeout=60) as resp, \
                    open(zip_path, 'wb') as out:
                total = int(resp.headers.get('Content-Length', 0) or 0)
                blocks = 0
                bs = 65536
                while True:
                    if self.cancel_flag:
                        raise RuntimeError('用户取消')
                    chunk = resp.read(bs)
                    if not chunk:
                        break
                    out.write(chunk)
                    blocks += 1
                    _hook(blocks, bs, total)

            self.log("  下载完成，正在解压…", 'info')
            os.makedirs(os.path.join(dst, 'mingw64'), exist_ok=True)
            root_dir = os.path.join(dst, 'mingw64')

            with zipfile.ZipFile(zip_path) as z:
                names = z.namelist()
                # winlibs 的 zip 顶层是一个同名目录，剥掉它
                tops = {n.split('/')[0] for n in names if '/' in n}
                strip = ''
                if len(tops) == 1:
                    strip = list(tops)[0] + '/'
                count = 0
                for n in names:
                    if n.endswith('/'):
                        continue
                    rel = n[len(strip):] if strip and n.startswith(strip) else n
                    if not rel or '..' in rel:
                        continue
                    target_path = os.path.join(root_dir, rel)
                    os.makedirs(os.path.dirname(target_path), exist_ok=True)
                    with z.open(n) as srcf, open(target_path, 'wb') as dstf:
                        shutil.copyfileobj(srcf, dstf)
                    count += 1
                    if count % 2000 == 0:
                        self.log(f"  已解压 {count} 个文件 …", 'progress')

            gcc_exe = os.path.join(root_dir, 'bin', 'gcc.exe')
            if not os.path.isfile(gcc_exe):
                raise RuntimeError('解压后找不到 bin/gcc.exe，包结构异常')
            self.log(f"  已安装便携 gcc：{gcc_exe}", 'ok')
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _step_path(self):
        if os.name != 'nt':
            self.log(f"  非 Windows 平台，请手动把 {self.home_dir}/bin 加入 PATH", 'warn')
            return
        bindir = os.path.join(self.home_dir, 'bin')
        try:
            import winreg
        except ImportError:
            self.log("  无法访问注册表，请手动配置 PATH", 'warn')
            return
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, 'Environment',
                                 0, winreg.KEY_READ | winreg.KEY_WRITE)
        except OSError as e:
            self.log(f"  打开环境变量注册表失败：{e}", 'warn')
            return
        try:
            try:
                cur, _ = winreg.QueryValueEx(key, 'Path')
            except FileNotFoundError:
                cur = ''
            parts = [p for p in cur.split(';') if p.strip()]
            if bindir.lower() in [p.lower() for p in parts]:
                self.log("  PATH 中已存在，无需重复添加。", 'info')
                return
            parts.append(bindir)
            winreg.SetValueEx(key, 'Path', 0, winreg.REG_EXPAND_SZ,
                              ';'.join(parts))
            self.log(f"  已把 {bindir} 加入用户 PATH（需重开终端生效）", 'ok')
        finally:
            winreg.CloseKey(key)

    def _step_uninstaller(self):
        # uninstall.bat 必须调用「真正的 Python」来解释 uninstall.py。
        # exe 环境下 sys.executable 指向安装器本身，直接用它会把
        # "setup.exe <卸载脚本>" 当成一次安装，导致卸载失效。
        pyexe = getattr(self, 'runtime_python', None) or self._system_python()
        if not pyexe:
            self.log("  找不到可用的 Python，卸载程序可能无法自动运行。", 'warn')
            pyexe = 'python'
        bat = os.path.join(self.target, 'uninstall.bat')
        content = (
            '@echo off\r\n'
            'echo 正在卸载 py++ ...\r\n'
            f'"{pyexe}" "{os.path.join(self.target, "uninstall.py")}"\r\n'
        )
        with open(bat, 'w', encoding='utf-8', newline='') as f:
            f.write(content)

        py = os.path.join(self.target, 'uninstall.py')
        with open(py, 'w', encoding='utf-8', newline='\n') as f:
            f.write(_UNINSTALL_PY % {
                'target': self.target.replace('\\', '\\\\'),
                'home': self.home_dir.replace('\\', '\\\\'),
            })
        self.log("  已写入 uninstall.bat / uninstall.py", 'info')


_UNINSTALL_PY = '''"""py++ 卸载程序（安装时自动生成）"""
import os, sys, shutil

TARGET = r"%(target)s"
BINDIR = r"%(home)s"          # 安装时实际使用的命令目录（非硬编码 ~/.pypp）


def remove_path():
    if os.name != 'nt':
        return
    try:
        import winreg
    except ImportError:
        return
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0,
                             winreg.KEY_READ | winreg.KEY_WRITE)
        try:
            cur, _ = winreg.QueryValueEx(key, "Path")
        except FileNotFoundError:
            cur = ""
        parts = [p for p in cur.split(";") if p.strip()]
        keep = [p for p in parts
                if p.lower() != os.path.join(BINDIR, "bin").lower()]
        if len(keep) != len(parts):
            winreg.SetValueEx(key, "Path", 0, winreg.REG_EXPAND_SZ,
                              ";".join(keep))
            print("已从 PATH 移除 py++")
        winreg.CloseKey(key)
    except Exception as e:
        print("PATH 清理跳过：", e)


def remove_assoc():
    """注销 .cpy 双击运行关联（只删 py++ 自己注册的键，别人的不动）。"""
    if os.name != 'nt':
        return
    try:
        sys.path.insert(0, os.path.join(TARGET, "src"))
        import assoc
        r = assoc.unregister(bindir=os.path.join(BINDIR, "bin"))
        if r.get("unregistered"):
            print("已注销 .cpy 文件关联")
        elif r.get("skipped"):
            print("文件关联清理:", r["skipped"])
        if r.get("error"):
            print("文件关联清理失败:", r["error"])
    except Exception as e:
        print("文件关联清理跳过：", e)


def main():
    print("卸载目录：", TARGET)
    remove_path()
    remove_assoc()
    # 内嵌的便携 Python 运行时（体积不小，必须一起清掉）
    runtime = os.path.join(BINDIR, "runtime")
    if os.path.isdir(runtime):
        shutil.rmtree(runtime, ignore_errors=True)
        print("已删除 Python 运行时：", runtime)
    if os.path.isdir(BINDIR):
        shutil.rmtree(BINDIR, ignore_errors=True)
        print("已删除命令目录：", BINDIR)
    print("")
    print("py++ 已卸载。源文件目录保留在", TARGET)
    print("如需彻底删除，请手动移除该目录。")
    input("按回车退出 ...")


if __name__ == "__main__":
    main()
'''


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------
def build_gui(uicheck=None):
    """构建并运行安装向导。

    uicheck: 可选的布局自检回调，签名 uicheck(ctx)。
             传入时会在 mainloop 之前调用一次，供 --uitest 无头验收。
    """
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox

    # --- 配色：尽量贴近 Python 安装器的蓝白风格 ---
    BG = '#ffffff'
    HEAD = '#306998'          # Python 蓝
    ACCENT = '#ffd43b'        # Python 黄
    TEXT = '#1a1a1a'
    SUBTEXT = '#5a5a5a'
    BORDER = '#d9d9d9'

    root = tk.Tk()
    root.title(f"py++ {VERSION} 安装程序")
    root.configure(bg=BG)

    # ---- 窗口尺寸 ----
    # 旧值 640x460 太小：选项页内容约 560px 高、进度页日志框也要撑开，
    # 460 会把内容裁掉（这正是用户反馈的"窗口过小"）。
    # 改为 760x620，并按屏幕分辨率做自适应上限（小屏笔记本别超出屏幕）。
    WIN_W, WIN_H = 760, 620
    MIN_W, MIN_H = 700, 560
    sw = root.winfo_screenwidth()
    sh = root.winfo_screenheight()
    w = min(WIN_W, max(MIN_W, sw - 120))
    h = min(WIN_H, max(MIN_H, sh - 160))
    x = max(0, (sw - w) // 2)
    y = max(0, (sh - h) // 3)          # 略偏上，视觉更像向导
    root.geometry(f"{w}x{h}+{x}+{y}")
    root.minsize(MIN_W, MIN_H)
    # 允许调整大小：不同 DPI / 字号下内容高度不一样，硬锁尺寸必然有人被裁
    root.resizable(True, True)
    try:
        root.iconbitmap(default='')
    except Exception:
        pass

    style = ttk.Style(root)
    try:
        style.theme_use('vista' if os.name == 'nt' else 'clam')
    except Exception:
        pass
    style.configure('TButton', font=('Microsoft YaHei UI', 10))
    style.configure('TLabel', background=BG, foreground=TEXT,
                    font=('Microsoft YaHei UI', 10))
    style.configure('TCheckbutton', background=BG, foreground=TEXT,
                    font=('Microsoft YaHei UI', 10))
    style.configure('TRadiobutton', background=BG, foreground=TEXT,
                    font=('Microsoft YaHei UI', 10))

    state = {
        'page': 0,
        'cancelled': False,
        'done': False,
    }

    # 顶部标题带
    header = tk.Frame(root, bg=HEAD, height=76)
    header.pack(fill='x')
    header.pack_propagate(False)
    tk.Label(header, text="py++", bg=HEAD, fg='white',
             font=('Microsoft YaHei UI', 22, 'bold')).pack(
        side='left', padx=(24, 10))
    tk.Label(header, text=f"{VERSION}\n像 Python 一样简洁，像 C++ 一样快",
             bg=HEAD, fg='#dfeaf5', justify='left',
             font=('Microsoft YaHei UI', 9)).pack(side='left', pady=8)

    body = tk.Frame(root, bg=BG)          # 各页面的容器（内容区可滚动）
    body.pack(fill='both', expand=True, padx=24, pady=(18, 0))

    # ------------------------------------------------------------------
    # 描述性文本（多行说明文字）在内容超长时应可滚动查看
    # ------------------------------------------------------------------
    def _scrollable(parent):
        """把可滚动内容挂到 parent 上，返回内层 Frame。

        注意：outer 用 fill=both/expand 铺满 parent，否则外层尺寸会是 1x1
        （踩过：canvas 里的内容不会反过来撑大外层 Frame）。
        """
        outer = tk.Frame(parent, bg=BG)
        canvas = tk.Canvas(outer, bg=BG, highlightthickness=0, bd=0)
        vsb = ttk.Scrollbar(outer, orient='vertical', command=canvas.yview)
        inner = tk.Frame(canvas, bg=BG)
        win = canvas.create_window((0, 0), window=inner, anchor='nw')

        def _on_inner(e):
            canvas.configure(scrollregion=canvas.bbox('all'))

        def _on_canvas(e):
            canvas.itemconfigure(win, width=e.width)

        def _on_wheel(e):
            # 内容没超出时可滚动区域为 0，此时忽略滚轮，避免"卡住"错觉
            box = canvas.bbox('all')
            if box and box[3] > canvas.winfo_height():
                canvas.yview_scroll(int(-1 * (e.delta / 120)), 'units')

        inner.bind('<Configure>', _on_inner)
        canvas.bind('<Configure>', _on_canvas)
        canvas.bind('<MouseWheel>', _on_wheel)
        inner.bind('<MouseWheel>', _on_wheel)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side='right', fill='y')
        canvas.pack(side='left', fill='both', expand=True)
        outer.pack(fill='both', expand=True)     # ★ 关键：铺满父容器
        return inner

    # ------------------------------------------------------------------
    # 第 1 页：欢迎
    # ------------------------------------------------------------------
    page_welcome = tk.Frame(body, bg=BG)
    tk.Label(page_welcome, text="欢迎安装 py++", bg=BG, fg=HEAD,
             font=('Microsoft YaHei UI', 15, 'bold')).pack(anchor='w')
    tk.Label(page_welcome, justify='left', bg=BG, fg=SUBTEXT,
             font=('Microsoft YaHei UI', 10),
             text=("这个向导将把 py++ 编译器安装到你的电脑上。\n\n"
                   "py++ 是一门新语言：Python 的简洁语法，接近 C++ 的运行速度，\n"
                   "并且代码有小错误也能自动修复并运行。\n\n"
                   "源文件扩展名是 .cpy\n\n"
                   "点击「下一步」继续。")).pack(anchor='w', pady=(14, 0))

    # ------------------------------------------------------------------
    # 第 2 页：选项（内容较长，放进可滚动容器，小屏也不丢内容）
    # ------------------------------------------------------------------
    page_options_wrap = tk.Frame(body, bg=BG)
    page_options = _scrollable(page_options_wrap)
    tk.Label(page_options, text="安装选项", bg=BG, fg=HEAD,
             font=('Microsoft YaHei UI', 15, 'bold')).pack(anchor='w')

    # 路径
    path_row = tk.Frame(page_options, bg=BG)
    path_row.pack(fill='x', pady=(16, 6))
    tk.Label(path_row, text="安装位置：", bg=BG).pack(side='left')
    default_dir = os.path.join(os.path.expanduser('~'), 'py++')
    path_var = tk.StringVar(value=default_dir)
    path_entry = ttk.Entry(path_row, textvariable=path_var, width=44)
    path_entry.pack(side='left', padx=(0, 6))

    def choose_dir():
        d = filedialog.askdirectory(title="选择 py++ 安装目录",
                                    initialdir=path_var.get())
        if d:
            path_var.set(os.path.join(d, 'py++') if
                         os.path.basename(d) != 'py++' else d)
    ttk.Button(path_row, text="浏览…", command=choose_dir).pack(side='left')

    # 选项组
    opt_box = tk.Frame(page_options, bg=BG, highlightbackground=BORDER,
                       highlightthickness=1)
    opt_box.pack(fill='x', pady=(14, 0))

    tk.Label(opt_box, text="要安装哪些组件？", bg=BG, fg=TEXT,
             font=('Microsoft YaHei UI', 10, 'bold')).pack(
        anchor='w', padx=14, pady=(12, 6))

    examples_var = tk.BooleanVar(value=True)
    path2_var = tk.BooleanVar(value=True)
    # .cpy 文件关联（双击运行）—— 仅 Windows 有效，默认勾选
    assoc_var = tk.BooleanVar(value=True)
    # C 编译器：多选（两个来源都不勾 = 不要编译器）
    gcc_download_var = tk.BooleanVar(value=True)
    gcc_bundled_var = tk.BooleanVar(value=False)

    # 包内是否带 557MB 的便携 gcc —— 单文件 exe 通常不带
    _has_bundled = os.path.isdir(BUNDLED_GCC)

    tk.Label(opt_box, text="C 编译器（决定运行速度）", bg=BG, fg=TEXT,
             font=('Microsoft YaHei UI', 10, 'bold')).pack(
        anchor='w', padx=14, pady=(12, 4))

    # --- 说明文字只在对应选项被勾选时显示 ---
    # 这样初始界面干净；勾选后立刻能看到"这一项到底装什么、多大、要不要联网"。
    def _make_desc(parent, text):
        """创建一条说明标签，但先不 pack（等勾选时再显示）。"""
        return tk.Label(parent, bg=BG, fg=SUBTEXT, justify='left',
                        wraplength=620, font=('Microsoft YaHei UI', 9),
                        text=text)

    def _sync_desc(var, labels):
        """按勾选状态显隐说明文字；labels 里允许含 None（跳过）。"""
        on = bool(var.get())
        for lb in labels:
            if lb is None:
                continue
            if on:
                lb.pack(anchor='w', padx=38, pady=(0, 8))
            else:
                lb.pack_forget()

    # 来源 1：在线下载
    tk.Checkbutton(opt_box, variable=gcc_download_var, bg=BG,
                   text="在线下载 C 编译器（推荐 · 原生高速）",
                   command=lambda: _sync_desc(gcc_download_var,
                                              [desc_dl]),
                   font=('Microsoft YaHei UI', 10),
                   activebackground=BG).pack(anchor='w', padx=14)
    desc_dl = _make_desc(
        opt_box,
        "    安装时从网络下载约 557MB 的 MinGW-W64 工具链，"
        "py++ 将以原生模式运行，速度接近 C++。\n"
        "    需要联网。两个来源都不勾选 = 不装编译器，"
        "用内置 VM 模式（功能完整，但速度慢很多）。")

    # 来源 2：包内自带
    cb_bundled = tk.Checkbutton(opt_box, variable=gcc_bundled_var, bg=BG,
                                text="使用安装包自带的编译器",
                                command=lambda: _sync_desc(gcc_bundled_var,
                                                           [desc_bd]),
                                font=('Microsoft YaHei UI', 10),
                                activebackground=BG)
    cb_bundled.pack(anchor='w', padx=14)
    if _has_bundled:
        desc_bd = _make_desc(
            opt_box,
            "    本安装包已内置便携工具链，直接部署，无需联网。"
            "体积约 557MB。\n"
            "    若同时勾选了「在线下载」，将优先使用包内自带的。")
    else:
        # 禁用项：不创建说明标签，也永不显示
        cb_bundled.configure(state='disabled')
        gcc_bundled_var.set(False)
        desc_bd = None

    # --- 示例程序 ---
    tk.Checkbutton(opt_box, variable=examples_var, bg=BG,
                   text="示例程序 (.cpy)",
                   command=lambda: _sync_desc(examples_var, [desc_ex]),
                   font=('Microsoft YaHei UI', 10, 'bold'),
                   activebackground=BG).pack(anchor='w', padx=14,
                                             pady=(6, 0))
    desc_ex = _make_desc(
        opt_box, "    包含 hello、容错演示、性能展示等示例。")

    # --- 加入 PATH ---
    tk.Checkbutton(opt_box, variable=path2_var, bg=BG,
                   text="把 pypp 命令加入 PATH",
                   command=lambda: _sync_desc(path2_var, [desc_path]),
                   font=('Microsoft YaHei UI', 10, 'bold'),
                   activebackground=BG).pack(anchor='w', padx=14)
    desc_path = _make_desc(
        opt_box, "    勾选后可在任意终端直接输入 pypp 运行程序。")

    # --- 双击运行 .cpy ---
    tk.Checkbutton(opt_box, variable=assoc_var, bg=BG,
                   text="双击运行 .cpy 文件（注册文件关联）",
                   command=lambda: _sync_desc(assoc_var, [desc_assoc]),
                   font=('Microsoft YaHei UI', 10, 'bold'),
                   activebackground=BG).pack(anchor='w', padx=14)
    desc_assoc = _make_desc(
        opt_box,
        "    勾选后，在文件资源管理器里双击任意 .cpy 文件就会直接运行，\n"
        "    不必先打包成 exe。运行结束后窗口会停住，方便查看输出。\n"
        "    只写入当前用户，不需要管理员权限；卸载时自动清理。")

    # 初始渲染：按各变量的默认值决定说明是否可见
    # （desc_bd 为 None 表示该选项被禁用，位置保留但永不显示说明）
    for _v, _l in ((gcc_download_var, [desc_dl]),
                   (gcc_bundled_var, [desc_bd]),
                   (examples_var, [desc_ex]),
                   (path2_var, [desc_path]),
                   (assoc_var, [desc_assoc])):
        _sync_desc(_v, _l)
    # 底部留白
    tk.Frame(opt_box, bg=BG, height=12).pack()

    # ------------------------------------------------------------------
    # 第 3 页：进度
    # ------------------------------------------------------------------
    page_progress = tk.Frame(body, bg=BG)
    tk.Label(page_progress, text="正在安装 py++", bg=BG, fg=HEAD,
             font=('Microsoft YaHei UI', 15, 'bold')).pack(anchor='w')
    tk.Label(page_progress, bg=BG, fg=SUBTEXT,
             text="请稍候，正在把文件复制到你的电脑上。").pack(anchor='w',
                                                          pady=(8, 12))
    # 进度条自适应宽度（旧值固定 560，是为 640 宽的窗口设的）
    bar = ttk.Progressbar(page_progress, mode='indeterminate')
    bar.pack(anchor='w', fill='x')

    log_frame = tk.Frame(page_progress, bg='#f7f7f7',
                         highlightbackground=BORDER, highlightthickness=1)
    log_frame.pack(fill='both', expand=True, pady=(12, 0))
    log_text = tk.Text(log_frame, height=14, wrap='word', bd=0,
                       bg='#f7f7f7', fg=TEXT, font=('Consolas', 9),
                       state='disabled')
    log_scroll = ttk.Scrollbar(log_frame, command=log_text.yview)
    log_text.configure(yscrollcommand=log_scroll.set)
    log_scroll.pack(side='right', fill='y')
    log_text.pack(side='left', fill='both', expand=True, padx=6, pady=6)

    COLORS = {'info': TEXT, 'ok': '#1a7f37', 'warn': '#9a6700',
              'error': '#b42318', 'step': HEAD, 'progress': SUBTEXT,
              'debug': '#8a8a8a'}

    def append_log(msg, level='info'):
        def _do():
            log_text.configure(state='normal')
            log_text.insert('end', msg + '\n', level)
            for tag, col in COLORS.items():
                log_text.tag_configure(tag, foreground=col)
            log_text.tag_configure('step',
                                   font=('Consolas', 9, 'bold'))
            log_text.see('end')
            log_text.configure(state='disabled')
        root.after(0, _do)

    # ------------------------------------------------------------------
    # 第 4 页：完成
    # ------------------------------------------------------------------
    page_done = tk.Frame(body, bg=BG)
    done_title = tk.Label(page_done, text="安装成功！", bg=BG, fg=HEAD,
                          font=('Microsoft YaHei UI', 15, 'bold'))
    done_title.pack(anchor='w')
    done_detail = tk.Label(page_done, justify='left', bg=BG, fg=TEXT,
                           font=('Microsoft YaHei UI', 10), text="")
    done_detail.pack(anchor='w', pady=(14, 0))

    # ------------------------------------------------------------------
    # 页面切换
    # ------------------------------------------------------------------
    pages = [page_welcome, page_options_wrap, page_progress, page_done]

    def show(idx):
        for p in pages:
            p.pack_forget()
        pages[idx].pack(fill='both', expand=True)
        state['page'] = idx
        # 按钮状态
        back_btn.configure(state='normal' if idx in (1,) else 'disabled')
        if idx == 0:
            next_btn.configure(text="下一步 >")
        elif idx == 1:
            next_btn.configure(text="安装")
        elif idx == 2:
            next_btn.configure(text="请稍候", state='disabled')
        else:
            next_btn.configure(text="完成", state='normal')
            back_btn.configure(state='disabled')
            cancel_btn.configure(state='disabled')

    # ------------------------------------------------------------------
    # 安装线程
    # ------------------------------------------------------------------
    install_ok = {'value': False}

    def start_install():
        target = path_var.get().strip()
        if not target:
            messagebox.showwarning("提示", "请先选择安装位置。")
            return
        if os.path.exists(target) and os.listdir(target):
            if not messagebox.askyesno(
                    "目录非空",
                    f"目录已存在且非空：\n{target}\n\n是否继续安装到这里？"):
                return
        show(2)
        bar.start(12)

        # 两个勾选 → gcc_mode（包内优先；都没勾 = 不要编译器）
        if gcc_bundled_var.get() and _has_bundled:
            _mode = 'bundled'
        elif gcc_download_var.get():
            _mode = 'download'
        else:
            _mode = 'none'

        inst = Installer(
            target_dir=target,
            gcc_mode=_mode,
            add_path=path2_var.get(),
            install_examples=examples_var.get(),
            assoc=assoc_var.get(),
            log=append_log,
        )
        def worker():
            ok = inst.run()
            install_ok['value'] = ok

            def finish():
                bar.stop()
                if ok:
                    launcher = os.path.join(PYPP_HOME, 'bin', 'pypp.bat')
                    lines = [
                        f"py++ 已安装到：{target}",
                        "",
                        "使用方法：",
                    ]
                    if path2_var.get():
                        lines.append("  在终端运行：  pypp 你的程序.cpy")
                        lines.append(f"  （PATH 已更新，请重新打开终端后生效）")
                    else:
                        lines.append(f"  在终端运行：  \"{launcher}\" 你的程序.cpy")
                    lines.append("")
                    if getattr(inst, 'assoc_status', '') == 'ok':
                        lines.append("· 双击任意 .cpy 文件即可直接运行"
                                     "（已注册文件关联）")

                    if inst.gcc_status == 'ok':
                        lines.append("已启用原生高速模式（速度接近 C++）。")
                    elif _mode == 'none':
                        lines.append("未安装 C 编译器，将使用内置 VM 模式"
                                     "（较慢，但程序照常运行）。")
                    elif _mode == 'download':
                        lines.append("⚠ 在线下载 C 编译器未成功，"
                                     "目前将使用内置 VM 模式。")
                        lines.append("  可重新运行本安装程序再试，"
                                     "或手动把 gcc 放进 PATH。")
                    else:
                        lines.append("⚠ 部署便携编译器未成功，"
                                     "目前将使用内置 VM 模式。")

                    # 只有真的装了示例才提示试跑
                    sample = os.path.join(target, 'examples', 'tolerant.cpy')
                    if examples_var.get() and os.path.isfile(sample):
                        lines += ["", "想看看容错效果？运行："]
                        if path2_var.get():
                            lines.append(f'  pypp "{sample}"')
                        else:
                            lines.append(f'  "{launcher}" "{sample}"')
                    if getattr(inst, 'runtime_status', '') == 'ok':
                        lines += ["",
                                  "已自带 Python 运行时，无需另装 Python。"]
                    elif getattr(inst, 'runtime_status', '') == 'system':
                        lines += ["",
                                  "使用系统已有的 Python 运行 py++。"]
                    # 快速精通指南（装到根目录的，优先指它）
                    qs = os.path.join(target, '快速精通.md')
                    if os.path.isfile(qs):
                        lines += ["", f"从零到精通：{qs}"]
                    lines += ["", f"卸载：双击 {os.path.join(target, 'uninstall.bat')}"]
                    done_detail.configure(text="\n".join(lines))
                    show(3)
                else:
                    messagebox.showerror(
                        "安装失败",
                        "安装过程中出现错误，详情见日志。\n"
                        "你可以关闭窗口后重试。")
                    next_btn.configure(text="重试", state='normal')
                    cancel_btn.configure(state='normal')
            root.after(0, finish)

        threading.Thread(target=worker, daemon=True).start()

    def on_next():
        idx = state['page']
        if idx == 0:
            show(1)
        elif idx == 1:
            start_install()
        elif idx == 3:
            root.destroy()

    def on_back():
        if state['page'] == 1:
            show(0)

    def on_cancel():
        if state['page'] == 2:
            if messagebox.askyesno("取消安装", "确定要取消安装吗？"):
                state['cancelled'] = True
                try:
                    bar.stop()
                except Exception:
                    pass
                root.destroy()
        else:
            root.destroy()

    # ------------------------------------------------------------------
    # 底部按钮条
    # ------------------------------------------------------------------
    footer = tk.Frame(root, bg=BG)
    footer.pack(fill='x', padx=24, pady=16)
    ttk.Separator(footer, orient='horizontal').pack(fill='x', pady=(0, 12))

    btn_row = tk.Frame(footer, bg=BG)
    btn_row.pack(fill='x')

    next_btn = ttk.Button(btn_row, text="下一步 >", command=on_next, width=12)
    next_btn.pack(side='right')
    cancel_btn = ttk.Button(btn_row, text="取消", command=on_cancel, width=10)
    cancel_btn.pack(side='right', padx=(0, 8))
    back_btn = ttk.Button(btn_row, text="< 上一步", command=on_back,
                          width=12, state='disabled')
    back_btn.pack(side='right', padx=(0, 8))

    root.protocol("WM_DELETE_WINDOW", on_cancel)
    show(0)

    # 布局自检（仅供 --uitest 使用，正常运行时为 None）
    if uicheck is not None:
        root.update_idletasks()
        uicheck({
            'root': root, 'show': show, 'pages': pages,
            'body': body, 'win_w': w, 'win_h': h,
            'min_w': MIN_W, 'min_h': MIN_H,
        })
        root.destroy()
        return 0

    root.mainloop()
    return 0


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def _find_tk_python():
    """找一个带 tkinter 的 Python 解释器路径。"""
    candidates = [
        sys.executable,
        r"C:\Users\%s\AppData\Local\Programs\Python\Python313\python.exe"
        % os.environ.get('USERNAME', ''),
        r"C:\Users\%s\AppData\Local\Programs\Python\Python312\python.exe"
        % os.environ.get('USERNAME', ''),
        r"C:\Users\%s\AppData\Local\Programs\Python\Python311\python.exe"
        % os.environ.get('USERNAME', ''),
        shutil.which('python'),
        shutil.which('python3'),
    ]
    for c in candidates:
        if c and os.path.isfile(c):
            r = subprocess.run([c, '-c', 'import tkinter'],
                               capture_output=True)
            if r.returncode == 0:
                return c
    return None


def _autotest():
    """无界面自检：在临时目录跑一遍安装，验证产物齐全后清理。

    用法：python install_gui.py --autotest          （跳过 gcc 大复制）
          python install_gui.py --autotest --full   （连 gcc 一起装）
          python install_gui.py --autotest --log <文件>   （结果写文件）

    不会改动用户 PATH，也不污染真实 ~/.pypp。

    ⚠️ 打包成 exe 后是 --windowed（无控制台），stdout 拿不到。
       所以额外支持 --log：把自检结果写进文件，供外部脚本读取。
    """
    import tempfile
    full = '--full' in sys.argv
    log_file = None
    if '--log' in sys.argv:
        i = sys.argv.index('--log')
        if i + 1 < len(sys.argv):
            log_file = sys.argv[i + 1]

    tmp = tempfile.mkdtemp(prefix='pypp_autotest_')
    target = os.path.join(tmp, 'py++')
    lines = []

    def out(msg=''):
        lines.append(str(msg))
        try:
            print(msg)
        except Exception:
            pass

    inst = Installer(
        target_dir=target,
        gcc_mode='bundled' if full else 'none',
        add_path=False,          # 关键：不碰真实 PATH
        install_examples=True,
        assoc=False,             # 关键：不往真实注册表写 .cpy 关联
        home_dir=os.path.join(tmp, '_home'),   # 关键：不污染真实 ~/.pypp
        log=lambda m, lv='info': out(f"[{lv}] {m}"),
    )
    out(f"py++ GUI 安装器自检  (目标 {target})")
    ok = inst.run()

    expect = ['src/pypp.py', 'src/lexer.py', 'src/codegen.py',
              'examples/hello.cpy', 'uninstall.py', 'uninstall.bat']
    missing = [p for p in expect
               if not os.path.isfile(os.path.join(target, p))]
    bindir = os.path.join(tmp, '_home', 'bin')
    if not os.path.isfile(os.path.join(bindir, 'pypp.bat')):
        missing.append('_home/bin/pypp.bat')
    out("")
    out("产物检查：" + ("全部齐全" if not missing else f"缺失 {missing}"))

    # 便携运行时是否就位
    rt = getattr(inst, 'runtime_python', None)
    out(f"运行时状态：{getattr(inst, 'runtime_status', '?')}  →  {rt}")
    if not rt or not os.path.isfile(rt):
        missing.append('runtime/python.exe')

    # 用装好的解释器试跑编译器（而不是 sys.executable —— exe 下那是安装器自己）
    run_ok = False
    if not missing and rt:
        try:
            r = subprocess.run(
                [rt, os.path.join(target, 'src', 'pypp.py'),
                 os.path.join(target, 'examples', 'hello.cpy'), '--no-color'],
                capture_output=True, text=True, timeout=180,
                encoding='utf-8', errors='replace')
            run_ok = r.returncode == 0 and 'hello' in (r.stdout or '').lower()
            out("试跑安装后的编译器：" + ("通过" if run_ok else "失败"))
            out((r.stdout or '').strip()[:300])
            if not run_ok:
                out("stderr: " + (r.stderr or '')[:300])
        except Exception as e:
            out(f"试跑失败：{type(e).__name__}: {e}")

    # 校验生成的 pypp.bat 指向的是真解释器，不是安装器自己
    bat_ok = False
    bat_path = os.path.join(bindir, 'pypp.bat')
    if os.path.isfile(bat_path):
        with open(bat_path, encoding='utf-8') as f:
            content = f.read()
        bat_ok = ('python' in content.lower() and
                  'setup.exe' not in content.lower())
        out("pypp.bat 内容检查：" + ("通过" if bat_ok else "★异常"))
        out("  " + content.strip().replace('\n', ' | '))
    else:
        out("★ 未生成 pypp.bat")

    shutil.rmtree(tmp, ignore_errors=True)
    okall = ok and not missing and run_ok and bat_ok
    out("")
    out("自检结果：" + ("PASS" if okall else "FAIL"))

    if log_file:
        try:
            with open(log_file, 'w', encoding='utf-8') as f:
                f.write('\n'.join(lines))
        except Exception:
            pass
    return 0 if okall else 1


def _uitest():
    """布局自检：真实建窗、逐页检查内容是否被裁掉。

    用法：python install_gui.py --uitest
    不需要人盯着看 —— 会把每页的"实际所需尺寸"打印出来并断言不溢出。
    这是为了守住「窗口过小、内容被裁」这个已经犯过一次的错。
    """
    result = {'ok': False}

    def _find_canvas(w):
        """在子树里找可滚动用的 Canvas。"""
        for c in w.winfo_children():
            if c.winfo_class() == 'Canvas':
                return c
            hit = _find_canvas(c)
            if hit:
                return hit
        return None

    def _deepest_text(w):
        """在子树里找 Text 控件（进度页日志框）。"""
        if w.winfo_class() == 'Text':
            return w
        for c in w.winfo_children():
            hit = _deepest_text(c)
            if hit:
                return hit
        return None

    def check(ctx):
        root = ctx['root']
        show = ctx['show']
        pages = ctx['pages']
        win_w, win_h = ctx['win_w'], ctx['win_h']
        # 选项页被包在可滚动容器里，其直接子控件是外层 Frame
        names = ['欢迎页', '选项页', '进度页', '完成页']
        print(f"窗口尺寸：{win_w}x{win_h}  (最小值 "
              f"{ctx['min_w']}x{ctx['min_h']})")
        print(f"屏幕尺寸：{root.winfo_screenwidth()}x"
              f"{root.winfo_screenheight()}")
        print("")
        print(f"{'页面':<8}{'需要宽':>8}{'需要高':>8}"
              f"{'可用宽':>8}{'可用高':>8}   判定")
        print("-" * 52)

        all_ok = True
        for i, name in enumerate(names):
            show(i)
            root.update_idletasks()
            root.update()
            p = pages[i]
            avail_w, avail_h = p.winfo_width(), p.winfo_height()

            # 内容真实尺寸：可滚动页要看 canvas 记录的 scrollregion
            need_w = need_h = 0
            canvas = _find_canvas(p)
            if canvas is not None:
                region = canvas.cget('scrollregion')
                if region:
                    x0, y0, x1, y1 = [int(float(v)) for v in region.split()]
                    need_w, need_h = x1 - x0, y1 - y0
                if need_h == 0:
                    bbox = canvas.bbox('all')
                    if bbox:
                        need_w, need_h = bbox[2] - bbox[0], bbox[3] - bbox[1]
            else:
                need_w, need_h = p.winfo_reqwidth(), p.winfo_reqheight()

            # 判定：需要 ≤ 可用 就 OK；可滚动页即使超出也不算丢内容
            scrollable = canvas is not None
            if need_h <= avail_h or scrollable:
                verdict = 'OK' + ('（可滚动）' if (scrollable and
                                                 need_h > avail_h) else '')
            else:
                verdict = '★内容被裁★'
                all_ok = False
            print(f"{name:<8}{need_w:>8}{need_h:>8}"
                  f"{avail_w:>8}{avail_h:>8}   {verdict}")

        # --- 断言 1：选项页的全部关键控件都在滚动区域内 ---
        show(1)
        root.update_idletasks()
        root.update()
        opt_outer = pages[1]
        canvas = _find_canvas(opt_outer)
        inner = None
        if canvas is not None:
            ids = canvas.find_all()
            if ids:
                win_id = canvas.itemcget(ids[0], 'window')
                if win_id:
                    inner = root.nametowidget(win_id)
        print("")
        if inner is None:
            print("★ 选项页：找不到滚动内容区")
            all_ok = False
        else:
            n_all = _count_all(inner)
            checks = sum(1 for w in _all_widgets(inner)
                         if w.winfo_class() == 'Checkbutton')
            print(f"选项页：滚动区内共 {n_all} 个控件"
                  f"（含 {checks} 个 Checkbutton）")
            if checks < 3:
                print(f"★ 选项页：Checkbutton 只有 {checks} 个，应为 3 个")
                all_ok = False

        # --- 断言 2：进度页日志框必须撑得开 ---
        show(2)
        root.update_idletasks()
        root.update()
        log_text = _deepest_text(pages[2])
        log_h = log_text.winfo_height() if log_text else 0
        if log_h < 150:
            print(f"★ 进度页日志框过矮（{log_h}px，应 >=150px）")
            all_ok = False
        else:
            print(f"进度页日志框高度：{log_h}px")

        # --- 断言 3：底部按钮不能被挤出窗口 ---
        root.update_idletasks()
        hdr_h = root.winfo_height()
        print(f"窗口实际高度：{hdr_h}px")
        if hdr_h < ctx['min_h']:
            print(f"★ 窗口高度小于最小值 {ctx['min_h']}px")
            all_ok = False

        result['ok'] = all_ok

    build_gui(uicheck=check)
    print("")
    print("UI 自检结果：", "PASS" if result['ok'] else "FAIL")
    return 0 if result['ok'] else 1


def _all_widgets(w):
    """递归列出所有子孙控件。"""
    out = []
    for c in w.winfo_children():
        out.append(c)
        out.extend(_all_widgets(c))
    return out


def _count_all(w):
    return len(_all_widgets(w))


def main():
    # 隐藏的自检入口：无界面跑一遍安装引擎，用于 CI / 回归测试
    if '--autotest' in sys.argv:
        return _autotest()
    # 布局自检：真实建窗检查控件是否被裁
    if '--uitest' in sys.argv:
        ok, err = _tk_available()
        if not ok:
            alt = _find_tk_python()
            if alt and os.path.abspath(alt) != os.path.abspath(sys.executable):
                print(f"当前解释器缺少 tkinter，改用：{alt}")
                return subprocess.call([alt, os.path.abspath(__file__),
                                        '--uitest'])
            print("无法进行 UI 自检：没有 tkinter。")
            return 1
        return _uitest()

    ok, err = _tk_available()
    if not ok:
        # 当前解释器没有 tkinter，尝试自动切换到系统 Python 重跑
        alt = _find_tk_python()
        if alt and os.path.abspath(alt) != os.path.abspath(sys.executable):
            print(f"当前解释器缺少 tkinter，改用：{alt}")
            return subprocess.call([alt, os.path.abspath(__file__)] + sys.argv[1:])
        print("=" * 60)
        print("无法启动图形安装器：当前 Python 没有 tkinter 模块。")
        print(f"错误信息：{err}")
        print("")
        print("解决办法（任选其一）：")
        print("  1. 使用带 tkinter 的官方 Python 运行：")
        print("     python install_gui.py")
        print("  2. 使用命令行安装器（功能完全一致）：")
        print("     python install.py")
        print("=" * 60)
        return 1
    return build_gui()


if __name__ == '__main__':
    sys.exit(main())
