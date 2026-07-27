CC ?= gcc
CFLAGS ?= -O2 -g -Wall -Wextra -Wno-unused-function -Wno-unused-parameter -std=gnu11 -pthread -msse2
LDFLAGS ?= -static -pthread

BIN_DIR ?= ../bin
SRC := v28_business_proxy.c

TRAIN_TARGETS := \
	$(BIN_DIR)/v28_int_alu_dense \
	$(BIN_DIR)/v28_int_div_serial \
	$(BIN_DIR)/v28_fp_alu_dense \
	$(BIN_DIR)/v28_simd_sse_dense \
	$(BIN_DIR)/v28_cache_L1_mixed \
	$(BIN_DIR)/v28_cache_L2_mixed \
	$(BIN_DIR)/v28_memory_seq_moderate \
	$(BIN_DIR)/v28_memory_random_mlp \
	$(BIN_DIR)/v28_coh_readmostly_sparse \
	$(BIN_DIR)/v28_marine_base \
	$(BIN_DIR)/v28_gofeed_base \
	$(BIN_DIR)/v28_flink_base \
	$(BIN_DIR)/v28_mysql_base \
	$(BIN_DIR)/v28_redis_base \
	$(BIN_DIR)/v28_pytorch_base \
	$(BIN_DIR)/v28_bvc_encoder_base

HELDOUT_TARGETS := \
	$(BIN_DIR)/v28_marine_heldout \
	$(BIN_DIR)/v28_gofeed_heldout \
	$(BIN_DIR)/v28_flink_heldout \
	$(BIN_DIR)/v28_mysql_heldout \
	$(BIN_DIR)/v28_redis_heldout \
	$(BIN_DIR)/v28_pytorch_heldout \
	$(BIN_DIR)/v28_bvc_encoder_heldout

TARGETS := $(TRAIN_TARGETS) $(HELDOUT_TARGETS)

all: $(TARGETS)
train: $(TRAIN_TARGETS)
heldout: $(HELDOUT_TARGETS)

$(BIN_DIR):
	mkdir -p $(BIN_DIR)

define build_rule
$(BIN_DIR)/$(1): $(SRC) | $(BIN_DIR)
	$$(CC) $$(CFLAGS) -DV28_KIND=$(2) -DV28_NAME=\"$(1)\" -o $$@ $$< $$(LDFLAGS)
endef

$(eval $(call build_rule,v28_int_alu_dense,V28_INT_ALU_DENSE))
$(eval $(call build_rule,v28_int_div_serial,V28_INT_DIV_SERIAL))
$(eval $(call build_rule,v28_fp_alu_dense,V28_FP_ALU_DENSE))
$(eval $(call build_rule,v28_simd_sse_dense,V28_SIMD_SSE_DENSE))
$(eval $(call build_rule,v28_cache_L1_mixed,V28_CACHE_L1_MIXED))
$(eval $(call build_rule,v28_cache_L2_mixed,V28_CACHE_L2_MIXED))
$(eval $(call build_rule,v28_memory_seq_moderate,V28_MEMORY_SEQ_MODERATE))
$(eval $(call build_rule,v28_memory_random_mlp,V28_MEMORY_RANDOM_MLP))
$(eval $(call build_rule,v28_coh_readmostly_sparse,V28_COH_READMOSTLY_SPARSE))
$(eval $(call build_rule,v28_marine_base,V28_MARINE_BASE))
$(eval $(call build_rule,v28_gofeed_base,V28_GOFEED_BASE))
$(eval $(call build_rule,v28_flink_base,V28_FLINK_BASE))
$(eval $(call build_rule,v28_mysql_base,V28_MYSQL_BASE))
$(eval $(call build_rule,v28_redis_base,V28_REDIS_BASE))
$(eval $(call build_rule,v28_pytorch_base,V28_PYTORCH_BASE))
$(eval $(call build_rule,v28_bvc_encoder_base,V28_BVC_ENCODER_BASE))
$(eval $(call build_rule,v28_marine_heldout,V28_MARINE_HELDOUT))
$(eval $(call build_rule,v28_gofeed_heldout,V28_GOFEED_HELDOUT))
$(eval $(call build_rule,v28_flink_heldout,V28_FLINK_HELDOUT))
$(eval $(call build_rule,v28_mysql_heldout,V28_MYSQL_HELDOUT))
$(eval $(call build_rule,v28_redis_heldout,V28_REDIS_HELDOUT))
$(eval $(call build_rule,v28_pytorch_heldout,V28_PYTORCH_HELDOUT))
$(eval $(call build_rule,v28_bvc_encoder_heldout,V28_BVC_ENCODER_HELDOUT))

clean:
	rm -f $(TARGETS)

.PHONY: all train heldout clean
