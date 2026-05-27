// 独立编译测试：验证 uarch_profile / lru_banked 基础行为。
// 编译: g++ -std=c++17 -O2 -I single_core_mvp/shared
//           single_core_mvp/shared/test_uarch.cc -o /tmp/test_uarch
// 测项:
//   1. UarchProfile::load 能解析示例 JSON 并 validate
//   2. validate 能正确 reject 不支持配置（schema_version != 2 / policy != lru / 容量不整除）
//   3. BankedSetAssocLRU bank_split + set 索引正确
//   4. TlbSim 命中行为
//   5. PageWalkSim levels 计数正确

#include <cassert>
#include <cstdio>
#include <fstream>
#include <string>

#include "uarch_profile.hh"
#include "lru_banked.hh"

using namespace tao_uarch;

static const char* SAMPLE_JSON = R"({
  "schema_version": 2,
  "source": "test",
  "core": {"num_cores": 4, "freq_ghz": 2.0, "isa": "X86"},
  "cache": {
    "l1d": {"size_b": 32768,   "assoc": 8,  "line_b": 64,
            "num_banks": 1, "bank_select_low_bit": 6, "policy": "lru"},
    "l1i": {"size_b": 32768,   "assoc": 8,  "line_b": 64,
            "num_banks": 1, "bank_select_low_bit": 6, "policy": "lru"},
    "l2":  {"size_b": 262144,  "assoc": 8,  "line_b": 64,
            "num_banks": 1, "bank_select_low_bit": 6, "policy": "lru"},
    "l3":  {"size_b": 2097152, "assoc": 16, "line_b": 64,
            "num_banks": 4, "bank_select_low_bit": 6, "policy": "lru"}
  },
  "tlb": {
    "dtlb": {"entries": 64, "assoc": 4},
    "itlb": {"entries": 64, "assoc": 4},
    "stlb": null
  },
  "page_walker": {
    "levels": 4, "page_size_bits": 12,
    "walk_attaches_to": "sequencer", "pwc_entries": 0
  },
  "coherence": {"protocol": "MESI_Three_Level"},
  "mshr": {"l1d_entries": 16, "l2_entries": 32, "l3_entries": 64}
})";

static std::string writeTmp(const char* body) {
    std::string path = "/tmp/test_uarch_profile.json";
    std::ofstream f(path);
    f << body;
    f.close();
    return path;
}

static void testParseAndValidate() {
    auto p = writeTmp(SAMPLE_JSON);
    auto cfg = UarchProfile::load(p);
    assert(cfg.schema_version == 2);
    assert(cfg.num_cores == 4);
    assert(cfg.l3.size_b == 2097152);
    assert(cfg.l3.num_banks == 4);
    assert(cfg.l3.lines_total() == 32768);
    assert(cfg.l3.lines_per_bank() == 8192);
    assert(cfg.l3.sets_per_bank() == 512);
    assert(cfg.dtlb.entries == 64);
    assert(!cfg.stlb.has_value());
    assert(cfg.protocol == "MESI_Three_Level");
    cfg.dump();
    std::printf("[PASS] testParseAndValidate\n");
}

static void testValidateReject() {
    // schema 错
    {
        std::string body = SAMPLE_JSON;
        size_t pos = body.find("\"schema_version\": 2");
        assert(pos != std::string::npos);
        body.replace(pos, 19, "\"schema_version\": 3");
        auto p = writeTmp(body.c_str());
        bool threw = false;
        try { UarchProfile::load(p); } catch (...) { threw = true; }
        assert(threw);
    }
    // policy != lru
    {
        std::string body = SAMPLE_JSON;
        size_t pos = body.find("\"lru\"");
        body.replace(pos, 5, "\"rrip\"");
        auto p = writeTmp(body.c_str());
        bool threw = false;
        try { UarchProfile::load(p); } catch (...) { threw = true; }
        assert(threw);
    }
    // 容量不整除
    {
        std::string body = SAMPLE_JSON;
        size_t pos = body.find("\"size_b\": 2097152");
        body.replace(pos, 17, "\"size_b\": 2097153");
        auto p = writeTmp(body.c_str());
        bool threw = false;
        try { UarchProfile::load(p); } catch (...) { threw = true; }
        assert(threw);
    }
    std::printf("[PASS] testValidateReject\n");
}

