CC      ?= gcc
CFLAGS  ?= -O2 -Wall -Wextra -static -pthread
LDFLAGS ?= -static -pthread

TARGET := socket_dir_snp_storm
SRC    := socket_dir_snp_storm.c

.PHONY: all clean run

all: $(TARGET)

$(TARGET): $(SRC)
	$(CC) $(CFLAGS) -o $@ $^ $(LDFLAGS)

run: $(TARGET)
	taskset -c 0-7 ./$(TARGET) 32 8 64

clean:
	rm -f $(TARGET)
