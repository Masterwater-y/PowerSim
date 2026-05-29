CC      ?= gcc
# SSE2 是 x86_64 baseline，gem5 X86 模型完整支持；AVX2/FMA 不被 gem5 SE 支持
override CFLAGS  := -O2 -g -Wall -Wextra -std=gnu11 -pthread -msse2 -mno-avx -mno-avx2 -mno-fma
override LDFLAGS := -static -pthread

mt_simd_fp: mt_simd_fp.c
	$(CC) $(CFLAGS) -o $@ $< $(LDFLAGS)

clean:
	rm -f mt_simd_fp

.PHONY: clean
