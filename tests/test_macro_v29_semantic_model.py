from __future__ import annotations

import types
import unittest

import torch
from torch import nn

from model.macro_v29_model import (
    CausalMacroTransformer,
    MacroV29Config,
    MacroV29TimingModel,
)
from train.macro_v29_dataset import V29_FIELD_SIZES


class CountingEmbedBackbone(nn.Module):
    def __init__(self, width: int = 24):
        super().__init__()
        self.projection = nn.Linear(width, width)
        self.calls = 0
        self.last_shape = None
        self.last_attention = None
        self.received_input_ids = False

    def forward(
        self, input_ids=None, inputs_embeds=None, attention_mask=None, **kwargs,
    ):
        self.calls += 1
        self.received_input_ids = input_ids is not None
        self.last_shape = tuple(inputs_embeds.shape)
        self.last_attention = attention_mask.detach().clone()
        return types.SimpleNamespace(
            last_hidden_state=self.projection(inputs_embeds),
            hidden_states=None,
        )


def semantic_batch(rows: int = 8):
    macros = 256
    generator = torch.Generator().manual_seed(41)
    fields = torch.cat([
        torch.randint(0, size, (rows, macros, 1), generator=generator)
        for size in V29_FIELD_SIZES
    ], dim=-1)
    return {
        "static_semantic": torch.randn(rows, macros, 12, generator=generator),
        "static_anchor": torch.randn(rows, macros, 24, generator=generator),
        "valid_macro_mask": torch.ones(rows, macros, dtype=torch.bool),
        "uop_fields": fields,
        "uop_valid_mask": torch.ones(rows, macros, dtype=torch.bool),
        "uop_to_macro": torch.arange(macros).repeat(rows, 1),
        "uop_access": torch.zeros(rows, macros, dtype=torch.long),
        "uop_semantic_flags": torch.zeros(rows, macros, dtype=torch.long),
        "uop_count": torch.ones(rows, macros, dtype=torch.long),
        "dynamic_uop_fields": torch.full(
            (rows, macros, 8), 8, dtype=torch.long,
        ),
        "chunk_summary": torch.zeros(rows, 38),
        "relation_features": torch.zeros(rows, 22),
        "state_features": torch.zeros(rows, 5),
        "uarch_features": torch.zeros(rows, 29),
        "sample_ptr": torch.tensor([0, rows]),
    }


def ordinary_model(*, learned_null: bool = False) -> MacroV29TimingModel:
    return MacroV29TimingModel(
        None,
        MacroV29Config(
            d_llm=24,
            d_model=48,
            d_field=4,
            n_heads=4,
            semantic_input_mode=(
                "learned_null_macro_token"
                if learned_null else "cached_macro_soft_token"
            ),
            semantic_dim=12,
            online_backbone_type="causal_transformer",
            semantic_source="learned_null" if learned_null else "real_cache",
            online_transformer_layers=2,
            online_transformer_heads=4,
            online_transformer_ffn_multiplier=4,
        ),
    )


