"""Single-pass store summary and modeled visibility integration."""
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class OrderedStorePipelineTest(unittest.TestCase):
    def test_sparse_fifo_budget_and_snapshot(self):
        code = r'''
#include "fastsim/spr_ordered_store_pipeline.hpp"
#include <cassert>
#include <stdexcept>
using P = fastsim::SprOrderedStorePipeline;
using S = fastsim::SprCommittedStoreBuffer::Store;
int main(){
 P p(2,2,2,1);
 assert(p.admit_sq(0)); auto a=p.offload(S{1,10,0,0,0,1},0);assert(a.accepted);
 auto sa=p.send_next(0,100);assert(sa && sa->id==a.id && sa->model_visible==100 && sa->release==101);
 assert(p.admit_sq(0)); auto b=p.offload(S{2,11,0,0,0,1},0);assert(b.accepted);
 auto sb=p.send_next(0,10);assert(sb && sb->id==b.id && sb->model_visible==101 && sb->release==102);
 assert(p.budget_used()==2 && !p.admit_sq(100));
 auto snapshot=p;
 assert(p.admit_sq(101));assert(p.budget_used()==2); // A completed, B still held
 assert(p.admit_sq(102));assert(p.budget_used()==2); // B completed
 assert(snapshot.budget_used()==2); // independent chunk snapshot
 assert(!snapshot.admit_sq(100));assert(snapshot.admit_sq(101));
 bool rejected=false;try{p.advance(101);}catch(const std::logic_error&){rejected=true;}assert(rejected);

 P merged(2,2,2,1);
 assert(merged.admit_sq(0));assert(merged.admit_sq(0));
 auto m1=merged.offload(S{10,3,0,0,0,1},0);
 auto m2=merged.offload(S{11,3,0,0,0,2},0);
 assert(m2.merged && m1.id==m2.id && merged.budget_used()==1);
 auto ms=merged.send_next(0,10);assert(ms && ms->id==m1.id);
 assert(merged.admit_sq(9));assert(!merged.admit_sq(10));
 assert(merged.admit_sq(11));

 P same(3,3,1,1);
 assert(same.admit_sq(0));auto s1=same.offload(S{1,1,0,0,0,1},0);assert(same.send_next(0,10));
 assert(same.admit_sq(0));auto s2=same.offload(S{2,1,0,0,0,1},0);auto t2=same.send_next(0,0);
 assert(t2 && t2->model_visible==10 && t2->release==11);
 assert(same.admit_sq(0));auto s3=same.offload(S{3,2,0,0,0,1},0);assert(!same.send_next(0,0));
 same.advance(10);assert(!same.send_next(10,10));
 auto t3=same.send_next(11,11);assert(t3 && t3->id==s3.id && t3->model_visible==11);
 assert(!same.begin_external_boundary(11));
 same.advance(12);assert(same.begin_external_boundary(12));
 assert(!same.admit_sq(12));same.end_external_boundary(20);
 assert(same.admit_sq(20));auto after=same.offload(S{4,2,0,0,0,1},20);
 assert(!same.send_next(20,20));auto after_send=same.send_next(21,21);
 assert(after_send && after_send->id==after.id && after_send->model_visible==21);
 (void)s1;(void)s2;
}
'''
        with tempfile.TemporaryDirectory(dir=ROOT / 'tmp') as d:
            source=Path(d)/'test.cpp';binary=Path(d)/'test';source.write_text(code)
            build=subprocess.run(['/opt/gcc-11/bin/g++','-std=c++17','-Wall','-Wextra','-Werror','-O2','-static-libstdc++','-static-libgcc','-I',str(ROOT/'include'),str(source),'-o',str(binary)],capture_output=True,text=True)
            self.assertEqual(build.returncode,0,build.stderr)
            run=subprocess.run([str(binary)],capture_output=True,text=True)
            self.assertEqual(run.returncode,0,run.stderr)

if __name__ == '__main__':
    unittest.main()
