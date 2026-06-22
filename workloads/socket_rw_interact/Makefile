CC      ?= gcc
CFLAGS  ?= -O2 -Wall -Wextra -static -pthread
LDFLAGS ?= -static -pthread

TARGET := socket_rw_interact
SRC    := socket_rw_interact.c

.PHONY: all clean run

all: $(TARGET)

$(TARGET): $(SRC)
	$(CC) $(CFLAGS) -o $@ $^ $(LDFLAGS)

run: $(TARGET)
	taskset -c 0-7 ./$(TARGET) 16 8 256

clean:
	rm -f $(TARGET)
