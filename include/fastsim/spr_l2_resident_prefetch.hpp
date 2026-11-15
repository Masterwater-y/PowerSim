#pragma once
#include "fastsim/cache.hpp"
#include "fastsim/spr_prefetch_input.hpp"
#include "fastsim/spr_prefetch_service.hpp"
#include "fastsim/spr_ruby_prefetcher.hpp"
#include "fastsim/spr_transaction_id.hpp"
#include "fastsim/spr_controller_resources.hpp"
#include <algorithm>
#include <array>
#include <unordered_set>
#include <cstdint>
#include <limits>
#include <functional>
#include <stdexcept>
#include <vector>

namespace fastsim {
// L1 predictor, queue and publication records. The optional lower-service
// adapter supplies LLC/DRAM and parent-service timing; without it the legacy
// private-L2-only path remains available. Owner must call
// advance before each ordered memory event, cancel_line before a write to that
// line on any core, and cancel_eviction after a private-L2 eviction. This narrow
// contract requires non-inclusive shared LLC, no unobserved cache mutations,
// and full restoration together with the shared/private cache transaction.
class SprL2ResidentPrefetch {
 public:
  struct Counters {
    std::uint64_t trained=0,unsupported_input=0,queued=0,unsupported_l2_miss=0,
      already_l1=0,issued=0,completed=0,cancelled_write=0,cancelled_eviction=0,
      cancelled_residency=0,capacity_blocked=0,duplicates=0,queue_overflow=0,
      stream_hits=0,stream_stale=0,stream_replaced=0,stream_consumed_not_used=0,
      suppressed=0,source_unready=0,skipped_dirty_victim=0,
      evictions=0,dirty_writebacks=0,unused_evictions=0,late_followers=0;
  };
  SprL2ResidentPrefetch(std::uint32_t cores,std::size_t host_limit_per_core,
                        std::size_t host_queue_per_core,
                        bool stream_hits=false,std::size_t l1_sets=0,
                        bool per_line_marks=false,bool suppress_issue=false)
      :cores_(cores),limit_(host_limit_per_core),service_(cores*host_limit_per_core),
       pending_by_core_(cores,0),source_ready_(cores),unresolved_sources_(cores),stream_hits_(stream_hits),per_line_marks_(per_line_marks),
       suppress_issue_(suppress_issue) {
    if(!cores || !limit_ || !host_queue_per_core)throw std::invalid_argument("invalid prefetch host bounds");
    if(stream_hits_ && !l1_sets)throw std::invalid_argument("stream hits require L1 sets");
    if(per_line_marks_ && !stream_hits_)throw std::invalid_argument("line marks require stream hits");
    if(stream_hits_ && !per_line_marks_)marks_.assign(cores,std::vector<std::optional<PreparedPrivateLookup>>(l1_sets));
    predictors_.reserve(cores);
    for(std::uint32_t i=0;i<cores;++i)predictors_.emplace_back(true,host_queue_per_core);
  }
  const Counters& stats()const{return stats_;}
  void share_service_ids(const SprTransactionId& ids) {ids_=ids;}
  // Retain trained state and pending requests across warmup/ROI boundaries.
  void reset_counters(){stats_=Counters{};}
  std::size_t pending()const{return service_.size();}
  std::optional<SprPrefetchService::Follower> follower(std::uint32_t core,std::uint64_t line,
                                                      std::uint64_t at) {
    const auto* entry=service_.waiting(core,line,at);
    if(!entry)return std::nullopt;
    const auto result=service_.attach_follower(entry->ticket,core,line,at);
    if(result) {service_.consume(entry->ticket);++stats_.late_followers;}
    return result;
  }
  std::size_t queued_pending()const {
    std::size_t total=0;for(const auto& predictor:predictors_)total+=predictor.queued();return total;
  }
  // A bounded model-time hint: private-cache tags can precede the modeled
  // response. Collisions conservatively retain the later ready edge.
  void note_l2_miss(std::uint32_t core,std::uint64_t line,std::uint64_t now,std::uint64_t ready) {
    auto& slot=source_ready_.at(core)[line % kSourceSlots];
    slot = slot > now ? std::max(slot,ready) : ready;
  }
  void mark_unresolved_source(std::uint32_t core,std::uint64_t line) {
    unresolved_sources_.at(core).insert(line);
  }
  void finish_source_batch() {
    for (auto& lines : unresolved_sources_) lines.clear();
  }
  template<class Evict>
  void advance(std::uint64_t now,const std::vector<PrivateHierarchy*>& caches,
               const std::vector<PrivateTransaction*>& transactions,Evict&& evict) {
    if(caches.size()!=cores_ || transactions.size()!=cores_)throw std::invalid_argument("prefetch core table mismatch");
    service_.complete_through(now,[&](const SprPrefetchService::Entry& entry){
      auto& cache=*caches.at(entry.core);
      if(!shared_services_ && (unresolved_sources_.at(entry.core).count(entry.line) ||
         source_ready_.at(entry.core)[entry.line % kSourceSlots] > now))
        return SprPrefetchService::Commit::kRetry;
      const auto prepared=cache.prepare_probe(entry.line);
      if(prepared.l1.hit) {--pending_by_core_[entry.core];++stats_.completed;return SprPrefetchService::Commit::kPublished;}
      if(!prepared.has_l2 || !prepared.l2.hit) {--pending_by_core_[entry.core];++stats_.cancelled_residency;return SprPrefetchService::Commit::kPermissionLost;}
      CoreCounters scratch;
      const auto result=cache.publish_prefetch_fill(entry.line,entry.issue,*entry.response,now,true,scratch,transactions.at(entry.core));
      stats_.evictions+=scratch.l1d.evictions+scratch.l2.evictions;
      stats_.dirty_writebacks+=scratch.l1d.writebacks+scratch.l2.writebacks;
      stats_.unused_evictions+=scratch.l1d.unused_prefetch_evictions+scratch.l2.unused_prefetch_evictions;
      if(result.status!=PrefetchPublicationStatus::kPublished)throw std::logic_error("ready L2-resident prefetch did not publish");
      if(stream_hits_ && per_line_marks_ && !entry.consumed) {
        if(!cache.mark_l1_prefetched(entry.line,transactions.at(entry.core)))
          throw std::logic_error("completed prefetch not resident for line mark");
      } else if(stream_hits_ && !entry.consumed) {
        auto marker=cache.prepare_probe(entry.line);
        if(!marker.l1.hit || marker.l1.set>=marks_[entry.core].size())throw std::logic_error("prefetch marker geometry mismatch");
        auto& slot=marks_[entry.core][marker.l1.set];
        if(slot)++stats_.stream_replaced;
        slot=marker;
      }
      if(result.result.l2_evicted)evict(entry.core,result.result.l2_evicted_line,result.result.l2_evicted_dirty,*entry.response);
      --pending_by_core_[entry.core];++stats_.completed;return SprPrefetchService::Commit::kPublished;
    });
  }
  // For an accepted same-cycle write, cancel first so an already-due
  // prefetch cannot become visible immediately before the conflicting write.
  template<class Evict>
  void advance_before_write(std::uint64_t line,std::uint64_t now,
               const std::vector<PrivateHierarchy*>& caches,
               const std::vector<PrivateTransaction*>& transactions,Evict&& evict) {
    cancel_line(line,now);
    advance(now,caches,transactions,evict);
  }
  // Consume at most once, immediately before an eligible demand lookup.
  // A set mutation invalidates the certificate, conservatively losing some
  // valid hits but avoiding stale marks after eviction/reinsert or rollback.
  bool consume_stream_hit(std::uint32_t core,std::uint64_t line,PrivateHierarchy& cache,
                          PrivateTransaction* transaction=nullptr) {
    if(!stream_hits_)return false;
    if(core>=cores_)throw std::out_of_range("prefetch core");
    if(per_line_marks_)return cache.consume_l1_prefetched(line,transaction);
    const auto current=cache.prepare_probe(line);
    if(current.l1.set>=marks_[core].size())throw std::logic_error("prefetch marker geometry mismatch");
    auto& marker=marks_[core][current.l1.set];
    if(!marker || marker->l1.tag_line!=line)return false;
    const bool valid=cache.can_commit(*marker) && current.l1.hit;
    marker.reset();
    if(!valid)++stats_.stream_stale;
    return valid;
  }
  // A certificate consumed before replay did not result in the eligible
  // L1 demand service required for continuation; no native hit is implied.
  void note_stream_consumed_not_used() {++stats_.stream_consumed_not_used;}
  void observe(std::uint32_t core,const SprPrefetchInput& input,HitLevel demand_level,
               std::uint64_t now,std::uint64_t l2_latency,
               PrivateHierarchy& cache,PrivateTransaction* transaction,bool stream_hit=false,
               const std::function<std::optional<std::uint64_t>(std::uint64_t)>& lower_service={},
               SprControllerResources* resources=nullptr,bool late_follow=false) {
    if(core>=cores_)throw std::out_of_range("prefetch core");
    if(input.status!=SprPrefetchInputStatus::kEligible || demand_level==HitLevel::kUnknown) {
      ++stats_.unsupported_input;return;
    }
    auto& predictor=predictors_[core];
    const auto queued_before=predictor.queued();
    const auto duplicates_before=predictor.duplicate_candidates();
    const auto overflow_before=predictor.overflow_candidates();
    if(demand_level!=HitLevel::kL1)predictor.observe_data_miss(input.physical_address,now);
    predictor.observe_data_access(input.pc,input.context,false,input.virtual_address,input.physical_address);
    if(stream_hits_ && ((stream_hit && demand_level==HitLevel::kL1) || late_follow)) {
      predictor.observe_prefetch_access(input.physical_address,now);
      if(!late_follow)++stats_.stream_hits;
    }
    ++stats_.trained;
    stats_.queued+=predictor.queued()-queued_before;
    stats_.duplicates+=predictor.duplicate_candidates()-duplicates_before;
    stats_.queue_overflow+=predictor.overflow_candidates()-overflow_before;
    issue_queued(core,now,l2_latency,cache,transaction,lower_service,resources);
  }
  void issue_queued(std::uint32_t core,std::uint64_t now,std::uint64_t l2_latency,
               PrivateHierarchy& cache,PrivateTransaction* transaction,
               const std::function<std::optional<std::uint64_t>(std::uint64_t)>& lower_service={},
               SprControllerResources* resources=nullptr) {
    shared_services_=shared_services_ || static_cast<bool>(lower_service);
    while(predictors_[core].queued()) {
      const auto before=predictors_[core].queued();
      issue_one(core,now,l2_latency,cache,transaction,lower_service,resources);
      if(predictors_[core].queued()==before)break;
    }
  }
  std::optional<std::uint64_t> next_response(std::uint64_t after) const {
    return service_.next_response(after);
  }
  void issue_one(std::uint32_t core,std::uint64_t now,std::uint64_t l2_latency,
               PrivateHierarchy& cache,PrivateTransaction* transaction,
               const std::function<std::optional<std::uint64_t>(std::uint64_t)>& lower_service,
               SprControllerResources* resources) {
    auto& predictor=predictors_[core];
    const auto candidate=predictor.peek();if(!candidate)return;
    // Paired diagnostic: preserve demand order and predictor training while
    // dropping each candidate before it can touch cache or service resources.
    if(suppress_issue_) {predictor.dequeue();++stats_.suppressed;return;}
    const auto line=*candidate/64;
    const auto prepared=cache.prepare_probe(line);
    if(prepared.l1.hit) {predictor.dequeue();++stats_.already_l1;return;}
    const bool source_busy=resources ? resources->data_service(core,line,now)!=nullptr :
        source_ready_.at(core)[line % kSourceSlots]>now;
    const bool resident=prepared.has_l2 && prepared.l2.hit && !source_busy &&
        !unresolved_sources_.at(core).count(line);
    if(!resident && !lower_service) {predictor.dequeue();++stats_.unsupported_l2_miss;return;}
    if(!lower_service && (unresolved_sources_.at(core).count(line) || source_busy)) {predictor.dequeue();++stats_.source_unready;return;}
    if(pending_by_core_[core]>=limit_) {++stats_.capacity_blocked;return;}
    if(resources && resources->admission(core,true,false,now)>now) {++stats_.capacity_blocked;return;}
    if(l2_latency>std::numeric_limits<std::uint64_t>::max()-now)
      throw std::overflow_error("prefetch response overflow");
    const auto ticket=ids_.allocate();
    const auto begun=service_.begin_at(now,ticket,core,line,now,SprPrefetchService::Level::kL1,0);
    if(begun==SprPrefetchService::Begin::kDuplicateLine) {predictor.dequeue();++stats_.duplicates;return;}
    if(begun!=SprPrefetchService::Begin::kAccepted) {++stats_.capacity_blocked;return;}
    auto ready=now;
    if(!resident) {
      const auto lower=lower_service(line);
      if(!lower) {service_.cancel(ticket);++stats_.capacity_blocked;return;}
      ready=*lower;
    }
    CoreCounters scratch;
    if(resident && !cache.commit_probe(prepared,false,scratch,transaction)) {service_.cancel(ticket);++stats_.capacity_blocked;return;}
    service_.select_response(ticket,ready+l2_latency);
    if(resources)resources->reserve(ticket,core,true,false,ready+l2_latency,
        line,now,SprControllerResources::Origin::kL1Prefetch,resources->parent(core,line,now));
    predictor.dequeue();++pending_by_core_[core];++stats_.issued;
  }
  void cancel_line(std::uint64_t line,std::uint64_t now) {
    for(std::uint32_t core=0;core<cores_;++core)cancel(core,line,now,true);
  }
  void cancel_eviction(std::uint32_t core,std::uint64_t line,std::uint64_t now) {
    if(core>=cores_)throw std::out_of_range("prefetch core");cancel(core,line,now,false);
  }
 private:
  void cancel(std::uint32_t core,std::uint64_t line,std::uint64_t now,bool write) {
    const auto* entry=service_.waiting(core,line,now);if(!entry)return;
    const auto ticket=entry->ticket;
    if(!service_.cancel(ticket))throw std::logic_error("prefetch cancellation lost identity");
    --pending_by_core_[core];if(write)++stats_.cancelled_write;else ++stats_.cancelled_eviction;
  }
  std::uint32_t cores_;
  std::size_t limit_;
  SprPrefetchService service_;
  SprTransactionId ids_;
  std::vector<std::size_t> pending_by_core_;
  static constexpr std::size_t kSourceSlots=1024;
  std::vector<std::array<std::uint64_t,kSourceSlots>> source_ready_;
  std::vector<std::unordered_set<std::uint64_t>> unresolved_sources_;
  std::vector<SprRubyPrefetcher> predictors_;
  bool stream_hits_;
  bool per_line_marks_;
  bool suppress_issue_;
  bool shared_services_=false;
  std::vector<std::vector<std::optional<PreparedPrivateLookup>>> marks_;
  Counters stats_;
};
}
