#pragma once
// Diagnostic JSONL only. No cache/scheduler state or simulator dependencies.
#include <cstdint>
#include <cstdlib>
#include <fstream>
#include <memory>
#include <ostream>
#include <sstream>
#include <stdexcept>
#include <string>

namespace fastsim { namespace llcaudit {
inline std::string& source_identity(){static std::string value;return value;}
class Writer {
    std::unique_ptr<std::ofstream> file_;
    std::ostream* out_=nullptr;
    std::uint64_t stride_=16,cap_=4000000,rows_=0,bytes_=0,omitted_=0;
    bool finished_=false;
    void begin(const char* domain) {
        *out_<<"{\"type\":\"begin\",\"schema\":\"llc-fill-audit-v1\",\"producer\":\""<<domain
             <<"\",\"line_bytes\":64,\"stride\":"<<stride_<<",\"row_cap\":"<<cap_<<",\"source_identity\":\""<<source_identity()<<"\"}\n";
    }
  public:
    static std::uint64_t setting(const char* key,std::uint64_t fallback) {
        const auto p=std::getenv(key);if(!p)return fallback;
        const std::string text(p);std::size_t n=0;
        const auto value=std::stoull(text,&n);
        if(text.empty()||text.front()=='-'||n!=text.size()||!value)
            throw std::runtime_error(std::string("invalid LLC audit setting: ")+key);
        return value;
    }
    Writer(const char* domain,const char* path_env) {
        const auto path=std::getenv(path_env);if(!path||!*path)return;
        stride_=setting("FASTSIM_LLC_AUDIT_STRIDE",16);
        cap_=setting("FASTSIM_LLC_AUDIT_MAX_ROWS",4000000);
        file_=std::make_unique<std::ofstream>(path,std::ios::out|std::ios::trunc);
        if(!*file_)throw std::runtime_error("cannot open LLC audit output");
        out_=file_.get();begin(domain);
    }
    Writer(std::ostream& out,const char* domain,std::uint64_t stride,std::uint64_t cap)
        :out_(&out),stride_(stride),cap_(cap) {
        if(!stride||!cap)throw std::invalid_argument("empty LLC audit sampling/cap");
        begin(domain);
    }
    ~Writer(){finish();}
    bool enabled()const{return out_!=nullptr;}
    bool selected(std::uint64_t line)const{return enabled()&&line%stride_==0;}
    void row(const std::string& value) {
        if(!enabled())return;
        if(finished_)throw std::logic_error("LLC audit write after finish");
        if(rows_>=cap_||bytes_+value.size()+1>1073741824ull){++omitted_;return;}
        *out_<<value<<'\n';++rows_;bytes_+=value.size()+1;
        if(!*out_)throw std::runtime_error("LLC audit write failed");
    }
    void finish(bool completed=false) {
        if(!enabled()||finished_)return;
        finished_=true;
        *out_<<"{\"type\":\"end\",\"rows\":"<<rows_<<",\"omitted\":"<<omitted_
             <<",\"completed\":"<<(completed?"true":"false")<<",\"truncated\":"<<(omitted_?"true":"false")<<"}\n";out_->flush();
    }
};
}} // fastsim::llcaudit
