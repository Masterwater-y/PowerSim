#include "fastsim/spr_frontend.hpp"

#include <algorithm>
#include <deque>
#include <limits>
#include <stdexcept>
#include <utility>
#include <vector>

namespace fastsim {
namespace {
using Cycle = std::uint64_t;
Cycle add(Cycle a, Cycle b) {
    if (b > std::numeric_limits<Cycle>::max() - a)
        throw std::overflow_error("SPR frontend cycle/counter overflow");
    return a + b;
}
void bump(Cycle &n, Cycle by = 1) { n = add(n, by); }
bool power2(std::uint32_t n) { return n && !(n & (n - 1)); }
void require(bool ok, const char *why) { if (!ok) throw std::invalid_argument(why); }
bool same(const SprFrontendMacro &a, const SprFrontendMacro &b) {
    return a.pc == b.pc && a.address_space_id == b.address_space_id &&
        a.code_identity == b.code_identity && a.instruction_bytes == b.instruction_bytes &&
        a.uops == b.uops && a.shared_allocation == b.shared_allocation &&
        a.microcode == b.microcode && a.unconditional_branch == b.unconditional_branch &&
        a.dsb_eligible == b.dsb_eligible;
}
void validate(const SprFrontendMacro &m, const SprFrontendConfig &c) {
    require(m.code_identity != 0, "SPR frontend requires functional code identity");
    require(m.uops != 0 && ((m.pseudo && m.instruction_bytes == 0) ||
            (m.instruction_bytes >= 1 && m.instruction_bytes <= 15)),
            "SPR frontend requires nonzero functional UOPs and x86 length 1..15");
    require(!m.shared_allocation || m.uops <= c.batch_uops,
            "SPR frontend shared allocation group exceeds bounded expansion batch");
    if (m.instruction_bytes) (void)add(m.pc, m.instruction_bytes - 1);
}
}

struct SprFrontend::Impl {
    struct Entry { SprFrontendMacro macro; Cycle available, stamp; };
    struct Block { Cycle asid, address, stamp; };
    struct Set { std::vector<Entry> entries; std::vector<Block> blocked; };
    struct Queued { Cycle cycle; std::uint32_t index, cost; };
    SprFrontendConfig config;
    SprFrontendCounters stats;
    bool reusable_bytes=true;
    std::vector<Set> sets;
    Cycle stamp = 0;
    bool have_context = false;
    Cycle context = 0;
    bool byte_valid = false, byte_service_valid = false;
    Cycle byte_base = 0, byte_buffer_ready = 0, byte_cycle = 0, byte_block = 0;
    // Distinct starts overlap this bounded buffer (at most buffer_bytes+14).
    std::vector<SprFrontendMacro> byte_identities;
    bool have_source = false;
    SprFrontendSource selected = SprFrontendSource::Mite;
    Cycle batch_submit = 0;
    std::uint32_t batch_macros = 0, batch_ops = 0;
    bool service_valid = false;
    Cycle service_cycle = 0;
    std::uint32_t service_slots = 0, service_ops = 0;
    bool macro_active = false, outstanding = false;
    SprFrontendMacro macro;
    SprFrontendSource source = SprFrontendSource::Mite;
    Cycle ready = 0, source_floor = 0, rename_floor = 0, sent_cycle = 0;
    std::uint32_t produced = 0, consumed = 0, fragment_end = 0;
    std::uint32_t occupancy = 0, sent_cost = 0;
    std::deque<Queued> queue;

