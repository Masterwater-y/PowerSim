CC      ?= gcc
CFLAGS  ?= -O2 -Wall -Wextra -static
LDFLAGS ?= -static

TARGET := cache_bench
SRC    := cache_bench.c

.PHONY: all clean run run-gem5

all: $(TARGET)

$(TARGET): $(SRC)
	$(CC) $(CFLAGS) -o $@ $^ $(LDFLAGS)

# 本机跑一下验证功能 (不经过 gem5)
run: $(TARGET)
	./$(TARGET) 8

# 经过 gem5 SE 跑并产出 SIFT trace;
# 从 workloads/cache_bench 相对 gem5 的路径: ../../gem5
run-gem5: $(TARGET)
	cd ../../gem5 && \
	./build/X86/gem5.opt \
	  --outdir=out/cache_bench \
	  configs/example/gem5_library/x86-se-xeon8457c-o3.py \
	  --num-cores=1 \
	  --sift=out/cache_bench/trace.sift \
	  --cmd=$(abspath $(TARGET)) \
	  -- 8

clean:
	rm -f $(TARGET)
