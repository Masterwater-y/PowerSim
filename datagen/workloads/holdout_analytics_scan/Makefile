CC      ?= gcc
CFLAGS  ?= -O2 -g -Wall -Wextra -std=gnu11 -pthread -I../common
LDFLAGS ?= -static -pthread

holdout_analytics_scan: holdout_analytics_scan.c ../common/pthread_harness.c ../common/pthread_harness.h
	$(CC) $(CFLAGS) -o $@ holdout_analytics_scan.c ../common/pthread_harness.c $(LDFLAGS)

clean:
	rm -f holdout_analytics_scan

.PHONY: clean
