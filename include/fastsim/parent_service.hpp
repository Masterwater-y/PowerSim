#pragma once

#include <algorithm>
#include <array>
#include <cstdint>
#include <limits>
#include <memory>
#include <queue>
#include <stdexcept>
#include <unordered_map>
#include <vector>
#include "fastsim/service_snapshot.hpp"

namespace fastsim {

// One core/controller domain per pool. All line and release indices contain
// IDs only. Data and the two resource-release facts live in the same record.
// This first migration retains the checkpoint-local legacy MSHR calendar.
class ParentServicePool {
 public:
  using Id = std::uint64_t;
  static constexpr Id none = 0;
  enum class Time : std::uint8_t { kUnknown, kFinalLocal, kSelectedFixed, kLegacyCoupled };
  enum class State : std::uint8_t { kUnknown, kInvalid, kShared, kExclusive, kModified };
  enum class Action : std::uint8_t { kUnknown, kNone, kFetchShared, kFetchExclusive, kUpgrade };
  enum class Role : std::uint8_t { kLoad, kPrepare, kStore };
  enum class Edge : std::uint8_t { kData, kPermission, kController, kLegacyCallback };
  enum class Resource : std::uint8_t { kTbe, kFill };
  enum class SourceReject : std::uint8_t { kAdmissionMismatch, kCarried };
  enum class SourceDecision : std::uint8_t {
    kSplit, kTargetUnknown, kActionUnknown, kCarried, kAdmissionMismatch,
    kLowerMissing, kBeforeAdmission, kAfterLegacyDone, kUnsupportedAction,
    kCallbackBeforeSelected, kResourceNonmonotone, kResourceCapacity, kHostCapacity, kCount
  };
  // Interface skeleton only: current callbacks keep their existing integer
  // clock. New estimated Q16 edges cannot be consumed without a path contract.
  enum class Evidence : std::uint8_t {
    kUnknown, kEstimated, kLowerBound, kUpperBound, kSelected, kFinalLocal, kLegacy
  };
  struct TimeEdge {
    std::uint64_t q16 = 0;
    std::uint32_t domain = 0, epoch = 0;
    Evidence evidence = Evidence::kUnknown;
    bool known() const { return evidence != Evidence::kUnknown; }
    bool comparable(const TimeEdge& b) const {
      return known() && b.known() && domain == b.domain && epoch == b.epoch;
    }
  };
  struct Facts {
    State pre_state = State::kUnknown;
    Action action = Action::kUnknown;
    bool lower_issued = false;
    // Absolute model-selected lower return, in the reference clock domain.
    // Zero is not a sentinel: validity is independent of the value.
    std::uint64_t lower_return = 0;
    bool lower_return_valid = false;
  };
  enum class Outcome : std::uint8_t {
    kAbsent, kFollower, kCompleted, kFuture, kUncertified
  };
  struct Lease {
    std::uint64_t begin = 0, release = 0;
    bool indexed = false;
  };
  struct Record {
    Id id = none;
    std::uint64_t line = 0, sequence = 0, admission = 0, data_ready = 0;
    Time data_time = Time::kUnknown;
    bool indexed = false;
    std::array<Lease, 2> leases{};
    // Request callbacks and prepare credits are not service leases.
    std::uint64_t permission_ready = 0, controller_ready = 0;
    std::uint64_t write_ready = 0, consumer_sequence = 0;
    std::uint32_t pins = 0;
    Action action = Action::kUnknown;
    Role origin = Role::kLoad, consumer = Role::kLoad;
    Time permission_time = Time::kUnknown;
    bool has_data_fill = false;
    Time controller_time = Time::kUnknown;
    std::uint64_t request_callback = 0;
    struct OwnedLease {
      std::uint64_t begin = 0;
      Edge release_edge = Edge::kController;
      bool indexed = false;
    };
    std::array<OwnedLease,2> owned{};
  };
  struct Decision {
    Outcome outcome = Outcome::kAbsent;
    Id id = none;
  };

