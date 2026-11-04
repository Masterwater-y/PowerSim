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


COMMON_END_POLICY='common-end-v1'
RETIREMENT_STOP_POLICY='retired-stream-completion-v1'


def _excluded_cycles(ledger,begin_cycle,end_cycle):
    epoch=ledger.get('counter_epoch')
    last_cycle=epoch['previous_cycle'] if epoch else None
    state=epoch['previous_state'] if epoch else 0
    sleep=wake=0
    for update in ledger['updates']:
        if last_cycle is not None:
            overlap=max(0,min(end_cycle,update['first_cycle'])-max(begin_cycle,last_cycle))
            if state==0:sleep+=overlap
            elif state==1:wake+=overlap
        last_cycle=update['last_cycle'];state=update['next_state']
    if last_cycle is not None:
        tail=max(0,end_cycle-max(begin_cycle,last_cycle))
        if state==0:sleep+=tail
        elif state==1:wake+=tail
    return sleep,wake


def validate_retirement_stop(ledger,period,expected_record_ordinal,*,counter_audit=None):
    """Re-derive the O(1) marker counter at its exact observation position.

    A marker does not consume a sequence number or break an ON run. Its
    next_sequence may therefore point inside a subsequently compressed RLE.
    """
    if 'retirement_stop' not in ledger:return None
    stop=ledger['retirement_stop']
    if not isinstance(stop,dict) or stop.get('schema')!='gem5-retirement-stop-v1':
        raise ValueError('invalid retirement stop anchor schema')
    for field in ('record_ordinal','tick','cycle','active_counter','next_sequence'):integer(stop,field)
    audit=counter_audit if counter_audit is not None else validate_native_counter(ledger,period)
    begin,end=audit['begin'],audit['end'];position=stop['next_sequence']
    if (stop['record_ordinal']!=expected_record_ordinal or expected_record_ordinal<=0 or
        not begin['tick']<=stop['tick']<=end['tick'] or not begin['cycle']<=stop['cycle']<=end['cycle'] or
        not begin['active_counter']<=stop['active_counter']<=end['active_counter'] or
        not begin['sequence']<position<=end['sequence']+1 or
        (stop['tick']+period-1)//period!=stop['cycle']):
        raise ValueError('retirement stop source/ROI/counter bounds mismatch')
    if any(snapshot['kind'] in ('account_out','account_in') and snapshot['record_ordinal']<expected_record_ordinal and
           (snapshot['sequence']>=position or snapshot['tick']>stop['tick']) for snapshot in ledger['snapshots']):
        raise ValueError('retirement stop precedes retained functional cut snapshot')
    epoch=ledger.get('counter_epoch');counter=0
    last_cycle=epoch['previous_cycle'] if epoch else None
    state=epoch['previous_state'] if epoch else 0
    prior_tick=epoch['tick'] if epoch else 0;prior_cycle=epoch['cycle'] if epoch else 0
    observations=[(u['first_sequence'],'update',u) for u in ledger['updates']]
    observations.extend((r['sequence'],'snapshot',r) for r in ledger['snapshots'])
    observations.sort(key=lambda item:item[0]);following=None
    for sequence,kind,row in observations:
        if kind=='snapshot':
            if sequence>=position:following=(row['tick'],row['cycle']);break
            prior_tick,prior_cycle=row['tick'],row['cycle']
            continue
        consumed=min(row['count'],max(0,position-sequence))
        if consumed==0:following=(row['first_tick'],row['first_cycle']);break
        if consumed==row['count']:
            counter=row['last_active_counter'];last_cycle=row['last_cycle'];state=row['next_state']
            prior_tick,prior_cycle=row['last_tick'],row['last_cycle']
        else:
            # Full-ledger validation permits compression only for one-cycle ON updates.
            counter=row['first_active_counter']+consumed*row['active_delta']
            last_cycle=row['first_cycle']+consumed-1;state=row['next_state']
            prior_tick=row['first_tick']+(consumed-1)*period;prior_cycle=last_cycle
            following=(row['first_tick']+consumed*period,row['first_cycle']+consumed)
            break
    if (last_cycle is None or stop['tick']<prior_tick or stop['cycle']<prior_cycle or
        (following is not None and (stop['tick']>following[0] or stop['cycle']>following[1])) or
        stop['cycle']<last_cycle or (state==2 and stop['cycle']-last_cycle>0xffffffff) or
        stop['active_counter']!=counter+(stop['cycle']-last_cycle if state==2 else 0)):
        raise ValueError('retirement stop counter/tail/observation chronology mismatch')
    sleep,wake=_excluded_cycles(ledger,begin['cycle'],stop['cycle'])
    span=stop['cycle']-begin['cycle'];delta=stop['active_counter']-begin['active_counter']
    if delta+sleep+wake!=span:
        raise ValueError('retirement stop native counter/state durations do not conserve cycle span')
    return {**stop,'completion_reference_cycles':(stop['tick']-begin['tick'])/period,
            'active_counter_delta':delta,'native_cycle_span':span,
            'sleep_cycles':sleep,'wakeup_excluded_cycles':wake}


def _clip_intervals(intervals,stop):
    if stop is None:return intervals
    clipped=[]
    for interval in intervals:
        if interval['start_tick']>stop['tick'] or interval['start_sequence']>=stop['next_sequence']:break
        if interval['end_tick']<=stop['tick'] and interval['end_sequence']<stop['next_sequence']:
            clipped.append(interval);continue
        row={**interval,'end_tick':stop['tick'],'end_cycle':stop['cycle'],'end_sequence':stop['next_sequence']}
        # Start counters are reconstructed from the validated common-end intervals.
        before=sum(i['active_cycles'] for i in clipped)
        row['active_cycles']=stop['active_counter_delta']-before
        if row['active_cycles']<0:raise ValueError('retirement stop interval counter regressed')
        clipped.append(row);break
    if sum(row['active_cycles'] for row in clipped)!=stop['active_counter_delta']:
        raise ValueError('retirement stop interval decomposition does not conserve native counter')
    return clipped


def macro_counts(source,meta,stream,task_ids):
    """Independently bind fsem + hot stream and count lifecycle macro members."""
    state=effective_initial_state(meta,stream)
    cuts={e['record_ordinal']:e for e in stream if e['event_kind'] in ('account_out','account_in')}
    per_pid=defaultdict(int);measured=0;excluded_macros=0;last_retiring_boundary=0
    with source.open('rb') as handle,mmap.mmap(handle.fileno(),0,access=mmap.ACCESS_READ) as data:
        if meta['task_clock_initial_known'] and meta['task_clock_initial_enabled'] and struct.unpack_from('<h',data,72+52)[0]<-1:
            raise ValueError('initial enabled task eligibility cannot be proven by a kernel record')
        if not struct.unpack_from('<Q',data,32)[0]&128:
            raise ValueError('new task clock requires bound functional retirement companion')
        excluded,aborts,hashes=load_retirement_eligibility(source,data,meta['core_id'],meta['source_record_count'])
        source_hash=struct.unpack_from('<Q',Path(str(source)+'.fsem').read_bytes(),40)[0]
        for ordinal in range(meta['source_record_count']):
            flags=struct.unpack_from('<H',data,72+64*ordinal+50)[0]
            if flags&1:last_retiring_boundary=ordinal+1
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
                source_stream_hash=source_hash,functional_retirement_hashes=hashes,last_retiring_boundary=last_retiring_boundary)


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
    per_task_active=defaultdict(int);per_task_macros=defaultdict(int);accounting_cores=[]
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
        stop=validate_retirement_stop(native,proxy['clock_period_ticks'],counts['last_retiring_boundary'],counter_audit=audited)
        if stop is not None:
            if any(observations[e['event_id']]>stop['tick'] for e in stream if e['record_ordinal']<counts['last_retiring_boundary']):
                raise ValueError('retirement stop precedes retained functional retirement observation')
            if any(snapshot['sequence']>=stop['next_sequence'] for snapshot in snapshots
                   if snapshot['record_ordinal']<counts['last_retiring_boundary']):
                raise ValueError('retirement stop must follow retained functional cut snapshots')
        accounting_cores.append(dict(core_id=core,native=native,audited=audited,counts=counts,totals=totals,
            resolution=resolution,resolution_sequence=resolution_sequence,stop=stop))
    complete=not unsupported and unknown==0 and macro>0
    result.update(active_status='available' if complete else 'unavailable-task-clock-coverage',active_cycles=active if complete else None,
                  inactive_cycles=other if complete else None,transition_cycles=transition,unknown_cycles=unknown,
                  sleep_cycles=sleep,wakeup_excluded_cycles=wake,task_macro_instructions=macro,
                  active_cpi=active/macro if complete else None,ownership_status='complete' if complete else 'unknown-coverage',
                  strict_active_scope=complete,coverage_status='complete' if complete else 'unknown-coverage',state_scope_status='task-lifecycle',
                  per_core=cores,per_task=[dict(os_tid=pid,active_cycles=per_task_active[pid],macro_instructions=per_task_macros[pid])
                                           for pid in sorted(task_ids)],limitations=['hardware perf equivalence is unverified',*unsupported],
                  task_clock_provenance=dict(native_ledgers_sha256=ledger_hashes,functional_retirement_sha256=fsem_hashes))
    return _apply_retirement_stop_policy(result,accounting_cores,unsupported,task_ids)


