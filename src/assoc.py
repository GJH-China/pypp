"""
py++ 双击运行支持 —— .cpy 文件关联（Windows）

目标：**双击 .cpy 文件就能直接跑起来**，不需要打包成 exe。

由两部分组成：
  1. 运行器 pypprun.bat（装在 <home>/bin/）——由**安装器**生成
     （它才知道该用哪个 Python、runner.py 在哪）；它的内容模板在
     runner.py 的 runner_bat_content()。Explorer 双击时由 bat 拉起
     python runner.py，由 runner 负责流式输出与界面。
  2. 文件关联（只写 HKCU，**不需要管理员权限**）：
       HKCU\\Software\\Classes\\.cpy                    默认值 → pypp.cpy
       HKCU\\Software\\Classes\\pypp.cpy                默认值 → 友好名
       HKCU\\Software\\Classes\\pypp.cpy\\shell\\open\\command → "<bindir>\\pypprun.bat" "%1"

只写当前用户（HKCU）而不是 HKLM：无需提权，也不会影响同机其他用户。

注册表读写走可注入原语（_reg_set / _reg_get / _reg_delete_tree），
测试时把它们换成内存假实现，即可验证"写了哪些键、什么值"而不碰真实注册表。
"""

import os

# ProgID：关联标识。换名字等于换一套注册表键，升级时别乱改。
PROG_ID = 'pypp.cpy'
EXT_NAME = '.cpy'
# 运行器 bat 的文件名（内容模板在 runner.py，由安装器生成）
RUNNER_NAME = 'pypprun.bat'
# .cpy 文件图标（build_icons.py 生成，安装器复制到 ~/.pypp/bin/）
ICON_NAME = 'cpy.ico'
FRIENDLY_NAME = 'py++ 源文件'

_NOT_WINDOWS = (os.name != 'nt')


# ---------------------------------------------------------------------------
# 注册表原语（可注入：测试换成内存假实现，不碰真实注册表）
# ---------------------------------------------------------------------------
def _reg_set(path, value, name=''):
    """写一个 REG_SZ。name='' 表示键的默认值。"""
    import winreg
    key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, path)
    try:
        winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
    finally:
        winreg.CloseKey(key)


def _reg_get(path, name=''):
    """读一个值；键或值不存在时返回 None。"""
    import winreg
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, path)
    except FileNotFoundError:
        return None
    try:
        try:
            return winreg.QueryValueEx(key, name)[0]
        except FileNotFoundError:
            return None
    finally:
        winreg.CloseKey(key)


def _reg_delete_tree(path):
    """递归删键（含子键）。不存在则静默。"""
    import winreg
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, path)
    except FileNotFoundError:
        return
    subs = []
    i = 0
    while True:
        try:
            subs.append(winreg.EnumKey(key, i))
        except OSError:
            break
        i += 1
    winreg.CloseKey(key)
    for s in subs:
        _reg_delete_tree(path + '\\' + s)
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
    except FileNotFoundError:
        pass


def _paths(bindir):
    """涉及的注册表路径与要写的值。"""
    return {
        'ext': r'Software\Classes' + '\\' + EXT_NAME,
        'progid': r'Software\Classes' + '\\' + PROG_ID,
        'command': r'Software\Classes' + '\\' + PROG_ID + r'\shell\open\command',
        'command_value': f'"{os.path.join(bindir, RUNNER_NAME)}" "%1"',
        # 文件图标：装在 bindir 里（与运行器同目录，~/.pypp/bin/），
        # 由 _reg_set 写到 PROG_ID\DefaultIcon。注销时随整个 ProgID 树删掉。
        'icon': r'Software\Classes' + '\\' + PROG_ID + r'\DefaultIcon',
        'icon_value': os.path.join(os.path.abspath(bindir), ICON_NAME),
    }


