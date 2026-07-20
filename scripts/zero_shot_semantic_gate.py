"""Zero-shot semantic gate for v24 macro assembly input.

Purpose:
  Verify whether Qwen2.5-Coder-1.5B-Instruct can identify performance
  bottlenecks from our macro-assembly representation. This is the gate
  we must pass before investing in v24 training.

Usage:
  /data00/yinhaolang/infer/.venv/bin/python scripts/zero_shot_semantic_gate.py

Env overrides (all optional):
  MODEL=Qwen/Qwen2.5-Coder-1.5B-Instruct
  DEVICE=auto            # auto | cuda | cpu
  MAX_NEW=280
  TEMP=0.2

Pass criteria (manual read):
  * pointer_chase case -> mentions memory-bound / cache miss / pointer chasing
  * branch_heavy case  -> mentions branch mispredict / branch heavy
  * compute_bound case -> mentions compute-bound / ALU / integer / FP throughput
  * mixed case         -> either "mixed" or a plausible dominant factor

If 3 of 4 read reasonably, gate passes. If <=1 answers plausibly, drop v24.
"""

from __future__ import annotations

import os
import sys
import textwrap
import time
from dataclasses import dataclass


SYSTEM_PROMPT = textwrap.dedent("""\
    You are a senior CPU performance analyst.
    You will be shown a short x86-style macro assembly trace with light
    functional tags:
      * stride tags: same, seq, str, far, rnd
      * reuse tags:  hot, warm, mid, cool, cold
      * capital letters like A, B, C denote hot cache lines shared across cores.
    You must identify the dominant performance bottleneck and briefly justify.
    Prefer these labels when applicable: memory-bound, branch-bound,
    compute-bound, mixed. Also mention specific effects such as pointer
    chasing, cache miss, branch mispredict, ILP-limited, divider-bound, etc.
    Keep the answer under 120 words.
""")


@dataclass
class Case:
    name: str
    expect: str
    trace: str


CASES = [
    Case(
        name="pointer_chase",
        expect="memory-bound, pointer chasing, dram/llc miss",
        trace=textwrap.dedent("""\
            cfg cores=1 clk=3G l1=32K l2=512K l3=16M rob=192 mshr=16
            target uops=1024 macros=512 mem=hi br=lo ld=430 st=6 atom=0
            asm
            mov rax rbx rnd cold
            mov rax rax rnd cold
            mov rax rax rnd cold
            mov rax rax rnd cold
            add rcx rax
            mov rax rax rnd cold
            mov rax rax rnd cold
            mov rax rax rnd cold
            cmp rax rcx
            jne L
            mov rax rax rnd cold
            mov rax rax rnd cold
            mov rax rax rnd cold
        """),
    ),
    Case(
        name="branch_heavy",
        expect="branch-bound, branch mispredict",
        trace=textwrap.dedent("""\
            cfg cores=1 clk=3G l1=32K l2=512K l3=16M rob=192 bpen=15
            target uops=1024 macros=520 mem=lo br=hi ld=40 st=8 atom=0
            asm
            add rax rbx
            cmp rax rcx
            jne L
            xor rdx rdx
            cmp rdx rax
            jne L
            add rbx rax
            cmp rbx rcx
            jne L
            imul rax rbx
            cmp rax rcx
            jne L
            add rsi rdi
            cmp rsi rax
            jne L
        """),
    ),
    Case(
        name="compute_bound",
        expect="compute-bound, ILP or divider or FP throughput",
        trace=textwrap.dedent("""\
            cfg cores=1 clk=3G l1=32K l2=512K l3=16M rob=192
            target uops=1024 macros=512 mem=lo br=lo ld=8 st=4 atom=0
            asm
            imul rax rbx
            imul rax rcx
            imul rax rdx
            imul rax rsi
            imul rax rdi
            idiv rbx
            imul rax rbx
            imul rax rcx
            imul rax rdx
            idiv rcx
            imul rax rbx
            imul rax rcx
            imul rax rdx
            imul rax rsi
        """),
    ),
    Case(
        name="mixed_shared",
        expect="mixed with false-sharing or coherence hint",
        trace=textwrap.dedent("""\
            cfg cores=4 clk=3G l1=32K l2=512K l3=16M mshr=16
            cores
            c0 mem=md br=lo hot=A
            c1 mem=md br=lo hot=A
            c2 mem=md br=lo hot=A
            c3 mem=md br=lo hot=A
            target c0 uops=1024 macros=512 mem=md br=lo ld=180 st=140 atom=0
            asm
            add rax rbx
            mov rax rbx same hot A
            st rax rbx same hot A
            add rcx rax
            mov rdx rbx same hot A
            st rdx rbx same hot A
            imul rax rbx
            mov rax rbx same hot A
            st rax rbx same hot A
            cmp rax rcx
            jne L
            mov rax rbx same hot A
            st rax rbx same hot A
        """),
    ),
]