static void testBankedLru() {
    CacheCfg c;
    c.size_b = 2097152; c.assoc = 16; c.line_b = 64;
    c.num_banks = 4; c.bank_select_low_bit = 6; c.policy = "lru";
    BankedSetAssocLRU lru;
    lru.configure(c);
    assert(lru.numBanks() == 4);
    assert(lru.setsPerBank() == 512);
    assert(lru.ways() == 16);

    // touch + contains
    assert(!lru.touch(0x1000));
    assert(lru.touch(0x1000));      // 第二次 hit
    assert(lru.contains(0x1000));
    // 不同 bank 互不干扰
    assert(!lru.touch(0x1040));     // 行号差 1，bank 不同
    assert(lru.contains(0x1000));

    // 填满一个 set 触发 eviction
    // line_b=64, num_banks=4, sets/bank=512, ways=16
    // bank_shift=0（行号 LSB 即 bank 选位）
    // 同 bank 同 set：行号差需要 = sets_per_bank * num_banks 的倍数
    uint64_t step = 4 * 512 * 64;  // = 2 KiB byte stride
    uint64_t base = 0x100000;
    for (int i = 0; i < 16; ++i) lru.touch(base + i * step);
    assert(lru.contains(base));
    int64_t ev = -1;
    bool hit = lru.touch(base + 16 * step, &ev);
    assert(!hit);
    // base 已被 evict（最 LRU）
    assert(!lru.contains(base));
    assert(ev == (int64_t)base);
    std::printf("[PASS] testBankedLru (eviction at base=0x%lx)\n",
                (unsigned long)base);
}

static void testTlb() {
    TlbCfg t; t.entries = 64; t.assoc = 4;
    TlbSim tlb;
    tlb.configure(t, 12);
    // 64 entries / 4 assoc = 16 sets
    assert(!tlb.translate(0x1000));
    assert(tlb.translate(0x1000));   // hit
    assert(!tlb.translate(0x2000));
    assert(tlb.translate(0x2000));
    std::printf("[PASS] testTlb\n");
}

static void testWalker() {
    WalkerCfg w; w.levels = 4; w.page_size_bits = 12;
    PageWalkSim pw;
    pw.configure(w);

    CacheCfg c1; c1.size_b = 32768; c1.assoc = 8; c1.line_b = 64;
    c1.num_banks = 1; c1.bank_select_low_bit = 6; c1.policy = "lru";
    CacheCfg c2 = c1; c2.size_b = 262144;
    CacheCfg c3 = c1; c3.size_b = 2097152; c3.num_banks = 4; c3.assoc = 16;

    BankedSetAssocLRU l1d, l2, l3;
    l1d.configure(c1); l2.configure(c2); l3.configure(c3);
    auto r = pw.walk(0xdeadbe00, l1d, l2, l3);
    assert(r.levels == 4);
    // 第一次 walk 全部 miss → 全跑到 DRAM
    assert(r.miss_dram == 4);
    // 第二次同 vpn → L1D 全部命中
    auto r2 = pw.walk(0xdeadbe00, l1d, l2, l3);
    assert(r2.hit_l1d == 4);
    std::printf("[PASS] testWalker\n");
}

static void testMshr() {
    MshrTracker m;
    m.configure(/*capacity=*/16, /*window_seq=*/0);
    assert(m.insert(0x1000, 1));   // unique
    assert(!m.insert(0x1000, 2));  // coalesced
    m.retire(0x1000);
    assert(m.insert(0x1000, 3));   // unique again
    // window
    MshrTracker m2;
    m2.configure(0, 100);
    assert(m2.insert(0x2000, 10));
    assert(!m2.insert(0x2000, 50));
    assert(m2.insert(0x2000, 200));  // 超窗口 → 被清除后又作 unique
    std::printf("[PASS] testMshr\n");
}

int main() {
    try {
        testParseAndValidate();
        testValidateReject();
        testBankedLru();
        testTlb();
        testWalker();
        testMshr();
    } catch (const std::exception& e) {
        std::fprintf(stderr, "[FAIL] exception: %s\n", e.what());
        return 1;
    }
    std::printf("ALL TESTS PASSED\n");
    return 0;
}
