#!/usr/bin/env python3
"""Run the whole pipeline. Every stage is cached, so rerunning is cheap and resumes where it stopped.

Usage:
  pipeline.py SRC [SRC ...] --work DIR [--picks 12] [--goal "wall prints"] [--exclude PATTERN ...]
  pipeline.py --work DIR --from edit       # resume at a stage: inventory metrics triage tag taste curate edit index

With AI_GATEWAY_API_KEY set: inventory -> metrics -> triage -> tag -> taste -> curate -> edit -> index.
Without it: inventory -> metrics -> triage -> sheets, then stops so Claude can curate by eye and
write picks.json; rerun with --from edit afterwards.
"""
import argparse, json, os, subprocess, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import gateway_key, load_config, work_dir, write_json

HERE = os.path.dirname(os.path.abspath(__file__))
STAGES = ['inventory', 'metrics', 'triage', 'tag', 'taste', 'curate', 'edit', 'index']


def run(script, *args):
    cmd = [sys.executable, os.path.join(HERE, script), *args]
    print('\n$', ' '.join(os.path.basename(c) if c.startswith(HERE) else c for c in cmd), flush=True)
    r = subprocess.run(cmd)
    if r.returncode:
        sys.exit(f'{script} failed ({r.returncode})')


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('sources', nargs='*')
    ap.add_argument('--work')
    ap.add_argument('--from', dest='start', default='inventory', choices=STAGES)
    ap.add_argument('--picks', type=int)
    ap.add_argument('--goal')
    ap.add_argument('--exclude', action='append', default=None, help='path/shoot pattern to skip (repeatable)')
    ap.add_argument('--budget', type=float)
    ap.add_argument('--no-edit', action='store_true')
    A = ap.parse_args()
    W = work_dir(A.work)
    cfg_path = os.path.join(W, 'config.json')
    cfg = json.load(open(cfg_path)) if os.path.exists(cfg_path) else {}
    for k, v in (('picks', A.picks), ('goal', A.goal), ('exclude_patterns', A.exclude), ('budget_usd', A.budget)):
        if v is not None:
            cfg[k] = v
    write_json(cfg_path, {**load_config(W), **cfg})
    w = ['--work', W]
    todo = STAGES[STAGES.index(A.start):]
    key = bool(gateway_key())
    if 'inventory' in todo:
        if not A.sources:
            sys.exit('give the source folder(s) to inventory, or --from a later stage')
        run('inventory.py', *A.sources, *w)
    if 'metrics' in todo:
        run('metrics.py', *w)
    if 'triage' in todo:
        run('triage.py', *w)
    if not key and any(s in todo for s in ('tag', 'taste', 'curate')):
        run('sheets.py', *w)
        print('\nNo AI_GATEWAY_API_KEY: look at the sheets above, shortlist with `sheets.py --codes ... --big`,\n'
              f'write {os.path.join(W, "picks.json")}, then rerun with --from edit.')
        return
    for s, script in (('tag', 'tag.py'), ('taste', 'taste.py'), ('curate', 'curate.py')):
        if s in todo:
            run(script, *w)
    if 'edit' in todo and not A.no_edit:
        run('edit.py', *w)
    run('build_index.py', *w)
    print(f'\nDone. Viewer: open "{os.path.join(W, "viewer.html")}"')


if __name__ == '__main__':
    main()
