import importlib.util
import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))


class BoundedWarmupPolicy(unittest.TestCase):
    def test_native_budget_requires_identity_checkpoint_after_switch(self):
        from roi_capture_policy import BoundedWarmup
        policy = BoundedWarmup(500000, defer_until_identities=True)
        with self.assertRaises(ValueError): policy.identities_ready()
        self.assertFalse(policy.checkpoint())
        with self.assertRaises(ValueError): policy.instruction_exit()
        self.assertTrue(policy.identities_ready())
        with self.assertRaises(ValueError): policy.identities_ready()
        self.assertEqual(policy.instruction_exit(), 'open-measurement')
        self.assertEqual(policy.instruction_exit(), 'finish-measurement')
        with self.assertRaises(ValueError): BoundedWarmup(0, defer_until_identities=True)

    def test_only_first_budget_exit_opens_measurement(self):
        from roi_capture_policy import BoundedWarmup
        p=BoundedWarmup(2000000)
        with self.assertRaises(ValueError):p.instruction_exit()
        self.assertTrue(p.checkpoint())
        self.assertFalse(p.checkpoint())
        self.assertEqual(p.instruction_exit(),'open-measurement')
        self.assertEqual(p.instruction_exit(),'finish-measurement')

    def test_default_marker_mode_keeps_old_behavior(self):
        from roi_capture_policy import BoundedWarmup
        p=BoundedWarmup(0);p.checkpoint()
        self.assertEqual(p.instruction_exit(),'finish-measurement')

    def test_time_limit_distinguishes_kvm_and_warmup_from_stat(self):
        from roi_capture_policy import WarmupDeadline
        p=WarmupDeadline(5)
        self.assertIsNone(p.observe('',100))
        self.assertIsNone(p.observe('CHECKPOINT marker: switch KVM -> O3+Ruby',101))
        self.assertEqual(p.observe('',106),'warmup-wall-time-limit')
        q=WarmupDeadline(5);q.observe('CHECKPOINT marker: switch KVM -> O3+Ruby',101)
        q.observe('WARMUP instruction budget reached: measurement window open',102)
        self.assertIsNone(q.observe('',110))


if __name__=='__main__':unittest.main()