    explicit Impl(const SprFrontendConfig &c) : config(c), sets(c.dsb_sets) {}
    Set &set(Cycle pc) { return sets[(pc >> 6) & (config.dsb_sets - 1)]; }
    const Set &set(Cycle pc) const { return sets[(pc >> 6) & (config.dsb_sets - 1)]; }
    const Entry *find(const SprFrontendMacro &m) const {
        for (const auto &e : set(m.pc).entries) if (same(e.macro, m)) return &e;
        return nullptr;
    }
    std::uint32_t width() const {
        return source == SprFrontendSource::Mite ? config.mite_width :
            source == SprFrontendSource::Dsb ? config.dsb_width : config.ms_width;
    }
    Cycle latency() const {
        return source == SprFrontendSource::Mite ? config.mite_latency :
            source == SprFrontendSource::Dsb ? config.dsb_latency : config.ms_latency;
    }
    void invalidate() {
        for (auto &s : sets) {
            bump(stats.dsb_invalidations, s.entries.size());
            s.entries.clear(); s.blocked.clear();
        }
        byte_valid = false; byte_identities.clear();
    }
    Cycle bytes(const SprFrontendMacro &m, Cycle earliest, Cycle response) {
        Cycle result = earliest;
        const Cycle last = add(m.pc, m.instruction_bytes - 1);
        Cycle block = m.pc & ~Cycle(config.byte_service_bytes - 1);
        const Cycle last_block = last & ~Cycle(config.byte_service_bytes - 1);
        for (;;) {
            const Cycle base = block & ~Cycle(config.byte_buffer_bytes - 1);
            if (!byte_valid || byte_base != base) {
                byte_valid = true; byte_base = base; byte_identities.clear();
                byte_buffer_ready = add(response, 1);
                bump(stats.byte_buffer_fills);
            }
            bool known = false;
            for (const auto &old : byte_identities) known |= old.pc == m.pc;
            if (!known) byte_identities.push_back(m);
            Cycle at = std::max(result, byte_buffer_ready);
            if (byte_service_valid) {
                at = std::max(at, byte_cycle);
                if (at == byte_cycle && block != byte_block) at = add(at, 1);
            }
            if (!byte_service_valid || at != byte_cycle || byte_block != block)
                bump(stats.byte_service_blocks);
            byte_service_valid = true; byte_cycle = at; byte_block = block;
            result = at;
            if (block == last_block) break;
            block = add(block, config.byte_service_bytes);
        }
        return result;
    }
    static bool line(const Entry &e, const SprFrontendMacro &m) {
        return e.macro.address_space_id == m.address_space_id && (e.macro.pc >> 6) == (m.pc >> 6);
    }
    static std::uint32_t slots(const SprFrontendMacro &m) { return m.shared_allocation ? 1 : m.uops; }
    void fill(Cycle cycle) {
        if (!reusable_bytes || source != SprFrontendSource::Mite || macro.pseudo || !macro.complete || !macro.dsb_eligible) return;
        const auto units = slots(macro);
        if (units > config.dsb_slots_per_entry) { bump(stats.dsb_oversize); return; }
        auto &s = set(macro.pc);
        const auto block = macro.pc & ~Cycle(31);
        for (const auto &b : s.blocked)
            if (b.asid == macro.address_space_id && b.address == block) return;
        unsigned branches = macro.unconditional_branch;
        for (const auto &e : s.entries)
            if (e.macro.address_space_id == macro.address_space_id &&
                    (e.macro.pc & ~Cycle(31)) == block && e.macro.pc != macro.pc)
                branches += e.macro.unconditional_branch;
        if (branches > 3) {
            auto first = std::remove_if(s.entries.begin(), s.entries.end(), [&](const Entry &e) {
                return e.macro.address_space_id == macro.address_space_id &&
                    (e.macro.pc & ~Cycle(31)) == block;
            });
            bump(stats.dsb_invalidations, std::distance(first, s.entries.end()));
            s.entries.erase(first, s.entries.end());
            // Bounded LRU tombstones retain the branch prior without an
            // unbounded history of every rejected code block in the workload.
            if (s.blocked.size() == std::size_t(config.dsb_ways) * 2) {
                auto victim = std::min_element(s.blocked.begin(), s.blocked.end(),
                    [](const Block &a, const Block &b) { return a.stamp < b.stamp; });
                s.blocked.erase(victim);
            }
            s.blocked.push_back({macro.address_space_id, block, add(stamp, 1)});
            bump(stamp); bump(stats.dsb_branch_limited_blocks); return;
        }
        s.entries.erase(std::remove_if(s.entries.begin(), s.entries.end(), [&](const Entry &e) {
            return e.macro.pc == macro.pc && e.macro.address_space_id == macro.address_space_id;
        }), s.entries.end());
        auto own = units;
        for (const auto &e : s.entries) if (line(e, macro)) own += slots(e.macro);
        const Cycle line_capacity = Cycle(config.dsb_ways) * config.dsb_slots_per_entry;
        if (own > line_capacity) { bump(stats.dsb_oversize); return; }
        auto used = [&] {
            Cycle ways = 0;
            for (std::size_t i = 0; i < s.entries.size(); ++i) {
                bool first = true;
                for (std::size_t j = 0; j < i; ++j)
                    if (line(s.entries[j], s.entries[i].macro)) { first = false; break; }
                if (!first) continue;
                Cycle total = 0;
                for (const auto &other : s.entries)
                    if (line(other, s.entries[i].macro)) total += slots(other.macro);
                if (line(s.entries[i], macro)) total += units;
                ways += (total + config.dsb_slots_per_entry - 1) / config.dsb_slots_per_entry;
            }
            bool exists = false;
            for (const auto &e : s.entries) exists |= line(e, macro);
            if (!exists) ways += (units + config.dsb_slots_per_entry - 1) / config.dsb_slots_per_entry;
            return ways;
        };
        while (used() > config.dsb_ways) {
            auto victim = s.entries.end();
            for (auto it = s.entries.begin(); it != s.entries.end(); ++it)
                if (!line(*it, macro) && (victim == s.entries.end() || it->stamp < victim->stamp)) victim = it;
            if (victim == s.entries.end()) throw std::logic_error("SPR DSB replacement invariant");
            const auto old = victim->macro;
            s.entries.erase(std::remove_if(s.entries.begin(), s.entries.end(), [&](const Entry &e) {
                return line(e, old);
            }), s.entries.end());
            bump(stats.dsb_evictions);
        }
        bump(stamp); s.entries.push_back({macro, add(cycle, 1), stamp}); bump(stats.dsb_fills);
    }
    Cycle next_service() {
        Cycle at = std::max(ready, source_floor);
        if (service_valid) {
            at = std::max(at, service_cycle);
            const auto limit = std::min(width(), config.idq_capacity);
            if (at == service_cycle && (service_slots == limit || service_ops == 4 * limit))
                at = add(at, 1);
        }
        return at;
    }
    // Advance only at service events, never scan idle cycles. The entire
    // source delivery group must fit IDQ credits, matching native ordering.
    void pump(Cycle through) {
        while (macro_active && produced < macro.uops) {
            const Cycle at = next_service();
            if (at > through) return;
            const auto limit = std::min(width(), config.idq_capacity);
            const bool same_cycle = service_valid && at == service_cycle;
            const auto old_slots = same_cycle ? service_slots : 0;
            const auto old_ops = same_cycle ? service_ops : 0;
            auto count = std::min(fragment_end - produced, 4 * limit - old_ops);
            if (!macro.shared_allocation) count = std::min(count, limit - old_slots);
            else if (produced == 0 && old_slots == limit) count = 0;
            const auto cost = macro.shared_allocation ? unsigned(produced == 0 && count) : count;
            if (!count) { ready = add(at, 1); continue; }
            if (cost > config.idq_capacity - occupancy) { bump(stats.idq_credit_stalls); return; }
            service_valid = true; service_cycle = at;
            service_slots = old_slots + cost; service_ops = old_ops + count;
            for (unsigned i = 0; i < count; ++i) {
                const auto index = produced++;
                const auto one_cost = macro.shared_allocation ? unsigned(index == 0) : 1;
                queue.push_back({at, index, one_cost});
            }
            occupancy += cost;
            stats.idq_high_water_slots = std::max<Cycle>(stats.idq_high_water_slots, occupancy);
            bump(stats.enqueued_uops, count);
            if (produced == macro.uops) fill(at);
            else if (produced == fragment_end) {
                fragment_end += std::min(config.batch_uops, macro.uops - fragment_end);
                ready = add(at, latency());
            }
        }
    }
};

SprFrontend::SprFrontend(const SprFrontendConfig &c) {
    require(power2(c.byte_service_bytes) && power2(c.byte_buffer_bytes) &&
            c.byte_buffer_bytes >= c.byte_service_bytes && c.byte_buffer_bytes <= 4096,
            "SPR frontend invalid instruction-byte geometry");
    require(power2(c.dsb_sets) && c.dsb_sets <= 65536 && c.dsb_ways && c.dsb_ways <= 64 &&
            c.dsb_slots_per_entry && c.dsb_slots_per_entry <= 64,
            "SPR frontend invalid bounded DSB geometry");
    require(c.mite_instructions_per_batch && c.mite_instructions_per_batch <= 64 &&
            c.mite_width && c.mite_width <= 64 && c.dsb_width && c.dsb_width <= 64 &&
            c.ms_width && c.ms_width <= 64 && c.rename_width && c.rename_width <= 64 &&
            c.batch_uops && c.batch_uops <= 4096 && c.idq_capacity && c.idq_capacity <= 65536,
            "SPR frontend invalid bounded service geometry");
    require(c.mite_latency && c.dsb_latency && c.ms_latency, "SPR frontend source latency must be positive");
    impl_.reset(new Impl(c));
}
SprFrontend::~SprFrontend() = default;
SprFrontend::SprFrontend(const SprFrontend &other) : impl_(new Impl(*other.impl_)) {}
SprFrontend &SprFrontend::operator=(const SprFrontend &other) {
    if (this != &other) impl_.reset(new Impl(*other.impl_));
    return *this;
}
SprFrontend::SprFrontend(SprFrontend &&) noexcept = default;
SprFrontend &SprFrontend::operator=(SprFrontend &&) noexcept = default;
SprFrontendSource SprFrontend::source_for(const SprFrontendMacro &m) const {
    validate(m, impl_->config);
    if (m.microcode) return SprFrontendSource::Microcode;
    if (m.pseudo || !m.complete || !m.dsb_eligible) return SprFrontendSource::Mite;
    return impl_->find(m) ? SprFrontendSource::Dsb : SprFrontendSource::Mite;
}
std::vector<std::uint64_t> SprFrontend::instruction_blocks(const SprFrontendMacro& m) const {
    const auto& p=*impl_; validate(m,p.config);
    bool invalid=p.have_context && p.context!=m.address_space_id;
    for(const auto& e:p.set(m.pc).entries)
        invalid|=e.macro.pc==m.pc && e.macro.address_space_id==m.address_space_id && !same(e.macro,m);
    for(const auto& old:p.byte_identities)
        invalid|=old.pc==m.pc && old.address_space_id==m.address_space_id && !same(old,m);
    if(m.pseudo || (!invalid && source_for(m)==SprFrontendSource::Dsb))return {};
    const auto mask=~Cycle(p.config.byte_buffer_bytes-1);
    const auto first=m.pc&mask,last=add(m.pc,m.instruction_bytes-1)&mask;
    std::vector<std::uint64_t> result;
    if(invalid || !p.byte_valid || p.byte_base!=first)result.push_back(first);
    if(last!=first)result.push_back(last);
    return result;
}
void SprFrontend::invalidate_code() {impl_->invalidate();}
void SprFrontend::begin_macro(const SprFrontendMacro &m, Cycle earliest, Cycle byte_ready, bool reusable_bytes) {
    auto &p = *impl_;
    require(!p.macro_active && !p.outstanding, "SPR frontend already has an active macro");
    validate(m, p.config);
    // Reserve overflow headroom before changing physical state.
    (void)add(std::max(earliest, byte_ready), add(2, std::max({p.config.mite_latency,
        p.config.dsb_latency, p.config.ms_latency, p.config.dsb_to_mite_delay})));
    if (p.have_context && p.context != m.address_space_id) p.invalidate();
    p.have_context = true; p.context = m.address_space_id;
    bool mismatch = false;
    for (const auto &e : p.set(m.pc).entries)
        if (e.macro.pc == m.pc && e.macro.address_space_id == m.address_space_id && !same(e.macro, m)) mismatch = true;
    for (const auto &old : p.byte_identities)
        if (old.pc == m.pc && old.address_space_id == m.address_space_id && !same(old,m)) mismatch = true;
    if (mismatch) p.invalidate();
    p.reusable_bytes=reusable_bytes;
    const auto next = reusable_bytes?source_for(m):(m.microcode?SprFrontendSource::Microcode:SprFrontendSource::Mite);
    bump(p.stats.dsb_lookups);
    Cycle submit = earliest;
    if (next == SprFrontendSource::Dsb) {
        bump(p.stats.dsb_hits);
        submit = std::max(submit, p.find(m)->available);
        for (auto &e : p.set(m.pc).entries) if (same(e.macro, m)) { bump(p.stamp); e.stamp = p.stamp; }
    } else if (!m.pseudo) submit = p.bytes(m, submit, byte_ready);
    const bool transition = p.have_source && p.selected != next;
    if (transition) {
        bump(p.stats.source_transitions);
        if (p.service_valid) submit = std::max(submit, p.service_cycle);
        if (p.selected == SprFrontendSource::Dsb && next == SprFrontendSource::Mite) {
            submit = add(submit, p.config.dsb_to_mite_delay);
            bump(p.stats.switch_delay_cycles, p.config.dsb_to_mite_delay);
        }
    }
    const auto max_macros = next == SprFrontendSource::Mite ? p.config.mite_instructions_per_batch :
        next == SprFrontendSource::Microcode ? 1U : 64U;
    const auto fragment = std::min(m.uops, p.config.batch_uops);
    const bool open_batch = p.have_source && !transition && submit <= p.batch_submit &&
        p.batch_macros < max_macros && fragment <= p.config.batch_uops - p.batch_ops;
    if (!open_batch) {
        if (p.have_source && !transition) submit = std::max(submit, add(p.batch_submit, 1));
        p.batch_submit = submit; p.batch_macros = 0; p.batch_ops = 0;
    } else submit = p.batch_submit;
    p.selected = next; p.have_source = true;
    ++p.batch_macros; p.batch_ops += fragment;
    p.source = next; p.macro = m; p.macro_active = true;
    p.produced = p.consumed = 0; p.fragment_end = fragment;
    p.ready = add(submit, p.latency());
    p.source_floor = 0;
    bump(p.stats.macros);
    if (next == SprFrontendSource::Mite) bump(p.stats.mite_macros);
    else if (next == SprFrontendSource::Dsb) bump(p.stats.dsb_macros);
    else bump(p.stats.ms_macros);
}
SprFrontendDelivery SprFrontend::deliver_next(Cycle earliest) {
    auto &p = *impl_;
    require(p.macro_active && !p.outstanding, "SPR frontend delivery requires active macro and prior acknowledgement");
    const Cycle floor = std::max(earliest, p.rename_floor);
    p.pump(floor);
    if (p.queue.empty()) p.pump(p.next_service());
    if (p.queue.empty()) throw std::logic_error("SPR frontend IDQ cannot make forward progress");
    const auto head = p.queue.front(); p.queue.pop_front();
    const auto cycle = std::max(floor, head.cycle);
    p.outstanding = true; p.sent_cycle = cycle; p.sent_cost = head.cost;
    ++p.consumed;
    bump(p.stats.uops); bump(p.stats.allocation_slots, head.cost);
    return {cycle, head.cost, p.source, head.index == 0, head.index + 1 == p.macro.uops};
}
void SprFrontend::accepted(Cycle actual) {
    auto &p = *impl_;
    require(p.outstanding && actual >= p.sent_cycle, "SPR frontend invalid Rename acknowledgement");
    p.pump(actual); // Held slots remain charged throughout the blocked interval.
    p.occupancy -= p.sent_cost; p.outstanding = false;
    p.rename_floor = actual; p.source_floor = actual;
    bump(p.stats.accepted_uops);
    if (p.consumed == p.macro.uops) p.macro_active = false;
}
bool SprFrontend::active() const { return impl_->macro_active; }
std::uint32_t SprFrontend::idq_occupancy() const { return impl_->occupancy; }
const SprFrontendCounters &SprFrontend::counters() const { return impl_->stats; }
void SprFrontend::reset() { const auto c = impl_->config; impl_.reset(new Impl(c)); }
void SprFrontend::reset_counters() { impl_->stats = {}; }
} // namespace fastsim
