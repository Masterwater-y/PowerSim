CC      ?= gcc
CFLAGS  ?= -O2 -Wall -Wextra -static -pthread
LDFLAGS ?= -static -pthread

TARGET := socket_dir_epoch
SRC    := socket_dir_epoch.c

.PHONY: all clean run

all: $(TARGET)

$(TARGET): $(SRC)
	$(CC) $(CFLAGS) -o $@ $^ $(LDFLAGS)

run: $(TARGET)
	taskset -c 0-7 ./$(TARGET) 128 8 8 2

clean:
	rm -f $(TARGET)
