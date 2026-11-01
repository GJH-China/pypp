"""
py++ 测试套件
覆盖：词法、语法、类型推断、代码生成、容错修复、VM 执行。
运行：python tests/test_all.py
"""

import os
import sys
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, 'src')
sys.path.insert(0, SRC)

from lexer import Lexer
from parser import Parser, FuncDef, Assign, For, If
from codegen import CGenerator
from repair import RepairReport, fix_source
from interpreter import Interpreter
from pypp import compile_and_run, find_toolchain


def run_code(source, **kw):
    """便捷函数：用 VM 执行源码，返回 (output, result)。"""
    r = compile_and_run(source, force_vm=True, quiet=True, **kw)
    return r.output, r


def run_native_ok(source):
    """检查能否生成合法 C（不编译，只看生成结果是否含 main）。"""
    lx = Lexer(source, repair_log=[])
    toks = lx.tokenize()
    ps = Parser(toks, repair_log=[])
    prog = ps.parse()
    gen = CGenerator()
    return gen.generate(prog)


class TestLexer(unittest.TestCase):
    def test_basic_tokens(self):
        toks = Lexer("x = 1 + 2").tokenize()
        types = [t.type for t in toks]
        self.assertIn('IDENT', types)
        self.assertIn('ASSIGN', types)
        self.assertIn('PLUS', types)
        self.assertIn('INT', types)

    def test_string(self):
        toks = Lexer('s = "hello"').tokenize()
        strs = [t.value for t in toks if t.type == 'STRING']
        self.assertEqual(strs, ['hello'])

    def test_comment_skipped(self):
        toks = Lexer("# comment\nx = 1").tokenize()
        self.assertFalse(any(t.type == 'COMMENT' for t in toks))

    def test_indent_dedent(self):
        src = "if x:\n    a = 1\nb = 2"
        toks = Lexer(src).tokenize()
        types = [t.type for t in toks]
        self.assertIn('INDENT', types)
        self.assertIn('DEDENT', types)

    def test_float_literal(self):
        toks = Lexer("pi = 3.14").tokenize()
        f = [t for t in toks if t.type == 'FLOAT']
        self.assertEqual(len(f), 1)
        self.assertAlmostEqual(f[0].value, 3.14)


class TestParser(unittest.TestCase):
    def test_assignment(self):
        prog = Parser(Lexer("x = 42").tokenize()).parse()
        self.assertTrue(any(isinstance(s, Assign) for s in prog.body))

    def test_function(self):
        prog = Parser(Lexer("def f(a):\n    return a").tokenize()).parse()
        fns = [s for s in prog.body if isinstance(s, FuncDef)]
        self.assertEqual(len(fns), 1)
        self.assertEqual(fns[0].name, 'f')
        self.assertEqual(fns[0].params, ['a'])

    def test_for_loop(self):
        prog = Parser(Lexer("for i in range(10):\n    x = i").tokenize()).parse()
        self.assertTrue(any(isinstance(s, For) for s in prog.body))

    def test_if_else(self):
        src = "if a > 1:\n    x = 1\nelse:\n    x = 2"
        prog = Parser(Lexer(src).tokenize()).parse()
        self.assertTrue(any(isinstance(s, If) for s in prog.body))

    def test_missing_colon_repaired(self):
        # 缺少冒号应被容错处理，不抛异常
        src = "if a > 1\n    x = 1"
        prog = Parser(Lexer(src).tokenize()).parse()
        self.assertTrue(any(isinstance(s, If) for s in prog.body))


class TestRepair(unittest.TestCase):
    def test_spelling_fix(self):
        rep = RepairReport()
        out = fix_source("pritn('hi')", rep)
        self.assertIn('print', out)
        self.assertTrue(rep.has_repairs)

    def test_comment_not_touched(self):
        rep = RepairReport()
        out = fix_source("# pritn here\nprint('ok')", rep)
        self.assertIn('# pritn here', out)   # 注释保持原样
        self.assertFalse(rep.has_repairs)     # 不应误报

    def test_string_not_touched(self):
        rep = RepairReport()
        out = fix_source('s = "pritn"', rep)
        self.assertIn('"pritn"', out)
        self.assertFalse(rep.has_repairs)

    # ------------------------------------------------------------------
    # 新增：模糊拼写纠正（编辑距离）—— 覆盖硬编码表覆盖不到的变体
    # ------------------------------------------------------------------
    def test_fuzzy_print_variants(self):
        """用户报告过 prit 漏修，这里覆盖一批 print 变体。"""
        for wrong in ('prit', 'prnt', 'pirnt', 'pprint', 'printt'):
            rep = RepairReport()
            out = fix_source(f"{wrong}('hi')", rep)
            self.assertIn('print', out, f"{wrong} 未被修复")
            self.assertTrue(rep.has_repairs)

    def test_fuzzy_long_word_typos(self):
        """长词允许编辑距离 2。"""
        cases = {'whiel': 'while', 'reutrn': 'return', 'rnage': 'range',
                 'flaot': 'float', 'breka': 'break'}
        for wrong, right in cases.items():
            rep = RepairReport()
            out = fix_source(f"{wrong} x", rep)
            self.assertIn(right, out, f"{wrong} 应修成 {right}")

    def test_fro_not_guessed_as_from(self):
        """fro 必须修成 for 而不是 from —— 这是特例表存在的理由。"""
        rep = RepairReport()
        out = fix_source("fro i in range(3):\n    print(i)", rep)
        self.assertIn('for', out)
        self.assertNotIn('from', out)

    def test_user_variables_not_mangled(self):
        """关键防线：用户变量名绝不能被模糊纠正误伤。"""
        src = ("foo = 10\nnum = 20\nnn = 30\nnd = 40\n"
               "print(foo + num + nn + nd)")
        rep = RepairReport()
        out = fix_source(src, rep)
        for name in ('foo', 'num', 'nn', 'nd'):
            self.assertIn(name, out, f"用户变量 {name} 被误改了")
        self.assertFalse(rep.has_repairs, "不该有任何修复动作")

    def test_function_and_param_names_exempt(self):
        """函数名与参数名也应豁免。"""
        src = "def foo(num, nd):\n    return num + nd\nprint(foo(1, 2))"
        rep = RepairReport()
        out = fix_source(src, rep)
        self.assertIn('def foo', out)
        self.assertIn('num', out)
        self.assertFalse(rep.has_repairs)

    def test_short_words_not_guessed(self):
        """长度 <3 的词不猜，避免 nn->in、nd->and 这类误判。"""
        from repair import fuzzy_correct
        for w in ('nn', 'nd', 'fi', 'x', 'ab'):
            self.assertIsNone(fuzzy_correct(w), f"{w} 不应被猜测")

    def test_import_module_name_parts_exempt(self):
        """
        模块名的每一级都要豁免。

        回归用例：`from urllib.parse import quote` 里的 `parse` 编辑距离 1
        于 `pass` 且首字母相同，会被改成 pass，整行解析崩坏。
        """
        from repair import _collect_user_names
        names = _collect_user_names('from urllib.parse import quote')
        self.assertIn('parse', names)
        self.assertIn('urllib', names)
        self.assertIn('quote', names)

    def test_from_import_not_mangled(self):
        """端到端：from urllib.parse import quote 必须原样保留。"""
        src = 'from urllib.parse import quote\nprint(quote("a b"))\n'
        rep = RepairReport()
        out = fix_source(src, rep)
        self.assertIn('from urllib.parse import quote', out)
        self.assertNotIn('pass', out)
        self.assertFalse(rep.has_repairs, "不该有任何修复动作")

    def test_plain_import_module_parts_exempt(self):
        """import os.path —— path 也不能被改。"""
        src = 'import os.path\nprint(os.path.join("a", "b"))\n'
        rep = RepairReport()
        out = fix_source(src, rep)
        self.assertIn('import os.path', out)
        self.assertFalse(rep.has_repairs)

    def test_dot_attribute_names_exempt(self):
        """
        点后的属性名不参与拼写纠正。

        回归用例：f.close() 里的 close 与 class 编辑距离 2 且首字母相同，
        会被 fuzzy 纠错改成 class，连带把后续行语法弄崩；
        c.inc() 的 inc 与 int 距离 1，同样会被误伤。
        属性名是外部 API 的名字，和用户自定义名一样必须豁免。
        """
        src = ('c = 0\n'
               'c.inc()\n'
               'f = io.open("a.bin", "rb")\n'
               'f.close()\n'
               'print("done")\n')
        rep = RepairReport()
        out = fix_source(src, rep)
        self.assertIn('c.inc()', out)          # inc 与 int 距离 1，曾被误伤
        self.assertIn('f.close()', out)        # close 与 class 距离 2，曾被误伤
        self.assertNotIn('class', out)
        self.assertIn('print("done")', out)   # 后续行不能被连带弄崩
        self.assertFalse(rep.has_repairs)

    def test_dot_attribute_exempt_keeps_real_fix(self):
        """点了豁免后，独立标识符的真拼写错误仍要修（防止修过头）。"""
        from repair import _fix_line_spelling
        fixed, changes = _fix_line_spelling('pritn(x)', user_names={'x'})
        self.assertEqual(fixed, 'print(x)')

    def test_relative_from_import_recognized(self):
        """
        `from . import x` 相对导入必须被识别（原正则要求首字符是字母，
        整行都匹配不上，绑定名也就拿不到豁免）。
        """
        from repair import _collect_user_names, _FROM_IMPORT_RE
        m = _FROM_IMPORT_RE.match('from . import mod')
        self.assertIsNotNone(m, "相对导入没被正则识别")
        self.assertEqual(m.group(1), '.')
        self.assertIn('mod', _collect_user_names('from . import mod'))
        # 多级相对导入
        self.assertIn('pkg', _collect_user_names('from ..pkg import y'))

    def test_spelling_still_fixed(self):
        """加了豁免后，真拼写错误仍要修（防止修过头）。"""
        rep = RepairReport()
        out = fix_source('pritn("hi")', rep)
        self.assertIn('print', out)
        self.assertTrue(rep.has_repairs)


class TestBrokenLines(unittest.TestCase):
    """跨行断裂修复：一个名字被换行拆散时应自动拼接。"""

    def test_split_print_two_lines(self):
        rep = RepairReport()
        out = fix_source('prin\nf\n("a")\n', rep)
        self.assertIn('print', out)
        self.assertTrue(rep.has_repairs)

    def test_split_print_three_lines(self):
        rep = RepairReport()
        out = fix_source('pr\nin\nt("a")\n', rep)
        self.assertIn('print', out)
        self.assertTrue(rep.has_repairs)

    def test_split_keyword_with_tail(self):
        """形态 B：末行是"标识符前缀 + 剩余代码"。"""
        rep = RepairReport()
        out = fix_source('whi\nle x > 0:\n    pass\n', rep)
        self.assertIn('while x > 0:', out)
        self.assertTrue(rep.has_repairs)

    def test_split_return(self):
        rep = RepairReport()
        out = fix_source('ret\nurn 1\n', rep)
        self.assertIn('return 1', out)

    def test_user_original_multiline_case(self):
        """用户报告的例子：prin / f / ( ... ) 应拼成一个 print 调用。"""
        src = 'name = "py++"\nprin\nf\n("hello，" + name)\n'
        r = compile_and_run(src, quiet=True)
        self.assertEqual(r.mode, 'native')
        self.assertIn('hello，py++', r.output)
        msgs = [it[2] for it in r.report.items if it[0] == 'repair']
        self.assertTrue(any('拆散' in m for m in msgs), f"未报告断裂：{msgs}")

    def test_normal_code_not_merged(self):
        """正常多行代码绝不能被误合并。"""
        src = ('x = 1\ny = 2\nprint(x + y)\n'
               'def foo(a, b):\n    return a + b\nprint(foo(3, 4))\n')
        rep = RepairReport()
        out = fix_source(src, rep)
        self.assertFalse(rep.has_repairs, f"误改：{rep.items}")
        self.assertEqual(out, src)

    def test_user_variable_call_not_merged(self):
        """用户自己的函数名被拆，不应猜成内置名。"""
        rep = RepairReport()
        out = fix_source('myfunc\ntion\n(x)\n', rep)
        self.assertFalse(rep.has_repairs, f"误改：{rep.items}")

    def test_builtin_alone_not_merged(self):
        """print 独占一行后跟括号，不应被当成断裂。"""
        rep = RepairReport()
        fix_source('print\n(x)\n', rep)
        self.assertFalse(rep.has_repairs)


