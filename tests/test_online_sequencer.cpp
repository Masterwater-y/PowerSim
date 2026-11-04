#include "fastsim/spr_sequencer.hpp"
#include <iostream>
#include <stdexcept>
using namespace fastsim;
using namespace fastsim::spr;
#define CHECK(x) do { if (!(x)) throw std::runtime_error(#x); } while (false)
static ResourceLimits limits() { return {16,16,16,48,256,256,256,112,4,4}; }
static SequencerRequest req(std::uint64_t seq, std::uint64_t address, SequencerKind kind = SequencerKind::Read, bool ordered = false) {
    return {{0,seq,0,1}, address, kind, ordered, 0};
}
static SequencerService admit(OnlineSequencer& s, SequencerRequest r) {
    auto p = s.peek(r);
    CHECK(p.status == SequencerStatus::NewService || p.status == SequencerStatus::Follower);
    auto u = s.grant(p, true, 0);
    CHECK(u.actions.size() == 1);
    return u.actions.front().service;
}
static void admission() {
    OnlineSequencer s(0,limits());
    auto r=req(1,0x1000);
    auto p=s.peek(r);
    CHECK(p.status == SequencerStatus::NewService);
    CHECK(s.outstanding()==0);
    CHECK(s.grant(p,false,0).status==SequencerStatus::BackendRefused);
    CHECK(s.accepted()==0 && s.live_lines()==0);
    auto token=admit(s,r);
    CHECK(s.peek(r).status==SequencerStatus::Duplicate);
    for (unsigned i=2;i<=16;++i) {
        auto f=req(i,0x1008); f.asid=i;
        CHECK(s.peek(f).status==SequencerStatus::Follower);
        admit(s,f);
    }
    CHECK(s.outstanding()==16 && s.live_lines()==1);
    CHECK(s.peek(req(17,0x1000)).status==SequencerStatus::Full);
    CHECK(s.peek(req(18,0x2000)).status==SequencerStatus::Full);
    auto u=s.callback(token,SequencerCallback::Read,5);
    CHECK(u.callback_accepted && u.actions.size()==16 && !u.wrote);
    CHECK(s.outstanding()==0 && s.live_lines()==0 && s.accepted()==s.released());
    CHECK(!s.callback(token,SequencerCallback::Read,5).callback_accepted);
    auto newer=admit(s,req(19,0x1000));
    CHECK(!s.callback(token,SequencerCallback::Read,6).callback_accepted);
    CHECK(s.outstanding()==1 && newer.generation!=token.generation);
    auto old=s.peek(req(20,0x2000));
    admit(s,req(21,0x3000));
    CHECK(s.grant(old,true,6).status==SequencerStatus::Stale);
    auto bad=req(23,0x4000,SequencerKind::Atomic);
    CHECK(s.peek(bad).status==SequencerStatus::Unsupported);
    bad.kind=SequencerKind::Prefetch; CHECK(s.peek(bad).status==SequencerStatus::Unsupported);
    bad.kind=SequencerKind::Locked; CHECK(s.peek(bad).status==SequencerStatus::Unsupported);
    bad=req(24,0); bad.owner.core=1; CHECK(s.peek(bad).status==SequencerStatus::WrongCore);
    OnlineSequencer other(1,limits()); CHECK(other.peek(bad).status==SequencerStatus::NewService);
    s.validate();
}
static void read_write_reissue() {
    OnlineSequencer s(0,limits());
    auto a=admit(s,req(1,64));
    admit(s,req(2,64));
    admit(s,req(3,64,SequencerKind::Write));
    admit(s,req(4,64));
    auto u=s.callback(a,SequencerCallback::Read,10);
    CHECK(u.actions.size()==3 && s.outstanding()==2);
    CHECK(u.actions[0].kind==SequencerActionKind::Completed);
    CHECK(u.actions[1].kind==SequencerActionKind::Completed);
    CHECK(u.actions[2].kind==SequencerActionKind::ReissueReady);
    auto b=s.peek_service(); CHECK(b && b->owner.sequence==3);
    CHECK(!s.grant_service(*b,false,10));
    CHECK(!s.callback(*b,SequencerCallback::Write,10).callback_accepted);
    CHECK(s.grant_service(*b,true,10));
    CHECK(!s.grant_service(*b,true,10));
    CHECK(!s.callback(a,SequencerCallback::Read,11).callback_accepted);
    CHECK(!s.callback(*b,SequencerCallback::Read,11).callback_accepted);
    u=s.callback(*b,SequencerCallback::Write,12);
    CHECK(u.wrote && u.actions.size()==2 && s.outstanding()==0);
    s.validate();
}
static void ordered_parking() {
    OnlineSequencer s(0,limits());
    auto a=admit(s,req(1,64,SequencerKind::Write,true));
    auto b=admit(s,req(2,128,SequencerKind::Write,true));
    admit(s,req(3,128,SequencerKind::Write,true));
    auto u=s.callback(b,SequencerCallback::Write,10);
    CHECK(!u.wrote && u.actions.size()==1 && u.actions[0].kind==SequencerActionKind::Parked);
    CHECK(!s.peek_service());
    u=s.callback(a,SequencerCallback::Write,11);
    CHECK(u.wrote && u.actions.size()==2 && s.outstanding()==2);
    auto c=s.peek_service(); CHECK(c && c->line==128 && c->generation!=b.generation);
    CHECK(!s.callback(b,SequencerCallback::Write,11).callback_accepted);
    CHECK(s.grant_service(*c,true,11));
    u=s.callback(*c,SequencerCallback::Write,11);
    CHECK(!u.wrote && u.actions.size()==1 && s.outstanding()==2);
    auto d=s.peek_service(); CHECK(d && d->earliest_cycle==11);
    CHECK(s.grant_service(*d,true,11));
    u=s.callback(*d,SequencerCallback::Write,12);
    CHECK(u.wrote && u.actions.size()==2 && s.outstanding()==0);
    s.validate();
}
static void no_coalesce() {
    OnlineSequencer s(0,limits());
    auto a=admit(s,req(1,64,SequencerKind::Write));
    admit(s,req(2,64));
    auto u=s.callback(a,SequencerCallback::Write,1,true);
    CHECK(u.wrote && s.outstanding()==1 && u.actions.size()==2);
    auto b=s.peek_service(); CHECK(b && b->kind==SequencerKind::Read);
    CHECK(s.grant_service(*b,true,1));
    CHECK(s.callback(*b,SequencerCallback::Read,2).actions.size()==1);
    s.validate();
}
static void service_admission_time() {
    OnlineSequencer s(0,limits());
    auto a=admit(s,req(1,64));
    admit(s,req(2,64,SequencerKind::Write));
    s.callback(a,SequencerCallback::Read,10);
    auto b=s.peek_service(); CHECK(b);
    CHECK(s.grant_service(*b,true,20));
    CHECK(!s.callback(*b,SequencerCallback::Write,19).callback_accepted);
    auto stale=*b; ++stale.owner.generation;
    CHECK(!s.callback(stale,SequencerCallback::Write,21).callback_accepted);
    CHECK(s.callback(*b,SequencerCallback::Write,21).callback_accepted);
}
static void interleaved_ordered_lines_and_live_bound() {
    OnlineSequencer s(0,limits());
    auto a=admit(s,req(1,64,SequencerKind::Write,true));
    auto b=admit(s,req(2,128,SequencerKind::Write,true));
    auto follower=req(3,64,SequencerKind::Write,true);
    auto p=s.peek(follower); CHECK(p.status==SequencerStatus::Follower);
    CHECK(s.grant(p,false,0).status==SequencerStatus::Follower);
    auto u=s.callback(a,SequencerCallback::Write,10);
    CHECK(u.wrote && u.actions.size()==2 && u.actions[1].kind==SequencerActionKind::Parked);
    s.validate();
    u=s.callback(b,SequencerCallback::Write,11);
    CHECK(u.wrote && u.actions.size()==2);
    auto c=s.peek_service(); CHECK(c && c->owner==follower.owner);
    CHECK(s.grant_service(*c,true,12));
    CHECK(s.callback(*c,SequencerCallback::Write,13).wrote);
    for (unsigned i=4;i<1000;++i) {
        auto token=admit(s,req(i,64));
        s.callback(token,SequencerCallback::Read,i+20);
        CHECK(s.live_lines()==0 && s.outstanding()==0);
    }
    CHECK(s.accepted()==s.released());
    s.validate();
}
static void protocol() {
    ProtocolCredits p(limits());
    ProtocolLease upgrade{{0,1,0,1},1,{ProtocolLevel::L1,true,false}};
    CHECK(p.peek(upgrade)==ProtocolStatus::Ready && p.acquire(upgrade));
    CHECK(p.tbes(ProtocolLevel::L1)==1 && p.fills(ProtocolLevel::L1)==0);
    CHECK(p.peek(upgrade)==ProtocolStatus::Duplicate);
    for (unsigned i=0;i<16;++i) {
        ProtocolLease miss{{0,i+2,0,1},i+2,{ProtocolLevel::L1,true,true}};
        CHECK(p.acquire(miss));
    }
    ProtocolLease blocked{{0,40,0,1},40,{ProtocolLevel::L1,true,true}};
    CHECK(p.peek(blocked)==ProtocolStatus::FillFull && !p.acquire(blocked));
    CHECK(p.tbes(ProtocolLevel::L1)==17 && p.fills(ProtocolLevel::L1)==16);
    auto stale=upgrade; ++stale.service_generation;
    auto mistyped=upgrade; mistyped.demand.data_fill=true;
    CHECK(!p.release(mistyped));
    CHECK(!p.release(stale) && p.release(upgrade) && !p.release(upgrade));
    for (unsigned i=0;i<48;++i) CHECK(p.acquire({{0,i+100,0,1},i+100,{ProtocolLevel::L2,true,true}}));
    CHECK(p.fills(ProtocolLevel::L2)==48 && p.fills(ProtocolLevel::L1)==16);
    for (unsigned i=0;i<256;++i) CHECK(p.acquire({{0,i+200,0,1},i+200,{ProtocolLevel::Llc,true,false}}));
    CHECK(p.peek({{0,900,0,1},900,{ProtocolLevel::Llc,true,false}})==ProtocolStatus::TbeFull);
    CHECK(p.peek({{0,901,0,1},901,{ProtocolLevel::L1,false,true}})==ProtocolStatus::Invalid);
    p.validate();
}
int main() {
    try { admission(); read_write_reissue(); ordered_parking(); no_coalesce(); protocol(); service_admission_time(); interleaved_ordered_lines_and_live_bound(); }
    catch (const std::exception& e) { std::cerr << "FAIL: " << e.what() << '\n'; return 1; }
    std::cout << "online Sequencer and protocol-credit tests passed\n";
}
