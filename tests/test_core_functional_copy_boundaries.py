"""Legacy materializers cannot silently discard a declared core companion."""
import json
from pathlib import Path
import runpy
import struct
import subprocess
import sys
import tempfile
import unittest
ROOT=Path(__file__).resolve().parents[1]

class CoreCopyBoundaryTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/'tmp');self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.source=self.root/'input/tao_trace/source.fst'
        self.source.parent.mkdir(parents=True)
        self.source.write_bytes(struct.pack('<8sIIIIQQ4Q',b'FSTRC01\0',7,72,64,0,0,1<<16,0,0,0,1))

    def test_formal_dataset_upgrade_refuses_core_companion_before_clone(self):
        sys.path.insert(0,str(ROOT/'tools'))
        try:module=runpy.run_path(str(ROOT/'tools/build_fst_v7_formal_dataset.py'))
        finally:sys.path.pop(0)
        output=self.root/'out/source.fst'
        with self.assertRaisesRegex(ValueError,'core functional'):
            module['upgrade_one'](self.source,output,False,True)
        self.assertFalse(output.exists())

    def test_static_map_materializer_refuses_core_companion_before_publication(self):
        (self.source.parent/'manifest.txt').write_text('0 binary source.fst\n')
        decoded=self.root/'decoded.jsonl';decoded.write_text(json.dumps(dict(pc=4096,size=1))+'\n')
        output=self.root/'out'
        result=subprocess.run([sys.executable,str(ROOT/'tools/materialize_fst_static_map_pilot.py'),
                               '--case',str(self.source.parent.parent),'--decoded',str(decoded),'--output',str(output)],
                              capture_output=True,text=True)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('core functional',result.stderr)
        self.assertFalse(output.exists())

if __name__=='__main__':unittest.main()
