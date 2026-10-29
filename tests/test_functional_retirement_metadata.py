"""Independent functional-only eligibility wire fixture and real simulator replay."""
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tools'))
from audit_perf_retirement import scan_functional_retirement
from test_software_prefetch_audit import fnv

class FunctionalRetirementMetadataTest(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  cls.temp=tempfile.TemporaryDirectory(dir=ROOT/'tmp');cls.addClassCleanup(cls.temp.cleanup)
  p=Path(cls.temp.name);cpp=p/'driver.cc';cls.exe=p/'driver'
  cpp.write_text(r'''
#include "fastsim/trace.hpp"
#include "fastsim/simulator.hpp"
#include <iostream>
int main(int argc,char**argv){try {
 if(argc!=2)return 2;
 fastsim::BinaryTraceSource inspect(argv[1]);fastsim::TraceRecord record;
 std::uint64_t macros=0;
 while(inspect.next(record)) {
     std::cout<<inspect.current_hardware_entry();
     if(!fastsim::has_flag(record.flags,fastsim::kMicroOp) || fastsim::has_flag(record.flags,fastsim::kLastMicroOp))++macros;
 }
 fastsim::upgrade_binary_trace_to_v7(argv[1],std::string(argv[1])+".upgrade");
 fastsim::BinaryTraceSource upgraded(std::string(argv[1])+".upgrade");
 if(upgraded.functional_retirement_complete()!=inspect.functional_retirement_complete())return 3;
 std::vector<std::unique_ptr<fastsim::TraceSource>> traces;
 traces.push_back(std::make_unique<fastsim::WarmupInstructionTraceSource>(
     std::make_unique<fastsim::BinaryTraceSource>(argv[1]),0,macros,0,inspect.record_count(),true));
 fastsim::SimulatorConfig c;c.cores=c.cha_count=1;c.core_model="interval_weave";
 c.interval_scheduler="time_epoch";c.chunk_instructions=c.interval_target_uops=1;
 c.response_queue_feedback=c.response_sparse_scoreboard=true;c.native_kernel_trace=true;c.syscall_restart_latency=0;c.measurement_scope=fastsim::MeasurementScope::kUserPlusKernel;
 fastsim::Simulator sim(c,std::move(traces));auto result=sim.run();
 const auto p=result.total_core().perf_retirement.physical;
 std::cout<<" "<<p.retired_macro_instructions<<" "<<p.perf_retired_loads<<" "<<p.retired_uops<<" "<<p.perf_retired_stores;
 }catch(const std::exception&e){std::cerr<<e.what();return 1;}}
''')
  built=subprocess.run(['/opt/gcc-11/bin/g++','-std=c++17','-O1','-I',str(ROOT/'include'),str(cpp),str(ROOT/'build/libfastsim_lib.a'),'-pthread','-Wl,-rpath,/opt/gcc-11/lib64','-o',str(cls.exe)],capture_output=True,text=True)
  if built.returncode:raise AssertionError(built.stderr)
 def fixture(self,p):
  records=[]
  for i in range(3):
   flags=1|1024|(2048 if i else 0)|(2|4096 if i!=1 else 0)
   if i==2:flags=1|2|4096
   records.append(struct.pack('<QQQQ4IHHhBB4BI',4096,32768,0,0,*([0]*4),8 if i!=1 else 0,flags,-2,0,0,*([0]*4),0))
  data=b''.join(records);fst=p/'core0.fst'
  fst.write_bytes(struct.pack('<8sIIIIQQ4Q',b'FSTRC01\0',7,72,64,0,3,16|128,0,0,0,0)+data)
  header=struct.pack('<8sIIIIQQQII',b'FSTRETV1',1,56,0,1,3,2,fnv(data,64),16,0)
  side=Path(str(fst)+'.fsem');side.write_bytes(header+b''.join(struct.pack('<QII',i,fnv(records[i],32),1) for i in (0,1)))
  return fst,side
 def test_hardware_entry_cost_preserved_memory_events_ineligible(self):
  with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as directory:
   fst,_=self.fixture(Path(directory));r=subprocess.run([str(self.exe),str(fst)],capture_output=True,text=True)
   self.assertEqual(r.returncode,0,r.stderr);self.assertEqual(r.stdout,'110 2 1 3 0')
 def test_independent_scan_uses_same_functional_event_eligibility(self):
  with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as directory:
   p=Path(directory);fst,_=self.fixture(p)
   meta=dict(core_id=0,source_file=fst.name,source_record_count=3,warmup_records=0)
   events=[dict(core_id=0,event_kind='initial_owner',record_ordinal=0,os_tid=101)]
   result=scan_functional_retirement(p,meta,events,{101})['physical']
   self.assertEqual(result['perf_retired_loads'],1)
   self.assertEqual(result['retired_macro_instructions'],2)
 def test_prefetch_population_policy_is_bound_to_functional_feature(self):
  for spr in (False,True):
   with self.subTest(spr=spr),tempfile.TemporaryDirectory(dir=ROOT/'tmp') as directory:
    p=Path(directory);fst=p/'hint.fst'
    hot=struct.pack('<QQQQ4IHHhBB4BI',4096,32768,0,0,*([0]*4),1,1|2|4096,-2,0,0,*([0]*4),0)
    fst.write_bytes(struct.pack('<8sIIIIQQ4Q',b'FSTRC01\0',7,72,64,0,1,16|64|(128 if spr else 0),0,0,0,0)+hot)
    Path(str(fst)+'.mreq').write_bytes(struct.pack('<8sIIIIQQQII',b'FSTMREQ1',2,56,0,1,1,1,fnv(hot,64),24,0)+struct.pack('<QIIB7s',0,fnv(hot,32),0,2,bytes(7)))
    if spr:Path(str(fst)+'.fsem').write_bytes(struct.pack('<8sIIIIQQQII',b'FSTRETV1',1,56,0,1,1,0,fnv(hot,64),16,0))
    r=subprocess.run([str(self.exe),str(fst)],capture_output=True,text=True)
    self.assertEqual(r.returncode,0,r.stderr)
    self.assertEqual(r.stdout,'0 1 '+str(int(spr))+' 1 0')
    meta=dict(core_id=0,source_file=fst.name,source_record_count=1,warmup_records=0)
    events=[dict(core_id=0,event_kind='initial_owner',record_ordinal=0,os_tid=101)]
    scanned=scan_functional_retirement(p,meta,events,{101},load_event_policy='spr-software-prefetch-v1' if spr else 'legacy-demand-only-v1')
    self.assertEqual(scanned['physical']['perf_retired_loads'],int(spr))
 def test_precise_fault_prefix_not_merged_into_hardware_entry(self):
  with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as directory:
   p=Path(directory);fst=p/'core0.fst';records=[]
   for i in range(4):
    flags=(1|4|1024|4096) if i==0 else (1|1024|(2048 if i==2 else 0)) if i<3 else (1|4|4096)
    records.append(struct.pack('<QQQQ4IHHhBB4BI',4096,32768,0,0,*([0]*4),8 if i in (0,3) else 0,flags,59 if i in (0,3) else -3,0,0,*([0]*4),0))
   hot=b''.join(records)
   fst.write_bytes(struct.pack('<8sIIIIQQ4Q',b'FSTRC01\0',7,72,64,0,4,16|128,0,0,0,0)+hot)
   Path(str(fst)+'.fsem').write_bytes(struct.pack('<8sIIIIQQQII',b'FSTRETV1',2,56,0,1,4,2,fnv(hot,64),16,0)+b''.join(struct.pack('<QII',i,fnv(records[i],32),3 if i==1 else 1) for i in (1,2)))
   r=subprocess.run([str(self.exe),str(fst)],capture_output=True,text=True)
   self.assertEqual(r.returncode,0,r.stderr);self.assertEqual(r.stdout,'0110 2 0 4 1')
 def test_missing_wrong_or_unbound_metadata_rejects(self):
  for fault in ['missing','hash','core','flags','duplicate','body']:
   with self.subTest(fault=fault),tempfile.TemporaryDirectory(dir=ROOT/'tmp') as directory:
    fst,side=self.fixture(Path(directory));data=bytearray(side.read_bytes())
    if fault=='missing':side.unlink()
    elif fault=='hash':data[44]^=1;side.write_bytes(data)
    elif fault=='core':data[16]=1;side.write_bytes(data)
    elif fault=='flags':data[68]=2;side.write_bytes(data)
    elif fault=='duplicate':data[72:80]=bytes(8);side.write_bytes(data)
    else:
     hot=bytearray(fst.read_bytes());hot[72]^=1;fst.write_bytes(hot)
    r=subprocess.run([str(self.exe),str(fst)],capture_output=True,text=True)
    self.assertNotEqual(r.returncode,0)
class FunctionalRetirementAuditBindingTest(unittest.TestCase):
 def test_independent_audit_rejects_wrong_whole_stream_binding(self):
  for fault in ('header_hash','ordinary_record'):
   with self.subTest(fault=fault),tempfile.TemporaryDirectory(dir=ROOT/'tmp') as directory:
    p=Path(directory);fst,side=FunctionalRetirementMetadataTest().fixture(p)
    path=side if fault=='header_hash' else fst
    data=bytearray(path.read_bytes());data[40 if fault=='header_hash' else 72+2*64]^=1
    path.write_bytes(data)
    meta=dict(core_id=0,source_file=fst.name,source_record_count=3,warmup_records=0)
    events=[dict(core_id=0,event_kind='initial_owner',record_ordinal=0,os_tid=101)]
    with self.assertRaisesRegex(ValueError,'stream.*identity|stream.*hash'):
     scan_functional_retirement(p,meta,events,{101})
 def test_native_accelerator_checks_large_stream(self):
  from fst_retirement_eligibility import load_retirement_eligibility
  with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as directory:
   p=Path(directory);fst,side=FunctionalRetirementMetadataTest().fixture(p)
   record=fst.read_bytes()[72+2*64:72+3*64];count=65537;hot=record*count
   fst.write_bytes(struct.pack('<8sIIIIQQ4Q',b'FSTRC01\0',7,72,64,0,count,16|128,0,0,0,0)+hot)
   side.write_bytes(struct.pack('<8sIIIIQQQII',b'FSTRETV1',1,56,0,1,count,0,fnv(hot,64),16,0))
   self.assertEqual(load_retirement_eligibility(fst,fst.read_bytes(),0,count)[0],set())
   data=bytearray(side.read_bytes());data[40]^=1;side.write_bytes(data)
   with self.assertRaisesRegex(ValueError,'stream.*identity'):
    load_retirement_eligibility(fst,fst.read_bytes(),0,count)
 def test_precise_fault_origin_requires_matching_open_boundary(self):
  with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as directory:
   p=Path(directory);fst,side=FunctionalRetirementMetadataTest().fixture(p)
   # Existing three-record fixture has an open first macro. Mark its first
   # kernel record as a proven abort but provide no matching exceptional start.
   b=bytearray(side.read_bytes());struct.pack_into('<I',b,8,2);struct.pack_into('<I',b,68,3);side.write_bytes(b)
   meta=dict(core_id=0,source_file=fst.name,source_record_count=3,warmup_records=0)
   events=[dict(core_id=0,event_kind='initial_owner',record_ordinal=0,os_tid=101)]
   with self.assertRaisesRegex(ValueError,'fault.*boundary|fault.*prefix'):
    scan_functional_retirement(p,meta,events,{101})
 def test_precise_fault_scan_discards_incomplete_load_membership(self):
  with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as directory:
   p=Path(directory);fst,side=FunctionalRetirementMetadataTest().fixture(p);data=fst.read_bytes();hot=data[72:]
   side.write_bytes(struct.pack('<8sIIIIQQQII',b'FSTRETV1',2,56,0,1,3,1,fnv(hot,64),16,0)+struct.pack('<QII',1,fnv(hot[64:128],32),3))
   event=dict(schema='fastsim-functional-macro-boundary-v1',core_id=0,record_ordinal=1,context_id=0,pc=4096,starts_macro=True,rom_entry=True,debug_prefix=False,had_open_macro=True)
   (p/'macro-boundary-core0.jsonl').write_text(json.dumps(event)+'\n')
   summary=dict(schema='fastsim-functional-macro-boundary-metadata-v1',core_id=0,source_file=fst.name,source_record_count=3,measurement_begin_ordinal=0,event_count=1,dropped_events=0,complete=True,incomplete_scope='measured-functional-members-v2',event_file='macro-boundary-core0.jsonl')
   (p/'macro-boundary-metadata-core0.json').write_text(json.dumps(summary))
   meta=dict(core_id=0,source_file=fst.name,source_record_count=3,warmup_records=0)
   events=[dict(core_id=0,event_kind='initial_owner',record_ordinal=0,os_tid=101)]
   result=scan_functional_retirement(p,meta,events,{101})['physical']
   self.assertEqual(result['perf_retired_loads'],1)
   self.assertEqual(result['incomplete_macros'],1)
   self.assertEqual(result['precise_fault_abandoned_macros'],1)
if __name__=='__main__':unittest.main()
