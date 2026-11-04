#include "fastsim/mixed_dram.hpp"
#include <algorithm>
#include <iostream>
#include <stdexcept>
#include <tuple>
using namespace fastsim;
void check(bool b,const char* why){if(!b)throw std::runtime_error(why);}
MixedDramConfig fixture(){MixedDramConfig c;c.dram.channels=1;c.dram.size_bytes=1ull<<30;
 c.dram.ranks_per_channel=1;c.dram.banks_per_channel=8;c.dram.bank_groups_per_rank=4;
 c.dram.row_bytes=8192;c.dram.read_buffer_size=4;c.dram.write_buffer_size=4;
 c.dram.write_low_threshold_percent=50;c.dram.write_high_threshold_percent=100;
 c.dram.min_reads_per_switch=1;c.dram.min_writes_per_switch=1;return c;}
MixedDramRequest request(unsigned id,unsigned arrival,unsigned line,bool write=true){return {{0,id,0,write?DramServiceKind::kWriteback:DramServiceKind::kRead},arrival,line};}
int main(){
 auto c=fixture();MixedDramController quiet(c,64);quiet.submit(request(0,0,0));
 auto p=quiet.advance_with_admissions(1);
 check(p.admissions.size()==1 && p.admissions[0].admission==0 && p.admissions[0].arrival==0 && p.admissions[0].line==0,"accepted write receipt before physical selection");
 check(p.completions.empty() && quiet.stats().pending_writes==1,"below-watermark write remains owned after admission");
 p=quiet.advance_with_admissions(1000);check(p.admissions.empty() && p.completions.empty(),"no repeated admission receipt");
 c.dram.write_low_threshold_percent=0;c.dram.write_buffer_size=1;
 using Row=std::tuple<unsigned long long,unsigned long long,unsigned long long>;
 std::vector<Row> first;
 for(unsigned chunk:{1u,7u,1000u}){
  MixedDramController native(c,64),legacy(c,64);std::vector<Row> rows;
  for(unsigned id=0;id<16;++id){auto r=request(id,0,id*128);native.submit(r);legacy.submit(r);}
  std::vector<MixedDramCompletion> a,b;
  for(unsigned at=chunk;at<1000+chunk;at+=chunk){
   auto progress=native.advance_with_admissions(at);auto old=legacy.advance(at);
   for(auto receipt:progress.admissions)rows.emplace_back(receipt.id.sequence,receipt.arrival,receipt.admission);
   a.insert(a.end(),progress.completions.begin(),progress.completions.end());b.insert(b.end(),old.begin(),old.end());
  }
  check(a==b,"receipt API must preserve physical timing/ordering");check(rows.size()==16,"one receipt per true accepted request");
  check(std::get<2>(rows.back())>0,"full queue delays acceptance until actual selection");
  if(first.empty())first=rows;else check(first==rows,"frontier granularity preserves receipts");
 }
 std::cout<<"mixed DRAM admission/physical-service separation tests passed\n";
}
