#include "fastsim/spr_native_coherence.hpp"
#include <algorithm>
#include <map>
#include <set>
#include <tuple>
#include <limits>

namespace fastsim::spr {
namespace {
using S=NativeLineState;
using K=NativeController;
using Event=NativeProtocolEvent;
using Message=NativeProtocolMessage;
using QueueKey=std::tuple<std::uint64_t,std::uint32_t,std::uint64_t>;
QueueKey key(const Message& m) { return {m.ready,m.from,m.order}; }
bool child(S s) { return s==S::S || s==S::E || s==S::M || s==S::SM || s==S::S_IL0 || s==S::E_IL0 || s==S::M_IL0 || s==S::SM_IL0; }
bool child_wait(S s) { return s==S::S_IL0 || s==S::E_IL0 || s==S::M_IL0 || s==S::SM_IL0; }
bool llc_blocked(S s) { return s==S::SS_MB || s==S::MT_MB || s==S::MT_IIB || s==S::MT_IB || s==S::MT_SB; }
std::uint64_t add(std::uint64_t a,std::uint64_t b) {
    if(b>UINT64_MAX-a) throw std::overflow_error("native coherence cycle overflow");
    return a+b;
}
}
struct NativeCoherence::State {
    struct Line {
        S state=S::I;
        std::optional<Message> transaction;
        std::optional<ProtocolLease> lease;
        std::vector<Message> readers;
        std::set<std::uint32_t> sharers,expected,acks;
        std::uint32_t exclusive=UINT32_MAX;
        std::uint64_t data=0,revision=0,child_token=0;
        bool dirty=false,baseline=false,has_data=false,data_received=false;
    };
    struct Controller {
        NativeControllerId id;
        std::map<std::uint64_t,Line> lines;
        std::array<std::map<QueueKey,Message>,3> ports;
        std::map<std::uint64_t,std::vector<Message>> waiting;
        std::optional<std::uint64_t> wake;
        std::uint64_t cycle=UINT64_MAX,serial=0;
        std::uint32_t transitions=0;
        std::array<bool,3> resource_blocked{};
    };
    const SimulatorConfig& config;
    NativeCoherenceHooks hooks;
    std::vector<ProtocolCredits*> private_credits,banks;
    std::vector<Controller> controllers;
    std::set<std::pair<std::uint64_t,std::uint32_t>> calendar;
    std::vector<std::array<std::uint64_t,2>> local_last;
    std::map<std::uint64_t,Message> memory;
    std::uint64_t serial=0,data_serial=0;
    std::uint64_t current=0;

