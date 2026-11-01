# examples/dict.cpy
# 演示：字典
# 运行：pypp examples/dict.cpy

# 字面量（键值可以是混合类型）
config = {"host": "localhost", "port": 8080, "debug": 1, "ratio": 0.75}

print("主机:", config["host"])
print("端口:", config["port"])
print("配置项数:", len(config))

# 写入
config["timeout"] = 30
print("加了 timeout 后:", len(config))

# 成员判断
if "debug" in config:
    print("处于调试模式")
if "proxy" not in config:
    print("没有配置 proxy")

# 遍历（顺序与插入顺序一致）
print("--- 所有键 ---")
for key in config:
    print(key)

# 带默认值的读取
print("取不存在的键:", config.get("proxy"))
print("带默认值:", config.get("proxy", "直连"))

# 常见用法：计数
words = ["apple", "banana", "apple", "cherry", "apple", "banana"]
counts = {}
for w in words:
    counts[w] = counts.get(w, 0) + 1
print("--- 词频 ---")
for w in counts:
    print(w + ":", counts[w])
