"""
打包源码分发包 pypp-<VERSION>-src.zip。

⚠️ 这是**源码包**，不是安装包。普通用户要的是 pypp-<VERSION>-setup.exe。
   两者绝不能放在同一个下载目录里，否则用户下错了还以为装坏了。

历史教训（2026-10-19）：
  旧包 pypp-1.1.0.zip 里 7 个文件已过时、还缺 assoc.py / runner.py
  —— 开发者按文档装完，双击 .cpy 依然跑不起来（双击运行正是这两个模块）。
  本脚本改为**自动收集**而非硬编码清单，并做完整性自检，避免再次漂移。

用法：
    python repack_zip.py            # 生成并自检
    python repack_zip.py --verify   # 只自检现有包
"""

import os
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
VERSION = "1.1.0"

# 输出名带 -src 后缀，和安装包区分开（文档与官网都按这个名字引用）
OUT = os.path.join(HERE, 'dist', 'pypp-%s-src.zip' % VERSION)
LEGACY = os.path.join(HERE, 'dist', 'pypp-%s.zip' % VERSION)

ROOT = 'pypp-%s' % VERSION

# ── 需要进包的源码模块（src/ 下全部 .py，自动收集）──
# 必须自动收集：assoc.py（文件关联）和 runner.py（双击运行器）就是
# 因为硬编码清单漏写而丢的。
def collect_src():
    d = os.path.join(HERE, 'src')
    return [(os.path.join(d, f), 'src/' + f)
            for f in sorted(os.listdir(d))
            if f.endswith('.py')]


def collect_dir(sub, exts, flatten=True):
    """收集子目录里的文件。flatten=True 时只保留文件名（包内平铺）。"""
    d = os.path.join(HERE, sub)
    if not os.path.isdir(d):
        return []
    out = []
    for f in sorted(os.listdir(d)):
        if not f.endswith(tuple(exts)):
            continue
        p = os.path.join(d, f)
        if not os.path.isfile(p):
            continue
        rel = (sub + '/' + f) if not flatten else f
        out.append((p, rel))
    return out


def build_items():
    items = []
    # ① 核心与工具：src/*.py（含 assoc.py、runner.py）
    items += collect_src()
    # ② 示例（根目录平铺到 examples/）
    items += [(p, 'examples/' + os.path.basename(p))
              for p, _ in collect_dir('examples', ['.cpy'])]
    # ③ 安装器与包管理（根目录）
    for f in ['install.py', 'install_gui.py', 'install_gui.bat',
              'pget.py', 'pget.bat', 'pyget.bat', 'cget.bat', 'cpget.bat',
              'README.md']:
        p = os.path.join(HERE, f)
        if os.path.isfile(p):
            items.append((p, f))
    # ④ Web 服务与接口文档
    p = os.path.join(HERE, 'web', 'server.py')
    if os.path.isfile(p):
        items.append((p, 'web/server.py'))
    p = os.path.join(HERE, 'docs', 'WEB_API.md')
    if os.path.isfile(p):
        items.append((p, 'docs/WEB_API.md'))
    # ⑤ 快速精通指南（⚠️ 现在在 docs/ 下，不是根目录）
    p = os.path.join(HERE, 'docs', '快速精通.md')
    if os.path.isfile(p):
        items.append((p, '快速精通.md'))
    # ⑥ 源码包安装说明 —— 解压后第一眼就看到「这不是安装包」
    p = os.path.join(HERE, 'docs', '源码包安装说明.md')
    if os.path.isfile(p):
        items.append((p, '请先读我-从源码安装.md'))
    return items


def verify(path):
    """自检：关键文件齐不齐、内容是不是当前的。"""
    if not os.path.isfile(path):
        print('✗ 包不存在：%s' % path)
        return False

    problems = []
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        files = {n[len(ROOT) + 1:]: n for n in names if n.startswith(ROOT + '/')}

        # ① 必备模块（双击运行依赖 assoc + runner）
        required = [
            'src/pypp.py', 'src/lexer.py', 'src/parser.py', 'src/codegen.py',
            'src/interpreter.py', 'src/repair.py',
            'src/assoc.py', 'src/runner.py',
            'install.py', 'install_gui.py', 'pget.py', 'README.md',
            '请先读我-从源码安装.md',
        ]
        for r in required:
            if r not in files:
                problems.append('缺文件：' + r)

        # ② 内容与当前源码一致（逐字节）
        import hashlib
        for rel, zname in files.items():
            local = os.path.join(HERE, rel.replace('/', os.sep))
            if not os.path.isfile(local):
                continue   # 包内独有文件不判
            zh = hashlib.md5(z.read(zname)).hexdigest()
            lh = hashlib.md5(open(local, 'rb').read()).hexdigest()
            if zh != lh:
                problems.append('内容过时：' + rel)

        # ③ 不能混入安装包
        for n in names:
            if n.lower().endswith('.exe'):
                problems.append('不该含 exe：' + n)

    if problems:
        print('✗ 自检未通过（%d 项）：' % len(problems))
        for p in problems:
            print('   · ' + p)
        return False

    print('✓ 自检通过：%d 个文件，必备模块齐全，内容与当前源码一致'
          % len(files))
    return True


def main():
    if '--verify' in sys.argv:
        return 0 if verify(OUT) else 1

    items = build_items()
    if not items:
        raise SystemExit('没有可打包的文件')

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with zipfile.ZipFile(OUT, 'w', zipfile.ZIP_DEFLATED) as z:
        for src, rel in items:
            z.write(src, ROOT + '/' + rel)

    size = os.path.getsize(OUT)
    print('已生成 %s  (%.1f KB, %d 个文件)' % (OUT, size / 1024, len(items)))

    # 列出模块，肉眼可见 assoc/runner 在不在
    mods = [rel for _, rel in items if rel.startswith('src/')]
    print('  src 模块：' + ', '.join(os.path.basename(m) for m in mods))

    ok = verify(OUT)
    if ok and os.path.exists(LEGACY):
        print()
        print('⚠️ 旧的 %s 仍在（无 -src 后缀，容易被当成安装包）。' % LEGACY)
        print('   建议删除，或改名后只留 -src 版本。')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
