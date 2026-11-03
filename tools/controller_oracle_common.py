"""Pure helpers for the independent gem5 controller oracle; no gem5 imports."""
import struct
import math

def varint(value):
    if value<0:raise ValueError('negative protobuf integer')
    out=bytearray()
    while value>=128:out.append((value&127)|128);value>>=7
    out.append(value);return bytes(out)

def message(fields):
    body=b''.join(varint(key<<3)+varint(value) for key,value in fields)
    return varint(len(body))+body

def packet_trace(rows):
    fields={'tick':1,'cmd':2,'addr':3,'size':4,'flags':5,'pkt_id':6}
    return b'gem5'+message([(3,10**12)])+b''.join(message([(fields[k],v) for k,v in row.items()]) for row in rows)

def read_packets(data):
    if data[:4]!=b'gem5':raise ValueError('bad packet trace magic')
    position=4;out=[];header=True
    def number():
        nonlocal position
        result=shift=0
        while True:
            if position>=len(data) or shift>63:raise ValueError('truncated/invalid protobuf integer')
            b=data[position];position+=1;result|=(b&127)<<shift
            if b<128:return result
            shift+=7
    while position<len(data):
        size=number();end=position+size
        if end>len(data):raise ValueError('truncated packet')
        row={}
        while position<end:
            tag=number();wire=tag&7;key=tag>>3
            if wire==0:row[key]=number()
            elif wire==2:
                length=number();position+=length
            else:raise ValueError('unsupported protobuf wire type')
        if position!=end:raise ValueError('invalid protobuf size')
        if header:
            if row.get(3)!=10**12:raise ValueError('unsupported packet clock')
            header=False
        else:
            names={1:'tick',2:'cmd',3:'addr',4:'size',5:'flags',6:'pkt_id',7:'pc'}
            out.append({names[k]:v for k,v in row.items() if k in names})
    if header:raise ValueError('missing packet header')
    return out

def decode_range(text):
    fields=list(map(int,text.split(':')))
    if len(fields)!=2 and len(fields)<4:raise ValueError('invalid range')
    start,end=fields[:2]
    if end<=start:raise ValueError('invalid range bounds')
    match,masks=(fields[2],fields[3:]) if len(fields)>2 else (0,[])
    if any(x<=0 for x in masks) or match>=1<<len(masks):raise ValueError('invalid range masks')
    return start,end,match,masks

def selects(text,address):
    start,end,match,masks=decode_range(text)
    return start<=address<end and sum((bin(address&m).count('1')%2)<<i for i,m in enumerate(masks))==match

def quantized_tick(value,period):
    if value<0 or period<=0:raise ValueError('invalid time conversion')
    return ((value+period-1)//period)*period

def validate_final_stream(rows,observed,frontend,backend):
    measured=[r for r in rows if r['phase']=='1' and r['kind']=='R']
    if len(measured)!=sum(x['read_bursts'] for x in observed):raise ValueError('stream read population mismatch')
    for field,key in [('arrival','arrival_cycle_sum'),('command','service_cycle_sum'),('response',None)]:
        value=sum(int(r[field]) for r in measured)
        expected=sum(x[key] for x in observed) if key else sum(x['data_ready_cycle_sum']+x['read_bursts']*(frontend+backend) for x in observed)
        if value!=expected:raise ValueError('stream does not bind final controller '+field)

def required_controller_sum(values,count,field):
    result=0
    for i in range(count):
        key='system.controllers%d.%s'%(i,field)
        if key not in values:raise ValueError('missing required controller stat '+key)
        value=values[key]
        if not math.isfinite(value) or value<0:raise ValueError('invalid controller stat '+key)
        result+=value
    return result

def validate_projection(rows):
    result=dict(measured_reads=0,measured_projected_reads=0,measured_unprojected_reads=0,
                measured_read_shift_sum=0,warmup_projected_reads=0,writebacks_unchanged=0)
    for row in rows:
        arrival=int(row['arrival']);projected=int(row['projected_arrival']);available=int(row['projection_available'])
        if arrival<0 or projected<0 or available not in (0,1):raise ValueError('invalid projected timestamp or coverage')
        if available:
            if row['kind']!='R':raise ValueError('only demand reads have a projection')
            shared=int(row['shared_issue']);corrected=int(row['corrected_issue'])
            if shared<0 or corrected<0 or arrival<shared or projected!=corrected+arrival-shared:
                raise ValueError('projection did not retain shared/MSHR path delay')
        elif projected!=arrival:raise ValueError('unavailable projection changed arrival')
        if row['kind']=='W':result['writebacks_unchanged']+=1
        elif row['phase']=='1':
            result['measured_reads']+=1
            result['measured_projected_reads' if available else 'measured_unprojected_reads']+=1
            result['measured_read_shift_sum']+=projected-arrival
        elif available:result['warmup_projected_reads']+=1
    if not result['measured_projected_reads']:raise ValueError('no measured demand-read projections')
    return result
