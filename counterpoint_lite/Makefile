PY ?= python3
ROOT_DIR := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
MODEL ?= $(ROOT_DIR)/examples/model_spr_core_minimal.json
OBS ?= $(ROOT_DIR)/examples/observation_toy_infeasible.json
OUT ?= $(ROOT_DIR)/out/smoke

.PHONY: help validate-machine enumerate run smoke perf-smoke minesim-smoke minesim-config-check clean print-vars

help:
	$(PY) -m counterpoint_lite --help

validate-machine:
	PYTHONPATH=$(ROOT_DIR) $(PY) -m counterpoint_lite validate-machine --output $(OUT)/machine.json

enumerate:
	PYTHONPATH=$(ROOT_DIR) $(PY) -m counterpoint_lite enumerate --model $(MODEL) --output-json $(OUT)/signatures.json --output-csv $(OUT)/signatures.csv

run:
	PYTHONPATH=$(ROOT_DIR) $(PY) -m counterpoint_lite run --model $(MODEL) --observation $(OBS) --output-dir $(OUT) || test $$? -eq 1

smoke: validate-machine enumerate run

perf-smoke:
	PYTHONPATH=$(ROOT_DIR) $(PY) $(ROOT_DIR)/scripts/run_perf_smoke.py --out $(OUT)/perf_smoke

minesim-smoke:
	PYTHONPATH=$(ROOT_DIR) $(PY) $(ROOT_DIR)/scripts/run_minesim_smoke.py --out $(OUT)/minesim_smoke

minesim-config-check:
	PYTHONPATH=$(ROOT_DIR) $(PY) $(ROOT_DIR)/scripts/run_minesim_config_check.py --out $(OUT)/minesim_config_check

clean:
	rm -rf $(ROOT_DIR)/out/*

print-vars:
	@echo ROOT_DIR=$(ROOT_DIR)
	@echo MODEL=$(MODEL)
	@echo OBS=$(OBS)
	@echo OUT=$(OUT)
