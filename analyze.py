#!/usr/bin/env python3
"""analyze.py - computes loss and recovery metrics from one run directory.

    python3 analyze.py results/sdn_loss60_r1

Reads sender_log.json, receiver_log.json, fault.json, run_info.json and
controller_events.jsonl (events written during this run only), and writes
metrics.json next to them.

Definitions (times in seconds after the fault unless stated):
  first-attempt loss in a window  = share of packets first sent in the window
                                    whose first copy never arrived (attempt 0)
  loss before fault               = window [run start, fault)
  loss during fault               = window [fault, fault + 15)
  loss after 15 s                 = window [fault + 15, end of sending)
  detect                          = first 'degraded' event after the fault
  reroute                         = first 'reroute' event after the fault
  first packet on alternate       = first arrival with DSCP 20 after the reroute
  backlog cleared                 = time at which the last packet that was lost on
                                    its first copy (sent during the fault window) finally
                                    arrived; 0 if nothing was lost
  never delivered                 = packets abandoned by the sender (never arrived)
"""
import json
import os
import sys

DURING = 15.0


def load(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path) as f:
        return json.load(f)


def window_loss(first_send, first_ok, lo, hi):
    seqs = [s for s, t in first_send.items() if lo <= t < hi]
    if not seqs:
        return None
    lost = sum(1 for s in seqs if s not in first_ok)
    return lost / float(len(seqs))


def main():
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    run = os.path.abspath(sys.argv[1])
    snd = load(os.path.join(run, 'sender_log.json'))
    rcv = load(os.path.join(run, 'receiver_log.json'))
    fault = load(os.path.join(run, 'fault.json'))
    info = load(os.path.join(run, 'run_info.json'), {})
    if snd is None or rcv is None:
        raise SystemExit('missing sender_log.json or receiver_log.json in %s' % run)

    first_send = {int(k): v for k, v in snd['first_send'].items()}
    stop_new = snd['stop_new_at']
    start = info.get('start', min(first_send.values()))
    arrivals = rcv['arrivals']                         # [t, seq, dscp, attempt, first_send, this_send]
    first_ok = {a[1] for a in arrivals if a[3] == 0}
    delivered_at = {}
    for t, seq, *_ in arrivals:
        if seq not in delivered_at or t < delivered_at[seq]:
            delivered_at[seq] = t

    events = []
    ev_path = os.path.join(run, 'controller_events.jsonl')
    if os.path.exists(ev_path):
        with open(ev_path) as f:
            for line in f:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if start - 1 <= e['t'] <= stop_new + 5:
                    events.append(e)

    m = {
        'sent': snd['stats']['sent'], 'retransmissions': snd['stats']['retx'],
        'abandoned': snd['stats']['abandoned'], 'delivered': len(delivered_at),
        'duplicates': rcv['dups'],
        'overhead': snd['stats']['retx'] / float(max(1, snd['stats']['sent'])),
        'avg_latency': None,
    }
    lat = [delivered_at[s] - first_send[s] for s in delivered_at if s in first_send]
    if lat:
        m['avg_latency'] = sum(lat) / len(lat)

    if fault and fault.get('fault') != 'none':
        tf = fault['t']
        m['fault'] = fault['fault']
        m['loss_before'] = window_loss(first_send, first_ok, start, tf)
        m['loss_during'] = window_loss(first_send, first_ok, tf, tf + DURING)
        m['loss_after'] = window_loss(first_send, first_ok, tf + DURING, stop_new)
        det = [e['t'] for e in events if e['kind'] == 'degraded' and e['t'] >= tf]
        rer = [e for e in events if e['kind'] == 'reroute' and e['t'] >= tf]
        m['detect_s'] = (min(det) - tf) if det else None
        if rer:
            tr = rer[0]['t']
            m['reroute_s'] = tr - tf
            alt = [a[0] for a in arrivals if a[2] == 20 and a[0] >= tr]
            m['first_alt_s'] = (min(alt) - tf) if alt else None
        else:
            m['reroute_s'] = None
            m['first_alt_s'] = None
        # backlog = packets first sent in the fault window whose first copy was lost
        backlog = [s for s, t in first_send.items() if tf <= t < tf + DURING and s not in first_ok]
        if not backlog:
            m['cleared_s'] = 0.0
        elif all(s in delivered_at for s in backlog):
            m['cleared_s'] = max(delivered_at[s] for s in backlog) - tf
        else:
            m['cleared_s'] = None
        post_arr = {s for s, t in delivered_at.items() if t >= tf + DURING}
        dur = max(1e-9, stop_new - (tf + DURING))
        m['goodput_after'] = len(post_arr) / dur
    else:
        m['fault'] = 'none'
        m['loss_overall'] = window_loss(first_send, first_ok, start, stop_new + 1)

    with open(os.path.join(run, 'metrics.json'), 'w') as f:
        json.dump(m, f, indent=1)
    for k, v in m.items():
        print('%-18s %s' % (k, ('%.4f' % v) if isinstance(v, float) else v))


if __name__ == '__main__':
    main()
