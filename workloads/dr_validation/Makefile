CC ?= gcc
CFLAGS ?= -O2 -g -Wall -Wextra -Wno-unused-function -Wno-unused-parameter -std=gnu11 -pthread -msse2 -fno-pie
LDFLAGS ?= -static -pthread

PROJECT_ROOT := $(abspath $(CURDIR)/../..)
ROI_DIR := $(PROJECT_ROOT)/workloads/common/roi
DYNAMORIO_MARKER_LDSCRIPT := $(ROI_DIR)/dynamoRIO_marker.ld
GEM5_BIN_DIR ?= bin/gem5
DYNAMORIO_BIN_DIR ?= bin/dynamoRIO
SRC := v28_business_proxy.c

TRAIN_NAMES := v28_int_alu_dense v28_int_div_serial v28_fp_alu_dense \
	v28_simd_sse_dense v28_cache_L1_mixed v28_cache_L2_mixed \
	v28_memory_seq_moderate v28_memory_random_mlp v28_coh_readmostly_sparse \
	v28_marine_base v28_gofeed_base v28_flink_base v28_mysql_base \
	v28_redis_base v28_pytorch_base v28_bvc_encoder_base

HELDOUT_NAMES := v28_marine_heldout v28_gofeed_heldout v28_flink_heldout \
	v28_mysql_heldout v28_redis_heldout v28_pytorch_heldout \
	v28_bvc_encoder_heldout

NAMES := $(TRAIN_NAMES) $(HELDOUT_NAMES)
GEM5_TARGETS := $(addprefix $(GEM5_BIN_DIR)/,$(NAMES))
DYNAMORIO_TARGETS := $(addprefix $(DYNAMORIO_BIN_DIR)/,$(NAMES))

all: $(GEM5_TARGETS)
train: $(addprefix $(GEM5_BIN_DIR)/,$(TRAIN_NAMES))
heldout: $(addprefix $(GEM5_BIN_DIR)/,$(HELDOUT_NAMES))
dynamoRIO: $(DYNAMORIO_TARGETS)

$(GEM5_BIN_DIR) $(DYNAMORIO_BIN_DIR):
	mkdir -p $@

define build_rule
$(GEM5_BIN_DIR)/$(1): $(SRC) $(ROI_DIR)/fastsim_roi.h | $(GEM5_BIN_DIR)
	$$(CC) $$(CFLAGS) -I$(ROI_DIR) -DV28_KIND=$(2) -DV28_NAME=\"$(1)\" \
		-o $$@ $$< $$(LDFLAGS) -no-pie
$(DYNAMORIO_BIN_DIR)/$(1): $(SRC) $(ROI_DIR)/fastsim_roi.h $(ROI_DIR)/dynamoRIO_markers.c $(DYNAMORIO_MARKER_LDSCRIPT) | $(DYNAMORIO_BIN_DIR)
	$$(CC) $$(CFLAGS) -DFASTSIM_DR_TRACE -I$(ROI_DIR) \
		-DV28_KIND=$(2) -DV28_NAME=\"$(1)\" -o $$@ \
		$$< $(ROI_DIR)/dynamoRIO_markers.c $$(LDFLAGS) -no-pie \
		-Wl,-T,$(DYNAMORIO_MARKER_LDSCRIPT)
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
	rm -f $(GEM5_TARGETS) $(DYNAMORIO_TARGETS)

.PHONY: all train heldout dynamoRIO clean
