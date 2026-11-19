#!/usr/bin/env python3
"""live_metrics.py - computes the project's measurements from a session folder while
it runs (used by dashboard.py) or after it ends (python3 live_metrics.py <dir>).

Reads these files from the session folder:
  session.jsonl            start, phase, fault and clear marks written by live_demo.py
  sender_events.jsonl      tx, ack, nack, abandon, hello, welcome (sender)
  receiver_events.jsonl    rx, gap, ack, nack, welcome (receiver)
  controller_events.jsonl  poll, degraded, reroute, failback, port_status (controller)

Definitions (same as analyze.py, but computed from events so they are live):
  first-attempt loss in a window = share of datagrams first sent in the window whose
                                   first copy (attempt 0) never arrived
  before / during / after        = [start, fault), [fault, fault+15 s), [fault+15 s, now)
  detect                         = first 'degraded' after the fault
  reroute                        = first 'reroute' after the fault
  first on alternate             = first arrival marked DSCP 20 after the reroute
  backlog cleared                = time the last datagram first sent during the fault window
                                   was delivered (None while still outstanding)
"""
import json
import os
import sys

DURING = 15.0
SEQ_WINDOW = 48


def _read(path):
    out = []
    if not os.path.exists(path):
        return out
    with open(path) as f:
        for line in f:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue    # a line may be half-written while the process is running
    return out


