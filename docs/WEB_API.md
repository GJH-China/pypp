# py++ 网站接口契约 (Web API Contract)

> 本文档是 py++ 与前端网页之间的**接口约定**。
> 另一个负责做网页的 AI，请严格按本契约实现，py++ 侧已按此约定预留好接口。
> 版本：py++ 1.1.0 ｜ 更新：2026-10

---

## 1. 核心约定

| 项目 | 约定值 |
|---|---|
| 源文件后缀 | **`.cpy`** （py++ 专用扩展名） |
| 语言名称 | py++ |
| 默认端口 | `8765` |
| 数据格式 | JSON，UTF-8，字段名 `snake_case` |
| 下载产物 | `dist/pypp-1.1.0.zip` （自包含，含 install.py） |

---

## 1.5 实测性能数据（网站可以直接引用）

> 以下数字为**实测结果**，测试脚本在 `bench/run_bench.py`，可复现。
> 前端展示"比 C++ 慢不到 13%"这类文案时，请以此为准。

### 测试条件

| 项目 | 值 |
|---|---|
| 编译器 | gcc 14.2.0 (MinGW-W64, MSYS2 构建) |
| 优化级别 | `-O2` |
| 平台 | Windows 11 / AMD64 |
| 计时方法 | 5 次取中位数，输入从 stdin 读取（避免常量折叠） |
| 测试内容 | ① 5000 万次整数累加 ② 2000 万次浮点加法 ③ 2000×2000 嵌套循环取模 |

### 结果

| 指标 | C++ | py++ (原生) | 倍率 |
|---|---|---|---|
| 综合用时 | 0.030 s | 0.033 s | **1.123x** |
| 判定 | — | — | ✓ 在 ±0.3 倍以内 |

**结论：py++ 原生模式比等效 C++ 慢约 12%，远优于"±0.3 倍"（即 ≤1.3 倍）的目标。**

### 前端可用的性能文案

- 标题：`比 C++ 慢不到 13%`
- 副标题：`实测 1.12x —— 目标 ±0.3 倍，轻松达标`
- 徽章：`原生编译 · -O2 优化 · 零运行时开销`

### 关于两种引擎的速度差异（重要）

py++ 有两个引擎，速度差异极大，网站**不要混为一谈**：

| 引擎 | 触发条件 | 相对 C++ 速度 |
|---|---|---|
| 原生模式 | 系统有 gcc | **1.12x**（接近 C++） |
| 内置 VM | 无 gcc 时兜底 | 约慢 3000～7000 倍 |

> 因此网站展示"接近 C++"时，务必确认访问者环境 `engine == "native"`
> （见 `/api/check`），否则应展示为"内置模式，功能完整但速度较慢"。

---

## 2. 后端接口一览

所有接口都以 `/api/` 为前缀，返回 JSON。

### 2.1 `GET /api/info` — 获取语言元信息

**响应：**
```json
{
  "name": "py++",
  "version": "1.1.0",
  "ext": ".cpy",
  "tagline": "像 Python 一样简洁，像 C++ 一样快",
  "engine": "native",
  "c_lib_available": true,
  "features": [
    "缩进语法，无需分号",
    "有 gcc 时编译为原生代码 (-O2)",
    "无 gcc 时自动降级到内置 VM",
    "小错误自动修复，程序照常运行",
    "import 真实 CPython，可直接用 pip 生态的第三方库",
    "pget 包管理器，用法与 pip 一致",
    "能调用 C 库：extern 声明 + #link 链接，pget cbind 自动转译头文件"
  ],
  "sample_code": "name = \"py++\"\nprint(\"hello，\" + name)",
  "install": {
    "gui": {
      "file": "install_gui.py",
      "launcher_windows": "install_gui.bat",
      "steps": ["欢迎", "安装选项", "安装进度", "完成"],
      "need_tkinter": true,
      "note": "Windows 用户可直接双击 install_gui.bat"
    },
    "cli": {
      "cmd": "python install.py",
      "check_cmd": "python install.py --check --json",
      "uninstall_cmd": "python install.py --uninstall"
    },
    "pget": {
      "file": "pget.py",
      "launcher_windows": "pget.bat",
      "install_dir": "~/.pypp/packages",
      "commands": ["install", "uninstall", "list", "show",
                   "freeze", "search", "download", "doctor",
                   "cbind", "clibs"],
      "examples": [
        "pget install requests",
        "pget list",
        "pget install -i https://pypi.tuna.tsinghua.edu.cn/simple numpy",
        "pget cbind zlib.h --link z --out zlib.cpy",
        "pget clibs"
      ],
      "note": "装到 py++ 自己的目录，原生模式与 VM 模式都能 import；cbind 把 C 头文件转译成 py++ 的 extern 声明"
    },
    "editions": [
      { "name": "完整版", "size": "约 557MB",
        "includes": "便携 C 编译器", "engine": "原生高速模式",
        "desc": "速度接近 C++" },
      { "name": "精简版", "size": "不到 1MB",
        "includes": "仅核心", "engine": "需系统自备 gcc",
        "desc": "体积小，适合已有编译器的用户" }
    ]
  }
}
```

