// Shared FST v7 trace reader for the Sniper/Zsim trace-driven ports.
//
// Header-only, C++11-clean (zsim builds with -std=c++0x -Werror) and free of
// C++17/20-only facilities so both simulators can include it directly with
// their native compile flags.  All layouts are byte-exact copies of FastSim's
// BinaryTraceHeader (72 B) and TraceRecord (64 B); static_asserts guard them.
//
// Reference: FastSim src/trace.cpp (BinaryTraceHeader, kTraceVersion=7) and
// include/fastsim/types.hpp (TraceRecord, TraceFlag, op-class encoding).
#ifndef FSTV7_FST_V7_HPP_
#define FSTV7_FST_V7_HPP_

#include <algorithm>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <map>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>
#include <zlib.h>
#include "tracebench_trust.hpp"

namespace fstv7 {

static const uint32_t kTraceVersion = 7;
static const uint16_t kHeaderSize = 72;
static const uint16_t kRecordSize = 64;

// ---------------------------------------------------------------------------
// On-disk flag encoding (mirrors fastsim::TraceFlag, types.hpp).
// ---------------------------------------------------------------------------
enum TraceFlag {
    kRetires = 1u << 0,
    kLoad = 1u << 1,
    kStore = 1u << 2,
    kAtomic = 1u << 3,
    kBranch = 1u << 4,
    kConditional = 1u << 5,
    kIndirect = 1u << 6,
    kCall = 1u << 7,
    kReturn = 1u << 8,
    kTaken = 1u << 9,
    kMicroOp = 1u << 10,
    kLastMicroOp = 1u << 11,
    kPhysicalAddress = 1u << 12,
    kSerialize = 1u << 13,
    kBranchOutcomeValid = 1u << 14,
    kVirtualPageToken = 1u << 15,
};

// Op-class privilege encoding (types.hpp).
static const int16_t kSyscallOpClass = -1;
static const int kKernelOpClassBias = 2;
static const int kSprSimdPack = 96;
static const int kSprSimdExtract = 97;
static const int kSprSimdInsert = 98;
static const int kSprStoreAddress = 99;
static const int kSprStoreData = 100;

inline bool has_flag(uint16_t flags, TraceFlag flag) {
    return (flags & static_cast<uint16_t>(flag)) != 0;
}

// ---------------------------------------------------------------------------
// 72-byte binary trace header, naturally aligned (8 + 16 + 16 + 32).
// ---------------------------------------------------------------------------
struct FstHeader {
    char magic[8];
    uint32_t version;
    uint32_t header_size;
    uint32_t record_size;
    uint32_t core_id;
    uint64_t record_count;
    uint64_t feature_flags;
    uint64_t reserved[4];
};
static_assert(sizeof(FstHeader) == 72, "FST header must stay 72 bytes");

// ---------------------------------------------------------------------------
// 64-byte trace record (byte-exact with fastsim::TraceRecord).
// ---------------------------------------------------------------------------
struct FstRecord {
    uint64_t pc;
    uint64_t address;
    uint64_t target;
    uint64_t next_pc;
    uint32_t producer_dists[4];
    uint16_t size;
    uint16_t flags;
    int16_t op_class;
    uint8_t n_src;
    uint8_t n_dst;
    uint8_t producer_classes[4];
    uint32_t reserved;

    bool retires() const { return has_flag(flags, kRetires); }
    bool is_load() const { return has_flag(flags, kLoad); }
    bool is_store() const { return has_flag(flags, kStore); }
    bool is_atomic() const { return has_flag(flags, kAtomic); }
    bool is_memory() const {
        return has_flag(flags, kLoad) || has_flag(flags, kStore) || has_flag(flags, kAtomic);
    }
    bool is_write() const { return has_flag(flags, kStore) || has_flag(flags, kAtomic); }
    bool is_branch() const { return has_flag(flags, kBranch); }
    bool is_conditional() const { return has_flag(flags, kConditional); }
    bool is_indirect() const { return has_flag(flags, kIndirect); }
    bool is_call() const { return has_flag(flags, kCall); }
    bool is_return() const { return has_flag(flags, kReturn); }
    bool is_taken() const { return has_flag(flags, kTaken); }
    bool is_microop() const { return has_flag(flags, kMicroOp); }
    bool is_last_microop() const { return has_flag(flags, kLastMicroOp); }
    bool is_syscall() const { return op_class == kSyscallOpClass; }
    bool is_kernel() const { return op_class < kSyscallOpClass; }
    bool outcome_valid() const { return has_flag(flags, kBranchOutcomeValid); }

