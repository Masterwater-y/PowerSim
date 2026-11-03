"""Exercise native PCState code bytes through the real precise replay block.

Uses the actual x86 and generic PCState headers. Tiny dependency shims exclude
logging/checkpoint infrastructure; PCState copy, reset, and replay code are real.
"""
from pathlib import Path
import os
import re
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class FrontendByteWitnessTest(unittest.TestCase):
    def test_shared_macro_precise_replay_preserves_exact_witness(self):
        frontend = (ROOT / "gem5-spr/src/cpu/o3/frontend_fetch.cc").read_text()
        match = re.search(
            r"if \(sharesAllocation\(current_macro\) &&\s*"
            r"!memoryGroup && at.microPC\(\) != 0\) \{(.*?)\n        \}",
            frontend,
            re.S,
        )
        self.assertIsNotNone(match)
        replay_body = match.group(1)
        source = r'''
#include "arch/x86/pcstate.hh"
#include <cassert>
using namespace gem5;
void actualReplay(PCStateBase& at) {
REPLAY_BODY
}
int main() {
    X86ISA::PCState pc(0x1000);
    std::array<uint8_t,15> bytes{};
    bytes[0]=0x48; bytes[1]=0x8b; bytes[2]=0x07;
    pc.size(3); pc.upc(1); pc.nupc(2); pc.npc(0x1003);
    pc.functionalBytes(bytes,3,0x8123);
    auto matches=[&](const X86ISA::PCState& p) {
        assert(p.size()==3 && p.functionalByteCount()==3);
        assert(p.functionalBytes()==bytes && p.functionalDecoderContext()==0x8123);
    };
    // Reproduce the former reset: size/NPC alone cannot restore code identity.
    auto old=pc; const auto size=old.size(); old.set(old.instAddr());
    old.size(size); old.npc(old.instAddr()+size);
    assert(old.functionalByteCount()==0);
    actualReplay(pc);
    matches(pc);
    assert(pc.instAddr()==0x1000 && pc.npc()==0x1003);
    assert(pc.microPC()==0 && pc.nupc()==1);
    // The surrounding frontend's squash/predicted-PC paths use these copies.
    std::unique_ptr<PCStateBase> clone;
    set(clone,pc); matches(clone->as<X86ISA::PCState>());
    X86ISA::PCState updated; set(updated,pc); matches(updated);
    pc.advance(); assert(pc.functionalByteCount()==0);
    updated.set(0x2000); assert(updated.functionalByteCount()==0);
}
'''.replace("REPLAY_BODY", replay_body)
        stubs = {
            "base/compiler.hh": "#pragma once\n#define GEM5_LIKELY(v) (v)\n",
            "base/types.hh": "#pragma once\n#include <cstdint>\nnamespace gem5 { using Addr=uint64_t; using MicroPC=uint64_t; }\n",
            "base/trace.hh": "#pragma once\n#include <ostream>\nnamespace gem5 { template<class... Args> void ccprintf(std::ostream&,const char*,Args...) {} }\n",
            "sim/serialize.hh": """#pragma once
namespace gem5 {
class CheckpointIn {}; class CheckpointOut {};
class Serializable { public: virtual ~Serializable()=default;
virtual void serialize(CheckpointOut&) const {};
virtual void unserialize(CheckpointIn&) {}; };
}
#define SERIALIZE_SCALAR(v) ((void)cp)
#define UNSERIALIZE_SCALAR(v) ((void)cp)
""",
        }
        with tempfile.TemporaryDirectory(dir=ROOT / "tmp") as directory:
            work = Path(directory)
            for relative, content in stubs.items():
                target = work / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content)
            (work / "test.cc").write_text(source)
            compiler = "/opt/gcc-11/bin/g++"
            build = subprocess.run(
                [compiler, "-std=c++17", "-Wall", "-Wextra", "-Werror",
                 "-I", str(work), "-I", str(ROOT / "gem5-spr/src"),
                 str(work / "test.cc"), "-o", str(work / "test")],
                capture_output=True, text=True,
            )
            self.assertEqual(build.returncode, 0, build.stderr)
            run = subprocess.run(
                [str(work / "test")], capture_output=True, text=True,
                env=dict(os.environ, LD_LIBRARY_PATH="/opt/gcc-11/lib64"),
            )
            self.assertEqual(run.returncode, 0, run.stderr)


if __name__ == "__main__":
    unittest.main()
