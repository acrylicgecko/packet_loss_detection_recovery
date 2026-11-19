#!/usr/bin/env python3
"""live_demo.py - an interactive Mininet session for the packet-loss project.

This is a standalone terminal tool. It is NOT the HTML lab. It builds the same
two-path topology as the experiment, starts the OS-Ken controller (pathguard.py)
and the sender/receiver pair, and lets you inject and remove faults while traffic
flows. Use dashboard.py in a second terminal to watch the measurements.

Run as root:

    sudo python3 live_demo.py                  # SDN rerouting on
    sudo python3 live_demo.py --static         # static baseline (controller only observes)

Commands at the prompt:

    demo             guided walkthrough of every project step, one at a time
    show setup       topology, links, sockets, OpenFlow rules and source files
    status           sender and receiver counters, last controller poll
    break            take the primary link s1-s2 down
    restore          bring the primary link s1-s2 back up
    meter <pps>      OpenFlow meter on s2 allowing <pps> packets/s towards h2 (loss fault)
    meter off        remove the meter
    loss <pct>       netem loss on s1-s2 (needs sch_netem); "loss 0" clears it
    report           loss before/during/after and recovery times so far
    help             this list
    quit             stop everything, save report.txt, exit

Environment: MN_DATAPATH=user and MN_NETEM=0 work on VMs without the
openvswitch or sch_netem kernel modules (same settings as the report's test runs).
"""
import argparse
import json
import os
import subprocess
import threading
import time

from mininet.link import TCLink
from mininet.log import setLogLevel
from mininet.net import Mininet
from mininet.node import RemoteController

from live_metrics import compute, fmt_pct, fmt_s
from run_experiment import CTL_IP, check_ports
from topo import PRIMARY_DELAY, USE_NETEM, PathTopo

HERE = os.path.dirname(os.path.abspath(__file__))
SHOW_EVENTS = {'degraded', 'reroute', 'failback', 'no_alternate', 'port_status'}
HELP = __doc__.split('Commands at the prompt:')[1].split('Environment:')[0].rstrip()
FILES = [
    ('topo.py', 'builds the two-path Mininet topology and the switch port numbers'),
    ('run_controller.py', 'starts the OS-Ken controller on TCP 6633'),
    ('pathguard.py', 'controller app: base forwarding, path probes, loss estimate, reroute, failback'),
    ('sender.py', 'h1: HELLO handshake, numbered UDP sends, ACK/NACK handling, retransmission'),
    ('receiver.py', 'h2: WELCOME reply, gap detection, ACK/NACK, per-path arrival counts'),
    ('live_demo.py', 'this session: starts everything and runs the commands'),
    ('dashboard.py', 'second terminal: live view of the measurements'),
    ('live_metrics.py', 'loss windows and recovery times, computed from the event logs'),
    ('analyze.py / run_experiment.py', 'batch experiments (not used by this session)'),
]


