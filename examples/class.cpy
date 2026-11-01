# examples/class.cpy
# 演示：类
# 运行：pypp examples/class.cpy
#
# 原生模式会把类编译成 C 结构体 + 一组函数（零开销），
# 所以字段访问和方法调用和普通变量一样快。

class Point:
    def __init__(self, x, y):
        self.x = x
        self.y = y

    def total(self):
        return self.x + self.y

    def move(self, dx, dy):
        self.x = self.x + dx
        self.y = self.y + dy


class Counter:
    def __init__(self, start):
        self.value = start

    def inc(self):
        self.value = self.value + 1
        return self.value


print("=== 点 ===")
p = Point(3, 4)
print("坐标:", p.x, p.y)
print("坐标和:", p.total())

p.move(10, 20)
print("移动后:", p.x, p.y)

print("=== 计数器 ===")
c = Counter(0)
c.inc()
c.inc()
print("计数值:", c.inc())

print("=== 带字符串字段 ===")


class Person:
    def __init__(self, name, age):
        self.name = name
        self.age = age

    def greet(self):
        print("你好，" + self.name)

    def birthday(self):
        self.age = self.age + 1
        return self.age


a = Person("py++", 1)
a.greet()
print("年龄:", a.age)
print("过生日后:", a.birthday())
