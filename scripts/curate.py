#!/usr/bin/env python3
"""Stage 6 (top-tier model, one call): curator pass over the shortlist -> picks.json.

Usage: curate.py --work DIR [--model anthropic/claude-opus-5.5] [--shortlist 60] [--picks N] [--manual]

Builds the shortlist from taste.csv (best overall first, user exclusions dropped, per-shoot cap),
labels it S01..Snn, writes curate_in.json and contact sheets sheets/shortlist_NN.jpg. Then, unless
--manual or no AI_GATEWAY_API_KEY, sends the whole shortlist to the curator model in one request so
it can balance variety across shoots and subjects, avoid near-twins, and rank. The result is cached
by (model, prompt, shortlist), so a rerun with the same inputs costs nothing.

With --manual (or no key) it stops after the sheets: Claude views them and writes picks.json by hand
using the same schema (codes like "S07" are fine in place of ids).
"""
import argparse, json, os, sys
from PIL import Image
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (add_work_arg, cache_get, cache_put, call_model, contact_sheet, data_url, fnum, gateway_key,
                    ledger_add, load_config, pick_with_cap, read_csv, save_codes, spent, thumb_path, version_key,
                    work_dir, write_json)

SYSTEM = """You are the final curator. The photographer wants {picks} photos for: {goal}.
You get a labelled shortlist (S01, S02, ...) that already passed a technical cut and a rubric pass; each label comes with its shoot, rubric scores and the rubric's critique. Look at every image yourself.
Choose exactly {picks} picks (or fewer only if the shortlist truly lacks quality) and {alts} alternates.
- Quality first, then balance: spread picks across shoots, subjects and genres; never pick two near-identical frames; no more than {cap} from any one shoot unless it is clearly the strongest work.
- The set should hang together for the goal (tone, variety of scale and subject, a few anchors).
- Rank picks 1..n, best first. For each pick give a short title, two sentences of why (specific to the frame and its role in the set), the crop to use, and an edit plan of at most 20 words (exposure, white balance, straighten, crop, mood).
- Alternates: one sentence each on why it nearly made it.
- notes: 2-3 sentences on how the set is balanced and what is missing."""
CROPS = ['native', '4:5', '3:2', '2:3', '1:1', '16:9', '5:7']
SCHEMA = {'title': 'curation', 'type': 'object', 'additionalProperties': False,
          'properties': {
              'picks': {'type': 'array', 'items': {'type': 'object', 'additionalProperties': False,
                        'properties': {'code': {'type': 'string'}, 'rank': {'type': 'integer'}, 'title': {'type': 'string'},
                                       'why': {'type': 'string'}, 'crop': {'type': 'string', 'enum': CROPS},
                                       'edit_plan': {'type': 'string'}},
                        'required': ['code', 'rank', 'title', 'why', 'crop', 'edit_plan']}},
              'alternates': {'type': 'array', 'items': {'type': 'object', 'additionalProperties': False,
                             'properties': {'code': {'type': 'string'}, 'why': {'type': 'string'}},
                             'required': ['code', 'why']}},
              'notes': {'type': 'string'}},
          'required': ['picks', 'alternates', 'notes']}


def shortlist(W, cfg, n):
    inv = {r['id']: r for r in read_csv(os.path.join(W, 'candidates.csv'))}
    rows = [r for r in read_csv(os.path.join(W, 'taste.csv')) if not r.get('exclude_reason') and r['id'] in inv]
    rows.sort(key=lambda r: (-fnum(r['overall']), -fnum(r['impact']), -fnum(r['uniqueness'])))
    sel = pick_with_cap(rows, n, cfg['per_shoot_cap'])
    for k, r in enumerate(sel, 1):
        r['code'] = f'S{k:02d}'
    return sel


