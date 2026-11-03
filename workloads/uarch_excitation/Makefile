CC ?= gcc
CFLAGS ?= -O2 -g -Wall -Wextra -std=gnu11 -pthread
LDFLAGS ?= -static -pthread
BIN_DIR ?= bin
SRC := uarch_excitation.c

TARGETS := \
	$(BIN_DIR)/uarch_rob80 \
	$(BIN_DIR)/uarch_rob160 \
	$(BIN_DIR)/uarch_rob240 \
	$(BIN_DIR)/uarch_iq24 \
	$(BIN_DIR)/uarch_iq56 \
	$(BIN_DIR)/uarch_iq72 \
	$(BIN_DIR)/uarch_iq88 \
	$(BIN_DIR)/uarch_dtlb48 \
	$(BIN_DIR)/uarch_dtlb80 \
	$(BIN_DIR)/uarch_dtlb96 \
	$(BIN_DIR)/uarch_l1_24k \
	$(BIN_DIR)/uarch_l1_40k \
	$(BIN_DIR)/uarch_l1_48k \
	$(BIN_DIR)/uarch_l2_768k \
	$(BIN_DIR)/uarch_l2_1280k \
	$(BIN_DIR)/uarch_l2_1536k

all: $(TARGETS)

$(BIN_DIR):
	mkdir -p $@

define build_rule
$(BIN_DIR)/$(1): $(SRC) | $(BIN_DIR)
	$$(CC) $$(CFLAGS) -DUARCH_KIND=$(2) -DUARCH_NAME=\"$(1)\" \
		-DUARCH_DEPTH=$(3) -DUARCH_FOOTPRINT=$(4) -o $$@ $$< $$(LDFLAGS)
endef

$(eval $(call build_rule,uarch_rob80,1,10,0))
$(eval $(call build_rule,uarch_rob160,1,20,0))
$(eval $(call build_rule,uarch_rob240,1,30,0))
$(eval $(call build_rule,uarch_iq24,2,3,0))
$(eval $(call build_rule,uarch_iq56,2,7,0))
$(eval $(call build_rule,uarch_iq72,2,9,0))
$(eval $(call build_rule,uarch_iq88,2,11,0))
$(eval $(call build_rule,uarch_dtlb48,3,0,48))
$(eval $(call build_rule,uarch_dtlb80,3,0,80))
$(eval $(call build_rule,uarch_dtlb96,3,0,96))
$(eval $(call build_rule,uarch_l1_24k,4,0,24576))
$(eval $(call build_rule,uarch_l1_40k,4,0,40960))
$(eval $(call build_rule,uarch_l1_48k,4,0,49152))
$(eval $(call build_rule,uarch_l2_768k,4,0,786432))
$(eval $(call build_rule,uarch_l2_1280k,4,0,1310720))
$(eval $(call build_rule,uarch_l2_1536k,4,0,1572864))

clean:
	rm -f $(TARGETS)

.PHONY: all clean
