import configparser
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from audit_dram_mechanisms import (audit_controllers, reference_decode,
                                   validate_probe_configuration, audit_write_activity,
                                   TIMINGS, GEOMETRY, CTRL_FIELDS)


class DramMechanismAuditTest(unittest.TestCase):
    def fixture(self):
        c = configparser.ConfigParser(interpolation=None)
        c['board.clk_domain'] = {'clock': '333'}
        for i in range(2):
            name = f'board.memory.mem_ctrl{i:02d}'
            c[name] = {'dram': name + '.dram', 'mem_sched_policy': 'frfcfs'}
            c[name + '.dram'] = {
                'range': f'0:1073741824:{i}:64',
                'addr_mapping': 'RoRaBaCoCh', 'page_policy': 'open_adaptive',
                'ranks_per_channel': '2', 'banks_per_rank': '16',
                'bank_groups_per_rank': '4', 'device_rowbuffer_size': '1024',
                'devices_per_rank': '8', 'tCL': '14160', 'tCWL': '14160',
                'tRCD_WR': '14160', 'tCCD_L_WR': '5000', 'tRTW': '1666',
                'tWTR': '5000', 'tWTR_L': '5000', 'tWR': '15000',
                'tREFI': '7800000', 'tRFC': '350000',
            }
        d = dict(channels=2, ranks_per_channel=2, banks_per_channel=16,
                 bank_groups_per_rank=4, row_bytes=8192, t_cl=43, t_cwl=43,
                 t_rcd_wr=43, t_ccd_l_wr=16, t_rtw=6, t_wtr=16,
                 t_wtr_l=16, t_wr=46, scheduler='frfcfs')
        for key in GEOMETRY:
            d.setdefault(key, 16)
        for key in CTRL_FIELDS:
            d.setdefault(key, 16)
        for key in TIMINGS:
            d.setdefault(key, 43)
        d.update(frontend_latency=31, backend_latency=31)
        for i in range(2):
            ctrl = f'board.memory.mem_ctrl{i:02d}'
            for key, source in GEOMETRY.items():
                c[ctrl + '.dram'][source] = str(d[key])
            for key, source in TIMINGS.items():
                if source not in c[ctrl + '.dram']:
                    c[ctrl + '.dram'][source] = str(d[key] * 333)
            for key, source in CTRL_FIELDS.items():
                c[ctrl][source] = str(d[key])
            c[ctrl]['static_frontend_latency'] = '10000'
            c[ctrl]['static_backend_latency'] = '10000'
        return c, d

    def test_all_controllers_not_only_first(self):
        c, d = self.fixture()
        result = audit_controllers(c, d)
        self.assertTrue(result['direct_valid'])
        c['board.memory.mem_ctrl01.dram']['tCWL'] = '15000'
        result = audit_controllers(c, d)
        self.assertFalse(result['direct_valid'])
        bad = [x for x in result['checks'] if not x['match']]
        self.assertEqual([(x['controller'], x['field']) for x in bad],
                         [('board.memory.mem_ctrl01', 't_cwl')])

    def test_missing_parameter_is_not_match(self):
        c, d = self.fixture()
        del d['t_wr']
        self.assertFalse(audit_controllers(c, d)['direct_valid'])

    def test_refresh_is_explicit_gap_not_parameter_match(self):
        c, d = self.fixture()
        result = audit_controllers(c, d)
        self.assertFalse(result['semantic_equivalence'])
        self.assertEqual(result['refresh'][0]['t_rfc_cycles'], 1052)
        self.assertEqual(result['refresh'][0]['t_refi_cycles'], 23424)
        self.assertFalse(result['refresh'][0]['modeled'])

    def test_unsupported_mapping_fails_closed(self):
        c, d = self.fixture()
        c['board.memory.mem_ctrl01.dram']['addr_mapping'] = 'RoCoRaBaCh'
        self.assertFalse(audit_controllers(c, d)['direct_valid'])

    def test_enabled_refresh_binds_physical_timers(self):
        c,d=self.fixture()
        d.update(refresh_enabled=True,t_refi_ticks=7800000,t_rfc_ticks=350000,
                 refresh_ticks_per_cycle=333,refresh_start_tick=0)
        result=audit_controllers(c,d)
        self.assertTrue(result['refresh'][0]['modeled'])
        self.assertTrue(result['direct_valid'])
        d['t_rfc_ticks']+=1
        self.assertFalse(audit_controllers(c,d)['direct_valid'])

    def test_reference_range_bits_bank_rank_row(self):
        c, _ = self.fixture()
        # Two channels, 128 columns, 16 banks, two ranks.
        for bank, rank, row in [(0, 0, 0), (3, 1, 7), (15, 1, 1)]:
            local_line = (((row * 2 + rank) * 16 + bank) * 128 + 17)
            for channel in range(2):
                address = (local_line * 2 + channel) * 64
                self.assertEqual(reference_decode(c, address, 64),
                                 dict(channel=channel, rank=rank, bank=bank,
                                      bank_group=bank % 4, row=row))
        with self.assertRaises(ValueError):
            reference_decode(c, 1 << 30, 64)

    def test_reference_xor_interleave_and_high_region(self):
        c, _ = self.fixture()
        for i in range(2):
            c[f'board.memory.mem_ctrl{i:02d}.dram']['range'] = (
                f'4294967296:5368709120:{i}:320')  # parity(address bits 6,8)
        self.assertEqual(reference_decode(c, (1 << 32) + 256, 64)['channel'], 1)
        self.assertEqual(reference_decode(c, (1 << 32) + 320, 64)['channel'], 0)

    def test_probe_must_use_exact_scored_configuration(self):
        _, d = self.fixture()
        probe = dict(line_size=64, configuration=dict(d))
        validate_probe_configuration(probe, d, 64)
        probe['configuration']['t_wr'] += 1
        with self.assertRaisesRegex(ValueError, 'configuration'):
            validate_probe_configuration(probe, d, 64)
        with self.assertRaisesRegex(ValueError, 'line size'):
            validate_probe_configuration(probe, d, 128)

    def test_native_accepted_writes_are_distinct_from_serviced_writes(self):
        rows = []
        for i in range(2):
            for field, value in [('writeReqs', 7), ('writeBursts', 7),
                                 ('dram.writeBursts', 3), ('numWriteReadTurnArounds', 1)]:
                rows.append(f'board.memory.mem_ctrl{i}.{field} {value} # test')
        result = audit_write_activity('\n'.join(rows), {0, 1})
        self.assertEqual(result['writeReqs'], 14)
        self.assertEqual(result['dram.writeBursts'], 6)
        self.assertEqual(result['numWriteReadTurnArounds'], 2)
        with self.assertRaisesRegex(ValueError, 'complete'):
            audit_write_activity('\n'.join(rows[:-1]), {0, 1})
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            audit_write_activity('\n'.join(rows + rows[:1]), {0, 1})
        with self.assertRaisesRegex(ValueError, 'complete'):
            audit_write_activity('\n'.join(rows).replace('mem_ctrl1.', 'mem_ctrl99.'), {0, 1})


if __name__ == '__main__':
    unittest.main()