    // Kernel records encode -(canonical) - kKernelOpClassBias; invert it.
    int canonical_op_class() const {
        return is_kernel() ? -static_cast<int>(op_class) - kKernelOpClassBias
                           : static_cast<int>(op_class);
    }

    // Retired-macro predicate, matching WarmupInstructionTraceSource's
    // completes_instruction (which defines the slice macro population):
    // the record must retire, and a non-micro-op completes itself while a
    // micro-op only completes a macro at its last-uop boundary.
    bool completes_macro() const {
        return retires() && (!has_flag(flags, kMicroOp) || has_flag(flags, kLastMicroOp));
    }
};
static_assert(sizeof(FstRecord) == 64, "FST record must stay 64 bytes");

// ---------------------------------------------------------------------------
// Manifest entry (binary-warmup-slice shape used by the SPR-M2 batch).
//
//   <core> binary-warmup-slice <rel-fst-path> <source-core>
//          <warmup-instructions> <take-instructions>
//          <warmup-records> <take-records>
// ---------------------------------------------------------------------------
struct ManifestEntry {
    uint32_t core_id;
    std::string format;
    std::string rel_path;
    uint32_t source_core;
    uint64_t warmup_instructions;
    uint64_t take_instructions;
    uint64_t warmup_records;
    uint64_t take_records;

