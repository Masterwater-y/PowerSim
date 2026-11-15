"""SPR load-bank admission follows target bit selection and accepted packets."""
import subprocess
import tempfile
import unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

class SprLoadBankTest(unittest.TestCase):
    def test_bank_admission_and_sparse_future_reservations(self):
        source=r'''
#include "fastsim/spr_load_bank.hpp"
#include <cassert>
#include <stdexcept>
int main() {
 fastsim::SprLoadBank bank;
 assert(bank.bank(0x1000)==0 && bank.bank(0x1004)==1 && bank.bank(0x1040)==0);
 assert(bank.available(0x1000,100));
 bank.accept(0x1000,100); bank.accept(0x1040,100);
 assert(!bank.available(0x1080,100) && bank.available(0x1004,100));
 assert(bank.available(0x1000,99) && bank.available(0x1000,101));
 bank.accept(0x1080,20); bank.accept(0x10c0,20);
 assert(!bank.available(0x1000,20) && !bank.available(0x1000,100));
 bool threw=false;try {bank.accept(0x1000,20);}catch(const std::logic_error&){threw=true;}
 assert(threw && !bank.available(0x1000,20));
 auto copy=bank; bank.prune(21);
 assert(bank.available(0x1000,20) && !bank.available(0x1000,100));
 assert(!copy.available(0x1000,20));
 bank.clear(); assert(bank.available(0x1000,100));
 bank.accept(0x1000,10);bank.accept(0x1000,10);
 bank.accept(0x1000,11);bank.accept(0x1000,11);
 assert(bank.first_available(0x1000,10)==12);
 assert(bank.first_available(0x1004,10)==10);
 fastsim::SprLoadBank joint;
 joint.accept(0x1000,0,3,true);joint.accept(0x1040,0,3,true);
 joint.accept(0x1004,1,3,true);joint.accept(0x1008,1,3,true);joint.accept(0x100c,1,3,true);
 assert(joint.first_available(0x1080,0,3,true)==2);
 fastsim::SprLoadBank unknown;
 unknown.accept(0,7,3,false);unknown.accept(0,7,3,false);unknown.accept(0,7,3,false);
 assert(unknown.first_available(0x1000,7,3,true)==8);
 assert(bank.first_available(0x1000,9)==9);
}
'''
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as d:
            path=Path(d)/'test.cpp';exe=Path(d)/'test';path.write_text(source)
            p=subprocess.run(['/opt/gcc-11/bin/g++','-std=c++17','-O2','-static-libstdc++','-static-libgcc','-I',str(ROOT/'include'),str(path),'-o',str(exe)],capture_output=True,text=True)
            self.assertEqual(p.returncode,0,p.stderr)
            p=subprocess.run([str(exe)],capture_output=True,text=True)
            self.assertEqual(p.returncode,0,p.stderr)
    def test_packet_eligibility_is_physical_and_proven_unsplit(self):
        source=r'''
#include "fastsim/spr_load_bank.hpp"
#include <cassert>
int main() {
 using B=fastsim::SprLoadBank;
 assert(B::packet_eligible(true,true,true,false,true));
 assert(!B::packet_eligible(false,true,true,false,true));
 assert(!B::packet_eligible(true,true,true,true,true));
 assert(!B::packet_eligible(true,true,false,false,true));
 assert(!B::packet_eligible(true,true,true,false,false));
}
'''
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as d:
            path=Path(d)/'test.cpp';exe=Path(d)/'test';path.write_text(source)
            p=subprocess.run(['/opt/gcc-11/bin/g++','-std=c++17','-O2','-static-libstdc++','-static-libgcc','-I',str(ROOT/'include'),str(path),'-o',str(exe)],capture_output=True,text=True)
            self.assertEqual(p.returncode,0,p.stderr)
            p=subprocess.run([str(exe)],capture_output=True,text=True);self.assertEqual(p.returncode,0,p.stderr)

    def test_gem5_source_differential(self):
        code=(ROOT/'gem5-spr/src/cpu/o3/lsq.cc').read_text()
        start=code.index('bool\nLSQ::loadBankAvailable(')
        end=code.index('\nvoid\nLSQ::insertLoad(',start)
        actual=code[start:end]
        source=r'''
#include "fastsim/spr_load_bank.hpp"
#include <array>
#include <cassert>
#include <cstdint>
#include <random>
using Addr=uint64_t;
struct LSQ {bool sprL1LoadBankConflicts=true;std::array<unsigned,16> loadBankUses{};bool loadBankAvailable(Addr)const;void noteLoadBank(Addr);};
'''+actual+r'''
int main(){
 std::mt19937_64 rng(41234);LSQ reference;fastsim::SprLoadBank candidate;
 for(uint64_t cycle=0;cycle<20000;++cycle){
  reference.loadBankUses.fill(0);candidate.prune(cycle);
  for(int i=0;i<16;++i){
   const Addr addr=rng()%2048;
   const bool accepted=(rng()%3)!=0;
   assert(reference.loadBankAvailable(addr)==candidate.available(addr,cycle));
   if(accepted && reference.loadBankAvailable(addr)){reference.noteLoadBank(addr);candidate.accept(addr,cycle);}
  }
 }
}
'''
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as d:
            path=Path(d)/'test.cpp';exe=Path(d)/'test';path.write_text(source)
            p=subprocess.run(['/opt/gcc-11/bin/g++','-std=c++17','-O2','-static-libstdc++','-static-libgcc','-I',str(ROOT/'include'),str(path),'-o',str(exe)],capture_output=True,text=True)
            self.assertEqual(p.returncode,0,p.stderr)
            p=subprocess.run([str(exe)],capture_output=True,text=True)
            self.assertEqual(p.returncode,0,p.stderr)

if __name__=='__main__':unittest.main()
