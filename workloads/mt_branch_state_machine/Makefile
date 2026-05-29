CC      ?= gcc
CFLAGS  ?= -O2 -g -Wall -Wextra -std=gnu11 -pthread -I../common
LDFLAGS ?= -static -pthread

mt_branch_state_machine: mt_branch_state_machine.c ../common/pthread_harness.c ../common/pthread_harness.h
	$(CC) $(CFLAGS) -o $@ mt_branch_state_machine.c ../common/pthread_harness.c $(LDFLAGS)

clean:
	rm -f mt_branch_state_machine

.PHONY: clean
