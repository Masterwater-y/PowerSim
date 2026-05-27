#!/usr/bin/env python3
"""V9 micro 粒度对齐校验：atomic_func vs detailed records.micro

校验 (tid, micro_seq, macro_pc, micro_pc) 1:1 对齐。
"""
import json, sys, os, glob

ATOMIC_DIR = sys.argv[1]
DETAILED_DIR = sys.argv[2]


def load_jsonl(path, fields):
    out = []
    with open(path) as f:
        for line in f:
            j = json.loads(line)
            out.append(tuple(j[k] for k in fields))
    return out


def core_id_of(name):
    # board.processor.coresN.core...
    import re
    m = re.search(r'cores(\d+)', name)
    return int(m.group(1))


def main():
    atomic_files = sorted(
        glob.glob(os.path.join(ATOMIC_DIR, '*.atomic_func.jsonl')),
        key=lambda p: core_id_of(os.path.basename(p)))
    detailed_files = sorted(
        glob.glob(os.path.join(DETAILED_DIR, '*.records.micro.jsonl')),
        key=lambda p: core_id_of(os.path.basename(p)))

    assert len(atomic_files) == len(detailed_files), \
        f"core 数不匹配: atomic={len(atomic_files)} detailed={len(detailed_files)}"

    fields = ('thread_id', 'micro_seq', 'macro_pc', 'micro_pc')
    total_rows = 0
    total_match = 0
    issues = []

    for af, df in zip(atomic_files, detailed_files):
        cid_a = core_id_of(os.path.basename(af))
        cid_d = core_id_of(os.path.basename(df))
        assert cid_a == cid_d, f"core id 不一致: {af} vs {df}"

        a_rows = load_jsonl(af, fields)
        d_rows = load_jsonl(df, fields)
        n = min(len(a_rows), len(d_rows))
        if len(a_rows) != len(d_rows):
            issues.append(f"core{cid_a} 行数不等 atomic={len(a_rows)} "
                          f"detailed={len(d_rows)}")

        match = sum(1 for i in range(n) if a_rows[i] == d_rows[i])
        rate = match / n * 100 if n else 0.0
        print(f"core{cid_a}: rows={n}, match={match}, rate={rate:.2f}%")

        # 列出前 5 个 mismatch
        mis_count = 0
        for i in range(n):
            if a_rows[i] != d_rows[i]:
                if mis_count < 5:
                    print(f"  mis@{i}: atomic={a_rows[i]} "
                          f"detailed={d_rows[i]}")
                mis_count += 1
        if mis_count > 5:
            print(f"  ... 共 {mis_count} 个 mismatch")

        total_rows += n
        total_match += match

    overall = total_match / total_rows * 100 if total_rows else 0.0
    print(f"\n=== overall: rows={total_rows} match={total_match} "
          f"rate={overall:.4f}% ===")
    for it in issues:
        print("WARN:", it)
    if overall >= 99.0:
        print("PASS (>=99%)")
        return 0
    print("FAIL (<99%)")
    return 1


if __name__ == '__main__':
    sys.exit(main())