class TestFullwidth(unittest.TestCase):
    """全角标点归一化（中文输入法高频错误）。"""

    def test_fullwidth_parens(self):
        rep = RepairReport()
        out = fix_source('print（1）', rep)
        self.assertIn('print(1)', out)
        self.assertTrue(rep.has_repairs)

    def test_fullwidth_colon(self):
        rep = RepairReport()
        out = fix_source('if x：\n    print(1)', rep)
        self.assertIn('if x:', out)

    def test_fullwidth_comma_and_quote(self):
        rep = RepairReport()
        out = fix_source('print（"a"，“b”）', rep)
        self.assertIn('print("a","b")', out)

    def test_string_content_not_touched(self):
        """字符串里的全角标点是内容，绝不能改。"""
        rep = RepairReport()
        src = 'print("（全角，标点）")\n'
        out = fix_source(src, rep)
        self.assertIn('（全角，标点）', out)
        self.assertFalse(rep.has_repairs)

    def test_comment_not_touched(self):
        rep = RepairReport()
        out = fix_source('# 这是注释（含全角）\nprint(1)\n', rep)
        self.assertIn('（含全角）', out)
        self.assertFalse(rep.has_repairs)

    def test_fullwidth_program_runs(self):
        """全角括号的程序应能正常运行（此前会被丢弃导致结构破坏）。"""
        r = compile_and_run('print（"hello"）', quiet=True)
        self.assertTrue(r.success)
        self.assertIn('hello', r.output)

    def test_user_fullwidth_case(self):
        """用户报告的例子：全角括号应被归一化而不是'忽略'。"""
        src = 'name = "py++"\nprinf（l）\nf\n("hello，" + name)\n'
        r = compile_and_run(src, quiet=True)
        msgs = ' '.join(it[2] for it in r.report.items if it[0] == 'repair')
        self.assertIn('全角', msgs)          # 全角被处理
        self.assertNotIn('无法识别', msgs)    # 不再是"忽略"
        self.assertIn('prinf', msgs)         # 拼写也被指出
        self.assertIn('碎片', msgs)          # 悬空片段被明确告知


class TestCache(unittest.TestCase):
    """编译产物缓存：正确性优先于速度。"""

    def setUp(self):
        # 每次用独立缓存目录，避免相互干扰
        import tempfile
        self.cache_tmp = tempfile.mkdtemp(prefix='pypp_cache_test_')
        self._old = os.environ.get('PYPP_CACHE_DIR')
        os.environ['PYPP_CACHE_DIR'] = self.cache_tmp

    def tearDown(self):
        if self._old is None:
            os.environ.pop('PYPP_CACHE_DIR', None)
        else:
            os.environ['PYPP_CACHE_DIR'] = self._old
        shutil.rmtree(self.cache_tmp, ignore_errors=True)

    def test_cache_returns_correct_result(self):
        """命中缓存也必须返回正确结果（不能跑旧代码）。"""
        r1 = compile_and_run('print("first")', quiet=True)
        r2 = compile_and_run('print("first")', quiet=True)
        self.assertEqual(r1.output.strip(), 'first')
        self.assertEqual(r2.output.strip(), 'first')

    def test_cache_invalidated_on_change(self):
        """源码变了必须重新编译，不能复用旧产物。"""
        r1 = compile_and_run('print("AAA")', quiet=True)
        r2 = compile_and_run('print("BBB")', quiet=True)
        self.assertEqual(r1.output.strip(), 'AAA')
        self.assertEqual(r2.output.strip(), 'BBB')

    def test_cache_key_uses_generated_c(self):
        """缓存键基于生成的 C 代码，容错修复不同则不能串。"""
        from pypp import _cache_key
        tool = {'path': 'gcc', 'version': 'test'}
        k1 = _cache_key('int main(){return 0;}', tool)
        k2 = _cache_key('int main(){return 1;}', tool)
        self.assertNotEqual(k1, k2)

    def test_cache_key_includes_compiler(self):
        """换编译器必须让缓存失效。"""
        from pypp import _cache_key
        k1 = _cache_key('code', {'path': 'gccA', 'version': 'v1'})
        k2 = _cache_key('code', {'path': 'gccB', 'version': 'v1'})
        self.assertNotEqual(k1, k2)

    def test_no_cache_env_disables(self):
        """PYPP_NO_CACHE 应关闭缓存。"""
        os.environ['PYPP_NO_CACHE'] = '1'
        try:
            r = compile_and_run('print("nocache")', quiet=True)
            self.assertEqual(r.output.strip(), 'nocache')
            self.assertFalse(getattr(r, 'from_cache', False))
        finally:
            os.environ.pop('PYPP_NO_CACHE', None)


class TestUndefinedVarNotice(unittest.TestCase):
    """原生模式也必须报告未定义变量（此前只有 VM 会报）。"""

    def test_native_reports_undefined_var(self):
        rep = RepairReport()
        from pypp import compile_and_run
        r = compile_and_run("print(z)", quiet=True)
        self.assertEqual(r.mode, 'native')
        self.assertEqual(r.output.strip(), '0')
        # 关键：应留下"未定义变量"的修复记录
        msgs = [it[2] for it in r.report.items if it[0] == 'repair']
        self.assertTrue(any("'z'" in m and '未定义' in m for m in msgs),
                        f"未报告未定义变量，实际记录：{msgs}")

    def test_native_and_vm_both_report(self):
        """两条引擎对未定义变量的行为应一致。"""
        r_native = compile_and_run("print(z)", force_vm=False, quiet=True)
        r_vm = compile_and_run("print(z)", force_vm=True, quiet=True)
        n_msgs = [it[2] for it in r_native.report.items if it[0] == 'repair']
        v_msgs = [it[2] for it in r_vm.report.items if it[0] == 'repair']
        self.assertTrue(any('未定义' in m for m in n_msgs))
        self.assertTrue(any('未定义' in m for m in v_msgs))

    def test_original_user_case(self):
        """用户原始例子：prit(y) / print(z) 两个问题都要被处理。"""
        src = "prit(y)\nprint(z)"
        r = compile_and_run(src, quiet=True)
        msgs = [it[2] for it in r.report.items if it[0] == 'repair']
        joined = ' '.join(msgs)
        self.assertIn("prit", joined)          # 拼写被指出
        self.assertIn("'y'", joined)           # y 未定义
        self.assertIn("'z'", joined)           # z 未定义
        self.assertEqual(r.output.split(), ['0', '0'])


class TestInterpreter(unittest.TestCase):
    def test_arithmetic(self):
        out, _ = run_code("print(2 + 3 * 4)")
        self.assertEqual(out.strip(), '14')

    def test_float(self):
        out, _ = run_code("print(1.5 + 2.5)")
        self.assertEqual(out.strip(), '4.0')

    def test_string_concat(self):
        out, _ = run_code('print("a" + "b")')
        self.assertEqual(out.strip(), 'ab')

    def test_for_sum(self):
        out, _ = run_code("s = 0\nfor i in range(1, 11):\n    s += i\nprint(s)")
        self.assertEqual(out.strip(), '55')

    def test_while(self):
        out, _ = run_code("i = 0\nwhile i < 5:\n    i += 1\nprint(i)")
        self.assertEqual(out.strip(), '5')

    def test_function(self):
        src = "def add(a, b):\n    return a + b\nprint(add(3, 4))"
        out, _ = run_code(src)
        self.assertEqual(out.strip(), '7')

    def test_if_else(self):
        out, _ = run_code("x = 5\nif x > 3:\n    print(1)\nelse:\n    print(2)")
        self.assertEqual(out.strip(), '1')

    def test_list(self):
        out, _ = run_code("a = [1, 2, 3]\nprint(a[1])")
        self.assertEqual(out.strip(), '2')

    def test_undefined_var_tolerated(self):
        out, r = run_code("print(undefined_thing)")
        self.assertEqual(out.strip(), '0')
        self.assertTrue(r.success)

    def test_div_zero_tolerated(self):
        out, r = run_code("print(10 / 0)")
        self.assertTrue(r.success)

    def test_string_methods(self):
        out, _ = run_code('print("hello".upper())')
        self.assertEqual(out.strip(), 'HELLO')

    def test_no_paren_print(self):
        out, _ = run_code('print "no parens"')
        self.assertEqual(out.strip(), 'no parens')

    def test_nested_loop(self):
        src = ("c = 0\nfor i in range(3):\n"
               "    for j in range(3):\n        c += 1\nprint(c)")
        out, _ = run_code(src)
        self.assertEqual(out.strip(), '9')

    def test_bool_logic(self):
        out, _ = run_code("print(1 and 0)\nprint(1 or 0)")
        self.assertEqual(out.strip(), '0\n1')


class TestCodeGen(unittest.TestCase):
    def test_generates_main(self):
        c = run_native_ok("print(1)")
        self.assertIn('int main(void)', c)

    def test_range_compiles_to_for(self):
        c = run_native_ok("for i in range(10):\n    print(i)")
        self.assertIn('for (long long i = 0;', c)
        # 关键：range 循环不该在 main 里构造运行时数组
        main_part = c[c.find('int main'):]
        self.assertNotIn('pypp_range', main_part)

    def test_type_inference_int(self):
        c = run_native_ok("x = 42")
        self.assertIn('long long x = 42;', c)

    def test_type_inference_float(self):
        c = run_native_ok("x = 3.14")
        self.assertIn('double x = 3.14;', c)

    def test_function_generated(self):
        c = run_native_ok("def f(a):\n    return a + 1\nprint(f(1))")
        self.assertIn('static', c)
        self.assertIn('f(', c)

    def test_print_not_lost(self):
        c = run_native_ok('print("hi")')
        self.assertIn('pypp_print_str', c)

    def test_if_body_not_lost(self):
        c = run_native_ok("if 1:\n    print(1)")
        self.assertIn('pypp_print_int', c)


class TestPipeline(unittest.TestCase):
    def test_tolerant_program_runs(self):
        src = ('pritn("start")\n'
               'x = 10\n'
               'if x > 5\n'
               '    print("big")\n'
               'print(undefined)\n')
        out, r = run_code(src)
        self.assertTrue(r.success)
        self.assertIn('start', out)
        self.assertIn('big', out)
        self.assertGreaterEqual(r.repairs, 2)

    def test_valid_program_no_repair(self):
        out, r = run_code("x = 1\nprint(x)")
        self.assertEqual(r.repairs, 0)


class TestNative(unittest.TestCase):
    """原生编译路径测试（需要系统有可用 gcc）。"""

    @classmethod
    def setUpClass(cls):
        from pypp import find_toolchain
        cls.tool = find_toolchain()
        if not cls.tool:
            raise unittest.SkipTest("无可用 C 工具链，跳过原生测试")

    def test_native_hello(self):
        r = compile_and_run('print("native hello")', force_vm=False, quiet=True)
        self.assertTrue(r.success)
        self.assertEqual(r.mode, 'native')
        self.assertIn('native hello', r.output)

    def test_native_arithmetic(self):
        r = compile_and_run('x = 7\ny = 6\nprint(x * y)', force_vm=False, quiet=True)
        self.assertTrue(r.success)
        self.assertEqual(r.mode, 'native')
        self.assertIn('42', r.output)

    def test_native_loop(self):
        src = 's = 0\nfor i in range(1, 101):\n    s += i\nprint(s)'
        r = compile_and_run(src, force_vm=False, quiet=True)
        self.assertEqual(r.mode, 'native')
        self.assertEqual(r.output.strip(), '5050')

    def test_native_tolerates_undefined(self):
        """关键：未定义变量在原生路径也应被编译期补全，不降级。"""
        r = compile_and_run('print(never_defined)', force_vm=False, quiet=True)
        self.assertTrue(r.success)
        self.assertEqual(r.mode, 'native')   # 不应降级到 VM
        self.assertEqual(r.output.strip(), '0')

    def test_native_function(self):
        src = 'def sq(n):\n    return n * n\nprint(sq(9))'
        r = compile_and_run(src, force_vm=False, quiet=True)
        self.assertEqual(r.mode, 'native')
        self.assertIn('81', r.output)

    def test_native_sha256_pipeline(self):
        """import hashlib/io/os 的文件哈希流水线在原生模式完整可用。

        同时守住两个点：
        1. 嵌入 CPython 下 io.open / read / update / hexdigest 全链路正确；
        2. 路径经 stdin 传入（pypp 不向编译产物转发命令行参数）。
        """
        import hashlib
        import os
        import tempfile
        d = tempfile.mkdtemp(prefix='pypp_sha_')
        p = os.path.join(d, 't.bin')
        data = b'py++ sha256 pipeline' * 997      # 跨多个读取块
        with open(p, 'wb') as fh:
            fh.write(data)
        src = ('import io\n'
               'import os\n'
               'import hashlib\n'
               'path = input()\n'
               'if int(os.path.isfile(path)):\n'
               '    f = io.open(path, "rb")\n'
               '    h = hashlib.sha256()\n'
               '    while True:\n'
               '        c = f.read(4096)\n'
               '        if len(c) == 0:\n'
               '            break\n'
               '        h.update(c)\n'
               '    f.close()\n'
               '    print(h.hexdigest())\n')
        try:
            r = compile_and_run(src, stdin_data=p + '\n',
                                force_vm=False, quiet=True)
            self.assertEqual(r.mode, 'native')     # 不允许静默降级
            self.assertEqual(r.repairs, 0)
            self.assertEqual(r.output.strip(),
                             hashlib.sha256(data).hexdigest())
        finally:
            os.remove(p)
            os.rmdir(d)

    def test_vm_and_native_agree(self):
        """同一个程序，两条路径结果应一致。"""
        src = 't = 0\nfor i in range(0, 50):\n    t += i\nprint(t)'
        r_native = compile_and_run(src, force_vm=False, quiet=True)
        r_vm = compile_and_run(src, force_vm=True, quiet=True)
        self.assertEqual(r_native.output.strip(), r_vm.output.strip())


