CC      ?= gcc
CFLAGS  ?= -O2 -Wall -Wextra -static -pthread
LDFLAGS ?= -static -pthread

TARGET := mt_bench
SRC    := mt_bench.c

.PHONY: all clean run

all: $(TARGET)

$(TARGET): $(SRC)
	$(CC) $(CFLAGS) -o $@ $^ $(LDFLAGS)

run: $(TARGET)
	./$(TARGET) 1000 4

clean:
	rm -f $(TARGET)
