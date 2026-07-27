"""Configuration contract for the standalone branch-predictor replay.

The parser accepts either the ``branch_predictor`` subtree from a TCSim
uarch profile or a complete v29 ``meta.json`` mapping.  Values in collected
profiles are strings, while hand-written replay configs commonly use native
JSON values; both forms are accepted.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from typing import Any, Mapping


def _power_of_two(value: int, name: str) -> int:
    number = int(value)
    if number <= 0 or number & (number - 1):
        raise ValueError(f"{name} must be a positive power of two, got {number}")
    return number


def _integer(value: Any, default: int) -> int:
    return int(default if value is None else value)


def _boolean(value: Any, default: bool) -> bool:
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"invalid boolean value {value!r}")


def _normalized(mapping: Mapping[str, Any]) -> dict[str, Any]:
    return {str(key).replace("_", "").lower(): value for key, value in mapping.items()}


def _value(mapping: Mapping[str, Any], name: str, default: Any = None) -> Any:
    return _normalized(mapping).get(name.replace("_", "").lower(), default)


def _section(mapping: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = _value(mapping, name, {})
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(f"branch predictor section {name!r} must be a mapping")
    return value


@dataclass(frozen=True)
class TournamentConfig:
    local_predictor_size: int = 2048
    local_counter_bits: int = 2
    local_history_table_size: int = 2048
    global_predictor_size: int = 8192
    global_counter_bits: int = 2
    choice_predictor_size: int = 8192
    choice_counter_bits: int = 2
    inst_shift: int = 0

    def validate(self) -> None:
        _power_of_two(self.local_predictor_size, "local_predictor_size")
        _power_of_two(self.local_history_table_size, "local_history_table_size")
        _power_of_two(self.global_predictor_size, "global_predictor_size")
        _power_of_two(self.choice_predictor_size, "choice_predictor_size")
        for name, bits in (
            ("local_counter_bits", self.local_counter_bits),
            ("global_counter_bits", self.global_counter_bits),
            ("choice_counter_bits", self.choice_counter_bits),
        ):
            if not 1 <= int(bits) <= 8:
                raise ValueError(f"{name} must be in [1,8], got {bits}")
        if int(self.inst_shift) < 0:
            raise ValueError("Tournament inst_shift must be non-negative")


@dataclass(frozen=True)
class BTBConfig:
    entries: int = 4096
    associativity: int = 1
    tag_bits: int = 16
    set_shift: int = 0
    replacement_policy: str = "LRURP"
    indexing_policy: str = "BTBSetAssociative"

    def validate(self) -> None:
        if int(self.entries) <= 0 or int(self.associativity) <= 0:
            raise ValueError("BTB entries and associativity must be positive")
        if int(self.entries) % int(self.associativity):
            raise ValueError("BTB entries must be divisible by associativity")
        _power_of_two(
            int(self.entries) // int(self.associativity), "BTB number of sets"
        )
        if not 1 <= int(self.tag_bits) <= 64:
            raise ValueError("BTB tag_bits must be in [1,64]")
        if int(self.set_shift) < 0:
            raise ValueError("BTB set_shift must be non-negative")
        if self.replacement_policy != "LRURP":
            raise ValueError(
                f"unsupported BTB replacement policy {self.replacement_policy!r}"
            )
        if self.indexing_policy != "BTBSetAssociative":
            raise ValueError(
                f"unsupported BTB indexing policy {self.indexing_policy!r}"
            )


@dataclass(frozen=True)
class RASConfig:
    entries: int = 16

    def validate(self) -> None:
        if int(self.entries) <= 0:
            raise ValueError("RAS entries must be positive")


@dataclass(frozen=True)
class IndirectConfig:
    sets: int = 256
    ways: int = 2
    tag_bits: int = 16
    path_length: int = 3
    speculative_path_length: int = 256
    ghr_bits: int = 13
    inst_shift: int = 0
    hash_ghr: bool = True
    hash_targets: bool = True
    # gem5's SimpleIndirectPredictor calls libc rand() without an explicit
    # seed.  glibc starts that stream at seed 1; making it explicit keeps the
    # standalone replay deterministic and reports the assumption.
    replacement_seed: int = 1

    def validate(self) -> None:
        _power_of_two(self.sets, "indirect sets")
        if int(self.ways) <= 0:
            raise ValueError("indirect ways must be positive")
        if not 1 <= int(self.tag_bits) <= 31:
            raise ValueError("indirect tag_bits must be in [1,31]")
        if int(self.path_length) <= 0:
            raise ValueError("indirect path_length must be positive")
        if int(self.speculative_path_length) < 0:
            raise ValueError("indirect speculative_path_length must be non-negative")
        if not 1 <= int(self.ghr_bits) <= 31:
            raise ValueError("indirect ghr_bits must be in [1,31]")
        if int(self.inst_shift) < 0:
            raise ValueError("indirect inst_shift must be non-negative")


@dataclass(frozen=True)
class ReplayConfig:
    tournament: TournamentConfig = TournamentConfig()
    btb: BTBConfig = BTBConfig()
    ras: RASConfig = RASConfig()
    indirect: IndirectConfig = IndirectConfig()
    num_threads: int = 1
    requires_btb_hit: bool = False
    update_btb_at_squash: bool = True
    speculative_history_update: bool = True
    direction_family: str = "TournamentBP"

    def validate(self) -> None:
        if self.direction_family != "TournamentBP":
            raise ValueError(
                f"unsupported direction predictor {self.direction_family!r}; "
                "this milestone implements TournamentBP only"
            )
        if int(self.num_threads) <= 0:
            raise ValueError("num_threads must be positive")
        _power_of_two(self.num_threads, "num_threads")
        # Current gem5 TournamentBP updates histories speculatively regardless
        # of the inherited parameter.  Reject false instead of pretending to
        # implement semantics absent from tournament.cc.
        if not self.speculative_history_update:
            raise ValueError(
                "TournamentBP replay requires speculative_history_update=true"
            )
        self.tournament.validate()
        self.btb.validate()
        self.ras.validate()
        self.indirect.validate()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def stable_hash(self) -> str:
        payload = json.dumps(
            self.to_dict(), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    @classmethod
    def from_mapping(cls, source: Mapping[str, Any]) -> "ReplayConfig":
        current: Mapping[str, Any] = source
        uarch = _value(current, "uarch_profile")
        if isinstance(uarch, Mapping):
            current = uarch
        predictor = _value(current, "branch_predictor")
        if isinstance(predictor, Mapping):
            current = predictor

        root = _section(current, "root")
        direction = _section(current, "conditionalBranchPred")
        btb = _section(current, "btb")
        btb_index = _section(current, "btb.btbIndexingPolicy")
        btb_repl = _section(current, "btb.btbReplPolicy")
        ras = _section(current, "ras")
        indirect = _section(current, "indirectBranchPred")

        required_sections = {
            "root": root,
            "conditionalBranchPred": direction,
            "btb": btb,
            "btb.btbIndexingPolicy": btb_index,
            "btb.btbReplPolicy": btb_repl,
            "ras": ras,
            "indirectBranchPred": indirect,
        }
        missing_sections = [
            name for name, section in required_sections.items() if not section
        ]
        if missing_sections:
            raise ValueError(
                "predictor config is incomplete; missing sections "
                + ", ".join(missing_sections)
            )

        direction_type = str(_value(direction, "type", "TournamentBP"))
        root_type = str(_value(root, "type", "BranchPredictor"))
        btb_type = str(_value(btb, "type", "SimpleBTB"))
        ras_type = str(_value(ras, "type", "ReturnAddrStack"))
        indirect_type = str(
            _value(indirect, "type", "SimpleIndirectPredictor")
        )
        if root_type != "BranchPredictor":
            raise ValueError(f"unsupported BPredUnit type {root_type!r}")
        if btb_type != "SimpleBTB":
            raise ValueError(f"unsupported BTB type {btb_type!r}")
        if ras_type != "ReturnAddrStack":
            raise ValueError(f"unsupported RAS type {ras_type!r}")
        if indirect_type != "SimpleIndirectPredictor":
            raise ValueError(f"unsupported indirect predictor {indirect_type!r}")

        root_shift = _integer(_value(root, "instShiftAmt"), 0)
        root_threads = _integer(_value(root, "numThreads"), 1)
        for name, section in (
            ("conditionalBranchPred", direction),
            ("btb", btb),
            ("ras", ras),
            ("indirectBranchPred", indirect),
        ):
            child_threads = _integer(_value(section, "numThreads"), root_threads)
            if child_threads != root_threads:
                raise ValueError(
                    f"predictor thread count mismatch root={root_threads} "
                    f"{name}={child_threads}"
                )
        btb_entries = _integer(_value(btb, "numEntries"), 4096)
        btb_assoc = _integer(_value(btb, "associativity"), 1)
        btb_tag_bits = _integer(_value(btb, "tagBits"), 16)
        for name, parent_value, child_value in (
            (
                "entries",
                btb_entries,
                _integer(_value(btb_index, "num_entries"), btb_entries),
            ),
            (
                "associativity",
                btb_assoc,
                _integer(_value(btb_index, "assoc"), btb_assoc),
            ),
            (
                "tag_bits",
                btb_tag_bits,
                _integer(_value(btb_index, "tag_bits"), btb_tag_bits),
            ),
        ):
            if parent_value != child_value:
                raise ValueError(
                    f"BTB {name} mismatch parent={parent_value} "
                    f"indexing_policy={child_value}"
                )
        config = cls(
            num_threads=root_threads,
            requires_btb_hit=_boolean(_value(root, "requiresBTBHit"), False),
            update_btb_at_squash=_boolean(
                _value(root, "updateBTBAtSquash"), True
            ),
            speculative_history_update=_boolean(
                _value(root, "speculativeHistUpdate"), True
            ),
            direction_family=direction_type,
            tournament=TournamentConfig(
                local_predictor_size=_integer(
                    _value(direction, "localPredictorSize"), 2048
                ),
                local_counter_bits=_integer(_value(direction, "localCtrBits"), 2),
                local_history_table_size=_integer(
                    _value(direction, "localHistoryTableSize"), 2048
                ),
                global_predictor_size=_integer(
                    _value(direction, "globalPredictorSize"), 8192
                ),
                global_counter_bits=_integer(
                    _value(direction, "globalCtrBits"), 2
                ),
                choice_predictor_size=_integer(
                    _value(direction, "choicePredictorSize"), 8192
                ),
                choice_counter_bits=_integer(
                    _value(direction, "choiceCtrBits"), 2
                ),
                inst_shift=_integer(_value(direction, "instShiftAmt"), root_shift),
            ),
            btb=BTBConfig(
                entries=btb_entries,
                associativity=btb_assoc,
                tag_bits=btb_tag_bits,
                set_shift=_integer(
                    _value(btb_index, "set_shift"),
                    _integer(_value(btb, "instShiftAmt"), root_shift),
                ),
                replacement_policy=str(_value(btb_repl, "type", "LRURP")),
                indexing_policy=str(
                    _value(btb_index, "type", "BTBSetAssociative")
                ),
            ),
            ras=RASConfig(entries=_integer(_value(ras, "numEntries"), 16)),
            indirect=IndirectConfig(
                sets=_integer(_value(indirect, "indirectSets"), 256),
                ways=_integer(_value(indirect, "indirectWays"), 2),
                tag_bits=_integer(_value(indirect, "indirectTagSize"), 16),
                path_length=_integer(
                    _value(indirect, "indirectPathLength"), 3
                ),
                speculative_path_length=_integer(
                    _value(indirect, "speculativePathLength"), 256
                ),
                ghr_bits=_integer(_value(indirect, "indirectGHRBits"), 13),
                inst_shift=_integer(
                    _value(indirect, "instShiftAmt"), root_shift
                ),
                hash_ghr=_boolean(_value(indirect, "indirectHashGHR"), True),
                hash_targets=_boolean(
                    _value(indirect, "indirectHashTargets"), True
                ),
                replacement_seed=_integer(
                    _value(indirect, "replacementSeed"), 1
                ),
            ),
        )
        config.validate()
        return config