class TestCodeGenCompletion(unittest.TestCase):
    """编译期补全能力测试。"""

    def test_undefined_var_completed(self):
        c = run_native_ok("print(mystery)")
        self.assertIn('long long mystery = 0;', c)

    def test_defined_var_not_duplicated(self):
        c = run_native_ok("x = 5\nprint(x)")
        # 不应生成两处 x 定义
        self.assertEqual(c.count('long long x ='), 1)

    def test_function_name_not_completed(self):
        c = run_native_ok("def f():\n    return 1\nprint(f())")
        self.assertNotIn('long long f = 0;', c)


class TestInstaller(unittest.TestCase):
    """GUI 安装器的安装引擎测试（无界面，不污染真实环境）。"""

    def _run_install(self, **kw):
        import tempfile
        sys.path.insert(0, os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))))
        import install_gui as G
        tmp = tempfile.mkdtemp(prefix='pypp_test_install_')
        logs = []
        inst = G.Installer(
            target_dir=os.path.join(tmp, 'py++'),
            with_gcc=False,       # 不复制 557MB 工具链
            add_path=False,       # 不碰真实 PATH
            home_dir=os.path.join(tmp, '_home'),   # 不碰真实 ~/.pypp
            log=lambda m, lv='info': logs.append((lv, m)),
            **kw)
        ok = inst.run()
        return ok, tmp, logs, G

    def test_install_creates_core_files(self):
        ok, tmp, logs, G = self._run_install()
        try:
            self.assertTrue(ok, f"安装失败：{logs}")
            t = os.path.join(tmp, 'py++')
            for p in ('src/pypp.py', 'src/lexer.py', 'src/codegen.py',
                      'src/parser.py', 'src/interpreter.py', 'src/repair.py',
                      'examples/hello.cpy', 'uninstall.py', 'uninstall.bat'):
                self.assertTrue(os.path.isfile(os.path.join(t, p)),
                                f"缺少 {p}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_install_creates_launcher(self):
        ok, tmp, logs, G = self._run_install()
        try:
            self.assertTrue(ok)
            launcher = os.path.join(tmp, '_home', 'bin', 'pypp.bat')
            self.assertTrue(os.path.isfile(launcher))
            body = open(launcher, encoding='utf-8').read()
            self.assertIn('pypp.py', body)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_install_copies_icons_to_bindir(self):
        """图标要落到 ~/.pypp/bin/（注册表 DefaultIcon 指向那里）。"""
        ok, tmp, logs, G = self._run_install()
        try:
            self.assertTrue(ok)
            bindir = os.path.join(tmp, '_home', 'bin')
            for ico in ('cpy.ico', 'pypp.ico'):
                p = os.path.join(bindir, ico)
                self.assertTrue(os.path.isfile(p), f"缺少 {p}")
                # 必须是有效的 ICO（不是空文件/占位文本）
                with open(p, 'rb') as f:
                    head = f.read(4)
                self.assertEqual(head, b'\x00\x00\x01\x00',
                                 f"{ico} 不是有效的 ICO 文件")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_installed_compiler_runs(self):
        """安装后的编译器应当能直接跑示例。"""
        ok, tmp, logs, G = self._run_install()
        try:
            self.assertTrue(ok)
            r = subprocess.run(
                [sys.executable, os.path.join(tmp, 'py++', 'src', 'pypp.py'),
                 os.path.join(tmp, 'py++', 'examples', 'hello.cpy'),
                 '--no-color'],
                capture_output=True, text=True, timeout=180)
            self.assertEqual(r.returncode, 0)
            self.assertIn('hello', r.stdout.lower())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_skip_examples(self):
        ok, tmp, logs, G = self._run_install(install_examples=False)
        try:
            self.assertTrue(ok)
            self.assertFalse(os.path.isdir(os.path.join(tmp, 'py++', 'examples')))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestAssoc(unittest.TestCase):
    """双击运行器 + .cpy 文件关联。

    用内存假注册表替换 assoc 的三个原语，验证"写了哪些键、什么值"，
    不碰真实注册表（真实注册表的验收在开发机上单独做）。
    """

    def setUp(self):
        import assoc
        self.assoc = assoc
        self.fake = {}                      # path -> {name: value}

        def _set(path, value, name=''):
            self.fake.setdefault(path, {})[name] = value

        def _get(path, name=''):
            return self.fake.get(path, {}).get(name)

        def _del(path):
            for k in list(self.fake):
                if k == path or k.startswith(path + '\\'):
                    del self.fake[k]

        self._orig = (assoc._reg_set, assoc._reg_get,
                      assoc._reg_delete_tree)
        assoc._reg_set, assoc._reg_get, assoc._reg_delete_tree = \
            _set, _get, _del

        self.tmp = tempfile.mkdtemp(prefix='pypp_assoc_')
        self.bindir = os.path.join(self.tmp, 'bin')
        os.makedirs(self.bindir)

    def tearDown(self):
        (self.assoc._reg_set, self.assoc._reg_get,
         self.assoc._reg_delete_tree) = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- 注册 -------------------------------------------------------------
    def test_register_writes_keys(self):
        # 造一个同目录 pypprun.bat + cpy.ico（安装器写的，这里模拟它们已在位），
        # 走无告警路径
        open(os.path.join(self.bindir, 'pypprun.bat'), 'w').close()
        open(os.path.join(self.bindir, 'cpy.ico'), 'w').close()
        r = self.assoc.register(self.bindir)
        self.assertTrue(r['registered'])
        self.assertEqual(r['warning'], '')
        ext = r'Software\Classes\.cpy'
        progid = r'Software\Classes\pypp.cpy'
        cmd = progid + r'\shell\open\command'
        self.assertEqual(self.fake[ext].get(''), self.assoc.PROG_ID)
        self.assertEqual(self.fake[progid].get(''),
                         self.assoc.FRIENDLY_NAME)
        self.assertEqual(
            self.fake[cmd].get(''),
            f'"{os.path.join(self.bindir, "pypprun.bat")}" "%1"')

    def test_register_warns_without_runner_bat(self):
        """bat 不在位（安装器没生成）→ warning 但不阻断注册。"""
        r = self.assoc.register(self.bindir)
        self.assertTrue(r['registered'])
        self.assertIn('pypprun.bat', r['warning'])

    def test_register_overwrites_stale(self):
        """重复安装：直接覆盖旧键，不报错。"""
        open(os.path.join(self.bindir, 'pypprun.bat'), 'w').close()
        self.assoc.register(self.bindir)
        self.assoc.register(self.bindir)
        self.assertEqual(
            self.fake[r'Software\Classes\.cpy'].get(''),
            self.assoc.PROG_ID)

    def test_register_sets_default_icon(self):
        """bindir 里有 cpy.ico → 写 DefaultIcon 指向它。"""
        open(os.path.join(self.bindir, 'pypprun.bat'), 'w').close()
        open(os.path.join(self.bindir, 'cpy.ico'), 'w').close()
        r = self.assoc.register(self.bindir)
        self.assertTrue(r['registered'])
        self.assertEqual(r['icon'], os.path.join(self.bindir, 'cpy.ico'))
        ico_key = r'Software\Classes\pypp.cpy\DefaultIcon'
        self.assertEqual(
            self.fake[ico_key].get(''),
            os.path.join(self.bindir, 'cpy.ico'))

    def test_register_without_icon_still_succeeds(self):
        """没有图标文件 → 只提示，注册照常成功（图标是便利功能）。"""
        open(os.path.join(self.bindir, 'pypprun.bat'), 'w').close()
        r = self.assoc.register(self.bindir)
        self.assertTrue(r['registered'])
        self.assertIsNone(r['icon'])
        self.assertIn('cpy.ico', r['warning'])

    def test_unregister_removes_icon_too(self):
        """注销时 DefaultIcon 随整个 ProgID 树一起消失。"""
        open(os.path.join(self.bindir, 'pypprun.bat'), 'w').close()
        open(os.path.join(self.bindir, 'cpy.ico'), 'w').close()
        self.assoc.register(self.bindir)
        self.assoc.unregister(self.bindir)
        ico_key = r'Software\Classes\pypp.cpy\DefaultIcon'
        self.assertNotIn(ico_key, self.fake)

    # -- 注销（归属守卫是重点）--------------------------------------------
    def test_unregister_roundtrip(self):
        open(os.path.join(self.bindir, 'pypp.bat'), 'w').close()
        self.assoc.register(self.bindir)
        r = self.assoc.unregister(bindir=self.bindir)
        self.assertTrue(r['unregistered'])
        self.assertNotIn(r'Software\Classes\.cpy', self.fake)
        self.assertNotIn(r'Software\Classes\pypp.cpy', self.fake)
        st = self.assoc.status()
        self.assertFalse(st['registered'])

    def test_unregister_guard_foreign_ext(self):
        """别人的 .cpy 关联（默认值不是我们的 ProgID）绝不能被删。"""
        other = os.path.join(self.tmp, 'other', 'bin')
        self.fake[r'Software\Classes\.cpy'] = {'': 'notepad.file'}
        r = self.assoc.unregister(bindir=other)
        self.assertFalse(r['unregistered'])
        self.assertIn('跳过', r['skipped'])
        self.assertEqual(self.fake[r'Software\Classes\.cpy'].get(''),
                         'notepad.file')

    def test_unregister_guard_other_pypp_install(self):
        """命令指向别的 .pypp 安装（换了 home 重装过）→ 不动。"""
        stale = os.path.join(self.tmp, 'old_home', 'bin')
        fresh = os.path.join(self.tmp, 'new_home', 'bin')
        self.fake[r'Software\Classes\.cpy'] = {'': self.assoc.PROG_ID}
        self.fake[r'Software\Classes\pypp.cpy\shell\open\command'] = {
            '': f'"{os.path.join(stale, "pypprun.bat")}" "%1"'}
        r = self.assoc.unregister(bindir=fresh)
        self.assertFalse(r['unregistered'])
        self.assertIn('不属于本次安装', r['skipped'])

    def test_status_empty(self):
        st = self.assoc.status()
        self.assertFalse(st['registered'])
        self.assertIsNone(st['ext_default'])

    # -- 非 Windows ---------------------------------------------------------
    def test_non_windows_noop(self):
        orig = self.assoc._NOT_WINDOWS
        self.assoc._NOT_WINDOWS = True
        try:
            r = self.assoc.register(self.bindir)
            self.assertFalse(r['supported'])
            self.assertFalse(r['registered'])
            self.assertEqual(self.fake, {})
            u = self.assoc.unregister(bindir=self.bindir)
            self.assertFalse(u['unregistered'])
        finally:
            self.assoc._NOT_WINDOWS = orig

    # -- GUI 安装器接线 ------------------------------------------------------
    def test_gui_installer_assoc_step(self):
        """GUI 安装器 assoc=True：跑完整流程，运行器落地、状态 ok。"""
        sys.path.insert(0, os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))))
        import install_gui as G
        tmp = tempfile.mkdtemp(prefix='pypp_assoc_gui_')
        try:
            logs = []
            inst = G.Installer(
                target_dir=os.path.join(tmp, 'py++'),
                with_gcc=False, add_path=False,
                home_dir=os.path.join(tmp, '_home'),
                assoc=True,
                log=lambda m, lv='info': logs.append((lv, m)))
            ok = inst.run()
            self.assertTrue(ok, f"安装失败：{logs}")
            self.assertEqual(inst.assoc_status, 'ok')
            self.assertTrue(os.path.isfile(
                os.path.join(tmp, '_home', 'bin', 'pypprun.bat')))
            # 假注册表里应留下关联
            self.assertEqual(self.fake[r'Software\Classes\.cpy'].get(''),
                             self.assoc.PROG_ID)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_gui_installer_assoc_off(self):
        """assoc=False（自检路线）：不碰注册表。"""
        sys.path.insert(0, os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))))
        import install_gui as G
        tmp = tempfile.mkdtemp(prefix='pypp_assoc_off_')
        try:
            logs = []
            inst = G.Installer(
                target_dir=os.path.join(tmp, 'py++'),
                with_gcc=False, add_path=False,
                home_dir=os.path.join(tmp, '_home'),
                assoc=False,
                log=lambda m, lv='info': logs.append((lv, m)))
            ok = inst.run()
            self.assertTrue(ok, f"安装失败：{logs}")
            self.assertEqual(inst.assoc_status, 'skipped')
            self.assertEqual(self.fake, {})
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_gui_installer_default_is_hermetic(self):
        """不传 assoc 参数 → 绝不碰注册表。

        回归用例（真实事故）：assoc 曾默认 True，而 TestInstaller 系列
        构造 Installer 时都不传该参数 —— 跑一遍测试就把用户真实的
        .cpy 关联改写指向临时 home，用户双击弹"打开方式"且循环复现。
        碰用户环境的功能必须 opt-in，这是这条测试存在的理由。
        （bat 文件由 launcher 步骤写入临时 home，无害；本测试只守注册表。）
        """
        sys.path.insert(0, os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))))
        import install_gui as G
        tmp = tempfile.mkdtemp(prefix='pypp_assoc_hermetic_')
        try:
            inst = G.Installer(
                target_dir=os.path.join(tmp, 'py++'),
                with_gcc=False, add_path=False,
                home_dir=os.path.join(tmp, '_home'),
                log=lambda m, lv='info': None)
            ok = inst.run()
            self.assertTrue(ok)
            self.assertEqual(inst.assoc_status, 'skipped')
            self.assertEqual(self.fake, {},
                             "注册表原语被调用过——默认值必须是 opt-out")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestRunner(unittest.TestCase):
    """运行器 UI（src/runner.py）：介于 CLI 与 TUI 之间的命令行界面。"""

    def _import_runner(self):
        sys.path.insert(0, SRC)
        import runner
        return runner

    def test_runner_bat_content(self):
        """bat 只负责拉起 runner.py；CRLF；UTF-8 代码页。"""
        runner = self._import_runner()
        body = runner.runner_bat_content(
            r'C:\py\runtime\python.exe', r'C:\py++\src\runner.py')
        self.assertIn('chcp 65001', body)
        self.assertIn(r'"C:\py\runtime\python.exe"', body)
        self.assertIn(r'"C:\py++\src\runner.py"', body)
        self.assertIn('"%~1"', body)
        self.assertIn('\r\n', body)

    def test_runner_bat_pure_ascii(self):
        """bat 必须纯 ASCII。

        cmd.exe 按 OEM 代码页（中文 Windows = GBK）解析 chcp 之前的
        行；UTF-8 中文注释会字节错位，严重时把注释和下一行命令粘成
        一整行（实测：'…都在' 不是内部或外部命令，启动直接失败）。
        中文 UX 全部放 runner.py。
        """
        runner = self._import_runner()
        body = runner.runner_bat_content('py.exe', 'runner.py')
        bad = [c for c in body if ord(c) >= 128]
        self.assertEqual(bad, [], f'bat 含非 ASCII 字符: {bad}')

    def test_display_width_cjk(self):
        """框线对齐：CJK 记 2 个显示单元。"""
        runner = self._import_runner()
        self.assertEqual(runner._dw('abc'), 3)
        self.assertEqual(runner._dw('中文'), 4)
        self.assertEqual(runner._dw('py++ 运行器'),
                         4 + 1 + 3 * 2)   # ASCII + 空格 + 3 个 CJK

    def test_box_contains_rows(self):
        runner = self._import_runner()
        box = runner._box([('py++', '运行器 v1.1.0')])
        self.assertIn('py++', box)
        self.assertIn('运行器 v1.1.0', box)
        self.assertIn('╭', box)
        self.assertIn('╰', box)

    def test_run_file_missing(self):
        """文件不存在 → 错误框 + 退出码 1（pause=False 不挂起）。"""
        runner = self._import_runner()
        code = runner.run_file(r'C:\no\such\file.cpy', pause=False)
        self.assertEqual(code, 1)

    def test_run_file_happy_path(self):
        """真实跑一个 .cpy（stream=True）：退出码 0。"""
        runner = self._import_runner()
        tmp = tempfile.mkdtemp(prefix='pypp_runner_')
        try:
            cpy = os.path.join(tmp, 'hello.cpy')
            with open(cpy, 'w', encoding='utf-8') as f:
                f.write('print("runner smoke ok")\n')
            code = runner.run_file(cpy, pause=False)
            self.assertEqual(code, 0)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestNativeStream(unittest.TestCase):
    """stream 模式与 UTF-8 解码（双击/交互场景的两大地基）。"""

    @classmethod
    def setUpClass(cls):
        from pypp import find_toolchain
        cls.tool = find_toolchain()
        if not cls.tool:
            raise unittest.SkipTest("无可用 C 工具链，跳过原生测试")

    def test_native_output_utf8_not_gbk(self):
        """回归：中文输出必须按 UTF-8 解码。

        曾用 text=True（locale 默认，中文 Windows 是 GBK）——
        程序输出 UTF-8 字节让读取线程 UnicodeDecodeError 崩溃，
        整个 stdout 丢失。实测事故：bench.cpy 加中文提示后必崩。
        """
        r = compile_and_run('print("中文输出测试abc123")',
                            force_vm=False, quiet=True)
        self.assertEqual(r.mode, 'native')
        self.assertIn('中文输出测试abc123', r.output)

    def test_stream_mode_no_capture(self):
        """stream=True 不捕获子进程 stdio（output 为空，退出码带回）。"""
        r = compile_and_run('print("streamed")', force_vm=False,
                            quiet=True, stream=True)
        self.assertEqual(r.mode, 'native')
        self.assertEqual(r.output, '')
        self.assertEqual(r.exit_code, 0)


