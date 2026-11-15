"""Static evidence required before modeling SPR integer-family routing."""
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


class IntegerFamilyInputTest(unittest.TestCase):
 def test_unknown_generic_managed_and_zero(self):
  code=r'''
#include "fastsim/spr_integer_family_input.hpp"
#include <cassert>
using namespace fastsim;
int main(){
 using S=SprIntegerFamilyInput::Status;
 fst::StaticUopFactsRow row;
 assert(SprIntegerFamilyInput::classify(nullptr,true).status==S::Unknown);
 assert(SprIntegerFamilyInput::classify(&row,true).status==S::Unknown);
 row.flags=fst::kStaticExecutionKnown;
 assert(SprIntegerFamilyInput::classify(&row,false).status==S::Unknown);
 assert(SprIntegerFamilyInput::classify(&row,true).status==S::Generic);
 row.zero_idiom=1;
 assert(SprIntegerFamilyInput::classify(&row,true).status==S::StaticZero);
 row.zero_idiom=0;row.integer_family=1;row.candidate_ports=0x863;row.nominal_latency=1;
 auto managed=SprIntegerFamilyInput::classify(&row,true);
 assert(managed.status==S::Managed && managed.family==1 && managed.candidates==0x863 && managed.nominal_latency==1);
 row.candidate_ports=0;assert(SprIntegerFamilyInput::classify(&row,true).status==S::Unknown);
 row.integer_family=0;row.candidate_ports=1;assert(SprIntegerFamilyInput::classify(&row,true).status==S::Unknown);
 row.integer_family=7;assert(SprIntegerFamilyInput::classify(&row,true).status==S::Unknown);
}
'''
  with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as d:
   src=Path(d)/'test.cc';exe=Path(d)/'test';src.write_text(code)
   p=subprocess.run(['/opt/gcc-11/bin/g++','-std=c++17','-O2','-Wall','-Wextra','-Werror','-static-libstdc++','-static-libgcc','-I',str(ROOT/'include'),str(src),'-o',str(exe)],capture_output=True,text=True)
   self.assertEqual(p.returncode,0,p.stderr)
   p=subprocess.run([str(exe)],capture_output=True,text=True);self.assertEqual(p.returncode,0,p.stderr)

if __name__=='__main__':unittest.main()
