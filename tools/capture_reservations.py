"""Shared atomic FST budget admission; dead workers never expire automatically."""
import fcntl,json,os,shutil,time
from contextlib import contextmanager
from pathlib import Path

def byte_snapshot(root,outputs):
    """One observation per file, shared by total and per-output accounting."""
    root=Path(root).resolve();outputs=[Path(p).resolve() for p in outputs]
    sizes={str(p):0 for p in outputs};total=0
    def scan_error(error):
        if not isinstance(error,FileNotFoundError):raise error
    for directory,_,names in os.walk(root,followlinks=False,onerror=scan_error):
        parent=Path(directory)
        owners=[str(p) for p in outputs if parent==p or p in parent.parents]
        for name in names:
            if not name.endswith('.fst'):continue
            f=parent/name
            try:
                if f.is_symlink() or not f.is_file():continue
                size=f.stat().st_size
            except FileNotFoundError:continue
            total+=size
            for owner in owners:sizes[owner]+=size
    return total,sizes

class ReservationPool:
    def __init__(self,root,ledger,*,limit,minimum_free=0):
        self.root=Path(root).resolve();self.ledger=Path(ledger);self.limit=limit;self.minimum_free=minimum_free
        self.lock=self.ledger.with_suffix('.lock')
    @contextmanager
    def locked(self):
        self.lock.parent.mkdir(parents=True,exist_ok=True)
        with self.lock.open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            state=json.loads(self.ledger.read_text()) if self.ledger.exists() else dict(entries={})
            yield state
            tmp=self.ledger.with_suffix('.tmp');tmp.write_text(json.dumps(state,indent=2)+'\n');tmp.replace(self.ledger)
    def claim(self,output,cap):
        output=Path(output).resolve();output.relative_to(self.root);key=str(output)
        if cap<=0:raise ValueError('positive reservation required')
        with self.locked() as state:
            entries=state['entries']
            if key in entries:raise RuntimeError('capture directory already reserved: '+key)
            # Do not expire a dead worker automatically: its producer may live on.
            actual,sizes=byte_snapshot(self.root,[*entries,key])
            remaining=sum(max(0,r['cap_bytes']-sizes[p]) for p,r in entries.items())
            requested=max(0,cap-sizes[key])
            if actual+remaining+requested>self.limit:return False
            if shutil.disk_usage(self.root).free<self.minimum_free+remaining+requested:return False
            entries[key]=dict(cap_bytes=cap,owner_pid=os.getpid(),reserved_unix=time.time())
            state.update(last_actual_bytes=actual,last_remaining_reserved_bytes=remaining+requested,
                         last_upper_bound_bytes=actual+remaining+requested,limit_bytes=self.limit,checked_unix=time.time())
            return True
    def release(self,output):
        key=str(Path(output).resolve())
        with self.locked() as state:
            entry=state['entries'][key]
            if entry['owner_pid']!=os.getpid():raise RuntimeError('reservation belongs to another worker')
            del state['entries'][key]