    State(const SimulatorConfig& c,NativeCoherenceHooks h,std::vector<ProtocolCredits*> p,std::vector<ProtocolCredits*> b)
        :config(c),hooks(std::move(h)),private_credits(std::move(p)),banks(std::move(b)),local_last(c.cores) {
        for(unsigned kind=0;kind<4;++kind) {
            const auto n=kind<2?c.cores:(kind==2?c.cha_count:static_cast<std::uint32_t>(c.native_coherence.directories.size()));
            for(std::uint32_t i=0;i<n;++i) { Controller x; x.id={static_cast<K>(kind),i}; controllers.push_back(std::move(x)); }
        }
    }
    std::uint32_t index(K kind,std::uint32_t n) const {
        return n+(kind==K::L0?0:(kind==K::L1?config.cores:(kind==K::Llc?2*config.cores:2*config.cores+config.cha_count)));
    }
    std::uint32_t bank(std::uint64_t line) const {return static_cast<std::uint32_t>(line&(config.cha_count-1));}
    std::uint32_t directory(std::uint64_t line) const {
        const auto address=line*config.l1d.line_size;
        std::optional<std::uint32_t> result;
        for(std::uint32_t i=0;i<config.native_coherence.directories.size();++i) {
            const auto& r=config.native_coherence.directories[i];
            if(address<r.start || address>=r.end) continue;
            std::uint64_t match=0;
            for(std::size_t bit=0;bit<r.masks.size();++bit)
                match|=static_cast<std::uint64_t>(__builtin_parityll(address&r.masks[bit]))<<bit;
            if(match==r.match) {if(result)throw std::invalid_argument("ambiguous native directory route");result=i;}
        }
        if(!result)throw std::invalid_argument("missing native directory route");
        return *result;
    }
    void audit(const char* name,const Message& m,std::uint64_t a=0,std::uint64_t b=0) {
        if(hooks.audit) hooks.audit(name,current,m.service.owner,m.line,a,b);
    }
    void schedule(std::uint32_t id,std::uint64_t when) {
        auto& c=controllers.at(id);
        if(c.wake && *c.wake<=when)return;
        if(c.wake)calendar.erase({*c.wake,id});
        c.wake=when;calendar.emplace(when,id);
    }
    unsigned port(std::uint32_t id,Event e,std::uint32_t from) const {
        const auto k=controllers[id].id.kind;
        if(k==K::L0)return (e==Event::Read || e==Event::Write)?1:0;
        if(k==K::L1) {
            if(controllers[from].id.kind==K::L0)return 2; // bufferFromL0, including demand GETS/GETX
            if(e==Event::Data || e==Event::Exclusive || e==Event::Ack || e==Event::Baseline)return 0;
            if(e==Event::GetS || e==Event::GetX || e==Event::Upgrade || e==Event::Inv)return 1;
            return 2;
        }
        if(k==K::Llc) {
            if(e==Event::Unblock || e==Event::ExclusiveUnblock)return 0;
            if(e==Event::MemoryData || e==Event::Data)return 1;
            return 2;
        }
        return e==Event::Fetch?0:2; // declaration order, not descending rank
    }
    void enqueue(Message m) {
        if(m.from>=controllers.size() || m.to>=controllers.size() || !m.token || !m.order ||
           m.service.owner.core>=config.cores || m.ready<current ||
           m.line!=m.service.line/config.l1d.line_size ||
           static_cast<unsigned>(m.event)>static_cast<unsigned>(Event::MemoryData))
            throw std::invalid_argument("invalid native protocol envelope");
        const auto from=controllers[m.from].id,to=controllers[m.to].id;
        const bool local=from.index==to.index;
        bool valid=false;
        switch(m.event) {
        case Event::Read:case Event::Write:valid=m.from==m.to && to.kind==K::L0 && to.index==m.service.owner.core;break;
        case Event::GetS:case Event::GetX:case Event::Upgrade:
            valid=(from.kind==K::L0 && to.kind==K::L1 && local) ||
                  (from.kind==K::L1 && to.kind==K::Llc && to.index==bank(m.line)) ||
                  (from.kind==K::Llc && to.kind==K::L1 && from.index==bank(m.line));break;
        case Event::Inv:valid=from.kind==K::Llc && to.kind==K::L1 && from.index==bank(m.line);break;
        case Event::InvElse:valid=from.kind==K::L1 && to.kind==K::L0 && local;break;
        case Event::ChildAck:case Event::ChildData:valid=from.kind==K::L0 && to.kind==K::L1 && local;break;
        case Event::Data:case Event::Exclusive:
            valid=(from.kind==K::L1 && to.kind==K::L0 && local) ||
                  (from.kind==K::Llc && to.kind==K::L1 && from.index==bank(m.line)) ||
                  (m.event==Event::Data && from.kind==K::L1 &&
                   ((to.kind==K::L1 && m.from!=m.to) || (to.kind==K::Llc && to.index==bank(m.line))));break;
        case Event::Stale:valid=from.kind==K::L1 && to.kind==K::L0 && local;break;
        case Event::Ack:valid=from.kind==K::L1 && to.kind==K::L1 && m.from!=m.to;break;
        case Event::Baseline:valid=from.kind==K::Llc && to.kind==K::L1 && from.index==bank(m.line);break;
        case Event::Unblock:case Event::ExclusiveUnblock:
            valid=from.kind==K::L1 && to.kind==K::Llc && to.index==bank(m.line) && from.index==m.service.owner.core;break;
        case Event::Fetch:valid=from.kind==K::Llc && to.kind==K::Directory && to.index==directory(m.line) && from.index==bank(m.line);break;
        case Event::MemoryData:
            valid=(from.kind==K::Directory && to.kind==K::Llc && from.index==directory(m.line) && to.index==bank(m.line)) ||
                  (m.from==m.to && to.kind==K::Directory && to.index==directory(m.line));break;
        }
        if(!valid)throw std::invalid_argument("invalid native protocol sender/route");
        for(auto sender:m.acknowledgers)
            if(sender<config.cores || sender>=2*config.cores || sender==m.to)
                throw std::invalid_argument("invalid native ACK obligation");
        auto& c=controllers.at(m.to);m.port=port(m.to,m.event,m.from);
        if(!c.ports[m.port].emplace(key(m),m).second)throw std::logic_error("duplicate native envelope identity");
        schedule(m.to,std::max(current,m.ready));
    }
    void send(const Message& origin,Event e,std::uint32_t from,std::uint32_t to,
              std::uint64_t delay,std::uint64_t data=0,bool dirty=false,bool owner_data=false,
              std::set<std::uint32_t> expected={}) {
        Message m=origin;m.event=e;m.from=from;m.to=to;m.ready=add(current,delay);
        m.order=++controllers.at(from).serial;m.data=data;m.dirty=dirty;m.owner_data=owner_data;
        m.acknowledgers=std::move(expected);
        const auto a=controllers[from].id,b=controllers[to].id;
        if(a.index==b.index && ((a.kind==K::L0 && b.kind==K::L1)||(a.kind==K::L1 && b.kind==K::L0))) {
            auto& last=local_last[a.index][a.kind==K::L0?0:1];m.ready=std::max(m.ready,last);last=m.ready;
        }
        if(hooks.sent)hooks.sent(m);
        enqueue(std::move(m));
    }
    unsigned rank(const Controller& c,unsigned p) const {return c.id.kind==K::Directory?p:((c.id.kind==K::L0)?1-p:2-p);}
    void wake(std::uint32_t id,std::uint64_t line,unsigned source_port) {
        auto& c=controllers[id];auto f=c.waiting.find(line);if(f==c.waiting.end())return;
        auto& list=f->second;
        for(auto it=list.begin();it!=list.end();) {
            if(c.id.kind==K::L0 || c.id.kind==K::L1 || rank(c,it->port)<rank(c,source_port)) {
                auto m=*it;it=list.erase(it);c.ports[m.port].emplace(key(m),m);schedule(id,current);
            } else ++it;
        }
        if(list.empty())c.waiting.erase(f);
    }
    void park(Controller& c,const Message& m) {c.waiting[m.line].push_back(m);audit("coherence_park",m,static_cast<unsigned>(c.id.kind),static_cast<unsigned>(m.event));}
    ProtocolCredits& credits(const Controller& c) {
        return *(c.id.kind==K::Llc?banks.at(c.id.index):private_credits.at(c.id.index));
    }
    ProtocolLease lease(const Controller& c,const Message& m,bool fill) const {
        return {m.service.owner,m.token,{c.id.kind==K::L0?ProtocolLevel::L1:(c.id.kind==K::L1?ProtocolLevel::L2:ProtocolLevel::Llc),true,fill}};
    }
    bool can_allocate(Controller& c,const Message& m,bool fill) {
        if(fill) {
            const auto allocation=hooks.allocation(c.id,m.line);
            if(allocation.victim.evicted)throw std::invalid_argument("native coherence replacement unsupported before transition mutation");
            if(!allocation.available)return false;
        }
        return credits(c).peek(lease(c,m,fill && c.id.kind!=K::Llc))==ProtocolStatus::Ready;
    }
    void allocate(Controller& c,Line& l,const Message& m,bool fill) {
        auto owned=lease(c,m,fill && c.id.kind!=K::Llc);
        if(!credits(c).acquire(owned))throw std::logic_error("native transition lease changed after precheck");
        l.lease=owned;l.transaction=m;l.baseline=false;l.has_data=!fill;l.data_received=false;l.expected.clear();l.acks.clear();
        if(fill)hooks.reserve(c.id,m.line,current);
        audit("coherence_acquire",m,static_cast<unsigned>(c.id.kind),fill);
    }
    void release(Controller& c,Line& l,const Message& m) {
        if(!l.lease || !credits(c).release(*l.lease))throw std::logic_error("native protocol duplicate lease release");
        audit("coherence_release",m,static_cast<unsigned>(c.id.kind),l.lease->demand.data_fill);l.lease.reset();
    }
    void require_transaction(const Line& l,const Message& m) {
        if(!l.transaction || l.transaction->token!=m.token ||
           l.transaction->service.owner!=m.service.owner || l.transaction->service.generation!=m.service.generation)throw std::logic_error("stale native transaction response");
    }
    void transition(Controller& c,Line& l,const Message& m,S next) {
        const auto prior=l.state;l.state=next;++l.revision;
        audit("coherence_transition",m,(static_cast<std::uint64_t>(c.id.kind)<<32)|static_cast<unsigned>(prior),static_cast<unsigned>(next));
    }
    bool l0(std::uint32_t id,Controller& c,Line& l,const Message& m);
    bool l1(std::uint32_t id,Controller& c,Line& l,const Message& m);
    bool llc(std::uint32_t id,Controller& c,Line& l,const Message& m);
    bool dir(std::uint32_t id,Controller& c,Line& l,const Message& m);
    void run(std::uint32_t id);
};

bool NativeCoherence::State::l0(std::uint32_t id,Controller& c,Line& l,const Message& m) {
    const auto parent=index(K::L1,c.id.index);
    if(m.event==Event::Read || m.event==Event::Write) {
        const bool write=m.event==Event::Write;
        if(l.state==S::IS || l.state==S::IM || l.state==S::SM) {park(c,m);return true;}
        if(l.state==S::I || (l.state==S::S && write)) {
            const bool fill=l.state==S::I;
            if(!can_allocate(c,m,fill))return false;
            allocate(c,l,m,fill);hooks.access(c.id,m.line,current,false);
            const auto e=!write?Event::GetS:(fill?Event::GetX:Event::Upgrade);
            transition(c,l,m,!write?S::IS:(fill?S::IM:S::SM));
            send(m,e,id,parent,*config.causal_l1_request_latency);
            audit(e==Event::GetS?"coherence_gets":(e==Event::GetX?"coherence_getx":"coherence_upgrade"),m);
            return true;
        }
        if(l.state!=S::S && l.state!=S::E && l.state!=S::M)throw std::logic_error("invalid native L0 demand state");
        hooks.access(c.id,m.line,current,false);
        if(write && l.state==S::E)transition(c,l,m,S::M);
        const bool wrote=hooks.callback(m.service,current);
        if(wrote) {l.data=++data_serial;l.dirty=true;if(l.state!=S::M)transition(c,l,m,S::M);audit("coherence_write",m,l.data,c.id.index);}
        return true;
    }
    if(m.event==Event::InvElse) {
        if(l.state==S::M)send(m,Event::ChildData,id,parent,*config.causal_l1_response_latency,l.data,l.dirty);
        else send(m,Event::ChildAck,id,parent,*config.causal_l1_response_latency);
        if(l.state==S::S || l.state==S::E || l.state==S::M) {
            hooks.invalidate(c.id,m.line,current);transition(c,l,m,S::I);l.dirty=false;
        } else if(l.state==S::SM) {transition(c,l,m,S::IM);}
        hooks.eviction(c.id.index,m.line,current);audit("coherence_child_invalidate",m,l.data);
        return true;
    }
    if(m.event==Event::Data || m.event==Event::Exclusive || m.event==Event::Stale) {
        require_transaction(l,m);
        if(l.state!=S::IS && l.state!=S::IM && l.state!=S::SM)throw std::logic_error("native L0 data outside transient");
        const bool write=l.transaction->event==Event::Write;
        if(write && m.event!=Event::Exclusive)throw std::logic_error("native write callback without exclusive data");
        hooks.install(c.id,m.line,current,m.dirty);l.data=m.data;l.dirty=m.dirty;
        transition(c,l,m,m.event==Event::Stale?S::I:(write?S::M:(m.event==Event::Exclusive?S::E:S::S)));
        release(c,l,m);
        const auto service=l.transaction->service;l.transaction.reset();
        const bool wrote=hooks.callback(service,current);
        if(wrote) {l.data=++data_serial;l.dirty=true;audit("coherence_write",m,l.data,c.id.index);}
        if(m.event==Event::Stale) {hooks.invalidate(c.id,m.line,current);hooks.eviction(c.id.index,m.line,current);}
        wake(id,m.line,m.port);return true;
    }
    throw std::logic_error("unsupported native L0 event");
}

bool NativeCoherence::State::l1(std::uint32_t id,Controller& c,Line& l,const Message& m) {
    const auto child_id=index(K::L0,c.id.index),llc_id=index(K::Llc,bank(m.line));
    const bool from_child=m.from==child_id;
    if(from_child && (m.event==Event::GetS || m.event==Event::GetX || m.event==Event::Upgrade)) {
        const bool read=m.event==Event::GetS;
        if(l.state==S::I || ((l.state==S::S || l.state==S::SS) && !read)) {
            const bool fill=l.state==S::I;
            if(!can_allocate(c,m,fill))return false;
            allocate(c,l,m,fill);hooks.access(c.id,m.line,current,false);
            transition(c,l,m,fill?(read?S::IS:S::IM):S::SM);
            send(m,read?Event::GetS:(fill?Event::GetX:Event::Upgrade),id,llc_id,add(*config.causal_l2_request_latency,config.noc_one_way_latency));
            return true;
        }
        if(l.state==S::S || l.state==S::SS || l.state==S::E || l.state==S::EE || l.state==S::M || l.state==S::MM) {
            const bool shared=l.state==S::S || l.state==S::SS;
            hooks.access(c.id,m.line,current,false);
            transition(c,l,m,shared?S::S:((l.state==S::E || l.state==S::EE)&&read?S::E:S::M));
            send(m,shared?Event::Data:Event::Exclusive,id,child_id,*config.causal_l2_response_latency,l.data,shared?false:l.dirty);
            return true;
        }
        park(c,m);return true;
    }
    if(m.event==Event::ChildAck || m.event==Event::ChildData) {
        if(!child_wait(l.state))throw std::logic_error("unexpected child invalidation response");
        if(l.child_token!=m.token)throw std::logic_error("stale child response");
        if(m.event==Event::ChildData) {l.data=m.data;l.dirty=m.dirty;hooks.install(c.id,m.line,current,m.dirty);}
        const auto next=l.state==S::S_IL0?S::SS:(l.state==S::SM_IL0?S::IM:((l.state==S::E_IL0 && m.event==Event::ChildAck)?S::EE:S::MM));
        // The transaction for an upgrade remains its own demand. The remote
        // request is parked independently and must be resumed in port order.
        if(next==S::IM)l.has_data=false;
        l.child_token=0;transition(c,l,m,next);wake(id,m.line,m.port);return true;
    }
    if(!from_child && (m.event==Event::GetS || m.event==Event::GetX || m.event==Event::Upgrade || m.event==Event::Inv)) {
        if(child_wait(l.state)) {park(c,m);return true;}
        if(child(l.state)) {
            if(l.state!=S::SM)l.transaction=m;
            l.child_token=m.token;
            // For SM preserve demand identity; child ACK echoes the remote
            // transaction, stored separately in the parked message below.
            const auto next=l.state==S::S?S::S_IL0:(l.state==S::E?S::E_IL0:(l.state==S::M?S::M_IL0:S::SM_IL0));
            transition(c,l,m,next);send(m,Event::InvElse,id,child_id,*config.causal_l2_request_latency);park(c,m);return true;
        }
        if(l.state==S::I || l.state==S::IS || l.state==S::IS_I || l.state==S::IM) {
            if(m.event!=Event::Inv)throw std::logic_error("forward request lacks native owner");
            send(m,Event::Ack,id,index(K::L1,m.service.owner.core),add(*config.causal_l2_response_latency,config.noc_one_way_latency));
            if(l.state==S::IS)transition(c,l,m,S::IS_I);
            return true;
        }
        if(l.state==S::SS && m.event==Event::Inv) {
            send(m,Event::Ack,id,index(K::L1,m.service.owner.core),add(*config.causal_l2_response_latency,config.noc_one_way_latency));
            hooks.invalidate(c.id,m.line,current);transition(c,l,m,S::I);l.transaction.reset();return true;
        }
        if(l.state!=S::EE && l.state!=S::MM)throw std::logic_error("native owner forwarding without current private data");
        const auto dest=index(K::L1,m.service.owner.core);
        send(m,Event::Data,id,dest,add(*config.causal_l2_response_latency,config.noc_one_way_latency),l.data,l.dirty,m.event==Event::GetS);
        audit("coherence_owner_data",m,l.data,c.id.index);
        if(m.event==Event::GetS) {
            send(m,Event::Data,id,llc_id,add(*config.causal_l2_response_latency,config.noc_one_way_latency),l.data,l.dirty,true);
            transition(c,l,m,S::SS);
        } else {hooks.invalidate(c.id,m.line,current);transition(c,l,m,S::I);}
        l.transaction.reset();return true;
    }
    if(m.event==Event::Data || m.event==Event::Exclusive || m.event==Event::Ack || m.event==Event::Baseline) {
        require_transaction(l,m);
        if(child_wait(l.state)) {park(c,m);return true;}
        auto expected=l.expected,acks=l.acks;
        bool baseline=l.baseline,has_data=l.has_data;
        if(m.event==Event::Ack) {
            if(!acks.insert(m.from).second)throw std::logic_error("duplicate native ACK sender");
        } else if(m.event==Event::Baseline) {
            if(baseline)throw std::logic_error("duplicate native ACK baseline");
            baseline=true;expected=m.acknowledgers;
        } else {
            if(l.data_received)throw std::logic_error("duplicate native data response");
            has_data=true;baseline=true;expected=m.acknowledgers;
        }
        if(baseline)for(auto sender:acks)if(!expected.count(sender))throw std::logic_error("unexpected native ACK sender");
        l.expected=std::move(expected);l.acks=std::move(acks);l.baseline=baseline;l.has_data=has_data;
        if(m.event==Event::Data || m.event==Event::Exclusive) {
            l.data_received=true;l.data=m.data;l.dirty=m.dirty;
            // Native u_writeDataFromL2Response runs on data arrival even if
            // ACKs remain. Preserve the transient slot and typed lease.
            hooks.update_data(c.id,m.line,current,l.dirty);
        }
        audit("coherence_ack_progress",m,l.acks.size(),l.baseline?l.expected.size():UINT64_MAX);
        if(!l.baseline || !l.has_data || l.acks!=l.expected) {
            if(l.state==S::IM && l.has_data)transition(c,l,m,S::SM);
            return true;
        }
        const auto request=*l.transaction;
        const bool read=request.event==Event::GetS;
        const bool stale=l.state==S::IS_I && m.event!=Event::Exclusive;
        // Ack_all/baseline completion has no native data-writing action.
        // Finish only the slot reservation/MRU, retaining its data metadata.
        hooks.finish_data(c.id,m.line,current);
        audit("coherence_retained_data",m,l.dirty,static_cast<unsigned>(m.event));
        if(read && m.event!=Event::Exclusive) {
            if(m.owner_data)send(request,Event::Unblock,id,llc_id,add(config.native_coherence.private_unblock_latency,config.noc_one_way_latency));
            send(request,stale?Event::Stale:Event::Data,id,child_id,*config.causal_l2_response_latency,l.data,stale?l.dirty:false);
            transition(c,l,m,stale?S::I:S::S);
        } else {
            send(request,Event::ExclusiveUnblock,id,llc_id,add(config.native_coherence.private_unblock_latency,config.noc_one_way_latency));
            send(request,Event::Exclusive,id,child_id,*config.causal_l2_response_latency,l.data,l.dirty);
            transition(c,l,m,read?S::E:S::M);
        }
        release(c,l,m);l.transaction.reset();
        if(stale)hooks.invalidate(c.id,m.line,current);
        wake(id,m.line,m.port);return true;
    }
    throw std::logic_error("unsupported native private-L1 event");
}

bool NativeCoherence::State::llc(std::uint32_t id,Controller& c,Line& l,const Message& m) {
    if(m.event==Event::Upgrade && !l.sharers.count(m.from)) {
        auto converted=m;converted.event=Event::GetX;
        return llc(id,c,l,converted); // native request_type_to_event:291-295
    }
    if(m.event==Event::GetS || m.event==Event::GetX || m.event==Event::Upgrade) {
        if(l.state==S::NP) {
            if(m.event==Event::Upgrade)throw std::logic_error("upgrade without LLC shared copy");
            // Routing validation is part of the proposal, before any lease/tag.
            const auto dir_id=index(K::Directory,directory(m.line));
            if(!can_allocate(c,m,true))return false;
            allocate(c,l,m,true);l.sharers.clear();l.readers.clear();
            if(m.event==Event::GetS){l.readers.push_back(m);l.sharers.insert(m.from);}
            transition(c,l,m,m.event==Event::GetS?S::ISS:S::IM);
            send(m,Event::Fetch,id,dir_id,add(config.native_coherence.llc_request_latency,config.noc_one_way_latency));
            return true;
        }
        if((l.state==S::ISS || l.state==S::IS) && m.event==Event::GetS) {
            if(!l.sharers.insert(m.from).second)throw std::logic_error("duplicate native LLC reader controller");
            l.readers.push_back(m);transition(c,l,m,S::IS);audit("coherence_merged_fetch",m);return true;
        }
        if(l.state==S::ISS || l.state==S::IS || l.state==S::IM || llc_blocked(l.state)){park(c,m);return true;}
        if(l.state==S::SS) {
            hooks.access(c.id,m.line,current,false);
            if(m.event==Event::GetS) {
                send(m,Event::Data,id,m.from,add(config.native_coherence.llc_response_latency,config.noc_one_way_latency),l.data,l.dirty);
                l.sharers.insert(m.from);return true;
            }
            if(m.event==Event::Upgrade && !l.sharers.count(m.from))throw std::logic_error("native upgrade requester is not a sharer");
            auto expected=l.sharers;expected.erase(m.from);l.transaction=m;
            for(auto peer:expected)send(m,Event::Inv,id,peer,add(*config.causal_llc_to_l2_latency,config.noc_one_way_latency));
            send(m,m.event==Event::Upgrade?Event::Baseline:Event::Data,id,m.from,
                 add(m.event==Event::Upgrade?*config.causal_llc_to_l2_latency:config.native_coherence.llc_response_latency,config.noc_one_way_latency),m.event==Event::Upgrade?0:l.data,m.event==Event::Upgrade?false:l.dirty,false,expected);
            transition(c,l,m,S::SS_MB);return true;
        }
        if(l.state==S::MT) {
            if(l.exclusive==UINT32_MAX || l.exclusive==m.from)throw std::logic_error("invalid exclusive native forwarding route");
            l.transaction=m;
            send(m,m.event,id,l.exclusive,add(*config.causal_llc_to_l2_latency,config.noc_one_way_latency));
            transition(c,l,m,m.event==Event::GetS?S::MT_IIB:S::MT_MB);audit("coherence_forward",m,l.exclusive);return true;
        }
        throw std::logic_error("unreachable native LLC request state");
    }
    if(m.event==Event::MemoryData) {
        require_transaction(l,m);
        if(l.state!=S::ISS && l.state!=S::IS && l.state!=S::IM)throw std::logic_error("native memory data without miss");
        hooks.install(c.id,m.line,current,m.dirty);l.data=m.data;l.dirty=false;
        const auto prior=l.state;
        if(prior==S::IM)send(*l.transaction,Event::Data,id,l.transaction->from,add(*config.causal_llc_to_l2_latency,config.noc_one_way_latency),l.data);
        else for(const auto& reader:l.readers)send(reader,prior==S::ISS?Event::Exclusive:Event::Data,id,reader.from,add(*config.causal_llc_to_l2_latency,config.noc_one_way_latency),l.data);
        release(c,l,m);l.readers.clear();transition(c,l,m,prior==S::IS?S::SS:S::MT_MB);
        if(prior==S::IS){l.transaction.reset();wake(id,m.line,m.port);}return true;
    }
    if(m.event==Event::ExclusiveUnblock) {
        require_transaction(l,m);
        if(l.state!=S::SS_MB && l.state!=S::MT_MB)throw std::logic_error("unexpected native exclusive unblock");
        l.sharers={m.from};l.exclusive=m.from;transition(c,l,m,S::MT);l.transaction.reset();wake(id,m.line,m.port);return true;
    }
    if(m.event==Event::Unblock || m.event==Event::Data) {
        require_transaction(l,m);
        if(m.event==Event::Unblock) {
            if(l.state!=S::MT_IIB && l.state!=S::MT_SB)throw std::logic_error("duplicate native shared unblock");
            l.sharers.insert(m.from);transition(c,l,m,l.state==S::MT_IIB?S::MT_IB:S::SS);
        } else {
            if(l.state!=S::MT_IIB && l.state!=S::MT_IB)throw std::logic_error("duplicate native owner data");
            if(m.from!=l.exclusive)throw std::logic_error("native owner data from nonowner");
            l.data=m.data;l.dirty=m.dirty;hooks.install(c.id,m.line,current,m.dirty);transition(c,l,m,l.state==S::MT_IIB?S::MT_SB:S::SS);
        }
        if(l.state==S::SS){l.exclusive=UINT32_MAX;l.transaction.reset();wake(id,m.line,m.port);}return true;
    }
    throw std::logic_error("unsupported native LLC event");
}

bool NativeCoherence::State::dir(std::uint32_t id,Controller& c,Line& l,const Message& m) {
    if(m.event==Event::Fetch) {
        if(l.state==S::IM){park(c,m);return true;}
        if(l.state!=S::I)throw std::invalid_argument("native Directory recall/replacement unsupported");
        if(memory.count(m.token))throw std::logic_error("duplicate native memory transaction");
        l.transaction=m;transition(c,l,m,S::IM);
        if(!memory.emplace(m.token,m).second)throw std::logic_error("duplicate native memory transaction");
        hooks.memory(m.token,m.service.owner,m.line,add(current,config.native_coherence.directory_memory_latency));
        audit("coherence_memory_fetch",m);return true;
    }
    if(m.event==Event::MemoryData) {
        require_transaction(l,m);if(l.state!=S::IM)throw std::logic_error("native directory stale memory data");
        const auto request=*l.transaction;
        send(request,Event::MemoryData,id,request.from,add(config.native_coherence.directory_memory_latency,config.noc_one_way_latency),0,false);
        l.exclusive=request.from;transition(c,l,m,S::M);l.transaction.reset();wake(id,m.line,m.port);return true;
    }
    throw std::logic_error("unsupported native directory event");
}

void NativeCoherence::State::run(std::uint32_t id) {
    auto& c=controllers.at(id);
    if(c.cycle!=current){c.cycle=current;c.transitions=0;}
    const auto limit=config.native_coherence.transitions[static_cast<unsigned>(c.id.kind)];
    bool resource_stall=false;
    while(c.transitions<limit) {
        bool progressed=false;
        for(unsigned p=0;p<3;++p) {
            auto& q=c.ports[p];if(c.resource_blocked[p] || q.empty() || std::get<0>(q.begin()->first)>current)continue;
            const auto m=q.begin()->second;
            auto found=c.lines.find(m.line);
            // No insertion on a resource/refusal path. Commit the local copy
            // only after a transition accepts; all outgoing actions are atomic
            // within this serialized continuation.
            Line fresh;if(c.id.kind==K::Llc)fresh.state=S::NP;
            Line& line=found==c.lines.end()?fresh:found->second;
            bool accepted=false;
            switch(c.id.kind) {
            case K::L0:accepted=l0(id,c,line,m);break;
            case K::L1:accepted=l1(id,c,line,m);break;
            case K::Llc:accepted=llc(id,c,line,m);break;
            case K::Directory:accepted=dir(id,c,line,m);break;
            }
            if(!accepted){resource_stall=true;c.resource_blocked[p]=true;continue;}
            const bool discard=line.state==S::I && !line.lease && !line.transaction && !c.waiting.count(m.line);
            if(found==c.lines.end()) {if(!discard)c.lines.emplace(m.line,std::move(fresh));}
            else if(discard)c.lines.erase(found);
            audit("coherence_service",m,(static_cast<std::uint64_t>(c.id.kind)<<48)|(static_cast<std::uint64_t>(c.id.index)<<32)|static_cast<unsigned>(m.event),m.port);
            q.erase(key(m));++c.transitions;progressed=true;c.resource_blocked.fill(false);break;
        }
        if(!progressed)break;
    }
    if(c.wake && *c.wake==current) {calendar.erase({current,id});c.wake.reset();}
    bool ready=false;std::optional<std::uint64_t> future;
    for(unsigned p=0;p<3;++p)if(!c.ports[p].empty() && !c.resource_blocked[p]) {
        const auto& q=c.ports[p];
        const auto at=std::get<0>(q.begin()->first);
        if(at<=current)ready=true;else if(!future || at<*future)future=at;
    }
    if(ready) {
        if(c.transitions==limit) {
            Message witness;witness.to=id;witness.service.owner.core=c.id.index;
            audit("coherence_budget_defer",witness,static_cast<unsigned>(c.id.kind),limit);
        } else if(!resource_stall)throw std::logic_error("native controller made no progress");
        schedule(id,add(current,1));
    } else if(future)schedule(id,*future);
}
NativeCoherence::NativeCoherence(const SimulatorConfig& c,NativeCoherenceHooks h,std::vector<ProtocolCredits*> p,std::vector<ProtocolCredits*> b)
    :state_(std::make_unique<State>(c,std::move(h),std::move(p),std::move(b))) {c.native_coherence.validate();}
NativeCoherence::~NativeCoherence()=default;
void NativeCoherence::request(const SequencerService& service,std::uint64_t ready) {
    auto& s=*state_;
    if(service.owner.core>=s.config.cores || service.line%s.config.l1d.line_size || !service.generation ||
       (service.kind!=SequencerKind::Read && service.kind!=SequencerKind::Write))
        throw std::invalid_argument("unsupported native ordinary service");
    if(ready<s.current)throw std::logic_error("native mandatory source behind frontier");
    if(s.serial==UINT64_MAX)throw std::overflow_error("native transaction generation overflow");
    Message m;m.event=service.kind==SequencerKind::Write?Event::Write:Event::Read;
    m.service=service;m.line=service.line/s.config.l1d.line_size;m.token=++s.serial;
    m.from=m.to=s.index(K::L0,service.owner.core);m.ready=ready;m.order=++s.controllers[m.from].serial;
    s.enqueue(std::move(m));
}
void NativeCoherence::memory_response(std::uint64_t token,std::uint64_t ready) {
    auto& s=*state_;if(ready<s.current)throw std::logic_error("native memory response behind frontier");
    auto f=s.memory.find(token);
    if(f==s.memory.end())throw std::logic_error("unowned native memory completion");
    auto m=f->second;s.memory.erase(f);m.event=Event::MemoryData;m.ready=ready;
    m.from=m.to;m.order=++s.controllers[m.to].serial;s.enqueue(std::move(m));
}
void NativeCoherence::receive(NativeProtocolMessage message) {state_->enqueue(std::move(message));}
void NativeCoherence::advance(std::uint64_t cycle) {
    auto& s=*state_;if(cycle<s.current)throw std::logic_error("native protocol frontier regressed");s.current=cycle;
    while(!s.calendar.empty() && s.calendar.begin()->first<=cycle) {
        const auto [at,id]=*s.calendar.begin();s.calendar.erase(s.calendar.begin());
        if(at<cycle)throw std::logic_error("native controller wake skipped by frontier");
        s.controllers[id].wake.reset();s.run(id);
    }
}
std::optional<std::uint64_t> NativeCoherence::next_cycle() const {
    return state_->calendar.empty()?std::nullopt:std::optional<std::uint64_t>(state_->calendar.begin()->first);
}
bool NativeCoherence::idle() const {
    if(!state_->calendar.empty() || !state_->memory.empty())return false;
    for(const auto& c:state_->controllers) {
        if(!c.waiting.empty())return false;
        for(const auto& q:c.ports)if(!q.empty())return false;
    }
    return true;
}
void NativeCoherence::validate() const {
    if(!idle())throw std::logic_error("native coherence retained protocol work at terminal frontier");
    for(const auto& c:state_->controllers)for(const auto& item:c.lines)
        if(item.second.lease || item.second.transaction)throw std::logic_error("native coherence leaked line transaction");
    for(auto* p:state_->private_credits)if(p) {p->validate();if(p->live_leases())throw std::logic_error("native private protocol retained terminal credit");}
    for(auto* p:state_->banks) {p->validate();if(p->live_leases())throw std::logic_error("native bank retained terminal credit");}
}
} // namespace fastsim::spr