def main():
    ap = add_work_arg(argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter))
    ap.add_argument('--model')
    ap.add_argument('--shortlist', type=int)
    ap.add_argument('--picks', type=int)
    ap.add_argument('--manual', action='store_true', help='stop after writing sheets; Claude curates by eye')
    A = ap.parse_args()
    W = work_dir(A.work)
    cfg = load_config(W)
    picks_n = A.picks or cfg['picks']
    alts_n = cfg['alternates']
    model = A.model or cfg['models']['curator']
    sel = shortlist(W, cfg, A.shortlist or cfg['shortlist'])
    if not sel:
        sys.exit('empty shortlist; run taste.py first')
    save_codes(W, {r['code']: r['id'] for r in sel})
    keep = ['code', 'id', 'shoot', 'overall', 'composition', 'light_color', 'moment_emotion', 'technical',
            'uniqueness', 'impact', 'category', 'crop', 'title', 'why', 'edit_notes']
    write_json(os.path.join(W, 'curate_in.json'), [{k: r.get(k) for k in keep} for r in sel])
    os.makedirs(os.path.join(W, 'sheets'), exist_ok=True)
    for s in range(0, len(sel), 20):
        contact_sheet([(f"{r['code']} {r['overall']}", thumb_path(W, r['id'], 512)) for r in sel[s:s + 20]],
                      os.path.join(W, 'sheets', f'shortlist_{s // 20 + 1:02d}.jpg'), cols=5, cell=360)
    print(f'shortlist of {len(sel)} -> curate_in.json, sheets/shortlist_*.jpg')
    if A.manual or not gateway_key():
        print('manual curation: view the sheets (and thumbs/<id>_1024.jpg for close calls), then write picks.json')
        return

    cap = max(2, round(picks_n * cfg['per_shoot_cap']))
    system = SYSTEM.format(picks=picks_n, alts=alts_n, goal=cfg['goal'], cap=cap)
    ver = version_key(model, system, [r['id'] for r in sel])
    key = version_key('curator', ver)
    obj = cache_get(W, 'curator', key)
    if obj is None:
        if spent(W) >= cfg['budget_usd']:
            sys.exit(f'budget ${cfg["budget_usd"]} reached; raise budget_usd in config.json or curate manually')
        content = []
        for r in sel:
            content += [{'type': 'text', 'text': f"{r['code']} | shoot {r['shoot']} | overall {r['overall']} "
                         f"(comp {r['composition']}, light {r['light_color']}, moment {r['moment_emotion']}, "
                         f"tech {r['technical']}, unique {r['uniqueness']}, impact {r['impact']}) | {r['category']} | {r['why']}"},
                        {'type': 'image_url', 'image_url': {'url': data_url(Image.open(thumb_path(W, r['id'], 512)), 82)}}]
        print(f'curating with {model} ...', flush=True)
        obj, usage, cost = call_model(model, system, content, SCHEMA, max_tokens=6000, timeout=600)
        ledger_add(W, 'curator', model, usage, cost, len(sel))
        cache_put(W, 'curator', key, obj)
    by = {r['code']: r for r in sel}
    picks = [{'id': by[p['code']]['id'], 'code': p['code'], 'rank': p['rank'], 'title': p['title'], 'why': p['why'],
              'crop': p['crop'], 'edit_plan': p['edit_plan']} for p in obj['picks'] if p['code'] in by]
    chosen = {p['code'] for p in obj['picks']}
    alts = [{'id': by[a['code']]['id'], 'code': a['code'], 'why': a['why']} for a in obj['alternates']
            if a['code'] in by and a['code'] not in chosen]
    chosen |= {a['code'] for a in alts}
    close = [{'id': r['id'], 'code': r['code'], 'why': r['why']} for r in sel if r['code'] not in chosen]
    write_json(os.path.join(W, 'picks.json'), {'goal': cfg['goal'], 'curator': model, 'notes': obj.get('notes', ''),
                                               'picks': sorted(picks, key=lambda p: p['rank']),
                                               'alternates': alts, 'close': close})
    print(f'{len(picks)} picks, {len(alts)} alternates, {len(close)} close calls -> picks.json; '
          f'curator spend ${spent(W, "curator"):.3f}, total ${spent(W):.3f}')


if __name__ == '__main__':
    main()
