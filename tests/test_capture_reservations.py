import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('reservations', ROOT/'tools/capture_reservations.py')
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class ReservationsTest(unittest.TestCase):
    def test_inflight_remainder_is_counted_once(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as d:
            r=Path(d); a=r/'a'; b=r/'b'; a.mkdir(); b.mkdir()
            pool=mod.ReservationPool(r,r/'ledger.json',limit=100)
            self.assertTrue(pool.claim(a,60))
            (a/'trace.fst').write_bytes(b'x'*40)
            self.assertTrue(pool.claim(b,40))
            self.assertFalse(pool.claim(r/'c',1))
            state=json.loads((r/'ledger.json').read_text())
            self.assertEqual(state['last_upper_bound_bytes'],100)
            pool.release(a); pool.release(b)

    def test_dead_owner_reservation_is_retained(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as d:
            r=Path(d); pool=mod.ReservationPool(r,r/'ledger.json',limit=100)
            self.assertTrue(pool.claim(r/'orphan',80))
            s=json.loads((r/'ledger.json').read_text())
            s['entries'][str(r/'orphan')]['owner_pid']=99999999
            (r/'ledger.json').write_text(json.dumps(s))
            self.assertFalse(pool.claim(r/'other',21))
            with self.assertRaises(RuntimeError):pool.release(r/'orphan')

    def test_non_fst_free_space_reserve_blocks_admission(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as d:
            r=Path(d)
            pool=mod.ReservationPool(r,r/'ledger.json',limit=100,minimum_free=10**30)
            self.assertFalse(pool.claim(r/'a',10))


if __name__=='__main__':unittest.main()
