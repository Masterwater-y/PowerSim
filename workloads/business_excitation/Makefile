CC ?= gcc
CFLAGS ?= -O2 -g -Wall -Wextra -Wno-unused-function -std=gnu11 -pthread -msse2
LDFLAGS ?= -static -pthread

PROJECT_ROOT := $(abspath $(CURDIR)/../..)
ROI_DIR := $(PROJECT_ROOT)/workloads/common/roi
DYNAMORIO_MARKER_LDSCRIPT := $(ROI_DIR)/dynamoRIO_marker.ld
GEM5_BIN_DIR ?= bin/gem5
DYNAMORIO_BIN_DIR ?= bin/dynamoRIO
SRC := business_excitation.c
NAMES := \
	gofeed_fanout_wide \
	gofeed_graph_pages_80 \
	gofeed_graph_48m_random \
	gofeed_shared_hotspot \
	pytorch_dense_batch \
	pytorch_embedding_768k \
	pytorch_embedding_1536k \
	pytorch_embedding_48m \
	mysql_hot_index_24k \
	mysql_hot_index_48k \
	mysql_index_1280k \
	mysql_table_96m_shared
GEM5_TARGETS := $(addprefix $(GEM5_BIN_DIR)/,$(NAMES))
DYNAMORIO_TARGETS := $(addprefix $(DYNAMORIO_BIN_DIR)/,$(NAMES))

all: $(GEM5_TARGETS)
dynamoRIO: $(DYNAMORIO_TARGETS)

$(GEM5_BIN_DIR) $(DYNAMORIO_BIN_DIR):
	mkdir -p $@

define build_rule
$(GEM5_BIN_DIR)/$(1): $(SRC) $(ROI_DIR)/fastsim_roi.h | $(GEM5_BIN_DIR)
	$$(CC) $$(CFLAGS) -I$(ROI_DIR) -DBUSINESS_KIND=$(2) \
		-DBUSINESS_NAME=\"$(1)\" -o $$@ $$< $$(LDFLAGS)
$(DYNAMORIO_BIN_DIR)/$(1): $(SRC) $(ROI_DIR)/fastsim_roi.h $(ROI_DIR)/dynamoRIO_markers.c $(DYNAMORIO_MARKER_LDSCRIPT) | $(DYNAMORIO_BIN_DIR)
	$$(CC) $$(CFLAGS) -DFASTSIM_DR_TRACE -I$(ROI_DIR) \
		-DBUSINESS_KIND=$(2) -DBUSINESS_NAME=\"$(1)\" -o $$@ \
		$$< $(ROI_DIR)/dynamoRIO_markers.c $$(LDFLAGS) \
		-Wl,-T,$(DYNAMORIO_MARKER_LDSCRIPT)
endef

$(eval $(call build_rule,gofeed_fanout_wide,GOFEED_FANOUT_WIDE))
$(eval $(call build_rule,gofeed_graph_pages_80,GOFEED_GRAPH_PAGES_80))
$(eval $(call build_rule,gofeed_graph_48m_random,GOFEED_GRAPH_48M_RANDOM))
$(eval $(call build_rule,gofeed_shared_hotspot,GOFEED_SHARED_HOTSPOT))
$(eval $(call build_rule,pytorch_dense_batch,PYTORCH_DENSE_BATCH))
$(eval $(call build_rule,pytorch_embedding_768k,PYTORCH_EMBEDDING_768K))
$(eval $(call build_rule,pytorch_embedding_1536k,PYTORCH_EMBEDDING_1536K))
$(eval $(call build_rule,pytorch_embedding_48m,PYTORCH_EMBEDDING_48M))
$(eval $(call build_rule,mysql_hot_index_24k,MYSQL_HOT_INDEX_24K))
$(eval $(call build_rule,mysql_hot_index_48k,MYSQL_HOT_INDEX_48K))
$(eval $(call build_rule,mysql_index_1280k,MYSQL_INDEX_1280K))
$(eval $(call build_rule,mysql_table_96m_shared,MYSQL_TABLE_96M_SHARED))

clean:
	rm -f $(GEM5_TARGETS) $(DYNAMORIO_TARGETS)

.PHONY: all dynamoRIO clean
