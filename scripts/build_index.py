#!/usr/bin/env python3
"""Stage 8: merge picks, scores, metrics and edits into <work>/index.json and <work>/viewer.html.

Usage: build_index.py --work DIR [--title "My keepers"] [--close 24] [--export OUT_DIR]

picks.json may come from curate.py or be written by hand (no-key path). Entries take "id" or a sheet
code ("S07", "C112"). Optional per-entry fields: rank, title, why, crop, edit_plan, category, and
"scores" ({composition, light_color, moment_emotion, technical, uniqueness, impact, overall}) when
there was no rubric pass. If picks.json has no "close" list, the best-scoring non-picks fill it.

viewer.html is self-contained (data inlined, images by relative path): open it straight from disk.
--export copies viewer.html, index.json and only the images they reference into OUT_DIR (a portable
folder you can zip or share); source paths inside the work dir are made relative.
"""
import argparse, collections, json, os, shutil, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (add_work_arg, fnum, load_config, read_csv, resolve_id, spent, thumb_path, work_dir, write_json)

TEMPLATE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'viewer', 'template.html')
SCORE_KEYS = ['composition', 'light_color', 'moment_emotion', 'technical', 'uniqueness', 'impact', 'overall']


def write_viewer(out_dir, idx):
    html = open(TEMPLATE, encoding='utf-8').read()
    data = json.dumps(idx, ensure_ascii=False).replace('</', '<\\/')
    with open(os.path.join(out_dir, 'viewer.html'), 'w', encoding='utf-8') as f:
        f.write(html.replace('__DATA__', data))


