"""
py++ 代码生成器 (CodeGen)
把 AST 编译成 C 源码，交给 gcc -O2 编译 —— 这是达到接近 C++ 速度的关键路径。

设计：
  - int   -> long long
  - float -> double
  - str   -> pypp_str（轻量 C 字符串结构）
  - list  -> pypp_list（带边界的动态数组）
  - 类型在生成时推断，避免运行时装箱

关键性能守则（编译期智能 / 运行期零开销）：
  1. range() 循环编译为原生 for，不产生任何运行时数组
  2. 变量类型一旦推断出来，后续运算全部走 C 原生指令
  3. 容错检查（越界、除零）在编译期处理，不进入循环体
  4. **没写 import 的程序，生成的 C 与"纯原生"完全一致**——
     不会引入 Python.h、不会调用 Py_Initialize，速度承诺不被稀释。
     Python 互操作只在你真正用到它时才付费。

Python 互操作（import）：
  引入一个动态类型 pyobj（即 PyObject*）。凡是来自 Python 库的值——
  模块、函数、返回对象——都用它承载；对它的属性访问 / 调用 / 下标
  翻译成 CPython C-API 调用。py++ 自己的数值运算仍然是纯 C 指令。
"""

from parser import *


# 内置名：这些名字即使被"引用"也不该被当作未定义变量补全
BUILTIN_NAMES = {
    'print', 'input', 'int', 'float', 'str', 'len', 'abs', 'min', 'max',
    'sum', 'range', 'pow', 'True', 'False', 'None',
}


class CodeGenError(Exception):
    pass


class NeedsVMError(CodeGenError):
    """当前程序无法用原生后端表达（例如 from x import *），需要退回 VM。"""


