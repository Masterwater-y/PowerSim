from __future__ import annotations

import pytest

from tcsim.branch_replay import (
    BTBConfig,
    BranchEvent,
    IndirectConfig,
    RASConfig,
    ReplayConfig,
    TournamentBPUReplay,
    TournamentConfig,
    attach_v29_meta_evaluation,
    replay_core_streams,
)
from tcsim.branch_replay.replay import GlibcRand, SimpleBTB, TournamentPredictor


def _small_config(**overrides):
    values = {
        "tournament": TournamentConfig(
            local_predictor_size=8,
            local_counter_bits=2,
            local_history_table_size=8,
            global_predictor_size=8,
            global_counter_bits=2,
            choice_predictor_size=8,
            choice_counter_bits=2,
            inst_shift=0,
        ),
        "btb": BTBConfig(
            entries=8, associativity=1, tag_bits=8, set_shift=0
        ),
        "ras": RASConfig(entries=4),
        "indirect": IndirectConfig(
            sets=8,
            ways=1,
            tag_bits=8,
            path_length=2,
            speculative_path_length=8,
            ghr_bits=3,
            inst_shift=0,
        ),
    }
    values.update(overrides)
    return ReplayConfig(**values)


def _event(
    pc,
    next_pc,
    *,
    taken=True,
    conditional=False,
    indirect=False,
    call=False,
    return_=False,
    history=None,
):
    return BranchEvent(
        pc=pc,
        taken=taken,
        target=next_pc if taken else 0,
        next_pc=next_pc,
        conditional=conditional,
        indirect=indirect,
        call=call,
        return_=return_,
        branch_history=history,
    )


def test_glibc_rand_matches_gem5_linux_process_stream():
    random = GlibcRand(1)
    assert [random.next() for _ in range(5)] == [
        1804289383,
        846930886,
        1681692777,
        1714636915,
        1957747793,
    ]


def test_tournament_golden_initial_repair_and_commit_state():
    config = _small_config().tournament
    predictor = TournamentPredictor(config, 1)

    prediction, history = predictor.lookup(0, 0x10)
    assert prediction is False
    assert history.global_history == 0
    assert history.local_history == 0
    assert history.global_used is False

    predictor.speculative_update(0, False, history)
    predictor.repair(0, True, history)
    predictor.commit(True, history)

    assert predictor.global_histories[0] == 1
    assert predictor.local_histories[0] == 1
    assert predictor.global_counters[0] == 1
    assert predictor.local_counters[0] == 1
    assert predictor.choice_counters[0] == 0

    # gem5 trains the global counter and GHR for unconditional branches too.
    uncond = predictor.speculative_update(0, True, None)
    predictor.commit(True, uncond)
    assert predictor.global_histories[0] == 3
    assert predictor.global_counters[1] == 1
    assert sum(predictor.local_counters) == 1


def test_simple_btb_golden_set_tag_lru_and_update_replacement():
    btb = SimpleBTB(
        BTBConfig(entries=4, associativity=2, tag_bits=8, set_shift=0), 1
    )
    # Two tags in set zero.  Touch PC 0, then insertion must evict PC 2.
    btb.update(0, 0, 100)
    btb.update(0, 2, 102)
    assert btb.lookup(0, 0) == 100
    btb.update(0, 4, 104)
    assert btb.lookup(0, 0) == 100
    assert btb.lookup(0, 2) is None
    assert btb.lookup(0, 4) == 104


def test_full_direct_target_provider_and_wrong_target_flow():
    replay = TournamentBPUReplay(_small_config())
    first = replay.process(_event(0x10, 0x80))
    second = replay.process(_event(0x10, 0x80))
    changed = replay.process(_event(0x10, 0x90))
    fourth = replay.process(_event(0x10, 0x90))

    assert first.full_miss and not first.btb_hit
    assert second.target_provider == "BTB" and not second.full_miss
    assert changed.btb_hit and changed.target_miss and changed.full_miss
    assert fourth.target_provider == "BTB" and not fourth.full_miss
    assert replay.report()["full_misses"] == 2


def test_indirect_predictor_overrides_absent_btb_after_cold_training():
    replay = TournamentBPUReplay(_small_config())
    cold = replay.process(_event(0x20, 0xA0, indirect=True))
    # GHR/path hashing moves the allocation set while history warms.  Once the
    # all-taken history repeats, the previously recorded target is reusable.
    for _ in range(3):
        replay.process(_event(0x20, 0xA0, indirect=True))
    warm = replay.process(_event(0x20, 0xA0, indirect=True))

    assert cold.indirect_lookup and not cold.indirect_hit and cold.full_miss
    assert warm.indirect_lookup and warm.indirect_hit
    assert warm.target_provider == "Indirect"
    assert warm.predicted_target == 0xA0
    assert not warm.full_miss
    # requiresBTBHit=false suppresses indirect allocation in the BTB.
    assert not warm.btb_hit


