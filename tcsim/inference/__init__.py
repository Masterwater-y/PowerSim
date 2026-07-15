"""Deployment-side free-running inference for fixed-chunk TCSim.

The heavy deployment implementation is loaded lazily so pure report-processing
tools do not require importing PyTorch.
"""

from importlib import import_module

__all__ = [
    "DeploymentRunner",
    "ModelContextPredictor",
    "PackedTrace",
    "TruthContextPredictor",
    "aggregate_trace_reports",
    "discover_packed_rollouts",
    "evaluate_packed_rollouts",
    "load_checkpoint_model",
    "load_manifest_rollouts",
]


def __getattr__(name):
    if name not in __all__:
        raise AttributeError(name)
    return getattr(import_module(".deployment", __name__), name)
