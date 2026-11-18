"""Native ROB observation counters used by adapter regression tests."""

from pathlib import Path
import hashlib


def apply(root):
    base = "common/performance_model/performance_models/rob_performance_model/"
    patches = [
        (
            base + "rob_timer.h",
            "   uint64_t m_totalConsumers;",
            "   uint64_t m_fstRetiredUops = 0;\n   uint64_t m_fstRetiredMacros = 0;\n   uint64_t m_fstExtraDeclared = 0;\n   uint64_t m_fstSerialDeclared = 0;\n   uint64_t m_totalConsumers;",
        ),
        (
            base + "rob_timer.cc",
            '   registerStatsMetric("rob_timer", core->getId(), "uops_total", &m_uops_total);',
            '   registerStatsMetric("rob_timer", core->getId(), "fst_retired_uops", &m_fstRetiredUops);\n   registerStatsMetric("rob_timer", core->getId(), "fst_retired_macros", &m_fstRetiredMacros);\n   registerStatsMetric("rob_timer", core->getId(), "fst_extra_declared", &m_fstExtraDeclared);\n   registerStatsMetric("rob_timer", core->getId(), "fst_serial_declared", &m_fstSerialDeclared);\n   registerStatsMetric("rob_timer", core->getId(), "uops_total", &m_uops_total);',
        ),
        (
            base + "rob_timer.cc",
            "         const uint64_t my_seqnr = entry->uop->getSequenceNumber();",
            "         m_fstExtraDeclared += entry->uop->getMicroOp()->fst_extra_dependencies.size();\n         m_fstSerialDeclared += entry->uop->getMicroOp()->isSerializing();\n         const uint64_t my_seqnr = entry->uop->getSequenceNumber();",
        ),
    ]
    patches.append(
        (
            base + "rob_timer.cc",
            "      entry->free();",
            "      if (entry->uop->getMicroOp()->fst_op_class != MicroOp::FST_OP_UNSET) {\n         ++m_fstRetiredUops;\n         m_fstRetiredMacros += entry->uop->isLast() && entry->uop->getMicroOp()->fst_counts_macro;\n      }\n      entry->free();",
        )
    )
    receipt = []
    for rel, old, new in patches:
        p = Path(root) / rel
        text = p.read_text()
        if text.count(old) != 1:
            raise RuntimeError("review patch anchor: " + rel)
        before = hashlib.sha256(p.read_bytes()).hexdigest()
        p.write_text(text.replace(old, new))
        receipt.append(
            dict(
                file=rel,
                before_sha256=before,
                after_sha256=hashlib.sha256(p.read_bytes()).hexdigest(),
            )
        )
    return receipt
