"""
py++ 语法分析器 (Parser)
把 Token 序列解析成 AST（抽象语法树）。
递归下降 + 运算符优先级爬升。
容错：遇到可修的语法错误时自动修补并记录。
"""

from dataclasses import dataclass, field
from typing import List, Optional, Any
from lexer import Token, Lexer, LexerError


# ---------- AST 节点 ----------
@dataclass
class Node:
    line: int = 0

@dataclass
class Program(Node):
    body: List[Node] = field(default_factory=list)

@dataclass
class Num(Node):
    value: Any = None
    is_float: bool = False

@dataclass
class Str(Node):
    value: str = ""

@dataclass
class Bool(Node):
    value: bool = False

@dataclass
class NoneLit(Node):
    pass

@dataclass
class Var(Node):
    name: str = ""

@dataclass
class BinOp(Node):
    op: str = ""
    left: Node = None
    right: Node = None

@dataclass
class UnaryOp(Node):
    op: str = ""
    operand: Node = None

@dataclass
class Compare(Node):
    op: str = ""
    left: Node = None
    right: Node = None

@dataclass
class BoolOp(Node):
    op: str = ""
    left: Node = None
    right: Node = None

@dataclass
class Call(Node):
    func: Node = None
    args: List[Node] = field(default_factory=list)

@dataclass
class Index(Node):
    obj: Node = None
    index: Node = None

@dataclass
class Attr(Node):
    obj: Node = None
    name: str = ""

@dataclass
class Assign(Node):
    target: Node = None
    value: Node = None

@dataclass
class AugAssign(Node):
    target: Node = None
    op: str = ""
    value: Node = None

@dataclass
class ExprStmt(Node):
    expr: Node = None

@dataclass
class If(Node):
    cond: Node = None
    body: List[Node] = field(default_factory=list)
    orelse: List[Node] = field(default_factory=list)

@dataclass
class While(Node):
    cond: Node = None
    body: List[Node] = field(default_factory=list)

@dataclass
class For(Node):
    var: str = ""
    iter: Node = None
    body: List[Node] = field(default_factory=list)

@dataclass
class FuncDef(Node):
    name: str = ""
    params: List[str] = field(default_factory=list)
    defaults: List[Node] = field(default_factory=list)
    body: List[Node] = field(default_factory=list)
    ret_type: str = "auto"

@dataclass
class Return(Node):
    value: Optional[Node] = None

@dataclass
class Break(Node):
    pass

@dataclass
class Continue(Node):
    pass

@dataclass
class Pass(Node):
    pass

@dataclass
class Import(Node):
    """import a、import a.b、import a as x、import a, b as c"""
    items: List[Any] = field(default_factory=list)   # [(module, alias)]


@dataclass
class FromImport(Node):
    """from a.b import c as d, e   /   from . import x   /   from a import *"""
    module: str = ""
    names: List[Any] = field(default_factory=list)   # [(name, alias)]，name='*' 表示全部
    level: int = 0                                   # 相对导入的点数


@dataclass
class ListLit(Node):
    items: List[Node] = field(default_factory=list)


@dataclass
class DictLit(Node):
    """{k: v, ...}"""
    keys: List[Node] = field(default_factory=list)
    values: List[Node] = field(default_factory=list)


@dataclass
class ClassDef(Node):
    """class Name:  —— 方法体存在 methods 里，类级赋值存在 attrs 里。"""
    name: str = ""
    bases: List[str] = field(default_factory=list)
    methods: List[Node] = field(default_factory=list)   # List[FuncDef]
    attrs: List[Node] = field(default_factory=list)     # List[Assign]


@dataclass
class ExternDecl(Node):
    """extern <ret> <name>(<type> <arg>, ...) —— 声明一个外部 C 函数。"""
    ret_type: str = "int"
    name: str = ""
    params: List[Any] = field(default_factory=list)     # [(ctype, argname)]


