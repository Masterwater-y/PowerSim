// uarch_profile.hh —— Oracle / ref_sim / pmu_report 的微架构配置单一信源。
//
// 设计原则：
//   1. 任何缓存 / TLB / page walker / MSHR 参数都从此结构读取，
//      禁止在 oracle / ref_sim 代码里 hardcode。
//   2. 由 run_mt_mvp.py 仿真前生成 uarch_profile.json，
//      或 tools/extract_uarch_profile.py 从已有 m5out/config.ini 提取。
//   3. 任何不支持的微架构维度（非 LRU 替换 / 非 inclusive / 非 MESI_Three_Level）
//      必须在 validate() 里 fail-fast，禁止 silent fallback。
//   4. 本头同时被：
//        gem5/src/cpu/o3/probe/tao_trace.cc  (训练侧 oracle)
//        single_core_mvp/mesi_ref_sim/include/simulator.hpp (部署侧 ref_sim)
//      include，保证 bit-exact 一致。
//
// 不依赖第三方 JSON 库（与 simulator.hpp 风格一致），手写最小解析器：
//   只支持 {"key": <number|string|null|object>}，足够 schema v2。

#pragma once

#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <optional>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>

namespace tao_uarch {

struct CacheCfg {
    uint64_t size_b = 0;
    uint32_t assoc = 0;
    uint32_t line_b = 64;
    uint32_t num_banks = 1;
    uint32_t bank_select_low_bit = 6;  // 默认 cacheline 内位数（64B → 6）
    std::string policy = "lru";

    uint32_t cacheline_bits() const {
        // line_b == 64 → 6
        uint32_t b = 0, v = line_b;
        while (v > 1) { v >>= 1; ++b; }
        return b;
    }
    uint64_t lines_total()    const { return size_b / line_b; }
    uint64_t lines_per_bank() const { return lines_total() / num_banks; }
    uint64_t sets_per_bank()  const { return lines_per_bank() / assoc; }
};

struct TlbCfg {
    uint32_t entries = 0;
    uint32_t assoc = 0;
};

struct WalkerCfg {
    uint32_t levels = 4;
    uint32_t page_size_bits = 12;     // 4 KiB
    std::string attach = "sequencer"; // "sequencer" | "functional" | "l2"
    uint32_t pwc_entries = 0;         // page walk cache，0 = 无
};

struct MshrCfg {
    uint32_t l1d = 16;
    uint32_t l2 = 32;
    uint32_t l3 = 64;
};

struct UarchProfile {
    int schema_version = 2;
    std::string source = "";
    std::string isa = "X86";
    uint32_t num_cores = 4;
    double freq_ghz = 2.0;

    CacheCfg l1d, l1i, l2, l3;
    TlbCfg dtlb, itlb;
    std::optional<TlbCfg> stlb;
    WalkerCfg walker;
    MshrCfg mshr;
    std::string protocol = "MESI_Three_Level";

    // ----- 工厂 -----
    static UarchProfile load(const std::string& path);
    void validate() const;

