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
#include <algorithm>
#include <array>
#include <cstdint>
#include <optional>

namespace fastsim {
// Functional port of RubyPrefetcher::observeDataAccess PC stride state.
// This class generates candidates only; it does not issue a cache request.
class SprIpStride {
  public:
    std::optional<std::uint64_t> observe(std::uint64_t pc, std::uint64_t context,
                                         bool secure, std::uint64_t vaddr,
                                         std::uint64_t paddr) {
        if (secure) return std::nullopt;
        constexpr std::uint64_t line_mask=~std::uint64_t{63};
        constexpr std::uint64_t page_mask=~std::uint64_t{4095};
        const auto line=vaddr & line_mask;
        const auto physical=paddr & line_mask;
        const auto set=((pc >> 2) ^ context) & 15;
        Entry* entry=nullptr;
        Entry* victim=&entries_[set*4];
        for (unsigned i=0; i<4; ++i) {
            auto& candidate=entries_[set*4+i];
            if (candidate.valid && candidate.pc==pc && candidate.context==context &&
                candidate.secure==secure) {entry=&candidate; break;}
            if (!candidate.valid || (victim->valid && candidate.last_use<victim->last_use))
                victim=&candidate;
        }
        if (!entry) {
            *victim=Entry{};
            victim->valid=true;victim->secure=secure;victim->context=context;
            victim->pc=pc;victim->last_line=line;victim->samples=1;
            victim->last_use=++clock_;
            return std::nullopt;
        }
        entry->last_use=++clock_;
        if (line==entry->last_line) return std::nullopt;
        if ((line & page_mask)!=(entry->last_line & page_mask)) {
            entry->last_line=line;entry->stride=0;entry->samples=1;
            return std::nullopt;
        }
        const auto delta=static_cast<std::int64_t>(line & 4095) -
            static_cast<std::int64_t>(entry->last_line & 4095);
        entry->samples=delta==entry->stride ? std::min(entry->samples+1,4u) : 2;
        entry->stride=delta;entry->last_line=line;
        if (entry->samples<4) return std::nullopt;
        const auto next=static_cast<std::int64_t>(line & 4095)+delta;
        if (next<0 || next>=4096) return std::nullopt;
        return (physical & page_mask) | static_cast<std::uint64_t>(next);
    }
  private:
    struct Entry {
        bool valid=false,secure=false;
        std::uint64_t context=0,pc=0,last_line=0,last_use=0;
        std::int64_t stride=0;
        unsigned samples=0;
    };
    std::array<Entry,64> entries_{};
    std::uint64_t clock_=0;
};
}
