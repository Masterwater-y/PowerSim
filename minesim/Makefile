CXX ?= g++

ROOT_DIR := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
BUILD_DIR ?= $(ROOT_DIR)/build
OBJ_DIR := $(BUILD_DIR)/obj

DYNAMORIO_ROOT ?= $(abspath $(ROOT_DIR)/../dynamorio)
DYNAMORIO_LIB_DIR ?= $(firstword $(wildcard $(DYNAMORIO_ROOT)/lib64/release $(DYNAMORIO_ROOT)/lib64/debug $(DYNAMORIO_ROOT)/lib64))

CPPFLAGS += -I$(ROOT_DIR)/include -I$(DYNAMORIO_ROOT)/include -DLINUX -DX86_64
CXXFLAGS ?= -O2 -std=c++17 -Wall -Wextra
LDFLAGS += -L$(DYNAMORIO_LIB_DIR) -Wl,-rpath,$(DYNAMORIO_LIB_DIR)
LDLIBS += -ldynamorio -ldrdecode -ldl -lpthread -lz

MINESIM_SRCS := \
	$(ROOT_DIR)/src/main.cpp \
	$(ROOT_DIR)/src/core/BranchPredictor.cpp \
	$(ROOT_DIR)/src/core/Cache.cpp \
	$(ROOT_DIR)/src/core/Config.cpp \
	$(ROOT_DIR)/src/core/InstDecoder.cpp \
	$(ROOT_DIR)/src/core/IntervalCore.cpp \
	$(ROOT_DIR)/src/core/MemoryHierarchy.cpp \
	$(ROOT_DIR)/src/core/PageTable.cpp \
	$(ROOT_DIR)/src/core/TLB.cpp \
	$(ROOT_DIR)/src/trace/TraceReader.cpp

CHECK_TRACE_SRCS := $(ROOT_DIR)/tools/check_trace.cpp

MINESIM_OBJS := $(patsubst $(ROOT_DIR)/%.cpp,$(OBJ_DIR)/%.o,$(MINESIM_SRCS))
CHECK_TRACE_OBJS := $(patsubst $(ROOT_DIR)/%.cpp,$(OBJ_DIR)/%.o,$(CHECK_TRACE_SRCS))

DEPS := $(MINESIM_OBJS:.o=.d) $(CHECK_TRACE_OBJS:.o=.d)

TRACE ?=
LIMIT ?= 50000

.PHONY: all minesim check_trace run trace-check clean print-vars

all: minesim check_trace

minesim: $(BUILD_DIR)/minesim

check_trace: $(BUILD_DIR)/check_trace

$(BUILD_DIR)/minesim: $(MINESIM_OBJS)
	@mkdir -p $(dir $@)
	$(CXX) $(CXXFLAGS) $^ $(LDFLAGS) $(LDLIBS) -o $@

$(BUILD_DIR)/check_trace: $(CHECK_TRACE_OBJS)
	@mkdir -p $(dir $@)
	$(CXX) $(CXXFLAGS) $^ -lz -o $@

$(OBJ_DIR)/%.o: $(ROOT_DIR)/%.cpp
	@mkdir -p $(dir $@)
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) -MMD -MP -c $< -o $@

run: $(BUILD_DIR)/minesim
	@test -n "$(TRACE)" || (echo "TRACE is required, e.g. make run TRACE=/path/to/file.trace.gz [LIMIT=50000]" >&2; exit 1)
	cd $(ROOT_DIR) && ./build/minesim "$(TRACE)" "$(LIMIT)"

trace-check: $(BUILD_DIR)/check_trace
	@test -n "$(TRACE)" || (echo "TRACE is required, e.g. make trace-check TRACE=/path/to/file.trace.gz [LIMIT=500000]" >&2; exit 1)
	cd $(ROOT_DIR) && ./build/check_trace "$(TRACE)" "$(LIMIT)"

print-vars:
	@printf 'ROOT_DIR=%s\n' '$(ROOT_DIR)'
	@printf 'BUILD_DIR=%s\n' '$(BUILD_DIR)'
	@printf 'DYNAMORIO_ROOT=%s\n' '$(DYNAMORIO_ROOT)'
	@printf 'DYNAMORIO_LIB_DIR=%s\n' '$(DYNAMORIO_LIB_DIR)'

clean:
	rm -rf $(BUILD_DIR)

-include $(DEPS)