    uint64_t total_records() const { return warmup_records + take_records; }
};

inline std::vector<ManifestEntry> parse_manifest(const std::string &manifest_path) {
    std::ifstream in(manifest_path.c_str());
    if (!in)
        throw std::runtime_error("cannot open manifest: " + manifest_path);

    // Resolve relative FST paths against the manifest's own directory.
    std::string dir;
    size_t slash = manifest_path.find_last_of('/');
    if (slash != std::string::npos)
        dir = manifest_path.substr(0, slash + 1);

    std::vector<ManifestEntry> entries;
    std::string line;
    size_t line_no = 0;
    while (std::getline(in, line)) {
        ++line_no;
        // Strip comments and blank lines.
        size_t hash = line.find('#');
        if (hash != std::string::npos)
            line = line.substr(0, hash);
        std::istringstream parser(line);
        std::string core_text, format, rel_path;
        if (!(parser >> core_text))
            continue;

        ManifestEntry e;
        parser >> format >> rel_path;
        if (format != "binary-warmup-slice" && format != "fastsim-binary-warmup-slice" &&
            format != "binary-warmup-state-slice" &&
            format != "fastsim-binary-warmup-state-slice") {
            throw std::runtime_error(manifest_path + ":" + std::to_string(line_no) +
                                     ": unsupported format '" + format +
                                     "' (need binary-warmup-slice)");
        }
        e.format = format;
        e.rel_path = rel_path;
        e.core_id = static_cast<uint32_t>(strtoul(core_text.c_str(), 0, 10));
        std::string source_text, wi, ti, wr, tr;
        if (!(parser >> source_text >> wi >> ti >> wr >> tr)) {
            throw std::runtime_error(manifest_path + ":" + std::to_string(line_no) +
                                     ": malformed warmup-slice entry");
        }
        e.source_core = static_cast<uint32_t>(strtoul(source_text.c_str(), 0, 10));
        e.warmup_instructions = strtoull(wi.c_str(), 0, 10);
        e.take_instructions = strtoull(ti.c_str(), 0, 10);
        e.warmup_records = strtoull(wr.c_str(), 0, 10);
        e.take_records = strtoull(tr.c_str(), 0, 10);
        entries.push_back(e);
    }
    if (entries.empty()) {
        throw std::runtime_error("no manifest entries in " + manifest_path);
    }
    return entries;
}

// Resolve an entry's FST path against the manifest directory.
inline std::string resolve_fst_path(const std::string &manifest_path, const std::string &rel_path) {
    size_t slash = manifest_path.find_last_of('/');
    if (slash == std::string::npos)
        return rel_path;
    if (!rel_path.empty() && rel_path[0] == '/')
        return rel_path;
    return manifest_path.substr(0, slash + 1) + rel_path;
}

// ---------------------------------------------------------------------------
// Sequential FST reader with ordinal seeking.  Only the fixed 64-byte record
// stream is exposed; sparse companion tables appended after the records are
// intentionally ignored.
// ---------------------------------------------------------------------------
inline uint64_t semantic_record_hash(uint64_t hash, const FstRecord &record, uint64_t code_address,
                                     uint32_t code_size, const std::vector<uint32_t> &extra) {
    auto append = [&](const void *data, size_t bytes) {
        auto p = static_cast<const unsigned char *>(data);
        for (size_t i = 0; i < bytes; i++)
            hash = (hash ^ p[i]) * 1099511628211ull;
    };
    append(&record, sizeof(record));
    append(&code_address, 8);
    append(&code_size, 4);
    uint32_t count = extra.size();
    append(&count, 4);
    if (count)
        append(extra.data(), count * 4);
    return hash;
}

// Functional companions are validated against the unmodified hot records.
class FunctionalCompanions {
    struct MemoryHeader {
        char magic[8];
        uint32_t version, header, core, flags;
        uint64_t records, rows, hash;
        uint32_t row_size, reserved;
    };
    struct MemoryRow {
        uint64_t ordinal;
        uint32_t hash, micro_pc;
        uint8_t role;
        uint8_t reserved[7];
    };
    struct DependencyHeader {
        char magic[8];
        uint32_t version, header, core, flags;
        uint64_t records, rows, extras;
    };
    struct DependencyRow {
        uint64_t ordinal;
        uint32_t extras, hash;
    };
    std::map<uint64_t, MemoryRow> roles;
    struct Deps {
        uint32_t hash;
        std::vector<uint32_t> values;
    };
    std::map<uint64_t, Deps> dependencies;
    bool has_roles = false, complete_dependencies = false;
    gzFile frontend = nullptr;
    uint64_t frontend_end = 0, frontend_begin = 0, frontend_pc = 0, frontend_asid = 0,
             frontend_rows = 0;
    unsigned frontend_size = 0;
    std::map<std::pair<uint64_t, uint64_t>, unsigned> lengths;
    uint64_t unknown_code = 0;
    static uint64_t env(const char *key, uint64_t fallback) {
        auto s = std::getenv(key);
        return s ? std::stoull(s) : fallback;
    }
    void frontend_row() {
        auto prior = frontend_end;
        unsigned char tag;
        if (gzread(frontend, &tag, 1) != 1 || tag != 1)
            throw std::runtime_error("incomplete frontend macro coverage");
        char b[80];
        if (gzread(frontend, b, 80) != 80)
            throw std::runtime_error("truncated frontend macro");
        std::memcpy(&frontend_begin, b, 8);
        std::memcpy(&frontend_end, b + 8, 8);
        std::memcpy(&frontend_pc, b + 16, 8);
        std::memcpy(&frontend_asid, b + 24, 8);
        frontend_size = static_cast<unsigned char>(b[56]);
        uint32_t observed, reserved, flags, known;
        std::memcpy(&flags, b + 48, 4);
        std::memcpy(&known, b + 52, 4);
        std::memcpy(&observed, b + 72, 4);
        std::memcpy(&reserved, b + 76, 4);
        if (frontend_begin != prior || frontend_end <= frontend_begin || frontend_size > 15 ||
            observed != frontend_end - frontend_begin || reserved || (flags & ~799u) ||
            (flags & ~known) || (known & ~1023u))
            throw std::runtime_error("invalid frontend macro descriptor");
        ++frontend_rows;
    }

    uint64_t expected_hash = 0, stream_hash = 14695981039346656037ull;

