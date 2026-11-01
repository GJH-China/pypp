# py++

> 像 Python 一样简洁，像 C++ 一样快

**简体中文** | [English](README.en.md)

py++ 是一门新的编程语言，目标是同时拿到两个世界的优点：

- **Python 的语法**：缩进块、无分号、`print("hello")` 直觉写法
- **C++ 的速度**：编译为原生机器码，实测性能在 C++ 的 ±0.3 倍以内
- **独有的容错**：代码有小错误也能跑，自动修复 + 友好提示
- **完整的 Python 生态**：`import` 真实 CPython，直接用 pip 上的第三方库
- **也能直接调 C 库**：`extern` 声明 + `#link` 链接，头文件可自动转译

源文件扩展名：**`.cpy`**

当前版本：**1.1.0**

<details>
<summary>1.1.0 更新内容</summary>

- 新增 **类**（`class`）——编译成 C 结构体 + 函数，无运行时开销
- 新增 **字典**（`dict`）——哈希表 + 插入顺序表，遍历顺序与 Python 一致
- 新增 **C 库互操作**：`extern` 声明、`#link` 链接、`cbind` 头文件转译
- **三个包管理命令**：`pyget`（Python）/ `cget`（C）/ `cpget`（C++），各管一摊
- **`import` 通吃三种库**：`import requests` / `import z` / `import stdc++`
- 修复字典/列表作为引用类型的行为（放进别的容器后修改会同步）
- 修复布尔值打印（`True`/`False` 而非 `1`/`0`）
- 修复把字典/列表传给 Python 函数（如 `json.dumps(d)`）编译失败的问题
- 提供 **exe 安装程序**（自带便携 Python 运行时，用户机器无需预装 Python）

</details>

---

## 快速开始

想系统地学一遍？打开随安装包分发的 **《py++ 快速精通》**（`docs/快速精通.md`）——
从零到精通，含已知边界清单。

普通用户只需要一个 exe：

```bash
# 1. 双击下载到的 pypp-1.1.0-setup.exe，按向导点下一步
#    安装程序自带 Python 运行时，你的机器上不需要预装 Python

# 2. 新开一个终端，跑第一个程序
pypp examples/hello.cpy

# 3. 看看容错能力
pypp examples/tolerant.cpy

# 4. 装库 —— 三种库各有一条专属命令
pyget install requests     # Python 库（PyPI）
cget  install zlib         # C 库
cpget install stdc++       # C++ 库

pypp examples/use_requests.cpy

# 5. 算文件哈希 —— import hashlib 的完整流水线（分块读、两引擎一致）
pypp examples/sha256.cpy

# 6. 不想开终端？双击就行
#    安装时勾选「双击运行 .cpy 文件」后，在文件资源管理器里
#    双击任意 .cpy 即直接运行，不必打包成 exe
```

### 从源码运行（开发者）

克隆后无需安装，直接跑：

```bash
python src/pypp.py examples/hello.cpy
```

前置条件只有两个：

- **Python 3.10+**（3.13 已验证）
- **C 编译器 gcc** —— 它决定你走哪条引擎

| 有没有 gcc | 引擎 | 速度 |
|---|---|---|
| 有 | 原生高速模式 | 接近 C++（实测慢 1.02~1.12 倍） |
| 没有 | 内置 VM | 慢很多，仅保证能跑 |

