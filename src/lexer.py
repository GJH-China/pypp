"""
py++ 词法分析器 (Lexer)
把源代码字符串切分成 Token 序列。
支持缩进敏感（INDENT/DEDENT），这是 Python 风格语法的关键。
"""

from dataclasses import dataclass
from typing import List, Any


@dataclass
class Token:
    type: str
    value: Any
    line: int
    col: int

    def __repr__(self):
        return f"Token({self.type}, {self.value!r}, L{self.line}:{self.col})"


# 关键字
KEYWORDS = {
    'if': 'IF', 'else': 'ELSE', 'elif': 'ELIF', 'while': 'WHILE',
    'for': 'FOR', 'in': 'IN', 'def': 'DEF', 'return': 'RETURN',
    'break': 'BREAK', 'continue': 'CONTINUE', 'and': 'AND', 'or': 'OR',
    'not': 'NOT', 'True': 'TRUE', 'False': 'FALSE', 'None': 'NONE',
    'class': 'CLASS', 'import': 'IMPORT', 'from': 'FROM', 'as': 'AS',
    'pass': 'PASS', 'global': 'GLOBAL', 'extern': 'EXTERN',
}

# 多字符运算符（长的必须排在前面，优先匹配）
OPERATORS = [
    ('**=', 'POW_ASSIGN'), ('//=', 'FLOORDIV_ASSIGN'), ('==', 'EQ'),
    ('!=', 'NE'), ('<=', 'LE'), ('>=', 'GE'), ('+=', 'PLUS_ASSIGN'),
    ('-=', 'MINUS_ASSIGN'), ('*=', 'STAR_ASSIGN'), ('/=', 'SLASH_ASSIGN'),
    ('%=', 'MOD_ASSIGN'), ('**', 'POW'), ('//', 'FLOORDIV'), ('->', 'ARROW'),
    ('+', 'PLUS'), ('-', 'MINUS'), ('*', 'STAR'), ('/', 'SLASH'),
    ('%', 'MOD'), ('=', 'ASSIGN'), ('<', 'LT'), ('>', 'GT'),
    ('(', 'LPAREN'), (')', 'RPAREN'), ('[', 'LBRACKET'), (']', 'RBRACKET'),
    ('{', 'LBRACE'), ('}', 'RBRACE'), (',', 'COMMA'), (':', 'COLON'),
    ('.', 'DOT'), (';', 'SEMI'), ('@', 'AT'),
]


class LexerError(Exception):
    def __init__(self, msg, line, col):
        super().__init__(msg)
        self.msg = msg
        self.line = line
        self.col = col


