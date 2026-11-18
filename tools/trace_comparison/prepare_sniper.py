"""Prepare an isolated Sniper tree with FST boundaries; reference is untouched."""

import argparse
import hashlib
import json
import pathlib
import shutil

p = argparse.ArgumentParser()
p.add_argument("--source", type=pathlib.Path, required=True)
p.add_argument("--output", type=pathlib.Path, required=True)
a = p.parse_args()
if a.output.exists():
    raise SystemExit("output exists; preserve it and choose another path")
shutil.copytree(
    a.source,
    a.output,
    symlinks=True,
    ignore=shutil.ignore_patterns("build", ".git", "__pycache__"),
)
include = a.output / "fst-adapter-include"
include.mkdir()
shutil.copy2(pathlib.Path(__file__).with_name("fst_v7.hpp"), include / "fst_v7.hpp")
shutil.copy2(
    pathlib.Path(__file__).with_name("tracebench_trust.hpp"),
    include / "tracebench_trust.hpp",
)
changes = []


def edit(rel, old, new):
    f = a.output / rel
    s = f.read_text()
    n = s.count(old)
    if n != 1:
        raise RuntimeError(f"{rel}: expected one patch anchor, got {n}")
    before = hashlib.sha256(f.read_bytes()).hexdigest()
    f.write_text(s.replace(old, new))
    changes.append(
        {
            "file": rel,
            "before_sha256": before,
            "after_sha256": hashlib.sha256(f.read_bytes()).hexdigest(),
        }
    )


