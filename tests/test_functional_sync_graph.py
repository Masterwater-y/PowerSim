import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from functional_sync_graph import build_graph


def row(core, ordinal, kind, member, region=1, generation=0, phase='measurement'):
    return dict(core_id=core, record_ordinal=ordinal, pc=4096, address_space_id=1,
                thread_id=member+1, region_instance=region, sync_object_id=77,
                event_kind=kind, generation=generation, team_size=2,
                member_index=member, phase=phase)


def final_region():
    return [row(0,0,1,0), row(0,1,3,0), row(1,0,3,1),
            row(0,2,4,0), row(1,1,4,1), row(0,3,7,0), row(1,2,7,1),
            row(0,4,8,0), row(0,5,2,0), row(1,3,8,1)]


class GraphTests(unittest.TestCase):
    def test_final_join_does_not_wait_for_late_worker_departure(self):
        events, deps, order = build_graph(final_region())
        index = {(e['core_id'],e['record_ordinal']):i for i,e in enumerate(events)}
        join, late = index[(0,5)], index[(1,3)]
        self.assertNotIn(late, deps[join])
        self.assertTrue({index[(0,3)],index[(1,2)]}.issubset(deps[index[(0,4)]]))
        self.assertLess(order.index(join), order.index(late))

    def test_reused_object_generations_are_distinct(self):
        events=[row(0,0,1,0),row(0,1,3,0),row(1,0,3,1),
                row(0,2,5,0),row(1,1,5,1),row(0,3,6,0),row(1,2,6,1),
                row(0,4,5,0,generation=8),row(1,3,5,1,generation=8),
                row(0,5,6,0,generation=8),row(1,4,6,1,generation=8)]
        es, deps, order=build_graph(events)
        by={(e['core_id'],e['record_ordinal']):i for i,e in enumerate(es)}
        self.assertTrue({by[(0,4)],by[(1,3)]}.issubset(deps[by[(0,5)]]))
        self.assertNotIn(by[(0,4)],deps[by[(0,3)]])
        self.assertEqual(len(order),len(events))

    def test_no_oracle_input_and_input_order_independent(self):
        a = build_graph(final_region())
        b = build_graph(list(reversed(final_region())))
        self.assertEqual(a, b)

    def test_missing_arrival_is_rejected_even_for_truncated_prefix(self):
        with self.assertRaisesRegex(ValueError, 'arrival'):
            build_graph([e for e in final_region() if not(e['core_id']==1 and e['event_kind']==7)])

    def test_cycle_rejected(self):
        # Each core departs before the other core reaches its arrival.
        events = [row(0,0,1,0),row(0,1,3,0),row(1,0,3,1),
                  row(0,2,6,0),row(0,3,5,0),row(1,1,6,1),row(1,2,5,1)]
        with self.assertRaisesRegex(ValueError,'cycle'):
            build_graph(events)

    def test_reverse_phase_dependency_rejected(self):
        events = final_region()
        events[0]['phase']='measurement'
        events[1]['phase']='warmup'
        with self.assertRaisesRegex(ValueError,'phase'):
            build_graph(events)

    def test_migration_and_oversubscription_rejected(self):
        events = final_region()
        events[-1]['core_id']=0
        events[-1]['record_ordinal']=6
        with self.assertRaisesRegex(ValueError,'binding|migration|oversub'):
            build_graph(events)

    def test_partial_last_region_without_departure_is_legal(self):
        events = [row(0,0,1,0), row(0,1,3,0),row(1,0,3,1),row(0,2,4,0),row(0,3,7,0)]
        e,d,o = build_graph(events)
        self.assertEqual(len(o),len(events))

if __name__ == '__main__': unittest.main()
