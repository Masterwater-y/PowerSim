#pragma once
#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <optional>
#include <stdexcept>
#include <vector>

namespace fastsim {
// Bounded event summary, not a per-cycle engine or a hardware resource model.
// The caller supplies the service completion and owns all shared credits and
// cache installation; no cache line becomes visible before complete_through.
class SprPrefetchInflight {
  public:
    explicit SprPrefetchInflight(std::size_t capacity):capacity_(capacity) {
        if(!capacity) throw std::invalid_argument("empty prefetch inflight summary");
        entries_.reserve(capacity);
    }
    bool start(std::uint64_t line,std::uint64_t issue,std::uint64_t completion) {
        if(completion<issue)throw std::invalid_argument("prefetch response precedes request");
        if(entries_.size()==capacity_)return false;
        for(const auto& e:entries_)if(e.line==line)return false;
        Entry item{line,issue,completion};
        const auto pos=std::upper_bound(entries_.begin(),entries_.end(),completion,
            [](auto time,const Entry& e){return time<e.completion;});
        entries_.insert(pos,item);return true;
    }
    std::optional<std::uint64_t> pending_until(std::uint64_t line,std::uint64_t at) const {
        for(const auto& e:entries_)if(e.line==line && e.issue<=at && at<e.completion)return e.completion;
        return std::nullopt;
    }
    template<class Callback> void complete_through(std::uint64_t now,Callback&& complete) {
        std::size_t count=0;
        while(count<entries_.size() && entries_[count].completion<=now) {
            const auto item=entries_[count];
            complete(item.line,item.completion);
            ++count;
        }
        entries_.erase(entries_.begin(),entries_.begin()+count);
    }
    std::size_t size() const {return entries_.size();}
  private:
    struct Entry {std::uint64_t line,issue,completion;};
    std::size_t capacity_;
    std::vector<Entry> entries_;
};
}