  public:
    ~FunctionalCompanions() {
        if (frontend)
            gzclose(frontend);
    }
    unsigned code_size(uint64_t pc) const {
        if (frontend && frontend_pc == pc && frontend_size)
            return frontend_size;
        auto i = lengths.find({frontend_asid, pc});
        return i == lengths.end() ? 0 : i->second;
    }
    uint64_t missing_code() const { return unknown_code; }
    uint64_t code_address(uint64_t pc) const {
        auto mix = [](uint64_t v) {
            v += 0x9e3779b97f4a7c15ull;
            v = (v ^ (v >> 30)) * 0xbf58476d1ce4e5b9ull;
            v = (v ^ (v >> 27)) * 0x94d049bb133111ebull;
            return v ^ (v >> 31);
        };
        auto seed = env("FST_CODE_SEED", 1), page_bits = env("FST_CODE_PAGE_BITS", 12),
             physical_bits = env("FST_CODE_PHYS_BITS", 48),
             dram = env("FST_CODE_DRAM_BYTES", 34359738368ull);
        if (page_bits >= physical_bits || physical_bits > 63 || page_bits >= 63 ||
            physical_bits - page_bits < 2)
            throw std::runtime_error("invalid code address geometry");
        uint64_t page_bytes = 1ull << page_bits, first = 1ull << (physical_bits - page_bits - 1),
                 pages = std::min(first, dram / page_bytes);
        if (!pages)
            throw std::runtime_error("empty code address range");
        uint64_t ns = mix(frontend_asid ^ mix(seed));
        uint64_t rotated = (ns << 23) | (ns >> (64 - 23));
        uint64_t h = mix((pc >> page_bits) ^ rotated ^ mix(seed ^ 0xd1b54a32d192ed03ull));
        uint64_t offset = (h % pages) * page_bytes + (pc & (page_bytes - 1));
        if (env("FST_CODE_SPLIT_DRAM", 1) && offset >= (3ull << 30))
            offset += (1ull << 30);
        return (first << page_bits) | offset;
    }
    static uint32_t hot_hash(const FstRecord &r) {
        uint32_t h = 2166136261u;
        for (auto p = reinterpret_cast<const uint8_t *>(&r), e = p + 64; p != e; ++p)
            h = (h ^ *p) * 16777619u;
        return h;
    }
    void open(const std::string &path, const FstHeader &fst) {
        if (frontend) {
            gzclose(frontend);
            frontend = nullptr;
        }
        frontend_end = frontend_begin = frontend_rows = frontend_asid = 0;
        unknown_code = 0;
        lengths.clear();
        {
            std::ifstream f((path + ".imap").c_str(), std::ios::binary);
            if (f) {
                char h[48];
                f.read(h, 48);
                uint32_t version, size, core;
                uint64_t records, count;
                std::memcpy(&version, h + 8, 4);
                std::memcpy(&size, h + 16, 4);
                std::memcpy(&core, h + 20, 4);
                std::memcpy(&records, h + 24, 8);
                std::memcpy(&count, h + 32, 8);
                if (!f || version < 1 || version > 4 ||
                    size != (version == 4   ? 72
                             : version == 1 ? 32
                                            : 64) ||
                    core != fst.core_id || records != fst.record_count)
                    throw std::runtime_error("invalid instruction map");
                for (uint64_t i = 0; i < count; i++) {
                    char b[72];
                    f.read(b, size);
                    uint64_t asid = 0, pc;
                    unsigned offset = version == 4 ? 8 : 0;
                    if (offset)
                        std::memcpy(&asid, b, 8);
                    std::memcpy(&pc, b + offset, 8);
                    unsigned n = static_cast<unsigned char>(b[offset + 26]);
                    if (!f || !n || n > 15)
                        throw std::runtime_error("invalid instruction length");
                    lengths[{asid, pc}] = n;
                }
            }
        }
        if (fst.feature_flags & 2048) {
            frontend = gzopen((path + ".ffe.gz").c_str(), "rb");
            char h[32];
            if (!frontend || gzread(frontend, h, 32) != 32 || std::memcmp(h, "FSTFFE1", 7))
                throw std::runtime_error("invalid frontend companion");
        }
        complete_dependencies = (fst.feature_flags & 32) != 0;
        roles.clear();
        dependencies.clear();
        has_roles = (fst.feature_flags & 64) != 0;
        stream_hash = 14695981039346656037ull;
        if (has_roles) {
            std::ifstream f((path + ".mreq").c_str(), std::ios::binary);
            MemoryHeader h;
            f.read(reinterpret_cast<char *>(&h), sizeof(h));
            if (!f || std::memcmp(h.magic, "FSTMREQ1", 8) || h.version != 2 || h.header != 56 ||
                h.core != fst.core_id || h.records != fst.record_count || h.row_size != 24 ||
                h.rows > h.records)
                throw std::runtime_error("invalid memory-role companion");
            expected_hash = h.hash;
            uint64_t prior = 0;
            for (uint64_t i = 0; i < h.rows; i++) {
                MemoryRow r;
                f.read(reinterpret_cast<char *>(&r), sizeof(r));
                if (!f || r.ordinal >= h.records || (i && r.ordinal <= prior) || r.role > 2)
                    throw std::runtime_error("invalid memory-role row");
                roles.emplace(r.ordinal, r);
                prior = r.ordinal;
            }
            if (f.peek() != std::char_traits<char>::eof())
                throw std::runtime_error("trailing memory-role data");
        }
        if (fst.feature_flags & 32) {
            std::ifstream f((path + ".deps").c_str(), std::ios::binary);
            DependencyHeader h;
            f.read(reinterpret_cast<char *>(&h), sizeof(h));
            if (!f || std::memcmp(h.magic, "FSTDEP1", 8) || h.version != 1 || h.header != 48 ||
                h.core != fst.core_id || h.records != fst.record_count || h.rows > h.records)
                throw std::runtime_error("invalid dependency companion");
            uint64_t prior = 0, total = 0;
            for (uint64_t i = 0; i < h.rows; i++) {
                DependencyRow r;
                f.read(reinterpret_cast<char *>(&r), sizeof(r));
                if (!f || r.ordinal >= h.records || (i && r.ordinal <= prior) || !r.extras ||
                    r.extras > 12)
                    throw std::runtime_error("invalid extended dependency row");
                Deps d;
                d.hash = r.hash;
                d.values.resize(r.extras);
                f.read(reinterpret_cast<char *>(d.values.data()), r.extras * 4);
                if (!f)
                    throw std::runtime_error("truncated extended dependencies");
                dependencies.emplace(r.ordinal, std::move(d));
                total += r.extras;
                prior = r.ordinal;
            }
            if (total != h.extras || f.peek() != std::char_traits<char>::eof())
                throw std::runtime_error("dependency companion coverage mismatch");
        }
    }
    const std::vector<uint32_t> &extra(uint64_t ordinal) const {
        static const std::vector<uint32_t> empty;
        auto i = dependencies.find(ordinal);
        return i == dependencies.end() ? empty : i->second.values;
    }
    void apply(FstRecord &r, uint64_t ordinal, bool last) {
        if (frontend) {
            while (ordinal >= frontend_end)
                frontend_row();
            if (ordinal < frontend_begin || r.pc != frontend_pc)
                throw std::runtime_error("frontend macro/record identity mismatch");
        }
        if (!code_size(r.pc)) {
            ++unknown_code;
            if (!env("FST_ALLOW_INCOMPLETE_CODE", 0))
                throw std::runtime_error(
                    "instruction length unavailable; explicit incomplete-code policy required");
        }
        if (r.is_memory() && !has_roles)
            throw std::runtime_error("memory request role capability required");
        if (complete_dependencies && tracebench::validation_enabled()) {
            uint32_t prior = 0;
            unsigned count = 0;
            bool ended = false;
            for (auto dist : r.producer_dists) {
                if (!dist) {
                    ended = true;
                    continue;
                }
                if (ended || dist <= prior || dist > ordinal)
                    throw std::runtime_error("invalid inline RAW dependency");
                prior = dist;
                ++count;
            }
            const auto &extensions = extra(ordinal);
            if (!extensions.empty() && count != 4)
                throw std::runtime_error("invalid extended RAW prefix");
            for (auto dist : extensions) {
                if (dist <= prior || dist > ordinal)
                    throw std::runtime_error("invalid extended RAW dependency");
                prior = dist;
                ++count;
            }
            if (count > r.n_src)
                throw std::runtime_error("RAW source count mismatch");
        }
        uint32_t h = tracebench::validation_enabled() ? hot_hash(r) : 0;
        auto d = dependencies.find(ordinal);
        if (tracebench::validation_enabled() && d != dependencies.end() && d->second.hash != h)
            throw std::runtime_error("dependency content mismatch");
        {
            if (tracebench::validation_enabled())
                for (auto p = reinterpret_cast<const uint8_t *>(&r), e = p + 64; p != e; ++p)
                    stream_hash = (stream_hash ^ *p) * 1099511628211ull;
            if (tracebench::validation_enabled() && last && has_roles && stream_hash != expected_hash)
                throw std::runtime_error("memory-role content identity mismatch");
        }
        if (last && frontend) {
            unsigned char tag;
            uint64_t footer[3];
            if (gzread(frontend, &tag, 1) != 1 || tag != 255 ||
                gzread(frontend, footer, 24) != 24 || footer[0] != ordinal + 1 ||
                footer[1] != frontend_rows || (tracebench::validation_enabled() && footer[2] != stream_hash) || gzgetc(frontend) != -1)
                throw std::runtime_error("frontend footer/hot identity mismatch");
        }
        auto role = roles.find(ordinal);
        if (role != roles.end()) {
            if (tracebench::validation_enabled() && role->second.hash != h)
                throw std::runtime_error("memory-role row identity mismatch");
            if (role->second.role == 1)
                r.flags &= ~(kLoad | kStore | kAtomic);
            if (role->second.role == 2)
                r.n_dst = 0;
        }
    }
};

class FstReader {
  public:
    FstReader() : ordinal_(0) { std::memset(&header_, 0, sizeof(header_)); }

