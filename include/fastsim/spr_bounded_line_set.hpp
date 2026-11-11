#pragma once
#include <cstddef>
#include <cstdint>
#include <stdexcept>
#include <vector>

namespace fastsim {
// Snapshot-copyable, fixed-capacity line deduplication. All bucket memory is
// allocated at construction; insert/find/erase allocate nothing. Open
// addressing uses backward-shift deletion (no accumulated tombstones), with
// <=25% target load. Worst-case probes are bounded by the bucket count; this
// is host infrastructure, not a target hardware queue or timing model.
class SprBoundedLineSet {
 public:
    enum class Insert {kInserted,kDuplicate,kFull};
    static constexpr std::size_t kMaxCapacity=1u<<20;
    explicit SprBoundedLineSet(std::size_t capacity)
        :buckets_(bucket_count(capacity)),capacity_(capacity),mask_(buckets_.size()-1) {}
    bool contains(std::uint64_t line)const {
        auto pos=hash(line)&mask_;
        while(buckets_[pos].occupied) {
            if(buckets_[pos].line==line)return true;
            pos=(pos+1)&mask_;
        }
        return false;
    }
    Insert insert(std::uint64_t line) {
        auto pos=hash(line)&mask_;
        while(buckets_[pos].occupied) {
            if(buckets_[pos].line==line)return Insert::kDuplicate;
            pos=(pos+1)&mask_;
        }
        if(size_==capacity_)return Insert::kFull;
        buckets_[pos]=Bucket{line,true};++size_;return Insert::kInserted;
    }
    bool erase(std::uint64_t line) {
        auto hole=hash(line)&mask_;
        while(buckets_[hole].occupied && buckets_[hole].line!=line)
            hole=(hole+1)&mask_;
        if(!buckets_[hole].occupied)return false;
        auto next=(hole+1)&mask_;
        while(buckets_[next].occupied) {
            const auto home=hash(buckets_[next].line)&mask_;
            if(((hole-home)&mask_)<((next-home)&mask_)) {
                buckets_[hole]=buckets_[next];hole=next;
            }
            next=(next+1)&mask_;
        }
        buckets_[hole]=Bucket{};--size_;return true;
    }
    std::size_t size()const{return size_;}
 private:
    struct Bucket {std::uint64_t line=0;bool occupied=false;};
    static std::uint64_t hash(std::uint64_t value) {
        value+=0x9e3779b97f4a7c15ull;
        value=(value^(value>>30))*0xbf58476d1ce4e5b9ull;
        value=(value^(value>>27))*0x94d049bb133111ebull;
        return value^(value>>31);
    }
    static std::size_t bucket_count(std::size_t capacity) {
        if(!capacity || capacity>kMaxCapacity)
            throw std::invalid_argument("invalid bounded line set capacity");
        std::size_t count=1;
        while(count<capacity*4)count*=2;
        return count;
    }
    std::vector<Bucket> buckets_;
    std::size_t capacity_,mask_,size_=0;
};
} // namespace fastsim
