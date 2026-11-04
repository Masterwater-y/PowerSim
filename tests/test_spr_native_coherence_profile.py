"""Strict coherence import uses connected controllers/queues, not suffix guesses."""
import importlib.util
import tempfile
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
def module(path,name):
    spec=importlib.util.spec_from_file_location(name,path)
    result=importlib.util.module_from_spec(spec);spec.loader.exec_module(result);return result
BASE=module(ROOT/'tests/test_spr_mechanism_profile.py','profile_fixture')
TOOL=module(ROOT/'tools/generate_spr_mechanism_profile.py','coherence_importer')

def fixture():
    c=BASE.fixture();r='board.cache_hierarchy.ruby_system.'
    c[r[:-1]].update(children='l1 l2 llc dir',block_size_bytes='64',randomization='false',network='net')
    c['net']={'type':'SimpleNetwork','number_of_virtual_networks':'3'}
    for name,version,budget in [('l1',0,32),('l2',0,32),('llc',0,4),('dir',0,32)]:
        if r+name not in c:c[r+name]={'type':'MESI_Three_Level_Directory_Controller'}
        c[r+name].update(version=str(version),cluster_id='0',transitions_per_cycle=str(budget),clk_domain='clock')
    c[r+'l2'].update(l2_select_num_bits='0',to_l2_latency='1')
    c[r+'llc'].update(l2_request_latency='2',l2_response_latency='7')
    c[r+'dir'].update(to_mem_ctrl_latency='1',addr_ranges='0:1073741824',memory_out_port='memory_controller.port')
    c['memory_controller']={'type':'MemCtrl'}
    for name,size,ways in [('dcache',49152,12),('l2cache',2097152,16),('llccache',12582912,12)]:
        c[name].update(size=str(size),assoc=str(ways),start_index_bit='6')
    def queue(name,ordered=False):
        c[name]={'type':'MessageBuffer','ordered':str(ordered).lower(),'delay_to_preserve_order':str(ordered).lower(),
                 'buffer_size':'0','max_dequeue_rate':'0','routing_priority':'0','allow_zero_latency':'false','randomization':'ruby_system'}
    queue('up',True);queue('down',True);queue('mandatory')
    c[r+'l1']['bufferFromL1']='down';c[r+'l2']['bufferToL0']='down'
    for ctrl,fields in [('l2',['requestFromL2','requestToL2','responseFromL2','responseToL2','unblockToL2']),
                        ('llc',['DirRequestFromL2Cache','L1RequestFromL2Cache','L1RequestToL2Cache','responseFromL2Cache','responseToL2Cache','unblockToL2Cache']),
                        ('dir',['requestToDir','responseToDir','responseFromDir','requestToMemory','responseFromMemory'])]:
        for field in fields:
            name=ctrl+'_'+field;queue(name);c[r+ctrl][field]=name
    # Homogeneous directed full mesh: one attached controller per switch.
    routers=['switch'+str(i) for i in range(4)];ext=[];internal=[]
    for i,(router,controller) in enumerate(zip(routers,('l1','l2','llc','dir'))):
        c[router]={'type':'Switch','router_id':str(i),'clk_domain':'clock','latency':'1',
                   'ext_routing_latency':'1','int_routing_latency':'1','virt_nets':'3','routing_unit':router+'_routing'}
        c[router+'_routing']={'type':'WeightBased','adaptive_routing':'false'}
        name='ext'+str(i);ext.append(name)
        c[name]={'type':'SimpleExtLink','link_id':str(i),'ext_node':r+controller,'int_node':router,
                 'latency':'1','bandwidth_factor':'16','weight':'1','supported_vnets':''}
        for j,dest in enumerate(routers):
            if i==j:continue
            name='internal'+str(i)+'_'+str(j);internal.append(name)
            c[name]={'type':'SimpleIntLink','link_id':str(len(internal)),'src_node':router,'dst_node':dest,
                     'latency':'1','bandwidth_factor':'16','weight':'1','supported_vnets':'','src_outport':'','dst_inport':''}
    c['net'].update(routers=' '.join(routers),ext_links=' '.join(ext),int_links=' '.join(internal),
                    clk_domain='clock',buffer_size='0',endpoint_bandwidth='1000',control_msg_size='8',data_msg_size='64',
                    physical_vnets_channels='',physical_vnets_bandwidth='',netifs='')
    for owner,field,count,zero in [(router,'port_buffers',12,'true') for router in routers]+[(name,'buffers',3,'false') for name in internal]:
        buffers=[]
        for i in range(count):
            name=owner+'_buffer'+str(i);buffers.append(name)
            c[name]={'type':'MessageBuffer','buffer_size':'0','max_dequeue_rate':'0','routing_priority':'0',
                     'ordered':'true','allow_zero_latency':zero,'delay_to_preserve_order':'false','randomization':'ruby_system'}
        c[owner][field]=' '.join(buffers)
    return c

