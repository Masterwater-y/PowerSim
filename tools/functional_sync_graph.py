"""Build synchronization causality solely from functional facts (no clocks)."""
from collections import defaultdict
import heapq


def build_graph(rows):
    events = sorted(rows, key=lambda e: (e['core_id'], e['record_ordinal']))
    deps = [set() for _ in events]
    streams, regions, barriers = defaultdict(list), defaultdict(list), defaultdict(list)
    bindings, owners = {}, {}
    anchors = set()
    for i, e in enumerate(events):
        core, asid, tid = e['core_id'], e['address_space_id'], e['thread_id']
        if e['event_kind'] not in range(1,9):
            raise ValueError('unsupported synchronization event')
        anchor = (core,e['record_ordinal'])
        if anchor in anchors: raise ValueError('duplicate event anchor')
        anchors.add(anchor)
        identity = (asid,tid)
        if identity in bindings and bindings[identity] != core:
            raise ValueError('thread migration changes static binding')
        if core in owners and owners[core] != identity:
            raise ValueError('oversubscription changes static binding')
        bindings[identity], owners[core] = core, identity
        streams[core].append(i)
        regions[(asid,e['region_instance'])].append(i)
        if e['event_kind'] in (5,6,7,8):
            barriers[(asid,e['region_instance'],e['sync_object_id'],e['generation'])].append(i)
    for stream in streams.values():
        active = {}
        for i in stream:
            e=events[i]
            identity=(e['address_space_id'],e['thread_id'])
            if e['event_kind']==3:
                if identity in active: raise ValueError('nested or overlapping implicit task')
                active[identity]=e['region_instance']
            elif e['event_kind']==4:
                if active.get(identity)!=e['region_instance']: raise ValueError('task end without matching begin')
                del active[identity]
        for a,b in zip(stream,stream[1:]): deps[b].add(a)
    for region in regions.values():
        begin = [i for i in region if events[i]['event_kind']==1]
        if len(begin)!=1: raise ValueError('missing or duplicate parallel begin')
        for i in region:
            if i != begin[0]: deps[i].add(begin[0])
        for i in region:
            if events[i]['event_kind']==2:
                ended = [j for j in region if events[j]['event_kind']==4]
                final = [j for j in region if events[j]['event_kind']==7]
                master = [j for j in region if events[j]['event_kind']==8 and events[j]['member_index']==0]
                n = events[i]['team_size']
                if len(ended)!=n or len(final)!=n or len(master)!=1:
                    raise ValueError('join missing final arrivals/task ends/master departure')
                deps[i].update(ended+final+master)
    for group in barriers.values():
        arrivals = [i for i in group if events[i]['event_kind'] in (5,7)]
        departures = [i for i in group if events[i]['event_kind'] in (6,8)]
        if departures:
            n = events[departures[0]]['team_size']
            if len(arrivals)!=n or {events[i]['member_index'] for i in arrivals} != set(range(n)):
                raise ValueError('departure missing complete arrival population')
            for i in departures: deps[i].update(arrivals)
    successors = [[] for _ in events]
    left = [len(d) for d in deps]
    for i, d in enumerate(deps):
        for p in d:
            if events[i]['phase']=='warmup' and events[p]['phase']!='warmup':
                raise ValueError('reverse measurement-to-warmup phase dependency')
            successors[p].append(i)
    ready = [i for i,c in enumerate(left) if c==0]
    heapq.heapify(ready)
    order = []
    while ready:
        i = heapq.heappop(ready)
        order.append(i)
        for j in successors[i]:
            left[j]-=1
            if left[j]==0: heapq.heappush(ready,j)
    if len(order)!=len(events): raise ValueError('synchronization dependency cycle')
    return events, deps, order
