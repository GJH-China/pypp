#include <cstdio>
int main() {
    long long seed = 0;
    scanf("%lld", &seed);
    long long N1 = 50000000 + seed;
    long long N2 = 20000000 + seed;
    long long N3 = 2000;

    long long s = 0;
    for (long long i = 0; i < N1; i++) s += i;
    printf("sum = %lld\n", s);

    double f = 0.0;
    for (long long i = 0; i < N2; i++) f = f + 1.5;
    printf("float = %g\n", f);

    long long count = 0;
    for (long long i = 0; i < N3; i++)
        for (long long j = 0; j < N3; j++)
            if ((i + j) % 7 == 0) count++;
    printf("count = %lld\n", count);
    return 0;
}
