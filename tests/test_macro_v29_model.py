from __future__ import annotations

from pathlib import Path
import tempfile
import types
import unittest

import torch
from torch import nn

from model.macro_v29_model import (
    MacroCrossCoreMixer,
    MacroV29Config,
    MacroV29TimingModel,
    macro_v29_loss,
)
from train.macro_v29_dataset import MacroContractError, V29_FIELD_SIZES
from train.train_macro_v29 import (
    CoreCountBucketedDistributedSampler,
    DistributedEvalSampler,
    build_timing_model,
    interleaved_trace_indices,
    load_trainable,
    reduced_validation,
    save_trainable,
)


class TinyBackbone(nn.Module):
    def __init__(self, vocab: int = 128, width: int = 24):
        super().__init__()
        self.embedding = nn.Embedding(vocab, width)
        self.projection = nn.Linear(width, width)

    def forward(self, input_ids, attention_mask, **kwargs):
        hidden = self.projection(self.embedding(input_ids))
        return types.SimpleNamespace(
            hidden_states=None,
            last_hidden_state=hidden,
        )


class DummyTokenizer:
    def __len__(self):
        return 128


def synthetic_batch(rows: int = 3):
    macros = 256
    uops = 2
    token_length = macros * 2
    values = []
    generator = torch.Generator().manual_seed(7)
    for size in V29_FIELD_SIZES:
        values.append(torch.randint(
            0, size, (rows, macros, uops, 1), generator=generator,
        ))
    fields = torch.cat(values, dim=-1).reshape(rows, macros * uops, -1)
    starts = torch.arange(0, token_length, 2).repeat(rows, 1)
    ends = starts + 2
    valid = torch.ones(rows, macros, dtype=torch.bool)
    target = torch.arange(1, macros + 1, dtype=torch.float32).repeat(rows, 1)
    horizons = torch.tensor([16, 32, 64, 128, 256, 512, 1024])
    prefix = (target.unsqueeze(-1) <= horizons).float()
    branch_mask = torch.zeros(rows, macros, dtype=torch.bool)
    branch_mask[:, ::31] = True
    return {
        "input_ids": torch.randint(
            0, 128, (rows, token_length), generator=generator,
        ),
        "attention_mask": torch.ones(rows, token_length, dtype=torch.long),
        "macro_token_start": starts,
        "macro_token_end": ends,
        "valid_macro_mask": valid,
        "uop_fields": fields,
        "uop_valid_mask": torch.ones(rows, macros * uops, dtype=torch.bool),
        "uop_to_macro": torch.arange(macros).repeat_interleave(uops).repeat(rows, 1),
        "uop_access": torch.zeros(rows, macros * uops, dtype=torch.long),
        "uop_semantic_flags": torch.zeros(rows, macros * uops, dtype=torch.long),
        "dynamic_uop_fields": torch.full(
            (rows, macros * uops, 8), 8, dtype=torch.long,
        ),
        "uop_count": torch.full((rows, macros), uops, dtype=torch.long),
        "chunk_summary": torch.zeros(rows, 38),
        "relation_features": torch.zeros(rows, 22),
        "state_features": torch.zeros(rows, 5),
        "uarch_features": torch.zeros(rows, 29),
        "sample_ptr": torch.tensor([0, rows]),
        "commit_time_target_macro": target,
        "prefix_target": prefix,
        "progress_target_macro": prefix.sum(dim=1),
        "branch_mask": branch_mask,
        "branch_miss_target": torch.zeros(rows, macros),
    }


