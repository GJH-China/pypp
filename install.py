"""
py++ 安装器 (install.py)
跨平台安装，支持三种模式：
  python install.py            → 安装 py++ 命令到本机
  python install.py --check    → 仅检测环境
  python install.py --json     → 输出机器可读 JSON（供网站调用）
  python install.py --uninstall→ 卸载

网站友好：所有输出都可切换为纯 JSON，便于网页端解析并展示。
"""

import os
import sys
import json
import platform
import shutil
import subprocess
import argparse
import stat

# 保证能找到同级模块
HERE = os.path.dirname(os.path.abspath(__file__))
SRC_DIR = os.path.join(HERE, 'src')
sys.path.insert(0, SRC_DIR)

VERSION = "1.1.0"

# ANSI
G = '\033[32m'; Y = '\033[33m'; R = '\033[31m'; C = '\033[36m'
B = '\033[1m'; N = '\033[0m'


def detect_environment():
    """探测运行环境，返回结构化字典。网站可直接消费。"""
    info = {
        'pypp_version': VERSION,
        'python': {
            'version': platform.python_version(),
            'executable': sys.executable,
            'ok': sys.version_info >= (3, 8),
        },
        'platform': {
            'system': platform.system(),
            'release': platform.release(),
            'machine': platform.machine(),
            'is_windows': os.name == 'nt',
        },
        'compiler': {
            'found': False,
            'path': None,
            'name': None,
            'version': None,
            'note': '未找到 C 编译器，将使用内置 VM 引擎（速度较慢）',
        },
        'engine': 'vm',
        'ready': False,
    }

    # 探测 gcc/clang
    from pypp import find_gcc
    gcc = find_gcc()
    if gcc:
        info['compiler']['found'] = True
        info['compiler']['path'] = gcc
        info['compiler']['name'] = os.path.basename(gcc)
        try:
            r = subprocess.run([gcc, '--version'], capture_output=True,
                               text=True, timeout=10)
            info['compiler']['version'] = r.stdout.split('\n')[0].strip()
        except Exception:
            pass
        info['compiler']['note'] = '已找到 C 编译器，可使用原生高速模式'
        info['engine'] = 'native'

    info['ready'] = info['python']['ok']
    return info


