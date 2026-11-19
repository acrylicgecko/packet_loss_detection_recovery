#!/usr/bin/env python3
"""receiver.py - runs on h2. Accepts the session (HELLO -> WELCOME), tracks sequence
numbers, detects gaps, sends ACK/NACK back to h1, counts arrivals per path (from the
IP DSCP mark set by the switch) and reports those counts to the controller.

Path marks: DSCP 10 = primary path, DSCP 20 = alternate path (set on s1).
--events writes one JSON line per protocol event (welcome, rx, gap, ack, nack).
"""
import argparse
import json
import select
import socket
import struct
import time

DATA, ACK, NACK, PROBE, HELLO, WELCOME = 0, 1, 2, 3, 4, 5
DATA_HDR = struct.Struct('!BIddB')
PROBE_HDR = struct.Struct('!BI')
ACK_HDR = struct.Struct('!BIH')
NACK_HDR = struct.Struct('!BH')
WELCOME_PKT = struct.pack('!B', WELCOME)
IP_RECVTOS = 13          # Linux: ask the kernel to pass the IP TOS byte with each datagram
DSCP_PATHS = (10, 20)    # 10 = primary, 20 = alternate


def recv_with_dscp(sock):
    data, anc, _flags, addr = sock.recvmsg(2048, 256)
    dscp = None
    for level, typ, val in anc:
        if level == socket.IPPROTO_IP and typ == socket.IP_TOS:
            dscp = val[0] >> 2
    return data, dscp, addr


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--bind', default='0.0.0.0')
    ap.add_argument('--data-port', type=int, default=9000)
    ap.add_argument('--probe-port', type=int, default=9002)
    ap.add_argument('--sender', default='10.0.0.1')
    ap.add_argument('--ack-port', type=int, default=9001)
    ap.add_argument('--ctl', default='10.0.0.254', help='controller address (reachable from h2)')
    ap.add_argument('--report-port', type=int, default=9100)
    ap.add_argument('--wait', type=float, default=0.25, help='reorder wait before a gap is NACKed (s)')
    ap.add_argument('--nack-gap', type=float, default=1.8, help='re-NACK interval for a missing seq (s)')
    ap.add_argument('--ack-every', type=float, default=0.5)
    ap.add_argument('--report-every', type=float, default=0.5)
    ap.add_argument('--duration', type=float, default=70.0)
    ap.add_argument('--status-every', type=float, default=0.0,
                    help='print a one-line status every N seconds (0 = off, used by live_demo.py)')
    ap.add_argument('--events', default=None, help='append protocol events to this JSONL file')
    ap.add_argument('--log', default='receiver_log.json')
    a = ap.parse_args()

    ev = open(a.events, 'a', buffering=1) if a.events else None

    def event(kind, **kw):
        if ev:
            ev.write(json.dumps(dict(t=round(time.time(), 6), k=kind, **kw)) + '\n')

    data_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    data_sock.setsockopt(socket.IPPROTO_IP, IP_RECVTOS, 1)
    data_sock.bind((a.bind, a.data_port))
    probe_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe_sock.setsockopt(socket.IPPROTO_IP, IP_RECVTOS, 1)
    probe_sock.bind((a.bind, a.probe_port))
    out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)   # ACK, NACK, WELCOME and REPORT go out from here
    event('listen', data='%s:%d' % (a.bind, a.data_port), probe='%s:%d' % (a.bind, a.probe_port))

    got = set()
    highest = cum = dups = delivered = 0
    gap_t, last_nack = {}, {}
    rx = {d: 0 for d in DSCP_PATHS}        # cumulative data arrivals per path
    prx = {d: 0 for d in DSCP_PATHS}       # cumulative probe arrivals per path
    arrivals = []                          # [t, seq, dscp, attempt, first_send, this_send]
    last_ack = last_rep = last_status = 0.0
    end = time.time() + a.duration

    while time.time() < end:
        ready, _, _ = select.select([data_sock, probe_sock], [], [], 0.05)
        for s in ready:
            pkt, dscp, addr = recv_with_dscp(s)
            now = time.time()
            if s is probe_sock:
                if len(pkt) >= PROBE_HDR.size and dscp in prx:
                    prx[dscp] += 1
                continue
            if pkt[:1] == bytes([HELLO]):
                out.sendto(WELCOME_PKT, (a.sender, a.ack_port))
                event('welcome', peer='%s:%d' % addr)
                continue
            if len(pkt) < DATA_HDR.size or pkt[0] != DATA:
                continue
            _, seq, first_t, this_t, attempt = DATA_HDR.unpack_from(pkt)
            if dscp in rx:
                rx[dscp] += 1
            arrivals.append([round(now, 6), seq, dscp, attempt, first_t, this_t])
            event('rx', seq=seq, att=attempt, dscp=dscp)
            if seq in got:
                dups += 1
                continue
            got.add(seq)
            delivered += 1
            if seq > highest:
                for x in range(highest + 1, seq):
                    gap_t.setdefault(x, now)       # a hole opens here
                    event('gap', seq=x)
                highest = seq
            while cum + 1 in got:
                cum += 1

        now = time.time()
        if highest and now - last_ack >= a.ack_every:
            sack = sorted((x for x in got if x > cum), reverse=True)[:24]
            out.sendto(ACK_HDR.pack(ACK, cum, len(sack)) +
                       struct.pack('!%dI' % len(sack), *sack), (a.sender, a.ack_port))
            event('ack', cum=cum, sack=len(sack))
            last_ack = now

        missing = [x for x in range(cum + 1, highest)
                   if x not in got
                   and now - gap_t.get(x, now) >= a.wait
                   and now - last_nack.get(x, -9.0) >= a.nack_gap][:16]
        if missing:
            for x in missing:
                last_nack[x] = now
            out.sendto(NACK_HDR.pack(NACK, len(missing)) +
                       struct.pack('!%dI' % len(missing), *missing), (a.sender, a.ack_port))
            event('nack', seqs=missing)

        if a.status_every and now - last_status >= a.status_every:
            last_status = now
            print('RX delivered=%d missing=%d dups=%d tos10=%d tos20=%d'
                  % (delivered, highest - delivered, dups, rx[10], rx[20]), flush=True)

        if now - last_rep >= a.report_every:
            body = json.dumps({'rx': {str(k): v for k, v in rx.items()},
                               'probe': {str(k): v for k, v in prx.items()},
                               'highest': highest, 'delivered': delivered}).encode()
            out.sendto(body, (a.ctl, a.report_port))
            last_rep = now

    with open(a.log, 'w') as f:
        json.dump({'delivered': delivered, 'dups': dups, 'highest': highest, 'cum': cum,
                   'rx': rx, 'probe_rx': prx, 'arrivals': arrivals}, f)
    print('receiver done delivered=%d dups=%d highest=%d' % (delivered, dups, highest), flush=True)


if __name__ == '__main__':
    main()
