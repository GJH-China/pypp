# py++ 基准测试：计算密集型
#
# ⚠️ 直接双击运行会停在第 6 行等输入 —— 这是**有意设计**：
#    seed 必须来自运行期输入，gcc 才不会把循环上界常量折叠掉，
#    基准测试才公平。随便输入一个整数（如 1）回车即可开始。
# 命令行一次到位：  echo 1 | pypp bench/bench.cpy
# 完整性能对比：    python bench/run_bench.py   （自动喂 seed，5 次取中位数）
seed = int(input("请输入一个整数 seed（任意数字即可，比如 1）: "))

N1 = 50000000 + seed
N2 = 20000000 + seed
N3 = 2000

s = 0
for i in range(0, N1):
    s += i
print("sum =", s)

f = 0.0
for i in range(0, N2):
    f = f + 1.5
print("float =", f)

count = 0
for i in range(0, N3):
    for j in range(0, N3):
        if (i + j) % 7 == 0:
            count += 1
print("count =", count)
