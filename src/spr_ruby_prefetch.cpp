/*
 * Copyright (c) 2020 Inria
 * Copyright (c) 2020 ARM Limited
 * All rights reserved
 *
 * The license below extends only to copyright in the software and shall
 * not be construed as granting a license to any other intellectual
 * property including but not limited to intellectual property relating
 * to a hardware implementation of the functionality of the software
 * licensed hereunder.  You may use the software subject to the license
 * terms below provided that you ensure that this notice is replicated
 * unmodified and in its entirety in all distributions of the software,
 * modified or unmodified, in source code or in binary form.
 *
 * Copyright (c) 1999-2012 Mark D. Hill and David A. Wood
 * All rights reserved.
 *
 * Redistribution and use in source and binary forms, with or without
 * modification, are permitted provided that the following conditions are
 * met: redistributions of source code must retain the above copyright
 * notice, this list of conditions and the following disclaimer;
 * redistributions in binary form must reproduce the above copyright
 * notice, this list of conditions and the following disclaimer in the
 * documentation and/or other materials provided with the distribution;
 * neither the name of the copyright holders nor the names of its
 * contributors may be used to endorse or promote products derived from
 * this software without specific prior written permission.
 *
 * THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
 * "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
 * LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
 * A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
 * OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
 * SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
 * LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
 * DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
 * THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
 * (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
 * OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
 */

// Candidate policy adapted from the selected gem5-spr RubyPrefetcher.
// Stream requestIssued/requestCompleted bits have no writers in that source;
// an observed miss matching its startup window therefore suppresses training.
#include "fastsim/spr_ruby_prefetch.hpp"
#include <algorithm>
#include <array>
#include <deque>
#include <limits>
#include <stdexcept>
#include <unordered_set>

