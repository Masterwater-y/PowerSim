CC      ?= gcc
CFLAGS  ?= -O2 -g -Wall -Wextra -std=gnu11 -pthread -I../common
LDFLAGS ?= -static -pthread

mt_graph_walk: mt_graph_walk.c ../common/pthread_harness.c ../common/pthread_harness.h
	$(CC) $(CFLAGS) -o $@ mt_graph_walk.c ../common/pthread_harness.c $(LDFLAGS)

clean:
	rm -f mt_graph_walk

.PHONY: clean
