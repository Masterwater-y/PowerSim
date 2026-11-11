"""Isolated Simulator frontier witness; production TSO+prefetch remains rejected."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

class L2TsoFrontierTest(unittest.TestCase):
    def test_store_release_follows_accepted_batch_frontier(self):
        # Only this isolated binary relaxes the configuration guard; it never
        # edits production sources or asserts general support of this mode.
        text=(ROOT/'src/config.cpp').read_text()
        guard='                                   (tso_store != TsoStoreMode::kSerial && !spr_l2_prefetch_follow) ||\n'
        self.assertEqual(text.count(guard),1)
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as temp:
            path=Path(temp); config=path/'config.cpp'; obj=path/'config.o'; exe=path/'witness'
            config.write_text(text.replace(guard,''))
            compiler='/opt/gcc-11/bin/g++'
            cmd=[compiler,'-std=c++17','-O2','-flto=16','-I',str(ROOT/'include'),'-c',str(config),'-o',str(obj)]
            p=subprocess.run(cmd,capture_output=True,text=True); self.assertEqual(p.returncode,0,p.stderr)
            cmd=[compiler,'-std=c++17','-O2','-flto=16','-I',str(ROOT/'include'),str(ROOT/'tests/test_spr_l2_tso_frontier.cpp'),str(obj),str(ROOT/'build/libfastsim_lib.a'),'-lz','-lpthread','-o',str(exe)]
            p=subprocess.run(cmd,capture_output=True,text=True); self.assertEqual(p.returncode,0,p.stderr)
            env=dict(os.environ); env['LD_LIBRARY_PATH']='/opt/gcc-11/lib64:'+env.get('LD_LIBRARY_PATH','')
            p=subprocess.run([str(exe)],capture_output=True,text=True,env=env); self.assertEqual(p.returncode,0,p.stderr)

if __name__ == '__main__': unittest.main()
