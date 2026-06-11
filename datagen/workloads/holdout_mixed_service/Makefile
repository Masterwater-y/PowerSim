CC      ?= gcc
CFLAGS  ?= -O2 -g -Wall -Wextra -std=gnu11 -pthread -I../common
LDFLAGS ?= -static -pthread

holdout_mixed_service: holdout_mixed_service.c ../common/pthread_harness.c ../common/pthread_harness.h
	$(CC) $(CFLAGS) -o $@ holdout_mixed_service.c ../common/pthread_harness.c $(LDFLAGS)

clean:
	rm -f holdout_mixed_service

.PHONY: clean
