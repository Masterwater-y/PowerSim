CC      ?= gcc
CFLAGS  ?= -O2 -g -Wall -Wextra -std=gnu11 -pthread -I../common
LDFLAGS ?= -static -pthread

holdout_sharded_kv: holdout_sharded_kv.c ../common/pthread_harness.c ../common/pthread_harness.h
	$(CC) $(CFLAGS) -o $@ holdout_sharded_kv.c ../common/pthread_harness.c $(LDFLAGS)

clean:
	rm -f holdout_sharded_kv

.PHONY: clean
