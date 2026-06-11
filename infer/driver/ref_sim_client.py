#!/usr/bin/env python3
"""ref_sim backend abstraction for the deploy-side inference driver.

Phase A note (docs/04-quantum-parallel-coherence.md §9.3):
    Phase 1a / 1c need a *speculative* path that probes the oracle but does
    not yet commit private cache state. In Phase A we expose those hooks but
    fall back to the legacy synchronous path inside ``PybindBackend`` so the
    driver can be rewritten to the target API even before the C++ split lands.
    Phase B will replace ``PybindBackend`` with ``LocalPybindBackend`` that
    talks to ``PyLocalRefSim`` (true speculative) and a ``CoordinatorClient``.
"""
from __future__ import annotations

import importlib
import json
import sys
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Dict, Optional

from driver.timing_functional_refsim import (  # noqa: E402
    TimingFunctionalBackend,
    TimingFunctionalConfig,
)


class RefSimBackend(ABC):
    @abstractmethod
    def on_ifetch(self, core_id: int, macro_pc_cl: int) -> Dict:
        raise NotImplementedError

    @abstractmethod
    def on_mem_access(self, core_id: int, paddr: int, is_store: bool,
                      size: int, seq: int = 0, thread_id: int = 0) -> Dict:
        raise NotImplementedError

    def on_request(self, core_id: int, paddr: int, is_store: bool,
                   size: int, seq: int = 0, thread_id: int = 0) -> Dict:
        return self.on_mem_access(core_id, paddr, is_store, size, seq, thread_id)

    @abstractmethod
    def on_commit(self, core_id: int, seq: int = 0) -> None:
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Speculative hooks (Phase A: default to synchronous fallback below).
    # ------------------------------------------------------------------
    def on_ifetch_speculative(self, core_id: int, macro_pc_cl: int) -> Dict:
        """Probe i-side oracle without committing local cache state.

        Default falls back to ``on_ifetch`` (Phase A behaviour). Phase B
        backends override this with a true read-only probe.
        """
        return self.on_ifetch(core_id, macro_pc_cl)

    def on_mem_access_speculative(self, core_id: int, paddr: int,
                                  is_store: bool, size: int,
                                  seq: int = 0, thread_id: int = 0) -> Dict:
        return self.on_mem_access(core_id, paddr, is_store, size, seq, thread_id)

    def commit_speculative(self, core_id: int, seq: int = 0) -> None:
        """Commit a previously-speculatively-probed µop.

        Default fallback delegates to ``on_commit``; Phase A path collapses
        speculative+commit into a single legacy call so behaviour is
        bit-identical to the pre-quantum driver.
        """
        self.on_commit(core_id, seq)


class PybindBackend(RefSimBackend):
    """Phase A backend: wraps the existing centralized ``ref_sim_py.RefSim``.

    speculative + commit hooks degrade to the synchronous ``on_*`` path so the
    quantum driver has a working backend even before the C++ split. As a
    consequence, in Phase A the speculative state IS persisted immediately
    (i.e. there is no rollback / re-apply); this is fine because the driver
    still issues µops in (core, micro_seq) order within a single quantum.
    """

    def __init__(self, uarch_profile: str, module_dir: Optional[str] = None):
        if module_dir:
            sys.path.insert(0, str(Path(module_dir).resolve()))
        mod = importlib.import_module("ref_sim_py")
        self.sim = mod.RefSim(str(uarch_profile))

    def on_ifetch(self, core_id: int, macro_pc_cl: int) -> Dict:
        return dict(self.sim.on_ifetch(int(core_id), int(macro_pc_cl)))

    def on_mem_access(self, core_id: int, paddr: int, is_store: bool,
                      size: int, seq: int = 0, thread_id: int = 0) -> Dict:
        return dict(self.sim.on_mem_access(int(core_id), int(paddr),
                                           bool(is_store), int(size),
                                           int(seq), int(thread_id)))

    def on_commit(self, core_id: int, seq: int = 0) -> None:
        self.sim.on_commit(int(core_id), int(seq))