def compute(d, now=None):
    session = _read(os.path.join(d, 'session.jsonl'))
    snd = _read(os.path.join(d, 'sender_events.jsonl'))
    rcv = _read(os.path.join(d, 'receiver_events.jsonl'))
    # the controller writes its event type as 'kind'; the sender and receiver use 'k'
    ctl = [dict(e, k=e.get('kind')) for e in _read(os.path.join(d, 'controller_events.jsonl'))]

    t0 = next((e['t'] for e in session if e['k'] == 'start'), None)
    if t0 is None:
        t0 = min([e['t'] for e in snd + rcv] or [0.0])
    now = now or max([e['t'] for e in snd + rcv + ctl + session] or [t0])
    faults = [e for e in session if e['k'] == 'fault']
    tf = faults[0]['t'] if faults else None
    phase = next((e['name'] for e in reversed(session) if e['k'] == 'phase'), '-')

    first_send, first_ok, delivered_at, tx_count, retx, abandon = {}, set(), {}, 0, 0, 0
    for e in snd:
        if e['k'] == 'tx':
            tx_count += 1
            if e['att'] == 0:
                first_send[e['seq']] = e['t']
            else:
                retx += 1
        elif e['k'] == 'abandon':
            abandon += 1
    arrivals = []
    for e in rcv:
        if e['k'] == 'rx':
            arrivals.append(e)
            if e['att'] == 0:
                first_ok.add(e['seq'])
            if e['seq'] not in delivered_at or e['t'] < delivered_at[e['seq']]:
                delivered_at[e['seq']] = e['t']
    acks = sum(1 for e in rcv if e['k'] == 'ack')
    nacks = sum(1 for e in rcv if e['k'] == 'nack')

    def loss(lo, hi):
        seqs = [s for s, t in first_send.items() if lo <= t < hi]
        if not seqs:
            return None
        return sum(1 for s in seqs if s not in first_ok) / float(len(seqs))

    m = {
        'phase': phase, 'now': now - t0, 'sent': len(first_send), 'delivered': len(delivered_at),
        'missing_now': len(first_send) - len(delivered_at), 'tx': tx_count, 'retx': retx,
        'abandoned': abandon, 'acks': acks, 'nacks': nacks,
        'fault': None, 'fault_kind': faults[-1].get('kind') if faults else None,
        'before': loss(t0, tf) if tf else loss(t0, now + 1),
        'during': None, 'after': None, 'detect': None, 'reroute': None, 'first_alt': None,
        'cleared': None, 'backlog': 0,
        # the 'during' window is final once it has closed plus a 3 s allowance for in-flight copies
        'final': bool(tf) and now >= tf + DURING + 3,
    }
    if tf:
        m['fault'] = tf - t0
        m['during'] = loss(tf, tf + DURING) if now >= tf else None
        m['after'] = loss(tf + DURING, now + 1) if now >= tf + DURING else None
        det = [e['t'] for e in ctl if e['k'] == 'degraded' and e['t'] >= tf]
        rer = [e['t'] for e in ctl if e['k'] == 'reroute' and e['t'] >= tf]
        if det:
            m['detect'] = min(det) - tf
        if rer:
            m['reroute'] = rer[0] - tf
            alt = [e['t'] for e in arrivals if e['dscp'] == 20 and e['t'] >= rer[0]]
            if alt:
                m['first_alt'] = min(alt) - tf
        backlog = [s for s, t in first_send.items() if tf <= t < tf + DURING and s not in first_ok]
        m['backlog'] = len(backlog)
        if backlog and all(s in delivered_at for s in backlog):
            m['cleared'] = max(delivered_at[s] for s in backlog) - tf
        elif not backlog and now >= tf + DURING:
            m['cleared'] = 0.0

    polls = [e for e in ctl if e['k'] == 'poll']
    if polls:
        p = polls[-1]
        m['active'] = p['active']
        m['probes_ok'] = p['probes_ok']
        m['loss_active'] = p['loss_active']
    else:
        m['active'] = 'P'
        m['probes_ok'] = {'P': None, 'A': None}
        m['loss_active'] = None

    # sequence window: the last SEQ_WINDOW sequence numbers, as the receiver sees them
    top = max(first_send) if first_send else 0
    window = []
    for s in range(max(1, top - SEQ_WINDOW + 1), top + 1):
        if s not in first_send:
            state = '.'
        elif s in first_ok:
            state = 'ok'
        elif s in delivered_at:
            state = 'retx'
        else:
            state = 'miss'
        window.append((s, state))
    m['window'] = window

    # merged event log, newest last
    log = []
    for e in session:
        if e['k'] in ('phase', 'fault', 'clear'):
            log.append((e['t'], e.get('text', e['k'])))
    for e in ctl:
        if e['k'] in ('degraded', 'reroute', 'failback', 'no_alternate'):
            log.append((e['t'], 'controller: ' + e['k'] + (' %s -> %s' % (e['frm'], e['to']) if 'to' in e else '')
                        + (' (%s)' % e['reason'] if e.get('reason') else '')))
    for e in rcv:
        if e['k'] == 'nack':
            log.append((e['t'], 'receiver NACK %s' % e['seqs'][:6]))
    for e in snd:
        if e['k'] == 'nack':
            log.append((e['t'], 'sender got NACK, retransmitting %s' % e['seqs'][:6]))
        elif e['k'] == 'abandon':
            log.append((e['t'], 'sender ABANDONED seq %d' % e['seq']))
    log.sort()
    m['log'] = [(round(t - t0, 2), text) for t, text in log[-14:]]
    m['t0'] = t0
    return m


def fmt_pct(v):
    return '–' if v is None else '%.1f%%' % (100 * v)


def fmt_s(v):
    return '–' if v is None else '%.2f s' % v


def main():
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    m = compute(sys.argv[1])
    print('phase            %s' % m['phase'])
    print('sent / delivered %d / %d   missing now %d' % (m['sent'], m['delivered'], m['missing_now']))
    print('transmissions    %d   retransmissions %d   abandoned %d' % (m['tx'], m['retx'], m['abandoned']))
    print('loss before      %s' % fmt_pct(m['before']))
    print('loss during 15s  %s' % fmt_pct(m['during']))
    print('loss after 15s   %s' % fmt_pct(m['after']))
    print('fault at         %s   detect %s   reroute %s   first on alternate %s   backlog cleared %s'
          % (fmt_s(m['fault']), fmt_s(m['detect']), fmt_s(m['reroute']), fmt_s(m['first_alt']),
             fmt_s(m['cleared'])))


if __name__ == '__main__':
    main()
