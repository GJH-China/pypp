# py++

> Python-like syntax. Near-C++ speed. Small mistakes still run.

**English** | [简体中文](README.md)

py++ is a programming language that tries to take the best of both worlds:

- **Python's syntax** — indentation blocks, no semicolons, `print("hello")` just works
- **C++'s speed** — compiled to native machine code; measured within 1.02–1.12x of equivalent C++
- **Built-in tolerance** — code with small mistakes still runs; py++ repairs it and tells you what it fixed
- **The whole Python ecosystem** — `import` drives a real CPython, so any PyPI package works
- **Direct C library access** — `extern` declarations + `#link`; header files can be translated automatically

Source file extension: **`.cpy`**

Current version: **1.1.0**

<details>
<summary>What's new in 1.1.0</summary>

- **Classes** (`class`) — compiled to C structs + functions, zero runtime cost
- **Dictionaries** (`dict`) — hash table plus an insertion-order array; iteration order matches Python
- **C interop** — `extern` declarations, `#link`, and `cbind` header translation
- **Three package managers** — `pyget` (Python) / `cget` (C) / `cpget` (C++), each with one job
- **`import` handles all three** — `import requests` / `import z` / `import stdc++`
- Fixed dict/list reference semantics (mutations now propagate through containers)
- Fixed boolean printing (`True`/`False` instead of `1`/`0`)
- Fixed compilation failure when passing dicts/lists to Python functions (e.g. `json.dumps(d)`)
- Ships a **Windows installer exe** with a bundled portable Python runtime — no Python required on the target machine

</details>

---

## Quick start

Want a guided tour? Read **《py++ 快速精通》** ([`docs/快速精通.md`](docs/快速精通.md)) —
a from-zero-to-fluent guide, including an honest list of current limitations. (Chinese only.)

Most users only need the installer:

```bash
# 1. Download and run pypp-1.1.0-setup.exe, click through the wizard.
#    The installer bundles a Python runtime — you do NOT need Python installed.

# 2. Open a new terminal and run your first program
pypp examples/hello.cpy

# 3. See the error tolerance
pypp examples/tolerant.cpy

# 4. Install libraries — one command per ecosystem
pyget install requests     # Python packages (PyPI)
cget  install zlib         # C libraries
cpget install stdc++       # C++ libraries

pypp examples/use_requests.cpy

# 5. Hash a file — a full `import hashlib` pipeline (chunked reads, identical on both engines)
pypp examples/sha256.cpy
```

### Running from source (developers)

No installation needed after cloning:

```bash
python src/pypp.py examples/hello.cpy
```

Only two prerequisites:

- **Python 3.10+** (verified on 3.13)
- **A C compiler (gcc)** — this decides which engine you get

| gcc available | Engine | Speed |
|---|---|---|
| Yes | Native | Near C++ (measured 1.02–1.12x slower) |
| No | Built-in VM | Much slower; guarantees it still runs |