  void configure(std::size_t capacity) {
    if (capacity == 0 || capacity > 65536)
      throw std::invalid_argument("parent service capacity must be 1..65536");
    if (active_) throw std::logic_error("reconfiguring live services");
    capacity_ = capacity;
    records_.reserve((capacity + block_size - 1) / block_size);
    free_.reserve(capacity);
    lines_.configure(capacity);
  }
  const Record* get(Id id) const {
    if (!id) return nullptr;
    const auto slot = static_cast<std::size_t>((id & slot_mask) - 1);
    const auto* r = slot < slots_ ? &records_[slot / block_size]->at(slot % block_size) : nullptr;
    return r && r->id == id ? r : nullptr;
  }
  Decision decide(std::uint64_t line, std::uint64_t admission) const {
    const auto id = lines_.get(line);
    if (!id) return {};
    const auto& r = record(id);
    if (r.data_time == Time::kUnknown) return {Outcome::kUncertified, r.id};
    if (admission < r.admission) return {Outcome::kFuture, r.id};
    return {completion(r) > admission ? Outcome::kFollower : Outcome::kCompleted, r.id};
  }
  void invalidate(std::uint64_t line) {
    const Id id = lines_.get(line);
    if (!id) return;
    lines_.erase(line);
    record(id).indexed = false;
    collect(id);
  }
  Id create(std::uint64_t line, std::uint64_t sequence,
            std::uint64_t admission, std::uint64_t ready, Time time,
            bool line_indexed) {
    if (ready < admission) throw std::logic_error("parent responds before admission");
    if (line_indexed) invalidate(line);
    if (active_ >= capacity_) return none;
    compact_expiry();
    if (next_ == (std::numeric_limits<Id>::max() >> slot_bits))
      throw std::overflow_error("parent service generation overflow");
    std::size_t slot;
    if (free_.empty()) {
      slot = slots_++;
      if (slot % block_size == 0) records_.push_back(std::make_shared<Block>());
    } else {
      slot = free_.pop_back();
    }
    const Id id = (++next_ << slot_bits) | (slot+1);
    writable_slot(slot) = Record{id,line,sequence,admission,ready,time,line_indexed,{}};
    ++active_;
    if (line_indexed) {
      lines_.put(line,id);
      expiry_.push({ready,id},expiry_less);
    }
    peak_ = std::max(peak_, active_);
    return id;
  }
  // Reclamation uses monotone dispatch, never program-order OoO issue.
  void expire_before(std::uint64_t watermark) {
    if (watermark < watermark_) throw std::logic_error("parent watermark moved backwards");
    watermark_ = watermark;
    for (std::size_t resource=0; resource<2; ++resource) {
      auto& heap=owned_releases_[resource];
      auto compare=[this,resource](Id a,Id b) {
        return owned_release(a,resource)<owned_release(b,resource);
      };
      while (!heap.empty() && owned_release(heap.top(),resource)<=watermark) {
        const auto id=heap.pop(compare);
        record(id).owned[resource].indexed=false;
        collect(id);
      }
    }
    while (!expiry_.empty() && expiry_.top().time <= watermark) {
      const Id id = expiry_.top().id;
      const auto expiry_time = expiry_.top().time;
      expiry_.pop(expiry_less);
      auto* r = mutable_get(id);
      if (!r) continue;
      if (expiry_time < completion(*r)) continue;
      if (r->indexed) {
        if (lines_.get(r->line) == id) lines_.erase(r->line);
        r->indexed = false;
      }
      collect(id);
    }
  }
  // An expiry can become stale when a functional generation is replaced.
  // Compact before scheduling: index size never grows with ROI length.
  void compact_expiry() {
    if (expiry_.size() < capacity_) return;
    decltype(expiry_) rebuilt;
    lines_.each([&](std::uint64_t, Id id) {
      const auto& r = *get(id);
      rebuilt.push({completion(r),r.id},expiry_less);
    });
    // Rebuilding does not erase cumulative copy diagnostics.
    expired_index_cost_ += index_heap_cost(expiry_);
    expiry_.swap(rebuilt);
  }
  void begin_checkpoint(std::size_t l1, std::size_t l2) {
    for (std::size_t level=0; level<2; ++level) {
      for (std::size_t i=0; i<releases_[level].size(); ++i) {
        const Id id = releases_[level].at(i);
        if (!id) continue;
        record(id).leases[level].indexed = false;
        collect(id);
      }
      releases_[level].assign(level==0 ? l1 : l2, none);
    }
  }
  bool has_calendar(std::size_t level) const { return !releases_.at(level).empty(); }
  std::uint64_t first_release(std::size_t level) const {
    const auto& heap = releases_.at(level);
    return heap.empty() ? 0 : release(heap.top(),level);
  }
  void attach_lease(Id id, std::size_t level, std::uint64_t begin,
                    std::uint64_t end) {
    auto& heap = releases_.at(level);
    if (heap.empty()) return;
    auto compare = [this,level](Id a, Id b) {
      return release(a,level) < release(b,level);
    };
    Id old = heap.pop(compare);
    if (old) {
      record(old).leases[level].indexed = false;
      collect(old);
    }
    record(id).leases[level] = {begin,end,true};
    heap.push(id,compare);
  }
  void discard_unindexed(Id id) { collect(id); }
  void pin(Id id) { ++record(id).pins; }
  void unpin(Id id) {
    auto& r = record(id);
    if (!r.pins) throw std::logic_error("unbalanced parent service pin");
    --r.pins;
    collect(id);
  }
  Id indexed_id(std::uint64_t line) const {
    return lines_.get(line);
  }
  // Called only by the request that owns the transaction. No second line
  // timing table is introduced by prepare/store.
  void set_service(Id id, Action action, Role origin, std::uint64_t data,
                   std::uint64_t permission, std::uint64_t controller, Time quality) {
    auto& r = record(id);
    r.action = action; r.origin = r.consumer = origin;
    r.has_data_fill = action == Action::kFetchShared || action == Action::kFetchExclusive;
    r.data_ready = data; r.permission_ready = permission; r.controller_ready = controller;
    r.data_time = r.permission_time = quality;
    r.controller_time=quality;
  }
  void set_boundaries(Id id,std::uint64_t data,Time data_quality,
                      std::uint64_t permission,Time permission_quality,
                      std::uint64_t controller,Time controller_quality) {
    auto& r=record(id);
    if ((data_quality!=Time::kUnknown && data<r.admission) ||
        (permission_quality!=Time::kUnknown && permission<r.admission) ||
        (controller_quality!=Time::kUnknown && controller<r.admission))
      throw std::logic_error("service boundary precedes admission");
    if (r.owned[0].indexed || r.owned[1].indexed)
      throw std::logic_error("changing an indexed service release edge");
    r.data_ready=data; r.data_time=data_quality;
    r.permission_ready=permission; r.permission_time=permission_quality;
    r.controller_ready=controller; r.controller_time=controller_quality;
  }
  // These calendars contain only IDs. The owning service holds both the
  // interval start and the typed release edge; no second timing table exists.
  void configure_owned(std::size_t tbes,std::size_t fills) {
    if (!tbes || !fills || active_)
      throw std::invalid_argument("invalid live service-resource configuration");
    owned_capacity_={tbes,fills};
    for (std::size_t r=0;r<2;++r) owned_releases_[r].reserve(owned_capacity_[r]);
  }
  bool can_own(bool fill,std::uint64_t begin) const {
    return own_decision(fill,begin)==SourceDecision::kSplit;
  }
  SourceDecision own_decision(bool fill,std::uint64_t begin) const {
    const std::size_t count=fill ? 2 : 1;
    for (std::size_t r=0;r<count;++r) {
      // Source admission is already frozen. We only certify a monotone
      // reservation that fits; moving it would require lower queue replay.
      if (begin<owned_last_begin_[r]) return SourceDecision::kResourceNonmonotone;
      if (owned_releases_[r].size()>=owned_capacity_[r])
        return SourceDecision::kResourceCapacity;
    }
    return SourceDecision::kSplit;
  }
  void own(Id id,bool fill,std::uint64_t begin) {
    if (!can_own(fill,begin))
      throw std::logic_error("uncertified service resource admission");
    for (std::size_t resource=0;resource<(fill ? 2u : 1u);++resource) {
      auto& lease=record(id).owned[resource];
      if (lease.indexed) throw std::logic_error("duplicate service resource allocation");
      lease={begin,resource==0 ? Edge::kController : Edge::kData,true};
      auto& heap=owned_releases_[resource];
      auto compare=[this,resource](Id a,Id b) {
        return owned_release(a,resource)<owned_release(b,resource);
      };
      heap.push(id,compare);
      owned_last_begin_[resource]=begin;
    }
  }
  std::size_t owned_size(Resource resource) const {
    return owned_releases_[static_cast<std::size_t>(resource)].size();
  }
  std::uint64_t owned_release(Id id,std::size_t resource) const {
    const auto& r=record(id);
    switch(r.owned[resource].release_edge) {
      case Edge::kData: return r.data_ready;
      case Edge::kPermission: return r.permission_ready;
      case Edge::kController: return r.controller_ready;
      case Edge::kLegacyCallback: return r.request_callback;
    }
    throw std::logic_error("invalid resource release edge");
  }
  void takeover(Id id, std::uint64_t write_callback, std::uint64_t sequence = 0,
                std::uint64_t write = std::numeric_limits<std::uint64_t>::max()) {
    auto& r = record(id);
    r.consumer = Role::kStore;
    // Fetched old line data stays distinct from this store's content edge.
    // The existing forwarding path still owns forwarded byte values.
    r.write_ready = write==std::numeric_limits<std::uint64_t>::max() ? write_callback : write;
    r.request_callback=write_callback;
    r.consumer_sequence = sequence;
    if (r.indexed) {
      compact_expiry();
      expiry_.push({completion(r),id},expiry_less);
    }
  }
  void index_service(Id id, std::uint64_t end) {
    const auto line = record(id).line;
    if (indexed_id(line)!=id) invalidate(line);
    auto& r = record(id);
    r.indexed = true;
    lines_.put(line,id);
    expiry_.push({end,id},expiry_less);
  }
  std::uint64_t read_gate(Id id, std::uint64_t admission,
      std::uint64_t sequence=std::numeric_limits<std::uint64_t>::max()) const {
    const auto& r = record(id);
    if (admission < r.admission ||
        r.data_time == Time::kUnknown || r.controller_time==Time::kUnknown)
      return admission;
    const auto write = r.consumer == Role::kStore && r.consumer_sequence<=sequence
        ? r.write_ready : 0;
    return std::max({admission,r.data_ready,r.controller_ready,write});
  }
  std::size_t record_bytes() const { return slots_ * sizeof(Record); }
  std::uint64_t copied_record_blocks() const { return copied_blocks_; }
  using IndexCost = ServiceSnapshotLineIndex::Cost;
  IndexCost index_cost() const {
    auto c = lines_.cost();
    c += index_heap_cost(expiry_);
    c += expired_index_cost_;
    const auto& f = free_.cost();
    c += IndexCost{f.leaves,f.directories,f.bytes};
    for (std::size_t i=0; i<2; ++i) {
      c += index_heap_cost(releases_[i]);
      c += index_heap_cost(owned_releases_[i]);
    }
    return c;
  }
  std::uint64_t line_index_rebuilds() const { return lines_.rebuilds(); }
  std::size_t size() const { return active_; }
  std::size_t peak() const { return peak_; }
  std::size_t index_size() const { return lines_.size(); }
  std::size_t expiry_size() const { return expiry_.size(); }
  void invalidate_all() {
    // Collect keys before mutation; no reference into a detached page escapes.
    std::vector<std::uint64_t> keys;
    keys.reserve(lines_.size());
    lines_.each([&](std::uint64_t line,Id) { keys.push_back(line); });
    for (const auto line : keys) invalidate(line);
  }
 private:
  static constexpr unsigned slot_bits = 17;
  static constexpr Id slot_mask = (Id{1} << slot_bits)-1;
  static constexpr std::size_t block_size = 32;
  using Block = std::array<Record,block_size>;
  Record& writable_slot(std::size_t slot) {
    auto& block = records_.at(slot / block_size);
    if (!block.unique()) {
      block = std::make_shared<Block>(*block);
      ++copied_blocks_;
    }
    return block->at(slot % block_size);
  }
  Record* mutable_get(Id id) {
    if (!get(id)) return nullptr;
    return &writable_slot(static_cast<std::size_t>((id & slot_mask)-1));
  }
  Record& record(Id id) {
    auto* r = mutable_get(id);
    if (!r) throw std::logic_error("stale parent service reference");
    return *r;
  }
  const Record& record(Id id) const {
    auto* r = get(id);
    if (!r) throw std::logic_error("stale parent service reference");
    return *r;
  }
  struct Expiry {
    std::uint64_t time;
    Id id;
    bool operator>(const Expiry& b) const {
      return time > b.time || (time == b.time && id > b.id);
    }
  };
  static bool expiry_less(const Expiry& a,const Expiry& b) { return b>a; }
  template<class T>
  static IndexCost index_heap_cost(const ServiceSnapshotHeap<T>& h) {
    const auto& c=h.cost();
    return {c.leaves,c.directories,c.bytes};
  }
  static std::uint64_t completion(const Record& r) {
    return std::max({r.data_ready,r.controller_ready,
        r.consumer == Role::kStore ? r.write_ready : 0});
  }
  std::uint64_t release(Id id,std::size_t level) const {
    return id ? record(id).leases[level].release : 0;
  }
  void collect(Id id) {
    auto* r = mutable_get(id);
    if (r && !r->indexed && !r->pins && !r->leases[0].indexed && !r->leases[1].indexed &&
        !r->owned[0].indexed && !r->owned[1].indexed) {
      r->id = none;
      free_.push_back(static_cast<std::size_t>((id & slot_mask)-1));
      --active_;
    }
  }
  std::size_t capacity_ = 16384, peak_ = 0, active_ = 0;
  Id next_ = 0;
  std::uint64_t watermark_ = 0;
  std::size_t slots_ = 0;
  std::uint64_t copied_blocks_ = 0;
  std::vector<std::shared_ptr<Block>> records_;
  ServiceSnapshotVector<std::size_t> free_;
  ServiceSnapshotLineIndex lines_;
  std::array<ServiceSnapshotHeap<Id>,2> releases_;
  std::array<ServiceSnapshotHeap<Id>,2> owned_releases_;
  std::array<std::size_t,2> owned_capacity_{65536,65536};
  std::array<std::uint64_t,2> owned_last_begin_{};
  ServiceSnapshotHeap<Expiry> expiry_;
  IndexCost expired_index_cost_;
};

struct UnifiedParentCounters {
  std::uint64_t requests=0, ordinary_reads=0, services=0, certified_services=0;
  std::uint64_t followers=0, waits=0, wait_cycles=0, max_wait_cycles=0;
  std::uint64_t future_fallbacks=0, source_fallbacks=0, special_fallbacks=0;
  std::uint64_t capacity_fallbacks=0, carried=0, max_records=0;
  std::uint64_t prepare_fetches=0, prepare_upgrades=0, prepare_no_transaction=0;
  std::uint64_t prepare_unknown=0, prepare_attaches=0, store_takeovers=0;
  std::uint64_t controller_waits=0, controller_wait_cycles=0, data_waits=0, data_wait_cycles=0;
  std::uint64_t legacy_coupled_services=0, credit_releases=0, copied_record_blocks=0;
  std::uint64_t split_services=0, resource_source_fallbacks=0;
  std::uint64_t tbe_allocations=0, fill_allocations=0;
  static constexpr std::size_t source_decisions =
      static_cast<std::size_t>(ParentServicePool::SourceDecision::kCount);
  using SourceLedger = std::array<std::array<std::array<std::uint64_t,source_decisions>,6>,3>;
  // New source candidates, not hint scans: direct/dropped mandatory sources
  // have Store role. Existing transient attachments do not create a source.
  SourceLedger source_ledger{};
  std::uint64_t copied_index_pages=0, copied_index_directories=0, copied_index_bytes=0;
  std::uint64_t line_index_rebuilds=0;
  std::uint64_t hit_fast_returns=0;
  void source_decision(ParentServicePool::Role role,ParentServicePool::Action action,
                       ParentServicePool::SourceDecision decision) {
    ++source_ledger[static_cast<std::size_t>(role)][static_cast<std::size_t>(action)]
        [static_cast<std::size_t>(decision)];
  }
  UnifiedParentCounters& operator+=(const UnifiedParentCounters& b) {
    requests+=b.requests; ordinary_reads+=b.ordinary_reads;
    services+=b.services; certified_services+=b.certified_services;
    followers+=b.followers; waits+=b.waits; wait_cycles+=b.wait_cycles;
    max_wait_cycles=std::max(max_wait_cycles,b.max_wait_cycles);
    future_fallbacks+=b.future_fallbacks; source_fallbacks+=b.source_fallbacks;
    special_fallbacks+=b.special_fallbacks; capacity_fallbacks+=b.capacity_fallbacks;
    carried+=b.carried; max_records=std::max(max_records,b.max_records);
    prepare_fetches+=b.prepare_fetches; prepare_upgrades+=b.prepare_upgrades;
    prepare_no_transaction+=b.prepare_no_transaction; prepare_unknown+=b.prepare_unknown;
    prepare_attaches+=b.prepare_attaches; store_takeovers+=b.store_takeovers;
    controller_waits+=b.controller_waits; controller_wait_cycles+=b.controller_wait_cycles;
    data_waits+=b.data_waits; data_wait_cycles+=b.data_wait_cycles;
    legacy_coupled_services+=b.legacy_coupled_services; credit_releases+=b.credit_releases;
    copied_record_blocks+=b.copied_record_blocks;
    split_services+=b.split_services; resource_source_fallbacks+=b.resource_source_fallbacks;
    tbe_allocations+=b.tbe_allocations; fill_allocations+=b.fill_allocations;
    copied_index_pages+=b.copied_index_pages;
    copied_index_directories+=b.copied_index_directories;
    copied_index_bytes+=b.copied_index_bytes;
    line_index_rebuilds+=b.line_index_rebuilds;
    hit_fast_returns+=b.hit_fast_returns;
    for (std::size_t r=0;r<3;++r)
      for (std::size_t a=0;a<6;++a)
        for (std::size_t d=0;d<source_decisions;++d)
          source_ledger[r][a][d]+=b.source_ledger[r][a][d];
    return *this;
  }
};
}  // namespace fastsim