    // 调试输出
    void dump(std::FILE* f = stderr) const;
};

// ====================== JSON 极小解析器 ======================
//
// 仅支持本 schema v2 用到的子集：
//   - 对象 { ... }
//   - 字符串 "..."（不处理转义里的 unicode）
//   - 整数 / 浮点（用 strtoll/strtod）
//   - null
//   - 嵌套对象
// 不支持数组（schema v2 不需要）。
//
// 用 path 寻址，path 用 '.' 分隔。例：cache.l3.num_banks。

namespace detail {

inline void skipWs(const std::string& s, size_t& p) {
    while (p < s.size()) {
        char c = s[p];
        if (c == ' ' || c == '\t' || c == '\n' || c == '\r') { ++p; }
        else break;
    }
}

// 把 s[p..] 里整个 token 切出（用于读 number / true / false / null）
inline std::string readToken(const std::string& s, size_t& p) {
    size_t start = p;
    while (p < s.size()) {
        char c = s[p];
        if (c == ',' || c == '}' || c == ']' || c == ' '
            || c == '\t' || c == '\n' || c == '\r') break;
        ++p;
    }
    return s.substr(start, p - start);
}

inline std::string readString(const std::string& s, size_t& p) {
    if (p >= s.size() || s[p] != '"')
        throw std::runtime_error("uarch_profile: expected string at "
                                 + std::to_string(p));
    ++p;
    size_t start = p;
    while (p < s.size() && s[p] != '"') {
        if (s[p] == '\\' && p + 1 < s.size()) p += 2;
        else ++p;
    }
    if (p >= s.size())
        throw std::runtime_error("uarch_profile: unterminated string");
    std::string out = s.substr(start, p - start);
    ++p; // skip closing "
    return out;
}

// 在 obj_text（已知第一个非空字符是 '{'）里寻找 key 对应的 value 起点；
// 返回 value 的子串（如果是对象就含 {} 整体，如果是 number/string 就含其原始 token）。
// 如果找不到 key 返回 std::nullopt。
inline std::optional<std::string>
findValue(const std::string& obj_text, const std::string& key);

// 从某个 ‘{’ 开始一直读到匹配 ‘}’，返回这段子串（含两端括号）。
inline std::string readObject(const std::string& s, size_t& p) {
    if (p >= s.size() || s[p] != '{')
        throw std::runtime_error("uarch_profile: expected '{' at "
                                 + std::to_string(p));
    int depth = 0;
    size_t start = p;
    bool in_str = false;
    while (p < s.size()) {
        char c = s[p];
        if (in_str) {
            if (c == '\\' && p + 1 < s.size()) { p += 2; continue; }
            if (c == '"') in_str = false;
            ++p;
            continue;
        }
        if (c == '"') { in_str = true; ++p; continue; }
        if (c == '{') ++depth;
        else if (c == '}') {
            --depth;
            if (depth == 0) {
                ++p;
                return s.substr(start, p - start);
            }
        }
        ++p;
    }
    throw std::runtime_error("uarch_profile: unterminated object");
}

inline std::optional<std::string>
findValue(const std::string& obj_text, const std::string& key) {
    if (obj_text.empty() || obj_text.front() != '{') return std::nullopt;
    size_t p = 1; // skip '{'
    skipWs(obj_text, p);
    while (p < obj_text.size() && obj_text[p] != '}') {
        // read key
        std::string k = readString(obj_text, p);
        skipWs(obj_text, p);
        if (p >= obj_text.size() || obj_text[p] != ':')
            throw std::runtime_error(
                "uarch_profile: expected ':' after key " + k);
        ++p;
        skipWs(obj_text, p);
        // capture value
        std::string val;
        if (obj_text[p] == '{') {
            val = readObject(obj_text, p);
        } else if (obj_text[p] == '"') {
            // 读字符串时把 "..." 整体保留
            size_t s_start = p;
            (void)readString(obj_text, p);
            val = obj_text.substr(s_start, p - s_start);
        } else {
            val = readToken(obj_text, p);
        }
        if (k == key) return val;
        skipWs(obj_text, p);
        if (p < obj_text.size() && obj_text[p] == ',') {
            ++p;
            skipWs(obj_text, p);
        }
    }
    return std::nullopt;
}

// path 形如 "cache.l3.num_banks"，逐级下钻
inline std::optional<std::string>
findPath(const std::string& root_obj, const std::string& path) {
    std::string cur = root_obj;
    size_t i = 0;
    while (i < path.size()) {
        size_t dot = path.find('.', i);
        std::string key = (dot == std::string::npos)
                              ? path.substr(i)
                              : path.substr(i, dot - i);
        auto v = findValue(cur, key);
        if (!v) return std::nullopt;
        if (dot == std::string::npos) return v;
        cur = *v;
        i = dot + 1;
    }
    return std::nullopt;
}

inline uint64_t parseU64(const std::string& tok) {
    return std::strtoull(tok.c_str(), nullptr, 10);
}
inline int64_t parseI64(const std::string& tok) {
    return std::strtoll(tok.c_str(), nullptr, 10);
}
inline double parseF64(const std::string& tok) {
    return std::strtod(tok.c_str(), nullptr);
}
inline std::string parseStr(const std::string& tok) {
    if (tok.size() >= 2 && tok.front() == '"' && tok.back() == '"')
        return tok.substr(1, tok.size() - 2);
    return tok;
}
inline bool isNull(const std::string& tok) {
    if (tok.size() < 4) return false;
    // 兼容前后有空格
    size_t i = 0;
    while (i < tok.size() && std::isspace((unsigned char)tok[i])) ++i;
    return tok.compare(i, 4, "null") == 0;
}

inline uint64_t getU64(const std::string& root, const std::string& path,
                       uint64_t def) {
    auto v = findPath(root, path);
    if (!v) return def;
    return parseU64(*v);
}
inline std::string getStr(const std::string& root, const std::string& path,
                          const std::string& def) {
    auto v = findPath(root, path);
    if (!v) return def;
    return parseStr(*v);
}
inline double getF64(const std::string& root, const std::string& path,
                     double def) {
    auto v = findPath(root, path);
    if (!v) return def;
    return parseF64(*v);
}

inline void parseCache(const std::string& root, const std::string& base,
                       CacheCfg& c, bool require) {
    auto obj = findPath(root, base);
    if (!obj) {
        if (require)
            throw std::runtime_error("uarch_profile: missing required "
                                     "cache section: " + base);
        return;
    }
    c.size_b   = getU64(root, base + ".size_b",   c.size_b);
    c.assoc    = (uint32_t)getU64(root, base + ".assoc", c.assoc);
    c.line_b   = (uint32_t)getU64(root, base + ".line_b", c.line_b);
    c.num_banks = (uint32_t)getU64(root, base + ".num_banks", c.num_banks);
    c.bank_select_low_bit =
        (uint32_t)getU64(root, base + ".bank_select_low_bit",
                         c.bank_select_low_bit);
    c.policy = getStr(root, base + ".policy", c.policy);
}

inline void parseTlb(const std::string& root, const std::string& base,
                     TlbCfg& t, bool require) {
    auto obj = findPath(root, base);
    if (!obj) {
        if (require)
            throw std::runtime_error("uarch_profile: missing required "
                                     "tlb section: " + base);
        return;
    }
    t.entries = (uint32_t)getU64(root, base + ".entries", t.entries);
    t.assoc   = (uint32_t)getU64(root, base + ".assoc", t.assoc);
}

} // namespace detail

inline UarchProfile UarchProfile::load(const std::string& path) {
    std::ifstream f(path);
    if (!f.good())
        throw std::runtime_error("uarch_profile: cannot open " + path);
    std::stringstream ss;
    ss << f.rdbuf();
    std::string root = ss.str();
    // 去掉前后空白让 root 以 '{' 开头
    size_t p = 0;
    detail::skipWs(root, p);
    root = root.substr(p);

    UarchProfile cfg;
    cfg.schema_version =
        (int)detail::getU64(root, "schema_version", cfg.schema_version);
    cfg.source         = detail::getStr(root, "source",   cfg.source);
    cfg.isa            = detail::getStr(root, "core.isa", cfg.isa);
    cfg.num_cores      = (uint32_t)detail::getU64(root, "core.num_cores",
                                                  cfg.num_cores);
    cfg.freq_ghz       = detail::getF64(root, "core.freq_ghz", cfg.freq_ghz);

    detail::parseCache(root, "cache.l1d", cfg.l1d, true);
    detail::parseCache(root, "cache.l1i", cfg.l1i, true);
    detail::parseCache(root, "cache.l2",  cfg.l2,  true);
    detail::parseCache(root, "cache.l3",  cfg.l3,  true);

    detail::parseTlb(root, "tlb.dtlb", cfg.dtlb, true);
    detail::parseTlb(root, "tlb.itlb", cfg.itlb, true);
    {
        auto stlb_obj = detail::findPath(root, "tlb.stlb");
        if (stlb_obj) {
            std::string body = *stlb_obj;
            // 去前后空白
            size_t i = 0;
            while (i < body.size()
                   && std::isspace((unsigned char)body[i])) ++i;
            std::string trimmed = body.substr(i);
            if (!detail::isNull(trimmed)) {
                TlbCfg s;
                detail::parseTlb(root, "tlb.stlb", s, true);
                cfg.stlb = s;
            }
        }
    }

    cfg.walker.levels =
        (uint32_t)detail::getU64(root, "page_walker.levels",
                                 cfg.walker.levels);
    cfg.walker.page_size_bits =
        (uint32_t)detail::getU64(root, "page_walker.page_size_bits",
                                 cfg.walker.page_size_bits);
    cfg.walker.attach =
        detail::getStr(root, "page_walker.walk_attaches_to",
                       cfg.walker.attach);
    cfg.walker.pwc_entries =
        (uint32_t)detail::getU64(root, "page_walker.pwc_entries",
                                 cfg.walker.pwc_entries);

    cfg.mshr.l1d =
        (uint32_t)detail::getU64(root, "mshr.l1d_entries", cfg.mshr.l1d);
    cfg.mshr.l2 =
        (uint32_t)detail::getU64(root, "mshr.l2_entries", cfg.mshr.l2);
    cfg.mshr.l3 =
        (uint32_t)detail::getU64(root, "mshr.l3_entries", cfg.mshr.l3);

    cfg.protocol =
        detail::getStr(root, "coherence.protocol", cfg.protocol);

    cfg.validate();
    return cfg;
}

inline void UarchProfile::validate() const {
    auto die = [](const std::string& m) {
        throw std::runtime_error("uarch_profile.validate: " + m);
    };
    if (schema_version != 2)
        die("only schema_version=2 supported, got "
            + std::to_string(schema_version));
    if (protocol != "MESI_Three_Level")
        die("only protocol=MESI_Three_Level supported, got '"
            + protocol + "'");
    auto chk_cache = [&](const char* name, const CacheCfg& c) {
        if (c.size_b == 0)  die(std::string(name) + ": size_b == 0");
        if (c.assoc == 0)   die(std::string(name) + ": assoc == 0");
        if (c.line_b == 0)  die(std::string(name) + ": line_b == 0");
        if (c.num_banks == 0) die(std::string(name) + ": num_banks == 0");
        if (c.policy != "lru")
            die(std::string(name)
                + ": only policy=lru supported, got '" + c.policy + "'");
        if (c.size_b % c.line_b != 0)
            die(std::string(name) + ": size_b not divisible by line_b");
        if (c.lines_total() % c.num_banks != 0)
            die(std::string(name) + ": size_b/line_b not divisible by "
                "num_banks");
        if (c.lines_per_bank() % c.assoc != 0)
            die(std::string(name) + ": lines_per_bank not divisible by "
                "assoc");
    };
    chk_cache("l1d", l1d);
    chk_cache("l1i", l1i);
    chk_cache("l2",  l2);
    chk_cache("l3",  l3);
    if (dtlb.entries == 0 || dtlb.assoc == 0)
        die("dtlb entries/assoc must be non-zero");
    if (itlb.entries == 0 || itlb.assoc == 0)
        die("itlb entries/assoc must be non-zero");
    if (dtlb.entries % dtlb.assoc != 0)
        die("dtlb.entries not divisible by assoc");
    if (itlb.entries % itlb.assoc != 0)
        die("itlb.entries not divisible by assoc");
    if (walker.levels < 2 || walker.levels > 5)
        die("page_walker.levels out of range (got "
            + std::to_string(walker.levels) + ")");
    if (walker.page_size_bits < 12 || walker.page_size_bits > 30)
        die("page_walker.page_size_bits out of range");
    if (walker.attach != "sequencer" && walker.attach != "functional"
        && walker.attach != "l2")
        die("page_walker.walk_attaches_to must be sequencer|functional|l2");
    if (num_cores == 0) die("core.num_cores == 0");
}

inline void UarchProfile::dump(std::FILE* f) const {
    std::fprintf(f,
        "[uarch_profile] schema=%d isa=%s cores=%u freq=%.2f GHz protocol=%s\n",
        schema_version, isa.c_str(), num_cores, freq_ghz, protocol.c_str());
    auto pc = [&](const char* n, const CacheCfg& c) {
        std::fprintf(f,
            "  %s size=%lu B assoc=%u line=%u banks=%u sets/bank=%lu\n",
            n, (unsigned long)c.size_b, c.assoc, c.line_b,
            c.num_banks, (unsigned long)c.sets_per_bank());
    };
    pc("l1d", l1d);
    pc("l1i", l1i);
    pc("l2 ", l2);
    pc("l3 ", l3);
    std::fprintf(f, "  dtlb entries=%u assoc=%u itlb entries=%u assoc=%u\n",
                 dtlb.entries, dtlb.assoc, itlb.entries, itlb.assoc);
    std::fprintf(f, "  walker levels=%u page_bits=%u attach=%s pwc=%u\n",
                 walker.levels, walker.page_size_bits,
                 walker.attach.c_str(), walker.pwc_entries);
    std::fprintf(f, "  mshr l1d=%u l2=%u l3=%u\n",
                 mshr.l1d, mshr.l2, mshr.l3);
}

} // namespace tao_uarch