class TestImportParsing(unittest.TestCase):
    """import / from ... import 的语法解析。"""

    def _first(self, src):
        toks = Lexer(src, repair_log=[]).tokenize()
        ps = Parser(toks, repair_log=[])
        prog = ps.parse()
        return prog.body[0], ps.repair_log

    def test_plain_import(self):
        from parser import Import
        node, rep = self._first("import math")
        self.assertIsInstance(node, Import)
        self.assertEqual(node.items, [('math', '')])
        self.assertEqual(rep, [])

    def test_import_dotted(self):
        from parser import Import
        node, _ = self._first("import os.path")
        self.assertEqual(node.items, [('os.path', '')])

    def test_import_as(self):
        from parser import Import
        node, _ = self._first("import numpy as np")
        self.assertEqual(node.items, [('numpy', 'np')])

    def test_import_multiple(self):
        from parser import Import
        node, _ = self._first("import json, re as regex")
        self.assertEqual(node.items, [('json', ''), ('re', 'regex')])

    def test_from_import(self):
        from parser import FromImport
        node, rep = self._first("from math import sqrt, pi as PI")
        self.assertIsInstance(node, FromImport)
        self.assertEqual(node.module, 'math')
        self.assertEqual(node.names, [('sqrt', ''), ('pi', 'PI')])
        self.assertEqual(rep, [])

    def test_from_import_star(self):
        from parser import FromImport
        node, _ = self._first("from math import *")
        self.assertEqual(node.names, [('*', '')])

    def test_from_import_relative(self):
        from parser import FromImport
        node, _ = self._first("from .pkg.mod import thing")
        self.assertEqual(node.level, 1)
        self.assertEqual(node.module, 'pkg.mod')

    def test_from_import_parenthesized(self):
        from parser import FromImport
        node, rep = self._first("from a import (b, c)")
        self.assertEqual(node.names, [('b', ''), ('c', '')])
        self.assertEqual(rep, [])


class TestImportVM(unittest.TestCase):
    """VM 模式下的 import（VM 本身是 Python，天然支持）。"""

    def test_import_math(self):
        out, r = run_code("import math\nprint(math.sqrt(16))")
        self.assertTrue(r.success)
        self.assertEqual(out.strip(), '4.0')

    def test_from_import(self):
        out, r = run_code("from math import floor\nprint(floor(3.9))")
        self.assertEqual(out.strip(), '3')

    def test_from_import_alias(self):
        out, r = run_code("from math import ceil as up\nprint(up(3.1))")
        self.assertEqual(out.strip(), '4')

    def test_import_json_roundtrip(self):
        out, r = run_code("import json\nprint(json.dumps([1, 2, 3]))")
        self.assertEqual(out.strip(), '[1, 2, 3]')

    def test_import_os(self):
        out, r = run_code("import os\nprint(os.path.basename('/a/b/c.txt'))")
        self.assertEqual(out.strip(), 'c.txt')

    def test_import_missing_module_tolerated(self):
        """导入不存在的模块不能崩，应友好降级。"""
        out, r = run_code("import definitely_no_such_module_xyz\nprint('alive')")
        self.assertTrue(r.success)
        self.assertIn('alive', out)


class TestImportNative(unittest.TestCase):
    """原生模式下的 import（嵌入 CPython）。"""

    @classmethod
    def setUpClass(cls):
        from pypp import find_toolchain, find_python_dev
        if not find_toolchain():
            raise unittest.SkipTest("无可用 C 工具链")
        if not find_python_dev():
            raise unittest.SkipTest("无可嵌入的 CPython 开发环境")

    def test_native_import_math(self):
        r = compile_and_run("import math\nprint(math.sqrt(16))",
                            force_vm=False, quiet=True)
        self.assertTrue(r.success)
        self.assertEqual(r.mode, 'native')
        self.assertEqual(r.output.strip(), '4.0')

    def test_native_from_import(self):
        r = compile_and_run("from math import sqrt\nprint(sqrt(81))",
                            force_vm=False, quiet=True)
        self.assertEqual(r.mode, 'native')
        self.assertEqual(r.output.strip(), '9.0')

    def test_native_import_as(self):
        r = compile_and_run("import math as m\nprint(m.floor(2.9))",
                            force_vm=False, quiet=True)
        self.assertEqual(r.output.strip(), '2')

    def test_native_string_roundtrip(self):
        """Python 返回字符串 → py++ 侧 str 运算。"""
        r = compile_and_run(
            "import json\ns = json.dumps([1, 2])\nprint(len(s))",
            force_vm=False, quiet=True)
        self.assertEqual(r.mode, 'native')
        self.assertEqual(r.output.strip(), '6')     # "[1, 2]"

    def test_native_pyobj_to_int(self):
        r = compile_and_run("import math\nx = int(math.sqrt(50))\nprint(x)",
                            force_vm=False, quiet=True)
        self.assertEqual(r.output.strip(), '7')

    def test_native_star_import_falls_back_to_vm(self):
        """from x import * 原生表达不了，必须自动降级而不是出错。"""
        r = compile_and_run("from math import *\nprint(sqrt(25))",
                            force_vm=False, quiet=True)
        self.assertTrue(r.success)
        self.assertEqual(r.mode, 'vm')
        self.assertEqual(r.output.strip(), '5.0')

    def test_native_import_error_tolerated(self):
        r = compile_and_run("import no_such_module_abc\nprint('ok')",
                            force_vm=False, quiet=True)
        self.assertTrue(r.success)
        self.assertIn('ok', r.output)


class TestZeroOverhead(unittest.TestCase):
    """核心承诺：不用 import 的程序，生成的 C 不能有任何 Python 痕迹。"""

    def test_no_import_no_python(self):
        c = run_native_ok("s = 0\nfor i in range(10):\n    s += i\nprint(s)")
        self.assertNotIn('Python.h', c)
        self.assertNotIn('Py_Initialize', c)
        self.assertNotIn('pypp_callv', c)
        self.assertNotIn('PyObject', c)

    def test_import_injects_python(self):
        c = run_native_ok("import math\nprint(math.sqrt(2))")
        self.assertIn('Python.h', c)
        self.assertIn('Py_Initialize', c)
        self.assertIn('pypp_import', c)

    def test_uses_python_flag(self):
        lx = Lexer("print(1)")
        gen = CGenerator()
        gen.generate(Parser(lx.tokenize(), repair_log=[]).parse())
        self.assertFalse(gen.uses_python)

        lx2 = Lexer("import os\nprint(1)")
        gen2 = CGenerator()
        gen2.generate(Parser(lx2.tokenize(), repair_log=[]).parse())
        self.assertTrue(gen2.uses_python)

    def test_imported_name_not_completed_as_int(self):
        """import 进来的名字不能被当成未定义变量补成 long long。"""
        c = run_native_ok("import math\nprint(math.pi)")
        self.assertNotIn('long long math = 0;', c)
        self.assertIn('PyObject* math', c)


