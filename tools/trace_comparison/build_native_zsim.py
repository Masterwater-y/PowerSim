"""Build native zsim components in an isolated directory; never modify reference."""

import pathlib, subprocess, shutil, os, argparse, hashlib, json, fcntl

R = pathlib.Path(__file__).resolve().parents[2]
A = R / "tools/trace_comparison"
parser = argparse.ArgumentParser()
parser.add_argument(
    "--source", type=pathlib.Path, default=R / "tmp/sniper-zsim-spr-20261112/zsim"
)
parser.add_argument(
    "--output", type=pathlib.Path, default=R / "tmp/native-trace-adapters-20261116"
)
parser.add_argument("--functional-root", type=pathlib.Path, default=R)
args = parser.parse_args()
W = args.output.resolve()
S = W / "zsim-src"
L = A
W.mkdir(parents=True, exist_ok=True)
build_lock = (W / ".build.lock").open("w")
fcntl.flock(build_lock, fcntl.LOCK_EX)
archive_source = args.functional_root / "build/libfastsim_lib.a"
archive_hash = hashlib.sha256(archive_source.read_bytes()).hexdigest()
archive = W / ("functional-reader-" + archive_hash[:16] + ".a")
if not archive.exists():
    shutil.copy2(archive_source, archive)
if hashlib.sha256(archive.read_bytes()).hexdigest() != archive_hash:
    raise SystemExit("functional reader archive changed while freezing")
headers = W / "functional-include"
if headers.exists():
    shutil.rmtree(headers)
shutil.copytree(args.functional_root / "include", headers)

if not S.exists():
    shutil.copytree(args.source / "src", S)
ref = args.source / "src"
patch_names = {
    "decoder.h",
    "ooo_core.cpp",
    "ooo_core.h",
    "ooo_core_recorder.h",
    "timing_cache.cpp",
    "filter_cache.h",
}
source_hashes = {
    str(f.relative_to(ref)): hashlib.sha256(f.read_bytes()).hexdigest()
    for f in ref.rglob("*")
    if f.is_file()
}
lock = W / "upstream-source-lock.json"
if lock.exists() and json.loads(lock.read_text()) != source_hashes:
    raise SystemExit("upstream source changed; select a fresh output directory")
for rel, digest in source_hashes.items():
    if (
        rel not in patch_names
        and hashlib.sha256((S / rel).read_bytes()).hexdigest() != digest
    ):
        raise SystemExit("isolated upstream file changed: " + rel)
lock.write_text(json.dumps(source_hashes, indent=2))
# Apply only frontend operand width and warmup support to an isolated native source.
ref = args.source / "src"
for name in [
    "decoder.h",
    "ooo_core.cpp",
    "ooo_core.h",
    "ooo_core_recorder.h",
    "timing_cache.cpp",
    "filter_cache.h",
]:
    text = (ref / name).read_text()
    if name == "filter_cache.h":
        text = text.replace(
            "        void setSourceId(uint32_t id) {",
            "        void traceFinishWarmup() { for(uint32_t i=0;i<numSets;i++)filterArray[i].availCycle=0; }\n        void setSourceId(uint32_t id) {",
        )
    if name == "decoder.h":
        text = (
            text.replace("#define MAX_UOP_SRC_REGS 2", "#define MAX_UOP_SRC_REGS 16")
            .replace("uint16_t rs[", "uint32_t rs[")
            .replace("uint16_t rd[", "uint32_t rd[")
            .replace("    uint8_t pad;", "    bool traceSerialize;\n    uint8_t pad;")
            .replace(
                "#define MAX_REGISTERS (REG_EXEC_TEMP + 64)",
                "extern uint32_t fstRegisterCapacity;\n#define MAX_REGISTERS fstRegisterCapacity",
            )
        )
    if name == "ooo_core_recorder.h":
        text = text.replace(
            "EventRecorder* getEventRecorder()",
            "bool traceDrained() const { return state == HALTED; }\n        EventRecorder* getEventRecorder()",
        )
    if name == "ooo_core.cpp":
        text = text.replace(
            "        // Mark retire at ROB",
            "        if(uop->traceSerialize) {\n            commitCycle = MAX(commitCycle, MAX(lastStoreAddrCommitCycle, lastStoreCommitCycle + uop->lat));\n            lastStoreAddrCommitCycle = commitCycle;\n        }\n        // Mark retire at ROB",
        )
        text = text.replace(
            "        rob.markRetire(commitCycle);",
            "        rob.markRetire(commitCycle);\n        traceLastCommit = rob.traceLastRetire();",
        )
        text += "\nvoid OOOCore::traceDrain(uint32_t tid) {\n if(traceLastCommit>curCycle)advance(traceLastCommit);\n while(curCycle>phaseEndCycle){phaseEndCycle+=zinfo->phaseLength;TakeBarrier(tid,tid); }\n}\n"
        text = text.replace(
            "    for (uint32_t i = 0; i < MAX_REGISTERS; i++) {",
            "    regScoreboard.resize(fstRegisterCapacity);\n    for (uint32_t i = 0; i < MAX_REGISTERS; i++) {",
        )
        text = text.replace(
            "uint64_t cOps = MAX(c0, c1);",
            "uint64_t cOps = MAX(c0, c1);\n        for (unsigned src = 2; src < MAX_UOP_SRC_REGS; ++src) {\n            cOps = MAX(cOps, regScoreboard[uop->rs[src]]);\n        }",
        )
    if name == "ooo_core.h":
        text = text.replace(
            "        inline void markRetire(uint64_t minRetireCycle) {",
            "        uint64_t traceLastRetire() const { return curRetireCycle; }\n        inline void markRetire(uint64_t minRetireCycle) {",
        )
        text = text.replace(
            "uint64_t regScoreboard[MAX_REGISTERS];",
            "std::vector<uint64_t> regScoreboard;\n        uint64_t traceLastCommit = 0;",
        )
        text = text.replace(
            "void initStats(AggregateStat* parentStat);",
            "void initStats(AggregateStat* parentStat);\n        void traceDrain(uint32_t tid);\n        bool traceDrained() const { return cRec.traceDrained(); }\n        uint64_t traceIssueCycle() const { return curCycle; }\n        uint64_t traceRobTail() const { return rob.traceLastRetire(); }\n        uint64_t traceUops() const { return uops; }\n        void traceWarmupBranch(Address pc, bool taken) { branchPred.predict(pc, taken); }",
        )
    if name == "timing_cache.cpp":
        text = text.replace(
            "uint64_t TimingCache::access(MemReq& req) {",
            "extern bool fstFunctionalWarmup;\nuint64_t TimingCache::access(MemReq& req) {\n    if (fstFunctionalWarmup) return Cache::access(req);",
        )
    dest = S / name
    if not dest.exists() or dest.read_text() != text:
        dest.write_text(text)
