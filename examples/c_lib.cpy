# examples/c_lib.cpy
# 演示：调用 C 库
# 运行：pypp examples/c_lib.cpy
#
# py++ 编译到 C，所以调用 C 库是"原生能力"——只需两件事：
#   1. extern 声明告诉编译器这个函数长什么样
#   2. #link 告诉链接器去哪个库里找
#
# 下面的声明可以用 `pget cbind <头文件> --link <库名>` 自动生成：
#   pget cbind tools/mingw64/mingw64/include/zlib.h --link z

#link z

extern str zlibVersion()
extern unsigned long compressBound(unsigned long sourceLen)
extern unsigned long crc32(unsigned long crc, str buf, unsigned int len)
extern unsigned long adler32(unsigned long adler, str buf, unsigned int len)

print("zlib 版本:", zlibVersion())

# 压缩上界：zlib 保证 compress() 的输出不会超过这个长度
print("100 字节的压缩上界:", compressBound(100))

# 校验和：值与 Python 的 zlib.crc32 / zlib.adler32 完全一致
print("crc32('hello'):", crc32(0, "hello", 5))
print("crc32('py++'):", crc32(0, "py++", 4))
print("adler32('hello'):", adler32(1, "hello", 5))

# 也可以调用 C 标准库（无需 #link，默认就会链接 libc）
extern int abs(int n)
extern double pow(double x, double y)

print("abs(-42):", abs(-42))
print("pow(2, 10):", pow(2, 10))
