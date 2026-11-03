"""Independent controller-only gem5 oracle. Run with the captured gem5 binary.

TSV: requested tick, physical cache-line number, input identity, R|W.
Generators connect directly through transparent monitors to each controller;
offline AddrRange routing avoids introducing an unrelated XBar schedule.
No full-system checkpoint/runner is changed. Requests may be delayed on retry;
accepted monitor timestamps, not requested timestamps, define actual ingress.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent))
from controller_oracle_common import packet_trace,decode_range,selects,quantized_tick

import m5
from m5.params import NULL
from m5.objects import (AddrRange,DRAMInterface,MemCtrl,PyTrafficGen,CommMonitor,
                        MemTraceProbe,Root,System,SrcClockDomain,VoltageDomain)

p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--config-json',type=Path,required=True)
p.add_argument('--requests',type=Path,required=True)
p.add_argument('--until',type=int,required=True)
p.add_argument('--reset-tick',type=int,default=0)
p.add_argument('--variant',choices=['native','quantized','quantized-no-refresh','native-no-refresh'],default='native')
a=p.parse_args()
source=json.loads(a.config_json.read_text());controllers=source['board']['memory']['mem_ctrl']
period=source['board']['clk_domain']['clock']
if isinstance(period,list):period=period[0]
period=int(period)
if any(c['clk_domain']!='board.clk_domain' or c['dram']['clk_domain']!='board.clk_domain' for c in controllers):
    raise ValueError('heterogeneous controller clocks need explicit conversion')
if any(c['dram']['device_bus_width']*c['dram']['devices_per_rank']*c['dram']['burst_length']//8!=64 for c in controllers):
    raise ValueError('oracle requires captured64-byte bursts')
m5.ticks.setGlobalFrequency('1THz')
system=System(mem_mode='timing');system.clk_domain=SrcClockDomain(clock=str(period)+'ps',voltage_domain=VoltageDomain())
ranges=sorted({decode_range(c['dram']['range'])[:2] for c in controllers})
system.mem_ranges=[AddrRange(start=lo,end=hi) for lo,hi in ranges]
system.controllers=[MemCtrl() for _ in controllers]
system.generators=[PyTrafficGen(elastic_req=False,max_outstanding_reqs=0,
    progress_check=str(a.until+1)+'ps') for _ in controllers]
system.monitors=[CommMonitor() for _ in controllers]
system.request_traces=[MemTraceProbe(trace_compress=False,trace_file='req-%d.pb'%i,
    manager=[system.monitors[i]],probe_name='PktRequest') for i in range(len(controllers))]
system.response_traces=[MemTraceProbe(trace_compress=False,trace_file='resp-%d.pb'%i,
    manager=[system.monitors[i]],probe_name='PktResponse') for i in range(len(controllers))]
out=Path(m5.options.outdir);copied=[]
metadata={'type','cxx_class','name','path','eventq_index','clk_domain','system','port',
          'power_state','power_model','dram','range'}

def clone(obj,values,kind):
    manifest={}
    for key,value in values.items():
        if key in metadata:continue
        if key not in obj._params:raise ValueError('unhandled captured parameter '+key)
        if isinstance(value,dict):raise ValueError('unhandled nested parameter '+key)
        ptype=obj._params[key].ptype_str
        chosen=value
        if ptype in ('Latency','Clock'):
            if a.variant.startswith('quantized') and key in (
                    'tCL','tRCD','tRP','tRAS','tRTP','tRRD','tRRD_L','tXAW','tCCD_L','tCS',
                    'tBURST','tBURST_MIN','tBURST_MAX','tCWL','tRCD_WR','tCCD_L_WR',
                    'tRTW','tWTR','tWTR_L','tWR','static_frontend_latency','static_backend_latency'):
                chosen=quantized_tick(int(value),period)
            if key=='tREFI' and a.variant.endswith('no-refresh'):chosen=10**18
            setattr(obj,key,str(chosen)+'ps')
        elif ptype in ('Current','Voltage'):
            setattr(obj,key,str(value)+('A' if ptype=='Current' else 'V'))
        elif ptype=='MemorySize':setattr(obj,key,str(value)+'B')
        else:setattr(obj,key,NULL if value is None else value)
        manifest[key]=dict(source=value,effective=chosen,param_type=ptype)
    return dict(kind=kind,fields=manifest)

streams=[[] for _ in controllers];identities=[[] for _ in controllers]
seen=set()
for text in a.requests.read_text().splitlines():
    if not text.strip():continue
    fields=text.split();tick,line,identity=map(int,fields[:3]);kind=fields[3] if len(fields)>3 else 'R'
    if len(fields) not in (3,4) or kind not in ('R','W') or tick<0 or tick>=a.until or identity in seen:
        raise ValueError('invalid or duplicate oracle request')
    seen.add(identity);address=line*64
    selected=[i for i,c in enumerate(controllers) if selects(c['dram']['range'],address)]
    if len(selected)!=1:raise ValueError('address does not route to exactly one captured controller')
    i=selected[0]
    if streams[i] and tick<streams[i][-1]['tick']:raise ValueError('non-monotonic per-channel arrival stream')
    streams[i].append(dict(tick=tick,cmd=1 if kind=='R' else 4,addr=address,size=64,pkt_id=identity))
    identities[i].append(identity)
for i,c in enumerate(controllers):
    ctrl=system.controllers[i];ctrl.dram=DRAMInterface()
    copied.append(dict(controller=clone(ctrl,c,'MemCtrl'),dram=clone(ctrl.dram,c['dram'],'DRAMInterface')))
    start,end,match,masks=decode_range(c['dram']['range'])
    ctrl.dram.range=AddrRange(start=start,end=end,masks=masks,intlvMatch=match)
    system.generators[i].port=system.monitors[i].cpu_side_port
    system.monitors[i].mem_side_port=ctrl.port
    (out/('input-%d.pb'%i)).write_bytes(packet_trace(streams[i]))
root=Root(full_system=False,system=system);m5.instantiate()
for i,gen in enumerate(system.generators):
    gen.start([gen.createTrace(a.until+1,str(out/('input-%d.pb'%i)))]) if streams[i] else gen.start([gen.createIdle(a.until+1)])
if a.reset_tick:
    if not 0<a.reset_tick<a.until:raise ValueError('invalid reset tick')
    m5.simulate(a.reset_tick);m5.stats.reset()
event=m5.simulate(a.until-m5.curTick());m5.stats.dump()
(out/'oracle.json').write_text(json.dumps(dict(schema='gem5-controller-oracle-v1',variant=a.variant,
    requested_identities=identities,requested_counts=[len(s) for s in streams],copied=copied,
    period=period,until=m5.curTick(),reset_tick=a.reset_tick,stop_cause=event.getCause(),
    source_sha256=hashlib.sha256(a.config_json.read_bytes()).hexdigest(),
    requests_sha256=hashlib.sha256(a.requests.read_bytes()).hexdigest(),
    initial_state='cold controller plus supplied warmup; fresh rank refresh phase'),indent=2)+'\n')
