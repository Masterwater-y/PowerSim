"""Sparse source boundaries correct grouping without importing cache outcomes."""
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'tools'))
from test_perf_retirement_validation import retirement_fixture
from audit_perf_retirement import scan_functional_retirement


def facts(trace, ordinal):
 meta=json.loads((trace/'thread-state-metadata-core0.json').read_text())
 event=dict(schema='fastsim-functional-macro-boundary-v1',core_id=0,record_ordinal=ordinal,
            context_id=0,pc=4096+4*ordinal,starts_macro=True,rom_entry=True,debug_prefix=False,had_open_macro=True)
 path=trace/'macro-boundary-core0.jsonl';path.write_text(json.dumps(event)+'\n')
 summary=dict(schema='fastsim-functional-macro-boundary-metadata-v1',core_id=0,source_file=meta['source_file'],
              source_record_count=8,measurement_begin_ordinal=2,event_count=1,dropped_events=0,complete=True,
              incomplete_scope='measured-functional-members-v2',event_file=path.name)
 (trace/'macro-boundary-metadata-core0.json').write_text(json.dumps(summary))
 rows=[json.loads(s) for s in (trace/'thread-state-core0.jsonl').read_text().splitlines()]
 return meta,rows,path,summary


class FunctionalMacroBoundaryScanTest(unittest.TestCase):
 def test_warmup_abandonment_stays_out_of_measured_incomplete(self):
  with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as directory:
   trace=Path(directory);retirement_fixture(trace);meta,rows,_,_=facts(trace,1)
   source=trace/meta['source_file'];data=bytearray(source.read_bytes())
   struct.pack_into('<H',data,72+50,1|1024);source.write_bytes(data)
   got=scan_functional_retirement(trace,meta,rows,[101])['physical']
   self.assertEqual(got.get('all_history_incomplete_macros'),1)
   self.assertEqual(got['incomplete_macros'],0)
   self.assertEqual(got['retired_macro_instructions'],5)

 def test_measured_abandonment_is_counted_and_not_merged(self):
  with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as directory:
   trace=Path(directory);retirement_fixture(trace);meta,rows,_,_=facts(trace,3)
   got=scan_functional_retirement(trace,meta,rows,[101])['physical']
   self.assertEqual(got['incomplete_macros'],1)
   self.assertEqual(got['incomplete_uops'],1)

 def test_incomplete_misbound_or_changed_boundary_facts_fail(self):
  for fault in ('dropped','incomplete','pc','duplicate','wrong_open','count'):
   with self.subTest(fault=fault),tempfile.TemporaryDirectory(dir=ROOT/'tmp') as directory:
    trace=Path(directory);retirement_fixture(trace);meta,rows,path,summary=facts(trace,3)
    if fault=='dropped':summary['dropped_events']=1
    if fault=='incomplete':summary['complete']=False
    if fault=='count':summary['event_count']=2
    if fault=='pc':
     e=json.loads(path.read_text());e['pc']+=4;path.write_text(json.dumps(e)+'\n')
    if fault=='duplicate':path.write_text(path.read_text()*2);summary['event_count']=2
    if fault=='wrong_open':
     e=json.loads(path.read_text());e['had_open_macro']=False;path.write_text(json.dumps(e)+'\n')
    (trace/'macro-boundary-metadata-core0.json').write_text(json.dumps(summary))
    with self.assertRaises(ValueError):scan_functional_retirement(trace,meta,rows,[101])

if __name__=='__main__':unittest.main()