class TestPgetUnit(unittest.TestCase):
    """pget 的纯逻辑单元测试（不联网）。"""

    @classmethod
    def setUpClass(cls):
        if ROOT not in sys.path:
            sys.path.insert(0, ROOT)
        import pget
        cls.pget = pget

    def test_version_key_ordering(self):
        k = self.pget._ver_key
        self.assertLess(k('1.0.0'), k('1.0.1'))
        self.assertLess(k('1.9.0'), k('1.10.0'))
        self.assertLess(k('2.0.0b1'), k('2.0.0'))    # 预发布 < 正式

    def test_version_eq_ignores_trailing_zeros(self):
        self.assertTrue(self.pget._ver_eq('1.0', '1.0.0'))
        self.assertFalse(self.pget._ver_eq('1.0', '1.1'))

    def test_satisfies_operators(self):
        s = self.pget.satisfies
        self.assertTrue(s('2.5', [('>=', '2.0'), ('<', '3')]))
        self.assertFalse(s('3.1', [('>=', '2.0'), ('<', '3')]))
        self.assertTrue(s('1.0', [('==', '1.0')]))
        self.assertFalse(s('1.1', [('==', '1.0')]))
        self.assertTrue(s('2.2.5', [('~=', '2.2')]))
        self.assertFalse(s('3.0', [('~=', '2.2')]))
        self.assertTrue(s('9.9', [('!=', '1.0')]))

    def test_parse_spec(self):
        self.assertEqual(self.pget.parse_spec('requests'),
                         ('requests', []))
        self.assertEqual(self.pget.parse_spec('flask>=3.0'),
                         ('flask', [('>=', '3.0')]))
        self.assertEqual(self.pget.parse_spec('a>=1,<2'),
                         ('a', [('>=', '1'), ('<', '2')]))
        # extras 应被剥掉
        self.assertEqual(self.pget.parse_spec('requests[socks]>=2')[0],
                         'requests')

    def test_wheel_score_prefers_binary(self):
        ws = self.pget.wheel_score
        win = 'charset_normalizer-3.5.2-cp313-cp313-win_amd64.whl'
        pure = 'requests-2.34.2-py3-none-any.whl'
        s_win = ws(win, '3.13')
        s_pure = ws(pure, '3.13')
        self.assertIsNotNone(s_win)
        self.assertIsNotNone(s_pure)
        self.assertGreater(s_win, s_pure)

    def test_wheel_score_rejects_wrong_python(self):
        ws = self.pget.wheel_score
        # cp312 的二进制 wheel 不能用在 3.13 上
        self.assertIsNone(ws('foo-1.0-cp312-cp312-win_amd64.whl', '3.13'))
        # abi3 向后兼容，应接受
        self.assertIsNotNone(ws('foo-1.0-cp39-abi3-win_amd64.whl', '3.13'))

    def test_pick_wheel(self):
        files = [
            {'filename': 'x-1.0-py3-none-any.whl', 'packagetype': 'bdist_wheel'},
            {'filename': 'x-1.0.tar.gz', 'packagetype': 'sdist'},
        ]
        w = self.pget.pick_wheel(files, '3.13')
        self.assertIsNotNone(w)
        self.assertTrue(w['filename'].endswith('.whl'))

    def test_norm_name(self):
        self.assertEqual(self.pget._norm('Charset_Normalizer'), 'charset-normalizer')
        self.assertEqual(self.pget._norm('zope.interface'), 'zope-interface')


class TestPgetUnifiedInstall(unittest.TestCase):
    """pget install 的生态分流：PyPI / C / C++ 共用一条命令。"""

    @classmethod
    def setUpClass(cls):
        if ROOT not in sys.path:
            sys.path.insert(0, ROOT)
        import pget
        cls.pget = pget

    def test_pypi_names_not_hijacked_by_c(self):
        """纯 PyPI 包名不能被误判成 C 库。"""
        for n in ('requests', 'numpy', 'flask', 'django', 'pillow'):
            d = self.pget.detect_ecosystem(n)
            self.assertEqual(d['kind'], 'pypi',
                             f"{n} 被误判成 {d['kind']}")

    def test_known_c_lib_detected_when_available(self):
        """工具链里有 zlib.h 时，zlib/z 都应识别为 C 库。"""
        if not self.pget._find_c_header('zlib.h'):
            self.skipTest('本机工具链没有 zlib.h')
        for n in ('zlib', 'z'):
            d = self.pget.detect_ecosystem(n)
            self.assertEqual(d['kind'], 'c', n)
            self.assertEqual(d['link'], 'z', n)
            self.assertIsNotNone(d['header'], n)

    def test_cpp_stdlib_recognized(self):
        d = self.pget.detect_ecosystem('stdc++')
        self.assertEqual(d['kind'], 'cpp')
        self.assertEqual(d['link'], 'stdc++')
        self.assertIsNone(d['header'])      # 标准库没有头文件可转译

    def test_header_path_recognized(self):
        h = self.pget._find_c_header('zlib.h')
        if not h:
            self.skipTest('本机工具链没有 zlib.h')
        d = self.pget.detect_ecosystem(h)
        self.assertEqual(d['kind'], 'c')
        self.assertEqual(d['link'], 'zlib')

    def test_missing_c_lib_falls_back_to_pypi_with_hint(self):
        """名字像 C 库但本机没有 → 按 PyPI 处理，且要给出提示。"""
        pget = self.pget
        missing = None
        for n in ('sqlite3', 'curl', 'openssl', 'pcre2'):
            if (not pget._find_c_header(n + '.h')
                    and not pget._has_c_library(n)):
                missing = n
                break
        if not missing:
            self.skipTest('候选库在本机都存在，无法测回退')
        d = pget.detect_ecosystem(missing)
        self.assertEqual(d['kind'], 'pypi')
        self.assertIn('若确实要 C 库', d['reason'])

    def test_c_lib_candidates_cover_aliases(self):
        """别名要能归一到正确的链接名。"""
        cands = self.pget._c_lib_candidates('OpenSSL')
        self.assertIn(('ssl', 'openssl/ssl.h'), cands)
        cands = self.pget._c_lib_candidates('libz')
        self.assertIn(('z', 'zlib.h'), cands)

    def test_install_c_generates_cpyi(self):
        """cmd_install_c 应产出带 #link 的 .cpyi 声明文件。"""
        h = self.pget._find_c_header('zlib.h')
        if not h:
            self.skipTest('本机工具链没有 zlib.h')
        import tempfile
        tmp = tempfile.mkdtemp(prefix='pypp_cget_')
        old = os.environ.get('PYPP_PKG_DIR')
        os.environ['PYPP_PKG_DIR'] = tmp
        try:
            n, f = self.pget.cmd_install_c('zlib')
            self.assertEqual(f, 0)
            out = os.path.join(tmp, '_c', 'z.cpyi')
            self.assertTrue(os.path.isfile(out), out)
            text = open(out, encoding='utf-8').read()
            self.assertIn('#link z', text)
            self.assertIn('extern', text)
            self.assertIn('crc32', text)
        finally:
            if old is None:
                os.environ.pop('PYPP_PKG_DIR', None)
            else:
                os.environ['PYPP_PKG_DIR'] = old
            import shutil as _sh
            _sh.rmtree(tmp, ignore_errors=True)


class TestThreeTools(unittest.TestCase):
    """pyget / cget / cpget 三个平级命令。"""

    @classmethod
    def setUpClass(cls):
        if ROOT not in sys.path:
            sys.path.insert(0, ROOT)
        import pget
        cls.pget = pget

    def test_tool_kind_from_env(self):
        """环境变量决定工具身份。"""
        pget = self.pget
        old = os.environ.get('PYPP_PGET_TOOL')
        try:
            for k in ('py', 'c', 'cpp', 'auto'):
                os.environ['PYPP_PGET_TOOL'] = k
                self.assertEqual(pget._tool_kind(), k)
            # 非法值 → auto
            os.environ['PYPP_PGET_TOOL'] = 'bogus'
            self.assertEqual(pget._tool_kind(), 'auto')
        finally:
            if old is None:
                os.environ.pop('PYPP_PGET_TOOL', None)
            else:
                os.environ['PYPP_PGET_TOOL'] = old

    def test_tool_kind_from_argv0(self):
        """没有环境变量时，从脚本名推断（pyget.bat / cget.exe …）。"""
        pget = self.pget
        old = os.environ.pop('PYPP_PGET_TOOL', None)
        try:
            self.assertEqual(pget._tool_kind('pyget.bat'), 'py')
            self.assertEqual(pget._tool_kind('cget.bat'), 'c')
            self.assertEqual(pget._tool_kind('cpget.bat'), 'cpp')
            self.assertEqual(pget._tool_kind('pget.bat'), 'auto')
            self.assertEqual(pget._tool_kind('python.exe'), 'auto')
        finally:
            if old is not None:
                os.environ['PYPP_PGET_TOOL'] = old

    def test_help_per_tool(self):
        """三个命令的帮助标题互不相同，且带自己的名字。"""
        pget = self.pget
        seen = {}
        for k in ('py', 'c', 'cpp'):
            h = pget._build_help(k)
            first = h.splitlines()[0]
            self.assertIn(pget._PROG[k], first, k)
            seen[k] = first
        self.assertEqual(len(set(seen.values())), 3)

    def test_pyget_does_not_install_c_lib(self):
        """pyget 只装 Python 包 —— zlib 是 C 库名，不应产出 _c/*.cpyi。"""
        import tempfile
        tmp = tempfile.mkdtemp(prefix='pypp_pyget_')
        old = os.environ.get('PYPP_PKG_DIR')
        os.environ['PYPP_PKG_DIR'] = tmp
        try:
            rc = self.pget.cmd_install(['zlib'], kind='py')
            self.assertNotEqual(rc, 0, "pyget 装 C 库名应当失败")
            self.assertFalse(
                os.path.isdir(os.path.join(tmp, '_c')),
                "pyget 不该生成 C 库声明")
        finally:
            if old is None:
                os.environ.pop('PYPP_PKG_DIR', None)
            else:
                os.environ['PYPP_PKG_DIR'] = old
            import shutil as _sh
            _sh.rmtree(tmp, ignore_errors=True)

    def test_cpp_stdlib_install_name_is_kept(self):
        """stdc++ 的文件名会被净化，但 import 名必须保留 +++。"""
        h = self.pget._find_c_header('zlib.h')
        if not h:
            self.skipTest('本机工具链没有 zlib.h')
        import tempfile
        tmp = tempfile.mkdtemp(prefix='pypp_cppget_')
        old = os.environ.get('PYPP_PKG_DIR')
        os.environ['PYPP_PKG_DIR'] = tmp
        try:
            rc = self.pget.cmd_install(['stdc++'], kind='cpp')
            self.assertEqual(rc, 0)
            d = os.path.join(tmp, '_cpp')
            self.assertTrue(os.path.isdir(d))
            files = os.listdir(d)
            self.assertEqual(len(files), 1, files)
            # 文件名里不能有 +（Windows 非法）
            self.assertNotIn('+', files[0])
            text = open(os.path.join(d, files[0]), encoding='utf-8').read()
            self.assertIn('#link stdc++', text)
            self.assertIn('import stdc++', text)   # 提示要写对
        finally:
            if old is None:
                os.environ.pop('PYPP_PKG_DIR', None)
            else:
                os.environ['PYPP_PKG_DIR'] = old
            import shutil as _sh
            _sh.rmtree(tmp, ignore_errors=True)


class TestCImportExpansion(unittest.TestCase):
    """`import <C库>` 会被展开成 #link + extern 声明。"""

    @classmethod
    def setUpClass(cls):
        if os.path.join(ROOT, 'src') not in sys.path:
            sys.path.insert(0, os.path.join(ROOT, 'src'))
        if ROOT not in sys.path:
            sys.path.insert(0, ROOT)
        import pypp
        cls.pypp = pypp

    def _with_pkgdir(self, tmp):
        old = os.environ.get('PYPP_PKG_DIR')
        os.environ['PYPP_PKG_DIR'] = tmp
        return old

    def _restore(self, old):
        if old is None:
            os.environ.pop('PYPP_PKG_DIR', None)
        else:
            os.environ['PYPP_PKG_DIR'] = old

    def test_expand_c_import(self):
        import tempfile
        tmp = tempfile.mkdtemp(prefix='pypp_cimp_')
        old = self._with_pkgdir(tmp)
        try:
            d = os.path.join(tmp, '_c')
            os.makedirs(d)
            with open(os.path.join(d, 'z.cpyi'), 'w', encoding='utf-8') as f:
                f.write('#link z\nextern str zlibVersion()\n')
            src = 'import z\nprint(zlibVersion())\n'
            out, libs = self.pypp._expand_c_imports(src)
            self.assertEqual(libs, ['z'])
            self.assertIn('#link z', out)
            self.assertIn('extern str zlibVersion()', out)
            self.assertNotIn('\nimport z\n', out)
        finally:
            self._restore(old)
            import shutil as _sh
            _sh.rmtree(tmp, ignore_errors=True)

    def test_unknown_import_untouched(self):
        """没有对应 .cpyi 的 import 必须原样保留（交给 Python）。"""
        import tempfile
        tmp = tempfile.mkdtemp(prefix='pypp_cimp2_')
        old = self._with_pkgdir(tmp)
        try:
            os.makedirs(os.path.join(tmp, '_c'))
            src = 'import math\nimport os.path\n'
            out, libs = self.pypp._expand_c_imports(src)
            self.assertEqual(libs, [])
            self.assertEqual(out, src)
        finally:
            self._restore(old)
            import shutil as _sh
            _sh.rmtree(tmp, ignore_errors=True)

    def test_cpp_plus_name_import(self):
        """import stdc++ 也要能匹配到 _cpp/stdc__.cpyi。"""
        import tempfile
        tmp = tempfile.mkdtemp(prefix='pypp_cimp3_')
        old = self._with_pkgdir(tmp)
        try:
            d = os.path.join(tmp, '_cpp')
            os.makedirs(d)
            with open(os.path.join(d, 'stdc__.cpyi'), 'w', encoding='utf-8') as f:
                f.write('#link stdc++\n')
            out, libs = self.pypp._expand_c_imports('import stdc++\n')
            self.assertEqual(libs, ['stdc++'])
            self.assertIn('#link stdc++', out)
        finally:
            self._restore(old)
            import shutil as _sh
            _sh.rmtree(tmp, ignore_errors=True)


