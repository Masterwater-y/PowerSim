"""v30 Global Shared-System reference components.

The production v30 path is intentionally not enabled by importing this
package.  The modules here are used first for causal sidecar construction,
correctness tests, residual audits, and throughput measurements.
"""

from .gss import (
    GSS_CATEGORICAL_CARDINALITIES,
    GSS_CATEGORICAL_FIELDS,
    GSS_CONTINUOUS_FIELDS,
    GSS_G1_CONTINUOUS_FIELDS,
    GSSFeatureEngine,
    GSSGeometry,
)
from .sidecar import GSS_SIDECAR_SCHEMA, load_gss_sidecar, slice_gss_window

__all__ = [
    "GSS_CATEGORICAL_CARDINALITIES",
    "GSS_CATEGORICAL_FIELDS",
    "GSS_CONTINUOUS_FIELDS",
    "GSS_G1_CONTINUOUS_FIELDS",
    "GSSFeatureEngine",
    "GSSGeometry",
    "GSS_SIDECAR_SCHEMA",
    "load_gss_sidecar",
    "slice_gss_window",
]
