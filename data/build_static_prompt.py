"""build_static_prompt.py — Phase 1 static-block prompt generator.

Reads ``data/v28_1/static_dict/*.parquet`` (Phase 0 artifact) and generates a
per-basic-block canonical prompt in one of four semantic-gate variants:

  real          — actual x86 mnemonic + operands from objdump
  pseudo        — mnemonic reconstructed from op_class (same synthesis path as
                  the v22 native renderer; we treat this as the "pseudo-asm"
                  control)
  shuffle       — mnemonic permutation-shuffle within the block (frequency
                  matched but structure destroyed)
  register_rename — semantics-preserving register rename (rax↔rbx etc.)

Output layout:
  <out>/<binary_hash>/<variant>/bb_<bb_id>.txt   canonical prompt text
  <out>/<binary_hash>/<variant>/index.parquet    (bb_id, module_pc_start, n_macros, prompt_path, sha1)

The prompt intentionally omits workload names, absolute PCs, seeds, cfg
hashes, and any oracle timing/coherence field. Block-local labels ``B{bb_id}``
replace absolute addresses so the model cannot memorize call-target PCs.

Usage:
  /data00/yinhaolang/infer/.venv/bin/python data/build_static_prompt.py \\
      --static-dict-dir data/v28_1/static_dict \\
      --out data/v28_1/prompts \\
      --variant real,pseudo,shuffle,register_rename
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import random
import re
import sys
import time
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

try:
    import pyarrow as pa
    import pyarrow.parquet as pq
except ImportError as exc:  # pragma: no cover
    raise RuntimeError(
        "pyarrow is required; run with /data00/yinhaolang/infer/.venv/bin/python"
    ) from exc


VARIANTS = ("real", "pseudo", "shuffle", "register_rename")

# gem5 OpClass -> pseudo mnemonic used by the v22 native renderer.  This is
# intentionally the same crude fallback so we can measure the gap between
# "pseudo asm" and "real asm".  See LLMSim/model/tokenizer.py :: OP_TO_MNEMONIC.
PSEUDO_MNEMONIC = {
    0: "nop",
    1: "add", 2: "add", 3: "add", 4: "add",
    5: "imul", 6: "imul",
    7: "idiv", 8: "idiv",
    10: "fadd", 11: "fadd", 12: "fmul",
    13: "fdiv",
    30: "vaddps", 31: "vmulps",
    40: "vshufps",
    56: "mov",   # load-like
    57: "mov",   # store-like
    88: "mfence",
}
REG_POOL = ["rax", "rbx", "rcx", "rdx", "rsi", "rdi", "r8", "r9", "r10",
            "r11", "r12", "r13", "r14", "r15", "rbp", "rsp"]

# semantics-preserving register rename permutation (see gate 13.2 of the
# design doc; applies to the whole block consistently)
RENAME_MAP = {
    "rax": "rbx", "rbx": "rax", "rcx": "rdx", "rdx": "rcx",
    "rsi": "rdi", "rdi": "rsi", "r8": "r9", "r9": "r8",
    "r10": "r11", "r11": "r10", "r12": "r13", "r13": "r12",
    "r14": "r15", "r15": "r14",
    "eax": "ebx", "ebx": "eax", "ecx": "edx", "edx": "ecx",
    "esi": "edi", "edi": "esi",
}
_REG_TOKEN_RE = re.compile(r"%?\b(r[a-z0-9]+|e[a-z]+x|[re][ds]i|[re]bp|[re]sp|r\d+[bwd]?)\b",
                            re.IGNORECASE)


def _rename_operands(op: str) -> str:
    def repl(match: "re.Match[str]") -> str:
        pref = "%" if match.group(0).startswith("%") else ""
        name = match.group(1).lower()
        renamed = RENAME_MAP.get(name, name)
        return f"{pref}{renamed}"
    return _REG_TOKEN_RE.sub(repl, op)


def _canonical_address(op: str, pc: int, bb_index: Dict[int, int]) -> str:
    """Replace absolute branch targets with block-local labels B{bb_id}."""
    def repl(match: "re.Match[str]") -> str:
        try:
            target = int(match.group(0), 16)
        except ValueError:
            return match.group(0)
        bb_id = bb_index.get(target, None)
        if bb_id is None:
            return f"B?"
        return f"B{bb_id}"
    return re.sub(r"\b0x[0-9a-fA-F]+\b", repl, op)


# ---------------------------------------------------------------------------
# Per-binary prompt build
# ---------------------------------------------------------------------------

def _load_binary_rows(parquet_path: str) -> List[dict]:
    tbl = pq.read_table(parquet_path).to_pydict()
    n = len(tbl["module_pc"])
    return [
        {k: tbl[k][i] for k in tbl}
        for i in range(n)
    ]


def _group_by_bb(rows: List[dict]) -> Dict[int, List[dict]]:
    out: Dict[int, List[dict]] = defaultdict(list)
    for r in rows:
        out[int(r["bb_id"])].append(r)
    for k in out:
        out[k].sort(key=lambda x: int(x["module_pc"]))
    return dict(out)


def _first_pc_per_bb(bb_map: Dict[int, List[dict]]) -> Dict[int, int]:
    return {bb_id: int(rows[0]["module_pc"]) for bb_id, rows in bb_map.items()}


def _pc_to_bb(bb_map: Dict[int, List[dict]]) -> Dict[int, int]:
    """PC (start of a block) -> bb_id, so branch targets can be relabeled."""
    return {int(rows[0]["module_pc"]): bb_id for bb_id, rows in bb_map.items()}


def _render_real_block(rows: List[dict], bb_index: Dict[int, int]) -> List[str]:
    lines = []
    for r in rows:
        m = str(r["mnemonic"]).lower()
        op = str(r["operands"] or "").strip()
        op = _canonical_address(op, int(r["module_pc"]), bb_index)
        lines.append(f"  {m} {op}".rstrip())
    return lines


def _render_pseudo_block(rows: List[dict], bb_index: Dict[int, int]) -> List[str]:
    """Reconstruct mnemonic from op_class (needs external op_class per PC).

    Since the static dict does not carry op_class (that only exists at the
    dynamic uop level), the pseudo variant here degrades to a fixed
    "mov <reg>, [addr]" template for load-like macros and "add <reg>, <reg>"
    for compute-like macros. This is intentionally low information so we can
    measure the semantic-gate delta of real assembly.
    """
    lines = []
    rnd = random.Random(int(rows[0]["module_pc"]) & 0xFFFFFFFF)
    for r in rows:
        # Very crude classification from the real mnemonic (branch/call/ret
        # preserved because those matter for CFG; other ops become mov/add).
        m = str(r["mnemonic"]).lower()
        if bool(r.get("is_call")):
            op = _canonical_address(str(r["operands"] or ""), int(r["module_pc"]), bb_index)
            lines.append(f"  call {op}")
        elif bool(r.get("is_return")):
            lines.append("  ret")
        elif bool(r.get("is_branch")):
            op = _canonical_address(str(r["operands"] or ""), int(r["module_pc"]), bb_index)
            lines.append(f"  jmp {op}")
        else:
            reg = rnd.choice(REG_POOL)
            other = rnd.choice(REG_POOL)
            if m.startswith("mov") or m.startswith("push") or m.startswith("pop"):
                lines.append(f"  mov {reg}, [{other}]")
            elif m.startswith("cmp") or m.startswith("test"):
                lines.append(f"  cmp {reg}, {other}")
            else:
                lines.append(f"  add {reg}, {other}")
    return lines


def _render_shuffle_block(rows: List[dict], bb_index: Dict[int, int]) -> List[str]:
    """Frequency-preserving mnemonic shuffle within the block."""
    real = _render_real_block(rows, bb_index)
    rnd = random.Random(sum(int(r["module_pc"]) for r in rows) & 0xFFFFFFFF)
    order = list(range(len(real)))
    rnd.shuffle(order)
    return [real[i] for i in order]


def _render_rename_block(rows: List[dict], bb_index: Dict[int, int]) -> List[str]:
    lines = []
    for r in rows:
        m = str(r["mnemonic"]).lower()
        op = str(r["operands"] or "").strip()
        op = _rename_operands(op)
        op = _canonical_address(op, int(r["module_pc"]), bb_index)
        lines.append(f"  {m} {op}".rstrip())
    return lines


VARIANT_RENDERERS = {
    "real": _render_real_block,
    "pseudo": _render_pseudo_block,
    "shuffle": _render_shuffle_block,
    "register_rename": _render_rename_block,
}


def _write_bb(out_dir: str, bb_id: int, lines: List[str]) -> Tuple[str, str]:
    body = "\n".join(lines) + "\n"
    header = f"B{bb_id}:\n"
    text = header + body
    path = os.path.join(out_dir, f"bb_{bb_id:06d}.txt")
    sha = hashlib.sha1(text.encode()).hexdigest()[:16]
    with open(path, "w") as fh:
        fh.write(text)
    return path, sha


def build_binary_prompts(parquet_path: str, out_root: str,
                         variants: Sequence[str]) -> Dict[str, dict]:
    rows = _load_binary_rows(parquet_path)
    binary_hash = str(rows[0]["binary_hash"])
    bb_map = _group_by_bb(rows)
    bb_index = _pc_to_bb(bb_map)
    reports: Dict[str, dict] = {}
    for variant in variants:
        if variant not in VARIANT_RENDERERS:
            raise ValueError(f"unknown variant {variant}")
        v_dir = os.path.join(out_root, binary_hash, variant)
        os.makedirs(v_dir, exist_ok=True)
        renderer = VARIANT_RENDERERS[variant]
        idx_rows: List[dict] = []
        for bb_id, block_rows in sorted(bb_map.items()):
            lines = renderer(block_rows, bb_index)
            path, sha = _write_bb(v_dir, bb_id, lines)
            idx_rows.append({
                "binary_hash": binary_hash,
                "bb_id": int(bb_id),
                "n_macros": int(len(block_rows)),
                "module_pc_start": int(block_rows[0]["module_pc"]),
                "prompt_path": path,
                "prompt_sha1": sha,
            })
        idx_path = os.path.join(v_dir, "index.parquet")
        cols = {k: [r[k] for r in idx_rows] for k in idx_rows[0].keys()}
        tmp = idx_path + ".tmp"
        pq.write_table(pa.table(cols), tmp, compression="zstd")
        os.replace(tmp, idx_path)
        reports[variant] = {
            "n_blocks": len(idx_rows),
            "index_parquet": idx_path,
        }
    return {"binary_hash": binary_hash, "variants": reports}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--static-dict-dir",
                    default="/data00/yinhaolang/LLMSim/data/v28_1/static_dict")
    ap.add_argument("--out",
                    default="/data00/yinhaolang/LLMSim/data/v28_1/prompts")
    ap.add_argument("--variant", default=",".join(VARIANTS),
                    help="comma-separated variants to build")
    args = ap.parse_args()

    variants = [v.strip() for v in args.variant.split(",") if v.strip()]
    for v in variants:
        if v not in VARIANT_RENDERERS:
            raise SystemExit(f"unknown variant: {v}")
    parquets = sorted(glob.glob(os.path.join(args.static_dict_dir, "*.parquet")))
    if not parquets:
        raise SystemExit(f"no static_dict parquets under {args.static_dict_dir}")
    os.makedirs(args.out, exist_ok=True)
    manifest_rows: List[dict] = []
    print(f"[build_static_prompt] {len(parquets)} binaries -> {args.out}", flush=True)
    for p in parquets:
        t0 = time.time()
        rep = build_binary_prompts(p, args.out, variants)
        dt = time.time() - t0
        rep["parquet"] = p
        rep["elapsed_s"] = dt
        manifest_rows.append(rep)
        vs = ", ".join(f"{k}={v['n_blocks']}" for k, v in rep["variants"].items())
        print(f"  {rep['binary_hash'][:12]}  {vs}  dt={dt:.1f}s", flush=True)
    with open(os.path.join(args.out, "manifest.jsonl"), "w") as fh:
        for r in manifest_rows:
            fh.write(json.dumps(r) + "\n")
    print(f"[build_static_prompt] manifest -> {os.path.join(args.out, 'manifest.jsonl')}",
          flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