class TestPgetCLI(unittest.TestCase):
    """pget 命令行（隔离目录，不污染真实环境）。"""

    @classmethod
    def setUpClass(cls):
        import tempfile
        cls.pkgdir = tempfile.mkdtemp(prefix='pypp_pkg_test_')
        cls.env = dict(os.environ)
        cls.env['PYPP_PKG_DIR'] = cls.pkgdir
        cls.env['NO_COLOR'] = '1'

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.pkgdir, ignore_errors=True)

    def _pget(self, *args, timeout=120):
        return subprocess.run(
            [sys.executable, os.path.join(ROOT, 'pget.py'), *args],
            capture_output=True, text=True, env=self.env, timeout=timeout)

    def test_version(self):
        r = self._pget('--version')
        self.assertEqual(r.returncode, 0)
        self.assertIn('pget', r.stdout)

    def test_help(self):
        r = self._pget('--help')
        self.assertEqual(r.returncode, 0)
        for cmd in ('install', 'uninstall', 'list', 'freeze', 'doctor'):
            self.assertIn(cmd, r.stdout)

    def test_list_empty(self):
        r = self._pget('list')
        self.assertEqual(r.returncode, 0)

    def test_freeze_empty(self):
        r = self._pget('freeze')
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip(), '')

    def test_doctor(self):
        r = self._pget('doctor')
        self.assertEqual(r.returncode, 0)
        self.assertIn('目标 Python', r.stdout)

    def test_unknown_command(self):
        r = self._pget('frobnicate')
        self.assertNotEqual(r.returncode, 0)

    def test_uninstall_missing(self):
        r = self._pget('uninstall', 'not_installed_pkg_xyz')
        self.assertNotEqual(r.returncode, 0)


class TestPgetNetwork(unittest.TestCase):
    """需要访问 PyPI 的测试；网络不可用时自动跳过。"""

    @classmethod
    def setUpClass(cls):
        import tempfile
        cls.pkgdir = tempfile.mkdtemp(prefix='pypp_pkg_net_')
        cls.env = dict(os.environ)
        cls.env['PYPP_PKG_DIR'] = cls.pkgdir
        cls.env['NO_COLOR'] = '1'
        if ROOT not in sys.path:
            sys.path.insert(0, ROOT)
        import pget
        if pget.pypi_json('six') is None:
            shutil.rmtree(cls.pkgdir, ignore_errors=True)
            raise unittest.SkipTest("PyPI 不可达，跳过联网测试")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(getattr(cls, 'pkgdir', ''), ignore_errors=True)

    def _pget(self, *args, timeout=300):
        return subprocess.run(
            [sys.executable, os.path.join(ROOT, 'pget.py'), *args],
            capture_output=True, text=True, env=self.env, timeout=timeout)

    def test_install_and_uninstall_roundtrip(self):
        # six 是极小的纯 Python 包，适合做往返测试
        r = self._pget('install', 'six')
        self.assertEqual(r.returncode, 0, msg=r.stderr[-500:])
        self.assertTrue(os.path.isdir(self.pkgdir))

        r2 = self._pget('list')
        self.assertIn('six', r2.stdout.lower())

        r3 = self._pget('freeze')
        self.assertIn('six==', r3.stdout.lower())

        r4 = self._pget('uninstall', 'six')
        self.assertEqual(r4.returncode, 0)
        r5 = self._pget('list')
        self.assertNotIn('six', r5.stdout.lower())

    def test_installed_pkg_importable_by_vm(self):
        """pget 装完的包，py++ 的 VM 模式要能 import。"""
        r = self._pget('install', 'six')
        self.assertEqual(r.returncode, 0, msg=r.stderr[-500:])
        env = dict(self.env)
        env['PYPP_NO_COLOR'] = '1'
        code = os.path.join(self.pkgdir, '_probe.cpy')
        with open(code, 'w', encoding='utf-8') as f:
            f.write("import six\nprint(six.__version__)\n")
        rr = subprocess.run(
            [sys.executable, '-m', 'src.pypp', code, '--vm', '--no-color'],
            capture_output=True, text=True, env=env, cwd=ROOT, timeout=120)
        self.assertTrue(rr.stdout.strip(), msg=rr.stderr[-400:])
        # 输出应当是一个版本号（形如 1.17.0）
        last = rr.stdout.strip().splitlines()[-1]
        self.assertRegex(last, r'^\d+\.\d+')


def run_both(source):
    """同一段源码分别用原生与 VM 跑，返回 (native_out, vm_out)。"""
    r_native = compile_and_run(source, force_vm=False, quiet=True)
    r_vm = compile_and_run(source, force_vm=True, quiet=True)
    return r_native, r_vm


class TestDict(unittest.TestCase):
    """字典：字面量、读写、len、in、遍历。"""

    def test_dict_literal_parsed(self):
        from parser import DictLit
        lx = Lexer('d = {"a": 1, "b": 2}')
        prog = Parser(lx.tokenize(), repair_log=[]).parse()
        self.assertIsInstance(prog.body[0].value, DictLit)
        self.assertEqual(len(prog.body[0].value.keys), 2)

    def test_empty_dict(self):
        out, r = run_code('d = {}\nprint(len(d))')
        self.assertEqual(out.strip(), '0')

    def test_dict_read_write(self):
        src = ('d = {"a": 1}\n'
               'print(d["a"])\n'
               'd["b"] = 2\n'
               'print(d["b"])\n'
               'print(len(d))')
        out, r = run_code(src)
        self.assertEqual(out.split(), ['1', '2', '2'])

    def test_dict_mixed_types(self):
        src = ('d = {"s": "hi", "i": 3, "f": 2.5}\n'
               'print(d["s"])\nprint(d["i"])\nprint(d["f"])')
        out, r = run_code(src)
        self.assertEqual(out.split(), ['hi', '3', '2.5'])

    def test_dict_membership(self):
        src = ('d = {"a": 1}\n'
               'if "a" in d:\n    print("yes")\n'
               'if "z" not in d:\n    print("no")')
        out, r = run_code(src)
        self.assertEqual(out.split(), ['yes', 'no'])

    def test_dict_iteration_order(self):
        """遍历顺序必须与插入顺序一致（和 Python 对齐）。"""
        src = ('d = {}\n'
               'd["x"] = 1\nd["y"] = 2\nd["z"] = 3\n'
               'for k in d:\n    print(k)')
        out, r = run_code(src)
        self.assertEqual(out.split(), ['x', 'y', 'z'])

    def test_dict_get(self):
        src = 'd = {"a": 1}\nprint(d.get("a"))\nprint(d.get("z", 9))'
        out, r = run_code(src)
        self.assertEqual(out.split(), ['1', '9'])

    def test_dict_arithmetic_on_value(self):
        src = ('d = {"n": 1}\n'
               'd["n"] = d["n"] + 41\n'
               'print(d["n"])')
        out, r = run_code(src)
        self.assertEqual(out.strip(), '42')

    def test_dict_counter_pattern(self):
        src = ('words = ["a", "b", "a", "c", "a"]\n'
               'counts = {}\n'
               'for w in words:\n'
               '    counts[w] = counts.get(w, 0) + 1\n'
               'print(counts["a"])\n'
               'print(counts["b"])')
        out, r = run_code(src)
        self.assertEqual(out.split(), ['3', '1'])


class TestDictNative(unittest.TestCase):
    """字典在原生模式下同样要正确，且与 VM 结果一致。"""

    @classmethod
    def setUpClass(cls):
        from pypp import find_toolchain
        if not find_toolchain():
            raise unittest.SkipTest("无可用 C 工具链")

    SRC = ('d = {"name": "py++", "ver": 1, "pi": 3.14}\n'
           'print(d["name"])\n'
           'print(d["ver"])\n'
           'print(d["pi"])\n'
           'print(len(d))\n'
           'd["extra"] = "hi"\n'
           'for k in d:\n    print(k)\n'
           'print(d["ver"] + 1)\n')

    def test_native_dict_matches_vm(self):
        rn, rv = run_both(self.SRC)
        self.assertTrue(rn.success, msg=rn.error)
        self.assertEqual(rn.mode, 'native')
        self.assertEqual(rn.output, rv.output)

    def test_native_dict_key_order(self):
        rn, _ = run_both(self.SRC)
        keys = rn.output.strip().split('\n')[4:7]
        self.assertEqual(keys, ['name', 'ver', 'pi'])

    def test_native_dict_used_before_defined_not_mangled(self):
        """字典变量不能被编译期补全成 long long。"""
        c = run_native_ok('d = {}\nd["a"] = 1\nprint(len(d))')
        self.assertIn('pypp_dict d', c)
        self.assertNotIn('long long d = 0;', c)


class TestClass(unittest.TestCase):
    """类：定义、构造、字段、方法。"""

    SRC = ('class Point:\n'
           '    def __init__(self, x, y):\n'
           '        self.x = x\n'
           '        self.y = y\n'
           '    def total(self):\n'
           '        return self.x + self.y\n'
           '    def scale(self, k):\n'
           '        self.x = self.x * k\n'
           '        return self.x\n'
           '\n'
           'p = Point(3, 4)\n'
           'print(p.x)\n'
           'print(p.y)\n'
           'print(p.total())\n'
           'print(p.scale(10))\n'
           'print(p.x)\n')

    def test_class_parsed(self):
        from parser import ClassDef
        src = ('class Foo:\n'
               '    def __init__(self, a):\n'
               '        self.a = a\n'
               '    def get(self):\n'
               '        return self.a\n')
        prog = Parser(Lexer(src).tokenize(), repair_log=[]).parse()
        cd = prog.body[0]
        self.assertIsInstance(cd, ClassDef)
        self.assertEqual(cd.name, 'Foo')
        self.assertEqual([m.name for m in cd.methods], ['__init__', 'get'])

    def test_class_vm(self):
        out, r = run_code(self.SRC)
        self.assertEqual(out.split(), ['3', '4', '7', '30', '30'])

    def test_class_string_field(self):
        src = ('class Person:\n'
               '    def __init__(self, name, age):\n'
               '        self.name = name\n'
               '        self.age = age\n'
               '    def greet(self):\n'
               '        print("hi " + self.name)\n'
               '    def birthday(self):\n'
               '        self.age = self.age + 1\n'
               '        return self.age\n'
               '\n'
               'a = Person("py++", 3)\n'
               'a.greet()\n'
               'print(a.age)\n'
               'print(a.birthday())\n')
        out, r = run_code(src)
        self.assertEqual(out.split(), ['hi', 'py++', '3', '4'])

    def test_class_method_with_default(self):
        src = ('class Box:\n'
               '    def __init__(self, w):\n'
               '        self.w = w\n'
               '    def area(self, h=2):\n'
               '        return self.w * h\n'
               '\n'
               'b = Box(5)\n'
               'print(b.area())\n'
               'print(b.area(3))\n')
        out, r = run_code(src)
        self.assertEqual(out.split(), ['10', '15'])


