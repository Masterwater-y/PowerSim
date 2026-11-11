#pragma once
// Content-only identity for FST inputs. No native oracle, target ticks, or
// hardware equivalence is inferred from a matching digest.
#include "fastsim/trace.hpp"
#include <array>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <optional>
#include <sstream>
#include <sys/stat.h>

namespace fastsim {
class PerfCacheSha256 {
    std::array<std::uint32_t,8> h_{{0x6a09e667,0xbb67ae85,0x3c6ef372,0xa54ff53a,0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19}};
    std::array<unsigned char,64> buffer_{};
    std::uint64_t bytes_=0;
    std::size_t used_=0;
    static std::uint32_t rotate(std::uint32_t x,unsigned n) {return (x>>n)|(x<<(32-n));}
    void block(const unsigned char* p) {
        static constexpr std::uint32_t k[64]={
            0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,
            0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,
            0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,
            0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,
            0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,
            0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,
            0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,
            0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2};
        std::uint32_t w[64];
        for(unsigned i=0;i<16;++i) w[i]=(std::uint32_t(p[i*4])<<24)|(std::uint32_t(p[i*4+1])<<16)|(std::uint32_t(p[i*4+2])<<8)|p[i*4+3];
        for(unsigned i=16;i<64;++i) {
            auto a=w[i-15],b=w[i-2];
            w[i]=w[i-16]+(rotate(a,7)^rotate(a,18)^(a>>3))+w[i-7]+(rotate(b,17)^rotate(b,19)^(b>>10));
        }
        auto a=h_[0],b=h_[1],c=h_[2],d=h_[3],e=h_[4],f=h_[5],g=h_[6],hh=h_[7];
        for(unsigned i=0;i<64;++i) {
            auto t1=hh+(rotate(e,6)^rotate(e,11)^rotate(e,25))+((e&f)^(~e&g))+k[i]+w[i];
            auto t2=(rotate(a,2)^rotate(a,13)^rotate(a,22))+((a&b)^(a&c)^(b&c));
            hh=g;g=f;f=e;e=d+t1;d=c;c=b;b=a;a=t1+t2;
        }
        h_[0]+=a;h_[1]+=b;h_[2]+=c;h_[3]+=d;h_[4]+=e;h_[5]+=f;h_[6]+=g;h_[7]+=hh;
    }
 public:
    void update(const char* data,std::size_t n) {
        bytes_+=n;
        while(n) {auto count=std::min(n,64-used_);std::memcpy(buffer_.data()+used_,data,count);used_+=count;data+=count;n-=count;if(used_==64){block(buffer_.data());used_=0;}}
    }
    std::string finish() {
        const auto bits=bytes_*8;
        const char mark=char(0x80),zero=0;update(&mark,1);
        while(used_!=56) update(&zero,1);
        char length[8];for(unsigned i=0;i<8;++i)length[7-i]=char(bits>>(8*i));update(length,8);
        std::ostringstream out;out<<std::hex<<std::setfill('0');for(auto value:h_)out<<std::setw(8)<<value;return out.str();
    }
    static std::string digest(const std::string& value) {PerfCacheSha256 s;s.update(value.data(),value.size());return s.finish();}
};

namespace perf_cache_source_detail {
inline void quote(std::ostream& out,const std::string& value) {
    out<<'"';for(unsigned char c:value){if(c=='"'||c=='\\')out<<'\\'<<c;else if(c<32)out<<"\\u00"<<"0123456789abcdef"[c>>4]<<"0123456789abcdef"[c&15];else out<<c;}out<<'"';
}
inline std::string optional_text(std::optional<std::uint64_t> value) {return value?std::to_string(*value):"-";}
inline void optional_json(std::ostream& out,std::optional<std::uint64_t> value) {if(value)out<<*value;else out<<"null";}
inline struct stat file_stat(const std::string& path) {struct stat result{};if(::stat(path.c_str(),&result))throw std::runtime_error("cannot stat functional input: "+path);return result;}
inline bool same_stat(const struct stat& a,const struct stat& b) {
    return a.st_dev==b.st_dev&&a.st_ino==b.st_ino&&a.st_size==b.st_size&&a.st_mtim.tv_sec==b.st_mtim.tv_sec&&a.st_mtim.tv_nsec==b.st_mtim.tv_nsec&&a.st_ctim.tv_sec==b.st_ctim.tv_sec&&a.st_ctim.tv_nsec==b.st_ctim.tv_nsec;
}
inline std::uint64_t little(const unsigned char* p,unsigned size) {std::uint64_t v=0;for(unsigned i=0;i<size;++i)v|=std::uint64_t(p[i])<<(i*8);return v;}
}
struct PerfCacheSourceArtifact {
    std::string role,path,sha256;
    std::uint64_t size_bytes=0;
    void write_json(std::ostream& out) const {
        using perf_cache_source_detail::quote;out<<"{\"role\":";quote(out,role);out<<",\"path\":";quote(out,path);out<<",\"size_bytes\":"<<size_bytes<<",\"sha256\":";quote(out,sha256);out<<'}';
    }
};
inline PerfCacheSourceArtifact perf_cache_source_artifact(const std::string& path,const std::string& role) {
    using namespace perf_cache_source_detail;
    auto before=file_stat(path);std::ifstream input(path,std::ios::binary);if(!input)throw std::runtime_error("cannot open functional input: "+path);
    PerfCacheSha256 hash;std::array<char,1<<16> buffer;std::uint64_t bytes=0;
    while(input){input.read(buffer.data(),buffer.size());const auto n=input.gcount();hash.update(buffer.data(),n);bytes+=n;}
    if(!input.eof())throw std::runtime_error("failed reading functional input: "+path);
    auto after=file_stat(path);if(!same_stat(before,after)||bytes!=std::uint64_t(after.st_size))throw std::runtime_error("functional input changed while hashing: "+path);
    return {role,path,hash.finish(),bytes};
}
struct PerfCacheFunctionalSource {
    std::uint32_t core_id=0,source_core_id=0,source_version=0;
    std::uint64_t source_record_count=0,skip_instructions=0,warmup_instructions=0,take_instructions=0;
    std::string slice_kind="full";
    std::optional<std::uint64_t> score_begin_ordinal,score_end_ordinal_exclusive,execution_begin_ordinal,execution_end_ordinal_exclusive;
    std::vector<PerfCacheSourceArtifact> artifacts;
    void write_json(std::ostream& out) const {
        using namespace perf_cache_source_detail;
        out<<"{\"core_id\":"<<core_id<<",\"source_core_id\":"<<source_core_id<<",\"source_version\":"<<source_version<<",\"source_record_count\":"<<source_record_count<<",\"slice_kind\":";quote(out,slice_kind);
        out<<",\"skip_instructions\":"<<skip_instructions<<",\"warmup_instructions\":"<<warmup_instructions<<",\"take_instructions\":"<<take_instructions;
        out<<",\"score_begin_ordinal\":";optional_json(out,score_begin_ordinal);out<<",\"score_end_ordinal_exclusive\":";optional_json(out,score_end_ordinal_exclusive);
        out<<",\"execution_begin_ordinal\":";optional_json(out,execution_begin_ordinal);out<<",\"execution_end_ordinal_exclusive\":";optional_json(out,execution_end_ordinal_exclusive);
        out<<",\"artifacts\":[";bool first=true;for(const auto& a:artifacts){if(!first)out<<',';first=false;a.write_json(out);}out<<"]}";
    }
};
struct PerfCacheSourceIdentity {
    std::string source_identity,status="complete";
    std::vector<PerfCacheFunctionalSource> sources;
    std::vector<PerfCacheSourceArtifact> artifacts;
    std::string canonical() const {
        using perf_cache_source_detail::optional_text;std::ostringstream out;out<<"functional-record-window-v2\n";
        for(const auto& s:sources){out<<"core\t"<<s.core_id<<'\t'<<s.source_core_id<<'\t'<<s.source_version<<'\t'<<s.source_record_count<<'\t'<<s.slice_kind<<'\t'<<s.skip_instructions<<'\t'<<s.warmup_instructions<<'\t'<<s.take_instructions<<'\t'<<optional_text(s.score_begin_ordinal)<<'\t'<<optional_text(s.score_end_ordinal_exclusive)<<'\t'<<optional_text(s.execution_begin_ordinal)<<'\t'<<optional_text(s.execution_end_ordinal_exclusive)<<'\n';
            for(const auto& a:s.artifacts)out<<"artifact\t"<<s.core_id<<'\t'<<a.role<<'\t'<<a.size_bytes<<'\t'<<a.sha256<<'\n';}
        for(const auto& a:artifacts) {
            out<<"global\t"<<a.role<<'\t'<<a.size_bytes<<'\t'<<a.sha256<<'\n';
        }
        return out.str();
    }
    bool same_inputs(const PerfCacheSourceIdentity& other) const {return canonical()==other.canonical()&&source_identity==other.source_identity&&status==other.status;}
    void write_json(std::ostream& out) const {
        using perf_cache_source_detail::quote;
        out<<"{\"schema\":\"fastsim-functional-source-v1\",\"identity_policy\":\"functional-record-window-v2\",\"source_identity\":";quote(out,source_identity);out<<",\"status\":";quote(out,status);
        out<<",\"roi_edges\":false,\"macro_boundary_complete\":false,\"participants\":[";bool first=true;for(const auto& s:sources){if(!first)out<<',';first=false;out<<s.core_id;}
        out<<"],\"sources\":[";first=true;for(const auto& s:sources){if(!first)out<<',';first=false;s.write_json(out);}out<<"],\"artifacts\":[";first=true;for(const auto& a:artifacts){if(!first)out<<',';first=false;a.write_json(out);}out<<"]}";
    }
};
inline PerfCacheSourceIdentity collect_perf_cache_source_identity(const std::string& manifest_path,const std::string& thread_state_plan="") {
    using namespace perf_cache_source_detail;
    auto entries=read_trace_manifest(manifest_path);std::sort(entries.begin(),entries.end(),[](const auto& a,const auto& b){return a.core_id<b.core_id;});
    if(entries.empty())throw std::runtime_error("functional source manifest is empty");
    PerfCacheSourceIdentity result;
    for(const auto& entry:entries) {
        if(entry.core_id!=result.sources.size())throw std::runtime_error("manifest cores must be dense starting at zero");
        if(entry.format!="binary"&&entry.format!="fastsim-binary"&&!entry.has_instruction_slice&&!entry.has_measurement_warmup)throw std::runtime_error("functional identity requires binary FST sources");
        auto fst=perf_cache_source_artifact(entry.path,"fst");
        auto before=file_stat(entry.path);std::ifstream stream(entry.path,std::ios::binary);std::array<unsigned char,72> header{};stream.read(reinterpret_cast<char*>(header.data()),header.size());
        auto version=little(header.data()+8,4),record_size=little(header.data()+16,4),flags=little(header.data()+32,8);
        if(!stream||std::memcmp(header.data(),"FSTRC01\0",8)||version<2||version>7||little(header.data()+12,4)!=72||record_size!=(version==2?40:64))throw std::runtime_error("invalid FST header: "+entry.path);
        PerfCacheFunctionalSource s;s.core_id=entry.core_id;s.source_core_id=entry.has_source_core_id?entry.source_core_id:entry.core_id;s.source_version=static_cast<std::uint32_t>(version);s.source_record_count=little(header.data()+24,8);
        if(little(header.data()+20,4)!=s.source_core_id)throw std::runtime_error("source core does not match FST header");
        if(s.source_record_count>(UINT64_MAX-72)/record_size||fst.size_bytes<72+s.source_record_count*record_size)throw std::runtime_error("truncated FST records");
        if(!same_stat(before,file_stat(entry.path)))throw std::runtime_error("FST changed while reading header");
        s.skip_instructions=entry.skip_instructions;s.warmup_instructions=entry.warmup_instructions;s.take_instructions=entry.take_instructions;
        s.score_begin_ordinal=0;s.score_end_ordinal_exclusive=s.source_record_count;s.execution_begin_ordinal=0;s.execution_end_ordinal_exclusive=s.source_record_count;
        if(entry.has_instruction_slice){s.slice_kind="slice";s.score_begin_ordinal.reset();s.score_end_ordinal_exclusive.reset();s.execution_begin_ordinal.reset();s.execution_end_ordinal_exclusive.reset();}
        if(entry.has_measurement_warmup){s.slice_kind="warmup";s.score_begin_ordinal.reset();s.score_end_ordinal_exclusive.reset();s.execution_end_ordinal_exclusive.reset();
            if(entry.has_record_counts){auto warm=entry.warmup_records,score=entry.take_records,execution=entry.execution_records.value_or(score);
                if(warm>s.source_record_count||score>s.source_record_count-warm||execution<score||execution>s.source_record_count-warm)throw std::runtime_error("invalid record boundaries");
                s.score_begin_ordinal=warm;s.score_end_ordinal_exclusive=warm+score;s.execution_end_ordinal_exclusive=warm+execution;}}
        s.artifacts.push_back(std::move(fst));
        if(version==7) {
            // Existing optional maps are consumed when present. A feature bit
            // binds its own mandatory file; unused undeclared new companions
            // are not treated as inputs by BinaryTraceSource.
            for(const std::string ext:{"deps","vmap","asmap","imap","ifmap","mreq","fsem","xlat.gz"}) {
                const auto path=entry.path+"."+ext;
                const bool required=(ext=="deps"&&(flags&(1ull<<5))) ||
                    (ext=="mreq"&&(flags&(1ull<<6))) ||
                    (ext=="fsem"&&(flags&(1ull<<7))) ||
                    (ext=="xlat.gz"&&(flags&(1ull<<9))) ||
                    (ext=="imap"&&(flags&(1ull<<12)));
                if(std::filesystem::exists(path))s.artifacts.push_back(perf_cache_source_artifact(path,ext));
                else if(required)throw std::runtime_error("required functional companion missing: "+path);
            }
            const std::array<std::pair<const char*,std::uint64_t>,7> declared{{
                {"ffe.gz",1ull<<11},{"sreq",1ull<<13},{"uops.gz",1ull<<14},
                {"ixlat.gz",1ull<<15},{"core.gz",1ull<<16},
                {"mmuctl.gz",1ull<<17},{"archb.gz",1ull<<18}}};
            for(const auto& item:declared)if(flags&item.second) {
                const auto path=entry.path+"."+item.first;
                if(!std::filesystem::exists(path))throw std::runtime_error("required functional companion missing: "+path);
                s.artifacts.push_back(perf_cache_source_artifact(path,item.first));
            }
        }
        if(entry.has_measurement_boundary_memory_state)s.artifacts.push_back(perf_cache_source_artifact(entry.measurement_boundary_memory_state_path,"boundary_memory_state"));
        std::sort(s.artifacts.begin(),s.artifacts.end(),[](const auto& a,const auto& b){return a.role<b.role;});
        if(!s.score_begin_ordinal)result.status="incomplete";
        result.sources.push_back(std::move(s));
    }
    if(!thread_state_plan.empty())result.artifacts.push_back(perf_cache_source_artifact(thread_state_plan,"thread_state_plan"));
    result.source_identity="sha256:"+PerfCacheSha256::digest(result.canonical());return result;
}
} // namespace fastsim
