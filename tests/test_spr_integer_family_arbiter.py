"""Managed integer ports: independent arbitration and shared reservations."""
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


class IntegerFamilyArbiterTest(unittest.TestCase):
 def run_cpp(self,code):
  with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as d:
   source=Path(d)/'test.cc';exe=Path(d)/'test';source.write_text(code)
   result=subprocess.run(['/opt/gcc-11/bin/g++','-std=c++17','-O2','-Wall','-Wextra','-Werror','-static-libstdc++','-static-libgcc','-I',str(ROOT/'include'),str(source),'-o',str(exe)],capture_output=True,text=True)
   self.assertEqual(result.returncode,0,result.stderr)
   result=subprocess.run([str(exe)],capture_output=True,text=True)
   self.assertEqual(result.returncode,0,result.stderr)

 def test_failed_probe_and_shared_ports(self):
  self.run_cpp(r'''
#include "fastsim/spr_integer_family_arbiter.hpp"
#include <cassert>
using A=fastsim::SprIntegerFamilyArbiter;
int main(){
 A a;
 constexpr unsigned p0=1u<<0,p1=1u<<1,p5=1u<<5;
 constexpr unsigned mask=p0|p1|p5;
 assert(a.probe(1,mask,0,mask)==0);
 assert(a.commit(1,mask,0,0,mask));
 assert(a.probe(1,mask,0,mask)==1);
 assert(a.reserve_generic(1,0)); // shared with generic FUPool
 assert(a.probe(1,mask,0,mask)==5);
 assert(a.reserve_generic(5,0));
 assert(a.probe(1,mask,0,mask)==A::kBusy);
 assert(a.probe(1,mask,1,mask)==1); // failed probe did not rotate
 assert(a.probe(2,mask,1,mask)==0); // independent family cursor
 assert(a.probe(1,p0|p5,1,p0|p5)==0); // independent candidate set
 assert(!a.commit(1,mask,1,7,mask)); // not an eligible candidate
 assert(!a.commit(1,mask,1,1,0)); // resource readiness not established
 auto checkpoint=a;
 assert(a.commit(1,mask,1,1,mask));
 assert(checkpoint.probe(1,mask,1,mask)==1); // copy rollback
 assert(a.probe(1,mask,1,mask)==5);
 assert(a.probe(0,mask,2,mask)==A::kUnsupported);
 assert(a.probe(1,0,2,mask)==A::kUnsupported);
}
''')

 def test_against_gem5_execution_ports_source(self):
  original=(ROOT/'gem5-spr/src/cpu/o3/int_execution.cc').read_text()
  begin=original.index('int\nExecutionPorts::probe(')
  end=original.index('\nResultForwarding::Stats::Stats(',begin)
  methods=original[begin:end]
  self.run_cpp(r'''
#include "fastsim/spr_integer_family_arbiter.hpp"
#include <array>
#include <cassert>
#include <cstdint>
#include <limits>
#include <random>
#include <stdexcept>
#include <unordered_map>
using Tick=std::uint64_t;
enum class IntExecutionFamily:unsigned {None,ALU,SHFT,SlowInt,BM,Branch};
struct ExecutionPort {
 Tick last=std::numeric_limits<Tick>::max();
 bool available(Tick now) const {return last!=now;}
 void accept(Tick now){last=now;}
};
struct ExecutionPorts {
 std::array<ExecutionPort*,12> ports{};
 std::unordered_map<unsigned,unsigned> cursors;
 struct Stats {std::array<unsigned,12> accepted{};std::array<std::array<unsigned,12>,6> acceptedByFamily{};} stats;
 int probe(IntExecutionFamily,std::uint16_t,Tick,std::uint16_t) const;
 void commit(IntExecutionFamily,std::uint16_t,unsigned,Tick);
};
#define panic_if(condition,...) do {if(condition) throw std::logic_error("native invalid grant");} while(0)
'''+methods+r'''
int main(){
 std::mt19937_64 rng(0x159cd);
 ExecutionPorts native;
 std::array<ExecutionPort,12> storage;
 for(unsigned p=0;p<12;++p)native.ports[p]=&storage[p];
 fastsim::SprIntegerFamilyArbiter model;
 constexpr unsigned masks[]={0x863,0x41,0x2,0x822,0x40};
 for(Tick cycle=0;cycle<20000;++cycle){
  model.prune(cycle);
  // Independent generic grants use the same physical ports.
  for(unsigned k=0;k<2;++k){
   unsigned port=rng()%12;
   if(storage[port].available(cycle)){
    storage[port].accept(cycle);
    assert(model.reserve_generic(port,cycle));
   }
  }
  for(unsigned n=0;n<5;++n){
   const unsigned family=1+rng()%5,mask=masks[rng()%5],eligible=rng()&0xfff;
   auto target=native.probe(static_cast<IntExecutionFamily>(family),mask,cycle,eligible);
   auto predicted=model.probe(family,mask,cycle,eligible);
   assert(target==predicted);
   if(target>=0){
    native.commit(static_cast<IntExecutionFamily>(family),mask,target,cycle);
    assert(model.commit(family,mask,cycle,target,eligible));
   }
  }
 }
}
''')

if __name__=='__main__':unittest.main()
