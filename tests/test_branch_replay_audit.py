from __future__ import annotations

import numpy as np
import pytest

from tcsim.branch_replay import (
    BTBConfig,
    BranchEvent,
    IndirectConfig,
    RASConfig,
    ReplayConfig,
    TournamentConfig,
    aggregate_audit_reports,
    audit_core_stream,
)
from tcsim.v29.contracts import FIELD_INDEX, FIELD_NAMES


def _config():
    return ReplayConfig(
        tournament=TournamentConfig(
            local_predictor_size=8,
            local_counter_bits=2,
            local_history_table_size=8,
            global_predictor_size=8,
            global_counter_bits=2,
            choice_predictor_size=8,
            choice_counter_bits=2,
            inst_shift=0,
        ),
        btb=BTBConfig(entries=8, associativity=1, tag_bits=8, set_shift=0),
        ras=RASConfig(entries=4),
        indirect=IndirectConfig(
            sets=8, ways=1, tag_bits=8, path_length=2,
            speculative_path_length=8, ghr_bits=3, inst_shift=0,
        ),
    )


def _event(pc, next_pc, *, taken=True, conditional=False):
    return BranchEvent(
        pc=pc,
        taken=taken,
        target=next_pc if taken else 0,
        next_pc=next_pc,
        conditional=conditional,
    )


def _arrays(events, indices, labels):
    n_uops = 8
    branch = np.zeros(n_uops, dtype=np.uint8)
    branch_miss = np.zeros(n_uops, dtype=np.uint8)
    macro_pc = np.zeros(n_uops, dtype=np.uint64)
    fields = np.zeros((n_uops, len(FIELD_NAMES)), dtype=np.uint16)
    for event, index, label in zip(events, indices, labels):
        branch[index] = 1
        branch_miss[index] = int(label)
        macro_pc[index] = event.pc
        fields[index, FIELD_INDEX["branch_kind"]] = (
            1 | (int(event.conditional) << 1)
        )
        fields[index, FIELD_INDEX["branch_taken"]] = 2 if event.taken else 1
    return {
        "branch": branch,
        "branch_miss": branch_miss,
        "macro_pc": macro_pc,
        "fields": fields,
    }


def test_event_and_fixed_uop_window_audit_keep_fp_fn_visible():
    events = [
        _event(0x10, 0x80),
        _event(0x10, 0x80),
        _event(0x20, 0x24, taken=False, conditional=True),
    ]
    arrays = _arrays(events, [0, 3, 7], [True, True, False])
    report = audit_core_stream(
        events, arrays, _config(), core_id=0, window_sizes=(4,), cold_branches=1
    )

    assert report["alignment"]["passed"] is True
    assert report["event"]["tp"] == 1
    assert report["event"]["fp"] == 0
    assert report["event"]["fn"] == 1
    assert report["event"]["tn"] == 1
    assert report["event"]["precision"] == 1.0
    assert report["event"]["recall"] == 0.5
    assert report["event"]["f1"] == pytest.approx(2.0 / 3.0)

    window = report["windows"]["4"]
    assert window["branch_windows"] == 2
    assert window["abs_error_sum"] == 1.0
    assert window["normalized_count_l1"] == 0.5
    assert window["exact_match_fraction"] == 0.5
    assert window["within_one_fraction"] == 1.0


def test_alignment_detects_pc_identity_mismatch():
    events = [_event(0x10, 0x80)]
    arrays = _arrays(events, [0], [True])
    arrays["macro_pc"][0] = 0x11
    report = audit_core_stream(
        events, arrays, _config(), core_id=0, window_sizes=(4,)
    )
    assert report["alignment"]["passed"] is False
    assert report["alignment"]["pc_mismatches"] == 1


def test_nested_aggregate_preserves_physical_core_count():
    events = [_event(0x10, 0x80)]
    core = audit_core_stream(
        events, _arrays(events, [0], [True]), _config(),
        core_id=0, window_sizes=(4,),
    )
    trace = aggregate_audit_reports([core, {**core, "core_id": 1}])
    suite = aggregate_audit_reports([trace, trace])
    assert trace["alignment"]["cores"] == 2
    assert trace["alignment"]["passed_cores"] == 2
    assert suite["alignment"]["cores"] == 4
    assert suite["alignment"]["passed_cores"] == 4
