// FST entry point for the native zsim OOOCore and contention engine.
#include "contention_sim.h"
#include "filter_cache.h"
#include "fst_memory.h"
#include "fst_native_input.hpp"
#include "ooo_core.h"
#include "parallel_prepare.hpp"
#include "zsim.h"
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstring>
#include <iostream>
#include <mutex>
#include <thread>
#include <vector>
fastsim::SimulatorConfig codeConfig;
uint32_t fstRegisterCapacity = 1024;
bool allowIncompleteCode = false;
bool fstFunctionalWarmup = false;
GlobSimInfo *zinfo = nullptr;
uint32_t lineBits = 6, procIdx = 0;
uint64_t procMask = 0;
Core *cores[MAX_THREADS];
static std::mutex phaseMutex;
static std::condition_variable phaseCv;
static unsigned arrivals = 0, generation = 0;
static std::atomic<unsigned> active(0);
static bool ended = false;
uint32_t getCid(uint32_t tid) { return tid; }
uint32_t TakeBarrier(uint32_t tid, uint32_t cid) {
    std::unique_lock<std::mutex> lock(phaseMutex);
    unsigned gen = generation;
    if (++arrivals == zinfo->numCores) {
        zinfo->contentionSim->simulatePhase(zinfo->globPhaseCycles + zinfo->phaseLength);
        zinfo->globPhaseCycles += zinfo->phaseLength;
        zinfo->numPhases++;
        ended = active.load() == 0;
        if (ended)
            for (unsigned c = 0; c < zinfo->numCores; c++)
                ended = ended && static_cast<OOOCore *>(cores[c])->traceDrained();
        arrivals = 0;
        generation++;
        phaseCv.notify_all();
    } else
        phaseCv.wait(lock, [&] { return gen != generation; });
    return ended ? UINT32_MAX : cid;
}
static BblInfo *block(unsigned instructions, unsigned uops) {
    auto b = static_cast<BblInfo *>(calloc(1, sizeof(BblInfo) + DynBbl::bytes(uops)));
    b->instrs = instructions;
    b->bytes = uops ? 4 : 0;
    auto d = &b->oooBbl[0];
    d->uops = uops;

    return b;
}
int main(int argc, char **argv) try {
    if (argc == 2 && std::string(argv[1]) == "--retirement-selftest") {
        ReorderBuffer<128, 4> rob;
        rob.markRetire(1000);
        for (unsigned i = 0; i < 16; i++)
            rob.markRetire(1);
        std::cout << "{\"max_completion\":1000,\"retirement_tail\":" << rob.traceLastRetire()
                  << "}\n";
        return 0;
    }
    if (argc < 2 || argc > 4)
        return 2;
    if (argc == 4) {
        if (std::string(argv[3]) != "--allow-incomplete-code")
            return 2;
        allowIncompleteCode = true;
    }
    auto preparationStart = std::chrono::steady_clock::now();
    bool selftest = std::string(argv[1]) == "--selftest";
    std::vector<NativeInput> inputs;
    unsigned preparationWorkers = 0;
    std::vector<double> preparationFileSeconds;
    if (!selftest) {
        if (argc < 3)
            throw std::runtime_error("source-bound config required");
        codeConfig = fastsim::load_simulator_config(argv[2]);
        auto entries = fstv7::parse_manifest(argv[1]);
        if (entries.empty()) throw std::runtime_error("empty physical-core manifest");
        inputs.resize(entries.size());
        preparationFileSeconds.resize(entries.size());
        for (unsigned i = 0; i < entries.size(); i++) {
            if (entries[i].core_id != i)
                throw std::runtime_error("dense physical core IDs required");
            inputs[i].entry = entries[i];
        }
        preparationWorkers = tracebench::preparation_workers(inputs.size());
        // Initialize optional process-global zlib tables before separate streams
        // are decoded concurrently (also supports BUILDFIXED/DYNAMIC_CRC_TABLE).
        (void)get_crc_table();
        unsigned char compressed[] = {0x78, 0x9c, 0x03, 0x00, 0x00, 0x00, 0x00, 0x01};
        unsigned char decoded[1];
        z_stream warm{};
        if (inflateInit(&warm) != Z_OK) throw std::runtime_error("zlib preparation init failed");
        warm.next_in = compressed; warm.avail_in = sizeof(compressed);
        warm.next_out = decoded; warm.avail_out = sizeof(decoded);
        int status = inflate(&warm, Z_FINISH);
        inflateEnd(&warm);
        if (status != Z_STREAM_END) throw std::runtime_error("zlib table preparation failed");
        tracebench::parallel_prepare(inputs.size(), preparationWorkers, [&](size_t i) {
            auto begin = std::chrono::steady_clock::now();
            inputs[i].preflight(argv[1]);
            preparationFileSeconds[i] = std::chrono::duration<double>(
                std::chrono::steady_clock::now() - begin).count();
        });
    }
    unsigned n = selftest ? 2 : inputs.size();
    for (const auto &input : inputs)
        fstRegisterCapacity = std::max(fstRegisterCapacity, uint32_t(input.records + 1));
    double preparationSeconds =
        std::chrono::duration<double>(std::chrono::steady_clock::now() - preparationStart).count();
    auto initializationStart = std::chrono::steady_clock::now();
    gm_init(2048ull << 20);
    zinfo = gm_calloc<GlobSimInfo>();
    zinfo->numCores = n;
    zinfo->lineSize = 64;
    zinfo->phaseLength = selftest ? 100 : 10000;
    zinfo->numDomains = 1;
    zinfo->freqMHz = 2600;
    zinfo->eventRecorders = gm_calloc<EventRecorder *>(n);
    zinfo->cores = gm_calloc<Core *>(n);
    zinfo->rootStat = new AggregateStat();
    zinfo->rootStat->init("root", "native zsim");
    FstMemorySystem mem;
    FstMachineGeom g = {n, 64, 32768, 8, 49152, 12, 2097152, 16, 13, 100663296, 12, 19, 100};
    if (!selftest) {
        auto config = fastsim::KeyValueConfig::load(argv[2]);
        g.l1iSize = config.get_u32("cache.l1i.size", g.l1iSize);
        g.l1iWays = config.get_u32("cache.l1i.associativity", g.l1iWays);
        g.l1dSize = config.get_u32("cache.l1d.size", g.l1dSize);
        g.l1dWays = config.get_u32("cache.l1d.associativity", g.l1dWays);
        g.l2Size = config.get_u32("cache.l2.size", g.l2Size);
        g.l2Ways = config.get_u32("cache.l2.associativity", g.l2Ways);
        g.l2Latency = config.get_u32("cache.l2.hit_latency", g.l2Latency);
        g.l3Size = config.get_u32("cache.llc.size", g.l3Size);
        g.l3Ways = config.get_u32("cache.llc.associativity", g.l3Ways);
        g.l3Latency = config.get_u32("cache.llc.hit_latency", g.l3Latency);
        g.l2Mshrs = config.get_u32("cache.l2.mshrs", g.l2Mshrs);
        g.l3Mshrs = config.get_u32("cache.llc.mshrs", g.l3Mshrs);
        if (!g.l2Mshrs || !g.l3Mshrs)
            throw std::runtime_error("native timing caches require positive MSHR capacities");
        zinfo->freqMHz = uint32_t(codeConfig.core_frequency_hz / 1e6);
    }
    mem.build(g);
    for (unsigned c = 0; c < n; c++) {
        g_string name((std::string("core-") + std::to_string(c)).c_str());
        auto core = new OOOCore(mem.l1i[c], mem.l1d[c], name);
        cores[c] = core;
        zinfo->cores[c] = core;
        zinfo->eventRecorders[c] = core->getEventRecorder();
        core->initStats(zinfo->rootStat);
    }
    mem.initStats(zinfo->rootStat);
    zinfo->contentionSim = new ContentionSim(1, 1);
    zinfo->contentionSim->postInit();
    zinfo->contentionSim->initStats(zinfo->rootStat);
    active = n;
    std::vector<uint64_t> eofIssue(n), eofRetire(n);
    auto run = [&](unsigned c) {
        auto f = cores[c]->GetFuncPtrs();
        auto one = block(1, 1);
        auto end = block(0, 0);
        one->oooBbl[0].uop[0].type = UOP_LOAD;
        one->oooBbl[0].uop[0].lat = 1;
        one->oooBbl[0].uop[0].portMask = 4;
        cores[c]->join();
        for (unsigned i = 0; i < 128; i++) {
            f.bblPtr(c, 0x1000, one);
            f.loadPtr(c, 0x100000 + 64 * i);
        }
        f.bblPtr(c, 0x1000, end);
        static_cast<OOOCore *>(cores[c])->traceDrain(c);
        eofIssue[c] = static_cast<OOOCore *>(cores[c])->traceIssueCycle();
        eofRetire[c] = static_cast<OOOCore *>(cores[c])->traceRobTail();
        cores[c]->leave();
        --active;
        while (TakeBarrier(c, c) != UINT32_MAX) {
        }
        free(one);
        free(end);
    };
    double initializationSeconds =
        std::chrono::duration<double>(std::chrono::steady_clock::now() - initializationStart)
            .count();
    auto warmupStart = std::chrono::steady_clock::now();
    if (!selftest) {
        // Functional warmup uses native cache/coherence operations, without weave events.
        fstFunctionalWarmup = true;
        for (unsigned c = 0; c < n; c++) {
            bool startsMacro = true;
            for (uint64_t j = 0; j < inputs[c].entry.warmup_records; j++) {
                const auto &row = inputs[c].normalized[j];
                const auto &r = row.r;
                if (startsMacro)
                    mem.l1i[c]->load(row.codepc, 0);
                startsMacro = r.completes_macro();
                if (r.is_branch())
                    static_cast<OOOCore *>(cores[c])->traceWarmupBranch(r.pc, r.is_taken());
                if (r.is_memory()) {
                    if (r.is_write())
                        mem.l1d[c]->store(r.address, 0);
                    else
                        mem.l1d[c]->load(r.address, 0);
                }
            }
        }
        for (unsigned c = 0; c < n; c++) {
            mem.l1i[c]->traceFinishWarmup();
            mem.l1d[c]->traceFinishWarmup();
        }
        fstFunctionalWarmup = false;
    }
    auto replay = [&](unsigned c) {
        uint64_t ordinal = inputs[c].entry.warmup_records;
        auto f = cores[c]->GetFuncPtrs();
        BblInfo *previous = nullptr;
        struct Row {
            fstv7::FstRecord r;
            DynUop u;
            uint64_t codepc;
            unsigned bytes;
        };
        std::vector<Row> rows;
        unsigned macroCount = 0, byteCount = 0;
        bool branchInBlock = false;
        auto submit = [&]() {
            if (rows.empty())
                return;
            auto b = block(macroCount, rows.size());
            b->bytes = byteCount;
            b->oooBbl[0].addr = rows.front().codepc;
            std::vector<DynUop> ops;
            ops.reserve(rows.size());
            for (unsigned i = 0; i < rows.size(); i++) {
                auto u = rows[i].u;
                u.decCycle = i / 4;
                ops.push_back(u);
            }
            std::memcpy(reinterpret_cast<char *>(b) + offsetof(BblInfo, oooBbl) +
                            offsetof(DynBbl, uop),
                        ops.data(), ops.size() * sizeof(DynUop));
            f.bblPtr(c, rows.front().codepc, b);
            free(previous);
            previous = b;
            for (auto &x : rows) {
                if (x.r.is_memory()) {
                    if (x.r.is_write())
                        f.storePtr(c, x.r.address);
                    else
                        f.loadPtr(c, x.r.address);
                }
                if (x.r.is_branch() && x.r.is_conditional())
                    f.branchPtr(c, x.r.pc, x.r.is_taken(), x.r.target, x.r.next_pc);
            }
            rows.clear();
            macroCount = byteCount = 0;
            branchInBlock = false;
        };
        cores[c]->join();
        bool newmacro = true;
        while (ordinal < inputs[c].normalized.size()) {
            const auto &nr = inputs[c].normalized[ordinal++];
            const auto &rec = nr.r;
            Row row{rec, nr.u, nr.codepc, nr.bytes};
            if (newmacro && !rows.empty() && row.codepc != rows.front().codepc + byteCount)
                submit();
            if (newmacro) {
                byteCount += row.bytes;
            }
            branchInBlock = branchInBlock || rec.is_branch();
            rows.push_back(row);
            newmacro = rec.completes_macro();
            if (newmacro)
                ++macroCount;
            if ((newmacro && branchInBlock) || rows.size() >= 64)
                submit();
        }
        submit();
        auto end = block(0, 0);
        f.bblPtr(c, 0x1000, end);
        static_cast<OOOCore *>(cores[c])->traceDrain(c);
        eofIssue[c] = static_cast<OOOCore *>(cores[c])->traceIssueCycle();
        eofRetire[c] = static_cast<OOOCore *>(cores[c])->traceRobTail();
        free(previous);
        cores[c]->leave();
        --active;
        while (TakeBarrier(c, c) != UINT32_MAX) {
        }
        free(end);
    };
    double warmupSeconds =
        std::chrono::duration<double>(std::chrono::steady_clock::now() - warmupStart).count();
    auto measurementStart = std::chrono::steady_clock::now();
    std::vector<std::thread> workers;
    for (unsigned c = 0; c < n; c++)
        workers.emplace_back(selftest ? run : std::function<void(unsigned)>(replay), c);
    for (auto &t : workers)
        t.join();
    double measurementSeconds =
        std::chrono::duration<double>(std::chrono::steady_clock::now() - measurementStart).count();
    std::cerr << "[TRACEBENCH-END] host_monotonic_ns="
              << std::chrono::duration_cast<std::chrono::nanoseconds>(
                     std::chrono::steady_clock::now().time_since_epoch()).count() << "\n";
    // Audit the actual prepared input only AFTER every model worker has ended.
    // This cannot consume CPU concurrently with the timed simulation interval.
    std::vector<uint64_t> preparedHashes(n, 14695981039346656037ull);
    if (!selftest) for (unsigned c = 0; c < n; ++c) {
        auto &h = preparedHashes[c];
        auto mix = [&](const void *p, size_t bytes) {
            const auto *b = static_cast<const unsigned char *>(p);
            for (size_t j = 0; j < bytes; ++j) h = (h ^ b[j]) * 1099511628211ull;
        };
        for (const auto &row : inputs[c].normalized) {
            h = fstv7::semantic_record_hash(h, row.r, row.codepc, row.bytes, {});
            mix(row.u.rs, sizeof(row.u.rs)); mix(row.u.rd, sizeof(row.u.rd));
            mix(&row.u.lat, sizeof(row.u.lat)); mix(&row.u.type, sizeof(row.u.type));
            mix(&row.u.portMask, sizeof(row.u.portMask));
            mix(&row.u.extraSlots, sizeof(row.u.extraSlots));
            mix(&row.u.traceSerialize, sizeof(row.u.traceSerialize));
        }
    }
    std::cout << "{\"core_engine\":\"zsim::OOOCore\",\"cores\":" << n
              << ",\"weave_phases\":" << zinfo->numPhases
              << ",\"weave_limit\":" << zinfo->contentionSim->getLastLimit()
              << ",\"instructions\":[";
    for (unsigned c = 0; c < n; c++) {
        if (c)
            std::cout << ",";
        std::cout << cores[c]->getInstrs();
    }
    std::cout << "],\"uops\":[";
    for (unsigned c = 0; c < n; c++) {
        if (c)
            std::cout << ",";
        std::cout << static_cast<OOOCore *>(cores[c])->traceUops();
    }
    std::cout << "],\"native_cycles\":[";
    for (unsigned c = 0; c < n; c++) {
        if (c)
            std::cout << ",";
        std::cout << cores[c]->getCycles();
    }
    std::cout << "],\"bound_eof_cycles\":[";
    for (unsigned c = 0; c < n; c++) {
        if (c)
            std::cout << ",";
        std::cout << eofIssue[c];
    }
    std::cout << "],\"bound_retirement_tail\":[";
    for (unsigned c = 0; c < n; c++) {
        if (c)
            std::cout << ",";
        std::cout << eofRetire[c];
    }
    std::cout << "],\"unknown_code_records\":[";
    for (unsigned c = 0; c < n; c++) {
        if (c)
            std::cout << ",";
        std::cout << (selftest ? 0 : inputs[c].unknown_code_records);
    }
    std::cout << "],\"allow_incomplete_code\":" << (allowIncompleteCode ? "true" : "false")
              << ",\"preparation_workers\":" << preparationWorkers
              << ",\"preparation_seconds\":" << preparationSeconds
              << ",\"initialization_seconds\":" << initializationSeconds
              << ",\"warmup_seconds\":" << warmupSeconds
              << ",\"measurement_seconds\":" << measurementSeconds << ",\"semantic_hashes\":[";
    for (unsigned c = 0; c < n; c++) {
        if (c)
            std::cout << ",";
        std::cout << "\"" << std::to_string(selftest ? 0 : inputs[c].semantic_hash) << "\"";
    }
    std::cout << "],\"prepared_hashes\":[";
    for (unsigned c = 0; c < n; ++c) {
        if (c) std::cout << ",";
        std::cout << "\"" << preparedHashes[c] << "\"";
    }
    std::cout << "],\"preparation_file_seconds\":[";
    for (unsigned c = 0; c < preparationFileSeconds.size(); ++c) {
        if (c) std::cout << ",";
        std::cout << preparationFileSeconds[c];
    }
    std::cout << "],\"frequency_hz\":" << uint64_t(zinfo->freqMHz) * 1000000ull
              << ",\"l2_mshrs\":" << g.l2Mshrs << ",\"llc_mshrs\":" << g.l3Mshrs << "}"
              << std::endl;
    zinfo->contentionSim->finish();
} catch (const std::exception &e) {
    std::cerr << "native adapter rejected input: " << e.what() << std::endl;
    return 1;
}
