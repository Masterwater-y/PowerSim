import importlib.util
import json
import shutil
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class PerfCacheSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = ROOT / 'tools/perf_cache_source.py'
        assert path.exists(), 'functional content identity implementation is missing'
        spec = importlib.util.spec_from_file_location('perf_cache_source', path)
        cls.api = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.api)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / 'tmp')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fst = self.root / 'a.fst'
        self.fst.write_bytes(struct.pack('<8sIIIIQQ4Q', b'FSTRC01\0', 7, 72, 64, 0, 4, 0, 0, 0, 0, 0) + bytes(256))
        self.manifest = self.root / 'manifest.txt'
        self.manifest.write_text('0 binary-warmup-slice a.fst 0 1 2 1 2\n')

    def collect(self):
        return self.api.collect_source_identity(self.manifest)

    def test_same_size_changed_fst_changes_identity(self):
        before = self.collect()
        value = bytearray(self.fst.read_bytes()); value[-1] = 7
        self.fst.write_bytes(value)
        self.assertNotEqual(before['source_identity'], self.collect()['source_identity'])

    def test_path_move_preserves_identity_and_validation(self):
        before = self.collect()
        moved = self.root / 'moved'; moved.mkdir()
        shutil.copyfile(self.fst, moved / 'a.fst')
        shutil.copyfile(self.manifest, moved / 'manifest.txt')
        after = self.api.collect_source_identity(moved / 'manifest.txt')
        self.assertEqual(before['source_identity'], after['source_identity'])
        self.api.validate_identity(before, after)

    def test_companion_and_thread_plan_content_bound(self):
        initial = self.collect()['source_identity']
        sidecar = Path(str(self.fst) + '.imap'); sidecar.write_bytes(b'first')
        self.assertNotEqual(initial, self.collect()['source_identity'])
        before = self.collect()['source_identity']; sidecar.write_bytes(b'other')
        self.assertNotEqual(before, self.collect()['source_identity'])
        plan = self.root / 'thread-state.plan'; plan.write_text('functional plan')
        with_plan = self.api.collect_source_identity(self.manifest, plan)
        self.assertNotEqual(with_plan['source_identity'], self.collect()['source_identity'])

    def test_core_and_range_changes_bound(self):
        before = self.collect()['source_identity']
        self.manifest.write_text('0 binary-warmup-slice a.fst 0 1 1 1 1\n')
        self.assertNotEqual(before, self.collect()['source_identity'])
        self.manifest.write_text('1 binary-warmup-slice a.fst 0 1 2 1 2\n')
        with self.assertRaises(ValueError): self.collect()

    def test_memory_request_companion_is_required_and_content_bound(self):
        value = bytearray(self.fst.read_bytes()); struct.pack_into('<Q', value, 32, 64)
        self.fst.write_bytes(value)
        with self.assertRaises((ValueError, OSError)): self.collect()
        sidecar = Path(str(self.fst) + '.mreq'); sidecar.write_bytes(b'first')
        before = self.collect()['source_identity']; sidecar.write_bytes(b'other')
        self.assertNotEqual(before, self.collect()['source_identity'])

    def test_functional_retirement_companion_required_and_content_bound(self):
        value=bytearray(self.fst.read_bytes());struct.pack_into('<Q',value,32,128)
        self.fst.write_bytes(value)
        with self.assertRaises((ValueError,OSError)):self.collect()
        sidecar=Path(str(self.fst)+'.fsem');sidecar.write_bytes(b'first')
        before=self.collect()['source_identity'];sidecar.write_bytes(b'other')
        self.assertNotEqual(before,self.collect()['source_identity'])

    def test_dynamic_translation_companion_required_and_content_bound(self):
        value=bytearray(self.fst.read_bytes());struct.pack_into('<Q',value,32,512)
        self.fst.write_bytes(value)
        with self.assertRaises((ValueError,OSError)): self.collect()
        sidecar=Path(str(self.fst)+'.xlat.gz');sidecar.write_bytes(b'first')
        before=self.collect()['source_identity'];sidecar.write_bytes(b'other')
        self.assertNotEqual(before,self.collect()['source_identity'])

    def test_required_dependency_missing_fails(self):
        value = bytearray(self.fst.read_bytes()); struct.pack_into('<Q', value, 32, 32)
        self.fst.write_bytes(value)
        with self.assertRaises((ValueError, OSError)): self.collect()

    def test_no_claim_of_roi_or_macro_completeness(self):
        value = self.collect()
        self.assertEqual(value['status'], 'complete')
        self.assertFalse(value['roi_edges']); self.assertFalse(value['macro_boundary_complete'])
        self.assertEqual(value['sources'][0]['score_begin_ordinal'], 1)
        self.assertEqual(value['sources'][0]['score_end_ordinal_exclusive'], 3)

    def test_metadata_tampering_not_hidden_by_digest(self):
        before = self.collect(); after = json.loads(json.dumps(before))
        after['sources'][0]['score_begin_ordinal'] = 0
        with self.assertRaises(ValueError): self.api.validate_identity(before, after)

    def test_declared_functional_companions_required_and_content_bound(self):
        companions = {'ffe.gz': 1 << 11, 'sreq': 1 << 13, 'uops.gz': 1 << 14,
                      'ixlat.gz': 1 << 15, 'core.gz': 1 << 16,
                      'mmuctl.gz': 1 << 17, 'archb.gz': 1 << 18,
                      'imap': 1 << 12}
        for extension, flag in companions.items():
            with self.subTest(extension=extension):
                value=bytearray(self.fst.read_bytes());struct.pack_into('<Q',value,32,flag)
                self.fst.write_bytes(value)
                sidecar=Path(str(self.fst)+'.'+extension)
                with self.assertRaises((ValueError,OSError)):self.collect()
                sidecar.write_bytes(b'first');first=self.collect()
                self.assertEqual(first['identity_policy'],'functional-record-window-v2')
                self.assertIn(extension,[a['role'] for a in first['sources'][0]['artifacts']])
                sidecar.write_bytes(b'other')
                self.assertNotEqual(first['source_identity'],self.collect()['source_identity'])
                sidecar.unlink()

    def test_legacy_scope_is_explicit_and_never_promoted(self):
        old=self.api.collect_source_identity(self.manifest,policy='functional-record-window-v1')
        new=self.collect()
        self.assertEqual(old['identity_policy'],'functional-record-window-v1')
        self.assertNotEqual(old['source_identity'],new['source_identity'])
        with self.assertRaises(ValueError):self.api.validate_identity(old,old)
        self.assertTrue(self.api.validate_identity(old,old,allow_legacy=True))
        with self.assertRaises(ValueError):self.api.validate_identity(old,new,allow_legacy=True)

    def test_each_core_companion_identity_is_bound(self):
        first=bytearray(self.fst.read_bytes());struct.pack_into('<Q',first,32,1<<11);self.fst.write_bytes(first)
        second=bytearray(first);struct.pack_into('<I',second,20,1)
        fst2=self.root/'b.fst';fst2.write_bytes(second)
        for path in (self.fst,fst2):Path(str(path)+'.ffe.gz').write_bytes(b'abc')
        self.manifest.write_text('0 binary a.fst\n1 binary b.fst\n')
        before=self.collect()
        Path(str(fst2)+'.ffe.gz').write_bytes(b'def')
        self.assertNotEqual(before['source_identity'],self.collect()['source_identity'])

