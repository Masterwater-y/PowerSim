#include "fastsim/core_continuation.hpp"
#include <algorithm>
#include <array>
#include <deque>
#include <limits>
#include <map>
#include <stdexcept>

namespace fastsim::spr {
namespace {
using Cycle = std::uint64_t;
Cycle add(Cycle a, Cycle b) {
    if (b > std::numeric_limits<Cycle>::max()-a) throw std::overflow_error("core continuation cycle overflow");
    return a+b;
}

// Persistent physical ports, per-OpClass intervals and genuine hardware FU
// ring attempts. Read-only earliest() never rotates a cursor.
class NativeIssueCalendar {
    std::array<Cycle,12> busy_{};
    std::array<std::array<Cycle,128>,12> next_op_{};
    std::array<SprGenericFuRing,128> rings_{};
    SprPortRoundRobin rr_;
    std::array<std::optional<Cycle>,128> failed_{}, denied_{};
    std::array<std::uint16_t,128> masks_{};
    Cycle cycle_=0;
    unsigned issued_=0, width_;
public:
    explicit NativeIssueCalendar(unsigned width):width_(width){}
    void advance(Cycle now,CoreContinuationCounters& counters) {
        if(now==cycle_)return;
        if(now<cycle_)throw std::logic_error("native issue time regressed");
        // No intervening readiness/port/dispatch event exists: every skipped
        // cycle makes one all-busy attempt for each ready generic class head.
        // The ring advances one candidate on failure, so reduce modulo size.
        for(unsigned op=0;op<128;++op)if(failed_[op] && *failed_[op]==cycle_) {
            const auto skipped=now-cycle_-1;
            counters.skipped_failed_attempts+=skipped;
            unsigned count=0;for(unsigned p=0;p<12;++p)count+=(masks_[op]>>p)&1;
            if(count)for(Cycle n=0;n<skipped%count;++n)rings_[op].attempt(masks_[op],0);
        }
        cycle_=now;issued_=0;
    }
    bool width_available()const{return issued_<width_;}
    Cycle earliest(const FunctionalCoreUop& u,Cycle now)const {
        if(issued_>=width_ && now<=cycle_)now=add(cycle_,1);
        if(!u.integer_family && denied_[u.canonical_op] && *denied_[u.canonical_op]>=now)
            now=add(*denied_[u.canonical_op],1);
        if(!u.candidate_ports)return now;
        Cycle result=UINT64_MAX;
        for(unsigned p=0;p<12;++p)if(u.candidate_ports&(1u<<p))
            result=std::min(result,std::max({now,busy_[p],next_op_[p][u.canonical_op]}));
        if(u.memory_write && !u.store_group)result=std::max(result,std::min(std::max(now,busy_[4]),std::max(now,busy_[9])));
        return result;
    }
    std::pair<int,int> attempt(const FunctionalCoreUop& u,Cycle now,CoreContinuationCounters& counters) {
        if(!width_available())return {-1,-1};
        if(!u.integer_family && denied_[u.canonical_op]==std::optional<Cycle>{now})return {-1,-1};
        if(!u.candidate_ports){++issued_;return {-2,-1};}
        ++counters.fu_attempts;
        int data=-1;
        if(u.memory_write && !u.store_group) {
            if(busy_[4]<=now)data=4;else if(busy_[9]<=now)data=9;
            // Native store-data precheck returns before touching the FU ring.
            if(data<0){++counters.fu_failed_attempts;denied_[u.canonical_op]=now;return {-1,-1};}
        }
        std::uint16_t eligible=0;
        for(unsigned p=0;p<12;++p)if(busy_[p]<=now && next_op_[p][u.canonical_op]<=now)
            eligible|=1u<<p;
        int port;
        if(u.integer_family)port=rr_.probe(u.integer_family,u.candidate_ports,eligible);
        else {
            port=rings_[u.canonical_op].attempt(u.candidate_ports,eligible);
            masks_[u.canonical_op]=u.candidate_ports;
        }
        if(port<0){
            ++counters.fu_failed_attempts;
            if(!u.integer_family){failed_[u.canonical_op]=now;denied_[u.canonical_op]=now;}
            return {-1,-1};
        }
        if(u.integer_family && !rr_.commit(u.integer_family,u.candidate_ports,port))throw std::logic_error("native RR commit failed");
        busy_[port]=add(now,u.integer_family?1:std::max(1u,u.fu_occupancy_cycles));
        next_op_[port][u.canonical_op]=add(now,u.issue_interval);
        if(data>=0)busy_[data]=add(now,std::max(1u,u.fu_occupancy_cycles));
        ++issued_;return {port,data};
    }
};

}

struct CoreContinuation::Impl {
    enum class Stage { Issue, Response, Ready, Complete };
    struct Entry {
        FunctionalCoreUop fact;
        Stage stage=Stage::Issue;
        Cycle dispatch=0, issue=0, completion=0;
        std::optional<Cycle> ready, result_ready;
        std::optional<StoreToken> store;
        std::optional<SprIqHandle> iq;
        std::optional<Cycle> last_attempt;
        std::vector<std::optional<Cycle>> responses;
    };
    SimulatorConfig config;
    std::uint32_t core;
    FinalIssueCalendar calendar;
    StoreBuffer* stores;
    std::unique_ptr<SprPartitionedIq> native_iq;
    NativeIssueCalendar native_issue;
    std::deque<FunctionalCoreUop> queued;
    std::deque<Entry> live;
    // Only a retired fast-adder version whose extra edge has not expired.
    // Erased by model time, bounded by one cycle of writeback/retirement.
    std::map<std::uint64_t,Cycle> retired_float_edges;
    struct Pair {
        std::optional<StoreToken> token;
        std::uint64_t address_sequence=0, address=0;
        std::uint32_t bytes=0;
        bool address_seen=false,data_seen=false,address_ready=false,data_ready=false,published=false;
    };
    std::map<std::uint64_t,Pair> pairs;
    std::uint64_t greatest_group=0;
    struct StorePublication { StoreToken token; std::uint64_t sequence; bool allow; };
    std::multimap<Cycle,StorePublication> store_publications;
    Cycle now=0, unseen_floor=0, dispatch_cycle=0, commit_cycle=0;
    std::uint32_t dispatch_used=0, commit_used=0;
    std::size_t iq_used=0,lq_used=0;
    std::uint64_t live_destinations=0;
    std::optional<std::uint64_t> last_appended, last_retired;
    bool source_finished=false, source_parked=false;
    CoreContinuationCounters counters;
    Impl(const SimulatorConfig& c,std::uint32_t id,StoreBuffer* b):config(c),core(id),calendar(c),stores(b),native_issue(c.issue_width){
        if(c.causal_spr_resources)native_iq=std::make_unique<SprPartitionedIq>(SprIqProfile{{c.spr_profile.iq_entries[0],c.spr_profile.iq_entries[1],c.spr_profile.iq_entries[2]}});
    }
    std::optional<SprIqHandle> group_handle(const FunctionalCoreUop& u)const {
        if(u.model_allocation_group)for(const auto& e:live)
            if(e.fact.model_allocation_group==u.model_allocation_group && e.iq)return e.iq;
        return std::nullopt;
    }
    bool iq_available(const FunctionalCoreUop& u)const {
        if(u.zero_idiom)return true;
        if(!native_iq)return iq_used<config.iq_entries;
        if(group_handle(u))return true;
        const auto d=SprPartitionedIq::domain_for(u.iq_op_class);
        return native_iq->used(d)<config.spr_profile.iq_entries[static_cast<unsigned>(d)];
    }
    Cycle earliest(const Entry& e)const {
        const auto floor=std::max(now,add(e.dispatch,config.dispatch_to_issue));
        return native_iq?native_issue.earliest(e.fact,floor):calendar.earliest(e.fact,floor);
    }
    bool dependencies_ready(const Entry& e) const {
        // Unknown older addresses block disambiguation. A completed address
        // permits a disjoint load even while its independent SD waits for RAW.
        if(stores && e.fact.memory_read)for(const auto& item:pairs) {
            const auto& p=item.second;
            if(p.address_sequence>=e.fact.sequence)continue;
            if(!p.address_ready)return false;
            const bool overlap=e.fact.memory_address<p.address+p.bytes &&
                p.address<e.fact.memory_address+e.fact.memory_bytes;
            if(overlap && !p.data_ready)return false;
        }
        if(stores && e.fact.memory_read)for(const auto& older:live) {
            if(older.fact.sequence>=e.fact.sequence)break;
            if(older.fact.memory_write && !older.fact.store_group && older.stage!=Stage::Complete)return false;
        }
        for(auto dependency:e.fact.dependencies) {
            auto ready_float=[&](Cycle extra) {
                if(std::find(e.fact.unclassified_dependencies.begin(),e.fact.unclassified_dependencies.end(),dependency)!=e.fact.unclassified_dependencies.end())
                    throw std::invalid_argument("FloatAdd dependency requires functional source register class");
                return e.fact.canonical_op==4 ||
                    std::find(e.fact.float_dependencies.begin(),e.fact.float_dependencies.end(),dependency)==e.fact.float_dependencies.end() || now>=extra;
            };
            if(last_retired && dependency<=*last_retired) {
                const auto edge=retired_float_edges.find(dependency);
                if(edge!=retired_float_edges.end() && !ready_float(edge->second))return false;
                continue;
            }
            auto found=std::find_if(live.begin(),live.end(),[dependency](const Entry& p){return p.fact.sequence==dependency;});
            if(found==live.end() || (found->stage!=Stage::Complete &&
                (!found->result_ready || *found->result_ready>now))) return false;
            if(found->fact.float_add_destination && !ready_float(add(found->completion,1)))return false;
        }
        return true;
    }
};
CoreContinuation::CoreContinuation(const SimulatorConfig& c,std::uint32_t core,StoreBuffer* stores) {
    const auto gaps=unsupported(c);
    if(!gaps.empty()) throw std::invalid_argument("core continuation unsupported: "+gaps.front());
    if(!c.rob_entries || !c.iq_entries || !c.lq_entries || !c.dispatch_width || !c.commit_width)
        throw std::invalid_argument("core continuation zero queue/stage capacity");
    impl_=std::make_unique<Impl>(c,core,stores);
}
CoreContinuation::~CoreContinuation()=default;
CoreContinuation::CoreContinuation(CoreContinuation&&) noexcept=default;
CoreContinuation& CoreContinuation::operator=(CoreContinuation&&) noexcept=default;
std::vector<std::string> CoreContinuation::unsupported(const SimulatorConfig& c) {
    std::vector<std::string> gaps;
    if(c.spr_shared_fu && !c.causal_spr_resources) gaps.push_back("spr_shared_fu");
    if(c.rename_free_list || c.response_rename_feedback) gaps.push_back("rename_free_list");
    if(c.iew_to_rename || c.commit_to_rename) gaps.push_back("backward_rename_visibility");
    if(c.response_branch_recovery) gaps.push_back("branch_recovery");
    if(c.dtlb.hierarchy_walk) gaps.push_back("hierarchy_translation");
    return gaps;
}
void CoreContinuation::append(std::vector<FunctionalCoreUop> facts,Cycle floor,bool finished) {
    auto& s=*impl_;
    if(s.source_finished) throw std::logic_error("append after core source finished");
    if(floor<s.unseen_floor) throw std::logic_error("unseen core source floor regressed");
    auto previous=s.last_appended;
    for(const auto& u:facts) {
        if((previous && u.sequence!=add(*previous,1)) || u.functional_dispatch_floor<s.unseen_floor)
            throw std::logic_error("core source violates published sequence/time frontier");
        if(s.native_iq && (!u.static_resources_complete || u.canonical_op<0 || u.canonical_op>=128 ||
             u.iq_op_class.empty() || u.integer_family>5 || u.candidate_ports>=4096 || !u.issue_interval ||
             (!u.candidate_ports && u.canonical_op!=0 && !u.zero_idiom)))
            throw std::invalid_argument("causal SPR requires complete static FU/IQ metadata");
        if(u.zero_idiom && (!s.config.causal_static_integer || !s.native_iq || u.zero_idiom>4 ||
             !u.destination_count || !u.dependencies.empty() || u.memory_read || u.memory_write ||
             u.integer_family || u.candidate_ports || u.model_allocation_group))
            throw std::invalid_argument("invalid native zero idiom continuation facts");
        if(u.memory_write && !s.stores) throw std::invalid_argument("core continuation store lifecycle adapter missing");
        if((u.memory_write || u.store_data_member) && !s.config.store_commit_to_iew_delay)
            throw std::invalid_argument("native stores require source-declared core.store_commit_to_iew_delay");
        if(s.native_iq && (u.canonical_op==kSprStoreAddress || u.canonical_op==kSprStoreData) && !u.store_group)
            throw std::invalid_argument("native split store lacks certified pair identity");
        if(u.store_group) {
            const auto& policy=s.config.spr_profile.static_uop_policy_sha256;
            if(policy.size()!=64 || policy==std::string(64,'0') ||
               !std::all_of(policy.begin(),policy.end(),[](char c){return (c>='0' && c<='9') || (c>='a' && c<='f');}))
                throw std::invalid_argument("split store continuation requires declared static source policy SHA256");
            if(!s.native_iq || !s.stores || (u.memory_write==u.store_data_member) ||
               u.memory_read || (u.memory_write && u.canonical_op!=kSprStoreAddress) ||
               (u.store_data_member && u.canonical_op!=kSprStoreData))
                throw std::invalid_argument("invalid split store continuation role");
            auto found=s.pairs.find(u.store_group);
            if(found==s.pairs.end()) {
                // Source IDs are monotonically assigned at the first member.
                // A high-water mark rejects reuse without whole-stream history.
                if(u.store_group<=s.greatest_group || !u.memory_write)
                    throw std::invalid_argument("split store group reused or missing preceding SA");
                s.greatest_group=u.store_group;
                found=s.pairs.emplace(u.store_group,Impl::Pair{}).first;
            }
            auto& pair=found->second;
            if(u.memory_write) {
                if(pair.address_seen)throw std::invalid_argument("duplicate store address member");
                pair.address_seen=true;pair.address_sequence=u.sequence;
                pair.address=u.store_address;pair.bytes=u.store_facts.bytes;
            } else {
                if(pair.data_seen)throw std::invalid_argument("duplicate store data member");
                pair.data_seen=true;
            }
        } else if(u.store_data_member)throw std::invalid_argument("store data lacks pair group");
        if(u.memory_read != !u.memory_requests.empty()) throw std::invalid_argument("core functional memory population mismatch");
        for(auto d:u.dependencies) if(d>=u.sequence) throw std::invalid_argument("noncausal core dependency");
        for(std::size_t i=0;i<u.memory_requests.size();++i) {
            const auto& owner=u.memory_requests[i];
            if(owner.core!=s.core || owner.sequence!=u.sequence) throw std::invalid_argument("core request owner mismatch");
            for(std::size_t j=0;j<i;++j) if(owner==u.memory_requests[j]) throw std::invalid_argument("duplicate core request owner");
        }
        previous=u.sequence;
    }
    if(finished)for(const auto& item:s.pairs)
        if(!item.second.address_seen || !item.second.data_seen)
            throw std::invalid_argument("source ended with incomplete SA/SD pair");
    for(auto& u:facts) s.queued.push_back(std::move(u));
    s.counters.appended+=facts.size();s.last_appended=previous;
    s.unseen_floor=floor;s.source_finished=finished;
}
void CoreContinuation::park_source(bool parked,Cycle cycle) {
    auto& s=*impl_;
    if(s.source_finished || cycle<s.now || parked==s.source_parked)
        throw std::logic_error("invalid causal source park/release");
    s.source_parked=parked;
    // Parking can happen during bounded refill at the current coordinator
    // frontier; do not move an older queued prefix forward. Releasing may
    // only admit younger entries at the certified release cycle.
    if(!parked)s.now=cycle;
}
CorePublication CoreContinuation::advance_until_blocked(Cycle limit) {
    auto& s=*impl_;
    if(limit<s.now) throw std::logic_error("core continuation time regressed");
    CorePublication out;
    auto next=[&out](Cycle t){if(!out.next_internal_cycle || t<*out.next_internal_cycle) out.next_internal_cycle=t;};
    for(;;) {
        out.next_internal_cycle.reset();
        s.calendar.discard_before(s.now);
        for(auto it=s.retired_float_edges.begin();it!=s.retired_float_edges.end();)
            if(it->second<=s.now)it=s.retired_float_edges.erase(it);else ++it;
        if(s.native_iq)s.native_issue.advance(s.now,s.counters);
        auto resource=[&](const char* kind,const Impl::Entry& e,std::uint64_t value) {
            if(s.native_iq)out.resources.push_back({kind,s.now,e.fact.sequence,static_cast<unsigned>(SprPartitionedIq::domain_for(e.fact.iq_op_class)),value});
        };
        auto release_iq=[&](Impl::Entry& e,SprIqRelease boundary) {
            if(!e.iq)return;
            if(!s.native_iq->release(*e.iq,boundary))throw std::logic_error("native IQ release boundary mismatch");
            e.iq.reset();resource("iq_release",e,s.native_iq->used(SprPartitionedIq::domain_for(e.fact.iq_op_class)));
        };
        // Unknown input at this frontier can still win an issue slot. The
        // source must publish it or a stronger functional bound first.
        if(!s.source_finished && !s.source_parked && s.queued.empty() && s.unseen_floor<=s.now) {
            out.needs_source=true;break;
        }
        bool changed=false;
        while(!s.store_publications.empty() && s.store_publications.begin()->first<=s.now) {
            const auto p=s.store_publications.begin()->second;
            if(p.allow){s.stores->allowWriteback(p.token,s.now);++s.counters.store_writeback_publications;}
            else {s.stores->commit(p.token,s.now);++s.counters.store_commit_publications;}
            s.store_publications.erase(s.store_publications.begin());changed=true;
        }
        for(auto& e:s.live) {
            ++s.counters.inspected_entries;
            if(e.stage==Impl::Stage::Ready && *e.ready<=s.now) {
                if(!s.calendar.writeback(s.now)) {e.ready=add(s.now,1);continue;}
                e.completion=s.now;e.stage=Impl::Stage::Complete;
                if(!e.fact.integer_family && !e.fact.memory_read && !e.fact.memory_write)
                    resource("generic_wake",e,e.completion);
                if(e.fact.store_group) {
                    auto& pair=s.pairs.at(e.fact.store_group);
                    if(!pair.token)throw std::logic_error("split completion lacks allocated SQ entry");
                    if(e.fact.store_data_member){pair.data_ready=true;s.stores->markDataReady(*pair.token,s.now);}
                    else {pair.address_ready=true;s.stores->markAddressReady(*pair.token,e.fact.store_address,s.now);}
                    if(pair.address_ready && pair.data_ready && !pair.published) {
                        s.stores->publish(*pair.token,s.now);pair.published=true;
                    }
                } else if(e.store) {
                    s.stores->markAddressReady(*e.store,e.fact.store_address,s.now);
                    s.stores->markDataReady(*e.store,s.now);
                    s.stores->publish(*e.store,s.now);
                }
                if(s.native_iq) {
                    if(e.fact.memory_read || e.fact.memory_write)release_iq(e,SprIqRelease::Complete);
                } else if(e.fact.memory_read) --s.iq_used;
                ++s.counters.completed;changed=true;
            }
        }
        if(s.commit_cycle!=s.now) {s.commit_cycle=s.now;s.commit_used=0;}
        while(!s.live.empty() && s.live.front().stage==Impl::Stage::Complete && s.commit_used<s.config.commit_width) {
            auto& e=s.live.front();
            if(add(e.completion,s.config.execute_to_commit)>s.now) break;
            out.retired.push_back({e.fact.sequence,e.issue,e.completion,s.now});
            if((e.store && !e.fact.store_group) || e.fact.store_data_member) {
                const auto token=e.fact.store_data_member?s.pairs.at(e.fact.store_group).token:e.store;
                if(!token)throw std::logic_error("store retirement lacks SQ token");
                const auto publication=add(s.now,*s.config.store_commit_to_iew_delay);
                s.store_publications.emplace(publication,Impl::StorePublication{*token,e.fact.sequence,false});
                s.store_publications.emplace(add(publication,1),Impl::StorePublication{*token,e.fact.sequence,true});
                if(e.fact.store_data_member)s.pairs.erase(e.fact.store_group);
            }
            if(e.fact.memory_read) --s.lq_used;
            s.live_destinations-=e.fact.destination_count;
            s.counters.destinations_retired+=e.fact.destination_count;
            if(e.fact.destination_count)resource("destination_release",e,s.live_destinations);
            if(e.fact.float_add_destination && add(e.completion,1)>s.now)
                s.retired_float_edges.emplace(e.fact.sequence,add(e.completion,1));
            s.last_retired=e.fact.sequence;s.live.pop_front();++s.commit_used;++s.counters.retired;changed=true;
        }
        if(s.dispatch_cycle!=s.now) {s.dispatch_cycle=s.now;s.dispatch_used=0;}
        while(!s.queued.empty() && s.dispatch_used<s.config.dispatch_width &&
              s.live.size()<s.config.rob_entries && s.iq_available(s.queued.front()) &&
              (!s.queued.front().memory_read || s.lq_used<s.config.lq_entries) &&
              s.queued.front().functional_dispatch_floor<=s.now) {
            if(s.native_iq && s.queued.front().memory_write && s.stores->used()>=s.config.spr_profile.limits.shared_stores)break;
            std::optional<SprIqHandle> iq;
            if(s.native_iq && !s.queued.front().zero_idiom) {
                const auto& fact=s.queued.front();const auto group=s.group_handle(fact);
                iq=group?s.native_iq->join(*group,fact.iq_op_class,fact.memory_read||fact.memory_write):
                    s.native_iq->acquire(fact.iq_op_class,true,fact.memory_read||fact.memory_write);
                if(!iq)throw std::logic_error("native IQ admission changed or incompatible model group");
            }
            std::optional<StoreToken> token;
            if(s.queued.front().memory_write) {
                token=s.stores->allocate(s.queued.front().store_facts,s.now);
                if(!token) break;
                if(s.queued.front().store_group) {
                    s.pairs.at(s.queued.front().store_group).token=token;
                    ++s.counters.paired_stores;++s.counters.store_address_uops;
                }
            }
            Impl::Entry e;e.store=token;e.iq=iq;e.fact=std::move(s.queued.front());s.queued.pop_front();e.dispatch=s.now;
            e.responses.resize(e.fact.memory_requests.size());
            if(e.fact.store_data_member)++s.counters.store_data_uops;
            if(e.fact.memory_read) ++s.lq_used;
            if(!s.native_iq)++s.iq_used;
            else if(!e.fact.zero_idiom) {
                const auto d=SprPartitionedIq::domain_for(e.fact.iq_op_class);
                s.counters.max_iq_used[static_cast<unsigned>(d)]=std::max<std::uint64_t>(s.counters.max_iq_used[static_cast<unsigned>(d)],s.native_iq->used(d));
                resource("iq_acquire",e,s.native_iq->used(d));
            }
            s.live_destinations+=e.fact.destination_count;
            s.counters.destinations_allocated+=e.fact.destination_count;
            s.counters.max_live_destinations=std::max(s.counters.max_live_destinations,s.live_destinations);
            if(e.fact.destination_count)resource("destination_allocate",e,s.live_destinations);
            if(e.fact.zero_idiom) {
                // The supported frontend stage delays are zero. Native rename
                // writes constant results and sets executed/results-woken here;
                // ROB/commit and destination ownership remain live separately.
                e.stage=Impl::Stage::Complete;e.completion=s.now;e.issue=s.now;
                ++s.counters.zero_eliminated;++s.counters.completed;
                resource("zero_idiom",e,e.fact.zero_idiom);
            }
            ++s.dispatch_used;++s.counters.dispatched;
            s.live.push_back(std::move(e));s.counters.max_live_rob=std::max<std::uint64_t>(s.counters.max_live_rob,s.live.size());changed=true;
        }
        // Close this dispatch prefix before the issue phase. Refilling a
        // host chunk cannot make IQ releases precede same-cycle allocation.
        if(s.native_iq && !s.source_finished && !s.source_parked && s.queued.empty() && s.unseen_floor<=s.now) {
            out.needs_source=true;break;
        }
        for(auto& e:s.live) {
            ++s.counters.inspected_entries;
            if(e.stage!=Impl::Stage::Issue || !s.dependencies_ready(e)) continue;
            if(s.native_iq) {
                if(add(e.dispatch,s.config.dispatch_to_issue)>s.now || e.last_attempt==std::optional<Cycle>{s.now})continue;
                if(!s.native_issue.width_available())break;
                e.last_attempt=s.now;
                const auto grant=s.native_issue.attempt(e.fact,s.now,s.counters);
                if(grant.first==-1)continue;
                resource("fu_grant",e,grant.first<0?12:grant.first);
                if(grant.second>=0)resource("fu_store_data",e,grant.second);
            } else {
                if(s.earliest(e)>s.now)continue;
                s.calendar.reserve(e.fact,s.now);
            }
            e.issue=s.now;++s.counters.issued;changed=true;
            if(e.fact.memory_read) {
                e.stage=Impl::Stage::Response;
                for(const auto& owner:e.fact.memory_requests) out.requests.push_back({owner,s.now});
            } else {
                if(s.native_iq) {if(!e.fact.memory_write)release_iq(e,SprIqRelease::Issue);}
                else --s.iq_used;
                e.stage=Impl::Stage::Ready;
                if(e.fact.integer_family)e.result_ready=add(s.now,e.fact.execution_latency);
                e.ready=add(add(s.now,e.fact.execution_latency),s.config.issue_to_execute);
            }
        }
        if(!out.requests.empty()) break;
        // A zero-latency completion or an issue-released IQ entry can enable
        // another transition in this same cycle, without a fabricated tick.
        if(changed) {++s.counters.internal_events;continue;}
        for(const auto& e:s.live) {
            if(e.result_ready && *e.result_ready>s.now)next(*e.result_ready);
            if(e.fact.float_add_destination && e.stage==Impl::Stage::Complete && add(e.completion,1)>s.now)next(add(e.completion,1));
            if(e.stage==Impl::Stage::Ready) next(*e.ready);
            if(e.stage==Impl::Stage::Issue && s.dependencies_ready(e))
                next(s.native_iq && e.last_attempt==std::optional<Cycle>{s.now} ? std::max(add(s.now,1),s.earliest(e)) : s.earliest(e));
        }
        for(const auto& edge:s.retired_float_edges)next(edge.second);
        if(!s.store_publications.empty())next(s.store_publications.begin()->first);
        if(!s.live.empty() && s.live.front().stage==Impl::Stage::Complete)
            next(std::max(add(s.live.front().completion,s.config.execute_to_commit),s.commit_used==s.config.commit_width?add(s.now,1):s.now));
        if(!s.queued.empty() && s.live.size()<s.config.rob_entries && s.iq_available(s.queued.front()) &&
           (!s.queued.front().memory_read || s.lq_used<s.config.lq_entries) &&
           (!s.queued.front().memory_write || s.stores->used()<s.config.spr_profile.limits.shared_stores))
            next(std::max(s.queued.front().functional_dispatch_floor,s.dispatch_used==s.config.dispatch_width?add(s.now,1):s.now));
        // A known queued head is an in-order dispatch prefix certificate:
        // unseen younger input cannot dispatch ahead of it. Stop and refill
        // at the exact cycle that this prefix empties, before advancing time.
        // A functional minimum is not an assigned dispatch timestamp.
        Cycle ceiling=limit;
        if(!s.source_finished && !s.source_parked && s.queued.empty()) ceiling=std::min(ceiling,std::max(s.now,s.unseen_floor));
        if(!out.next_internal_cycle || *out.next_internal_cycle>ceiling) {s.now=ceiling;break;}
        if(*out.next_internal_cycle<=s.now) throw std::logic_error("core continuation internal progress proof failed");
        s.now=*out.next_internal_cycle;
    }
    for(const auto& e:s.live) if(e.stage==Impl::Stage::Response)
        for(std::size_t i=0;i<e.responses.size();++i) if(!e.responses[i]) out.blocked_on.push_back(e.fact.memory_requests[i]);
    out.not_before=s.now;
    out.finished=s.source_finished && s.queued.empty() && s.live.empty() && s.store_publications.empty();
    out.needs_source=out.needs_source || (!s.source_finished && !s.source_parked && s.queued.empty() && s.now>=s.unseen_floor);
    return out;
}
void CoreContinuation::on_store_buffer_response(Cycle cycle) {
    auto& s=*impl_;
    if(cycle<s.now)throw std::logic_error("store-buffer wake behind core frontier");
    s.now=cycle;
}
void CoreContinuation::on_completion(PendingOwner owner,Cycle response) {
    auto& s=*impl_;
    if(response<s.now) throw std::logic_error("core completion behind published frontier");
    for(auto& e:s.live) for(std::size_t i=0;i<e.fact.memory_requests.size();++i) if(e.fact.memory_requests[i]==owner) {
        if(e.stage!=Impl::Stage::Response || e.responses[i] || response<e.issue)
            throw std::logic_error("duplicate/early core completion");
        e.responses[i]=response;
        if(std::all_of(e.responses.begin(),e.responses.end(),[](const auto& r){return r.has_value();})) {
            Cycle ready=add(add(e.issue,e.fact.execution_latency),s.config.issue_to_execute);
            for(const auto& r:e.responses) ready=std::max(ready,add(*r,s.config.load_response_to_ready));
            e.ready=ready;e.stage=Impl::Stage::Ready;
        }
        return;
    }
    throw std::logic_error("stale/unowned core completion");
}
const CoreContinuationCounters& CoreContinuation::counters() const{return impl_->counters;}
}