class CGenerator:
    def __init__(self):
        self.tmp_count = 0
        self.var_types = {}          # 全局变量类型
        self.func_types = {}         # 函数名 -> 返回类型
        self.scopes = []             # 作用域栈: list[dict]
        self.func_returns = {}       # 函数真实返回类型
        # 记录编译期补全过的未定义变量，供上层生成"温馨提示"
        self.completed_vars = []
        # ---- Python 互操作 ----
        self.uses_python = False     # 是否用到了 import（决定要不要注入 Python 桥接层）
        self._c_declared = set()     # 已经用 PyObject* 声明过的名字
        # ---- 类 ----
        self.classes = {}            # 类名 -> {'fields':{名:类型}, 'methods':{名:FuncDef}}
        self.ctor_hints = {}         # 类名 -> {参数下标: 类型}（从构造点推断）
        self.method_hints = {}       # (类名, 方法名) -> {参数下标: 类型}
        # ---- C 互操作 ----
        self.externs = {}            # 函数名 -> (返回类型, [(参数类型, 参数名)])

    # ---------- 类型推断 ----------
    def infer_type(self, node) -> str:
        if isinstance(node, Num):
            return 'double' if node.is_float else 'long long'
        if isinstance(node, Str):
            return 'str'
        if isinstance(node, Bool):
            return 'bool'
        if isinstance(node, NoneLit):
            return 'long long'
        if isinstance(node, Var):
            return self.lookup_type(node.name)
        if isinstance(node, ListLit):
            return 'list:' + self._list_etype(node)
        if isinstance(node, DictLit):
            return 'dict'
        if isinstance(node, BinOp):
            lt, rt = self.infer_type(node.left), self.infer_type(node.right)
            if lt == 'dyn' or rt == 'dyn':
                return 'dyn'
            if node.op in ('+',) and (lt == 'str' or rt == 'str'):
                return 'str'
            if node.op == '//':
                return 'long long'
            if lt == 'double' or rt == 'double' or node.op == '/':
                return 'double'
            return 'long long'
        if isinstance(node, UnaryOp):
            return 'bool' if node.op == 'not' else self.infer_type(node.operand)
        if isinstance(node, Compare):
            return 'bool'
        if isinstance(node, BoolOp):
            lt, rt = self.infer_type(node.left), self.infer_type(node.right)
            if node.op == 'or' and lt == 'str' and rt == 'str':
                return 'str'
            return 'bool'
        if isinstance(node, Call):
            return self._call_type(node)
        if isinstance(node, Index):
            ot = self.infer_type(node.obj)
            if ot == 'dict':
                return 'dyn'
            if ot and ot.startswith('list:'):
                return ot[5:]
            if ot == 'pyobj':
                return 'pyobj'
            return 'long long'
        if isinstance(node, Attr):
            ot = self.infer_type(node.obj)
            if ot == 'pyobj':
                return 'pyobj'
            if ot and ot.startswith('obj:'):
                info = self.classes.get(ot[4:])
                if info:
                    return info['fields'].get(node.name, 'long long')
            return 'auto'
        return 'auto'

    def _call_type(self, node: Call) -> str:
        if isinstance(node.func, Var):
            name = node.func.name
            if name in self.externs:
                return self._extern_pytype(self.externs[name][0])
            if name in self.classes:
                return 'obj:' + name
            if self.lookup_type(name) == 'pyobj':
                return 'pyobj'
            if name in self.func_returns:
                return self.func_returns[name]
            builtin = {'input': 'str', 'str': 'str', 'int': 'long long',
                       'float': 'double', 'len': 'long long', 'abs': 'long long',
                       'min': 'long long', 'max': 'long long', 'sum': 'long long',
                       'pow': 'long long', 'range': 'list'}
            return builtin.get(name, 'long long')
        if isinstance(node.func, Attr):
            ot = self.infer_type(node.func.obj)
            if ot == 'pyobj':
                return 'pyobj'
            if ot and ot.startswith('obj:'):
                cls = ot[4:]
                info = self.classes.get(cls)
                if info and node.func.name in info['methods']:
                    m = info['methods'][node.func.name]
                    return self._find_return_type(m.body)
                if node.func.name == 'get':
                    return 'dyn'
            if node.func.name in ('upper', 'lower', 'strip'):
                return 'str'
            if node.func.name == 'split':
                return 'list'
            if node.func.name == 'get':
                return 'dyn'
        return 'long long'

    def lookup_type(self, name):
        for scope in reversed(self.scopes):
            if name in scope:
                return scope[name]
        return self.var_types.get(name, 'auto')

    def declare_var(self, name, ttype):
        if self.scopes:
            self.scopes[-1][name] = ttype
        else:
            self.var_types[name] = ttype

    def new_tmp(self):
        self.tmp_count += 1
        return f"_t{self.tmp_count}"

    def ctype(self, t):
        if t and t.startswith('obj:'):
            return t[4:]                       # 类实例直接映射为同名 C 结构体
        if t and t.startswith('list'):
            return 'pypp_list'                 # list:str / list:double / list:long long
        return {'long long': 'long long', 'double': 'double', 'str': 'pypp_str',
                'list': 'pypp_list', 'dict': 'pypp_dict', 'dyn': 'pypp_val',
                'auto': 'pypp_val', 'pyobj': 'PyObject*', 'ptr': 'void*',
                'void': 'void',
                'bool': 'long long', 'int': 'long long',
                'float': 'double'}.get(t, 'pypp_val')

    # ---------- 主入口 ----------
    def generate(self, program: Program) -> str:
        # 先登记所有 import 绑定的名字为 pyobj 类型，
        # 这样函数体里引用它们时也能推断出正确类型（函数先生成）。
        imported = self._collect_imports(program.body)
        for name in imported:
            self.var_types[name] = 'pyobj'
        if imported:
            self.uses_python = True

        self.externs = self._collect_externs(program)

        # 类的处理顺序很关键：
        #   1) 先登记类名（后面的扫描要知道哪些名字是类）
        #   2) 扫描调用点，推断构造/方法参数类型
        #   3) 再从 __init__ 归纳出字段及其类型
        for stmt in program.body:
            if isinstance(stmt, ClassDef):
                self.classes[stmt.name] = {'fields': {}, 'methods': {}}
        self._scan_hints(program)
        for stmt in program.body:
            if isinstance(stmt, ClassDef):
                self._register_class(stmt)

        for stmt in program.body:
            if isinstance(stmt, FuncDef):
                self.func_types[stmt.name] = (
                    stmt.ret_type if stmt.ret_type != 'auto' else 'long long')
        self._infer_func_returns(program)

        extern_defs = self._gen_externs()
        class_defs = self._gen_classes(program)
        func_defs = self._gen_functions(program)
        main_body = self._gen_main(program)
        header = self.runtime_header(self.uses_python)
        return "\n".join(
            p for p in (header, extern_defs, class_defs, func_defs, main_body)
            if p) + "\n"

    def _collect_imports(self, body) -> dict:
        """递归收集 import 绑定的名字 → 模块名。"""
        out = {}

        def walk(stmts):
            for s in stmts:
                if isinstance(s, Import):
                    for mod, alias in s.items:
                        if mod:
                            out[alias or mod.split('.')[0]] = mod
                elif isinstance(s, FromImport):
                    for n, a in s.names:
                        if n != '*':
                            out[a or n] = s.module
                elif isinstance(s, If):
                    walk(s.body); walk(s.orelse)
                elif isinstance(s, (While, For)):
                    walk(s.body)
                elif isinstance(s, FuncDef):
                    walk(s.body)

        walk(body)
        return out

    # ---------- 类：静态分析 ----------
    @staticmethod
    def _ast_children(node):
        """通用子节点遍历（AST 都是 dataclass，直接反射字段）。"""
        if not isinstance(node, Node):
            return []
        out = []
        for fname in node.__dataclass_fields__:
            v = getattr(node, fname, None)
            if isinstance(v, Node):
                out.append(v)
            elif isinstance(v, list):
                out.extend(x for x in v if isinstance(x, Node))
        return out

    def _literal_type(self, node):
        """只在能明确判断时给出类型；无法判断返回 None（不猜）。"""
        if isinstance(node, Num):
            return 'double' if node.is_float else 'long long'
        if isinstance(node, Str):
            return 'str'
        if isinstance(node, Bool):
            return 'long long'
        if isinstance(node, ListLit):
            return 'list'
        if isinstance(node, DictLit):
            return 'dict'
        return None

    def _scan_hints(self, program):
        """
        扫描全程序的调用点，推断构造参数与方法参数的类型。

        为什么需要这一步：`def __init__(self, name)` 里的 name 本身看不出
        类型，但 `Person("py++")` 这个调用点能看出是字符串。没有它，
        `self.name` 会被当成整数，字符串拼接就错了。
        """
        self.ctor_hints = {}
        self.method_hints = {}
        var_cls = {}

        def arg_type(a):
            t = self._literal_type(a)
            if t:
                return t
            if isinstance(a, Var):
                return var_cls.get(a.name)
            return None

        def walk(node):
            if node is None:
                return
            if isinstance(node, Assign) and isinstance(node.target, Var) \
                    and isinstance(node.value, Call) \
                    and isinstance(node.value.func, Var) \
                    and node.value.func.name in self.classes:
                var_cls[node.target.name] = node.value.func.name
            if isinstance(node, Call):
                f = node.func
                if isinstance(f, Var) and f.name in self.classes:
                    d = self.ctor_hints.setdefault(f.name, {})
                    for i, a in enumerate(node.args):
                        t = arg_type(a)
                        if t and i not in d:
                            d[i] = t
                elif isinstance(f, Attr):
                    cls = None
                    if isinstance(f.obj, Var):
                        cls = var_cls.get(f.obj.name)
                    if cls and cls in self.classes:
                        d = self.method_hints.setdefault((cls, f.name), {})
                        for i, a in enumerate(node.args):
                            t = arg_type(a)
                            if t and i not in d:
                                d[i] = t
            for ch in self._ast_children(node):
                walk(ch)

        for stmt in program.body:
            walk(stmt)

    def _collect_self_assigns(self, body, out):
        """收集 self.<名字> = ... 里的字段名。"""
        for s in body:
            if isinstance(s, Assign) and isinstance(s.target, Attr) \
                    and isinstance(s.target.obj, Var) \
                    and s.target.obj.name == 'self':
                if s.target.name not in out:
                    out[s.target.name] = None
            elif isinstance(s, If):
                self._collect_self_assigns(s.body, out)
                self._collect_self_assigns(s.orelse, out)
            elif isinstance(s, (While, For)):
                self._collect_self_assigns(s.body, out)
            elif isinstance(s, FuncDef):
                self._collect_self_assigns(s.body, out)

    def _register_class(self, cd: ClassDef):
        info = self.classes.setdefault(cd.name, {'fields': {}, 'methods': {}})
        info['methods'] = {m.name: m for m in cd.methods}

        # 字段名
        names = {}
        for m in cd.methods:
            self._collect_self_assigns(m.body, names)
        for a in cd.attrs:
            if isinstance(a, Assign) and isinstance(a.target, Var):
                names.setdefault(a.target.name, None)

        # 字段类型：优先看 __init__ 里的赋值
        fields = {}
        init = info['methods'].get('__init__')
        ptypes = {}
        if init:
            params = [p for p in init.params if p != 'self']
            hints = self.ctor_hints.get(cd.name, {})
            for i, p in enumerate(params):
                if i in hints:
                    ptypes[p] = hints[i]
        for fname in names:
            fields[fname] = 'long long'
        if init:
            for s in init.body:
                if isinstance(s, Assign) and isinstance(s.target, Attr) \
                        and isinstance(s.target.obj, Var) \
                        and s.target.obj.name == 'self':
                    t = self._literal_type(s.value)
                    if t is None and isinstance(s.value, Var):
                        t = ptypes.get(s.value.name)
                    if t is None:
                        t = fields.get(s.target.name) or 'long long'
                    fields[s.target.name] = t
        info['fields'] = fields

    # ---------- 类：代码生成 ----------
    def _gen_classes(self, program) -> str:
        chunks = []
        for stmt in program.body:
            if isinstance(stmt, ClassDef):
                chunks.append(self.gen_classdef(stmt))
        return "\n".join(c for c in chunks if c)

    def gen_classdef(self, cd: ClassDef) -> str:
        if cd.bases:
            raise NeedsVMError(
                f"类继承暂不支持（class {cd.name}({cd.bases[0]})）")
        info = self.classes.get(cd.name, {'fields': {}, 'methods': {}})
        fields = info['fields']
        lines = [f"/* ===== class {cd.name} ===== */", "typedef struct {"]
        if fields:
            for fname, ftype in fields.items():
                lines.append(f"    {self.ctype(ftype)} {fname};")
        else:
            lines.append("    char _pypp_empty;")
        lines.append(f"}} {cd.name};")

        for m in cd.methods:
            lines.append(self._gen_method(cd, m))
        lines.append(self._gen_ctor(cd))
        return "\n".join(lines)

    def _gen_method(self, cd: ClassDef, m: FuncDef) -> str:
        rt = self._find_return_type(m.body)
        cret = self.ctype(rt)
        # __init__ 的参数类型要从"构造点"推断，而不是从方法调用点
        if m.name == '__init__':
            hints = dict(self.ctor_hints.get(cd.name, {}))
        else:
            hints = self.method_hints.get((cd.name, m.name), {})
        params = []
        idx = 0
        for p in m.params:
            if p == 'self':
                params.append(f"{cd.name}* self")
                continue
            t = hints.get(idx, 'long long')
            params.append(f"{self.ctype(t)} {p}")
            idx += 1
        if not any(p == 'self' for p in m.params):
            params.insert(0, f"{cd.name}* self")
        plist = ', '.join(params) if params else f"{cd.name}* self"

        self.scopes.append({'self': 'obj:' + cd.name})
        lines = [f"static {cret} {cd.name}_{m.name}({plist}) {{"]
        for stmt in m.body:
            lines.extend(self._indent(self.gen_stmt(stmt), 1))
        lines.append("}")
        self.scopes.pop()
        return "\n".join(lines)

    def _gen_ctor(self, cd: ClassDef) -> str:
        info = self.classes.get(cd.name, {'fields': {}, 'methods': {}})
        init = info['methods'].get('__init__')
        if not init:
            return (f"static {cd.name} {cd.name}_new(void) {{ "
                    f"{cd.name} _self; memset(&_self, 0, sizeof(_self)); "
                    f"return _self; }}")
        hints = self.ctor_hints.get(cd.name, {})
        params = []
        idx = 0
        for p in init.params:
            if p == 'self':
                continue
            t = hints.get(idx, 'long long')
            params.append(f"{self.ctype(t)} {p}")
            idx += 1
        plist = ', '.join(params) if params else 'void'

        self.scopes.append({'self': 'obj:' + cd.name})
        lines = [f"static {cd.name} {cd.name}_new({plist}) {{",
                 f"    {cd.name} _self;",
                 "    memset(&_self, 0, sizeof(_self));",
                 f"    {cd.name}* self = &_self;"]
        for stmt in init.body:
            lines.extend(self._indent(self.gen_stmt(stmt), 1))
        lines.append("    return _self;")
        lines.append("}")
        self.scopes.pop()
        return "\n".join(lines)

    # ---------- extern（C 互操作） ----------
    def _extern_ctype(self, t: str) -> str:
        """
        C 类型名 → extern 原型里使用的 C 类型。

        这里刻意**保留原始宽度**（int 就是 int，不是 long long）：
        C 的 int 是 32 位，若声明成 64 位，函数返回时高 32 位是未定义的
        （x64 调用约定只保证 EAX 有效），会读到垃圾值。
        声明准确宽度后，参数/返回值的转换交给 C 编译器处理。
        """
        low = (t or 'int').strip().lower()
        low = low.replace('const', ' ')
        if '*' in low:
            base = ' '.join(low.replace('*', ' ').split())
            if base in ('char', 'unsigned char', ''):
                return 'const char*'
            return 'void*'
        low = ' '.join(low.split())
        table = {
            'void': 'void', 'int': 'int', 'long': 'long', 'short': 'short',
            'char': 'char', 'unsigned': 'unsigned int',
            'unsigned int': 'unsigned int', 'unsigned long': 'unsigned long',
            'unsigned short': 'unsigned short', 'unsigned char': 'unsigned char',
            'signed char': 'signed char', 'long long': 'long long',
            'unsigned long long': 'unsigned long long',
            'size_t': 'size_t', 'ssize_t': 'long long', 'wchar_t': 'wchar_t',
            'long double': 'double', 'int8_t': 'signed char',
            'int16_t': 'short', 'int32_t': 'int', 'int64_t': 'long long',
            'uint8_t': 'unsigned char', 'uint16_t': 'unsigned short',
            'uint32_t': 'unsigned int', 'uint64_t': 'unsigned long long',
            'intptr_t': 'long long', 'uintptr_t': 'unsigned long long',
            'float': 'float', 'double': 'double', 'bool': 'int',
            'str': 'const char*', 'cstr': 'const char*',
            'ptr': 'void*', 'void*': 'void*',
        }
        return table.get(low, 'long long')

    @staticmethod
    def _extern_is_unsigned(t: str) -> bool:
        low = (t or '').lower()
        if 'unsigned' in low:
            return True
        return ' '.join(low.split()) in ('size_t', 'uint8_t', 'uint16_t',
                                         'uint32_t', 'uint64_t', 'uintptr_t')

    def _extern_pytype(self, t: str) -> str:
        """C 类型名 → py++ 侧类型。"""
        low = (t or 'int').strip().lower()
        if '*' in low or low in ('ptr', 'void*'):
            return 'ptr'
        low = ' '.join(low.replace('const', ' ').split())
        if low in ('float', 'double', 'long double'):
            return 'double'
        if low in ('str', 'cstr'):
            return 'str'
        if low == 'void':
            return 'void'
        return 'long long'

    def _collect_externs(self, program) -> dict:
        out = {}
        for stmt in program.body:
            if isinstance(stmt, ExternDecl) and stmt.name:
                out[stmt.name] = (stmt.ret_type, stmt.params)
        return out

    def _gen_externs(self) -> str:
        if not self.externs:
            return ''
        lines = ["/* ===== extern C 函数声明 ===== */"]
        for name, (ret, params) in self.externs.items():
            cret = self._extern_ctype(ret)
            if params:
                plist = ', '.join(self._extern_ctype(pt) for pt, _ in params)
            else:
                plist = 'void'
            lines.append(f"extern {cret} {name}({plist});")
        return "\n".join(lines)

    def _gen_extern_arg(self, node) -> str:
        """把 py++ 实参转成 C 能接受的表达式（数值转换交给 C 编译器）。"""
        t = self.infer_type(node)
        e = self.gen_expr(node)
        if t == 'str':
            return f"pypp_str_cstr({e})"
        if t == 'pyobj':
            return f"PyUnicode_AsUTF8({e})"
        return e

    def _gen_extern_call(self, name: str, argnodes) -> str:
        ret, params = self.externs[name]
        args = [self._gen_extern_arg(a) for a in argnodes]
        call = f"{name}({', '.join(args)})"
        pt = self._extern_pytype(ret)
        if pt == 'str':
            return f"pypp_s({call})"
        if pt == 'ptr':
            return f"((void*)({call}))"
        if pt == 'void':
            return f"((void)({call}), 0)"
        if pt == 'double':
            return f"((double)({call}))"
        # 无符号返回要零扩展，否则高位会被符号扩展成负数
        if self._extern_is_unsigned(ret):
            return f"((long long)(unsigned long long)({call}))"
        return f"((long long)({call}))"

    def _infer_func_returns(self, program):
        for stmt in program.body:
            if isinstance(stmt, FuncDef):
                rt = stmt.ret_type
                if rt == 'auto':
                    rt = self._find_return_type(stmt.body)
                self.func_returns[stmt.name] = rt

    def _find_return_type(self, body):
        for stmt in body:
            if isinstance(stmt, Return) and stmt.value is not None:
                t = self.infer_type(stmt.value)
                if t != 'auto':
                    return t
            if isinstance(stmt, If):
                t = (self._find_return_type(stmt.body)
                     or self._find_return_type(stmt.orelse))
                if t:
                    return t
            if isinstance(stmt, (While, For)):
                t = self._find_return_type(stmt.body)
                if t:
                    return t
        return 'long long'

    def _gen_functions(self, program) -> str:
        chunks = []
        for stmt in program.body:
            if isinstance(stmt, FuncDef):
                chunks.append(self.gen_funcdef(stmt))
        return "\n".join(chunks)

    def _gen_main(self, program) -> str:
        self.scopes = [{}]
        # 编译期补全：先扫描本层用到的、但从未赋值的变量，补一个 0 初值
        # 这样"未定义变量"不会让原生编译失败（零开销容错）
        imported = self._collect_imports(program.body)
        declared = self._collect_assigned_names(program.body)
        declared |= set(imported.keys())      # import 进来的名字不算"未定义"
        referenced = set()
        for stmt in program.body:
            if not isinstance(stmt, (FuncDef, ClassDef, ExternDecl)):
                self._collect_referenced(stmt, referenced)
        undeclared = {n for n in (referenced - declared)
                      if not n.startswith('__skip__')}

        head = []
        for name in sorted(undeclared):
            if name in self.func_types or name in BUILTIN_NAMES:
                continue
            if name in self.classes or name in self.externs:
                continue                     # 类名 / extern 函数名不是变量
            self.scopes[0][name] = 'long long'
            self.completed_vars.append(name)
            head.append(f"    long long {name} = 0;  /* 编译期自动补全 */")

        # 先生成语句体（这一步会决定 self.uses_python），再组装 main
        body = []
        for stmt in program.body:
            if isinstance(stmt, (FuncDef, ClassDef, ExternDecl)):
                continue
            body.extend(self._indent(self.gen_stmt(stmt), 1))

        lines = ["int main(void) {"]
        if self.uses_python:
            lines.append("    pypp_py_init();")
        lines.extend(head)
        lines.extend(body)
        if self.uses_python:
            lines.append("    fflush(stdout);")
            lines.append("    pypp_py_finalize();")
        lines.append("    return 0;")
        lines.append("}")
        self.scopes = []
        return "\n".join(lines)

    def _collect_assigned_names(self, body) -> set:
        """收集所有被赋值的变量名（视作已定义）。"""
        names = set()
        def walk(stmts):
            for s in stmts:
                if isinstance(s, Assign) and isinstance(s.target, Var):
                    names.add(s.target.name)
                elif isinstance(s, AugAssign) and isinstance(s.target, Var):
                    names.add(s.target.name)
                elif isinstance(s, For):
                    names.add(s.var)
                    walk(s.body)
                elif isinstance(s, If):
                    walk(s.body); walk(s.orelse)
                elif isinstance(s, (While,)):
                    walk(s.body)
                elif isinstance(s, FuncDef):
                    walk(s.body)
        walk(body)
        return names

    def _collect_referenced(self, node, out: set):
        """收集语句中引用到的所有变量名。"""
        if node is None:
            return
        if isinstance(node, Var):
            out.add(node.name)
            return
        if isinstance(node, (Num, Str, Bool, NoneLit, Pass, Break, Continue)):
            return
        if isinstance(node, BinOp):
            self._collect_referenced(node.left, out)
            self._collect_referenced(node.right, out)
        elif isinstance(node, UnaryOp):
            self._collect_referenced(node.operand, out)
        elif isinstance(node, Compare):
            self._collect_referenced(node.left, out)
            self._collect_referenced(node.right, out)
        elif isinstance(node, BoolOp):
            self._collect_referenced(node.left, out)
            self._collect_referenced(node.right, out)
        elif isinstance(node, Call):
            if isinstance(node.func, Var):
                out.add(f"__skip__{node.func.name}")   # 函数名不算变量
            elif isinstance(node.func, Attr):
                self._collect_referenced(node.func.obj, out)
            for a in node.args:
                self._collect_referenced(a, out)
        elif isinstance(node, Index):
            self._collect_referenced(node.obj, out)
            self._collect_referenced(node.index, out)
        elif isinstance(node, Attr):
            self._collect_referenced(node.obj, out)
        elif isinstance(node, ListLit):
            for it in node.items:
                self._collect_referenced(it, out)
        elif isinstance(node, DictLit):
            for it in node.keys:
                self._collect_referenced(it, out)
            for it in node.values:
                self._collect_referenced(it, out)
        elif isinstance(node, Assign):
            self._collect_referenced(node.value, out)
            self._collect_referenced(node.target, out)
        elif isinstance(node, AugAssign):
            self._collect_referenced(node.target, out)
            self._collect_referenced(node.value, out)
        elif isinstance(node, ExprStmt):
            self._collect_referenced(node.expr, out)
        elif isinstance(node, Return):
            self._collect_referenced(node.value, out)
        elif isinstance(node, If):
            self._collect_referenced(node.cond, out)
            for s in node.body:
                self._collect_referenced(s, out)
            for s in node.orelse:
                self._collect_referenced(s, out)
        elif isinstance(node, While):
            self._collect_referenced(node.cond, out)
            for s in node.body:
                self._collect_referenced(s, out)
        elif isinstance(node, For):
            self._collect_referenced(node.iter, out)
            for s in node.body:
                self._collect_referenced(s, out)

    @staticmethod
    def _indent(lines, level):
        pad = "    " * level
        return [(pad + ln) if ln.strip() else ln for ln in lines]

    # ---------- 语句生成：返回"未缩进"的行列表 ----------
    def gen_stmt(self, node) -> List[str]:
        if isinstance(node, Import):
            return self.gen_import(node)
        if isinstance(node, FromImport):
            return self.gen_from_import(node)
        if isinstance(node, ClassDef):
            return []          # 类已在文件顶层生成，main 里无需再出现
        if isinstance(node, ExternDecl):
            return []          # extern 已生成原型
        if isinstance(node, FuncDef):
            return [self.gen_funcdef(node)]
        if isinstance(node, If):
            return self.gen_if(node)
        if isinstance(node, While):
            return self.gen_while(node)
        if isinstance(node, For):
            return self.gen_for(node)
        if isinstance(node, Assign):
            return self.gen_assign(node)
        if isinstance(node, AugAssign):
            return self.gen_augassign(node)
        if isinstance(node, ExprStmt):
            return self.gen_expr_stmt(node)
        if isinstance(node, Return):
            return self.gen_return(node)
        if isinstance(node, Break):
            return ["break;"]
        if isinstance(node, Continue):
            return ["continue;"]
        if isinstance(node, Pass):
            return [";"]
        return [";"]

    def gen_block(self, body) -> List[str]:
        """生成一个缩进块（相对当前层 +1），内部处理作用域。"""
        self.scopes.append({})
        out = []
        for stmt in body:
            out.extend(self._indent(self.gen_stmt(stmt), 1))
        self.scopes.pop()
        return out

    # ---------- import ----------
    def _decl_py(self, name: str, init: str) -> str:
        """声明（或复用）一个 PyObject* 变量。"""
        self.uses_python = True
        self.declare_var(name, 'pyobj')
        if name in self._c_declared:
            return f"{name} = {init};"
        self._c_declared.add(name)
        return f"PyObject* {name} = {init};"

    def gen_import(self, node: Import) -> List[str]:
        """import a / import a.b as c —— 与 CPython 一致：import a.b 绑定顶层名 a。"""
        lines = []
        for mod, alias in node.items:
            if not mod:
                continue
            if alias:
                bind, target = alias, mod
            else:
                bind = mod.split('.')[0]
                target = bind
            lines.append(self._decl_py(bind, f'pypp_import("{target}")'))
        return lines

    def gen_from_import(self, node: FromImport) -> List[str]:
        """from a import b as c（含子模块：from PIL import Image）"""
        if any(n == '*' for n, _ in node.names):
            raise NeedsVMError("'from ... import *' 无法在编译期确定名字")
        mod = ('.' * node.level) + node.module
        lines = []
        for name, alias in node.names:
            bind = alias or name
            lines.append(self._decl_py(
                bind, f'pypp_import_from("{mod}", "{name}")'))
        return lines

    def gen_funcdef(self, node: FuncDef) -> str:
        rt = self.func_returns.get(node.name, 'long long')
        cret = self.ctype(rt)
        params = []
        for i, p in enumerate(node.params):
            if i < len(node.defaults) and node.defaults[i] is not None:
                dt = self.infer_type(node.defaults[i])
                params.append(f"{self.ctype(dt)} {p}")
            else:
                params.append(f"long long {p}")
        plist = ', '.join(params) if params else 'void'
        lines = [f"static {cret} {node.name}({plist}) {{"]
        # 函数作用域
        self.scopes.append({p: 'long long' for p in node.params})
        for stmt in node.body:
            lines.extend(self._indent(self.gen_stmt(stmt), 1))
        self.scopes.pop()
        lines.append("}")
        return "\n".join(lines)

    def gen_if(self, node: If) -> List[str]:
        cond = self.gen_expr(node.cond)
        lines = [f"if (({cond}) != 0) {{"]
        lines.extend(self.gen_block(node.body))
        if node.orelse:
            lines.append("} else {")
            lines.extend(self.gen_block(node.orelse))
        lines.append("}")
        return lines

    def gen_while(self, node: While) -> List[str]:
        lines = ["while (1) {"]
        cond = self.gen_expr(node.cond)
        lines.append(f"    if (!(({cond}) != 0)) break;")
        lines.extend(self.gen_block(node.body))
        lines.append("}")
        return lines

    def gen_for(self, node: For) -> List[str]:
        it = node.iter
        # range() 特化：编译为原生 for 循环（速度关键）
        if isinstance(it, Call) and isinstance(it.func, Var) and it.func.name == 'range':
            args = it.args
            if len(args) == 1:
                start, stop, step = "0", self.gen_expr(args[0]), "1"
            elif len(args) == 2:
                start, stop, step = (self.gen_expr(args[0]),
                                     self.gen_expr(args[1]), "1")
            else:
                start, stop, step = (self.gen_expr(args[0]),
                                     self.gen_expr(args[1]),
                                     self.gen_expr(args[2]))
            var = node.var
            lines = [f"for (long long {var} = {start}; "
                     f"({step} > 0 ? {var} < {stop} : {var} > {stop}); "
                     f"{var} += {step}) {{"]
            self.scopes.append({var: 'long long'})
            for stmt in node.body:
                lines.extend(self._indent(self.gen_stmt(stmt), 1))
            self.scopes.pop()
            lines.append("}")
            return lines
        # 遍历字典：按插入顺序拿键（与 Python dict 一致）
        if self.infer_type(it) == 'dict':
            pre, var = self.gen_dict_ref(it)
            i = self.new_tmp()
            lines = list(pre) + [
                f"for (long long {i} = 0; {i} < {var}.b->len; {i}++) {{",
                f"    pypp_val {node.var} = {var}.b->keys[{i}];"]
            self.scopes.append({node.var: 'dyn'})
            for stmt in node.body:
                lines.extend(self._indent(self.gen_stmt(stmt), 1))
            self.scopes.pop()
            lines.append("}")
            return lines
        # 遍历 Python 对象（列表/元组等）
        if self.infer_type(it) == 'pyobj':
            src = self.gen_expr(it)
            tmp = self.new_tmp()
            i = self.new_tmp()
            lines = [
                f"PyObject* {tmp} = pypp_iter_list({src});",
                f"for (long long {i} = 0; {i} < pypp_iter_len({tmp}); {i}++) {{",
                f"    PyObject* {node.var} = pypp_iter_at({tmp}, {i});"]
            self.scopes.append({node.var: 'pyobj'})
            for stmt in node.body:
                lines.extend(self._indent(self.gen_stmt(stmt), 1))
            self.scopes.pop()
            lines.append("}")
            return lines
        # 遍历列表
        lst = self.gen_expr(it)
        tmp = self.new_tmp()
        var = node.var
        ot = self.infer_type(it)
        etype = ot[5:] if (ot and ot.startswith('list:')) else 'long long'
        at = {'str': 'pypp_list_at_s',
              'double': 'pypp_list_at_f'}.get(etype, 'pypp_list_at_i')
        lines = [f"pypp_list {tmp} = {lst};",
                 f"for (long long _i = 0; _i < {tmp}.b->len; _i++) {{",
                 f"    {self.ctype(etype)} {var} = {at}({tmp}, _i);"]
        self.scopes.append({var: etype})
        for stmt in node.body:
            lines.extend(self._indent(self.gen_stmt(stmt), 1))
        self.scopes.pop()
        lines.append("}")
        return lines

    def gen_assign(self, node: Assign):
        # ---- 字典元素赋值：d[k] = v ----
        if isinstance(node.target, Index) and \
                self.infer_type(node.target.obj) == 'dict':
            pre, var = self.gen_dict_ref(node.target.obj)
            return pre + [
                f"pypp_dict_set(&{var}, {self.gen_valarg(node.target.index)}, "
                f"{self.gen_valarg(node.value)});"]
        # ---- 属性赋值：self.x = v / obj.x = v / pyobj.attr = v ----
        if isinstance(node.target, Attr):
            ot = self.infer_type(node.target.obj)
            if ot and ot.startswith('obj:'):
                objexpr = self.gen_expr(node.target.obj)
                arrow = '->' if self._is_self_ptr(node.target.obj) else '.'
                return [f"{objexpr}{arrow}{node.target.name} = "
                        f"{self.gen_expr(node.value)};"]
            if ot == 'pyobj':
                objexpr = self.gen_expr(node.target.obj)
                return [f'pypp_setattr({objexpr}, "{node.target.name}", '
                        f'{self.gen_pyarg(node.value)});']
            return [f"(void)({self.gen_expr(node.value)});"]

        val = self.gen_expr(node.value)
        ttype = self.infer_type(node.value)
        if isinstance(node.target, Var):
            name = node.target.name
            known = self.lookup_type(name)
            if known == 'auto':
                self.declare_var(name, ttype)
                return [f"{self.ctype(ttype)} {name} = {val};"]
            return [f"{name} = {val};"]
        if isinstance(node.target, Index):
            obj = self.gen_expr(node.target.obj)
            idx = self.gen_expr(node.target.index)
            tmp = self.new_tmp()
            return [f"{{ long long {tmp} = {idx}; "
                    f"if ({tmp} >= 0 && {tmp} < {obj}.b->len) "
                    f"{obj}.b->idata[{tmp}] = (long long)({val}); }}"]
        return [f"(void)({val});"]

    def gen_augassign(self, node: AugAssign):
        t = node.target
        op = {'PLUS_ASSIGN': '+', 'MINUS_ASSIGN': '-', 'STAR_ASSIGN': '*',
              'SLASH_ASSIGN': '/', 'MOD_ASSIGN': '%', 'POW_ASSIGN': '**',
              'FLOORDIV_ASSIGN': '/'}[node.op]
        val = self.gen_expr(node.value)
        if isinstance(t, Var):
            if op == '**':
                return [f"{t.name} = (long long)pow((double){t.name}, "
                        f"(double)({val}));"]
            return [f"{t.name} {op}= ({val});"]
        if isinstance(t, Index):
            obj = self.gen_expr(t.obj)
            idx = self.gen_expr(t.index)
            tmp = self.new_tmp()
            return [f"{{ long long {tmp} = {idx}; "
                    f"if ({tmp} >= 0 && {tmp} < {obj}.b->len) "
                    f"{obj}.b->idata[{tmp}] {op}= (long long)({val}); }}"]
        return [";"]

    def gen_expr_stmt(self, node: ExprStmt):
        e = node.expr
        code = self.gen_expr(e)
        # print 等返回 0 的调用，直接执行
        return [f"{code};"]

    def gen_return(self, node: Return):
        if node.value is None:
            return ["return 0;"]
        return [f"return {self.gen_expr(node.value)};"]

    # ---------- 表达式生成 ----------
    def gen_expr(self, node) -> str:
        if isinstance(node, Num):
            return repr(float(node.value)) if node.is_float else str(int(node.value))
        if isinstance(node, Str):
            return f"pypp_s({self.c_str(node.value)})"
        if isinstance(node, Bool):
            return "1" if node.value else "0"
        if isinstance(node, NoneLit):
            return "0"
        if isinstance(node, Var):
            return node.name
        if isinstance(node, BinOp):
            return self.gen_binop(node)
        if isinstance(node, UnaryOp):
            if node.op == 'not':
                if self.infer_type(node.operand) == 'dyn':
                    return f"(!pypp_v_truthy({self.gen_expr(node.operand)}))"
                return f"(!({self.gen_expr(node.operand)}))"
            return f"(-({self.gen_expr(node.operand)}))"
        if isinstance(node, Compare):
            if node.op in ('in', 'not in'):
                return self._gen_membership(node)
            lt, rt = self.infer_type(node.left), self.infer_type(node.right)
            if lt == 'dyn' or rt == 'dyn':
                return (f'pypp_val_cmp({self.gen_valarg(node.left)}, '
                        f'{self.gen_valarg(node.right)}, "{node.op}")')
            l, r = self.gen_expr(node.left), self.gen_expr(node.right)
            ops = {'==': '==', '!=': '!=', '<': '<', '>': '>',
                   '<=': '<=', '>=': '>='}
            return f"(({l}) {ops[node.op]} ({r}))"
        if isinstance(node, BoolOp):
            lt, rt = self.infer_type(node.left), self.infer_type(node.right)
            if lt == 'dyn' or rt == 'dyn':
                if node.op == 'and':
                    return (f"(pypp_v_truthy({self.gen_valarg(node.left)}) ? "
                            f"pypp_v_truthy({self.gen_valarg(node.right)}) : 0)")
                return (f"(pypp_v_truthy({self.gen_valarg(node.left)}) ? 1 : "
                        f"pypp_v_truthy({self.gen_valarg(node.right)}))")
            l, r = self.gen_expr(node.left), self.gen_expr(node.right)
            return f"(({l}) {'&&' if node.op == 'and' else '||'} ({r}))"
        if isinstance(node, ListLit):
            return self.gen_listlit(node)
        if isinstance(node, DictLit):
            return self.gen_dictlit(node)
        if isinstance(node, Call):
            return self.gen_call(node)
        if isinstance(node, Index):
            ot = self.infer_type(node.obj)
            if ot == 'dict':
                pre, var = self.gen_dict_ref(node.obj)
                k = self.gen_valarg(node.index)
                expr = f"pypp_dict_get(&{var}, {k})"
                if pre:
                    return f'({{ {" ".join(pre)} {expr}; }})'
                return expr
            obj = self.gen_expr(node.obj)
            if ot == 'pyobj':
                idx = self.gen_pyarg(node.index)
                return f"pypp_getitem({obj}, {idx})"
            idx = self.gen_expr(node.index)
            if ot and ot.startswith('list:'):
                fn = {'str': 'pypp_list_at_s',
                      'double': 'pypp_list_at_f'}.get(ot[5:], 'pypp_list_at_i')
                return f"{fn}({obj}, {idx})"
            return f"pypp_list_at_i({obj}, {idx})"
        if isinstance(node, Attr):
            ot = self.infer_type(node.obj)
            if ot == 'pyobj':
                obj = self.gen_expr(node.obj)
                return f'pypp_getattr({obj}, "{node.name}")'
            if ot and ot.startswith('obj:'):
                objexpr = self.gen_expr(node.obj)
                arrow = '->' if self._is_self_ptr(node.obj) else '.'
                return f"{objexpr}{arrow}{node.name}"
            return "0"
        return "0"

    def gen_binop(self, node: BinOp) -> str:
        lt, rt = self.infer_type(node.left), self.infer_type(node.right)
        op = node.op
        # 字典取出来的值是动态类型（pypp_val），走动态运算
        if lt == 'dyn' or rt == 'dyn':
            fn = {'+': 'pypp_val_add', '-': 'pypp_val_sub', '*': 'pypp_val_mul',
                  '/': 'pypp_val_div', '//': 'pypp_val_floordiv',
                  '%': 'pypp_val_mod', '**': 'pypp_val_pow'}.get(op)
            if fn:
                return (f"{fn}({self.gen_valarg(node.left)}, "
                        f"{self.gen_valarg(node.right)})")
        l, r = self.gen_expr(node.left), self.gen_expr(node.right)
        if op == '+':
            if lt == 'str' or rt == 'str':
                ls = l if lt == 'str' else f"pypp_to_str({l})"
                rs = r if rt == 'str' else f"pypp_to_str({r})"
                return f"pypp_str_concat({ls}, {rs})"
            return f"(({l}) + ({r}))"
        if op in ('-', '*'):
            return f"(({l}) {op} ({r}))"
        if op == '/':
            return f"((double)({l}) / (double)({r}))"
        if op == '//':
            return f"((long long)({l}) / (long long)({r}))"
        if op == '%':
            return f"((long long)({l}) % (long long)({r}))"
        if op == '**':
            return f"((long long)pow((double)({l}), (double)({r})))"
        return f"(({l}) + ({r}))"

    def _list_etype(self, node: ListLit) -> str:
        """列表元素类型：只要有一个字符串元素，整列就按字符串处理。"""
        ts = [self.infer_type(it) for it in node.items]
        if any(t == 'str' for t in ts):
            return 'str'
        if any(t == 'double' for t in ts):
            return 'double'
        # 全是布尔值时用 bool 模式，否则 [True, False] 会打印成 [1, 0]
        if ts and all(t == 'bool' for t in ts):
            return 'bool'
        return 'long long'

    def gen_listlit(self, node: ListLit) -> str:
        etype = self._list_etype(node)
        mode = {'long long': 0, 'double': 1, 'str': 2, 'bool': 3}[etype]
        tmp = self.new_tmp()
        pre = [f"pypp_list {tmp} = pypp_list_new({max(1, len(node.items))}, "
               f"{mode});"]
        for it in node.items:
            v = self.gen_expr(it)
            if etype == 'str':
                pre.append(f"pypp_list_push_s(&{tmp}, {v});")
            elif etype == 'double':
                pre.append(f"pypp_list_push_f(&{tmp}, (double)({v}));")
            else:
                pre.append(f"pypp_list_push_i(&{tmp}, (long long)({v}));")
        return f'({{ {" ".join(pre)} {tmp}; }})'

    def gen_call(self, node: Call) -> str:
        f = node.func
        if isinstance(f, Var):
            # extern 声明的 C 函数
            if f.name in self.externs:
                return self._gen_extern_call(f.name, node.args)
            # 类构造：Point(3, 4) -> Point_new(3, 4)
            if f.name in self.classes:
                args = [self.gen_expr(a) for a in node.args]
                return f"{f.name}_new({', '.join(args)})"
            # Python 侧的可调用对象（import 进来的模块 / 函数）
            if self.lookup_type(f.name) == 'pyobj':
                return self._gen_py_call(f.name, node.args)
            args = [self.gen_expr(a) for a in node.args]
            return self.gen_builtin_call(f.name, node.args, args)
        if isinstance(f, Attr):
            ot = self.infer_type(f.obj)
            # 对 Python 对象的属性调用：obj.method(...)
            if ot == 'pyobj':
                obj = self.gen_expr(f.obj)
                return self._gen_py_call(
                    f'pypp_getattr({obj}, "{f.name}")', node.args)
            if ot == 'dict':
                return self._gen_dict_method(f.obj, f.name, node.args)
            if ot and ot.startswith('obj:'):
                return self._gen_class_method(ot[4:], f.obj, f.name, node.args)
            obj = self.gen_expr(f.obj)
            args = [self.gen_expr(a) for a in node.args]
            return self.gen_method_call(ot, obj, f.name, node.args, args)
        return "0"

    def _is_self_ptr(self, node) -> bool:
        """self 在方法体里已经是结构体指针，取字段要用 ->。"""
        return (isinstance(node, Var) and node.name == 'self'
                and str(self.lookup_type('self')).startswith('obj:'))

    def _gen_class_method(self, cls: str, objnode, method: str, argnodes) -> str:
        """obj.method(args) -> ClassName_method(&obj, args)"""
        pre = []
        if self._is_self_ptr(objnode):
            selfexpr = 'self'
        elif isinstance(objnode, Var):
            selfexpr = f"&{self.gen_expr(objnode)}"
        else:
            tmp = self.new_tmp()
            pre.append(f"{cls} {tmp} = {self.gen_expr(objnode)};")
            selfexpr = f"&{tmp}"
        args = [selfexpr] + [self.gen_expr(a) for a in argnodes]
        call = f"{cls}_{method}({', '.join(args)})"
        if pre:
            return f'({{ {" ".join(pre)} {call}; }})'
        return call

    # ---------- 字典 ----------
    def gen_dict_ref(self, node):
        """拿到一个可寻址的 pypp_dict 变量名，必要时先落临时变量。"""
        if isinstance(node, Var):
            return [], self.gen_expr(node)
        tmp = self.new_tmp()
        return [f"pypp_dict {tmp} = {self.gen_expr(node)};"], tmp

    def gen_valarg(self, node) -> str:
        """把任意 py++ 值转换成 pypp_val（字典的键与值都用它承载）。

        容器（dict/list）与 Python 对象不再被拍平成字符串，而是作为对象值
        存进字典——否则 `{"a": inner}` 取出来会变成 "[dict]"，
        与 VM/Python 的输出不一致（这是曾经的真实 bug）。
        """
        t = self.infer_type(node)
        e = self.gen_expr(node)
        if t == 'dyn':
            return e
        if t == 'bool':
            # bool 必须单独处理：否则 True 会被当成整数打印成 1
            return f"pypp_v_b(({e}) ? 1 : 0)"
        if t == 'double':
            return f"pypp_v_f((double)({e}))"
        if t == 'str':
            return f"pypp_v_s({e})"
        if t == 'pyobj':
            # 用 Python 的 str() 做 repr，保证打印时与 VM 一致
            return f"pypp_v_obj_py({e})"
        if t == 'dict':
            # 按值传句柄：复合语句表达式不是左值，不能取地址
            return f"pypp_v_dict_tag(pypp_dict_snapshot_v({e}))"
        if t and t.startswith('list'):
            return f"pypp_v_list_tag(pypp_list_snapshot_v({e}))"
        return f"pypp_v_i((long long)({e}))"

    def gen_dictlit(self, node: DictLit) -> str:
        tmp = self.new_tmp()
        pre = [f"pypp_dict {tmp} = pypp_dict_new();"]
        for k, v in zip(node.keys, node.values):
            pre.append(f"pypp_dict_set(&{tmp}, {self.gen_valarg(k)}, "
                       f"{self.gen_valarg(v)});")
        return f'({{ {" ".join(pre)} {tmp}; }})'

    def _gen_dict_method(self, objnode, method, argnodes) -> str:
        pre, var = self.gen_dict_ref(objnode)
        if method == 'get':
            if not argnodes:
                call = f"pypp_dict_get(&{var}, pypp_v_none())"
            elif len(argnodes) == 1:
                call = (f"pypp_dict_get(&{var}, {self.gen_valarg(argnodes[0])})")
            else:
                call = (f"pypp_dict_get_or(&{var}, {self.gen_valarg(argnodes[0])},"
                        f" {self.gen_valarg(argnodes[1])})")
        elif method in ('has_key', 'contains'):
            call = (f"pypp_v_b(pypp_dict_has(&{var}, "
                    f"{self.gen_valarg(argnodes[0])}))")
        elif method == 'clear':
            call = f"(pypp_dict_clear(&{var}), pypp_v_none())"
        else:
            call = "pypp_v_none()"
        if pre:
            return f'({{ {" ".join(pre)} {call}; }})'
        return call

    def _gen_membership(self, node: Compare) -> str:
        """x in y / x not in y"""
        neg = node.op == 'not in'
        rt = self.infer_type(node.right)
        pre = []
        if rt == 'dict':
            pre, var = self.gen_dict_ref(node.right)
            expr = f"pypp_dict_has(&{var}, {self.gen_valarg(node.left)})"
        elif rt == 'pyobj':
            expr = (f"pypp_contains({self.gen_expr(node.right)}, "
                    f"{self.gen_pyarg(node.left)})")
        elif rt == 'str' and self.infer_type(node.left) == 'str':
            hay = self.gen_expr(node.right)
            needle = self.gen_expr(node.left)
            expr = (f"(strstr(pypp_str_cstr({hay}), "
                    f"pypp_str_cstr({needle})) != NULL)")
        else:
            expr = "0"
        if pre:
            expr = f'({{ {" ".join(pre)} {expr}; }})'
        return f"(!{expr})" if neg else expr

    def _gen_py_call(self, fexpr: str, argnodes) -> str:
        """生成一次 Python 侧调用。"""
        self.uses_python = True
        parts = [self.gen_pyarg(a) for a in argnodes]
        if parts:
            return f'pypp_callv({fexpr}, {len(parts)}, {", ".join(parts)})'
        return f'pypp_callv({fexpr}, 0)'

    def gen_pyarg(self, node) -> str:
        """把一个 py++ 值转换成 PyObject*（用于传给 Python 函数）。"""
        t = self.infer_type(node)
        e = self.gen_expr(node)
        if t == 'pyobj':
            return e
        if t == 'double':
            return f'pypp_num((double)({e}))'
        if t == 'str':
            return f'pypp_str_py(pypp_str_cstr({e}))'
        if t == 'bool':
            # 布尔必须是真 Python 布尔，否则 json.dumps 会输出 1/0
            return f'pypp_bool_py(({e}) ? 1 : 0)'
        if t and t.startswith('list'):
            return f'pypp_list_py({e})'
        if t == 'dict':
            # 此前漏了这一支：字典会被当成 int 传出去（json.dumps(d) 直接崩）
            return f'pypp_dict_py({e})'
        if t == 'dyn':
            return f'pypp_val_py({e})'
        return f'pypp_num_i((long long)({e}))'

    def gen_builtin_call(self, name, argnodes, args) -> str:
        if name == 'print':
            if not argnodes:
                return "(pypp_print_endl(), 0)"
            parts = []
            for i, a in enumerate(argnodes):
                t = self.infer_type(a)
                if t == 'dyn':
                    parts.append(f"pypp_print_val({args[i]})")
                elif t == 'pyobj':
                    parts.append(f"pypp_print_py({args[i]})")
                elif t == 'dict':
                    parts.append(f"pypp_print_dict({args[i]})")
                elif t and t.startswith('list'):
                    parts.append(f"pypp_print_list({args[i]})")
                elif t == 'bool':
                    parts.append(f"pypp_print_bool({args[i]})")
                elif t == 'double':
                    parts.append(f"pypp_print_float((double)({args[i]}))")
                elif t == 'str':
                    parts.append(f"pypp_print_str({args[i]})")
                else:
                    parts.append(f"pypp_print_int((long long)({args[i]}))")
                if i < len(argnodes) - 1:
                    parts.append("pypp_print_sep()")
            parts.append("pypp_print_endl()")
            return "(" + ", ".join(parts) + ", 0)"
        if name == 'input':
            if argnodes:
                return (f"(pypp_print_str({args[0]}), pypp_print_endl(), "
                        f"pypp_input())")
            return "pypp_input()"
        if name == 'int':
            if not argnodes:
                return "0"
            t = self.infer_type(argnodes[0])
            if t == 'dyn':
                return f"pypp_val_int({args[0]})"
            if t == 'pyobj':
                return f"pypp_to_ll({args[0]})"
            if t == 'str':
                return f"pypp_to_int({args[0]})"
            return f"((long long)({args[0]}))"
        if name == 'float':
            if not argnodes:
                return "0.0"
            t = self.infer_type(argnodes[0])
            if t == 'dyn':
                return f"pypp_val_float({args[0]})"
            if t == 'pyobj':
                return f"pypp_to_double({args[0]})"
            if t == 'str':
                return f"pypp_to_float({args[0]})"
            return f"((double)({args[0]}))"
        if name == 'str':
            if not argnodes:
                return 'pypp_s("")'
            t = self.infer_type(argnodes[0])
            if t == 'dyn':
                return f"pypp_val_to_str({args[0]})"
            if t == 'pyobj':
                return f"pypp_pystr({args[0]})"
            if t == 'str':
                return args[0]
            if t == 'double':
                return f"pypp_to_str_f({args[0]})"
            return f"pypp_to_str({args[0]})"
        if name == 'len':
            if not argnodes:
                return "0"
            t = self.infer_type(argnodes[0])
            if t == 'dyn':
                return f"pypp_val_len({args[0]})"
            if t == 'dict':
                return f"pypp_dict_len({args[0]})"
            if t == 'pyobj':
                return f"pypp_len_py({args[0]})"
            if t == 'str':
                return f"pypp_len_str({args[0]})"
            return f"pypp_list_len({args[0]})"
        if name == 'abs':
            t = self.infer_type(argnodes[0]) if argnodes else 'long long'
            return (f"pypp_abs_f({args[0]})" if t == 'double'
                    else f"pypp_abs_i({args[0]})")
        if name == 'min':
            return f"pypp_min_i({args[0]}, {args[1]})" if len(args) >= 2 else "0"
        if name == 'max':
            return f"pypp_max_i({args[0]}, {args[1]})" if len(args) >= 2 else "0"
        if name == 'sum':
            return f"pypp_sum_list({args[0]})"
        if name == 'range':
            if len(args) == 1:
                return f"pypp_range(0, {args[0]}, 1)"
            if len(args) == 2:
                return f"pypp_range({args[0]}, {args[1]}, 1)"
            return f"pypp_range({args[0]}, {args[1]}, {args[2]})"
        if name == 'pow':
            return f"((long long)pow((double)({args[0]}), (double)({args[1]})))"
        if name in self.func_types:
            return f"{name}({', '.join(args)})"
        return "0"

    def gen_method_call(self, otype, obj, method, argnodes, args) -> str:
        if method == 'append':
            if args:
                t = self.infer_type(argnodes[0]) if argnodes else 'long long'
                if t == 'double':
                    return f"(pypp_list_push_f(&{obj}, (double)({args[0]})), 0)"
                return f"(pypp_list_push_i(&{obj}, (long long)({args[0]})), 0)"
            return "0"
        if method in ('upper', 'lower', 'strip'):
            return obj
        return "0"

    def c_str(self, s: str) -> str:
        out = ['"']
        for ch in s:
            if ch == '"':
                out.append('\\"')
            elif ch == '\\':
                out.append('\\\\')
            elif ch == '\n':
                out.append('\\n')
            elif ch == '\t':
                out.append('\\t')
            elif ch == '\r':
                out.append('\\r')
            elif ord(ch) < 32:
                out.append(f'\\x{ord(ch):02x}')
            else:
                out.append(ch)
        out.append('"')
        return ''.join(out)

    # ---------- 内嵌运行时 ----------
    def runtime_header(self, uses_python=False) -> str:
        return RUNTIME_C + (PY_RUNTIME_C if uses_python else "")