def main():
    ap = add_work_arg(argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter))
    ap.add_argument('--title', default='')
    ap.add_argument('--close', type=int, default=24)
    ap.add_argument('--export')
    ap.add_argument('--no-full', action='store_true', help='with --export: leave out full-resolution edits')
    A = ap.parse_args()
    W = work_dir(A.work)
    cfg = load_config(W)
    P = json.load(open(os.path.join(W, 'picks.json')))
    inv = {r['id']: r for r in read_csv(os.path.join(W, 'inventory.csv'))}
    met = {r['id']: r for r in read_csv(os.path.join(W, 'metrics.csv'))}
    tags = {r['id']: r for r in read_csv(os.path.join(W, 'tags.csv'))}
    taste = {r['id']: r for r in read_csv(os.path.join(W, 'taste.csv'))}
    cand = read_csv(os.path.join(W, 'candidates.csv'))
    mpath = os.path.join(W, 'edited', 'manifest.json')
    man = json.load(open(mpath)) if os.path.exists(mpath) else {}

    def rel(p):
        return os.path.relpath(p, W) if p and os.path.exists(p) else None

    def item(p, kind):
        rid = resolve_id(W, p.get('id') or p.get('code'))
        r, m, t, s, e = inv.get(rid, {}), met.get(rid, {}), tags.get(rid, {}), taste.get(rid, {}), man.get(rid, {})
        scores = p.get('scores') or ({k: int(fnum(s[k])) for k in SCORE_KEYS if s.get(k)} if s else None) or None
        return {
            'id': rid, 'kind': kind, 'code': p.get('code', ''), 'rank': p.get('rank'),
            'title': p.get('title') or s.get('title') or t.get('one_line') or '',
            'why': p.get('why') or s.get('why') or '',
            'rubric_why': s.get('why', '') if p.get('why') and s.get('why') != p.get('why') else '',
            'edit_plan': p.get('edit_plan') or s.get('edit_notes') or '',
            'crop': p.get('crop') or s.get('crop') or '', 'category': p.get('category') or s.get('category') or t.get('scene') or '',
            'print_size': s.get('best_print_size', ''), 'scores': scores,
            'tag': {k: t[k] for k in ('scene', 'subjects', 'one_line', 'keeper_score', 'people_of_interest') if t.get(k)} or None,
            'shoot': r.get('shoot', ''), 'date': (r.get('capture_date') or '')[:10], 'camera': r.get('camera_model', ''),
            'dims': f"{r.get('width', '')}x{r.get('height', '')}" if r.get('width') else '',
            'source': r.get('path', ''), 'raw_source': r.get('raw_path', ''), 'edited_from': e.get('source', ''),
            'metrics': {k: fnum(m[k]) for k in ('sharpness', 'mean_luma', 'clip_hi_pct', 'clip_lo_pct', 'colorfulness',
                                                 'faces', 'long_side', 'tech_score') if m.get(k) not in (None, '')} or None,
            'edit': e.get('params'),
            'images': {'thumb': rel(thumb_path(W, rid, 512)), 'large': rel(thumb_path(W, rid, 1024)),
                       'edited': rel(os.path.join(W, e['web'])) if e.get('web') else None,
                       'before': rel(os.path.join(W, e['before'])) if e.get('before') else None,
                       'full': rel(os.path.join(W, 'edited', e['full'])) if e.get('full') else None},
        }

    picks = [item(p, 'pick') for p in sorted(P.get('picks', []), key=lambda p: p.get('rank') or 999)]
    for k, p in enumerate(picks, 1):
        p['rank'] = p['rank'] or k
    alts = [item(p, 'alternate') for p in P.get('alternates', [])]
    used = {p['id'] for p in picks + alts}
    if 'close' in P:
        close = [item(p, 'close') for p in P['close'] if resolve_id(W, p.get('id') or p.get('code')) not in used]
    else:
        pool = sorted(taste.values(), key=lambda r: -fnum(r.get('overall'))) if taste else \
            sorted(cand, key=lambda r: -fnum(r.get('tech_score')))
        close = [item({'id': r['id']}, 'close') for r in pool if r['id'] not in used][:A.close]
    close = close[:A.close]

    by_stage = collections.defaultdict(float)
    cp = os.path.join(W, 'costs.jsonl')
    if os.path.exists(cp):
        for line in open(cp):
            try:
                d = json.loads(line)
                by_stage[d['stage']] += float(d.get('cost') or 0)
            except Exception:
                pass
    shortlist = os.path.join(W, 'curate_in.json')
    funnel = [('Photos found', len(inv)), ('Passed technical cut', len(cand)), ('Tagged', len(tags)),
              ('Rubric scored', len(taste)),
              ('Shortlist', len(json.load(open(shortlist))) if os.path.exists(shortlist) else 0), ('Picks', len(picks))]
    idx = {'title': A.title or f'{len(picks)} keepers', 'goal': P.get('goal') or cfg['goal'], 'notes': P.get('notes', ''),
           'curator': P.get('curator', 'manual'), 'generated': time.strftime('%Y-%m-%d %H:%M'),
           'shoots': len({r['shoot'] for r in inv.values()}),
           'funnel': [{'label': a, 'n': b} for a, b in funnel if b],
           'cost': {'total': round(spent(W), 4), 'by_stage': {k: round(v, 4) for k, v in by_stage.items()}},
           'picks': picks, 'alternates': alts, 'close': close}
    write_json(os.path.join(W, 'index.json'), idx)
    write_viewer(W, idx)
    print(f'index.json: {len(picks)} picks, {len(alts)} alternates, {len(close)} close calls; '
          f'{sum(bool(p["images"]["edited"]) for p in picks)} picks edited -> {os.path.join(W, "viewer.html")}')

    if A.export:
        out = os.path.abspath(A.export)
        os.makedirs(out, exist_ok=True)
        for it in picks + alts + close:
            if A.no_full:
                it['images']['full'] = None
            for k, v in it['images'].items():
                if v:
                    os.makedirs(os.path.dirname(os.path.join(out, v)), exist_ok=True)
                    shutil.copy2(os.path.join(W, v), os.path.join(out, v))
            for k in ('source', 'raw_source', 'edited_from'):
                if it[k] and os.path.abspath(it[k]).startswith(W + os.sep):
                    it[k] = os.path.relpath(it[k], W)
        write_json(os.path.join(out, 'index.json'), idx)
        write_viewer(out, idx)
        print('exported to', out)


if __name__ == '__main__':
    main()
