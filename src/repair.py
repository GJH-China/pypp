"""
py++ 容错修复层 (Repair)
这一层是 py++ 的招牌能力：即使代码有小错误，也自动修补并运行，
同时用黄色警告告诉用户"你的代码有小错误，并无大碍，py++ 已兼容运行"。

三类处理策略：
  1. 词法/语法级小错  → 自动修补（缺失冒号、未闭合字符串、缩进不齐、拼写近似…）
  2. 常见拼写错误      → 智能纠错（pritn -> print, ture -> True）
  3. 无法修补的结构错误 → 明确报错，附修复建议

⚠️ 本文件不要引入冷门标准库。src/ 在 exe 里是**数据文件**，
PyInstaller 的静态分析看不到这里面的 import，没被打包的模块会让
安装器在 `from runner import runner_bat_content` 时直接崩。
（difflib 曾在这里，2026-10-14 删。）
"""

import re

# ANSI 颜色（当 NO_COLOR 环境变量存在或被显式关闭时，全部置空）
import os as _os
_USE_COLOR = _os.environ.get('PYPP_NO_COLOR', '') != '1'

YELLOW = '\033[33m'
RED = '\033[31m'
GREEN = '\033[32m'
CYAN = '\033[36m'
BOLD = '\033[1m'
RESET = '\033[0m'


def set_color(enabled: bool):
    """全局开关颜色输出（Web 接口用纯文本）。"""
    global YELLOW, RED, GREEN, CYAN, BOLD, RESET
    if enabled:
        YELLOW, RED, GREEN, CYAN, BOLD, RESET = (
            '\033[33m', '\033[31m', '\033[32m', '\033[36m', '\033[1m', '\033[0m')
    else:
        YELLOW = RED = GREEN = CYAN = BOLD = RESET = ''


if not _USE_COLOR:
    set_color(False)


class RepairReport:
    def __init__(self):
        self.items = []   # (level, line, msg, detail)

    def add(self, level, line, msg, detail=""):
        self.items.append((level, line, msg, detail))

    @property
    def has_repairs(self):
        return any(lvl == 'repair' for lvl, *_ in self.items)

    @property
    def has_errors(self):
        return any(lvl == 'error' for lvl, *_ in self.items)

    def render(self, use_color=True) -> str:
        if not self.items:
            return ""
        out = []
        for level, line, msg, detail in self.items:
            if use_color:
                if level == 'repair':
                    tag = f"{YELLOW}{BOLD}[已自动修复]{RESET}"
                elif level == 'error':
                    tag = f"{RED}{BOLD}[错误]{RESET}"
                else:
                    tag = f"{CYAN}[提示]{RESET}"
            else:
                tag = {'repair': '[已自动修复]', 'error': '[错误]', 'info': '[提示]'}[level]
            loc = f"第 {line} 行" if line else ""
            out.append(f"  {tag} {loc} {msg}")
            if detail:
                out.append(f"         {detail}")
        return "\n".join(out)


# 常见拼写错误映射：错误写法 -> 正确写法
# ---------------------------------------------------------------------------
# 拼写修复：两级策略
#   一级 = 编辑距离算法（见 fuzzy_correct），覆盖任意单/双字符错位，
#          是主力，不依赖穷举。
#   二级 = 下面这张"特例表"，只保留算法确实推不出来的少数情况：
#          · 语义歧义（fro → for，但算法会选 from）
#          · 同义词而非拼写错（fucntion → def）
#          · 距离超出阈值且无唯一解（ture → True）
#          原则上这张表越小越好，能靠算法推出来的一律不加进来。
# ---------------------------------------------------------------------------
SPELLING_FIXES = {
    'pint': 'print',        # 算法会因 len<=4 限制而犹豫
    'ture': 'True', 'treu': 'True', 'turee': 'True',
    'flase': 'False', 'fasle': 'False',
    'nnoe': 'None', 'noen': 'None',
    'fi': 'if',             # 太短，算法不猜
    'esle': 'else',
    'fro': 'for',           # 关键：算法会误选 from
    'fucntion': 'def',      # 同义词，非拼写
    'lenght': 'len',
    'lne': 'len',           # 太短
    'inpt': 'input',
    'oor': 'or',            # 太短
    'elsif': 'elif',        # 语义近似而非拼写
    'beak': 'break',
    'defn': 'def',
}


