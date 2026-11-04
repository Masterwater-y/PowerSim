#!/usr/bin/env python3
"""Import actual gem5 SPR objects without copying native timing or outcomes.

The JSON describes a target, not runtime qualification. Proposed flat config stays
OFF. Source-capability declarations concern functional facts, never hit/fusion or
timing observations. Unsupported object graphs fail before writing an output.
"""
import argparse
import ast
import configparser
import hashlib
import json
import runpy
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
RUBY = 'board.cache_hierarchy.ruby_system'
SCHEMA = 'spr-mechanism-profile-v1'
# The supported SPR three-domain routing contract. Capacities and object names
# do not define a role. Any filter policy outside this disjoint layout rejects.
MEMORY_OPCLASSES = frozenset('''FloatMemRead FloatMemWrite MemRead MemWrite
SimdIndexedLoad SimdIndexedStore SimdStrideSegmentedLoad SimdStrideSegmentedStore
SimdStridedLoad SimdStridedStore SimdUnitStrideFaultOnlyFirstLoad SimdUnitStrideLoad
SimdUnitStrideMaskLoad SimdUnitStrideMaskStore SimdUnitStrideSegmentedFaultOnlyFirstLoad
SimdUnitStrideSegmentedLoad SimdUnitStrideSegmentedStore SimdUnitStrideStore
SimdWholeRegisterLoad SimdWholeRegisterStore X86StoreAddress'''.split())



class ProfileError(ValueError):
    pass


