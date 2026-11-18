#!/usr/bin/env python3
"""Validate model fill decisions and report member diagnostics, never PMU scores."""
import argparse
from collections import Counter,defaultdict
import hashlib
import json
from pathlib import Path

def load_rows(path):
    with Path(path).open() as handle:
        for line in handle:
            if line.strip():yield json.loads(line)

def analyze_rows(rows,source_identity):
    rows=iter(rows);head=next(rows,None)
    if (not source_identity or not head or head.get('schema')!='llc-fill-audit-v1'
            or head.get('producer')!='retired-fill-model'
            or head.get('source_identity')!=source_identity
            or type(head.get('stride')) is not int or head['stride']<1):
        raise ValueError('missing or mismatched diagnostic identity/schema')
    seen=set();counts=Counter();pcs=Counter();n=0;tail=None
    for row in rows:
        if tail is not None:raise ValueError('records after closure')
        if row.get('type')=='end':tail=row;continue
        if row.get('type')!='decision':raise ValueError('unexpected diagnostic record')
        key=tuple(row[k] for k in ('core','ordinal','fragment','line'))
        if key in seen:raise ValueError('duplicate source fragment')
        seen.add(key);n+=1
        if not 0<=row['own']<=7 or not 0<=row['result']<=7:
            raise ValueError('invalid event mask')
        if not row['measurement'] or not row['task']:continue
        counts['sampled_measured_task_fragments']+=1
        if row['private_level']==5 and row['result']==1:
            counts['sampled_unknown_private_to_pending']+=1
        if (row['private_level']==0 and not row.get('input_fallback_reasons',0)&3
                and row['own']==0 and row['result']==1):
            counts['sampled_hit_to_pending']+=1;pcs[row['pc']]+=1
            counts['canonical_at_or_after_parent_end']+=row['canonical']>=row['parent_end']
            if row.get('final_issue') is None:counts['final_issue_unavailable']+=1
            else:counts['final_at_or_after_parent_end']+=row['final_issue']>=row['parent_end']
    if (not tail or tail.get('completed') is not True or tail.get('truncated') is not False
            or tail.get('omitted')!=0 or tail.get('rows')!=n):
        raise ValueError('incomplete/truncated diagnostic closure')
    keys=('sampled_unknown_private_to_pending','sampled_measured_task_fragments','sampled_hit_to_pending',
          'canonical_at_or_after_parent_end','final_at_or_after_parent_end','final_issue_unavailable')
    return dict(schema='retired-fill-model-diagnosis-v1',unit='sampled load-member-fragment',
        source_identity=source_identity,stride=head['stride'],sample_rows=n,
        **{k:counts[k] for k in keys},top_hit_to_pending_pcs=pcs.most_common(20),
        interpretation='time-relation diagnostics; not native false positives or architectural macro PMU error')

def pair_spr_members(model,native):
    """Caller must first bind both artifacts to the same sealed functional source.

    Compare only single-fragment member lines. No native unknown is made a hit.
    This is a diagnostic confusion matrix, not a full architectural PMU score.
    """
    refs={};groups=defaultdict(list)
    for row in native:
        key=tuple(row[k] for k in ('core','ordinal','line'))
        if key in refs:raise ValueError('duplicate native source member')
        refs[key]=row
    for row in model:groups[tuple(row[k] for k in ('core','ordinal','line'))].append(row)
    result={f'l{l}':dict(tp=0,fp=0,fn=0,tn=0,unknown=0) for l in (1,2,3)}
    excluded=Counter()
    for key,parts in groups.items():
        if key not in refs:raise ValueError('model/native source anchor mismatch')
        q=refs[key]
        if len(parts)!=1 or q.get('split') or q.get('flags',0)&12:
            excluded['multiple_fragments_split_atomic_or_write']+=1;continue
        p=parts[0]
        for l in (1,2,3):
            out=result[f'l{l}'];bit=1<<(l-1)
            if q['spr_unknown_mask']&bit:out['unknown']+=1;continue
            a=bool(p['result']&bit);b=bool(q['spr_miss_mask']&bit)
            out['tp' if a and b else 'fp' if a else 'fn' if b else 'tn']+=1
    result['excluded']=dict(excluded)
    result['unit']='sampled single-fragment load-member cache line'
    return result

def verify_artifact(path,metadata):
    path=Path(path);digest=hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda:handle.read(1024*1024),b''):digest.update(block)
    if metadata.get('sha256')!=digest.hexdigest() or metadata.get('size_bytes')!=path.stat().st_size:
        raise ValueError('diagnostic artifact is not the exact replay output')

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--stats',type=Path,required=True);p.add_argument('--audit',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True);args=p.parse_args()
    simulation=json.loads(args.stats.read_text())
    identity=simulation['functional_source_identity']
    if identity.get('status')!='complete':raise ValueError('incomplete functional source identity')
    verify_artifact(args.audit,simulation.get('retired_fill_audit',{}))
    summary_path=Path(str(args.audit)+'.summary.json')
    verify_artifact(summary_path,simulation.get('retired_fill_summary',{}))
    summary=json.loads(summary_path.read_text())
    if summary.get('source_identity')!=identity['source_identity']:
        raise ValueError('summary source identity mismatch')
    for row in summary['groups']:
        if sum(row['decisions'])!=row['members'] or sum(row['transitions'])!=row['members']:
            raise ValueError('full member counters do not conserve')
    result=analyze_rows(load_rows(args.audit),identity['source_identity'])
    result['full_member_summary']=summary
    args.output.write_text(json.dumps(result,indent=2)+'\n')

if __name__=='__main__':main()