# 内置名集合（用于判断标识符是否为拼写错误）
KNOWN_NAMES = set(SPELLING_FIXES.values()) | {
    'if', 'else', 'elif', 'while', 'for', 'in', 'def', 'return', 'break',
    'continue', 'and', 'or', 'not', 'True', 'False', 'None', 'class',
    'import', 'from', 'as', 'pass', 'global',
    'print', 'input', 'int', 'float', 'str', 'len', 'abs', 'min', 'max',
    'sum', 'range', 'pow', 'append', 'upper', 'lower', 'split', 'strip',
}


# 参与"模糊拼写纠正"的内置关键字/函数。
# 只对这些名字做编辑距离匹配 —— 数量有限、语义明确，误伤风险可控。
# 用户自定义变量/函数名不在此列，避免把 x 改成 y 这类误判。
_FUZZY_TARGETS = {
    'print', 'input', 'int', 'float', 'str', 'len', 'abs', 'min', 'max',
    'sum', 'range', 'pow', 'append',
    'if', 'else', 'elif', 'while', 'for', 'in', 'def', 'return', 'break',
    'continue', 'and', 'or', 'not', 'True', 'False', 'None', 'class',
    'import', 'from', 'as', 'pass', 'global',
}


def _levenshtein(a: str, b: str, cap: int = 3) -> int:
    """带提前剪枝的编辑距离，超过 cap 直接返回 cap+1。"""
    if a == b:
        return 0
    la, lb = len(a), len(b)
    if abs(la - lb) > cap:
        return cap + 1
    prev = list(range(lb + 1))
    for i in range(1, la + 1):
        cur = [i] + [0] * lb
        row_min = cur[0]
        for j in range(1, lb + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if cur[j] < row_min:
                row_min = cur[j]
        if row_min > cap:
            return cap + 1
        prev = cur
    return prev[lb]


def fuzzy_correct(word: str):
    """
    对"疑似拼错的内置名"做编辑距离匹配。
    返回 (正确名, 距离) 或 None。

    规则（刻意保守，避免误伤用户变量）：
      - 词长 < 3 时不猜（'nn'/'nd'/'fi' 这类噪音太大）
      - 词已经在 _FUZZY_TARGETS 里 → 不处理
      - 允许的距离：短词(<=4)最多 1，长词最多 2
      - 首字母必须一致（foo->for 挡不住，但 num->sum 能挡住）
      - 必须存在唯一最优解，有并列则不猜
    """
    if len(word) < 3:
        return None
    if word in _FUZZY_TARGETS:
        return None
    lw = word.lower()
    limit = 1 if len(word) <= 4 else 2
    best, best_d, tie = None, limit + 1, False
    for cand in _FUZZY_TARGETS:
        lc = cand.lower()
        if lc[0] != lw[0]:          # 首字母必须相同
            continue
        d = _levenshtein(lw, lc, cap=limit)
        if d > limit:
            continue
        if d < best_d:
            best, best_d, tie = cand, d, False
        elif d == best_d:
            tie = True
    if best is None or tie:
        return None
    return best, best_d



_ASSIGN_RE = re.compile(r'^\s*([A-Za-z_][A-Za-z0-9_]*(?:\s*,\s*[A-Za-z_][A-Za-z0-9_]*)*)\s*(?:[-+*/%]|//|\*\*)?=(?!=)')
_DEF_RE = re.compile(r'^\s*def\s+([A-Za-z_][A-Za-z0-9_]*)')
_FOR_RE = re.compile(r'^\s*for\s+([A-Za-z_][A-Za-z0-9_]*)\s+in\b')
_PARAM_RE = re.compile(r'^\s*def\s+[A-Za-z_][A-Za-z0-9_]*\s*\(([^)]*)\)')
_CLASS_RE = re.compile(r'^\s*class\s+([A-Za-z_][A-Za-z0-9_]*)')
_IMPORT_RE = re.compile(r'^\s*import\s+(.+)')
# ⚠️ 两处细节：
#   1. 模块名本身也要捕获 —— `from urllib.parse import quote` 里的 `parse`
#      是模块名的组成部分，漏掉它就会被当成 pass 的拼写错误改掉
#      （编辑距离 1、首字母相同，能通过前两道防误伤）。
#   2. 模块名允许**以点开头** —— `from . import x`（相对导入）很常见，
#      原正则要求首字符必须是字母，导致这种行完全不被识别、绑定名也不豁免。
_FROM_IMPORT_RE = re.compile(
    r'^\s*from\s+(\.*[A-Za-z_][A-Za-z0-9_.]*|\.+)\s+import\s+(.+)')


def _import_bound_names(spec: str) -> set:
    """从 import 语句尾部解析出它绑定了哪些名字（别名优先）。"""
    out = set()
    for part in spec.split(','):
        part = part.strip().rstrip(')').strip()
        if not part or part == '*':
            continue
        if ' as ' in part:
            out.add(part.split(' as ')[-1].strip())
        else:
            out.add(part.split('.')[0].strip())
    return {n for n in out if re.match(r'^[A-Za-z_][A-Za-z0-9_]*$', n)}


def _module_name_parts(mod: str) -> set:
    """
    把 `urllib.parse` 拆成 {'urllib', 'parse'}。

    模块名的每一级都享有豁免权 —— 它们不是用户变量，但同样是"合法名字"，
    不该被模糊纠正改写。`from urllib.parse import quote` 里的 `parse`
    正是栽在这里（被改成了 `pass`）。
    """
    out = set()
    for seg in mod.split('.'):
        seg = seg.strip()
        if re.match(r'^[A-Za-z_][A-Za-z0-9_]*$', seg):
            out.add(seg)
    return out


def _collect_user_names(source: str) -> set:
    """
    扫描全文，找出用户自己定义的名字（变量 / 函数名 / 函数参数 / 类名 / import 别名）。
    这些名字享有"拼写豁免权" —— 绝不能被模糊纠正。
    这是防止把 foo -> for、num -> sum、Point -> print 这类误伤的关键防线。
    """
    names = set()
    for raw in source.split('\n'):
        line = raw
        h = _find_comment_start(line)
        if h >= 0:
            line = line[:h]
        if not line.strip():
            continue
        m = _ASSIGN_RE.match(line)
        if m:
            for part in m.group(1).split(','):
                names.add(part.strip())
        m = _DEF_RE.match(line)
        if m:
            names.add(m.group(1))
        m = _CLASS_RE.match(line)
        if m:
            names.add(m.group(1))
        m = _FOR_RE.match(line)
        if m:
            names.add(m.group(1))
        m = _PARAM_RE.match(line)
        if m and m.group(1).strip():
            for part in m.group(1).split(','):
                p = part.split('=')[0].strip()
                if p:
                    names.add(p)
        m = _IMPORT_RE.match(line)
        if m:
            names |= _import_bound_names(m.group(1))
            # import urllib.parse —— 模块名各级也豁免
            for part in m.group(1).split(','):
                head = part.strip().split(' as ')[0].strip()
                names |= _module_name_parts(head)
        m = _FROM_IMPORT_RE.match(line)
        if m:
            # group(1)=模块名（含点分各级），group(2)=导入的名字
            names |= _module_name_parts(m.group(1))
            names |= _import_bound_names(m.group(2))
    return names


def fix_source(source: str, report: RepairReport) -> str:
    """
    预处理阶段：修复明显的拼写错误。
    顺序很重要（严格自上而下）：
      0. 全角标点归一化（中文输入法高频错误，且会破坏结构）
      1. 多行断裂修复（否则标识符已被逐行改写，断裂信息丢失）
      2. 逐行拼写修复（两级策略：特例表 -> 编辑距离）
    用户自定义的名字（变量/函数/参数）一律豁免。
    """
    source = normalize_fullwidth(source, report)
    source = fix_broken_lines(source, report)
    user_names = _collect_user_names(source)
    lines = source.split('\n')
    out = []
    for i, line in enumerate(lines, 1):
        # 跳过字符串内部（粗略：不在引号内的部分才替换）
        fixed, changed = _fix_line_spelling(line, user_names)
        if changed:
            for wrong, right in changed:
                report.add('repair', i,
                           f"'{wrong}' 疑似拼写错误，已自动更正为 '{right}'",
                           "这无大碍，py++ 已帮你改正并继续运行")
            line = fixed
        out.append(line)
    result = '\n'.join(out)
    # 最后扫一遍：有没有无法可靠还原的悬空片段，明确告知用户
    detect_dangling_fragments(result, report)
    return result


# ---------------------------------------------------------------------------
# 多行断裂修复
# ---------------------------------------------------------------------------
# 场景：用户手抖把一行代码敲成了几行，例如
#       prin
#       f
#       ("hello")
#   本意是 print("hello")。
#
# 为什么必须跨行处理：
#   逐行看 'prin' 像 print 的拼写错误，会被单独改成 print，
#   于是变成 print / f / (...) 三条语句 —— 语义彻底跑偏，却还报"已修复"。
#   只有把相邻几行拼起来看，才能认出这其实是一个被拆散的函数名。
#
# 两种真实形态：
#   A：名字完整拆开，末尾独立成行 ——  prin / f / ("a")
#   B：末行是"标识符前缀 + 剩余代码" ——  whi / le x > 0:
#
# 触发条件（三条同时满足，刻意保守）：
#   1. 连续若干行都以标识符字符开头，且中间没有赋值/运算符打断
#   2. 各行的标识符前缀拼起来恰好是某个已知内置名（精确，或距离<=1）
#   3. 名字之后紧跟 '(' 或一个空格（说明是调用/关键字，不是裸变量）
# ---------------------------------------------------------------------------

_BUILTIN_LOWER = {c.lower(): c for c in _FUZZY_TARGETS}
MAX_MERGE_LINES = 3          # 最多合并 3 行，避免组合爆炸

# 只匹配"内置名 + 其后必须是空格或 ("的形态：
#   print(...)   while x:   return 1   range(...)   if x:   True
# 不匹配：print_x（用户变量）、printer（用户变量）
_NAME_START_RE = re.compile(r'^([A-Za-z_][A-Za-z0-9_]*)(\s|\()')
_IDENT_ONLY_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')


def _match_builtin(name: str):
    """name 是否是某个内置名（精确，或编辑距离<=1）。返回规范写法或 None。"""
    low = name.lower()
    if low in _BUILTIN_LOWER:
        return _BUILTIN_LOWER[low]
    if len(low) < 3:
        return None
    best, best_d, tie = None, 2, False
    for cand_low, cand in _BUILTIN_LOWER.items():
        if cand_low[0] != low[0]:
            continue
        d = _levenshtein(low, cand_low, cap=1)
        if d > 1:
            continue
        if d < best_d:
            best, best_d, tie = cand, d, False
        elif d == best_d:
            tie = True
    return None if tie else best


def _merge_ident_run(lines, report):
    """规则 1：把连续标识符行拼成内置名。"""
    out = []
    i = 0
    n = len(lines)
    while i < n:
        res = _try_merge(lines, i, MAX_MERGE_LINES)
        if res:
            new_last, k = res
            frag = ' + '.join(s.strip() for s in lines[i:i + k])
            canonical = new_last.split('(')[0].split()[0]
            report.add('repair', i + 1,
                       f"第 {i+1}-{i+k} 行的代码被换行拆散了"
                       f"（{frag}），已自动拼接为 '{canonical}'",
                       "这无大碍，py++ 已帮你拼接并继续运行")
            out.append(new_last)
            i += k
            continue
        out.append(lines[i])
        i += 1
    return out


def _try_merge(lines, start, max_k):
    """
    尝试从 start 开始合并 2..max_k 行。
    返回 (合并后的首行文本, 消耗的行数) 或 None。
    """
    n = len(lines)
    for k in range(2, max_k + 1):
        if start + k > n:
            break
        seg = lines[start:start + k]
        # 收集每行的"标识符前缀"
        prefixes = []
        ok = True
        for s in seg:
            st = s.strip()
            if not st:
                ok = False
                break
            if _IDENT_ONLY_RE.match(st):
                prefixes.append(st)
            else:
                m = _NAME_START_RE.match(st)
                if m and m.group(1).isidentifier():
                    prefixes.append(m.group(1))
                else:
                    ok = False
                    break
        if not ok or len(prefixes) < k:
            # 末行必须贡献前缀；中间行必须纯标识符
            if not (ok and len(prefixes) >= 1):
                # 尝试更短的 k 无意义，直接放弃
                pass
            if k == 2:
                continue
            break

        # 中间行（除最后一行）必须是纯标识符，否则不是一个名字被拆开
        mid_ok = all(_IDENT_ONLY_RE.match(s.strip())
                     for s in seg[:-1])
        if not mid_ok:
            continue

        joined = ''.join(prefixes)
        canonical = _match_builtin(joined)
        if not canonical:
            continue

        # 末行：把标识符前缀替换成规范名
        last = seg[-1].strip()
        rest = last[len(prefixes[-1]):]
        # 名字后面必须紧跟空格或 '('，否则可能是别的标识符（如 printer）
        if rest and not (rest[0] in ' \t('):
            continue
        new_last = canonical + rest
        return new_last, k
    return None


# ---------------------------------------------------------------------------
# 全角标点归一化
# ---------------------------------------------------------------------------
# 中文输入法下极容易打出全角标点（（），：等），这在编程时是高频错误，
# 比拼写错误更常见。原实现只是"跳过并丢弃"，会破坏代码结构
# （例如 （） 被丢掉后，调用就变成了裸标识符）。
# 这里改为统一归一化到半角 —— 保留结构，语义正确。
#
# 注意：只处理"代码区"，字符串字面量内部一律不动
#      （用户可能真的想输出全角中文标点）。
# ---------------------------------------------------------------------------

_FULLWIDTH_MAP = {
    '（': '(', '）': ')',
    '，': ',', '：': ':', '；': ';',
    '“': '"', '”': '"', '‘': "'", '’': "'",
    '［': '[', '］': ']', '｛': '{', '｝': '}',
    '＝': '=', '＋': '+', '－': '-', '＊': '*', '／': '/',
    '＜': '<', '＞': '>', '！': '!', '％': '%',
    '＆': '&', '｜': '|', '～': '~', '＾': '^',
    '。': '.', '　': ' ', '＃': '#', '＠': '@',
    '＄': '$', '？': '?', '、': ',',
}

_FW_TABLE = str.maketrans(_FULLWIDTH_MAP)


def normalize_fullwidth(source: str, report: RepairReport) -> str:
    """把代码区的全角标点归一化为半角，保护字符串与注释。"""
    out_lines = []
    for lineno, line in enumerate(source.split('\n'), 1):
        fixed = _normalize_line(line, lineno, report)
        out_lines.append(fixed)
    return '\n'.join(out_lines)


def _normalize_line(line: str, lineno: int, report: RepairReport) -> str:
    """逐行归一化，跳过字符串内部与注释。"""
    # 注释整体不动（注释里写全角标点是正常的）
    hash_idx = _find_comment_start(line)
    code = line if hash_idx < 0 else line[:hash_idx]
    tail = '' if hash_idx < 0 else line[hash_idx:]

    result = []
    buf = []
    i = 0
    n = len(code)
    in_str = None
    # 字符串的"闭引号"候选：半角同类 + 全角对应闭合符
    CLOSERS = {'"': ('"', '”'), "'": ("'", '’')}
    while i < n:
        ch = code[i]
        if in_str:
            # 字符串内容整体保留，但允许"全角闭引号"结束字符串
            if ch == '\\' and i + 1 < n:
                buf.append(ch); buf.append(code[i + 1]); i += 2; continue
            if ch in CLOSERS[in_str]:
                # 结束字符串，统一写成半角引号
                if ch != in_str:
                    report.add('repair', lineno,
                               f"'{ch}' 是全角引号，已自动换成半角 {in_str}",
                               "这无大碍，py++ 已兼容运行")
                buf.append(in_str)
                in_str = None
                i += 1
                continue
            buf.append(ch)
            i += 1
            continue
        # 开引号：半角 " '，或全角 “ ‘
        if ch in ('"', "'", '“', '‘'):
            half = ch.translate(_FW_TABLE)
            if buf:
                result.append(''.join(buf)); buf = []
            in_str = half
            buf.append(half)
            if ch != half:
                report.add('repair', lineno,
                           f"'{ch}' 是全角引号，已自动换成半角 {half}",
                           "这无大碍，py++ 已兼容运行")
            i += 1
            continue
        # 孤立的全角闭引号（没有对应开引号）：当普通字符归一化
        if ch in _FULLWIDTH_MAP:
            if buf:
                result.append(''.join(buf)); buf = []
            half = _FULLWIDTH_MAP[ch]
            report.add('repair', lineno,
                       f"'{ch}' 是全角字符，已自动换成半角 '{half}'",
                       "这无大碍，py++ 已兼容运行")
            result.append(half)
            i += 1
            continue
        buf.append(ch)
        i += 1
    if buf:
        result.append(''.join(buf))
    return ''.join(result) + tail


def fix_broken_lines(source: str, report: RepairReport) -> str:
    """
    修复被换行拆散的内置名（跨行评估，见上方说明）。
    返回修复后的源码。
    """
    lines = _merge_ident_run(source.split('\n'), report)
    return '\n'.join(lines)


def detect_dangling_fragments(source: str, report: RepairReport) -> None:
    """
    检测"悬空片段"：孤立的标识符行 + 紧跟的括号行。

    背景：像 prin / f / (...) 这种断裂已被 fix_broken_lines 修好。
    但如果断裂发生在"名字已经带括号"之后（prinf(l) / f / (...)），
    信息已无法可靠还原 —— 此时绝不能乱猜，而应明确告知用户。

    判断标准（保守）：
      某行是一个孤立标识符（可能是内置名的残片），
      且下一行以 '(' 开头 —— 这两行凑一起像一次调用，
      但前一行本身没被任何赋值/运算连接，属于悬空。
    只报告，不改写。
    """
    lines = source.split('\n')
    n = len(lines)
    for i in range(n - 1):
        cur = lines[i].strip()
        nxt = lines[i + 1].strip()
        if not cur or not nxt:
            continue
        if not _IDENT_ONLY_RE.match(cur):
            continue
        if len(cur) > 2:
            continue
        if not nxt.startswith('('):
            continue
        # 这行的标识符是否是某个内置名的残片
        if cur.lower() in _BUILTIN_LOWER:
            continue
        report.add('repair', i + 1,
                   f"第 {i+1} 行的 '{cur}' 看起来是某个函数名被敲断后留下的碎片，"
                   f"且与第 {i+2} 行的括号不完整",
                   f"py++ 无法确定你的本意，已原样保留。"
                   f"建议检查是否想写 print(...)")


def is_short_fragment(s: str, canon: str) -> bool:
    """孤立的短标识符，且可能是某个内置名被打散后的残片。"""
    if not s or len(s) > 2:
        return False
    if s.lower() in _BUILTIN_LOWER:
        return False
    # 残片应该是 canon 的子串（被敲散的部分）
    return s.lower() in canon.lower()


def _looks_like_broken_builtin(line: str):
    """
    判断一行是否是"残缺的内置名"。
    例：'prinf' 距离 print 为 1 → 是残缺名，返回 (规范名, 已带括号?)
        'hello' 不是 → None
    """
    st = line.strip()
    m = _NAME_START_RE.match(st)
    if not m:
        return None
    name = m.group(1)
    if name.lower() in _BUILTIN_LOWER:
        return None            # 已经是完整的内置名，不算残缺
    canon = _match_builtin(name)
    if not canon:
        return None
    has_paren = st[len(name):].lstrip().startswith('(')
    return canon, has_paren, name


def _fix_line_spelling(line: str, user_names=None):
    """逐行拼写修复，保护字符串内容与注释。"""
    changes = []
    # 先剥离注释（# 之后的内容不处理）
    hash_idx = _find_comment_start(line)
    code_part = line if hash_idx < 0 else line[:hash_idx]
    comment_part = '' if hash_idx < 0 else line[hash_idx:]

    result = []
    i = 0
    n = len(code_part)
    in_str = None
    buf = []
    while i < n:
        ch = code_part[i]
        if in_str:
            buf.append(ch)
            if ch == '\\' and i + 1 < n:
                buf.append(code_part[i + 1]); i += 2; continue
            if ch == in_str:
                result.append(''.join(buf)); buf = []; in_str = None
            i += 1
            continue
        if ch in '"\'':
            if buf:
                result.append(_replace_words(''.join(buf), changes, user_names))
                buf = []
            in_str = ch
            buf.append(ch)
            i += 1
            continue
        buf.append(ch)
        i += 1
    if buf:
        if in_str:
            result.append(''.join(buf))
        else:
            result.append(_replace_words(''.join(buf), changes, user_names))
    return ''.join(result) + comment_part, changes


def _find_comment_start(line: str) -> int:
    """找到行内注释的起始位置（忽略字符串内的 #）。返回 -1 表示无注释。"""
    in_str = None
    i = 0
    n = len(line)
    while i < n:
        ch = line[i]
        if in_str:
            if ch == '\\':
                i += 2
                continue
            if ch == in_str:
                in_str = None
            i += 1
            continue
        if ch in '"\'':
            in_str = ch
        elif ch == '#':
            return i
        i += 1
    return -1


def _replace_words(text: str, changes, user_names=None):
    user_names = user_names or set()

    def repl(m):
        w = m.group(0)
        # 用户自己定义的名字享有豁免权，绝不纠正
        if w in user_names:
            return w
        # 一级：特例表（人工确认过的，优先）
        if w in SPELLING_FIXES:
            correct = SPELLING_FIXES[w]
            changes.append((w, correct))
            return correct
        # 二级：编辑距离通用纠正
        r = fuzzy_correct(w)
        if r:
            correct, _dist = r
            changes.append((w, correct))
            return correct
        return w
    # (?<![.\w])：点后面的属性名不参与纠错。
    # 属性名是外部 API 的名字（f.close() / os.path.isfile(...)），
    # 和用户自定义名一样必须豁免——否则 close 会被 fuzzy 改成 class
    # （距离 2、首字母相同），连带把下一行语法弄崩。
    # \w 兜底：标识符中间的位置也永远不会被匹配。
    return re.sub(r'(?<![.\w])[A-Za-z_][A-Za-z0-9_]*', repl, text)


def format_welcome_fixed_msg(fix_count: int) -> str:
    """py++ 招牌提示语。"""
    return (f"{YELLOW}{BOLD}"
            f"[py++ 温馨提示] 检测到你的代码有 {fix_count} 处小错误，"
            f"并无大碍，py++ 已兼容运行。{RESET}")
