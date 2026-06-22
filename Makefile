.SHELLFLAGS := -eu -o pipefail -c
SHELL := /bin/bash

.PHONY: bootstrap build build-minesim build-workloads smoke full clean

bootstrap:
	./scripts/bootstrap.sh

build: build-minesim build-workloads

build-minesim:
	. ./env.sh && $(MAKE) -C minesim minesim

build-workloads:
	find workloads -mindepth 2 -maxdepth 2 -name Makefile -print0 | \
		while IFS= read -r -d '' mf; do $(MAKE) -C "$$(dirname "$$mf")" all; done

smoke:
	./scripts/verify_full_loop.sh --smoke

full:
	./scripts/verify_full_loop.sh --full

clean:
	$(MAKE) -C minesim clean
	find workloads -mindepth 2 -maxdepth 2 -name Makefile -print0 | \
		while IFS= read -r -d '' mf; do $(MAKE) -C "$$(dirname "$$mf")" clean; done
