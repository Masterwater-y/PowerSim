from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from eval.macro_v29_semantic_gate import DEFAULT_VARIANTS, main


def write_run(root: Path, variant: str, *, kind: str, wape: float) -> None:
    directory = root / variant
    directory.mkdir(parents=True)
    run = {
        "base_model": "qwen-test",
        "tiny_backbone": kind != "macro-native-qwen-training",
        "tiny_width": 32,
        "d_model": 64,
        "d_field": 4,
        "n_heads": 4,
        "lora_r": 8,
        "lora_alpha": 16,
        "lora_dropout": 0.0,
        "freeze_backbone": True,
        "max_steps": 200,
        "batch_size": 1,
        "sequence_length": 4,
        "sequence_stride": 4,
        "max_tokens": 4096,
        "train_split": "train",
        "validation_split": "seed0_inference",
        "train_workload_roles": "",
        "validation_workload_roles": "business_heldout",
        "train_sources": 16,
        "validation_sources": 7,
        "train_sequences": 100,
        "validation_sequences": 70,
        "tokenizer_size": 1000,
        "tokenizer_fingerprint": "a" * 64,
        "base_model_commit": "b" * 40,
        "backbone_config_fingerprint": "c" * 64,
        "allow_download": False,
        "init_trainable": "",
        "initialized_from": None,
        "seed": 1234,
        "world_size": 1,
        "dtype": "bf16",
        "lr_head": 3.0e-4,
        "lr_lora": 1.0e-4,
        "weight_decay": 0.05,
        "warmup_fraction": 0.03,
        "gradient_clip": 1.0,
        "eval_batches": 32,
        "cores": "1",
        "trainable_parameters": 123456,
        "semantic_run_contract": "macro-v29-semantic-run-v4",
        "train_order_policy": "seeded-random-v1",
        "trainable_init_policy": "isolated-seed-domains-v1",
        "validation_order_policy": "seeded-within-trace-round-robin-v2",
        "semantic_variant": variant,
    }
    final = {
        "status": "PASS",
        "kind": kind,
        "steps": 200,
        "semantic_variant": variant,
        "validation": {
            "commit_wape": float(wape),
            "batches": 32,
            "per_trace": {
                f"seed0/W_{name}/trace": {
                    "absolute_error": float(wape) * 100.0,
                    "target_magnitude": 100.0,
                    "valid_macros": 1000,
                    "commit_wape": float(wape),
                }
                for name in (
                    "alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta",
                )
            },
        },
    }
    (directory / "run.json").write_text(json.dumps(run))
    (directory / "final_report.json").write_text(json.dumps(final))


