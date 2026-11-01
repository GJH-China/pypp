# examples/python_lib.cpy
# 演示：py++ 直接使用 Python 标准库
# 运行：pypp examples/python_lib.cpy
# 说明：这些是 Python 自带的库，无需 pget 安装。

import math
import json
import random
import os

print("=== 数学 ===")
print("圆周率:", math.pi)
print("根号 2:", math.sqrt(2))
print("向上取整 3.2:", math.ceil(3.2))

print("=== JSON ===")
data = json.dumps([1, 2, 3])
print("编码结果:", data)
print("长度:", len(data))

print("=== 随机数 ===")
random.seed(2026)
print("掷骰子:", random.randint(1, 6))

print("=== 路径 ===")
print("文件名:", os.path.basename("/home/user/notes.txt"))

print("=== 从模块直接取函数 ===")
from math import floor
print("向下取整 9.9:", floor(9.9))
