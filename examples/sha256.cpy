# examples/sha256.cpy
# 演示：用 py++ 计算任意文件的 SHA-256 值
# 运行：pypp examples/sha256.cpy
#       （运行后按提示输入文件路径；也支持 echo 文件 | pypp examples/sha256.cpy）
#
# 这条脚本走的是 py++ 的「Python 库」通道：import 进来的是真正的 CPython，
# hashlib / io / os 都是货真价实的标准库，原生模式与 VM 模式行为一致。
#
# 分块读取（每块 8192 字节），再大的文件也不会一次性吃进内存。

import io
import os
import hashlib

print("=== py++ SHA-256 计算器 ===")
path = input("请输入文件路径: ")

if int(os.path.isfile(path)):
    f = io.open(path, "rb")
    h = hashlib.sha256()
    size = 0
    while True:
        chunk = f.read(8192)
        n = len(chunk)
        if n == 0:
            break
        size = size + n
        h.update(chunk)
    f.close()
    print("文件:", path)
    print("大小:", size, "字节")
    print("SHA-256:", h.hexdigest())
else:
    print("错误: 找不到文件 ->", path)
    print("用法: pypp sha256.cpy  （运行后输入要计算的路径）")
