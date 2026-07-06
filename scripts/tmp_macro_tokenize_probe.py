"""One-off probe: c04 raw parquet -> macro sequence -> assembly-style tokens
-> Qwen tokenizer length + native-token ratio.
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import Counter, defaultdict, deque
from pathlib import Path

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from model import tokenizer as tk  # noqa: E402


OP_CLASS_NAME = {
    0: "nop", 1: "iAlu", 2: "iMul", 3: "iDiv",
    4: "fAdd", 5: "fCmp", 6: "fCvt", 7: "fMul", 8: "fMac",
    9: "fDiv", 10: "fMisc", 11: "fSqrt",
    12: "sAdd", 13: "sAddAcc", 14: "sAlu", 15: "sCmp", 16: "sCvt",
    17: "sMisc", 18: "sMul", 19: "sMac", 20: "sMatMul",
    21: "sShift", 22: "sShAcc", 23: "sDiv", 24: "sSqrt",
    25: "sfAdd", 26: "sfAlu", 27: "sfCmp", 28: "sfCvt", 29: "sfDiv",
    30: "sfMisc", 31: "sfMul", 32: "sfMac", 33: "sfMatMul", 34: "sfSqrt",
    35: "sRdAdd", 36: "sRdAlu", 37: "sRdCmp",
    38: "sfRdAdd", 39: "sfRdCmp",
    56: "MemRd", 57: "MemWr", 58: "fMemRd", 59: "fMemWr",
    60: "iPref",
    88: "sys",
}


def op_class_name(oc: int) -> str:
    return OP_CLASS_NAME.get(int(oc), f"op{int(oc)}")


def path_class_name(pc: int) -> int:
    # 0 L1 hit, 1 L2 hit, 2 LLC hit, 3 DRAM, 4 remote (from data/build_windows.py)
    return int(pc)


REG_NAMES = ["ax", "bx", "cx", "dx", "si", "di", "bp", "sp",
             "r8", "r9", "r10", "r11", "r12", "r13", "r14", "r15"]


def reg_name(idx: int) -> str:
    if idx < 0:
        idx = 0
    return REG_NAMES[idx % len(REG_NAMES)]


def rd_bucket_to_tag(b: int) -> str:
    """Map project RD bucket to a short semantic tag."""
    if b == tk.RD_LE8:
        return "hot"
    if b == tk.RD_LE64:
        return "warm"
    if b in (tk.RD_LE512, tk.RD_LE4K):
        return "mid"
    if b in (tk.RD_LE32K, tk.RD_LE256K):
        return "cool"
    if b == tk.RD_FAR:
        return "cold"
    return ""  # RD_COLD / RD_NONMEM: no tag (first touch or non-mem)


def stride_to_tag(b: int) -> str:
    if b in (tk.ST_SAME,):
        return "same"
    if b in (tk.ST_P1, tk.ST_M1):
        return "seq"
    if b in (tk.ST_P2_8, tk.ST_M2_8):
        return "str"
    if b in (tk.ST_P9_64, tk.ST_M9_64):
        return "far"
    if b == tk.ST_LARGE:
        return "rnd"
    return ""


def annotate_stride_rd(rows_df: pd.DataFrame) -> None:
    """In-place annotate _rd_bucket and _stride_bucket columns.

    Uses same bounded-window logic as data/build_windows.py but works on a
    DataFrame. Bounded window fixed at 8192 mem references.
    """
    rd_col = [tk.RD_NONMEM] * len(rows_df)
    st_col = [tk.ST_NONMEM] * len(rows_df)
    last_line: int | None = None
    seen_lines: set[int] = set()
    active_pos: dict[int, int] = {}
    active_queue: deque = deque()
    mem_idx = 0
    rd_window = 8192

    is_load = rows_df["is_load"].to_numpy()
    is_store = rows_df["is_store"].to_numpy()
    is_atom = rows_df["is_atomic"].to_numpy()
    line_arr = rows_df["cacheline_addr"].to_numpy()

    # simple prefix-count "fenwick-lite" for RD: we just count distinct lines
    # visited in the rd_window; that's a good enough approximation for this
    # probe.
    for i in range(len(rows_df)):
        if not (is_load[i] or is_store[i] or is_atom[i]):
            continue
        mem_idx += 1
        line = int(line_arr[i]) if line_arr[i] else None
        # stride
        if last_line is None or line is None:
            st_col[i] = tk.ST_FIRST
        else:
            # stride in cache-line units. cacheline_addr is byte-aligned to 64;
            # normalise to line index.
            delta = (line - last_line) // 64 if abs(line - last_line) >= 64 else (line - last_line)
            st_col[i] = tk.stride_bucket_from_delta(delta)
        # rd
        expire_before = mem_idx - rd_window
        while active_queue and active_queue[0][0] < expire_before:
            old_pos, old_line = active_queue.popleft()
            if active_pos.get(old_line) == old_pos:
                del active_pos[old_line]
        if line is None:
            rd_col[i] = tk.RD_COLD
        else:
            prev = active_pos.get(line)
            if prev is None:
                rd_col[i] = tk.RD_FAR if line in seen_lines else tk.RD_COLD
            else:
                rd = mem_idx - 1 - prev  # approximation: bounded refs between
                rd_col[i] = tk.rd_bucket_from_distance(rd)
            active_pos[line] = mem_idx
            active_queue.append((mem_idx, line))
            seen_lines.add(line)
            last_line = line
    rows_df["_rd_bucket"] = rd_col
    rows_df["_stride_bucket"] = st_col


def render_macro(rows: pd.DataFrame, mode: str = "compact") -> tuple[str, list[str]]:
    """Render one macro as an assembly-like line.

    mode:
      compact - x86 register names + short cache tags (target 4 tok/macro)
      verbose - previous 'r1,[mem] ; L1hit' style (baseline)
    """
    head = rows.iloc[0]
    n_uops = len(rows)

    if int(head.is_call) == 1:
        mnem = "call"
    elif int(head.is_return) == 1:
        mnem = "ret"
    elif int(head.is_branch_indirect) == 1:
        mnem = "jmp"
    elif int(head.is_branch_cond) == 1:
        mnem = "jne"
    elif int(head.is_branch) == 1:
        mnem = "jmp"
    elif int(head.is_atomic) == 1:
        mnem = "lock"
    elif int(head.is_load) == 1 or int(head.op_class) in (56, 58):
        mnem = "mov"
    elif int(head.is_store) == 1 or int(head.op_class) in (57, 59):
        mnem = "mov"
    else:
        oc = int(head.op_class)
        if oc == 1:
            mnem = "add"
        elif oc == 2:
            mnem = "imul"
        elif oc == 3:
            mnem = "idiv"
        elif oc in (4, 7, 8):
            mnem = "addsd"
        elif oc == 9:
            mnem = "divsd"
        elif oc in (5, 6):
            mnem = "cvt"
        elif 12 <= oc <= 24:
            mnem = "padd"
        elif 25 <= oc <= 34:
            mnem = "addps"
        else:
            mnem = op_class_name(oc)

    n_src = int(head.n_src)
    n_dst = int(head.n_dst)

    is_load = int(head.is_load) or int(head.op_class) in (56, 58)
    is_store = int(head.is_store) or int(head.op_class) in (57, 59)
    is_mem = is_load or is_store or int(head.is_atomic) == 1
    is_br = int(head.is_branch) == 1

    if mode == "compact":
        # Format: mnem dst src [tag]* -- no commas or brackets, whitespace only
        dst = reg_name(n_dst if n_dst > 0 else 0)
        src = reg_name(n_src if n_src > 0 else 0)
        if is_load:
            operand = f"{dst} {src}"
        elif is_store:
            operand = f"{dst} {src}"
        elif is_br:
            operand = "L"
        else:
            operand = f"{dst} {src}"

        tags: list[str] = []
        if is_mem:
            pc = path_class_name(int(head.path_class))
            if pc == 0:
                tags.append("L1")
            elif pc == 1:
                tags.append("L2")
            elif pc == 2:
                tags.append("L3")
            elif pc == 3:
                tags.append("dram")
            elif pc == 4:
                tags.append("rem")
            # stride/RD
            st_bkt = int(head["_stride_bucket"]) if "_stride_bucket" in head.index else 0
            rd_bkt = int(head["_rd_bucket"]) if "_rd_bucket" in head.index else 0
            st_tag = stride_to_tag(st_bkt)
            rd_tag = rd_bucket_to_tag(rd_bkt)
            if st_tag:
                tags.append(st_tag)
            if rd_tag:
                tags.append(rd_tag)
            # coherence
            coh = int(head["coh_oracle"]) if "coh_oracle" in head.index else 0
            if coh in (2, 3):
                tags.append("shared")
            if int(head.dtlb_hit) == 0:
                tags.append("tlbm")
        if is_br and int(head.mispredicted) != 0:
            tags.append("mp")
        if n_uops >= 8:
            tags.append(f"x{n_uops}")

        if tags:
            line = f"{mnem} {operand} {' '.join(tags)}"
        else:
            line = f"{mnem} {operand}"
        return line, [mnem]

    # verbose mode: original format
    if is_load:
        op_str = f"r{n_dst},[mem]"
    elif is_store:
        op_str = f"[mem],r{n_src}"
    elif is_br:
        op_str = "lbl"
    else:
        op_str = f"r{max(n_dst,1)},r{max(n_src,1)}"

    ann = []
    if is_mem:
        pc = path_class_name(int(head.path_class))
        if pc == 0:
            ann.append("L1hit")
        elif pc == 1:
            ann.append("L2hit")
        elif pc == 2:
            ann.append("LLChit")
        elif pc == 3:
            ann.append("dram")
        elif pc == 4:
            ann.append("remote")
        if int(head.dtlb_hit) == 0:
            ann.append("tlb_miss")
    if is_br and int(head.mispredicted) != 0:
        ann.append("mispred")
    if n_uops >= 4:
        ann.append(f"uops={n_uops}")

    if ann:
        line = f"{mnem} {op_str} ; {' '.join(ann)}"
    else:
        line = f"{mnem} {op_str}"
    return line, [mnem]


def group_macros(df: pd.DataFrame, max_macros: int) -> list[pd.DataFrame]:
    """Split df into macro-groups. A macro ends at is_last_microop==1 or right
    before macro_pc changes (whichever comes first).
    """
    macros: list[pd.DataFrame] = []
    start = 0
    prev_pc = int(df.iloc[0].macro_pc)
    n = len(df)
    for i in range(n):
        row = df.iloc[i]
        pc = int(row.macro_pc)
        if pc != prev_pc and i > start:
            macros.append(df.iloc[start:i])
            start = i
            prev_pc = pc
            if len(macros) >= max_macros:
                return macros
        else:
            prev_pc = pc
        if int(row.is_last_microop) == 1:
            macros.append(df.iloc[start:i + 1])
            start = i + 1
            if start < n:
                prev_pc = int(df.iloc[start].macro_pc)
            if len(macros) >= max_macros:
                return macros
    if start < n:
        macros.append(df.iloc[start:n])
    return [m for m in macros if len(m) > 0]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--parquet",
        default=(
            "data/raw_v7_seedB_c04_infer17/W_ads_ranking_proxy/tao_trace/"
            "board.processor.cores0.core.tao_trace.tao_trace.aligned.parquet"
        ),
    )
    ap.add_argument("--max-uops", type=int, default=200000,
                    help="only read the first N uop rows")
    ap.add_argument("--max-macros", type=int, default=4096)
    ap.add_argument("--tokenizer",
                    default="Qwen/Qwen3-0.6B-Base",
                    help="HF model id or local path")
    ap.add_argument("--print-lines", type=int, default=20)
    ap.add_argument("--mode", choices=["compact", "verbose"], default="compact")
    args = ap.parse_args()

    print(f"reading {args.parquet}")
    df = pd.read_parquet(args.parquet)
    if args.max_uops and len(df) > args.max_uops:
        df = df.iloc[:args.max_uops].copy()
    print(f"loaded rows={len(df)}")

    annotate_stride_rd(df)
    print("annotated stride/RD buckets")

    macros = group_macros(df, max_macros=args.max_macros)
    print(f"macros={len(macros)}")

    # macro size stats
    sizes = [len(m) for m in macros]
    print(f"macro-size (uops per macro): "
          f"mean={sum(sizes)/len(sizes):.2f} "
          f"min={min(sizes)} max={max(sizes)} "
          f"p50={sorted(sizes)[len(sizes)//2]} "
          f"p90={sorted(sizes)[int(len(sizes)*0.9)]}")

    macros = [m for m in macros if len(m) > 0]
    lines: list[str] = []
    mnemonics: Counter[str] = Counter()
    for m in macros:
        line, mnem = render_macro(m, mode=args.mode)
        lines.append(line)
        mnemonics.update(mnem)
    print(f"unique mnemonics: {len(mnemonics)}")
    print(f"top mnemonics: {mnemonics.most_common(15)}")

    # tokenize with Qwen
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    try:
        from transformers import AutoTokenizer
    except Exception as e:
        print(f"transformers import failed: {e}")
        return 1
    print(f"loading tokenizer {args.tokenizer}")
    tok = AutoTokenizer.from_pretrained(args.tokenizer, trust_remote_code=True)
    unk_id = tok.unk_token_id
    print(f"tokenizer vocab={len(tok)} unk_id={unk_id}")

    # Tokenize each line separately so we get per-macro token count
    per_line_tokens = []
    all_ids: list[int] = []
    n_unk = 0
    for line in lines:
        ids = tok.encode(line, add_special_tokens=False)
        per_line_tokens.append(len(ids))
        all_ids.extend(ids)
        if unk_id is not None:
            n_unk += sum(1 for i in ids if i == unk_id)
    total = len(all_ids)
    print()
    print("===== token stats =====")
    print(f"total tokens        : {total}")
    print(f"total macros        : {len(lines)}")
    print(f"avg tokens / macro  : {total/len(lines):.2f}")
    print(f"unk tokens          : {n_unk} ({100*n_unk/max(total,1):.3f}%)")
    print(f"per-macro token len : "
          f"min={min(per_line_tokens)} "
          f"p50={sorted(per_line_tokens)[len(per_line_tokens)//2]} "
          f"p90={sorted(per_line_tokens)[int(len(per_line_tokens)*0.9)]} "
          f"max={max(per_line_tokens)}")

    # mnemonic-level native-token analysis: for each unique word appearing in
    # the rendered lines, check if it becomes a single token when encoded in
    # isolation. This is a proxy for "Qwen recognises this word as a whole".
    word_freq: Counter[str] = Counter()
    for line in lines:
        # very loose split: keep [ ] , ; ' as separators
        buf = ""
        for ch in line:
            if ch.isalnum() or ch == "_":
                buf += ch
            else:
                if buf:
                    word_freq[buf] += 1
                    buf = ""
        if buf:
            word_freq[buf] += 1
    single_token_words = 0
    multi_token_words = 0
    single_token_occ = 0
    multi_token_occ = 0
    example_multi: list[tuple[str, list[str]]] = []
    for w, c in word_freq.most_common():
        # encode with leading space to reflect natural context
        ids_lead = tok.encode(" " + w, add_special_tokens=False)
        n = len(ids_lead)
        if n == 1:
            single_token_words += 1
            single_token_occ += c
        else:
            multi_token_words += 1
            multi_token_occ += c
            if len(example_multi) < 12:
                pieces = tok.convert_ids_to_tokens(ids_lead)
                example_multi.append((w, pieces))
    total_occ = single_token_occ + multi_token_occ
    print()
    print("===== word-level nativeness =====")
    print(f"unique words         : {len(word_freq)}")
    print(f"single-token words   : {single_token_words}")
    print(f"multi-token words    : {multi_token_words}")
    if total_occ > 0:
        print(f"single-token coverage: "
              f"{100*single_token_occ/total_occ:.2f}% of word occurrences")
    if example_multi:
        print("multi-token examples (word -> pieces):")
        for w, pieces in example_multi:
            print(f"  {w!r:16s} -> {pieces}")

    print()
    print(f"===== first {args.print_lines} rendered macros =====")
    for i, line in enumerate(lines[:args.print_lines]):
        ids = tok.encode(line, add_special_tokens=False)
        pieces = tok.convert_ids_to_tokens(ids)
        print(f"[{i:03d}] ({len(ids):2d} tok) {line}")
        print(f"       -> {pieces}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
