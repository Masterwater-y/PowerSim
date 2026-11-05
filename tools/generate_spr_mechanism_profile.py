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


# Exact current native implementation + active FUList policy. The certificate
# deliberately rejects arbitrary tables; normalized values below are evidence,
# not a claim that the C++ scheduler imports arbitrary FU graphs.
GENERIC_FU_PRESET = 'spr-generic-fu-v1'
GENERIC_FU_SOURCE_FILES = ('src/cpu/o3/fu_pool.cc', 'src/cpu/o3/fu_pool.hh',
    'src/cpu/o3/int_execution.cc', 'src/cpu/o3/inst_queue.cc', 'src/cpu/o3/iew.cc',
    'src/arch/x86/X86CPU.py', 'src/cpu/FuncUnit.py')
GENERIC_FU_SOURCE_SHA256 = 'a5b6d870de249e17b095edcaa906dcb3b95ad04ff47309009ce9bd001fa15290'
GENERIC_FU_POLICY_SHA256 = '8df7dcbaf04bc93d51f1ef707d85418a0a01c96347a309a9380cc3d74631d5cb'
GENERIC_FU_CANONICAL = dict(zip(
    ('IntAlu IntMult IntDiv FloatAdd FloatCmp FloatCvt FloatMult FloatMultAcc FloatDiv FloatMisc FloatSqrt '
     'SimdAdd SimdAddAcc SimdAlu SimdCmp SimdCvt SimdMisc SimdMult SimdShift SimdFloatAdd SimdFloatCmp '
     'SimdFloatCvt SimdFloatDiv SimdFloatMult Bf16Cvt SimdPack SimdExtract SimdInsert '
     'MemRead MemWrite FloatMemRead FloatMemWrite X86StoreAddress X86StoreData').split(),
    (1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,21,25,27,28,29,31,87,96,97,98,56,57,58,59,99,100)))



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

    def generic_fu(self, cpu, iq, integer):
        policy=iq[0]['routing']['fu_policy']
        fingerprint=hashlib.sha256(json.dumps(policy,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        digest=hashlib.sha256();sources=[]
        for relative in GENERIC_FU_SOURCE_FILES:
            data=(self.source_root/relative).read_bytes()
            digest.update(relative.encode()+b'\0'+data)
            sources.append({'path':relative,'sha256':hashlib.sha256(data).hexdigest()})
        normalized={};port=0
        # Other targets remain descriptive, but cannot allocate an arbitrarily
        # large runtime port table while being refused by this twelve-port preset.
        bounded_policy=policy if sum(fu['count'] for fu in policy)<=12 else []
        for fu in bounded_policy:
            for operation in fu['operations']:
                name=operation['op_class']
                if not fu['count'] or name not in GENERIC_FU_CANONICAL:continue
                row=normalized.setdefault(name,dict(canonical_op=GENERIC_FU_CANONICAL[name],ports=[],latency=0,pipelined=True,issue_intervals=[]))
                # Native FUPool uses max latency and AND pipelining across
                # all capable units; each physical replica keeps its own II.
                row['latency']=max(row['latency'],operation['latency'])
                row['pipelined'] &= operation['pipelined']
                row['ports'].extend(range(port,port+fu['count']))
                row['issue_intervals'].extend([operation['issue_latency']]*fu['count'])
            port+=fu['count']
        normalized['No_OpClass']=dict(canonical_op=0,ports=[],latency=1,pipelined=True,issue_intervals=[])
        graph=integer.get('execution_ports',{})
        routing=iq[0]['routing']
        verified=(fingerprint==GENERIC_FU_POLICY_SHA256 and digest.hexdigest()==GENERIC_FU_SOURCE_SHA256
                  and graph.get('native_fu_slots')==12
                  and [p['number'] for p in graph.get('ports',[])]==list(range(12))
                  and graph.get('fu_pool')==routing['fu_pool']
                  and routing['branch_ports']==['0','6'] and routing['store_data_ports']==['4','9']
                  and integer.get('result_paths',{}).get('rules')==[])
        return dict(preset=GENERIC_FU_PRESET if verified else '',
                    status='source_verified_preset' if verified else 'unsupported',
                    policy_sha256=fingerprint,source_sha256=digest.hexdigest(),source_files=sources,
                    normalized_operations=normalized,
                    source_keys=[cpu+'.instQueues']+routing['source_keys']+graph.get('source_keys',[]))

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

    def core_configuration(self, cpu, domain):
        """Read optional immutable parameters, preserving legacy unknowns."""
        def optional_value(obj, field, unit):
            if field.lower() not in self.obj(obj):
                return None
            return dict(value=self.nonnegative(obj, field), unit=unit, domain=domain,
                        source_keys=[obj+'.'+field])
        physical = {'Int':'numPhysIntRegs', 'Float':'numPhysFloatRegs',
                    'Vec':'numPhysVecRegs', 'VecPred':'numPhysVecPredRegs',
                    'Mat':'numPhysMatRegs', 'CC':'numPhysCCRegs'}
        result = dict(physical_registers={name:optional_value(cpu,field,'physical_registers')
                                         for name,field in physical.items()},
                      num_threads=optional_value(cpu,'numThreads','hardware_contexts'),
                      architectural_registers=None,
                      architectural_register_source='instantiated-ISA-core-companion-v1',
                      frontend=None, runtime_qualified=False)
        ref = self.obj(cpu).get('frontend', 'Null')
        if ref not in ('', 'Null', 'NULL', 'null'):
            self.obj(ref, 'FrontendCoordinator')
            groups = {'instructionBytes':('InstructionBytes', {'bufferBytes':'bytes','bytesPerCycle':'bytes_per_cycle'}),
                      'idq':('InstructionDeliveryQueue', {'capacity':'allocation_slots'}),
                      'mite':('MiteService', {}), 'dsb':('DsbTemplates', {}), 'ms':('MicrocodeService', {})}
            frontend = {}
            for name,(typ,fields) in groups.items():
                obj = self.reference(ref,name,typ)
                if name in ('mite','dsb','ms'):
                    fields = {'instructionsPerBatch':'macro_instructions', 'opsPerCycle':'allocation_slots_per_cycle',
                              'batchOps':'functional_uops', 'latency':'core_cycles'}
                if name == 'dsb':
                    fields.update(numSets='sets',associativity='ways',opsPerEntry='functional_uops')
                frontend[name] = {field:optional_value(obj,field,unit) for field,unit in fields.items()}
            frontend['dsbToMiteDelay'] = optional_value(ref,'dsbToMiteDelay','core_cycles')
            result['frontend'] = frontend
        return result

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
                'core_configuration':self.core_configuration(cpu,domain),
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

    def coherence_topology(self, network, controllers, cpu):
        """Admit the captured homogeneous complete graph, not arbitrary SimpleNetwork.

        This binds topology facts for the modeled-link approximation; it does
        not implement or certify native switch/throttle service.
        """
        objects={};keys=[]
        def fields(name, typ, names):
            self.obj(name,typ)
            facts={'type':typ}
            for key in names:
                facts[key]=self.value(name,key);keys.append(name+'.'+key)
            keys.append(name+'.type');objects[name]=facts
            return facts
        def names(key):
            result=self.value(network,key).split()
            if not result or len(set(result))!=len(result):
                raise ProfileError('missing/duplicate native topology '+key)
            return result
        net=fields(network,'SimpleNetwork',('ext_links','int_links','routers','clk_domain','buffer_size',
                   'endpoint_bandwidth','control_msg_size','data_msg_size','physical_vnets_channels',
                   'physical_vnets_bandwidth','netifs','number_of_virtual_networks'))
        routers=names('routers');external=names('ext_links');internal=names('int_links')
        if len(set(routers+external+internal))!=len(routers+external+internal):
            raise ProfileError('native topology object identities overlap')
        if net['physical_vnets_channels'].strip() or net['physical_vnets_bandwidth'].strip() or net['netifs'].strip():
            raise ProfileError('native topology per-vnet channels/network interfaces unsupported')
        if self.nonnegative(network,'buffer_size')!=0:
            raise ProfileError('native topology bounded network buffers unsupported')
        for key in ('endpoint_bandwidth','control_msg_size','data_msg_size'):self.positive(network,key)
        reference,clock_keys=self.clock_period(self.value(cpu,'clk_domain'));keys.extend(clock_keys)
        def clock(owner):
            period,clock_keys=self.clock_period(self.value(owner,'clk_domain'));keys.extend(clock_keys)
            for key in clock_keys:
                obj,param=key.rsplit('.',1)
                objects.setdefault(obj,{'type':self.value(obj,'type')})[param]=self.value(obj,param)
            if period!=reference:raise ProfileError('unsupported native topology router/network clock: '+owner)
            return period
        clock(network)
        buffer_objects=set()
        def buffers(owner,field,count,zero_latency):
            refs=self.value(owner,field).split();keys.append(owner+'.'+field)
            objects[owner][field]=' '.join(refs)
            if len(refs)!=count or len(set(refs))!=len(refs):
                raise ProfileError('native topology buffer population mismatch: '+owner)
            for name in refs:
                if name in buffer_objects:raise ProfileError('native topology shared internal buffer unsupported')
                buffer_objects.add(name)
                info=fields(name,'MessageBuffer',('buffer_size','max_dequeue_rate','routing_priority','ordered',
                             'allow_zero_latency','delay_to_preserve_order','randomization'))
                if any(self.nonnegative(name,k)!=0 for k in ('buffer_size','max_dequeue_rate','routing_priority')) or \
                   info['ordered']!='true' or info['allow_zero_latency']!=zero_latency or \
                   info['delay_to_preserve_order']!='false' or info['randomization'] not in ('ruby_system','false','disabled'):
                    raise ProfileError('unsupported native topology internal buffer contract: '+name)
        router_ids=[];router_shapes=[]
        for router in routers:
            info=fields(router,'Switch',('router_id','clk_domain','latency','ext_routing_latency',
                                        'int_routing_latency','virt_nets','routing_unit'))
            router_ids.append(self.nonnegative(router,'router_id'))
            if self.positive(router,'virt_nets')!=3:raise ProfileError('native topology router vnet mismatch')
            router_shapes.append((clock(router),)+(tuple(self.positive(router,k) for k in
                                   ('latency','ext_routing_latency','int_routing_latency'))))
            unit=self.reference(router,'routing_unit','WeightBased')
            fields(unit,'WeightBased',('adaptive_routing',))
            if self.boolean(unit,'adaptive_routing'):raise ProfileError('native topology adaptive routing unsupported')
            buffers(router,'port_buffers',len(routers)*3,'true')
        if sorted(router_ids)!=list(range(len(routers))) or len(set(router_shapes))!=1:
            raise ProfileError('heterogeneous/invalid native topology routers')
        allowed={'MESI_Three_Level_'+kind+'_Controller' for kind in ('L0Cache','L1Cache','L2Cache','Directory','DMA')}
        endpoints={name:obj['type'] for name,obj in controllers if obj.get('type') in allowed}
        # Other attached controller families would imply unrepresented traffic.
        attached={};router_endpoints={};link_shapes=[]
        def link(name,typ,ends):
            info=fields(name,typ,('link_id','latency','bandwidth_factor','weight','supported_vnets')+ends)
            if info['supported_vnets'].strip():raise ProfileError('native topology per-vnet link routing unsupported')
            shape=tuple(self.positive(name,k) for k in ('latency','bandwidth_factor','weight'))
            if shape[2]!=1:raise ProfileError('native topology nonuniform route weights unsupported')
            link_shapes.append(shape)
            return info
        for name in external:
            info=link(name,'SimpleExtLink',('ext_node','int_node'))
            endpoint,router=info['ext_node'],info['int_node']
            if endpoint not in endpoints or endpoint in attached or router not in routers or router in router_endpoints:
                raise ProfileError('native topology requires one active endpoint per listed router')
            attached[endpoint]=name;router_endpoints[router]=endpoint
            objects[endpoint]={'type':endpoints[endpoint]};keys.append(endpoint+'.type')
        if set(attached)!=set(endpoints) or set(router_endpoints)!=set(routers):
            raise ProfileError('native topology omits active controller endpoint/router')
        edges=set()
        for name in internal:
            info=link(name,'SimpleIntLink',('src_node','dst_node','src_outport','dst_inport'))
            edge=(info['src_node'],info['dst_node'])
            if edge[0] not in routers or edge[1] not in routers or edge[0]==edge[1] or edge in edges:
                raise ProfileError('native topology invalid/duplicate internal router edge')
            if info['src_outport'].strip() or info['dst_inport'].strip():
                raise ProfileError('native topology directional router ports unsupported')
            edges.add(edge)
            buffers(name,'buffers',3,'false')
        if edges!={(a,b) for a in routers for b in routers if a!=b}:
            raise ProfileError('native topology requires a complete directed router graph')
        for group in (external,internal):
            if len({self.nonnegative(name,'link_id') for name in group})!=len(group):
                raise ProfileError('native topology duplicate link identity')
        if len(set(link_shapes))!=1:raise ProfileError('heterogeneous native topology link latency/bandwidth/weight')
        # The complete object/property projection is independently digestible;
        # the enclosing profile also binds the immutable whole config hash.
        source_files={}
        for relative,pattern,name in [
            ('src/mem/ruby/network/BasicLink.py',r'bandwidth_factor','coherence_topology_link_contract'),
            ('src/mem/ruby/network/BasicRouter.py',r'class BasicRouter','coherence_topology_router_contract'),
            ('src/mem/ruby/network/simple/SimpleLink.py',r'class SimpleIntLink','coherence_topology_internal_link'),
            ('src/mem/ruby/network/simple/SimpleNetwork.py',r'class SwitchPortBuffer','coherence_topology_switch_buffers')]:
            self.source_rule(relative,pattern,name)
            source_files[relative]=hashlib.sha256((self.source_root/relative).read_bytes()).hexdigest()
        identity=hashlib.sha256(json.dumps({'objects':objects,'source_files':source_files},sort_keys=True,separators=(',',':')).encode()).hexdigest()
        return {'contract':'homogeneous-full-mesh-one-endpoint-per-router-v1','network':network,
                'routers':routers,'external_links':external,'internal_links':internal,
                'attached_endpoints':router_endpoints,
                'inactive_dma_endpoints':[name for name,typ in endpoints.items() if typ.endswith('_DMA_Controller')],
                'objects':objects,'source_files':source_files,'sha256':identity,'source_keys':list(dict.fromkeys(keys)),
                'qualification':'topology_gate_only; modeled-link transport remains approximate'}

    def coherence(self, controllers, cores):
        """Strict opt-in import: old profiles do not gain implicit coherence."""
        groups=[]
        for typ in ('L0Cache','L1Cache','L2Cache','Directory'):
            group=[name for name,obj in controllers if obj.get('type')=='MESI_Three_Level_'+typ+'_Controller']
            group.sort(key=lambda name:self.nonnegative(name,'version'))
            if not group:raise ProfileError('native coherence missing '+typ+' controller')
            if [self.nonnegative(name,'version') for name in group]!=list(range(len(group))):
                raise ProfileError('native coherence requires contiguous controller identities')
            if any(self.nonnegative(name,'cluster_id')!=0 for name in group):
                raise ProfileError('native coherence multiple clusters unsupported')
            groups.append(group)
        l0s,l1s,llcs,dirs=groups
        if len(l0s)!=len(cores) or len(l1s)!=len(cores) or [cid for cid,_ in cores]!=list(range(len(cores))):
            raise ProfileError('native coherence requires every active CPU participant')
        cpu=cores[0][1]
        network=self.obj(RUBY).get('network',RUBY+'.network')
        self.obj(network,'SimpleNetwork')
        if self.positive(network,'number_of_virtual_networks')!=3:
            raise ProfileError('native coherence requires three native virtual networks')
        if self.boolean(RUBY,'randomization'):
            raise ProfileError('native coherence randomized network unsupported')
        block=self.positive(RUBY,'block_size_bytes')
        if block & (block-1):raise ProfileError('native coherence block size must be power of two')
        bank_bits=(len(llcs)-1).bit_length()
        if len(llcs)!=(1<<bank_bits):raise ProfileError('native coherence LLC banks must be power of two')
        block_bits=block.bit_length()-1
        facts={'controller_objects':groups,'queues':{},'routes':{},'source_keys':[],
               'transport':{'status':'approximate','policy':'modeled-links-stable-source-order-v1',
                            'unrepresented':['PerfectSwitch arbitration','Throttle bandwidth and serialization','native network contention']}}
        topology=self.coherence_topology(network,controllers,cpu)
        facts['network_topology']=topology
        facts['transport']['admitted_topology_contract']=topology['contract']
        facts['transport']['broader_topologies_qualified']=False
        facts['source_keys'].extend(topology['source_keys'])
        flat={'core.causal_native_coherence':'false','spr.coherence.source_bound':'true',
              'spr.coherence.ordered_private_links':'true','spr.coherence.unordered_network_queues':'true',
              'spr.coherence.deterministic_unbounded_queues':'true',
              'spr.coherence.transport_policy':facts['transport']['policy'],
              'uncore.cha_count':len(llcs),'uncore.cha_xor_hash':'false','uncore.llc_fill_response_latency':0}
        def queue(owner,key,local=False):
            name=self.reference(owner,key,'MessageBuffer')
            props={k:self.value(name,k) for k in ('ordered','delay_to_preserve_order','buffer_size','max_dequeue_rate','routing_priority','allow_zero_latency','randomization')}
            if local and (props['ordered']!='true' or props['delay_to_preserve_order']!='true'):
                raise ProfileError('native coherence private link must preserve ordered arrival')
            if not local and key!='mandatoryQueue' and (props['ordered']!='false' or props['delay_to_preserve_order']!='false'):
                raise ProfileError('native coherence network queues must be unordered')
            if any(int(props[k])!=0 for k in ('buffer_size','max_dequeue_rate','routing_priority')) or props['allow_zero_latency']!='false':
                raise ProfileError('native coherence bounded/rate-limited/zero-latency queue unsupported')
            if props['randomization'] not in ('ruby_system','false','disabled'):
                raise ProfileError('native coherence random queue unsupported')
            facts['queues'][name]=props
            facts['source_keys'] += [owner+'.'+key]+[name+'.'+k for k in props]
            return name
        for (cid,core),l0,l1 in zip(cores,l0s,l1s):
            actual,_=self.sequencer_for(cid,core,controllers)
            if actual!=l0:raise ProfileError('native coherence CPU/controller version mismatch')
            up=queue(l0,'bufferToL1',True);down=queue(l0,'bufferFromL1',True)
            if self.value(l1,'bufferFromL0')!=up or self.value(l1,'bufferToL0')!=down:
                raise ProfileError('native coherence private link references disagree')
            queue(l0,'mandatoryQueue')
            if self.nonnegative(l1,'l2_select_num_bits')!=bank_bits:
                raise ProfileError('native coherence bank selection bits disagree')
            for key in ('requestFromL2','requestToL2','responseFromL2','responseToL2','unblockToL2'):queue(l1,key)
        for llc in llcs:
            for key in ('DirRequestFromL2Cache','L1RequestFromL2Cache','L1RequestToL2Cache','responseFromL2Cache','responseToL2Cache','unblockToL2Cache'):queue(llc,key)
        for directory in dirs:
            for key in ('requestToDir','responseToDir','responseFromDir','requestToMemory','responseFromMemory'):queue(directory,key)
        budgets=[]
        for kind,group in zip(('l0','l1','llc','directory'),groups):
            values=[self.positive(name,'transitions_per_cycle') for name in group]
            if len(set(values))!=1:raise ProfileError('heterogeneous native coherence transition budgets')
            for name in group:self.protocol_edge(name,'transitions_per_cycle','coherence:'+kind,cpu)
            budgets.append(values[0]);flat['spr.coherence.'+kind+'_transitions_per_cycle']=values[0]
            facts['source_keys'] += [name+'.transitions_per_cycle' for name in group]
        facts['transition_budgets']=budgets
        for key,param,group in [('llc_request_latency','l2_request_latency',llcs),('llc_response_latency','l2_response_latency',llcs),('directory_memory_latency','to_mem_ctrl_latency',dirs),('private_unblock_latency','to_l2_latency',l1s)]:
            edges=[self.protocol_edge(name,param,'coherence:'+key,cpu) for name in group]
            if len({e['value'] for e in edges})!=1:raise ProfileError('heterogeneous native coherence protocol edges')
            flat['spr.coherence.'+key]=edges[0]['value'];facts[key]=edges
        geometry={}
        for level,group,param,index_bit in [('l1d',l0s,'Dcache',block_bits),('l2',l1s,'cache',block_bits),('llc',llcs,'L2cache',block_bits+bank_bits)]:
            objects=[self.reference(name,param,'RubyCache') for name in group]
            shapes=[(self.positive(name,'size'),self.positive(name,'assoc'),self.nonnegative(name,'start_index_bit')) for name in objects]
            if len(set(shapes))!=1:raise ProfileError('heterogeneous native coherence cache geometry')
            size,ways,start=shapes[0]
            sets=size//(ways*block)
            if start!=index_bit or size%(ways*block) or not sets or sets&(sets-1):
                raise ProfileError('native coherence cache index/geometry unsupported')
            flat['cache.'+level+'.size']=size*(len(llcs) if level=='llc' else 1)
            flat['cache.'+level+'.associativity']=ways;flat['cache.'+level+'.line_size']=block
            geometry[level]={'objects':objects,'bank_size':size,'ways':ways,'sets':sets,'start_index_bit':start}
            facts['source_keys'] += [name+'.'+k for name in objects for k in ('size','assoc','start_index_bit')]
        facts['geometry']=geometry
        ranges=[]
        for directory in dirs:
            raw=self.value(directory,'addr_ranges').split()
            if len(raw)!=1:raise ProfileError('native coherence requires one directory address range')
            try:parts=[int(x,0) for x in raw[0].split(':')]
            except ValueError:raise ProfileError('invalid native directory address range')
            if len(parts)<2 or parts[1]<=parts[0]:raise ProfileError('invalid native directory address range')
            start,end=parts[:2];match=parts[2] if len(parts)>2 else 0;masks=parts[3:]
            if not masks and match or len(masks)>63 or match>=(1<<len(masks)) or any(m<=0 for m in masks):
                raise ProfileError('invalid native directory interleave')
            port=self.value(directory,'memory_out_port');self.obj(port.rsplit('.',1)[0],'MemCtrl')
            ranges.append({'start':start,'end':end,'match':match,'masks':masks,'memory_port':port,'controller':directory})
        if len({r['memory_port'] for r in ranges})!=len(ranges):raise ProfileError('native coherence shared memory-port contract unsupported')
        facts['routes']={'physical_bank_low_bit':block_bits,'bank_select_bits':bank_bits,'directories':ranges}
        flat['spr.coherence.directory_count']=len(ranges)
        for i,r in enumerate(ranges):
            prefix='spr.coherence.directory.'+str(i)+'.'
            flat.update({prefix+k:r[k] for k in ('start','end','match')})
            flat[prefix+'masks']=','.join(str(m) for m in r['masks'])
        for relative,pattern,name in [
            ('src/mem/ruby/protocol/MESI_Three_Level-L0cache.sm',r'transition\(S, Store, SM\)','coherence_l0_upgrade'),
            ('src/mem/ruby/protocol/MESI_Three_Level-L1cache.sm',r'wakeUpAllBuffers\(address\)','coherence_private_wake_all'),
            ('src/mem/ruby/protocol/MESI_Two_Level-L2cache.sm',r'transition\(MT_IIB, Unblock, MT_IB\)','coherence_llc_unblock_order'),
            ('src/mem/ruby/protocol/MESI_Two_Level-dir.sm',r'transition\(I, Fetch, IM\)','coherence_directory_fetch'),
            ('src/mem/slicc/symbols/StateMachine.py',r'for port in self.in_ports','coherence_port_declaration_order'),
            ('src/mem/ruby/network/MessageBuffer.cc',r'm_delay_to_preserve_order','coherence_local_order')]:
            self.source_rule(relative,pattern,name)
        bound_sources=['src/mem/ruby/protocol/MESI_Three_Level.slicc', 'src/mem/ruby/protocol/MESI_Three_Level-L0cache.sm', 'src/mem/ruby/protocol/MESI_Three_Level-L1cache.sm', 'src/mem/ruby/protocol/MESI_Two_Level-L2cache.sm', 'src/mem/ruby/protocol/MESI_Two_Level-dir.sm', 'src/mem/ruby/protocol/MESI_Three_Level-msg.sm', 'src/mem/ruby/protocol/MESI_Two_Level-msg.sm', 'src/mem/slicc/symbols/StateMachine.py', 'src/mem/slicc/ast/FuncCallExprAST.py', 'src/mem/ruby/slicc_interface/AbstractController.cc', 'src/mem/ruby/network/MessageBuffer.cc']
        source_hashes={path:hashlib.sha256((self.source_root/path).read_bytes()).hexdigest() for path in bound_sources}
        source_identity=hashlib.sha256(json.dumps(source_hashes,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        facts['protocol_source_identity']={'sha256':source_identity,'files':source_hashes}
        flat['spr.coherence.source_identity']=source_identity
        facts['source_keys']=list(dict.fromkeys(facts['source_keys']))
        facts['status']='ordinary_data_target_facts_only'
        return facts,flat


def generate(config, *, participant_ids=None, source_capabilities=None, source_root=ROOT/'gem5-spr', native_coherence=False):
    capabilities = source_capabilities or {}
    if not isinstance(capabilities, dict) or any(not isinstance(k, str) or type(v) is not bool for k, v in capabilities.items()):
        raise ProfileError('source capabilities must map names to booleans')
    importer = Importer(config, source_root)
    cores, policy = importer.select(participant_ids)
    controllers = importer.active_controllers()
    profiles = [importer.core(cid, cpu, controllers, capabilities) for cid, cpu in cores]
    for profile in profiles:
        profile['generic_fu']=importer.generic_fu(profile['cpu_object'],profile['iq'],profile['integer_execution'])
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
    flat['spr.profile.generic_fu_preset']=first['generic_fu']['preset']
    flat['spr.profile.integer_nominal_shape_verified']=str(first['integer_execution']['nominal_shape_verified']).lower()
    coherence={'status':'not_requested','transport':{'status':'unsupported'}}
    if native_coherence:
        coherence,coherence_flat=importer.coherence(controllers,cores)
        flat.update(coherence_flat)
    return {'schema': SCHEMA, 'identity': {'static_uop_policy_sha256':static_policy, 'config_path': str(Path(config).resolve()),
             'config_sha256': hashlib.sha256(Path(config).read_bytes()).hexdigest(),
             'generator_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
             'source_capabilities_sha256': hashlib.sha256(json.dumps(capabilities, sort_keys=True).encode()).hexdigest(),
             'source_rules': importer.rules}, 'selection': {'policy': policy, 'participant_ids': [p['core_id'] for p in profiles]},
            'source_capabilities': capabilities, 'per_core': profiles, 'flat_config': flat, 'native_coherence':coherence,
            'qualification': 'target_description_only; no runtime, accuracy, native-PMU or throughput certification'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--participant-ids', help='comma separated CPU IDs from the selected active CPU objects')
    parser.add_argument('--source-capabilities', type=Path, help='JSON object mapping functional capability names to booleans')
    parser.add_argument('--source-root', type=Path, default=ROOT/'gem5-spr')
    parser.add_argument('--native-coherence', action='store_true', help='strict ordinary-data controller/queue/route import; runtime stays OFF')
    args = parser.parse_args()
    try:
        ids = None if args.participant_ids is None else [int(v) for v in args.participant_ids.split(',')]
        caps = {} if args.source_capabilities is None else json.loads(args.source_capabilities.read_text())
        profile = generate(args.config, participant_ids=ids, source_capabilities=caps, source_root=args.source_root,native_coherence=args.native_coherence)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(profile, indent=2, sort_keys=True) + '\n')
    except (ValueError, OSError, configparser.Error) as exc:
        print('SPR profile rejected: ' + str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
