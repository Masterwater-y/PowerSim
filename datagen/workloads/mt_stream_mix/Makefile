CC      ?= gcc
CFLAGS  ?= -O2 -g -Wall -Wextra -std=gnu11 -pthread -I../common
LDFLAGS ?= -static -pthread

mt_stream_mix: mt_stream_mix.c ../common/pthread_harness.c ../common/pthread_harness.h
	$(CC) $(CFLAGS) -o $@ mt_stream_mix.c ../common/pthread_harness.c $(LDFLAGS)

clean:
	rm -f mt_stream_mix

.PHONY: clean