RUNTIME_C = r'''
/* ===== py++ runtime (auto-generated) ===== */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>

typedef long long pypp_int;
typedef double    pypp_float;

typedef struct { char* data; long long len; } pypp_str;
static pypp_str pypp_str_new(const char* s){ pypp_str r; r.len=(long long)strlen(s); r.data=(char*)malloc(r.len+1); memcpy(r.data,s,r.len+1); return r; }
static pypp_str pypp_s(const char* s){ return pypp_str_new(s); }
static pypp_str pypp_str_concat(pypp_str a, pypp_str b){ pypp_str r; r.len=a.len+b.len; r.data=(char*)malloc(r.len+1); memcpy(r.data,a.data,a.len); memcpy(r.data+a.len,b.data,b.len); r.data[r.len]=0; return r; }
static const char* pypp_str_cstr(pypp_str s){ return s.data?s.data:""; }

/* mode: 0=整数 1=浮点 2=字符串
   与字典一样，列表也是"引用类型"：pypp_list 是句柄，数据在堆上的 body。 */
typedef struct { long long* idata; double* fdata; pypp_str* sdata;
                 long long len; long long cap; int mode; } pypp_list_body;
typedef struct { pypp_list_body* b; } pypp_list;

static pypp_list_body* pypp_list_body_new(long long cap, int mode){
    pypp_list_body* b=(pypp_list_body*)malloc(sizeof(pypp_list_body));
    b->len=0; b->cap=cap>0?cap:4; b->mode=mode;
    b->idata=(long long*)calloc(b->cap,sizeof(long long));
    b->fdata=(double*)calloc(b->cap,sizeof(double));
    b->sdata=(pypp_str*)calloc(b->cap,sizeof(pypp_str));
    return b;
}
static pypp_list pypp_list_new(long long cap, int mode){
    pypp_list l; l.b = pypp_list_body_new(cap, mode); return l;
}
static void pypp_list_grow(pypp_list_body* l){
    if(l->len>=l->cap){ l->cap*=2;
        l->idata=(long long*)realloc(l->idata,l->cap*sizeof(long long));
        l->fdata=(double*)realloc(l->fdata,l->cap*sizeof(double));
        l->sdata=(pypp_str*)realloc(l->sdata,l->cap*sizeof(pypp_str)); }
}
static void pypp_list_push_i(pypp_list* l, long long v){ pypp_list_grow(l->b); l->b->idata[l->b->len++]=v; }
static void pypp_list_push_f(pypp_list* l, double v){ pypp_list_grow(l->b); l->b->fdata[l->b->len++]=v; }
static void pypp_list_push_s(pypp_list* l, pypp_str v){ pypp_list_grow(l->b); l->b->sdata[l->b->len++]=v; }
static pypp_list pypp_range(long long start, long long stop, long long step){
    if(step==0) step=1;
    long long n = (stop-start)/step; if(n<0) n=0;
    pypp_list l=pypp_list_new(n>0?n:1,0);
    for(long long i=start; step>0?i<stop:i>stop; i+=step) pypp_list_push_i(&l,i);
    return l;
}
static long long pypp_list_at_i(pypp_list l, long long i){ pypp_list_body* B=l.b; if(!B) return 0; if(i<0)i+=B->len; if(i<0||i>=B->len) return 0; return B->idata[i]; }
static double pypp_list_at_f(pypp_list l, long long i){ pypp_list_body* B=l.b; if(!B) return 0; if(i<0)i+=B->len; if(i<0||i>=B->len) return 0; return B->fdata[i]; }
static pypp_str pypp_list_at_s(pypp_list l, long long i){ pypp_list_body* B=l.b; if(!B) return pypp_s(""); if(i<0)i+=B->len; if(i<0||i>=B->len) return pypp_s(""); return B->sdata[i]; }
static long long pypp_list_len(pypp_list l){ return l.b ? l.b->len : 0; }

static void pypp_print_int(long long v){ printf("%lld", v); }
static void pypp_print_bool(long long v){ printf("%s", v ? "True" : "False"); }
static void pypp_print_float(double v){
    if(v==(double)(long long)v) printf("%.1f", v);
    else { char buf[64]; snprintf(buf,sizeof(buf),"%g",v); printf("%s",buf); }
}
static void pypp_print_str(pypp_str s){ printf("%s", pypp_str_cstr(s)); }
/* ⚠️ 换行后必须 flush：管道里 stdout 是全缓冲（不到 4KB 不吐），
   不刷新的话长跑程序的输出会攒到最后一次性涌出，
   "边运行边输出"就成了空话（IDE/管道消费者看着像假死）。
   一行一次 fflush 的成本可忽略（stdio 缓冲为空时几乎零开销）。 */
static void pypp_print_endl(){ printf("\n"); fflush(stdout); }
static void pypp_print_sep(){ printf(" "); }
/* 列表的 repr 必须与 Python/VM 逐字节一致：
   元素之间 ", "，字符串元素带单引号。 */
static pypp_str pypp_list_repr_body(pypp_list_body* l){
    pypp_str r = pypp_s("[");
    if(!l) return pypp_str_concat(r, pypp_s("]"));
    for(long long i=0;i<l->len;i++){
        if(i) r = pypp_str_concat(r, pypp_s(", "));
        if(l->mode==2){ r = pypp_str_concat(r, pypp_s("'"));
                       r = pypp_str_concat(r, l->sdata[i]);
                       r = pypp_str_concat(r, pypp_s("'")); }
        else if(l->mode==3){ r = pypp_str_concat(r, pypp_s(l->idata[i] ? "True" : "False")); }
        else if(l->mode==1){ char b[64];
                       if(l->fdata[i]==(double)(long long)l->fdata[i]) snprintf(b,sizeof(b),"%.1f",l->fdata[i]);
                       else snprintf(b,sizeof(b),"%g",l->fdata[i]);
                       r = pypp_str_concat(r, pypp_s(b)); }
        else { char b[64]; snprintf(b,sizeof(b),"%lld",l->idata[i]);
               r = pypp_str_concat(r, pypp_s(b)); }
    }
    return pypp_str_concat(r, pypp_s("]"));
}
static pypp_str pypp_list_repr(pypp_list l){ return pypp_list_repr_body(l.b); }
static void pypp_print_list(pypp_list l){ pypp_print_str(pypp_list_repr(l)); }
/* 列表放进字典时，只需把句柄拷进堆上的 pypp_list 即可——
   两边指向同一个 body，天然是 Python 的引用语义。 */
static void* pypp_list_snapshot_v(pypp_list src){
    pypp_list* p = (pypp_list*)malloc(sizeof(pypp_list));
    p->b = src.b;
    return (void*)p;
}

static pypp_str pypp_input(){
    char buf[4096];
    /* 先刷新提示语：stdout 在管道里是块缓冲，不 flush 的话
       input("提示") 的提示要等缓冲区满才出现（管道场景看起来像假死）。 */
    fflush(stdout);
    if(fgets(buf,sizeof(buf),stdin)==NULL) return pypp_s("");
    long long n=(long long)strlen(buf);
    while(n>0&&(buf[n-1]=='\n'||buf[n-1]=='\r')) buf[--n]=0;
    return pypp_s(buf);
}
static long long pypp_to_int(pypp_str s){ return atoll(s.data); }
static double pypp_to_float(pypp_str s){ return atof(s.data); }
static pypp_str pypp_to_str(long long v){ char b[64]; snprintf(b,sizeof(b),"%lld",v); return pypp_s(b); }
static pypp_str pypp_to_str_f(double v){ char b[64]; snprintf(b,sizeof(b),"%g",v); return pypp_s(b); }
static long long pypp_len_str(pypp_str s){ return s.len; }
static long long pypp_abs_i(long long v){ return v<0?-v:v; }
static double pypp_abs_f(double v){ return fabs(v); }
static long long pypp_min_i(long long a,long long b){ return a<b?a:b; }
static long long pypp_max_i(long long a,long long b){ return a>b?a:b; }
static long long pypp_sum_list(pypp_list l){ long long s=0; if(l.b) for(long long i=0;i<l.b->len;i++) s+=l.b->idata[i]; return s; }

/* ===== 动态值 pypp_val（字典的键与值） ===== */
/* 字典天然是混合类型（{"name":"py++","n":3}），静态类型承载不了，
   所以字典元素统一用带标签的 pypp_val。取出来的值参与运算时走
   下面的动态辅助函数——这是"字典是动态容器"的诚实代价。 */
#define PYPP_V_INT   0
#define PYPP_V_FLOAT 1
#define PYPP_V_STR   2
#define PYPP_V_BOOL  3
#define PYPP_V_NONE  4
#define PYPP_V_OBJ   5     /* 容器/对象：dict、list、Python 对象 */
#define PYPP_V_DICT  6     /* 专门标记 py++ 字典，便于转回 Python dict */
#define PYPP_V_LIST  7     /* 专门标记 py++ 列表，便于转回 Python list */

/* 对象值用 void* 承载，并自带一个打印/转字符串的钩子，
   保证"字典里放字典"这类嵌套结构打印结果与 VM 一致。
   owned=1 表示这块内存是快照拷贝，由 pypp_val 自己负责释放。 */
typedef struct { int tag; long long i; double f; pypp_str s;
                 void* obj; int owned; pypp_str (*repr_fn)(void*); } pypp_val;

static pypp_val pypp_v_obj(void* p, pypp_str (*repr_fn)(void*), int owned){
    pypp_val r; memset(&r,0,sizeof(r));
    r.tag=PYPP_V_OBJ; r.obj=p; r.repr_fn=repr_fn; r.owned=owned;
    return r;
}
static pypp_str pypp_dict_repr_hook(void* p);
static pypp_str pypp_list_repr_hook(void* p);
static pypp_val pypp_v_dict_tag(void* p){ pypp_val r = pypp_v_obj(p, pypp_dict_repr_hook, 1); r.tag=PYPP_V_DICT; return r; }
static pypp_val pypp_v_list_tag(void* p){ pypp_val r = pypp_v_obj(p, pypp_list_repr_hook, 1); r.tag=PYPP_V_LIST; return r; }

static pypp_val pypp_v_i(long long v){ pypp_val r; memset(&r,0,sizeof(r)); r.tag=PYPP_V_INT; r.i=v; return r; }
static pypp_val pypp_v_f(double v){ pypp_val r; memset(&r,0,sizeof(r)); r.tag=PYPP_V_FLOAT; r.f=v; return r; }
static pypp_val pypp_v_s(pypp_str v){ pypp_val r; memset(&r,0,sizeof(r)); r.tag=PYPP_V_STR; r.s=v; return r; }
static pypp_val pypp_v_b(int v){ pypp_val r; memset(&r,0,sizeof(r)); r.tag=PYPP_V_BOOL; r.i=v?1:0; return r; }
static pypp_val pypp_v_none(void){ pypp_val r; memset(&r,0,sizeof(r)); r.tag=PYPP_V_NONE; return r; }

static int pypp_v_isnum(pypp_val v){
    return v.tag==PYPP_V_INT || v.tag==PYPP_V_FLOAT || v.tag==PYPP_V_BOOL;
}
static long long pypp_val_int(pypp_val v){
    if(v.tag==PYPP_V_STR) return atoll(v.s.data ? v.s.data : "0");
    if(v.tag==PYPP_V_FLOAT) return (long long)v.f;
    if(v.tag==PYPP_V_OBJ||v.tag==PYPP_V_DICT||v.tag==PYPP_V_LIST) return 0;
    return v.i;
}
static double pypp_val_float(pypp_val v){
    if(v.tag==PYPP_V_STR) return atof(v.s.data ? v.s.data : "0");
    if(v.tag==PYPP_V_FLOAT) return v.f;
    if(v.tag==PYPP_V_OBJ||v.tag==PYPP_V_DICT||v.tag==PYPP_V_LIST) return 0;
    return (double)v.i;
}
static pypp_str pypp_val_to_str(pypp_val v){
    char b[64];
    switch(v.tag){
        case PYPP_V_FLOAT:
            if(v.f==(double)(long long)v.f) snprintf(b,sizeof(b),"%.1f",v.f);
            else snprintf(b,sizeof(b),"%g",v.f);
            return pypp_s(b);
        case PYPP_V_STR:  return v.s;
        case PYPP_V_BOOL: return pypp_s(v.i?"True":"False");
        case PYPP_V_NONE: return pypp_s("None");
        case PYPP_V_OBJ:
        case PYPP_V_DICT:
        case PYPP_V_LIST:  return v.repr_fn ? v.repr_fn(v.obj) : pypp_s("<object>");
        default: snprintf(b,sizeof(b),"%lld",v.i); return pypp_s(b);
    }
}
static int pypp_v_truthy(pypp_val v){
    switch(v.tag){
        case PYPP_V_FLOAT: return v.f!=0;
        case PYPP_V_STR:   return v.s.len>0;
        case PYPP_V_NONE:  return 0;
        case PYPP_V_OBJ:
        case PYPP_V_DICT:
        case PYPP_V_LIST:  return v.obj!=NULL;
        default:           return v.i!=0;
    }
}
static long long pypp_val_len(pypp_val v){
    if(v.tag==PYPP_V_STR) return v.s.len;
    return 0;
}
static void pypp_print_val(pypp_val v){
    if(v.tag==PYPP_V_STR) pypp_print_str(v.s);
    else if(v.tag==PYPP_V_FLOAT) pypp_print_float(v.f);
    else if(v.tag==PYPP_V_BOOL) printf("%s", v.i?"True":"False");
    else if(v.tag==PYPP_V_NONE) printf("None");
    else if(v.tag==PYPP_V_OBJ||v.tag==PYPP_V_DICT||v.tag==PYPP_V_LIST) pypp_print_str(pypp_val_to_str(v));
    else pypp_print_int(v.i);
}
/* 数值化：让 int/float 参与同一套算术 */
static double pypp_v_num(pypp_val v){ return pypp_v_isnum(v) ? (v.tag==PYPP_V_FLOAT?v.f:(double)v.i) : 0; }

static pypp_val pypp_val_add(pypp_val a, pypp_val b){
    if(a.tag==PYPP_V_STR || b.tag==PYPP_V_STR)
        return pypp_v_s(pypp_str_concat(pypp_val_to_str(a), pypp_val_to_str(b)));
    if(a.tag==PYPP_V_FLOAT || b.tag==PYPP_V_FLOAT)
        return pypp_v_f(pypp_v_num(a)+pypp_v_num(b));
    return pypp_v_i(a.i+b.i);
}
static pypp_val pypp_val_sub(pypp_val a, pypp_val b){
    if(a.tag==PYPP_V_FLOAT || b.tag==PYPP_V_FLOAT) return pypp_v_f(pypp_v_num(a)-pypp_v_num(b));
    return pypp_v_i(a.i-b.i);
}
static pypp_val pypp_val_mul(pypp_val a, pypp_val b){
    if(a.tag==PYPP_V_FLOAT || b.tag==PYPP_V_FLOAT) return pypp_v_f(pypp_v_num(a)*pypp_v_num(b));
    return pypp_v_i(a.i*b.i);
}
static pypp_val pypp_val_div(pypp_val a, pypp_val b){
    double d=pypp_v_num(b); if(d==0) return pypp_v_f(0);
    return pypp_v_f(pypp_v_num(a)/d);
}
static pypp_val pypp_val_floordiv(pypp_val a, pypp_val b){
    long long d=pypp_val_int(b); if(d==0) return pypp_v_i(0);
    return pypp_v_i(pypp_val_int(a)/d);
}
static pypp_val pypp_val_mod(pypp_val a, pypp_val b){
    long long d=pypp_val_int(b); if(d==0) return pypp_v_i(0);
    return pypp_v_i(pypp_val_int(a)%d);
}
static pypp_val pypp_val_pow(pypp_val a, pypp_val b){
    return pypp_v_i((long long)pow(pypp_v_num(a), pypp_v_num(b)));
}
static int pypp_val_cmp(pypp_val a, pypp_val b, const char* op){
    if(pypp_v_isnum(a) && pypp_v_isnum(b)){
        double x=pypp_v_num(a), y=pypp_v_num(b);
        if(!strcmp(op,"==")) return x==y;
        if(!strcmp(op,"!=")) return x!=y;
        if(!strcmp(op,"<"))  return x<y;
        if(!strcmp(op,">"))  return x>y;
        if(!strcmp(op,"<=")) return x<=y;
        return x>=y;
    }
    { const char* x=pypp_val_to_str(a).data; const char* y=pypp_val_to_str(b).data;
      int c=strcmp(x?x:"", y?y:"");
      if(!strcmp(op,"==")) return c==0;
      if(!strcmp(op,"!=")) return c!=0;
      if(!strcmp(op,"<"))  return c<0;
      if(!strcmp(op,">"))  return c>0;
      if(!strcmp(op,"<=")) return c<=0;
      return c>=0; }
}
static int pypp_val_eq(pypp_val a, pypp_val b){
    if(pypp_v_isnum(a) && pypp_v_isnum(b)) return pypp_v_num(a)==pypp_v_num(b);
    if(a.tag!=b.tag) return 0;
    if(a.tag==PYPP_V_STR){ const char* x=a.s.data; const char* y=b.s.data;
                           return strcmp(x?x:"", y?y:"")==0; }
    return a.tag==PYPP_V_NONE;
}

/* ===== 字典 pypp_dict：开放寻址哈希表 + 插入顺序表 ===== */
/* 哈希表负责 O(1) 查找；keys 数组额外记录插入顺序，
   这样 for k in d 的顺序和 Python 一致（VM 侧就是 Python dict）。 */
typedef struct { pypp_val key; pypp_val val; int used; } pypp_slot;
/* 字典是"引用类型"：pypp_dict 只是句柄，真正的数据在堆上的 body 里。
   这样把字典放进另一个字典时拷贝的是句柄，两边指向同一份数据——
   与 Python 的引用语义一致（修改原字典，外面看得见）。 */
typedef struct { pypp_slot* slots; long long cap; long long len;
                 pypp_val* keys; long long kcap; } pypp_dict_body;
typedef struct { pypp_dict_body* b; } pypp_dict;

static pypp_dict_body* pypp_dict_body_new(void){
    pypp_dict_body* b=(pypp_dict_body*)malloc(sizeof(pypp_dict_body));
    b->cap=8; b->len=0; b->kcap=8;
    b->slots=(pypp_slot*)calloc((size_t)b->cap,sizeof(pypp_slot));
    b->keys=(pypp_val*)calloc((size_t)b->kcap,sizeof(pypp_val));
    return b;
}
static unsigned long long pypp_val_hash(pypp_val v){
    if(v.tag==PYPP_V_STR){
        unsigned long long h=1469598103934665603ULL;
        const unsigned char* p=(const unsigned char*)(v.s.data?v.s.data:"");
        while(*p){ h^=(unsigned long long)*p++; h*=1099511628211ULL; }
        return h;
    }
    if(v.tag==PYPP_V_FLOAT) return (unsigned long long)(long long)v.f * 2654435761ULL;
    if(v.tag==PYPP_V_NONE)  return 0x9e3779b97f4a7c15ULL;
    return (unsigned long long)v.i * 2654435761ULL;
}
static pypp_dict pypp_dict_new(void){
    pypp_dict d; d.b = pypp_dict_body_new(); return d;
}
static void pypp_dict_rehash(pypp_dict_body* d, long long newcap){
    pypp_slot* old=d->slots; long long oldcap=d->cap;
    d->slots=(pypp_slot*)calloc((size_t)newcap,sizeof(pypp_slot));
    d->cap=newcap;
    for(long long i=0;i<oldcap;i++){
        if(!old[i].used) continue;
        unsigned long long h=pypp_val_hash(old[i].key)%(unsigned long long)newcap;
        while(d->slots[h].used) h=(h+1)%(unsigned long long)newcap;
        d->slots[h]=old[i];
    }
    free(old);
}
static pypp_slot* pypp_dict_slot(pypp_dict_body* d, pypp_val k, int create){
    if(!d || d->cap<=0) return NULL;
    if(create && d->len*4 >= d->cap*3) pypp_dict_rehash(d, d->cap*2);
    unsigned long long h=pypp_val_hash(k)%(unsigned long long)d->cap;
    while(d->slots[h].used){
        if(pypp_val_eq(d->slots[h].key,k)) return &d->slots[h];
        h=(h+1)%(unsigned long long)d->cap;
    }
    if(!create) return NULL;
    d->slots[h].used=1; d->slots[h].key=k; d->slots[h].val=pypp_v_none(); d->len++;
    if(d->len > d->kcap){
        d->kcap*=2;
        d->keys=(pypp_val*)realloc(d->keys,(size_t)d->kcap*sizeof(pypp_val));
    }
    d->keys[d->len-1]=k;
    return &d->slots[h];
}
static void pypp_dict_set(pypp_dict* d, pypp_val k, pypp_val v){
    pypp_slot* s=pypp_dict_slot(d->b,k,1); if(s) s->val=v;
}
static pypp_val pypp_dict_get(pypp_dict* d, pypp_val k){
    pypp_slot* s=pypp_dict_slot(d->b,k,0); return s?s->val:pypp_v_none();
}
static pypp_val pypp_dict_get_or(pypp_dict* d, pypp_val k, pypp_val dflt){
    pypp_slot* s=pypp_dict_slot(d->b,k,0); return s?s->val:dflt;
}
static int pypp_dict_has(pypp_dict* d, pypp_val k){
    return pypp_dict_slot(d->b,k,0)!=NULL;
}
static long long pypp_dict_len(pypp_dict d){ return d.b ? d.b->len : 0; }
static void pypp_dict_clear(pypp_dict* d){
    pypp_dict_body* b = d->b;
    if(!b) return;
    if(b->slots) free(b->slots);
    if(b->keys) free(b->keys);
    b->slots=(pypp_slot*)calloc((size_t)8,sizeof(pypp_slot));
    b->keys=(pypp_val*)calloc((size_t)8,sizeof(pypp_val));
    b->cap=8; b->kcap=8; b->len=0;
}
/* 字典 repr：{'k': v, ...}，键按插入顺序（与 Python/VM 一致）。
   值与键都可能是容器，所以走 pypp_val_to_str 递归。 */
static pypp_str pypp_dict_repr_impl(pypp_dict_body* d){
    pypp_str r = pypp_s("{");
    if(!d) return pypp_str_concat(r, pypp_s("}"));
    for(long long i=0;i<d->len;i++){
        if(i) r = pypp_str_concat(r, pypp_s(", "));
        pypp_val k = d->keys[i];
        pypp_slot* ps = pypp_dict_slot(d, k, 0);
        pypp_val v = ps ? ps->val : pypp_v_none();
        /* 键是字符串时加单引号，与 Python 一致 */
        if(k.tag==PYPP_V_STR){ r = pypp_str_concat(r, pypp_s("'")); }
        r = pypp_str_concat(r, pypp_val_to_str(k));
        if(k.tag==PYPP_V_STR){ r = pypp_str_concat(r, pypp_s("'")); }
        r = pypp_str_concat(r, pypp_s(": "));
        if(v.tag==PYPP_V_STR){ r = pypp_str_concat(r, pypp_s("'")); }
        r = pypp_str_concat(r, pypp_val_to_str(v));
        if(v.tag==PYPP_V_STR){ r = pypp_str_concat(r, pypp_s("'")); }
    }
    return pypp_str_concat(r, pypp_s("}"));
}
static pypp_str pypp_dict_repr_hook(void* p){ return pypp_dict_repr_impl(((pypp_dict*)p)->b); }
static pypp_str pypp_list_repr_hook(void* p){ return pypp_list_repr_body(((pypp_list*)p)->b); }
static void pypp_print_dict(pypp_dict d){ pypp_print_str(pypp_dict_repr_impl(d.b)); }
/* 字典放进别的字典时，只需把句柄拷进堆上的 pypp_dict 即可——
   两边指向同一个 body，天然是 Python 的引用语义。
   接收值（不是指针），这样复合语句表达式也能直接传进来。 */
static void* pypp_dict_snapshot_v(pypp_dict src){
    pypp_dict* p = (pypp_dict*)malloc(sizeof(pypp_dict));
    p->b = src.b;
    return (void*)p;
}
/* 释放一个 owned 的对象值（仅释放句柄本身，body 由引用它的变量负责） */
static void pypp_val_release(pypp_val* v){
    if((v->tag==PYPP_V_OBJ||v->tag==PYPP_V_DICT||v->tag==PYPP_V_LIST) && v->owned && v->obj){ free(v->obj); v->obj=NULL; v->owned=0; }
}
/* ===== end runtime ===== */
'''


