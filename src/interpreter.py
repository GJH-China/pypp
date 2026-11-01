"""
py++ 字节码/树遍历解释器 (VM) —— 降级执行路径
当系统没有 gcc 时使用，保证 py++ 永远能跑起来（速度慢一些，但功能完整）。
作为"安全网"，优先使用简单的树遍历解释器。

关于 import：VM 本身就跑在 Python 里，所以 import 是"原生能力"——
任何能装进 Python 的第三方库，VM 模式下都能直接用，无需任何桥接。
"""

import importlib

from parser import *


class ReturnSignal(Exception):
    def __init__(self, value):
        self.value = value


class BreakSignal(Exception):
    pass


class ContinueSignal(Exception):
    pass


class Environment:
    def __init__(self, parent=None):
        self.vars = {}
        self.parent = parent

    def get(self, name):
        env = self
        while env:
            if name in env.vars:
                return env.vars[name]
            env = env.parent
        raise NameError(name)

    def has(self, name):
        env = self
        while env:
            if name in env.vars:
                return True
            env = env.parent
        return False

    def set(self, name, value):
        self.vars[name] = value

    def assign(self, name, value):
        env = self
        while env:
            if name in env.vars:
                env.vars[name] = value
                return
            env = env.parent
        self.vars[name] = value


class Function:
    def __init__(self, node: FuncDef, closure: Environment):
        self.node = node
        self.closure = closure

    def call(self, interp, args):
        env = Environment(self.closure)
        params = self.node.params
        defaults = self.node.defaults
        # 参数绑定，缺失则用默认值或 0（容错）
        for i, p in enumerate(params):
            if i < len(args):
                env.set(p, args[i])
            elif i < len(defaults) and defaults[i] is not None:
                env.set(p, interp.eval(defaults[i], env))
            else:
                env.set(p, 0)
        try:
            for stmt in self.node.body:
                interp.exec_stmt(stmt, env)
        except ReturnSignal as r:
            return r.value
        return 0


class VMInstance:
    """VM 里的类实例：字段存在 fields 里，方法通过 get_attr 动态绑定。"""

    def __init__(self, cls):
        self.cls = cls
        self.fields = {}

    def __repr__(self):
        return f"<{self.cls.node.name} object>"


class VMClass:
    def __init__(self, node: ClassDef, closure: Environment, interp=None):
        self.node = node
        self.closure = closure
        self.interp = interp
        self.methods = {m.name: m for m in node.methods}
        self.attrs = {}
        for a in node.attrs:
            if isinstance(a, Assign) and isinstance(a.target, Var):
                self.attrs[a.target.name] = 0

    def __call__(self, *args):
        """构造实例并执行 __init__（参数按普通调用传入）。"""
        inst = VMInstance(self)
        inst.fields.update(self.attrs)
        init = self.methods.get('__init__')
        if init:
            BoundMethod(self.interp, init, self.closure, inst)(*args)
        return inst

    def __repr__(self):
        return f"<class {self.node.name}>"


class BoundMethod:
    """把方法节点和实例绑在一起，调用时自动注入 self。"""

    def __init__(self, interp, node: FuncDef, closure: Environment, inst):
        self.interp = interp
        self.node = node
        self.closure = closure
        self.inst = inst

    def __call__(self, *args):
        env = Environment(self.closure)
        params = self.node.params
        defaults = self.node.defaults
        ai = 0
        for i, p in enumerate(params):
            if p == 'self':
                env.set('self', self.inst)
            elif ai < len(args):
                env.set(p, args[ai])
                ai += 1
            elif i < len(defaults) and defaults[i] is not None:
                env.set(p, self.interp.eval(defaults[i], env))
            else:
                env.set(p, 0)
        if not any(p == 'self' for p in params):
            env.set('self', self.inst)
        try:
            for stmt in self.node.body:
                self.interp.exec_stmt(stmt, env)
        except ReturnSignal as r:
            return r.value
        return 0