base = "common/performance_model/performance_models/"
edit(
    base + "micro_op/micro_op.h",
    "   int fst_op_class;",
    "   bool fst_counts_macro;           // fragment executes but does not retire a macro\n   int fst_op_class;",
)
edit(
    base + "micro_op/micro_op.cc",
    "   this->fst_op_class = FST_OP_UNSET;",
    "   this->fst_counts_macro = true;\n   this->fst_op_class = FST_OP_UNSET;",
)
edit(
    base + "rob_performance_model/rob_timer.cc",
    "      if (entry->uop->isLast())\n         instructionsExecuted++;",
    "      if (entry->uop->isLast() && entry->uop->getMicroOp()->fst_counts_macro)\n         instructionsExecuted++;",
)
edit(
    "common/trace_frontend/trace_thread.h",
    "void handleFstMacro(const std::vector<fstv7::FstRecord> &recs, PerformanceModel *prfmdl);",
    "void handleFstMacro(const std::vector<fstv7::FstRecord> &recs, PerformanceModel *prfmdl, bool completes_macro = true);",
)
edit(
    "common/trace_frontend/trace_thread.cc",
    "                                 PerformanceModel *prfmdl)\n{",
    "                                 PerformanceModel *prfmdl, bool completes_macro)\n{",
)
edit(
    "common/trace_frontend/trace_thread.cc",
    "      uop->fst_op_class = r.canonical_op_class();",
    "      uop->fst_counts_macro = completes_macro;\n      uop->fst_op_class = r.canonical_op_class();",
)
edit(
    "common/trace_frontend/trace_thread.cc",
    "         handleFstMacro(m_fst_macro, tail_core->getPerformanceModel());\n         tail_core->countInstructions(m_fst_macro.back().pc, 1);\n         ++m_instructions_retired;",
    "         handleFstMacro(m_fst_macro, tail_core->getPerformanceModel(), false);",
)
edit(
    "common/trace_frontend/trace_thread.cc",
    '   m_where.store("exiting-fst", std::memory_order_relaxed);',
    '   printf("[FST-ENDPOINT:%u] complete_macros=%lu model_elapsed_fs=%lu stop=%d\\n",\n          m_thread->getId(), (unsigned long)take_macros,\n          (unsigned long)time_end.getFS(), m_stop ? 1 : 0);\n   m_where.store("exiting-fst", std::memory_order_relaxed);',
)
edit(
    "common/trace_frontend/trace_thread.cc",
    "#include <vector>",
    "#include <vector>\n#include <mutex>\n#include <condition_variable>",
)
edit(
    "common/trace_frontend/trace_thread.cc",
    "void TraceThread::m_run_func_fst()",
    "static void waitForFstWarmup(unsigned participants)\n{\n   static std::mutex mutex; static std::condition_variable cv;\n   static unsigned arrived = 0;\n   std::unique_lock<std::mutex> lock(mutex);\n   if (++arrived == participants) cv.notify_all();\n   else cv.wait(lock, [&]{ return arrived == participants; });\n}\n\nvoid TraceThread::m_run_func_fst()",
)
edit(
    "common/trace_frontend/trace_thread.cc",
    "   uint64_t take_macros = 0;",
    '   uint64_t take_macros = 0;\n   auto begin_measurement = [&] {\n      waitForFstWarmup(Sim()->getCfg()->getInt("traceinput/num_apps"));\n      printf("[FST-BEGIN:%u] model_elapsed_fs=%lu\\n", m_thread->getId(),\n             (unsigned long)prfmdl->getElapsedTime().getFS());\n   };\n   if (m_fst_warmup_records == 0) begin_measurement();',
)
edit(
    "common/trace_frontend/trace_thread.cc",
    "      if (m_stop) break;\n   }\n\n   // Flush a trailing fragment",
    "      if (ordinal == m_fst_warmup_records) begin_measurement();\n      if (m_stop) break;\n   }\n\n   // Flush a trailing fragment",
)
edit(
    base + "micro_op/micro_op.h",
    "   uint32_t fst_producer_dists[4];",
    "   std::vector<uint32_t> fst_extra_dependencies;\n   uint32_t fst_producer_dists[4];",
)
edit(
    "common/trace_frontend/trace_thread.h",
    "      std::vector<fstv7::FstRecord> m_fst_macro;",
    "      std::vector<std::vector<uint32_t>> m_fst_extra_dependencies;\n      std::vector<fstv7::FstRecord> m_fst_macro;",
)
edit(
    "common/trace_frontend/trace_thread.cc",
    "      uop->fst_counts_macro = completes_macro;",
    "      uop->fst_extra_dependencies = m_fst_extra_dependencies.at(i);\n      uop->fst_counts_macro = completes_macro;",
)
edit(
    "common/trace_frontend/trace_thread.cc",
    "         m_fst_macro.push_back(rec);",
    "         m_fst_extra_dependencies.push_back(m_fst_reader.current_dependency_extensions());\n         m_fst_macro.push_back(rec);",
)
# Both complete-macro and trailing-fragment release paths clear their metadata.
f = a.output / "common/trace_frontend/trace_thread.cc"
text = f.read_text()
text = text.replace(
    "m_fst_macro.clear();", "m_fst_macro.clear(); m_fst_extra_dependencies.clear();"
)
f.write_text(text)
edit(
    base + "rob_performance_model/rob_timer.cc",
    "         const uint64_t my_seqnr = entry->uop->getSequenceNumber();",
    "         const uint64_t my_seqnr = entry->uop->getSequenceNumber();\n         for (uint32_t dist : entry->uop->getMicroOp()->fst_extra_dependencies)\n         {\n            if (dist && dist <= my_seqnr && my_seqnr-dist >= lowestValidSequenceNumber)\n               entry->uop->addDependency(my_seqnr-dist);\n         }",
)
edit(
    "common/trace_frontend/trace_thread.cc",
    "core->readInstructionMemory(rec.pc, rec.size ? rec.size : 1);",
    "core->readInstructionMemory(m_fst_reader.modeled_code_address(rec.pc), m_fst_reader.instruction_size(rec.pc));",
)
edit(
    "common/trace_frontend/trace_thread.cc",
    "   ins->setSize(head.size ? head.size : 1);",
    "   ins->setSize(m_fst_reader.instruction_size(head.pc));",
)
edit(
    "common/trace_frontend/trace_thread.cc",
    "prfmdl->createDynamicInstruction(ins, head.pc);",
    "prfmdl->createDynamicInstruction(ins, m_fst_reader.modeled_code_address(head.pc));",
)
edit(
    "common/trace_frontend/trace_thread.cc",
    '   m_where.store("exiting-fst", std::memory_order_relaxed);',
    '   printf("[FST-CODE-COVERAGE:%u] unknown_records=%lu\\n", m_thread->getId(), (unsigned long)m_fst_reader.unknown_code_records());\n   m_where.store("exiting-fst", std::memory_order_relaxed);',
)
edit(
    base + "micro_op/micro_op.h",
    "#define MAXIMUM_NUMBER_OF_DEPENDENCIES MAXIMUM_NUMBER_OF_SOURCE_REGISTERS",
    "#define MAXIMUM_NUMBER_OF_DEPENDENCIES 64",
)
edit(
    base + "micro_op/dynamic_micro_op.cc",
    "      assert(this->dependenciesLength < MAXIMUM_NUMBER_OF_DEPENDENCIES);",
    '      if (this->dependenciesLength >= MAXIMUM_NUMBER_OF_DEPENDENCIES) LOG_PRINT_ERROR("dependency capacity exceeded");',
)
edit(
    base + "rob_performance_model/rob_timer.cc",
    "uint64_t deps_to_remove[8], num_dtr = 0;",
    "uint64_t deps_to_remove[MAXIMUM_NUMBER_OF_DEPENDENCIES], num_dtr = 0;",
)
edit(
    "common/performance_model/dynamic_instruction.cc",
    '#include "dynamic_instruction.h"',
    '#include "dynamic_instruction.h"\n#include "fst_instruction.h"',
)
edit(
    "common/performance_model/dynamic_instruction.cc",
    "core->accessBranchPredictor(eip, branch_info.taken",
    "core->accessBranchPredictor(dynamic_cast<FstInstruction*>(instruction) ? instruction->getAddress() : eip, branch_info.taken",
)
edit(
    "common/trace_frontend/trace_thread.cc",
    "      uop->fst_counts_macro = completes_macro;",
    "      uop->setSerializing(fstv7::has_flag(r.flags, fstv7::kSerialize));\n      uop->fst_counts_macro = completes_macro;",
)
from sniper_review_checks import apply

