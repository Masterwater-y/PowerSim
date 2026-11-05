#include "fastsim/core_continuation.hpp"
#include <cassert>
#include <iostream>
#include <stdexcept>
using namespace fastsim;
using namespace fastsim::spr;

static SimulatorConfig config() {
    SimulatorConfig c;
    c.dispatch_to_issue = 0;
    c.issue_to_execute = 0;
    c.execute_to_commit = 0;
    c.dispatch_width = 8;
    c.issue_width = 8;
    c.commit_width = 8;
    c.integer_alu_units = 1;
    return c;
}
static FunctionalCoreUop alu(std::uint64_t seq) { FunctionalCoreUop u; u.sequence=seq; return u; }
static FunctionalCoreUop load(std::uint64_t seq) {
    auto u=alu(seq); u.memory_read=true; u.fu_pool=IntervalFuPool::kMemory;
    u.memory_requests.push_back({0,seq,0,1}); return u;
}
static void independent_overlap() {
    auto c=config();
    CoreContinuation core(c,0);
    auto dependent=alu(1); dependent.dependencies={0};
    core.append({load(0),dependent,alu(2)},0,true);
    auto p=core.advance_until_blocked(0);
    assert(p.requests.size()==1 && p.requests[0].cycle==0);
    p=core.advance_until_blocked(5);
    assert(p.retired.empty());
    assert(core.counters().issued==2); // Independent ALU overlaps unknown load.
    core.on_completion({0,0,0,1},10);
    p=core.advance_until_blocked(20);
    assert(p.finished && p.retired.size()==3);
    assert(p.retired[1].issue_cycle>=10);
    assert(p.retired[2].issue_cycle<10); // No provisional dependent FU reservation.
}
static void cross_chunk_and_paused_source() {
    auto c=config(); CoreContinuation core(c,0);
    core.append({load(0)},0,false);
    auto p=core.advance_until_blocked(0);
    assert(p.requests.size()==1);
    p=core.advance_until_blocked(100);
    assert(p.needs_source && !p.finished && p.not_before==0);
    auto dependent=alu(1);dependent.dependencies={0};
    core.append({dependent,alu(2)},0,true);
    core.advance_until_blocked(5);
    assert(core.counters().issued==2);
    core.on_completion({0,0,0,1},10);
    p=core.advance_until_blocked(20);
    assert(p.finished && p.retired.size()==3);
    assert(p.retired[1].issue_cycle==10 && p.retired[2].issue_cycle==0);
}
static void rob_and_load_queue_release_at_retirement() {
    auto c=config();c.rob_entries=2;c.lq_entries=1;
    CoreContinuation core(c,0);
    auto slow=alu(0);slow.execution_latency=20;
    core.append({slow,load(1),load(2)},0,true);
    auto p=core.advance_until_blocked(0);assert(p.requests.size()==1);
    core.on_completion({0,1,0,1},2);
    p=core.advance_until_blocked(10);
    assert(p.requests.empty() && core.counters().dispatched==2);
    p=core.advance_until_blocked(20);
    assert(p.requests.size()==1 && p.requests[0].owner.sequence==2 && p.requests[0].cycle==20);
    core.on_completion({0,2,0,1},22);
    p=core.advance_until_blocked(25);assert(p.finished);
}
static void fragments_and_stale_callbacks() {
    auto c=config();c.iq_entries=1;CoreContinuation core(c,0);
    auto u=load(0);u.memory_requests.push_back({0,0,1,1});
    core.append({u,alu(1)},0,true);
    auto p=core.advance_until_blocked(0);assert(p.requests.size()==2);
    core.on_completion({0,0,0,1},2);
    p=core.advance_until_blocked(3);assert(core.counters().dispatched==1);
    bool rejected=false;
    try {core.on_completion({0,0,0,1},4);} catch(const std::logic_error&) {rejected=true;}
    assert(rejected);
    core.on_completion({0,0,1,1},5);
    p=core.advance_until_blocked(10);assert(p.finished && p.retired[1].issue_cycle==5);
    rejected=false;
    try {core.on_completion({0,0,1,1},10);} catch(const std::logic_error&) {rejected=true;}
    assert(rejected);
}
static void source_and_capability_fail_closed() {
    auto c=config();c.spr_shared_fu=true;
    assert(!CoreContinuation::unsupported(c).empty());
    bool rejected=false;
    try {CoreContinuation core(c,0);} catch(const std::invalid_argument&) {rejected=true;}
    assert(rejected);
    c=config();CoreContinuation core(c,0);core.append({},5,false);
    auto p=core.advance_until_blocked(20);assert(p.needs_source && p.not_before==5);
    rejected=false;
    try {core.append({load(0)},5,false);} catch(const std::logic_error&) {rejected=true;}
    assert(rejected);
}
static void zero_latency_chain() {
    auto c=config();CoreContinuation core(c,0);
    auto a=alu(0);a.execution_latency=0;
    auto b=alu(1);b.execution_latency=0;b.dependencies={0};
    core.append({a,b},0,true);
    auto p=core.advance_until_blocked(5);
    assert(p.finished && p.retired.size()==2 && p.retired[1].issue_cycle==1);
}
static void queued_prefix_certificate() {
    auto c=config();auto head=alu(0);head.functional_dispatch_floor=100;
    CoreContinuation split(c,0),whole(c,0);
    split.append({head},0,false);
    auto p=split.advance_until_blocked(0);
    assert(!p.needs_source && p.next_internal_cycle==100);
    p=split.advance_until_blocked(100);
    assert(p.needs_source && p.not_before==100);
    // The younger fact's minimum is zero, but the committed in-order dispatch
    // prefix proves it cannot actually dispatch before 100.
    split.append({alu(1)},0,true);
    const auto a=split.advance_until_blocked(200);
    whole.append({head,alu(1)},0,true);
    const auto b=whole.advance_until_blocked(200);
    assert(a.finished && b.finished && a.retired.size()==2 && b.retired.size()==2);
    for(unsigned i=0;i<2;++i) {
        assert(a.retired[i].issue_cycle==b.retired[i].issue_cycle);
        assert(a.retired[i].retire_cycle==b.retired[i].retire_cycle);
    }
}
// RED: the current continuation ignores the frontend/rename-to-dispatch edge.
static void causal_rename_stage_edge() {
    auto c=config();c.causal_retired_frontend=true;c.spr_retired_frontend=true;c.rename_to_dispatch=2;
    CoreContinuation core(c,0);auto u=alu(0);fst::RetiredFrontendMacro m;
    m.end_record=1;m.observed_uops=1;m.instruction_size=1;m.pc=4096;m.code_identity=11;
    m.known_flags=fst::kFrontendRequiredKnown;m.flags=fst::kFrontendStartsMacro|fst::kFrontendComplete;
    u.frontend=m;core.append({u},0,true);
    const auto p=core.advance_until_blocked(20);
    if(p.retired.size()!=1 || p.retired[0].issue_cycle<2)
        throw std::runtime_error("causal rename acknowledgement must precede dispatch by two cycles");
}
void test_core_continuation() {
    causal_rename_stage_edge();
    independent_overlap(); cross_chunk_and_paused_source();
    rob_and_load_queue_release_at_retirement();fragments_and_stale_callbacks();
    source_and_capability_fail_closed();zero_latency_chain();queued_prefix_certificate();
}
#ifdef FASTSIM_CORE_CONTINUATION_STANDALONE
int main() { test_core_continuation(); std::cout << "core continuation tests passed\n"; }
#endif