# ---------------------------------------------------------------------------
# 对外 API
# ---------------------------------------------------------------------------
def register(bindir, log=None):
    """注册 .cpy 双击运行。bindir 必须是运行器所在目录（<home>/bin）。

    pypprun.bat 由**安装器**生成（它才知道 Python 路径与 runner.py 位置），
    本函数只写注册表；bat 不在位时给出 warning 但不阻断。
    返回结果 dict；任何一步失败都不抛异常（关联装不上不影响 py++ 使用）。
    """
    res = {'supported': not _NOT_WINDOWS, 'registered': False,
           'runner': None, 'ext_key': None, 'command': None,
           'icon': None, 'skipped': '', 'error': '', 'warning': ''}
    if _NOT_WINDOWS:
        res['skipped'] = '非 Windows 平台，文件关联仅支持 Windows'
        if log:
            log(res['skipped'])
        return res

    bindir = os.path.abspath(bindir)
    try:
        # 1) 确认运行器在位（bat 由安装器写入，这里只核对）
        res['runner'] = os.path.join(bindir, RUNNER_NAME)
        if not os.path.isfile(res['runner']):
            msg = f"{bindir} 里没有 {RUNNER_NAME}，双击会找不到运行器"
            res['warning'] = msg
            if log:
                log(f"  注意：{msg}")

        # 2) 写关联
        p = _paths(bindir)
        _reg_set(p['ext'], PROG_ID)
        _reg_set(p['progid'], FRIENDLY_NAME)
        _reg_set(p['command'], p['command_value'])
        res['ext_key'] = p['ext']
        res['command'] = p['command_value']
        res['registered'] = True
        if log:
            log(f"  已注册 {EXT_NAME} → {PROG_ID}（双击即可运行）")

        # 3) 文件图标（可选）：图标不在位只提示，不影响双击运行
        if os.path.isfile(p['icon_value']):
            try:
                _reg_set(p['icon'], p['icon_value'])
                res['icon'] = p['icon_value']
                if log:
                    log(f"  已设置 {EXT_NAME} 文件图标 → {p['icon_value']}")
            except Exception as e:              # noqa: BLE001
                msg = f"图标注册失败（{type(e).__name__}: {e}）"
                res['warning'] = (res['warning'] + '；' + msg
                                  if res['warning'] else msg)
                if log:
                    log(f"  注意：{msg}")
        else:
            msg = f"{bindir} 里没有 {ICON_NAME}，将使用系统默认图标"
            res['warning'] = (res['warning'] + '；' + msg
                              if res['warning'] else msg)
            if log:
                log(f"  注意：{msg}")
    except Exception as e:                      # noqa: BLE001 - 绝不为关联丢安装
        res['error'] = f'{type(e).__name__}: {e}'
        res['registered'] = False
        if log:
            log(f"  文件关联注册失败（不影响 py++ 使用）: {res['error']}")
    return res


def unregister(bindir=None, log=None):
    """注销 .cpy 关联。

    ⚠️ 归属守卫：只删**自己的**键——
      · .cpy 的默认值必须正是我们的 PROG_ID；
      · command 引用的运行器必须位于 bindir 指定的目录
        （bindir 缺省时退而求其次：只要指向某个 .pypp/bin/ 下的
        pypprun.bat 就算我们的）。
    任何一条不满足就整体跳过，绝不误伤别的程序注册的 .cpy 关联。
    """
    res = {'supported': not _NOT_WINDOWS, 'unregistered': False,
           'skipped': '', 'error': ''}
    if _NOT_WINDOWS:
        res['skipped'] = '非 Windows 平台，无需注销'
        return res

    try:
        p = _paths(bindir or r'C:\.pypp\bin')
        ext_default = _reg_get(p['ext'])
        command = _reg_get(p['command'])
        if ext_default != PROG_ID:
            res['skipped'] = f"{EXT_NAME} 默认值为 {ext_default!r}，非 py++ 关联，跳过"
            if log:
                log(f"  {res['skipped']}")
            return res

        runner = os.path.join(bindir, RUNNER_NAME) if bindir else None
        ours = False
        if runner:
            ours = (command or '').lower().startswith(f'"{runner.lower()}"')
        else:
            # 没给 bindir：指向任意 .pypp\bin\pypprun.bat 都算我们的
            low = (command or '').lower()
            ours = (RUNNER_NAME in low) and ('.pypp' in low)
        if not ours:
            res['skipped'] = f"关联命令 {command!r} 不属于本次安装，跳过"
            if log:
                log(f"  {res['skipped']}")
            return res

        _reg_delete_tree(p['ext'])
        _reg_delete_tree(p['progid'])
        res['unregistered'] = True
        if log:
            log(f"  已注销 {EXT_NAME} 文件关联")
    except Exception as e:                      # noqa: BLE001
        res['error'] = f'{type(e).__name__}: {e}'
        if log:
            log(f"  文件关联注销失败: {res['error']}")
    return res


def status():
    """当前 .cpy 关联状态（供 --check / 自检 / 排障）。"""
    res = {'supported': not _NOT_WINDOWS, 'registered': False,
           'ext_default': None, 'command': None}
    if _NOT_WINDOWS:
        return res
    try:
        p = _paths(r'C:\.pypp\bin')
        res['ext_default'] = _reg_get(p['ext'])
        res['command'] = _reg_get(p['command'])
        res['registered'] = (res['ext_default'] == PROG_ID
                             and bool(res['command']))
    except Exception as e:                      # noqa: BLE001
        res['error'] = f'{type(e).__name__}: {e}'
    return res
