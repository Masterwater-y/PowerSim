#include "fst_memory.h"

#include "cache.h"
#include "cache_arrays.h"
#include "coherence_ctrls.h"
#include "hash.h"
#include "mem_ctrls.h"
#include "memory_hierarchy.h"
#include "repl_policies.h"
#include "timing_cache.h"
#include "zsim.h"
#include <cassert>

namespace {

// Build a terminal FilterCache (L1I/L1D).  Mirrors BuildCacheBank's terminal
// branch but takes explicit geometry instead of a libconfig Config.
FilterCache *makeFilterCache(uint32_t sizeBytes, uint32_t ways, uint32_t lineSize, g_string &name) {
    uint32_t numLines = sizeBytes / lineSize;
    uint32_t numSets = numLines / ways;
    assert(numSets > 0 && (numSets & (numSets - 1)) == 0);

    HashFamily *hf = new IdHashFamily();
    LRUReplPolicy<false> *rp = new LRUReplPolicy<false>(numLines);
    SetAssocArray *array = new SetAssocArray(numLines, ways, rp, hf);
    MESITerminalCC *cc = new MESITerminalCC(numLines, name);
    rp->setCC(cc);
    // Terminal caches hide accLat in the pipeline (accLat = 0).
    return new FilterCache(numSets, numLines, cc, array, rp, 0, 0, name);
}

// Build a non-terminal inclusive/MSI backing cache (L2/L3).
Cache *makeBackingCache(uint32_t sizeBytes, uint32_t ways, uint32_t lineSize, uint32_t latency,
                        uint32_t mshrs, g_string &name) {
    uint32_t numLines = sizeBytes / lineSize;
    uint32_t numSets = numLines / ways;
    assert(numSets > 0 && (numSets & (numSets - 1)) == 0);

    HashFamily *hf = new IdHashFamily();
    LRUReplPolicy<true> *rp = new LRUReplPolicy<true>(numLines);
    SetAssocArray *array = new SetAssocArray(numLines, ways, rp, hf);
    MESICC *cc = new MESICC(numLines, false /*nonInclusiveHack*/, name);
    rp->setCC(cc);
    return new TimingCache(numLines, cc, array, rp, latency, latency, mshrs, 1, ways, ways, 0,
                           name);
}

} // namespace

FstMemorySystem::FstMemorySystem() : l3(nullptr), mem(nullptr) {}

void FstMemorySystem::build(const FstMachineGeom &geom) {
    geom_ = geom;
    const uint32_t n = geom.numCores;
    l1i.resize(n);
    l1d.resize(n);
    l2.resize(n);

    // Per-core terminal filter caches and private L2.
    for (uint32_t c = 0; c < n; ++c) {
        g_string iname((std::string("l1i-") + std::to_string(c)).c_str());
        g_string dname((std::string("l1d-") + std::to_string(c)).c_str());
        g_string l2name((std::string("l2-") + std::to_string(c)).c_str());

        FilterCache *ic = makeFilterCache(geom.l1iSize, geom.l1iWays, geom.lineSize, iname);
        FilterCache *dc = makeFilterCache(geom.l1dSize, geom.l1dWays, geom.lineSize, dname);
        Cache *l2c = makeBackingCache(geom.l2Size, geom.l2Ways, geom.lineSize, geom.l2Latency,
                                      geom.l2Mshrs, l2name);

        ic->setSourceId(c);
        ic->setFlags(MemReq::IFETCH | MemReq::NOEXCL);
        dc->setSourceId(c);

        l1i[c] = ic;
        l1d[c] = dc;
        l2[c] = l2c;
        allCaches_.push_back(ic);
        allCaches_.push_back(dc);
        allCaches_.push_back(l2c);

        // L1I/L1D -> private L2.
        g_vector<MemObject *> l2Parent;
        l2Parent.push_back(l2c);
        g_vector<BaseCache *> l2Children;
        l2Children.push_back(ic);
        l2Children.push_back(dc);
        ic->setParents(0, l2Parent, nullptr);
        dc->setParents(1, l2Parent, nullptr);
        l2c->setChildren(l2Children, nullptr);
    }

    // Shared L3.
    g_string l3name("l3");
    l3 = makeBackingCache(geom.l3Size, geom.l3Ways, geom.lineSize, geom.l3Latency, geom.l3Mshrs,
                          l3name);
    allCaches_.push_back(l3);

    g_vector<MemObject *> l3Parent;
    g_vector<BaseCache *> l3Children;
    for (uint32_t c = 0; c < n; ++c) {
        l3Parent.clear();
        l3Parent.push_back(l3);
        // Each L2 occupies its own child slot on the single L3 bank.
        l2[c]->setParents(c, l3Parent, nullptr);
        l3Children.push_back(l2[c]);
    }
    l3->setChildren(l3Children, nullptr);

    // Single constant-latency DRAM controller.  Latency is expressed in core
    // cycles (zsim runs a single core-cycle domain).
    g_string memname("mem-0");
    mem = new SimpleMemory(geom.dramLatency, memname);
    g_vector<MemObject *> memVec;
    memVec.push_back(mem);
    l3->setParents(0, memVec, nullptr);
}

void FstMemorySystem::initStats(AggregateStat *rootStat) {
    AggregateStat *memHierStat = new AggregateStat();
    memHierStat->init("memHier", "FST zsim memory hierarchy");
    for (Cache *c : allCaches_)
        c->initStats(memHierStat);
    rootStat->append(memHierStat);
}

void FstMemorySystem::warmupLoad(uint32_t core, Address addr, bool ifetch) {
    if (ifetch)
        l1i[core]->load(addr, 0);
    else
        l1d[core]->load(addr, 0);
}

void FstMemorySystem::warmupStore(uint32_t core, Address addr) { l1d[core]->store(addr, 0); }
