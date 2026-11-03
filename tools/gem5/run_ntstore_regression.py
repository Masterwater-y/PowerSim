"""Small O3 SE integration probe; no accuracy or hardware-PMU claims."""
import argparse
import json
from pathlib import Path
import m5
from m5.objects import (AddrRange, Process, Root, SEWorkload, SimpleMemory,
                        SrcClockDomain, System, SystemXBar, VoltageDomain, X86O3CPU)

parser = argparse.ArgumentParser()
parser.add_argument('--binary', required=True)
args = parser.parse_args()
system = System()
system.clk_domain = SrcClockDomain(clock='3GHz', voltage_domain=VoltageDomain())
system.mem_mode = 'timing'
system.mem_ranges = [AddrRange('512MiB')]
system.membus = SystemXBar()
system.cpu = X86O3CPU()
system.cpu.icache_port = system.membus.cpu_side_ports
system.cpu.dcache_port = system.membus.cpu_side_ports
system.cpu.createInterruptController()
system.cpu.interrupts[0].pio = system.membus.mem_side_ports
system.cpu.interrupts[0].int_requestor = system.membus.cpu_side_ports
system.cpu.interrupts[0].int_responder = system.membus.mem_side_ports
system.memory = SimpleMemory(range=system.mem_ranges[0], latency='10ns')
system.memory.port = system.membus.mem_side_ports
system.system_port = system.membus.cpu_side_ports
system.workload = SEWorkload.init_compatible(args.binary)
system.cpu.workload = Process(cmd=[args.binary])
system.cpu.createThreads()
root = Root(full_system=False, system=system)
m5.instantiate()
event = m5.simulate(1000000000)
m5.stats.dump()
commit_status = system.cpu.resolveStat('commit.status')
result = dict(cause=event.getCause(), code=event.getCode(),
              commit_status={name: int(value) for name, value in
                             zip(commit_status.subnames, commit_status.value)})
(Path(m5.options.outdir) / 'ntstore-result.json').write_text(json.dumps(result, indent=2) + '\n')
print(json.dumps(dict(cause=event.getCause(), code=event.getCode())))
if event.getCause() != 'exiting with last active thread context' or event.getCode() != 0:
    raise RuntimeError('NT-store integration guest failed or timed out')