class MacroSemanticGateTest(unittest.TestCase):
    def run_gate(self, root: Path) -> tuple[int, dict]:
        output = root / "gate.json"
        argv = [
            "macro_v29_semantic_gate.py",
            "--runs-root", str(root),
            "--output", str(output),
        ]
        with patch.object(sys, "argv", argv), redirect_stdout(io.StringIO()):
            return main(), json.loads(output.read_text())

    def test_tiny_contract_runs_cannot_pass_semantic_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for variant in DEFAULT_VARIANTS:
                write_run(
                    root, variant,
                    kind="tiny-training-loop-contract-smoke",
                    wape=0.9,
                )
            return_code, report = self.run_gate(root)
            self.assertEqual(return_code, 3)
            self.assertEqual(report["status"], "UNKNOWN")
            self.assertFalse(report["qwen_evidence_ready"])
            self.assertEqual(report["failures"], [])

    def test_capacity_matched_qwen_improvements_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for variant in DEFAULT_VARIANTS:
                wape = 0.8 if variant in {"real", "register_rename"} else 1.0
                write_run(
                    root, variant,
                    kind="macro-native-qwen-training",
                    wape=wape,
                )
            return_code, report = self.run_gate(root)
            self.assertEqual(return_code, 0)
            self.assertEqual(report["status"], "PASS")
            self.assertTrue(report["qwen_evidence_ready"])
            self.assertEqual(report["failures"], [])
            bootstrap = report["paired_bootstrap"]["real_vs_pseudo"]
            self.assertEqual(bootstrap["workload_clusters"], 7)
            self.assertAlmostEqual(bootstrap["point_estimate"], 0.2)
            self.assertGreater(bootstrap["ci_lower"], 0.0)

    def test_short_qwen_smoke_cannot_pass_semantic_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for variant in DEFAULT_VARIANTS:
                write_run(
                    root, variant,
                    kind="macro-native-qwen-training",
                    wape=0.8 if variant == "real" else 1.0,
                )
            final_path = root / "real" / "final_report.json"
            final = json.loads(final_path.read_text())
            final["steps"] = 1
            final_path.write_text(json.dumps(final))
            return_code, report = self.run_gate(root)
            self.assertEqual(return_code, 3)
            self.assertEqual(report["status"], "UNKNOWN")
            self.assertFalse(report["evidence_complete"])

    def test_qwen_gate_rejects_non_significant_cluster_effect(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for variant in DEFAULT_VARIANTS:
                write_run(
                    root, variant,
                    kind="macro-native-qwen-training",
                    wape=(4.0 / 7.0)
                    if variant in {"real", "register_rename"} else 1.0,
                )
            real_path = root / "real" / "final_report.json"
            real = json.loads(real_path.read_text())
            real["validation"]["per_trace"] = {
                f"seed0/W_{name}/trace": {
                    "absolute_error": 400.0 if name == "eta" else 0.0,
                    "target_magnitude": 100.0,
                    "valid_macros": 1000,
                }
                for name in (
                    "alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta",
                )
            }
            real_path.write_text(json.dumps(real))
            for variant in DEFAULT_VARIANTS:
                if variant == "real":
                    continue
                path = root / variant / "final_report.json"
                final = json.loads(path.read_text())
                if variant == "register_rename":
                    final["validation"]["per_trace"] = dict(
                        real["validation"]["per_trace"]
                    )
                else:
                    final["validation"]["per_trace"] = {
                        f"seed0/W_{name}/trace": {
                            "absolute_error": 100.0,
                            "target_magnitude": 100.0,
                            "valid_macros": 1000,
                        }
                        for name in (
                            "alpha", "beta", "gamma", "delta", "epsilon",
                            "zeta", "eta",
                        )
                    }
                path.write_text(json.dumps(final))
            return_code, report = self.run_gate(root)
            self.assertEqual(return_code, 2)
            self.assertEqual(report["status"], "FAIL")
            self.assertLessEqual(
                report["paired_bootstrap"]["real_vs_pseudo"]["ci_lower"],
                0.0,
            )
            self.assertTrue(any(
                "not positive" in failure for failure in report["failures"]
            ))

    def test_qwen_gate_rejects_unpaired_trace_sets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for variant in DEFAULT_VARIANTS:
                write_run(
                    root, variant,
                    kind="macro-native-qwen-training",
                    wape=0.8 if variant in {"real", "register_rename"} else 1.0,
                )
            path = root / "pseudo" / "final_report.json"
            final = json.loads(path.read_text())
            final["validation"]["per_trace"].pop("seed0/W_gamma/trace")
            path.write_text(json.dumps(final))
            return_code, report = self.run_gate(root)
            self.assertEqual(return_code, 2)
            self.assertEqual(report["status"], "FAIL")
            self.assertFalse(report["qwen_evidence_ready"])
            self.assertTrue(any(
                "trace set differs" in failure for failure in report["failures"]
            ))

    def test_qwen_gate_rejects_resumed_control(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for variant in DEFAULT_VARIANTS:
                write_run(
                    root, variant,
                    kind="macro-native-qwen-training",
                    wape=0.8 if variant in {"real", "register_rename"} else 1.0,
                )
            path = root / "pseudo" / "run.json"
            run = json.loads(path.read_text())
            run["init_trainable"] = "/tmp/old-pseudo.pt"
            run["initialized_from"] = {"step": 100}
            path.write_text(json.dumps(run))
            return_code, report = self.run_gate(root)
            self.assertEqual(return_code, 2)
            self.assertEqual(report["status"], "FAIL")
            self.assertFalse(report["qwen_evidence_ready"])
            self.assertIn("init_trainable", report["fairness_mismatches"]["pseudo"])


if __name__ == "__main__":
    unittest.main()