class Demo:
    def __init__(self, out, static, rate):
        self.out = os.path.abspath(out)
        self.static = static
        self.rate = rate
        self.ctl_events = os.path.join(self.out, 'controller_events.jsonl')
        self.snd_events = os.path.join(self.out, 'sender_events.jsonl')
        self.rcv_events = os.path.join(self.out, 'receiver_events.jsonl')
        self.session = os.path.join(self.out, 'session.jsonl')
        self.t0 = time.time()
        self.net = None
        self.procs = []
        self.ctl = None
        self.stop_tail = threading.Event()

    # ------------------------------------------------------------ session marks
    def mark(self, k, **kw):
        with open(self.session, 'a') as f:
            f.write(json.dumps(dict(t=round(time.time(), 6), k=k, **kw)) + '\n')

    # ------------------------------------------------------------ setup
    def start(self):
        os.makedirs(self.out, exist_ok=True)
        for p in (self.session, self.snd_events, self.rcv_events, self.ctl_events):
            open(p, 'w').close()
        self.mark('start', mode='static' if self.static else 'sdn')

        env = dict(os.environ, PATHGUARD_EVENTS=self.ctl_events,
                   PATHGUARD_STATIC='1' if self.static else '0')
        self.ctl = subprocess.Popen(['python3', os.path.join(HERE, 'run_controller.py')], cwd=HERE, env=env,
                                    stdout=open(os.path.join(self.out, 'controller.out'), 'w'),
                                    stderr=subprocess.STDOUT)
        time.sleep(3)

        self.net = Mininet(topo=PathTopo(), link=TCLink, controller=None,
                           autoSetMacs=True, autoStaticArp=True)
        self.net.addController('c0', controller=RemoteController, ip='127.0.0.1', port=6633)
        ctl_host = self.net.addHost('ctl', inNamespace=False)
        self.net.addLink(ctl_host, self.net.get('s4'))
        self.net.start()
        self.net.waitConnected(timeout=30)
        missing = [sw.name for sw in self.net.switches if not sw.connected()]
        if missing:
            raise SystemExit('switches did not connect to the controller: %s' % ', '.join(missing))
        check_ports(self.net)

        ctl_host.cmd('ip addr add %s/24 dev ctl-eth0' % CTL_IP)
        ctl_host.cmd('ip link set ctl-eth0 up')
        h1, h2 = self.net.get('h1', 'h2')
        for h in (h1, h2):
            h.cmd('ethtool -K %s-eth0 tx off rx off' % h.name)
        ctl_mac = ctl_host.intf().MAC()
        h1.cmd('arp -s %s %s' % (CTL_IP, ctl_mac))
        h2.cmd('arp -s %s %s' % (CTL_IP, ctl_mac))

        # receiver first, so it is listening when the sender sends HELLO
        rx = h2.popen(['python3', os.path.join(HERE, 'receiver.py'), '--duration', '86400',
                       '--status-every', '2', '--events', self.rcv_events,
                       '--log', os.path.join(self.out, 'receiver_log.json')],
                      stdout=open(os.path.join(self.out, 'receiver.out'), 'w'), stderr=subprocess.STDOUT)
        time.sleep(1.0)
        tx = h1.popen(['python3', os.path.join(HERE, 'sender.py'), '--duration', '86400',
                       '--rate', str(self.rate), '--status-every', '2', '--events', self.snd_events,
                       '--log', os.path.join(self.out, 'sender_log.json')],
                      stdout=open(os.path.join(self.out, 'sender.out'), 'w'), stderr=subprocess.STDOUT)
        self.procs = [rx, tx]
        threading.Thread(target=self.tail_events, daemon=True).start()
        self.wait_ready()

    def wait_ready(self):
        """Wait for the handshake and first data, then for the controller's probe window to fill.
        Before that the controller cannot judge the alternate path, so a fault typed too early
        would not trigger an immediate reroute."""
        print('handshake and first datagrams (about 10 s)...', flush=True)
        end = time.time() + 30
        while time.time() < end and not (self.has_event(self.snd_events, 'welcome')
                                         and self.has_event(self.rcv_events, 'rx')):
            time.sleep(0.5)
        time.sleep(6.5)     # probe window: 8 polls + 2 lag polls at 0.5 s
        self.mark('phase', name='ready', text='session ready')
        print('ready. Type "demo" for the guided walkthrough, or "help".', flush=True)

    # ------------------------------------------------------------ helpers
    def has_event(self, path, kind):
        return any(e.get('k') == kind for e in read_jsonl(path))

    def tail_events(self):
        pos = 0
        while not self.stop_tail.is_set():
            if os.path.exists(self.ctl_events):
                with open(self.ctl_events) as f:
                    f.seek(pos)
                    lines = f.readlines()
                    pos = f.tell()
                for line in lines:
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue
                    if e.get('kind') in SHOW_EVENTS:
                        print('\n[%7.2fs] controller: %s' % (e['t'] - self.t0, describe(e)), flush=True)
                        print('> ', end='', flush=True)
            time.sleep(0.2)

    def last_event(self, kind):
        found = None
        for e in read_jsonl(self.ctl_events):
            if e.get('kind') == kind:
                found = e
        return found

    def last_line(self, name):
        try:
            with open(os.path.join(self.out, name)) as f:
                lines = [l.strip() for l in f if l.strip()]
            return lines[-1] if lines else '(no output yet)'
        except OSError:
            return '(no output yet)'

    def wait_for(self, cond, timeout, label):
        print('waiting for %s' % label, end='', flush=True)
        end = time.time() + timeout
        while time.time() < end:
            if cond():
                print(' done', flush=True)
                return True
            print('.', end='', flush=True)
            time.sleep(1.0)
        print(' timed out after %ds' % timeout, flush=True)
        return False

    def ctl_after(self, kind, since):
        return [e for e in read_jsonl(self.ctl_events) if e.get('kind') == kind and e['t'] >= since]

    # ------------------------------------------------------------ commands
    def cmd_status(self):
        print('sender   :', self.last_line('sender.out'))
        print('receiver :', self.last_line('receiver.out'))
        poll = self.last_event('poll')
        if poll:
            print('controller: active=%s loss_active=%s loss_other=%s probes_ok=%s'
                  % (poll['active'], fmt(poll['loss_active']), fmt(poll['loss_other']), poll['probes_ok']))
        print('mode     :', 'static (no reroute)' if self.static else 'SDN (reroute on)')

    def cmd_show_setup(self):
        print('\nPROGRAMS (folder %s)' % HERE)
        for name, purpose in FILES:
            print('  %-30s %s' % (name, purpose))
        print('\nADDRESSES')
        print('  h1 10.0.0.1 (sender, UDP ports 9001 for ACK/NACK/WELCOME)')
        print('  h2 10.0.0.2 (receiver, UDP ports 9000 data, 9002 probes)')
        print('  controller 10.0.0.254 (root namespace, reports on UDP 9100), OpenFlow on TCP 6633')
        print('\nLINKS (Mininet interface name, switch port number is the last digit)')
        for l in self.net.links:
            print('  %-10s <-> %s' % (l.intf1.name, l.intf2.name))
        print('\nSOCKETS (ss -uan on each host, UDP only)')
        for h in self.net.get('h1', 'h2'):
            lines = [l for l in h.cmd('ss -uan').splitlines() if any(':%d' % p in l for p in (9000, 9001, 9002, 9100))]
            print('  %s:' % h.name)
            for l in lines or ['    (none yet)']:
                print('    ' + l.strip())
        print('\nHANDSHAKE (from the event logs)')
        for e in read_jsonl(self.snd_events)[:4] + read_jsonl(self.rcv_events)[:3]:
            if e.get('k') in ('hello', 'welcome', 'listen'):
                print('  %+7.3fs %-8s %s' % (e['t'] - self.t0, e['k'], {k: v for k, v in e.items() if k not in ('t', 'k')}))
        print('\nOPENFLOW RULES (priority, first lines per switch)')
        for sw in ('s1', 's2', 's3', 's4'):
            r = subprocess.run(['ovs-ofctl', '-O', 'OpenFlow13', 'dump-flows', sw],
                               capture_output=True, text=True)
            rules = [l[l.index('priority='):].strip() for l in r.stdout.splitlines() if 'priority=' in l]
            print('  %s: %d rules' % (sw, len(rules)))
            for rule in rules[:4]:
                print('      ' + rule[:110])
        print()

    def s1_s2_link(self):
        return self.net.linksBetween(self.net.get('s1'), self.net.get('s2'))[0]

    def cmd_break(self):
        self.mark('fault', kind='break', text='fault: primary link s1-s2 goes DOWN')
        self.net.configLinkStatus('s1', 's2', 'down')
        print('primary link s1-s2 is DOWN')

    def cmd_restore(self):
        self.net.configLinkStatus('s1', 's2', 'up')
        self.mark('clear', kind='break', text='repair: primary link s1-s2 is UP')
        print('primary link s1-s2 is UP')

    def cmd_loss(self, pct):
        link = self.s1_s2_link()
        for intf in (link.intf1, link.intf2):
            # call tc directly: Mininet's config() does not raise when tc fails
            args = ['tc', 'qdisc', 'replace', 'dev', intf.name, 'root', 'netem']
            if USE_NETEM:
                args += ['delay', PRIMARY_DELAY]
            args += ['loss', '%g%%' % pct]
            r = subprocess.run(args, capture_output=True, text=True)
            if r.returncode != 0:
                print('netem loss failed on %s (is sch_netem loaded?): %s' % (intf.name, r.stderr.strip()))
                return
        if pct:
            self.mark('fault', kind='loss', text='fault: %g%% packet loss on s1-s2' % pct)
        else:
            self.mark('clear', kind='loss', text='repair: loss removed from s1-s2')
        print('loss on s1-s2 set to %g%%' % pct)

    def _remove_meter(self):
        os.system('ovs-ofctl -O OpenFlow13 del-flows s2 "priority=6,ip,nw_dst=10.0.0.2" 2>/dev/null')
        os.system('ovs-ofctl -O OpenFlow13 del-meter s2 meter=1 2>/dev/null')

    def cmd_meter(self, pps):
        self._remove_meter()
        os.system('ovs-ofctl -O OpenFlow13 add-meter s2 meter=1,pktps,burst,band=type=drop,rate=%d,burst_size=1'
                  % pps)
        os.system('ovs-ofctl -O OpenFlow13 add-flow s2 "priority=6,ip,nw_dst=10.0.0.2,actions=meter:1,output:2"')
        self.mark('fault', kind='meter', text='fault: primary path limited to %d packets/s (excess is dropped)' % pps)
        print('meter on s2: %d packets/s allowed towards h2' % pps)

    def cmd_meter_off(self, quiet=False):
        self._remove_meter()
        self.mark('clear', kind='meter', text='repair: meter removed, primary path is clean')
        if not quiet:
            print('meter removed')

    def cmd_report(self):
        m = compute(self.out)
        print('\n' + report_text(m) + '\n')
        return m

    # ------------------------------------------------------------ guided walkthrough
    def phase(self, name, text):
        self.mark('phase', name=name, text=text)
        print('\n%s\n%s' % ('=' * 70, name), flush=True)

    def pause(self):
        input('  [Enter] to continue ')

    def cmd_demo(self):
        self.phase('1. Set up the network', 'topology, links, sockets, OpenFlow rules')
        self.cmd_show_setup()
        self.pause()

        self.phase('2. Connection', 'UDP has no connection setup, so the sender says HELLO and the receiver answers WELCOME')
        hellos = [e for e in read_jsonl(self.snd_events) if e.get('k') == 'hello']
        welcome = [e for e in read_jsonl(self.snd_events) if e.get('k') == 'welcome']
        if welcome:
            print('  sender sent %d HELLO, session accepted %.2fs after the first HELLO'
                  % (len(hellos), welcome[0]['t'] - hellos[0]['t']))
        self.pause()

        self.phase('3. Path viability', 'the controller probes both paths before the fault')
        poll = self.last_event('poll')
        if poll:
            print('  probes arriving within the window: primary %s, alternate %s'
                  % ('healthy' if poll['probes_ok']['P'] else 'UNHEALTHY',
                     'healthy' if poll['probes_ok']['A'] else 'UNHEALTHY'))
        self.pause()

        self.phase('4. Intermittent traffic and sequence tracking',
                   'h1 sends numbered datagrams; h2 detects gaps; baseline loss before any fault')
        time.sleep(12)
        m = compute(self.out)
        print('  sent %d, delivered %d, missing now %d, loss before fault %s'
              % (m['sent'], m['delivered'], m['missing_now'], fmt_pct(m['before'])))
        self.pause()

        self.phase('5. Fault: the primary path degrades',
                   'a meter limits s2 to 3 packets/s, so most datagrams on the primary path are dropped')
        self.cmd_meter(3)
        t_fault = time.time()
        self.wait_for(lambda: bool(self.ctl_after('reroute', t_fault)), 40, 'the controller to reroute')
        for e in self.ctl_after('degraded', t_fault)[:1] + self.ctl_after('reroute', t_fault)[:1]:
            print('  %+.2fs controller: %s' % (e['t'] - t_fault, describe(e)))
        self.pause()

        self.phase('6. Recovery: NACKs and retransmission',
                   'lost datagrams are NACKed or timed out and sent again on the alternate path')
        self.wait_for(lambda: compute(self.out)['cleared'] is not None, 40, 'the backlog to clear')
        print(report_text(compute(self.out)))
        self.pause()

        self.phase('7. Repair: fault removed, failback', 'the primary path is clean again')
        t_fix = time.time()
        self.cmd_meter_off(quiet=True)
        self.wait_for(lambda: bool(self.ctl_after('failback', t_fix)), 40, 'failback to the primary path')
        print(report_text(compute(self.out)))
        self.pause()

        self.phase('8. Results', 'loss before, during and after; recovery times')
        self.wait_for(lambda: compute(self.out)['final'], 30, 'the fault window to close')
        self.cmd_report()

    # ------------------------------------------------------------ REPL
    def repl(self):
        print('Session running. Type "demo" for the guided walkthrough, or "help".')
        while True:
            try:
                line = input('> ').strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return
            if not line:
                continue
            parts = line.split()
            name, args = parts[0].lower(), parts[1:]
            try:
                if name in ('quit', 'exit', 'q'):
                    return
                elif name == 'help':
                    print(HELP)
                elif name == 'demo':
                    self.cmd_demo()
                elif name == 'show' and args[:1] == ['setup']:
                    self.cmd_show_setup()
                elif name == 'status':
                    self.cmd_status()
                elif name == 'report':
                    self.cmd_report()
                elif name == 'break':
                    self.cmd_break()
                elif name == 'restore':
                    self.cmd_restore()
                elif name == 'loss' and len(args) == 1:
                    self.cmd_loss(float(args[0]))
                elif name == 'meter' and args == ['off']:
                    self.cmd_meter_off()
                elif name == 'meter' and len(args) == 1:
                    self.cmd_meter(int(args[0]))
                else:
                    print('unknown command: %s (type help)' % line)
            except ValueError:
                print('bad argument in: %s' % line)

    def stop(self):
        self.stop_tail.set()
        for p in self.procs:
            p.terminate()
        if self.ctl is not None:
            self.ctl.terminate()
        if self.net is not None:
            self.net.stop()
        try:
            with open(os.path.join(self.out, 'report.txt'), 'w') as f:
                f.write(report_text(compute(self.out)) + '\n')
        except Exception as e:  # never let the report block shutdown
            print('could not write report: %s' % e)
        print('stopped. logs and report.txt in %s' % self.out)


