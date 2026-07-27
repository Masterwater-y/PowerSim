"""Functional-only, standalone replay of gem5's Tournament branch BPU.

This module deliberately has no gem5 import or runtime dependency.  It mirrors
the current TournamentBP, SimpleBTB, ReturnAddrStack and
SimpleIndirectPredictor provider/update order.  Events are retired correct-path
facts, so wrong-path pollution and overlapping in-flight branches remain an
explicit approximation boundary.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
import math
from typing import Any, Iterable, Optional

from .config import BTBConfig, IndirectConfig, RASConfig, ReplayConfig, TournamentConfig


MAX_ADDR = (1 << 64) - 1


class BranchType(str, Enum):
    RETURN = "Return"
    CALL_DIRECT = "CallDirect"
    CALL_INDIRECT = "CallIndirect"
    DIRECT_COND = "DirectCond"
    DIRECT_UNCOND = "DirectUncond"
    INDIRECT_COND = "IndirectCond"
    INDIRECT_UNCOND = "IndirectUncond"


class TargetProvider(str, Enum):
    NONE = "NoTarget"
    BTB = "BTB"
    RAS = "RAS"
    INDIRECT = "Indirect"


@dataclass(frozen=True)
class BranchEvent:
    pc: int
    taken: bool
    target: int
    next_pc: int
    conditional: bool = False
    indirect: bool = False
    call: bool = False
    return_: bool = False
    thread_id: int = 0
    branch_history: Optional[int] = None

    def __post_init__(self) -> None:
        if int(self.pc) < 0 or int(self.next_pc) <= 0:
            raise ValueError("branch event requires non-negative PC and positive next_pc")
        if bool(self.taken) and int(self.target) != int(self.next_pc):
            raise ValueError("taken branch target must equal architectural next_pc")
        if not bool(self.taken) and int(self.target) != 0:
            raise ValueError("not-taken branch target must be zero")
        if self.return_ and not self.indirect:
            object.__setattr__(self, "indirect", True)
        if int(self.thread_id) < 0:
            raise ValueError("thread_id must be non-negative")

    @property
    def branch_type(self) -> BranchType:
        if self.return_:
            return BranchType.RETURN
        if self.call:
            return BranchType.CALL_INDIRECT if self.indirect else BranchType.CALL_DIRECT
        if self.indirect:
            return BranchType.INDIRECT_COND if self.conditional else BranchType.INDIRECT_UNCOND
        return BranchType.DIRECT_COND if self.conditional else BranchType.DIRECT_UNCOND

    @property
    def actual_target(self) -> int:
        return int(self.next_pc)


@dataclass
class TournamentHistory:
    global_history: int
    local_prediction: bool
    global_prediction: bool
    global_used: bool
    local_history_index: Optional[int]
    local_history: Optional[int]


class TournamentPredictor:
    """State transition equivalent of current gem5 ``tournament.cc``."""

    def __init__(self, config: TournamentConfig, num_threads: int) -> None:
        config.validate()
        self.config = config
        self.local_counters = [0] * config.local_predictor_size
        self.local_histories = [0] * config.local_history_table_size
        self.global_counters = [0] * config.global_predictor_size
        self.choice_counters = [0] * config.choice_predictor_size
        self.global_histories = [0] * int(num_threads)
        self.local_history_bits = (config.local_predictor_size - 1).bit_length()
        self.local_mask = (1 << self.local_history_bits) - 1
        global_bits = max(
            (config.global_predictor_size - 1).bit_length(),
            (config.choice_predictor_size - 1).bit_length(),
        )
        self.history_mask = (1 << global_bits) - 1
        self.global_mask = config.global_predictor_size - 1
        self.choice_mask = config.choice_predictor_size - 1
        self.local_threshold = (1 << (config.local_counter_bits - 1)) - 1
        self.global_threshold = (1 << (config.global_counter_bits - 1)) - 1
        self.choice_threshold = (1 << (config.choice_counter_bits - 1)) - 1

    def _local_index(self, pc: int) -> int:
        return (int(pc) >> self.config.inst_shift) & (
            self.config.local_history_table_size - 1
        )

    def lookup(self, thread_id: int, pc: int) -> tuple[bool, TournamentHistory]:
        local_history_index = self._local_index(pc)
        local_history = self.local_histories[local_history_index] & self.local_mask
        local_prediction = self.local_counters[local_history] > self.local_threshold
        global_history = self.global_histories[thread_id]
        global_prediction = (
            self.global_counters[global_history & self.global_mask]
            > self.global_threshold
        )
        global_used = (
            self.choice_counters[global_history & self.choice_mask]
            > self.choice_threshold
        )
        history = TournamentHistory(
            global_history=global_history,
            local_prediction=local_prediction,
            global_prediction=global_prediction,
            global_used=global_used,
            local_history_index=local_history_index,
            local_history=local_history,
        )
        return (global_prediction if global_used else local_prediction), history

    def speculative_update(
        self,
        thread_id: int,
        predicted_taken: bool,
        history: Optional[TournamentHistory],
    ) -> TournamentHistory:
        if history is None:
            history = TournamentHistory(
                global_history=self.global_histories[thread_id],
                local_prediction=True,
                global_prediction=True,
                global_used=True,
                local_history_index=None,
                local_history=None,
            )
        self.global_histories[thread_id] = (
            (self.global_histories[thread_id] << 1) | int(predicted_taken)
        ) & self.history_mask
        if history.local_history_index is not None:
            index = history.local_history_index
            # gem5 stores this in unsigned and only masks it at lookup.  Python
            # integers do not wrap, so keep the low 32 bits used by the source.
            self.local_histories[index] = (
                (self.local_histories[index] << 1) | int(predicted_taken)
            ) & 0xFFFFFFFF
        return history

    def repair(self, thread_id: int, actual_taken: bool, history: TournamentHistory) -> None:
        self.global_histories[thread_id] = (
            (history.global_history << 1) | int(actual_taken)
        ) & self.history_mask
        if history.local_history_index is not None and history.local_history is not None:
            self.local_histories[history.local_history_index] = (
                (history.local_history << 1) | int(actual_taken)
            ) & 0xFFFFFFFF

    @staticmethod
    def _update_counter(counters: list[int], index: int, taken: bool, bits: int) -> None:
        if taken:
            counters[index] = min((1 << bits) - 1, counters[index] + 1)
        else:
            counters[index] = max(0, counters[index] - 1)

    def commit(self, actual_taken: bool, history: TournamentHistory) -> None:
        valid_local = history.local_history is not None
        if (
            valid_local
            and history.local_prediction != history.global_prediction
        ):
            choice_index = history.global_history & self.choice_mask
            if history.local_prediction == actual_taken:
                self._update_counter(
                    self.choice_counters,
                    choice_index,
                    False,
                    self.config.choice_counter_bits,
                )
            elif history.global_prediction == actual_taken:
                self._update_counter(
                    self.choice_counters,
                    choice_index,
                    True,
                    self.config.choice_counter_bits,
                )
        self._update_counter(
            self.global_counters,
            history.global_history & self.global_mask,
            actual_taken,
            self.config.global_counter_bits,
        )
        if valid_local:
            self._update_counter(
                self.local_counters,
                int(history.local_history) & self.local_mask,
                actual_taken,
                self.config.local_counter_bits,
            )


@dataclass
class BTBEntry:
    valid: bool = False
    tag: int = MAX_ADDR
    thread_id: int = -1
    target: Optional[int] = None
    last_touch: int = 0


class SimpleBTB:
    def __init__(self, config: BTBConfig, num_threads: int) -> None:
        config.validate()
        self.config = config
        self.num_threads = int(num_threads)
        self.num_sets = config.entries // config.associativity
        self.set_mask = self.num_sets - 1
        self.tag_shift = config.set_shift + int(math.log2(self.num_sets))
        self.tag_mask = (1 << config.tag_bits) - 1 if config.tag_bits < 64 else MAX_ADDR
        self.thread_bits = (self.num_threads - 1).bit_length()
        if self.tag_shift - config.set_shift - self.thread_bits < 0:
            raise ValueError("BTB has too few sets for configured threads")
        self.entries = [
            [BTBEntry() for _ in range(config.associativity)]
            for _ in range(self.num_sets)
        ]
        self.clock = 0

    def _set(self, pc: int, thread_id: int) -> int:
        thread_shift = self.tag_shift - self.config.set_shift - self.thread_bits
        return (
            (int(pc) >> self.config.set_shift) ^ (int(thread_id) << thread_shift)
        ) & self.set_mask

    def _tag(self, pc: int) -> int:
        return (int(pc) >> self.tag_shift) & self.tag_mask

    def lookup(self, thread_id: int, pc: int) -> Optional[int]:
        self.clock += 1
        tag = self._tag(pc)
        for entry in self.entries[self._set(pc, thread_id)]:
            if entry.valid and entry.tag == tag and entry.thread_id == thread_id:
                entry.last_touch = self.clock
                return entry.target
        return None

    def update(self, thread_id: int, pc: int, target: int) -> None:
        self.clock += 1
        candidates = self.entries[self._set(pc, thread_id)]
        # AssociativeCache::findVictim does not special-case an existing tag;
        # ties select the first physical way.
        victim = min(enumerate(candidates), key=lambda pair: (pair[1].last_touch, pair[0]))[1]
        victim.valid = True
        victim.tag = self._tag(pc)
        victim.thread_id = int(thread_id)
        victim.target = int(target)
        victim.last_touch = self.clock


@dataclass
class RASFrame:
    call_pc: int
    return_target: Optional[int]


@dataclass
class RASHistory:
    pushed: bool = False
    popped: bool = False
    old_tos: int = 0
    popped_frame: Optional[RASFrame] = None


@dataclass
class _RASStack:
    entries: list[Optional[RASFrame]]
    used: int = 0
    tos: int = 0


class ReturnAddrStack:
    def __init__(self, config: RASConfig, num_threads: int) -> None:
        config.validate()
        self.capacity = int(config.entries)
        self.stacks = [
            _RASStack(entries=[None] * self.capacity) for _ in range(num_threads)
        ]

    def push(self, thread_id: int, frame: RASFrame) -> RASHistory:
        stack = self.stacks[thread_id]
        stack.tos = (stack.tos + 1) % self.capacity
        stack.entries[stack.tos] = frame
        stack.used = min(self.capacity, stack.used + 1)
        return RASHistory(pushed=True)

    def pop(self, thread_id: int) -> tuple[Optional[RASFrame], RASHistory]:
        stack = self.stacks[thread_id]
        frame = stack.entries[stack.tos]
        history = RASHistory(
            popped=True, old_tos=stack.tos, popped_frame=frame
        )
        stack.used = max(0, stack.used - 1)
        stack.tos = (stack.tos - 1) % self.capacity
        return frame, history

    def squash(self, thread_id: int, history: Optional[RASHistory]) -> None:
        if history is None:
            return
        stack = self.stacks[thread_id]
        if history.pushed:
            stack.used = max(0, stack.used - 1)
            stack.tos = (stack.tos - 1) % self.capacity
        if history.popped:
            stack.tos = history.old_tos
            stack.entries[stack.tos] = history.popped_frame
            stack.used = min(self.capacity, stack.used + 1)


class GlibcRand:
    """Pure-Python glibc random()/rand() stream used by gem5 on Linux."""

    def __init__(self, seed: int = 1) -> None:
        seed = int(seed) & 0x7FFFFFFF
        if seed == 0:
            seed = 1
        state = [seed]
        for index in range(1, 31):
            state.append((16807 * state[index - 1]) % 2147483647)
        for index in range(31, 34):
            state.append(state[index - 31])
        for index in range(34, 344):
            state.append((state[index - 31] + state[index - 3]) & 0xFFFFFFFF)
        self.state = state[-31:]
        self.position = 0

    def next(self) -> int:
        value = (
            self.state[self.position]
            + self.state[(self.position + 28) % 31]
        ) & 0xFFFFFFFF
        self.state[self.position] = value
        self.position = (self.position + 1) % 31
        return value >> 1


@dataclass
class IndirectHistory:
    ghr: int
    pc: int = MAX_ADDR
    set_index: int = 0
    tag: int = 0
    hit: bool = False
    was_indirect: bool = False


@dataclass
class _IndirectThread:
    ghr: int = 0
    path: list[tuple[int, int, int]] = field(default_factory=list)


@dataclass
class _IndirectEntry:
    tag: int = 0
    target: Optional[int] = None


class SimpleIndirectPredictor:
    def __init__(self, config: IndirectConfig, num_threads: int) -> None:
        config.validate()
        self.config = config
        self.ghr_mask = (1 << config.ghr_bits) - 1
        self.threads = [_IndirectThread() for _ in range(num_threads)]
        self.cache = [
            [_IndirectEntry() for _ in range(config.ways)]
            for _ in range(config.sets)
        ]
        self.random = GlibcRand(config.replacement_seed)

    def _set(self, pc: int, thread_id: int) -> int:
        thread = self.threads[thread_id]
        value = int(pc) >> self.config.inst_shift
        if self.config.hash_ghr:
            value ^= thread.ghr
        if self.config.hash_targets:
            shift = int(math.log2(self.config.sets)) // self.config.path_length
            for position, (_path_pc, target, _seq) in enumerate(
                reversed(thread.path[-self.config.path_length :])
            ):
                value ^= int(target) >> (
                    self.config.inst_shift + position * shift
                )
        return value & (self.config.sets - 1)

    def _tag(self, pc: int) -> int:
        return (int(pc) >> self.config.inst_shift) & ((1 << self.config.tag_bits) - 1)

    def make_history(self, thread_id: int) -> IndirectHistory:
        return IndirectHistory(ghr=self.threads[thread_id].ghr)

    def lookup(
        self, thread_id: int, pc: int
    ) -> tuple[Optional[int], IndirectHistory]:
        history = self.make_history(thread_id)
        history.pc = int(pc)
        history.was_indirect = True
        history.set_index = self._set(pc, thread_id)
        history.tag = self._tag(pc)
        for entry in self.cache[history.set_index]:
            if entry.tag == history.tag and entry.target is not None:
                history.hit = True
                return entry.target, history
        return None, history

    @staticmethod
    def _is_indirect_no_return(branch_type: BranchType) -> bool:
        return branch_type in {
            BranchType.CALL_INDIRECT,
            BranchType.INDIRECT_COND,
            BranchType.INDIRECT_UNCOND,
        }

    def speculative_update(
        self,
        thread_id: int,
        seq_num: int,
        predicted_taken: bool,
        predicted_target: int,
        branch_type: BranchType,
        history: Optional[IndirectHistory],
    ) -> IndirectHistory:
        if history is None:
            history = self.make_history(thread_id)
        history.was_indirect = self._is_indirect_no_return(branch_type)
        thread = self.threads[thread_id]
        if history.was_indirect:
            thread.path.append((history.pc, int(predicted_target), int(seq_num)))
        thread.ghr = ((thread.ghr << 1) | int(predicted_taken)) & self.ghr_mask
        return history

    def repair(
        self,
        thread_id: int,
        seq_num: int,
        actual_taken: bool,
        actual_target: int,
        branch_type: BranchType,
        history: IndirectHistory,
    ) -> None:
        thread = self.threads[thread_id]
        history.was_indirect = self._is_indirect_no_return(branch_type)
        thread.ghr = history.ghr
        if history.was_indirect:
            if thread.path:
                thread.path.pop()
            history.set_index = self._set(history.pc, thread_id)
            history.tag = self._tag(history.pc)
            thread.path.append((history.pc, int(actual_target), int(seq_num)))
        thread.ghr = ((thread.ghr << 1) | int(actual_taken)) & self.ghr_mask
        if history.was_indirect and actual_taken:
            entries = self.cache[history.set_index]
            for entry in entries:
                if entry.tag == history.tag:
                    entry.target = int(actual_target)
                    return
            victim = entries[self.random.next() % self.config.ways]
            victim.tag = history.tag
            victim.target = int(actual_target)

    def commit(self, thread_id: int) -> None:
        thread = self.threads[thread_id]
        limit = self.config.path_length + self.config.speculative_path_length
        while len(thread.path) >= limit:
            thread.path.pop(0)


@dataclass(frozen=True)
class BranchPrediction:
    seq_num: int
    pc: int
    branch_type: str
    conditional_prediction: bool
    predicted_taken: bool
    actual_taken: bool
    predicted_target: Optional[int]
    actual_target: int
    target_provider: str
    btb_hit: bool
    indirect_lookup: bool
    indirect_hit: bool
    direction_miss: bool
    final_direction_miss: bool
    target_unavailable_miss: bool
    target_miss: bool
    target_side_miss: bool
    full_miss: bool
    ras_target_unknown: bool


class ReplayStats:
    def __init__(self) -> None:
        self.branches = 0
        self.conditional_branches = 0
        self.conditional_direction_misses = 0
        self.final_direction_misses = 0
        self.target_misses = 0
        self.target_unavailable_misses = 0
        self.target_side_misses = 0
        self.full_misses = 0
        self.btb_lookups = 0
        self.btb_hits = 0
        self.indirect_lookups = 0
        self.indirect_hits = 0
        self.ras_target_unknown = 0
        self.mispredict_due_to_btb_miss = 0
        self.providers = {provider.value: 0 for provider in TargetProvider}
        self.by_type: dict[str, dict[str, int]] = {}

    def add(self, prediction: BranchPrediction) -> None:
        self.branches += 1
        self.conditional_branches += int(
            prediction.branch_type in {
                BranchType.DIRECT_COND.value,
                BranchType.INDIRECT_COND.value,
            }
        )
        self.conditional_direction_misses += int(prediction.direction_miss)
        self.final_direction_misses += int(prediction.final_direction_miss)
        self.target_misses += int(prediction.target_miss)
        self.target_unavailable_misses += int(prediction.target_unavailable_miss)
        self.target_side_misses += int(prediction.target_side_miss)
        self.full_misses += int(prediction.full_miss)
        self.btb_lookups += 1
        self.btb_hits += int(prediction.btb_hit)
        self.indirect_lookups += int(prediction.indirect_lookup)
        self.indirect_hits += int(prediction.indirect_hit)
        self.ras_target_unknown += int(prediction.ras_target_unknown)
        self.mispredict_due_to_btb_miss += int(
            prediction.full_miss and prediction.actual_taken and not prediction.btb_hit
        )
        self.providers[prediction.target_provider] += 1
        item = self.by_type.setdefault(
            prediction.branch_type, {"branches": 0, "misses": 0}
        )
        item["branches"] += 1
        item["misses"] += int(prediction.full_miss)

    @staticmethod
    def _rate(count: int, opportunities: int) -> float:
        return float(count) / opportunities if opportunities else float("nan")

    def report(self) -> dict[str, Any]:
        by_type = {
            name: {
                **values,
                "miss_rate": self._rate(values["misses"], values["branches"]),
            }
            for name, values in sorted(self.by_type.items())
        }
        return {
            "branches": self.branches,
            "conditional_branches": self.conditional_branches,
            "conditional_direction_misses": self.conditional_direction_misses,
            "conditional_direction_miss_rate": self._rate(
                self.conditional_direction_misses, self.conditional_branches
            ),
            "final_direction_misses": self.final_direction_misses,
            "target_misses": self.target_misses,
            "target_unavailable_misses": self.target_unavailable_misses,
            "target_side_misses": self.target_side_misses,
            "full_misses": self.full_misses,
            "full_miss_rate": self._rate(self.full_misses, self.branches),
            "btb_lookups": self.btb_lookups,
            "btb_hits": self.btb_hits,
            "btb_hit_rate": self._rate(self.btb_hits, self.btb_lookups),
            "indirect_lookups": self.indirect_lookups,
            "indirect_hits": self.indirect_hits,
            "indirect_hit_rate": self._rate(
                self.indirect_hits, self.indirect_lookups
            ),
            "ras_target_unknown": self.ras_target_unknown,
            "mispredict_due_to_btb_miss": self.mispredict_due_to_btb_miss,
            "target_providers": dict(self.providers),
            "by_type": by_type,
        }


class TournamentBPUReplay:
    """Serial correct-path replay of the complete current Tournament BPU."""

    def __init__(self, config: ReplayConfig) -> None:
        config.validate()
        self.config = config
        self.tournament = TournamentPredictor(config.tournament, config.num_threads)
        self.btb = SimpleBTB(config.btb, config.num_threads)
        self.ras = ReturnAddrStack(config.ras, config.num_threads)
        self.indirect = SimpleIndirectPredictor(config.indirect, config.num_threads)
        self.stats = ReplayStats()
        self.sequence = 0
        self.learned_return_targets: dict[int, int] = {}
        self.functional_histories = [0] * config.num_threads
        self.functional_history_checks = 0
        self.functional_history_mismatches = 0

    def _return_target(self, event: BranchEvent) -> Optional[int]:
        return self.learned_return_targets.get(int(event.pc))

    def _update_btb(self, event: BranchEvent) -> None:
        if not self.config.requires_btb_hit:
            if event.return_:
                return
            if event.indirect:
                return
        self.btb.update(event.thread_id, event.pc, event.actual_target)

    def process(self, event: BranchEvent) -> BranchPrediction:
        if event.thread_id >= self.config.num_threads:
            raise ValueError(
                f"event thread_id={event.thread_id} exceeds configured threads"
            )
        if event.branch_history is not None:
            self.functional_history_checks += 1
            self.functional_history_mismatches += int(
                (int(event.branch_history) & 0xFFFF)
                != self.functional_histories[event.thread_id]
            )
        self.sequence += 1
        seq_num = self.sequence
        branch_type = event.branch_type

        tournament_history: Optional[TournamentHistory]
        if event.conditional:
            cond_prediction, tournament_history = self.tournament.lookup(
                event.thread_id, event.pc
            )
        else:
            cond_prediction, tournament_history = True, None
        predicted_taken = bool(cond_prediction)

        btb_target = self.btb.lookup(event.thread_id, event.pc)
        btb_hit = btb_target is not None
        provider = TargetProvider.NONE
        predicted_target: Optional[int] = None
        if btb_hit and predicted_taken:
            provider = TargetProvider.BTB
            predicted_target = int(btb_target)
        branch_detected = btb_hit or not self.config.requires_btb_hit

        ras_history: Optional[RASHistory] = None
        ras_unknown = False
        if branch_detected and event.call:
            ras_history = self.ras.push(
                event.thread_id,
                RASFrame(
                    call_pc=event.pc,
                    return_target=self._return_target(event),
                ),
            )
        elif branch_detected and event.return_:
            frame, ras_history = self.ras.pop(event.thread_id)
            if frame is not None and frame.return_target is not None:
                provider = TargetProvider.RAS
                predicted_target = frame.return_target
            else:
                ras_unknown = True

        indirect_history: Optional[IndirectHistory] = None
        indirect_lookup = False
        indirect_hit = False
        if predicted_taken and branch_detected and event.indirect and not event.return_:
            indirect_lookup = True
            indirect_target, indirect_history = self.indirect.lookup(
                event.thread_id, event.pc
            )
            if indirect_target is not None:
                indirect_hit = True
                provider = TargetProvider.INDIRECT
                predicted_target = int(indirect_target)

        if provider is TargetProvider.NONE:
            predicted_taken = False
            predicted_target = None

        tournament_history = self.tournament.speculative_update(
            event.thread_id, predicted_taken, tournament_history
        )
        # The actual not-taken successor is a legal functional fact and gives
        # the exact fallthrough.  For a taken event its fallthrough is absent;
        # pc+1 is used only in speculative state that is immediately repaired.
        speculative_target = (
            int(predicted_target)
            if predicted_target is not None
            else (event.next_pc if not event.taken else event.pc + 1)
        )
        indirect_history = self.indirect.speculative_update(
            event.thread_id,
            seq_num,
            predicted_taken,
            speculative_target,
            branch_type,
            indirect_history,
        )

        final_direction_miss = predicted_taken != event.taken
        target_miss = bool(
            predicted_taken
            and event.taken
            and predicted_target != event.actual_target
        )
        full_miss = bool(final_direction_miss or target_miss)
        direction_miss = bool(event.conditional and cond_prediction != event.taken)
        target_unavailable_miss = bool(
            event.taken and cond_prediction and not predicted_taken
        )
        target_side_miss = bool(full_miss and not direction_miss)

        if full_miss:
            self.tournament.repair(
                event.thread_id, event.taken, tournament_history
            )
            self.indirect.repair(
                event.thread_id,
                seq_num,
                event.taken,
                event.actual_target,
                branch_type,
                indirect_history,
            )
            if event.taken and ras_history is None:
                if event.return_:
                    _frame, ras_history = self.ras.pop(event.thread_id)
                elif event.call:
                    ras_history = self.ras.push(
                        event.thread_id,
                        RASFrame(
                            call_pc=event.pc,
                            return_target=self._return_target(event),
                        ),
                    )
            elif not event.taken and ras_history is not None:
                self.ras.squash(event.thread_id, ras_history)
                ras_history = None
            if event.taken and self.config.update_btb_at_squash:
                self._update_btb(event)

        if event.return_ and event.taken and ras_history is not None:
            frame = ras_history.popped_frame
            if frame is not None:
                self.learned_return_targets[frame.call_pc] = event.actual_target
        if event.call and not event.taken:
            self.learned_return_targets[event.pc] = event.next_pc

        self.functional_histories[event.thread_id] = (
            (self.functional_histories[event.thread_id] << 1) | int(event.taken)
        ) & 0xFFFF

        self.tournament.commit(event.taken, tournament_history)
        self.indirect.commit(event.thread_id)
        if event.taken and not self.config.update_btb_at_squash:
            self._update_btb(event)

        prediction = BranchPrediction(
            seq_num=seq_num,
            pc=event.pc,
            branch_type=branch_type.value,
            conditional_prediction=bool(cond_prediction),
            predicted_taken=predicted_taken,
            actual_taken=event.taken,
            predicted_target=predicted_target,
            actual_target=event.actual_target,
            target_provider=provider.value,
            btb_hit=btb_hit,
            indirect_lookup=indirect_lookup,
            indirect_hit=indirect_hit,
            direction_miss=direction_miss,
            final_direction_miss=final_direction_miss,
            target_unavailable_miss=target_unavailable_miss,
            target_miss=target_miss,
            target_side_miss=target_side_miss,
            full_miss=full_miss,
            ras_target_unknown=ras_unknown,
        )
        self.stats.add(prediction)
        return prediction

    def run(self, events: Iterable[BranchEvent]) -> dict[str, Any]:
        for event in events:
            self.process(event)
        return self.report()

    def report(self) -> dict[str, Any]:
        return {
            "name": "standalone_tournament_full_bpu_replay",
            "config_hash": self.config.stable_hash(),
            "config": self.config.to_dict(),
            **self.stats.report(),
            "oracle_labels_consumed_as_input": False,
            "functional_history_checks": self.functional_history_checks,
            "functional_history_mismatches": self.functional_history_mismatches,
            "functional_inputs": [
                "pc",
                "branch type flags",
                "taken",
                "branch_target",
                "branch_next_pc",
                "thread_id",
            ],
            "approximation": {
                "execution_order": "retired_correct_path_serial_resolution",
                "wrong_path_pollution": "not_observable_ignored",
                "overlap_timing": "not_observable_serialized",
                "ras": "causal_call_pc_to_return_target_learning",
                "taken_call_fallthrough": "unknown_until_observed_causally",
                "indirect_random": (
                    "glibc_rand_stream_seed_"
                    f"{self.config.indirect.replacement_seed}"
                ),
            },
        }


def prediction_to_dict(prediction: BranchPrediction) -> dict[str, Any]:
    return asdict(prediction)
