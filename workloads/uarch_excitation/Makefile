CC ?= gcc
CFLAGS ?= -O2 -g -Wall -Wextra -std=gnu11 -pthread
LDFLAGS ?= -static -pthread
PROJECT_ROOT := $(abspath $(CURDIR)/../..)
ROI_DIR := $(PROJECT_ROOT)/workloads/common/roi
DYNAMORIO_MARKER_LDSCRIPT := $(ROI_DIR)/dynamoRIO_marker.ld
GEM5_BIN_DIR ?= bin/gem5
DYNAMORIO_BIN_DIR ?= bin/dynamoRIO
SRC := uarch_excitation.c

NAMES := \
	uarch_rob80 \
	uarch_rob160 \
	uarch_rob240 \
	uarch_iq24 \
	uarch_iq56 \
	uarch_iq72 \
	uarch_iq88 \
	uarch_dtlb48 \
	uarch_dtlb80 \
	uarch_dtlb96 \
	uarch_l1_24k \
	uarch_l1_40k \
	uarch_l1_48k \
	uarch_l2_768k \
	uarch_l2_1280k \
	uarch_l2_1536k
GEM5_TARGETS := $(addprefix $(GEM5_BIN_DIR)/,$(NAMES))
DYNAMORIO_TARGETS := $(addprefix $(DYNAMORIO_BIN_DIR)/,$(NAMES))

all: $(GEM5_TARGETS)
dynamoRIO: $(DYNAMORIO_TARGETS)

$(GEM5_BIN_DIR) $(DYNAMORIO_BIN_DIR):
	mkdir -p $@

define build_rule
$(GEM5_BIN_DIR)/$(1): $(SRC) $(ROI_DIR)/fastsim_roi.h | $(GEM5_BIN_DIR)
	$$(CC) $$(CFLAGS) -I$(ROI_DIR) -DUARCH_KIND=$(2) \
		-DUARCH_NAME=\"$(1)\" -DUARCH_DEPTH=$(3) \
		-DUARCH_FOOTPRINT=$(4) -o $$@ $$< $$(LDFLAGS)
$(DYNAMORIO_BIN_DIR)/$(1): $(SRC) $(ROI_DIR)/fastsim_roi.h $(ROI_DIR)/dynamoRIO_markers.c $(DYNAMORIO_MARKER_LDSCRIPT) | $(DYNAMORIO_BIN_DIR)
	$$(CC) $$(CFLAGS) -DFASTSIM_DR_TRACE -I$(ROI_DIR) \
		-DUARCH_KIND=$(2) -DUARCH_NAME=\"$(1)\" \
		-DUARCH_DEPTH=$(3) -DUARCH_FOOTPRINT=$(4) -o $$@ \
		$$< $(ROI_DIR)/dynamoRIO_markers.c $$(LDFLAGS) \
		-Wl,-T,$(DYNAMORIO_MARKER_LDSCRIPT)
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
	rm -f $(GEM5_TARGETS) $(DYNAMORIO_TARGETS)

.PHONY: all dynamoRIO clean
