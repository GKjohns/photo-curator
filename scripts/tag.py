#!/usr/bin/env python3
"""Stage 4 (cheap model): batch vision tagging of every candidate at 512px.

Usage: tag.py --work DIR [--model openai/gpt-6-luna] [--batch 8] [--concurrency 4] [--limit N]

Needs AI_GATEWAY_API_KEY. Per photo it records scene, subjects, a 1-10 keeper score, a caption,
people-of-interest matches (only when the user configured `people` for this run) and whether it
hits one of the user's natural-language exclusions. Cached per content hash in cache/tag/<id>.json;
the cache key includes model + prompt + exclusions, so reruns cost nothing unless those change.
Writes tags.csv (one row per tagged candidate).
"""
import argparse, os, sys, threading
from concurrent.futures import ThreadPoolExecutor
from PIL import Image
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (add_work_arg, cache_get, cache_put, call_model, data_url, exclusion_text, gateway_key,
                    ledger_add, load_config, people_refs, read_csv, spent, thumb_path, version_key, work_dir,
                    write_csv)

SCENES = ['portrait', 'couple', 'group', 'child', 'candid', 'street', 'landscape', 'cityscape', 'architecture',
          'nature', 'animal', 'food', 'still_life', 'event', 'sport', 'night', 'abstract', 'document', 'other']
PROMPT = """You are a fast first-pass photo culler. The photographer wants roughly {picks} photos for: {goal}.
Candidate images follow, each preceded by a label like "IMAGE img3". For every image return one result, same order, with:
- label: the image label exactly as given
- scene: one of {scenes}
- subjects: short description of what is in the frame (no guessing names unless matched to a reference)
- people_count: number of clearly visible people
- people_of_interest: names of reference people you are confident appear (empty list if none or no references)
- keeper_score: 1-10, would a good editor keep this for the goal? 5 = ordinary competent frame, 7 = strong, 9+ = rare. Use the full range.
- technical: "sharp|soft|blurry; well_exposed|under|over"
- one_line: caption, at most 14 words
- exclude: true if the image matches any exclusion below, or is a screenshot, receipt, document or meme
- exclude_reason: short reason, or ""
Exclusions set by the user for this run:
{exclusions}
People of interest set by the user for this run:
{people}
Judge identity only from the reference images, never from stereotypes. When unsure, leave the name out."""

ITEM = {'type': 'object', 'additionalProperties': False,
        'properties': {'label': {'type': 'string'}, 'scene': {'type': 'string', 'enum': SCENES},
                       'subjects': {'type': 'string'}, 'people_count': {'type': 'integer'},
                       'people_of_interest': {'type': 'array', 'items': {'type': 'string'}},
                       'keeper_score': {'type': 'integer'}, 'technical': {'type': 'string'},
                       'one_line': {'type': 'string'}, 'exclude': {'type': 'boolean'},
                       'exclude_reason': {'type': 'string'}},
        'required': ['label', 'scene', 'subjects', 'people_count', 'people_of_interest', 'keeper_score',
                     'technical', 'one_line', 'exclude', 'exclude_reason']}
SCHEMA = {'title': 'tags', 'type': 'object', 'additionalProperties': False,
          'properties': {'results': {'type': 'array', 'items': ITEM}}, 'required': ['results']}
COLS = ['id', 'scene', 'subjects', 'people_count', 'people_of_interest', 'keeper_score', 'technical', 'one_line',
        'exclude', 'exclude_reason', 'model']


def main():
    ap = add_work_arg(argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter))
    ap.add_argument('--model')
    ap.add_argument('--batch', type=int, default=8)
    ap.add_argument('--concurrency', type=int, default=4)
    ap.add_argument('--limit', type=int)
    A = ap.parse_args()
    W = work_dir(A.work)
    cfg = load_config(W)
    model = A.model or cfg['models']['tag']
    ref_parts, people = people_refs(cfg)
    prompt = PROMPT.format(picks=cfg['picks'], goal=cfg['goal'], scenes=', '.join(SCENES),
                           exclusions=exclusion_text(cfg), people=people or '(none)')
    ver = version_key(model, prompt, [p.get('ref') for p in cfg.get('people') or []])
    rows = read_csv(os.path.join(W, 'candidates.csv'))
    if not rows:
        sys.exit('no candidates.csv; run triage.py first')
    todo = [r for r in rows if cache_get(W, 'tag', r['id'], ver) is None]
    if A.limit:
        todo = todo[:A.limit]
    print(f'{len(rows)} candidates, {len(rows) - len(todo)} cached, {len(todo)} to tag with {model}', flush=True)
    if todo and not gateway_key():
        sys.exit('AI_GATEWAY_API_KEY not set: use the no-key path (sheets.py) described in SKILL.md')
    lock, stop = threading.Lock(), threading.Event()
    batches = [todo[i:i + A.batch] for i in range(0, len(todo), A.batch)]
    done = [0]

    def run(b):
        if stop.is_set():
            return
        if spent(W) >= cfg['budget_usd']:
            stop.set()
            print(f'budget ${cfg["budget_usd"]} reached; stopping', flush=True)
            return
        content = ([{'type': 'text', 'text': 'Reference people:'}] + ref_parts) if ref_parts else []
        for k, r in enumerate(b, 1):
            im = Image.open(thumb_path(W, r['id'], 512))
            content += [{'type': 'text', 'text': f'IMAGE img{k}'}, {'type': 'image_url', 'image_url': {'url': data_url(im)}}]
        try:
            obj, usage, cost = call_model(model, prompt, content, SCHEMA, max_tokens=220 * len(b) + 400)
        except Exception as e:
            print('FAIL batch', [r['id'] for r in b], e, file=sys.stderr, flush=True)
            return
        results = obj.get('results') or []
        res = {x.get('label'): x for x in results}
        with lock:
            ledger_add(W, 'tag', model, usage, cost, len(b))
            for k, r in enumerate(b, 1):
                x = res.get(f'img{k}')
                if x is None and len(results) == len(b):   # labels mangled but order intact
                    x = results[k - 1]
                if not x:
                    continue
                x = {kk: v for kk, v in x.items() if kk != 'label'}
                cache_put(W, 'tag', r['id'], {**x, 'id': r['id'], 'model': model}, ver)
            done[0] += len(b)
            if done[0] % 200 < len(b):
                print(f'  {done[0]}/{len(todo)} tagged, ${spent(W, "tag"):.3f} so far', flush=True)

    with ThreadPoolExecutor(A.concurrency) as ex:
        list(ex.map(run, batches))
    out = []
    for r in rows:
        t = cache_get(W, 'tag', r['id'], ver)
        if t:
            t['people_of_interest'] = ';'.join(t.get('people_of_interest') or [])
            out.append(t)
    write_csv(os.path.join(W, 'tags.csv'), out, COLS)
    print(f'{len(out)}/{len(rows)} tagged -> tags.csv; tag spend ${spent(W, "tag"):.3f}, total ${spent(W):.3f}')


if __name__ == '__main__':
    main()