    void open(const std::string &path) {
        path_ = path;
        in_.close();
        in_.clear();
        in_.open(path.c_str(), std::ios::binary);
        if (!in_)
            throw std::runtime_error("cannot open FST: " + path);

        in_.read(reinterpret_cast<char *>(&header_), sizeof(header_));
        if (!in_)
            throw std::runtime_error("truncated FST header: " + path);
        static const char kMagic[8] = {'F', 'S', 'T', 'R', 'C', '0', '1', '\0'};
        if (std::memcmp(header_.magic, kMagic, 8) != 0) {
            throw std::runtime_error("bad FST magic in " + path);
        }
        if (header_.version != kTraceVersion) {
            throw std::runtime_error("unsupported FST version " + std::to_string(header_.version) +
                                     " in " + path + " (want 7)");
        }
        if (header_.header_size != kHeaderSize || header_.record_size != kRecordSize) {
            throw std::runtime_error("FST geometry mismatch in " + path);
        }
        if (!(header_.feature_flags & 32))
            throw std::runtime_error("complete RAW dependencies required");
        companions_.open(path, header_);
        ordinal_ = 0;
        in_.seekg(0, std::ios::end);
        auto file_size = in_.tellg();
        if (header_.record_count > (UINT64_MAX - 72) / 64 || file_size < 0)
            throw std::runtime_error("FST file/header size mismatch");
        const uint64_t records_end = 72 + header_.record_count * 64;
        const bool syscall_table = header_.feature_flags & 8;
        uint64_t expected_end = records_end;
        if (syscall_table) {
            const uint64_t count = header_.reserved[1];
            if (!count || header_.reserved[0] != records_end || header_.reserved[2] != 128 ||
                count > (UINT64_MAX - records_end) / 128)
                throw std::runtime_error("invalid FST syscall metadata geometry");
            expected_end += count * 128;
        }
        if (uint64_t(file_size) != expected_end)
            throw std::runtime_error("FST file/header size mismatch");
        in_.seekg(72, std::ios::beg);
        semantic_hash_ = 14695981039346656037ull;
        cache_.clear();
        cache_.reserve(header_.record_count);
        for (uint64_t i = 0; i < header_.record_count; i++) {
            FstRecord r;
            in_.read(reinterpret_cast<char *>(&r), sizeof(r));
            if (!in_)
                throw std::runtime_error("truncated FST preload");
            companions_.apply(r, i, i + 1 == header_.record_count);
            // Companion hashes describe disk bytes. Normalize SPR classes only
            // after their validation, matching BinaryTraceSource's projection.
            if ((header_.feature_flags & 1024) && !r.is_syscall()) {
                const bool kernel = r.is_kernel();
                int op = r.canonical_op_class();
                if (op < 0 || op > 93)
                    throw std::runtime_error("unsupported SPR operation class");
                if (op >= 18 && op <= 20) op = 96 + op - 18;
                else if (op >= 21 && op <= 60) op -= 3;
                else if (op == 61) op = 99;
                else if (op == 62) op = 100;
                else if (op >= 63) op -= 5;
                r.op_class = kernel ? -op - kKernelOpClassBias : op;
            }
            auto length = companions_.code_size(r.pc);
            cache_.push_back({r, companions_.code_address(r.pc), length ? length : 1});
            const auto &cached = cache_.back();
            if (tracebench::validation_enabled())
                semantic_hash_ = semantic_record_hash(semantic_hash_, r, cached.code_address,
                                                      cached.code_size, companions_.extra(i));
        }
        uint64_t previous = 0;
        for (uint64_t i = 0; syscall_table && i < header_.reserved[1]; ++i) {
            // Syscall timestamps are validated metadata, never model timing.
            unsigned char row[128];
            in_.read(reinterpret_cast<char *>(row), sizeof(row));
            uint64_t ordinal, syscall_ordinal, reserved;
            std::memcpy(&ordinal, row, 8);
            std::memcpy(&syscall_ordinal, row + 8, 8);
            std::memcpy(&reserved, row + 120, 8);
            if (!in_ || ordinal >= header_.record_count || syscall_ordinal != i ||
                (i && ordinal <= previous) || reserved || (row[119] & ~3u))
                throw std::runtime_error("invalid FST syscall metadata row");
            previous = ordinal;
        }
        if (in_.peek() != std::char_traits<char>::eof())
            throw std::runtime_error("trailing FST data");
        in_.close();
    }

