CC      ?= gcc
CFLAGS  ?= -O2 -Wall -Wextra -static
LDFLAGS ?= -static

TARGET := codec_pipeline
SRC    := codec_pipeline.c

.PHONY: all clean run

all: $(TARGET)

$(TARGET): $(SRC)
	$(CC) $(CFLAGS) -o $@ $^ $(LDFLAGS)

run: $(TARGET)
	./$(TARGET) 1

clean:
	rm -f $(TARGET)
