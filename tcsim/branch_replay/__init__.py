"""Standalone functional branch-predictor replay."""

from .config import (
    BTBConfig,
    IndirectConfig,
    RASConfig,
    ReplayConfig,
    TournamentConfig,
)
from .replay import (
    BranchEvent,
    BranchPrediction,
    BranchType,
    TargetProvider,
    TournamentBPUReplay,
    prediction_to_dict,
)
from .io import (
    REPLAY_CACHE_CONTRACT,
    aggregate_core_reports,
    attach_v29_meta_evaluation,
    discover_aligned_files,
    event_from_mapping,
    events_from_cache_arrays,
    iter_aligned_events,
    replay_core_streams,
)
from .audit import (
    aggregate_audit_reports,
    audit_core_stream,
    audit_trace,
    merge_binary_reports,
    merge_window_reports,
)

__all__ = [
    "BTBConfig",
    "BranchEvent",
    "BranchPrediction",
    "BranchType",
    "IndirectConfig",
    "REPLAY_CACHE_CONTRACT",
    "RASConfig",
    "ReplayConfig",
    "TargetProvider",
    "TournamentBPUReplay",
    "TournamentConfig",
    "aggregate_core_reports",
    "aggregate_audit_reports",
    "attach_v29_meta_evaluation",
    "audit_core_stream",
    "audit_trace",
    "discover_aligned_files",
    "event_from_mapping",
    "events_from_cache_arrays",
    "iter_aligned_events",
    "merge_binary_reports",
    "merge_window_reports",
    "prediction_to_dict",
    "replay_core_streams",
]