class Interpreter:
    def __init__(self, output=None, repair_report=None):
        self.globals = Environment()
        self.funcs = {}
        self.output = output or (lambda s: print(s, end=''))
        self.repair_report = repair_report
        self.warn = []
        self.extern_names = set()      # 声明过的 C 函数名（VM 无法真正调用）

    def run(self, program: Program):
        for stmt in program.body:
            self.exec_stmt(stmt, self.globals)

    # ---------- 语句 ----------
    def exec_stmt(self, node, env: Environment):
        if isinstance(node, Import):
            self.do_import(node, env)
            return
        if isinstance(node, FromImport):
            self.do_from_import(node, env)
            return
        if isinstance(node, ClassDef):
            env.set(node.name, VMClass(node, env, self))
            return
        if isinstance(node, ExternDecl):
            self.extern_names.add(node.name)
            return
        if isinstance(node, FuncDef):
            fn = Function(node, env)
            self.funcs[node.name] = fn
            env.set(node.name, fn)
            return
        if isinstance(node, If):
            cond = self.eval(node.cond, env)
            if self.truthy(cond):
                for s in node.body:
                    self.exec_stmt(s, env)
            else:
                for s in node.orelse:
                    self.exec_stmt(s, env)
            return
        if isinstance(node, While):
            guard = 0
            while self.truthy(self.eval(node.cond, env)):
                try:
                    for s in node.body:
                        self.exec_stmt(s, env)
                except BreakSignal:
                    break
                except ContinueSignal:
                    continue
                guard += 1
                if guard > 100_000_000:
                    self.warn.append("检测到可能的死循环，已自动中断")
                    break
            return
        if isinstance(node, For):
            it = self.eval(node.iter, env)
            if isinstance(it, list) or isinstance(it, range):
                for v in it:
                    guard = 0
                    env.assign(node.var, v)
                    try:
                        for s in node.body:
                            self.exec_stmt(s, env)
                    except BreakSignal:
                        break
                    except ContinueSignal:
                        continue
            elif isinstance(it, str):
                for ch in it:
                    env.assign(node.var, ch)
                    try:
                        for s in node.body:
                            self.exec_stmt(s, env)
                    except BreakSignal:
                        break
                    except ContinueSignal:
                        continue
            elif isinstance(it, dict):
                for k in list(it.keys()):
                    env.assign(node.var, k)
                    try:
                        for s in node.body:
                            self.exec_stmt(s, env)
                    except BreakSignal:
                        break
                    except ContinueSignal:
                        continue
            return
        if isinstance(node, Assign):
            val = self.eval(node.value, env)
            self.assign_target(node.target, val, env)
            return
        if isinstance(node, AugAssign):
            cur = self.eval(node.target, env)
            val = self.eval(node.value, env)
            opmap = {'PLUS_ASSIGN': '+', 'MINUS_ASSIGN': '-', 'STAR_ASSIGN': '*',
                     'SLASH_ASSIGN': '/', 'MOD_ASSIGN': '%', 'POW_ASSIGN': '**',
                     'FLOORDIV_ASSIGN': '//'}
            newv = self.binop(opmap[node.op], cur, val)
            self.assign_target(node.target, newv, env)
            return
        if isinstance(node, ExprStmt):
            self.eval(node.expr, env)
            return
        if isinstance(node, Return):
            raise ReturnSignal(self.eval(node.value, env) if node.value else 0)
        if isinstance(node, Break):
            raise BreakSignal()
        if isinstance(node, Continue):
            raise ContinueSignal()
        if isinstance(node, Pass):
            return

    # ---------- import ----------
    def do_import(self, node: Import, env: Environment):
        """import a / import a.b as c —— 与 CPython 语义一致：import a.b 绑定名字 a。"""
        for mod, alias in node.items:
            if not mod:
                continue
            try:
                if alias:
                    obj = importlib.import_module(mod)
                    bind = alias
                else:
                    top = mod.split('.')[0]
                    obj = importlib.import_module(top)
                    bind = top
                    if '.' in mod:
                        importlib.import_module(mod)   # 确保子模块也已加载
                env.set(bind, obj)
            except Exception as e:
                bind = alias or mod.split('.')[0]
                env.set(bind, 0)
                self.warn.append(
                    f"无法导入模块 '{mod}'（{type(e).__name__}: {e}），已按 0 处理；"
                    f"若为第三方库，可先执行：pget install {mod.split('.')[0]}")

    def do_from_import(self, node: FromImport, env: Environment):
        """from a import b as c / from a import *"""
        try:
            if node.level:
                pkg = self.globals.vars.get('__package__', '') or ''
                obj = importlib.import_module('.' * node.level + node.module, pkg)
            else:
                obj = importlib.import_module(node.module)
        except Exception as e:
            self.warn.append(
                f"无法导入模块 '{node.module}'（{type(e).__name__}: {e}）")
            for name, alias in node.names:
                if name != '*':
                    env.set(alias or name, 0)
            return
        for name, alias in node.names:
            if name == '*':
                for k in dir(obj):
                    if not k.startswith('_'):
                        env.set(k, getattr(obj, k))
                continue
            try:
                env.set(alias or name, getattr(obj, name))
            except AttributeError:
                env.set(alias or name, 0)
                self.warn.append(f"模块 '{node.module}' 中没有 '{name}'，已按 0 处理")

    def get_attr(self, obj, name):
        """真实的属性读取（模块 / 对象 / 内置类型统一走这里）。"""
        if isinstance(obj, VMInstance):
            if name in obj.fields:
                return obj.fields[name]
            m = obj.cls.methods.get(name)
            if m:
                return BoundMethod(self, m, obj.cls.closure, obj)
            if name in obj.cls.attrs:
                return obj.cls.attrs[name]
            self.warn.append(f"对象没有属性 '{name}'，已按 0 处理")
            return 0
        if isinstance(obj, VMClass):
            m = obj.methods.get(name)
            if m:
                return BoundMethod(self, m, obj.closure, None)
            if name in obj.attrs:
                return obj.attrs[name]
            self.warn.append(f"类没有属性 '{name}'，已按 0 处理")
            return 0
        try:
            return getattr(obj, name)
        except AttributeError:
            self.warn.append(f"对象没有属性 '{name}'，已按 0 处理")
            return 0
        except Exception as e:
            self.warn.append(f"读取属性 '{name}' 失败（{type(e).__name__}），已按 0 处理")
            return 0

    def assign_target(self, target, value, env):
        if isinstance(target, Var):
            # 首次在全局定义则进全局
            if not env.has(target.name) and env is not self.globals:
                env.set(target.name, value)
            else:
                env.assign(target.name, value)
        elif isinstance(target, Attr):
            obj = self.eval(target.obj, env)
            if isinstance(obj, VMInstance):
                obj.fields[target.name] = value
            elif isinstance(obj, VMClass):
                obj.attrs[target.name] = value
            else:
                try:
                    setattr(obj, target.name, value)
                except Exception:
                    self.warn.append(f"无法设置属性 '{target.name}'，已忽略")
        elif isinstance(target, Index):
            obj = self.eval(target.obj, env)
            idx = self.eval(target.index, env)
            try:
                obj[idx] = value
            except Exception:
                pass

    # ---------- 表达式 ----------
    def eval(self, node, env: Environment):
        if isinstance(node, Num):
            return node.value
        if isinstance(node, Str):
            return node.value
        if isinstance(node, Bool):
            return node.value
        if isinstance(node, NoneLit):
            return 0
        if isinstance(node, Var):
            if env.has(node.name):
                return env.get(node.name)
            # 容错：未定义变量 → 0
            self.warn.append(f"未定义的变量 '{node.name}'，已按 0 处理")
            return 0
        if isinstance(node, BinOp):
            l = self.eval(node.left, env)
            r = self.eval(node.right, env)
            return self.binop(node.op, l, r)
        if isinstance(node, UnaryOp):
            v = self.eval(node.operand, env)
            if node.op == 'not':
                return not self.truthy(v)
            return -v
        if isinstance(node, Compare):
            l = self.eval(node.left, env)
            r = self.eval(node.right, env)
            ops = {'==': lambda a, b: a == b, '!=': lambda a, b: a != b,
                   '<': lambda a, b: a < b, '>': lambda a, b: a > b,
                   '<=': lambda a, b: a <= b, '>=': lambda a, b: a >= b,
                   'in': lambda a, b: a in b,
                   'not in': lambda a, b: a not in b}
            try:
                return ops[node.op](l, r)
            except (TypeError, KeyError):
                return False
            except Exception:
                return False
        if isinstance(node, BoolOp):
            l = self.eval(node.left, env)
            if node.op == 'and':
                return self.eval(node.right, env) if self.truthy(l) else l
            else:
                return l if self.truthy(l) else self.eval(node.right, env)
        if isinstance(node, ListLit):
            return [self.eval(it, env) for it in node.items]
        if isinstance(node, DictLit):
            d = {}
            for k, v in zip(node.keys, node.values):
                d[self.eval(k, env)] = self.eval(v, env)
            return d
        if isinstance(node, Call):
            return self.eval_call(node, env)
        if isinstance(node, Index):
            obj = self.eval(node.obj, env)
            idx = self.eval(node.index, env)
            try:
                return obj[idx]
            except (TypeError, IndexError, KeyError):
                return 0
            except Exception:
                return 0
        if isinstance(node, Attr):
            return self.get_attr(self.eval(node.obj, env), node.name)
        return 0

    def binop(self, op, l, r):
        try:
            return self._binop(op, l, r)
        except TypeError:
            self.warn.append(f"运算 '{op}' 的操作数类型不支持，已按 0 处理")
            return 0
        except Exception as e:
            self.warn.append(f"运算 '{op}' 出错（{type(e).__name__}），已按 0 处理")
            return 0

    def _binop(self, op, l, r):
        if op == '+':
            return l + r
        if op == '-':
            return l - r
        if op == '*':
            return l * r
        if op == '/':
            if r == 0:
                self.warn.append("除数为 0，已按 0 处理")
                return 0
            return l / r
        if op == '//':
            if r == 0:
                self.warn.append("除数为 0，已按 0 处理")
                return 0
            return l // r
        if op == '%':
            if r == 0:
                self.warn.append("取模除数为 0，已按 0 处理")
                return 0
            return l % r
        if op == '**':
            return l ** r
        return 0

    def truthy(self, v):
        if v is None:
            return False
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)):
            return v != 0
        if isinstance(v, (str, list)):
            return len(v) > 0
        return True

    def eval_call(self, node: Call, env):
        # 内置
        if isinstance(node.func, Var):
            name = node.func.name
            args = [self.eval(a, env) for a in node.args]
            if name == 'print':
                self.do_print(args)
                return 0
            if name == 'input':
                if args:
                    self.do_print(args)
                try:
                    return input()
                except EOFError:
                    return ""
            if name == 'int':
                return int(args[0]) if args else 0
            if name == 'float':
                return float(args[0]) if args else 0.0
            if name == 'str':
                return self.py_str(args[0]) if args else ""
            if name == 'len':
                return len(args[0]) if args else 0
            if name == 'abs':
                return abs(args[0]) if args else 0
            if name == 'min':
                return min(args) if args else 0
            if name == 'max':
                return max(args) if args else 0
            if name == 'sum':
                return sum(args[0]) if args else 0
            if name == 'range':
                return list(range(*[int(x) for x in args]))
            if name == 'pow':
                return args[0] ** args[1]
            if name in self.extern_names:
                self.warn.append(
                    f"C 函数 '{name}' 需要原生模式才能调用（内置引擎不含 C 运行时）")
                return 0
            # 环境中的可调用对象：import 进来的函数（from math import floor）
            if env.has(name):
                val = env.get(name)
                if callable(val):
                    try:
                        return val(*args)
                    except TypeError:
                        for k in range(len(args) - 1, -1, -1):
                            try:
                                return val(*args[:k])
                            except TypeError:
                                continue
                        self.warn.append(f"调用 '{name}' 参数不匹配，已忽略")
                        return 0
                    except Exception as e:
                        self.warn.append(
                            f"调用 '{name}' 失败（{type(e).__name__}: {e}），已按 0 处理")
                        return 0
            if name in self.funcs:
                return self.funcs[name].call(self, args)
            # 容错：未知函数
            self.warn.append(f"未知函数 '{name}'，已忽略调用")
            return 0
        # 方法调用
        if isinstance(node.func, Attr):
            obj = self.eval(node.func.obj, env)
            method = node.func.name
            args = [self.eval(a, env) for a in node.args]
            # 通用路径：真实 Python 对象 / VM 实例 / 内置类型统一走 get_attr
            fn = self.get_attr(obj, method)
            if callable(fn):
                try:
                    return fn(*args)
                except TypeError:
                    # 容错：参数个数不对时，尽量少传几个再试
                    for k in range(len(args) - 1, -1, -1):
                        try:
                            return fn(*args[:k])
                        except TypeError:
                            continue
                    self.warn.append(f"调用 '{method}' 参数不匹配，已忽略")
                    return 0
                except Exception as e:
                    self.warn.append(
                        f"调用 '{method}' 失败（{type(e).__name__}: {e}），已按 0 处理")
                    return 0
            return 0
        return 0

    def py_str(self, v):
        if isinstance(v, bool):
            return 'True' if v else 'False'
        if isinstance(v, float):
            if v == int(v):
                return f"{v:.1f}"
            return f"{v:g}"
        return str(v)

    def do_print(self, args):
        parts = []
        for a in args:
            parts.append(self.py_str(a))
        self.output(" ".join(parts) + "\n")
