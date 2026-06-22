CC      ?= gcc
CFLAGS  ?= -O2 -Wall -Wextra -static -fno-omit-frame-pointer
LDFLAGS ?= -static

TARGET := analytics_roi
SRC    := analytics_roi.c

.PHONY: all clean run

all: $(TARGET)

$(TARGET): $(SRC)
	$(CC) $(CFLAGS) -o $@ $^ $(LDFLAGS)

run: $(TARGET)
	./$(TARGET) --warmup 2 --roi-iters 8 --perf-json perf_roi.json

clean:
	rm -f $(TARGET) perf_roi.json