def test_causal_ras_learns_return_only_after_first_resolved_return():
    replay = TournamentBPUReplay(_small_config())
    first_call = replay.process(_event(0x100, 0x500, call=True))
    first_return = replay.process(
        _event(0x700, 0x105, indirect=True, return_=True)
    )
    second_call = replay.process(_event(0x100, 0x500, call=True))
    second_return = replay.process(
        _event(0x700, 0x105, indirect=True, return_=True)
    )

    assert first_call.full_miss
    assert first_return.ras_target_unknown and first_return.full_miss
    assert not second_call.full_miss
    assert second_return.target_provider == "RAS"
    assert second_return.predicted_target == 0x105
    assert not second_return.full_miss


def test_requires_btb_hit_gates_indirect_but_config_change_is_effective():
    config = _small_config(requires_btb_hit=True)
    replay = TournamentBPUReplay(config)
    cold = replay.process(_event(0x40, 0xC0, indirect=True))
    warm = replay.process(_event(0x40, 0xC0, indirect=True))

    assert not cold.btb_hit and not cold.indirect_lookup and cold.full_miss
    assert warm.btb_hit and warm.indirect_lookup
    assert warm.target_provider == "BTB"
    assert not warm.full_miss


def test_functional_history_audit_uses_only_committed_outcomes():
    replay = TournamentBPUReplay(_small_config())
    replay.process(_event(1, 10, history=0))
    replay.process(_event(2, 3, taken=False, conditional=True, history=1))
    replay.process(_event(3, 20, history=2))
    report = replay.report()
    assert report["functional_history_checks"] == 3
    assert report["functional_history_mismatches"] == 0


def test_profile_parser_tracks_current_gem5_parameters_and_hard_fails_tage():
    profile = {
        "branch_predictor": {
            "root": {
                "numthreads": "1",
                "requiresbtbhit": "false",
                "updatebtbatsquash": "true",
                "speculativehistupdate": "true",
            },
            "conditionalBranchPred": {
                "type": "TournamentBP",
                "localpredictorsize": "16",
                "localhistorytablesize": "8",
                "globalpredictorsize": "32",
                "choicepredictorsize": "64",
            },
            "btb": {
                "type": "SimpleBTB",
                "numentries": "128",
                "associativity": "2",
                "tagbits": "12",
            },
            "btb.btbIndexingPolicy": {
                "type": "BTBSetAssociative",
                "set_shift": "1",
            },
            "btb.btbReplPolicy": {"type": "LRURP"},
            "ras": {"type": "ReturnAddrStack", "numentries": "8"},
            "indirectBranchPred": {
                "type": "SimpleIndirectPredictor",
                "indirectsets": "64",
                "indirectways": "4",
                "indirecttagsize": "10",
                "indirectpathlength": "2",
                "speculativepathlength": "16",
                "indirectghrbits": "6",
            },
        }
    }
    config = ReplayConfig.from_mapping(profile)
    assert config.tournament.local_predictor_size == 16
    assert config.tournament.local_history_table_size == 8
    assert config.btb.entries == 128
    assert config.btb.associativity == 2
    assert config.btb.set_shift == 1
    assert config.ras.entries == 8
    assert config.indirect.ways == 4

    profile["branch_predictor"]["conditionalBranchPred"]["type"] = "TAGE"
    with pytest.raises(ValueError, match="TournamentBP only"):
        ReplayConfig.from_mapping(profile)


def test_btb_config_change_alters_real_replay_capacity_and_outcome():
    stream = [
        _event(0, 100),
        _event(2, 102),
        _event(0, 100),
    ]
    small = TournamentBPUReplay(
        _small_config(btb=BTBConfig(entries=2, associativity=1, tag_bits=8))
    ).run(stream)
    large = TournamentBPUReplay(
        _small_config(btb=BTBConfig(entries=4, associativity=1, tag_bits=8))
    ).run(stream)
    assert small["full_misses"] == 3
    assert large["full_misses"] == 2


def test_label_summary_is_attached_only_after_functional_replay():
    config = _small_config()
    report = replay_core_streams(
        [(7, [_event(0x10, 0x80), _event(0x10, 0x80)])], config
    )
    assert report["oracle_labels_consumed_as_input"] is False
    attach_v29_meta_evaluation(
        report,
        {"cores": [{"core_id": 7, "n_branches": 2, "n_branch_misses": 1}]},
    )
    assert report["true_misses"] == 1
    assert report["predicted_misses"] == 1
    assert report["miss_count_abs_relative_error"] == 0.0
    assert report["oracle_labels_consumed_as_input"] is False
    assert report["oracle_labels_used_post_replay_for_evaluation"] is True


def test_incomplete_predictor_profile_hard_fails_instead_of_using_defaults():
    with pytest.raises(ValueError, match="incomplete"):
        ReplayConfig.from_mapping({"branch_predictor": {"root": {}}})