class Importer:
    def __init__(self, path, source_root):
        self.path = Path(path)
        self.config = configparser.ConfigParser(interpolation=None)
        with self.path.open() as stream:
            self.config.read_file(stream)
        self.source_root = Path(source_root)
        self.rules = {}

    def obj(self, name, typ=None):
        if name not in self.config:
            raise ProfileError('missing object: ' + name)
        result = self.config[name]
        if typ and result.get('type') != typ:
            raise ProfileError('{}: expected {}, got {}'.format(name, typ, result.get('type')))
        return result

    def value(self, name, key):
        obj = self.obj(name)
        if key.lower() not in obj:
            raise ProfileError('missing field: {}.{}'.format(name, key))
        return obj[key]

    def reference(self, name, key, typ):
        target = self.value(name, key)
        self.obj(target, typ)
        return target

    def positive(self, name, key):
        raw = self.value(name, key)
        try:
            value = int(raw)
        except ValueError:
            raise ProfileError('{}.{}, expected positive integer'.format(name, key))
        if value <= 0 or value > 0xffffffff:
            raise ProfileError('{}.{}, expected positive uint32'.format(name, key))
        return value

    def nonnegative(self, name, key):
        try:
            value = int(self.value(name, key))
        except ValueError:
            raise ProfileError('{}.{}: expected nonnegative uint32'.format(name, key))
        if value < 0 or value > 0xffffffff:
            raise ProfileError('{}.{}: expected nonnegative uint32'.format(name, key))
        return value

    def boolean(self, name, key):
        value = self.value(name, key).lower()
        if value not in ('true', 'false'):
            raise ProfileError('{}.{}, expected true/false'.format(name, key))
        return value == 'true'

    def typed(self, name, key, unit, domain):
        return {'value': self.positive(name, key), 'unit': unit, 'domain': domain,
                'source_keys': [name + '.' + key]}

    def clock_period(self, obj, seen=None):
        seen=set() if seen is None else set(seen)
        if obj in seen:raise ProfileError('cyclic clock domain: '+obj)
        seen.add(obj);typ=self.value(obj,'type')
        if typ=='SrcClockDomain':
            values=self.value(obj,'clock').split()
            if len(values)!=1:raise ProfileError('unsupported dynamic clock domain: '+obj)
            return self.positive(obj,'clock'),[obj+'.clock']
        if typ=='DerivedClockDomain':
            parent=self.value(obj,'clk_domain');period,keys=self.clock_period(parent,seen)
            return period*self.positive(obj,'clk_divider'),[obj+'.clk_domain',obj+'.clk_divider']+keys
        raise ProfileError('unsupported clock domain: '+obj)

    def protocol_edge(self,obj,key,domain,cpu):
        source_clock=self.value(obj,'clk_domain');reference_clock=self.value(cpu,'clk_domain')
        period,source_keys=self.clock_period(source_clock)
        reference,reference_keys=self.clock_period(reference_clock)
        # A scalar rounded latency cannot preserve cross-domain edge phase.
        # This production adapter currently qualifies equal clock periods only.
        if period!=reference:raise ProfileError('unsupported protocol clock-domain conversion: '+obj)
        result=self.typed(obj,key,'reference_core_cycles',domain)
        result.update(source_clock_period_ticks=period,reference_clock_period_ticks=reference,
                      raw_controller_cycles=result['value'],conversion='identity-equal-clock-periods')
        result['source_keys'] += [obj+'.clk_domain',cpu+'.clk_domain']+source_keys+reference_keys
        result['source_keys']=list(dict.fromkeys(result['source_keys']))
        return result

    def source_rule(self, relative, pattern, name):
        path = self.source_root / relative
        text = path.read_text()
        if not re.search(pattern, text):
            raise ProfileError('unsupported source rule: ' + relative + ':' + name)
        fingerprint = hashlib.sha256(path.read_bytes()).hexdigest()
        evidence = {'path': str(path.resolve()), 'sha256': fingerprint,
                    'rule': name, 'pattern': pattern}
        self.rules[name] = evidence
        return 'source:' + relative + ':' + name

    def select(self, participants):
        proc = self.obj('board.processor', 'SubSystem')
        explicit = proc.get('cores', '').split()
        if explicit:
            names = []
            for name in explicit:
                obj = self.obj(name)
                if obj.get('type') == 'BaseO3CPU':
                    names.append(name)
                elif obj.get('type') == 'SubSystem':
                    # Python SimObject ownership can serialize a child named core
                    # without a separate SubSystem core parameter.
                    child = obj.get('core', name + '.core')
                    self.obj(child, 'BaseO3CPU')
                    names.append(child)
                else:
                    raise ProfileError('unsupported processor core object: ' + name)
            policy = 'processor-cores-list'
        else:
            names = []
            for child in proc.get('children', '').split():
                if re.fullmatch(r'switch\d+', child):
                    name = 'board.processor.' + child + '.core'
                    self.obj(name, 'BaseO3CPU')
                    names.append(name)
            if not names:
                raise ProfileError('missing active O3 CPU list; explicit processor cores or switch children required')
            policy = 'processor-switch-o3-children'
        cores = {}
        for name in names:
            raw_id = self.value(name, 'cpu_id')
            try:
                cid = int(raw_id)
            except ValueError:
                raise ProfileError('invalid CPU participant id: ' + raw_id)
            if cid < 0 or cid > 0xffffffff or cid in cores:
                raise ProfileError('duplicate/invalid CPU participant id: ' + raw_id)
            cores[cid] = name
        ids = sorted(cores) if participants is None else participants
        if not ids or len(ids) != len(set(ids)) or any(cid not in cores for cid in ids):
            raise ProfileError('unknown/duplicate CPU participant selection')
        return [(cid, cores[cid]) for cid in sorted(ids)], policy

    def active_controllers(self):
        root = self.obj(RUBY, 'RubySystem')
        names = [RUBY + '.' + child for child in root.get('children', '').split()]
        if not names:
            raise ProfileError('RubySystem children missing')
        # An unrelated object with matching suffix/type is never an active object.
        return [(name, self.obj(name)) for name in names]

    def sequencer_for(self, cid, cpu, controllers):
        aliases = {cpu}
        match = re.fullmatch(r'board\.processor\.switch(\d+)\.core', cpu)
        if match:
            start = 'board.processor.start' + match.group(1) + '.core'
            if start in self.config:
                if int(self.value(start, 'cpu_id')) != cid:
                    raise ProfileError('switch/start CPU identity mismatch')
                aliases.add(start)
        candidates = []
        for name, obj in controllers:
            if obj.get('type') != 'MESI_Three_Level_L0Cache_Controller':
                continue
            seq = self.reference(name, 'sequencer', 'RubySequencer')
            ports = self.value(seq, 'in_ports').split()
            matched = [p.rsplit('.', 1)[0] for p in ports if p.endswith('.dcache_port')]
            if not set(matched).intersection(aliases):
                continue
            if len(matched) != 1:
                raise ProfileError('ambiguous CPU port binding: ' + seq)
            candidates.append((name, seq))
        if len(candidates) != 1:
            raise ProfileError('missing or ambiguous CPU port to RubySequencer: ' + cpu)
        return candidates[0]

    def integer_execution(self, cpu, iq):
        # This describes active target objects; it never qualifies a runtime.
        if 'resultForwarding' not in self.config[cpu]:
            return {'status':'unsupported','reason':'integer target object graph absent',
                    'nominal_shape_verified':False}
        forwarding=self.reference(cpu,'resultForwarding','ResultForwarding')
        # Native bindIntegerResources shares actual object pointers, not port
        # numbers. Resolve the CPU-owned table before qualifying any resources.
        shared=self.reference(cpu,'executionPorts','ExecutionPorts')
        port_objects=self.value(shared,'ports').split()
        if len(port_objects)!=12 or len(set(port_objects))!=12:
            raise ProfileError('integer shared execution ports require twelve unique objects: '+shared)
        port_table={}
        graph_keys=[cpu+'.executionPorts',shared+'.ports',cpu+'.intALU']
        for port in port_objects:
            self.obj(port,'ExecutionPort')
            try:
                number=int(self.value(port,'number'))
            except ValueError:
                raise ProfileError('invalid shared execution port number: '+port)
            if number<0 or number>=12 or number in port_table:
                raise ProfileError('invalid/duplicate shared execution port number: '+port)
            port_table[number]=port
            graph_keys.append(port+'.number')
        if self.value(cpu,'intALU')!='Null':
            raise ProfileError('integer legacy ALU and individual ALUs conflict: '+cpu)
        # iq_routing has already resolved the active IQs and the one shared
        # pool. Native numFU is the sum of selected FUDesc counts (zero ignored).
        pool=iq[0]['routing']['fu_pool']
        slots=sum(fu['count'] for fu in iq[0]['routing']['fu_policy'])
        if slots!=12:
            raise ProfileError('integer shared execution ports require twelve native FU slots: '+pool)
        graph_keys += [q['object']+'.fuPool' for q in iq]+[pool+'.FUList']
        graph_keys += [fu+'.count' for fu in self.value(pool,'FUList').split()]
        graph_keys += [self.source_rule('src/cpu/o3/fu_pool.cc',
            r'ports && numFU != 12','integer_twelve_native_fu_slots'),
            self.source_rule('src/cpu/o3/fu_pool.cc',
            r'ports->get\(port->number\) != port','integer_shared_port_pointer_binding'),
            self.source_rule('src/cpu/o3/fu_pool.cc',
            r'alu && !alus.empty\(\)','integer_legacy_individual_alu_exclusion')]
        paths=self.value(forwarding,'paths').split()
        path_rules=[]
        for obj in paths:
            self.obj(obj,'ResultPathRule')
            path_rules.append({key:self.value(obj,key) for key in
                ('producerFamily','producerPort','resultKind','consumerFamily','consumerPort','sourceIndex','delay')})
        resources=[]
        objects=self.value(cpu,'intALUs').split()
        if not objects or len(objects)!=len(set(objects)):
            raise ProfileError('invalid integer ALU resource vector: '+cpu)
        expected_families=[1]*len(objects)+[2,3,4,5]
        for key in ('intSHFT','intSlow','intBM','intBranch'):
            objects.append(self.reference(cpu,key,'IntExecutionResource'))
        for obj in objects:
            if self.value(obj,'type') not in ('IntegerALU','IntExecutionResource'):
                raise ProfileError('unsupported integer execution object: '+obj)
            values={}
            for name,key in [('family','family'),('issue_interval','issueInterval'),
                             ('completion_delay','completionDelay'),('prf_delay','prfDelay')]:
                value=int(self.value(obj,key))
                if value<0 or (name in ('family','issue_interval') and value==0):
                    raise ProfileError('invalid integer resource parameter: '+obj+'.'+key)
                values[name]=value
            if values['family']>5: raise ProfileError('unknown integer resource family')
            values.update(object=obj,source_keys=[obj+'.'+key for key in
                ('family','issueInterval','completionDelay','prfDelay')])
            if self.value(obj,'type')=='IntegerALU':
                port=self.reference(obj,'port','ExecutionPort')
                values['port']=int(self.value(port,'number'))
                if values['port']<0 or values['port']>=12 or values['family']!=1:
                    raise ProfileError('invalid IntegerALU family/port: '+obj)
                if port_table[values['port']]!=port:
                    raise ProfileError('IntegerALU is disconnected from selected shared port object: '+obj)
                values['port_object']=port
                values['source_keys'] += [obj+'.port',port+'.number',cpu+'.executionPorts',shared+'.ports']
            resources.append(values)
        nominal=(not paths and expected_families==[1,1,1,1,1,2,3,4,5]
                 and all(r['family']==family for r,family in zip(resources,expected_families))
                 and all('port' in r for r in resources[:5])
                 and sorted(r['port'] for r in resources[:5])==[0,1,5,6,11]
                 and all(r['issue_interval']==1 and r['completion_delay']==r['prf_delay']==0 for r in resources))
        return {'status':'unsupported','nominal_shape_verified':nominal,'resources':resources,
                'execution_ports':{'object':shared,'ports':[{'number':n,'object':port_table[n]} for n in range(12)],
                    'fu_pool':pool,'native_fu_slots':slots,'source_keys':graph_keys},
                'result_paths':{'objects':paths,'rules':path_rules,'active':bool(paths),
                    'status':'unsupported' if paths else 'disabled_by_target',
                    'source_keys':[cpu+'.resultForwarding',forwarding+'.paths']}}

    def prefetch(self, controller):
        enabled = self.boolean(controller, 'enable_prefetch')
        ref = self.reference(controller, 'prefetcher', 'RubyPrefetcher')
        data = {'enabled': enabled, 'object': ref, 'status': 'unsupported' if enabled else 'disabled_by_target',
                'source_keys': [controller + '.enable_prefetch', controller + '.prefetcher']}
        for key in ['ip_stride', 'cross_page', 'queue_dedup']:
            data[key] = self.boolean(ref, key)
        for key in ['num_streams', 'unit_filter', 'nonunit_filter', 'train_misses', 'num_startup_pfs', 'page_shift']:
            data[key] = self.positive(ref, key)
        return data

    def iq_routing(self, cpu, domain):
        iqnames = self.value(cpu, 'instQueues').split()
        if len(iqnames) != 3 or len(set(iqnames)) != 3:
            raise ProfileError('unsupported three-IQ object graph: ' + cpu)
        enum_path = self.source_root / 'src/cpu/FuncUnit.py'
        tree = ast.parse(enum_path.read_text())
        opclasses = None
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name == 'OpClass':
                for field in node.body:
                    if isinstance(field, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'vals' for t in field.targets):
                        opclasses = set(ast.literal_eval(field.value))
        if not opclasses or not MEMORY_OPCLASSES.issubset(opclasses):
            raise ProfileError('unsupported native IQ routing OpClass schema')
        enum_rule = self.source_rule('src/cpu/FuncUnit.py', r'class OpClass\(Enum\):', 'iq_opclass_schema')
        filter_rule = self.source_rule('src/cpu/o3/inst_queue.cc',
            r'return \(allowedOpClasses.empty\(\) \|\| contains\(allowedOpClasses\)\) &&\s*!contains\(excludedOpClasses\);',
            'iq_filter_admission')
        capable_rule = self.source_rule('src/cpu/o3/inst_queue.cc', r'_fuPool->isCapable\(op_class\)', 'iq_fu_admission')
        fu_rule = self.source_rule('src/cpu/o3/fu_pool.cc',
            r'if \(i->number\) \{[\s\S]*?capabilityList.set\(j->opClass\);', 'iq_fu_capabilities')
        resolved = {}
        pools = set()
        for obj in iqnames:
            self.obj(obj, 'IQUnit')
            filters = {}
            for key in ['allowedOpClasses', 'excludedOpClasses']:
                values = self.value(obj, key).split()
                if len(values) != len(set(values)) or not set(values).issubset(opclasses):
                    raise ProfileError('unsupported IQ routing filter: ' + obj + '.' + key)
                filters[key] = set(values)
            allowed = filters['allowedOpClasses']; excluded = filters['excludedOpClasses']
            if not allowed and excluded == MEMORY_OPCLASSES | {'X86StoreData'}:
                role = 'execution'
            elif allowed == MEMORY_OPCLASSES and not excluded:
                role = 'memory'
            elif allowed == {'X86StoreData'} and not excluded:
                role = 'store_data'
            else:
                raise ProfileError('unsupported IQ routing filter policy: ' + obj)
            if role in resolved:
                raise ProfileError('ambiguous IQ routing role: ' + role)
            pool = self.reference(obj, 'fuPool', 'FUPool')
            pools.add(pool)
            sources = [cpu + '.instQueues', obj + '.allowedOpClasses', obj + '.excludedOpClasses',
                       obj + '.fuPool', pool + '.FUList', pool + '.branchPorts', pool + '.storeDataPorts',
                       enum_rule, filter_rule, capable_rule, fu_rule]
            capabilities = set()
            fu_policy = []
            fus = self.value(pool, 'FUList').split()
            if not fus or len(fus) != len(set(fus)):
                raise ProfileError('unsupported FU capability descriptor list: ' + pool)
            for fu in fus:
                self.obj(fu, 'FUDesc')
                count = int(self.value(fu, 'count'))
                if count < 0 or count > 0xffffffff:
                    raise ProfileError('invalid FU capability count: ' + fu)
                sources += [fu + '.count', fu + '.opList']
                operations = []
                opnames = self.value(fu, 'opList').split()
                if len(opnames) != len(set(opnames)):
                    raise ProfileError('duplicate FU capability operation object: ' + fu)
                for opname in opnames:
                    self.obj(opname, 'OpDesc')
                    op = self.value(opname, 'opClass')
                    if op not in opclasses:
                        raise ProfileError('unknown FU capability OpClass: ' + op)
                    operations.append({'op_class': op, 'latency': self.positive(opname, 'opLat'),
                                       'issue_latency': self.positive(opname, 'issueLat'),
                                       'pipelined': self.boolean(opname, 'pipelined')})
                    sources += [opname + '.' + key for key in ['opClass', 'opLat', 'issueLat', 'pipelined']]
                    if count:
                        capabilities.add(op)
                fu_policy.append({'count': count, 'operations': operations})
            # No_OpClass bypasses the FU check in native numFreeEntries().
            effective = ((allowed or opclasses) - excluded) & (capabilities | {'No_OpClass'})
            if role != 'execution' and not allowed.issubset(capabilities):
                raise ProfileError('missing FU capability for IQ routing: ' + obj)
            if role == 'execution' and not (effective - {'No_OpClass'}):
                raise ProfileError('missing execution FU capability: ' + obj)
            routing = {'allowed_opclasses': sorted(allowed), 'excluded_opclasses': sorted(excluded),
                       'effective_opclasses': sorted(effective), 'fu_pool': pool,
                       'fu_policy': fu_policy, 'branch_ports': self.value(pool, 'branchPorts').split(),
                       'store_data_ports': self.value(pool, 'storeDataPorts').split(), 'source_keys': sources}
            resolved[role] = {'role': role, 'object': obj, 'routing': routing,
                              'entries': self.typed(obj, 'numEntries', 'allocation_slots', domain + ':iq:' + role)}
        if set(resolved) != {'execution', 'memory', 'store_data'} or len(pools) != 1:
            raise ProfileError('unsupported IQ routing: three disjoint domains must share one FU pool')
        return [resolved[role] for role in ['execution', 'memory', 'store_data']]

    def core(self, cid, cpu, controllers, capabilities):
        domain = 'core:' + str(cid)
        iq = self.iq_routing(cpu, domain)
        l1, seq = self.sequencer_for(cid, cpu, controllers)
        dcache = self.reference(l1, 'Dcache', 'RubyCache')
        if self.reference(seq, 'dcache', 'RubyCache') != dcache:
            raise ProfileError('Sequencer/L1 Dcache reference mismatch')
        l2s = [name for name, obj in controllers
               if obj.get('type') == 'MESI_Three_Level_L1Cache_Controller'
               and obj.get('bufferFromL0') == self.value(l1, 'bufferToL1')]
        if len(l2s) != 1:
            raise ProfileError('missing/ambiguous private L2 link: ' + l1)
        l2 = l2s[0]
        l2cache = self.reference(l2, 'cache', 'RubyCache')
        llcs = [name for name, obj in controllers if obj.get('type') == 'MESI_Three_Level_L2Cache_Controller']
        if not llcs:
            raise ProfileError('missing active LLC controller')
        def replacement(cache):
            obj=self.reference(cache,'replacement_policy','LRURP')
            return {'policy':'lru','native_type':'LRURP','runtime_qualified':False,
                    'source_keys':[cache+'.replacement_policy',obj+'.type']}
        for cache in [dcache,l2cache]+[self.reference(name,'L2cache','RubyCache') for name in llcs]:
            ref=self.value(cache,'replacement_policy')
            if self.value(ref,'type')!='LRURP':raise ProfileError('unsupported native replacement policy: '+ref)
        cache_replacement={'l1d':replacement(dcache),'l2':replacement(l2cache),
            'llc':replacement(self.reference(llcs[0],'L2cache','RubyCache'))}
        cache_replacement['llc']['source_keys']=list(dict.fromkeys(
            key for name in llcs for key in replacement(self.reference(name,'L2cache','RubyCache'))['source_keys']))
        llc_caps = [self.positive(name, 'number_of_TBEs') for name in llcs]
        if len(set(llc_caps)) != 1:
            raise ProfileError('unsupported heterogeneous LLC TBE limits')
        llc_delays=[self.positive(name,'to_l1_latency') for name in llcs]
        if len(set(llc_delays))!=1:raise ProfileError('heterogeneous LLC response protocol edges')
        protocol_edges={
            'l1_request_latency':self.protocol_edge(l1,'request_latency',domain+':l0_request',cpu),
            'l1_response_latency':self.protocol_edge(l1,'response_latency',domain+':l0_response',cpu),
            'l2_request_latency':self.protocol_edge(l2,'l1_request_latency',domain+':l1_request',cpu),
            'l2_response_latency':self.protocol_edge(l2,'l1_response_latency',domain+':l1_response',cpu),
            'llc_to_l2_latency':{'value':llc_delays[0],'unit':'cycles','domain':'llc:to_private_l2',
                'source_keys':[name+'.to_l1_latency' for name in llcs]}}
        llc_edges=[self.protocol_edge(name,'to_l1_latency','llc:to_private_l2',cpu) for name in llcs]
        protocol_edges['llc_to_l2_latency']=dict(llc_edges[0])
        protocol_edges['llc_to_l2_latency']['source_keys']=list(dict.fromkeys(
            key for edge in llc_edges for key in edge['source_keys']))
        queue=self.reference(l1,'mandatoryQueue','MessageBuffer')
        queue_latency=self.protocol_edge(l1,'mandatory_queue_latency',domain+':mandatory_entry',cpu)
        data_latency=self.typed(dcache,'dataAccessLatency','reference_core_cycles',domain+':data_array_entry')
        seq_period,seq_keys=self.clock_period(self.value(seq,'clk_domain'))
        if seq_period!=queue_latency['reference_clock_period_ticks']:
            raise ProfileError('unsupported Sequencer clock-domain conversion: '+seq)
        data_latency.update(source_clock_period_ticks=seq_period,
            reference_clock_period_ticks=seq_period,raw_sequencer_cycles=data_latency['value'],
            conversion='identity-equal-clock-periods')
        data_latency['source_keys'] += [seq+'.clk_domain',cpu+'.clk_domain']+seq_keys
        mandatory_entry={'mandatory_queue_latency':queue_latency,'data_access_latency':data_latency,
            'ordered':self.boolean(queue,'ordered'),'source_keys':[l1+'.mandatoryQueue',queue+'.ordered'],
            'effective_service_cycles':queue_latency['value']+max(data_latency['value']-1,0)}
        limits = {
            'ordinary_requests': self.typed(seq, 'max_outstanding_requests', 'requests', domain + ':ordinary'),
            'prepare_requests': self.typed(seq, 'max_store_prepare_outstanding', 'hints', domain + ':prepare'),
            'l1_data_fills': self.typed(dcache, 'fill_capacity', 'data_fills', domain + ':l1_data_fill'),
            'l2_data_fills': self.typed(l2cache, 'fill_capacity', 'data_fills', domain + ':l2_data_fill'),
            'l1_tbes': self.typed(l1, 'number_of_TBEs', 'transient_entries', domain + ':l1_tbe'),
            'l2_tbes': self.typed(l2, 'number_of_TBEs', 'transient_entries', domain + ':l2_tbe'),
            'llc_tbes': {'value': llc_caps[0], 'unit': 'transient_entries', 'domain': 'llc:per_bank',
                         'source_keys': [name + '.number_of_TBEs' for name in llcs]},
            'shared_stores': self.typed(cpu, 'SQEntries', 'store_entries', domain + ':sq_and_committed'),
            'distinct_store_lines': self.typed(cpu, 'tsoOrderedStoreLineWindow', 'cache_lines', domain + ':store_lines'),
        }
        shared_rule = self.source_rule('src/cpu/o3/lsq_unit.cc',
            r'const size_t used = storeQueue\.size\(\) \+ committedStoreBuffer\.size\(\);',
            'shared_store_budget')
        line_rule = self.source_rule('src/cpu/o3/lsq_unit.cc',
            r'storeBufferLinesInFlight\.size\(\)\s*>=\s*tsoOrderedStoreLineWindow',
            'distinct_store_line_window')
        limits['shared_stores']['source_keys'].append(shared_rule)
        limits['distinct_store_lines']['source_keys'].append(line_rule)
        rule = self.source_rule('src/cpu/o3/lsq_unit.cc', r'while\s*\(offloaded\s*<\s*4\s*&&\s*storesToWB\s*>\s*0', 'store_offload_width')
        limits['offload_width'] = {'value': 4, 'unit': 'store_entries_per_cycle', 'domain': domain + ':offload', 'source_keys': [rule]}
        bank_rule = self.source_rule('src/cpu/o3/lsq.cc', r'loadBankUses\[\(addr\s*>>\s*2\)\s*&\s*15\]\s*<\s*2', 'l1_load_bank_mapping')
        flag_keys = [
            ('store_buffer', 'sprStoreBuffer'), ('early_store_prepare', 'sprEarlyStorePrepare'),
            ('load_bank_conflicts', 'sprL1LoadBankConflicts'), ('decoupled_frontend', 'decoupledFrontEnd'),
            ('response_throttling', 'recvRespThrottling')]
        flags = {name: self.boolean(cpu, key) for name, key in flag_keys}
        flag_sources = {name: cpu + '.' + key for name, key in flag_keys}
        mmu = self.reference(cpu, 'mmu', 'X86MMU')
        itlb = self.reference(mmu, 'itb', 'X86TLB'); dtlb = self.reference(mmu, 'dtb', 'X86TLB')
        stlb = self.reference(itlb, 'next_level', 'X86TLB')
        if self.reference(dtlb, 'next_level', 'X86TLB') != stlb:
            raise ProfileError('I/D translation must reference one shared STLB per core')
        if self.value(itlb, 'entry_type') != 'instruction' or self.value(dtlb, 'entry_type') != 'data':
            raise ProfileError('unsupported L1 TLB entry domain')
        if self.value(stlb, 'entry_type') != 'unified':
            raise ProfileError('unsupported STLB entry domain')
        if not self.boolean(stlb, 'spr_shared_domains'):
            raise ProfileError('unsupported STLB domain organization')
        translation = {'domain': domain, 'status': 'unsupported'}
        for role, name in [('itlb', itlb), ('dtlb', dtlb), ('stlb', stlb)]:
            translation[role] = {'object': name, 'entries': self.typed(name, 'size', 'translations', domain + ':' + role)}
            if role != 'stlb':
                translation[role].update(next_level=stlb, next_level_hit_latency=self.positive(name, 'next_level_hit_latency'))
        mechanisms = {}
        for name, flag in [('store_buffer', 'store_buffer'), ('early_store_prepare', 'early_store_prepare'),
                           ('load_bank_conflicts', 'load_bank_conflicts'), ('ftq', 'decoupled_frontend'),
                           ('read_response_throttle', 'response_throttling')]:
            mechanisms[name] = {'active': flags[flag], 'status': 'unsupported' if flags[flag] else 'disabled_by_target',
                                'reason': 'target imported; runtime implementation qualification is separate',
                                'source_keys': [flag_sources[flag]]}
        for name, capability in [('macro_fusion', 'macro_fusion_facts'), ('translation_fills', 'translation_facts')]:
            present = capabilities.get(capability, False)
            mechanisms[name] = {'active': True, 'status': 'unsupported' if present else 'missing_functional_input',
                                'required_capability': capability}
        return {'core_id': cid, 'cpu_object': cpu, 'sequencer': seq, 'limits': limits, 'iq': iq,
                'l0_transitions_per_cycle':self.typed(l1,'transitions_per_cycle',
                    'successful_transitions_per_controller_cycle',domain+':l0_controller'),
                'protocol_edges':protocol_edges,'cache_replacement':cache_replacement,'mandatory_entry':mandatory_entry,
                'store_commit_to_iew_delay':{'value':self.nonnegative(cpu,'commitToIEWDelay'),
                    'unit':'core_cycles','domain':domain+':commit_to_iew',
                    'source_keys':[cpu+'.commitToIEWDelay']},
                'integer_execution':self.integer_execution(cpu,iq),
                'active_flags': flags, 'mechanisms': mechanisms, 'support': 'unsupported', 'runtime_qualified': False,
                'load_bank': {'banks': 16, 'ports_per_bank': 2, 'address_shift': 2, 'source_keys': [bank_rule]},
                'cache_ports': {'load': self.positive(cpu, 'cacheLoadPorts'), 'store': self.positive(cpu, 'cacheStorePorts')},
                'prefetch': {'l1': self.prefetch(l1), 'l2': self.prefetch(l2)}, 'translation': translation}