class TestClassNative(unittest.TestCase):
    """类在原生模式下编译为 C 结构体 + 函数，且与 VM 结果一致。"""

    @classmethod
    def setUpClass(cls):
        from pypp import find_toolchain
        if not find_toolchain():
            raise unittest.SkipTest("无可用 C 工具链")

    SRC = TestClass.SRC

    def test_native_class_matches_vm(self):
        rn, rv = run_both(self.SRC)
        self.assertTrue(rn.success, msg=rn.error)
        self.assertEqual(rn.mode, 'native')
        self.assertEqual(rn.output, rv.output)

    def test_class_compiles_to_struct(self):
        c = run_native_ok(TestClass.SRC)
        self.assertIn('typedef struct {', c)
        self.assertIn('} Point;', c)
        self.assertIn('Point_new(', c)
        self.assertIn('Point_total(', c)

    def test_self_uses_arrow(self):
        """方法体里的 self 是指针，必须生成 self->x 而不是 self.x。"""
        c = run_native_ok(TestClass.SRC)
        self.assertIn('self->x', c)
        self.assertNotIn('self.x', c)

    def test_class_name_not_treated_as_variable(self):
        c = run_native_ok(TestClass.SRC)
        self.assertNotIn('long long Point = 0;', c)

    def test_inheritance_falls_back_to_vm(self):
        """继承暂不支持，应自动降级而不是编译失败。"""
        src = ('class A:\n'
               '    def __init__(self):\n'
               '        self.v = 1\n'
               'class B(A):\n'
               '    pass\n')
        r = compile_and_run(src, force_vm=False, quiet=True)
        self.assertTrue(r.success)


class TestClassNotMangledByRepair(unittest.TestCase):
    """回归：类名曾被容错层误改成 print（Point -> print，编辑距离 1）。"""

    def test_point_not_rewritten_to_print(self):
        src = ('class Point:\n'
               '    def __init__(self, x, y):\n'
               '        self.x = x\n'
               '        self.y = y\n'
               '\n'
               'p = Point(1, 2)\n'
               'print(p.x)\n')
        out, r = run_code(src)
        self.assertEqual(out.strip(), '1')

    def test_class_name_exempt_from_fuzzy(self):
        from repair import fix_source, RepairReport
        rep = RepairReport()
        fixed = fix_source('class Point:\n    pass\n', rep)
        self.assertIn('Point', fixed)
        self.assertNotIn('class print', fixed)

    def test_import_alias_exempt_from_fuzzy(self):
        """import 绑定的名字也不该被模糊纠正。"""
        from repair import fix_source, RepairReport
        rep = RepairReport()
        fixed = fix_source('import math\nimport numpy as np\nprint(np.pi)\n', rep)
        self.assertIn('import numpy as np', fixed)


class TestNestedContainers(unittest.TestCase):
    """容器嵌套：把 dict/list/对象放进字典时不能被拍平成字符串。

    这是曾经的真实 bug：`{"a": inner}` 取出来变成 "[dict]"，
    列表元素打印成 [a, b] 而不是 ['a', 'b']，两引擎输出不一致。
    """

    def test_dict_in_dict_native_matches_vm(self):
        src = 'inner = {"x": 1}\nouter = {"a": inner}\nprint(outer["a"])\n'
        n, v = run_both(src)
        self.assertTrue(n.success, msg=n.error)
        self.assertEqual(n.output, v.output)
        self.assertEqual(n.output.strip(), "{'x': 1}")

    def test_list_in_dict_native_matches_vm(self):
        src = 'arr = [1, 2, 3]\nd = {"v": arr}\nprint(d["v"])\n'
        n, v = run_both(src)
        self.assertTrue(n.success, msg=n.error)
        self.assertEqual(n.output, v.output)
        self.assertEqual(n.output.strip(), '[1, 2, 3]')

    def test_string_list_repr_has_quotes(self):
        """Python 里 print(["a","b"]) 输出 ['a', 'b']（带单引号）。"""
        src = 'print(["a", "b"])\n'
        n, v = run_both(src)
        self.assertEqual(n.output, v.output)
        self.assertEqual(n.output.strip(), "['a', 'b']")

    def test_print_dict_directly(self):
        """直接 print 一个字典变量（此前会生成非法的整数转换）。"""
        src = 'd = {"n": 1, "s": "hi"}\nprint(d)\n'
        n, v = run_both(src)
        self.assertTrue(n.success, msg=n.error)
        self.assertEqual(n.output, v.output)
        self.assertEqual(n.output.strip(), "{'n': 1, 's': 'hi'}")

    def test_empty_containers(self):
        src = 'a = {}\nb = []\nprint(a)\nprint(b)\n'
        n, v = run_both(src)
        self.assertEqual(n.output, v.output)
        self.assertEqual(n.output.split('\n')[0].strip(), '{}')
        self.assertEqual(n.output.split('\n')[1].strip(), '[]')

    def test_bool_list_repr(self):
        """print([True, False]) 必须是 [True, False]，不是 [1, 0]。"""
        src = 'print([True, False])\n'
        n, v = run_both(src)
        self.assertEqual(n.output, v.output)
        self.assertEqual(n.output.strip(), '[True, False]')

    def test_mixed_container_matrix(self):
        """一次性覆盖多种容器组合，两引擎必须逐字节一致。"""
        src = ('d = {"a": 1, "b": 2.5, "c": "s", "d": True}\n'
               'print(d)\n'
               'print(d["a"], d["b"], d["c"], d["d"])\n'
               'print([1, 2, 3])\n'
               'print(["x", "y"])\n'
               'print([True, False])\n'
               'inner = {"k": [1, 2]}\n'
               'outer = {"o": inner}\n'
               'print(outer)\n'
               'print(outer["o"])\n'
               'd["nested"] = {"z": 1}\n'
               'print(d["nested"])\n')
        n, v = run_both(src)
        self.assertTrue(n.success, msg=n.error)
        self.assertEqual(n.output, v.output)

    def test_dict_assigned_list_is_reference(self):
        """d["new"] = [9,9] 之后能正常取出来（曾因取地址失败而编译不过）。"""
        src = 'd = {}\nd["new"] = [9, 9]\nprint(d["new"])\n'
        n, v = run_both(src)
        self.assertTrue(n.success, msg=n.error)
        self.assertEqual(n.output, v.output)
        self.assertEqual(n.output.strip(), '[9, 9]')

    def test_nested_dict_reference_semantics(self):
        """引用语义（与 Python 一致）：放进字典后再改原字典，外边看得见。"""
        src = ('inner = {"x": 1}\n'
               'outer = {"a": inner}\n'
               'inner["x"] = 99\n'
               'print(outer["a"])\n')
        n, v = run_both(src)
        self.assertEqual(n.output, v.output)
        self.assertEqual(n.output.strip(), "{'x': 99}")

    def test_nested_list_reference_semantics(self):
        """同一个道理对列表成立：往原列表 append，字典里的也变。"""
        src = ('a = [1, 2]\n'
               'b = {"v": a}\n'
               'a.append(3)\n'
               'print(b["v"])\n')
        n, v = run_both(src)
        self.assertEqual(n.output, v.output)
        self.assertEqual(n.output.strip(), '[1, 2, 3]')


class TestBoolSemantics(unittest.TestCase):
    """布尔值必须打印 True/False，与 Python 一致（曾经打印 1/0）。"""

    def test_print_true_false(self):
        src = 'print(True)\nprint(False)\n'
        n, v = run_both(src)
        self.assertEqual(n.output, v.output)
        self.assertEqual(n.output.split('\n')[0].strip(), 'True')
        self.assertEqual(n.output.split('\n')[1].strip(), 'False')

    def test_comparison_is_bool(self):
        src = 'print(1 == 1)\nprint(1 < 2)\nprint(3 > 2 and 1 < 2)\n'
        n, v = run_both(src)
        self.assertEqual(n.output, v.output)
        for line in n.output.strip().split('\n'):
            self.assertIn(line.strip(), ('True', 'False'))

    def test_bool_in_dict(self):
        src = 'd = {"flag": True}\nprint(d)\nprint(d["flag"])\n'
        n, v = run_both(src)
        self.assertEqual(n.output, v.output)
        self.assertEqual(n.output.split('\n')[0].strip(), "{'flag': True}")

    def test_bool_still_arithmetic_compatible(self):
        """布尔参与算术仍然是 1/0（Python 语义：True+1 == 2）。"""
        src = 'print((3 > 2) + 1)\nx = 3 > 2\ny = x + 1\nprint(y)\n'
        n, v = run_both(src)
        self.assertEqual(n.output, v.output)
        self.assertEqual(n.output.split('\n')[0].strip(), '2')
        self.assertEqual(n.output.split('\n')[1].strip(), '2')

    def test_not_operator_is_bool(self):
        src = 'print(not True)\nprint(not False)\n'
        n, v = run_both(src)
        self.assertEqual(n.output, v.output)
        self.assertEqual(n.output.split('\n')[0].strip(), 'False')
        self.assertEqual(n.output.split('\n')[1].strip(), 'True')


class TestPythonInteropContainers(unittest.TestCase):
    """py++ 容器传给 Python 函数：此前字典被当成 int，直接编译不过。"""

    def test_dict_passed_to_python_func(self):
        src = ('import json\n'
               'd = {"a": 1, "b": [1, 2, 3]}\n'
               'print(json.dumps(d))\n')
        r = compile_and_run(src, force_vm=False, quiet=True)
        self.assertTrue(r.success, msg=r.error)
        self.assertEqual(r.mode, 'native')
        self.assertEqual(r.output.strip(), '{"a": 1, "b": [1, 2, 3]}')

    def test_list_passed_to_python_func(self):
        src = 'import json\nprint(json.dumps([1, 2, 3]))\n'
        r = compile_and_run(src, force_vm=False, quiet=True)
        self.assertTrue(r.success, msg=r.error)
        self.assertEqual(r.mode, 'native')
        self.assertEqual(r.output.strip(), '[1, 2, 3]')

    def test_nested_dict_with_bool_to_json(self):
        src = ('import json\n'
               'd = {"x": True, "y": {"z": 1}}\n'
               'print(json.dumps(d))\n')
        r = compile_and_run(src, force_vm=False, quiet=True)
        self.assertTrue(r.success, msg=r.error)
        self.assertEqual(r.mode, 'native')
        # 布尔必须是真 JSON 布尔（true），不是 1
        self.assertEqual(r.output.strip(), '{"x": true, "y": {"z": 1}}')

    def test_containers_to_python_match_vm(self):
        src = ('import json\n'
               'd = {"a": 1, "b": [1, 2, 3], "c": {"x": True}, "e": 2.5}\n'
               'print(json.dumps(d))\n'
               'print(json.dumps([1, 2, 3]))\n')
        n, v = run_both(src)
        self.assertTrue(n.success, msg=n.error)
        self.assertEqual(n.mode, 'native')
        self.assertEqual(n.output, v.output)


class TestStringList(unittest.TestCase):
    """字符串列表（此前 pypp_list 只能存数字）。"""

    def test_string_list_vm(self):
        out, r = run_code('a = ["x", "y", "z"]\nprint(a[1])\nprint(len(a))')
        self.assertEqual(out.split(), ['y', '3'])

    def test_string_list_iteration(self):
        src = 'a = ["x", "y"]\nfor v in a:\n    print(v)'
        out, r = run_code(src)
        self.assertEqual(out.split(), ['x', 'y'])

    def test_string_list_print(self):
        out, r = run_code('a = ["x", "y"]\nprint(a)')
        self.assertEqual(out.strip(), "['x', 'y']")

    def test_float_list(self):
        out, r = run_code('a = [1.5, 2.5]\nprint(a[0] + a[1])')
        self.assertEqual(out.strip(), '4.0')

    def test_native_string_list_matches_vm(self):
        from pypp import find_toolchain
        if not find_toolchain():
            self.skipTest("无可用 C 工具链")
        src = ('words = ["apple", "banana", "apple"]\n'
               'counts = {}\n'
               'for w in words:\n'
               '    counts[w] = counts.get(w, 0) + 1\n'
               'print(counts["apple"])\n')
        rn, rv = run_both(src)
        self.assertTrue(rn.success, msg=rn.error)
        self.assertEqual(rn.output, rv.output)
        self.assertEqual(rn.output.strip(), '2')


class TestExternParsing(unittest.TestCase):
    """extern C 函数声明的语法解析。"""

    def _first(self, src):
        prog = Parser(Lexer(src, repair_log=[]).tokenize(),
                      repair_log=[]).parse()
        return prog.body[0]

    def test_simple_extern(self):
        from parser import ExternDecl
        n = self._first("extern int puts(str s)")
        self.assertIsInstance(n, ExternDecl)
        self.assertEqual(n.name, 'puts')
        self.assertEqual(n.ret_type, 'int')
        self.assertEqual(n.params, [('str', 's')])

    def test_void_extern(self):
        n = self._first("extern void srand(unsigned int seed)")
        self.assertEqual(n.ret_type, 'void')
        self.assertEqual(n.params, [('unsigned int', 'seed')])

    def test_pointer_return(self):
        n = self._first("extern const char * getenv(str name)")
        self.assertEqual(n.name, 'getenv')
        self.assertEqual(n.ret_type, 'const char *')
        self.assertEqual(n.params, [('str', 'name')])

    def test_unsigned_long(self):
        n = self._first("extern unsigned long crc32(unsigned long crc, ptr buf)")
        self.assertEqual(n.ret_type, 'unsigned long')
        self.assertEqual(n.params,
                         [('unsigned long', 'crc'), ('ptr', 'buf')])

    def test_no_params(self):
        n = self._first("extern str zlibVersion()")
        self.assertEqual(n.params, [])

    def test_unnamed_params(self):
        n = self._first("extern int uncompress(ptr, ptr, ptr, unsigned long)")
        self.assertEqual([p[0] for p in n.params],
                         ['ptr', 'ptr', 'ptr', 'unsigned long'])


