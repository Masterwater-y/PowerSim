// Minimal transport dependencies; the two function bodies below are production.
#include <cstdio>
#include <string>
#define DPRINTF(...) do {} while (0)
template<class T, class U> T safe_cast(U value) {return static_cast<T>(value);}
struct RubySystem {uint64_t clockPeriod()const{return 10;}};
struct Packet;
using PacketPtr=Packet*;
struct RubyPort {
 struct MemResponsePort {
  unsigned deliveries=0; uint64_t deliveredAt=0;
  std::string name()const{return "cpu";}
  void schedTimingResp(PacketPtr,uint64_t when){++deliveries;deliveredAt=when;}
 };
 struct SenderState {MemResponsePort* port;explicit SenderState(MemResponsePort*p):port(p){}};
};
struct Packet {
 RequestPtr req; RubyPort::SenderState* state=nullptr;
 bool isResponse()const{return true;}
 bool htmTransactionFailedInCache()const{return false;}
 uint64_t getAddr()const{return 0x80000000000003fdULL;}
 void pushSenderState(RubyPort::SenderState*s){assert(!state);state=s;}
 RubyPort::SenderState* popSenderState(){auto s=state;state=nullptr;return s;}
};
struct Queue {
 unsigned scheduled=0;uint64_t scheduledAt=0;
 bool isConnected()const{return true;}
 void schedTimingReq(PacketPtr,uint64_t when){++scheduled;scheduledAt=when;}
};
struct HookOwner {RubySystem system;RubySystem*m_ruby_system=&system;Queue memRequestPort;};
