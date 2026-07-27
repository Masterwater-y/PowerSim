"""Project-local Hugging Face cache and model-path resolution.

LLMSim keeps model artifacts under ``data/hf_cache`` so training and
deployment never fall back to the root user's home-directory cache.  Logical
Hub ids found in old checkpoints remain supported: when their revision is
present locally, they are converted to an absolute project snapshot path.
"""
from __future__ import annotations

import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROJECT_HF_HOME = PROJECT_ROOT / "data" / "hf_cache"
PROJECT_HF_HUB_CACHE = PROJECT_HF_HOME / "hub"
PROJECT_MODEL_ROOT = PROJECT_ROOT / "data" / "models"
LEGACY_ROOT_HF_HUB_CACHE = Path("/root/.cache/huggingface/hub")


def configure_project_hf_cache() -> None:
    """Force Hugging Face libraries to use LLMSim-owned storage."""

    os.environ["HF_HOME"] = str(PROJECT_HF_HOME)
    os.environ["HF_HUB_CACHE"] = str(PROJECT_HF_HUB_CACHE)
    os.environ["HUGGINGFACE_HUB_CACHE"] = str(PROJECT_HF_HUB_CACHE)
    os.environ["TRANSFORMERS_CACHE"] = str(PROJECT_HF_HUB_CACHE)


def local_model_path(model_name_or_path: str | os.PathLike[str]) -> str:
    """Resolve a cached Hub id to its absolute project snapshot path.

    Existing filesystem paths are normalized.  A Hub id that has not yet
    been cached is returned unchanged; downloads still land in the project
    cache because :func:`configure_project_hf_cache` is applied at import.
    """

    value = str(model_name_or_path)
    candidate = Path(value).expanduser()
    if candidate.exists():
        return str(candidate.resolve())
    try:
        legacy_relative = candidate.relative_to(LEGACY_ROOT_HF_HUB_CACHE)
    except ValueError:
        legacy_relative = None
    if legacy_relative is not None:
        migrated = PROJECT_HF_HUB_CACHE / legacy_relative
        if migrated.exists():
            return str(migrated.resolve())
    if value.count("/") != 1:
        return value
    organization, repository = value.split("/", 1)
    stable_alias = PROJECT_MODEL_ROOT / organization / repository
    if stable_alias.exists():
        return str(stable_alias.absolute())
    cache_dir = PROJECT_HF_HUB_CACHE / (
        f"models--{organization}--{repository}"
    )
    main_ref = cache_dir / "refs" / "main"
    if not main_ref.is_file():
        return value
    revision = main_ref.read_text().strip()
    snapshot = cache_dir / "snapshots" / revision
    if not revision or not snapshot.is_dir():
        return value
    return str(snapshot.resolve())


configure_project_hf_cache()
