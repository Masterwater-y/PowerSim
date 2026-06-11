CC      ?= gcc
CFLAGS  ?= -O2 -g -Wall -Wextra -std=gnu11 -pthread
LDFLAGS ?= -static -pthread

mt_indirect_jump: mt_indirect_jump.c
	$(CC) $(CFLAGS) -o $@ $< $(LDFLAGS)

clean:
	rm -f mt_indirect_jump

.PHONY: clean