namespace fastsim::spr {
struct RubyPrefetcher::Impl {
    struct Unit {std::uint64_t address=0;std::uint32_t hits=0;};
    struct NonUnit {std::uint64_t address=0;std::uint32_t hits=0;std::int64_t stride=0;};
    struct Stream {std::uint64_t address=0,use=0;std::int64_t stride=0;bool valid=false;};
    struct PcStride {
        bool valid=false;std::int32_t context=-1;
        std::uint64_t pc=0,last_line=0,use=0;std::int64_t stride=0;unsigned samples=0;
    };
    RubyPrefetchProfile profile;
    RubyPrefetchStats counts;
    std::deque<Unit> positive,negative;
    std::deque<NonUnit> nonunit;
    std::vector<Stream> streams;
    std::array<PcStride,64> pc_stride{};
    std::unordered_set<std::uint64_t> queued;
    std::uint64_t cycle=0,pc_clock=0;
    explicit Impl(RubyPrefetchProfile p):profile(p),streams(p.num_streams){}
    void advance(std::uint64_t now) {
        if(now<cycle)throw std::logic_error("prefetch model event time regressed");
        cycle=now;
    }
    std::uint64_t line(std::uint64_t value)const{return value&~(std::uint64_t(profile.block_size)-1);}
    std::uint64_t page(std::uint64_t value)const{return value&~((std::uint64_t(1)<<profile.page_shift)-1);}
    std::uint64_t next(std::uint64_t value,std::int64_t stride)const {
        // Addr arithmetic in Ruby wraps modulo 2^64; signed multiplication
        // here is converted before arithmetic to avoid host signed overflow.
        return value+std::uint64_t(stride)*profile.block_size;
    }
    void candidate(std::uint64_t address,PrefetchOrigin origin,PendingOwner trigger,std::vector<PrefetchCandidate>& out) {
        address=line(address);
        if(profile.queue_dedup && !queued.insert(address).second){++counts.duplicate_candidates;return;}
        out.push_back({address,origin,trigger});++counts.requested;
        if(origin==PrefetchOrigin::Stream)++counts.stream_candidates;
        else if(origin==PrefetchOrigin::IpStride)++counts.ip_candidates;
        else ++counts.adjacent_candidates;
    }
    template<class T> void push(std::deque<T>& q,T value,unsigned capacity) {
        if(q.size()==capacity)q.pop_front();
        q.push_back(value);
    }
    Stream* matching(std::uint64_t address) {
        for(auto& s:streams)if(s.valid)
            for(unsigned j=0;j<profile.num_startup_pfs;++j)
                if(next(s.address,-s.stride*std::int64_t(j))==address)return &s;
        return nullptr;
    }
    Stream& victim() {
        auto* selected=&streams.front();
        for(auto& s:streams){if(!s.valid)return s;if(s.use<selected->use)selected=&s;}
        return *selected;
    }
    void initialize(std::uint64_t address,std::int64_t stride,PendingOwner trigger,std::vector<PrefetchCandidate>& out) {
        ++counts.streams_allocated;
        auto& stream=victim();stream={line(address),cycle,stride,true};
        const auto original_page=page(stream.address);auto target=stream.address;
        for(unsigned n=0;n<profile.num_startup_pfs;++n) {
            target=next(target,stride);
            if(page(target)!=original_page) {
                if(!profile.cross_page){stream.valid=false;return;}
                ++counts.pages_crossed;
            }
            candidate(target,PrefetchOrigin::Stream,trigger,out);
        }
        stream.address=target;
    }
    bool unit(std::deque<Unit>& filter,std::uint64_t address,std::int64_t stride,PendingOwner trigger,std::vector<PrefetchCandidate>& out) {
        for(auto& e:filter)if(e.address==address) {
            e.address=next(e.address,stride);++e.hits;
            if(e.hits>=profile.train_misses)initialize(address,stride,trigger,out);
            return true;
        }
        push(filter,Unit{next(address,stride),0},profile.unit_filter);return false;
    }
    void miss(std::uint64_t address,PendingOwner trigger,std::vector<PrefetchCandidate>& out) {
        address=line(address);++counts.misses_observed;
        if(matching(address))return;
        if(unit(positive,address,1,trigger,out) || unit(negative,address,-1,trigger,out))return;
        for(auto& e:nonunit)if(page(e.address)==page(address)) {
            const auto delta=std::int64_t(address-page(address))-std::int64_t(e.address-page(e.address));
            if(!delta)return;
            if(delta==e.stride) {
                ++e.hits;
                if(e.hits>profile.train_misses){const auto stride=e.stride/profile.block_size;e={};initialize(address,stride,trigger,out);}
            } else e.hits=0;
            e.address=address;e.stride=delta;return;
        }
        push(nonunit,NonUnit{address,0,0},profile.nonunit_filter);
    }
    void extend(std::uint64_t address,PendingOwner trigger,std::vector<PrefetchCandidate>& out) {
        auto* stream=matching(line(address));if(!stream)return;
        const auto target=next(stream->address,stream->stride);
        if(page(target)!=page(stream->address)) {
            if(!profile.cross_page){stream->valid=false;return;}
            ++counts.pages_crossed;
        }
        stream->address=target;stream->use=cycle;candidate(target,PrefetchOrigin::Stream,trigger,out);
    }
    static bool eligible(const PrefetchAccess& a) {
        if(!a.complete)throw std::invalid_argument("prefetch request eligibility is undeclared");
        return a.physical && !a.uncacheable && !a.strictly_ordered && !a.instruction && !a.prefetch && !a.page_walk && !a.secure;
    }
};
RubyPrefetcher::RubyPrefetcher(RubyPrefetchProfile p) {
    if(!p.block_size || (p.block_size&(p.block_size-1)) || !p.page_shift || p.page_shift>30 ||
       p.block_size>(std::uint64_t(1)<<p.page_shift) || !p.num_streams || !p.unit_filter || !p.nonunit_filter ||
       !p.train_misses || p.num_startup_pfs>8)
        throw std::invalid_argument("invalid native prefetch geometry");
    impl_=std::make_unique<Impl>(p);
}
RubyPrefetcher::~RubyPrefetcher()=default;
RubyPrefetcher::RubyPrefetcher(RubyPrefetcher&&) noexcept=default;
RubyPrefetcher& RubyPrefetcher::operator=(RubyPrefetcher&&) noexcept=default;
std::vector<PrefetchCandidate> RubyPrefetcher::observe_data_access(const PrefetchAccess& a,std::uint64_t cycle) {
    auto& s=*impl_;std::vector<PrefetchCandidate> out;
    if(!s.profile.ip_stride){s.advance(cycle);return out;}
    const bool eligible=Impl::eligible(a);s.advance(cycle);
    if(!eligible || !a.pc || !a.virtual_address)return out;
    const auto line=s.line(*a.virtual_address),physical=s.line(*a.physical),pc=*a.pc;
    const auto base=(((pc>>2)^std::uint64_t(a.context))&15)*4;
    auto* victim=&s.pc_stride[base];auto* entry=static_cast<Impl::PcStride*>(nullptr);
    for(unsigned way=0;way<4;++way) {
        auto& c=s.pc_stride[base+way];
        if(c.valid && c.pc==pc && c.context==a.context){entry=&c;break;}
        if(!c.valid || (victim->valid && c.use<victim->use))victim=&c;
    }
    if(s.pc_clock==UINT64_MAX)throw std::overflow_error("prefetch PC policy LRU clock overflow");
    if(!entry){*victim={true,a.context,pc,line,++s.pc_clock,0,1};return out;}
    entry->use=++s.pc_clock;if(line==entry->last_line)return out;
    if(s.page(line)!=s.page(entry->last_line)){entry->last_line=line;entry->stride=0;entry->samples=1;return out;}
    const auto delta=std::int64_t(line-s.page(line))-std::int64_t(entry->last_line-s.page(entry->last_line));
    entry->samples=delta==entry->stride?std::min(entry->samples+1,4u):2;
    entry->stride=delta;entry->last_line=line;if(entry->samples<4)return out;
    const auto offset=std::int64_t(line-s.page(line))+delta;
    if(offset<0 || offset>=std::int64_t(std::uint64_t(1)<<s.profile.page_shift)){++s.counts.ip_page_stops;return out;}
    s.candidate(s.page(physical)|std::uint64_t(offset),PrefetchOrigin::IpStride,a.trigger,out);return out;
}
std::vector<PrefetchCandidate> RubyPrefetcher::observe_data_miss(const PrefetchAccess& a,std::uint64_t cycle) {
    const bool eligible=Impl::eligible(a);impl_->advance(cycle);std::vector<PrefetchCandidate> out;
    if(eligible)impl_->miss(*a.physical,a.trigger,out);
    return out;
}
std::vector<PrefetchCandidate> RubyPrefetcher::observe_miss(std::uint64_t physical,std::uint64_t cycle,PendingOwner trigger) {
    impl_->advance(cycle);std::vector<PrefetchCandidate> out;impl_->miss(physical,trigger,out);return out;
}
std::vector<PrefetchCandidate> RubyPrefetcher::observe_adjacent(std::uint64_t physical,std::uint64_t cycle,PendingOwner trigger) {
    auto& s=*impl_;s.advance(cycle);std::vector<PrefetchCandidate> out;const auto line=s.line(physical),other=line^s.profile.block_size;
    if(s.page(line)==s.page(other))s.candidate(other,PrefetchOrigin::Adjacent,trigger,out);
    return out;
}
std::vector<PrefetchCandidate> RubyPrefetcher::observe_prefetch_hit(std::uint64_t physical,std::uint64_t cycle,PendingOwner trigger) {
    impl_->advance(cycle);++impl_->counts.hits;std::vector<PrefetchCandidate> out;impl_->extend(physical,trigger,out);return out;
}
std::vector<PrefetchCandidate> RubyPrefetcher::observe_prefetch_miss(std::uint64_t physical,std::uint64_t cycle,PendingOwner trigger) {
    impl_->advance(cycle);++impl_->counts.partial_hits;std::vector<PrefetchCandidate> out;impl_->extend(physical,trigger,out);return out;
}
void RubyPrefetcher::dequeued(std::uint64_t physical){impl_->queued.erase(impl_->line(physical));}
const RubyPrefetchStats& RubyPrefetcher::stats()const{return impl_->counts;}
std::size_t RubyPrefetcher::queued_lines()const{return impl_->queued.size();}
std::size_t RubyPrefetcher::live_streams()const{return std::count_if(impl_->streams.begin(),impl_->streams.end(),[](const auto& s){return s.valid;});}
} // namespace fastsim::spr
