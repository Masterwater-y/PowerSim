"""Functional first-cut proofs; no native timing values enter effective state."""
import mmap
from pathlib import Path
import re
import struct
from fst_retirement_eligibility import load_retirement_eligibility

KERNEL_SHA256='da3d5e7dbf696b977f635d1123e06b324af873183d90111101ced005dc64e174'
RESOLUTION_KIND='first-account-out-idle-prefix-v1'
TASK_RESOLUTION_KIND='first-account-out-task-prefix-v1'
CUT_PCS={0xffffffff81517a0a:'account_out',0xffffffff81067eef:'account_in'}


def _integer(value,name):
    if not isinstance(value,int) or isinstance(value,bool) or not 0<=value<1<<64:
        raise ValueError('invalid initial resolution integer: '+name)
    return value


def effective_initial_state(meta,stream):
    """Apply only the versioned structural proof, retaining raw metadata.

    Callers that authorize a plan/reference must additionally call the full
    bound-source validator below. Graph validation alone supplies no native
    active counter availability.
    """
    raw=dict(known=meta.get('task_clock_initial_known',False),
             enabled=meta.get('task_clock_initial_enabled',False),pid=meta.get('task_clock_initial_pid',0))
    proof=meta.get('task_clock_initial_resolution')
    if proof is None:return raw
    if (not isinstance(proof,dict) or set(proof)!={'kind','record_ordinal','prev_pid','source_stream_hash'} or
        proof.get('kind') not in (RESOLUTION_KIND,TASK_RESOLUTION_KIND)):
        raise ValueError('unsupported functional task clock initial resolution')
    pid=_integer(raw['pid'],'initial_pid')
    ordinal=_integer(proof.get('record_ordinal'),'record_ordinal')
    if (_integer(proof.get('prev_pid'),'prev_pid')!=pid or
        proof['kind']!=(RESOLUTION_KIND if pid==0 else TASK_RESOLUTION_KIND)):
        raise ValueError('initial resolution PID/kind does not match raw initial owner')
    _integer(proof.get('source_stream_hash'),'source_stream_hash')
    if (raw!={'known':False,'enabled':False,'pid':pid} or
        raw['known'] is not False or raw['enabled'] is not False or
        meta.get('task_clock_contract')!='task-lifecycle-basecpu-active-v1' or
        meta.get('task_clock_counter_domain')!='gem5-basecpu-active-cycles-v1' or
        meta.get('task_clock_continuous_capture') is not True or
        meta.get('accounting_contract')!='task-oncpu-switch-entry-v1' or
        meta.get('common_cut_id')!='first-core-target-common-end-v1' or
        any(meta.get(f) is not True for f in ('kernel_stream_complete','common_cut_complete','owner_continuity_to_common_cut','task_clock_common_cut_complete')) or
        any(not isinstance(meta.get(f),int) or isinstance(meta[f],bool) or meta[f]!=0
            for f in ('read_failures','identity_failures','unretained_hook_events')) or
        meta.get('kernel_sha256')!=KERNEL_SHA256 or
        not re.fullmatch(r'sha256:[0-9a-f]{64}',str(meta.get('kernel_profile_id','')))):
        raise ValueError('initial resolution lacks complete continuous exact-kernel evidence')
    local=sorted((e for e in stream if e.get('core_id')==meta['core_id']),key=lambda e:e.get('record_ordinal',-1))
    initial=[e for e in local if e.get('event_kind')=='initial_owner']
    cuts=[e for e in local if e.get('event_kind') in ('account_out','account_in')]
    if (len(initial)!=1 or initial[0].get('os_tid')!=pid or initial[0].get('record_ordinal')!=0 or
        initial[0].get('boundary')!='before_record' or initial[0].get('state')!='RUNNING' or not cuts or
        cuts[0].get('event_kind')!='account_out' or cuts[0].get('prev_pid')!=pid or
        cuts[0].get('record_ordinal')!=ordinal or cuts[0].get('pc')!=0xffffffff81517a0a or
        cuts[0].get('boundary')!='after_record' or
        cuts[0].get('phase')!=('warmup' if ordinal<meta.get('warmup_records',0) else 'measurement') or
        initial[0].get('phase')!=('warmup' if meta.get('warmup_records',0)>0 else 'measurement') or
        not isinstance(initial[0].get('task_id'),int) or isinstance(initial[0]['task_id'],bool) or initial[0]['task_id']<=0 or
        cuts[0].get('prev_task_id')!=initial[0]['task_id']):
        raise ValueError('initial resolution does not match first account_out and initial owner')
    return dict(known=True,enabled=True,pid=pid)


def validate_initial_resolution(source,meta,stream):
    """Bind .fsem/full FST and require every true lifecycle cut exactly once."""
    proof=meta.get('task_clock_initial_resolution')
    if proof is None:return None
    effective_initial_state(meta,stream)
    source=Path(source);count=_integer(meta.get('source_record_count'),'source_record_count')
    with source.open('rb') as handle,mmap.mmap(handle.fileno(),0,access=mmap.ACCESS_READ) as data:
        if (len(data)<72+64*count or data[:8]!=b'FSTRC01\0' or struct.unpack_from('<I',data,8)[0]!=7 or
            struct.unpack_from('<I',data,12)[0]!=72 or struct.unpack_from('<I',data,16)[0]!=64 or
            struct.unpack_from('<I',data,20)[0]!=meta['core_id'] or struct.unpack_from('<Q',data,24)[0]!=count):
            raise ValueError('initial resolution FST source identity/population mismatch')
        features=struct.unpack_from('<Q',data,32)[0]
        if features&(128|256|16)!=(128|256|16):
            raise ValueError('initial resolution requires complete retirement, privilege and raw-syscall source')
        excluded,aborts,hashes=load_retirement_eligibility(source,data,meta['core_id'],count)
        source_hash=struct.unpack_from('<Q',Path(str(source)+'.fsem').read_bytes(),40)[0]
        if proof['source_stream_hash']!=source_hash:
            raise ValueError('initial resolution source stream hash mismatch')
        actual=[]
        for ordinal in range(count):
            offset=72+64*ordinal;pc=struct.unpack_from('<Q',data,offset)[0]
            flags,op=struct.unpack_from('<Hh',data,offset+50)
            if op==-1:raise ValueError('initial resolution raw-syscall source contains collapsed marker')
            completes=flags&1 and (not flags&1024 or flags&2048)
            if pc in CUT_PCS and completes and ordinal not in excluded and ordinal not in aborts:
                if op>=-1:raise ValueError('initial resolution lifecycle cut lacks kernel privilege')
                actual.append((ordinal,CUT_PCS[pc]))
        functional_cuts=[e for e in sorted(stream,key=lambda e:e['record_ordinal'])
                         if e['core_id']==meta['core_id'] and e.get('event_kind') in ('account_out','account_in')]
        if any(CUT_PCS.get(e.get('pc'))!=e['event_kind'] or e.get('boundary')!='after_record' for e in functional_cuts):
            raise ValueError('initial resolution functional cut PC/boundary mismatch')
        expected=[(e['record_ordinal'],e['event_kind']) for e in functional_cuts]
        if expected!=actual:
            raise ValueError('initial resolution complete source cut population/order mismatch')
        if not actual or actual[0]!=(proof['record_ordinal'],'account_out'):
            raise ValueError('initial resolution first source cut is not the declared account_out')
    return dict(kind=proof['kind'],cut_count=len(actual),source_stream_hash=source_hash,functional_retirement_sha256=hashes)
