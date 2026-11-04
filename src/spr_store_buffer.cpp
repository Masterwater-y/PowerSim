#include "fastsim/spr_store_buffer.hpp"

#include <algorithm>
#include <deque>
#include <limits>
#include <stdexcept>
#include <unordered_map>
#include <utility>

namespace fastsim::spr {

struct StoreBuffer::Impl {
    struct Store {
        StoreToken token;
        StoreFacts facts;
        std::uint64_t address = 0;
        bool address_ready = false, data_ready = false, published = false;
        bool committed = false, can_wb = false, prepare_attempted = false;
    };
    struct Write {
        WriteCandidate candidate;
        bool issued = false, completed = false;
    };
    struct Hint {
        PrepareToken token;
        StoreToken store;
        std::uint64_t source_id, line;
    };
    BufferProfile profile;
    EventSink sink;
    StoreBufferCounters counts;
    std::deque<Store> sq;
    std::deque<Write> buffer;
    std::unordered_map<std::uint64_t, std::size_t> lines;
    std::unordered_map<std::uint64_t, Hint> hints;
    std::uint64_t generation = 1, next_store = 1, next_write = 1, next_hint = 1;
    std::uint64_t last_cycle = 0, offload_cycle = 0, port_cycle = 0;
    std::size_t offloads = 0, ports = 0;