mem = (L / "fst_memory.cpp").read_text()
if not (W / "fst_memory.cpp").exists() or (W / "fst_memory.cpp").read_text() != mem:
    (W / "fst_memory.cpp").write_text(mem)
sources = [
    "galloc",
    "log",
    "hash",
    "cache_arrays",
    "coherence_ctrls",
    "cache",
    "memory_hierarchy",
    "mem_ctrls",
    "network",
    "timing_event",
    "text_stats",
    "ooo_core",
    "ooo_core_recorder",
    "core_recorder",
    "timing_core",
    "timing_cache",
    "contention_sim",
]

flags = [
    "g++",
    "-O2",
    "-g",
    "-std=c++20",
    "-pthread",
    "-DMT_SAFE_LOG",
    "-Wno-deprecated-declarations",
    "-I" + str(A),
    "-I" + str(S),
    "-I" + str(L),
    "-I" + str(headers),
]
objs = []
for src in [A / "native_zsim.cpp", W / "fst_memory.cpp"] + [
    S / (s + ".cpp") for s in sources
]:
    obj = W / (src.stem + ".o")
    objs.append(str(obj))
    if not obj.exists() or obj.stat().st_mtime < max(
        src.stat().st_mtime,
        (S / "decoder.h").stat().st_mtime,
        max(p.stat().st_mtime for p in S.rglob("*.h")),
        max(p.stat().st_mtime for p in A.iterdir() if p.suffix in [".hpp", ".h", ".H"]),
    ):
        p = subprocess.run(
            flags + ["-c", str(src), "-o", str(obj)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        if p.returncode:
            print(p.stdout)
            raise SystemExit(p.returncode)
p = subprocess.run(
    ["g++", "-pthread"]
    + objs
    + [
        str(archive),
        "-lz",
        "-flto=16",
        "-o",
        str(W / "native-zsim"),
    ],
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    text=True,
)
print(p.stdout)
if not p.returncode:
    receipt = {
        "source": str(args.source.resolve()),
        "functional_reader_archive_sha256": archive_hash,
        "functional_header_hashes": {
            str(f.relative_to(headers)): hashlib.sha256(f.read_bytes()).hexdigest()
            for f in headers.rglob("*")
            if f.is_file()
        },
        "binary_sha256": hashlib.sha256((W / "native-zsim").read_bytes()).hexdigest(),
        "upstream_sources": {},
        "compiled_sources": {},
        "adapter_sources": {},
    }
    for f in ref.glob("*"):
        if f.is_file():
            receipt["upstream_sources"][f.name] = hashlib.sha256(
                f.read_bytes()
            ).hexdigest()
    for f in S.glob("*"):
        if f.is_file():
            receipt["compiled_sources"][f.name] = hashlib.sha256(
                f.read_bytes()
            ).hexdigest()
    for f in A.glob("*"):
        if f.is_file():
            receipt["adapter_sources"][f.name] = hashlib.sha256(
                f.read_bytes()
            ).hexdigest()
    (W / "native-build-receipt.json").write_text(json.dumps(receipt, indent=2))
raise SystemExit(p.returncode)
