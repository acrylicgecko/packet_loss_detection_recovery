"""pathguard.py - OS-Ken SDN application for the packet-loss experiment.

Responsibilities
  1. Install base forwarding on s1..s4 for the 10.0.0.x hosts and the controller.
  2. Monitor both paths:
       - data loss on the ACTIVE path = packets counted leaving s1 (flow stats)
         versus packets counted arriving at h2 (REPORT from receiver.py);
       - path health for BOTH paths from probe packets the controller injects at s1
         with packet-out (one per path per poll); arrivals counted at h2.
  3. Reroute: on loss above 20% for two consecutive polls, or on PORT_STATUS
     link-down of the active primary port, move data (s1) and ACK return (s4) to the
     alternate path with priority-200 flows. Fail back to the primary path after
     6 s with healthy primary probes.

Set PATHGUARD_STATIC=1 to disable rerouting (static baseline; monitoring still logs).
Events are appended as JSON lines to PATHGUARD_EVENTS (default controller_events.jsonl).
"""
import json
import os
import socket
import time

from os_ken.base import app_manager
from os_ken.controller import ofp_event
from os_ken.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from os_ken.lib import hub
from os_ken.lib.packet import ethernet, ipv4, packet, udp
from os_ken.ofproto import ofproto_v1_3

PRIMARY, ALT = 'P', 'A'
S1, S2, S3, S4 = 1, 2, 3, 4          # datapath ids = Mininet switch numbers
OUT = {PRIMARY: 2, ALT: 3}           # s1 output ports towards s2 / s3
DSCP = {PRIMARY: 10, ALT: 20}        # mark written on s1 (and on probes)
H1_IP, H2_IP, CTL_IP = '10.0.0.1', '10.0.0.2', '10.0.0.254'
H2_MAC, CTL_MAC = '00:00:00:00:00:02', '00:00:00:00:00:fe'
S4_CTL_PORT = 4                      # NAT port on s4 (added last in run_experiment.py)
DATA_PORT, ACK_PORT, PROBE_PORT, REPORT_PORT = 9000, 9001, 9002, 9100
PRIO_BASE, PRIO_REROUTE = 10, 200

POLL = 0.5                           # seconds between polls
WINDOW = 8                           # polls in the loss window (4 s)
MIN_SAMPLES = 24                    # packets needed in the window before a loss estimate is used
THRESH = 0.20                        # degradation threshold
CONSEC = 4                           # consecutive bad polls (2 s) before rerouting
PROBE_WINDOW = 8                     # polls in the probe window (4 s)
LAG = 2                              # newest polls skipped: packets sent in them may not have arrived yet (1 s)
PROBE_OK = 0.8                       # healthy if at least this share of probes arrived
FAILBACK_AFTER = 6.0                 # seconds on the alternate path before failback is allowed

STATIC = os.environ.get('PATHGUARD_STATIC', '0') == '1'
EVENTS = os.environ.get('PATHGUARD_EVENTS', 'controller_events.jsonl')