> `performance` 字段见 [1.5 节](#15-实测性能数据网站可以直接引用)；
> `install` 字段用于渲染下载引导与安装说明；
> `install.pget` 用于渲染"包管理器"介绍与命令示例。

---

### 2.2 `GET /api/check` — 检测服务器/访客环境

**响应：**
```json
{
  "python": { "version": "3.13.14", "ok": true },
  "platform": { "system": "Windows", "machine": "AMD64" },
  "compiler": {
    "found": true,
    "name": "gcc.exe",
    "version": "gcc (MinGW-W64) 14.2.0"
  },
  "engine": "native",
  "ready": true
}
```
- `engine` 取值：`"native"`（原生高速） 或 `"vm"`（内置解释器）
- 前端可据此展示"当前引擎：原生高速模式 ✅"

> **新增字段（1.1.0 起）**
> - `engine`：**本服务**（在线沙箱）实际可用的引擎。为 `"vm"` 时，
>   **C 库示例讲不了**（见下方 `/api/run` 的 `c_lib_unsupported`）。
> - `c_lib_available`：`engine == "native"` 的布尔等价形式。
>   前端可据此决定"在线试运行"区是否默认展示 `c_lib.cpy`，
>   若为 `false` 建议改用 `hello.cpy` / `dict.cpy` 做演示，
>   并在 C 库特性卡片上标注「需本地原生模式」。

---

### 2.3 `GET /api/download` — 下载编译器

**响应：** 二进制流（`Content-Type: application/zip`），
`Content-Disposition: attachment; filename="pypp-1.1.0.zip"`。

**压缩包结构：**
```
pypp-1.1.0/
├── install.py          # 命令行安装器（--check / --json / --uninstall）
├── install_gui.py      # 图形安装器（四步向导，需 tkinter）
├── install_gui.bat     # Windows 一键启动图形安装器
├── pget.py             # 包管理器（对标 pip，支持 import 第三方库 + cbind 转译 C 头文件）
├── pget.bat            # Windows 一键启动 pget
├── src/                # 编译器源码
│   ├── pypp.py         # 主驱动 + 工具链/Python 探测
│   ├── lexer.py
│   ├── parser.py
│   ├── codegen.py      # 含 CPython 桥接层 + C 互操作
│   ├── interpreter.py
│   └── repair.py
├── examples/
│   ├── hello.cpy
│   ├── tolerant.cpy
│   ├── showcase.cpy
│   ├── python_lib.cpy    # 用 Python 标准库（无需联网）
│   ├── use_requests.cpy  # 用 pget 装的第三方库
│   ├── dict.cpy          # 字典
│   ├── class.cpy         # 类
│   └── c_lib.cpy         # 调用 C 库（zlib / libm）
├── docs/WEB_API.md
├── web/server.py
└── README.md
```

**前端建议：**
1. 放一个"下载 py++"按钮，`href="/api/download"`；
2. 下载完成的引导文案可写：
   > 解压后双击 **install_gui.bat**（Windows），按向导完成安装。
   > 在「安装选项」页勾选「便携 C 编译器」即可启用接近 C++ 的原生高速模式。
3. 介绍第三方库能力时，可用：
   > 想用 Python 的库？`pget install requests` —— 和 pip 一样的用法，
   > 装完就能在 `.cpy` 里 `import requests`。
4. 介绍 C 库能力时（**仅在原生模式下可用**），可用：
   > 要调 C 库？`pget cbind zlib.h --link z --out zlib.cpy` 自动把头文件
   > 转译成声明，然后在 `.cpy` 里直接 `crc32(...)`。零绑定层，直通 ABI。

> 注意：图形安装器依赖 Python 的 `tkinter`。若用户环境没有，
> `install_gui.bat` 会给出提示并引导使用 `python install.py`。

---

### 2.4 `POST /api/run` — 在线试运行（可选）

**请求：**
```json
{ "code": "print(\"hello world\")" }
```

**响应：**
```json
{
  "ok": true,
  "mode": "native",
  "output": "hello world\n",
  "warnings": "",
  "repairs": [
    { "line": 1, "message": "'pritn' 疑似拼写错误，已自动更正为 'print'" }
  ],
  "engine_label": "原生高速模式",
  "time_ms": 3.2,
  "needs_native": false,
  "c_lib_unsupported": false,
  "note": ""
}
```

**字段说明：**

| 字段 | 类型 | 说明 |
|---|---|---|
| `ok` | bool | 是否成功。**`c_lib_unsupported` 为真时恒为 `false`** |
| `mode` | string | `"native"` / `"vm"` / `"timeout"` |
| `output` | string | 程序 stdout |
| `warnings` | string | 清理过提示语后的 stderr |
| `repairs` | array | 自动修复列表 |
| `engine_label` | string | 可直接显示的中文引擎名 |
| `time_ms` | number | 端到端耗时 |
| `needs_native` | bool | 代码里是否有 `extern` / `#link`（**1.1.0 新增**） |
| `c_lib_unsupported` | bool | 需要原生但降级到了 VM（**1.1.0 新增**） |
| `note` | string | 给用户看的解释文案（**1.1.0 新增**） |

> **⚠️ 前端必读：C 库示例的「无输出」问题**
>
> `c_lib.cpy` 这类示例依赖 C 库，而 **C 库只在原生模式可用**。
> 在线沙箱若没有 gcc，会静默降级到 VM，此时 `extern` 调用一律返回 `0`，
> 页面看起来就像"没有输出"。
>
> 服务端从 1.1.0 起会显式标记这种情况：
> ```json
> { "ok": false, "mode": "vm", "needs_native": true,
>   "c_lib_unsupported": true,
>   "note": "这段代码用到了 C 库（extern / #link），而 C 库只在原生高速模式下可用……" }
> ```
> **前端应在 `c_lib_unsupported == true` 时：**
> 1. 不要当成"程序跑通了但没输出"，而是展示 `note` 的说明；
> 2. 弱化 `output` 里那串 `0`（那是降级占位值，不是真实结果）；
> 3. 引导用户"下载后在本地运行"，或把演示切换成非 C 库的示例。
>
> 另注：`mode` 的判定**不要用 `"原生模式" in stderr`** 这种子串匹配 ——
> VM 模式下 C 库的修复提示里也含这几个字，会误判成原生。
> 服务端已改用精确的引擎标记行解析。

**安全要求（务必实现）：**
1. 只允许 `POST`，请求体 ≤ 64KB
2. 服务端用**子进程 + 超时**（建议 5 秒）执行，超时即杀
3. 建议以低权限用户运行，禁用网络
4. 生产环境应使用容器隔离

---

## 3. 前端页面建议

一个最小可用的网页包含三块：

1. **Hero 区**：标题 "py++"、副标题 tagline、下载按钮
2. **代码演示区**：一个文本框（预填 `sample_code`）+ "运行"按钮 → 调用 `/api/run` → 展示输出与"已自动修复"提示
3. **特性区**：渲染 `features` 数组

### 代码高亮建议
`.cpy` 的语法与 Python 高度一致，可直接复用 **Prism.js 的 `python` 语法**，
或注册别名 `Prism.languages.cpy = Prism.languages.python`。

### 主题色建议
- 主色：`#534AB7`（紫）
- 强调色：`#1D9E75`（青绿，用于"已自动修复"提示）
- 警告色：`#EF9F27`（琥珀，用于小错误提示）

---

## 4. 启动方式

py++ 自带一个可直接运行的 Web 服务：

```bash
python web/server.py            # 默认 http://localhost:8765
python web/server.py --port 9000
```

前端 AI 只需按上述契约对接即可，无需关心编译器内部实现。
