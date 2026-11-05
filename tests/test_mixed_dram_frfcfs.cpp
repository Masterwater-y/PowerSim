#include "fastsim/mixed_dram.hpp"
#include <iostream>
#include <stdexcept>
using namespace fastsim;
namespace {
void check(bool b,const char* m){if(!b)throw std::runtime_error(m);}
MixedDramConfig cfg(MixedDramScheduler policy) {
    MixedDramConfig c;c.scheduler=policy;c.page_policy="open";
    auto& d=c.dram;d.channels=d.ranks_per_channel=1;d.banks_per_channel=d.bank_groups_per_rank=2;
    d.row_bytes=256;d.t_cl=d.t_rcd=d.t_rp=10;d.burst_cycles=4;
    d.t_rrd=d.t_rrd_l=4;d.t_ccd_l=100;d.t_cs=2;d.t_ras=d.t_rtp=0;
    d.frontend_latency=d.backend_latency=0;d.frfcfs_selection_window=0;
    c.t_cwl=c.t_rcd_wr=10;return c;
}
MixedDramRequest req(unsigned id,std::uint64_t line,std::uint64_t time=0) {
    return {{0,id,0,DramServiceKind::kRead},time,line};
}
std::vector<MixedDramCompletion> run(MixedDramConfig c,unsigned step) {
    MixedDramController m(c,64);m.submit(req(0,0));m.submit(req(1,1));m.submit(req(2,4));
    std::vector<MixedDramCompletion> out;
    for(unsigned end=step;end<1000;end+=step){auto r=m.advance(end);out.insert(out.end(),r.begin(),r.end());}
    auto r=m.advance(1000);out.insert(out.end(),r.begin(),r.end());return out;
}
void refresh_tests() {
    auto c=cfg(MixedDramScheduler::kBankPrepFrfcfs);
    c.dram.refresh_enabled=true;c.dram.refresh_ticks_per_cycle=1;
    c.dram.t_refi_ticks=100;c.dram.t_rfc_ticks=20;c.dram.refresh_start_tick=0;
    MixedDramController m(c,64);m.submit(req(0,0,90));
    check(m.advance(90).empty(),"exclusive preparation frontier cannot consume arrival");
    check(m.advance(91).empty(),"rank preparation wins equal-cycle selection");
    const auto result=m.advance(200);
    check(result.size()==1 && result[0].selection==110 && result[0].command==120,
          "closed refresh rank resumes only at legal availability edge");
}
void refresh_edges() {
    auto c=cfg(MixedDramScheduler::kBankPrepFrfcfs);auto& d=c.dram;
    d.refresh_enabled=true;d.refresh_ticks_per_cycle=1;d.t_refi_ticks=100;d.t_rfc_ticks=20;d.t_ccd_l=4;
    auto exercise=[&](unsigned step) {
        MixedDramController m(c,64);m.submit(req(0,0,80));m.submit(req(1,1,90));
        std::vector<MixedDramCompletion> out;
        for(unsigned end=step;end<300;end+=step){auto v=m.advance(end);out.insert(out.end(),v.begin(),v.end());}
        auto v=m.advance(300);out.insert(out.end(),v.begin(),v.end());return out;
    };
    const auto ref=exercise(300);
    check(ref.size()==2 && ref[0].dram_ready==104 && ref[1].selection==124 &&
          ref[1].command==134 && !ref[1].row_hit,"refresh honors reserved read data and invalidates open row");
    for(unsigned step:{1,7,90,91})check(exercise(step)==ref,"refresh arrival frontier partition invariance");
    c.page_policy="close";const auto closed=exercise(300);
    check(closed.size()==2 && closed[0].auto_precharged && closed[1].selection==124,
          "closed bank still waits for reserved auto-PRE and read data obligations");
    c.page_policy="open";
    d.ranks_per_channel=2;MixedDramController ranks(c,64);
    ranks.submit(req(0,0,80));ranks.submit(req(1,1,90));ranks.submit(req(2,8,90));
    const auto parallel=ranks.advance(300);
    check(parallel.size()==3 && parallel[1].id.sequence==2 && parallel[1].selection==110 &&
          parallel[2].selection==124,"unaffected ready rank progresses while another drains data");
    d.channels=2;MixedDramController channels(c,64);
    channels.submit(req(0,0,80));channels.submit(req(1,2,90));channels.submit(req(2,1,90));
    const auto independent=channels.advance(300);
    check(independent.size()==3 && independent[1].id.sequence==2 && independent[1].selection==110,
          "independent channel progresses during other rank preparation");
    d.channels=d.ranks_per_channel=1;d.write_low_threshold_percent=0;c.t_wr=30;
    MixedDramController writes(c,64);auto wr=req(0,0,80);wr.id.kind=DramServiceKind::kWriteback;
    writes.submit(wr);writes.submit(req(1,1,90));const auto rw=writes.advance(300);
    check(rw.size()==2 && rw[0].dram_ready==104 && rw[1].selection==164 && !rw[1].row_hit,
          "write recovery PRE and read direction switch retain rank obligations");
    c.t_wr=0;MixedDramController idle(c,64);const std::uint64_t far=90000000000090ull;
    idle.submit(req(0,0,far));auto late=idle.advance(far+100);
    check(late.size()==1 && late[0].selection==far+20,"idle rank skips trillion periods arithmetically");
    MixedDramController projected(c,64);bool bad=false;
    try{projected.reserve_projected_batch({req(0,0)});}catch(const std::invalid_argument&){bad=true;}
    check(bad && projected.stats().submitted==0,"refresh projected rejection precedes ownership");
    for(unsigned invalid:{0u,1u,2u,3u}) {
        auto broken=c;
        if(invalid==0)broken.dram.refresh_ticks_per_cycle=0;
        if(invalid==1)broken.dram.t_refi_ticks=10;
        if(invalid==2)broken.dram.t_rfc_ticks=90;
        if(invalid==3)broken.dram.refresh_start_tick=UINT64_MAX;
        bad=false;try{MixedDramController model(broken,64);}catch(const std::invalid_argument&){bad=true;}
        check(bad,"invalid refresh domain rejects at construction");
    }
    MixedDramController owned(c,64);owned.submit(req(0,0,80));bad=false;
    try{owned.advance(MixedDramController::max_frontier);}catch(const std::invalid_argument&){bad=true;}
    check(bad && owned.stats().submitted==1 && owned.stats().admitted==0 &&
          owned.refresh_observations()[0].preparations==0,"tick conversion horizon rejects before owners/calendar mutation");
    check(owned.advance(81).size()==1,"rejected horizon preserves undelivered service");
    // Real native frontend receipts are independent of rank readiness.
    d.frontend_latency=7;MixedDramController frontend(c,64);frontend.enable_native_frontend();
    auto w0=req(0,0,90);w0.id.kind=DramServiceKind::kWriteback;
    auto w1=w0;w1.id.sequence=1;
    frontend.submit(w0);frontend.submit(w1);frontend.submit(req(2,0,90));
    const auto accepted=frontend.advance_with_admissions(100);
    check(accepted.admissions.size()==3 && accepted.completions.empty() &&
          accepted.admissions[1].merged_write && accepted.admissions[2].forwarded_read &&
          *accepted.admissions[2].frontend_response==97,"refresh preserves native merge/forward/ACK admission");
    const auto physical=frontend.advance_with_admissions(200);
    check(physical.completions.size()==1 && physical.completions[0].dram_ready==134 &&
          frontend.stats().writes_serviced==1 && frontend.stats().reads_serviced==0,
          "frontend ACK does not rewrite or duplicate physical refresh-delayed service");
    check(!frontend.next_owned_event(),"periodic refresh alone does not keep owned calendar alive");
    d.frontend_latency=0;
    // A new eligible rank must interrupt the wake chosen for an older blocked rank.
    d.ranks_per_channel=2;MixedDramController wake(c,64);
    wake.submit(req(0,0,80));wake.submit(req(1,1,90));wake.advance(111);
    wake.submit(req(2,8,111));const auto interrupted=wake.advance(200);
    check(interrupted.size()==2 && interrupted[0].id.sequence==2 && interrupted[0].selection==111,
          "new rank arrival interrupts previously scheduled refresh wait");
    d.ranks_per_channel=1;
    d.refresh_ticks_per_cycle=3;d.t_refi_ticks=301;d.t_rfc_ticks=60;
    MixedDramController fractional(c,64);fractional.submit(req(0,0,91));
    check(fractional.advance(91).empty() && fractional.advance(92).empty(),"fractional tick preparation preserves exclusive frontier");
    auto rounded=fractional.advance(150);check(rounded.size()==1 && rounded[0].selection==111,"rank availability rounds source ticks upward");
}
void tests() {
    refresh_tests();refresh_edges();
    const auto native=run(cfg(MixedDramScheduler::kBankPrepFrfcfs),1000);
    const auto old=run(cfg(MixedDramScheduler::kRowHitFirstApproximation),1000);
    check(native.size()==3 && native[0].id.sequence==0 && native[1].id.sequence==2 && native[2].id.sequence==1,"hidden closed bank must beat nonseamless row hit");
    check(old[1].id.sequence==1,"legacy row-hit-first stays unchanged");
    check(native[1].command<native[2].command,"model bank preparation admits earlier column");
    for(unsigned step:{1,7,100})check(run(cfg(MixedDramScheduler::kBankPrepFrfcfs),step)==native,"strict frontier slicing cannot change candidate or command");
    auto c=cfg(MixedDramScheduler::kBankPrepFrfcfs);c.dram.t_ccd_l=4;
    const auto seamless=run(c,1000);check(seamless[1].id.sequence==1 && seamless[1].row_hit,"seamless hit keeps FCFS hit priority");
    c=cfg(MixedDramScheduler::kBankPrepFrfcfs);c.dram.t_rrd=c.dram.t_rrd_l=100;
    const auto visible=run(c,1000);check(visible[1].id.sequence==1 && visible[1].row_hit,"unhidden bank preparation cannot displace first prepped hit");
    bool bad=false;c.dram.frfcfs_selection_window=1;
    try{MixedDramController model(c,64);}catch(const std::invalid_argument&){bad=true;}
    check(bad,"source full-queue policy cannot silently honor legacy selection truncation");
    c.dram.frfcfs_selection_window=0;c.dram.refresh_enabled=true;c.dram.refresh_ticks_per_cycle=0;bad=false;
    try{MixedDramController refresh(c,64);}catch(const std::invalid_argument&){bad=true;}
    check(bad,"refresh requires an explicit valid tick domain");
    c.dram.refresh_enabled=false;MixedDramController strict(c,64);bad=false;
    try{strict.reserve_projected_batch({req(0,0)});}catch(const std::invalid_argument&){bad=true;}
    check(bad && strict.stats().submitted==0,"bank-prep source policy requires strict certified arrivals before mutation");
    // All first requests miss: select oldest request within earliest bank set,
    // not rank/bank index scan order. Later known arrivals remain ordinary.
    c=cfg(MixedDramScheduler::kBankPrepFrfcfs);MixedDramController model(c,64);
    model.submit(req(9,4));model.submit(req(10,0));
    auto first=model.advance(1);check(first.size()>=1 && first[0].id.sequence==9,"equal earliest banks preserve request queue order");
    model.submit(req(11,1,1));model.advance(1000);
    check(model.stats().reads_serviced==3,"later source admission retains full population");
}
}
int main(){try{tests();std::cout<<"mixed DRAM bank-prep FRFCFS tests passed\n";return 0;}catch(const std::exception& e){std::cerr<<e.what()<<'\n';return 1;}}
