#pragma once
#include "fastsim/spr_store_input.hpp"
#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <limits>
#include <optional>
#include <stdexcept>
#include <vector>

namespace fastsim {
// Optional, sparse sidecar for ONE CoreChunk. The integration must allocate
// this object only when store facts are enabled; it is not globally allocated
// and this class is not itself wired into Simulator. Entries are keyed by the
// chunk-local ChunkMemoryEvent index (not by UOP, macro or global FST ordinal).
// The producer must append one entry per store event, including nullopt for an
// observed store whose facts are unavailable or ineligible. Other event indices
// have no entry. The caller owns the sidecar for the same lifetime as the chunk
// and must seal it after building the memory vector before any lookup.
//
// This transports only copied functional values. It doesn't schedule a store,
// certify the target producer, represent time, or establish merge ordering.
class SprStoreFactsChunk {
 public:
    enum class Status { kNotStore, kUnknown, kKnown };
    struct Lookup {Status status; SprStoreInput value{};};

    void append(std::uint32_t memory_event_index,
                const std::optional<SprStoreInput>& facts) {
        if(sealed_) throw std::logic_error("store facts chunk is sealed");
        if(!index_.empty() && memory_event_index<=index_.back().event)
            throw std::invalid_argument("store facts event indices must increase");
        std::uint32_t value_index=kUnknown;
        if(facts) {
            if(values_.size()>=kUnknown)
                throw std::length_error("too many store facts in chunk");
            value_index=static_cast<std::uint32_t>(values_.size());
            values_.push_back(*facts);
        }
        try {index_.push_back(Index{memory_event_index,value_index});}
        catch(...) {if(facts)values_.pop_back();throw;}
    }
    void seal(std::size_t memory_event_count) {
        if(sealed_)throw std::logic_error("store facts chunk already sealed");
        if(memory_event_count>std::numeric_limits<std::uint32_t>::max() ||
           (!index_.empty() && index_.back().event>=memory_event_count))
            throw std::invalid_argument("store facts index outside chunk");
        event_count_=static_cast<std::uint32_t>(memory_event_count);sealed_=true;
    }
    Lookup lookup(std::uint32_t memory_event_index) const {
        if(!sealed_)throw std::logic_error("store facts chunk is not sealed");
        if(memory_event_index>=event_count_)throw std::out_of_range("store facts event index");
        const auto i=std::lower_bound(index_.begin(),index_.end(),memory_event_index,
            [](const Index& a,std::uint32_t event){return a.event<event;});
        if(i==index_.end() || i->event!=memory_event_index)return {Status::kNotStore,{}};
        if(i->value==kUnknown)return {Status::kUnknown,{}};
        return {Status::kKnown,values_.at(i->value)};
    }
    std::size_t store_count()const{return index_.size();}
    std::size_t known_count()const{return values_.size();}
    // Reuse only after the resident owner has released the entire old chunk.
    // Retain vector capacity; returned Lookup values are independent copies.
    void reset() {
        index_.clear();values_.clear();event_count_=0;sealed_=false;
    }
 private:
    struct Index {std::uint32_t event,value;};
    static constexpr std::uint32_t kUnknown=std::numeric_limits<std::uint32_t>::max();
    std::vector<Index> index_;
    std::vector<SprStoreInput> values_;
    std::uint32_t event_count_=0;
    bool sealed_=false;
};
} // namespace fastsim
