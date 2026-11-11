#pragma once
#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <limits>
#include <optional>
#include <stdexcept>
#include <vector>
#include "fastsim/spr_transaction_id.hpp"
namespace fastsim {
// Per-core accounting only. Caller chooses predicted arrival and completion
// edges and separately models controller TBE, fill and line-service behavior.
// Every ordinary demand alias occupies one ordinary credit and completes in
// line order. HTM abort, locked-RMW and SW-prefetch skip need separate paths.
class SprSequencerCredits {
 public:
    struct Demand {std::uint64_t id; bool alias;};
    SprSequencerCredits(std::size_t demand_capacity,std::size_t prepare_capacity)
      :demand_capacity_(demand_capacity),prepare_capacity_(prepare_capacity) {
        if(!demand_capacity) throw std::invalid_argument("zero demand credit capacity");
        demand_.reserve(demand_capacity);prepare_.reserve(prepare_capacity);
    }
    std::optional<Demand> admit_demand(std::uint64_t line) {
        if(demand_.size()>=demand_capacity_)return std::nullopt;
        const bool alias=std::any_of(demand_.begin(),demand_.end(),
            [line](const auto& entry){return entry.line==line;});
        const auto id=ids_.allocate();demand_.push_back({id,line});return Demand{id,alias};
    }
    // A full prepare budget drops the optional hint; it does not stall demand.
    // Zero follows gem5's convention of unlimited prepare budget.
    std::optional<std::uint64_t> admit_prepare() {
        if(prepare_capacity_ && prepare_.size()>=prepare_capacity_)return std::nullopt;
        const auto id=ids_.allocate();prepare_.push_back(id);return id;
    }
    void complete_demand(std::uint64_t id) {
        auto it=std::find_if(demand_.begin(),demand_.end(),[id](const auto& e){return e.id==id;});
        if(it==demand_.end())throw std::logic_error("unknown demand credit");
        if(std::any_of(demand_.begin(),it,[&](const Entry& older){return older.line==it->line;}))
            throw std::logic_error("same-line demand completion is not FIFO");
        demand_.erase(it);
    }
    void complete_prepare(std::uint64_t id) {
        auto it=std::find(prepare_.begin(),prepare_.end(),id);
        if(it==prepare_.end())throw std::logic_error("unknown prepare credit");
        prepare_.erase(it);
    }
    std::size_t demand_count()const{return demand_.size();}
    std::size_t prepare_count()const{return prepare_.size();}
    std::size_t line_count()const {
        std::size_t count=0;
        for(auto it=demand_.begin();it!=demand_.end();++it)
            if(std::none_of(demand_.begin(),it,[&](const auto& e){return e.line==it->line;}))++count;
        return count;
    }
 private:
    struct Entry {std::uint64_t id,line;};
    std::size_t demand_capacity_,prepare_capacity_;
    SprTransactionId ids_;
    std::vector<Entry> demand_;
    std::vector<std::uint64_t> prepare_;
};
}
