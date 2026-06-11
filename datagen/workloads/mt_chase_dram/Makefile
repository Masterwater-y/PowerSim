CC      ?= gcc
CFLAGS  ?= -O2 -g -Wall -Wextra -std=gnu11 -pthread
LDFLAGS ?= -static -pthread

mt_chase_dram: mt_chase_dram.c
	$(CC) $(CFLAGS) -o $@ $< $(LDFLAGS)

clean:
	rm -f mt_chase_dram

.PHONY: clean
