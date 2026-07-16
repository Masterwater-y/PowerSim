"""TCSim v29 global-time prefix-progress implementation.

The v29 package is deliberately versioned separately from the v28.1
fixed-chunk pipeline.  Cache/checkpoint contracts are incompatible by design.
"""

from .contracts import (
    DATASET_SCHEMA_VERSION,
    FEATURE_SCHEMA_VERSION,
    MODEL_INPUT_CONTRACT,
)

__all__ = [
    "DATASET_SCHEMA_VERSION",
    "FEATURE_SCHEMA_VERSION",
    "MODEL_INPUT_CONTRACT",
]
