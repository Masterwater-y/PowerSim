"""Directory inputs must survive guest staging without losing checkpoint identity."""
import ast
import base64
import io
import os
from pathlib import Path, PurePosixPath
import shlex
import tarfile
import tempfile
import unittest
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[1]

class ArchiveStagingTest(unittest.TestCase):
    def namespace(self):
        tree=ast.parse((ROOT/'scripts/run_gem5_sync_probe.py').read_text())
        functions=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in
                   ('validate_input_archive','_b64_heredoc','boot_script')]
        ns=dict(base64=base64,os=os,shlex=shlex,tarfile=tarfile,PurePosixPath=PurePosixPath)
        exec(compile(ast.Module(body=functions,type_ignores=[]),'<staging>','exec'),ns)
        self.assertIn('validate_input_archive',ns,'directory input validation is missing')
        return ns

    def archive(self,root,name='lib/python3.12/encodings/__init__.py',kind=tarfile.REGTYPE):
        p=root/'inputs.tar'
        with tarfile.open(p,'w') as out:
            info=tarfile.TarInfo(name);info.type=kind
            if kind==tarfile.REGTYPE:
                info.size=4;out.addfile(info,io.BytesIO(b'data'))
            else:
                info.linkname='/etc/passwd';out.addfile(info)
        return p

    def test_directory_inputs_reach_guest_before_launcher(self):
        ns=self.namespace()
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as d:
            root=Path(d);archive=self.archive(root);binary=root/'binary';binary.write_bytes(b'bin')
            args=SimpleNamespace(workload_binary=str(binary),input_file=[],input_archive=[str(archive)],
                omp_threads=4,omp_proc_bind='',omp_places='',omp_wait_policy='',skip_workload_validation=False,
                warmup_steps=None,workload_args='-I -B coreml_pb.py',rate_copies=4,rate_launcher=str(binary))
            script=ns['boot_script'](args)
            # Execute the actual generated extraction fragment against a temporary cwd.
            start=script.index('tar -xf ');end=script.index('\n',start)
            command=script[start:end].replace('/tmp/run',str(root))
            import subprocess
            staged=root/'.fastsim-input-0.tar';staged.write_bytes(archive.read_bytes())
            subprocess.run(['/bin/sh','-ec',command],cwd=root,check=True,capture_output=True)
            self.assertEqual((root/'lib/python3.12/encodings/__init__.py').read_bytes(),b'data')
            self.assertLess(start,script.index('/tmp/run/rate-launcher --cpus'))

    def test_parent_path_is_rejected(self):
        ns=self.namespace()
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as d:
            with self.assertRaises(ValueError):ns['validate_input_archive'](self.archive(Path(d),'../escape'))

    def test_link_is_rejected(self):
        ns=self.namespace()
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as d:
            with self.assertRaises(ValueError):ns['validate_input_archive'](self.archive(Path(d),'lib/link',tarfile.SYMTYPE))

    def test_archive_cannot_replace_workload_or_launcher(self):
        ns=self.namespace()
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as d:
            for name in ['workload','rate-launcher','.fastsim-stage.b64']:
                with self.subTest(name=name),self.assertRaises(ValueError):
                    ns['validate_input_archive'](self.archive(Path(d),name))

if __name__=='__main__':unittest.main()
