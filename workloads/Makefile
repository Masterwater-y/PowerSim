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

# 允许直接使用 `make int_div` 这类裸目标，等价于构建 `bin/int_div`。
$(NAMES): %: bin/%

clean:
	rm -f $(BINS)

list:
	@echo $(BINS)

.PHONY: all clean list $(NAMES)
