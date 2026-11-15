#include "fastsim/bounded_store_timing.hpp"
#include "fastsim/cache.hpp"
#include <iostream>
#include <stdexcept>

using namespace fastsim;
static void check(bool ok, const char* why) { if (!ok) throw std::runtime_error(why); }

int main() {
    try {
        BoundedStoreLifecycle lifecycle(2, 1, 2, 1, 2);
        const BoundedStoreIdentity first{0, 10, 0};
        const BoundedStoreIdentity second{0, 11, 0};
        check(lifecycle.prepare(first, 100, 10, 30), "first prepare must be accepted");
        check(!lifecycle.prepare(second, 200, 11, 31), "full hint credit drops only prepare");
        check(lifecycle.next_visible(100, 29) == 30, "another core must not observe an unfinished prepare");
        check(lifecycle.next_visible(100, 30) == 30, "ready permission stays hidden until publication");
        auto snapshot = lifecycle;
        const auto h1 = lifecycle.head(first, 100, 12, 5);
        const auto h2 = lifecycle.head(second, 200, 13, 5);
        check(h1.send >= 12 && h1.response >= h1.send && h1.release >= h1.response,
              "first head has distinct valid edges");
        check(h2.send >= 13 && h2.response >= h2.send && h2.release >= h2.response,
              "dropped hint must still produce mandatory head");
        check(h2.response < h1.response && h2.release >= h1.release,
              "younger callbacks cannot reclaim ordered store entries first");
        check(lifecycle.stats().heads == 2 && lifecycle.stats().prepare_dropped == 1,
              "mandatory population and hint drop are independent");
        lifecycle = snapshot;
        check(lifecycle.stats().heads == 0 && lifecycle.next_visible(100, 29) == 30,
              "transaction restore includes state and counters");
        lifecycle.invalidate(100, 20);
        check(lifecycle.next_visible(100, 19) == 30, "later invalidation must not rewrite earlier model time");
        check(!lifecycle.next_visible(100, 20), "invalidation removes stale permission generation");
        lifecycle.advance(30);
        check(lifecycle.prepare(second, 200, 30, 31), "completed hint returns its credit across batches");
        BoundedStoreLifecycle shared_demand(1, 1, 1, 1, 2);
        check(shared_demand.demand(0, 0, 40).response == 40, "ordinary demand reserves shared credit");
        check(shared_demand.head({0, 1, 0}, 64, 1, 2).send == 40,
              "head must share demand credit with ordinary loads");
        BoundedStoreLifecycle line_window(1, 1, 3, 2, 1);
        check(line_window.head({0, 1, 0}, 64, 0, 30).send == 0, "first line admitted");
        check(line_window.head({0, 2, 0}, 64, 1, 10).send == 1, "same line shares ordered line window");
        check(line_window.head({0, 3, 0}, 65, 2, 1).send == 30, "different line waits for active window");
        BoundedStoreLifecycle retained(1, 1, 2, 1, 2);
        check(retained.prepare({0, 1, 0}, 64, 0, 10), "prepare admitted");
        retained.advance(10);
        const auto ready_generation=retained.generation({0, 1, 0});
        check(ready_generation && retained.publish_prepare({0, 1, 0}, *ready_generation,
            [](std::uint32_t, std::uint64_t, std::uint64_t){return true;}), "ready permission published");
        check(retained.prepare({0, 2, 0}, 65, 10, 20), "completed hint frees credit before its head");
        const auto prepared_head=retained.head({0, 1, 0}, 64, 11, 100);
        check(prepared_head.response == 12 && prepared_head.permission == 10,
              "completed permission survives its credit release until head");
        BoundedStoreLifecycle release_edges(1, 1, 1, 1, 1);
        const auto edges=release_edges.head({0, 1, 0}, 64, 1, 5, 1, 2);
        check(edges.send == 1 && edges.response == 6 && edges.release == 8,
              "callback and SQ release remain distinct edges");
        BoundedStoreLifecycle loss(1, 1, 1, 1, 1);
        check(loss.prepare({0, 1, 0}, 64, 0, 20), "permission request accepted");
        loss.invalidate(64, 10);
        check(loss.head({0, 1, 0}, 64, 5, 30).response == 35,
              "permission lost before response cannot complete the head");
        BoundedStoreLifecycle ids(1, 2, 1, 1, 1);
        check(ids.prepare({0, 1, 0}, 64, 0, 10), "first generation");
        auto saved=ids;
        check(ids.prepare({0, 2, 0}, 65, 1, 11), "speculative generation");
        const auto stale=ids.generation({0, 2, 0});
        ids=saved;
        check(ids.prepare({0, 2, 0}, 65, 1, 11), "same functional store replayed");
        check(stale && ids.generation({0, 2, 0}) && stale != ids.generation({0, 2, 0}),
              "rollback must not recycle a generation");
        BoundedStoreLifecycle publication(2, 1, 1, 1, 1);
        check(publication.prepare({0, 7, 0}, 42, 1, 30), "publication prepare");
        const auto generation=publication.generation({0, 7, 0});
        int visible=0;
        auto install=[&](std::uint32_t, std::uint64_t, std::uint64_t){++visible; return true;};
        publication.advance(29);
        check(generation && !publication.publish_prepare({0, 7, 0}, *generation, install) && visible==0,
              "cache cannot be published before completion");
        const auto checkpoint=publication;
        publication.advance(30);
        check(publication.publish_prepare({0, 7, 0}, *generation, install) && visible==1,
              "cache publishes only at certified completion");
        publication=checkpoint;visible=0;
        check(publication.next_visible(42, 29)==30 && visible==0,
              "rollback restores both pending state and caller cache transaction");
        publication.invalidate(42, 29);
        publication.advance(30);
        check(!publication.publish_prepare({0, 7, 0}, *generation, install) && visible==0,
              "invalidated generation never publishes");
        BoundedStoreLifecycle callbacks(2, 2, 2, 1, 2);
        check(callbacks.prepare({0, 3, 0}, 101, 1, 40), "first cross-batch prepare");
        check(callbacks.prepare({1, 4, 0}, 102, 2, 30), "second cross-batch prepare");
        std::vector<std::uint64_t> completed;
        const auto publish_due = [&](BoundedStoreIdentity, std::uint64_t line,
                                     std::uint64_t, std::uint64_t) {
            completed.push_back(line); return true;
        };
        callbacks.complete_prepares(29, publish_due);
        check(completed.empty(), "no cross-batch completion before response");
        auto pending_checkpoint = callbacks;
        callbacks.complete_prepares(40, publish_due);
        check(completed == std::vector<std::uint64_t>({102, 101}),
              "callbacks follow model response order, not prepare order");
        callbacks = pending_checkpoint;
        completed.clear();
        callbacks.invalidate(102, 29);
        callbacks.complete_prepares(40, publish_due);
        check(completed == std::vector<std::uint64_t>({101}),
              "restored and invalidated pending completion cannot publish");
        BoundedStoreLifecycle ordered_invalidation(1, 1, 1, 1, 1);
        check(ordered_invalidation.prepare({0, 5, 0}, 103, 1, 30), "ordered prepare");
        ordered_invalidation.invalidate(103, 35);
        completed.clear();
        ordered_invalidation.complete_prepares(40, publish_due);
        check(completed == std::vector<std::uint64_t>({103}),
              "a later invalidation must not suppress an earlier callback");
        CacheConfig cache_config;
        cache_config.size_bytes=128;cache_config.associativity=2;cache_config.line_size=64;
        PrivateHierarchy private_cache(cache_config,cache_config);
        CoreCounters scratch;
        BoundedStoreLifecycle cache_lifecycle(2,1,1,1,1);
        check(cache_lifecycle.prepare({0,9,0},42,1,30),"cache prepare admitted");
        const auto cache_generation=cache_lifecycle.generation({0,9,0});
        const auto cache_checkpoint=cache_lifecycle;
        auto cache_transaction=private_cache.begin_transaction();
        auto cache_install=[&](std::uint32_t,std::uint64_t line,std::uint64_t) {
            private_cache.complete_fill(line,false,scratch,&cache_transaction);return true;
        };
        cache_lifecycle.advance(29);
        check(cache_generation && !cache_lifecycle.publish_prepare({0,9,0},*cache_generation,cache_install)
              && !private_cache.contains(42),"real private cache cannot fill before ready edge");
        cache_lifecycle.advance(30);
        check(cache_lifecycle.publish_prepare({0,9,0},*cache_generation,cache_install)
              && private_cache.contains(42),"real private cache fills at ready edge");
        private_cache.restore(cache_transaction);cache_lifecycle=cache_checkpoint;
        check(!private_cache.contains(42) && cache_lifecycle.next_visible(42,29)==30,
              "rollback restores real private cache together with pending lifecycle");
        std::cout << "bounded store resource lifecycle passed\n";
    } catch (const std::exception& e) { std::cerr << e.what() << '\n'; return 1; }
}