def read_jsonl(path):
    out = []
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    return out


def fmt(v):
    return '-' if v is None else '%.1f%%' % (100 * v)


def describe(e):
    k = e['kind']
    if k == 'degraded':
        return 'path %s degraded (%s)' % (e['path'], e.get('reason') or 'loss %s' % fmt(e.get('loss')))
    if k == 'reroute':
        return 'REROUTE %s -> %s (%s)' % (e['frm'], e['to'], e['reason'])
    if k == 'failback':
        return 'FAILBACK %s -> %s' % (e['frm'], e['to'])
    if k == 'no_alternate':
        return 'path %s degraded but the other path is not healthy: staying put' % e['path']
    if k == 'port_status':
        return 'port of path %s is %s' % (e['path'], 'DOWN' if e['down'] else 'up')
    return k


def report_text(m):
    during_note = '' if m.get('final') or m['fault'] is None else ' (live, window still open)'
    return '\n'.join([
        'RESULTS so far (session time %s, phase %s)' % (fmt_s(m['now']), m['phase']),
        '  sent %d, delivered %d, missing now %d' % (m['sent'], m['delivered'], m['missing_now']),
        '  transmissions %d, retransmissions %d, abandoned %d' % (m['tx'], m['retx'], m['abandoned']),
        '  first-attempt loss: before fault %s, during 15 s %s%s, after 15 s %s (to now)'
        % (fmt_pct(m['before']), fmt_pct(m['during']), during_note, fmt_pct(m['after'])),
        '  recovery: fault at %s, detect %s, reroute %s, first on alternate %s, backlog cleared %s'
        % (fmt_s(m['fault']), fmt_s(m['detect']), fmt_s(m['reroute']), fmt_s(m['first_alt']),
           fmt_s(m['cleared'])),
    ])


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--out', default=os.path.join(HERE, 'results', 'live'),
                    help='directory for this session\'s logs')
    ap.add_argument('--static', action='store_true', help='disable rerouting (baseline)')
    ap.add_argument('--rate', type=float, default=8.0, help='datagrams per second')
    a = ap.parse_args()
    if os.geteuid() != 0:
        raise SystemExit('run as root (sudo)')

    setLogLevel('warning')
    subprocess.run(['mn', '-c'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # clear old Mininet state
    demo = Demo(a.out, a.static, a.rate)
    try:
        demo.start()
        demo.repl()
    finally:
        demo.stop()


if __name__ == '__main__':
    main()
