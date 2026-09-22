from m5.objects.Probe import ProbeListenerObject


class BranchEvents(ProbeListenerObject):
    type = "BranchEvents"
    cxx_class = "gem5::o3::BranchEvents"
    cxx_header = "cpu/o3/probe/branch_events.hh"
