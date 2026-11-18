#pragma once
#include "fastsim/config.hpp"
#include "fastsim/trace.hpp"
#include "fst_v7.hpp"
#include "tracebench_trust.hpp"
#include "ooo_core.h"
#include <limits>
#include <map>
extern fastsim::SimulatorConfig codeConfig;
extern bool allowIncompleteCode;
struct NativeReader {
    fastsim::BinaryTraceSource source;
    uint64_t seq = 0, physical_pc = 0;
    uint64_t semantic_hash = 14695981039346656037ull;
    unsigned code_size = 0;
    uint64_t unknown_code = 0;
    std::vector<uint32_t> extra;
    explicit NativeReader(const std::string &path) : source(path) {
        if (!source.complete_dependencies())
            throw std::runtime_error("complete dependencies required");
    }
    bool next(fstv7::FstRecord &r) {
        fastsim::TraceRecord v;
        if (!source.next(v))
            return false;
        ++seq;
        static_assert(sizeof(v) == sizeof(r));
        std::memcpy(&r, &v, sizeof(r));
        const auto *code = source.static_instruction(v.pc);
        const auto *page = source.instruction_page_mapping(v.pc);
        const auto *frontend = source.current_frontend_macro();
        if (!code && (!frontend || !frontend->instruction_size) && !allowIncompleteCode)
            throw std::runtime_error("instruction length unavailable at ordinal " +
                                     std::to_string(seq) + " PC " + std::to_string(v.pc));
        if (!page && codeConfig.instruction_address_mode != "modeled")
            throw std::runtime_error("physical instruction translation missing");
        code_size = code ? code->size : (frontend ? frontend->instruction_size : 0);
        if (!code_size) {
            code_size = 1;
            ++unknown_code;
        }
        physical_pc = codeConfig.instruction_address_mode == "modeled"
                          ? fastsim::modeled_instruction_physical_address(
                                codeConfig, source.current_address_space_id(), v.pc)
                          : (page->physical_page << 12) | (v.pc & 4095);
        extra = source.current_dependency_extensions();
        if (extra.size() > 12)
            throw std::runtime_error("more than 16 producing dependencies");
        if (v.is_memory()) {
            auto role = source.current_memory_request_role();
            if (!role)
                throw std::runtime_error("memory request role unavailable");
            if (*role == fastsim::fst::MemoryRequestRole::NoAccess)
                r.flags &= ~(fstv7::kLoad | fstv7::kStore | fstv7::kAtomic);
            // Native zsim decoder represents software prefetch as a load without a result register.
            if (*role == fastsim::fst::MemoryRequestRole::SoftwarePrefetch)
                r.n_dst = 0;
        }
        if (r.canonical_op_class() < -1 || r.canonical_op_class() > 100)
            throw std::runtime_error("unsupported operation class");
        if (tracebench::validation_enabled())
            semantic_hash =
                fstv7::semantic_record_hash(semantic_hash, r, physical_pc, code_size, extra);
        if (r.is_branch() && r.is_conditional()) {
            auto physical = [&](uint64_t pc) {
                if (codeConfig.instruction_address_mode == "modeled")
                    return fastsim::modeled_instruction_physical_address(
                        codeConfig, source.current_address_space_id(), pc);
                auto mapping = source.instruction_page_mapping(pc);
                if (!mapping)
                    throw std::runtime_error("missing functional branch-target translation");
                return (mapping->physical_page << 12) | (pc & 4095);
            };
            r.target = physical(r.target ? r.target : (code ? code->direct_target : 0));
            r.next_pc = physical(v.pc + code_size);
        }
        return true;
    }
};
inline DynUop translateUop(const fstv7::FstRecord &r, uint64_t seq,
                           const std::vector<uint32_t> &extra = {}) {
    DynUop u = {};
    u.lat = 1;
    u.portMask = 0x23;
    u.type = UOP_GENERAL;
    for (unsigned k = 0; k < 4; k++)
        if (r.producer_dists[k])
            u.rs[k] = r.producer_dists[k] < seq ? seq - r.producer_dists[k] : 0;
    for (unsigned k = 0; k < extra.size(); k++)
        if (extra[k])
            u.rs[k + 4] = extra[k] < seq ? seq - extra[k] : 0;
    u.rd[0] = seq;
    int op = r.canonical_op_class();
    if (r.is_memory()) {
        u.type = r.is_write() ? UOP_STORE : UOP_LOAD;
        u.portMask = r.is_write() ? 0x10 : 0x04;
    } else if (op == 2) {
        u.lat = 3;
        u.portMask = 2;
    } else if (op == 3) {
        u.lat = 20;
        u.portMask = 1;
        u.extraSlots = 19;
    } else if (op == 7) {
        u.lat = 5;
        u.portMask = 1;
    } else if (op == 8) {
        u.lat = 5;
        u.portMask = 1;
    } else if (op == 9) {
        u.lat = 14;
        u.portMask = 1;
        u.extraSlots = 13;
    } else if (op == 11) {
        u.lat = 24;
        u.portMask = 1;
        u.extraSlots = 23;
    } else if (op >= 4 && op <= 55) {
        u.lat = 3;
        u.portMask = 3;
    } else if (op == 99) {
        u.type = UOP_STORE_ADDR;
        u.portMask = 4;
    }
    if (fstv7::has_flag(r.flags, fstv7::kSerialize))
        u.traceSerialize = true;
    return u;
}

struct NormalizedRecord {
    fstv7::FstRecord r;
    DynUop u;
    uint64_t codepc;
    unsigned bytes;
};
struct NativeInput {
    fstv7::ManifestEntry entry;
    std::string path;
    uint64_t records = 0, macros = 0, unknown_code_records = 0, user_uops = 0,
             external_dependencies = 0;
    uint64_t semantic_hash = 0;
    std::vector<NormalizedRecord> normalized;
    void preflight(const std::string &manifest) {
        path = fstv7::resolve_fst_path(manifest, entry.rel_path);
        NativeReader reader(path);
        fstv7::FstRecord r;
        normalized.reserve(entry.warmup_records + entry.take_records);
        while (reader.next(r)) {
            ++records;
            if (records >= UINT32_MAX)
                throw std::runtime_error("native operand index capacity exceeded");
            normalized.push_back(
                {r, translateUop(r, records, reader.extra), reader.physical_pc, reader.code_size});
            if (records > entry.warmup_records) {
                if (tracebench::validation_enabled()) {
                    auto deps = reader.extra;
                    deps.insert(deps.end(), r.producer_dists, r.producer_dists + 4);
                    for (auto dist : deps)
                        if (dist && dist >= records)
                            ++external_dependencies;
                }
                if (r.completes_macro())
                    ++macros;
                if (!r.is_kernel())
                    ++user_uops;
            }
        }
        unknown_code_records = reader.unknown_code;
        semantic_hash = reader.semantic_hash;
        if (records != entry.warmup_records + entry.take_records ||
            macros != entry.take_instructions)
            throw std::runtime_error("FST population mismatch");
    }
};
