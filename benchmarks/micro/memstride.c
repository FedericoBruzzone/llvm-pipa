#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <time.h>

static inline uint64_t now_ns(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000000ull + (uint64_t)ts.tv_nsec;
}

static uint64_t xorshift64(uint64_t *state) {
    uint64_t x = *state;
    x ^= x << 13;
    x ^= x >> 7;
    x ^= x << 17;
    *state = x;
    return x;
}

int main(int argc, char **argv) {
    // Usage: ./memstride [stride] [iterations]
    // Defaults chosen to produce a measurable memory-heavy workload.
    size_t stride = 64;
    size_t iterations = 20000000ull;

    if (argc > 1) {
        char *end = NULL;
        unsigned long s = strtoul(argv[1], &end, 10);
        if (end && *end == '\0' && s > 0) {
            stride = (size_t)s;
        } else {
            fprintf(stderr, "Invalid stride: %s\n", argv[1]);
            return 2;
        }
    }

    if (argc > 2) {
        char *end = NULL;
        unsigned long long it = strtoull(argv[2], &end, 10);
        if (end && *end == '\0' && it > 0) {
            iterations = (size_t)it;
        } else {
            fprintf(stderr, "Invalid iterations: %s\n", argv[2]);
            return 2;
        }
    }

    // Keep size reasonably large to exercise caches/DRAM while still practical in containers.
    const size_t n = 1u << 20; // 1,048,576 elements
    int *arr = (int *)malloc(n * sizeof(int));
    if (!arr) {
        fprintf(stderr, "Allocation failed\n");
        return 1;
    }

    // Deterministic init with lightweight PRNG.
    uint64_t seed = 0x9e3779b97f4a7c15ull;
    for (size_t i = 0; i < n; ++i) {
        arr[i] = (int)(xorshift64(&seed) & 0x7fffffff);
    }

    // Stride loop with wrap-around to keep accesses in-bounds.
    volatile uint64_t sink = 0;
    size_t idx = 0;

    uint64_t t0 = now_ns();
    for (size_t i = 0; i < iterations; ++i) {
        sink += (uint64_t)arr[idx];
        // Mix read/write to stress memory hierarchy a bit more.
        arr[idx] = arr[idx] + (int)(i & 7u);
        idx += stride;
        if (idx >= n) idx -= n;
    }
    uint64_t t1 = now_ns();

    double elapsed_ms = (double)(t1 - t0) / 1e6;
    // Print sink to prevent optimization removing the loop.
    printf("memstride: n=%zu stride=%zu iterations=%zu sink=%llu elapsed_ms=%.3f\n",
           n, stride, iterations, (unsigned long long)sink, elapsed_ms);

    free(arr);
    return 0;
}