> ⚠️ **本仓库不包含 gcc 工具链**（约 1.4 GB，不适合放进版本库）。
> Windows 上可用 [winlibs](https://winlibs.com/) 的 MinGW-w64，解压即用；
> 或用 MSYS2 `pacman -S mingw-w64-x86_64-gcc`。

跑起来先看第一行，确认用的哪个引擎：

```
[py++] 引擎: 原生高速模式 (gcc.exe)    ← 期望
[py++] 引擎: 内置 VM 模式              ← 没找到 gcc
```

想装成全局命令、并让 `.cpy` 双击直接运行：

```bash
python install_gui.py       # 图形向导（或双击 install_gui.bat）
```

## 双击运行 .cpy（文件关联 + 运行器 UI）

安装时默认勾选「双击运行 .cpy 文件」。装好后：

- **双击**任意 `.cpy` 文件 → 弹出运行器窗口直接运行，不必打包 exe
- 运行器界面（介于 CLI 与 TUI 之间，cmd/PowerShell/Terminal 全兼容）：
  头部信息框（文件名/目录/引擎）→ 程序输出**实时透传** →
  尾部状态框（引擎/耗时/退出码）→ `按任意键关闭窗口`
- 程序里 `input()` 的提示语**实时可见**、可以直接输入——
  不再是跑完才打印、只能盲打（曾经的坑）
- 运行器自动切到脚本所在目录——脚本里 `open("隔壁文件.txt")`
  这类相对路径找得到旁边的东西
- **拖拽** `.cpy` 到 `pypprun.bat` 图标上同样可以运行
- 卸载时自动清理，不留孤儿注册表项

实现与边界：

| 项 | 说明 |
| --- | --- |
| 写入位置 | 只写 `HKCU\Software\Classes`（当前用户），**不需要管理员权限** |
| 运行器 | `<home>/bin/pypprun.bat`（纯 ASCII，只负责 `chcp 65001` + 拉起 `src/runner.py`）；界面/流式/暂停逻辑全在 Python 侧 |
| bat 为什么纯 ASCII | cmd 按 GBK 解析 chcp 之前的行，UTF-8 中文注释会字节错位、把注释和命令粘成一整行 |
| 归属守卫 | 卸载/改注册只删**自己的**键：`.cpy` 默认值必须是 `pypp.cpy`，命令必须指向本机安装目录 |
| 边界 | 仅 Windows；不抢已有 association 的所有权，重复安装直接覆盖旧键 |

命令行用法照旧 `pypp 你的程序.cpy`；在交互式终端里运行时，
程序输出同样实时直连控制台（管道/CI 场景自动回退为捕获，行为不变）。

```bash
python install.py               # 安装（默认注册关联）
python install.py --no-assoc    # 安装但不注册关联
python install.py --uninstall   # 卸载（连同关联一起清理）
```

---

## 一个例子

```python
# examples/hello.cpy
name = "py++"
print("hello，" + name)

total = 0
for i in range(1, 101):
    total += i
print("1 加到 100 =", total)
```

运行：

```
hello，py++
1 加到 100 = 5050
```

---

## 容错能力（py++ 的招牌）

就算代码有点小错误，py++ 也会自动修复并运行，只给你一个友好提示：

```python
pritn("开始")        # 拼写错误
x = 10
if x > 5             # 少了冒号
    print("大于 5")
print(y)             # y 从未定义
```

输出：

```
[py++ 温馨提示] 检测到你的代码有 3 处小错误，并无大碍，py++ 已兼容运行。
  [已自动修复] 第 1 行 'pritn' 疑似拼写错误，已自动更正为 'print'
  [已自动修复] 第 3 行 此处缺少 :，已自动补全，程序照常运行
  [已自动修复]  未定义的变量 'y'，已按 0 处理
开始
大于 5
0
```

**程序完整跑完了** —— 这就是 py++ 和其他语言最大的不同。

### 容错的设计原则：编译期智能，运行期零开销

重要：这些修复**全部发生在编译阶段**，不会拖慢运行。生成的机器码是干净的，没有任何"每次循环都检查"的负担。这一点是 py++ 能不牺牲速度的关键。

### 容错是怎么判断的

不是靠穷举错误清单，而是四级递进：

| 层级 | 机制 | 例子 |
| --- | --- | --- |
| 1 | 全角标点归一化 | `print（x）` → `print(x)`（中文输入法高频错误） |
| 2 | 特例表（算法推不出来的） | `fro` → `for`（算法会误选 `from`） |
| 3 | 编辑距离相似度 | `prit` / `prnt` / `whiel` → 自动纠正 |
| 4 | **跨行结构评估** | 名字被换行拆散时，合并相邻行再判断 |

第 4 级值得一提。看这段代码：

```python
name = "py++"
prin
f
("hello，" + name)
```

本意是 `print("hello，" + name)`，但被换行拆成了三行。

**如果逐行纠错**，会把 `prin` 单独改成 `print`，于是变成 `print` / `f` / `(...)` 三条语句——语义彻底跑偏，却还报"已修复"。

py++ 的做法是**把相邻几行拼起来一起评估**：`prin`+`f` → `prinf`，与 `print` 距离仅 1，于是正确识别并合并。实测输出 `hello，py++`，与代码本意一致。

### 容错的边界（诚实说明）

py++ 只修**能确定**的错误，对无法确定的**明确告知**而不是乱猜。例如：

```python
prinf（l）
f
("hello，" + name)
```

这里 `prinf` 会被修成 `print`、全角括号会被归一化，但 `f` 和后面的括号是**悬空片段**——存在多种可能的"本意"，无法可靠判定。此时 py++ 会保留原样并提示：

```
[已自动修复] 第 3 行的 'f' 看起来是某个函数名被敲断后留下的碎片…
        py++ 无法确定你的本意，已原样保留。建议检查是否想写 print(...)
```

**为什么宁可不修**：猜错比不猜更糟——它会让用户以为代码是对的。

其他边界：
- 修复只在内置名/关键字范围内做相似度匹配，不是理解语义
- 用户自定义的名字不参与猜测（保证 `foo`、`num` 永不被误改）
- 跨语言写法（`cout << x`）不在范围内——那是语言差异，不是拼写错误

---

## 两套执行引擎

| 引擎        | 触发条件          | 速度     | 说明                       |
| --------- | ------------- | ------ | ------------------------ |
| **原生模式**  | 系统有 gcc/clang | 接近 C++ | 生成 C → `gcc -O2` → 原生机器码 |
| **VM 模式** | 没有 C 编译器      | 较慢但可用  | 内置树遍历解释器，保证永远能跑          |

py++ 启动时自动探测，无需手动切换。也可强制指定：

```bash
pypp code.cpy --vm        # 强制用内置 VM
```

### 编译速度优化

py++ 会在编译期做几件事，让"改一下跑一下"的体验尽量轻快：

**1. 编译产物缓存（默认开启）**

源码没变时，直接复用上次编译的可执行文件。实测：

| 场景 | 耗时 |
| --- | --- |
| 首次运行（需编译） | ~440ms |
| 再次运行（命中缓存） | **~10-50ms** |

缓存键 = 生成的 C 代码哈希 + 编译器指纹 + py++ 版本。
**注意**：键基于"修复后的 C 代码"，所以改名、改容错结果都会自动重新编译 —— 不会跑到旧结果。

缓存放在 `~/.pypp/cache`，控制方式：

```bash
PYPP_NO_CACHE=1 pypp code.cpy     # 临时关闭缓存
```

**2. 工具链探测只做轻量检查**

早期版本每次运行都会**试编译一个 hello 程序**来验证工具链（~289ms）。
现在只跑 `gcc --version`（~13ms）——因为真正的编译失败本来就有 VM 兜底，
不需要预先"体检"。

---

## 使用 Python 的库（import + pyget）

这是 py++ 区别于"自造玩具语言"的地方：**它能真正用上 Python 的整个生态**。

```python
# examples/use_requests.cpy
import requests
from urllib3 import __version__

r = requests.get("https://api.github.com")
print("状态码:", r.status_code)
print("urllib3:", __version__)
```

### 它是怎么做到的

关键在于：**py++ 不假装自己实现了 Python 库，而是直接跑真正的 CPython。**

| 引擎 | import 的实现 | 结果 |
| --- | --- | --- |
| **原生模式** | 把 CPython 解释器**嵌进**生成的可执行文件，用 C-API 调用 | 真实、完整、支持二进制扩展 |
| **VM 模式** | VM 本身就跑在 Python 里，`import` 是它的原生能力 | 真实、完整 |

也就是说：**任何能装进 Python 的库，py++ 都能用**——包括带 C 扩展的（NumPy、Pillow、lxml…），因为最终执行的就是它们本来的二进制。

### 零开销：不写 import 就完全不受影响

这点很重要。**只有你真正写了 `import`，生成的 C 代码里才会出现 Python 桥接层。**

| 程序 | 生成的 C 里有什么 |
| --- | --- |
| `print("hi")` | 纯 C，无 `Python.h`、无 `Py_Initialize` |
| `import math` + 调用 | 注入 CPython 桥接层 |

所以"接近 C++ 的速度"这个承诺不会被稀释——**Python 互操作只在你用到它时才付费**。

py++ 自己的数值运算仍然是纯 C 指令；只有"来自 Python 库的值"才用 `PyObject*` 承载（内部类型叫 `pyobj`），属性访问、方法调用、下标都翻译成对应的 C-API 调用。

### 包管理器：pyget / cget / cpget

py++ 的包管理是**三个平级命令**，各管一摊，谁也不用猜：

| 命令 | 管什么 | 装到哪 | 在 `.cpy` 里怎么用 |
| --- | --- | --- | --- |
| `pyget install <包>` | Python 库（PyPI） | `~/.pypp/packages` | `import requests` |
| `cget install <库>` | C 库 | `~/.pypp/packages/_c` | `import z` |
| `cpget install <库>` | C++ 库 | `~/.pypp/packages/_cpp` | `import stdc++` |

三个命令都支持完整子命令：`install` / `uninstall` / `list` / `show` /
`freeze` / `search` / `download` / `doctor`，外加 C/C++ 专属的
`cbind`（转译头文件）和 `clibs`（列本机可用的库）。

```bash
# Python 库
pyget install requests
pyget install "flask>=3.0"
pyget install -i https://pypi.tuna.tsinghua.edu.cn/simple numpy
pyget list

# C 库
cget install zlib                  # 按库名，自动找 zlib.h
cget install ./inc/curl/curl.h     # 按头文件路径
cget search z                      # 列出本机名字含 z 的 C 库
cget list

# C++ 库
cpget install stdc++               # C++ 标准库
cpget install fmt
cpget list
```

> 老名字 `pget` 仍然可用，它会自动识别你要装的是哪一类；
> 也可以用 `--py` / `--c` / `--cpp` 明确指定。

**`import` 一条语法通吃三种库**

```python
import requests      # Python 库 —— 走真实 CPython
import z             # C 库    —— 编译期展开成 #link + extern
import stdc++        # C++ 库  —— 同上
```

对 C/C++ 库，`import` 做的是**编译期展开**（零运行时开销）：
py++ 发现 `~/.pypp/packages/_c/z.cpyi` 存在，就把里面的 `#link z` 和
`extern` 声明直接铺进你的源码。所以你不会为此付出任何性能代价。

**PyPI 路径做了什么**

- 直连 PyPI JSON API，按平台与 Python 版本给 wheel 打分选最优
  （优先 `cp313-win_amd64` 这类二进制包，其次 `py3-none-any`）
- 解析 wheel 里的 `METADATA`，**递归安装依赖**
- 升级时先按 `RECORD` 清掉旧版本，不留混杂文件
- 没有可用 wheel（只有源码包）时**自动回退到 pip** 构建

**C / C++ 路径做了什么**

转译头文件生成 extern 声明：

```bash
$ cget install zlib
cget 识别为 C 库：zlib  (匹配到 zlib.h)
✓ 已生成 19 条 extern 声明 → ~/.pypp/packages/_c/z.cpyi
cget   其中 77 个函数无法表达（变参/函数指针等），已注明原因
✓ 完成：已安装 C 库 z
  声明文件目录：~/.pypp/packages/_c
  在 .cpy 里用：import z
```

实测可用：

```python
import z
print(zlibVersion())          # 1.3.2
print(crc32(0, "hello", 5))   # 907060870
```

> 找不到头文件时（库是二进制发布的），仍会生成一个只含 `#link <库名>`
> 的声明文件，并明确告诉你无法自动转译函数签名。

**装到哪里**：`~/.pypp/packages`（C 库在 `_c/`，C++ 库在 `_cpp/`）。**原生模式与 VM 模式都会把这个目录加进 `sys.path`**，所以两边看到的是同一批包——不会出现"原生能跑、VM 找不到模块"。

> 环境变量：`PYPP_PKG_DIR` 换安装目录，`PYPP_INDEX` 换索引源，`PYPP_PYTHON` 指定嵌入哪个 Python。

### 边界（诚实说明）

- **`from x import *`** 无法在编译期确定名字，原生模式会**自动降级到 VM**（功能一致，速度略低），不会报错。
- 原生模式需要本机有一个**带头文件和导入库**的 CPython（官方安装器装的都有）。没有时同样降级到 VM，import 照常可用。
- wheel 的 ABI 标签必须匹配：`pyget` 会按嵌入目标 Python 的版本挑包（如 `cp313`）。若你本机 Python 是 3.12，就会挑 `cp312` 的包。
- **C/C++ 库只在原生模式可用**（VM 没有 C 运行时），且**要求库本身已经装在本机**——`cget`/`cpget` 做的是「转译声明 + 链接」，不是下载 C 库。
- py++ 自身的语法还在成长中（类、字典等尚未支持），但**通过这些语法拿到的 Python 对象，其能力是完整的**。

---

## 调用 C 库

py++ 编译到 C，所以**调用 C 库是它的原生能力**——不像别的语言要写 FFI 胶水。
只需要两件事：

```python
#link z                                     # 1. 告诉链接器去哪个库找

extern str zlibVersion()                    # 2. 声明函数长什么样
extern unsigned long crc32(unsigned long crc, str buf, unsigned int len)

print(zlibVersion())                        # 1.3.2
print(crc32(0, "hello", 5))                 # 907060870
```

运行 `pypp examples/c_lib.cpy` 即可。实测值与 Python 的 `zlib.crc32`
逐位一致。

> **⚠️ C 库只在原生模式可用。**
> 内置 VM 里没有 C 运行时，`extern` 调用会一律返回 `0`（并且会明确提示
> 「需要原生模式才能调用」，不会静默糊弄）。所以精简版 + 系统无 gcc 的
> 环境下，这个示例看不到真实结果。装完整版（含便携 gcc）即可。

### 不用手写声明：一条 cget install 搞定

```bash
cget install zlib        # 按库名，cget 自己去找头文件
```

输出：

```python
#link z

extern str zlibVersion()
extern unsigned long compressBound(unsigned long sourceLen)
extern unsigned long crc32(unsigned long crc, ptr, unsigned int len)
extern unsigned long adler32(unsigned long adler, ptr, unsigned int len)
...
# 跳过 deflateInit_：参数类型无法转换（z_streamp strm）
```

也可以只转译、不安装，并自己指定输出文件（路径按你的 gcc 安装位置改）：

```bash
cget cbind /usr/include/zlib.h --link z --out zlib.cpyi
```

想先看看工具链里有哪些 C 库可用：

```bash
cget clibs z        # 列出名字里含 z 的库，并标注有没有对应头文件
```

| 写法 | 效果 |
| --- | --- |
| `cget install zlib` | 按**库名**，在工具链里找头文件 |
| `cget install zlib.h` | 按**头文件路径** |
| `cpget install stdc++` | **C++ 标准库**，只生成 `#link` |
| `pget install xxx` | 通用入口，自动识别 |

### 它做了什么

| 步骤 | 处理 |
| --- | --- |
| 去噪 | 剥掉注释、`#define`、`__attribute__`、`WINAPI` 等装饰 |
| 跟随 include | `uLong` 定义在 `zconf.h` 里 → 递归读进来解析 typedef |
| 类型映射 | `char*` → `str`，其他指针 → `ptr`，浮点 → `float`，整数保留原宽度 |
| 跳过不可表示的 | 按值传结构体、无法识别的类型 → 跳过并注明原因 |

### 为什么整数要"保留原宽度"

C 的 `int` 是 32 位。如果把它声明成 64 位，函数返回时**高 32 位是未定义的**
（x64 调用约定只保证 EAX 有效），会读到垃圾值。所以 `extern` 里写的类型
会原样保留：`unsigned long` 就是 `unsigned long`。

同理，无符号返回值会做零扩展——否则 `crc32('py++')` 会显示成负数。

### 边界（诚实说明）

- **转译是起点，不是终点**。C 的类型系统比 py++ 复杂（结构体、函数指针、
  变参、位域），无法表达的函数会被跳过并在输出里注明。签名请按实际头文件核对。
- **C 库只在原生模式下可用**。VM 没有 C 运行时，遇到 extern 会明确提示
  "需要原生模式"，而不是静默返回 0。
- 目前支持的类型：整数（含 unsigned / 各宽度）、浮点、`char*`、`void*`。
  结构体只能以指针（`ptr`）形式传递。

---

## 安装器

### exe 安装程序（推荐给普通用户）

下载 `pypp-1.1.0-setup.exe` 双击运行即可，**机器上不需要预先装 Python**。

安装程序内嵌了一份精简的便携 Python 运行时（压缩 11.8MB / 解压 31.6MB），
安装时释放到 `~/.pypp/runtime/`，`pypp.bat` 会直接调用它。

四步向导，模仿 Python 官方安装器：

```
欢迎  →  安装选项  →  安装进度  →  完成
```

「安装选项」页是可勾选的多选项，**勾中某一项时才会就地展开该项的说明文字**：

| 选项 | 说明 |
| --- | --- |
| 在线下载便携 C 编译器 | 勾上 = 原生高速模式（接近 C++）。从 winlibs 下载约 557MB，需要联网 |
| 使用安装包内的 C 编译器 | 仅当发行包内自带 gcc 时可见；不占下载流量，但初始包会大很多 |
| 示例程序 (.cpy) | 装上 hello / 容错演示 / 性能展示等示例 |
| 把 pypp 命令加入 PATH | 勾上后可在任意终端直接 `pypp 你的程序.cpy` |

> 关键提示：**装不装 gcc 是速度分水岭**。只有核心代码时整个安装不到 1MB，
> 加上 gcc 工具链则是 557MB —— 这正是我们把它做成可选项的原因。
> 两个编译器选项都不勾 = 使用内置 VM 模式（较慢但完全能用），
> 而且 py++ 仍会自动识别你系统里已有的 gcc。

> 下载源可用环境变量 `PYPP_GCC_URL` 覆盖（内网镜像 / 离线包场景）。

### 图形安装器（源码方式）

```bash
python install_gui.py       # 需要带 tkinter 的 Python
```

Windows 上也可以**直接双击** `install_gui.bat`，它会自动挑选可用的解释器。

无界面自检（用于 CI，不会改动你的 PATH）：

```bash
python install_gui.py --autotest              # 跳过 gcc 大复制
python install_gui.py --autotest --full       # 连 gcc 一起验证
python install_gui.py --autotest --log out.txt  # exe 下无控制台，把结果写文件
python install_gui.py --uitest                # 只做窗口布局测量
```

### 自己打包 exe

```bash
python build_icons.py       # 先生成文件图标 → assets/{cpy,pypp}.ico（可选，已有可跳过）
python build_runtime.py     # 再抽一份便携 Python 运行时 → dist/runtime/pypp-runtime.zip
python build_exe.py         # 最后打包成 dist/installer/pypp-1.1.0-setup.exe
python repack_zip.py        # 源码 zip 包 → dist/pypp-1.1.0.zip
```

`build_runtime.py` 从系统 Python 里挑出 `python.exe` / `python3XX.dll` / `DLLs/` /
`Lib/`（剔除 test、idlelib、`__pycache__` 等），保证解压后 `sys.prefix`
指向自身、可独立运行。

### 命令行安装器（网站 / 脚本调用）

```bash
python install.py             # 完整安装
python install.py --check     # 只检测环境
python install.py --json      # 输出 JSON（网站调用）
python install.py --uninstall # 卸载
```

`--check` 输出示例：

```
py++ 1.1.0 环境检测

  ✓ Python 3.13.14
  ·  系统 Windows 11 / AMD64
  ✓ C 编译器 gcc.exe  gcc (MinGW-W64) 14.2.0

  执行引擎: 原生高速模式
```

### 卸载

图形安装器会在安装目录写入 `uninstall.bat`，双击即可卸载
（清理 `~/.pypp` 命令目录，并从用户 PATH 中移除对应条目）。

---

## 网站 / 在线服务

py++ 自带一个 Web 服务，方便做在线编辑器、下载页：

```bash
python web/server.py          # http://localhost:8765
```

接口：

| 接口              | 方法   | 用途        |
| --------------- | ---- | --------- |
| `/api/info`     | GET  | 语言元信息     |
| `/api/check`    | GET  | 环境检测      |
| `/api/download` | GET  | 下载编译器 zip |
| `/api/run`      | POST | 在线试运行代码   |

完整契约见 [`docs/WEB_API.md`](docs/WEB_API.md)，前端 AI 按此对接即可。

---

## 项目结构

```
py++
├── src/
│   ├── pypp.py          # 主驱动：整条流水线 + 工具链/Python 探测
│   ├── lexer.py         # 词法分析（缩进敏感）
│   ├── parser.py        # 语法分析（递归下降）
│   ├── codegen.py       # 生成 C 代码（速度关键 + CPython 桥接层）
│   ├── interpreter.py   # 内置 VM（降级路径）
│   └── repair.py        # 容错修复层
├── web/server.py        # Web 服务
├── examples/            # 示例程序 (.cpy)
├── docs/
│   ├── 快速精通.md       # 从零到精通的完整指南（随安装包分发）
│   ├── WEB_API.md      # 网站接口契约
│   └── 源码包安装说明.md  # 从源码安装的步骤与常见问题
├── install_gui.py       # 图形安装器（tkinter）
├── install_gui.bat      # Windows 一键启动图形安装器
├── install.py           # 命令行安装器（网站调用）
├── pget.py              # 包管理器核心（pyget/cget/cpget 共用）
├── pget.bat             # 通用入口
├── pyget.bat            # Python 库（PyPI）
├── cget.bat             # C 库
├── cpget.bat            # C++ 库
├── build_icons.py       # 生成文件图标（.cpy / exe）
├── assets/
│   ├── cpy.ico          # .cpy 文件图标（注册表 DefaultIcon 用）
│   ├── pypp.ico         # exe 图标（PyInstaller --icon 用）
│   └── _snakes.png      # 从参考图提取的双蛇素材
└── tests/test_all.py    # 测试套件
```

图标设计是 Python 的 `.py` 图标与 C++ 的 `.cpp` 图标的缝合体：
文档页 + 蓝黄双蛇（从 Python 图标真实提取）+ 洋红 `++`。
`.cpy` 用文档样式（源码文件），exe 用应用方块样式。
重新生成：`python build_icons.py`。

---

## 语言特性现状

**已支持**

- 变量、整数、浮点、字符串、布尔
- 算术 / 比较 / 逻辑运算，`in` / `not in` 成员判断
- `if / elif / else`、`while`、`for ... in`
- `def` 函数定义（含默认参数）
- **列表**（整数 / 浮点 / 字符串）、下标访问、遍历
- **字典**：`{k: v}` 字面量、`d[k]` 读写、`len`、`in`、遍历、`.get()`
- **类**：`class` + `__init__` + 方法，编译为 C 结构体（零开销）
- `print` / `input` / `len` / `str` / `int` / `float` / `abs` / `min` / `max` / `sum` / `range`
- **`import a` / `import a.b as c` / `from a import b`** —— 真实 CPython，
  第三方库（含二进制扩展）可直接使用

**规划中**

- 类继承（现在会自动降级到 VM）
- 集合、元组、切片、关键字参数
- 生成器 / 推导式

### 字典

```python
config = {"host": "localhost", "port": 8080, "debug": 1}

print(config["host"])        # localhost
config["timeout"] = 30       # 写入
print(len(config))           # 4

if "debug" in config:
    print("调试模式")

for key in config:           # 遍历顺序 = 插入顺序（与 Python 一致）
    print(key)

print(config.get("proxy", "直连"))   # 取不到时用默认值
```

字典的值是**动态类型**（可以混合存放字符串/整数/浮点），
所以从字典里取出来的值参与运算时走动态路径——这是"字典本来就是动态容器"
的诚实代价。原生实现是**哈希表**，查找是 O(1)。

### 类

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

**原生模式把类编译成 C 结构体 + 一组函数**，不是查表：

```c
typedef struct { long long x; long long y; } Point;
static long long Point_total(Point* self) { return self->x + self->y; }
static Point Point_new(long long x, long long y) { ... }
```

所以字段访问就是结构体成员访问，方法调用就是普通函数调用——**没有运行时开销**。

字段类型和参数类型由**调用点推断**：`Person("py++", 3)` 让编译器知道
`self.name` 是字符串、`self.age` 是整数，不需要写类型标注。

**边界**：类继承暂不支持（会自动降级到 VM，功能一致）。

---

## 性能说明

py++ 的原生模式生成的是**类型已知**的 C 代码：整数直接映射为 `long long`，  
循环体不携带任何运行时类型检查。这让 gcc 的优化器能充分发力。

### 实测结果（可复现）

测试脚本：`python bench/run_bench.py`（需要 gcc）

| 项目   | C++     | py++ (原生) | 倍率         |
| ---- | ------- | --------- | ---------- |
| 综合用时 | 0.030 s | 0.033 s   | **1.123x** |

测试内容：5000 万次整数累加 + 2000 万次浮点加法 + 2000×2000 嵌套循环取模。  
编译器 gcc 14.2.0，`-O2`，5 次取中位数，stdin 输入防止常量折叠。

**结论：py++ 原生模式比等效 C++ 慢约 12%，远优于"±0.3 倍"的目标。**

> 注意：内置 VM 引擎（无 gcc 时的兜底）速度约慢 3000～7000 倍。  
> "接近 C++"这一承诺仅适用于原生模式。

---

## 许可

本项目采用 **[PolyForm Noncommercial 1.0.0](https://polyformproject.org/licenses/noncommercial/1.0.0)**，
完整条款见 [LICENSE](LICENSE)。

| 用途 | 是否免费 | 说明 |
|---|---|---|
| 个人学习、研究、实验 | ✅ | 无需申请 |
| 业余项目、私有娱乐 | ✅ | 无需申请 |
| 教学、学校等教育机构 | ✅ | 无需申请 |
| 慈善、公共研究、政府机构 | ✅ | 无需申请 |
| **公司内部生产使用** | ❌ | **需书面授权** |
| **打包进商业产品/服务** | ❌ | **需书面授权** |
| **基于它提供收费服务** | ❌ | **需书面授权** |

非商业目的下可以自由修改和分发，但**必须保留本协议与版权声明**。

> **商业授权**：a3845908079@outlook.com（备用 3845908079@qq.com），
> 也可在仓库提 Issue。请说明公司/组织、使用场景与预计规模。
>
> ⚠️ 说明：由于限制商业使用，本项目**严格来说不是 OSI 定义的开源软件**，
> 而是「源码公开（source-available）」。这样措辞是准确的，
> 也避免了对使用者产生误导。