def generate(config, *, participant_ids=None, source_capabilities=None, source_root=ROOT/'gem5-spr'):
    capabilities = source_capabilities or {}
    if not isinstance(capabilities, dict) or any(not isinstance(k, str) or type(v) is not bool for k, v in capabilities.items()):
        raise ProfileError('source capabilities must map names to booleans')
    importer = Importer(config, source_root)
    cores, policy = importer.select(participant_ids)
    controllers = importer.active_controllers()
    profiles = [importer.core(cid, cpu, controllers, capabilities) for cid, cpu in cores]
    integer_graphs=[p['integer_execution']['execution_ports'] for p in profiles
                    if 'execution_ports' in p['integer_execution']]
    for field in ('object','fu_pool'):
        if len({graph[field] for graph in integer_graphs})!=len(integer_graphs):
            raise ProfileError('unsupported cross-core shared integer '+field)
    port_objects=[port['object'] for graph in integer_graphs for port in graph['ports']]
    if len(set(port_objects))!=len(port_objects):
        raise ProfileError('unsupported cross-core shared integer port objects')
    if len({p['translation']['stlb']['object'] for p in profiles}) != len(profiles):
        raise ProfileError('unsupported cross-core shared STLB')
    # Flat consumer currently supports a homogeneous target. Reject differences
    # instead of silently choosing core0; per-core object/domain IDs remain unique.
    def signature(p):
        return ([v['value'] for v in p['limits'].values()], [q['entries']['value'] for q in p['iq']],
                [{key: value for key, value in q['routing'].items() if key not in ('fu_pool', 'source_keys')} for q in p['iq']],
                p['active_flags'], p['cache_ports'],p['store_commit_to_iew_delay']['value'],p['l0_transitions_per_cycle']['value'],[e['value'] for e in p['protocol_edges'].values()],
                (p['mandatory_entry']['mandatory_queue_latency']['value'],p['mandatory_entry']['data_access_latency']['value'],p['mandatory_entry']['ordered']),
                [(r['family'],r.get('port'),r['issue_interval'],r['completion_delay'],r['prf_delay'])
                    for r in p['integer_execution'].get('resources',[])],
                p['integer_execution'].get('result_paths',{}).get('rules'),
                {k: {a: b for a, b in v.items() if a not in ('object', 'source_keys')} for k, v in p['prefetch'].items()},
                [(p['translation'][k]['entries']['value'], p['translation'][k].get('next_level_hit_latency')) for k in ['itlb', 'dtlb', 'stlb']])
    if any(signature(p) != signature(profiles[0]) for p in profiles[1:]):
        raise ProfileError('unsupported heterogeneous CPU target profiles')
    first = profiles[0]
    flat = {'spr.profile.enabled': 'false', 'spr.profile.support': 'unsupported'}
    flat.update({'spr.profile.' + key: v['value'] for key, v in first['limits'].items()})
    flat['core.store_commit_to_iew_delay']=first['store_commit_to_iew_delay']['value']
    flat['spr.memory.l0_transitions_per_cycle']=first['l0_transitions_per_cycle']['value']
    flat.update({'spr.memory.'+key:value['value'] for key,value in first['protocol_edges'].items()})
    flat.update({'cache.'+key+'.replacement':value['policy'] for key,value in first['cache_replacement'].items()})
    flat.update({'spr.memory.mandatory_queue_latency':first['mandatory_entry']['mandatory_queue_latency']['value'],
                 'spr.memory.l1_data_access_latency':first['mandatory_entry']['data_access_latency']['value'],
                 'spr.memory.mandatory_queue_ordered':str(first['mandatory_entry']['ordered']).lower()})
    flat.update({'spr.profile.iq_' + q['role']: q['entries']['value'] for q in first['iq']})
    flat.update({'spr.profile.' + key: str(v).lower() for key, v in first['active_flags'].items()})
    flat.update({'spr.profile.load_banks': 16, 'spr.profile.load_bank_ports': 2,
                 'spr.profile.cache_load_ports': first['cache_ports']['load'], 'spr.profile.cache_store_ports': first['cache_ports']['store']})
    # The child-local producer defines the byte-level source policy identity.
    # This is decoder/collector semantics, not any native timing observation.
    policy_module=runpy.run_path(str(Path(source_root)/'src/cpu/o3/probe/generate_static_uop_policy.py'))
    static_policy=policy_module['policy_digest'](source_root).hex()
    flat['spr.profile.static_uop_policy_sha256']=static_policy
    flat['spr.profile.integer_nominal_shape_verified']=str(first['integer_execution']['nominal_shape_verified']).lower()
    return {'schema': SCHEMA, 'identity': {'static_uop_policy_sha256':static_policy, 'config_path': str(Path(config).resolve()),
             'config_sha256': hashlib.sha256(Path(config).read_bytes()).hexdigest(),
             'generator_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
             'source_capabilities_sha256': hashlib.sha256(json.dumps(capabilities, sort_keys=True).encode()).hexdigest(),
             'source_rules': importer.rules}, 'selection': {'policy': policy, 'participant_ids': [p['core_id'] for p in profiles]},
            'source_capabilities': capabilities, 'per_core': profiles, 'flat_config': flat,
            'qualification': 'target_description_only; no runtime, accuracy, native-PMU or throughput certification'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--participant-ids', help='comma separated CPU IDs from the selected active CPU objects')
    parser.add_argument('--source-capabilities', type=Path, help='JSON object mapping functional capability names to booleans')
    parser.add_argument('--source-root', type=Path, default=ROOT/'gem5-spr')
    args = parser.parse_args()
    try:
        ids = None if args.participant_ids is None else [int(v) for v in args.participant_ids.split(',')]
        caps = {} if args.source_capabilities is None else json.loads(args.source_capabilities.read_text())
        profile = generate(args.config, participant_ids=ids, source_capabilities=caps, source_root=args.source_root)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(profile, indent=2, sort_keys=True) + '\n')
    except (ValueError, OSError, configparser.Error) as exc:
        print('SPR profile rejected: ' + str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
