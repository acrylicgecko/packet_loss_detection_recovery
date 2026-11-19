#!/usr/bin/env python3
"""summarize.py - averages metrics.json over repeats, per mode and fault.

    python3 summarize.py results
"""
import glob
import json
import os
import sys
from collections import defaultdict


def pct(v):
    return '–' if v is None else '%.1f%%' % (100 * v)


def sec(v):
    return '–' if v is None else '%.2f s' % v


def mean(vals):
    v = [x for x in vals if x is not None]
    return sum(v) / len(v) if v else None


def main():
    root = sys.argv[1] if len(sys.argv) > 1 else 'results'
    groups = defaultdict(list)
    for path in sorted(glob.glob(os.path.join(root, '*_r*', 'metrics.json'))):
        name = os.path.basename(os.path.dirname(path))
        key = name.rsplit('_r', 1)[0]           # e.g. sdn_meter3
        with open(path) as f:
            groups[key].append(json.load(f))
    cols = ['loss_before', 'loss_during', 'loss_after', 'detect_s', 'reroute_s', 'first_alt_s',
            'cleared_s', 'overhead', 'goodput_after']
    print('%-16s %-3s ' % ('run', 'n') + ' '.join('%-12s' % c for c in cols))
    for key in sorted(groups):
        rows = groups[key]
        vals = []
        for c in cols:
            v = mean([r.get(c) for r in rows])
            if c in ('loss_before', 'loss_during', 'loss_after', 'overhead'):
                vals.append(pct(v))
            elif c == 'goodput_after':
                vals.append('-' if v is None else '%.2f/s' % v)
            else:
                vals.append(sec(v))
        print('%-16s %-3d ' % (key, len(rows)) + ' '.join('%-12s' % x for x in vals))


if __name__ == '__main__':
    main()
