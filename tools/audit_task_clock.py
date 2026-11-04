"""Offline independent BaseCPU counter audit; no observations enter replay."""
from collections import defaultdict
import hashlib
import json
import mmap
from pathlib import Path
import struct
from fst_retirement_eligibility import load_retirement_eligibility
from thread_state_graph import TASK_CLOCK_CONTRACT, TASK_CLOCK_DOMAIN
from task_clock_initial import effective_initial_state

KERNEL_SHA256='da3d5e7dbf696b977f635d1123e06b324af873183d90111101ced005dc64e174'

def integer(row,key,minimum=0):
    value=row.get(key)
    if not isinstance(value,int) or isinstance(value,bool) or not minimum<=value<1<<64:
        raise ValueError('invalid native task clock integer: '+key)
    return value

def validate_native_counter(ledger,period):
    """Recompute raw notification progression, including pending ON tails.

    RLE compresses only contiguous one-cycle ON updates. Snapshot counters are
    derived from independent notifications plus unsampled ON tails, never from
    eligibility durations or producer validity booleans.
    """
    updates=ledger.get('updates');snapshots=ledger.get('snapshots')
    epoch=ledger.get('counter_epoch')
    if not isinstance(updates,list) or (not updates and epoch is None) or not isinstance(snapshots,list) or not snapshots:
        raise ValueError('missing native counter ledger')
    rejections=ledger.get('rejections')
    unsupported={'unsupported-workload-active-halt','native-roi-initial-eligibility-unknown','unknown-native-task-clock-coverage'}
    if not isinstance(rejections,list) or any(reason not in unsupported for reason in rejections):
        raise ValueError('native task clock rejected collection')
    combined=[]
    for kind,rows in (('update',updates),('snapshot',snapshots)):
        previous=-1
        for row in rows:
            first=integer(row,'first_sequence' if kind=='update' else 'sequence')
            last=integer(row,'last_sequence') if kind=='update' else first
            if first<=previous or last<first:
                raise ValueError('native counter source order/sequence mismatch')
            previous=last;combined.append((first,last,kind,row))
    combined.sort(key=lambda item:item[0])
    sequence=0;counter=0;state=0;last_cycle=None;last_tick=None
    observation_tick=0;observation_cycle=0
    if epoch is not None:
        if (not isinstance(epoch,dict) or epoch.get('schema')!='gem5-basecpu-counter-epoch-v1' or
            epoch.get('source')!='BaseCPU::previousState/previousCycle' or
            integer(epoch,'sequence')!=0 or integer(epoch,'period',1)!=period):
            raise ValueError('invalid native counter epoch')
        observation_tick=integer(epoch,'tick');observation_cycle=integer(epoch,'cycle')
        last_cycle=integer(epoch,'previous_cycle');last_tick=observation_tick
        state=integer(epoch,'previous_state')
        if state>2 or last_cycle>observation_cycle or (observation_tick+period-1)//period!=observation_cycle:
            raise ValueError('invalid native inherited counter state')
        sequence=1
    begin=None;end=None;measured=[];sleep=0;wake=0;begin_native_state=None
    for first,last,kind,row in combined:
        if first!=sequence:raise ValueError('native counter missing or duplicate observation sequence')
        sequence=last+1
        first_tick=integer(row,'first_tick' if kind=='update' else 'tick')
        first_cycle=integer(row,'first_cycle' if kind=='update' else 'cycle')
        if first_tick<observation_tick or first_cycle<observation_cycle:
            raise ValueError('native counter chronology regressed across snapshot/update sequence')
        observation_tick=integer(row,'last_tick' if kind=='update' else 'tick')
        observation_cycle=integer(row,'last_cycle' if kind=='update' else 'cycle')
        if kind=='update':
            count=integer(row,'count',1);delta=integer(row,'delta');active=integer(row,'active_delta')
            fc,lc=integer(row,'first_cycle'),integer(row,'last_cycle')
            ft,lt=integer(row,'first_tick'),integer(row,'last_tick')
            before,after=integer(row,'first_active_counter'),integer(row,'last_active_counter')
            sleeping=row.get('sleeping');previous=row.get('previous_state');nxt=row.get('next_state')
            bootstrap=row.get('bootstrap',False)
            if (not isinstance(bootstrap,bool) or row.get('period')!=period or delta>0xffffffff or
                sleeping not in (-1,0,1) or previous!=state or nxt!=(0 if sleeping==1 else 1 if sleeping==0 else 2) or
                before!=counter or active!=(delta if state==2 else 0) or after!=counter+active*count or
                last-first+1!=count):
                raise ValueError('native counter notification/state/delta mismatch')
            if count>1 and (state!=2 or nxt!=2 or sleeping!=-1 or delta!=1 or
                            lc-fc!=count-1 or lt-ft!=(count-1)*period):
                raise ValueError('invalid native ON run-length compression')
            if count==1 and (fc!=lc or ft!=lt):raise ValueError('invalid native singleton update')
            if last_cycle is None:
                if not bootstrap or first!=0 or state!=0 or active:
                    raise ValueError('missing native observer bootstrap epoch')
            elif (bootstrap or fc<last_cycle or ((fc-last_cycle)&0xffffffff)!=delta or
                  (state==2 and fc-last_cycle>0xffffffff) or ft<last_tick):
                raise ValueError('native counter delta does not match previous update')
            if (ft+period-1)//period!=fc or (lt+period-1)//period!=lc:
                raise ValueError('native counter cycle/tick/frequency mismatch')
            # Retain excluded windows diagnostically; no identity with elapsed
            # unhalted cycles is asserted.
            if begin is not None and end is None and last_cycle is not None:
                overlap=max(0,lc-max(last_cycle,begin['cycle']))
                if state==0:sleep+=overlap
                elif state==1:wake+=overlap
            counter=after;state=nxt;last_cycle=lc;last_tick=lt
        else:
            cycle,tick,total=integer(row,'cycle'),integer(row,'tick'),integer(row,'active_counter')
            integer(row,'record_ordinal');integer(row,'pid')
            if (last_cycle is None or cycle<last_cycle or (state==2 and cycle-last_cycle>0xffffffff) or tick<last_tick or
                (tick+period-1)//period!=cycle or total!=counter+(cycle-last_cycle if state==2 else 0)):
                raise ValueError('native snapshot counter/tail mismatch')
            if not isinstance(row.get('enabled'),bool) or not isinstance(row.get('known'),bool) or (row['enabled'] and not row['known']):
                raise ValueError('invalid native snapshot eligibility')
            if row.get('kind')=='roi_begin':
                if begin is not None:raise ValueError('duplicate native ROI begin')
                begin=row;measured=[row];begin_native_state=state
            elif row.get('kind')=='roi_end':
                if begin is None or end is not None:raise ValueError('invalid native ROI end')
                end=row;measured.append(row)
                tail=max(0,cycle-max(last_cycle,begin['cycle']))
                if state==0:sleep+=tail
                elif state==1:wake+=tail
            elif row.get('kind') in ('account_out','account_in'):
                if end is not None:raise ValueError('native cut after closure')
                if begin is not None:measured.append(row)
            else:raise ValueError('unknown native snapshot kind')
    if begin is None or end is None or measured[-1] is not end:
        raise ValueError('missing native ROI closure')
    intervals=ledger.get('intervals')
    if not isinstance(intervals,list) or len(intervals)!=len(measured)-1:
        raise ValueError('missing native accounting intervals')
    for actual,a,b in zip(intervals,measured,measured[1:]):
        expected=dict(start_tick=a['tick'],end_tick=b['tick'],start_cycle=a['cycle'],end_cycle=b['cycle'],
                      active_cycles=b['active_counter']-a['active_counter'],start_sequence=a['sequence'],end_sequence=b['sequence'],
                      pid=a['pid'],enabled=a['enabled'],known=a['known'])
        if actual!=expected or b['tick']<a['tick'] or b['active_counter']<a['active_counter']:
            raise ValueError('native interval differs from independent counter snapshots')
    return dict(active_counter_delta=end['active_counter']-begin['active_counter'],sleep_cycles=sleep,
                wakeup_excluded_cycles=wake,begin=begin,end=end,intervals=intervals,begin_native_state=begin_native_state)


def macro_counts(source,meta,stream,task_ids):
    """Independently bind fsem + hot stream and count lifecycle macro members."""
    state=effective_initial_state(meta,stream)
    cuts={e['record_ordinal']:e for e in stream if e['event_kind'] in ('account_out','account_in')}
    per_pid=defaultdict(int);measured=0;excluded_macros=0
    with source.open('rb') as handle,mmap.mmap(handle.fileno(),0,access=mmap.ACCESS_READ) as data:
        if meta['task_clock_initial_known'] and meta['task_clock_initial_enabled'] and struct.unpack_from('<h',data,72+52)[0]<-1:
            raise ValueError('initial enabled task eligibility cannot be proven by a kernel record')
        if not struct.unpack_from('<Q',data,32)[0]&128:
            raise ValueError('new task clock requires bound functional retirement companion')
        excluded,aborts,hashes=load_retirement_eligibility(source,data,meta['core_id'],meta['source_record_count'])
        source_hash=struct.unpack_from('<Q',Path(str(source)+'.fsem').read_bytes(),40)[0]
        for ordinal in range(meta['source_record_count']):
            flags=struct.unpack_from('<H',data,72+64*ordinal+50)[0]
            completes=bool(flags&1 and (not flags&1024 or flags&2048))
            if ordinal>=meta['warmup_records'] and completes:
                if ordinal in excluded or ordinal in aborts:excluded_macros+=1
                else:
                    measured+=1
                    if state['known'] and state['enabled']:per_pid[state['pid']]+=1
            cut=cuts.get(ordinal)
            if cut:
                state=dict(known=True,enabled=cut['event_kind']=='account_in',
                           pid=cut['next_pid'] if cut['event_kind']=='account_in' else cut['prev_pid'])
    return dict(task=sum(count for pid,count in per_pid.items() if pid in task_ids),per_pid=dict(per_pid),
                measurement_macro_instructions=measured,excluded_hardware_entry_macros=excluded_macros,
                source_stream_hash=source_hash,functional_retirement_hashes=hashes)


def audit_task_clock(trace_dir,evidence,proxy):
    trace_dir=Path(trace_dir);metas=evidence['metadata'];task_ids=set(proxy['task_ids'])
    paths=sorted((trace_dir/'oracle').glob('task-clock-native-core*.json'))
    expected={f'task-clock-native-core{m["core_id"]}.json' for m in metas}
    if {p.name for p in paths}!=expected:
        return {**proxy,'active_status':'unavailable-native-counter-ledger','active_cycles':None,
                'accounting_contract':TASK_CLOCK_CONTRACT,'counter_domain':TASK_CLOCK_DOMAIN}
    result={**proxy,'accounting_contract':TASK_CLOCK_CONTRACT,'counter_domain':TASK_CLOCK_DOMAIN,
            'active_source':'independent-native-BaseCPU-counter-ledger','perf_equivalence':'unverified',
            'recorded_owner_proxy':{field:proxy[field] for field in ('task_macro_instructions','per_core','per_task')}}
    active=other=transition=unknown=sleep=wake=macro=0;cores=[];ledger_hashes={};fsem_hashes={};unsupported=[]
    per_task_active=defaultdict(int);per_task_macros=defaultdict(int)
    for meta in metas:
        path=trace_dir/'oracle'/f'task-clock-native-core{meta["core_id"]}.json'
        core=meta['core_id'];native=json.loads(path.read_text());ledger_hashes[path.name]=hashlib.sha256(path.read_bytes()).hexdigest()
        if (native.get('schema')!='gem5-task-clock-native-v1' or native.get('oracle_only') is not True or
            native.get('contract')!=TASK_CLOCK_CONTRACT or native.get('counter_domain')!=TASK_CLOCK_DOMAIN or
            native.get('kernel_sha256')!=KERNEL_SHA256 or meta.get('kernel_sha256')!=KERNEL_SHA256 or
            native.get('profile_id')!=meta.get('kernel_profile_id') or not isinstance(native.get('profile_id'),str) or
            not native['profile_id'].startswith('sha256:') or len(native['profile_id'])!=71 or
            any(native.get(f)!=meta.get(f) for f in ('core_id','source_file','source_record_count','warmup_records','measurement_records'))):
            raise ValueError('native task clock contract/source/kernel/profile identity mismatch')
        if (native.get('valid') is not (not native.get('rejections')) or
            sorted(native.get('task_ids',[]))!=sorted(task_ids) or
            native.get('common_cut_complete')!=meta['task_clock_common_cut_complete'] or
            any(native.get('initial_'+field)!=meta['task_clock_initial_'+field] for field in ('known','enabled','pid')) or
            any(native.get(field)!=proxy[field] for field in ('common_start_tick','common_end_tick','clock_period_ticks','ticks_per_second'))):
            raise ValueError('native task clock state/task/window certificate mismatch')
        if native.get('initial_resolution')!=meta.get('task_clock_initial_resolution'):
            raise ValueError('native task clock initial resolution differs from functional metadata')
        unsupported.extend(f'core{core}:{reason}' for reason in native['rejections'])
        audited=validate_native_counter(native,proxy['clock_period_ticks'])
        if (audited['begin']['tick']!=proxy['common_start_tick'] or audited['end']['tick']!=proxy['common_end_tick'] or
            audited['begin']['record_ordinal']!=meta['warmup_records'] or
            audited['end']['record_ordinal']!=meta['source_record_count']):
            raise ValueError('native task clock ROI differs from common elapsed window')
        stream=sorted((e for e in evidence['rows'] if e['core_id']==core),key=lambda e:(e['record_ordinal'],e['boundary']!='before_record'))
        cuts=[e for e in stream if e['event_kind'] in ('account_out','account_in')]
        snapshots=[s for s in native['snapshots'] if s['kind'] in ('account_out','account_in')]
        if len(cuts)!=len(snapshots):raise ValueError('native clock cut population mismatch')
        known=meta['task_clock_initial_known'];enabled=meta['task_clock_initial_enabled'];pid=meta['task_clock_initial_pid']
        observations={row['event_id']:row['commit_tick'] for row in
            map(json.loads,(trace_dir/'oracle/thread-state-observation.jsonl').read_text().splitlines())}
        for event,snapshot in zip(cuts,snapshots):
            if snapshot['tick']!=observations[event['event_id']]:
                raise ValueError('native counter cut differs from independent retirement observation')
            if (snapshot['kind'],snapshot['record_ordinal'])!=(event['event_kind'],event['record_ordinal']):
                raise ValueError('native clock cut order/source mismatch')
            expected_pid=event['prev_pid'] if event['event_kind']=='account_out' else event['next_pid']
            if snapshot['known'] is not True or snapshot['enabled']!=(event['event_kind']=='account_in') or snapshot['pid']!=expected_pid:
                raise ValueError('native clock snapshot functional eligibility mismatch')
        # Every snapshot eligibility, including ROI seams, is reconstructed
        # from the functional stream instead of trusting copied native flags.
        for snapshot in native['snapshots']:
            known=meta['task_clock_initial_known'];enabled=meta['task_clock_initial_enabled'];pid=meta['task_clock_initial_pid']
            for event in cuts:
                applies=event['record_ordinal']<snapshot['record_ordinal'] or (
                    event['record_ordinal']==snapshot['record_ordinal'] and snapshot['kind'] in ('account_out','account_in'))
                if applies:known=True;enabled=event['event_kind']=='account_in';pid=event['next_pid'] if enabled else event['prev_pid']
            if (snapshot['known'],snapshot['enabled'],snapshot['pid'])!=(known,enabled,pid):
                raise ValueError('native ROI eligibility differs from functional lifecycle')
        if (audited['begin_native_state']!=2 and audited['begin']['known'] and audited['begin']['enabled'] and
            audited['begin']['pid'] in task_ids):
            unsupported.append(f'core{core}:workload-not-on-at-roi-begin-unmodeled')
        counts=macro_counts(trace_dir/meta['source_file'],meta,stream,task_ids)
        if native.get('source_stream_hash')!=counts['source_stream_hash']:
            raise ValueError('native clock source stream hash mismatch')
        native_counts=native.get('per_pid_macros')
        if (not isinstance(native_counts,list) or len({r.get('pid') for r in native_counts})!=len(native_counts) or
            {r.get('pid'):r.get('macro_instructions') for r in native_counts}!=counts['per_pid']):
            raise ValueError('native clock macro population differs from independently counted FST')
        fsem_hashes.update(counts['functional_retirement_hashes']);macro+=counts['task']
        for pid,count in counts['per_pid'].items():
            if pid in task_ids:per_task_macros[pid]+=count
        totals=dict(active=0,other=0,transition=0,unknown=0)
        resolution=meta.get('task_clock_initial_resolution')
        resolution_sequence=next((s['sequence'] for s in snapshots if resolution and
            s['kind']=='account_out' and s['record_ordinal']==resolution['record_ordinal']),None)
        if resolution and resolution['prev_pid']>0 and resolution_sequence is not None and audited['begin']['sequence']<resolution_sequence:
            # Raw unknown flags must not hide a workload halt after resolving
            # the positive task prefix. Recompute this gate from native state
            # and notifications, even if a forged producer emits the proof.
            if audited['begin_native_state']!=2:
                unsupported.append(f'core{core}:initial-resolution-non-on-at-roi-begin')
            if any(update['sleeping']==1 and audited['begin']['sequence']<update['first_sequence']<resolution_sequence
                   for update in native['updates']):
                unsupported.append(f'core{core}:initial-resolution-sleep-during-prefix')
        for interval in audited['intervals']:
            bucket='unknown' if not interval['known'] else 'transition' if not interval['enabled'] else 'active' if interval['pid'] in task_ids else 'other'
            if (resolution_sequence is not None and not interval['known'] and interval['pid']==resolution['prev_pid'] and
                interval['end_sequence']<=resolution_sequence):
                bucket='active' if resolution['prev_pid'] in task_ids else 'other'
            totals[bucket]+=interval['active_cycles']
            if bucket=='active':per_task_active[interval['pid']]+=interval['active_cycles']
        expected_summary={'active_cycles':totals['active'],'other_active_cycles':totals['other'],
                          'transition_active_cycles':totals['transition'],'unknown_active_cycles':totals['unknown'],
                          'task_macro_instructions':counts['task']}
        if any(native.get(field)!=value for field,value in expected_summary.items()):
            raise ValueError('native task clock summary differs from independent counter/population audit')
        # Detect workload halts independently of producer's halted_pids list.
        halted=set()
        if audited['begin_native_state']!=2 and audited['begin']['known'] and audited['begin']['enabled']:
            halted.add(audited['begin']['pid'])
        for update in native['updates']:
            if (update['sleeping']!=1 or update['first_sequence']<=audited['begin']['sequence'] or
                update['first_sequence']>=audited['end']['sequence']):continue
            preceding=[s for s in native['snapshots'] if s['sequence']<update['first_sequence']]
            if preceding:
                state=preceding[-1]
                if state['kind']!='roi_end' and state['known'] and state['enabled']:halted.add(state['pid'])
        if sorted(halted)!=sorted(native.get('halted_pids',[])):raise ValueError('native halt population mismatch')
        if halted&task_ids:unsupported.append(f'core{core}:workload-halt-unmodeled')
        if not meta['task_clock_common_cut_complete']:unsupported.append(f'core{core}:missing-functional-eligibility-closure')
        if totals['unknown']:unsupported.append(f'core{core}:unknown-eligibility')
        active+=totals['active'];other+=totals['other'];transition+=totals['transition'];unknown+=totals['unknown']
        sleep+=audited['sleep_cycles'];wake+=audited['wakeup_excluded_cycles']
        cores.append(dict(core_id=core,active_cycles=totals['active'],inactive_cycles=totals['other'],transition_cycles=totals['transition'],
                          unknown_cycles=totals['unknown'],task_macro_instructions=counts['task'],sleep_cycles=audited['sleep_cycles'],
                          wakeup_excluded_cycles=audited['wakeup_excluded_cycles'],native_active_counter_delta=audited['active_counter_delta']))
    complete=not unsupported and unknown==0 and macro>0
    result.update(active_status='available' if complete else 'unavailable-task-clock-coverage',active_cycles=active if complete else None,
                  inactive_cycles=other if complete else None,transition_cycles=transition,unknown_cycles=unknown,
                  sleep_cycles=sleep,wakeup_excluded_cycles=wake,task_macro_instructions=macro,
                  active_cpi=active/macro if complete else None,ownership_status='complete' if complete else 'unknown-coverage',
                  strict_active_scope=complete,coverage_status='complete' if complete else 'unknown-coverage',state_scope_status='task-lifecycle',
                  per_core=cores,per_task=[dict(os_tid=pid,active_cycles=per_task_active[pid],macro_instructions=per_task_macros[pid])
                                           for pid in sorted(task_ids)],limitations=['hardware perf equivalence is unverified',*unsupported],
                  task_clock_provenance=dict(native_ledgers_sha256=ledger_hashes,functional_retirement_sha256=fsem_hashes))
    return result