> ⚠️ **This repository does not include a gcc toolchain** (~1.4 GB, unsuitable for version control).
> On Windows, grab [winlibs](https://winlibs.com/) MinGW-w64 (unzip and go),
> or install MSYS2 and run `pacman -S mingw-w64-x86_64-gcc`.

Check the first line of output to see which engine ran:

```
[py++] 引擎: 原生高速模式 (gcc.exe)    ← native (what you want)
[py++] 引擎: 内置 VM 模式              ← gcc not found
```

To install as a global command and enable double-click `.cpy` execution:

```bash
python install_gui.py       # graphical wizard (or double-click install_gui.bat)
```

---

## Double-click to run `.cpy`

The installer enables "run `.cpy` on double-click" by default. Once installed:

- **Double-click** any `.cpy` file to run it — no need to build an exe
- A runner window (between a CLI and a TUI; works in cmd, PowerShell and Windows Terminal):
  header box (file / directory / engine) → **live streamed** program output →
  footer box (engine / elapsed / exit code) → `press any key to close`
- `input()` prompts appear **as they happen** and accept typing immediately —
  no more "prints everything at the end and you type blind"
- The runner changes to the script's directory, so relative paths like
  `open("neighbour.txt")` resolve correctly
- **Drag and drop** a `.cpy` onto `pypprun.bat` works too
- Uninstalling cleans everything up; no orphan registry keys

Implementation and limits:

| Item | Detail |
| --- | --- |
| Registry location | Only `HKCU\Software\Classes` (current user) — **no admin rights needed** |
| Runner | `<home>/bin/pypprun.bat` (pure ASCII; just `chcp 65001` + launch `src/runner.py`); all UI / streaming / pausing logic lives in Python |
| Why the bat is pure ASCII | cmd parses lines before `chcp` using the OEM code page (GBK on Chinese Windows); UTF-8 Chinese comments shift bytes and glue comments onto commands, breaking the script |
| Ownership guard | Uninstall/modify only removes **its own** keys: the `.cpy` default value must be `pypp.cpy`, and the command must point at this installation |
| Limits | Windows only; does not steal an existing association, re-installing overwrites the old keys |

CLI usage is unchanged: `pypp yourprogram.cpy`. In an interactive terminal the output
is also streamed live; piped/CI usage automatically falls back to capturing (same behaviour as before).

```bash
python install.py               # install (registers the association by default)
python install.py --no-assoc    # install without registering the association
python install.py --uninstall   # uninstall (removes the association too)
```

---

## An example

```python
# examples/hello.cpy
name = "py++"
print("hello, " + name)

total = 0
for i in range(1, 101):
    total += i
print("1 to 100 =", total)
```

Output:

```
hello, py++
1 to 100 = 5050
```

---

## Error tolerance (the headline feature)

Write code with a few small mistakes; py++ repairs and runs it, with a friendly note:

```python
pritn("start")       # typo
x = 10
if x > 5             # missing colon
    print("over 5")
print(y)             # y never defined
```

Output:

```
[py++ 温馨提示] 检测到你的代码有 3 处小错误，并无大碍，py++ 已兼容运行。
  [已自动修复] 第 1 行 'pritn' 疑似拼写错误，已自动更正为 'print'
  [已自动修复] 第 3 行 此处缺少 :，已自动补全，程序照常运行
  [已自动修复]  未定义的变量 'y'，已按 0 处理
start
over 5
0
```

**The program ran to completion.** That is the single biggest difference from other languages.

### Design principle: smart at compile time, zero cost at run time

All of these repairs happen **during compilation**. The generated machine code is clean —
there is no "check on every loop iteration" overhead. This is exactly what lets py++ stay fast.

### How the repairs are decided

Not by brute-forcing an error list, but in four escalating levels:

| Level | Mechanism | Example |
| --- | --- | --- |
| 1 | Full-width punctuation normalisation | `print（x）` → `print(x)` (very common with Chinese IMEs) |
| 2 | Special-case table (things algorithms get wrong) | `fro` → `for` (similarity would pick `from`) |
| 3 | Edit-distance similarity | `prit` / `prnt` / `whiel` → corrected |
| 4 | **Cross-line structural evaluation** | when a name is split across lines, join neighbours before deciding |

Level 4 is worth a look:

```python
name = "py++"
prin
f
("hello, " + name)
```

The intent was `print("hello, " + name)`, split across three lines.

**Line-by-line repair** would turn `prin` into `print`, leaving `print` / `f` / `(...)` as three
separate statements — the meaning is destroyed, yet it still reports "fixed".

py++ instead **evaluates neighbouring lines together**: `prin`+`f` → `prinf`, distance 1 from
`print`, so it merges them correctly. Output is `hello, py++`, matching the intent.

### Honest limits

py++ only fixes what it can be **certain** about. When it cannot decide, it says so rather than guessing:

```python
prinf（l）
f
("hello, " + name)
```

Here `prinf` becomes `print` and the full-width parentheses are normalised, but `f` and the
trailing bracket are **dangling fragments** with several plausible intentions. py++ leaves them
alone and tells you:

```
[已自动修复] 第 3 行的 'f' 看起来是某个函数名被敲断后留下的碎片…
        py++ 无法确定你的本意，已原样保留。建议检查是否想写 print(...)
```

**Why refusing to fix is better than guessing**: a wrong fix teaches the user that broken code is correct.

Other limits:
- Similarity matching only applies to builtin names and keywords — this is not semantic understanding
- User-defined names are never guessed at, so `foo` and `num` can never be "corrected"
- Cross-language idioms (`cout << x`) are out of scope — that is a language difference, not a typo

---

## Two execution engines

| Engine | Trigger | Speed | How |
| --------- | ------------- | ------ | ------------------------ |
| **Native** | gcc/clang present | Near C++ | generate C → `gcc -O2` → machine code |
| **VM** | No C compiler | Slower but works | built-in tree-walking interpreter |

Detection is automatic; you can force the VM:

```bash
pypp code.cpy --vm
```

### Compile-time speed

**1. Build artifact cache (on by default)**

If the source has not changed, the previously built executable is reused:

| Scenario | Time |
| --- | --- |
| First run (compiles) | ~440 ms |
| Repeat run (cache hit) | **~10–50 ms** |

The cache key is the hash of the generated C plus a compiler fingerprint plus the py++ version.
Because the key is based on the **repaired** C, renaming things or changing repair results
triggers a rebuild — you never run a stale binary.

Cache lives in `~/.pypp/cache`:

```bash
PYPP_NO_CACHE=1 pypp code.cpy     # disable for one run
```

**2. Lightweight toolchain detection**

Early versions compiled a hello-world to validate the toolchain on every run (~289 ms).
Now it just runs `gcc --version` (~13 ms) — a real compile failure already falls back to the VM,
so no pre-flight check is needed.

---

## Using Python libraries (`import` + `pyget`)

This is what separates py++ from a toy language: **it can use the entire Python ecosystem**.

```python
# examples/use_requests.cpy
import requests
from urllib3 import __version__

r = requests.get("https://api.github.com")
print("status:", r.status_code)
print("urllib3:", __version__)
```

### How it works

The key idea: **py++ does not reimplement Python libraries — it runs real CPython.**

| Engine | How `import` works | Result |
| --- | --- | --- |
| **Native** | embeds the CPython interpreter **into** the produced executable, calls it via the C API | real, complete, binary extensions work |
| **VM** | the VM itself is Python, so `import` is native to it | real, complete |

In other words: **any library that installs into Python also works in py++**, including
C-extension packages (NumPy, Pillow, lxml…), because the actual binaries are what execute.

### Zero cost: no `import`, no overhead

This matters. **The Python bridge only appears in the generated C if you actually write `import`.**

| Program | What the generated C contains |
| --- | --- |
| `print("hi")` | pure C — no `Python.h`, no `Py_Initialize` |
| `import math` + call | CPython bridge injected |

So the "near C++" promise is not diluted — **you pay for Python interop only when you use it.**

py++'s own arithmetic remains plain C instructions; only values **coming from Python libraries**
are carried as `PyObject*` (internal type name `pyobj`), with attribute access, method calls and
indexing translated into the corresponding C API calls.

### Package managers: pyget / cget / cpget

Three peer commands, one job each:

| Command | Manages | Installs to | Use in `.cpy` |
| --- | --- | --- | --- |
| `pyget install <pkg>` | Python (PyPI) | `~/.pypp/packages` | `import requests` |
| `cget install <lib>` | C libraries | `~/.pypp/packages/_c` | `import z` |
| `cpget install <lib>` | C++ libraries | `~/.pypp/packages/_cpp` | `import stdc++` |

All three support the full set of subcommands: `install` / `uninstall` / `list` / `show` /
`freeze` / `search` / `download` / `doctor`, plus the C/C++-only
`cbind` (translate a header) and `clibs` (list libraries available in the toolchain).

```bash
# Python
pyget install requests
pyget install "flask>=3.0"
pyget install -i https://pypi.tuna.tsinghua.edu.cn/simple numpy
pyget list

# C
cget install zlib                  # by library name; finds zlib.h
cget install ./inc/curl/curl.h     # by header path
cget search z                      # list local C libraries containing "z"
cget list

# C++
cpget install stdc++               # C++ standard library
cpget install fmt
cpget list
```

> The legacy `pget` name still works and auto-detects the category;
> `--py` / `--c` / `--cpp` force it.

**One `import` syntax for all three ecosystems**

```python
import requests      # Python — goes through real CPython
import z             # C      — expanded at compile time into #link + extern
import stdc++        # C++    — same
```

For C/C++ the `import` is a **compile-time expansion** (zero runtime cost): py++ finds
`~/.pypp/packages/_c/z.cpyi` and inlines its `#link z` plus `extern` declarations into your source.

**What the PyPI path does**

- Talks to the PyPI JSON API directly, scoring wheels by platform and Python version
  (prefers binary wheels like `cp313-win_amd64` over `py3-none-any`)
- Parses `METADATA` inside the wheel and **installs dependencies recursively**
- On upgrade, removes the old version by `RECORD` first, leaving no mixed files
- Falls back to **pip** when only a source distribution is available

**What the C/C++ path does**

Translates headers into extern declarations:

```bash
$ cget install zlib
cget 识别为 C 库：zlib  (匹配到 zlib.h)
✓ 已生成 19 条 extern 声明 → ~/.pypp/packages/_c/z.cpyi
cget   其中 77 个函数无法表达（变参/函数指针等），已注明原因
✓ 完成：已安装 C 库 z
```

Working example:

```python
import z
print(zlibVersion())          # 1.3.2
print(crc32(0, "hello", 5))   # 907060870
```

> When no header is found (binary-only distributions), a declaration file containing only
> `#link <name>` is still generated, and py++ tells you it could not translate signatures.

**Where packages go**: `~/.pypp/packages` (C in `_c/`, C++ in `_cpp/`). **Both engines add this
directory to `sys.path`**, so they see the same packages — no "works native, missing on VM".

> Environment variables: `PYPP_PKG_DIR` (install dir), `PYPP_INDEX` (index URL), `PYPP_PYTHON` (which Python to embed).

### Honest limits

- **`from x import *`** cannot resolve names at compile time, so native mode **falls back to the VM**
  automatically (same behaviour, a bit slower) instead of failing.
- Native mode needs a CPython with **headers and import library** on the machine (the official
  installer provides these). Without it, py++ also falls back to the VM, where `import` still works.
- Wheel ABI tags must match: `pyget` picks packages for the embedded Python (e.g. `cp313`).
- **C/C++ libraries work in native mode only** (the VM has no C runtime), and the library must
  already be installed locally — `cget`/`cpget` translate declarations and link, they do not download C libraries.
- py++'s own syntax is still growing; but **Python objects obtained through it are fully capable**.

---

## Calling C libraries

py++ compiles to C, so **calling C is native to it** — no FFI glue:

```python
#link z                                     # 1. which library to link against

extern str zlibVersion()                    # 2. declare the signature
extern unsigned long crc32(unsigned long crc, str buf, unsigned int len)

print(zlibVersion())                        # 1.3.2
print(crc32(0, "hello", 5))                 # 907060870
```

Run `pypp examples/c_lib.cpy`. The result is bit-identical to Python's `zlib.crc32`.

> **⚠️ C libraries only work in native mode.**
> The built-in VM has no C runtime; `extern` calls return `0` and py++ explicitly says
> "native mode required" — it does not silently pretend. So on a machine without gcc you will
> not see real values here. Install the full version (with portable gcc) to get them.

### Skip the hand-written declarations

```bash
cget install zlib        # by library name; cget locates the header itself
```

Result:

```python
#link z

extern str zlibVersion()
extern unsigned long compressBound(unsigned long sourceLen)
extern unsigned long crc32(unsigned long crc, ptr, unsigned int len)
extern unsigned long adler32(unsigned long adler, ptr, unsigned int len)
...
# 跳过 deflateInit_：参数类型无法转换（z_streamp strm）
```

Or translate without installing, choosing the output file (adjust the path to your gcc install):

```bash
cget cbind /usr/include/zlib.h --link z --out zlib.cpyi
```

To see which C libraries the toolchain offers:

```bash
cget clibs z        # list libraries whose name contains "z", noting header availability
```

| Form | Effect |
| --- | --- |
| `cget install zlib` | by **library name**, found in the toolchain |
| `cget install zlib.h` | by **header path** |
| `cpget install stdc++` | **C++ standard library**; emits `#link` only |
| `pget install xxx` | generic entry point, auto-detects |

### What the translation does

| Step | Handling |
| --- | --- |
| De-noise | strip comments, `#define`, `__attribute__`, `WINAPI` and similar decorations |
| Follow includes | `uLong` is defined in `zconf.h` → read it recursively to resolve typedefs |
| Type mapping | `char*` → `str`, other pointers → `ptr`, floats → `float`, integers keep their width |
| Skip what cannot be expressed | structs by value, unknown types → skipped with the reason noted |

### Why integers keep their original width

C's `int` is 32 bits. Declaring it as 64 bits means the **upper 32 bits of a return value are
undefined** (the x64 calling convention only guarantees EAX), so you read garbage. `extern`
therefore preserves the declared type exactly: `unsigned long` stays `unsigned long`.

Likewise, unsigned return values are zero-extended — otherwise `crc32('py++')` would print negative.

### Honest limits

- **Translation is a starting point, not the finish line.** C's type system is more complex
  (structs, function pointers, varargs, bitfields); functions that cannot be expressed are skipped
  and noted. Always verify signatures against the real header.
- **C libraries work in native mode only.**
- Supported types: integers (signed/unsigned, all widths), floats, `char*`, `void*`.
  Structs can only be passed as pointers (`ptr`).

---

## Installer

### Windows installer exe (recommended)

Download `pypp-1.1.0-setup.exe` and run it — **no Python needed on the machine**.

It bundles a trimmed portable Python runtime (11.8 MB compressed / 31.6 MB extracted),
unpacked to `~/.pypp/runtime/`; `pypp.bat` calls it directly.

Four-step wizard, modelled on the official Python installer:

```
Welcome  →  Options  →  Progress  →  Done
```

The **Options** page uses checkboxes; the explanation for an option expands inline only when it is ticked:

| Option | Description |
| --- | --- |
| Download a portable C compiler | Ticked = native mode (near C++). ~557 MB from winlibs; needs internet |
| Use the bundled C compiler | Only shown if the release ships gcc; no download, but a much larger initial package |
| Example programs (.cpy) | Installs hello / tolerance demo / benchmark samples |
| Add `pypp` to PATH | Lets you run `pypp yourprogram.cpy` from any terminal |

> Key point: **gcc or no gcc is the speed dividing line.** Core-only installs are under 1 MB;
> with the toolchain it is 557 MB — which is exactly why it is optional.
> With neither compiler option ticked you get the built-in VM (slower but fully usable),
> and py++ still auto-detects any gcc already on your system.

> The download source can be overridden with `PYPP_GCC_URL` (internal mirrors / offline bundles).

### Graphical installer (from source)

```bash
python install_gui.py       # needs a Python with tkinter
```

On Windows you can also **double-click** `install_gui.bat`, which picks a usable interpreter.

Headless self-test (for CI; does not touch your PATH):

```bash
python install_gui.py --autotest                # skips the large gcc copy
python install_gui.py --autotest --full         # includes gcc
python install_gui.py --autotest --log out.txt  # no console under the exe; write results to a file
python install_gui.py --uitest                  # window layout measurement only
```

### Building the exe yourself

```bash
python build_icons.py       # generate icons → assets/{cpy,pypp}.ico (optional)
python build_runtime.py     # extract a portable Python → dist/runtime/pypp-runtime.zip
python build_exe.py         # package → dist/installer/pypp-1.1.0-setup.exe
python repack_zip.py        # source distribution → dist/pypp-1.1.0-src.zip
```

`build_runtime.py` picks `python.exe` / `python3XX.dll` / `DLLs/` / `Lib/` from the system
Python (dropping `test`, `idlelib`, `__pycache__` …) so that after extraction `sys.prefix`
points at itself and the runtime is self-contained.

### Command-line installer (web / scripted use)

```bash
python install.py             # full install
python install.py --check     # detect environment only
python install.py --json      # JSON output (for web front-ends)
python install.py --uninstall # uninstall
```

`--check` sample output:

```
py++ 1.1.0 环境检测

  ✓ Python 3.13.14
  ·  系统 Windows 11 / AMD64
  ✓ C 编译器 gcc.exe  gcc (MinGW-W64) 14.2.0

  执行引擎: 原生高速模式
```

### Uninstall

The graphical installer writes `uninstall.bat` into the install directory; double-click it
to remove the `~/.pypp` command directory and the corresponding PATH entry.

---

## Web service

py++ ships a small web service for online editors and download pages:

```bash
python web/server.py          # http://localhost:8765
```

| Endpoint | Method | Purpose |
| --------------- | ---- | --------- |
| `/api/info`     | GET  | language metadata |
| `/api/check`    | GET  | environment detection |
| `/api/download` | GET  | download the compiler zip |
| `/api/run`      | POST | run code online |

The full contract is in [`docs/WEB_API.md`](docs/WEB_API.md).

---

## Project layout

```
py++
├── src/
│   ├── pypp.py          # driver: pipeline + toolchain/Python detection
│   ├── lexer.py         # lexer (indentation sensitive)
│   ├── parser.py        # recursive-descent parser
│   ├── codegen.py       # C generation (speed-critical) + CPython bridge
│   ├── interpreter.py   # built-in VM (fallback path)
│   └── repair.py        # error-tolerance layer
├── web/server.py        # web service
├── examples/            # sample programs (.cpy)
├── docs/
│   ├── 快速精通.md       # complete guide from zero to fluent (Chinese)
│   ├── WEB_API.md      # web API contract
│   └── 源码包安装说明.md  # installing from source + FAQ (Chinese)
├── install_gui.py       # graphical installer (tkinter)
├── install_gui.bat      # one-click launcher for the graphical installer
├── install.py           # CLI installer (used by the website)
├── pget.py              # package manager core (shared by pyget/cget/cpget)
├── pget.bat             # generic entry point
├── pyget.bat            # Python packages (PyPI)
├── cget.bat             # C libraries
├── cpget.bat            # C++ libraries
├── build_icons.py       # generate file icons (.cpy / exe)
├── assets/
│   ├── cpy.ico          # .cpy file icon (registry DefaultIcon)
│   ├── pypp.ico         # exe icon (PyInstaller --icon)
│   └── _snakes.png      # snake artwork extracted from the reference icon
└── tests/test_all.py    # test suite
```

The icon design stitches Python's `.py` icon to C++'s `.cpp` icon:
a document page + two snakes (extracted from the real Python icon) + a magenta `++`.
`.cpy` uses the document style, the exe uses the application style.
Regenerate with `python build_icons.py`.

---

## Language status

**Supported**

- Variables; integers, floats, strings, booleans
- Arithmetic / comparison / logical operators, `in` / `not in` membership
- `if / elif / else`, `while`, `for ... in`
- `def` functions (with default arguments)
- **Lists** (int / float / string), indexing, iteration
- **Dicts**: `{k: v}` literals, `d[k]` read/write, `len`, `in`, iteration, `.get()`
- **Classes**: `class` + `__init__` + methods, compiled to C structs (zero cost)
- `print` / `input` / `len` / `str` / `int` / `float` / `abs` / `min` / `max` / `sum` / `range`
- **`import a` / `import a.b as c` / `from a import b`** — real CPython,
  including third-party packages with binary extensions

**Planned**

- Class inheritance (currently falls back to the VM automatically)
- Sets, tuples, slicing, keyword arguments
- Generators / comprehensions

### Dicts

```python
config = {"host": "localhost", "port": 8080, "debug": 1}

print(config["host"])        # localhost
config["timeout"] = 30       # write
print(len(config))           # 4

if "debug" in config:
    print("debug mode")

for key in config:           # insertion order, matching Python
    print(key)

print(config.get("proxy", "direct"))   # default when missing
```

Dict values are **dynamically typed** (strings / ints / floats can mix), so values read back
take the dynamic path when used in arithmetic — the honest cost of a genuinely dynamic container.
The native implementation is a **hash table**: O(1) lookup.

### Classes

```python
class Point:
    def __init__(self, x, y):
        self.x = x
        self.y = y

    def total(self):
        return self.x + self.y

p = Point(3, 4)
print(p.x, p.y)     # 3 4
print(p.total())    # 7
```

**In native mode a class becomes a C struct plus a set of functions** — no table lookups:

```c
typedef struct { long long x; long long y; } Point;
static long long Point_total(Point* self) { return self->x + self->y; }
static Point Point_new(long long x, long long y) { ... }
```

Field access is plain struct member access; method calls are ordinary function calls — **zero runtime cost**.

Field and parameter types are **inferred from call sites**: `Person("py++", 3)` tells the compiler
that `self.name` is a string and `self.age` an integer, with no type annotations required.

**Limit**: class inheritance is not yet supported (falls back to the VM, same behaviour).

---

## Performance

Native mode emits C with **fully known types**: integers map straight to `long long`, and loop
bodies carry no runtime type checks, which lets gcc's optimiser do its full work.

### Measured (reproducible)

Benchmark: `python bench/run_bench.py` (requires gcc)

| Metric | C++ | py++ (native) | Ratio |
| ---- | ------- | --------- | ---------- |
| Total time | 0.030 s | 0.033 s | **1.123x** |

Workload: 50M integer additions + 20M float additions + 2000×2000 nested modulo loops.
gcc 14.2.0, `-O2`, median of 5 runs, stdin input to prevent constant folding.

**Conclusion: native py++ is about 12% slower than equivalent C++.**

> Note: the built-in VM engine (the fallback without gcc) is roughly 3000–7000x slower.
> The "near C++" claim applies to native mode only.

---

## License

This project uses the **[PolyForm Noncommercial 1.0.0](https://polyformproject.org/licenses/noncommercial/1.0.0)**.
See [LICENSE](LICENSE) for the full terms.

| Use | Free | Notes |
|---|---|---|
| Personal study, research, experimentation | ✅ | No permission needed |
| Hobby projects, private use | ✅ | No permission needed |
| Teaching, schools and other educational institutions | ✅ | No permission needed |
| Charities, public research, government bodies | ✅ | No permission needed |
| **Production use inside a company** | ❌ | **Written permission required** |
| **Bundling into a commercial product/service** | ❌ | **Written permission required** |
| **Offering a paid service built on it** | ❌ | **Written permission required** |

Noncommercial use may freely modify and redistribute, but **must retain this licence and the copyright notice**.

> **Commercial licensing**: a3845908079@outlook.com (backup: 3845908079@qq.com),
> or open an issue. Please state your company/organisation, intended use, and expected scale.
>
> ⚠️ Because commercial use is restricted, this project is **not open source under the OSI
> definition** — it is **source-available**. Stating it this way is accurate and avoids
> misleading users.