class SemanticModelTest(unittest.TestCase):
    def setUp(self):
        torch.manual_seed(43)
        self.backbone = CountingEmbedBackbone()
        self.model = MacroV29TimingModel(
            self.backbone,
            MacroV29Config(
                d_llm=24,
                d_model=48,
                d_field=4,
                n_heads=4,
                semantic_input_mode="cached_macro_soft_token",
                semantic_dim=12,
            ),
        ).eval()

    def test_all_core_rows_use_one_batched_macro_qwen_call(self):
        batch = semantic_batch(rows=8)
        output = self.model(batch)
        self.assertEqual(self.backbone.calls, 1)
        self.assertEqual(self.backbone.last_shape, (8, 256, 24))
        self.assertFalse(self.backbone.received_input_ids)
        self.assertEqual(tuple(output["commit_time_macro"].shape), (8, 256))
        self.assertTrue(torch.all(
            output["commit_time_macro"][:, 1:]
            >= output["commit_time_macro"][:, :-1]
        ))

    def test_tail_mask_is_passed_as_online_attention_mask(self):
        batch = semantic_batch(rows=2)
        batch["valid_macro_mask"][0, 200:] = False
        with torch.no_grad():
            hidden = self.model._semantic_macro_hidden(
                batch, batch["valid_macro_mask"],
            )
        self.assertEqual(tuple(hidden.shape), (2, 256, 24))
        self.assertEqual(int(self.backbone.last_attention[0].sum()), 200)
        self.assertEqual(int(self.backbone.last_attention[1].sum()), 256)

    def test_soft_token_path_is_core_permutation_equivariant(self):
        batch = semantic_batch(rows=3)
        with torch.no_grad():
            reference = self.model(batch)["commit_time_macro"]
        permutation = torch.tensor([2, 0, 1])
        permuted = {}
        for key, value in batch.items():
            if key == "sample_ptr":
                permuted[key] = value
            elif torch.is_tensor(value) and value.ndim and value.shape[0] == 3:
                permuted[key] = value[permutation]
            else:
                permuted[key] = value
        with torch.no_grad():
            actual = self.model(permuted)["commit_time_macro"]
        torch.testing.assert_close(actual, reference[permutation])

    def test_shape_contract_fails_before_backbone(self):
        batch = semantic_batch(rows=2)
        batch["static_semantic"] = batch["static_semantic"][:, :255]
        with self.assertRaises(ValueError):
            self.model(batch)
        self.assertEqual(self.backbone.calls, 0)

    def test_online_qwen_row_chunking_preserves_outputs(self):
        batch = semantic_batch(rows=8)
        with torch.no_grad():
            reference = self.model(batch)["commit_time_macro"]
        self.assertEqual(self.backbone.calls, 1)
        self.model.config.backbone_core_chunk_size = 2
        with torch.no_grad():
            actual = self.model(batch)["commit_time_macro"]
        self.assertEqual(self.backbone.calls, 5)
        torch.testing.assert_close(actual, reference, rtol=0.0, atol=0.0)

    def test_activation_diagnostics_are_opt_in_and_count_valid_elements(self):
        batch = semantic_batch(rows=2)
        batch["valid_macro_mask"][0, 200:] = False
        with torch.no_grad():
            plain = self.model(batch)
        self.assertNotIn("activation_statistics", plain)
        self.model.collect_activation_diagnostics = True
        with torch.no_grad():
            diagnosed = self.model(batch)
        statistics = diagnosed["activation_statistics"]
        expected = (200 + 256) * 48
        self.assertEqual(
            int(statistics["llm_branch_used"]["count"]), expected,
        )
        for values in statistics.values():
            self.assertTrue(torch.isfinite(values["sum_squares"]))

    def test_chunk_checkpoint_preserves_full_gradient_path(self):
        torch.manual_seed(47)
        backbone = CountingEmbedBackbone()
        model = MacroV29TimingModel(
            backbone,
            MacroV29Config(
                d_llm=24,
                d_model=48,
                d_field=4,
                n_heads=4,
                semantic_input_mode="cached_macro_soft_token",
                semantic_dim=12,
                backbone_core_chunk_size=2,
                backbone_chunk_checkpoint=True,
                cross_target_block=2,
            ),
        ).train()
        output = model(semantic_batch(rows=4))
        output["commit_time_macro"].mean().backward()
        groups = {
            "backbone": backbone.projection.weight.grad,
            "semantic": model.semantic_adapter[1].weight.grad,
            "cross": model.core_mixer.r_proj.weight.grad,
            "head": model.gap_head[0].weight.grad,
        }
        for name, gradient in groups.items():
            self.assertIsNotNone(gradient, msg=f"missing {name} gradient")
            self.assertTrue(
                torch.isfinite(gradient).all(), msg=f"non-finite {name} gradient",
            )
            self.assertGreater(
                float(gradient.abs().sum()), 0.0,
                msg=f"zero {name} gradient",
            )
        self.assertGreater(backbone.calls, 2)


