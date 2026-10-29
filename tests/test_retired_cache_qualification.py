"""Exercise production native/model macro qualification without cache truth inputs."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class RetiredCacheQualification(unittest.TestCase):
    def test_completed_forwarding_and_follower_have_complete_miss_membership(self):
        code=r'''
#include "gem5-fs/src/cpu/o3/probe/perf_retirement.hh"
#include <cassert>
#include <sstream>
int main() {
 using namespace gem5::o3::retirement;
 NativeFacts f; f.resolved=true;f.store_forward=true;
 assert(classifyIndependent(f)==(L1|Forwarded));
 assert(classifyRetiredCache(f)==0);
 Member a;a.context=0;a.sequence=1;a.owner=7;a.read=true;a.measured=true;
 Ledger ledger;ledger.commit(a);
 ledger.resolve(0,1,classify(f),classifyIndependent(f)|classifyRetiredCache(f));
 auto c=ledger.snapshot({7}).workload_task;
 assert(c.perf_retired_loads==1);
 assert(c.independent_l1_misses==0 && c.independent_l2_misses==0 && c.independent_l3_misses==0);
 assert(c.independent_l1_misses_unknown==0 && c.independent_l2_misses_unknown==0 && c.independent_l3_misses_unknown==0);
 NativeFacts alias;alias.resolved=true;alias.admissions=alias.responses=1;
 alias.external_misses=alias.coalesced=1;
 assert(classifyIndependent(alias)==FillBuffer);
 assert(classifyRetiredCache(alias)==0);
 // One direct cold fragment and one coalesced fragment: only the direct
 // fragment's own observations set the miss bits. It remains fully known.
 NativeFacts split=alias;split.admissions=split.responses=split.external_misses=2;
 split.hierarchy_requests=split.accesses=split.tag_misses=1;
 split.l2_accesses=split.l2_tag_misses=split.l3_accesses=split.l3_tag_misses=1;
 assert(classifyRetiredCache(split)==(L2Miss|L3Miss));
 // Partial facts must not be made complete by the forwarding/follower fix.
 alias.resolved=false;assert(classifyRetiredCache(alias)==(L2Unknown|L3Unknown));
 split.l3_accesses=split.l3_tag_misses=0;
 assert(classifyRetiredCache(split)==(L2Miss|L3Unknown));
 // Locally conserved lower counts are insufficient if one direct fragment
 // has no lower observation. Such a gap cannot become a known negative.
 NativeFacts missing;missing.resolved=true;
 missing.admissions=missing.responses=missing.hierarchy_requests=missing.accesses=2;
 missing.external_misses=missing.tag_misses=2;
 missing.l2_accesses=missing.l2_hits=1;
 assert((classifyRetiredCache(missing)&(L2Unknown|L3Unknown))==(L2Unknown|L3Unknown));
 // Permission upgrade proves resident data; permission traffic at lower
 // levels is not the retired read's data miss. Unknown outcomes stay unknown.
 NativeFacts upgrade;upgrade.resolved=true;
 upgrade.admissions=upgrade.responses=upgrade.hierarchy_requests=upgrade.accesses=1;
 upgrade.external_misses=upgrade.permission_upgrades=1;
 upgrade.l2_accesses=upgrade.l2_other=upgrade.l2_permission_upgrades=1;
 upgrade.l3_accesses=upgrade.l3_other=upgrade.l3_permission_upgrades=1;
 assert(classifyIndependent(upgrade)==L1);
 assert(classifyRetiredCache(upgrade)==0);
 upgrade.permission_upgrades=0;upgrade.tag_misses=1;
 assert(classifyIndependent(upgrade)==Miss);
 assert(classifyRetiredCache(upgrade)==L2Hit);
 upgrade.l2_permission_upgrades=0;
 assert(classifyRetiredCache(upgrade)&L2Unknown);
 // A mixed split may send ownership traffic below a resident L2 fragment.
 // Aggregate LLC tag misses cannot prove a data miss for the other fragment.
 NativeFacts mixed; mixed.resolved=true;
 mixed.admissions=mixed.responses=mixed.hierarchy_requests=mixed.accesses=2;
 mixed.external_misses=mixed.tag_misses=2;
 mixed.l2_accesses=2;mixed.l2_tag_misses=1;
 mixed.l2_other=mixed.l2_permission_upgrades=1;
 mixed.l3_accesses=2;mixed.l3_hits=mixed.l3_tag_misses=1;
 Ledger ambiguous;Member am=a;am.sequence=17;ambiguous.commit(am);ambiguous.resolveNative(0,17,mixed);
 const auto amb=ambiguous.snapshot({7}).physical;
 assert(amb.independent_l1_misses==1 && amb.independent_l2_misses==1);
 assert(amb.independent_l3_misses==0 && amb.independent_l3_misses_unknown==1);
 // LLC remote-owner HitM service is a shared-L3 snoop hit; an LLC
 // transient merge with no resident data remains a miss for this core's
 // direct request. Preserve typed facts rather than folding both into other.
 NativeFacts remote=missing;remote.admissions=remote.responses=1;
 remote.hierarchy_requests=remote.accesses=remote.tag_misses=remote.external_misses=1;
 remote.l2_accesses=remote.l2_tag_misses=1;remote.l2_hits=0;
 remote.l3_accesses=remote.l3_other=remote.l3_remote_supplies=1;
 assert(classifyRetiredCache(remote)==(L2Miss|L3Hit));
 remote.l3_remote_supplies=0;remote.l3_merged_misses=1;
 assert(classifyRetiredCache(remote)==(L2Miss|L3Miss));
 remote.l3_accesses=2; // missing one distinct lookup must remain unknown
 assert(classifyRetiredCache(remote)&L3Unknown);
 Ledger diagnostic;Member unknown=a;unknown.sequence=15;unknown.pc=0x123;
 diagnostic.commit(unknown);remote.l3_accesses=2;
 diagnostic.resolveNative(0,15,remote);
 std::ostringstream facts;diagnostic.writeNativeDiagnostics(facts);
 assert(facts.str().find("\"pc\":291")!=std::string::npos);
 assert(facts.str().find("\"l3_merged_misses\":1")!=std::string::npos);
 // A proven external hardware entry is not an x86 memory instruction.
 // Keep functional microcode cost/population, but classify event membership
 // as ineligible rather than treating its internal IDT/stack reads as loads.
 Ledger entry;Member h=a;h.sequence=20;h.rom=h.rom_entry=h.hardware_entry=true;
 h.write=true;entry.commit(h);
 auto e=entry.snapshot({7}).workload_task;
 assert(e.retired_macro_instructions==1 && e.retired_uops==1);
 assert(e.perf_retired_loads==0 && e.perf_retired_stores==0);
 assert(e.excluded_hardware_entry_macros==1 && e.independent_l1_misses_unknown==0);
 assert(entry.lastHardwareEntry());
 // An arbitrary ROM continuation is not sufficient proof of ineligibility.
 h.sequence=21;h.rom_entry=h.hardware_entry=false;entry.commit(h);
 e=entry.snapshot({7}).workload_task;
 assert(e.perf_retired_loads==1 && e.independent_l1_misses_unknown==1);
 // SPR explicitly counts supported SW prefetch instructions as retired
 // loads/L1 hits regardless of their asynchronous physical request source.
 Member pf;pf.sequence=9;pf.owner=7;pf.measured=true;pf.software_prefetch=true;
 Ledger prefetch;prefetch.commit(pf);
 auto p=prefetch.snapshot({7}).workload_task;
 assert(p.perf_retired_loads==1 && p.perf_retired_stores==0);
 assert(p.independent_l1_hits==1 && p.independent_l1_misses==0);
 assert(p.independent_l1_misses_unknown==0 && p.independent_l2_misses_unknown==0 && p.independent_l3_misses_unknown==0);
}
'''
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as tmp:
            src=Path(tmp)/'check.cc';exe=Path(tmp)/'check';src.write_text(code)
            build=subprocess.run(['/opt/gcc-11/bin/g++','-std=c++17','-static-libstdc++','-static-libgcc',
                '-I',str(ROOT),str(src),'-o',str(exe)],capture_output=True,text=True)
            self.assertEqual(build.returncode,0,build.stderr)
            run=subprocess.run([str(exe)],capture_output=True,text=True)
            self.assertEqual(run.returncode,0,run.stderr)

    def test_production_model_shared_service_mapping(self):
        source=(ROOT/'src/simulator.cpp').read_text()
        marker='inline unsigned retired_shared_cache_source('
        self.assertTrue(marker in source,'model lacks typed retired shared-service mapping')
        enum='enum class SharedTimingPath {'+source.split('enum class SharedTimingPath {',1)[1].split('};',1)[0]+'};'
        function=marker+source.split(marker,1)[1].split('\n}\n',1)[0]+'\n}'
        code='#include "fastsim/perf_retirement.hpp"\n#include <cassert>\nusing fastsim::PerfRetirementLedger;\n'+enum+'\n'+function+r'''
int main(){
 using F=fastsim::PerfRetirementLedger;
 assert(retired_shared_cache_source(SharedTimingPath::kRemoteSupply)==F::L3Hit);
 assert(retired_shared_cache_source(SharedTimingPath::kPermissionUpgrade)==F::L3Hit);
 assert(retired_shared_cache_source(SharedTimingPath::kMergedMemory)==F::L3Miss);
 assert(retired_shared_cache_source(SharedTimingPath::kMemory)==F::L3Miss);
 assert(retired_shared_cache_source(SharedTimingPath::kLocal)==F::L3Unknown);
 F f;f.accept(true,true,false,false,true,true,F::Miss,F::Miss|F::L2Miss|
   retired_shared_cache_source(SharedTimingPath::kRemoteSupply));
 auto p=f.snapshot().physical;assert(p.l3_miss_qualified==0 && p.l3_miss_unknown==0);
}
'''
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as directory:
            p=Path(directory);(p/'test.cc').write_text(code)
            b=subprocess.run(['/opt/gcc-11/bin/g++','-std=c++17','-static-libstdc++','-static-libgcc','-I',str(ROOT/'include'),str(p/'test.cc'),'-o',str(p/'test')],capture_output=True,text=True)
            self.assertEqual(b.returncode,0,b.stderr)
            r=subprocess.run([str(p/'test')],capture_output=True,text=True)
            self.assertEqual(r.returncode,0,r.stderr)

    def test_native_levels_and_model_macro_union(self):
        code = r'''
#include "include/fastsim/perf_retirement.hpp"
#include "gem5-fs/src/cpu/o3/probe/perf_retirement.hh"
#include <cassert>
int main() {
 using namespace gem5::o3::retirement;
 NativeFacts n; n.resolved=true;
 n.responses=n.admissions=n.hierarchy_requests=n.accesses=1;
 n.external_hits=n.hits=1;
 assert(classifyRetiredCache(n)==0); // known L1 hit: no lower-level event
 n.external_hits=n.hits=0; n.external_misses=n.tag_misses=1;
 n.l2_accesses=n.l2_hits=1;
 assert(classifyRetiredCache(n)==L2Hit);
 n.l2_hits=0; n.l2_tag_misses=1; n.l3_accesses=n.l3_tag_misses=1;
 assert(classifyRetiredCache(n)==(L2Miss|L3Miss));
 n.l3_tag_misses=0; n.l3_other=1;
 assert(classifyRetiredCache(n)==(L2Miss|L3Unknown));
 // Mixed split: the coalesced fragment does not clone a parent's service;
 // the direct fragment's own independently observed service remains known.
 n.responses=n.admissions=2;n.external_misses=2;
 n.l3_other=0;n.l3_hits=1;n.coalesced=1;
 assert(classifyRetiredCache(n)==(L2Miss|L3Hit));
 n.l3_hits=0;n.l3_tag_misses=1;
 assert(classifyRetiredCache(n)==(L2Miss|L3Miss));
 n.store_forward=true;
 assert(classifyRetiredCache(n)==0);
 using F=fastsim::PerfRetirementLedger;
 F f;
 f.accept(false,true,false,false,true,true,F::L1,F::L1|F::L2Unknown|F::L3Unknown);
 f.accept(true,true,false,false,true,true,F::Miss,F::Miss|F::L2Miss|F::L3Miss);
 auto s=f.snapshot().workload_task;
 assert(s.perf_retired_loads==1 && s.l2_miss_qualified==1 && s.l3_miss_qualified==1);
 assert(s.l2_miss_unknown==0 && s.l3_miss_unknown==0);
 assert(s.l2_hit_unknown==1 && s.l3_hit_unknown==1);
 f.accept(false,true,false,false,true,true,F::Miss,F::Miss|F::L2Miss|F::L3Miss);
 assert(f.snapshot().physical.l3_miss_qualified==1); // incomplete tail not retired
 Member m; m.context=0; m.sequence=1; m.owner=7; m.read=true; m.measured=true;
 Ledger ledger; ledger.commit(m);
 ledger.resolve(0,1,Miss,Miss|L2Miss|L3Miss);
 auto g=ledger.snapshot({7}).workload_task;
 assert(g.independent_l2_misses==1 && g.independent_l3_misses==1);
 assert(g.independent_l2_misses_unknown==0);
 F warm;
 warm.accept(false,true,false,false,true,true,F::Miss,F::Miss|F::L2Miss|F::L3Miss);
 warm.begin_measurement();
 warm.accept(true,false,false,false,true,true,0,0);
 auto w=warm.snapshot().physical;
 assert(w.retired_macro_instructions==1 && w.retired_uops==1);
 assert(w.l2_miss_qualified==1 && w.warmup_spanning_macros==1);
 Ledger unresolved; m.sequence=20; unresolved.commit(m);
 assert(unresolved.snapshot({7}).physical.independent_l2_misses_unknown==1);

}
'''
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as tmp:
            src=Path(tmp)/'check.cc'; exe=Path(tmp)/'check'
            src.write_text(code)
            build=subprocess.run(['/opt/gcc-11/bin/g++','-std=c++17','-static-libstdc++','-static-libgcc','-I',str(ROOT),str(src),'-o',str(exe)],capture_output=True,text=True)
            self.assertEqual(build.returncode,0,build.stderr)
            run=subprocess.run([str(exe)],capture_output=True,text=True)
            self.assertEqual(run.returncode,0,run.stderr)


    def test_production_report_names_bind_exact_retired_events(self):
        code=r'''#include "fastsim/retired_cache_report.hpp"
#include <iostream>
int main(){fastsim::SimulationStats s;s.cores.resize(1);fastsim::SimulatorConfig c;
 c.core_model="interval_weave";fastsim::write_retired_cache_report(std::cout,s,c);}
'''
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as tmp:
            src=Path(tmp)/'report.cc';exe=Path(tmp)/'report';src.write_text(code)
            build=subprocess.run(['/opt/gcc-11/bin/g++','-std=c++17','-static-libstdc++','-static-libgcc','-I',str(ROOT/'include'),str(src),'-o',str(exe)],capture_output=True,text=True)
            self.assertEqual(build.returncode,0,build.stderr)
            data=json.loads(subprocess.check_output([str(exe)],text=True))
            import sys
            sys.path.insert(0,str(ROOT/'tools'))
            from retired_cache_contract import EVENTS
            for scope in data['scopes'].values():
                self.assertEqual(set(scope['events']),set(EVENTS))
                for name,event in scope['events'].items():
                    self.assertEqual((event['event'],event['umask']),(EVENTS[name]['event'],EVENTS[name]['umask']))


if __name__=='__main__':unittest.main()
