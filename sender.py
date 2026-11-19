#!/usr/bin/env python3
"""sender.py - runs on h1. Opens a session with the receiver, then streams numbered
UDP datagrams to h2 and recovers losses with NACK-triggered and timeout-triggered
retransmission.

Wire format (network byte order):
  DATA    !B I d d B   type=0, seq, first-send time, this-send time, attempt (0 = first)
  ACK     !B I H       type=1, cumulative seq, count, then count x I (SACK seqs)
  NACK    !B H         type=2, count, then count x I (missing seqs)
  HELLO   !B           type=4, session request (sender -> receiver, repeated until WELCOME)
  WELCOME !B           type=5, session accepted (receiver -> sender)

UDP has no connection setup, so HELLO/WELCOME is the application-level handshake: the
sender does not start data until the receiver has answered.

--events writes one JSON line per protocol event (hello, welcome, tx, ack, nack, abandon).
live_demo.py and dashboard.py read that file.
"""
import argparse
import json
import socket
import struct
import time

DATA, ACK, NACK, HELLO, WELCOME = 0, 1, 2, 4, 5
DATA_HDR = struct.Struct('!BIddB')
ACK_HDR = struct.Struct('!BIH')
NACK_HDR = struct.Struct('!BH')
HELLO_PKT = struct.pack('!B', HELLO)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--bind', default='10.0.0.1', help='local address to bind (h1)')
    ap.add_argument('--dest', default='10.0.0.2', help='receiver address (h2)')
    ap.add_argument('--port', type=int, default=9000, help='receiver data port')
    ap.add_argument('--ack-port', type=int, default=9001, help='local port for ACK/NACK/WELCOME')
    ap.add_argument('--rate', type=float, default=8.0, help='new datagrams per second')
    ap.add_argument('--duration', type=float, default=60.0, help='seconds of new traffic')
    ap.add_argument('--payload', type=int, default=64, help='datagram size in bytes')
    ap.add_argument('--rto', type=float, default=3.0, help='base retransmission timeout (s)')
    ap.add_argument('--max-retry', type=int, default=5, help='retransmissions before abandoning')
    ap.add_argument('--drain', type=float, default=10.0, help='seconds to wait for ACKs after sending stops')
    ap.add_argument('--hello-timeout', type=float, default=15.0, help='give up the handshake after N seconds')
    ap.add_argument('--status-every', type=float, default=0.0,
                    help='print a one-line status every N seconds (0 = off, used by live_demo.py)')
    ap.add_argument('--events', default=None, help='append protocol events to this JSONL file')
    ap.add_argument('--log', default='sender_log.json')
    a = ap.parse_args()

    ev = open(a.events, 'a', buffering=1) if a.events else None

    def event(kind, **kw):
        if ev:
            ev.write(json.dumps(dict(t=round(time.time(), 6), k=kind, **kw)) + '\n')

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((a.bind, a.ack_port))
    dest = (a.dest, a.port)

    # ---- 1. session setup: HELLO until the receiver answers WELCOME
    sock.settimeout(0.5)
    hello_end = time.time() + a.hello_timeout
    connected = False
    while time.time() < hello_end and not connected:
        sock.sendto(HELLO_PKT, dest)
        event('hello', dst='%s:%d' % dest)
        try:
            data, _ = sock.recvfrom(64)
        except socket.timeout:
            continue
        if data[:1] == bytes([WELCOME]):
            connected = True
            event('welcome', peer='%s:%d' % dest)
    if not connected:
        raise SystemExit('no WELCOME from %s:%d (is receiver.py running?)' % dest)
    print('session established with %s:%d' % dest, flush=True)

    # ---- 2. data transfer
    sock.settimeout(0.02)
    pad = b'x' * max(0, a.payload - DATA_HDR.size)
    unacked = {}       # seq -> {'tx': last send time, 'retries': n}
    first_send = {}    # seq -> time of first transmission
    stats = {'sent': 0, 'retx': 0, 'ack_rx': 0, 'nack_rx': 0, 'abandoned': 0}

    def transmit(seq, attempt, now):
        first = first_send.get(seq, now)
        sock.sendto(DATA_HDR.pack(DATA, seq, first, now, attempt) + pad, dest)
        event('tx', seq=seq, att=attempt)

    def retransmit(seq, now):
        e = unacked.get(seq)
        if e is None:
            return
        if e['retries'] >= a.max_retry:
            del unacked[seq]
            stats['abandoned'] += 1
            event('abandon', seq=seq)
            return
        e['retries'] += 1
        e['tx'] = now
        stats['retx'] += 1
        transmit(seq, e['retries'], now)

    start = time.time()
    stop_new = start + a.duration
    hard_stop = stop_new + a.drain
    interval = 1.0 / a.rate
    next_send = start
    seq = 0
    last_status = start

    while True:
        now = time.time()
        if now < stop_new and now >= next_send:
            seq += 1
            unacked[seq] = {'tx': now, 'retries': 0}
            first_send[seq] = now
            transmit(seq, 0, now)
            stats['sent'] += 1
            next_send += interval
        if (now >= stop_new and not unacked) or now >= hard_stop:
            break

        # timer-driven retransmission with exponential backoff, capped at 8 s
        for s in list(unacked):
            e = unacked.get(s)
            if e and now - e['tx'] > min(8.0, a.rto * 1.4 ** e['retries']):
                retransmit(s, now)

        if a.status_every and now - last_status >= a.status_every:
            last_status = now
            print('TX sent=%d retx=%d abandoned=%d unacked=%d'
                  % (stats['sent'], stats['retx'], stats['abandoned'], len(unacked)), flush=True)

        try:
            data, _ = sock.recvfrom(2048)
        except socket.timeout:
            continue
        if not data:
            continue
        try:
            if data[0] == ACK:
                _, cum, n = ACK_HDR.unpack_from(data)
                sacks = struct.unpack_from('!%dI' % n, data, ACK_HDR.size)
                stats['ack_rx'] += 1
                event('ack', cum=cum, sack=len(sacks))
                for s in [k for k in unacked if k <= cum]:
                    del unacked[s]
                for s in sacks:
                    unacked.pop(s, None)
            elif data[0] == NACK:
                _, n = NACK_HDR.unpack_from(data)
                stats['nack_rx'] += 1
                missing = struct.unpack_from('!%dI' % n, data, NACK_HDR.size)
                event('nack', seqs=list(missing))
                for s in missing:
                    e = unacked.get(s)
                    # ignore a NACK for a packet resent in the last 0.5 s (one burst, one resend)
                    if e and now - e['tx'] >= 0.5:
                        retransmit(s, now)
        except struct.error:
            continue  # malformed datagram: ignore

    sock.close()
    stats['still_unacked'] = len(unacked)
    stats['duration_s'] = round(time.time() - start, 3)
    with open(a.log, 'w') as f:
        json.dump({'stats': stats, 'first_send': {str(k): v for k, v in first_send.items()},
                   'stop_new_at': stop_new}, f)
    event('done', stats=stats)
    print('sender done', stats, flush=True)


if __name__ == '__main__':
    main()
