# LLMSim workloads Makefile —— 统一编译所有 bench_*.c -> bin/<name>
# 静态链接（gem5 SE 模式无动态 loader），-pthread，O2。
CC      ?= gcc
CFLAGS  ?= -O2 -g -Wall -std=gnu11 -pthread -Icommon -msse2
LDFLAGS ?= -static -pthread

SRCS := $(wildcard src/bench_*.c)
BINS := $(patsubst src/bench_%.c,bin/%,$(SRCS))
NAMES := $(patsubst bin/%,%,$(BINS))

all: $(BINS)

bin/%: src/bench_%.c common/tao_bench.h
	@mkdir -p bin
	$(CC) $(CFLAGS) -o $@ $< $(LDFLAGS)

# 特殊处理：fp_compute_dense / fp_lite 用 -O0，让 µop 序列与源码字面对应，
# 方便精确控制 FP / store / IPC 比例（不再与编译器优化斗智斗勇）。
bin/fp_compute_dense: CFLAGS = -O0 -g -Wall -std=gnu11 -pthread -Icommon -msse2
bin/fp_lite:          CFLAGS = -O0 -g -Wall -std=gnu11 -pthread -Icommon -msse2

# 允许直接使用 `make int_div` 这类裸目标，等价于构建 `bin/int_div`。
$(NAMES): %: bin/%

clean:
	rm -f $(BINS)

list:
	@echo $(BINS)

.PHONY: all clean list $(NAMES)