changes.extend(apply(a.output))
edit(
    base + "micro_op/dynamic_micro_op.cc",
    "         dependencies[dependenciesLength] = this->sequenceNumber - this->microOpTypeOffset - this->intraInstructionDependencies;",
    '         if (dependenciesLength >= MAXIMUM_NUMBER_OF_DEPENDENCIES) LOG_PRINT_ERROR("dependency capacity exceeded during removal");\n         dependencies[dependenciesLength] = this->sequenceNumber - this->microOpTypeOffset - this->intraInstructionDependencies;',
)
edit(
    "common/trace_frontend/trace_thread.cc",
    "#include <condition_variable>",
    "#include <condition_variable>\n#include <chrono>",
)
edit(
    "common/trace_frontend/trace_thread.cc",
    'printf("[FST-BEGIN:%u] model_elapsed_fs=%lu\\n", m_thread->getId(),\n             (unsigned long)prfmdl->getElapsedTime().getFS());',
    'printf("[FST-BEGIN:%u] model_elapsed_fs=%lu host_monotonic_ns=%lu\\n", m_thread->getId(),\n             (unsigned long)prfmdl->getElapsedTime().getFS(), (unsigned long)std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::now().time_since_epoch()).count());',
)
edit(
    "common/trace_frontend/trace_thread.cc",
    'model_elapsed_fs=%lu stop=%d\\n",',
    'model_elapsed_fs=%lu stop=%d host_monotonic_ns=%lu\\n",',
)
edit(
    "common/trace_frontend/trace_thread.cc",
    "(unsigned long)time_end.getFS(), m_stop ? 1 : 0);",
    "(unsigned long)time_end.getFS(), m_stop ? 1 : 0, (unsigned long)std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::now().time_since_epoch()).count());",
)
edit(
    "common/trace_frontend/trace_thread.cc",
    '   m_where.store("exiting-fst", std::memory_order_relaxed);',
    '   printf("[FST-SEMANTICS:%u] hash=%lu\\n", m_thread->getId(), (unsigned long)m_fst_reader.semantic_hash());\n   m_where.store("exiting-fst", std::memory_order_relaxed);',
)
(a.output / "fst-adapter-patch-receipt.json").write_text(
    json.dumps(
        {
            "patches": changes,
            "final_files": {
                rel: hashlib.sha256((a.output / rel).read_bytes()).hexdigest()
                for rel in {c["file"] for c in changes}
            },
            "fst_header_sha256": hashlib.sha256(
                (include / "fst_v7.hpp").read_bytes()
            ).hexdigest(),
        },
        indent=2,
    )
)
print(a.output)
