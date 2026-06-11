#!/usr/bin/env python3
"""Online strictly-causal window features for the deploy-side driver."""
from __future__ import annotations

from collections import defaultdict, deque
from typing import Dict


class OnlineWindowFeatures:
    def __init__(self, dram_banks: int = 16, row_bytes: int = 8192):
        self.win64 = deque()
        self.win256_cl = deque()
        self.win1024_cl = deque()
        self.win256_dram = deque()
        self.cl_count64 = defaultdict(int)
        self.pc_count64 = defaultdict(int)
        self.bank_count64 = defaultdict(int)
        self.cl_count256 = defaultdict(int)
        self.cl_count1024 = defaultdict(int)
        self.dram_bank_count = defaultdict(int)
        self.dram_row_count = defaultdict(int)
        self.last_cl_pos = {}
        self.last_branch_pos = -1
        self.pos = 0
        self.dram_banks = dram_banks
        self.row_bytes = row_bytes
        # C.1: incremental scalars to replace per-call sum(generator) over win64.
        self.mem_count64 = 0
        self.br_count64 = 0

    def derive_before_update(self, row: Dict, d_attrs: Dict) -> Dict:
        is_load = row.get("is_load", 0)
        is_store = row.get("is_store", 0)
        is_atomic = row.get("is_atomic", 0)
        cur_mem = bool(is_load or is_store or is_atomic)
        cur_br = bool(row.get("is_branch", 0))
        cur_cl = int(row.get("cacheline_paddr", 0))
        cur_pc = int(row.get("macro_pc", 0))
        cur_bk = int(d_attrs.get("d_bank_id", 0))
        cur_pa = int(row.get("paddr", 0))
        bank_mask = self.dram_banks - 1
        cur_dram_bank = (cur_pa >> 6) & bank_mask
        cur_row = (cur_pa // self.row_bytes) if self.row_bytes > 0 else 0

        # C.1: mem_count64 / br_count64 are maintained incrementally in
        # update()/_trim(); replaces two sum(generator) scans of win64.
        out = {
            "mem_density_W64": self.mem_count64 if self.mem_count64 < 32767 else 32767,
            "branch_density_W64": self.br_count64 if self.br_count64 < 32767 else 32767,
            "unique_cl_W64": min(len(self.cl_count64), 32767),
            "pc_freq_W64": min(self.pc_count64.get(cur_pc, 0), 32767),
            "bank_conflict_W64": min(self.bank_count64.get(cur_bk, 0), 32767) if cur_mem else 0,
            "unique_cl_W256": min(len(self.cl_count256), 255),
            "unique_cl_W1024": min(len(self.cl_count1024), 2047),
            "dram_bank_id": cur_dram_bank if cur_dram_bank < 15 else 15,
            "dram_bank_freq_W256": min(self.dram_bank_count.get(cur_dram_bank, 0), 255) if cur_mem else 0,
            "dram_row_freq_W256": min(self.dram_row_count.get(cur_row, 0), 255) if cur_mem else 0,
        }
        # C.1: int.bit_length() is ~3x faster than math.log2() and avoids
        # float<->int round-trip; for dist>=1, floor(log2(dist)) == bit_length-1.
        if cur_mem and cur_cl in self.last_cl_pos:
            dist = self.pos - self.last_cl_pos[cur_cl]
            v = (dist.bit_length() - 1) if dist > 0 else 0
            out["cl_reuse_dist_log"] = 15 if v > 15 else (0 if v < 0 else v)
        else:
            out["cl_reuse_dist_log"] = 15
        if self.last_branch_pos >= 0:
            dist = self.pos - self.last_branch_pos
            v = (dist.bit_length() - 1) if dist > 0 else 0
            out["time_since_last_branch_log"] = 15 if v > 15 else (0 if v < 0 else v)
        else:
            out["time_since_last_branch_log"] = 15
        return out

    def update(self, row: Dict, d_attrs: Dict) -> None:
        cur_mem = bool(row.get("is_load", 0) or row.get("is_store", 0)
                       or row.get("is_atomic", 0))
        cur_br = bool(row.get("is_branch", 0))
        cur_cl = int(row.get("cacheline_paddr", 0))
        cur_pc = int(row.get("macro_pc", 0))
        cur_bk = int(d_attrs.get("d_bank_id", 0))
        cur_pa = int(row.get("paddr", 0))
        bank_mask = self.dram_banks - 1
        cur_dram_bank = int((cur_pa >> 6) & bank_mask)
        cur_row = int(cur_pa // self.row_bytes) if self.row_bytes > 0 else 0

        self.win64.append((self.pos, cur_mem, cur_br, cur_cl, cur_pc, cur_bk))
        if cur_mem:
            self.mem_count64 += 1
        if cur_br:
            self.br_count64 += 1
        self.pc_count64[cur_pc] += 1
        if cur_mem:
            self.cl_count64[cur_cl] += 1
            self.last_cl_pos[cur_cl] = self.pos
            self.bank_count64[cur_bk] += 1
            self.win256_cl.append((self.pos, cur_cl))
            self.cl_count256[cur_cl] += 1
            self.win1024_cl.append((self.pos, cur_cl))
            self.cl_count1024[cur_cl] += 1
            self.win256_dram.append((self.pos, cur_dram_bank, cur_row))
            self.dram_bank_count[cur_dram_bank] += 1
            self.dram_row_count[cur_row] += 1
        if cur_br:
            self.last_branch_pos = self.pos

        self._trim()
        self.pos += 1

    def _trim(self) -> None:
        while len(self.win64) > 64:
            _, ev_mem, ev_br, ev_cl, ev_pc, ev_bk = self.win64.popleft()
            if ev_mem:
                self.mem_count64 -= 1
            if ev_br:
                self.br_count64 -= 1
            self._dec(self.pc_count64, ev_pc)
            if ev_mem:
                self._dec(self.cl_count64, ev_cl)
                self._dec(self.bank_count64, ev_bk)
        while len(self.win256_cl) > 256:
            _, ev_cl = self.win256_cl.popleft()
            self._dec(self.cl_count256, ev_cl)
        while len(self.win1024_cl) > 1024:
            _, ev_cl = self.win1024_cl.popleft()
            self._dec(self.cl_count1024, ev_cl)
        while len(self.win256_dram) > 256:
            _, ev_bank, ev_row = self.win256_dram.popleft()
            self._dec(self.dram_bank_count, ev_bank)
            self._dec(self.dram_row_count, ev_row)

    @staticmethod
    def _dec(counter, key) -> None:
        v = counter[key] - 1
        if v <= 0:
            del counter[key]
        else:
            counter[key] = v
