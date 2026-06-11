CC      ?= gcc
# 关键：-fno-if-conversion 禁止编译器把 if 折叠成 cmov，保证产生真正的 BR_COND
CFLAGS  ?= -O2 -g -Wall -Wextra -std=gnu11 -pthread -fno-if-conversion -fno-if-conversion2
LDFLAGS ?= -static -pthread

mt_branch_storm: mt_branch_storm.c
	$(CC) $(CFLAGS) -o $@ $< $(LDFLAGS)

clean:
	rm -f mt_branch_storm

.PHONY: clean
