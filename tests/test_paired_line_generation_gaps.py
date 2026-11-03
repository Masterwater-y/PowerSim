#!/usr/bin/env python3

import importlib.util
import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "paired_line_generation_gaps",
    ROOT / "tools" / "analyze_paired_line_generation_gaps.py")
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def event(sequence, fs_admission, fs_response,
          native_admission, native_response, coalesced):
    return MODULE.Event(
        core=0, sequence=sequence, line=10,
        fastsim_admission=fs_admission,
        fastsim_response=fs_response,
        native_admission_tick=native_admission,
        native_response_tick=native_response,
        native_coalesced=coalesced)


class PairedLineGenerationGapTest(unittest.TestCase):
    def test_native_only_axes_remain_separate(self):
        # Each pair is isolated by a different line.  The first mismatch is
        # fixed by native spacing alone; the second by native lifetime alone.
        first_parent = event(1, 0, 5, 0, 7, False)
        first_child = event(2, 8, 9, 4, 7, True)
        second_parent = event(3, 20, 25, 20, 30, False)
        second_parent.line = 11
        second_child = event(4, 28, 29, 26, 30, True)
        second_child.line = 11
        result = MODULE.analyze(
            [first_parent, first_child, second_parent, second_child], 1)
        self.assertEqual(result["native_oracle_mismatches"], 0)
        self.assertEqual(
            result["disagreement_axis_classification"]["native_only"],
            {"issue_spacing_only_suffices": 1,
             "parent_lifetime_only_suffices": 1})

    def test_callback_boundary_is_half_open(self):
        events = [
            event(1, 0, 5, 0, 5, False),
            event(2, 5, 6, 5, 6, False),
        ]
        result = MODULE.analyze(events, 1)
        self.assertEqual(
            result["fastsim_vs_native_oracle"],
            {"both": 0, "fastsim_only": 0,
             "native_only": 0, "neither": 2})

    def test_oracle_mismatch_is_not_axis_evidence(self):
        events = [
            event(1, 0, 10, 0, 10, False),
            event(2, 5, 10, 5, 10, False),
        ]
        result = MODULE.analyze(events, 1)
        self.assertEqual(result["native_oracle_mismatches"], 1)
        self.assertEqual(
            result["disagreement_axis_classification"]["native_only"], {})
        self.assertEqual(
            result["disagreement_axis_classification"]["fastsim_only"], {})


if __name__ == "__main__":
    unittest.main()
