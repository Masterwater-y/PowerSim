"""Small regression for ROM entries sharing a decoded thread-hook PC."""
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ThreadStateHookOriginTest(unittest.TestCase):
    def test_production_hook_predicate_excludes_rom_at_every_hook_pc(self):
        from tests.test_spr_functional_retirement_capture import definition
        source = (ROOT / 'gem5-spr/src/cpu/o3/probe/tao_trace.cc').read_text()
        method = definition(source, 'TaoTrace::isThreadStateHook(')
        # Execute the production predicate body with explicit PC/micro-PC inputs.
        body = method[method.index('{'):]
        code = r'''
#include <cassert>
#include <cstdint>
bool isRomMicroPC(uint64_t pc) { return bool(pc & 0x8000); }
struct TaoTrace {
 uint64_t thread_state_marker_pc_=0x428640;
 uint64_t scheduler_switch_pc_=0xffffffff81600170ULL;
 uint64_t scheduler_wakeup_pc_=0xffffffff810683b3ULL;
 uint64_t scheduler_account_out_pc_=0xffffffff81517a0aULL;
 uint64_t scheduler_account_in_pc_=0xffffffff81517a1bULL;
 bool functional_task_clock_=true;
 bool isThreadStateHook(uint64_t pc,uint64_t micro_pc) const BODY
};
int main() {
 TaoTrace t;
 const uint64_t hooks[]={t.thread_state_marker_pc_,t.scheduler_switch_pc_,
  t.scheduler_wakeup_pc_,t.scheduler_account_out_pc_,t.scheduler_account_in_pc_,
  0xffffffff81517740ULL,0xffffffff81517803ULL,0xffffffff81517851ULL,
  0xffffffff810a021aULL,0xffffffff8109f97bULL};
 for (auto pc:hooks) {
  assert(t.isThreadStateHook(pc,0));
  assert(t.isThreadStateHook(pc,2)); // decoded macro's final UOP
  assert(!t.isThreadStateHook(pc,0x8000)); // external entry starts at same PC
  assert(!t.isThreadStateHook(pc,0x8036)); // final ROM member at same PC
 }
 assert(!t.isThreadStateHook(0x123456,0));
 t.functional_task_clock_=false;
 assert(!t.isThreadStateHook(t.scheduler_account_out_pc_,0));
 assert(t.isThreadStateHook(t.thread_state_marker_pc_,0));
}
'''.replace('BODY', body)
        with tempfile.TemporaryDirectory(dir=ROOT / 'tmp') as directory:
            path = Path(directory)
            (path / 'test.cc').write_text(code)
            subprocess.run(['/opt/gcc-11/bin/g++', '-std=c++17', str(path / 'test.cc'),
                            '-o', str(path / 'test')], check=True, capture_output=True)
            result = subprocess.run([str(path / 'test')], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr.decode())


if __name__ == '__main__':
    unittest.main()
