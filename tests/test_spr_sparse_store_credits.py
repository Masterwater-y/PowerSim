"""Sparse store events and shared credit: model ordering, not native timing."""
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


class SparseStoreCreditsTest(unittest.TestCase):
 def test_deferred_offload_budget_and_fallback(self):
  code=r'''
#include "fastsim/spr_sparse_store_credits.hpp"
#include <cassert>
#include <optional>
#include <stdexcept>
using P=fastsim::SprSparseStoreCredits;
using S=fastsim::SprCommittedStoreBuffer::Store;
S store(unsigned seq,unsigned line){return S{seq,line,0,0,0,1};}
int main(){
 P p(2,2,2,1,4);
 assert(p.admit_sq(1,0));assert(p.schedule_offload(store(1,10),100));
 assert(p.admit_sq(2,1));assert(p.schedule_offload(store(2,11),101));
 assert(p.budget_used()==2 && p.queued_offloads()==2);
 assert(!p.admit_sq(3,2)); // future retirement must not free capacity
 auto pending_snapshot=p;
 p.advance(99);assert(p.queued_offloads()==2);
 assert(pending_snapshot.queued_offloads()==2 && pending_snapshot.next_transition().value()==100);
 assert(p.next_transition().value()==100);
 assert(!p.send_next(99,120));
 assert(!p.admit_sq(3,99));
 assert(!p.send_next(100,std::nullopt)); // no final model callback, no send
 auto a=p.send_next(100,150);assert(a && a->release==151);
 auto b=p.send_next(101,110);assert(b && b->model_visible==151 && b->release==152);
 assert(!p.admit_sq(3,150));
 auto snapshot=p;
 assert(p.admit_sq(3,151));assert(!snapshot.admit_sq(3,150));
 bool backwards=false;try {p.advance(150);}catch(const std::logic_error&){backwards=true;}
 assert(backwards);

 P merge(2,2,2,1,4);
 assert(merge.admit_sq(1,0));assert(merge.admit_sq(2,0));
 assert(merge.schedule_offload(store(1,1),10));assert(merge.schedule_offload(store(2,1),10));
 merge.advance(9);assert(merge.budget_used()==2);
 merge.advance(10);assert(merge.budget_used()==1);
 assert(merge.admit_sq(3,10));
 auto m=merge.send_next(10,20);assert(m && m->release==21);
 assert(!merge.begin_external_boundary(10));
 assert(merge.schedule_offload(store(3,2),11));
 auto n=merge.send_next(11,21);assert(n && n->release==22);
 assert(!merge.begin_external_boundary(21));
 assert(merge.begin_external_boundary(22));assert(!merge.admit_sq(4,22));
 merge.end_external_boundary(30);assert(merge.admit_sq(4,30));
 assert(merge.schedule_offload(store(4,3),30));assert(!merge.send_next(30,30));
 assert(merge.send_next(31,31));

 P width(3,1,3,1,3);
 for(unsigned i=1;i<=3;++i)assert(width.admit_sq(i,0));
 for(unsigned i=1;i<=3;++i)assert(width.schedule_offload(store(i,i),10));
 assert(width.next_transition().value()==10);width.advance(10);
 assert(width.queued_offloads()==2 && width.next_transition().value()==11);
 width.advance(11);assert(width.queued_offloads()==1 && width.next_transition().value()==12);
 width.advance(12);assert(width.queued_offloads()==0);

 P window(2,2,1,1,2);
 assert(window.admit_sq(1,0));assert(window.admit_sq(2,0));
 assert(window.schedule_offload(store(1,1),0));assert(window.schedule_offload(store(2,2),0));
 assert(window.send_next(0,10));assert(!window.send_next(0,10));
 assert(window.next_transition().value()==11);
 assert(!window.send_next(10,10));assert(window.send_next(11,11));

 P bounded(2,1,2,1,1);assert(bounded.admit_sq(1,0));assert(bounded.admit_sq(2,0));
 assert(bounded.schedule_offload(store(1,1),2));
 assert(!bounded.schedule_offload(store(2,2),2));
 bounded.advance(2);assert(bounded.schedule_offload(store(2,2),2));
}
'''
  with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as d:
   src=Path(d)/'test.cc';exe=Path(d)/'test';src.write_text(code)
   p=subprocess.run(['/opt/gcc-11/bin/g++','-std=c++17','-O2','-Wall','-Wextra','-Werror','-static-libstdc++','-static-libgcc','-I',str(ROOT/'include'),str(src),'-o',str(exe)],capture_output=True,text=True)
   self.assertEqual(p.returncode,0,p.stderr)
   p=subprocess.run([str(exe)],capture_output=True,text=True);self.assertEqual(p.returncode,0,p.stderr)

if __name__=='__main__':unittest.main()
