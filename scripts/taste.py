#!/usr/bin/env python3
"""Stage 5 (Sonnet-class model): six-part taste rubric on the best tagged survivors at 1024px.

Usage: taste.py --work DIR [--model anthropic/claude-sonnet-5.5] [--top 300] [--concurrency 4]

Selects the top --top tagged candidates (keeper_score, then technical score; drops user exclusions;
caps any one shoot at config per_shoot_cap so one big shoot can't crowd out the rest), then scores
each one alone. Cached per content hash in cache/taste/<id>.json (key = model + rubric + goal).
Writes taste.csv sorted by overall.
"""
import argparse, os, sys, threading
from concurrent.futures import ThreadPoolExecutor
from PIL import Image
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (add_work_arg, cache_get, cache_put, call_model, data_url, exclusion_text, fnum, gateway_key,
                    ledger_add, load_config, people_refs, pick_with_cap, read_csv, spent, thumb_path, version_key,
                    work_dir, write_csv)

GOALS = {
    'wall prints': 'Framed prints for a home. Favour frames that read across a room, hold up at 11x14 or larger, and live well together on a wall.',
    'portfolio': 'A photographer\'s portfolio. Favour craft, a distinct point of view, and range; penalise competent but generic frames hard.',
    'instagram': 'An Instagram feed. Favour frames that read instantly at phone size and survive a 4:5 crop; strong subject, clean background.',
    'album': 'A family or event album. Favour real moments, expressions and connection; technical flaws matter less than the moment.',
}
SYSTEM = """You are a veteran photo editor. The photographer wants about {picks} photos for: {goal}.
{goal_note}
You see one candidate per request (plus optional reference images of people the user named for this run). Stage-1 notes come from a cheaper model and may be wrong; judge for yourself.

Score with integers 1-10. Be a hard grader: 5 is an ordinary competent frame, 7 is a keeper, 9+ is rare. Use the full range.
- composition: structure, subject placement, background, edges, negative space
- light_color: quality of light, colour harmony, tonal range
- moment_emotion: expression, gesture, story, mood
- technical: focus where it matters, noise, exposure, enough resolution for the goal (pixel size is given)
- uniqueness: not a generic snapshot; something only this photographer could have made
- impact: holds attention at the size it will be seen
overall: not an average; your gut call as an editor for this goal. keeper = overall >= 7 and no exclusion.
People the user marked "prefer" may lift overall by up to 1 point when the frame is competent.
Exclusions set by the user for this run (any match: keeper=false, exclude_reason set):
{exclusions}
People of interest for this run:
{people}
category: a short genre label (portrait, family, street, landscape, architecture, travel, night, detail, ...).
crop: the crop you would use (native keeps the camera frame).
Keep text terse: why is exactly two specific sentences of honest critique."""
INTS = ['composition', 'light_color', 'moment_emotion', 'technical', 'uniqueness', 'impact', 'overall']
CROPS = ['native', '4:5', '3:2', '2:3', '1:1', '16:9', '5:7']
SIZES = ['5x7', '8x10', '11x14', '16x20', '24x36', 'screen only']
SCHEMA = {'title': 'taste', 'type': 'object', 'additionalProperties': False,
          'properties': {**{k: {'type': 'integer'} for k in INTS},
                         'keeper': {'type': 'boolean'}, 'exclude_reason': {'type': 'string'},
                         'category': {'type': 'string'}, 'crop': {'type': 'string', 'enum': CROPS},
                         'edit_notes': {'type': 'string'}, 'best_print_size': {'type': 'string', 'enum': SIZES},
                         'title': {'type': 'string'}, 'why': {'type': 'string'}},
          'required': INTS + ['keeper', 'exclude_reason', 'category', 'crop', 'edit_notes', 'best_print_size', 'title', 'why']}
COLS = ['id'] + INTS + ['keeper', 'exclude_reason', 'category', 'crop', 'edit_notes', 'best_print_size', 'title', 'why', 'shoot', 'model']


