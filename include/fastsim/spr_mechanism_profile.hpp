#pragma once

#include "fastsim/pending_resources.hpp"
#include <array>
#include <cstdint>
#include <stdexcept>
#include <string>
#include <vector>

namespace fastsim::spr {

// A target configuration is not a declaration that the runtime models it.
enum class SupportStatus {
    Modeled, Approximate, Unsupported, DisabledByTarget, MissingFunctionalInput
};

inline const char* support_status_name(SupportStatus status) {
    switch (status) {
    case SupportStatus::Modeled: return "modeled";
    case SupportStatus::Approximate: return "approximate";
    case SupportStatus::Unsupported: return "unsupported";
    case SupportStatus::DisabledByTarget: return "disabled_by_target";
    case SupportStatus::MissingFunctionalInput: return "missing_functional_input";
    }
    throw std::invalid_argument("invalid SPR support status");
}

struct ResourceLimits {
    // Separate populations and release events, even where capacities coincide.
    std::uint32_t ordinary_requests = 0;   // requests, followers included
    std::uint32_t prepare_requests = 0;    // optional hints
    std::uint32_t l1_data_fills = 0;       // data fills, not permission upgrades
    std::uint32_t l2_data_fills = 0;
    std::uint32_t l1_tbes = 0;             // protocol transient entries
    std::uint32_t l2_tbes = 0;
    std::uint32_t llc_tbes = 0;            // per LLC bank
    std::uint32_t shared_stores = 0;       // SQ + committed buffer entries
    std::uint32_t distinct_store_lines = 0; // distinct in-flight cache lines
    std::uint32_t offload_width = 0;       // store entries / core cycle

    void validate() const {
        if (!ordinary_requests || !prepare_requests || !l1_data_fills ||
            !l2_data_fills || !l1_tbes || !l2_tbes || !llc_tbes ||
            !shared_stores || !distinct_store_lines || !offload_width)
            throw std::invalid_argument("SPR resource limits must have positive typed units");
    }
};

struct TargetProfile {
    ResourceLimits limits;
    std::string static_uop_policy_sha256;
    // Importer proof (or explicitly declared manual target provenance): exact
    // integer resources, interval1, completion/PRF0, empty ResultForwarding.
    bool integer_nominal_shape_verified = false;
    std::array<std::uint32_t, 3> iq_entries{}; // execution, memory, store-data allocation slots
    bool enabled = false;
    bool store_buffer = false;
    bool early_store_prepare = false;
    bool load_bank_conflicts = false;
    bool decoupled_frontend = false;
    bool response_throttling = false;
    std::uint32_t load_banks = 0;
    std::uint32_t load_bank_ports = 0;
    std::uint32_t cache_load_ports = 0;
    std::uint32_t cache_store_ports = 0;
    SupportStatus support = SupportStatus::Unsupported;

    void validate() const {
        if (!enabled) return;
        limits.validate();
        for (auto capacity : iq_entries)
            if (!capacity) throw std::invalid_argument("SPR IQ allocation slots must be positive");
        if (!cache_load_ports || !cache_store_ports || !load_banks || !load_bank_ports)
            throw std::invalid_argument("SPR ports and load-bank units must be positive");
        if (support != SupportStatus::Modeled && support != SupportStatus::Approximate)
            throw std::invalid_argument("SPR target runtime support is undeclared or unsupported");
        if (decoupled_frontend || response_throttling)
            throw std::invalid_argument("SPR FTQ/ReadResp active target subdomain is unsupported");
        if (early_store_prepare && !store_buffer)
            throw std::invalid_argument("SPR early prepare requires the native store-buffer profile");
    }
};

// Optional structural audit ledger, deliberately independent of timing counters.
// A caller keeps it for a bounded audit population; it is not a request allocator.
// Every relation uses the existing owner identity, including generation/core.
enum class Population : std::uint8_t {
    RetiredMacro, OriginalStore, SyntheticWrite, PrepareHint,
    OrdinaryRequest, Follower, PhysicalService
};

class PopulationLedger {
public:
    void add_root(Population kind, PendingOwner owner) {
        if (kind != Population::RetiredMacro)
            throw std::invalid_argument("only retired macro population can be a ledger root");
        add_node(kind, owner);
    }

    void add(Population kind, PendingOwner owner,
             Population parent_kind, PendingOwner parent) {
        if (!allowed(kind, parent_kind))
            throw std::invalid_argument("invalid SPR population relation");
        const auto parent_index = find(parent_kind, parent);
        // Validate before insertion: a rejected call leaves every count intact.
        if (contains(kind, owner))
            throw std::invalid_argument("duplicate SPR population owner");
        nodes_.push_back({kind, owner});
        edges_.push_back({nodes_.size() - 1, parent_index});
    }

    void relate(Population kind, PendingOwner owner,
                Population parent_kind, PendingOwner parent) {
        if (!allowed(kind, parent_kind))
            throw std::invalid_argument("invalid SPR population relation");
        const auto child_index = find(kind, owner);
        const auto parent_index = find(parent_kind, parent);
        for (const auto& edge : edges_)
            if (edge.child == child_index && edge.parent == parent_index)
                throw std::invalid_argument("duplicate SPR population relation");
        edges_.push_back({child_index, parent_index});
    }

    std::size_t count(Population kind) const {
        std::size_t n = 0;
        for (const auto& node : nodes_) n += node.kind == kind;
        return n;
    }

    void validate() const {
        for (std::size_t index = 0; index < nodes_.size(); ++index) {
            if (nodes_[index].kind == Population::RetiredMacro) continue;
            bool mapped = false;
            for (const auto& edge : edges_) {
                if (edge.child >= nodes_.size() || edge.parent >= nodes_.size())
                    throw std::invalid_argument("dangling SPR population mapping");
                mapped |= edge.child == index;
            }
            if (!mapped) throw std::invalid_argument("unmapped SPR population member");
        }
    }

private:
    struct Node { Population kind; PendingOwner owner; };
    struct Edge { std::size_t child; std::size_t parent; };
    std::vector<Node> nodes_;
    std::vector<Edge> edges_;

    static bool allowed(Population kind, Population parent) {
        switch (kind) {
        case Population::OriginalStore: return parent == Population::RetiredMacro;
        case Population::SyntheticWrite:
        case Population::PrepareHint: return parent == Population::OriginalStore;
        case Population::OrdinaryRequest:
            return parent == Population::OriginalStore || parent == Population::SyntheticWrite;
        case Population::Follower: return parent == Population::OrdinaryRequest;
        case Population::PhysicalService:
            return parent == Population::OrdinaryRequest || parent == Population::Follower;
        case Population::RetiredMacro: return false;
        }
        return false;
    }

    bool contains(Population kind, PendingOwner owner) const {
        for (const auto& node : nodes_)
            if (node.kind == kind && node.owner == owner) return true;
        return false;
    }

    void add_node(Population kind, PendingOwner owner) {
        if (contains(kind, owner))
            throw std::invalid_argument("duplicate SPR population owner");
        nodes_.push_back({kind, owner});
    }

    std::size_t find(Population kind, PendingOwner owner) const {
        for (std::size_t i = 0; i < nodes_.size(); ++i)
            if (nodes_[i].kind == kind && nodes_[i].owner == owner) return i;
        throw std::invalid_argument("dangling SPR population owner/generation mapping");
    }
};

} // namespace fastsim::spr
