CC ?= gcc
CFLAGS ?= -O2 -g -Wall -Wextra -Wno-unused-function -Wno-unused-parameter -std=gnu11 -pthread -msse2
LDFLAGS ?= -static -pthread

BIN_DIR ?= ../bin
SRC := v27_microbench.c

TRAIN_TARGETS := \
	$(BIN_DIR)/int_alu_dense \
	$(BIN_DIR)/int_div_serial \
	$(BIN_DIR)/fp_alu_dense \
	$(BIN_DIR)/simd_sse_dense \
	$(BIN_DIR)/stream_seq_L2 \
	$(BIN_DIR)/stream_seq_DRAM \
	$(BIN_DIR)/random_DRAM \
	$(BIN_DIR)/chase_DRAM \
	$(BIN_DIR)/coh_read_share \
	$(BIN_DIR)/coh_write_share \
	$(BIN_DIR)/coh_false_share \
	$(BIN_DIR)/coh_asym_rw \
	$(BIN_DIR)/phase_coh_onset \
	$(BIN_DIR)/skew_hot_cold \
	$(BIN_DIR)/ranking_mix_private

HELDOUT_TARGETS := \
	$(BIN_DIR)/phase_coh_decay

TARGETS := $(TRAIN_TARGETS) $(HELDOUT_TARGETS)
LEGACY_TARGETS := \
	$(BIN_DIR)/int_div_dense \
	$(BIN_DIR)/fp_fma_dense \
	$(BIN_DIR)/simd_dense \
	$(BIN_DIR)/chase_LLC \
	$(BIN_DIR)/coh_ws \
	$(BIN_DIR)/coh_atomic_cas \
	$(BIN_DIR)/coh_prod_cons \
	$(BIN_DIR)/coh_migratory \
	$(BIN_DIR)/skew_producer_amp

all: $(TARGETS)

$(BIN_DIR):
	mkdir -p $(BIN_DIR)

define build_rule
$(BIN_DIR)/$(1): $(SRC) | $(BIN_DIR)
	$$(CC) $$(CFLAGS) -DV27_KIND=$(2) -DV27_NAME=\"$(1)\" -o $$@ $$< $$(LDFLAGS)
endef

$(eval $(call build_rule,int_alu_dense,V27_INT_ALU_DENSE))
$(eval $(call build_rule,int_div_serial,V27_INT_DIV_SERIAL))
$(eval $(call build_rule,fp_alu_dense,V27_FP_ALU_DENSE))
$(eval $(call build_rule,simd_sse_dense,V27_SIMD_SSE_DENSE))
$(eval $(call build_rule,stream_seq_L2,V27_STREAM_SEQ_L2))
$(eval $(call build_rule,stream_seq_DRAM,V27_STREAM_SEQ_DRAM))
$(eval $(call build_rule,random_DRAM,V27_RANDOM_DRAM))
$(eval $(call build_rule,chase_DRAM,V27_CHASE_DRAM))
$(eval $(call build_rule,coh_read_share,V27_COH_READ_SHARE))
$(eval $(call build_rule,coh_write_share,V27_COH_WRITE_SHARE))
$(eval $(call build_rule,coh_false_share,V27_COH_FALSE_SHARE))
$(eval $(call build_rule,coh_asym_rw,V27_COH_ASYM_RW))
$(eval $(call build_rule,phase_ws_grow,V27_PHASE_WS_GROW))
$(eval $(call build_rule,phase_coh_onset,V27_PHASE_COH_ONSET))
$(eval $(call build_rule,skew_hot_cold,V27_SKEW_HOT_COLD))
$(eval $(call build_rule,ranking_mix_private,V27_RANKING_MIX_PRIVATE))
$(eval $(call build_rule,phase_ws_shrink,V27_PHASE_WS_SHRINK))
$(eval $(call build_rule,phase_coh_decay,V27_PHASE_COH_DECAY))

clean:
	rm -f $(TARGETS) $(LEGACY_TARGETS)

.PHONY: all clean
