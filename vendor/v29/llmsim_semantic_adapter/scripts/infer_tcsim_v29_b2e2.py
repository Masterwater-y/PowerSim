#!/usr/bin/env python3
"""Run the authoritative TCSim v29 CLI with a B2/E2 checkpoint loader."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import runpy
import sys


REPO = Path(__file__).resolve().parents[1]
TCSIM_ROOT = Path(os.environ.get("TCSIM_ROOT", "/data00/yinhaolang/TCSim"))
for path in (REPO, TCSIM_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from tcsim.v29 import inference as upstream_inference  # noqa: E402
from train.tcsim_v29_semantic_inference import (  # noqa: E402
    load_semantic_checkpoint_runner,
)


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--semantic-cache-root", default=None)
    parser.add_argument("--static-manifest", default=None)
    parser.add_argument("--semantic-sidecar-root", default=None)
    known, remaining = parser.parse_known_args()

    def bound_loader(checkpoint_path: str, **kwargs):
        return load_semantic_checkpoint_runner(
            checkpoint_path,
            semantic_cache_root=known.semantic_cache_root,
            static_manifest=known.static_manifest,
            semantic_sidecar_root=known.semantic_sidecar_root,
            **kwargs,
        )

    upstream_inference.load_checkpoint_runner = bound_loader
    sys.argv = [str(TCSIM_ROOT / "scripts/infer_v29.py"), *remaining]
    runpy.run_path(str(TCSIM_ROOT / "scripts/infer_v29.py"), run_name="__main__")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
