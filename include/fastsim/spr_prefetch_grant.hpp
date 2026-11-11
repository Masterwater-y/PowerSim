#pragma once
#include <array>
#include <cstddef>
#include <cstdint>
#include <stdexcept>
#include <vector>

namespace fastsim {
// Snapshot-friendly directory-generation summaries ONLY for lines with live
// prefetch owners. This is a safety certificate, not a target cache/TBE or
// directory model. Caller synchronously tracks every accepted model directory
// transition affecting such a line and never evicts a watched line until every
// owner retires. The supplied nonzero generation comes from a global monotone
// allocator outside rollback; inactive/stale callbacks must be rejected by the
// owner's transaction ticket before they can mutate this table.
class SprPrefetchGrantTable {
  public:
    using Bitmap=std::array<std::uint64_t,4>;
    enum class Track { kAdded, kAlreadyTracked, kCapacity };
    enum class Transition { kChanged, kStale, kUntracked };
    enum class Validation { kValid, kRevoked, kUnknown };
    explicit SprPrefetchGrantTable(std::size_t capacity):capacity_(capacity) {
        if(!capacity)throw std::invalid_argument("empty prefetch grant table");
        entries_.reserve(capacity);
    }
    Track track(std::uint64_t line,std::uint64_t generation,const Bitmap& sharers) {
        if(!generation)throw std::invalid_argument("zero prefetch grant generation");
        if(find(line))return Track::kAlreadyTracked;
        if(entries_.size()==capacity_)return Track::kCapacity;
        entries_.push_back(Entry{line,generation,sharers});return Track::kAdded;
    }
    Transition transition(std::uint64_t line,std::uint64_t expected,
                          std::uint64_t generation,const Bitmap& sharers) {
        auto* entry=find(line);
        if(!entry)return Transition::kUntracked;
        if(entry->generation!=expected)return Transition::kStale;
        if(!generation || generation<=expected)
            throw std::invalid_argument("prefetch grant generation did not advance");
        entry->generation=generation;entry->sharers=sharers;return Transition::kChanged;
    }
    Validation validate(std::uint64_t line,std::uint32_t core,std::uint64_t expected) const {
        if(core>=256)throw std::out_of_range("prefetch core exceeds grant bitmap");
        const auto* entry=find(line);
        if(!entry)return Validation::kUnknown;
        if(!expected || entry->generation!=expected ||
           !(entry->sharers[core/64] & (std::uint64_t{1} << (core%64))))
            return Validation::kRevoked;
        return Validation::kValid;
    }
    bool forget(std::uint64_t line) {
        for(std::size_t i=0;i<entries_.size();++i)if(entries_[i].line==line) {
            entries_.erase(entries_.begin()+i);return true;
        }
        return false;
    }
    std::size_t size() const {return entries_.size();}
  private:
    struct Entry {std::uint64_t line,generation;Bitmap sharers;};
    Entry* find(std::uint64_t line) {
        for(auto& entry:entries_)if(entry.line==line)return &entry;
        return nullptr;
    }
    const Entry* find(std::uint64_t line) const {
        for(const auto& entry:entries_)if(entry.line==line)return &entry;
        return nullptr;
    }
    std::size_t capacity_;
    std::vector<Entry> entries_;
};
}
