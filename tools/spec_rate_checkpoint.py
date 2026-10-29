"""Content-bound staged KVM checkpoints; no timing or PMU reference selection."""
import hashlib
import json
from pathlib import Path


class RateCheckpointFlow:
    """Marker order is explicit; a restored marker is never expected to fire again."""
    def __init__(self, phase, restored=False):
        if phase not in ('staged', 'compute-ready'):
            raise ValueError('unknown checkpoint phase')
        self.save_marker = 1 if phase == 'staged' else 2
        self.marker = self.save_marker if restored else 0
        self.switched = False

    def checkpoint(self):
        self.marker += 1
        if self.switched:
            return 'ignore'
        if self.marker < self.save_marker:
            return 'continue'
        if self.marker == self.save_marker:
            return 'save'
        self.switched = True
        return 'switch'


def sha256(path):
    digest=hashlib.sha256()
    with Path(path).open('rb') as handle:
        for block in iter(lambda:handle.read(8<<20),b''):digest.update(block)
    return digest.hexdigest()


def payload_files(directory):
    directory=Path(directory)
    return {str(p.relative_to(directory)):sha256(p)
            for p in sorted(directory.rglob('*'))
            if p.is_file() and p.name!='fastsim-checkpoint.json'}


def validate_receipt(directory,receipt,identity):
    if receipt['identity']!=identity:raise ValueError('checkpoint input/hardware identity mismatch')
    if 'm5.cpt' not in receipt['files'] or receipt['files']!=payload_files(directory):
        raise ValueError('checkpoint payload missing or corrupted')


def write_receipt(directory,identity):
    directory=Path(directory)
    receipt=dict(schema='fastsim-rate-staged-kvm-checkpoint-v1',identity=identity,
                 files=payload_files(directory))
    if 'm5.cpt' not in receipt['files']:raise ValueError('checkpoint has no CPU state')
    (directory/'fastsim-checkpoint.json').write_text(json.dumps(receipt,indent=2)+'\n')
    return receipt
