CC ?= gcc
CFLAGS ?= -O2 -g -Wall -Wextra -Wno-unused-function -std=gnu11 -pthread -msse2
LDFLAGS ?= -static -pthread

BIN_DIR ?= bin
SRC := business_excitation.c
TARGETS := \
	$(BIN_DIR)/gofeed_fanout_wide \
	$(BIN_DIR)/gofeed_graph_pages_80 \
	$(BIN_DIR)/gofeed_graph_48m_random \
	$(BIN_DIR)/gofeed_shared_hotspot \
	$(BIN_DIR)/pytorch_dense_batch \
	$(BIN_DIR)/pytorch_embedding_768k \
	$(BIN_DIR)/pytorch_embedding_1536k \
	$(BIN_DIR)/pytorch_embedding_48m \
	$(BIN_DIR)/mysql_hot_index_24k \
	$(BIN_DIR)/mysql_hot_index_48k \
	$(BIN_DIR)/mysql_index_1280k \
	$(BIN_DIR)/mysql_table_96m_shared

all: $(TARGETS)

$(BIN_DIR):
	mkdir -p $(BIN_DIR)

define build_rule
$(BIN_DIR)/$(1): $(SRC) | $(BIN_DIR)
	$$(CC) $$(CFLAGS) -DBUSINESS_KIND=$(2) -DBUSINESS_NAME=\"$(1)\" -o $$@ $$< $$(LDFLAGS)
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
	rm -f $(TARGETS)

.PHONY: all clean
