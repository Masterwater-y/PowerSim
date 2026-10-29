import importlib.util
from pathlib import Path
import tempfile
import unittest
import json
import os
import signal
import subprocess
import sys
import time

SPEC = importlib.util.spec_from_file_location('capture_storage', Path(__file__).resolve().parents[1] / 'tools/trace_capture_storage.py')
ROOT = Path(__file__).resolve().parents[1]


class StorageGuardTest(unittest.TestCase):
    def test_exited_leader_cannot_leave_live_writer_descendants(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as directory:
            root=Path(directory);self.root_fixture(root);output=root/'capture'
            code="""import os,sys,time
from pathlib import Path
child=os.fork()
if child: raise SystemExit(0)
Path(sys.argv[1]).write_text(str(os.getpid()))
time.sleep(2)
Path(sys.argv[2]).write_bytes(b'x'*2000000)
time.sleep(20)
"""
            command=[sys.executable,SPEC.origin,'--root',str(root),'--output',str(output),
                     '--case-cap-bytes','100000','--',sys.executable,'-c',code,
                     str(output/'child.pid'),str(output/'late.fst')]
            child=None
            try:
                result=subprocess.run(command,capture_output=True,text=True,timeout=10)
                child=int((output/'child.pid').read_text())
                stat=Path(f'/proc/{child}/stat')
                self.assertTrue(not stat.exists() or stat.read_text().split()[2]=='Z',
                                'leader exited but writer descendant is still alive')
                self.assertNotEqual(result.returncode,0)
                self.assertEqual(json.loads((output/'capture-receipt.json').read_text())['guard_reason'],
                                 'orphan-producer-group')
            finally:
                if child:
                    try:os.kill(child,signal.SIGKILL)
                    except ProcessLookupError:pass

    def test_fast_clean_exit_keeps_final_measurement_milestones(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as directory:
            root=Path(directory);(root/'configs').mkdir()
            (root/'configs/trace-storage-policy.json').write_text(json.dumps(dict(
                total_fst_cap_bytes=1000000,total_fst_stop_margin_bytes=100,
                minimum_free_disk_bytes=0)))
            output=root/'capture'
            command=[sys.executable,str(ROOT/'tools/trace_capture_storage.py'),
                '--root',str(root),'--output',str(output),'--case-cap-bytes','10000',
                '--wall-seconds','10','--',sys.executable,'-c',
                "import time; time.sleep(.1); print('CHECKPOINT marker: switch KVM -> O3+Ruby'); print('measurement window open'); print('common-end')"]
            result=subprocess.run(command,capture_output=True,text=True,timeout=15)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            receipt=json.loads((output/'capture-receipt.json').read_text())
            self.assertTrue(receipt['measurement_open'])
            self.assertIsNotNone(receipt['detailed_warmup_wall_seconds'])

    def test_inventory_survives_directory_removed_during_scan(self):
        from unittest.mock import patch
        import shutil
        module=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(module)
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]/'tmp') as directory:
            root=Path(directory);gone=root/'transient';gone.mkdir()
            (root/'retained.fst').write_bytes(b'123')
            original=os.scandir
            def disappearing(path):
                if str(path)==str(gone) and gone.exists():shutil.rmtree(gone)
                return original(path)
            with patch('os.scandir',side_effect=disappearing):
                self.assertEqual(module.fst_bytes(root),3)

    def test_warmup_wall_deadline_stops_real_producer(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]/'tmp') as directory:
            root=Path(directory);self.root_fixture(root);output=root/'capture'
            code='import time;print("CHECKPOINT marker: switch KVM -> O3+Ruby",flush=True);time.sleep(20)'
            command=[sys.executable,SPEC.origin,'--root',str(root),'--output',str(output),
                     '--case-cap-bytes','100000','--warmup-wall-seconds','1','--',sys.executable,'-c',code]
            result=subprocess.run(command,capture_output=True,text=True,timeout=8)
            receipt=json.loads((output/'capture-receipt.json').read_text())
            self.assertNotEqual(result.returncode,0)
            self.assertEqual(receipt['guard_reason'],'warmup-wall-time-limit')
            self.assertFalse(receipt['measurement_open'])

    def root_fixture(self, root):
        (root/'configs').mkdir()
        (root/'configs/trace-storage-policy.json').write_text(json.dumps(dict(
            total_fst_cap_bytes=1000000, total_fst_stop_margin_bytes=10000,
            minimum_free_disk_bytes=0)))

    def test_final_exit_still_checks_case_budget(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]/'tmp') as directory:
            root=Path(directory);self.root_fixture(root);output=root/'capture'
            code='import time,sys;from pathlib import Path;time.sleep(.1);Path(sys.argv[1]).write_bytes(b"x"*2000)'
            command=[sys.executable,SPEC.origin,'--root',str(root),'--output',str(output),
                     '--case-cap-bytes','1000','--',sys.executable,'-c',code,str(output/'a.fst')]
            result=subprocess.run(command,capture_output=True,text=True,timeout=10)
            receipt=json.loads((output/'capture-receipt.json').read_text())
            self.assertEqual(receipt['guard_reason'],'case-fst-limit')
            self.assertNotEqual(result.returncode,0)

    def test_sigterm_stops_producer_group_and_writes_receipt(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]/'tmp') as directory:
            root=Path(directory);self.root_fixture(root);output=root/'capture'
            code='import os,sys,time;from pathlib import Path;Path(sys.argv[1]).write_text(str(os.getpid()));time.sleep(60)'
            command=[sys.executable,SPEC.origin,'--root',str(root),'--output',str(output),
                     '--case-cap-bytes','100000','--',sys.executable,'-c',code,str(output/'child.pid')]
            process=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
            child=None
            try:
                deadline=time.monotonic()+5
                while not (output/'child.pid').exists() and time.monotonic()<deadline:
                    time.sleep(.01)
                child=int((output/'child.pid').read_text())
                process.terminate();process.communicate(timeout=8)
                stat=Path(f'/proc/{child}/stat')
                stopped=not stat.exists() or stat.read_text().split()[2]=='Z'
                self.assertTrue(stopped,'SIGTERM orphaned a live producer')
                receipt=json.loads((output/'capture-receipt.json').read_text())
                self.assertEqual(receipt['guard_reason'],'signal-SIGTERM')
                self.assertNotEqual(process.returncode,0)
            finally:
                if process.poll() is None:process.kill();process.communicate()
                if child is not None:
                    try:os.killpg(child,signal.SIGKILL)
                    except ProcessLookupError:pass

    def test_global_and_free_reserves_reject_before_capture(self):
        self.assertTrue(SPEC.origin and Path(SPEC.origin).exists(), 'storage guard not implemented')
        module = importlib.util.module_from_spec(SPEC)
        SPEC.loader.exec_module(module)
        policy = dict(total_fst_cap_bytes=1000, total_fst_stop_margin_bytes=100,
                      minimum_free_disk_bytes=200)
        self.assertEqual(module.stop_reason(policy, total=900, case=10, case_cap=500, free=900), 'global-fst-limit')
        self.assertEqual(module.stop_reason(policy, total=100, case=10, case_cap=500, free=199), 'free-disk-reserve')
        self.assertEqual(module.stop_reason(policy, total=100, case=500, case_cap=500, free=900), 'case-fst-limit')
        self.assertIsNone(module.stop_reason(policy, total=100, case=10, case_cap=500, free=900))

    def test_inventory_does_not_double_count_symlinked_traces(self):
        self.assertTrue(SPEC.origin and Path(SPEC.origin).exists(), 'storage guard not implemented')
        module = importlib.util.module_from_spec(SPEC)
        SPEC.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'a.fst').write_bytes(b'a' * 123)
            (root / 'copy.fst').symlink_to(root / 'a.fst')
            self.assertEqual(module.fst_bytes(root), 123)


if __name__ == '__main__':
    unittest.main()