@dataclass
class RangeCall(Node):
    start: Node = None
    stop: Node = None
    step: Node = None


class ParseError(Exception):
    def __init__(self, msg, line):
        super().__init__(msg)
        self.msg = msg
        self.line = line


class Parser:
    def __init__(self, tokens: List[Token], repair_log=None):
        self.toks = tokens
        self.pos = 0
        self.repair_log = repair_log if repair_log is not None else []

    # ---------- 工具 ----------
    @property
    def cur(self) -> Token:
        return self.toks[self.pos]

    def peek(self, offset=0) -> Token:
        idx = min(self.pos + offset, len(self.toks) - 1)
        return self.toks[idx]

    def advance(self) -> Token:
        t = self.toks[self.pos]
        if self.pos < len(self.toks) - 1:
            self.pos += 1
        return t

    def check(self, ttype) -> bool:
        return self.cur.type == ttype

    def match(self, *ttypes) -> Optional[Token]:
        if self.cur.type in ttypes:
            return self.advance()
        return None

    def expect(self, ttype, what="") -> Token:
        if self.cur.type == ttype:
            return self.advance()
        # 容错：缺失记号 → 凭空补一个
        self.repair_log.append(('parse', self.cur.line, self.cur.col,
                                f"此处缺少 {what or ttype}，已自动补全，程序照常运行"))
        return Token(ttype, None, self.cur.line, self.cur.col)

    def skip_newlines(self):
        while self.check('NEWLINE'):
            self.advance()

    # ---------- 入口 ----------
    def parse(self) -> Program:
        body = self.parse_block(top=True)
        return Program(body=body, line=1)

    def parse_block(self, top=False) -> List[Node]:
        body = []
        self.skip_newlines()
        if not top:
            if self.match('INDENT') is None:
                # 期望缩进块但没缩进 → 容错：把它当成单行语句
                self.repair_log.append(('parse', self.cur.line, self.cur.col,
                                        "代码块缺少缩进，已自动识别"))
            else:
                body.append(self.parse_statement())
                while not self.check('DEDENT') and not self.check('EOF'):
                    self.skip_newlines()
                    if self.check('DEDENT') or self.check('EOF'):
                        break
                    body.append(self.parse_statement())
                self.match('DEDENT')
                return body
            # 单行块
            if not self.check('EOF') and not self.check('DEDENT'):
                body.append(self.parse_statement())
            return body
        else:
            while not self.check('EOF'):
                self.skip_newlines()
                if self.check('EOF'):
                    break
                body.append(self.parse_statement())
            return body

    # ---------- 语句 ----------
    def parse_statement(self) -> Node:
        t = self.cur
        if t.type == 'IF':
            return self.parse_if()
        if t.type == 'WHILE':
            return self.parse_while()
        if t.type == 'FOR':
            return self.parse_for()
        if t.type == 'DEF':
            return self.parse_func()
        if t.type == 'RETURN':
            return self.parse_return()
        if t.type == 'BREAK':
            self.advance(); self._end_stmt()
            return Break(line=t.line)
        if t.type == 'CONTINUE':
            self.advance(); self._end_stmt()
            return Continue(line=t.line)
        if t.type == 'PASS':
            self.advance(); self._end_stmt()
            return Pass(line=t.line)
        if t.type == 'CLASS':
            return self.parse_class()
        if t.type == 'EXTERN':
            return self.parse_extern()
        if t.type == 'IMPORT':
            return self.parse_import()
        if t.type == 'FROM':
            return self.parse_from_import()
        return self.parse_expr_statement()

    # ---------- extern（C 互操作） ----------
    # C 类型关键词：参数列表里最后一个标识符若属于此集合，说明它是类型
    # 而不是参数名（例如 `unsigned long` 的 'long'）。
    _C_TYPE_WORDS = {
        'void', 'int', 'char', 'short', 'long', 'float', 'double',
        'unsigned', 'signed', 'const', 'volatile', 'size_t', 'ssize_t',
        'wchar_t', 'bool', 'int8_t', 'int16_t', 'int32_t', 'int64_t',
        'uint8_t', 'uint16_t', 'uint32_t', 'uint64_t',
        'intptr_t', 'uintptr_t', 'str', 'ptr', 'cstr',
    }

    @staticmethod
    def _type_from_tokens(toks) -> str:
        parts = []
        for t in toks:
            if t.type == 'STAR':
                parts.append('*')
            elif t.type == 'IDENT':
                parts.append(str(t.value))
        return ' '.join(parts) if parts else 'void'

    def _parse_extern_param(self):
        """解析一个参数：'int x' / 'const char * s' / 'void' / 'unsigned long'（无名）。"""
        toks = []
        while (not self.check('COMMA') and not self.check('RPAREN')
               and not self.check('EOF')):
            toks.append(self.advance())
        name = ''
        if (len(toks) >= 2 and toks[-1].type == 'IDENT'
                and toks[-1].value not in self._C_TYPE_WORDS):
            name = toks.pop().value
        return self._type_from_tokens(toks), name

    def parse_extern(self) -> Node:
        """extern int puts(str s)  —— 声明一个外部 C 函数。"""
        line = self.cur.line
        self.advance()                       # extern
        toks = []
        while (not self.check('LPAREN') and not self.check('NEWLINE')
               and not self.check('EOF')):
            toks.append(self.advance())
        # 最后一个 IDENT 是函数名，前面的才是返回类型
        name = ''
        while toks and toks[-1].type != 'IDENT':
            toks.pop()
        if toks:
            name = toks.pop().value
        ret = self._type_from_tokens(toks)
        params = []
        if self.match('LPAREN'):
            while not self.check('RPAREN') and not self.check('EOF'):
                params.append(self._parse_extern_param())
                if not self.match('COMMA'):
                    break
            self.expect('RPAREN', ')')
        self._end_stmt()
        return ExternDecl(ret_type=ret, name=name, params=params, line=line)

    # ---------- import ----------
    def _parse_dotted_name(self) -> str:
        """解析 a.b.c 形式的点分名字。"""
        parts = [self.expect('IDENT', '模块名').value or '']
        while self.check('DOT'):
            self.advance()
            parts.append(self.expect('IDENT', '模块名').value or '')
        return '.'.join(parts)

    def parse_import(self) -> Node:
        line = self.cur.line
        self.advance()                       # 吃掉 import
        items = []
        while not self.check('NEWLINE') and not self.check('EOF'):
            mod = self._parse_dotted_name()
            alias = ''
            if self.check('AS'):
                self.advance()
                alias = self.expect('IDENT', '别名').value or ''
            if mod:
                items.append((mod, alias))
            if not self.match('COMMA'):
                break
        self._end_stmt()
        return Import(items=items, line=line)

    def parse_from_import(self) -> Node:
        line = self.cur.line
        self.advance()                       # 吃掉 from
        level = 0
        while self.check('DOT'):             # 相对导入：from . import x
            self.advance()
            level += 1
        mod = ''
        if not self.check('IMPORT'):
            mod = self._parse_dotted_name()
        self.expect('IMPORT', 'import')
        names = []
        paren = bool(self.match('LPAREN'))   # from a import (b, c)
        if self.check('STAR'):
            self.advance()
            names.append(('*', ''))
        else:
            while not self.check('NEWLINE') and not self.check('EOF'):
                if paren and self.check('RPAREN'):
                    break
                n = self.expect('IDENT', '名字').value or ''
                alias = ''
                if self.check('AS'):
                    self.advance()
                    alias = self.expect('IDENT', '别名').value or ''
                if n:
                    names.append((n, alias))
                if not self.match('COMMA'):
                    break
        if paren:
            self.expect('RPAREN', ')')
        self._end_stmt()
        return FromImport(module=mod, names=names, level=level, line=line)

    def _end_stmt(self):
        while self.check('SEMI'):
            self.advance()
        self.match('NEWLINE')

    def parse_if(self) -> Node:
        line = self.cur.line
        self.advance()  # if
        cond = self.parse_expr()
        self.expect('COLON', ':')
        body = self.parse_block()
        orelse = []
        # 处理 elif / else
        self.skip_newlines()
        while self.check('ELIF'):
            self.advance()
            c2 = self.parse_expr()
            self.expect('COLON', ':')
            b2 = self.parse_block()
            orelse = [If(cond=c2, body=b2, orelse=[], line=self.cur.line)]
            self.skip_newlines()
            if self.check('ELSE'):
                self.advance()
                self.expect('COLON', ':')
                orelse[0].orelse = self.parse_block()
            break
        else:
            if self.check('ELSE'):
                self.advance()
                self.expect('COLON', ':')
                orelse = self.parse_block()
        return If(cond=cond, body=body, orelse=orelse, line=line)

    def parse_while(self) -> Node:
        line = self.cur.line
        self.advance()
        cond = self.parse_expr()
        self.expect('COLON', ':')
        body = self.parse_block()
        return While(cond=cond, body=body, line=line)

    def parse_for(self) -> Node:
        line = self.cur.line
        self.advance()
        var = self.expect('IDENT', '循环变量').value or '_'
        self.expect('IN', 'in')
        it = self.parse_expr()
        self.expect('COLON', ':')
        body = self.parse_block()
        return For(var=var, iter=it, body=body, line=line)

    def parse_func(self) -> Node:
        line = self.cur.line
        self.advance()
        name = self.expect('IDENT', '函数名').value or 'anonymous'
        self.expect('LPAREN', '(')
        params, defaults = [], []
        while not self.check('RPAREN') and not self.check('EOF'):
            p = self.expect('IDENT', '参数名')
            if p.value:
                params.append(p.value)
            if self.match('ASSIGN'):
                defaults.append(self.parse_expr())
            else:
                defaults.append(None)
            if not self.match('COMMA'):
                break
        self.expect('RPAREN', ')')
        # 可选返回类型 ->int
        ret_type = 'auto'
        if self.match('ARROW'):
            rt = self.advance()
            ret_type = rt.value if rt.value in ('int', 'float', 'bool', 'str', 'auto') else 'auto'
        self.expect('COLON', ':')
        body = self.parse_block()
        return FuncDef(name=name, params=params, defaults=defaults,
                       body=body, ret_type=ret_type, line=line)

    def parse_class(self) -> Node:
        line = self.cur.line
        self.advance()                       # class
        name = self.expect('IDENT', '类名').value or 'Anonymous'
        bases = []
        if self.match('LPAREN'):             # class Foo(Base):
            while not self.check('RPAREN') and not self.check('EOF'):
                b = self.expect('IDENT', '基类名').value
                if b:
                    bases.append(b)
                if not self.match('COMMA'):
                    break
            self.expect('RPAREN', ')')
        self.expect('COLON', ':')
        body = self.parse_block()
        methods, attrs = [], []
        for stmt in body:
            if isinstance(stmt, FuncDef):
                methods.append(stmt)
            else:
                attrs.append(stmt)
        return ClassDef(name=name, bases=bases, methods=methods,
                        attrs=attrs, line=line)

    def parse_return(self) -> Node:
        line = self.cur.line
        self.advance()
        if self.check('NEWLINE') or self.check('EOF') or self.check('DEDENT'):
            self._end_stmt()
            return Return(value=None, line=line)
        val = self.parse_expr()
        self._end_stmt()
        return Return(value=val, line=line)

    # ---------- 表达式 ----------
    # 支持"无括号调用"的内置函数（Python2 风格）：print "x" / print x+y
    SUGAR_FUNCS = {'print', 'len', 'int', 'float', 'str', 'abs', 'min',
                   'max', 'sum', 'range', 'pow', 'input'}

    def parse_expr_statement(self) -> Node:
        line = self.cur.line
        # 处理 print 等内置函数的无括号写法
        if (self.cur.type == 'IDENT' and self.cur.value in self.SUGAR_FUNCS
                and self._is_bare_call()):
            return self._parse_sugar_call()

        target = self.parse_postfix_or_expr()
        # 赋值
        if self.check('ASSIGN'):
            self.advance()
            value = self.parse_expr()
            self._end_stmt()
            return Assign(target=target, value=value, line=line)
        if self.cur.type in ('PLUS_ASSIGN', 'MINUS_ASSIGN', 'STAR_ASSIGN',
                             'SLASH_ASSIGN', 'MOD_ASSIGN', 'POW_ASSIGN',
                             'FLOORDIV_ASSIGN'):
            op = self.advance()
            value = self.parse_expr()
            self._end_stmt()
            return AugAssign(target=target, op=op.type, value=value, line=line)
        self._end_stmt()
        return ExprStmt(expr=target, line=line)

    def _is_bare_call(self):
        """判断内置函数名后面是否跟了参数（而非 '=' 或 '('）。"""
        nxt = self.peek(1)
        if nxt.type in ('ASSIGN', 'LPAREN', 'NEWLINE', 'EOF', 'SEMI'):
            return False
        # 后跟运算符也不算无括号调用
        if nxt.type in ('PLUS', 'MINUS', 'STAR', 'SLASH', 'MOD', 'POW',
                        'FLOORDIV', 'EQ', 'NE', 'LT', 'GT', 'LE', 'GE',
                        'DOT', 'LBRACKET', 'COLON', 'COMMA'):
            return False
        return True

    def _parse_sugar_call(self):
        line = self.cur.line
        func = Var(name=self.advance().value, line=line)
        args = [self.parse_expr()]
        self._end_stmt()
        return ExprStmt(expr=Call(func=func, args=args, line=line), line=line)

    def parse_postfix_or_expr(self):
        """解析可作为赋值目标的表达式（变量名、下标），否则回退到完整表达式。"""
        # 赋值目标通常是单个 Var 或 Index；用完整表达式解析后再判断即可
        return self.parse_expr()

    def parse_expr(self):
        return self.parse_or()

    def parse_or(self):
        left = self.parse_and()
        while self.check('OR'):
            line = self.cur.line
            self.advance()
            right = self.parse_and()
            left = BoolOp(op='or', left=left, right=right, line=line)
        return left

    def parse_and(self):
        left = self.parse_not()
        while self.check('AND'):
            line = self.cur.line
            self.advance()
            right = self.parse_not()
            left = BoolOp(op='and', left=left, right=right, line=line)
        return left

    def parse_not(self):
        if self.check('NOT'):
            line = self.cur.line
            self.advance()
            return UnaryOp(op='not', operand=self.parse_not(), line=line)
        return self.parse_comparison()

    def parse_comparison(self):
        left = self.parse_arith()
        line = self.cur.line
        opmap = {'EQ': '==', 'NE': '!=', 'LT': '<', 'GT': '>', 'LE': '<=', 'GE': '>='}
        while True:
            if self.cur.type in opmap:
                op = opmap[self.advance().type]
                right = self.parse_arith()
                left = Compare(op=op, left=left, right=right, line=line)
            elif self.check('IN'):
                self.advance()
                right = self.parse_arith()
                left = Compare(op='in', left=left, right=right, line=line)
            elif self.check('NOT') and self.peek(1).type == 'IN':
                self.advance()
                self.advance()
                right = self.parse_arith()
                left = Compare(op='not in', left=left, right=right, line=line)
            else:
                break
        return left

    def parse_arith(self):
        left = self.parse_term()
        while self.check('PLUS') or self.check('MINUS'):
            line = self.cur.line
            op = self.advance().type
            right = self.parse_term()
            left = BinOp(op='+' if op == 'PLUS' else '-', left=left, right=right, line=line)
        return left

    def parse_term(self):
        left = self.parse_unary()
        while self.check('STAR') or self.check('SLASH') or self.check('MOD') or self.check('FLOORDIV'):
            line = self.cur.line
            op = self.advance().type
            right = self.parse_unary()
            sym = {'STAR': '*', 'SLASH': '/', 'MOD': '%', 'FLOORDIV': '//'}[op]
            left = BinOp(op=sym, left=left, right=right, line=line)
        return left

    def parse_unary(self):
        if self.check('MINUS'):
            line = self.cur.line
            self.advance()
            return UnaryOp(op='-', operand=self.parse_unary(), line=line)
        if self.check('PLUS'):
            self.advance()
            return self.parse_unary()
        return self.parse_power()

    def parse_power(self):
        base = self.parse_postfix()
        if self.check('POW'):
            line = self.cur.line
            self.advance()
            exp = self.parse_unary()
            return BinOp(op='**', left=base, right=exp, line=line)
        return base

    def parse_postfix(self):
        node = self.parse_atom()
        while True:
            if self.check('LPAREN'):
                line = self.cur.line
                self.advance()
                args = []
                while not self.check('RPAREN') and not self.check('EOF'):
                    args.append(self.parse_expr())
                    if not self.match('COMMA'):
                        break
                self.expect('RPAREN', ')')
                node = Call(func=node, args=args, line=line)
            elif self.check('LBRACKET'):
                line = self.cur.line
                self.advance()
                idx = self.parse_expr()
                self.expect('RBRACKET', ']')
                node = Index(obj=node, index=idx, line=line)
            elif self.check('DOT'):
                line = self.cur.line
                self.advance()
                name = self.expect('IDENT', '属性名').value or ''
                node = Attr(obj=node, name=name, line=line)
            else:
                break
        return node

    def parse_atom(self):
        t = self.cur
        if t.type == 'INT':
            self.advance()
            return Num(value=t.value, is_float=False, line=t.line)
        if t.type == 'FLOAT':
            self.advance()
            return Num(value=t.value, is_float=True, line=t.line)
        if t.type == 'STRING':
            self.advance()
            return Str(value=t.value, line=t.line)
        if t.type == 'TRUE':
            self.advance()
            return Bool(value=True, line=t.line)
        if t.type == 'FALSE':
            self.advance()
            return Bool(value=False, line=t.line)
        if t.type == 'NONE':
            self.advance()
            return NoneLit(line=t.line)
        if t.type == 'IDENT':
            self.advance()
            return Var(name=t.value, line=t.line)
        if t.type == 'LPAREN':
            self.advance()
            e = self.parse_expr()
            self.expect('RPAREN', ')')
            return e
        if t.type == 'LBRACKET':
            self.advance()
            items = []
            while not self.check('RBRACKET') and not self.check('EOF'):
                items.append(self.parse_expr())
                if not self.match('COMMA'):
                    break
            self.expect('RBRACKET', ']')
            return ListLit(items=items, line=t.line)
        if t.type == 'LBRACE':
            self.advance()
            keys, values = [], []
            while not self.check('RBRACE') and not self.check('EOF'):
                keys.append(self.parse_expr())
                self.expect('COLON', ':')
                values.append(self.parse_expr())
                if not self.match('COMMA'):
                    break
            self.expect('RBRACE', '}')
            return DictLit(keys=keys, values=values, line=t.line)
        # 容错：未知记号 → 当作 0 继续
        self.repair_log.append(('parse', t.line, t.col,
                                f"此处出现了无法理解的记号 {t.value!r}，已临时忽略"))
        self.advance()
        return Num(value=0, is_float=False, line=t.line)