# ===========================================================================
# Python 互操作运行时（仅当程序里出现 import 时才注入）
# ===========================================================================
# 设计要点：
#   · 把 CPython 解释器嵌进生成的可执行文件，用 C-API 调用 Python 库
#   · py++ 自己的数值/字符串运算仍是纯 C 指令，不经过 Python 对象
#   · 所有"来自 Python 的值"统一用 PyObject* 承载（pyobj 类型）
#   · 出错时打印 Python 的 traceback，但不让程序崩溃（延续容错哲学）
#
# 说明：为保持桥接层简单，这里不做过度的引用计数优化，允许少量泄漏。
#       对于脚本级程序这完全可接受；py++ 的"性能敏感路径"本来就不走这里。
# ===========================================================================
PY_RUNTIME_C = r'''
/* ===== py++ <-> CPython 桥接层 ===== */
#include <stdarg.h>
#include <Python.h>

/* 初始化解释器，并把 pget 安装的包目录插到 sys.path 最前面 */
static void pypp_py_init(void){
    Py_Initialize();
    const char* extra = getenv("PYPP_PYPATH");
    if(extra && *extra){
        PyObject* path = PySys_GetObject("path");
        PyObject* p = PyUnicode_FromString(extra);
        if(path && p) PyList_Insert(path, 0, p);
        Py_XDECREF(p);
    }
}
static void pypp_py_finalize(void){ Py_Finalize(); }

static PyObject* pypp_import(const char* name){
    PyObject* m = PyImport_ImportModule(name);
    if(!m){
        PyErr_Print();
        fprintf(stderr, "[py++] 提示：模块 '%s' 不可用，可执行  pget install %s  安装\n", name, name);
    }
    return m;
}

static PyObject* pypp_getattr(PyObject* o, const char* n){
    if(!o) return NULL;
    PyObject* r = PyObject_GetAttrString(o, n);
    if(!r) PyErr_Print();
    return r;
}

/* 复刻 CPython 的 "from X import Y" 语义：
   先取属性；取不到时，Y 很可能是 X 的子模块（如 from PIL import Image），
   于是再尝试导入 "X.Y"。 */
static PyObject* pypp_import_from(const char* mod, const char* name){
    PyObject* m = PyImport_ImportModule(mod);
    if(m){
        PyObject* v = PyObject_GetAttrString(m, name);
        if(v){ Py_DECREF(m); return v; }
        PyErr_Clear();
        Py_DECREF(m);
    } else {
        PyErr_Clear();
    }
    char buf[512];
    snprintf(buf, sizeof(buf), "%s.%s", mod, name);
    PyObject* sub = PyImport_ImportModule(buf);
    if(!sub){
        fprintf(stderr, "[py++] 无法从模块 '%s' 导入 '%s'\n", mod, name);
        PyErr_Print();
    }
    return sub;
}

/* 可变参数调用：pypp_callv(f, 2, a, b) */
static PyObject* pypp_callv(PyObject* f, int n, ...){
    if(!f) return NULL;
    va_list ap; va_start(ap, n);
    PyObject* t = PyTuple_New(n);
    for(int i = 0; i < n; i++){
        PyObject* a = va_arg(ap, PyObject*);
        PyTuple_SET_ITEM(t, i, a ? a : Py_NewRef(Py_None));
    }
    va_end(ap);
    PyObject* r = PyObject_CallObject(f, t);
    Py_DECREF(t);
    if(!r) PyErr_Print();
    return r;
}

/* ---- py++ 值 -> Python 对象 ---- */
static PyObject* pypp_num(double d){ return PyFloat_FromDouble(d); }
static PyObject* pypp_num_i(long long v){ return PyLong_FromLongLong(v); }
static PyObject* pypp_bool_py(long long v){ return PyBool_FromLong(v ? 1 : 0); }
static PyObject* pypp_str_py(const char* s){ return PyUnicode_FromString(s ? s : ""); }
static PyObject* pypp_list_py(pypp_list l){
    PyObject* o = PyList_New(0);
    if(!o) return NULL;
    pypp_list_body* B = l.b;
    if(!B) return o;
    for(long long i = 0; i < B->len; i++){
        PyObject* v;
        if(B->mode == 2)      v = PyUnicode_FromString(B->sdata[i].data ? B->sdata[i].data : "");
        else if(B->mode == 3) v = PyBool_FromLong(B->idata[i] ? 1 : 0);
        else if(B->mode == 1) v = PyFloat_FromDouble(B->fdata[i]);
        else                 v = PyLong_FromLongLong(B->idata[i]);
        if(v){ PyList_Append(o, v); Py_DECREF(v); }
    }
    return o;
}

/* ---- Python 对象 -> py++ 值 ---- */
/* 统一按 py++ 的格式规则输出：浮点与整数走和纯 C 路径完全相同的格式化，
   保证"原生模式"和"VM 模式"打印同一个值的结果一致。
   其余类型（字符串/列表/字典/None/自定义对象）交给 Python 的 str()。 */
static pypp_str pypp_pystr(PyObject* o){
    if(!o) return pypp_s("None");
    if(PyFloat_Check(o)){
        double d = PyFloat_AsDouble(o);
        char b[64];
        if(d == (double)(long long)d) snprintf(b, sizeof(b), "%.1f", d);
        else                          snprintf(b, sizeof(b), "%g", d);
        return pypp_s(b);
    }
    if(PyLong_Check(o) && !PyBool_Check(o)){
        long long v = PyLong_AsLongLong(o);
        if(!PyErr_Occurred()){
            char b[64];
            snprintf(b, sizeof(b), "%lld", v);
            return pypp_s(b);
        }
        PyErr_Clear();
    }
    PyObject* s = PyObject_Str(o);
    if(!s){ PyErr_Print(); return pypp_s(""); }
    const char* c = PyUnicode_AsUTF8(s);
    pypp_str r = pypp_s(c ? c : "");
    Py_DECREF(s);
    return r;
}
static void pypp_print_py(PyObject* o){
    pypp_print_str(pypp_pystr(o));
}
/* Python 对象作为 pypp_val 存入字典：保存 PyObject* 并 INCREF，
   打印时交给 Python 的 str()，保证与 VM 完全一致。 */
static pypp_str pypp_pyobj_repr_hook(void* p){ return pypp_pystr((PyObject*)p); }
static pypp_val pypp_v_obj_py(PyObject* o){
    if(o) Py_INCREF(o);
    return pypp_v_obj((void*)o, pypp_pyobj_repr_hook, 0);
}
static long long pypp_to_ll(PyObject* o){
    if(!o) return 0;
    PyObject* i = PyNumber_Long(o);
    if(!i){ PyErr_Clear(); return 0; }
    long long v = PyLong_AsLongLong(i);
    Py_DECREF(i);
    if(PyErr_Occurred()){ PyErr_Clear(); return 0; }
    return v;
}
static double pypp_to_double(PyObject* o){
    if(!o) return 0.0;
    PyObject* i = PyNumber_Float(o);
    if(!i){ PyErr_Clear(); return 0.0; }
    double v = PyFloat_AsDouble(i);
    Py_DECREF(i);
    if(PyErr_Occurred()){ PyErr_Clear(); return 0.0; }
    return v;
}
static long long pypp_len_py(PyObject* o){
    if(!o) return 0;
    Py_ssize_t n = PyObject_Length(o);
    if(n < 0){ PyErr_Clear(); return 0; }
    return (long long)n;
}
static PyObject* pypp_getitem(PyObject* o, PyObject* k){
    if(!o) return NULL;
    PyObject* r = PyObject_GetItem(o, k);
    if(!r) PyErr_Print();
    return r;
}
static void pypp_setattr(PyObject* o, const char* n, PyObject* v){
    if(!o) return;
    if(PyObject_SetAttrString(o, n, v) != 0) PyErr_Print();
}
static int pypp_contains(PyObject* c, PyObject* x){
    if(!c) return 0;
    int r = PySequence_Contains(c, x);
    if(r < 0){ PyErr_Clear(); return 0; }
    return r;
}
/* pypp_val -> PyObject*（把字典里取出的动态值传给 Python 函数） */
static PyObject* pypp_dict_py(pypp_dict d);
static PyObject* pypp_val_py(pypp_val v){
    switch(v.tag){
        case PYPP_V_FLOAT: return PyFloat_FromDouble(v.f);
        case PYPP_V_STR:   return PyUnicode_FromString(v.s.data ? v.s.data : "");
        case PYPP_V_BOOL:  return PyBool_FromLong(v.i);
        case PYPP_V_NONE:  return Py_NewRef(Py_None);
        case PYPP_V_OBJ: {
            /* 对象值以字符串形式交给 Python（保证不崩；深转换留待后续） */
            pypp_str s = v.repr_fn ? v.repr_fn(v.obj) : pypp_s("<object>");
            return PyUnicode_FromString(s.data ? s.data : "");
        }
        case PYPP_V_DICT:  return pypp_dict_py(*(pypp_dict*)v.obj);
        case PYPP_V_LIST:  return pypp_list_py(*(pypp_list*)v.obj);
        default:           return PyLong_FromLongLong(v.i);
    }
}
/* 字典 -> Python dict（把 py++ 字典传给 Python 函数，
   例如 json.dumps(d)。此前漏了这一支，字典被当成 int 直接编不过。 */
static PyObject* pypp_dict_py(pypp_dict d){
    PyObject* o = PyDict_New();
    if(!o) return NULL;
    pypp_dict_body* B = d.b;
    if(!B) return o;
    for(long long i = 0; i < B->len; i++){
        pypp_val k = B->keys[i];
        pypp_slot* ps = pypp_dict_slot(B, k, 0);
        pypp_val v = ps ? ps->val : pypp_v_none();
        PyObject* pk = pypp_val_py(k);
        PyObject* pv = pypp_val_py(v);
        if(pk && pv){ PyDict_SetItem(o, pk, pv); }
        Py_XDECREF(pk); Py_XDECREF(pv);
    }
    return o;
}
/* for x in <python 对象>：先物化成 Python 列表，再按下标取 */
static PyObject* pypp_iter_list(PyObject* o){
    if(!o) return NULL;
    PyObject* l = PySequence_List(o);
    if(!l){ PyErr_Print(); }
    return l;
}
static long long pypp_iter_len(PyObject* l){
    return l ? (long long)PyList_Size(l) : 0;
}
static PyObject* pypp_iter_at(PyObject* l, long long i){
    if(!l) return NULL;
    PyObject* v = PyList_GetItem(l, (Py_ssize_t)i);
    return v ? Py_NewRef(v) : NULL;
}
/* ===== end CPython bridge ===== */
'''
