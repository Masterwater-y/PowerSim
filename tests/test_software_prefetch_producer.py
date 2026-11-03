"""Exercise actual producer eligibility assignments against its native ledger."""
from pathlib import Path
import os
import re
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
OVERLAY = ROOT / 'gem5-collection/overlay/src'

class SoftwarePrefetchProducerTest(unittest.TestCase):
    def test_software_prefetch_retires_without_architectural_memory_result(self):
        source = (OVERLAY / 'cpu/o3/probe/tao_trace.cc').read_text()
        body = source.split('TaoTrace::accountPerfRetirementCommit(', 1)[1]
        assignments = '\n'.join(re.search(r'member\.' + field + r'\s*=.*?;', body, re.S).group()
                                for field in ('software_prefetch', 'read', 'write'))
        driver = r'''
#include "cpu/o3/probe/perf_retirement.hh"
#include <cassert>
using namespace gem5::o3;
struct StaticInst {
    bool prefetch=false, load=true, store=false, atomic=false;
    bool isDataPrefetch() const {return prefetch;}
    bool isLoad() const {return load;}
    bool isStore() const {return store;}
    bool isAtomic() const {return atomic;}
};
retirement::Member classify(const StaticInst* si, uint64_t seq) {
    retirement::Member member;
    member.first=member.last=member.measured=member.user=true;
    member.sequence=seq; member.owner=10;
ASSIGNMENTS
    return member;
}
int main() {
    retirement::Ledger ledger;
    StaticInst inst; inst.prefetch=true;
    ledger.commit(classify(&inst,1));
    auto counts=ledger.snapshot({10}).physical;
    assert(counts.retired_macro_instructions==1 && counts.retired_uops==1);
    assert(counts.perf_retired_loads==1 && counts.perf_retired_stores==0);
    inst.prefetch=false;
    ledger.commit(classify(&inst,2)); ledger.resolve(0,2,retirement::L1);
    counts=ledger.snapshot({10}).physical;
    assert(counts.retired_macro_instructions==2 && counts.perf_retired_loads==2);
    inst.atomic=true; inst.load=inst.store=false;
    ledger.commit(classify(&inst,3)); ledger.resolve(0,3,retirement::L1);
    counts=ledger.snapshot({10}).physical;
    assert(counts.perf_retired_loads==3 && counts.perf_retired_stores==1);
}
'''.replace('ASSIGNMENTS', assignments)
        with tempfile.TemporaryDirectory(dir=ROOT / 'tmp') as temp:
            cpp=Path(temp)/'producer.cpp'; exe=Path(temp)/'producer'
            cpp.write_text(driver)
            subprocess.run([os.environ.get('CXX','c++'),'-std=c++17','-I',str(OVERLAY),str(cpp),'-o',str(exe)],check=True)
            subprocess.run([str(exe)],check=True)

if __name__ == '__main__':
    unittest.main()
