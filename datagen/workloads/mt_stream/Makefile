CC      ?= gcc
CFLAGS  ?= -O2 -g -Wall -Wextra -std=gnu11 -pthread
LDFLAGS ?= -static -pthread

mt_stream: mt_stream.c
	$(CC) $(CFLAGS) -o $@ $< $(LDFLAGS)

clean:
	rm -f mt_stream

.PHONY: clean
