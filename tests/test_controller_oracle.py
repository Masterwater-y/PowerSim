import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
from controller_oracle_common import packet_trace, read_packets, decode_range, quantized_tick, validate_final_stream, required_controller_sum, validate_projection

class ControllerOracleTest(unittest.TestCase):
    def test_packet_roundtrip_read_write(self):
        rows=[dict(tick=100,cmd=1,addr=64,size=64,pkt_id=3),
              dict(tick=101,cmd=4,addr=128,size=64,pkt_id=4)]
        self.assertEqual(read_packets(packet_trace(rows)),rows)
    def test_range_preserves_hole_xor_and_match(self):
        self.assertEqual(decode_range('4294967296:35433480192:2:64:128:256'),
                         (4294967296,35433480192,2,[64,128,256]))
        with self.assertRaises(ValueError):decode_range('1:0')
    def test_quantization_direction(self):
        self.assertEqual(quantized_tick(14160,333),14319)
        self.assertEqual(quantized_tick(3332,333),3663)
        self.assertEqual(quantized_tick(0,333),0)
    def test_truncated_trace_rejects(self):
        with self.assertRaises(ValueError):read_packets(packet_trace([dict(tick=1,cmd=1,addr=0,size=64,pkt_id=1)])[:-1])
    def test_stream_must_match_final_controller_stages(self):
        rows=[dict(phase='1',kind='R',arrival='10',command='20',response='40')]
        observed=[dict(read_bursts=1,arrival_cycle_sum=10,service_cycle_sum=20,data_ready_cycle_sum=30)]
        validate_final_stream(rows,observed,4,6)
        rows[0]['command']='21'
        with self.assertRaisesRegex(ValueError,'command'):validate_final_stream(rows,observed,4,6)
    def test_missing_controller_stat_is_not_zero(self):
        s={'system.controllers0.dram.readBursts':0,'system.controllers1.dram.readBursts':2}
        self.assertEqual(required_controller_sum(s,2,'dram.readBursts'),2)
        del s['system.controllers0.dram.readBursts']
        with self.assertRaisesRegex(ValueError,'missing'):required_controller_sum(s,2,'dram.readBursts')
    def test_projection_retains_path_delay_and_reports_coverage(self):
        rows=[dict(kind='R',phase='1',arrival='30',shared_issue='10',corrected_issue='100',projected_arrival='120',projection_available='1'),
              dict(kind='W',phase='1',arrival='40',projected_arrival='40',projection_available='0')]
        self.assertEqual(validate_projection(rows)['measured_projected_reads'],1)
        rows[0]['projected_arrival']='100'
        with self.assertRaisesRegex(ValueError,'path'):validate_projection(rows)
if __name__=='__main__':unittest.main()
