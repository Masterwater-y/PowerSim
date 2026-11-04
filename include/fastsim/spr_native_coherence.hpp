#pragma once
#include "fastsim/config.hpp"
#include "fastsim/cache.hpp"
#include "fastsim/spr_sequencer.hpp"
#include <functional>
#include <memory>
#include <set>

namespace fastsim::spr {
enum class NativeController : std::uint8_t { L0, L1, Llc, Directory };
struct NativeControllerId {
    NativeController kind=NativeController::L0;
    std::uint32_t index=0;
};
enum class NativeLineState : std::uint8_t {
    I,S,E,M,IS,IM,SM,SS,EE,MM,IS_I,S_IL0,E_IL0,M_IL0,SM_IL0,
    NP,MT,ISS,SS_MB,MT_MB,MT_IIB,MT_IB,MT_SB,MI
};
enum class NativeProtocolEvent : std::uint8_t {
    Read,Write,GetS,GetX,Upgrade,Inv,InvElse,ChildAck,ChildData,
    Data,Exclusive,Stale,Ack,Baseline,Unblock,ExclusiveUnblock,Fetch,MemoryData
};
// Controller IDs use [L0 cores, L1 cores, LLC banks, directories]. Transport
// messages are model-generated, never a retired-input/native-outcome channel.
struct NativeProtocolMessage {
    NativeProtocolEvent event=NativeProtocolEvent::Read;
    std::uint32_t from=0,to=0,port=0;
    std::uint64_t line=0,token=0,ready=0,order=0;
    SequencerService service{};
    std::uint64_t data=0;
    bool dirty=false,owner_data=false;
    std::set<std::uint32_t> acknowledgers;
};
// The adapter owns permission/transaction state; every tag/data operation goes
// through the production PrivateHierarchy/SharedSystem storage hooks below.
// There is no second cache, directory model, ordinary table or DRAM calendar.
struct NativeCoherenceHooks {
    std::function<NativeCacheAllocation(NativeControllerId,std::uint64_t)> allocation;
    std::function<void(NativeControllerId,std::uint64_t,std::uint64_t)> reserve;
    std::function<void(NativeControllerId,std::uint64_t,std::uint64_t,bool)> install;
    // Private data-arrival action is separate from permission/lease completion.
    std::function<void(NativeControllerId,std::uint64_t,std::uint64_t,bool)> update_data;
    std::function<void(NativeControllerId,std::uint64_t,std::uint64_t)> finish_data;
    std::function<void(NativeControllerId,std::uint64_t,std::uint64_t)> invalidate;
    std::function<void(NativeControllerId,std::uint64_t,std::uint64_t,bool)> access;
    std::function<void(std::uint32_t,std::uint64_t,std::uint64_t)> eviction;
    std::function<bool(const SequencerService&,std::uint64_t)> callback;
    std::function<void(std::uint64_t,PendingOwner,std::uint64_t,std::uint64_t)> memory;
    std::function<void(const char*,std::uint64_t,PendingOwner,std::uint64_t,std::uint64_t,std::uint64_t)> audit;
    std::function<void(const NativeProtocolMessage&)> sent; // optional bounded transport observation
};
class NativeCoherence {
public:
    NativeCoherence(const SimulatorConfig&, NativeCoherenceHooks,
                    std::vector<ProtocolCredits*> private_credits,
                    std::vector<ProtocolCredits*> bank_credits);
    ~NativeCoherence();
    NativeCoherence(const NativeCoherence&)=delete;
    NativeCoherence& operator=(const NativeCoherence&)=delete;
    // Ordinary credit is already owned by OnlineSequencer. Mandatory readiness
    // is model-derived; no native observation is accepted by this interface.
    void request(const SequencerService&,std::uint64_t ready);
    void memory_response(std::uint64_t transaction,std::uint64_t ready);
    // Shared transport ingress, used by internally generated sends as well.
    // Rejects past/invalid envelopes before controller state or credits change.
    void receive(NativeProtocolMessage);
    void advance(std::uint64_t cycle);
    std::optional<std::uint64_t> next_cycle() const;
    bool idle() const;
    void validate() const;
private:
    struct State;
    std::unique_ptr<State> state_;
};
} // namespace fastsim::spr
