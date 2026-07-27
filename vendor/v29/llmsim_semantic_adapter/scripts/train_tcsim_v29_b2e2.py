#!/usr/bin/env python3
"""Train the controlled E2-null or B2-frozen TCSim v29 experiment."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence


REPO = Path(__file__).resolve().parents[1]
TCSIM_ROOT = Path(os.environ.get("TCSIM_ROOT", "/data00/yinhaolang/TCSim"))
for path in (REPO, TCSIM_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from tcsim.utils.config import TCSimConfig  # noqa: E402

from model.tcsim_v29_semantic import B2_VARIANTS, SUPPORTED_VARIANTS  # noqa: E402
from train.macro_v29_dataset import CachedSemanticSource  # noqa: E402
from train.tcsim_v29_semantic_train import train_one_semantic_run  # noqa: E402


def _manifest_sources(path: str | Path, split: str) -> List[Any]:
    manifest_path = Path(path).resolve()
    manifest = json.loads(manifest_path.read_text())
    base = manifest_path.parent
    output: List[Any] = []
    for item in manifest.get("splits", {}).get(split, []):
        if not isinstance(item, Mapping):
            value = Path(str(item))
            output.append(str(value if value.is_absolute() else base / value))
            continue
        value = item.get("cache_dir")
        if not value:
            continue
        source = dict(item)
        cache_dir = Path(str(value))
        source["cache_dir"] = str(
            cache_dir if cache_dir.is_absolute() else base / cache_dir
        )
        output.append(source)
    return output


def _cache_path(source: Any) -> str:
    value = source.get("cache_dir") if isinstance(source, Mapping) else source
    return str(Path(str(value)).resolve())


def _validate_separation(
    train_sources: Sequence[Any], validation_sources: Sequence[Any],
) -> None:
    train_by_path = {_cache_path(source): source for source in train_sources}
    validation_by_path = {
        _cache_path(source): source for source in validation_sources
    }
    for path in sorted(set(train_by_path) & set(validation_by_path)):
        train = train_by_path[path]
        validation = validation_by_path[path]
        if not isinstance(train, Mapping) or not isinstance(validation, Mapping):
            raise RuntimeError(f"train/validation cache overlap: {path}")
        train_policy = dict(train.get("sample_split", {}) or {})
        validation_policy = dict(validation.get("sample_split", {}) or {})
        train_partition = train_policy.pop("partition", None)
        validation_partition = validation_policy.pop("partition", None)
        if not (
            train_partition == "train"
            and validation_partition == "validation"
            and train_policy == validation_policy
        ):
            raise RuntimeError(
                "train/validation overlap is not the matching disjoint block "
                f"split: {path}"
            )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--variant", choices=sorted(SUPPORTED_VARIANTS), required=True)
    parser.add_argument(
        "--manifest",
        default=str(TCSIM_ROOT / "data/v29_global_time_dataset/manifest.json"),
    )
    parser.add_argument("--train-split", default="train")
    parser.add_argument("--validation-split", default="validation")
    parser.add_argument(
        "--config", default=str(TCSIM_ROOT / "configs/v29_100m.yaml"),
    )
    parser.add_argument("--out", required=True)
    parser.add_argument("--device", default="auto")
    parser.add_argument("--max-steps", type=int, default=30000)
    parser.add_argument("--resume", default=None)
    parser.add_argument(
        "--resume-rng-policy",
        choices=("exact", "aligned-reseed"),
        default="exact",
        help=(
            "exact requires rank-local sidecars from the new checkpoint; "
            "use aligned-reseed once when continuing a legacy checkpoint"
        ),
    )
    parser.add_argument("--seed", type=int, default=1234)
    parser.add_argument("--num-workers", type=int, default=None)
    parser.add_argument("--semantic-dim", type=int, default=5120)
    parser.add_argument(
        "--semantic-permutation-seed",
        type=int,
        default=None,
        help=(
            "B2-only deterministic per-binary PC-to-embedding permutation; "
            "preserves the cached-vector marginal distribution"
        ),
    )
    parser.add_argument("--semantic-cache-root", default=None)
    parser.add_argument("--semantic-sidecar-root", default=None)
    parser.add_argument(
        "--static-manifest",
        default=str(REPO / "data/v28_1/static_dict/manifest.jsonl"),
    )
    parser.add_argument(
        "--expected-semantic-model", default="Qwen3-14B",
        help="B2 fail-closed substring check against cache provenance",
    )
    parser.add_argument(
        "--sdpa-backend",
        choices=("auto", "flash", "no_flash", "efficient", "math"),
        default=None,
    )
    parser.add_argument(
        "--amp-dtype", choices=("fp32", "bf16", "fp16"), default=None,
    )
    args = parser.parse_args()

    manifest = json.loads(Path(args.manifest).read_text())
    if manifest.get("schema_version") != "tcsim-v29-manifest-1":
        raise SystemExit("unsupported TCSim v29 manifest schema")
    quality = dict(manifest.get("quality", {}) or {})
    if quality.get("status") != "pass":
        raise SystemExit(
            "TCSim v29 manifest quality is not pass: "
            + "; ".join(map(str, quality.get("blockers", [])))
        )
    train_sources = _manifest_sources(args.manifest, args.train_split)
    validation_sources = _manifest_sources(
        args.manifest, args.validation_split,
    )
    if not train_sources:
        raise SystemExit("no TCSim v29 training sources")
    _validate_separation(train_sources, validation_sources)

    cache_root = args.semantic_cache_root
    if args.variant in B2_VARIANTS:
        if not cache_root:
            raise SystemExit("B2-frozen requires --semantic-cache-root")
        if not args.semantic_sidecar_root:
            raise SystemExit("B2-frozen requires --semantic-sidecar-root")
        cache = CachedSemanticSource(cache_root)
        if cache.semantic_dim != int(args.semantic_dim):
            raise SystemExit(
                f"semantic cache dim {cache.semantic_dim} != {args.semantic_dim}"
            )
        cached_model = str(cache.manifest["semantic_encoder_model"])
        if (
            args.expected_semantic_model
            and args.expected_semantic_model.lower() not in cached_model.lower()
        ):
            raise SystemExit(
                f"semantic cache model {cached_model!r} does not contain "
                f"{args.expected_semantic_model!r}"
            )
        has_lora = bool(cache.manifest.get(
            "semantic_encoder_lora_adapter_fingerprint"
        ))
        if args.variant == "b2-lora" and not has_lora:
            raise SystemExit("B2-LoRA requires a LoRA-adapted semantic cache")
        if args.variant == "b2-frozen" and has_lora:
            raise SystemExit(
                "LoRA-adapted cache must use --variant b2-lora"
            )
    else:
        if args.semantic_permutation_seed is not None:
            raise SystemExit(
                "E2-null cannot use --semantic-permutation-seed"
            )
        if cache_root:
            raise SystemExit("E2-null must not receive --semantic-cache-root")
        if args.semantic_sidecar_root:
            raise SystemExit("E2-null must not receive --semantic-sidecar-root")
        args.static_manifest = None

    config = TCSimConfig.load(args.config)
    if args.sdpa_backend is not None:
        config.model = {**config.model, "sdpa_backend": args.sdpa_backend}
    if args.amp_dtype is not None:
        config.train = {**config.train, "amp_dtype": args.amp_dtype}
    config.train = {**config.train, "seed": int(args.seed)}
    if args.num_workers is not None:
        config.train = {
            **config.train, "num_workers": int(args.num_workers),
        }
    result = train_one_semantic_run(
        train_sources,
        validation_sources,
        args.out,
        config,
        variant=args.variant,
        semantic_dim=args.semantic_dim,
        semantic_permutation_seed=args.semantic_permutation_seed,
        semantic_cache_root=cache_root,
        static_manifest=args.static_manifest,
        semantic_sidecar_root=args.semantic_sidecar_root,
        device=args.device,
        max_steps=args.max_steps,
        resume=args.resume,
        resume_rng_policy=args.resume_rng_policy,
    )
    if int(os.environ.get("RANK", "0")) == 0:
        print(f"[B2/E2 train] variant={args.variant} result={result}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