def _apply_retirement_stop_policy(result,cores,common_reasons,task_ids):
    """Keep the verified full-window diagnostic and separately crop the reference."""
    common=dict(active_end_policy=COMMON_END_POLICY,active_status=result['active_status'],
        active_cycles=sum(core['totals']['active'] for core in cores),
        inactive_cycles=sum(core['totals']['other'] for core in cores),
        transition_cycles=sum(core['totals']['transition'] for core in cores),
        unknown_cycles=sum(core['totals']['unknown'] for core in cores),
        sleep_cycles=result['sleep_cycles'],wakeup_excluded_cycles=result['wakeup_excluded_cycles'],
        per_core=result['per_core'],per_task=result['per_task'],reasons=list(common_reasons))
    result.update(active_end_policy=COMMON_END_POLICY,common_end_diagnostic=common)
    present=[core['stop'] is not None for core in cores]
    if not any(present):return result
    result['active_end_policy']=RETIREMENT_STOP_POLICY
    if not all(present):
        result.update(active_status='unavailable-retirement-stop-coverage',active_cycles=None,inactive_cycles=None,
            transition_cycles=None,unknown_cycles=None,sleep_cycles=None,wakeup_excluded_cycles=None,
            active_cpi=None,strict_active_scope=False,ownership_status='unknown-coverage',coverage_status='unknown-coverage',
            per_core=[dict(core_id=core['core_id'],task_macro_instructions=core['counts']['task'],
                retirement_stop_status='available' if core['stop'] else 'missing') for core in cores],
            limitations=[*result['limitations'],'missing-per-core-retirement-stop'])
        return result
    # A common-end halt or uncertified tail is outside the new local window.
    # Re-derive in-window halt evidence instead of promoting producer booleans.
    reasons=[reason for reason in common_reasons if not any(label in reason for label in
        ('unsupported-workload-active-halt','workload-halt-unmodeled','missing-functional-eligibility-closure','unknown-eligibility'))]
    totals=dict(active=0,other=0,transition=0,unknown=0);per_core=[];per_task=defaultdict(int);sleep=wake=0
    for core in cores:
        native,audited,stop=core['native'],core['audited'],core['stop']
        clipped=_clip_intervals(audited['intervals'],stop);local=dict(active=0,other=0,transition=0,unknown=0)
        resolution,sequence=core['resolution'],core['resolution_sequence']
        for span in clipped:
            bucket='unknown' if not span['known'] else 'transition' if not span['enabled'] else 'active' if span['pid'] in task_ids else 'other'
            if resolution and not span['known'] and span['pid']==resolution['prev_pid'] and span['end_sequence']<=sequence:
                bucket='active' if span['pid'] in task_ids else 'other'
            local[bucket]+=span['active_cycles']
            if bucket=='active':per_task[span['pid']]+=span['active_cycles']
        if local['unknown']:reasons.append(f"core{core['core_id']}:unknown-retirement-stop-eligibility")
        for update in native['updates']:
            if not (update['sleeping']==1 and audited['begin']['sequence']<update['first_sequence']<stop['next_sequence']):continue
            prior=[snapshot for snapshot in native['snapshots'] if snapshot['sequence']<update['first_sequence']]
            if prior and prior[-1]['known'] and prior[-1]['enabled'] and prior[-1]['pid'] in task_ids:
                reasons.append(f"core{core['core_id']}:workload-halt-before-retirement-stop")
        for bucket in totals:totals[bucket]+=local[bucket]
        sleep+=stop['sleep_cycles'];wake+=stop['wakeup_excluded_cycles']
        per_core.append(dict(core_id=core['core_id'],active_cycles=local['active'],inactive_cycles=local['other'],
            transition_cycles=local['transition'],unknown_cycles=local['unknown'],
            task_macro_instructions=core['counts']['task'],completion_reference_cycles=stop['completion_reference_cycles'],
            sleep_cycles=stop['sleep_cycles'],wakeup_excluded_cycles=stop['wakeup_excluded_cycles'],
            native_active_counter_delta=stop['active_counter_delta'],native_cycle_span=stop['native_cycle_span'],retirement_stop={field:stop[field] for field in
                ('schema','record_ordinal','tick','cycle','active_counter','next_sequence')}))
    complete=not reasons and totals['unknown']==0 and result['task_macro_instructions']>0
    result.update(active_status='available' if complete else 'unavailable-task-clock-coverage',
        active_cycles=totals['active'] if complete else None,inactive_cycles=totals['other'] if complete else None,
        transition_cycles=totals['transition'],unknown_cycles=totals['unknown'],sleep_cycles=sleep,wakeup_excluded_cycles=wake,
        active_cpi=totals['active']/result['task_macro_instructions'] if complete else None,
        strict_active_scope=complete,ownership_status='complete' if complete else 'unknown-coverage',
        coverage_status='complete' if complete else 'unknown-coverage',per_core=per_core,
        per_task=[dict(os_tid=row['os_tid'],active_cycles=per_task[row['os_tid']],macro_instructions=row['macro_instructions'])
                  for row in result['per_task']],limitations=['hardware perf equivalence is unverified',*reasons])
    return result
