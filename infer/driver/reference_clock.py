#!/usr/bin/env python3
"""Reference-clock update used by scheme 3: fc += fl; rc=max(rc, fc+el).

Quantum-extended (see docs/04-quantum-parallel-coherence.md):
    - fetch_clock_base : per-core quantum boundary baseline; deadline = base + Δt
    - committed_this_quantum : how many µops this core committed in current quantum
    - advance_base(Δt) : roll baseline forward by Δt + slack to avoid deadline regression
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ReferenceClock:
    fetch_clock: float = 0.0
    ready_clock: float = 0.0
    fetch_clock_base: float = 0.0
    committed_this_quantum: int = 0

    def step(self, fetch_lat: float, exec_lat: float):
        self.fetch_clock += max(float(fetch_lat), 0.0)
        self.ready_clock = max(self.ready_clock,
                               self.fetch_clock + max(float(exec_lat), 0.0))
        self.committed_this_quantum += 1
        return self.fetch_clock, self.ready_clock

    def advance_base(self, delta_t: float) -> float:
        """Advance the quantum baseline by Δt + slack.

        slack = max(0, fetch_clock - (base+Δt)); next deadline becomes
        fetch_clock + Δt when slack > 0, otherwise base + Δt. Returns new base.
        """
        deadline = self.fetch_clock_base + float(delta_t)
        slack = max(0.0, self.fetch_clock - deadline)
        self.fetch_clock_base = deadline + slack
        self.committed_this_quantum = 0
        return self.fetch_clock_base
