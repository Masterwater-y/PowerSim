from __future__ import annotations

import json
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from data.build_macro_v29_semantic_cache import (
    build_static_records,
    normalized_assembly,
    semantic_key_fingerprint,
    verify_encoded_prefix,
)
from train.macro_v29_dataset import CachedSemanticSource, MacroContractError


def write_cache(root: Path) -> Path:
    parquet = root / "static.parquet"
    parquet.write_bytes(b"synthetic-static-identity")
    shard = root / "shards" / "abc.npz"
    shard.parent.mkdir()
    np.savez(
        shard,
        pcs=np.asarray([100, 200], dtype=np.uint64),
        semantic=np.asarray([[1, 2, 3], [4, 5, 6]], dtype=np.float16),
        anchor=np.asarray([[7, 8], [9, 10]], dtype=np.float16),
        semantic_key_hashes=np.asarray([b"a" * 64, b"b" * 64], dtype="S64"),
    )
    def sha(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    pc_set_hash = hashlib.sha256(json.dumps(
        [100, 200], sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    manifest = {
        "schema_version": "macro-v29-semantic-cache-1",
        "semantic_encoder_model": "synthetic-qwen",
        "semantic_encoder_revision": "revision-a",
        "semantic_encoder_artifact_fingerprint": "artifact-a",
        "semantic_encoder_config_fingerprint": "config-a",
        "tokenizer_fingerprint": "tokenizer-a",
        "semantic_prompt_schema_version": "prompt-a",
        "semantic_pooling_policy": "final-token",
        "semantic_dim": 3,
        "anchor_dim": 2,
        "anchor_policy": "mean-embedding",
        "offline_encoder_frozen": True,
        "model_facing_identity_fields": [],
        "binaries": [{
            "binary_hash": "abc",
            "parquet": str(parquet),
            "cache_file": "shards/abc.npz",
            "parquet_sha256": sha(parquet),
            "shard_sha256": sha(shard),
            "pc_set_hash": pc_set_hash,
        }],
    }
    (root / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True) + "\n"
    )
    return parquet


class FakeStaticResolver:
    binary_hash = "binary-hash"

    def __init__(self):
        common = {
            "binary_hash": self.binary_hash,
            "section_name": ".text",
            "decode_source": "objdump-test",
            "size_bytes": 5,
            "bytes_hex": "9090909090",
            "is_branch": False,
            "target": -1,
            "bb_id": 7,
            "semantic_valid": True,
        }
        self.rows = {
            0x401000: {**common, "mnemonic": "mov", "operands": "rax,0x401999"},
            0x401005: {**common, "mnemonic": "add", "operands": "rax,7"},
        }

    def coverage(self, pcs):
        missing = [pc for pc in pcs if pc not in self.rows]
        return {
            "n_missing": len(missing), "n_invalid": 0,
            "missing": missing, "invalid": [],
        }

    def render_window(self, pcs):
        return [
            f"{self.rows[pc]['mnemonic']} {self.rows[pc]['operands']}"
            for pc in pcs
        ]


class SemanticCacheTest(unittest.TestCase):
    def test_recompute_replays_original_first_batch_shape(self):
        records = [{"pc": value} for value in range(5)]
        semantic = np.arange(15, dtype=np.float16).reshape(5, 3)
        anchor = np.arange(10, dtype=np.float16).reshape(5, 2)
        with patch(
            "data.build_macro_v29_semantic_cache.encode_records",
            return_value=(semantic[:3].copy(), anchor[:3].copy()),
        ) as mocked:
            report = verify_encoded_prefix(
                records,
                semantic,
                anchor,
                tokenizer=object(),
                model=object(),
                device="cpu",
                batch_size=3,
                max_prompt_tokens=256,
                verify_samples=2,
            )
        replayed = mocked.call_args.args[0]
        self.assertEqual(replayed, records[:3])
        self.assertEqual(mocked.call_args.kwargs["batch_size"], 3)
        self.assertEqual(report["verify_samples"], 2)
        self.assertEqual(report["replay_samples"], 3)
        self.assertEqual(report["max_recompute_abs_error"], 0.0)

    def test_recompute_failure_reports_semantic_and_anchor_errors(self):
        records = [{"pc": value} for value in range(2)]
        stored_semantic = np.zeros((2, 3), dtype=np.float16)
        stored_anchor = np.zeros((2, 2), dtype=np.float16)
        recomputed_semantic = stored_semantic.copy()
        recomputed_semantic[0, 0] = np.float16(1.0)
        with patch(
            "data.build_macro_v29_semantic_cache.encode_records",
            return_value=(recomputed_semantic, stored_anchor.copy()),
        ):
            with self.assertRaisesRegex(
                MacroContractError, r"semantic=1.0 anchor=0.0",
            ):
                verify_encoded_prefix(
                    records,
                    stored_semantic,
                    stored_anchor,
                    tokenizer=object(),
                    model=object(),
                    device="cpu",
                    batch_size=2,
                    max_prompt_tokens=256,
                    verify_samples=1,
                )

    def test_gather_is_ordered_padded_and_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parquet = write_cache(root)
            cache = CachedSemanticSource(root)
            semantic, anchor = cache.gather_window(
                [200, 100], parquet, k_macro=4,
            )
            np.testing.assert_array_equal(semantic[:2], [[4, 5, 6], [1, 2, 3]])
            np.testing.assert_array_equal(anchor[:2], [[9, 10], [7, 8]])
            np.testing.assert_array_equal(semantic[2:], 0)
            np.testing.assert_array_equal(anchor[2:], 0)
            self.assertEqual(cache.contract["semantic_dim"], 3)
            self.assertEqual(len(cache.manifest_hash), 64)
            with self.assertRaises(MacroContractError):
                cache.gather_window([300], parquet, k_macro=4)

    def test_fixed_permutation_is_deterministic_joint_and_has_no_fixed_point(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parquet = write_cache(root)
            first = CachedSemanticSource(root, fixed_permutation_seed=17)
            second = CachedSemanticSource(root, fixed_permutation_seed=17)
            first_semantic, first_anchor = first.gather_window(
                [100, 200], parquet, k_macro=2,
            )
            second_semantic, second_anchor = second.gather_window(
                [100, 200], parquet, k_macro=2,
            )
            np.testing.assert_array_equal(first_semantic, second_semantic)
            np.testing.assert_array_equal(first_anchor, second_anchor)
            np.testing.assert_array_equal(first_semantic, [[4, 5, 6], [1, 2, 3]])
            np.testing.assert_array_equal(first_anchor, [[9, 10], [7, 8]])
            report = first.intervention_report
            self.assertEqual(report["mode"], "fixed_semantic_permute")
            self.assertEqual(report["binaries"][0]["fixed_points"], 0)

    def test_semantic_key_is_provenance_sensitive(self):
        base = {
            "binary": "a", "pc": 1, "bytes": "90", "asm": "nop",
            "context": "x", "decoder": "d", "model": "m",
            "tokenizer": "t", "prompt": "p", "pool": "last",
        }
        reference = semantic_key_fingerprint(base)
        for key in base:
            changed = dict(base)
            changed[key] = str(changed[key]) + "-changed"
            self.assertNotEqual(reference, semantic_key_fingerprint(changed), key)

    def test_static_and_shard_mutation_invalidates_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parquet = write_cache(root)
            cache = CachedSemanticSource(root)
            cache.gather_window([100], parquet, k_macro=2)
            parquet.write_bytes(b"mutated-static-identity")
            with self.assertRaises(MacroContractError):
                CachedSemanticSource(root)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parquet = write_cache(root)
            shard = root / "shards/abc.npz"
            shard.write_bytes(shard.read_bytes() + b"mutation")
            cache = CachedSemanticSource(root)
            with self.assertRaises(MacroContractError):
                cache.gather_window([100], parquet, k_macro=2)

    def test_prompt_uses_bb_context_without_absolute_identity(self):
        resolver = FakeStaticResolver()
        records = build_static_records(
            resolver,
            [0x401005],
            encoder_provenance={"encoder": "frozen-test"},
        )
        self.assertEqual(len(records), 1)
        prompt = records[0]["prompt"]
        self.assertIn("mov rax,<imm_large>", prompt)
        self.assertIn("add rax,7", prompt)
        self.assertNotIn("0x401000", prompt)
        self.assertNotIn("0x401999", prompt)
        self.assertNotIn("workload", prompt.lower())
        self.assertEqual(
            normalized_assembly("imul rax, rbx, 0x10"),
            "imul rax, rbx, 0x10",
        )


if __name__ == "__main__":
    unittest.main()