class LocalPybindBackend(RefSimBackend):
    """Phase B backend: wraps ``ref_sim_py.Coordinator`` + ``LocalRefSim``.

    The driver-side façade looks identical to ``PybindBackend`` from the
    caller's perspective (``on_*_speculative(cid, ...)``), but underneath we
    route through the per-core ``LocalRefSim`` exposed by the C++ Coordinator.
    In B.1 the Coordinator is centralized internally, so probe/commit are
    bit-equivalent to ``PybindBackend``; once B.4+ split private state down to
    LocalRefSim the behaviour stays unchanged on the surface.

    Adds two extra methods used by the driver Phase 2 / report:
      * ``reconcile(deltas, results)``  — coordinator merge (B.1: stub)
      * ``drain_counters()``            — global + per-core counters dict
    """

    def __init__(self, uarch_profile: str, module_dir: Optional[str] = None):
        if module_dir:
            sys.path.insert(0, str(Path(module_dir).resolve()))
        mod = importlib.import_module("ref_sim_py")
        self.coord = mod.Coordinator(str(uarch_profile))
        self._locals: Dict[int, object] = {}
        self._phase1c_parallel_safe = True

    def _local(self, core_id: int):
        ls = self._locals.get(core_id)
        if ls is None:
            ls = self.coord.local(int(core_id))
            self._locals[core_id] = ls
        return ls

    # Legacy synchronous path (kept for compatibility; routes through local).
    def on_ifetch(self, core_id: int, macro_pc_cl: int) -> Dict:
        # C.2: C++ 返回的就是 fresh py::dict，去掉外层冗余 dict(...) 拷贝。
        return self._local(core_id).on_ifetch_speculative(int(macro_pc_cl))

    def on_mem_access(self, core_id: int, paddr: int, is_store: bool,
                      size: int, seq: int = 0, thread_id: int = 0) -> Dict:
        return self._local(core_id).on_mem_access_speculative(
            int(paddr), bool(is_store), int(size), int(seq), int(thread_id))

    def on_commit(self, core_id: int, seq: int = 0) -> None:
        # B.1 過渡: probe 已经把状态写入；commit 暂时是 no-op。
        pass

    # True speculative path: signature still takes core_id first to keep
    # inference_driver.py call sites unchanged.
    def on_ifetch_speculative(self, core_id: int, macro_pc_cl: int) -> Dict:
        return self._local(core_id).on_ifetch_speculative(int(macro_pc_cl))

    def on_mem_access_speculative(self, core_id: int, paddr: int,
                                  is_store: bool, size: int,
                                  seq: int = 0, thread_id: int = 0) -> Dict:
        return self._local(core_id).on_mem_access_speculative(
            int(paddr), bool(is_store), int(size), int(seq), int(thread_id))

    def commit_speculative(self, core_id: int, seq: int = 0) -> None:
        # D.5a: probe 已经通过 atomic/bank-locked 共享结构推进状态；
        # commit 保留为稳定 API hook。
        # 注意：driver phase1c_commit 当前签名只传 (cid, seq)，C++ 端的
        # commit_speculative 需要 (paddr, is_store, size, seq, thread_id)。
        self._local(core_id).commit_speculative(0, False, 0, int(seq), 0)

    # E.1: 单核每 quantum 一次 pybind 调用，把 K 次 ifetch+mem_access
    # 合并为一次 batch_probe。fields 元组顺序必须与 C++ 端一致：
    #   (macro_pc, paddr, is_load, is_store, is_atomic, size, seq, thread_id)
    def batch_probe(self, core_id: int, fields):
        return self._local(core_id).batch_probe(fields)

    # F.1: SoA ndarray 输入，返回 d_bank_id ndarray；mock/label 热路径使用。
    def batch_probe_pod(self, core_id: int, macro_pc, paddr, cl_paddr,
                        is_load, is_store, is_atomic, is_branch,
                        size, micro_seq, thread_id):
        return self._local(core_id).batch_probe_pod(
            macro_pc, paddr, cl_paddr, is_load, is_store, is_atomic,
            is_branch, size, micro_seq, thread_id)

    # E.2: phase1c 完成后，把已 commit 行的窗口特征 update 一次性下推。
    # fields 与 batch_probe 同布局并在末尾追加 d_bank_id_post_probe。
    # committed_mask 与 fields 等长；False 元素表示 phase1c 早退后未提交，
    # C++ 端遇到第一个 False 即停止累积（与 phase1c 累积语义一致）。
    def batch_window_update(self, core_id: int, fields, committed_mask):
        self._local(core_id).batch_window_update(fields, committed_mask)

    # E.6: phase1c 下沉到 C++：deadline walk + ReferenceClock + win.update。
    # 返回 dict，包含 consumed / clock state / macro_inc / events。
    def commit_quantum(self, core_id: int, fields11, fetch_lats, exec_lats,
                       mispreds, is_microop, is_last_microop,
                       label_fetch_ticks, first_fetch_tick: float,
                       fetch_clock: float, ready_clock: float,
                       fetch_clock_base: float, committed_this_quantum: int,
                       delta_t: int, label_driven: bool, emit_bin: bool):
        return self._local(core_id).commit_quantum(
            fields11, fetch_lats, exec_lats, mispreds,
            is_microop, is_last_microop, label_fetch_ticks,
            float(first_fetch_tick), float(fetch_clock), float(ready_clock),
            float(fetch_clock_base), int(committed_this_quantum),
            int(delta_t), bool(label_driven), bool(emit_bin))

    # F.1b: commit_quantum 的 ndarray/POD 版本，避免构造 fields11 等 list。
    def commit_quantum_pod(self, core_id: int, macro_pc, paddr, cl_paddr,
                           is_load, is_store, is_atomic, is_branch,
                           micro_seq, thread_id, d_bank_id,
                           fetch_lats, exec_lats, mispreds,
                           is_microop, is_last_microop, label_fetch_ticks,
                           first_fetch_tick: float, fetch_clock: float,
                           ready_clock: float, fetch_clock_base: float,
                           committed_this_quantum: int, delta_t: int,
                           label_driven: bool, emit_bin: bool):
        return self._local(core_id).commit_quantum_pod(
            macro_pc, paddr, cl_paddr, is_load, is_store, is_atomic,
            is_branch, micro_seq, thread_id, d_bank_id,
            fetch_lats, exec_lats, mispreds, is_microop, is_last_microop,
            label_fetch_ticks, float(first_fetch_tick), float(fetch_clock),
            float(ready_clock), float(fetch_clock_base),
            int(committed_this_quantum), int(delta_t),
            bool(label_driven), bool(emit_bin))

    # ------------------------------------------------------------------
    # Coordinator-only methods (driver Phase 2 / report).
    # ------------------------------------------------------------------
    def reconcile(self, deltas=None, results=None) -> None:
        """Phase 2 reconcile barrier. D.5a keeps this as a stable API hook."""
        self.coord.reconcile(list(deltas or []), list(results or []))

    def drain_counters(self) -> Dict:
        return dict(self.coord.drain_counters())


