#!/usr/bin/env python3
"""Stage 3 (local, free): shoot-level exclusions + technical cut -> candidates.csv.

Usage: triage.py --work DIR [--blur-pct 15] [--keep-dupes]

Drops, with a reason recorded in triage.csv:
  excluded  path or shoot matches config exclude_patterns (substring, or /regex/)
  small     long side < config min_long_side (screenshots, web exports)
  dupe      not the best frame of its near-duplicate cluster
  exposure  badly clipped or very dark/bright with no recoverable range (RAW pairs are kept)
  blurry    peak sharpness (sharpest tiles) below half the floor, or in the bottom --blur-pct of its
            shoot AND below the floor
            (relative-to-shoot keeps intentionally soft styles from being wiped out)
  error     could not be read
Everything else is a candidate for the model stages.
"""
import argparse, collections, os, re, sys
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import add_work_arg, fnum, load_config, read_csv, work_dir, write_csv


def matcher(patterns):
    rx = []
    for p in patterns or []:
        if len(p) > 2 and p.startswith('/') and p.endswith('/'):
            rx.append(re.compile(p[1:-1], re.I))
        else:
            rx.append(re.compile(re.escape(p), re.I))
    return lambda s: next((r.pattern for r in rx if r.search(s)), None)


def main():
    ap = add_work_arg(argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter))
    ap.add_argument('--blur-pct', type=float, default=15)
    ap.add_argument('--blur-floor', type=float, default=80, help='floor for peak tile sharpness (Laplacian variance at 1024px)')
    ap.add_argument('--keep-dupes', action='store_true')
    A = ap.parse_args()
    W = work_dir(A.work)
    cfg = load_config(W)
    inv = read_csv(os.path.join(W, 'inventory.csv'))
    met = {m['id']: m for m in read_csv(os.path.join(W, 'metrics.csv'))}
    dup = {d['id']: d for d in read_csv(os.path.join(W, 'dupes.csv'))}
    if not met:
        sys.exit('no metrics.csv; run metrics.py first')
    excl = matcher(cfg.get('exclude_patterns'))

    sharp_by_shoot = collections.defaultdict(list)
    for r in inv:
        m = met.get(r['id'])
        if m and m.get('ok') == '1':
            sharp_by_shoot[r['shoot']].append(fnum(m['sharp_peak']))
    cut = {s: float(np.percentile(v, A.blur_pct)) if len(v) >= 8 else 0 for s, v in sharp_by_shoot.items()}

    out, reasons = [], collections.Counter()
    for r in inv:
        m = met.get(r['id'], {})
        why = ''
        hit = excl(f"{r['path']} {r['shoot']}")
        if hit:
            why = f'excluded:{hit}'
        elif m.get('ok') != '1':
            why = 'error'
        elif fnum(m['long_side']) < cfg['min_long_side']:
            why = 'small'
        elif not A.keep_dupes and r['id'] in dup and dup[r['id']]['is_best'] != '1':
            why = 'dupe'
        elif not r.get('raw_path') and (fnum(m['clip_hi_pct']) > 12 or fnum(m['clip_lo_pct']) > 35
                                        or fnum(m['mean_luma']) < 0.06 or fnum(m['mean_luma']) > 0.94):
            why = 'exposure'
        elif fnum(m['sharp_peak']) < A.blur_floor * 0.5 or \
                (fnum(m['sharp_peak']) < cut.get(r['shoot'], 0) and fnum(m['sharp_peak']) < A.blur_floor):
            why = 'blurry'   # hopeless, or soft relative to its own shoot and below the floor
        reasons[why.split(':')[0] or 'candidate'] += 1
        out.append({**r, 'reject': why, 'tech_score': m.get('tech_score', ''),
                    'cluster_id': dup.get(r['id'], {}).get('cluster_id', '')})
    cols = list(inv[0].keys()) + ['reject', 'tech_score', 'cluster_id']
    write_csv(os.path.join(W, 'triage.csv'), out, cols)
    cand = [r for r in out if not r['reject']]
    write_csv(os.path.join(W, 'candidates.csv'), cand, cols)
    print(f"{len(inv)} photos -> {len(cand)} candidates   " + ', '.join(f'{k}={v}' for k, v in reasons.most_common()))
    if not cand:
        print('no candidates left: check the reasons above (e.g. lower min_long_side in config.json, '
              'or --blur-floor), then rerun triage')
    ex = collections.Counter(r['shoot'] for r in out if r['reject'].startswith('excluded'))
    if ex:
        print('excluded shoots:', ', '.join(f'{s} ({n})' for s, n in ex.most_common(10)))


if __name__ == '__main__':
    main()
