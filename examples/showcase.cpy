# py++ 能力演示
# 故意包含小错误，看 py++ 如何自动兼容

pritn("=== py++ 演示开始 ===")

# 1. 基础运算
a = 15
b = 4
print("a + b =", a + b)
print("a * b =", a * b)
print("a / b =", a / b)

# 2. 循环求和（这里故意少写冒号）
total = 0
for i in range(1, 11)
    total += i
print("1加到10 =", total)

# 3. 函数
def fib(n):
    if n <= 1:
        return n
    return fib(n - 1) + fib(n - 2)
print("fib(20) =", fib(20))

# 4. 条件分支
score = 85
if score >= 90:
    print("优秀")
elif score >= 80:
    print("良好")
else:
    print("及格")

# 5. 未定义变量（容错测试）
print("未定义变量 =", not_defined_var)

print("=== 演示结束 ===")
