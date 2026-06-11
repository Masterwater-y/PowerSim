# MTAO — TAO Multi-core CPU Simulator
# ----------------------------------------
# 一键编排：编译 / 烟囱 / 基线 benchmark / 清理。
# 真实命令委托到各子模块自己的脚本，本 Makefile 不重复实现。
#
# 用法：
#   make bootstrap   # 第一次：clone gem5 + apply patch + 编译全套
#   make build       # 仅重新编译 gem5 + ref_sim_py.so
#   make smoke       # label-driven 烟囱（不需 ckpt）
#   make bench       # 用现成 ckpt + 5K rows benchmark
#   make quantum-sweep   # Quantum Δt 扫描
#   make clean       # 清理 runs / __pycache__
# ----------------------------------------

ROOT := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
SCRIPTS := $(ROOT)/scripts
DATAGEN := $(ROOT)/datagen
INFER   := $(ROOT)/infer
TRAIN   := $(ROOT)/train
GEM5    := $(ROOT)/gem5
PYTHON ?= /root/miniconda3/envs/yinhaolang/bin/python
JOBS   ?= $(shell nproc)

.PHONY: help bootstrap build build-gem5 build-refsim build-workloads \
        smoke bench quantum-sweep clean

help:
	@echo "MTAO targets:"
	@echo "  bootstrap       - clone gem5 + apply patches + build everything (first time)"
	@echo "  build           - rebuild gem5 + ref_sim_py.so (no clone)"
	@echo "  build-gem5      - only gem5"
	@echo "  build-refsim    - only mesi_ref_sim/build (ref_sim_py.so)"
	@echo "  build-workloads - only workloads/*"
	@echo "  smoke           - 5K rows label-driven smoke"
	@echo "  bench           - 5K rows ckpt benchmark (single GPU 0)"
	@echo "  quantum-sweep   - run scripts/05_quantum_sweep.sh"
	@echo "  clean           - remove tmp/, runs/ and __pycache__"

bootstrap:
	@echo ">>> [bootstrap] one-shot setup"
	bash $(ROOT)/bootstrap.sh

# build = bootstrap 中的步骤 b)+c)，要求 gem5/ 已存在
build: build-gem5 build-refsim build-workloads

build-gem5:
	@echo ">>> [gem5] re-run datagen/scripts/install.sh on existing gem5/"
	cd $(DATAGEN) && bash scripts/install.sh $(GEM5)

build-refsim:
	@echo ">>> [mesi_ref_sim] build ref_sim_py.so under infer/"
	cd $(INFER) && PYTHON=$(PYTHON) bash scripts/install.sh

build-workloads:
	@echo ">>> [workloads] build microbenches under datagen/workloads/"
	for w in mt_stream_mix mt_stencil2d mt_graph_walk mt_branch_state_machine mt_indirect_dispatch ; do \
	    $(MAKE) -C $(DATAGEN)/workloads/$$w ; \
	done

smoke:
	@echo ">>> [smoke] 5K rows label-driven"
	bash $(SCRIPTS)/04_infer.sh --mode label --smoke

bench:
	@echo ">>> [bench] 5K rows ckpt mode (GPU 0)"
	bash $(SCRIPTS)/04_infer.sh --mode ckpt --smoke

quantum-sweep:
	bash $(SCRIPTS)/05_quantum_sweep.sh \
	    --deltas 1,128,256,512,1024

clean:
	rm -rf $(ROOT)/runs $(ROOT)/tmp
	find $(ROOT) -name __pycache__ -type d -prune -exec rm -rf {} +
