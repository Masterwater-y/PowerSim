#pragma once
#include "fastsim/cache.hpp"
#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <stdexcept>
#include <vector>

namespace fastsim {
// Bounded LLC-resident, L2-only data publication. The owner supplies a
// source-ready certificate derived from its own accepted model events; an
// LLC tag alone is not evidence that an outstanding demand has returned.
// The owner must cancel on same-line demand/write/coherence transitions and
// snapshot this object with LLC, private caches, directory and timing state.
class SprLlcResidentPrefetch {
 public:
  enum class Begin { kIssued, kLlcMissing, kUncertified, kAlreadyPrivate, kDuplicate, kCapacity };
  struct Counters {
    std::uint64_t issued=0, completed=0, skipped_llc_missing=0, skipped_uncertified=0,
      skipped_private=0, skipped_duplicate=0, skipped_capacity=0,
      skipped_dirty_victim=0, cancelled_source=0, cancelled_permission=0, cancelled_write=0, cancelled_demand=0;
  };
  explicit SprLlcResidentPrefetch(std::uint32_t cores,std::size_t per_core)
      : cores_(cores),limit_(per_core) {
    if(!cores || !per_core)throw std::invalid_argument("invalid LLC resident prefetch bounds");
    pending_.reserve(static_cast<std::size_t>(cores)*per_core);
  }
  Begin begin(std::uint32_t core,std::uint64_t line,std::uint64_t issue,
              std::uint64_t response,bool source_ready,SetAssociativeCache& llc,
              PrivateHierarchy& private_cache) {
    if(core>=cores_ || response<issue)throw std::invalid_argument("invalid prefetch request");
    check_time(issue);
    const auto source=llc.prepare_lookup(line);
    if(!source.hit){++stats_.skipped_llc_missing;return Begin::kLlcMissing;}
    if(!source_ready){++stats_.skipped_uncertified;return Begin::kUncertified;}
    if(private_cache.prepare_probe_l2(line).hit){++stats_.skipped_private;return Begin::kAlreadyPrivate;}
    std::size_t count=0;
    for(const auto& p:pending_) {
      if(p.core==core)++count;
      if(p.core==core && p.line==line){++stats_.skipped_duplicate;return Begin::kDuplicate;}
    }
    if(count>=limit_){++stats_.skipped_capacity;return Begin::kCapacity;}
    pending_.push_back({core,line,issue,response});++stats_.issued;return Begin::kIssued;
  }
  void cancel_before_write(std::uint64_t line,std::uint64_t now) {
    cancel_line(line,now,true);
  }
  void cancel_line(std::uint64_t line,std::uint64_t now,bool write) {
    check_time(now);
    for(auto it=pending_.begin();it!=pending_.end();) {
      if(it->line!=line){++it;continue;}
      it=pending_.erase(it);if(write)++stats_.cancelled_write;else ++stats_.cancelled_demand;
    }
  }
  template<class Permission,class Publish,class Evict>
  void advance(std::uint64_t now,SetAssociativeCache& llc,
               const std::vector<PrivateHierarchy*>& caches,
               const std::vector<PrivateTransaction*>& tx,Permission&& permission,
               Publish&& publish,Evict&& evict) {
    check_time(now);
    if(caches.size()!=cores_ || tx.size()!=cores_)throw std::invalid_argument("prefetch core table mismatch");
    for(std::size_t i=0;i<pending_.size();) {
      const auto entry=pending_[i];
      if(entry.response>now){++i;continue;}
      auto& cache=*caches.at(entry.core);
      if(!llc.contains(entry.line)) ++stats_.cancelled_source;
      else if(!permission(entry.core,entry.line)) ++stats_.cancelled_permission;
      else if(!cache.l2_prefetch_safe_victim(entry.line)) ++stats_.skipped_dirty_victim;
      else {
        CacheCounters count;
        const auto result=cache.publish_prefetch_fill_l2(entry.line,entry.issue,entry.response,now,true,count,tx.at(entry.core));
        if(result.status!=PrefetchPublicationStatus::kPublished)throw std::logic_error("prefetch publication failed");
        if(result.result.l2_evicted)evict(entry.core,result.result.l2_evicted_line,result.result.l2_evicted_dirty,entry.response);
        publish(entry.core,entry.line);
        ++stats_.completed;
      }
      pending_.erase(pending_.begin()+static_cast<std::ptrdiff_t>(i));
    }
  }
  const Counters& stats()const{return stats_;}
  std::size_t pending()const{return pending_.size();}
  void reset_counters(){stats_=Counters{};initial_pending_=pending_.size();}
  std::size_t initial_pending()const{return initial_pending_;}
 private:
  struct Pending {std::uint32_t core;std::uint64_t line,issue,response;};
  void check_time(std::uint64_t now){if(now<frontier_)throw std::invalid_argument("prefetch time moved backwards");frontier_=now;}
  std::uint32_t cores_;std::size_t limit_;std::uint64_t frontier_=0;std::size_t initial_pending_=0;
  std::vector<Pending> pending_;Counters stats_;
};
} // namespace fastsim
