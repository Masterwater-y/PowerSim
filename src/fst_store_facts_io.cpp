#include "fastsim/fst_store_facts_io.hpp"
#include "fastsim/fst_dependencies.hpp"
#include "fastsim/fst_memory_requests.hpp"
#include "fastsim/types.hpp"
#include <filesystem>
#include <limits>
#include <stdexcept>
namespace fastsim::fst {
void validate_store_facts(const StoreRequestFacts& f,std::uint64_t address,std::uint32_t size) {
    if ((f.attributes&~kKnownStoreAttributes) || f.address!=address || f.size!=size)
        throw std::runtime_error("store functional facts disagree with hot record");
    if ((f.attributes&kStorePagedFragments) &&
        (f.attributes&(kStoreFactsComplete|kStoreSplit))!=(kStoreFactsComplete|kStoreSplit))
        throw std::runtime_error("paged request facts require complete explicit split identity");
    const auto mask=size>=64?~std::uint64_t{0}:((std::uint64_t{1}<<size)-1);
    if (size<64 && (f.byte_enable&~mask)) throw std::runtime_error("store byte-enable exceeds size");
    constexpr std::uint64_t secure=0x10000000ull, uncacheable=0x400ull, strict=0x800ull;
    // Native Request flags required by LSQUnit::isOrdinaryStore. LOCAL access,
    // barriers and transactional state additionally require normalized facts.
    constexpr std::uint64_t forbidden=0x400ull|0x800ull|0x10000ull|0x40000ull|0x80000ull|
        0x100000ull|0x200000ull|0xc00000ull|0x1000000ull|0xc0000000ull|
        0x300000000ull|0xf0000000000ull|0xf00000000000ull|0x1000000000000ull|
        0x2000000000000ull|0x20000000000000ull;
    if (f.attributes&kStoreFactsComplete) {
        if (bool(f.request_flags&secure)!=bool(f.attributes&kStoreSecure) ||
            bool(f.request_flags&uncacheable)!=bool(f.attributes&kStoreUncacheable) ||
            bool(f.request_flags&strict)!=bool(f.attributes&kStoreStrictlyOrdered) ||
            bool(f.request_flags&0x80000ull)!=bool(f.attributes&kStoreNoAccess) ||
            (size && size<=64 && bool(f.byte_enable!=mask)!=bool(f.attributes&kStoreMasked)))
            throw std::runtime_error("normalized functional request facts contradict raw flags/mask");
    }
    if ((f.attributes&(kStoreOrdinary|kOrdinaryLoad)) && (f.request_flags&forbidden))
        throw std::runtime_error("ordinary functional request contradicts native special flags");
    if (f.attributes&kStoreOrdinary) {
        const auto excluded=kStoreNoAccess|kStoreSplit|kStoreMasked|kStoreAtomic|
            kStoreUncacheable|kStoreStrictlyOrdered|kStoreHtm|kStoreSpecial;
        if (!(f.attributes&kStoreFactsComplete) || (f.attributes&excluded) ||
            (f.attributes&kMemoryRead) || !size || size>64 || f.byte_enable!=mask || address>UINT64_MAX-(size-1) ||
            address/64!=(address+size-1)/64)
            throw std::runtime_error("store ordinary eligibility lacks complete positive functional facts");
    }
    if (f.attributes&kOrdinaryLoad) {
        if (!(f.attributes&kStoreFactsComplete) || !(f.attributes&kMemoryRead) ||
            (f.attributes&(kStoreOrdinary|kStoreNoAccess|kStoreMasked|kStoreAtomic|
             kStoreUncacheable|kStoreStrictlyOrdered|kStoreHtm|kStoreSpecial)) ||
            (f.request_flags&(0x100ull|0x20000000ull)) || !size || size>64 || f.byte_enable!=mask)
            throw std::runtime_error("ordinary load lacks complete functional eligibility");
    }
    if ((f.attributes&kStoreFactsComplete) && size>64)
        throw std::runtime_error("store byte-enable exceeds v1 functional schema");
}
void validate_store_record_kind(const StoreRequestFacts& f,const void* record) {
    const auto& hot=*static_cast<const fastsim::TraceRecord*>(record);
    if (bool(f.attributes&kMemoryRead)!=(hot.is_memory() && !hot.is_write()) ||
        ((f.attributes&kStoreOrdinary) && (!fastsim::has_flag(hot.flags,fastsim::kStore) ||
          fastsim::has_flag(hot.flags,fastsim::kAtomic) || fastsim::has_flag(hot.flags,fastsim::kLoad))) ||
        ((f.attributes&kOrdinaryLoad) && (!fastsim::has_flag(hot.flags,fastsim::kLoad) || hot.is_write())))
        throw std::runtime_error("functional request qualification contradicts hot operation kind");
}
StoreFactsWriter::StoreFactsWriter(const std::string& path,std::uint32_t core)
    :out_(path,std::ios::binary|std::ios::in|std::ios::out|std::ios::trunc) {
    header_.core_id=core;
    if (!out_) throw std::runtime_error("cannot open store facts companion: "+path);
    out_.write(reinterpret_cast<const char*>(&header_),sizeof(header_));
}
void StoreFactsWriter::observe(std::uint64_t ordinal,const void* record,bool write,const StoreRequestFacts* facts) {
    if(finished_ || ordinal!=observed_ || write!=(facts!=nullptr))
        throw std::logic_error("store companion requires exactly one explicit row per memory record");
    if(facts) {
        validate_store_facts(*facts,facts->address,facts->size);
        validate_store_record_kind(*facts,record);
        if(facts->attributes&kStorePagedFragments) header_.version=2;
        auto row=*facts;row.record_ordinal=ordinal;row.hot_record_hash=dependency_record_hash(record);
        out_.write(reinterpret_cast<const char*>(&row),sizeof(row));++header_.row_count;
    }
    if(!out_) throw std::runtime_error("cannot write functional store companion");
    header_.stream_hash=memory_request_stream_hash(header_.stream_hash,record);++observed_;
}
void StoreFactsWriter::finish(std::uint64_t records) {
    if(finished_) return;
    if(records!=observed_) throw std::logic_error("store companion final population mismatch");
    header_.record_count=records;header_.flags=1;
    out_.seekp(0);out_.write(reinterpret_cast<const char*>(&header_),sizeof(header_));out_.flush();
    if(!out_) throw std::runtime_error("cannot finalize functional store companion");
    finished_=true;
}
StoreFactsReader::StoreFactsReader(const std::string& path,std::uint32_t core,std::uint64_t records)
    :in_(path,std::ios::binary) {
    StoreRequestHeader expected;
    in_.read(reinterpret_cast<char*>(&header_),sizeof(header_));
    if(!in_ || header_.magic!=expected.magic || (header_.version!=1 && header_.version!=2) || header_.header_size!=sizeof(header_) ||
       header_.row_size!=sizeof(StoreRequestFacts) || header_.core_id!=core || header_.flags!=1 ||
       header_.reserved || header_.record_count!=records || header_.row_count>records ||
       header_.row_count>(UINT64_MAX-sizeof(header_))/sizeof(StoreRequestFacts) ||
       std::filesystem::file_size(path)!=sizeof(header_)+header_.row_count*sizeof(StoreRequestFacts))
        throw std::runtime_error("invalid/missing functional store companion: "+path);
    read_next();
}
void StoreFactsReader::read_next() {
    auto previous=next_;next_.reset();
    if(read_==header_.row_count) return;
    StoreRequestFacts row;in_.read(reinterpret_cast<char*>(&row),sizeof(row));
    if(!in_ || row.record_ordinal>=header_.record_count ||
       (previous && row.record_ordinal<=previous->record_ordinal))
        throw std::runtime_error("unordered/invalid functional store row");
    if((row.attributes&kStorePagedFragments) && header_.version<2)
        throw std::runtime_error("paged physical request facts require v2 declaration");
    validate_store_facts(row,row.address,row.size);
    next_=row;++read_;
}
void StoreFactsReader::observe(std::uint64_t ordinal,const void* record,bool write,std::uint64_t address,std::uint32_t size) {
    current_.reset();
    if(ordinal!=observed_) throw std::logic_error("functional store source ordinal regressed");
    if(next_ && next_->record_ordinal<ordinal) throw std::runtime_error("unconsumed functional store row");
    if(write) {
        if(!next_ || next_->record_ordinal!=ordinal || next_->hot_record_hash!=dependency_record_hash(record))
            throw std::runtime_error("missing/wrong-identity functional store row");
        validate_store_facts(*next_,address,size);validate_store_record_kind(*next_,record);current_=next_;read_next();
    } else if(next_ && next_->record_ordinal==ordinal) throw std::runtime_error("functional store row on nonwrite");
    hash_=memory_request_stream_hash(hash_,record);++observed_;
    if(observed_==header_.record_count) finish();
}
void StoreFactsReader::finish() {
    if(observed_!=header_.record_count || next_ || read_!=header_.row_count || hash_!=header_.stream_hash)
        throw std::runtime_error("functional store full-stream identity/population mismatch");
}
} // namespace fastsim::fst
