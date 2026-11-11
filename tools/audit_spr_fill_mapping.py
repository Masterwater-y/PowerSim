#!/usr/bin/env python3
"""Audit actual FastSim output against gem5-spr fill/credit parameters.

Static parameter comparison only: this does not verify source identity, model
mechanisms, OoO equivalence, CPI/PMU, or fill/transaction populations.
"""
import argparse
import configparser
import json
import re
from pathlib import Path


def source_paths_bind(native_config, result):
    """Require all scored core FST paths under this native case directory.

    This binds the named inputs, but is not a hash, target identity or
    sampling-window validation. Relocated inputs must be explicitly re-bound.
    """
    if not isinstance(result,dict):return False
    identity=result.get('functional_source_identity',{})
    if not isinstance(identity,dict):return False
    participants=identity.get('participants',[])
    sources=identity.get('sources',[])
    if not isinstance(participants,list) or not participants or not isinstance(sources,list):return False
    by_core={}
    for source in sources:
        if not isinstance(source,dict):return False
        core=source.get('core_id')
        if type(core) is not int:return False
        if core in by_core:return False
        by_core[core]=source
    case_dir=native_config.parent.resolve()
    for core in participants:
        src=by_core.get(core,{})
        artifacts=src.get('artifacts',[])
        if not isinstance(artifacts,list):return False
        paths=[x.get('path') for x in artifacts
               if isinstance(x,dict) and x.get('role')=='fst']
        if len(paths)!=1 or not isinstance(paths[0],str):return False
        path=Path(paths[0]).resolve()
        if case_dir not in path.parents:return False
    return True


