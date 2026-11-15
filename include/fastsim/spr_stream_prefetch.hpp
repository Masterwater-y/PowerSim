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

#pragma once
#include <array>
#include <cstdint>
#include <deque>
#include <optional>

namespace fastsim {
// Functional port of the RubyPrefetcher miss filters and one-deep streams for
// the configured SPR 64B/4KiB, 8/8 filters, four streams and threshold four.
// The caller owns queue deduplication, service, completion, and cache effects.
class SprStreamPrefetch {
  public:
    std::optional<std::uint64_t> observe_miss(std::uint64_t address,std::uint64_t cycle) {
        const auto line=address & ~std::uint64_t{63};
        // Source getPrefetchEntry matches one in-flight startup candidate.
        // In this source build requestIssued/requestCompleted are never set;
        // matching the current stream exits without another request.
        if (find_stream(line)) return std::nullopt;
        if (unit(positive_,line,1,cycle)) return candidate_;
        if (unit(negative_,line,-1,cycle)) return candidate_;
        nonunit(line,cycle);
        return candidate_;
    }
    std::optional<std::uint64_t> observe_prefetch_access(std::uint64_t address,std::uint64_t cycle) {
        candidate_.reset();
        auto* stream=find_stream(address & ~std::uint64_t{63});
        if (!stream) return std::nullopt;
        const auto next=advance(stream->address,stream->stride);
        if ((next>>12)!=(stream->address>>12)) { stream->valid=false;return std::nullopt; }
        stream->address=next;stream->use=cycle;return next;
    }
  private:
    struct Unit {std::uint64_t address=0;unsigned hits=0;};
    struct NonUnit {std::uint64_t address=0;std::int64_t stride=0;unsigned hits=0;};
    struct Stream {std::uint64_t address=0,use=0;int stride=0;bool valid=false;};
    template<typename T> static void append(std::deque<T>& q,T entry) {
        if (q.size()==8) q.pop_front();
        q.push_back(entry);
    }
    static std::uint64_t advance(std::uint64_t line,int stride) {
        return line + static_cast<std::uint64_t>(static_cast<std::int64_t>(stride)*64);
    }
    Stream* find_stream(std::uint64_t line) {
        for(auto& s:streams_)if(s.valid && s.address==line)return &s;
        return nullptr;
    }
    void initialize(std::uint64_t line,int stride,std::uint64_t cycle) {
        Stream* victim=&streams_[0];
        for(auto& s:streams_) {
            if(!s.valid) {victim=&s;break;}
            if(s.use<victim->use) victim=&s;
        }
        *victim=Stream{line,cycle,stride,true};
        const auto next=advance(line,stride);
        if ((line>>12)!=(next>>12)) {victim->valid=false;return;}
        victim->address=next;candidate_=next;
    }
    bool unit(std::deque<Unit>& q,std::uint64_t line,int stride,std::uint64_t cycle) {
        candidate_.reset();
        for(auto& e:q)if(e.address==line) {
            e.address=advance(e.address,stride);
            if(++e.hits>=4) initialize(line,stride,cycle);
            return true;
        }
        append(q,Unit{advance(line,stride),0});return false;
    }
    void nonunit(std::uint64_t line,std::uint64_t cycle) {
        candidate_.reset();
        for(auto& e:nonunit_)if((e.address>>12)==(line>>12)) {
            const auto delta=static_cast<std::int64_t>(line)-static_cast<std::int64_t>(e.address);
            if(!delta)return;
            if(delta==e.stride) {
                if(++e.hits>4) {
                    const auto stride=static_cast<int>(e.stride/64);
                    e=NonUnit{};initialize(line,stride,cycle);
                }
            } else e.hits=0;
            e.address=line;e.stride=delta;return;
        }
        append(nonunit_,NonUnit{line,0,0});
    }
    std::deque<Unit> positive_,negative_;
    std::deque<NonUnit> nonunit_;
    std::array<Stream,4> streams_{};
    std::optional<std::uint64_t> candidate_;
};
}
