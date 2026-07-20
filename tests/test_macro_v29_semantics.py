from __future__ import annotations

from collections import Counter
import unittest

from train.macro_v29_dataset import SemanticVariantInstructionResolver


class FakeStaticResolver:
    def __init__(self):
        self.rows = {
            100: {
                "mnemonic": "mov", "is_branch": False,
            },
            200: {
                "mnemonic": "imul", "is_branch": False,
            },
            300: {
                "mnemonic": "cmp", "is_branch": False,
            },
            400: {
                "mnemonic": "jne", "is_branch": True,
            },
        }
        self.rendered = [
            "mov rax, qword ptr [r8+0x40]",
            "imul r10d, eax, 7",
            "cmp rcx, rdx",
            "jne .L_back_3",
        ]

    def render_window(self, macro_pcs):
        positions = {pc: index for index, pc in enumerate((100, 200, 300, 400))}
        return [self.rendered[positions[int(pc)]] for pc in macro_pcs]

    def is_architectural_branch(self, macro_pc):
        return bool(self.rows[int(macro_pc)]["is_branch"])

    def coverage(self, macro_pcs):
        missing = [int(pc) for pc in macro_pcs if int(pc) not in self.rows]
        return {"n_missing": len(missing), "missing": missing}


def mnemonic(text: str) -> str:
    return text.split(maxsplit=1)[0]


def operands(text: str) -> str:
    pieces = text.split(maxsplit=1)
    return pieces[1] if len(pieces) == 2 else ""


class SemanticVariantTest(unittest.TestCase):
    def setUp(self):
        self.base = FakeStaticResolver()
        self.pcs = [100, 200, 300, 400]

    def test_pseudo_is_coarse_deterministic_and_address_free(self):
        resolver = SemanticVariantInstructionResolver(self.base, "pseudo")
        first = resolver.render_window(self.pcs)
        second = resolver.render_window(self.pcs)
        self.assertEqual(first, second)
        self.assertEqual(first, [
            "mov rax, qword ptr [rbx]",
            "add rax, rbx",
            "cmp rax, rbx",
            "jmp .L_back_3",
        ])
        self.assertNotIn("0x40", "\n".join(first))
        self.assertTrue(resolver.is_architectural_branch(400))

    def test_mnemonic_shuffle_preserves_frequency_and_operands(self):
        resolver = SemanticVariantInstructionResolver(
            self.base, "mnemonic_shuffle",
        )
        real = self.base.render_window(self.pcs)
        first = resolver.render_window(self.pcs)
        second = resolver.render_window(self.pcs)
        self.assertEqual(first, second)
        self.assertNotEqual(first, real)
        self.assertEqual(
            Counter(map(mnemonic, first)), Counter(map(mnemonic, real)),
        )
        self.assertEqual(list(map(operands, first)), list(map(operands, real)))

    def test_register_rename_is_consistent_and_keeps_special_stack_regs(self):
        resolver = SemanticVariantInstructionResolver(
            self.base, "register_rename",
        )
        renamed = resolver.render_window(self.pcs)
        self.assertEqual(renamed[0], "mov rbx, qword ptr [r9+0x40]")
        self.assertEqual(renamed[1], "imul r11d, ebx, 7")
        self.assertEqual(renamed[2], "cmp rdx, rcx")
        self.assertEqual(
            resolver._register_rename(["mov rbp, rsp"])[0],
            "mov rbp, rsp",
        )


if __name__ == "__main__":
    unittest.main()
