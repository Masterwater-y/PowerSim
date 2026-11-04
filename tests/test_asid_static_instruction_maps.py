"""ASID maps bind geometry to the current retired address space, fail closed."""
from pathlib import Path
import os
import struct
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from audit_fst_static_instruction_maps import audit_map

HEADER = struct.Struct('<8sIIIIQQII')
ROW = struct.Struct('<QQQQHBB4xQQQQ')


def fixture(directory, *, rows=None, version=4, declared=True):
    path = directory / 'core0.fst'
    path.write_bytes(struct.pack('<8sIIIIQQQQQQ', b'FSTRC01\0', 7, 72, 64, 0, 3,
                                (1 << 12) if declared else 0, 0, 0, 0, 0)
                     + b''.join(struct.pack('<Q', 4096) + bytes(56) for _ in range(3)))
    Path(str(path) + '.asmap').write_bytes(HEADER.pack(b'FSTASM1\0', 1, 48, 16, 0, 3, 3, 0, 0)
                                         + b''.join(struct.pack('<QQ', i, asid) for i, asid in enumerate([4096,8192,4096])))
    rows = rows or [(4096,4096,4098,12288,33,2,1,1,0,2,0),
                    (8192,4096,4101,16384,33,5,1,4,0,8,0)]
    encoded = b''.join(ROW.pack(*r) for r in rows)
    if version in (2, 3):
        encoded = encoded[8:72]
    if version == 1:
        encoded = struct.pack('<QQQHB5x',4096,4098,12288,33,2)
    Path(str(path) + '.imap').write_bytes(HEADER.pack(f'FSTIMP{version}\0'.encode(), version, 48,
                                                   72 if version == 4 else 32 if version == 1 else 64, 0, 3,
                                                   len(rows) if version == 4 else 1, 1 if version == 1 else 3, 0 if version == 1 else 1) + encoded)
    return path


class AsidStaticMapTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build = tempfile.TemporaryDirectory(prefix='asid-map-build-', dir=ROOT / 'tmp')
        cls.binary = Path(cls.build.name) / 'reader'
        source = Path(cls.build.name) / 'reader.cpp'
        source.write_text(r'''
#include "fastsim/trace.hpp"
#include <cassert>
#include <iostream>
int main(int argc,char**argv){try{
 if(argc>3 && std::string(argv[2])=="upgrade"){
  fastsim::upgrade_binary_trace_to_v7(argv[1],argv[3],fastsim::SyscallAbi::kUnknown);return 0;
 }
 if(argc>2 && std::string(argv[2])=="write") {
  fastsim::BinaryTraceWriter w(argv[1],0);w.enable_asid_static_instruction_map();
  w.set_static_instruction_isa(fastsim::StaticInstructionIsa::kX86_64);
  fastsim::StaticInstructionInfo a;a.pc=4096;a.size=2;a.fallthrough_pc=4098;
  a.flags=33;a.direct_target=12288;a.operand_semantics_valid=true;
  a.read_register_mask[0]=1;a.write_register_mask[0]=2;
  auto b=a;b.size=5;b.fallthrough_pc=4101;b.direct_target=16384;
  b.read_register_mask[0]=4;b.write_register_mask[0]=8;
  w.register_static_instruction(4096,a);w.register_static_instruction(8192,b);
  bool conflict=false;try {w.register_static_instruction(4096,b);}catch(const std::invalid_argument&){conflict=true;}
  assert(conflict);w.set_static_instruction_map_complete();
  fastsim::TraceRecord r;r.pc=4096;
  for(auto asid:{4096,8192,4096}){w.set_address_space_id(asid);w.append(r);}
  w.close();return 0;
 }
 fastsim::BinaryTraceSource source(argv[1]);fastsim::TraceRecord r;
 if(argc>2 && std::string(argv[2])=="validate")return 0;
 if(argc>2 && std::string(argv[2])=="capabilities"){
 std::cout<<source.static_instruction_map_complete()<<","<<source.static_instruction_operands_complete()
          <<","<<static_cast<int>(source.static_instruction_isa());return 0;}
 if(argc>2 && std::string(argv[2])=="single"){
 assert(source.next(r));assert(source.static_instruction(4096)->size==2);return 0;}
 if(argc>2){ assert(!source.static_instruction(4096));return 0; }
 assert(!source.static_instruction(4096));
 assert(!source.all_static_instructions());
 for(int i=0;i<3;++i){assert(source.next(r));auto p=source.static_instruction(4096);
 assert(p);assert(source.all_static_instructions()->size()==1);assert(p->size==(i==1?5:2));assert(p->direct_target==(i==1?16384:12288));
 assert(p->read_register_mask[0]==(i==1?4:1));assert(p->write_register_mask[0]==(i==1?8:2));
 assert(source.static_instruction_map_complete());assert(source.static_instruction_operands_complete());}
 return 0;
 }catch(const std::exception&e){std::cerr<<e.what()<<"\n";return 2;}}
''')
        command = ['g++', '-std=c++17', '-static-libstdc++', '-O0', '-I'+str(ROOT/'include'), str(source),
                   str(ROOT/'src/trace.cpp'), str(ROOT/'src/fst_dynamic_translations.cpp'), str(ROOT/'src/retired_frontend_facts.cpp'), '-lz', '-o', str(cls.binary)]
        subprocess.run(command, check=True)

    @classmethod
    def tearDownClass(cls): cls.build.cleanup()

    def native(self, path, *args):
        return subprocess.run([str(self.binary), str(path), *args], capture_output=True, text=True)

    def test_upgrade_preserves_asid_definitions_and_declared_coverage(self):
        for flags in (0,1,2,3):
            with self.subTest(flags=flags), tempfile.TemporaryDirectory(dir=ROOT/'tmp') as directory:
                path=fixture(Path(directory))
                imap=Path(str(path)+'.imap')
                encoded=bytearray(imap.read_bytes());struct.pack_into('<I',encoded,40,flags);imap.write_bytes(encoded)
                upgraded=Path(directory)/'upgraded.fst'
                result=self.native(path,'upgrade',str(upgraded))
                self.assertEqual(result.returncode,0,result.stderr)
                self.assertTrue(Path(str(upgraded)+'.imap').is_file(), 'mandatory ASID map was lost')
                self.assertEqual(Path(str(upgraded)+'.imap').read_bytes(),imap.read_bytes())
                self.assertEqual(Path(str(upgraded)+'.asmap').read_bytes(),Path(str(path)+'.asmap').read_bytes())
                hot=upgraded.read_bytes()
                self.assertTrue(struct.unpack_from('<Q',hot,32)[0] & (1<<12))
                self.assertEqual(self.native(upgraded,'validate').returncode,0)
                if flags==3:
                    self.assertEqual(self.native(upgraded).returncode,0)

    def test_upgrade_preserves_unexecuted_pc_definitions(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as directory:
            path=fixture(Path(directory),rows=[
                (4096,4096,4098,12288,33,2,1,1,0,2,0),
                (4096,4100,4101,0,0,1,1,8,0,16,0),
                (8192,4096,4101,16384,33,5,1,4,0,8,0)])
            upgraded=Path(directory)/'upgraded.fst'
            result=self.native(path,'upgrade',str(upgraded))
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertTrue(Path(str(upgraded)+'.imap').is_file(), 'mandatory ASID map was lost')
            self.assertEqual(Path(str(upgraded)+'.imap').read_bytes(),Path(str(path)+'.imap').read_bytes())

    def test_writer_roundtrip_and_conflicting_definition(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as directory:
            path=Path(directory)/'written.fst'
            result=self.native(path,'write')
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(self.native(path).returncode,0)
            self.assertEqual(audit_map(path)['instruction_rows'],2)

    def test_same_pc_lookup_returns_to_first_asid(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as directory:
            path = fixture(Path(directory))
            result = self.native(path)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(audit_map(path)['instruction_rows'], 2)

    def test_invalid_identity_and_rows_fail_closed(self):
        mutations = ['missing-map','missing-asmap','zero-asid','wrong-asid','wrong-core','wrong-count',
                     'duplicate-key','conflicting-key','truncated','undeclared','old-fst-version','reserved','unmarked-operands']
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory(dir=ROOT/'tmp') as directory:
                path = fixture(Path(directory), declared=mutation!='undeclared')
                imap = Path(str(path)+'.imap')
                data = bytearray(imap.read_bytes())
                if mutation == 'missing-map': imap.unlink()
                elif mutation == 'missing-asmap': Path(str(path)+'.asmap').unlink()
                else:
                    if mutation == 'old-fst-version':
                        hot=bytearray(path.read_bytes());struct.pack_into('<I',hot,8,6);path.write_bytes(hot)
                    if mutation == 'reserved': data[84]=1
                    if mutation == 'unmarked-operands': data[83]=0
                    if mutation == 'zero-asid': struct.pack_into('<Q',data,48,0)
                    if mutation == 'wrong-asid': struct.pack_into('<Q',data,48,2048)
                    if mutation == 'wrong-core': struct.pack_into('<I',data,20,1)
                    if mutation == 'wrong-count': struct.pack_into('<Q',data,24,4)
                    if mutation in ('duplicate-key','conflicting-key'):
                        data[120:136] = data[48:64]
                    if mutation == 'truncated': data = data[:-1]
                    imap.write_bytes(data)
                self.assertNotEqual(self.native(path,'validate').returncode, 0)
                with self.assertRaises(ValueError): audit_map(path)

    def test_legacy_versions_still_decode_single_asid(self):
        for version in (1,2,3):
            with self.subTest(version=version), tempfile.TemporaryDirectory(dir=ROOT/'tmp') as directory:
                path=fixture(Path(directory),version=version,declared=False)
                Path(str(path)+'.asmap').unlink()
                result=self.native(path,'single')
                self.assertEqual(result.returncode,0,result.stderr)
                self.assertEqual(audit_map(path)['version'],version)

    def test_legacy_pc_maps_report_unsafe_capabilities_for_multi_asid(self):
        for version in (1,2,3):
            with self.subTest(version=version), tempfile.TemporaryDirectory(dir=ROOT/'tmp') as directory:
                path=fixture(Path(directory),version=version,declared=False)
                result=self.native(path,'capabilities')
                self.assertEqual(result.returncode,0,result.stderr)
                self.assertEqual(result.stdout,'0,0,0')
                report=audit_map(path)
                self.assertFalse(report['map_complete'])
                self.assertFalse(report['operands_complete'])
                self.assertFalse(report['scope_safe'])
                self.assertIsNone(report['isa'])
                self.assertTrue(report['declared_map_complete'])
                self.assertEqual(report['declared_operands_complete'],version!=1)
                strict=subprocess.run([sys.executable,str(ROOT/'tools/audit_fst_static_instruction_maps.py'),
                    str(path),'--require-map-complete','--require-operands'],capture_output=True,text=True)
                self.assertEqual(strict.returncode,2,strict.stdout)

    def test_legacy_map_validates_existing_address_space_companion(self):
        for version in (1,2,3):
            for mutation in ('core','count','zero-asid','truncated'):
                with self.subTest(version=version,mutation=mutation), tempfile.TemporaryDirectory(dir=ROOT/'tmp') as directory:
                    path=fixture(Path(directory),version=version,declared=False)
                    asmap=Path(str(path)+'.asmap');encoded=bytearray(asmap.read_bytes())
                    if mutation=='core':struct.pack_into('<I',encoded,20,1)
                    if mutation=='count':struct.pack_into('<Q',encoded,24,4)
                    if mutation=='zero-asid':struct.pack_into('<Q',encoded,56,0)
                    if mutation=='truncated':encoded=encoded[:-1]
                    asmap.write_bytes(encoded)
                    self.assertNotEqual(self.native(path,'validate').returncode,0)
                    with self.assertRaises(ValueError):audit_map(path)

if __name__=='__main__': unittest.main()
