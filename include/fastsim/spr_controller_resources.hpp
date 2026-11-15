#pragma once
#include <algorithm>
#include <cstdint>
#include <limits>
#include <vector>
namespace fastsim {
// Stage-two resource intervals. Cancelled traffic keeps its reservation until
// its response; copies are rolled back with the cache/service transaction.
class SprControllerResources {
 public:
  enum class Origin {kDemand,kL1Prefetch,kL2Prefetch,kSoftwarePrefetch};
  struct Entry {
    std::uint64_t ticket, release;
    std::uint32_t core;
    bool l1,l2;
    std::uint64_t line=0,issue=0,parent_service=0;
    Origin origin=Origin::kDemand;
    bool invalidated=false;
    std::uint64_t data_ready=0;
    bool data_known=false;
    bool prefetch_service=false;
    std::uint64_t accepted=0;
  };
  struct Limits {std::uint32_t l1_fill=16,l2_fill=48,tbes=256;};
  void configure(Limits limits) {limits_=limits;}
  void expire(std::uint64_t now) {
    entries_.erase(std::remove_if(entries_.begin(),entries_.end(),
      [now](const auto& e){return e.release<=now;}),entries_.end());
  }
  std::uint64_t admission(std::uint32_t core,bool l1,bool l2,std::uint64_t at) const {
    for(;;) {
      std::uint32_t n1=0,n2=0;
      auto next=std::numeric_limits<std::uint64_t>::max();
      for(const auto& e:entries_)if(e.core==core && e.release>at) {
        n1+=e.l1;n2+=e.l2;
        if((l1 && e.l1)||(l2 && e.l2))next=std::min(next,e.release);
      }
      if((!l1 || (n1<limits_.l1_fill && n1<limits_.tbes)) &&
         (!l2 || (n2<limits_.l2_fill && n2<limits_.tbes)))return at;
      at=next;
    }
  }
  void reserve(std::uint64_t ticket,std::uint32_t core,bool l1,bool l2,std::uint64_t response,
               std::uint64_t line=0,std::uint64_t issue=0,Origin origin=Origin::kDemand,
               std::uint64_t parent=0,bool data_known=true,
               std::uint64_t accepted=std::numeric_limits<std::uint64_t>::max()) {
    const bool prefetch=origin==Origin::kL1Prefetch || origin==Origin::kL2Prefetch || prefetch_origin(parent);
    entries_.push_back({ticket,response,core,l1,l2,line,issue,parent,origin,false,response,data_known,prefetch,
        accepted==std::numeric_limits<std::uint64_t>::max()?issue:accepted});
  }
  bool prefetch_origin(std::uint64_t ticket) const {
    if(!ticket)return false;
    for(const auto& entry:entries_)if(entry.ticket==ticket)return entry.prefetch_service;
    return false;
  }
  std::uint64_t parent(std::uint32_t core,std::uint64_t line,std::uint64_t at) const {
    for(auto it=entries_.rbegin();it!=entries_.rend();++it)
      if(it->core==core && it->line==line && it->l2 && !it->invalidated && it->release>at)
        return it->ticket;
    return 0;
  }
  const Entry* data_service(std::uint32_t core,std::uint64_t line,std::uint64_t at,bool lower_only=true) const {
    for(auto it=entries_.rbegin();it!=entries_.rend();++it)
      if(it->core==core && it->line==line && (lower_only?it->l2:(it->l1||it->l2)) &&
         !it->invalidated && it->data_known && it->accepted<=at && it->data_ready>at)
        return &*it;
    return nullptr;
  }
  const Entry* shared_data_service(std::uint64_t line,std::uint64_t at) const {
    for(auto it=entries_.rbegin();it!=entries_.rend();++it)
      if(it->line==line && it->l2 && !it->invalidated && it->data_known &&
         it->issue<=at && it->data_ready>at)return &*it;
    return nullptr;
  }
  const Entry* known_data_service(std::uint32_t core,std::uint64_t line,std::uint64_t at) const {
    for(auto it=entries_.rbegin();it!=entries_.rend();++it)
      if(it->core==core && it->line==line && it->l2 && !it->invalidated && it->data_known &&
         it->issue<=at)return &*it;
    return nullptr;
  }
  void set_targets(std::uint64_t ticket,bool l1,bool l2) {
    for(auto& e:entries_)if(e.ticket==ticket){e.l1=l1;e.l2=l2;return;}
  }
  void invalidate(std::uint64_t line) {
    for(auto& e:entries_)if(e.line==line)e.invalidated=true;
  }
  void finish(std::uint64_t ticket,std::uint64_t response) {
    for(auto& e:entries_)if(e.ticket==ticket){e.release=response;return;}
  }
  void select_data(std::uint64_t ticket,std::uint64_t response) {
    for(auto& e:entries_)if(e.ticket==ticket){e.data_ready=response;e.data_known=true;return;}
  }
  std::uint64_t next_release(std::uint64_t after) const {
    auto next=std::numeric_limits<std::uint64_t>::max();
    for(const auto& e:entries_)if(e.release>after)next=std::min(next,e.release);
    return next;
  }
 private:
  Limits limits_;
  std::vector<Entry> entries_;
};
}