class CoherenceProfileTest(unittest.TestCase):
    def generate(self,c):
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as tmp:
            p=Path(tmp)/'config.ini'
            with p.open('w') as f:c.write(f)
            return TOOL.generate(p,native_coherence=True)
    def test_distinct_native_edges_and_controller_scopes(self):
        result=self.generate(fixture());facts=result['native_coherence'];flat=result['flat_config']
        self.assertEqual(facts['transition_budgets'],[32,32,4,32])
        self.assertEqual(flat['spr.coherence.llc_response_latency'],7)
        self.assertEqual(flat['spr.memory.llc_to_l2_latency'],1)
        self.assertEqual(flat['spr.coherence.directory_memory_latency'],1)
        self.assertEqual(flat['spr.coherence.private_unblock_latency'],1)
        self.assertEqual(facts['transport']['status'],'approximate')
        self.assertEqual(facts['transport']['admitted_topology_contract'],'homogeneous-full-mesh-one-endpoint-per-router-v1')
        self.assertFalse(facts['transport']['broader_topologies_qualified'])
        self.assertEqual(flat['core.causal_native_coherence'],'false')
    def test_connected_topology_identity_is_bound(self):
        facts=self.generate(fixture())['native_coherence']
        topology=facts['network_topology']
        self.assertEqual(topology['contract'],'homogeneous-full-mesh-one-endpoint-per-router-v1')
        self.assertEqual(len(topology['routers']),4)
        self.assertEqual(len(topology['internal_links']),12)
        self.assertEqual(len(topology['sha256']),64)
        for key in ('net.ext_links','ext1.latency','internal0_1.bandwidth_factor','switch2.clk_domain','clock.clock'):
            self.assertIn(key,facts['source_keys'])
    def test_connected_internal_buffers_reject_unsupported_contract(self):
        for owner,field,value in [('switch1_buffer0','buffer_size','1'),('internal0_1_buffer0','ordered','false')]:
            c=fixture();c[owner][field]=value
            with self.assertRaisesRegex(ValueError,'buffer'):self.generate(c)
    def test_unconnected_heterogeneous_decoy_is_ignored(self):
        c=fixture();c['unconnected_switch']={'type':'Switch','clk_domain':'missing_clock','latency':'99'}
        result=self.generate(c)['native_coherence']['network_topology']
        self.assertNotIn('unconnected_switch',result['objects'])
    def test_missing_connected_topology_rejects(self):
        c=fixture();del c['net']['ext_links']
        with self.assertRaisesRegex(ValueError,'missing.*ext_links'):self.generate(c)
    def test_heterogeneous_connected_links_reject(self):
        for obj,field,value in [('ext1','latency','17'),('ext1','bandwidth_factor','8'),
                                ('internal0_1','latency','2'),('internal0_1','bandwidth_factor','8')]:
            with self.subTest(object=obj,field=field):
                c=fixture();c[obj][field]=value
                with self.assertRaisesRegex(ValueError,'heterogeneous.*link'):self.generate(c)
    def test_heterogeneous_router_clock_rejects(self):
        c=fixture();c['slow_clock']={'type':'SrcClockDomain','clock':str(int(c['clock']['clock'])*2)}
        c['switch1']['clk_domain']='slow_clock'
        with self.assertRaisesRegex(ValueError,'clock'):self.generate(c)
    def test_incomplete_or_misattached_graph_rejects(self):
        for mode in range(3):
            c=fixture()
            if mode==0:c['net']['int_links']=' '.join(c['net']['int_links'].split()[1:])
            if mode==1:c['ext1']['ext_node']=c['ext0']['ext_node']
            if mode==2:c['internal0_1']['dst_node']='unlisted_switch'
            with self.assertRaisesRegex(ValueError,'topology|endpoint|router'):self.generate(c)
    def test_adaptive_or_per_vnet_contract_rejects(self):
        for mode in range(2):
            c=fixture()
            if mode==0:c['switch1_routing']['adaptive_routing']='true'
            else:c['net']['physical_vnets_channels']='1 2 1'
            with self.assertRaisesRegex(ValueError,'routing|vnet'):self.generate(c)
    def test_unordered_private_link_rejects(self):
        c=fixture();c['up']['ordered']='false'
        with self.assertRaisesRegex(ValueError,'private link'):self.generate(c)
    def test_random_network_rejects(self):
        c=fixture();c['board.cache_hierarchy.ruby_system']['randomization']='true'
        with self.assertRaisesRegex(ValueError,'random'):self.generate(c)
    def test_geometry_routing_conflict_rejects(self):
        c=fixture();c['llccache']['start_index_bit']='9'
        with self.assertRaisesRegex(ValueError,'index'):self.generate(c)

if __name__=='__main__':unittest.main()
