"""Portable functional origin facts. No native service, cache or timing input."""
import hashlib
from pathlib import Path
import struct
import subprocess
HEADER=struct.Struct('<8sIIIIQQQII')
ROW=struct.Struct('<QII')
def load_retirement_eligibility(source,data,core,count):
    features=struct.unpack_from('<Q',data,32)[0]
    if not features&128:return set(),set(),{}
    if (data[:8]!=b'FSTRC01\0' or struct.unpack_from('<I',data,8)[0]!=7 or
        struct.unpack_from('<I',data,20)[0]!=core or struct.unpack_from('<Q',data,24)[0]!=count):
        raise ValueError('functional retirement source header mismatch')
    path=Path(str(source)+'.fsem')
    if not path.is_file():raise ValueError('required functional retirement companion missing')
    content=path.read_bytes()
    if len(content)<HEADER.size:raise ValueError('truncated functional retirement header')
    magic,version,size,cid,flags,records,rows,expected_stream_hash,row_size,reserved=HEADER.unpack_from(content)
    if (magic!=b'FSTRETV1' or version not in (1,2) or size!=HEADER.size or cid!=core or flags!=1 or
        records!=count or rows>count or row_size!=ROW.size or reserved or len(content)!=HEADER.size+rows*ROW.size):
        raise ValueError('invalid functional retirement header/population')
    # Verify the complete hot stream, not just exceptional rows. Large captures
    # use the maintained native accelerator; never silently skip this binding.
    if count<=65536:
        value=14695981039346656037
        for byte in memoryview(data)[72:72+count*64]:value=((value^byte)*1099511628211)&0xffffffffffffffff
        if value!=expected_stream_hash:raise ValueError('functional retirement stream identity mismatch')
    else:
        helper=Path(__file__).resolve().parents[1]/'build/fastsim_fst_stream_hash'
        if not helper.is_file():raise ValueError('functional retirement stream hash helper missing; run cmake --build build')
        check=subprocess.run([str(helper),str(source),str(count),str(expected_stream_hash)],capture_output=True,text=True)
        if check.returncode:raise ValueError('functional retirement stream identity mismatch: '+check.stderr)
    excluded=set();aborts=set();previous=-1
    for offset in range(HEADER.size,len(content),ROW.size):
        ordinal,expected,origin=ROW.unpack_from(content,offset)
        if ordinal<=previous or ordinal>=count or (origin!=1 and (version!=2 or origin!=3)):raise ValueError('invalid functional retirement row/order')
        record=data[72+64*ordinal:72+64*(ordinal+1)];value=2166136261
        for b in record:value=((value^b)*16777619)&0xffffffff
        flags,op=struct.unpack_from('<Hh',record,50)
        if value!=expected or not flags&1 or not flags&1024 or op>=-1:
            raise ValueError('functional retirement hot-record binding mismatch')
        excluded.add(ordinal)
        if origin&2:aborts.add(ordinal)
        previous=ordinal
    return excluded,aborts,{str(path.resolve()):hashlib.sha256(content).hexdigest()}
