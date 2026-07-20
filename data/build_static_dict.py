"""build_static_dict.py — v28_1 static disassembly dictionary builder.

Reads TSim v28 workload ELF binaries and produces a per-binary static dictionary
mapping ``module_pc -> real x86 macro instruction`` via ``objdump -d``. This is
Phase 0 artifact #1 in ``docs/LLM语义建模方案.md`` §5. It is macro-level
(each row is one x86 macro instruction, not a gem5 microop) so it can be joined
with the v28_1 aligned parquet ``macro_pc`` column.

Contract:
  input:  --workload-bin  /data00/yinhaolang/TSim/workloads/bin
          --binary        one or more v28_* ELF filenames (default: all v28_*)
  output: <out>/<binary_hash>.parquet with columns
            binary_hash, module_pc, size_bytes, bytes_hex,
            mnemonic, operands, is_branch, is_call, is_return,
            branch_relative_target, bb_id, cfg_next, cfg_taken
  meta:   <out>/manifest.jsonl (one row per binary with sha1/path/timestamp)

Gate: ``python -m data.build_static_dict --verify --sample 1000`` re-disassembles
random rows with objdump and requires 100% agreement, else exits 2.
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import random
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

try:
    import pyarrow as pa
    import pyarrow.parquet as pq
except ImportError as exc:  # pragma: no cover
    raise RuntimeError(
        "pyarrow is required; run with /data00/yinhaolang/infer/.venv/bin/python"
    ) from exc


STATIC_DICT_SCHEMA_VERSION = "real-x86-objdump-wide-v2"

OBJDUMP_LINE_RE = re.compile(
    r"^\s*(?P<pc>[0-9a-f]+):\s+(?P<mnemonic>[a-z][a-z0-9\.]*)\s*(?P<operands>[^#\n]*?)\s*(?:#.*)?$",
    re.IGNORECASE,
)
OBJDUMP_LINE_WITH_BYTES_RE = re.compile(
    r"^\s*(?P<pc>[0-9a-f]+):\s+(?P<bytes>(?:[0-9a-f]{2}\s+)+)\s*"
    r"(?P<mnemonic>\(bad\)|[a-z\.][a-z0-9\.]*)\s*"
    r"(?P<operands>[^#\n]*?)\s*(?:#.*)?$",
    re.IGNORECASE,
)
SECTION_HEADER_RE = re.compile(r"^Disassembly of section (?P<sect>\S+):")
FUNC_HEADER_RE = re.compile(r"^[0-9a-f]+ <(?P<name>[^>]+)>:\s*$")
SECTION_ROW_RE = re.compile(
    r"^\s*\d+\s+(?P<name>\S+)\s+(?P<size>[0-9a-fA-F]+)\s+"
    r"(?P<vma>[0-9a-fA-F]+)\s+[0-9a-fA-F]+\s+"
    r"(?P<file_offset>[0-9a-fA-F]+)\s+"
)
BRANCH_MNEMONICS = {
    "jmp", "je", "jne", "jz", "jnz", "js", "jns", "jc", "jnc",
    "jo", "jno", "jp", "jnp", "jpe", "jpo",
    "ja", "jae", "jb", "jbe", "jg", "jge", "jl", "jle",
    "jcxz", "jecxz", "jrcxz", "loop", "loope", "loopne",
    "jmpq", "jz", "callq", "call", "retq", "ret",
}
CALL_MNEMONICS = {"call", "callq"}
RET_MNEMONICS = {"ret", "retq", "retf"}
COND_BRANCH_PREFIX = ("j", "loop")


@dataclass
class MacroRow:
    section_name: str
    decode_source: str
    module_pc: int
    size_bytes: int
    bytes_hex: str
    mnemonic: str
    operands: str
    is_branch: bool
    is_call: bool
    is_return: bool
    branch_relative_target: int
    bb_id: int
    cfg_next: int
    cfg_taken: int


@dataclass(frozen=True)
class ExecutableSection:
    name: str
    size: int
    vma: int
    file_offset: int


def sha1_file(path: str) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 16), b""):
            h.update(block)
    return h.hexdigest()


def run_objdump(binary_path: str, extra_args: Sequence[str] = ()) -> str:
    # -w is non-negotiable: without it objdump wraps long instruction bytes
    # onto a continuation line.  The old parser then emitted size=0 rows and
    # never observed instructions longer than seven bytes.
    args = ["objdump", "-d", "-w", "-M", "intel", *extra_args, binary_path]
    try:
        out = subprocess.run(
            args,
            check=True, capture_output=True, text=True, timeout=600,
        )
    except subprocess.CalledProcessError as exc:  # pragma: no cover
        raise RuntimeError(f"objdump failed on {binary_path}: {exc.stderr}") from exc
    return out.stdout


def executable_sections(binary_path: str) -> Tuple[ExecutableSection, ...]:
    """Read executable section VMA/file offsets without a Python ELF package."""
    proc = subprocess.run(
        ["objdump", "-h", binary_path],
        check=True, capture_output=True, text=True, timeout=60,
    )
    lines = proc.stdout.splitlines()
    sections: List[ExecutableSection] = []
    for index, line in enumerate(lines[:-1]):
        match = SECTION_ROW_RE.match(line)
        if not match:
            continue
        flags = lines[index + 1]
        if "CODE" not in flags or "CONTENTS" not in flags:
            continue
        sections.append(ExecutableSection(
            name=str(match.group("name")),
            size=int(match.group("size"), 16),
            vma=int(match.group("vma"), 16),
            file_offset=int(match.group("file_offset"), 16),
        ))
    if not sections:
        raise RuntimeError(f"no executable sections found in {binary_path}")
    return tuple(sections)


def raw_instruction_bytes(
    binary_path: str,
    sections: Sequence[ExecutableSection],
    pc: int,
    size: int,
) -> bytes:
    for section in sections:
        if section.vma <= pc and pc + size <= section.vma + section.size:
            offset = section.file_offset + (pc - section.vma)
            with open(binary_path, "rb") as handle:
                handle.seek(offset)
                data = handle.read(size)
            if len(data) != size:
                raise RuntimeError(
                    f"short binary read at pc=0x{pc:x}: {len(data)} != {size}"
                )
            return data
    raise RuntimeError(
        f"pc range 0x{pc:x}..0x{pc + size:x} is outside executable sections"
    )


def _parse_branch_target(operands: str, pc: int) -> int:
    op = operands.strip().split()
    if not op:
        return -1
    first = op[0].rstrip(",")
    if first.startswith("0x"):
        try:
            return int(first, 16)
        except ValueError:
            return -1
    if re.fullmatch(r"[0-9a-f]+", first):
        try:
            return int(first, 16)
        except ValueError:
            return -1
    return -1


def parse_objdump(text: str) -> Iterable[MacroRow]:
    section_name = ""
    bb_id = -1
    prev_was_branch = True
    prev_pc = -1
    prev_size = 0
    prev_row: Optional[MacroRow] = None
    for line in text.splitlines():
        section_match = SECTION_HEADER_RE.match(line)
        if section_match:
            section_name = str(section_match.group("sect"))
            bb_id += 1
            prev_was_branch = True
            continue
        m_func = FUNC_HEADER_RE.match(line)
        if m_func:
            bb_id += 1
            prev_was_branch = True
            continue
        if not line.strip():
            continue
        m = OBJDUMP_LINE_WITH_BYTES_RE.match(line)
        if not m:
            # Wide objdump output must always carry raw bytes.  Falling back
            # to a no-bytes regex caused the first byte to be parsed as the
            # mnemonic whenever objdump rendered `(bad)` or `.byte`.
            continue
        pc_hex = m.group("pc")
        try:
            pc = int(pc_hex, 16)
        except ValueError:
            continue
        mnemonic = m.group("mnemonic").lower()
        operands = (m.group("operands") or "").strip()
        raw_bytes = m.groupdict().get("bytes", "") or ""
        bytes_hex = "".join(raw_bytes.split())
        size_bytes = len(bytes_hex) // 2
        if mnemonic == "(bad)":
            mnemonic = ".byte"
            operands = ", ".join(
                f"0x{bytes_hex[index:index + 2]}"
                for index in range(0, len(bytes_hex), 2)
            )
        # basic block boundary: after any control-flow, start a new bb
        is_call = mnemonic in CALL_MNEMONICS
        is_return = mnemonic in RET_MNEMONICS
        is_branch = (
            is_call or is_return
            or mnemonic in BRANCH_MNEMONICS
            or (mnemonic.startswith(COND_BRANCH_PREFIX)
                and mnemonic not in {"jecxz"})  # allow jecxz above
        )
        if prev_was_branch:
            bb_id += 1
        target = _parse_branch_target(operands, pc) if is_branch else -1
        row = MacroRow(
            section_name=section_name,
            decode_source="linear_objdump",
            module_pc=pc,
            size_bytes=size_bytes,
            bytes_hex=bytes_hex,
            mnemonic=mnemonic,
            operands=operands,
            is_branch=is_branch,
            is_call=is_call,
            is_return=is_return,
            branch_relative_target=int(target),
            bb_id=int(bb_id),
            cfg_next=-1,
            cfg_taken=int(target),
        )
        if prev_row is not None:
            # fill previous row's cfg_next with this row's pc (fall-through)
            prev_row.cfg_next = int(pc)
            yield prev_row
        prev_row = row
        prev_pc = pc
        prev_size = size_bytes
        prev_was_branch = is_branch
    if prev_row is not None:
        yield prev_row


def write_parquet(rows: Sequence[MacroRow], out_path: str, binary_hash: str) -> None:
    cols = {
        "schema_version": [STATIC_DICT_SCHEMA_VERSION] * len(rows),
        "binary_hash": [binary_hash] * len(rows),
        "section_name": [r.section_name for r in rows],
        "decode_source": [r.decode_source for r in rows],
        "module_pc": [int(r.module_pc) for r in rows],
        "size_bytes": [int(r.size_bytes) for r in rows],
        "bytes_hex": [r.bytes_hex for r in rows],
        "mnemonic": [r.mnemonic for r in rows],
        "operands": [r.operands for r in rows],
        "is_branch": [bool(r.is_branch) for r in rows],
        "is_call": [bool(r.is_call) for r in rows],
        "is_return": [bool(r.is_return) for r in rows],
        "branch_relative_target": [int(r.branch_relative_target) for r in rows],
        "bb_id": [int(r.bb_id) for r in rows],
        "cfg_next": [int(r.cfg_next) for r in rows],
        "cfg_taken": [int(r.cfg_taken) for r in rows],
    }
    tbl = pa.table(cols)
    tmp_path = out_path + ".tmp"
    pq.write_table(tbl, tmp_path, compression="zstd")
    os.replace(tmp_path, out_path)


def validate_rows_against_binary(
    rows: Sequence[MacroRow],
    binary_path: str,
) -> None:
    sections = executable_sections(binary_path)
    seen = set()
    for row in rows:
        if row.module_pc in seen:
            raise RuntimeError(f"duplicate instruction pc 0x{row.module_pc:x}")
        seen.add(row.module_pc)
        if not 1 <= row.size_bytes <= 15:
            raise RuntimeError(
                f"illegal x86 instruction size at 0x{row.module_pc:x}: "
                f"{row.size_bytes}"
            )
        if len(row.bytes_hex) != 2 * row.size_bytes:
            raise RuntimeError(
                f"bytes/size mismatch at 0x{row.module_pc:x}: "
                f"{row.bytes_hex!r} vs {row.size_bytes}"
            )
        raw = raw_instruction_bytes(
            binary_path, sections, row.module_pc, row.size_bytes,
        )
        if raw.hex() != row.bytes_hex.lower():
            raise RuntimeError(
                f"ELF byte mismatch at 0x{row.module_pc:x}: "
                f"objdump={row.bytes_hex} elf={raw.hex()}"
            )


def recover_instruction_at_pc(binary_path: str, pc: int) -> MacroRow:
    """Decode one dynamic macro PC even if linear objdump lost alignment."""
    text = run_objdump(
        binary_path,
        extra_args=(
            f"--start-address=0x{int(pc):x}",
            f"--stop-address=0x{int(pc) + 16:x}",
        ),
    )
    for row in parse_objdump(text):
        if int(row.module_pc) == int(pc):
            row.decode_source = "targeted_dynamic_pc"
            row.bb_id = -1
            row.cfg_next = -1
            row.cfg_taken = int(row.branch_relative_target)
            return row
    raise RuntimeError(f"targeted objdump could not decode pc 0x{int(pc):x}")


def build_binary(
    binary_path: str,
    out_dir: str,
    *,
    force: bool = False,
    required_pcs: Sequence[int] = (),
) -> Tuple[str, str, int]:
    binary_hash = sha1_file(binary_path)
    out_path = os.path.join(out_dir, f"{binary_hash}.parquet")
    if os.path.exists(out_path) and not force:
        # count rows
        n = pq.ParquetFile(out_path).metadata.num_rows
        return binary_hash, out_path, int(n)
    text = run_objdump(binary_path)
    rows = list(parse_objdump(text))
    if not rows:
        raise RuntimeError(f"no rows parsed from {binary_path}")
    by_pc = {int(row.module_pc): row for row in rows}
    for pc in sorted({int(value) for value in required_pcs}):
        existing = by_pc.get(pc)
        if existing is None or existing.mnemonic in {"", "(bad)", ".byte"}:
            by_pc[pc] = recover_instruction_at_pc(binary_path, pc)
    rows = sorted(by_pc.values(), key=lambda row: int(row.module_pc))
    validate_rows_against_binary(rows, binary_path)
    os.makedirs(out_dir, exist_ok=True)
    write_parquet(rows, out_path, binary_hash)
    return binary_hash, out_path, len(rows)


def resolve_binaries(bin_root: str, names: Sequence[str]) -> List[Tuple[str, str]]:
    if names:
        selected = [os.path.join(bin_root, n) for n in names]
    else:
        selected = sorted(glob.glob(os.path.join(bin_root, "v28_*")))
        selected = [p for p in selected if os.path.isfile(p)]
    if not selected:
        raise FileNotFoundError(f"no v28_* binaries under {bin_root}")
    out = []
    for p in selected:
        if not os.path.isfile(p):
            raise FileNotFoundError(p)
        out.append((os.path.basename(p), p))
    return out


def verify_roundtrip(dict_dir: str, sample: int, seed: int = 0) -> Tuple[int, int]:
    """Re-parse the full objdump output (same command as build) and compare
    against the stored dictionary. Using the same command avoids narrow-range
    boundary artifacts (prefixes like ``rex.w`` / ``data16`` getting split off).
    Returns (n_checked, n_matched)."""
    rng = random.Random(seed)
    manifest_path = os.path.join(dict_dir, "manifest.jsonl")
    if not os.path.isfile(manifest_path):
        raise FileNotFoundError(manifest_path)
    n_check = 0
    n_ok = 0
    with open(manifest_path, "r") as fh:
        entries = [json.loads(line) for line in fh if line.strip()]
    for ent in entries:
        parq = ent["parquet"]
        binp = ent["binary_path"]
        tbl = pq.read_table(
            parq,
            columns=[
                "module_pc", "size_bytes", "bytes_hex", "mnemonic", "operands",
                "decode_source",
            ],
        )
        pcs = tbl["module_pc"].to_pylist()
        sizes = tbl["size_bytes"].to_pylist()
        bytes_hex = tbl["bytes_hex"].to_pylist()
        mnems = tbl["mnemonic"].to_pylist()
        opers = tbl["operands"].to_pylist()
        decode_sources = tbl["decode_source"].to_pylist()
        # Re-parse the FULL objdump output; this is the authoritative source.
        text = run_objdump(binp)
        parsed: Dict[int, Tuple[str, str]] = {}
        for row in parse_objdump(text):
            parsed[int(row.module_pc)] = (row.mnemonic, row.operands.strip())
        sections = executable_sections(binp)
        idxs = list(range(len(pcs)))
        rng.shuffle(idxs)
        take = idxs[: max(1, min(sample, len(idxs)))]
        for i in take:
            pc = int(pcs[i])
            n_check += 1
            if str(decode_sources[i]) == "targeted_dynamic_pc":
                recovered = recover_instruction_at_pc(binp, pc)
                got = (recovered.mnemonic, recovered.operands.strip())
            else:
                got = parsed.get(pc)
            if got is None:
                continue
            raw = raw_instruction_bytes(
                binp, sections, pc, int(sizes[i]),
            ).hex()
            if (
                got[0] == str(mnems[i]).lower()
                and got[1] == str(opers[i]).strip()
                and raw == str(bytes_hex[i]).lower()
                and 1 <= int(sizes[i]) <= 15
            ):
                n_ok += 1
    return n_check, n_ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workload-bin", default="/data00/yinhaolang/TSim/workloads/bin")
    ap.add_argument("--binary", action="append", default=[],
                    help="specific v28_* binary basename; may be repeated")
    ap.add_argument("--out", default="/data00/yinhaolang/LLMSim/data/v28_1/static_dict")
    ap.add_argument("--verify", action="store_true",
                    help="run objdump roundtrip agreement check after build")
    ap.add_argument("--force", action="store_true",
                    help="rebuild even when the hash-named parquet exists")
    ap.add_argument(
        "--required-pcs-npy", action="append", default=[],
        help="dynamic macro_pc.npy whose unique PCs must be targeted-decoded",
    )
    ap.add_argument("--sample", type=int, default=1000,
                    help="how many pcs per binary to re-check in verify mode")
    ap.add_argument("--gate-min-agreement", type=float, default=1.0,
                    help="minimum required objdump agreement fraction")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    binaries = resolve_binaries(args.workload_bin, args.binary)
    if args.required_pcs_npy and len(binaries) != 1:
        raise ValueError("--required-pcs-npy requires exactly one --binary")
    required_pcs = set()
    for pc_path in args.required_pcs_npy:
        try:
            import numpy as np
        except ImportError as exc:
            raise RuntimeError("numpy is required for --required-pcs-npy") from exc
        values = np.load(pc_path, mmap_mode="r")
        required_pcs.update(int(value) for value in np.unique(values))
    manifest_path = os.path.join(args.out, "manifest.jsonl")
    existing_manifest: Dict[str, dict] = {}
    if os.path.isfile(manifest_path):
        with open(manifest_path, "r") as handle:
            for line in handle:
                if line.strip():
                    value = json.loads(line)
                    existing_manifest[str(value["binary_name"])] = value
    print(f"[build_static_dict] {len(binaries)} binaries -> {args.out}", flush=True)
    for name, path in binaries:
        t0 = time.time()
        binary_hash, out_path, n = build_binary(
            path, args.out, force=bool(args.force), required_pcs=required_pcs,
        )
        dt = time.time() - t0
        row = {
            "binary_name": name,
            "binary_path": path,
            "binary_hash": binary_hash,
            "schema_version": STATIC_DICT_SCHEMA_VERSION,
            "parquet": out_path,
            "n_rows": int(n),
            "n_required_dynamic_pcs": int(len(required_pcs)),
            "elapsed_s": dt,
            "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        existing_manifest[name] = row
        print(f"  {name}  hash={binary_hash[:12]}  rows={n}  dt={dt:.1f}s", flush=True)
    with open(manifest_path, "w") as fh:
        for r in sorted(
            existing_manifest.values(), key=lambda value: str(value["binary_name"]),
        ):
            fh.write(json.dumps(r) + "\n")
    print(f"[build_static_dict] manifest -> {manifest_path}", flush=True)

    if args.verify:
        n_check, n_ok = verify_roundtrip(args.out, args.sample)
        frac = n_ok / max(1, n_check)
        print(f"[gate static_dict] objdump agreement: {n_ok}/{n_check} = {frac*100:.2f}%",
              flush=True)
        if frac < args.gate_min_agreement:
            print(f"[gate static_dict] FAIL: below {args.gate_min_agreement*100:.2f}%",
                  flush=True)
            return 2
        print("[gate static_dict] PASS", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
