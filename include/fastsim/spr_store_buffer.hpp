#pragma once

#include <cstddef>
#include <cstdint>
#include <functional>
#include <memory>
#include <optional>
#include <vector>

namespace fastsim::spr {

struct BufferProfile {
    std::size_t shared_store_entries = 112;
    std::size_t line_window = 4;
    std::size_t offload_width = 4;
    std::size_t cache_store_ports = 2;
    std::size_t prepare_outstanding = 16; // Native zero sentinel means unlimited.
    std::size_t line_bytes = 64;
    bool early_prepare = true;
};

// Tokens name transactions, not physical lines; generations survive chunk changes.
struct StoreToken { std::uint64_t id = 0, generation = 0; };
struct BufferWriteToken { std::uint64_t id = 0, generation = 0; };
struct PrepareToken { std::uint64_t id = 0, generation = 0; };

enum class StoreKind { Ordinary, NoAccess, Atomic, Split, Uncacheable,
                       StrictlyOrdered, Unsupported };
struct StoreFacts {
    std::uint64_t source_id = 0;
    std::uint64_t context = 0;
    std::uint64_t requestor = 0;
    std::uint64_t flags = 0; // Full functional request flags, used in tail matching.
    std::size_t bytes = 0;
    // Ordinary certifies native isOrdinaryStore eligibility. Caller must reject
    // masked/LLSC/RMW/swap/mem-management/release/local/cache-maintenance,
    // STORE_NO_DATA/all-zero/HTM/barrier/prefetch and missing functional identity.
    StoreKind kind = StoreKind::Unsupported;
};

struct WriteCandidate {
    BufferWriteToken token;
    std::uint64_t revision = 0;
    std::uint64_t line = 0;
    std::uint64_t context = 0, requestor = 0, flags = 0;
    std::vector<bool> mask;
    // Zero means untouched. Values are original StoreToken IDs, never fake data.
    std::vector<std::uint64_t> last_writer;
};
struct PrepareCandidate {
    StoreToken store;
    std::uint64_t line = 0;
    std::uint64_t context = 0, requestor = 0;
    // Original functional flags. Backend derives PF_EXCLUSIVE|STORE_PREPARE
    // plus SECURE; it must not copy all original flags onto a hint packet.
    std::uint64_t flags = 0;
};
enum class PrepareOutcome { Accepted, Dropped, Rejected, Stale };
struct PrepareResult {
    PrepareOutcome outcome = PrepareOutcome::Stale;
    PrepareToken token;
};
enum class PrepareTerminal { Ignored, TakenOver, Fill, Cancelled };
enum class StoreEventKind {
    Allocate, AddressReady, DataReady, Publish, Commit, CanWriteback,
    StoreBufferCombined, NoAccessComplete, WriteAccepted, WriteComplete,
    BufferCreditReleased, PrepareAccepted, PrepareDropped, PrepareComplete,
    PermissionInvalidated
};
struct StoreEvent {
    StoreEventKind kind;
    std::uint64_t cycle = 0;
    StoreToken store;
    BufferWriteToken write;
    PrepareToken prepare;
    std::uint64_t source_id = 0, line = 0;
    PrepareTerminal prepare_terminal = PrepareTerminal::Ignored;
};
struct StoreBufferCounters {
    std::uint64_t allocated = 0, allocation_refused = 0;
    std::uint64_t architectural_combined = 0, no_access_complete = 0;
    std::uint64_t offloaded = 0, merged = 0;
    std::uint64_t writes_created = 0, writes_accepted = 0, writes_completed = 0;
    std::uint64_t buffer_credits_released = 0;
    std::uint64_t prepares_accepted = 0, prepares_dropped = 0, prepares_completed = 0;
    std::uint64_t invalid_callbacks = 0;
};

// Own one instance per core; retain it across trace chunks. Caller supplies only
// model cycles and functional facts. This class never calls cache/oracle code.
// Backend acceptance callbacks must not re-enter this object. Event sinks may
// inspect counters/state, but must not mutate the object or throw.
class StoreBuffer {
  public:
    using EventSink = std::function<void(const StoreEvent&)>;
    using WriteAcceptor = std::function<bool(const WriteCandidate&)>;
    using PrepareAcceptor = std::function<bool(const PrepareCandidate&, PrepareToken)>;
    explicit StoreBuffer(BufferProfile profile = {}, EventSink sink = {});
    ~StoreBuffer();
    StoreBuffer(const StoreBuffer&) = delete;
    StoreBuffer& operator=(const StoreBuffer&) = delete;

    // Unsupported kinds and malformed facts throw; lack of capacity returns nullopt.
    std::optional<StoreToken> allocate(const StoreFacts&, std::uint64_t cycle);
    void markAddressReady(StoreToken, std::uint64_t physical_address, std::uint64_t cycle);
    void markDataReady(StoreToken, std::uint64_t cycle);
    void publish(StoreToken, std::uint64_t cycle);
    void commit(StoreToken, std::uint64_t cycle);
    void allowWriteback(StoreToken, std::uint64_t cycle);
    std::size_t offload(std::uint64_t cycle);

    // Pure peek. Failed backend admission changes no request/port/line credit.
    std::optional<WriteCandidate> peekWrite(std::uint64_t cycle) const;
    bool trySendWrite(const WriteCandidate&, std::uint64_t cycle, const WriteAcceptor&);
    bool completeWrite(BufferWriteToken, std::uint64_t cycle);
    std::optional<PrepareCandidate> peekPrepare(std::uint64_t cycle) const;
    PrepareResult trySendPrepare(const PrepareCandidate&, std::uint64_t cycle,
                                 const PrepareAcceptor&);
    bool completePrepare(PrepareToken, PrepareTerminal, std::uint64_t cycle);

    // Invalidation never fabricates completion or frees an accepted request.
    // Permission reuse is owned by the backend; tokens still identify its replies.
    void invalidateLine(std::uint64_t physical_address, std::uint64_t cycle);
    // Explicit owner reincarnation, allowed only with no live stores/writes/hints.
    void advanceGeneration();
    std::size_t used() const;
    std::size_t sqSize() const;
    std::size_t committedSize() const;
    std::size_t inflightLines() const;
    std::size_t outstandingPrepares() const;
    const StoreBufferCounters& counters() const;

  private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

} // namespace fastsim::spr
