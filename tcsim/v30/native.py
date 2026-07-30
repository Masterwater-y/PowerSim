"""Optional native batched backend for the deployment GSS hot path."""
from __future__ import annotations

from dataclasses import asdict
import importlib
from typing import Any, Mapping, Optional, Tuple

import numpy as np

from .gss import GSSGeometry


def load_native_gss() -> Optional[type]:
    """Return the compiled engine class, or None for the reference fallback."""
    try:
        module = importlib.import_module("tcsim.v30._gss_native")
    except ImportError:
        return None
    return module.NativeGSS


class NativeGSSFeatureEngine:
    """Thin typed wrapper around the pybind11 batch engine."""

    backend_name = "cpp-flat-cow-batch-v1"

    def __init__(
        self,
        geometry: GSSGeometry,
        *,
        ema_alpha: float = 0.02,
        recent_horizon_events: int = 4096,
    ) -> None:
        engine_type = load_native_gss()
        if engine_type is None:
            raise ImportError(
                "v30 native GSS extension is not built; run "
                "scripts/build_v30_gss_native.py"
            )
        values = asdict(geometry)
        self._engine = engine_type(
            values["l1_sets"], values["l1_ways"],
            values["l2_sets"], values["l2_ways"],
            values["llc_sets_per_bank"], values["llc_ways"],
            values["llc_banks"], float(ema_alpha),
            int(recent_horizon_events),
        )

    def preview_batch(self, events: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
        return self._engine.preview(np.asarray(events, dtype=np.int64, order="C"))

    def commit_batch(self, events: np.ndarray) -> None:
        self._engine.commit(np.asarray(events, dtype=np.int64, order="C"))

    def state_summary(self) -> Mapping[str, int]:
        return {
            str(key): int(value)
            for key, value in dict(self._engine.state_summary()).items()
        }
