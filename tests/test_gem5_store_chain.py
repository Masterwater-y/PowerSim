"""Opt-in frozen gem5 probe for CPU-role versus Ruby-transition domains.

GEM5_STORE_CHAIN_BINARY must be a MESI_Three_Level gem5 with buffer stores.
The SE probe is a mechanism counterexample, not a workload accuracy test.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import resource
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]


def sent_tick_for_line(debug, line_address):
    sent = re.findall(r'^\s*(\d+): .*Memory request \(pkt: WriteReq '
                      r'\[([0-9a-fA-F]+):[0-9a-fA-F]+\]\).* was sent\b', debug, re.M)
    ticks = [int(tick) for tick, address in sent if int(address, 16) // 64 == line_address // 64]
    if not ticks:
        raise ValueError('no successful demand send for the specified line')
    return min(ticks)


class SentDemandParsingTests(unittest.TestCase):
    def test_failed_or_foreign_line_requests_do_not_prove_send(self):
        debug = ('100: lsq: Memory request (pkt: WriteReq [3000:3007]) was sent\n'
                 '200: lsq: Memory request (pkt: WriteReq [2000:2007]) was not sent\n'
                 '900: lsq: Memory request (pkt: WriteReq [2008:200f]) was sent\n')
        self.assertEqual(sent_tick_for_line(debug, 0x2000), 900)
        with self.assertRaises(ValueError):
            sent_tick_for_line(debug, 0x4000)


@unittest.skipUnless(os.environ.get('GEM5_STORE_CHAIN_BINARY'),
                     'set GEM5_STORE_CHAIN_BINARY for the Ruby mechanism probe')
class StoreChainGem5Tests(unittest.TestCase):
    def test_younger_prepare_can_overtake_a_sent_head_demand(self):
        gem5 = Path(os.environ['GEM5_STORE_CHAIN_BINARY']).resolve()
        out = Path(os.environ.get('GEM5_STORE_CHAIN_OUT',
                                  ROOT / 'tmp/store-chain-gem5-regression')).resolve()
        out.mkdir(parents=True, exist_ok=True)
        source = ROOT / 'tests/workloads/store_chain_cold.S'
        for kind in ('nt', 'single'):
            with self.subTest(kind=kind):
                binary = out / kind
                subprocess.run(['/opt/gcc-11/bin/gcc', '-nostdlib', '-static',
                                '-Wl,--build-id=none', *(['-DSINGLE'] if kind == 'single' else []),
                                str(source), '-o', str(binary)], check=True)
                directory = out / (kind + '-gem5')
                command = [str(gem5), '--outdir=' + str(directory),
                           '--debug-flags=LSQUnit,ProtocolTrace', '--debug-file=debug.log',
                           str(ROOT / 'gem5-fs/configs/deprecated/example/se.py'),
                           '--ruby', '--cpu-type=X86O3CPU', '--num-cpus=1',
                           '--mem-size=512MiB', '--l0d_size=32KiB', '--l0d_assoc=8',
                           '--l1d_size=256KiB', '--l1d_assoc=8', '--l2_size=2MiB', '--l2_assoc=16',
                           '--param=system.cpu[0].tsoStore="buffer"',
                           '--param=system.cpu[0].SQEntries=114',
                           '--param=system.cpu[0].cacheStorePorts=200', '--cmd=' + str(binary)]
                result = subprocess.run(command, capture_output=True, text=True, timeout=90,
                                        preexec_fn=lambda: resource.setrlimit(resource.RLIMIT_CORE, (0, 0)))
                directory.mkdir(exist_ok=True)
                (directory / 'driver.log').write_text(result.stdout + result.stderr)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn('exiting with last active thread context', result.stdout)
                debug = (directory / 'debug.log').read_text()
                prepare = re.findall(r'^\s*(\d+)\s+0\s+L0Cache\s+Store_Prepare\s+I>SP_IE\s+\[([^]]+)\]', debug, re.M)
                takeover = re.findall(r'^\s*(\d+)\s+0\s+L0Cache\s+Store\s+SP_IE>IM\s+\[([^]]+)\]', debug, re.M)
                if kind == 'nt':
                    self.assertEqual(len(prepare), 64)
                    self.assertEqual(len(takeover), 64)
                    self.assertEqual(prepare, takeover, 'all 64 same-line prepares precede demand in the same Ruby tick')
                    for tick, identity in prepare:
                        line = int(re.search(r'line (0x[0-9a-fA-F]+)', identity)[1], 16)
                        self.assertLess(sent_tick_for_line(debug, line), int(tick),
                                        'head demand was already sent before the younger prepare reached L0')
                else:
                    self.assertEqual(prepare, [])
                    self.assertEqual(takeover, [])
                    self.assertRegex(debug, r'L0Cache\s+Store\s+I>IM')
                (directory / 'receipt.json').write_text(json.dumps(dict(
                    command=command, returncode=result.returncode,
                    gem5_sha256=hashlib.sha256(gem5.read_bytes()).hexdigest(),
                    guest_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),
                    source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                    cold_prepare_transitions=len(prepare), takeover_transitions=len(takeover),
                    accuracy_acceptance=False), indent=2) + '\n')


if __name__ == '__main__':
    unittest.main()
