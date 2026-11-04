#include "fastsim/spr_ruby_prefetch.hpp"
#include <iostream>
#include <stdexcept>
using namespace fastsim::spr;
namespace {
void check(bool value,const char* what){if(!value)throw std::runtime_error(what);}
PrefetchAccess access(std::uint64_t v,std::uint64_t p,std::uint64_t pc=0x100,int context=0) {
    PrefetchAccess a;a.complete=true;a.physical=p;a.virtual_address=v;a.pc=pc;a.context=context;return a;
}
void tests() {
    RubyPrefetchProfile profile;profile.ip_stride=true;
    {
        RubyPrefetcher pf(profile);
        for(unsigned i=0;i<3;++i)check(pf.observe_data_access(access(0x1000+i*64,0x9000+i*64),i).empty(),"IP must wait four distinct samples");
        auto out=pf.observe_data_access(access(0x10c0,0x90c0),3);
        check(out.size()==1 && out[0].address==0x9100 && out[0].origin==PrefetchOrigin::IpStride,"IP stride translates next virtual offset using current physical page");
        check(pf.observe_data_access(access(0x10c7,0x90c7),4).empty(),"same-line accesses do not train a new stride sample");
        check(pf.stats().ip_candidates==1 && pf.queued_lines()==1,"queue only holds live candidates");
        pf.dequeued(0x9107);check(pf.queued_lines()==0,"dequeue clears aligned dedup membership");
        check(pf.observe_data_access(access(0x1100,0x9100),5).at(0).address==0x9140,"IP stream continues autonomously");
    }
    {
        RubyPrefetcher pf(profile);
        for(unsigned i=0;i<4;++i)pf.observe_data_access(access(0x1f00+i*64,0x9f00+i*64),i);
        check(pf.stats().ip_page_stops==1 && !pf.stats().ip_candidates,"IP must stop at virtual page boundary");
        check(pf.observe_data_access(access(0x2000,0x15000),4).empty(),"new virtual page resets stride training");
    }
    {
        RubyPrefetcher pf(profile);
        for(unsigned i=0;i<3;++i){pf.observe_data_access(access(0x1000+i*64,0x9000+i*64,0x100,0),i);pf.observe_data_access(access(0x2000+i*128,0xa000+i*128,0x100,1),i);}
        check(pf.observe_data_access(access(0x10c0,0x90c0,0x100,0),3).at(0).address==0x9100,"context zero has own IP stride state");
        check(pf.observe_data_access(access(0x2180,0xa180,0x100,1),3).at(0).address==0xa200,"context one has own IP stride state");
    }
    {
        RubyPrefetchProfile p;p.train_misses=4;RubyPrefetcher pf(p);
        for(unsigned i=0;i<4;++i)check(pf.observe_miss(0x1000+i*64,i).empty(),"stream unit filter threshold is four hits after initial insertion");
        auto out=pf.observe_miss(0x1100,4);check(out.size()==1 && out[0].address==0x1140 && out[0].origin==PrefetchOrigin::Stream,"fifth unit miss allocates stream");
        check(pf.observe_miss(0x1140,5).empty(),"native outstanding stream match suppresses new training without inventing issued bits");
        auto next=pf.observe_prefetch_hit(0x1140,6);check(next.size()==1 && next[0].address==0x1180,"prefetch hit extends stream");
        check(pf.stats().hits==1 && pf.stats().streams_allocated==1,"logical populations stay distinct");
    }
    {
        RubyPrefetchProfile p;p.train_misses=1;RubyPrefetcher pf(p);
        check(pf.observe_miss(0x1200,0).empty(),"negative initial");
        check(pf.observe_miss(0x11c0,1).at(0).address==0x1180,"negative unit filter trains separately");
    }
    {
        RubyPrefetchProfile p;p.train_misses=1;RubyPrefetcher pf(p);
        for(unsigned i=0;i<3;++i)check(pf.observe_miss(0x1000+i*128,i).empty(),"nonunit threshold is strict greater, not unit threshold");
        check(pf.observe_miss(0x1180,3).at(0).address==0x1200,"nonunit stride uses byte delta divided by block size");
    }
    {
        RubyPrefetcher pf(profile);
        auto a=pf.observe_adjacent(0x1000,0);check(a.size()==1 && a[0].address==0x1040 && a[0].origin==PrefetchOrigin::Adjacent,"L2 adjacent is xor one line");
        check(pf.observe_adjacent(0x1007,1).empty() && pf.stats().duplicate_candidates==1,"queued dedup is shared across generators");
        pf.dequeued(0x1040);check(pf.observe_adjacent(0x1000,2).size()==1,"dedup ends at dequeue rather than response");
        RubyPrefetchProfile p;p.queue_dedup=false;RubyPrefetcher no(p);
        check(no.observe_adjacent(0x1000,0).size()==1 && no.observe_adjacent(0x1000,0).size()==1,"dedup disabled target keeps duplicates");
    }
    {
        RubyPrefetchProfile p;p.train_misses=1;RubyPrefetcher pf(p);
        pf.observe_miss(0x1f80,0);check(pf.observe_miss(0x1fc0,1).empty(),"stream startup stops at page boundary");
        check(pf.live_streams()==0 && pf.stats().streams_allocated==1,"page stop deallocates newly allocated stream");
        p.cross_page=true;RubyPrefetcher cross(p);cross.observe_miss(0x1f80,0);
        check(cross.observe_miss(0x1fc0,1).at(0).address==0x2000 && cross.stats().pages_crossed==1,"stream cross-page target flag consumed");
    }
    {
        RubyPrefetchProfile p;p.unit_filter=1;p.nonunit_filter=1;p.train_misses=1;RubyPrefetcher pf(p);
        pf.observe_miss(0x1000,0);pf.observe_miss(0x2000,1);
        check(pf.observe_miss(0x1040,2).empty(),"FIFO filter overwrite removes old candidate");
    }
    {
        for(unsigned kind=0;kind<8;++kind) {
            RubyPrefetcher pf(profile);auto a=access(0x1000,0x9000);
            if(kind==0)a.physical.reset();
            if(kind==1)a.uncacheable=true;
            if(kind==2)a.strictly_ordered=true;
            if(kind==3)a.instruction=true;
            if(kind==4)a.prefetch=true;
            if(kind==5)a.page_walk=true;
            if(kind==6)a.secure=true;
            if(kind==7)a.pc.reset();
            for(unsigned i=0;i<8;++i)check(pf.observe_data_access(a,i).empty(),"ineligible IP request must not train");
            check(pf.stats().ip_candidates==0,"ineligible source has no candidate");
        }
        RubyPrefetcher pf(profile);auto a=access(0x1000,0x9000);a.complete=false;bool bad=false;
        try{pf.observe_data_miss(a,0);}catch(const std::invalid_argument&){bad=true;}check(bad,"unknown request eligibility is not ordinary");
        a.complete=true;pf.observe_data_miss(a,1);bad=false;
        try{pf.observe_data_miss(a,0);}catch(const std::logic_error&){bad=true;}check(bad,"model event time may not regress");
    }
    {
        RubyPrefetcher pf(profile);
        // Five PCs mapping to one set: only four survive. The fifth allocation
        // evicts the oldest first, while touching an entry protects it.
        for(unsigned i=0;i<4;++i)pf.observe_data_access(access(0x1000,0x9000,0x100+i*64),0);
        pf.observe_data_access(access(0x1040,0x9040,0x100),1);
        pf.observe_data_access(access(0x1000,0x9000,0x200),2);
        check(pf.observe_data_access(access(0x1080,0x9080,0x100),3).empty(),"touched PC retains samples, third still below threshold");
        check(pf.observe_data_access(access(0x10c0,0x90c0,0x100),4).size()==1,"IP replacement protects recent entry");
        for(unsigned i=1;i<4;++i)check(pf.observe_data_access(access(0x1000+i*64,0x9000+i*64,0x140),4+i).empty(),"evicted PC restarts training");
    }
    {
        RubyPrefetchProfile p;p.train_misses=1;p.num_startup_pfs=2;p.num_streams=1;RubyPrefetcher pf(p);
        fastsim::PendingOwner owner{2,19,1,7};pf.observe_miss(0x1000,10,owner);
        const auto out=pf.observe_miss(0x1040,10,owner);
        check(out.size()==2 && out[0].address==0x1080 && out[1].address==0x10c0 && out[0].trigger==owner,"startup count and functional owner preserved");
        check(pf.observe_prefetch_miss(0x1080,11,owner).at(0).address==0x1100,"partial hit extends from stream tail, not hit address");
        check(pf.stats().partial_hits==1 && pf.stats().requested==3,"partial hit/request populations preserved");
        // Replacing the sole stream must discard its old matching window.
        pf.observe_miss(0x2000,12);pf.observe_miss(0x2040,13);
        check(pf.observe_prefetch_hit(0x1100,14).empty(),"replaced stream no longer extends old chain");
    }
    {
        RubyPrefetchProfile p;p.ip_stride=false;RubyPrefetcher pf(p);
        for(unsigned i=0;i<8;++i)check(pf.observe_data_access(access(0x1000+i*64,0x9000+i*64),i).empty(),"L2 IP disabled consumes profile");
        check(pf.stats().ip_candidates==0,"disabled target is not unsupported");
        check(pf.observe_data_access(PrefetchAccess{},9).empty(),"disabled target requires no unused IP facts");
        bool bad=false;p.num_startup_pfs=9;try{RubyPrefetcher broken(p);}catch(const std::invalid_argument&){bad=true;}check(bad,"unsupported native inflight geometry rejected");
    }
}
}
int main(){try{tests();std::cout<<"SPR autonomous prefetch candidate tests passed\n";return 0;}catch(const std::exception& e){std::cerr<<e.what()<<'\n';return 1;}}
