#include <iostream>
#include "drmemtrace/analyzer.h"
#include "drmemtrace/analysis_tool.h"
#include "drmemtrace/memref.h"

using namespace dynamorio::drmemtrace;

class MyTool : public analysis_tool_t {
public:
    MyTool() {}
    virtual bool process_memref(const memref_t &memref) override {
        std::cout << "Memref type: " << memref.instr.type << " addr: " << memref.instr.addr << "\n";
        return true;
    }
    virtual bool print_results() override {
        std::cout << "Done\n";
        return true;
    }
};

int main() {
    MyTool tool;
    analysis_tool_t *tools[1] = { &tool };
    analyzer_t analyzer("/home/yinhaolang/simulators/minesim/trace_data/657_xzs.trace/657_xzs.xz_s_base.mytest-m64.589905.0399.trace.gz", tools, 1);
    if (!analyzer) {
        std::cerr << "Failed to init analyzer: " << analyzer.get_error_string() << "\n";
        return 1;
    }
    if (!analyzer.run()) {
        std::cerr << "Failed to run analyzer: " << analyzer.get_error_string() << "\n";
        return 1;
    }
    return 0;
}
