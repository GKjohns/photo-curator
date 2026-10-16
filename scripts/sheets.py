#!/usr/bin/env python3
"""Contact sheets for Claude to look at directly: the no-key path, or any manual review.

Usage:
  sheets.py --work DIR [--top 240] [--per-sheet 30]     # best candidates by technical score, per-shoot cap
  sheets.py --work DIR --codes C004,C017,C102 --big     # second look at chosen frames, larger cells
  sheets.py --work DIR --shoot "card/2026-05-01_0930"   # one shoot, every candidate

Each cell is labelled with a short code (C001...) mapped to the content-hash id in codes.json, so
picks.json can reference codes. Writes sheets/review_NN.jpg (or sheets/big_NN.jpg).
"""
import argparse, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (add_work_arg, contact_sheet, fnum, load_codes, load_config, pick_with_cap, read_csv,
                    save_codes, thumb_path, work_dir)


def main():
    ap = add_work_arg(argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter))
    ap.add_argument('--top', type=int, default=240)
    ap.add_argument('--per-sheet', type=int, default=30)
    ap.add_argument('--codes', help='comma list of codes or ids for a closer look')
    ap.add_argument('--shoot')
    ap.add_argument('--big', action='store_true', help='4 columns of 1024px thumbs instead of 6 x 512px')
    A = ap.parse_args()
    W = work_dir(A.work)
    cfg = load_config(W)
    rows = read_csv(os.path.join(W, 'candidates.csv'))
    if not rows:
        sys.exit('no candidates (run triage.py, or loosen its thresholds)')
    codes = load_codes(W)
    rev = {v: k for k, v in codes.items()}
    if A.codes:
        want = [codes.get(c.strip(), c.strip()) for c in A.codes.split(',') if c.strip()]
        by = {r['id']: r for r in rows}
        sel = [by[i] for i in want if i in by]
    else:
        pool = [r for r in rows if not A.shoot or r['shoot'] == A.shoot]
        pool.sort(key=lambda r: -fnum(r.get('tech_score')))
        sel = pool if A.shoot else pick_with_cap(pool, A.top, cfg['per_shoot_cap'])
        sel.sort(key=lambda r: (r['shoot'], r['capture_date']))
    n = len(codes)
    new = {}
    for r in sel:
        if r['id'] not in rev:
            n += 1
            c = f'C{n:03d}'
            rev[r['id']] = c
            new[c] = r['id']
    save_codes(W, new)
    os.makedirs(os.path.join(W, 'sheets'), exist_ok=True)
    per = 12 if A.big else A.per_sheet
    stem = 'big' if A.big else 'review'
    out = []
    for s in range(0, len(sel), per):
        p = os.path.join(W, 'sheets', f'{stem}_{s // per + 1:02d}.jpg')
        contact_sheet([(f"{rev[r['id']]}  {r['shoot'][-22:]}", thumb_path(W, r['id'], 1024 if A.big else 512))
                       for r in sel[s:s + per]], p, cols=4 if A.big else 6, cell=480 if A.big else 300)
        out.append(p)
    print(f'{len(sel)} photos on {len(out)} sheets:')
    for p in out:
        print(' ', p)


if __name__ == '__main__':
    main()