def install_command(json_mode=False, use_assoc=False):
    """把 pypp 命令安装到用户可执行目录。

    use_assoc 默认 False：注册文件关联会写真实注册表（HKCU），
    必须由调用方显式决定（main() 按 --no-assoc 取反后传入）。
    与 Installer(assoc=...) 同一约定：碰用户环境的功能一律 opt-in。
    （踩过的坑：默认 True 时测试套件跑一遍就把真实 .cpy 关联
    改写指向临时 home，用户双击弹"打开方式"且循环复现。）"""
    result = {'action': 'install', 'success': False, 'messages': [], 'launcher': None}

    def log(msg):
        result['messages'].append(msg)
        if not json_mode:
            print(msg)

    # 生成启动脚本
    if os.name == 'nt':
        bindir = os.path.join(os.path.expanduser('~'), '.pypp', 'bin')
        os.makedirs(bindir, exist_ok=True)
        launcher = os.path.join(bindir, 'pypp.bat')
        pyexe = sys.executable
        entries = [
            '@echo off',
            f'"{pyexe}" "{os.path.join(SRC_DIR, "pypp.py")}" %*',
        ]
        content = '\n'.join(entries) + '\n'
    else:
        bindir = os.path.join(os.path.expanduser('~'), '.pypp', 'bin')
        os.makedirs(bindir, exist_ok=True)
        launcher = os.path.join(bindir, 'pypp')
        pyexe = sys.executable
        content = f'#!/bin/sh\nexec "{pyexe}" "{os.path.join(SRC_DIR, "pypp.py")}" "$@"\n'

    with open(launcher, 'w', encoding='utf-8', newline='\n') as f:
        f.write(content)
    if os.name != 'nt':
        os.chmod(launcher, os.stat(launcher).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    result['launcher'] = launcher
    log(f"{G}✓{N} 已生成命令: {launcher}")

    # pget 命令（包管理器，对标 pip）
    pget_py = os.path.join(os.path.dirname(SRC_DIR), 'pget.py')
    if os.path.isfile(pget_py):
        if os.name == 'nt':
            pget_launcher = os.path.join(bindir, 'pget.bat')
            pget_content = (f'@echo off\n"{pyexe}" "{pget_py}" %*\n')
        else:
            pget_launcher = os.path.join(bindir, 'pget')
            pget_content = f'#!/bin/sh\nexec "{pyexe}" "{pget_py}" "$@"\n'
        with open(pget_launcher, 'w', encoding='utf-8', newline='\n') as f:
            f.write(pget_content)
        if os.name != 'nt':
            os.chmod(pget_launcher,
                     os.stat(pget_launcher).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        result['pget_launcher'] = pget_launcher
        log(f"{G}✓{N} 已生成命令: {pget_launcher}")

    # 双击运行器（pypprun.bat → python runner.py）：
    # 装在 bindir 里，由 assoc 注册进 .cpy 的 open 命令。
    # bat 只负责"拉起"，界面/流式输出/暂停逻辑都在 src/runner.py。
    if os.name == 'nt':
        from runner import runner_bat_content
        runner_bat = os.path.join(bindir, 'pypprun.bat')
        runner_entry = os.path.join(SRC_DIR, 'runner.py')
        with open(runner_bat, 'w', encoding='utf-8', newline='') as f:
            f.write(runner_bat_content(pyexe, runner_entry))
        result['runner_launcher'] = runner_bat
        log(f"{G}✓{N} 已生成双击运行器: {runner_bat}")

    # .cpy 文件图标（build_icons.py 生成）：复制到 bindir，
    # 注册表 DefaultIcon 指向这里的 cpy.ico。没有就跳过，不影响双击运行。
    if os.name == 'nt':
        assets = os.path.join(os.path.dirname(SRC_DIR), 'assets')
        for ico in ('cpy.ico', 'pypp.ico'):
            src = os.path.join(assets, ico)
            if os.path.isfile(src):
                try:
                    shutil.copy2(src, os.path.join(bindir, ico))
                    log(f"{G}✓{N} 已复制图标: {os.path.join(bindir, ico)}")
                except OSError as e:
                    log(f"{Y}提示{N} 图标复制失败（{e}），将使用系统默认图标")
            else:
                log(f"{Y}提示{N} 未找到 {src}，跳过文件图标")

    # .cpy 双击运行（文件关联）——只写 HKCU，不需要管理员权限
    if use_assoc:
        from assoc import register as assoc_register
        r = assoc_register(bindir, log=lambda m: log(m))
        result['assoc'] = r
    else:
        result['assoc'] = {'registered': False,
                           'skipped': '按 --no-assoc 要求跳过'}
        log("已跳过 .cpy 文件关联注册（--no-assoc）")

    # 提示 PATH
    if os.name == 'nt':
        if result['assoc'].get('registered'):
            log(f"{G}提示{N} 双击任意 .cpy 文件即可直接运行（运行器: "
                f"{os.path.join(bindir, 'pypprun.bat')}）")
        log(f"{Y}提示{N} 请把以下目录加入 PATH 环境变量：")
        log(f"     {bindir}")
        log(f"   或直接使用:  \"{launcher}\" your_code.cpy")
    else:
        log(f"{Y}提示{N} 请把 {bindir} 加入 PATH")

    result['success'] = True
    return result


def uninstall(json_mode=False):
    result = {'action': 'uninstall', 'success': False, 'messages': []}
    bindir = os.path.join(os.path.expanduser('~'), '.pypp')
    # 先注销文件关联（要在目录删除前做：unregister 要核对归属）
    from assoc import unregister as assoc_unregister
    ar = assoc_unregister(bindir=os.path.join(bindir, 'bin'))
    result['assoc'] = ar
    for m in ([ar.get('skipped')] if ar.get('skipped') else []):
        result['messages'].append(m)
    if ar.get('unregistered'):
        result['messages'].append("已注销 .cpy 文件关联")
    if os.path.exists(bindir):
        shutil.rmtree(bindir, ignore_errors=True)
        result['messages'].append(f"已移除 {bindir}")
    result['success'] = True
    if not json_mode:
        for m in result['messages']:
            print(m)
    return result


def print_env_human(info):
    print(f"\n{B}{C}py++ {info['pypp_version']} 环境检测{N}\n")
    py = info['python']
    ok = f"{G}✓{N}" if py['ok'] else f"{R}✗{N}"
    print(f"  {ok} Python {py['version']}   ({py['executable']})")
    pl = info['platform']
    print(f"  ·  系统 {pl['system']} {pl['release']} / {pl['machine']}")
    cp = info['compiler']
    if cp['found']:
        print(f"  {G}✓{N} C 编译器 {cp['name']}  {cp['version']}")
        print(f"       路径 {cp['path']}")
    else:
        print(f"  {Y}!{N} {cp['note']}")
    eng = f"{G}原生高速模式{N}" if info['engine'] == 'native' else f"{Y}内置 VM 模式{N}"
    print(f"\n  {B}执行引擎: {eng}{N}\n")


def main():
    parser = argparse.ArgumentParser(description='py++ 安装器')
    parser.add_argument('--check', action='store_true', help='仅检测环境')
    parser.add_argument('--json', action='store_true', help='输出 JSON（网站用）')
    parser.add_argument('--uninstall', action='store_true', help='卸载')
    parser.add_argument('--no-assoc', action='store_true',
                        help='不注册 .cpy 文件关联（双击运行），仅 Windows 有效')
    args = parser.parse_args()

    if args.uninstall:
        r = uninstall(args.json)
        if args.json:
            print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0

    if args.check:
        info = detect_environment()
        if args.json:
            print(json.dumps(info, ensure_ascii=False, indent=2))
        else:
            print_env_human(info)
        return 0 if info['ready'] else 1

    # 检查 + 安装
    info = detect_environment()
    if not args.json:
        print_env_human(info)

    r = install_command(args.json, use_assoc=not args.no_assoc)
    if args.json:
        out = {'install': r, 'environment': info}
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print(f"\n{G}{B}py++ {VERSION} 安装完成！{N}")
        print(f"试试运行:  pypp examples/hello.cpy\n")
    return 0 if r['success'] else 1


if __name__ == '__main__':
    sys.exit(main())
