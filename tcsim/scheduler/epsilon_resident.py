"""Phase 1: epsilon resident scheduler.

Implements plan §2 semantics:

    for each active core:
        if chunk is empty: load fixed-K chunk, predict Δ̂, E_pred = T_pred + Δ̂
    emit sample
    E_min = min(E_pred over active)
    fast = { c | E_pred[c] <= E_min + epsilon }
    slow = active - fast
    commit fast (exactly once), advance cursor, T_pred += Δ̂
    slow chunks stay resident, exposure += 1

The scheduler is pure Python and does not depend on torch; a predictor callable
is injected so the same scheduler drives both training-cache rollouts and
inference.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from ..chunker.fixed_chunk import Chunk


# Predictor contract: given (core_id, chunk, per-core state dict, sample-level
# context list), returns predicted Δ̂ (in cycles).
Predictor = Callable[[int, Chunk, dict, List[dict]], float]


@dataclass
class CoreState:
    core_id: int
    cursor: int = 0                 # index into `chunks_by_core[core_id]`
    T_pred: float = 0.0             # predicted virtual cycle of the prefix already committed
    E_pred: float = 0.0             # predicted end cycle of the current chunk
    delta_hat: float = 0.0
    resident: bool = False
    exposure: int = 0
    state_version: int = 0
    active: bool = True
    finished: bool = False
    current_chunk_id: int = -1
    force_fast: bool = False


@dataclass
class ScheduleSample:
    step: int
    trace_id: str
    core_records: List[dict]        # {core_id, chunk_id, resident, context_only, exposure, T_pred, E_pred, delta_hat}
    E_min: float
    limit: float
    fast_cores: List[int]
    slow_cores: List[int]


@dataclass
class SchedulerStats:
    n_samples: int = 0
    n_commits: int = 0
    n_resident_events: int = 0
    n_unique_chunk_encodes: int = 0
    max_exposure: int = 0
    total_forward_budget_used: int = 0


class EpsilonResidentScheduler:
    def __init__(
        self,
        chunks_by_core: Dict[int, List[Chunk]],
        predictor: Predictor,
        *,
        epsilon: float,
        max_forward_budget: Optional[int] = None,
        max_resident_exposure: int = 0,
        trace_id: str = "trace",
    ) -> None:
        self.chunks_by_core = chunks_by_core
        self.predictor = predictor
        self.epsilon = float(epsilon)
        self.max_forward_budget = (
            None if max_forward_budget is None or int(max_forward_budget) <= 0
            else int(max_forward_budget)
        )
        self.max_resident_exposure = int(max_resident_exposure)
        self.trace_id = trace_id
        self.states: Dict[int, CoreState] = {
            c: CoreState(core_id=c) for c in sorted(chunks_by_core.keys())
        }
        self.stats = SchedulerStats()
        # unique encode key set for cache-hit metric
        self._encoded: set = set()
        self._committed_keys: set = set()

    def _active_cores(self) -> List[int]:
        return [c for c, st in self.states.items() if not st.finished and st.active]

    def _ensure_current_chunk(self, core_id: int, sample_ctx: List[dict]) -> Optional[Chunk]:
        st = self.states[core_id]
        chunks = self.chunks_by_core[core_id]
        if st.cursor >= len(chunks):
            st.finished = True
            return None
        ch = chunks[st.cursor]
        if st.current_chunk_id != ch.chunk_id:
            # newly loaded — predict Δ̂
            st.current_chunk_id = ch.chunk_id
            st.delta_hat = float(max(1.0, self.predictor(core_id, ch, self._state_view(st), sample_ctx)))
            st.E_pred = st.T_pred + st.delta_hat
            st.exposure = 0
            key = (self.trace_id, core_id, ch.chunk_id)
            if key not in self._encoded:
                self._encoded.add(key)
                self.stats.n_unique_chunk_encodes += 1
        return ch

    def _state_view(self, st: CoreState) -> dict:
        return {
            "core_id": st.core_id,
            "cursor": st.cursor,
            "T_pred": st.T_pred,
            "E_pred": st.E_pred,
            "resident": st.resident,
            "exposure": st.exposure,
            "state_version": st.state_version,
            "delta_hat": st.delta_hat,
            "force_fast": st.force_fast,
        }

    def run(self) -> List[ScheduleSample]:
        samples: List[ScheduleSample] = []
        step = 0
        while any(not st.finished for st in self.states.values()):
            if self.max_forward_budget is not None and step >= self.max_forward_budget:
                break
            active = self._active_cores()
            if not active:
                break
            # load / predict current chunk for each active core
            core_records: List[dict] = []
            for c in active:
                st = self.states[c]
                ch = self._ensure_current_chunk(c, core_records)
                if ch is None:
                    continue
                core_records.append({
                    "core_id": c,
                    "chunk_id": ch.chunk_id,
                    "resident": st.resident,
                    "first_exposure": st.exposure == 0,
                    "exposure": st.exposure,
                    # Audit-only scheduler state.  The torch dataset must not
                    # pass these values to the duration model.
                    "audit_T_pred": st.T_pred,
                    "audit_E_pred": st.E_pred,
                    "audit_delta_hat": st.delta_hat,
                    "state_version": st.state_version,
                    "n_uops": ch.n_uops,
                    "has_atomic": ch.has_atomic,
                    "has_serialize": ch.has_serialize,
                })
            if not core_records:
                break
            E_min = min(r["audit_E_pred"] for r in core_records)
            limit = E_min + self.epsilon
            fast = [r["core_id"] for r in core_records if r["audit_E_pred"] <= limit]
            slow = [r["core_id"] for r in core_records if r["audit_E_pred"] > limit]
            for r in core_records:
                if self.states[r["core_id"]].force_fast and r["core_id"] not in fast:
                    slow.remove(r["core_id"])
                    fast.append(r["core_id"])
            # sync events force fast set to include atomic/serialize cores
            for r in core_records:
                if r["has_atomic"] or r["has_serialize"]:
                    if r["core_id"] not in fast:
                        # sync must not be indefinitely delayed
                        slow.remove(r["core_id"])
                        fast.append(r["core_id"])
            # exactly-once commit accounting
            for c in fast:
                st = self.states[c]
                key = (self.trace_id, c, st.current_chunk_id)
                if key in self._committed_keys:
                    raise RuntimeError(f"double commit for {key}")
                self._committed_keys.add(key)
                st.T_pred = st.E_pred
                st.cursor += 1
                st.current_chunk_id = -1
                st.resident = False
                st.exposure = 0
                st.force_fast = False
                self.stats.n_commits += 1
            # slow cores keep resident
            for c in slow:
                st = self.states[c]
                st.resident = True
                st.exposure += 1
                self.stats.n_resident_events += 1
                self.stats.max_exposure = max(self.stats.max_exposure, st.exposure)
                if 0 < self.max_resident_exposure <= st.exposure:
                    # Soft escape: the next emitted context still observes the
                    # resident chunk, then the scheduler commits it exactly
                    # once even if it remains beyond epsilon.
                    st.force_fast = True
            for r in core_records:
                r["context_only"] = r["core_id"] in slow

            samples.append(ScheduleSample(
                step=step,
                trace_id=self.trace_id,
                core_records=core_records,
                E_min=E_min,
                limit=limit,
                fast_cores=fast,
                slow_cores=slow,
            ))
            step += 1
        self.stats.n_samples = len(samples)
        self.stats.total_forward_budget_used = len(samples)
        return samples


def sample_to_row(sample: ScheduleSample) -> dict:
    return {
        "trace_id": sample.trace_id,
        "step": sample.step,
        "audit_E_min": sample.E_min,
        "audit_limit": sample.limit,
        "fast_cores": list(sample.fast_cores),
        "slow_cores": list(sample.slow_cores),
        "core_records": list(sample.core_records),
    }
