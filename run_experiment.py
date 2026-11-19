#!/usr/bin/env python3
"""run_experiment.py - builds the two-path topology, runs sender (h1) and
receiver (h2), injects one fault on the primary path, and saves logs.

Run as root, with run_controller.py already running in another terminal:

    sudo python3 run_experiment.py --out results/sdn_loss60_r1 --fault loss --loss 60
    sudo python3 run_experiment.py --out results/static_down_r1 --fault down
    sudo python3 run_experiment.py --out results/none_r1 --fault none

Output directory gets: sender_log.json, receiver_log.json, fault.json,
run_info.json. Analyse with:  python3 analyze.py results/<dir>
"""
import argparse
import json
import os
import subprocess
import sys
import time

from mininet.link import TCLink
from mininet.log import setLogLevel
from mininet.net import Mininet
from mininet.node import RemoteController

from topo import PRIMARY_DELAY, PathTopo

HERE = os.path.dirname(os.path.abspath(__file__))
CTL_IP = '10.0.0.254'
EXPECTED_PORTS = {          # must match the comments in topo.py and the constants in pathguard.py
    's1': {1: 'h1', 2: 's2', 3: 's3'},
    's2': {1: 's1', 2: 's4'},
    's3': {1: 's1', 2: 's4'},
    's4': {1: 's2', 2: 's3', 3: 'h2', 4: 'ctl'},
}


def peer_at(node, port):
    intf = node.intfs.get(port)
    if intf is None or intf.link is None:
        return None
    other = intf.link.intf2 if intf.link.intf1 is intf else intf.link.intf1
    return other.node.name


def check_ports(net):
    for sw, expect in EXPECTED_PORTS.items():
        for port, peer in expect.items():
            got = peer_at(net.get(sw), port)
            if got != peer:
                raise SystemExit('port map mismatch: %s port %d is %r, expected %r' % (sw, port, got, peer))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--out', required=True, help='directory for the logs of this run')
    ap.add_argument('--fault', choices=['none', 'loss', 'meter', 'down'], default='loss',
                    help="loss = tc netem on s1-s2 (needs the sch_netem kernel module); "
                         "meter = OpenFlow meter on s2 that drops packets above --meter-pps "
                         "(fallback when netem is unavailable)")
    ap.add_argument('--meter-pps', type=int, default=3, help='packets/s allowed through s2 (fault=meter)')
    ap.add_argument('--loss', type=float, default=60.0, help='loss %% on the primary path (fault=loss)')
    ap.add_argument('--fault-at', type=float, default=15.0, help='seconds after sending starts')
    ap.add_argument('--duration', type=float, default=60.0, help='seconds of sending')
    ap.add_argument('--rate', type=float, default=8.0, help='datagrams per second')
    a = ap.parse_args()
    if os.geteuid() != 0:
        raise SystemExit('run as root (sudo)')

    out = os.path.abspath(a.out)
    os.makedirs(out, exist_ok=True)
    setLogLevel('warning')

    net = Mininet(topo=PathTopo(), link=TCLink, controller=None, autoSetMacs=True, autoStaticArp=True)
    net.addController('c0', controller=RemoteController, ip='127.0.0.1', port=6633)
    # the controller's report address: a root-namespace node, so the OS-Ken
    # process (run outside Mininet) can receive REPORT datagrams from h2
    ctl = net.addHost('ctl', inNamespace=False)
    net.addLink(ctl, net.get('s4'))          # added last, so it becomes s4 port 4
    net.start()
    try:
        try:
            net.waitConnected(timeout=30)
        except Exception:
            pass
        # waitConnected() only prints a warning on timeout, so check the switches directly
        missing = [sw.name for sw in net.switches if not sw.connected()]
        if missing:
            raise SystemExit('switches did not connect to the controller (is run_controller.py running?): %s'
                             % ', '.join(missing))
        check_ports(net)
        ctl.cmd('ip addr add %s/24 dev ctl-eth0' % CTL_IP)
        ctl.cmd('ip link set ctl-eth0 up')
        h1, h2 = net.get('h1', 'h2')
        for h in (h1, h2):   # veth checksum offload leaves UDP checksums unfinished for the switch
            h.cmd('ethtool -K %s-eth0 tx off rx off' % h.name)
        ctl_mac = ctl.intf().MAC()
        h1.cmd('arp -s %s %s' % (CTL_IP, ctl_mac))
        h2.cmd('arp -s %s %s' % (CTL_IP, ctl_mac))

        rx_log = os.path.join(out, 'receiver_log.json')
        tx_log = os.path.join(out, 'sender_log.json')
        rx = h2.popen(['python3', os.path.join(HERE, 'receiver.py'),
                       '--duration', str(a.duration + 20), '--log', rx_log],
                      stdout=open(os.path.join(out, 'receiver.out'), 'w'), stderr=subprocess.STDOUT)
        time.sleep(1.0)
        tx = h1.popen(['python3', os.path.join(HERE, 'sender.py'),
                       '--duration', str(a.duration), '--rate', str(a.rate), '--log', tx_log],
                      stdout=open(os.path.join(out, 'sender.out'), 'w'), stderr=subprocess.STDOUT)

        start = time.time()
        time.sleep(a.fault_at)
        fault = {'fault': a.fault, 't': time.time(), 'loss_pct': a.loss if a.fault == 'loss' else None,
                 'meter_pps': a.meter_pps if a.fault == 'meter' else None}
        if a.fault == 'loss':
            link = net.linksBetween(net.get('s1'), net.get('s2'))[0]
            link.intf1.config(delay=PRIMARY_DELAY, loss=a.loss)
            link.intf2.config(delay=PRIMARY_DELAY, loss=a.loss)
        elif a.fault == 'meter':
            s2 = net.get('s2').name
            os.system('ovs-ofctl -O OpenFlow13 add-meter %s meter=1,pktps,burst,band=type=drop,rate=%d,burst_size=1'
                      % (s2, a.meter_pps))
            os.system('ovs-ofctl -O OpenFlow13 add-flow %s "priority=6,ip,nw_dst=10.0.0.2,actions=meter:1,output:2"'
                      % s2)
        elif a.fault == 'down':
            net.configLinkStatus('s1', 's2', 'down')
        with open(os.path.join(out, 'fault.json'), 'w') as f:
            json.dump(fault, f)
        print('fault injected: %s' % fault, flush=True)

        tx.wait()
        rx.wait()
        with open(os.path.join(out, 'run_info.json'), 'w') as f:
            json.dump({'args': vars(a), 'start': start, 'controller_events': 'controller_events.jsonl'}, f, indent=1)
        print('run complete, logs in %s' % out, flush=True)
    finally:
        net.stop()


if __name__ == '__main__':
    main()
