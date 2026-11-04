#include "fastsim/functional_sync.hpp"
#include <algorithm>
#include <map>
#include <array>
#include <fstream>
#include <queue>
#include <set>
#include <stdexcept>

namespace fastsim {
FunctionalSyncPlan::FunctionalSyncPlan(const std::string& path, std::uint32_t cores,
                                     bool batch_observations)
    : batch_observations_(batch_observations) {
    std::ifstream in(path);
    const auto fail=[&] { throw std::runtime_error("invalid functional synchronization plan: "+path); };
    std::string tag;
    std::uint64_t ncores=0, count=0;
    if (!(in>>tag)) fail();
    thread_state_=tag=="FASTSIM_THREAD_STATE_PLAN_V1";
    if ((!thread_state_ && tag!="FASTSIM_FUNCTIONAL_SYNC_PLAN_V1") ||
        !(in>>tag>>ncores) || tag!="CORES" || ncores!=cores ||
        !(in>>tag>>count) || tag!="EVENTS" || count==0 || count>1000000) fail();
    std::uint64_t ntasks=0;
    if(thread_state_ && (!(in>>tag>>ntasks) || tag!="TASKS" || !ntasks || ntasks>1000000)) fail();
    layouts_.resize(cores); streams_.resize(cores); initial_owners_.resize(cores);
    owner_continuity_.resize(cores, false);
    for (std::uint32_t core=0; core<cores; ++core) {
        std::uint64_t id=0;
        auto& l=layouts_[core];
        l.source_core=core;
        if (!(in>>tag>>id>>l.records>>l.warmup_records>>l.measurement_records) ||
            tag!="CORE" || id!=core || l.records==0 || l.warmup_records>l.records ||
            l.measurement_records!=l.records-l.warmup_records) fail();
        if(thread_state_ && !(in>>initial_owners_[core])) fail();
    }
    std::set<std::uint64_t> tids, runtime_ids;
    std::map<std::uint64_t,std::array<std::uint64_t,2>> task_identity;
    for(std::uint64_t i=0;i<ntasks;++i) {
        std::uint64_t tid=0,tgid=0,runtime=0;
        if(!(in>>tag>>tid>>tgid>>runtime) || tag!="TASK" || !tid || !tgid || !runtime ||
           !tids.insert(tid).second || !runtime_ids.insert(runtime).second) fail();
        task_ids_.push_back(tid);
        task_identity[tid]={tgid,runtime};
    }
    // Optional functional coverage provenance. Legacy plans have no authority
    // to attribute the unobserved tail between a local EOF and the common cut.
    const auto events_begin = in.tellg();
    if (in >> tag && tag == "ACCOUNTING") {
        if (!thread_state_ || !(in >> accounting_contract_) ||
            accounting_contract_ != "task-oncpu-switch-entry-v1") fail();
        for (std::uint32_t core=0; core<cores; ++core) {
            std::uint64_t given=0, complete=0;
            if (!(in >> tag >> given >> complete) || tag!="CLOSURE" ||
                given!=core || complete>1) fail();
            owner_continuity_[core]=complete!=0;
        }
    } else {
        in.clear();
        in.seekg(events_begin);
    }
    const auto clock_begin = in.tellg();
    if (in >> tag && tag == "TASK_CLOCK") {
        if (!thread_state_ || !(in >> task_clock_contract_) ||
            task_clock_contract_ != "task-lifecycle-basecpu-active-v1") fail();
        task_clock_initial_.resize(cores);task_clock_closure_.resize(cores);
        for(std::uint32_t core=0;core<cores;++core) {
            std::uint64_t given=0,known=0,enabled=0,pid=0,closure=0;
            if(!(in>>tag>>given>>known>>enabled>>pid>>closure)||tag!="CLOCK_STATE"||given!=core||
               known>1||enabled>1||closure>1||(enabled&&!known))fail();
            task_clock_initial_[core]={known!=0,enabled!=0,pid};task_clock_closure_[core]=closure!=0;
            if(known&&enabled&&pid!=initial_owners_[core])fail();
        }
    } else { in.clear();in.seekg(clock_begin); }
    events_.resize(static_cast<std::size_t>(count));
    observed_.resize(events_.size()); released_.resize(events_.size());
    std::vector<std::vector<std::size_t>> successors(events_.size());
    std::vector<std::size_t> remaining(events_.size());
    for(std::size_t id=0; id<events_.size(); ++id) {
        auto& e=events_[id];
        e.thread_state=thread_state_;
        std::uint64_t given=0, phase=0, nd=0;
        bool parsed=false;
        if(thread_state_) parsed=static_cast<bool>(in>>tag>>given>>e.core>>e.ordinal>>e.pc>>e.kind>>phase>>e.arg0>>e.arg1>>e.arg2>>nd);
        else parsed=static_cast<bool>(in>>tag>>given>>e.core>>e.ordinal>>e.pc>>e.address_space>>e.kind>>phase>>nd);
        if (!parsed || tag!="EVENT" || given!=id || e.core>=cores || e.kind<1 || e.kind>(thread_state_?9u:8u) ||
            phase>1 || e.pc==0 || (!thread_state_ && e.address_space==0) || e.ordinal>=layouts_[e.core].records ||
            nd>events_.size()) fail();
        if(thread_state_ && e.kind==6 && (e.arg2<1 || e.arg2>3)) fail();
        e.measurement=phase!=0;
        if (e.measurement != (e.ordinal>=layouts_[e.core].warmup_records)) fail();
        auto& stream=streams_[e.core];
        if (!stream.empty() && events_[stream.back()].ordinal>=e.ordinal) fail();
        std::set<std::size_t> unique;
        for (std::uint64_t j=0; j<nd; ++j) {
            std::uint64_t p=0;
            if (!(in>>p) || p>=events_.size() || p==id || !unique.insert(static_cast<std::size_t>(p)).second) fail();
            e.predecessors.push_back(static_cast<std::size_t>(p));
            successors[static_cast<std::size_t>(p)].push_back(id);
        }
        // Source order is a required edge, not a scheduling guess.
        if (!stream.empty() && !unique.count(stream.back())) fail();
        stream.push_back(id);
        remaining[id]=e.predecessors.size();
    }
    if(in>>tag) fail();
    if(thread_state_) {
        std::set<std::uint64_t> identified;
        std::map<std::uint64_t,std::uint32_t> owner_core;
        std::map<std::uint64_t,std::uint64_t> last_request;
        std::map<std::uint64_t,std::pair<std::uint64_t,std::size_t>> pending;
        const auto bind_owner=[&](std::uint64_t tid,std::uint32_t core) {
            if(!tid) return;
            const auto found=owner_core.find(tid);
            if(found!=owner_core.end() && found->second!=core) fail();
            owner_core[tid]=core;
        };
        for(std::uint32_t core=0;core<cores;++core) {
            auto owner=initial_owners_[core]; bind_owner(owner,core);
            bool enabled=!task_clock_contract_.empty()&&task_clock_initial_[core].enabled;
            bool known=!task_clock_contract_.empty()&&task_clock_initial_[core].known;
            auto eligible=!task_clock_contract_.empty()?task_clock_initial_[core].pid:0;
            std::optional<std::pair<std::uint64_t,std::uint64_t>> cut;
            for(auto id:streams_[core]) {
                const auto& e=events_[id];
                if(e.kind<=5) {
                    if(!e.arg0 || e.arg0!=owner || !task_identity.count(e.arg0)) fail();
                    bind_owner(e.arg0,core);
                    if(e.kind==1) {
                        const auto& identity=task_identity.at(e.arg0);
                        if(e.arg1!=identity[0] || e.arg2<=0 || static_cast<std::uint64_t>(e.arg2)!=identity[1] ||
                           !identified.insert(e.arg0).second) fail();
                    } else {
                        if(!identified.count(e.arg0) || !e.arg1) fail();
                        if(e.kind==2 || e.kind==4) {
                            if(e.arg2<0 || e.arg1<=last_request[e.arg0] || pending.count(e.arg0)) fail();
                            const auto op=static_cast<std::uint64_t>(e.arg2)>>32;
                            if((op & ~std::uint64_t(128))!=(e.kind==2?0u:1u)) fail();
                            last_request[e.arg0]=e.arg1;
                            pending[e.arg0]={e.arg1,id};
                        } else {
                            const auto attempt=pending.find(e.arg0);
                            if(attempt==pending.end() || attempt->second.first!=e.arg1 ||
                               events_[attempt->second.second].kind!=(e.kind==3?2u:4u) ||
                               std::find(e.predecessors.begin(),e.predecessors.end(),attempt->second.second)==e.predecessors.end()) fail();
                            pending.erase(attempt);
                        }
                    }
                } else if(e.kind==8 || e.kind==9) {
                    if(task_clock_contract_.empty()||e.arg2||e.arg0==e.arg1||
                       e.pc!=(e.kind==8?0xffffffff81517a0aULL:0xffffffff81067eefULL))fail();
                    if(e.kind==8) {
                        if(cut||e.arg0!=owner||(known&&(!enabled||eligible!=e.arg0)))fail();
                        cut=std::make_pair(e.arg0,e.arg1);known=true;enabled=false;
                    } else {
                        if(!cut||*cut!=std::make_pair(e.arg0,e.arg1)||enabled||e.arg1!=owner)fail();
                        cut.reset();known=true;enabled=true;eligible=e.arg1;
                    }
                } else if(e.kind==6) {
                    if(!task_clock_contract_.empty()&&e.arg0!=e.arg1&&(!cut||*cut!=std::make_pair(e.arg0,e.arg1)))fail();
                    if(e.arg0!=owner) fail();
                    bind_owner(e.arg1,core);owner=e.arg1;
                } else if(!e.arg0 || e.arg2<0 || e.arg2>1) fail();
            }
        }
        if(identified.size()!=task_identity.size()) fail();
    }
    std::queue<std::size_t> ready;
    for(std::size_t i=0;i<events_.size();++i) {
        for(auto p:events_[i].predecessors)
            if(!events_[i].measurement && events_[p].measurement) fail();
        if(remaining[i]==0) ready.push(i);
    }
    std::size_t visited=0;
    while(!ready.empty()) {
        const auto i=ready.front(); ready.pop(); ++visited;
        for(auto j:successors[i]) if(--remaining[j]==0) ready.push(j);
    }
    if(visited!=events_.size()) fail();
    configure_execution_boundaries();
}
FunctionalSyncPlan::FunctionalSyncPlan(const std::string& sync_path,
                                     const std::string& state_path,
                                     std::uint32_t cores, bool batch_observations)
    : FunctionalSyncPlan(state_path, cores, batch_observations) {
    FunctionalSyncPlan sync(sync_path, cores);
    if (!thread_state_ || sync.thread_state_)
        throw std::runtime_error("combined scheduler plans have incompatible domains");
    for (std::uint32_t core=0; core<cores; ++core) {
        const auto& a=layouts_[core]; const auto& b=sync.layouts_[core];
        if (a.records!=b.records || a.warmup_records!=b.warmup_records ||
            a.measurement_records!=b.measurement_records)
            throw std::runtime_error("combined scheduler plans disagree on source bounds");
    }
    const auto state_count=events_.size();
    for (auto e:sync.events_) {
        for (auto& p:e.predecessors) p+=state_count;
        events_.push_back(std::move(e));
    }
    if (events_.size()>1000000)
        throw std::runtime_error("combined scheduler plan exceeds event limit");
    streams_.assign(cores, {});
    for (std::size_t id=0; id<events_.size(); ++id)
        streams_[events_[id].core].push_back(id);
    std::vector<std::vector<std::size_t>> successors(events_.size());
    std::vector<std::size_t> remaining(events_.size());
    for (auto& stream:streams_) {
        std::sort(stream.begin(),stream.end(),[&](auto a,auto b) {
            return events_[a].ordinal<events_[b].ordinal;
        });
        for (std::size_t i=1; i<stream.size(); ++i) {
            auto& event=events_[stream[i]];
            const auto prev=stream[i-1];
            if (events_[prev].ordinal==event.ordinal)
                throw std::runtime_error("combined scheduler plans share an ambiguous boundary");
            if (std::find(event.predecessors.begin(),event.predecessors.end(),prev)==event.predecessors.end())
                event.predecessors.push_back(prev);
        }
    }
    for (std::size_t i=0; i<events_.size(); ++i) {
        remaining[i]=events_[i].predecessors.size();
        for (auto p:events_[i].predecessors) {
            if (!events_[i].measurement && events_[p].measurement)
                throw std::runtime_error("combined scheduler graph crosses measurement backwards");
            successors[p].push_back(i);
        }
    }
    std::queue<std::size_t> ready;
    for (std::size_t i=0; i<remaining.size(); ++i) if (!remaining[i]) ready.push(i);
    std::size_t visited=0;
    while (!ready.empty()) {
        auto i=ready.front();ready.pop();++visited;
        for (auto next:successors[i]) if (!--remaining[next]) ready.push(next);
    }
    if (visited!=events_.size()) throw std::runtime_error("combined scheduler graph contains a cycle");
    observed_.assign(events_.size(),std::nullopt);
    released_.assign(events_.size(),std::nullopt);
    configure_execution_boundaries();
}
void FunctionalSyncPlan::configure_execution_boundaries() {
    execution_boundaries_.assign(events_.size(),true);
    execution_predecessors_.clear();
    execution_predecessors_.reserve(events_.size());
    for (const auto& e:events_) execution_predecessors_.push_back(e.predecessors);
    execution_boundary_count_=events_.size();
    if (!batch_observations_ || accounting_contract_.empty() ||
        !std::all_of(owner_continuity_.begin(),owner_continuity_.end(),[](bool b){ return b; })) return;
    const auto observation=[&](std::size_t id) {
        return events_[id].thread_state && events_[id].kind<=5;
    };
    // A later, required local control parent already proves completion of all
    // preceding local observations. Remove that redundant *timing* edge only;
    // immutable source dependencies and all functional anchor checks remain.
    for (auto& parents:execution_predecessors_) {
        std::map<std::uint32_t,std::uint64_t> latest_control;
        for (auto p:parents) if (!observation(p))
            latest_control[events_[p].core]=std::max(latest_control[events_[p].core],events_[p].ordinal);
        parents.erase(std::remove_if(parents.begin(),parents.end(),[&](auto p) {
            const auto control=latest_control.find(events_[p].core);
            return observation(p) && control!=latest_control.end() && control->second>events_[p].ordinal;
        }),parents.end());
    }
    for (std::size_t i=0;i<events_.size();++i) if (observation(i)) execution_boundaries_[i]=false;
    // Observations with a real cross-core consumer/parent remain execution
    // boundaries: their corrected retirement cannot be replaced by an older
    // local frontier. Ordinary local metadata never suspends a producer.
    for (std::size_t i=0;i<events_.size();++i)
        for (auto p:execution_predecessors_[i]) if(events_[p].core!=events_[i].core) {
            execution_boundaries_[p]=true;
            execution_boundaries_[i]=true;
        }
    std::vector<std::optional<std::size_t>> preceding(events_.size());
    for (const auto& stream:streams_) {
        std::optional<std::size_t> gate;
        for (auto id:stream) {
            preceding[id]=gate;
            if(execution_boundaries_[id]) gate=id;
        }
    }
    for (std::size_t i=0;i<events_.size();++i) {
        if(!execution_boundaries_[i]) continue;
        std::set<std::size_t> parents;
        for(auto p:execution_predecessors_[i]) {
            if(execution_boundaries_[p]) parents.insert(p);
            else if(preceding[p]) parents.insert(*preceding[p]);
        }
        execution_predecessors_[i].assign(parents.begin(),parents.end());
    }
    execution_boundary_count_=static_cast<std::size_t>(std::count(execution_boundaries_.begin(),execution_boundaries_.end(),true));
}
void FunctionalSyncPlan::observe(std::size_t id,std::uint64_t ready) {
    if(!requires_execution_boundary(id)) throw std::logic_error("passive observation has no execution permission boundary");
    if(observed_.at(id)) throw std::logic_error("sync event observed twice");
    observed_[id]=ready;
}
bool FunctionalSyncPlan::can_release(std::size_t id) const {
    if(!observed_.at(id) || released_.at(id)) return false;
    for(auto p:execution_predecessors_.at(id)) if(!released_.at(p)) return false;
    return true;
}
std::uint64_t FunctionalSyncPlan::predecessor_ready(std::size_t id) const {
    std::uint64_t ready=0;
    for(auto p:execution_predecessors_.at(id)) {
        if(!released_.at(p)) throw std::logic_error("sync predecessor not released");
        ready=std::max(ready,*released_[p]);
    }
    return ready;
}
std::uint64_t FunctionalSyncPlan::release(std::size_t id) {
    if(!can_release(id)) throw std::logic_error("sync event release is not ready");
    released_[id]=std::max(*observed_[id],predecessor_ready(id));
    ++released_count_;
    return *released_[id];
}
}
