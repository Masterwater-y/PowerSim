/* Oracle-only architectural retirement accounting. No timing/FST input. */
#ifndef __CPU_O3_PROBE_PERF_RETIREMENT_HH__
#define __CPU_O3_PROBE_PERF_RETIREMENT_HH__
#define TAOTRACE_RETIREMENT_ROM_BOUNDARIES 1
#define TAOTRACE_RETIREMENT_INDEPENDENT_EVENTS 1

#include <cstdint>
#include <fstream>
#include <initializer_list>
#include <map>
#include <memory>
#include <ostream>
#include <set>
#include <string>
#include <stdexcept>
#include <utility>
#include <vector>

namespace gem5 { namespace o3 { namespace retirement {

enum Source : unsigned {
    Unknown=1, L1=2, Miss=4, FillBuffer=8, Forwarded=16,
    L2Hit=32, L2Miss=64, L3Hit=128, L3Miss=256, L2Unknown=512, L3Unknown=1024
};

inline bool knownRomEntry(uint64_t micro_pc, uint64_t rom_bit,
                          std::initializer_list<uint64_t> entries)
{
    if (!(micro_pc & rom_bit)) return false;
    const auto offset=micro_pc & ~rom_bit;
    for (const auto entry : entries) if (entry==offset) return true;
    return false;
}

struct NativeMemberDiagnostic {
    uint64_t sequence=0, pc=0, micro_pc=0, next_micro_pc=0, macroop_identity=0;
    bool first=false, last=false, micro=false, macroop_present=false;
    std::string name, macroop_name, disasm;
};

inline void writeJsonString(std::ostream &out, const std::string &value)
{
    static constexpr const char digits[]="0123456789abcdef";
    out << '"';
    for (unsigned char c : value) {
        if (c=='"' || c=='\\') out << '\\' << c;
        else if (c<32) out << "\\u00" << digits[c>>4] << digits[c&15];
        else out << c;
    }
    out << '"';
}

struct DebugPrefixMember {
    bool micro=false, first=false, debug_control=false;
};

inline bool debugControlName(const std::string &name)
{
    return name=="warn" || name=="warn_once" || name=="hack" ||
           name=="hack_once" || name=="inform" || name=="inform_once";
}

// Only simulator debug controls may be absent before the first committed
// member. Never infer a start from an empty ledger or a mnemonic alone.
template<class Fetch>
inline bool debugOnlyPrefix(uint64_t micro_pc, Fetch fetch)
{
    if (!micro_pc || micro_pc>64) return false;
    for (uint64_t i=0;i<micro_pc;++i) {
        const auto prefix=fetch(i);
        if (!prefix.micro || !prefix.debug_control || (i==0 && !prefix.first))
            return false;
    }
    return true;
}

// Complete sparse functional facts; bounded human-readable samples elsewhere
// are diagnostics only. No clocks, cache outcomes or PMU values are emitted.
class BoundaryStream {
    std::ofstream out_;
    std::string directory_;
    unsigned core_=0;
    uint64_t count_=0, previous_=0;
  public:
    bool opened() const { return out_.is_open(); }
    void open(const std::string &directory,unsigned core) {
        if (opened()) return;
        directory_=directory;core_=core;
        out_.open(directory+"/macro-boundary-core"+std::to_string(core)+".jsonl");
        if (!out_) throw std::runtime_error("cannot open functional macro boundaries");
    }
    void record(uint64_t ordinal,uint32_t context,uint64_t pc,bool starts,
                bool rom,bool debug,bool had_open) {
        if (!opened() || (count_ && ordinal<=previous_))
            throw std::runtime_error("unordered functional macro boundary");
        out_ << "{\"schema\":\"fastsim-functional-macro-boundary-v1\",\"core_id\":" << core_
             << ",\"record_ordinal\":" << ordinal << ",\"context_id\":" << context
             << ",\"pc\":" << pc << ",\"starts_macro\":" << (starts?"true":"false")
             << ",\"rom_entry\":" << (rom?"true":"false")
             << ",\"debug_prefix\":" << (debug?"true":"false")
             << ",\"had_open_macro\":" << (had_open?"true":"false") << "}\n";
        if (!out_) throw std::runtime_error("failed functional macro boundary write");
        previous_=ordinal;++count_;
    }
    void finish(const std::string &source,uint64_t records,uint64_t begin,bool complete) {
        if (!opened()) throw std::runtime_error("functional macro boundary stream missing");
        out_.flush();
        if (!out_) throw std::runtime_error("failed functional macro boundary flush");
        out_.close();
        std::ofstream meta(directory_+"/macro-boundary-metadata-core"+std::to_string(core_)+".json");
        meta << "{\"schema\":\"fastsim-functional-macro-boundary-metadata-v1\",\"core_id\":" << core_
             << ",\"source_file\":";writeJsonString(meta,source);
        meta << ",\"source_record_count\":" << records << ",\"measurement_begin_ordinal\":" << begin
             << ",\"event_count\":" << count_ << ",\"dropped_events\":0,\"complete\":"
             << (complete?"true":"false")
             << ",\"incomplete_scope\":\"measured-functional-members-v2\","
                "\"event_file\":\"macro-boundary-core" << core_ << ".jsonl\"}\n";
        meta.close();
        if (!meta) throw std::runtime_error("failed functional macro boundary metadata write");
    }
};

inline void writeNativeMemberDiagnostic(std::ostream &out,
                                         const NativeMemberDiagnostic &m)
{
    out << "{\"inst_seq_num\":" << m.sequence << ",\"pc\":" << m.pc
        << ",\"micro_pc\":" << m.micro_pc << ",\"next_micro_pc\":" << m.next_micro_pc
        << ",\"first_micro\":" << (m.first ? "true" : "false")
        << ",\"last_micro\":" << (m.last ? "true" : "false")
        << ",\"is_microop\":" << (m.micro ? "true" : "false")
        << ",\"macroop_present\":" << (m.macroop_present ? "true" : "false")
        << ",\"macroop_identity\":" << m.macroop_identity << ",\"name\":";
    writeJsonString(out,m.name);
    out << ",\"macroop_name\":"; writeJsonString(out,m.macroop_name);
    out << ",\"disasm\":"; writeJsonString(out,m.disasm);
    out << '}';
}

// Exact independent registry facts, never the functional path-class or MSHR
// predictor. A terminal reason mask does not identify partial forwarding bytes.
struct NativeFacts {
    bool resolved=false, store_forward=false, other_terminal=false;
    uint64_t admissions=0, responses=0, coalesced=0, hierarchy_requests=0;
    uint64_t external_hits=0, external_misses=0;
    uint64_t accesses=0, hits=0, tag_misses=0;
    uint64_t permission_upgrades=0, merged_misses=0, remote_supplies=0;
    uint64_t l2_accesses=0,l2_hits=0,l2_tag_misses=0,l2_other=0;
    uint64_t l3_accesses=0,l3_hits=0,l3_tag_misses=0,l3_other=0;
    uint64_t l3_merged_misses=0,l3_remote_supplies=0,terminal_reason_mask=0;
    uint64_t l2_permission_upgrades=0,l3_permission_upgrades=0;
};

inline unsigned classify(const NativeFacts &n)
{
    const unsigned forwarding=n.store_forward ? unsigned(Forwarded) : 0u;
    if (!n.resolved || n.other_terminal || n.store_forward ||
        !n.responses || n.responses!=n.admissions ||
        n.responses!=n.external_hits+n.external_misses ||
        n.responses!=n.hierarchy_requests+n.coalesced ||
        n.accesses!=n.hierarchy_requests ||
        n.accesses!=n.hits+n.tag_misses || n.permission_upgrades ||
        n.merged_misses || n.remote_supplies ||
        n.external_hits<n.hits || n.external_misses<n.tag_misses)
        return Unknown|forwarding;
    // A coalesced response can share a resident L1 parent. Only followers of
    // a native controller miss are fill followers. This is a service mapping,
    // not certification of a hardware FB_HIT event.
    unsigned source=0;
    if (n.hits || n.external_hits>n.hits) source|=L1;
    if (n.tag_misses) source|=Miss;
    if (n.external_misses>n.tag_misses) source|=FillBuffer;
    return source ? source : Unknown;
}

// Full-address forwarding is recorded only by LSQUnit's full coverage path.
// Older issued/discarded packets remain request events but do not describe the
// data which completed this retired load. Hardware event mapping stays unverified.
inline unsigned classifyIndependent(const NativeFacts &n)
{
    // LSQ emits StoreForward only after full address/size coverage. The data
    // is delivered locally; earlier discarded Ruby requests are not this
    // retired load's effective service. Keep the explicit forwarding label,
    // with known negative membership in all three retired miss events.
    if (n.resolved && n.store_forward && !n.other_terminal) return L1|Forwarded;
    // An S->M ownership upgrade has resident data. It is a permission miss,
    // not a retired-load data miss, even if the callback is externally marked
    // miss and ownership traffic continues through lower levels.
    if (n.resolved && !n.other_terminal && n.permission_upgrades && !n.tag_misses &&
        n.responses && n.responses==n.admissions &&
        n.responses==n.external_hits+n.external_misses &&
        n.responses==n.hierarchy_requests+n.coalesced &&
        n.accesses==n.hierarchy_requests && n.accesses==n.hits+n.permission_upgrades &&
        !n.merged_misses && !n.remote_supplies && n.external_hits>=n.hits &&
        n.external_misses>=n.permission_upgrades) {
        unsigned source=L1;
        if (n.external_misses>n.permission_upgrades) source|=FillBuffer;
        return source;
    }
    return classify(n);
}

// SPR retired-load service membership. Native Sequencer followers are FB-path
// loads, not copies of the initiating request. A directly observed fragment
// can still contribute its own lower-level miss in a mixed split load.
inline unsigned classifyRetiredCache(const NativeFacts &n) {
    if (n.resolved && n.store_forward && !n.other_terminal) return 0;
    if (!n.resolved || n.other_terminal ||
        !n.responses || n.responses!=n.admissions)
        return L2Unknown|L3Unknown;
    const auto upper=classifyIndependent(n);
    // Complete L1 hits and coalesced-only followers have no lower-level
    // lookup for this load. No inherited parent miss is generated.
    if (!(upper&Unknown) && !(upper&Miss)) return 0;
    unsigned result=0;
    if (n.l2_hits || n.l2_permission_upgrades) result|=L2Hit;
    if (n.l2_tag_misses) result|=L2Miss;
    const bool l2_unknown=n.l2_accesses!=n.tag_misses ||
        n.l2_other!=n.l2_permission_upgrades ||
        n.l2_accesses!=n.l2_hits+n.l2_tag_misses+n.l2_permission_upgrades;
    if (l2_unknown)
        result|=L2Unknown;
    // Complete private-L2 resident data needs no LLC data event. Its lower
    // ownership requests remain independently counted request diagnostics.
    if (!l2_unknown && !n.l2_tag_misses) return result;
    // Mixed L2 data/ownership paths need fragment attribution below L2.
    // An aggregate positive LLC bit is not proven: OR-ing it with Unknown
    // would incorrectly make sourceCounts certify that positive membership.
    if (n.l2_permission_upgrades && n.l2_tag_misses) return result|L3Unknown;
    // MT remote-owner service is a shared LLC snoop/HitM hit, independently
    // of the physical data supplier. IS/ISS merges have no resident LLC data
    // and retain the miss for this originating core's direct request.
    if (n.l3_hits || n.l3_remote_supplies || n.l3_permission_upgrades) result|=L3Hit;
    if (n.l3_tag_misses || n.l3_merged_misses) result|=L3Miss;
    if (l2_unknown || n.l3_accesses!=n.l2_tag_misses ||
        n.l3_other!=n.l3_merged_misses+n.l3_remote_supplies+n.l3_permission_upgrades ||
        n.l3_accesses!=n.l3_hits+n.l3_tag_misses+n.l3_merged_misses+n.l3_remote_supplies+n.l3_permission_upgrades)
        result|=L3Unknown;
    return result;
}

#define TAOTRACE_RETIREMENT_COUNTS(X) \
    X(retired_macro_instructions) X(retired_uops) X(native_retired_uops) \
    X(perf_retired_loads) X(perf_retired_stores) X(perf_l1d_load_hits) \
    X(perf_l1d_load_misses) X(perf_l1d_load_fb_hits) \
    X(independent_l1_hits) X(independent_l1_misses) X(independent_fb_hits) \
    X(independent_l2_hits) X(independent_l2_misses) X(independent_l3_hits) X(independent_l3_misses)
#define TAOTRACE_RETIREMENT_COVERAGE(X) \
    X(classified_loads) X(unclassified_loads) X(incomplete_macros) \
    X(incomplete_uops) X(mixed_source_loads) X(forwarded_loads) \
    X(scope_crossing_macros) X(warmup_spanning_macros) \
    X(leading_incomplete_macros) X(unresolved_source_uops) X(unsupported_loads) \
    X(rom_entry_macros) X(rom_microcode_macros) X(boundary_discontinuities) \
    X(excluded_hardware_entry_macros) X(precise_fault_abandoned_macros) \
    X(independent_l1_hits_unknown) X(independent_l1_misses_unknown) \
    X(independent_fb_hits_unknown) X(independent_retired_loads_unknown) \
    X(independent_retired_stores_unknown) \
    X(all_history_incomplete_macros) X(all_history_incomplete_uops) \
    X(independent_l2_hits_unknown) X(independent_l2_misses_unknown) \
    X(independent_l3_hits_unknown) X(independent_l3_misses_unknown)

struct Counts {
#define DECLARE(name) uint64_t name=0;
    TAOTRACE_RETIREMENT_COUNTS(DECLARE)
    TAOTRACE_RETIREMENT_COVERAGE(DECLARE)
#undef DECLARE
    Counts &operator+=(const Counts &other) {
#define ADD(name) name+=other.name;
        TAOTRACE_RETIREMENT_COUNTS(ADD)
        TAOTRACE_RETIREMENT_COVERAGE(ADD)
#undef ADD
        return *this;
    }
};

struct Scopes {
    Counts physical, user, kernel, workload_task, other_task;
};

inline void writeCounts(std::ostream &out, const Counts &c, bool available)
{
    out << "{\"available\":" << (available ? "true" : "false");
#define WRITE(name) out << ",\"" #name "\":"; \
    if (available) out << c.name; else out << "null";
    TAOTRACE_RETIREMENT_COUNTS(WRITE)
    out << ",\"coverage\":{\"unit_contract\":\"native-ISA-membership-not-hardware-certification\"";
    TAOTRACE_RETIREMENT_COVERAGE(WRITE)
#undef WRITE
    out << "}}";
}

#undef TAOTRACE_RETIREMENT_COUNTS
#undef TAOTRACE_RETIREMENT_COVERAGE

struct Member {
    uint32_t context=0;
    uint64_t sequence=0, owner=0, pc=0;
    bool first=true, last=true, read=false, write=false, user=true;
    bool measured=false, syscall=false, unsupported=false, software_prefetch=false;
    bool rom=false, rom_entry=false, hardware_entry=false;
};

class Ledger {
    struct Macro {
        uint64_t owner=0, pc=0, uops=0, unresolved=0, measured_functional_uops=0;
        bool user=true, read=false, write=false, warmup=false;
        bool leading=false, crossing=false, unsupported=false;
        bool rom_entry=false, rom=false, measured=false, hardware_entry=false, precise_fault=false;
        unsigned source=0;
        unsigned independent_source=0;
        std::vector<std::pair<uint32_t,uint64_t>> identities;
    };
    using Identity=std::pair<uint32_t,uint64_t>;
    std::map<uint32_t,std::shared_ptr<Macro>> open_;
    std::map<Identity,std::shared_ptr<Macro>> waiting_;
    std::vector<std::shared_ptr<Macro>> completed_loads_;
    std::vector<std::shared_ptr<Macro>> abandoned_;
    std::map<uint64_t,Scopes> owners_;
    bool last_hardware_entry_=false,last_precise_fault_abandonment_=false;
    struct NativeSample {
        uint32_t context=0; uint64_t sequence=0,owner=0,pc=0;
        unsigned membership=0; NativeFacts facts;
    };
    std::map<unsigned,uint64_t> native_unknown_masks_;
    std::vector<NativeSample> native_unknown_samples_;
    static void add(Scopes &s, const Counts &c, bool user) {
        s.physical+=c;
        (user ? s.user : s.kernel)+=c;
    }
    static Counts sourceCounts(const Macro &m) {
        Counts c;
        const unsigned classes=m.source&(L1|Miss|FillBuffer);
        c.forwarded_loads=bool(m.source&Forwarded);
        c.mixed_source_loads=classes && (classes&(classes-1));
        c.unresolved_source_uops=m.unresolved;
        c.unsupported_loads=m.unsupported;
        const bool boundary_unknown=m.unsupported || m.leading || m.crossing;
        const bool unknown=boundary_unknown || m.unresolved ||
            (m.independent_source&Unknown);
        // Positive membership can be proven even when another member's source
        // is unresolved. Each event keeps its own unknown population.
        c.independent_l1_hits=!boundary_unknown &&
            bool(m.independent_source&L1);
        c.independent_l1_misses=!boundary_unknown && bool(m.independent_source&Miss);
        c.independent_fb_hits=!boundary_unknown && bool(m.independent_source&FillBuffer);
        c.independent_l1_hits_unknown=unknown && !c.independent_l1_hits;
        c.independent_l1_misses_unknown=unknown && !c.independent_l1_misses;
        c.independent_fb_hits_unknown=unknown && !c.independent_fb_hits;
        c.independent_l2_hits=!boundary_unknown && bool(m.independent_source&L2Hit);
        c.independent_l2_hits_unknown=(boundary_unknown || m.unresolved || (m.independent_source&L2Unknown)) && !c.independent_l2_hits;
        c.independent_l2_misses=!boundary_unknown && bool(m.independent_source&L2Miss);
        c.independent_l2_misses_unknown=(boundary_unknown || m.unresolved || (m.independent_source&L2Unknown)) && !c.independent_l2_misses;
        c.independent_l3_hits=!boundary_unknown && bool(m.independent_source&L3Hit);
        c.independent_l3_hits_unknown=(boundary_unknown || m.unresolved || (m.independent_source&L3Unknown)) && !c.independent_l3_hits;
        c.independent_l3_misses=!boundary_unknown && bool(m.independent_source&L3Miss);
        c.independent_l3_misses_unknown=(boundary_unknown || m.unresolved || (m.independent_source&L3Unknown)) && !c.independent_l3_misses;
        if (m.unresolved || (m.source&Unknown) || !classes ||
            m.unsupported || m.leading) c.unclassified_loads=1;
        else {
            c.classified_loads=1;
            if (m.source&Miss) c.perf_l1d_load_misses=1;
            else if (m.source&FillBuffer) c.perf_l1d_load_fb_hits=1;
            else c.perf_l1d_load_hits=1;
        }
        return c;
    }
  public:
    bool hasOpen(uint32_t context) const {
        const auto found=open_.find(context);
        return found!=open_.end() && bool(found->second);
    }
    bool lastHardwareEntry() const { return last_hardware_entry_; }
    bool lastPreciseFaultAbandonment() const { return last_precise_fault_abandonment_; }
    bool notePreciseFault(uint32_t context,uint64_t pc) {
        const auto found=open_.find(context);
        if (found==open_.end() || !found->second || found->second->pc!=pc) return false;
        found->second->precise_fault=true;return true;
    }
    void commit(const Member &member) {
        last_precise_fault_abandonment_=false;
        auto &pending=open_[member.context];
        if ((member.first || member.rom_entry) && pending) {
            // Only an independently accepted precise fault at the same PC
            // proves why this incomplete prefix did not retire. Other starts
            // still fail the discontinuity gate, including unproven IRQ gaps.
            const bool precise=pending->precise_fault && member.rom_entry && member.hardware_entry;
            last_precise_fault_abandonment_=precise;
            if (member.measured) {
                Counts c;
                if (precise) {
                    c.precise_fault_abandoned_macros=bool(pending->measured_functional_uops);
                    add(owners_[pending->owner],c,pending->user);
                } else {
                    c.boundary_discontinuities=1;add(owners_[member.owner],c,member.user);
                }
            }
            abandoned_.push_back(pending);
            pending.reset();
        }
        if (!pending) {
            pending=std::make_shared<Macro>();
            pending->leading=!member.first && !member.rom_entry;
            pending->rom_entry=member.rom_entry;
            pending->hardware_entry=member.rom_entry && member.rom && member.hardware_entry;
            pending->owner=member.owner; pending->pc=member.pc; pending->user=member.user;
        }
        auto &m=*pending;
        last_hardware_entry_=m.hardware_entry;
        m.crossing|=m.owner!=member.owner || m.user!=member.user;
        m.owner=member.owner; m.user=member.user;
        ++m.uops;
        if (member.measured && (!member.syscall || member.first))
            ++m.measured_functional_uops;
        m.warmup|=!member.measured;
        m.measured|=member.measured;
        m.read|=!m.hardware_entry && (member.read || member.software_prefetch);
        m.write|=!m.hardware_entry && member.write;
        // SPR counts retired software prefetch hints in ALL_LOADS and L1_HIT,
        // independently of their asynchronous cache-fill request. They have
        // known negative membership in the three retired-load miss events.
        if (member.software_prefetch) {
            m.source|=L1;
            m.independent_source|=L1;
        }
        m.unsupported|=!m.hardware_entry && (member.unsupported || member.rom);
        m.rom|=member.rom;
        if (member.read && !m.hardware_entry) {
            ++m.unresolved;
            const Identity identity{member.context,member.sequence};
            waiting_[identity]=pending;
            m.identities.push_back(identity);
        }
        if (member.measured) {
            Counts c;
            c.native_retired_uops=1;
            c.retired_uops=!member.syscall || member.first;
            add(owners_[member.owner],c,member.user);
        }
        if (!member.last) return;
        if (member.measured) {
            Counts c;
            c.retired_macro_instructions=1;
            c.perf_retired_loads=m.read; c.perf_retired_stores=m.write;
            c.scope_crossing_macros=m.crossing;
            c.warmup_spanning_macros=m.warmup;
            c.leading_incomplete_macros=m.leading;
            c.rom_entry_macros=m.rom_entry; c.rom_microcode_macros=m.rom;
            c.excluded_hardware_entry_macros=m.hardware_entry;
            c.independent_retired_loads_unknown=m.read &&
                (m.unsupported || m.leading || m.crossing);
            c.independent_retired_stores_unknown=m.write &&
                (m.unsupported || m.leading || m.crossing);
            add(owners_[m.owner],c,m.user);
            if (m.read) completed_loads_.push_back(pending);
        } else {
            // Completed warmup macros cannot contribute to a measured event.
            for (const auto &identity : m.identities) waiting_.erase(identity);
        }
        pending.reset();
    }
    void resolve(uint32_t context, uint64_t sequence, unsigned source,
                 unsigned independent_source=0) {
        const auto found=waiting_.find({context,sequence});
        if (found==waiting_.end()) return;
        found->second->source|=source ? source : Unknown;
        found->second->independent_source|=independent_source ? independent_source :
            (source ? source : Unknown);
        --found->second->unresolved;
        waiting_.erase(found);
    }
    void resolveNative(uint32_t context,uint64_t sequence,const NativeFacts &facts) {
        const unsigned membership=classifyIndependent(facts)|classifyRetiredCache(facts);
        const unsigned unknown=membership&(Unknown|L2Unknown|L3Unknown);
        const auto waiting=waiting_.find({context,sequence});
        // Diagnostic samples never become a reference fallback. Count every
        // measured unresolved-class observation; retain at most eight examples
        // per mask, so a noisy early class cannot hide another class.
        if (unknown && waiting!=waiting_.end() && waiting->second->measured) {
            const auto &macro=*waiting->second;
            const auto count=++native_unknown_masks_[unknown];
            if (count<=8) native_unknown_samples_.push_back(
                {context,sequence,macro.owner,macro.pc,membership,facts});
        }
        resolve(context,sequence,classify(facts),membership);
    }
    void writeNativeDiagnostics(std::ostream &out) const {
        out << "{\"diagnostic_only\":true,\"unit\":\"measured-read-uop-service-observation\",\"unknown_masks\":{";
        bool first=true;
        for (const auto &entry:native_unknown_masks_) {
            if (!first) out << ',';
            first=false;
            out << '\"' << entry.first << "\":" << entry.second;
        }
        out << "},\"samples\":[";first=true;
        for (const auto &sample:native_unknown_samples_) {
            if (!first) out << ',';
            first=false;
            out << "{\"context\":" << sample.context << ",\"sequence\":" << sample.sequence
                << ",\"owner\":" << sample.owner << ",\"pc\":" << sample.pc
                << ",\"membership\":" << sample.membership << ",\"facts\":{";
            const auto &f=sample.facts;
            out << "\"resolved\":" << (f.resolved?"true":"false");
#define WRITE_FACT(name) out << ",\"" #name "\":" << f.name;
            WRITE_FACT(store_forward) WRITE_FACT(other_terminal) WRITE_FACT(terminal_reason_mask)
            WRITE_FACT(admissions) WRITE_FACT(responses) WRITE_FACT(coalesced) WRITE_FACT(hierarchy_requests)
            WRITE_FACT(external_hits) WRITE_FACT(external_misses) WRITE_FACT(accesses) WRITE_FACT(hits)
            WRITE_FACT(tag_misses) WRITE_FACT(permission_upgrades) WRITE_FACT(merged_misses) WRITE_FACT(remote_supplies)
            WRITE_FACT(l2_accesses) WRITE_FACT(l2_hits) WRITE_FACT(l2_tag_misses) WRITE_FACT(l2_other)
            WRITE_FACT(l3_accesses) WRITE_FACT(l3_hits) WRITE_FACT(l3_tag_misses) WRITE_FACT(l3_other)
            WRITE_FACT(l3_merged_misses) WRITE_FACT(l3_remote_supplies)
            WRITE_FACT(l2_permission_upgrades) WRITE_FACT(l3_permission_upgrades)
#undef WRITE_FACT
            out << "}}";
        }
        out << "]}";
    }
    std::map<uint64_t,Scopes> owners() const {
        auto result=owners_;
        for (const auto &m : completed_loads_)
            add(result[m->owner],sourceCounts(*m),m->user);
        const auto tail=[&](const std::shared_ptr<Macro> &m) {
            if (!m) return;
            Counts c;
            c.all_history_incomplete_macros=1; c.all_history_incomplete_uops=m->uops;
            c.incomplete_macros=bool(m->measured_functional_uops);
            c.incomplete_uops=m->measured_functional_uops;
            c.leading_incomplete_macros=m->leading && m->measured;
            add(result[m->owner],c,m->user);
        };
        for (const auto &entry : open_) tail(entry.second);
        for (const auto &m : abandoned_) tail(m);
        return result;
    }
    Scopes snapshot(const std::set<uint64_t> &workload) const {
        Scopes result;
        for (const auto &entry : owners()) {
            result.physical+=entry.second.physical;
            result.user+=entry.second.user;
            result.kernel+=entry.second.kernel;
            if (entry.first)
                (workload.count(entry.first) ? result.workload_task : result.other_task)
                    +=entry.second.physical;
        }
        return result;
    }
};

}}} // namespace gem5::o3::retirement
#endif
