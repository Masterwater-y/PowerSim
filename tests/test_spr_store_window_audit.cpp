#include "fastsim/spr_store_window.hpp"
#include <iostream>
#include <stdexcept>

static void check(bool ok,const char* why) { if(!ok)throw std::runtime_error(why); }
int main() {
    try {
        fastsim::SprStoreWindow window(2,2);
        const auto a=window.reserve(1,10);
        window.complete(a,80,{101,10,1,4,70,0,9});
        const auto b=window.reserve(2,10);
        window.complete(b,50,{102,10,2,1,40});
        const auto before=window.inspect(3,12);
        check(before.capacity_wait==38 && before.blocker.sequence==102 &&
              before.blocker.role==2 && before.blocker.level==1 &&
              before.blocker.send==10 && before.blocker.service==40,
              "full window must identify the earliest releasing owner");
        const auto admitted=window.reserve(3,12);
        check(admitted.admission==50,"inspection must not change admission");
        window.complete(admitted,55,{103,50,3,0,5});
        const auto same=window.inspect(1,51);
        check(same.capacity_wait==0,"same-line follower must not require a new line slot");
        const auto parent=window.origin_for_frontier(80);
        check(parent.sequence==101 && parent.role==1 && parent.eligible==9 && parent.release==80,
              "frontier identifies its extending store and timing");
        window.barrier_until(100,{1,true,parent});
        const auto barrier=window.inspect(4,60);
        check(barrier.barrier_wait==40 && barrier.barrier_marginal_wait==40 && barrier.barrier.kind==1 &&
              barrier.barrier.inherited && barrier.barrier.parent.sequence==101,
              "barrier preserves source and inherited predecessor");
        const auto delayed=window.reserve(4,60);
        window.complete(delayed,105,{104,100,1,0,5});
        const auto masked=window.inspect(5,61);
        check(masked.barrier_wait==39 && masked.barrier_marginal_wait==0,
              "prior send can fully mask the same barrier");
        std::cout << "store window audit passed\n";
    } catch(const std::exception& e) {std::cerr<<e.what()<<'\n';return 1;}
}
