#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <inttypes.h>

static inline uint64_t mix64(uint64_t x) {
  x ^= x >> 33;
  x *= 0xff51afd7ed558ccdULL;
  x ^= x >> 33;
  x *= 0xc4ceb9fe1a85ec53ULL;
  x ^= x >> 33;
  return x;
}

int main(int argc, char **argv) {
  uint64_t n = 50000000ULL;
  if (argc > 1) {
    char *end = NULL;
    unsigned long long parsed = strtoull(argv[1], &end, 10);
    if (end == argv[1] || (end && *end != '\0') || parsed == 0ULL) {
      fprintf(stderr, "Usage: %s [iterations>0]\n", argv[0]);
      return 2;
    }
    n = (uint64_t)parsed;
  }

  volatile uint64_t sink = 0;
  uint64_t a = 0x123456789abcdef0ULL;
  uint64_t b = 0x0fedcba987654321ULL;
  uint64_t acc = 0;

  for (uint64_t i = 0; i < n; ++i) {
    a = a * 1664525ULL + 1013904223ULL;
    b ^= (a << 7) | (a >> 57);
    uint64_t t1 = (a + b) * 0x9e3779b97f4a7c15ULL;
    uint64_t t2 = (a ^ b) + (i * 0x94d049bb133111ebULL);
    acc += mix64(t1 ^ t2);
    acc ^= (acc << 13) | (acc >> 51);
  }

  sink = acc;
  printf("%" PRIu64 "\n", sink);
  return 0;
}