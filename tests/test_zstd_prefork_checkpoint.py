#!/usr/bin/env python3
"""Source invariants for the Zstd pre-fork ROI checkpoint."""

from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PATCH = ROOT / "patches" / "tcsim-zstd-prefork-checkpoint.patch"


def patch_section(patch: str, path: str) -> str:
    marker = f"diff --git a/{path} b/{path}\n"
    start = patch.index(marker) + len(marker)
    next_section = patch.find("\ndiff --git ", start)
    return patch[start:] if next_section == -1 else patch[start:next_section]


def resulting_hunks(section: str) -> str:
    lines = []
    for line in section.splitlines():
        if line.startswith(("---", "+++", "@@")) or line.startswith("-"):
            continue
        if line.startswith(("+", " ")):
            lines.append(line[1:])
    return "\n".join(lines)


class ZstdPreforkCheckpointTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        patch = PATCH.read_text(encoding="utf-8")
        cls.benchfn_section = patch_section(
            patch,
            "TCSim/workloads/spec2026/benchspec/CPU/777.zstd_r/"
            "src/zstd-1.5.6/programs/benchfn.c",
        )
        cls.zstdcli = resulting_hunks(
            patch_section(
                patch,
                "TCSim/workloads/spec2026/benchspec/CPU/777.zstd_r/"
                "src/zstd-1.5.6/programs/zstdcli.c",
            )
        )

    def test_checkpoint_is_after_control_creation_and_before_fork(self) -> None:
        control = self.zstdcli.index("gem5_roi_create_wave_control(workers)")
        children = self.zstdcli.index(
            "pid_t* children = (pid_t*)calloc((size_t)workers, sizeof(pid_t))"
        )
        allocation_check = self.zstdcli.index("if (!children)")
        checkpoint = self.zstdcli.index(
            'gem5_roi_checkpoint_once("zstd-before-worker-fork", 777)'
        )
        fork = self.zstdcli.index("pid_t child = fork()")
        self.assertLess(control, children)
        self.assertLess(children, allocation_check)
        self.assertLess(allocation_check, checkpoint)
        self.assertLess(checkpoint, fork)

    def test_worker_checkpoint_barrier_is_removed(self) -> None:
        self.assertIn(
            '-                    gem5_roi_wave_checkpoint('
            '"zstd-first-block", 777);',
            self.benchfn_section,
        )
        self.assertNotIn(
            "gem5_roi_wave_checkpoint(",
            resulting_hunks(self.benchfn_section),
        )

    def test_per_worker_warmup_and_workbegin_barrier_remain(self) -> None:
        body = resulting_hunks(self.benchfn_section)
        warmup = body.index(
            "p.srcBuffers[blockNb], warmupSize,"
        )
        workbegin = body.index(
            'gem5_roi_wave_begin("zstd-64k-warmup-complete", 777);'
        )
        self.assertLess(warmup, workbegin)


if __name__ == "__main__":
    unittest.main()
