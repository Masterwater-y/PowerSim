"""Optional native fused context helpers for v29 deployment."""
from __future__ import annotations

import importlib
import os
from typing import Any, Optional

import numpy as np


def load_native_context() -> Optional[Any]:
    backend = os.environ.get("TCSIM_CONTEXT_BACKEND", "auto").strip().lower()
    if backend not in {"auto", "native", "python"}:
        raise ValueError(
            "TCSIM_CONTEXT_BACKEND must be auto, native, or python"
        )
    if backend == "python":
        return None
    try:
        return importlib.import_module("tcsim.v29._context_native")
    except ImportError as error:
        if backend == "native":
            raise ImportError(
                "TCSIM_CONTEXT_BACKEND=native but the v29 context extension "
                "is unavailable; run scripts/build_v29_context_native.py"
            ) from error
        return None


def pressure_summary(
    fields: np.ndarray,
    resources: np.ndarray,
    resource_compact: np.ndarray,
    valid: np.ndarray,
    semantic: np.ndarray,
    functional_line: np.ndarray,
    functional_page: np.ndarray,
    producer_log: np.ndarray,
    macro_id: np.ndarray,
    macro_end: np.ndarray,
) -> np.ndarray:
    module = load_native_context()
    if module is None:
        raise ImportError(
            "v29 native context extension is not built; run "
            "scripts/build_v29_context_native.py"
        )
    return module.pressure_summary(
        np.asarray(fields, dtype=np.int64, order="C"),
        np.asarray(resources, dtype=np.int64, order="C"),
        np.asarray(resource_compact, dtype=np.uint32, order="C"),
        np.asarray(valid, dtype=np.bool_, order="C"),
        np.asarray(semantic, dtype=np.uint8, order="C"),
        np.asarray(functional_line, dtype=np.int64, order="C"),
        np.asarray(functional_page, dtype=np.int64, order="C"),
        np.asarray(producer_log, dtype=np.float32, order="C"),
        np.asarray(macro_id, dtype=np.uint32, order="C"),
        np.asarray(macro_end, dtype=np.bool_, order="C"),
    )


def cross_features(
    resources: np.ndarray,
    physical_line: np.ndarray,
    access: np.ndarray,
    valid: np.ndarray,
    resource_compact: np.ndarray,
    dram_row_radix: int,
) -> tuple[np.ndarray, np.ndarray]:
    module = load_native_context()
    if module is None:
        raise ImportError(
            "v29 native context extension is not built; run "
            "scripts/build_v29_context_native.py"
        )
    return module.cross_features(
        np.asarray(resources, dtype=np.int64, order="C"),
        np.asarray(physical_line, dtype=np.int64, order="C"),
        np.asarray(access, dtype=np.uint8, order="C"),
        np.asarray(valid, dtype=np.bool_, order="C"),
        np.asarray(resource_compact, dtype=np.uint32, order="C"),
        int(dram_row_radix),
    )
