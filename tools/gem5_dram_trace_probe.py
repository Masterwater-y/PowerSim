"""Run with the baseline gem5 binary: isolated, read-only DRAM differential.

Requests are TSV: tick, cache-line number, ordinal. Geometry and parameters
come from the selected experiment's config.ini. The probe has cold initial
banks and fresh refresh phase; it is not a replacement for the FS baseline.
"""
import argparse
import configparser
from pathlib import Path
import struct

import m5
from m5.objects import AddrRange, DDR4_2400_8x8, MemCtrl, PyTrafficGen
from m5.objects import Root, SrcClockDomain, System, VoltageDomain


def varint(value):
    out = bytearray()
    while value >= 128:
        out.append((value & 127) | 128)
        value >>= 7
    out.append(value)
    return bytes(out)


def message(fields):
    body = b''.join(varint(tag << 3) + varint(value) for tag, value in fields)
    return varint(len(body)) + body


p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--gem5-config', required=True)
p.add_argument('--requests', required=True)
p.add_argument('--duration', type=int, required=True)
a = p.parse_args()
cfg = configparser.ConfigParser(interpolation=None)
cfg.optionxform = str
cfg.read(a.gem5_config)
names = sorted(n for n in cfg.sections() if cfg[n].get('type') == 'MemCtrl')
channels = len(names)
period = int(cfg[cfg[names[0]]['clk_domain']]['clock'])
system = System(mem_mode='timing')
system.clk_domain = SrcClockDomain(clock=str(period)+'ps', voltage_domain=VoltageDomain())
system.mem_ranges = [AddrRange('3GiB')]
system.controllers = [MemCtrl() for _ in names]
system.generators = [PyTrafficGen() for _ in names]
streams = [[] for _ in names]
for line in Path(a.requests).read_text().splitlines():
    tick, address_line, ordinal = map(int, line.split())
    if tick < 0 or tick >= a.duration:
        raise ValueError('request outside probe duration')
    streams[address_line % channels].append((tick, address_line*64, ordinal))
for channel, name in enumerate(names):
    ctrl = system.controllers[channel]
    src = cfg[name]
    src_dram = cfg[src['dram']]
    ctrl.dram = DDR4_2400_8x8()
    for key in ['mem_sched_policy', 'min_reads_per_switch', 'min_writes_per_switch',
                'write_high_thresh_perc', 'write_low_thresh_perc']:
        setattr(ctrl, key, src[key])
    for key in ['static_frontend_latency', 'static_backend_latency', 'command_window']:
        setattr(ctrl, key, src[key]+'ps')
    for key in src_dram:
        if key.startswith('t') and key not in ('type', 'two_cycle_activate'):
            setattr(ctrl.dram, key, src_dram[key]+'ps')
    for key in ['activation_limit', 'addr_mapping', 'bank_groups_per_rank', 'banks_per_rank',
                'ranks_per_channel', 'max_accesses_per_row', 'page_policy',
                'read_buffer_size', 'write_buffer_size', 'enable_dram_powerdown']:
        setattr(ctrl.dram, key, src_dram[key])
    # This diagnostic explicitly supports the captured 3-GiB, eight-channel,
    # 64-byte non-XOR stripe; fail rather than silently choose another mapping.
    expected = '0:3221225472:%d:64:128:256' % channel
    if channels != 8 or src_dram['range'] != expected:
        raise ValueError('unsupported address range: '+src_dram['range'])
    ctrl.dram.range = AddrRange(start=0, size=3<<30, intlvHighBit=8,
                               intlvBits=3, intlvMatch=channel)
    system.generators[channel].port = ctrl.port
    if streams[channel] != sorted(streams[channel]):
        raise ValueError('requests must be ordered within each channel')
    path = Path(m5.options.outdir)/('channel%d.pb' % channel)
    # gem5 src/proto/packet.proto and protoio.hh, little-endian "gem5" magic.
    data = struct.pack('<I', 0x356d6567) + message([(3, 10**12)])
    data += b''.join(message([(1, tick), (2, 1), (3, address), (4, 64), (6, ordinal)])
                     for tick, address, ordinal in streams[channel])
    path.write_bytes(data)
root = Root(full_system=False, system=system)
m5.instantiate()
for channel, gen in enumerate(system.generators):
    if streams[channel]:
        gen.start([gen.createTrace(a.duration + 1, str(Path(m5.options.outdir)/('channel%d.pb' % channel)))])
    else:
        gen.start([gen.createIdle(a.duration + 1)])
event = m5.simulate(a.duration)
print('probe ended', m5.curTick(), event.getCause())