class Lexer:
    def __init__(self, source: str, repair_log=None):
        # 统一换行符
        self.src = source.replace('\r\n', '\n').replace('\r', '\n')
        self.pos = 0
        self.line = 1
        self.col = 1
        self.tokens: List[Token] = []
        self.indent_stack = [0]
        self.paren_depth = 0
        self.repair_log = repair_log if repair_log is not None else []

    def _peek(self, offset=0):
        idx = self.pos + offset
        return self.src[idx] if idx < len(self.src) else '\0'

    def _advance(self):
        ch = self.src[self.pos]
        self.pos += 1
        if ch == '\n':
            self.line += 1
            self.col = 1
        else:
            self.col += 1
        return ch

    def tokenize(self) -> List[Token]:
        at_line_start = True
        while self.pos < len(self.src):
            if at_line_start and self.paren_depth == 0:
                # _handle_indent 返回 True 表示"该行是空行/注释行"，需要下一轮继续
                still_line_start = self._handle_indent()
                if not still_line_start:
                    at_line_start = False
                if self.pos >= len(self.src):
                    break
                if still_line_start:
                    continue
            ch = self._peek()

            # 空白
            if ch in ' \t':
                self._advance()
                continue
            # 注释
            if ch == '#':
                while self.pos < len(self.src) and self._peek() != '\n':
                    self._advance()
                continue
            # 换行
            if ch == '\n':
                self._advance()
                at_line_start = True
                continue

            # 字符串
            if ch in '"\'':
                self._read_string()
                continue

            # 数字
            if ch.isdigit() or (ch == '.' and self._peek(1).isdigit()):
                self._read_number()
                continue

            # 标识符/关键字
            if ch.isalpha() or ch == '_':
                self._read_identifier()
                continue

            # 运算符
            if self._read_operator():
                continue

            # 不认识 → 跳过并记录（容错）
            self.repair_log.append(('lex', self.line, self.col, f"忽略无法识别的字符 {ch!r}"))
            self._advance()

        # 收尾
        self._dedent_to(0)
        self.tokens.append(Token('NEWLINE', None, self.line, self.col))
        self.tokens.append(Token('EOF', None, self.line, self.col))
        return self.tokens

    def _handle_indent(self):
        """
        在行首计算缩进，生成 INDENT/DEDENT。
        返回 True  → 该行为空行/纯注释行（消费掉了，调用方需继续循环处理下一行）
        返回 False → 缩进已处理完毕，调用方可以开始读取该行内容
        """
        # 跳过行首空白，计算视觉宽度
        width = 0
        while self.pos < len(self.src) and self._peek() in ' \t':
            if self._peek() == '\t':
                width += 8 - (width % 8)
            else:
                width += 1
            self._advance()

        # 空行 / 纯注释行 → 整行消费掉
        if self.pos >= len(self.src) or self._peek() == '\n' or self._peek() == '#':
            if self._peek() == '#':
                while self.pos < len(self.src) and self._peek() != '\n':
                    self._advance()
            if self.pos < len(self.src) and self._peek() == '\n':
                self._advance()
            return True

        # 有实际内容 → 计算 INDENT/DEDENT
        cur = self.indent_stack[-1]
        if width > cur:
            self.indent_stack.append(width)
            self.tokens.append(Token('INDENT', width, self.line, 1))
        elif width < cur:
            self._dedent_to(width)
        return False

    def _dedent_to(self, width):
        while self.indent_stack[-1] > width:
            self.indent_stack.pop()
            self.tokens.append(Token('DEDENT', width, self.line, 1))
        if self.indent_stack[-1] != width:
            # 缩进不匹配 → 容错，调整栈
            self.repair_log.append(('lex', self.line, self.col,
                                    f"缩进量 {width} 与预期 {self.indent_stack[-1]} 不符，已自动对齐"))
            self.indent_stack[-1] = width

    def _read_string(self):
        quote = self._peek()
        line, col = self.line, self.col
        self._advance()
        buf = []
        while self.pos < len(self.src):
            ch = self._peek()
            if ch == '\\':
                self._advance()
                esc = self._advance()
                mapping = {'n': '\n', 't': '\t', 'r': '\r', '\\': '\\',
                           '"': '"', "'": "'", '0': '\0'}
                buf.append(mapping.get(esc, esc))
                continue
            if ch == quote:
                self._advance()
                self.tokens.append(Token('STRING', ''.join(buf), line, col))
                return
            if ch == '\n':
                # 未闭合字符串 → 容错：在行尾自动闭合
                self.repair_log.append(('lex', line, col, "字符串未闭合，已自动补上引号"))
                self.tokens.append(Token('STRING', ''.join(buf), line, col))
                return
            buf.append(self._advance())
        self.tokens.append(Token('STRING', ''.join(buf), line, col))

    def _read_number(self):
        line, col = self.line, self.col
        buf = []
        is_float = False
        while self.pos < len(self.src) and (self._peek().isdigit() or self._peek() == '.'):
            if self._peek() == '.':
                if is_float:
                    break
                # 检查是不是范围或属性 (如 1..) —— 简单处理
                if not self._peek(1).isdigit():
                    break
                is_float = True
            buf.append(self._advance())
        # 浮点后缀 f
        if self._peek() == 'f' and not is_float:
            is_float = True
            self._advance()
        text = ''.join(buf)
        if is_float:
            self.tokens.append(Token('FLOAT', float(text), line, col))
        else:
            self.tokens.append(Token('INT', int(text), line, col))

    def _read_identifier(self):
        line, col = self.line, self.col
        buf = []
        while self.pos < len(self.src) and (self._peek().isalnum() or self._peek() == '_'):
            buf.append(self._advance())
        name = ''.join(buf)
        ttype = KEYWORDS.get(name, 'IDENT')
        self.tokens.append(Token(ttype, name, line, col))

    def _read_operator(self):
        for op, ttype in OPERATORS:
            if self.src.startswith(op, self.pos):
                line, col = self.line, self.col
                for _ in range(len(op)):
                    self._advance()
                if ttype in ('LPAREN', 'LBRACKET', 'LBRACE'):
                    self.paren_depth += 1
                elif ttype in ('RPAREN', 'RBRACKET', 'RBRACE'):
                    self.paren_depth = max(0, self.paren_depth - 1)
                self.tokens.append(Token(ttype, op, line, col))
                return True
        return False