class PathGuard(app_manager.OSKenApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.dps = {}
        self.active = PRIMARY
        self.switched_at = 0.0
        self.bad_polls = 0
        self.link_down = {PRIMARY: False, ALT: False}
        # data counters: cumulative totals per path, folded from flow stats and receiver reports
        self.flow_last = {PRIMARY: 0, ALT: 0}
        self.sent = {PRIMARY: 0, ALT: 0}
        self.rx = {PRIMARY: 0, ALT: 0}
        self.prev = {'sent': {PRIMARY: 0, ALT: 0}, 'rx': {PRIMARY: 0, ALT: 0},
                     'psent': {PRIMARY: 0, ALT: 0}, 'prx': {PRIMARY: 0, ALT: 0}}
        self.probe_sent = {PRIMARY: 0, ALT: 0}
        self.probe_rx = {PRIMARY: 0, ALT: 0}
        self.hist = {PRIMARY: [], ALT: []}          # per poll: (sent, rx)
        self.phist = {PRIMARY: [], ALT: []}         # per poll: (probes sent, probes received)
        self.events_fh = open(EVENTS, 'a', buffering=1)
        self.event('start', static=STATIC)
        hub.spawn(self.report_listener)
        hub.spawn(self.monitor_loop)

    # ----------------------------------------------------------- logging
    def event(self, kind, **kw):
        rec = dict(t=round(time.time(), 6), kind=kind, active=self.active, **kw)
        self.events_fh.write(json.dumps(rec) + '\n')
        self.logger.info('%s %s', kind, kw)

    # ----------------------------------------------------------- switch setup
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def on_features(self, ev):
        dp = ev.msg.datapath
        self.dps[dp.id] = dp
        p = dp.ofproto_parser
        if dp.id == S1:
            self.add_flow(dp, PRIO_BASE,
                          dict(eth_type=0x0800, ipv4_dst=H2_IP, ip_proto=17, udp_dst=DATA_PORT),
                          [p.OFPActionSetField(ip_dscp=DSCP[PRIMARY]), p.OFPActionOutput(OUT[PRIMARY])])
            self.add_flow(dp, 5, dict(eth_type=0x0800, ipv4_dst=H2_IP), [p.OFPActionOutput(OUT[PRIMARY])])
            self.add_flow(dp, 5, dict(eth_type=0x0800, ipv4_dst=H1_IP), [p.OFPActionOutput(1)])
        elif dp.id in (S2, S3):
            self.add_flow(dp, 5, dict(eth_type=0x0800, ipv4_dst=H2_IP), [p.OFPActionOutput(2)])
            self.add_flow(dp, 5, dict(eth_type=0x0800, ipv4_dst=H1_IP), [p.OFPActionOutput(1)])
        elif dp.id == S4:
            self.add_flow(dp, 5, dict(eth_type=0x0800, ipv4_dst=H2_IP), [p.OFPActionOutput(3)])
            self.add_flow(dp, 5, dict(eth_type=0x0800, ipv4_dst=H1_IP), [p.OFPActionOutput(1)])
            self.add_flow(dp, 5, dict(eth_type=0x0800, ipv4_dst=CTL_IP), [p.OFPActionOutput(S4_CTL_PORT)])
        self.event('switch_up', dpid=dp.id)

    def add_flow(self, dp, prio, match_kw, actions, command=None):
        p, ofp = dp.ofproto_parser, dp.ofproto
        match = p.OFPMatch(**match_kw)
        if command is None:
            inst = [p.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
            msg = p.OFPFlowMod(datapath=dp, priority=prio, match=match, instructions=inst,
                               command=ofp.OFPFC_ADD)
        else:
            msg = p.OFPFlowMod(datapath=dp, priority=prio, match=match, command=command,
                               out_port=ofp.OFPP_ANY, out_group=ofp.OFPG_ANY)
        dp.send_msg(msg)

    # ----------------------------------------------------------- counters
    @set_ev_cls(ofp_event.EventOFPFlowStatsReply, MAIN_DISPATCHER)
    def on_flow_stats(self, ev):
        dp = ev.msg.datapath
        if dp.id != S1:
            return
        counts = {PRIMARY: 0, ALT: 0}
        for st in ev.msg.body:
            if st.match.get('ipv4_dst') != H2_IP:
                continue
            if st.priority == PRIO_BASE:
                counts[PRIMARY] = st.packet_count
            elif st.priority == PRIO_REROUTE:
                counts[ALT] = st.packet_count
        for path in (PRIMARY, ALT):
            c, last = counts[path], self.flow_last[path]
            # a counter that went down means the flow was deleted and re-added
            self.sent[path] += c - last if c >= last else c
            self.flow_last[path] = c

    def report_listener(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(('0.0.0.0', REPORT_PORT))
        sock.settimeout(0.5)
        while True:
            try:
                data, _ = sock.recvfrom(4096)
            except socket.timeout:
                hub.sleep(0)
                continue
            try:
                rep = json.loads(data.decode())
            except ValueError:
                continue
            for path, key in ((PRIMARY, str(DSCP[PRIMARY])), (ALT, str(DSCP[ALT]))):
                self.rx[path] = int(rep['rx'].get(key, 0))
                self.probe_rx[path] = int(rep['probe'].get(key, 0))

    # ----------------------------------------------------------- probes
    def send_probe(self, path):
        dp = self.dps.get(S1)
        if dp is None:
            return
        p, ofp = dp.ofproto_parser, dp.ofproto
        pkt = packet.Packet()
        pkt.add_protocol(ethernet.ethernet(dst=H2_MAC, src=CTL_MAC, ethertype=0x0800))
        pkt.add_protocol(ipv4.ipv4(src=CTL_IP, dst=H2_IP, proto=17))
        pkt.add_protocol(udp.udp(src_port=REPORT_PORT, dst_port=PROBE_PORT))
        pkt.add_protocol(b'probe')
        pkt.serialize()
        actions = [p.OFPActionSetField(ip_dscp=DSCP[path]), p.OFPActionOutput(OUT[path])]
        dp.send_msg(p.OFPPacketOut(datapath=dp, buffer_id=ofp.OFP_NO_BUFFER,
                                   in_port=ofp.OFPP_CONTROLLER, actions=actions, data=pkt.data))
        self.probe_sent[path] += 1

    # ----------------------------------------------------------- monitor loop
    def monitor_loop(self):
        while True:
            hub.sleep(POLL)
            self.poll()

    def poll(self):
        dp = self.dps.get(S1)
        if dp is not None:
            p = dp.ofproto_parser
            dp.send_msg(p.OFPFlowStatsRequest(datapath=dp, table_id=0, match=p.OFPMatch()))
            self.send_probe(PRIMARY)
            self.send_probe(ALT)
        for path in (PRIMARY, ALT):
            ds = self.sent[path] - self.prev['sent'][path]
            dr = self.rx[path] - self.prev['rx'][path]
            dps_ = self.probe_sent[path] - self.prev['psent'][path]
            dpr = self.probe_rx[path] - self.prev['prx'][path]
            self.prev['sent'][path] = self.sent[path]
            self.prev['rx'][path] = self.rx[path]
            self.prev['psent'][path] = self.probe_sent[path]
            self.prev['prx'][path] = self.probe_rx[path]
            self.hist[path] = (self.hist[path] + [(ds, dr)])[-(WINDOW + LAG):]
            self.phist[path] = (self.phist[path] + [(dps_, dpr)])[-(PROBE_WINDOW + LAG):]
        self.decide()

    @staticmethod
    def settled(hist, n):
        """The n polls that ended LAG polls ago, or None if not enough history yet."""
        if len(hist) < n + LAG:
            return None
        return hist[len(hist) - n - LAG:len(hist) - LAG]

    def loss(self, path):
        w = self.settled(self.hist[path], WINDOW)
        if w is None:
            return None
        sent = sum(s for s, _ in w)
        rx = sum(r for _, r in w)
        if sent < MIN_SAMPLES:
            return None
        return max(0.0, 1.0 - rx / float(sent))

    def healthy(self, path):
        if self.link_down[path]:
            return False
        w = self.settled(self.phist[path], PROBE_WINDOW)
        if w is None:
            return True                  # not enough probes yet: assume healthy
        sent = sum(s for s, _ in w)
        rx = sum(r for _, r in w)
        if sent < 4:
            return True
        return rx >= PROBE_OK * sent

    def other(self, path):
        return ALT if path == PRIMARY else PRIMARY

    def decide(self):
        cur = self.active
        l_cur = self.loss(cur)
        l_alt = self.loss(self.other(cur))
        self.event('poll', loss_active=l_cur, loss_other=l_alt,
                   probes_ok={k: self.healthy(k) for k in (PRIMARY, ALT)},
                   counters={k: [self.sent[k], self.rx[k]] for k in (PRIMARY, ALT)})
        if STATIC:
            return
        bad = l_cur is not None and l_cur > THRESH
        self.bad_polls = self.bad_polls + 1 if bad else 0
        if self.bad_polls == CONSEC:
            self.event('degraded', path=cur, loss=l_cur)
        if self.bad_polls >= CONSEC:
            if self.healthy(self.other(cur)):
                self.set_path(self.other(cur), 'loss')
            else:
                self.event('no_alternate', path=cur)
                self.bad_polls = 0
        elif (cur == ALT and time.time() - self.switched_at >= FAILBACK_AFTER
              and self.settled(self.phist[PRIMARY], PROBE_WINDOW) is not None and self.healthy(PRIMARY)):
            self.set_path(PRIMARY, 'failback')

    # ----------------------------------------------------------- link events
    @set_ev_cls(ofp_event.EventOFPPortStatus, MAIN_DISPATCHER)
    def on_port_status(self, ev):
        dp, desc = ev.msg.datapath, ev.msg.desc
        if dp.id != S1 or desc.port_no not in (OUT[PRIMARY], OUT[ALT]):
            return
        path = PRIMARY if desc.port_no == OUT[PRIMARY] else ALT
        down = bool(desc.state & ofproto_v1_3.OFPPS_LINK_DOWN)
        self.link_down[path] = down
        self.event('port_status', path=path, down=down)
        if down and path == self.active and not STATIC:
            self.event('degraded', path=path, loss=None, reason='port-status')
            if self.healthy(self.other(path)):
                self.set_path(self.other(path), 'port-status')
            else:
                self.event('no_alternate', path=path)

    # ----------------------------------------------------------- reroute
    def set_path(self, path, reason):
        if path == self.active:
            return
        old = self.active
        self.active = path
        self.switched_at = time.time()
        self.bad_polls = 0
        self.hist[path] = []            # start a fresh loss window on the new path
        self.event('reroute' if reason != 'failback' else 'failback',
                   to=path, frm=old, reason=reason)
        match_s1 = dict(eth_type=0x0800, ipv4_dst=H2_IP, ip_proto=17, udp_dst=DATA_PORT)
        match_s4 = dict(eth_type=0x0800, ipv4_dst=H1_IP, ip_proto=17, udp_dst=ACK_PORT)
        s1, s4 = self.dps.get(S1), self.dps.get(S4)
        if s1 is not None:
            if path == ALT:
                p = s1.ofproto_parser
                self.add_flow(s1, PRIO_REROUTE, match_s1,
                              [p.OFPActionSetField(ip_dscp=DSCP[ALT]), p.OFPActionOutput(OUT[ALT])])
            else:
                self.add_flow(s1, PRIO_REROUTE, match_s1, None,
                              command=s1.ofproto.OFPFC_DELETE_STRICT)
        if s4 is not None:
            if path == ALT:
                p = s4.ofproto_parser
                self.add_flow(s4, PRIO_REROUTE, match_s4, [p.OFPActionOutput(2)])   # ACKs via s3
            else:
                self.add_flow(s4, PRIO_REROUTE, match_s4, None,
                              command=s4.ofproto.OFPFC_DELETE_STRICT)
        self.event('flowmod_sent', to=path, switches=[S1, S4])
