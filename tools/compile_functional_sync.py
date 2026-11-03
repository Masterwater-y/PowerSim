#!/usr/bin/env python3
"""Compile validated portable sync facts into an oracle-free sparse runtime plan."""
import argparse
import json
from pathlib import Path
from functional_sync_graph import build_graph
from validate_functional_sync import validate, json_rows


def compile_plan(trace_dir, output, allow_truncated=False):
    trace_dir = Path(trace_dir)
    result = validate(trace_dir, allow_truncated, functional_only=True)
    if not result['valid']:
        raise ValueError('; '.join(result['errors']))
    metas = sorted((json.loads(p.read_text()) for p in trace_dir.glob('functional-sync-metadata-core*.json')),
                   key=lambda m:m['core_id'])
    if [m['core_id'] for m in metas] != list(range(len(metas))):
        raise ValueError('runtime plan requires dense physical cores')
    events, deps, _ = build_graph([e for m in metas for e in json_rows(
        trace_dir / f"functional-sync-core{m['core_id']}.jsonl")])
    if any(e['team_size']>len(metas) for e in events):
        raise ValueError('declared team exceeds static core capacity')
    if any(m['source_record_count']==0 for m in metas):
        raise ValueError('runtime plan requires nonempty physical core sources')
    lines = ['FASTSIM_FUNCTIONAL_SYNC_PLAN_V1',f'CORES {len(metas)} EVENTS {len(events)}']
    for m in metas:
        b=json.loads((trace_dir/f"functional-boundary-core{m['core_id']}.json").read_text())
        lines.append(f"CORE {m['core_id']} {m['source_record_count']} {b['warmup_records']} {b['measurement_records']}")
    for i,e in enumerate(events):
        fields=[i,e['core_id'],e['record_ordinal'],e['pc'],e['address_space_id'],e['event_kind'],
                int(e['phase']=='measurement'),len(deps[i]),*sorted(deps[i])]
        lines.append('EVENT '+' '.join(map(str,fields)))
    Path(output).parent.mkdir(parents=True,exist_ok=True)
    Path(output).write_text('\n'.join(lines)+'\n')
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--trace-dir',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--allow-truncated',action='store_true')
    a=p.parse_args()
    try: print(json.dumps(compile_plan(a.trace_dir,a.output,a.allow_truncated),indent=2))
    except (ValueError,OSError) as e: p.exit(1,f'{e}\n')
if __name__=='__main__': main()
