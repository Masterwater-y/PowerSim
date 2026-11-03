import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from analyze_store_lifecycle import interval_union, inspect_record, measurement_window, native_total, FIELDS

class StoreLifecycleTest(unittest.TestCase):
    def row(self):
        r = dict.fromkeys(FIELDS, 0)
        r.update(dispatch=2, issue=3, retire=10, eligible=12, prior_release=20,
                 send=21, release=51, prepare=12, permission=50, role=2, fragments=1)
        return r
    def test_exact_stage_conservation_without_inventing_callback(self):
        a = inspect_record(self.row())
        self.assertEqual(a['residence'], 49)
        self.assertEqual(a['admission']+a['fifo_wait']+a['service'], 41)
        self.assertIsNone(a['callback_service'])
    def test_interval_union_and_boundary_clipping(self):
        self.assertEqual(interval_union([(0, 20), (10, 30), (40, 60)], 5, 50), 35)
        self.assertEqual(interval_union([], 0, 10), 0)
    def test_invalid_stage_order_is_rejected(self):
        r = self.row(); r['send'] = 52
        with self.assertRaises(ValueError): inspect_record(r)
    def test_callback_only_if_explicitly_present(self):
        r = self.row(); r['flags'] = 8; r['callback'] = 50
        self.assertEqual(inspect_record(r)['callback_service'], 29)
        r['callback'] = 52
        with self.assertRaises(ValueError): inspect_record(r)
    def test_nonzero_warmup_origin(self):
        begin,end=measurement_window(100,50)
        self.assertEqual((begin,end),(100,150))
        self.assertEqual(interval_union([(0,20),(110,120)],begin,end),10)
    def test_no_native_store_is_valid_empty_population(self):
        self.assertEqual(native_total(dict(samples=0,mean_cycles=None)),0)
if __name__ == '__main__': unittest.main()