class OrdinaryBackboneControlTest(unittest.TestCase):
    def test_online_transformer_is_strictly_causal_with_tail_mask(self):
        torch.manual_seed(101)
        transformer = CausalMacroTransformer(
            24, 48, 4, 2, 4, 0.0,
        ).eval()
        generator = torch.Generator().manual_seed(103)
        values = torch.randn(2, 256, 24, generator=generator)
        valid = torch.ones(2, 256, dtype=torch.bool)
        valid[0, 230:] = False
        changed = values.clone()
        changed[:, 129:] = torch.randn(
            2, 127, 24, generator=generator,
        ) * 17.0
        with torch.no_grad():
            reference = transformer(values, valid)
            actual = transformer(changed, valid)
        torch.testing.assert_close(
            actual[:, :129], reference[:, :129], rtol=0.0, atol=1.0e-6,
        )
        self.assertEqual(float(actual[0, 230:].abs().sum()), 0.0)

    def test_B_reads_real_cached_semantics(self):
        torch.manual_seed(107)
        model = ordinary_model().eval()
        batch = semantic_batch(rows=2)
        changed = dict(batch)
        changed["static_semantic"] = batch["static_semantic"].clone()
        changed["static_semantic"][:, :128] += 3.0
        with torch.no_grad():
            reference = model._semantic_macro_features(
                batch, batch["valid_macro_mask"],
            )
            actual = model._semantic_macro_features(
                changed, changed["valid_macro_mask"],
            )
        self.assertGreater(float((actual - reference).abs().sum()), 0.0)

    def test_E_uses_only_learned_null_and_has_full_gradient_path(self):
        torch.manual_seed(109)
        model = ordinary_model(learned_null=True).train()
        batch = semantic_batch(rows=2)
        del batch["static_semantic"]
        del batch["static_anchor"]
        batch["null_semantic_marker"] = torch.zeros(
            2, 256, dtype=torch.uint8,
        )
        output = model(batch)
        output["commit_time_macro"].mean().backward()
        gradients = {
            "null": model.null_semantic.grad,
            "adapter": model.semantic_adapter[1].weight.grad,
            "ordinary": model.online_transformer.layers[0].q_proj.weight.grad,
            "cross": model.core_mixer.r_proj.weight.grad,
            "head": model.gap_head[0].weight.grad,
        }
        for name, gradient in gradients.items():
            self.assertIsNotNone(gradient, msg=f"missing {name} gradient")
            self.assertTrue(
                torch.isfinite(gradient).all(),
                msg=f"non-finite {name} gradient",
            )
            self.assertGreater(
                float(gradient.abs().sum()), 0.0,
                msg=f"zero {name} gradient",
            )

    def test_B_E_common_parameters_have_identical_initialization(self):
        torch.manual_seed(113)
        model_b = ordinary_model()
        torch.manual_seed(113)
        model_e = ordinary_model(learned_null=True)
        state_b = model_b.state_dict()
        state_e = model_e.state_dict()
        common = sorted(set(state_b) & set(state_e))
        self.assertTrue(common)
        for name in common:
            torch.testing.assert_close(
                state_b[name], state_e[name], rtol=0.0, atol=0.0,
                msg=lambda message, key=name: f"{key}: {message}",
            )

    def test_formal_ordinary_backbone_matches_A_trainable_capacity(self):
        transformer = CausalMacroTransformer(
            1536, 384, 8, 5, 4, 0.0,
        )
        ordinary_parameters = sum(
            parameter.numel() for parameter in transformer.parameters()
        )
        a_lora_plus_projection = 9_309_568
        self.assertEqual(ordinary_parameters, 9_556_992)
        self.assertLess(
            abs(ordinary_parameters - a_lora_plus_projection)
            / a_lora_plus_projection,
            0.05,
        )


if __name__ == "__main__":
    unittest.main()
