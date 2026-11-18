"""Freeze a separate FastSim tree and remove duplicate audits only in opt-in mode."""

import argparse
import hashlib
import json
import pathlib
import shutil

ROOT = pathlib.Path(__file__).resolve().parents[2]
HERE = pathlib.Path(__file__).resolve().parent
p = argparse.ArgumentParser(description=__doc__)
p.add_argument("--output", type=pathlib.Path, required=True)
a = p.parse_args()
a.output.mkdir(parents=True, exist_ok=False)
for name in ["src", "include", "tools", "configs"]:
    shutil.copytree(
        ROOT / name, a.output / name, ignore=shutil.ignore_patterns("__pycache__")
    )
shutil.copy2(ROOT / "CMakeLists.txt", a.output / "CMakeLists.txt")
shutil.copy2(HERE / "tracebench_trust.hpp", a.output / "include/tracebench_trust.hpp")
shared = pathlib.Path("gem5-spr/src/mem/spr_retired_load_miss.hh")
(a.output / shared).parent.mkdir(parents=True)
shutil.copy2(ROOT / shared, a.output / shared)
changes = []


def edit(rel, old, new, count=1):
    path = a.output / rel
    text = path.read_text()
    if text.count(old) != count:
        raise ValueError(
            f"{rel}: expected {count} patch anchors for {old[:65]!r}, found {text.count(old)}"
        )
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    path.write_text(text.replace(old, new))
    changes.append(
        dict(
            file=rel,
            before_sha256=before,
            after_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        )
    )


files = [
    "src/trace.cpp",
    "src/retired_frontend_facts.cpp",
    "src/fst_dynamic_translations.cpp",
    "src/fst_static_uop_reader.cpp",
    "src/fst_store_facts_io.cpp",
]
for name in files:
    path = a.output / name
    path.write_text('#include "tracebench_trust.hpp"\n' + path.read_text())

gate = "tracebench::validation_enabled()"
edit(
    "src/retired_frontend_facts.cpp",
    '    Reader r(path+".ffe.gz"); header(r,core);',
    "    if (" + gate + ') {\n    Reader r(path+".ffe.gz"); header(r,core);',
)
edit(
    "src/retired_frontend_facts.cpp",
    "    impl_->reader=std::make_unique<Reader>",
    "    }\n    impl_->reader=std::make_unique<Reader>",
)
edit(
    "src/fst_dynamic_translations.cpp",
    "  std::ifstream hot(path, std::ios::binary);",
    "  if (" + gate + ") {\n  std::ifstream hot(path, std::ios::binary);",
)
edit(
    "src/fst_dynamic_translations.cpp",
    "  cursor_ = 0;\n  states_.clear();",
    "  }\n  cursor_ = 0;\n  states_.clear();",
)
edit(
    "src/fst_dynamic_translations.cpp",
    "b.hash != hash(&record, sizeof(record), 2166136261u, false)",
    "(" + gate + " && b.hash != hash(&record, sizeof(record), 2166136261u, false))",
)
edit(
    "src/trace.cpp",
    "                                    std::uint64_t ordinal) {",
    "                                    std::uint64_t ordinal) {\n    if (!"
    + gate
    + ") return;",
)
edit(
    "src/trace.cpp",
    "if (memory_request_roles_complete_ || functional_retirement_complete_)",
    "if ("
    + gate
    + " && (memory_request_roles_complete_ || functional_retirement_complete_))",
)
for expected in ["expected_retirement_hash_", "expected_memory_request_hash_"]:
    old = "memory_request_stream_hash_!=" + expected
    count = (a.output / "src/trace.cpp").read_text().count(old)
    if count:
        edit("src/trace.cpp", old, "(" + gate + " && " + old + ")", count)
    old = "memory_request_stream_hash_ != " + expected
    count = (a.output / "src/trace.cpp").read_text().count(old)
    if count:
        edit("src/trace.cpp", old, "(" + gate + " && " + old + ")", count)
for old in [
    "row.hot_record_hash!=fst::dependency_record_hash(&record)",
    "row.hot_record_hash != fst::dependency_record_hash(&record)",
    "next_dependency_row_.hot_record_hash != fst::dependency_record_hash(&record)",
]:
    edit("src/trace.cpp", old, "(" + gate + " && " + old + ")")

edit(
    "src/fst_static_uop_reader.cpp",
    "    validate_static_uop(row,operands);",
    "    if (!" + gate + ") return;\n    validate_static_uop(row,operands);",
)
edit(
    "src/fst_static_uop_reader.cpp",
    "row.hot_record_hash!=static_uop_record_hash(record)",
    "(" + gate + " && row.hot_record_hash!=static_uop_record_hash(record))",
)
edit(
    "src/fst_static_uop_reader.cpp",
    "    hash_=static_uop_stream_hash(hash_,record);++observed_;",
    "    if ("
    + gate
    + ") hash_=static_uop_stream_hash(hash_,record);\n    ++observed_;",
)
edit(
    "src/fst_static_uop_reader.cpp",
    "footer.stream_hash!=hash_",
    "(" + gate + " && footer.stream_hash!=hash_)",
)
edit(
    "src/fst_store_facts_io.cpp",
    "void validate_store_facts(const StoreRequestFacts& f,std::uint64_t address,std::uint32_t size) {",
    "void validate_store_facts(const StoreRequestFacts& f,std::uint64_t address,std::uint32_t size) {\n    if (!"
    + gate
    + ") return;",
)
edit(
    "src/fst_store_facts_io.cpp",
    "void validate_store_record_kind(const StoreRequestFacts& f,const void* record) {",
    "void validate_store_record_kind(const StoreRequestFacts& f,const void* record) {\n    if (!"
    + gate
    + ") return;",
)
edit(
    "src/fst_store_facts_io.cpp",
    "next_->hot_record_hash!=dependency_record_hash(record)",
    "(" + gate + " && next_->hot_record_hash!=dependency_record_hash(record))",
)
edit(
    "src/fst_store_facts_io.cpp",
    "    hash_=memory_request_stream_hash(hash_,record);++observed_;",
    "    if ("
    + gate
    + ") hash_=memory_request_stream_hash(hash_,record);\n    ++observed_;",
)
edit(
    "src/fst_store_facts_io.cpp",
    "hash_!=header_.stream_hash",
    "(" + gate + " && hash_!=header_.stream_hash)",
)
edit(
    "src/main.cpp",
    "    const auto stats=simulator.run();",
    '    const auto stats=simulator.run();\n    std::cerr << "[TRACEBENCH-END] host_monotonic_ns=" << std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::now().time_since_epoch()).count() << "\\n";',
)

receipt = dict(
    patches=changes,
    files={
        str(f.relative_to(a.output)): hashlib.sha256(f.read_bytes()).hexdigest()
        for name in ["src", "include"]
        for f in (a.output / name).rglob("*")
        if f.is_file()
    },
)
receipt["additional_compiled_inputs"] = {
    str(rel): hashlib.sha256((a.output / rel).read_bytes()).hexdigest()
    for rel in [shared, pathlib.Path("CMakeLists.txt")]
}
(a.output / "benchmark-patch-receipt.json").write_text(json.dumps(receipt, indent=2))
print(a.output)
