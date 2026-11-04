"""Exact machine code and collection policy for independent native task clocks."""
import ast
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
KERNEL=Path('/root/.cache/gem5/x86-linux-kernel-5.4.49-1.0.0')

class NativeTaskClockProfileTest(unittest.TestCase):
    def test_exact_kernel_has_paired_lifecycle_and_current_task_byte_proofs(self):
        if not KERNEL.is_file():self.skipTest('optional audited guest kernel absent')
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as directory:
            output=Path(directory)/'profile.json'
            run=subprocess.run(['python3',str(ROOT/'tools/generate_guest_scheduler_profile.py'),
                '--kernel',str(KERNEL),'--output',str(output),'--inventory-only','--task-clock'],
                capture_output=True,text=True)
            self.assertEqual(run.returncode,0,run.stderr)
            data=json.loads(output.read_text())['machine_code_evidence']
            self.assertEqual(data['task_clock_contract'],'task-lifecycle-basecpu-active-v1')
            self.assertEqual(data['boundaries']['account_out']['pc'],'0xffffffff81517a0a')
            self.assertEqual(data['boundaries']['account_in']['pc'],'0xffffffff81067eef')
            self.assertEqual(data['boundaries']['account_out']['next_pointer_register'],'r13')
            self.assertEqual(data['boundaries']['account_in']['next_pointer_register'],'r15')
            proofs={p['name']:p for p in data['proofs']}
            for name in ('account_out','account_in','same_task_switch_bypass','current_task_store',
                         'perf_sched_out_call','perf_sched_in_next_and_call'):
                self.assertTrue(proofs[name]['bytes_verified'],name)

    def test_preflight_rechecks_new_proofs_and_preserves_legacy_default(self):
        if not KERNEL.is_file():self.skipTest('optional audited guest kernel absent')
        tool=ROOT/'tools/generate_guest_scheduler_profile.py'
        spec=importlib.util.spec_from_file_location('clock_profile_generator',tool)
        helper=importlib.util.module_from_spec(spec);spec.loader.exec_module(helper)
        runner=ROOT/'scripts/run_gem5_sync_probe.py'
        node=next(n for n in ast.parse(runner.read_text()).body
                  if isinstance(n,ast.FunctionDef) and n.name=='thread_state_profile')
        scope=dict(__file__=str(runner),Path=Path,importlib=__import__('importlib'),
                   json=json,hashlib=hashlib)
        exec(compile(ast.Module(body=[node],type_ignores=[]),str(runner),'exec'),scope)
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as directory:
            path=Path(directory)/'profile.json'
            legacy=helper.generate(KERNEL)
            path.write_text(json.dumps(legacy))
            params,_=scope['thread_state_profile'](path,KERNEL)
            self.assertNotIn('scheduler_account_out_pc',params)
            with self.assertRaisesRegex(ValueError,'evidence mismatch'):
                scope['thread_state_profile'](path,KERNEL,task_clock=True)
            profile=helper.generate(KERNEL,task_clock=True);path.write_text(json.dumps(profile))
            params,_=scope['thread_state_profile'](path,KERNEL,task_clock=True)
            self.assertEqual(params['scheduler_account_out_pc'],0xffffffff81517a0a)
            self.assertEqual(params['scheduler_account_in_pc'],0xffffffff81067eef)
            profile['machine_code_evidence']['proofs'][-1]['bytes_verified']=False
            path.write_text(json.dumps(profile))
            with self.assertRaisesRegex(ValueError,'evidence mismatch'):
                scope['thread_state_profile'](path,KERNEL,task_clock=True)

    def test_capture_policy_requires_thread_state_for_task_clock(self):
        spec=importlib.util.spec_from_file_location('native_clock_collection',ROOT/'gem5-spr/configs/fastsim/collection.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        base=['--fastsim-root',str(ROOT),'--workload-binary','fixture','--participant-cores','4']
        args=module.parse_args(base)
        self.assertTrue(hasattr(args,'task_clock'),'new explicit --task-clock flag missing')
        self.assertFalse(args.task_clock)
        args=module.parse_args(base+['--task-clock'])
        with self.assertRaisesRegex(ValueError,'thread-state'):module.validate_options(args)
        args=module.parse_args(base+['--task-clock','--thread-state','--scheduler-profile','fixture'])
        module.validate_options(args)
        options=module.trace_options(args,'trace','profile',{})
        self.assertTrue(options['functional_task_clock'])

if __name__=='__main__':unittest.main()