def system_prompt(cfg):
    _, people = people_refs(cfg) if cfg.get('people') else (None, '')
    return SYSTEM.format(picks=cfg['picks'], goal=cfg['goal'], goal_note=GOALS.get(cfg['goal'].lower(), ''),
                         exclusions=exclusion_text(cfg), people=people or '(none)')


def select(W, cfg, top):
    tags = {t['id']: t for t in read_csv(os.path.join(W, 'tags.csv'))}
    excl = {p['name'].lower() for p in cfg.get('people') or [] if p.get('want') == 'exclude'}
    pref = {p['name'].lower() for p in cfg.get('people') or [] if p.get('want') == 'prefer'}
    pool = []
    for r in read_csv(os.path.join(W, 'candidates.csv')):
        t = tags.get(r['id'])
        if not t or t.get('exclude') == 'True':
            continue
        names = {n.lower() for n in (t.get('people_of_interest') or '').split(';') if n}
        if names & excl:
            continue
        pri = fnum(t['keeper_score']) + 0.05 * fnum(r.get('tech_score')) + (1.0 if names & pref else 0)
        pool.append({**r, '_pri': pri, '_tag': t})
    pool.sort(key=lambda r: -r['_pri'])
    return pick_with_cap(pool, top, cfg['per_shoot_cap'])


def main():
    ap = add_work_arg(argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter))
    ap.add_argument('--model')
    ap.add_argument('--top', type=int)
    ap.add_argument('--concurrency', type=int, default=4)
    A = ap.parse_args()
    W = work_dir(A.work)
    cfg = load_config(W)
    model = A.model or cfg['models']['taste']
    system = system_prompt(cfg)
    ref_parts, _ = people_refs(cfg)
    ver = version_key(model, system, [p.get('ref') for p in cfg.get('people') or []])
    sel = select(W, cfg, A.top or cfg['taste_top'])
    if not sel:
        sys.exit('nothing to score; run tag.py first (or use the no-key path)')
    todo = [r for r in sel if cache_get(W, 'taste', r['id'], ver) is None]
    print(f'{len(sel)} selected for the rubric, {len(sel) - len(todo)} cached, {len(todo)} to score with {model}', flush=True)
    if todo and not gateway_key():
        sys.exit('AI_GATEWAY_API_KEY not set')
    lock, stop = threading.Lock(), threading.Event()

    def run(r):
        if stop.is_set():
            return
        if spent(W) >= cfg['budget_usd']:
            stop.set()
            print(f'budget ${cfg["budget_usd"]} reached; stopping', flush=True)
            return
        t = r['_tag']
        txt = (f"CANDIDATE from shoot '{r['shoot']}'. Original pixels: {r['width']}x{r['height']}. "
               f"Stage-1 notes: scene={t['scene']}; {t['subjects']}; people={t['people_count']}"
               + (f"; matched={t['people_of_interest']}" if t.get('people_of_interest') else '') + '.')
        content = list(ref_parts) + [{'type': 'text', 'text': txt},
                                     {'type': 'image_url', 'image_url': {'url': data_url(Image.open(thumb_path(W, r['id'], 1024)), 88)}}]
        try:
            obj, usage, cost = call_model(model, system, content, SCHEMA, max_tokens=900)
        except Exception as e:
            print('FAIL', r['id'], e, file=sys.stderr, flush=True)
            return
        with lock:
            ledger_add(W, 'taste', model, usage, cost, 1)
            cache_put(W, 'taste', r['id'], {**obj, 'id': r['id'], 'model': model}, ver)

    with ThreadPoolExecutor(A.concurrency) as ex:
        list(ex.map(run, todo))
    out = []
    for r in sel:
        t = cache_get(W, 'taste', r['id'], ver)
        if t:
            out.append({**t, 'shoot': r['shoot']})
    out.sort(key=lambda d: (-fnum(d.get('overall')), -fnum(d.get('impact'))))
    write_csv(os.path.join(W, 'taste.csv'), out, COLS)
    keepers = sum(str(d.get('keeper')) == 'True' for d in out)
    print(f'{len(out)} scored, {keepers} keepers -> taste.csv; taste spend ${spent(W, "taste"):.3f}, total ${spent(W):.3f}')


if __name__ == '__main__':
    main()
