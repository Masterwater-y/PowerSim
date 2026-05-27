# Static-linked build for gem5 SE-mode (v2: coherence-only).
#
# usage:
#   make           # build mt_micro_coh
#   make clean

CC      ?= gcc
CFLAGS  ?= -O2 -g -Wall -Wextra -std=gnu11 -pthread
LDFLAGS ?= -static -pthread

mt_micro_coh: mt_micro_coh.c
	$(CC) $(CFLAGS) -o $@ $< $(LDFLAGS)

clean:
	rm -f mt_micro_coh

.PHONY: clean