class MacroModelTest(unittest.TestCase):
    def setUp(self):
        torch.manual_seed(11)
        self.model = MacroV29TimingModel(
            TinyBackbone(),
            MacroV29Config(
                d_llm=24, d_model=48, d_field=4, n_heads=4, dropout=0.0,
            ),
        ).eval()

    def test_shapes_monotonicity_and_loss(self):
        batch = synthetic_batch()
        output = self.model(batch)
        self.assertEqual(tuple(output["commit_time_macro"].shape), (3, 256))
        self.assertEqual(tuple(output["commit_logits"].shape), (3, 256, 7))
        self.assertTrue(torch.all(
            output["commit_time_macro"][:, 1:]
            >= output["commit_time_macro"][:, :-1]
        ))
        loss = macro_v29_loss(output, batch)
        self.assertTrue(torch.isfinite(loss["total"]))
        loss["total"].backward()
        self.assertEqual(tuple(output["core_state"].shape), (3, 256, 48))
        cross_gradient = sum(
            float(parameter.grad.abs().sum())
            for parameter in self.model.core_mixer.parameters()
            if parameter.grad is not None
        )
        self.assertGreater(cross_gradient, 0.0)

    def test_legacy_summary_checkpoint_architecture_is_retained(self):
        model = MacroV29TimingModel(
            TinyBackbone(),
            MacroV29Config(
                d_llm=24,
                d_model=48,
                d_field=4,
                n_heads=4,
                core_mixer_mode="summary",
            ),
        ).eval()
        names = set(model.state_dict())
        self.assertIn("core_gate", names)
        self.assertIn("core_mixer.attention.in_proj_weight", names)
        self.assertIn("core_mixer.ffn.3.weight", names)
        self.assertNotIn("core_mixer.r_proj.weight", names)
        with torch.no_grad():
            output = model(synthetic_batch())
        self.assertEqual(tuple(output["core_state"].shape), (3, 48))

    def test_old_run_without_architecture_schema_selects_summary_mixer(self):
        args = types.SimpleNamespace(
            tiny_backbone=True,
            tiny_width=24,
            seed=91,
            d_model=64,
            d_field=4,
            n_heads=4,
            semantic_variant="real",
        )
        model = build_timing_model(args, DummyTokenizer())
        self.assertEqual(model.config.core_mixer_mode, "summary")
        self.assertIn("core_gate", model.state_dict())

    def test_cross_mixer_target_block_is_exact(self):
        torch.manual_seed(13)
        full = MacroCrossCoreMixer(
            48, 4, 0.0,
            relation_width=22,
            state_width=5,
            target_block=0,
        ).eval()
        blocked = MacroCrossCoreMixer(
            48, 4, 0.0,
            relation_width=22,
            state_width=5,
            target_block=2,
        ).eval()
        blocked.load_state_dict(full.state_dict())
        state = torch.randn(8, 16, 48)
        valid = torch.ones(8, 16, dtype=torch.bool)
        valid[2, 13:] = False
        relation = torch.randn(8, 22)
        dynamic_state = torch.randn(8, 5)
        sample_ptr = torch.tensor([0, 4, 8])
        with torch.no_grad():
            expected, expected_cross = full(
                state, valid, sample_ptr, relation, dynamic_state,
            )
            actual, actual_cross = blocked(
                state, valid, sample_ptr, relation, dynamic_state,
            )
        torch.testing.assert_close(actual, expected, rtol=1e-5, atol=1e-6)
        torch.testing.assert_close(
            actual_cross, expected_cross, rtol=1e-5, atol=1e-6,
        )

    def test_cross_context_is_per_macro_and_excludes_self_core(self):
        torch.manual_seed(17)
        mixer = MacroCrossCoreMixer(
            48, 4, 0.0,
            relation_width=22,
            state_width=5,
            target_block=2,
        ).eval()
        state = torch.randn(3, 16, 48)
        valid = torch.ones(3, 16, dtype=torch.bool)
        relation = torch.randn(3, 22)
        dynamic_state = torch.randn(3, 5)
        with torch.no_grad():
            _mixed, cross = mixer(
                state, valid, torch.tensor([0, 3]), relation, dynamic_state,
            )
            _single, single_cross = mixer(
                state[:1], valid[:1], torch.tensor([0, 1]),
                relation[:1], dynamic_state[:1],
            )
        self.assertGreater(float(torch.var(cross[0], dim=0).sum()), 0.0)
        torch.testing.assert_close(single_cross, torch.zeros_like(single_cross))

    def test_c1_cross_mixer_keeps_all_ddp_parameter_hooks(self):
        torch.manual_seed(18)
        mixer = MacroCrossCoreMixer(
            48, 4, 0.0,
            relation_width=22,
            state_width=5,
            target_block=2,
        ).train()
        state = torch.randn(1, 16, 48)
        valid = torch.ones(1, 16, dtype=torch.bool)
        relation = torch.randn(1, 22)
        dynamic_state = torch.randn(1, 5)
        mixed, cross = mixer(
            state, valid, torch.tensor([0, 1]), relation, dynamic_state,
        )
        torch.testing.assert_close(cross, torch.zeros_like(cross))
        mixed.square().mean().backward()
        for name, parameter in mixer.named_parameters():
            self.assertIsNotNone(
                parameter.grad, msg=f"c1 leaves {name} unused for DDP",
            )
            self.assertTrue(torch.isfinite(parameter.grad).all(), msg=name)

    def test_bucketed_sampler_aligns_core_count_across_ranks(self):
        class FakeDataset:
            sample_core_counts = [1] * 5 + [4] * 7 + [8] * 3 + [32] * 9

            def __len__(self):
                return len(self.sample_core_counts)

        dataset = FakeDataset()
        samplers = [
            CoreCountBucketedDistributedSampler(
                dataset, num_replicas=4, rank=rank, seed=23,
            )
            for rank in range(4)
        ]
        per_rank = [list(sampler) for sampler in samplers]
        self.assertEqual(len({len(values) for values in per_rank}), 1)
        for step in range(len(per_rank[0])):
            observed = {
                dataset.sample_core_counts[per_rank[rank][step]]
                for rank in range(4)
            }
            self.assertEqual(len(observed), 1)
        self.assertEqual(
            set().union(*(set(values) for values in per_rank)),
            set(range(len(dataset.sample_core_counts))),
        )

    def test_core_permutation_equivariance(self):
        batch = synthetic_batch()
        with torch.no_grad():
            reference = self.model(batch)["commit_time_macro"]
        permutation = torch.tensor([2, 0, 1])
        permuted = {}
        for key, value in batch.items():
            if key == "sample_ptr":
                permuted[key] = value
            elif value.ndim and value.shape[0] == 3:
                permuted[key] = value[permutation]
            else:
                permuted[key] = value
        with torch.no_grad():
            actual = self.model(permuted)["commit_time_macro"]
        torch.testing.assert_close(actual, reference[permutation])

    def test_sequence_cumulative_drift_is_active(self):
        batch = synthetic_batch(rows=4)
        batch.update({
            "sample_ptr": torch.tensor([0, 2, 4]),
            "sample_period_cycles": 64.0,
            "horizons": torch.tensor([16, 32, 64, 128, 256, 512, 1024]),
            "row_sequence": torch.tensor([0, 0, 0, 0]),
            "row_sequence_step": torch.tensor([0, 0, 1, 1]),
            "core_slots": torch.tensor([0, 1, 0, 1]),
        })
        output = self.model(batch)
        loss = macro_v29_loss(output, batch)
        self.assertGreater(float(loss["cumulative"].detach()), 0.0)

        batch["row_sequence_step"] = torch.tensor([0, 0, 2, 2])
        with self.assertRaises(ValueError):
            macro_v29_loss(output, batch)

    def test_side_only_is_invariant_to_assembly_tokens(self):
        torch.manual_seed(19)
        model = MacroV29TimingModel(
            TinyBackbone(),
            MacroV29Config(
                d_llm=24, d_model=48, d_field=4, n_heads=4,
                semantic_mode="side_only",
            ),
        ).eval()
        first = synthetic_batch()
        second = {
            key: value.clone() if torch.is_tensor(value) else value
            for key, value in first.items()
        }
        second["input_ids"] = (second["input_ids"] + 37) % 128
        with torch.no_grad():
            expected = model(first)["commit_time_macro"]
            actual = model(second)["commit_time_macro"]
        torch.testing.assert_close(actual, expected, rtol=0.0, atol=0.0)

    def test_llm_only_is_invariant_to_structured_side(self):
        torch.manual_seed(23)
        model = MacroV29TimingModel(
            TinyBackbone(),
            MacroV29Config(
                d_llm=24, d_model=48, d_field=4, n_heads=4,
                semantic_mode="llm_only",
            ),
        ).eval()
        first = synthetic_batch()
        second = {
            key: value.clone() if torch.is_tensor(value) else value
            for key, value in first.items()
        }
        second["uop_fields"].fill_(1)
        second["dynamic_uop_fields"].fill_(0)
        second["uop_access"].fill_(7)
        second["uop_semantic_flags"].fill_(11)
        for key in (
            "chunk_summary", "relation_features", "state_features",
            "uarch_features",
        ):
            second[key].fill_(3.0)
        with torch.no_grad():
            expected = model(first)["commit_time_macro"]
            actual = model(second)["commit_time_macro"]
        torch.testing.assert_close(actual, expected, rtol=0.0, atol=0.0)

    def test_trainable_checkpoint_round_trip_is_strict(self):
        torch.manual_seed(29)
        first = MacroV29TimingModel(
            TinyBackbone(),
            MacroV29Config(d_llm=24, d_model=48, d_field=4, n_heads=4),
        ).eval()
        torch.manual_seed(31)
        second = MacroV29TimingModel(
            TinyBackbone(),
            MacroV29Config(d_llm=24, d_model=48, d_field=4, n_heads=4),
        ).eval()
        contract = {"semantic_variant": "real", "tokenizer_size": 128}
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = save_trainable(
                first, Path(directory), 7, contract=contract,
            )
            report = load_trainable(
                second, checkpoint, expected_contract=contract,
            )
            self.assertEqual(report["step"], 7)
            self.assertGreater(report["n_trainable_tensors"], 0)
            for (first_name, first_value), (second_name, second_value) in zip(
                first.named_parameters(), second.named_parameters(),
            ):
                self.assertEqual(first_name, second_name)
                torch.testing.assert_close(second_value, first_value)
            with self.assertRaises(MacroContractError):
                load_trainable(
                    second,
                    checkpoint,
                    expected_contract={"semantic_variant": "pseudo"},
                )

    def test_distributed_eval_sampler_has_no_duplicates(self):
        shards = [
            list(DistributedEvalSampler(range(10), num_replicas=3, rank=rank))
            for rank in range(3)
        ]
        self.assertEqual(shards, [[0, 3, 6, 9], [1, 4, 7], [2, 5, 8]])
        flattened = [value for shard in shards for value in shard]
        self.assertEqual(sorted(flattened), list(range(10)))
        self.assertEqual(len(flattened), len(set(flattened)))

    def test_semantic_variants_share_identical_trainable_initialization(self):
        common = {
            "tiny_backbone": True,
            "tiny_width": 24,
            "seed": 91,
            "d_model": 64,
            "d_field": 4,
            "n_heads": 4,
        }
        models = {
            variant: build_timing_model(
                types.SimpleNamespace(**common, semantic_variant=variant),
                DummyTokenizer(),
            )
            for variant in ("real", "side_only", "llm_only")
        }
        reference = dict(models["real"].named_parameters())
        for variant in ("side_only", "llm_only"):
            observed = dict(models[variant].named_parameters())
            self.assertEqual(set(observed), set(reference))
            for name, value in reference.items():
                self.assertTrue(
                    torch.equal(value, observed[name]),
                    msg=f"{variant} initialization differs at {name}",
                )

    def test_short_validation_order_round_robins_traces(self):
        trace_ids = ["A", "A", "A", "B", "B", "C"]
        order = interleaved_trace_indices(trace_ids, seed=17)
        self.assertEqual(order, interleaved_trace_indices(trace_ids, seed=17))
        self.assertEqual(sorted(order), list(range(6)))
        self.assertEqual({trace_ids[index] for index in order[:3]}, {"A", "B", "C"})
        shards = [
            list(DistributedEvalSampler(
                range(6), num_replicas=3, rank=rank, indices=order,
            ))
            for rank in range(3)
        ]
        flattened = [value for shard in shards for value in shard]
        self.assertEqual(sorted(flattened), list(range(6)))

    def test_validation_reports_disjoint_per_trace_totals(self):
        batch = synthetic_batch(rows=4)
        batch.update({
            "row_sequence": torch.tensor([0, 0, 1, 1]),
            "trace_id": ["run/W_alpha/hash", "run/W_beta/hash"],
        })
        report = reduced_validation(
            self.model,
            [batch],
            torch.device("cpu"),
            dtype_name="fp32",
            max_batches=1,
        )
        self.assertEqual(set(report["per_trace"]), {
            "run/W_alpha/hash", "run/W_beta/hash",
        })
        self.assertEqual(
            report["per_trace"]["run/W_alpha/hash"]["valid_macros"],
            512,
        )
        self.assertEqual(sum(
            item["valid_macros"] for item in report["per_trace"].values()
        ), report["valid_macros"])


if __name__ == "__main__":
    unittest.main()
