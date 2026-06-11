CC      ?= gcc
CFLAGS  ?= -O2 -g -Wall -Wextra -std=gnu11 -pthread -I../common
LDFLAGS ?= -static -pthread

mt_indirect_dispatch: mt_indirect_dispatch.c ../common/pthread_harness.c ../common/pthread_harness.h
	$(CC) $(CFLAGS) -o $@ mt_indirect_dispatch.c ../common/pthread_harness.c $(LDFLAGS)

clean:
	rm -f mt_indirect_dispatch

.PHONY: clean
