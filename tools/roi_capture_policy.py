"""Functional instruction-budget warmup and host wall-time guard, no PMU inputs."""
class BoundedWarmup:
    def __init__(self,instructions,defer_until_identities=False):
        if type(instructions) is not int or instructions<0:raise ValueError('invalid warmup instructions')
        if defer_until_identities and not instructions:raise ValueError('deferred native warmup requires positive instructions')
        self.instructions=instructions;self.switched=False;self.measured=False
        self.defer_until_identities=defer_until_identities;self.armed=False

    def checkpoint(self):
        if self.switched:return False
        self.switched=True
        if self.defer_until_identities:return False
        self.armed=True
        return True

    def identities_ready(self):
        if not self.defer_until_identities or not self.switched or self.armed or self.measured:
            raise ValueError('unexpected native identity checkpoint order')
        self.armed=True
        return True

    def instruction_exit(self):
        if not self.switched:raise ValueError('instruction exit before KVM handoff')
        if not self.armed:raise ValueError('instruction exit before native warmup armed')
        if self.instructions and not self.measured:
            self.measured=True
            return 'open-measurement'
        return 'finish-measurement'


class WarmupDeadline:
    def __init__(self,seconds):
        if seconds<0:raise ValueError('invalid warmup deadline')
        self.seconds=seconds;self.started=None;self.measured=False;self.measurement_started=None

    def observe(self,text,now):
        if self.started is None and 'CHECKPOINT marker: switch KVM -> O3+Ruby' in text:
            self.started=now
        if 'measurement window open' in text:
            self.measured=True
            if self.measurement_started is None:self.measurement_started=now
        if self.seconds and self.started is not None and not self.measured and now-self.started>=self.seconds:
            return 'warmup-wall-time-limit'
        return None
