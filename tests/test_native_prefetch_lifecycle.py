#!/usr/bin/env python3
"""Source invariants for Ruby software-prefetch lifecycle closure."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PATCH = ROOT / "patches" / "p2-external-native-prefetch-lifecycle.patch"


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


class NativePrefetchLifecycleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        patch = PATCH.read_text(encoding="utf-8")
        cls.protocol = patch_section(
            patch, "src/mem/ruby/protocol/RubySlicc_Exports.sm"
        )
        cls.sequencer = resulting_hunks(
            patch_section(patch, "src/mem/ruby/system/Sequencer.cc")
        )
        cls.ruby_port = resulting_hunks(
            patch_section(patch, "src/mem/ruby/system/RubyPort.cc")
        )

    def test_direct_drop_has_distinct_consumed_status(self) -> None:
        self.assertIn("PrefetchSkipped", self.protocol)
        direct_drop = re.search(
            r"if \(pkt->cmd\.isSWPrefetch\(\) && "
            r"seq_req_list\.size\(\) > 1\) \{(?P<body>.*?)\n    \}",
            self.sequencer,
            re.DOTALL,
        )
        self.assertIsNotNone(direct_drop)
        body = direct_drop.group("body")
        self.assertIn(
            "            pkt->req, "
            "TaoTraceNativeTerminalReason::PrefetchSkipped);",
            body,
        )
        self.assertIn("        return RequestStatus_PrefetchSkipped;", body)
        self.assertNotIn("TaoTraceNativeAccessRegistry::noteAdmission", body)

    def test_skipped_prefetch_bypasses_native_admission(self) -> None:
        skip_return = self.sequencer.index(
            "    if (status == RequestStatus_PrefetchSkipped)"
        )
        admission_comment = self.sequencer.index(
            "// Only requests accepted into the Sequencer request table",
            skip_return,
        )
        self.assertLess(skip_return, admission_comment)
        self.assertIn(
            "        return status;",
            self.sequencer[skip_return:admission_comment],
        )

    def test_ruby_clone_preserves_identity_and_shares_ledger(self) -> None:
        clone_start = self.ruby_port.index(
            "if (pkt->cmd.isSWPrefetch()"
        )
        clone = self.ruby_port[clone_start:]
        self.assertIn(
            "            pkt->req->getExtension<"
            "TaoTraceRubyResponseExtension>();",
            clone,
        )
        self.assertIn(
            "        RequestPtr req = "
            "std::make_shared<Request>(*pkt->req);",
            clone,
        )
        self.assertIn("        req->setExtension(native_response);", clone)
        self.assertNotIn("pkt->req->getPaddr(),", clone)

    def test_skipped_and_rejected_clones_are_released(self) -> None:
        skip = re.search(
            r"if \(requestStatus == RequestStatus_PrefetchSkipped\) "
            r"\{(?P<body>.*?)\n    \}",
            self.ruby_port,
            re.DOTALL,
        )
        self.assertIsNotNone(skip)
        self.assertIn("        delete pkt;", skip.group("body"))
        self.assertIn(
            "        schedTimingResp(pf, curTick());", skip.group("body")
        )

        failure = self.ruby_port[
            self.ruby_port.index(
                "SenderState *ss = safe_cast<SenderState *>"
            ):
            self.ruby_port.rindex("pkt = pf;") + len("pkt = pf;")
        ]
        self.assertIn("if (pf != nullptr)", failure)
        self.assertIn("delete pkt;", failure)
        self.assertIn("pkt = pf;", failure)


if __name__ == "__main__":
    unittest.main()
