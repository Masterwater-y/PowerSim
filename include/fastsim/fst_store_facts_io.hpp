#pragma once
#include "fastsim/fst_store_requests.hpp"
#include <fstream>
#include <optional>
#include <string>
namespace fastsim::fst {
void validate_store_facts(const StoreRequestFacts&, std::uint64_t address, std::uint32_t size);
void validate_store_record_kind(const StoreRequestFacts&, const void* record);
class StoreFactsWriter {
public:
    StoreFactsWriter(const std::string& path,std::uint32_t core);
    void observe(std::uint64_t ordinal,const void* record,bool write,
                 const StoreRequestFacts* facts);
    void finish(std::uint64_t records);
private:
    std::fstream out_;
    StoreRequestHeader header_;
    std::uint64_t observed_=0;
    bool finished_=false;
};
class StoreFactsReader {
public:
    StoreFactsReader(const std::string& path,std::uint32_t core,std::uint64_t records);
    void observe(std::uint64_t ordinal,const void* record,bool write,
                 std::uint64_t address,std::uint32_t size);
    void finish();
    std::uint32_t version() const { return header_.version; }
    const StoreRequestFacts* current() const { return current_?&*current_:nullptr; }
private:
    std::ifstream in_;
    StoreRequestHeader header_;
    std::optional<StoreRequestFacts> next_,current_;
    std::uint64_t observed_=0,read_=0,hash_=14695981039346656037ull;
    void read_next();
};
} // namespace fastsim::fst