class TestExternCodegen(unittest.TestCase):
    """extern 生成的 C 原型与调用。"""

    def test_extern_prototype_emitted(self):
        c = run_native_ok("extern int puts(str s)\nputs(\"hi\")")
        self.assertIn('extern int puts(const char*);', c)
        self.assertIn('puts(', c)

    def test_extern_not_completed_as_variable(self):
        c = run_native_ok("extern int puts(str s)\nputs(\"hi\")")
        self.assertNotIn('long long puts = 0;', c)

    def test_unsigned_return_zero_extended(self):
        c = run_native_ok("extern unsigned long crc32(unsigned long c)\n"
                          "x = crc32(0)\nprint(x)")
        self.assertIn('unsigned long long', c)

    def test_link_directive(self):
        from pypp import _collect_link_flags
        self.assertEqual(_collect_link_flags("#link z\nprint(1)"), ['-lz'])
        self.assertEqual(_collect_link_flags("#link z m"), ['-lz', '-lm'])
        self.assertEqual(_collect_link_flags("#link -lm"), ['-lm'])
        self.assertEqual(_collect_link_flags("print(1)"), [])
        self.assertEqual(_collect_link_flags("#link  z  m"), ['-lz', '-lm'])


class TestExternNative(unittest.TestCase):
    """真正调用系统 C 库（用自带 mingw 里的 zlib）。"""

    @classmethod
    def setUpClass(cls):
        from pypp import find_toolchain
        tc = find_toolchain()
        if not tc:
            raise unittest.SkipTest("无可用 C 工具链")
        cls.prefix = tc['prefix']
        zlib_h = os.path.join(cls.prefix, 'include', 'zlib.h')
        if not os.path.isfile(zlib_h):
            raise unittest.SkipTest("工具链里没有 zlib.h")

    def test_zlib_crc32(self):
        src = ('#link z\n'
               'extern unsigned long crc32(unsigned long crc, str buf, '
               'unsigned int len)\n'
               'print(crc32(0, "hello", 5))\n')
        r = compile_and_run(src, force_vm=False, quiet=True)
        self.assertTrue(r.success, msg=r.error)
        self.assertEqual(r.mode, 'native')
        self.assertEqual(r.output.strip(), '907060870')

    def test_zlib_version_is_string(self):
        src = ('#link z\n'
               'extern str zlibVersion()\n'
               'v = zlibVersion()\n'
               'print(len(v) > 0)\n')
        r = compile_and_run(src, force_vm=False, quiet=True)
        self.assertTrue(r.success, msg=r.error)
        # 比较表达式是布尔值：Python 语义下打印 True（不是 1）
        self.assertEqual(r.output.strip(), 'True')

    def test_vm_reports_extern_honestly(self):
        """VM 没有 C 运行时，必须明确告知而不是静默给 0。"""
        src = '#link z\nextern int crc32(int a)\nprint(crc32(0))\n'
        r = compile_and_run(src, force_vm=True, quiet=True)
        self.assertTrue(any('原生模式' in str(it) for it in r.report.items))

    # ── 回归：extern 参数个数错会段错误（2026-10-19 实测） ────────────
    #
    # 历史：用户把 crc32 写成 `extern int crc32(const char*)`（真实是 3 个
    # 参数），py++ 照着错声明生成调用 → 寄存器错位 → 解引用野指针 →
    # 段错误。而当时 pypp 把退出码吞了，对外还显示"完成 运行 xxx ms"， 
    # 调用方完全无法察觉（ok=true, output=""）。
    # 两道防线：
    #   ① 编译前用真实头文件签名核对，参数个数不符直接报错
    #   ② 子进程崩溃（退出码非 0）必须 success=False

    def test_extern_arity_mismatch_is_rejected(self):
        """参数个数不符：编译期报错，不生成会崩的代码。"""
        if not find_toolchain():
            self.skipTest('需要 gcc')
        src = ('#link z\n'
               'extern int crc32(const char*)\n'
               'print(crc32("abc"))\n')
        r = compile_and_run(src, quiet=True)
        self.assertFalse(r.success)
        self.assertIn('个参数', r.error or '')

    def test_extern_correct_arity_still_works(self):
        """正确签名不能被误伤（zlibVersion 是 0 参数，带 ZEXPORT 宏）。"""
        if not find_toolchain():
            self.skipTest('需要 gcc')
        src = ('#link z\n'
               'extern unsigned long crc32(unsigned long, const char*, unsigned int)\n'
               'print(crc32(0, "abc", 3))\n')
        r = compile_and_run(src, quiet=True)
        self.assertTrue(r.success, msg=r.error)
        self.assertEqual(r.output.strip(), '891568578')

    def test_crash_does_not_fall_back_to_vm(self):
        """运行期崩溃不得被"降级到 VM"掩盖成正常输出。

        ⚠️ 这是 2026-10-19 实测事故的回归测试，构造必须精确：
          · extern 声明参数个数不足（crc32 真实 3 参，只声明 1 参）
          · 必须**绕过** 2.9 的签名校验，否则测不到崩溃路径
          · 崩溃标志是退出码 3221225477（0xC0000005 访问违规）
        旧行为：崩溃 → success=True、mode=vm、output="0\n"（VM 兜出个 0）
        新行为：success=False、mode=native、明确报"运行期崩溃"
        """
        if not find_toolchain():
            self.skipTest('需要 gcc')
        import pypp as _pypp
        saved = _pypp._validate_externs
        _pypp._validate_externs = lambda *a, **k: []   # 故意关掉校验
        try:
            r = compile_and_run(
                '#link z\nextern int crc32(const char*)\nprint(crc32("abc"))\n',
                quiet=True)
        finally:
            _pypp._validate_externs = saved

        # 若环境导致没崩溃（例如 gcc 版本差异），跳过而不是误报
        if r.exit_code in (0, None) and r.mode == 'native':
            self.skipTest('本机未复现崩溃，跳过')

        self.assertEqual(r.mode, 'native',
                         '崩溃后不得降级到 VM（会掩盖真实错误）')
        self.assertFalse(r.success, '运行期崩溃必须 success=False')
        self.assertIn('崩溃', r.error or '')

class TestCbindTranspile(unittest.TestCase):
    """pget cbind：C 头文件 → py++ extern 声明。"""

    @classmethod
    def setUpClass(cls):
        if ROOT not in sys.path:
            sys.path.insert(0, ROOT)
        import pget
        cls.pget = pget

    def test_basic_transpile(self):
        code, n = self.pget.transpile_header(
            "int add(int a, int b);\n"
            "double scale(double x);\n"
            "void nothing(void);\n")
        self.assertEqual(n, 3)
        self.assertIn('extern int add(int a, int b)', code)
        self.assertIn('extern double scale(double x)', code)
        self.assertIn('extern void nothing()', code)

    def test_const_char_pointer_becomes_str(self):
        code, _ = self.pget.transpile_header("const char *name(void);\n")
        self.assertIn('extern str name()', code)

    def test_other_pointer_becomes_ptr(self):
        code, _ = self.pget.transpile_header("void *alloc(unsigned long n);\n")
        self.assertIn('ptr', code)

    def test_typedef_resolution(self):
        code, _ = self.pget.transpile_header(
            "typedef unsigned long uLong;\n"
            "uLong crc(uLong seed);\n")
        self.assertIn('extern unsigned long crc(unsigned long seed)', code)

    def test_export_macro_stripped(self):
        code, _ = self.pget.transpile_header(
            "ZEXTERN int ZEXPORT foo(void);\n")
        self.assertIn('extern int foo()', code)

    def test_unknown_type_skipped(self):
        code, n = self.pget.transpile_header(
            "int ok(int a);\n"
            "int bad(UnknownStruct s);\n")
        self.assertEqual(n, 1)
        self.assertIn('# 跳过 bad', code)

    def test_link_directive_emitted(self):
        code, _ = self.pget.transpile_header("int f(void);\n", libname='z')
        self.assertIn('#link z', code)

    def test_struct_by_value_skipped(self):
        code, n = self.pget.transpile_header(
            "struct S { int x; };\n"
            "int use(struct S s);\n"
            "int fine(int a);\n")
        self.assertEqual(n, 1)


# ---------------------------------------------------------------------------
# Web 服务：C 库示例的引擎判定
# ---------------------------------------------------------------------------
# 背景：线上反馈「在线试运行 c_lib 无输出」。查出来是两个问题：
#   1. 服务器没有 gcc → 静默降级 VM → extern 全返回 0，看起来像没输出
#   2. mode 用 `'原生模式' in stderr` 判定，而 VM 下 C 库的修复提示里
#      也有「需要原生模式才能调用」，导致 **VM 被误判成 native**
# 这组测试把两条都钉住。
# ---------------------------------------------------------------------------

_WEB_SRV = os.path.join(ROOT, 'web', 'server.py')


def _load_web_server():
    """加载 web/server.py 为模块（它不在 sys.path 上）。"""
    import importlib.util
    if not os.path.isfile(_WEB_SRV):
        return None
    spec = importlib.util.spec_from_file_location('_pypp_web_srv', _WEB_SRV)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class TestWebServerCfg(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = _load_web_server()
        if cls.m is None:
            raise unittest.SkipTest('web/server.py 不存在')

    def test_needs_native_detects_extern(self):
        self.assertTrue(self.m._needs_native('extern int abs(int n)\n'))
        self.assertTrue(self.m._needs_native('#link z\n'))
        self.assertTrue(self.m._needs_native('  #link  z\n'))

    def test_needs_native_false_for_plain_code(self):
        self.assertFalse(self.m._needs_native('print("hi")\n'))
        self.assertFalse(self.m._needs_native('# 只是注释\nprint(1)\n'))
        self.assertFalse(self.m._needs_native(''))

    def test_c_lib_example_is_detected_as_native(self):
        p = os.path.join(ROOT, 'examples', 'c_lib.cpy')
        if not os.path.isfile(p):
            self.skipTest('缺少 c_lib.cpy')
        with open(p, encoding='utf-8') as f:
            self.assertTrue(self.m._needs_native(f.read()))

    def test_plain_example_not_detected(self):
        p = os.path.join(ROOT, 'examples', 'hello.cpy')
        if not os.path.isfile(p):
            self.skipTest('缺少 hello.cpy')
        with open(p, encoding='utf-8') as f:
            self.assertFalse(self.m._needs_native(f.read()))

    def test_detect_mode_native(self):
        err = ('[py++] 引擎: 原生高速模式 (gcc.exe)  |  文件: t.cpy\n'
               '[py++] 完成 运行 3ms  (原生模式 · 复用上次编译结果)\n')
        self.assertEqual(self.m._detect_mode(err), 'native')

    def test_detect_mode_vm_with_clib_warning_is_not_native(self):
        """⚠️ 回归测试：VM 下 C 库的提示里含「需要原生模式」，
        不能被子串匹配误判成 native。"""
        err = ('[py++ 温馨提示] 检测到你的代码有 5 处小错误，并无大碍，'
               'py++ 已兼容运行。\n'
               "  [已自动修复]  C 函数 'crc32' 需要原生模式才能调用"
               "（内置引擎不含 C 运行时）\n"
               '[py++] 完成 运行 3ms  (VM 模式)\n')
        self.assertEqual(self.m._detect_mode(err), 'vm')

    def test_detect_mode_engine_line_only(self):
        err = '[py++] 引擎: 内置 VM 模式  |  文件: t.cpy\n'
        self.assertEqual(self.m._detect_mode(err), 'vm')

    def test_detect_mode_defaults_to_vm(self):
        self.assertEqual(self.m._detect_mode(''), 'vm')
        self.assertEqual(self.m._detect_mode('随便什么输出'), 'vm')

    def test_c_lib_unsupported_flag_logic(self):
        """需要原生 + 落到 VM → 必须置 c_lib_unsupported。"""
        m = self.m
        code = 'extern int abs(int n)\nprint(abs(-1))\n'
        native = m._needs_native(code) and m._detect_mode(
            '[py++] 引擎: 原生高速模式') != 'native'
        vm = m._needs_native(code) and m._detect_mode(
            '[py++] 引擎: 内置 VM 模式') != 'native'
        self.assertFalse(native)   # 原生模式：不算 unsupported
        self.assertTrue(vm)        # VM 模式：算 unsupported


if __name__ == '__main__':
    unittest.main(verbosity=2)