CC      ?= gcc
CFLAGS  ?= -O2 -g -Wall -Wextra -std=gnu11 -pthread -I../common
LDFLAGS ?= -static -pthread -lm

mt_stencil2d: mt_stencil2d.c ../common/pthread_harness.c ../common/pthread_harness.h
	$(CC) $(CFLAGS) -o $@ mt_stencil2d.c ../common/pthread_harness.c $(LDFLAGS)

clean:
	rm -f mt_stencil2d

.PHONY: clean
