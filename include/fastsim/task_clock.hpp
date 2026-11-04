#pragma once
#include <cstdint>
#include <limits>
#include <optional>
#include <stdexcept>
#include <unordered_set>
#include <utility>
#include <vector>
namespace fastsim {
// Functional task-accounting eligibility, independent of assembly CPU ownership.
// All times are model-computed corrected-retirement Q16 reference clocks.
class TaskClock {
public:
 struct State { bool known=false, enabled=false; std::uint64_t pid=0; };
 TaskClock(std::vector<State> states, const std::vector<std::uint64_t>& tasks)
  : states_(std::move(states)),tasks_(tasks.begin(),tasks.end()),last_(states_.size()),
    active_(states_.size()),other_(states_.size()),transition_(states_.size()),unknown_(states_.size()),
    pending_(states_.size()),finished_(states_.size(),false) {
  if(states_.empty()||tasks_.count(0))throw std::invalid_argument("invalid task clock population");
  for(const auto& s:states_)if(s.enabled&&!s.known)throw std::invalid_argument("unknown enabled task clock");
 }
 void begin_measurement(std::uint64_t origin) {
  require_open();if(measuring_)throw std::logic_error("task clock measurement repeated");
  for(auto t:last_)if(t>origin)throw std::runtime_error("task clock origin regressed");
  for(auto& t:last_) {
   t=origin;
  }
  measuring_=true;
 }
 void account_out(std::size_t core,std::uint64_t prev,std::uint64_t next,std::uint64_t time) {
  require_event(core,time);const auto& s=states_[core];
  if(prev==next||pending_[core]||(s.known&&(!s.enabled||s.pid!=prev)))throw std::runtime_error("task clock unpaired account_out");
  accrue(core,time);pending_[core]=std::make_pair(prev,next);states_[core]={true,false,prev};
 }
 void account_in(std::size_t core,std::uint64_t prev,std::uint64_t next,std::uint64_t time) {
  require_event(core,time);
  if(!pending_[core]||*pending_[core]!=std::make_pair(prev,next)||states_[core].enabled)throw std::runtime_error("task clock unpaired account_in");
  accrue(core,time);pending_[core].reset();states_[core]={true,true,next};
 }
 bool workload_eligible(std::size_t core)const {const auto& s=states_.at(core);return s.known&&s.enabled&&tasks_.count(s.pid);}
 void finish_core(std::size_t core,std::uint64_t time) {
  if(!measuring_) {
   throw std::logic_error("task clock finish before measurement");
  }
  require_event(core,time);
  accrue(core,time);
  finished_[core]=true;
 }
 void finish_common(std::uint64_t end,const std::vector<bool>& closure) {
  require_open();if(!measuring_||closure.size()!=states_.size())throw std::logic_error("task clock closure shape");
  for(std::size_t c=0;c<states_.size();++c)if(!finished_[c]||end<last_[c])throw std::runtime_error("task clock common end precedes completion");
  complete_=true;
  for(std::size_t c=0;c<states_.size();++c){
   // Completion seals the supplied retirement population. Common elapsed
   // time does not authorize executing or charging an unobserved suffix.
   if(!closure[c]||unknown_[c]||!states_[c].known)complete_=false;
  }closed_=true;
 }
 const std::vector<std::uint64_t>& active_q16()const{return active_;}
 const std::vector<std::uint64_t>& other_q16()const{return other_;}
 const std::vector<std::uint64_t>& transition_q16()const{return transition_;}
 const std::vector<std::uint64_t>& unknown_q16()const{return unknown_;}
 bool complete()const{return complete_;}
private:
 void require_open()const{if(closed_)throw std::logic_error("task clock closed");}
 void require_event(std::size_t c,std::uint64_t time)const{require_open();if(finished_.at(c)||time<last_[c])throw std::runtime_error("task clock boundary regressed or closed");}
 void accrue(std::size_t c,std::uint64_t time){
  if(time<last_[c])throw std::runtime_error("task clock time regressed");
  if(measuring_){const auto& s=states_[c];auto& value=!s.known?unknown_[c]:!s.enabled?transition_[c]:tasks_.count(s.pid)?active_[c]:other_[c];
   const auto delta=time-last_[c];if(value>std::numeric_limits<std::uint64_t>::max()-delta)throw std::overflow_error("task clock counter overflow");value+=delta;}
  last_[c]=time;
 }
 std::vector<State> states_;std::unordered_set<std::uint64_t> tasks_;
 std::vector<std::uint64_t> last_,active_,other_,transition_,unknown_;
 std::vector<std::optional<std::pair<std::uint64_t,std::uint64_t>>> pending_;
 std::vector<bool> finished_;bool measuring_=false,closed_=false,complete_=false;
};
}
