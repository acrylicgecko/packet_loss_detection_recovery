"""topo.py - two-path topology for the packet-loss experiment.

    h1 (10.0.0.1) -- s1 ==== s2 ==== s4 -- h2 (10.0.0.2)     primary path
                    s1 ==== s3 ====/                          alternate path
                    ctl (10.0.0.254, NAT interface, added by run_experiment.py)

Mininet numbers switch ports in the order links are added. The controller
(pathguard.py) depends on these port numbers:

    s1: port 1 = h1, port 2 = s2 (primary), port 3 = s3 (alternate)
    s2: port 1 = s1, port 2 = s4
    s3: port 1 = s1, port 2 = s4
    s4: port 1 = s2, port 2 = s3, port 3 = h2, port 4 = ctl (added last)

run_experiment.py checks these numbers at start-up (see check_ports()).
"""
import os

from mininet.topo import Topo

# OVS datapath: "kernel" (default, needs the openvswitch kernel module) or "user"
# (userspace datapath, for VMs without that module). Set MN_DATAPATH=user to use it.
DATAPATH = os.environ.get('MN_DATAPATH', 'kernel')

# Link delays use tc netem. On kernels without sch_netem, set MN_NETEM=0 to build the
# same topology without delays (the path roles and port numbers do not change).
USE_NETEM = os.environ.get('MN_NETEM', '1') != '0'
PRIMARY_DELAY = "3ms" if USE_NETEM else None
ALTERNATE_DELAY = '5ms' if USE_NETEM else None


class PathTopo(Topo):
    @staticmethod
    def _d(delay):
        return {'delay': delay} if delay else {}

    def build(self):
        h1 = self.addHost('h1', ip='10.0.0.1/24')
        h2 = self.addHost('h2', ip='10.0.0.2/24')
        s1 = self.addSwitch('s1', protocols='OpenFlow13', datapath=DATAPATH)
        s2 = self.addSwitch('s2', protocols='OpenFlow13', datapath=DATAPATH)
        s3 = self.addSwitch('s3', protocols='OpenFlow13', datapath=DATAPATH)
        s4 = self.addSwitch('s4', protocols='OpenFlow13', datapath=DATAPATH)
        self.addLink(h1, s1)
        self.addLink(s1, s2, **self._d(PRIMARY_DELAY))     # primary leg 1
        self.addLink(s2, s4, **self._d(PRIMARY_DELAY))     # primary leg 2
        self.addLink(s1, s3, **self._d(ALTERNATE_DELAY))   # alternate leg 1
        self.addLink(s3, s4, **self._d(ALTERNATE_DELAY))   # alternate leg 2
        self.addLink(s4, h2)


topos = {'pathtopo': PathTopo}
