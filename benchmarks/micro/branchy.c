#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <time.h>

/*
 * branchy.c
 * Micro benchmark focused on branch-heavy control flow.
 *
 * Usage:
 *   ./branchy <iterations>
 *
 * Default iterations: 40,000,000
 */

static inline uint64_t xorshift64star(uint64_t *state) {
    uint64_t x = *state;
    x ^= x >> 12;
    x ^= x << 25;
    x ^= x >> 27;
    *state = x;
    return x * 2685821657736338717ULL;
}

int main(int argc, char **argv) {
    long long iterations = 40000000LL;
    if (argc > 1) {
        char *end = NULL;
        long long v = strtoll(argv[1], &end, 10);
        if (end && *end == '\0' && v > 0) {
            iterations = v;
        }
    }

    /* Fixed seed for reproducibility across runs */
    uint64_t s = 0x9E3779B97F4A7C15ULL;
    uint64_t acc = 0xC2B2AE3D27D4EB4FULL;

    clock_t t0 = clock();

    for (long long i = 0; i < iterations; ++i) {
        uint64_t r = xorshift64star(&s);
        uint32_t x = (uint32_t)(r ^ (r >> 32));

        /* Intentionally branch-heavy, data-dependent, and mixed predictability */
        if (x & 1U) {
            acc += (uint64_t)(x * 3U + 1U);
            if (x & 4U) {
                acc ^= (acc << 7) | (acc >> 57);
            } else {
                acc += (acc >> 3);
            }
        } else {
            acc ^= (uint64_t)(x + 0x9e3779b9U);
            if (x & 8U) {
                acc -= (acc << 5);
            } else {
                acc ^= (acc >> 11);
            }
        }

        if ((x & 0x3FU) == 0x15U) {
            acc += (uint64_t)i;
        } else if ((x & 0x3FU) == 0x2AU) {
            acc ^= (uint64_t)(i * 0x27d4eb2dU);
        } else if ((x & 0x3FU) == 0x3FU) {
            acc -= (uint64_t)(i ^ x);
        }

        /* Prevent trivial simplifications */
        acc = (acc << 1) | (acc >> 63);
    }

    clock_t t1 = clock();
    double elapsed = (double)(t1 - t0) / (double)CLOCKS_PER_SEC;

    /* Print stable output so the loop cannot be optimized away */
    printf("branchy: iters=%lld acc=%llu time=%.6f\n",
           iterations,
           (unsigned long long)acc,
           elapsed);

    /* Keep side effects alive while ensuring stable exit code for harness checks */
    volatile uint64_t exit_guard = acc;
    (void)exit_guard;
    return 0;
}