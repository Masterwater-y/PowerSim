"""Native functional macro starts distinguish debug controls from missing UOPs."""
from pathlib import Path
import os
import re
import subprocess
import tempfile
import unittest
ROOT = Path(__file__).resolve().parents[1]


class FrontendMacroStartsTest(unittest.TestCase):
    def test_proven_debug_prefix_is_start_but_missing_functional_leader_is_not(self):
        text = (ROOT / 'gem5-spr/src/cpu/o3/probe/tao_trace.cc').read_text()
        method = text[text.index('TaoTrace::observeFstFrontend('):]
        first = method[method.index('    bool first='):method.index('    bool hardware_entry=')]
        observed = re.search(r'        const bool observed_start=.*?;', method, re.S).group(0)
        source = r'''
#include "gem5-spr/src/cpu/o3/probe/perf_retirement.hh"
#include "include/fastsim/retired_frontend_facts.hpp"
#include <cassert>
#include <memory>
#include <typeinfo>
#include <vector>
struct Static {
    bool first=false;
    std::string name="ld";
    virtual ~Static()=default;
    bool isMicroop() const {return true;}
    bool isFirstMicroop() const {return first;}
    std::string getName() const {return name;}
};
namespace X86ISA { struct MicroDebug:Static {}; }
struct ConditionalDebug:X86ISA::MicroDebug {};
struct Macro {
    std::vector<std::shared_ptr<Static>> prefix;
    std::shared_ptr<Static> fetchMicroop(uint64_t i) const {return prefix.at(i);}
};
struct PC { uint64_t index; uint64_t microPC() const {return index;} };
struct Inst {
    std::shared_ptr<Static> staticInst;
    std::shared_ptr<Macro> macroop;
    PC pc;
    const PC& pcState() const {return pc;}
};
namespace retirement=gem5::o3::retirement;
bool actualStart(const Inst& value) {
    const auto inst=&value;
    const auto si=inst->staticInst;
    const bool pseudo=false;
    [[maybe_unused]] const bool hardware_entry=false;
FIRST_BLOCK
OBSERVED_START
    fastsim::fst::RetiredFrontendMacro descriptor;
    descriptor.known_flags=fastsim::fst::kFrontendRequiredKnown;
    if(observed_start) descriptor.flags|=fastsim::fst::kFrontendStartsMacro;
    return descriptor.flags&fastsim::fst::kFrontendStartsMacro;
}
int main() {
    auto op=std::make_shared<Static>();
    auto debug=std::make_shared<X86ISA::MicroDebug>();
    debug->first=true; debug->name="warn";
    Inst valid{op,std::make_shared<Macro>(),PC{1}};
    valid.macroop->prefix={debug};
    assert(actualStart(valid));
    auto ordinary=std::make_shared<Static>(); ordinary->first=true;
    ordinary->name="warn"; // Mnemonic alone does not prove omitted debug work.
    valid.macroop->prefix={ordinary}; assert(!actualStart(valid));
    auto conditional=std::make_shared<ConditionalDebug>();
    conditional->first=true; conditional->name="warn";
    valid.macroop->prefix={conditional}; assert(!actualStart(valid));
    valid.macroop->prefix={debug,ordinary}; valid.pc.index=2;
    assert(!actualStart(valid)); // A real omitted functional leader remains false.
    valid.macroop->prefix={debug,debug}; assert(actualStart(valid));
    debug->first=false; valid.pc.index=1; assert(!actualStart(valid));
    op->first=true; assert(actualStart(valid));
}
'''.replace('FIRST_BLOCK', first).replace('OBSERVED_START', observed)
        with tempfile.TemporaryDirectory(dir=ROOT / 'tmp') as directory:
            work = Path(directory)
            (work / 'test.cc').write_text(source)
            build = subprocess.run(
                ['/opt/gcc-11/bin/g++', '-std=c++17', '-Wall', '-Wextra', '-Werror',
                 '-I', str(ROOT), str(work / 'test.cc'), '-lz', '-o', str(work / 'test')],
                capture_output=True, text=True,
            )
            self.assertEqual(build.returncode, 0, build.stderr)
            run = subprocess.run([str(work / 'test')], capture_output=True, text=True,
                                 env=dict(os.environ, LD_LIBRARY_PATH='/opt/gcc-11/lib64'))
            self.assertEqual(run.returncode, 0, run.stderr)


if __name__ == '__main__':
    unittest.main()