def build_messages(system: str, user: str) -> list[dict]:
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def format_prompt(tok, messages: list[dict]) -> str:
    return tok.apply_chat_template(
        messages, tokenize=False, add_generation_prompt=True
    )


def main() -> int:
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    model_name = os.environ.get("MODEL", "Qwen/Qwen2.5-Coder-3B-Instruct")
    device_pref = os.environ.get("DEVICE", "auto").lower()
    max_new = int(os.environ.get("MAX_NEW", "280"))
    temp = float(os.environ.get("TEMP", "0.2"))

    if device_pref == "cuda" and not torch.cuda.is_available():
        print("[warn] DEVICE=cuda requested but CUDA unavailable, falling back to cpu",
              file=sys.stderr)
    use_cuda = torch.cuda.is_available() and device_pref != "cpu"
    device = "cuda" if use_cuda else "cpu"
    dtype = torch.bfloat16 if use_cuda else torch.float32

    print(f"[gate] model = {model_name}")
    print(f"[gate] device = {device}, dtype = {dtype}")
    print(f"[gate] max_new = {max_new}, temperature = {temp}")

    os.environ.setdefault("HF_HUB_ENABLE_HF_TRANSFER", "1")

    t0 = time.time()
    tok = AutoTokenizer.from_pretrained(model_name, trust_remote_code=True)
    if tok.pad_token is None:
        tok.pad_token = tok.eos_token
    model = AutoModelForCausalLM.from_pretrained(
        model_name,
        torch_dtype=dtype,
        device_map=device if use_cuda else None,
        trust_remote_code=True,
    )
    if not use_cuda:
        model = model.to("cpu")
    model.eval()
    print(f"[gate] loaded in {time.time() - t0:.1f}s")

    for case in CASES:
        messages = build_messages(SYSTEM_PROMPT, case.trace.rstrip())
        prompt = format_prompt(tok, messages)
        inputs = tok(prompt, return_tensors="pt").to(model.device)

        gen_kwargs = dict(
            max_new_tokens=max_new,
            do_sample=temp > 0.0,
            temperature=max(temp, 1e-5),
            top_p=0.9,
            repetition_penalty=1.05,
            pad_token_id=tok.pad_token_id,
        )

        with torch.no_grad():
            t1 = time.time()
            out = model.generate(**inputs, **gen_kwargs)
            dt = time.time() - t1

        completion_ids = out[0, inputs["input_ids"].shape[1]:]
        completion = tok.decode(completion_ids, skip_special_tokens=True).strip()

        print("=" * 78)
        print(f"[case] {case.name}    (expect: {case.expect})")
        print(f"[gen ] {dt:.1f}s, {completion_ids.shape[0]} tokens")
        print("-" * 78)
        print("TRACE:")
        print(textwrap.indent(case.trace.rstrip(), "  "))
        print("-" * 78)
        print("MODEL:")
        print(textwrap.indent(completion or "(empty)", "  "))
        print()

    print("=" * 78)
    print("[gate] done. Manual read: 3/4 plausible => pass; <=1 => drop v24.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