class PerfCacheSourceCppTests(PerfCacheSourceTests):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        header = ROOT / 'include/fastsim/perf_cache_source.hpp'
        assert header.exists(), 'C++ functional identity implementation is missing'
        cls.buildtemp = tempfile.TemporaryDirectory(dir=ROOT / 'tmp')
        cls.addClassCleanup(cls.buildtemp.cleanup)
        source = Path(cls.buildtemp.name) / 'probe.cpp'
        cls.probe = source.with_suffix('')
        source.write_text('''#include "fastsim/perf_cache_source.hpp"
#include <iostream>
int main(int argc,char** argv) { try {
if (argc==1) { for (const auto& s: {std::string(),std::string("abc"),std::string(1000000,'a')}) std::cout<<fastsim::PerfCacheSha256::digest(s)<<"\\n"; return 0; }
auto value=fastsim::collect_perf_cache_source_identity(argv[1],argc>2?argv[2]:""); value.write_json(std::cout);
} catch(const std::exception& e) {std::cerr<<e.what();return 2;} }
''')
        subprocess.run(['/opt/gcc-11/bin/g++', '-std=c++17', '-O2', '-I' + str(ROOT / 'include'), str(source), str(ROOT / 'build/libfastsim_lib.a'), '-pthread','-lz', '-Wl,-rpath,/opt/gcc-11/lib64', '-o', str(cls.probe)], check=True, capture_output=True)

    def cpp(self, manifest=None, plan=None):
        args = [str(self.probe), str(manifest or self.manifest)]
        if plan: args.append(str(plan))
        return json.loads(subprocess.check_output(args, text=True))

    def test_sha_known_vectors(self):
        import hashlib
        observed = subprocess.check_output([str(self.probe)], text=True).splitlines()
        self.assertEqual(observed, [hashlib.sha256(s).hexdigest() for s in (b'', b'abc', b'a' * 1000000)])

    def test_cpp_python_identity_equal(self):
        plan = self.root / 'plan'; plan.write_text('plan\n')
        Path(str(self.fst) + '.asmap').write_bytes(b'optional input')
        cpp = self.cpp(plan=plan)
        py = self.api.collect_source_identity(self.manifest, plan)
        self.api.validate_identity(cpp, py)
        self.assertEqual(cpp, py)

    def test_cpp_missing_required_dependency_rejected(self):
        value = bytearray(self.fst.read_bytes()); struct.pack_into('<Q', value, 32, 32)
        self.fst.write_bytes(value)
        process = subprocess.run([str(self.probe), str(self.manifest)], capture_output=True)
        self.assertEqual(process.returncode, 2)

    def test_cpp_dynamic_translation_required_and_identity_equal(self):
        value=bytearray(self.fst.read_bytes());struct.pack_into('<Q',value,32,512)
        self.fst.write_bytes(value)
        failed=subprocess.run([str(self.probe),str(self.manifest)],capture_output=True)
        self.assertEqual(failed.returncode,2)
        sidecar=Path(str(self.fst)+'.xlat.gz');sidecar.write_bytes(b'first')
        self.api.validate_identity(self.cpp(),self.collect())
        before=self.cpp()['source_identity'];sidecar.write_bytes(b'other')
        self.assertNotEqual(before,self.cpp()['source_identity'])

    def test_cpp_all_new_companions_required_and_parity(self):
        companions={'ffe.gz':1<<11,'imap':1<<12,'sreq':1<<13,'uops.gz':1<<14,
                    'ixlat.gz':1<<15,'core.gz':1<<16,'mmuctl.gz':1<<17,'archb.gz':1<<18}
        for extension,flag in companions.items():
            with self.subTest(extension=extension):
                value=bytearray(self.fst.read_bytes());struct.pack_into('<Q',value,32,flag);self.fst.write_bytes(value)
                sidecar=Path(str(self.fst)+'.'+extension)
                self.assertEqual(subprocess.run([str(self.probe),str(self.manifest)],capture_output=True).returncode,2)
                sidecar.write_bytes(b'one');first=self.cpp();self.assertEqual(first,self.collect())
                sidecar.write_bytes(b'two');self.assertNotEqual(first['source_identity'],self.cpp()['source_identity'])
                sidecar.unlink()

    def test_cpp_context_and_state_and_implicit_slice(self):
        for line in ('0 fastsim-binary-context-v1 a.fst 0 1 1 1 1 3',
                     '0 binary-warmup-state-slice a.fst 0 1 1 1 1 memory.json',
                     '0 binary-slice a.fst 0 1 1', '0 binary a.fst'):
            (self.root / 'memory.json').write_text('{}')
            self.manifest.write_text(line + '\n')
            self.api.validate_identity(self.cpp(), self.collect())


if __name__ == '__main__': unittest.main()