def audit(native, result):
    issues = []
    mismatches = []
    resources = {}
    if not isinstance(result,dict):result={}
    cfg = result.get('configuration')
    identity = result.get('functional_source_identity')
    if not isinstance(cfg, dict):
        cfg = {}; issues.append('missing FastSim configuration')
    if not isinstance(identity, dict) or identity.get('status') != 'complete':
        identity = {}; issues.append('missing or incomplete FastSim functional source identity')
    participants = identity.get('participants')
    cores = cfg.get('cores')
    if (not isinstance(participants, list) or not participants or
            any(type(x) is not int or x < 0 for x in participants) or
            len(set(participants)) != len(participants) or
            type(cores) is not int or cores <= 0 or
            any(x >= cores for x in participants)):
        issues.append('invalid or missing participant/core mapping')
        participants = []
    else:
        participants = sorted(participants)
    enabled = cfg.get('spr_load_fill_feedback')
    if type(enabled) is not bool:
        issues.append('missing or invalid FastSim spr_load_fill_feedback')
        enabled = None
    roots = sorted({s[:-len('l1_controllers0')] for s in native.sections()
                    if s.endswith('l1_controllers0')})
    if len(roots) != 1:
        issues.append('expected exactly one Ruby l1_controllers0 hierarchy root')
        root = ''
    else:
        root = roots[0]

    def read(section, key, expected_type=None):
        label = section + '.' + key
        if not native.has_section(section):
            issues.append('missing native section ' + section); return None
        if expected_type and native.get(section, 'type', fallback=None) != expected_type:
            issues.append('wrong or missing native type for ' + section); return None
        value = native.get(section, key, fallback=None)
        try:
            parsed = int(value)
            if parsed < 0: raise ValueError()
            return parsed
        except (TypeError, ValueError):
            issues.append('missing or invalid native ' + label); return None

    def model(key):
        val = cfg.get(key)
        if type(val) is not int or val <= 0:
            issues.append('missing or invalid FastSim ' + key); return None
        return val

    def compare(name, values, key, relation, zero_unlimited=False, strict=True):
        result_value = model(key) if key else None
        values_set = set(x for x in values if x is not None)
        if zero_unlimited and 0 in values_set:
            issues.append('native ' + name + ' is unlimited (zero) and has no finite mapping')
        if len(values_set) > 1:
            issues.append('heterogeneous native ' + name + ' cannot map to one FastSim value')
        valid = len(values) > 0 and all(x is not None for x in values)
        comparable = valid and len(values_set) == 1 and not (zero_unlimited and 0 in values_set)
        if valid and not values_set:
            comparable = False
        match = result_value == next(iter(values_set)) if comparable and result_value is not None else None
        if strict and match is False:
            mismatches.append(name + ' does not match FastSim ' + key)
        resources[name] = {'values': values, 'fastsim_key': key,
                           'fastsim_value': result_value, 'numeric_match': match,
                           'relation': relation}

    l1f=[];l2f=[];l1t=[];l2t=[];seq=[];prepare=[]
    for core in participants:
        a=root+'l1_controllers'+str(core);b=root+'l2_controllers'+str(core)
        l1f.append(read(a+'.Dcache','fill_capacity','RubyCache'))
        l2f.append(read(b+'.cache','fill_capacity','RubyCache'))
        l1t.append(read(a,'number_of_TBEs','MESI_Three_Level_L0Cache_Controller'))
        l2t.append(read(b,'number_of_TBEs','MESI_Three_Level_L1Cache_Controller'))
        seq.append(read(a+'.sequencer','max_outstanding_requests','RubySequencer'))
        prepare.append(read(a+'.sequencer','max_store_prepare_outstanding','RubySequencer'))
        if native.get(a+'.sequencer','is_cpu_sequencer',fallback='').lower() != 'true':
            issues.append('native sequencer is not confirmed as CPU sequencer: '+a+'.sequencer')
    compare('l1d_fill',l1f,'spr_l1d_fill_entries','partial_load_model',True)
    compare('private_l2_fill',l2f,'spr_l2_fill_entries','partial_load_model',True)
    compare('cpu_sequencer_outstanding',seq,'ruby_sequencer_max_outstanding','parameter_only')
    compare('cpu_sequencer_store_prepare',prepare,None,'unrepresented')
    compare('l1_controller_tbes',l1t,'l1d_mshrs','capacity_proxy_only',strict=False)
    compare('private_l2_controller_tbes',l2t,'l2_mshrs','capacity_proxy_only',strict=False)
    l3_sections=[]
    if root:
        pattern=re.compile(re.escape(root)+r'l3_controllers([0-9]+)$')
        l3_sections=sorted(((int(m.group(1)),s) for s in native.sections()
                            for m in [pattern.fullmatch(s)] if m),key=lambda x:x[0])
    if not l3_sections:
        issues.append('missing shared LLC controller sections')
    else:
        cha_count=cfg.get('cha_count')
        if cha_count is not None and (type(cha_count) is not int or
                [i for i,_ in l3_sections] != list(range(cha_count))):
            issues.append('shared LLC controller indices do not match FastSim cha_count')
    l3t=[read(s,'number_of_TBEs','MESI_Three_Level_L2Cache_Controller') for _,s in l3_sections]
    compare('shared_llc_controller_tbes',l3t,'llc_mshrs','capacity_proxy_only',strict=False)
    return {'schema':'spr-fill-parameter-audit-v1',
            'status':'unscorable' if issues else 'mismatch' if mismatches else 'pass',
            'feature_enabled':enabled, 'participants':participants,
            'scope':'static parameter mapping only',
            'mechanism_equivalence_certified':False,
            'source_identity_independently_validated':False,
            'issues':issues, 'mismatches':mismatches, 'resources':resources}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--native-config',type=Path,required=True)
    p.add_argument('--fastsim-result',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    args=p.parse_args()
    native=configparser.ConfigParser(interpolation=None,strict=True)
    try:
        with args.native_config.open() as f:native.read_file(f)
        with args.fastsim_result.open() as f:result=json.load(f)
        out=audit(native,result)
        out['source_path_binding']=source_paths_bind(args.native_config,result)
        if not out['source_path_binding']:
            out['issues'].append('native config and participant FST source paths do not bind to the same case')
            out['status']='unscorable'
    except (OSError,ValueError,configparser.Error) as exc:
        out={'schema':'spr-fill-parameter-audit-v1','status':'unscorable',
             'mechanism_equivalence_certified':False,'issues':[str(exc)]}
    out['native_config']=str(args.native_config)
    out['fastsim_result']=str(args.fastsim_result)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(out,indent=2)+'\n')
    print(json.dumps({'status':out['status'],'output':str(args.output)}))
    return 0 if out['status']=='pass' else 1
if __name__=='__main__':raise SystemExit(main())