def make_timing_functional_backend(uarch_profile: str, args=None) -> TimingFunctionalBackend:
    profile = json.loads(Path(uarch_profile).read_text())
    cfg = TimingFunctionalConfig(
        row_tick_stride=int(getattr(args, "tf_row_tick_stride", 256)),
        prefetch_degree=int(getattr(args, "tf_prefetch_degree", 1)),
        prefetch_coverage=float(getattr(args, "tf_prefetch_coverage", 0.25)),
        snp_coverage=float(getattr(args, "tf_snp_coverage", 0.45)),
        private_sideband_load_coverage=float(
            getattr(args, "tf_private_sideband_load_coverage", 0.018)
        ),
        private_sideband_store_coverage=float(
            getattr(args, "tf_private_sideband_store_coverage", 0.040)
        ),
        l1_load_fold_l2_coverage=float(
            getattr(args, "tf_l1_load_fold_l2_coverage", 0.41)
        ),
        l1_load_fold_llc_coverage=float(
            getattr(args, "tf_l1_load_fold_llc_coverage", 0.14)
        ),
        l1_load_fold_min_miss_rate=float(
            getattr(args, "tf_l1_load_fold_min_miss_rate", 0.01)
        ),
        l1_load_fold_max_miss_rate=float(
            getattr(args, "tf_l1_load_fold_max_miss_rate", 0.05)
        ),
        l1_load_fold_min_llc_l2_ratio=float(
            getattr(args, "tf_l1_load_fold_min_llc_l2_ratio", 2.0)
        ),
    )
    backend = TimingFunctionalBackend(profile, cfg)
    functional_dir = getattr(args, "functional_dir", None)
    if functional_dir:
        backend.precompute_functional_dir(
            functional_dir,
            warmup_records_per_core=int(
                getattr(args, "refsim_warmup_records_per_core", 0)
            ),
        )
    return backend
