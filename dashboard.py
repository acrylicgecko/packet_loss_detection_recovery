#!/usr/bin/env python3
"""dashboard.py - live terminal view of a running session. Run it in a second terminal:

    python3 dashboard.py results/live

It only reads the session folder, so it can be started and stopped at any time.
Ctrl-C leaves the session running.
"""
import argparse
import os
import time

from live_metrics import SEQ_WINDOW, compute, fmt_pct, fmt_s

GREEN, RED, AMBER, PURPLE, GREY, BOLD, RESET = '\033[32m', '\033[31m', '\033[33m', '\033[35m', '\033[90m', '\033[1m', '\033[0m'
CLEAR = '\033[H\033[2J'
SEQ_STYLE = {'ok': (GREEN, 'G'), 'retx': (PURPLE, 'P'), 'miss': (RED, 'R'), '.': (GREY, '.')}


def path_line(name, active, probes_ok, down):
    if down:
        state, colour = 'DOWN', RED
    elif probes_ok is False:
        state, colour = 'unhealthy', AMBER
    else:
        state, colour = 'healthy', GREEN
    mark = '<- traffic here' if active else ''
    return '%s%s%s  %s%-9s%s %s' % (BOLD if active else '', name, RESET, colour, state, RESET, mark)


def render(d):
    m = compute(d)
    lines = []
    add = lines.append
    add('%sPACKET LOSS RECOVERY - live%s   session time %s   phase: %s%s%s'
        % (BOLD, RESET, fmt_s(m['now']), BOLD, m['phase'], RESET))
    add('')
    primary_down = m['fault_kind'] == 'break' and m['fault'] is not None
    alt_name = 'alternate'
    add('  h1 ─ s1 ═══ s2 ═══ s4 ─ h2   %s' % path_line('primary (s1-s2-s4)', m['active'] == 'P',
                                                     m['probes_ok'].get('P'), primary_down))
    add('        s1 ═══ s3 ═══ s4        %s' % path_line('%s (s1-s3-s4)' % alt_name, m['active'] == 'A',
                                                     m['probes_ok'].get('A'), False))
    add('')
    lo = m['loss_active']
    add('  controller   %s  active=%s   loss on active path (4 s window): %s'
        % ('monitoring', m['active'], '–' if lo is None else '%.0f%%' % (100 * lo)))
    add('  fault        %s' % ('none' if m['fault'] is None else
                              '%s at %s into the session' % (m['fault_kind'], fmt_s(m['fault']))))
    add('')
    add('  %sKPI%s   sent %-5d delivered %-5d missing now %-4d transmissions %-5d retransmissions %-5d abandoned %d'
        % (BOLD, RESET, m['sent'], m['delivered'], m['missing_now'], m['tx'], m['retx'], m['abandoned']))
    add('        ACKs %d   NACKs %d' % (m['acks'], m['nacks']))
    add('')
    live = '' if m['final'] or m['fault'] is None else ' (live)'
    add('  %sFIRST-ATTEMPT LOSS%s   before fault %s   during 15 s %s%s   after 15 s %s'
        % (BOLD, RESET, fmt_pct(m['before']), fmt_pct(m['during']), live, fmt_pct(m['after'])))
    add('  %sRECOVERY CLOCK%s      detect %s   reroute %s   first on alternate %s   backlog cleared %s'
        % (BOLD, RESET, fmt_s(m['detect']), fmt_s(m['reroute']), fmt_s(m['first_alt']), fmt_s(m['cleared'])))
    add('')
    cells = []
    for s, state in m['window']:
        colour, ch = SEQ_STYLE[state]
        cells.append('%s%s%s' % (colour, ch, RESET))
    add('  %sSEQUENCE WINDOW%s (last %d sent)   %s G arrived first time   %s P retransmitted   %s R missing   . not sent'
        % (BOLD, RESET, SEQ_WINDOW, GREEN + 'G' + RESET, PURPLE + 'P' + RESET, RED + 'R' + RESET))
    for i in range(0, len(cells), 24):
        add('    ' + ' '.join(cells[i:i + 24]))
    add('')
    add('  %sEVENT LOG%s (newest last)' % (BOLD, RESET))
    if not m['log']:
        add('    (no events yet)')
    for t, text in m['log']:
        add('    %s[%7.2fs]%s %s' % (GREY, t, RESET, text))
    return '\n'.join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('dir', nargs='?', default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                           'results', 'live'))
    ap.add_argument('--interval', type=float, default=0.5)
    a = ap.parse_args()
    try:
        while True:
            print(CLEAR + render(a.dir), flush=True)
            time.sleep(a.interval)
    except KeyboardInterrupt:
        print(RESET + '\nstopped viewing (the session keeps running)')


if __name__ == '__main__':
    main()
