#include "fastsim/memory_runtime.hpp"
#include <cassert>
#include <iostream>
#include <stdexcept>
using namespace fastsim;
using namespace fastsim::spr;

template<class F> void rejects(F f) {
    bool yes=false;
    try { f(); } catch(const std::logic_error&) { yes=true; }
    assert(yes);
}
static MemoryEvent event(std::uint64_t seq, SafeKey key) {
    return {PendingOwner{0,seq,0,1},key,MemoryEventKind::Request,0};
}
int main() {
    // An idle host producer is not a proof that its next request is late.
    MemoryRuntime r(2);
    r.enqueue(event(2,{20,2}));
    r.publish(0,SourcePublication::not_before({30,0}));
    assert(!r.pop_ready());
    r.publish(1,SourcePublication::not_before({10,0}));
    assert(!r.pop_ready());
    r.enqueue(event(1,{10,2}));
    r.publish(1,SourcePublication::not_before({30,0}));
    assert(r.pop_ready()->owner.sequence==1);
    assert(r.pop_ready()->owner.sequence==2);
    assert(!r.pop_ready());
    rejects([&]{ r.enqueue(event(3,{9,0})); });

    // Response unlocks a legal successor in the SAME cycle, no guessed +1.
    MemoryRuntime same(1);
    PendingOwner token{0,42,0,1};
    same.publish(0,SourcePublication::blocked_on({{token}}, {10,2}));
    same.enqueue({token,{10,0},MemoryEventKind::Completion,0});
    assert(same.pop_ready()->kind==MemoryEventKind::Completion);
    same.enqueue(event(43,{10,2}));
    same.publish(0,SourcePublication::not_before({11,0}));
    auto send=same.pop_ready();
    assert(send && send->key.cycle==10 && send->key.position==2);
    // Real zero-extra-cycle causal chains consume states, not target cycles.
    for(unsigned i=0;i<8;++i) {
        const SafeKey key{10,3+i};
        same.enqueue(event(50+i,key));
        assert(same.pop_ready()->key==key);
    }
    rejects([&]{ same.publish(0,SourcePublication::not_before({10,1})); });

    // Candidate equality does not close a hardware arbitration group.
    MemoryRuntime arbitration(2);
    arbitration.enqueue(event(1,{3,2}));
    arbitration.publish(0,SourcePublication::not_before({4,0}));
    arbitration.publish(1,SourcePublication::not_before({3,2}));
    assert(!arbitration.pop_ready());
    arbitration.enqueue(event(2,{3,2}));
    arbitration.publish(1,SourcePublication::finished());
    auto group=arbitration.pop_ready_group();
    assert(group.size()==2 && group[0].key==group[1].key);
    assert(group[0].owner.sequence==1 && group[1].owner.sequence==2);

    // The production cache class stays unchanged until actual admission/fill.
    CacheConfig cfg; cfg.size_bytes=128; cfg.associativity=2; cfg.line_size=64;
    SetAssociativeCache cache(cfg); CacheCounters counters{};
    CacheEventAdapter backend(cache,counters);
    PendingOwner owner{0,90,0,1};
    auto p=backend.prepare(owner,0x40,false);
    assert(!p.lookup.hit && counters.accesses==0 && !cache.contains(0x40));
    auto admitted=backend.admit(p);
    assert(admitted && !admitted->hit && counters.accesses==1);
    assert(!cache.contains(0x40));
    assert(backend.complete(owner));
    assert(cache.contains(0x40) && !backend.complete(owner));
    auto stale=backend.prepare(PendingOwner{0,91,0,1},0x40,false);
    assert(cache.invalidate(0x40));
    const auto before=counters.accesses;
    assert(!backend.admit(stale) && counters.accesses==before);
    // Generation invalidation makes an old fill inert without deleting owners.
    PendingOwner old{0,92,0,1};
    auto pending=backend.prepare(old,0x41,false);
    assert(backend.admit(pending));
    PendingOwner unrelated{0,93,0,1};
    assert(backend.admit(backend.prepare(unrelated,0x42,false)));
    backend.invalidate(0x41);
    assert(backend.complete(unrelated) && cache.contains(0x42));
    assert(!backend.complete(old) && !cache.contains(0x41));
    PendingOwner fresh{0,92,0,2};
    assert(backend.admit(backend.prepare(fresh,0x41,false)));
    assert(backend.complete(fresh) && cache.contains(0x41));
    auto historical=backend.prepare(PendingOwner{0,94,0,1},0x45,false);
    for(std::uint64_t line=0x1000; line<0x2000; ++line) backend.invalidate(line);
    assert(backend.pending()==0 && !backend.admit(historical));
    assert(backend.admit(backend.prepare(PendingOwner{0,94,0,2},0x45,false)));
    assert(backend.complete(PendingOwner{0,94,0,2}));
    std::cout << "memory runtime frontier/cache visibility tests passed\n";
}