    Impl(BufferProfile p, EventSink s) : profile(p), sink(std::move(s)) {
        if (!p.shared_store_entries || !p.line_window || !p.offload_width ||
            !p.cache_store_ports || !p.line_bytes || (p.line_bytes & (p.line_bytes - 1)))
            throw std::invalid_argument("invalid SPR store buffer resource profile");
    }
    void checkCycle(std::uint64_t cycle) const {
        if (cycle < last_cycle)
            throw std::invalid_argument("SPR store buffer model cycle moved backwards");
    }
    void touch(std::uint64_t cycle) { checkCycle(cycle); last_cycle = cycle; }
    std::uint64_t line(std::uint64_t addr) const {
        return addr & ~(static_cast<std::uint64_t>(profile.line_bytes) - 1);
    }
    bool portAvailable(std::uint64_t cycle) const {
        return cycle != port_cycle || ports < profile.cache_store_ports;
    }
    void consumePort(std::uint64_t cycle) {
        if (cycle != port_cycle) { port_cycle = cycle; ports = 0; }
        ++ports;
    }
    Store& store(StoreToken t) {
        auto it = std::find_if(sq.begin(), sq.end(), [&](const Store& s) {
            return s.token.id == t.id && s.token.generation == t.generation;
        });
        if (it == sq.end()) throw std::invalid_argument("unknown or stale SPR store token");
        return *it;
    }
    void emit(StoreEvent event) const { if (sink) sink(event); }
    void storeEvent(StoreEventKind kind, const Store& s, std::uint64_t cycle,
                    BufferWriteToken write = {}) const {
        emit({kind, cycle, s.token, write, {}, s.facts.source_id,
              s.address_ready ? line(s.address) : 0});
    }
    void transition(StoreToken t, std::uint64_t cycle, bool Store::* member,
                    StoreEventKind kind) {
        checkCycle(cycle);
        auto& s = store(t);
        if (s.*member) throw std::invalid_argument("duplicate SPR store transition");
        touch(cycle); s.*member = true; storeEvent(kind, s, cycle);
    }
    static bool same(const WriteCandidate& a, const WriteCandidate& b) {
        return a.token.id == b.token.id && a.token.generation == b.token.generation &&
            a.revision == b.revision && a.line == b.line && a.context == b.context &&
            a.requestor == b.requestor && a.flags == b.flags && a.mask == b.mask &&
            a.last_writer == b.last_writer;
    }
    static bool same(const PrepareCandidate& a, const PrepareCandidate& b) {
        return a.store.id == b.store.id && a.store.generation == b.store.generation &&
            a.line == b.line && a.context == b.context && a.requestor == b.requestor &&
            a.flags == b.flags;
    }
};

StoreBuffer::StoreBuffer(BufferProfile profile, EventSink sink)
    : impl_(std::make_unique<Impl>(profile, std::move(sink))) {}
StoreBuffer::~StoreBuffer() = default;

std::optional<StoreToken>
StoreBuffer::allocate(const StoreFacts& facts, std::uint64_t cycle) {
    auto& p = *impl_;
    p.checkCycle(cycle);
    if (facts.kind != StoreKind::Ordinary && facts.kind != StoreKind::NoAccess)
        throw std::invalid_argument("unsupported SPR store class");
    if (facts.kind == StoreKind::Ordinary && (!facts.bytes || facts.bytes > p.profile.line_bytes))
        throw std::invalid_argument("ordinary SPR store requires a nonempty single-line extent");
    if (used() >= p.profile.shared_store_entries) { ++p.counts.allocation_refused; return {}; }
    if (p.next_store == std::numeric_limits<std::uint64_t>::max())
        throw std::overflow_error("SPR store identity exhausted");
    p.touch(cycle);
    StoreToken token{p.next_store++, p.generation};
    p.sq.push_back({token, facts});
    ++p.counts.allocated;
    p.storeEvent(StoreEventKind::Allocate, p.sq.back(), cycle);
    return token;
}

void StoreBuffer::markAddressReady(StoreToken token, std::uint64_t addr,
                                   std::uint64_t cycle) {
    auto& p = *impl_; p.checkCycle(cycle);
    auto& s = p.store(token);
    if (s.address_ready) throw std::invalid_argument("duplicate SPR address readiness");
    const auto offset = addr - p.line(addr);
    if (s.facts.kind == StoreKind::Ordinary && s.facts.bytes > p.profile.line_bytes - offset)
        throw std::invalid_argument("split SPR store requires an unsupported fragment path");
    p.touch(cycle); s.address = addr; s.address_ready = true;
    p.storeEvent(StoreEventKind::AddressReady, s, cycle);
}
void StoreBuffer::markDataReady(StoreToken token, std::uint64_t cycle) {
    impl_->transition(token, cycle, &Impl::Store::data_ready, StoreEventKind::DataReady);
}
void StoreBuffer::publish(StoreToken token, std::uint64_t cycle) {
    impl_->transition(token, cycle, &Impl::Store::published, StoreEventKind::Publish);
}
void StoreBuffer::commit(StoreToken token, std::uint64_t cycle) {
    impl_->transition(token, cycle, &Impl::Store::committed, StoreEventKind::Commit);
}
void StoreBuffer::allowWriteback(StoreToken token, std::uint64_t cycle) {
    impl_->transition(token, cycle, &Impl::Store::can_wb, StoreEventKind::CanWriteback);
}

std::size_t StoreBuffer::offload(std::uint64_t cycle) {
    auto& p = *impl_; p.touch(cycle);
    if (p.offload_cycle != cycle) { p.offload_cycle = cycle; p.offloads = 0; }
    std::size_t moved = 0;
    while (!p.sq.empty() && p.offloads < p.profile.offload_width) {
        const auto s = p.sq.front();
        BufferWriteToken combined_write;
        if (!s.published || !s.committed || !s.can_wb) break;
        if (s.facts.kind == StoreKind::NoAccess) {
            ++p.counts.no_access_complete;
        } else {
            if (!s.address_ready || !s.data_ready) break;
            const auto line = p.line(s.address);
            auto* target = p.buffer.empty() ? nullptr : &p.buffer.back();
            if (target && (target->issued || target->candidate.line != line ||
                target->candidate.context != s.facts.context ||
                target->candidate.requestor != s.facts.requestor ||
                target->candidate.flags != s.facts.flags)) target = nullptr;
            if (!target) {
                if (p.next_write == std::numeric_limits<std::uint64_t>::max())
                    throw std::overflow_error("SPR synthetic write identity exhausted");
                WriteCandidate c;
                c.token = {p.next_write++, p.generation};
                c.line = line; c.context = s.facts.context; c.requestor = s.facts.requestor;
                c.flags = s.facts.flags;
                c.mask.resize(p.profile.line_bytes, false);
                c.last_writer.resize(p.profile.line_bytes, 0);
                p.buffer.push_back({std::move(c)});
                target = &p.buffer.back();
                ++p.counts.writes_created;
            } else {
                ++p.counts.merged;
            }
            ++target->candidate.revision;
            const auto offset = s.address - line;
            for (std::size_t i = 0; i < s.facts.bytes; ++i) {
                target->candidate.mask[offset + i] = true;
                target->candidate.last_writer[offset + i] = s.token.id;
            }
            ++p.counts.architectural_combined;
            ++p.counts.offloaded;
            combined_write = target->candidate.token;
        }
        p.sq.pop_front(); ++p.offloads; ++moved;
        p.storeEvent(s.facts.kind == StoreKind::NoAccess ?
                     StoreEventKind::NoAccessComplete : StoreEventKind::StoreBufferCombined,
                     s, cycle, combined_write);
    }
    return moved;
}

std::optional<WriteCandidate> StoreBuffer::peekWrite(std::uint64_t cycle) const {
    const auto& p = *impl_; p.checkCycle(cycle);
    if (!p.portAvailable(cycle)) return {};
    for (const auto& w : p.buffer) {
        if (w.issued) continue;
        if (!p.lines.count(w.candidate.line) && p.lines.size() >= p.profile.line_window)
            return {}; // Native drain stops at the first unissued blocked entry.
        return w.candidate;
    }
    return {};
}

bool StoreBuffer::trySendWrite(const WriteCandidate& candidate, std::uint64_t cycle,
                               const WriteAcceptor& accept) {
    auto& p = *impl_;
    const auto eligible = peekWrite(cycle);
    if (!eligible || !Impl::same(*eligible, candidate)) return false;
    if (!accept || !accept(*eligible)) return false;
    // The acceptor must be non-reentrant. Its true result certifies ordinary
    // backend request credit; no independent ordinary budget lives here.
    p.touch(cycle);
    auto it = std::find_if(p.buffer.begin(), p.buffer.end(), [&](const auto& w) {
        return w.candidate.token.id == eligible->token.id;
    });
    it->issued = true;
    ++p.lines[eligible->line]; p.consumePort(cycle); ++p.counts.writes_accepted;
    p.emit({StoreEventKind::WriteAccepted, cycle, {}, eligible->token, {}, 0, eligible->line});
    return true;
}

bool StoreBuffer::completeWrite(BufferWriteToken token, std::uint64_t cycle) {
    auto& p = *impl_; p.checkCycle(cycle);
    auto it = std::find_if(p.buffer.begin(), p.buffer.end(), [&](const auto& w) {
        return w.candidate.token.id == token.id && w.candidate.token.generation == token.generation;
    });
    if (it == p.buffer.end() || !it->issued || it->completed) {
        ++p.counts.invalid_callbacks; return false;
    }
    auto line = p.lines.find(it->candidate.line);
    if (line == p.lines.end() || !line->second)
        throw std::logic_error("SPR in-flight line accounting corrupt");
    p.touch(cycle); it->completed = true;
    if (--line->second == 0) p.lines.erase(line);
    ++p.counts.writes_completed;
    p.emit({StoreEventKind::WriteComplete, cycle, {}, token, {}, 0, it->candidate.line});
    while (!p.buffer.empty() && p.buffer.front().completed) {
        const auto released_token = p.buffer.front().candidate.token;
        const auto released_line = p.buffer.front().candidate.line;
        ++p.counts.buffer_credits_released;
        p.buffer.pop_front();
        p.emit({StoreEventKind::BufferCreditReleased, cycle, {}, released_token, {}, 0, released_line});
    }
    return true;
}

std::optional<PrepareCandidate> StoreBuffer::peekPrepare(std::uint64_t cycle) const {
    const auto& p = *impl_; p.checkCycle(cycle);
    if (!p.portAvailable(cycle)) return {};
    for (const auto& s : p.sq) {
        if ((!p.profile.early_prepare && !s.can_wb) || s.facts.kind != StoreKind::Ordinary)
            break;
        if (!s.address_ready || s.prepare_attempted) continue;
        return PrepareCandidate{s.token, p.line(s.address), s.facts.context, s.facts.requestor,
                                s.facts.flags};
    }
    return {};
}

PrepareResult StoreBuffer::trySendPrepare(const PrepareCandidate& candidate,
                                          std::uint64_t cycle, const PrepareAcceptor& accept) {
    auto& p = *impl_;
    const auto eligible = peekPrepare(cycle);
    if (!eligible || !Impl::same(*eligible, candidate)) return {};
    auto& s = p.store(candidate.store);
    if (p.profile.prepare_outstanding && p.hints.size() >= p.profile.prepare_outstanding) {
        p.touch(cycle); s.prepare_attempted = true; ++p.counts.prepares_dropped;
        p.consumePort(cycle); // Native cap drop returns Issued to the CPU port.
        p.storeEvent(StoreEventKind::PrepareDropped, s, cycle);
        return {PrepareOutcome::Dropped, {}};
    }
    if (p.next_hint == std::numeric_limits<std::uint64_t>::max())
        throw std::overflow_error("SPR prepare identity exhausted");
    PrepareToken token{p.next_hint, p.generation};
    if (!accept || !accept(*eligible, token)) return {PrepareOutcome::Rejected, {}};
    p.touch(cycle); ++p.next_hint; s.prepare_attempted = true;
    p.hints.emplace(token.id, Impl::Hint{token, s.token, s.facts.source_id, eligible->line});
    p.consumePort(cycle); ++p.counts.prepares_accepted;
    p.emit({StoreEventKind::PrepareAccepted, cycle, s.token, {}, token,
            s.facts.source_id, eligible->line});
    return {PrepareOutcome::Accepted, token};
}

bool StoreBuffer::completePrepare(PrepareToken token, PrepareTerminal reason, std::uint64_t cycle) {
    auto& p = *impl_; p.checkCycle(cycle);
    auto it = p.hints.find(token.id);
    if (it == p.hints.end() || it->second.token.generation != token.generation) {
        ++p.counts.invalid_callbacks; return false;
    }
    p.touch(cycle);
    const auto hint = it->second;
    p.hints.erase(it); ++p.counts.prepares_completed;
    p.emit({StoreEventKind::PrepareComplete, cycle, hint.store, {}, token,
            hint.source_id, hint.line, reason});
    return true;
}

void StoreBuffer::invalidateLine(std::uint64_t addr, std::uint64_t cycle) {
    impl_->touch(cycle);
    impl_->emit({StoreEventKind::PermissionInvalidated, cycle, {}, {}, {}, 0, impl_->line(addr)});
}
void StoreBuffer::advanceGeneration() {
    auto& p = *impl_;
    if (used() || !p.hints.empty())
        throw std::logic_error("cannot reincarnate SPR store owner with live transactions");
    if (p.generation == std::numeric_limits<std::uint64_t>::max())
        throw std::overflow_error("SPR store generation exhausted");
    ++p.generation;
}
std::size_t StoreBuffer::used() const { return sqSize() + committedSize(); }
std::size_t StoreBuffer::sqSize() const { return impl_->sq.size(); }
std::size_t StoreBuffer::committedSize() const { return impl_->buffer.size(); }
std::size_t StoreBuffer::inflightLines() const { return impl_->lines.size(); }
std::size_t StoreBuffer::outstandingPrepares() const { return impl_->hints.size(); }
const StoreBufferCounters& StoreBuffer::counters() const { return impl_->counts; }

} // namespace fastsim::spr