    const FstHeader &header() const { return header_; }
    const std::string &path() const { return path_; }
    uint64_t record_count() const { return header_.record_count; }
    uint64_t ordinal() const { return ordinal_; }

    // Read the next record. Returns false at end of the record stream.
    bool next(FstRecord &out) {
        if (ordinal_ >= header_.record_count)
            return false;
        out = cache_.at(ordinal_).record;
        ++ordinal_;
        return true;
    }

    // Move to absolute record ordinal (0-based).
    uint64_t semantic_hash() const { return semantic_hash_; }
    uint64_t modeled_code_address(uint64_t pc) const {
        if (!ordinal_ || pc != cache_.at(ordinal_ - 1).record.pc)
            throw std::runtime_error("code lookup outside current macro");
        return cache_[ordinal_ - 1].code_address;
    }
    unsigned instruction_size(uint64_t pc) const {
        if (!ordinal_ || pc != cache_.at(ordinal_ - 1).record.pc)
            throw std::runtime_error("instruction size outside current macro");
        return cache_[ordinal_ - 1].code_size;
    }
    uint64_t unknown_code_records() const { return companions_.missing_code(); }
    const std::vector<uint32_t> &current_dependency_extensions() const {
        return companions_.extra(ordinal_ - 1);
    }
    void seek(uint64_t ordinal) {
        if (ordinal > header_.record_count)
            throw std::runtime_error("seek past end of FST");
        ordinal_ = ordinal;
    }

  private:
    struct CachedRecord {
        FstRecord record;
        uint64_t code_address;
        unsigned code_size;
    };
    uint64_t semantic_hash_ = 14695981039346656037ull;
    std::vector<CachedRecord> cache_;
    FunctionalCompanions companions_;
    std::string path_;
    std::ifstream in_;
    FstHeader header_;
    uint64_t ordinal_;
};

} // namespace fstv7

#endif // FSTV7_FST_V7_HPP_
