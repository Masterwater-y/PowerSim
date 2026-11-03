"""Exercise independent rate copies and their shared detailed-entry gate."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import ast
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]


class RateCaptureTest(unittest.TestCase):
    def test_private_copy_outputs_do_not_clobber_each_other(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as directory:
            dest=Path(directory);(dest/'inputs').mkdir();(dest/'inputs/value').write_text('shared-input')
            (dest/'probe.c').write_text(r'''
#include "fastsim_rate_roi.h"
int main(void) {
  FILE *input=fopen("inputs/value","r"); char value[32];
  if (!input || !fgets(value,sizeof(value),input)) return 3;
  fclose(input); fastsim_rate_roi_ready("private-output-probe");
  FILE *output=fopen("result.txt","w"); if (!output) return 4;
  fprintf(output,"%s:%s",getenv("FASTSIM_RATE_COPY"),value); fclose(output);
  return 0;
}
''')
            for source,binary in [(ROOT/'workloads/spec_roi_integration/fastsim_rate_launcher.c',dest/'launcher'),(dest/'probe.c',dest/'probe')]:
                subprocess.run(['gcc','-O2','-D_GNU_SOURCE','-I'+str(ROOT/'workloads/spec_roi_integration'),str(source),'-o',str(binary)],check=True,capture_output=True)
            cpus=sorted(os.sched_getaffinity(0))[:4]
            result=subprocess.run([str(dest/'launcher'),'--cpus',','.join(map(str,cpus)),
                '--private-cwd','--',str(dest/'probe')],cwd=dest,
                env=dict(os.environ,GEM5_DISABLE_ROI_MARKER='1'),capture_output=True,text=True,timeout=15)
            self.assertEqual(result.returncode,0,result.stderr)
            for i in range(4):
                self.assertEqual((dest/f'copy-{i}/result.txt').read_text(),f'{i}:shared-input')
            self.assertFalse((dest/'result.txt').exists())

    def test_housekeeping_pins_only_tasks_in_selected_proc_tree(self):
        tree=ast.parse((ROOT/'scripts/run_gem5_sync_probe.py').read_text())
        functions=[n for n in tree.body if isinstance(n,ast.FunctionDef)
                   and n.name=='guest_housekeeping_script']
        self.assertEqual(len(functions),1,'guest housekeeping setup is missing')
        import shlex
        namespace=dict(shlex=shlex)
        exec(compile(ast.Module(body=functions,type_ignores=[]),'<housekeeping>','exec'),namespace)
        original=os.sched_getaffinity(0);cpu=max(original)
        child=subprocess.Popen(['sleep','30'])
        try:
            with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as directory:
                root=Path(directory);task=root/'proc'/str(child.pid)/'task'/str(child.pid)
                task.mkdir(parents=True);(task/'comm').write_text('test-worker\n')
                script=namespace['guest_housekeeping_script'](cpu,root/'proc',root/'sys')
                result=subprocess.run(['/bin/sh','-e'],input=script,text=True,capture_output=True,timeout=5)
                self.assertEqual(result.returncode,0,result.stderr)
                self.assertEqual(os.sched_getaffinity(child.pid),{cpu})
                self.assertEqual(os.sched_getaffinity(0),original)
        finally:
            child.terminate();child.wait(timeout=5)

    def test_rate_requires_budget_except_checkpoint_only(self):
        tree=ast.parse((ROOT/'scripts/run_gem5_sync_probe.py').read_text())
        functions=[n for n in tree.body if isinstance(n,ast.FunctionDef)
                   and n.name=='validate_rate_options']
        self.assertEqual(len(functions),1,'rate option validation is missing')
        namespace={};exec(compile(ast.Module(body=functions,type_ignores=[]),'<options>','exec'),namespace)
        args=SimpleNamespace(rate_copies=4,omp_threads=4,rate_launcher=Path('launcher'),
            functional_sync=False,omp_proc_bind='',omp_places='',omp_wait_policy='',
            warmup_insts=0,save_boot_checkpoint=Path('checkpoint'),
            restore_boot_checkpoint=None,checkpoint_only=False)
        with self.assertRaisesRegex(ValueError,'positive'):namespace['validate_rate_options'](args)
        args.checkpoint_only=True;namespace['validate_rate_options'](args)
        args.checkpoint_only=False;args.warmup_insts=1000000;namespace['validate_rate_options'](args)

    def test_checkpoint_identity_binds_inputs_and_hardware(self):
        import sys
        sys.path.insert(0,str(ROOT/'tools'))
        try:
            from spec_rate_checkpoint import validate_receipt
        except ImportError:
            self.fail('checkpoint identity validation is missing')
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as directory:
            path=Path(directory);(path/'m5.cpt').write_bytes(b'cpu state')
            import hashlib
            identity={'binary':'one','channels':8}
            receipt={'identity':identity,'files':{'m5.cpt':hashlib.sha256(b'cpu state').hexdigest()}}
            validate_receipt(path,receipt,identity)
            with self.assertRaises(ValueError):validate_receipt(path,receipt,dict(identity,channels=2))
            (path/'m5.cpt').write_bytes(b'corrupted')
            with self.assertRaises(ValueError):validate_receipt(path,receipt,identity)

    def test_guest_rate_launch_does_not_force_openmp(self):
        tree=ast.parse((ROOT/'scripts/run_gem5_sync_probe.py').read_text())
        functions=[n for n in tree.body if isinstance(n,ast.FunctionDef)
                   and n.name in ('_b64_heredoc','boot_script')]
        import base64, shlex
        ns=dict(os=os,base64=base64,shlex=shlex)
        exec(compile(ast.Module(body=functions,type_ignores=[]),'<boot>', 'exec'),ns)
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as directory:
            binary=Path(directory)/'binary';binary.write_bytes(b'test')
            args=SimpleNamespace(workload_binary=str(binary),input_file=[],omp_threads=4,
                omp_proc_bind='',omp_places='',omp_wait_policy='',skip_workload_validation=False,
                warmup_steps=None,workload_args='--memdb --size 2000 --testset main --verify',
                rate_copies=4,rate_launcher=str(binary))
            script=ns['boot_script'](args)
            self.assertIn('/tmp/run/rate-launcher --cpus 0,1,2,3 -- /tmp/run/workload',script)
            self.assertNotIn('OMP_NUM_THREADS',script)

    def test_all_independent_copies_reach_gate_and_keep_single_thread(self):
        header = ROOT / 'workloads/spec_roi_integration/fastsim_rate_roi.h'
        launcher = ROOT / 'workloads/spec_roi_integration/fastsim_rate_launcher.c'
        self.assertTrue(header.exists() and launcher.exists(), 'rate copy gate is missing')
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as directory:
            dest = Path(directory)
            (dest/'probe.c').write_text(r'''
#include "fastsim_rate_roi.h"
#include <dirent.h>
int main(void) {
  fastsim_rate_roi_ready("probe");
  DIR *d=opendir("/proc/self/task"); int n=0; struct dirent *e;
  while ((e=readdir(d))) if (e->d_name[0]>='0' && e->d_name[0]<='9') ++n;
  closedir(d);
  printf("copy=%s threads=%d cpu=%d\n",getenv("FASTSIM_RATE_COPY"),n,sched_getcpu());
  return n!=1;
}
''')
            for source, binary in ((launcher, dest/'launcher'), (dest/'probe.c',dest/'probe')):
                subprocess.run(['gcc','-D_GNU_SOURCE','-O2','-I'+str(header.parent),
                                str(source),'-o',str(binary)],check=True,capture_output=True)
            cpus=sorted(os.sched_getaffinity(0))[:4]
            self.assertEqual(len(cpus),4)
            env=dict(os.environ,GEM5_DISABLE_ROI_MARKER='1')
            result=subprocess.run([str(dest/'launcher'),'--cpus',','.join(map(str,cpus)),
                                   '--',str(dest/'probe')],cwd=dest,env=env,
                                  text=True,capture_output=True,timeout=15)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertIn('all-ready copies=4',result.stdout)
            for index,cpu in enumerate(cpus):
                self.assertEqual((dest/f'copy-{index}.stdout').read_text(),
                                 f'copy={index} threads=1 cpu={cpu}\n')
            # Failure before the marker must terminate the waiting launch,
            # even while other copies still hold their inherited ready pipe.
            (dest/'probe.c').write_text('#include "fastsim_rate_roi.h"\n'
                'int main(void) { if (!strcmp(getenv("FASTSIM_RATE_COPY"),"0")) return 7; '
                'fastsim_rate_roi_ready("probe"); return 0; }\n')
            subprocess.run(['gcc','-D_GNU_SOURCE','-O2','-I'+str(header.parent),
                str(dest/'probe.c'),'-o',str(dest/'probe')],check=True,capture_output=True)
            try:
                failed=subprocess.run([str(dest/'launcher'),'--cpus',','.join(map(str,cpus)),
                    '--',str(dest/'probe')],cwd=dest,env=env,text=True,capture_output=True,timeout=3)
            except subprocess.TimeoutExpired:
                self.fail('failed copy leaves rate launcher blocked at entry gate')
            self.assertNotEqual(failed.returncode,0)


if __name__ == '__main__':
    unittest.main()
