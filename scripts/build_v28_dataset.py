#!/usr/bin/env python3
"""v28 defaults for the generic oracle-context dataset builder."""
from __future__ import annotations

import os
import sys

from build_v27_dataset import main


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


if __name__ == "__main__":
    if "--raw-root-glob" not in sys.argv:
        sys.argv.extend([
            "--raw-root-glob",
            "/data00/yinhaolang/TSim/data/raw_v28_1_business_a2_sharedzipf_seed*_c*",
        ])
    if "--contract-file" not in sys.argv:
        sys.argv.extend([
            "--contract-file",
            os.path.join(ROOT, "configs", "v28_business_workloads.json"),
        ])
    raise SystemExit(main())
