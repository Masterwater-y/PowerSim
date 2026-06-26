import json
f = open('data/windows_exp_tstart/windows.jsonl')
for _ in range(2):
    r = json.loads(f.readline())
    print(r['workload'], 'n_core=', r['n_core'])
    print('  t_start_rel=', [round(x, 1) for x in r['t_start_rel']])
    print('  instr_retired=', r['instr_retired'])
    print('  cpi=', [round(c[0], 3) for c in r['label']])
    print('  tokens_len=', len(r['tokens']))
for line in f:
    r = json.loads(line)
    if r['workload'] == 'W_false_sharing':
        print('FS t_start_rel=', [round(x, 1) for x in r['t_start_rel']])
        print('FS cpi=', [round(c[0], 3) for c in r['label']])
        break
