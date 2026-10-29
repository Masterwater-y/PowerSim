"""Exercise native window tails, simulator-only prefixes and complete sparse facts."""
from pathlib import Path
import json
import os
import subprocess
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
HEADER=ROOT/'gem5-fs/src/cpu/o3/probe/perf_retirement.hh'
DRIVER=r'''
#include "cpu/o3/probe/perf_retirement.hh"
#include <cassert>
using namespace gem5::o3::retirement;
Member m(unsigned seq,bool first,bool last,bool measured) {
 Member r;r.sequence=seq;r.owner=10;r.first=first;r.last=last;r.measured=measured;return r;
}
int main(int argc,char**argv) {
 assert(argc==2);
 Ledger warm;warm.commit(m(1,true,false,false));
 auto w=warm.snapshot({10}).physical;
 assert(w.incomplete_macros==0 && w.incomplete_uops==0);
 assert(w.all_history_incomplete_macros==1 && w.all_history_incomplete_uops==1);
 auto rom=m(2,false,false,true);rom.rom=true;rom.rom_entry=true;
 warm.commit(rom);warm.commit(m(3,false,true,true));
 w=warm.snapshot({10}).physical;
 assert(w.incomplete_macros==0 && w.all_history_incomplete_macros==1);
 Ledger cross;cross.commit(m(4,true,false,false));cross.commit(m(5,false,false,true));
 w=cross.snapshot({10}).physical;
 assert(w.incomplete_uops==1 && w.all_history_incomplete_uops==2);
 auto prefix=[](uint64_t i){return DebugPrefixMember{true,i==0,true};};
 assert(debugControlName("warn_once"));assert(debugControlName("inform_once"));
 assert(!debugControlName("fatal"));assert(!debugControlName("movntdq"));
 assert(debugOnlyPrefix(1,prefix));assert(!debugOnlyPrefix(0,prefix));
 assert(!debugOnlyPrefix(2,[](uint64_t i){return DebugPrefixMember{true,i==0,i==0};}));
 assert(!debugOnlyPrefix(1,[](uint64_t){return DebugPrefixMember{true,false,true};}));
 BoundaryStream stream;stream.open(argv[1],0);
 stream.record(3,0,0x100,true,false,true,false);
 stream.record(6,0,0x200,true,true,false,true);
 stream.finish("core0.fst",8,2,true);
}
'''

class NativeMacroBoundaryTest(unittest.TestCase):
 def test_measured_tails_debug_prefix_and_unbounded_fact_stream(self):
  text=HEADER.read_text()
  for api in ('all_history_incomplete_macros','debugOnlyPrefix','class BoundaryStream','debugControlName'):
   self.assertIn(api,text)
  with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as directory:
   p=Path(directory);(p/'test.cc').write_text(DRIVER)
   built=subprocess.run(['/opt/gcc-11/bin/g++','-std=c++17','-Wall','-Wextra','-Werror','-I',str(ROOT/'gem5-fs/src'),str(p/'test.cc'),'-o',str(p/'test')],capture_output=True,text=True)
   self.assertEqual(built.returncode,0,built.stderr)
   run=subprocess.run([str(p/'test'),str(p)],env=dict(os.environ,LD_LIBRARY_PATH='/opt/gcc-11/lib64'),capture_output=True,text=True)
   self.assertEqual(run.returncode,0,run.stderr)
   rows=[json.loads(s) for s in (p/'macro-boundary-core0.jsonl').read_text().splitlines()]
   self.assertEqual([r['record_ordinal'] for r in rows],[3,6])
   self.assertTrue(rows[0]['debug_prefix']);self.assertTrue(rows[1]['had_open_macro'])
   meta=json.loads((p/'macro-boundary-metadata-core0.json').read_text())
   self.assertTrue(meta['complete']);self.assertEqual(meta['event_count'],2)
   self.assertEqual(meta['source_record_count'],8);self.assertEqual(meta['dropped_events'],0)


 def test_production_binding_rejects_conditional_debug_subclasses(self):
  source=(ROOT/'gem5-fs/src/cpu/o3/probe/tao_trace.cc').read_text()
  expression=source.split('const bool debug=',1)[1].split(';',1)[0]
  driver=r'''#include "cpu/o3/probe/perf_retirement.hh"
#include <typeinfo>
#include <cassert>
struct StaticInst {virtual ~StaticInst()=default;};
namespace X86ISA {struct MicroDebug:StaticInst {};struct MicroDebugFlags:MicroDebug {};}
struct Semantic:StaticInst {};
using namespace gem5::o3;
bool accepted(std::shared_ptr<StaticInst> prefix,const std::string &name) {
 const bool debug=EXPRESSION;return debug;
}
int main() {
 assert(accepted(std::make_shared<X86ISA::MicroDebug>(),"warn_once"));
 assert(!accepted(std::make_shared<X86ISA::MicroDebugFlags>(),"warn_once"));
 assert(!accepted(std::make_shared<Semantic>(),"warn_once"));
 assert(!accepted(std::make_shared<X86ISA::MicroDebug>(),"panic"));
}
'''.replace('EXPRESSION',expression)
  with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as directory:
   p=Path(directory);(p/'test.cc').write_text(driver)
   built=subprocess.run(['/opt/gcc-11/bin/g++','-std=c++17','-I',str(ROOT/'gem5-fs/src'),str(p/'test.cc'),'-o',str(p/'test')],capture_output=True,text=True)
   self.assertEqual(built.returncode,0,built.stderr)
   run=subprocess.run([str(p/'test')],env=dict(os.environ,LD_LIBRARY_PATH='/opt/gcc-11/lib64'),capture_output=True,text=True)
   self.assertEqual(run.returncode,0,run.stderr)

if __name__=='__main__':unittest.main()
