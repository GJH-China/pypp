# examples/use_requests.cpy
# 演示：py++ 使用 pget 安装的第三方库
#
# 先装库：
#     pget install requests
# 再运行：
#     pypp examples/use_requests.cpy
#
# requests 是纯 Python 包，但它的依赖里带二进制扩展
# （charset_normalizer），能跑起来就说明原生嵌入的 CPython 一切正常。

import requests
from urllib3 import __version__ as urllib3_version

print("requests 版本:", requests.__version__)
print("urllib3  版本:", urllib3_version)

print("=== 常见状态码 ===")
print("200 表示:", requests.codes.ok)
print("404 表示:", requests.codes.not_found)

print("=== 真实请求（需要联网）===")
r = requests.get("https://pypi.org/pypi/six/json")
print("状态码:", r.status_code)
if r.status_code == 200:
    info = r.json()
    print("six 最新版本:", info["info"]["version"])
else:
    print("（网络不可用，跳过）")
