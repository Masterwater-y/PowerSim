#pragma once

#include "fastsim/spr_mechanism_profile.hpp"
#include <deque>
#include <map>
#include <optional>

namespace fastsim::spr {

enum class SequencerKind { Read, Ifetch, Write, Atomic, Prefetch, Locked, Uncacheable };
struct SequencerRequest {
    PendingOwner owner{};
    std::uint64_t address = 0;
    SequencerKind kind = SequencerKind::Read;
    bool ordered_store = false;
    std::uint64_t asid = 0; // provenance only; physical line is the alias key
    std::uint64_t model_accept_cycle = 0; // overwritten by grant, never a source timing input
};
enum class SequencerStatus { NewService, Follower, Full, Unsupported, Duplicate, WrongCore, Stale, BackendRefused };
struct SequencerAdmission {
    SequencerRequest request;
    SequencerStatus status = SequencerStatus::Unsupported;
    std::uint64_t revision = 0;
};
struct SequencerService {
    PendingOwner owner{};
    std::uint64_t line = 0;
    std::uint64_t generation = 0;
    SequencerKind kind = SequencerKind::Read;
    std::uint64_t earliest_cycle = 0;
    std::uint64_t entry_overlap = 0; // only native ordered-store retry/wake paths
};
// Aligned byte addresses. cache_address is the configured modeled namespace;
// routed_address is real RAM backing, never a replacement cache identity.
struct InstructionAddress {
    std::uint64_t address_space_id=0, virtual_block_address=0, code_identity=0;
    std::uint64_t cache_address=0, routed_address=0;
    bool modeled=true;
    friend bool operator==(const InstructionAddress& a,const InstructionAddress& b) {
        return a.address_space_id==b.address_space_id && a.virtual_block_address==b.virtual_block_address &&
            a.code_identity==b.code_identity && a.cache_address==b.cache_address && a.routed_address==b.routed_address && a.modeled==b.modeled;
    }
};
struct NativeInstructionRequest { SequencerService service; InstructionAddress address; };
enum class SequencerActionKind { Issued, FollowerAccepted, Completed, ReissueReady, Parked };
struct SequencerAction {
    SequencerActionKind kind;
    PendingOwner owner;
    SequencerService service;
};
struct SequencerUpdate {
    SequencerStatus status = SequencerStatus::Stale;
    std::vector<SequencerAction> actions;
    bool callback_accepted = false;
    bool wrote = false; // only a completed Write authorizes dirty publication
};
enum class SequencerCallback { Read, Write };

// One instance per CPU Sequencer. Prepare ownership belongs to StoreBuffer.
// peek is pure; grant follows backend acceptance in the same serialized event.
// backend_accepted applies only to NewService; a Follower needs no backend send.
// ReissueReady is a proposal, made active only by grant_service. Callback kind
// comes from the model backend, never native diagnostic timing/outcome inputs.
class OnlineSequencer {
public:
    OnlineSequencer(std::uint32_t core, const ResourceLimits&, std::uint64_t line_bytes = 64);
    SequencerAdmission peek(const SequencerRequest&) const;
    SequencerUpdate grant(const SequencerAdmission&, bool backend_accepted,
                          std::uint64_t cycle);
    std::optional<SequencerService> peek_service() const;
    bool grant_service(const SequencerService&, bool backend_accepted, std::uint64_t cycle);
    // Backend data/permission was invalidated before callback. Keep every
    // ordinary owner/credit; retry through a fresh service generation.
    SequencerUpdate retry_service(const SequencerService&, std::uint64_t cycle);
    SequencerUpdate callback(const SequencerService&, SequencerCallback,
                             std::uint64_t cycle, bool no_coales = false);
    std::size_t outstanding() const { return outstanding_; }
    std::size_t live_lines() const { return lines_.size(); }
    std::uint64_t accepted() const { return accepted_; }
    std::uint64_t released() const { return released_; }
    void validate() const;
private:
    struct Line {
        std::deque<SequencerRequest> requests;
        SequencerService service;
        bool inflight = false;
        bool parked = false;
        std::uint64_t admitted_cycle = 0;
    };
    std::uint32_t core_;
    std::size_t capacity_;
    std::uint64_t line_bytes_;
    std::uint64_t revision_ = 0, next_service_ = 1;
    std::uint64_t accepted_ = 0, released_ = 0;
    std::size_t outstanding_ = 0;
    std::map<std::uint64_t, Line> lines_;
    std::deque<PendingOwner> ordered_;
    std::optional<std::pair<std::uint64_t, std::uint64_t>> visible_line_; // cycle,line
    void reissue(std::uint64_t line, Line&, std::uint64_t cycle, SequencerUpdate&, bool overlap = false);
    static bool matches(const SequencerService&, const SequencerService&);
};

enum class ProtocolLevel { L1, L2, Llc };
struct ProtocolDemand {
    ProtocolLevel level = ProtocolLevel::L1;
    bool tbe = false;
    bool data_fill = false;
};
struct ProtocolLease {
    PendingOwner owner{};
    std::uint64_t service_generation = 0;
    ProtocolDemand demand;
};
enum class ProtocolStatus { Ready, TbeFull, FillFull, Duplicate, Invalid };

// A private L1/L2 domain and one LLC bank domain. Callers must route LLC leases
// to a shared bank instance; never clone LLC capacity per CPU.
// No hint/request accounting here. Charges are explicit protocol transitions.
class ProtocolCredits {
public:
    explicit ProtocolCredits(const ResourceLimits&);
    ProtocolStatus peek(const ProtocolLease&) const;
    bool acquire(const ProtocolLease&);
    bool release(const ProtocolLease&);
    std::size_t tbes(ProtocolLevel) const;
    std::size_t fills(ProtocolLevel) const;
    std::size_t live_leases() const { return leases_.size(); }
    void validate() const;
private:
    ResourceLimits limits_;
    std::vector<ProtocolLease> leases_;
};

} // namespace fastsim::spr
