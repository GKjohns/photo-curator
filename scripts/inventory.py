#!/usr/bin/env python3
"""Stage 1: inventory one or more sources into <work>/inventory.csv and shoots.csv.

Usage:
  inventory.py --detect                         # list mounted volumes / camera cards with photo counts
  inventory.py SRC [SRC ...] --work DIR [--gap-hours 3]

One row per photo: RAW+JPEG siblings (same folder, same stem) collapse into one row with
`path` = the fast preview file (JPEG if present) and `raw_path` = the RAW. Byte-identical copies
across folders or drives collapse by content hash (`copies` counts them). Shoots are the first
sub-folder under each source; camera-card folders (DCIM/100XXXXX) and loose files are grouped by
capture-date gaps instead. Read-only on the source. Reruns reuse a path/size/mtime index, so only
new or changed files are hashed.
"""
import argparse, collections, datetime, glob, json, os, re, subprocess, sys
from PIL import Image
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (IMG_EXT, RAW_EXT, add_work_arg, content_hash, ext_of, load_config, work_dir,
                    write_csv, write_json)

EDIT_DIRS = {'edited', 'edits', 'export', 'exports', 'final', 'finals', 'ready', 'selects', 'styles',
             'instagram', 'ig', 'web', 'output', 'processed', 'iter'}
SKIP_DIRS = {'.trashes', '.spotlight-v100', '.fseventsd', '.temporaryitems', 'node_modules', '.git',
             'thumbs', 'cache', '.thumbnails', 'private', 'avchd', 'clip', 'sub'}
CARD_DIR = re.compile(r'^\d{3}[A-Z0-9_]{5}$', re.I)   # DCIM/100MSDCF, 101CANON, 100_FUJI ...
SYSTEM_VOLUMES = {'Macintosh HD', 'Recovery', 'Preboot', 'VM', 'Update', 'Data'}


def detect():
    cands = []
    for v in sorted(glob.glob('/Volumes/*')) + [os.path.expanduser('~/Pictures')]:
        name = os.path.basename(v)
        if name in SYSTEM_VOLUMES or not os.path.isdir(v) or os.path.islink(v):
            continue
        n, budget = 0, 200000
        for dp, dn, fn in os.walk(v):
            dn[:] = [d for d in dn if not d.startswith('.') and d.lower() not in SKIP_DIRS]
            n += sum(1 for f in fn if ext_of(f) in IMG_EXT and not f.startswith('.'))
            budget -= len(fn) + 1
            if budget <= 0:
                break
        if n:
            cands.append({'path': v, 'camera_card': os.path.isdir(os.path.join(v, 'DCIM')),
                          'photos': n, 'partial_count': budget <= 0})
    print(json.dumps(cands, indent=1))


def exif_basics(path, ext):
    """(width, height, capture_iso, camera) best effort, cheap."""
    w = h = None
    date = cam = ''
    if ext in RAW_EXT:
        try:
            import rawpy
            with rawpy.imread(path) as r:
                w, h = r.sizes.width, r.sizes.height
                if r.sizes.flip in (5, 6):
                    w, h = h, w
        except Exception:
            pass
        try:
            o = subprocess.run(['exiftool', '-json', '-n', '-DateTimeOriginal', '-Model', '-ImageWidth', '-ImageHeight', path],
                               capture_output=True, timeout=30).stdout
            d = json.loads(o)[0]
            date = str(d.get('DateTimeOriginal') or '')
            cam = str(d.get('Model') or '')
            if not w and d.get('ImageWidth'):
                w, h = int(d['ImageWidth']), int(d['ImageHeight'])
        except Exception:
            pass
    else:
        try:
            im = Image.open(path)
            w, h = im.size
            ex = im.getexif()
            if ex.get(0x0112, 1) in (5, 6, 7, 8):
                w, h = h, w
            cam = str(ex.get(272, '')).strip('\x00 ')
            date = str(ex.get_ifd(0x8769).get(36867, '') or ex.get(306, ''))
        except Exception:
            pass
    iso = ''
    if date:
        try:
            iso = datetime.datetime.strptime(date[:19], '%Y:%m:%d %H:%M:%S').isoformat()
        except ValueError:
            pass
    return w, h, iso, cam


def shoot_of(root, path):
    rel = os.path.relpath(path, root).split(os.sep)
    parts = rel[:-1]
    if parts and parts[0].upper() == 'DCIM':
        return None   # camera card: group by date later
    if not parts or all(CARD_DIR.match(p) for p in parts):
        return None
    label = parts[0]
    return f'{os.path.basename(root.rstrip(os.sep)) or root}/{label}'


def main():
    ap = add_work_arg(argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter))
    ap.add_argument('sources', nargs='*')
    ap.add_argument('--detect', action='store_true')
    ap.add_argument('--gap-hours', type=float, default=3.0, help='date gap that splits card/loose files into shoots')
    ap.add_argument('--min-bytes', type=int, default=50_000)
    A = ap.parse_args()
    if A.detect:
        return detect()
    if not A.sources:
        sys.exit('give at least one source folder, volume or card (or --detect)')
    W = work_dir(A.work)
    cfg = load_config(W)
    idx_path = os.path.join(W, 'cache', 'inventory_index.json')
    os.makedirs(os.path.dirname(idx_path), exist_ok=True)
    idx = json.load(open(idx_path)) if os.path.exists(idx_path) else {}

    files = []
    for src in A.sources:
        root = os.path.abspath(os.path.expanduser(src))
        if not os.path.isdir(root):
            sys.exit(f'not a folder: {root}')
        for dp, dn, fn in os.walk(root):
            dn[:] = sorted(d for d in dn if not d.startswith('.') and d.lower() not in SKIP_DIRS)
            for f in sorted(fn):
                if f.startswith('.') or ext_of(f) not in IMG_EXT:
                    continue
                files.append((root, os.path.join(dp, f)))
    print(f'{len(files)} image files found', flush=True)

    recs, hashed = [], 0
    for k, (root, p) in enumerate(files, 1):
        try:
            st = os.stat(p)
        except OSError:
            continue
        if st.st_size < A.min_bytes:
            continue
        sig = f'{st.st_size}:{int(st.st_mtime)}'
        hit = idx.get(p)
        if hit and hit.get('sig') == sig:
            meta = hit
        else:
            ext = ext_of(p)
            w, h, date, cam = exif_basics(p, ext)
            meta = {'sig': sig, 'hash': content_hash(p), 'w': w, 'h': h, 'date': date, 'cam': cam}
            idx[p] = meta
            hashed += 1
            if hashed % 250 == 0:
                write_json(idx_path, idx)
                print(f'  hashed {hashed} ({k}/{len(files)})', flush=True)
        date = meta['date'] or datetime.datetime.fromtimestamp(st.st_mtime).isoformat(timespec='seconds')
        parts = [x.lower() for x in os.path.relpath(p, root).split(os.sep)[:-1]]
        recs.append({'root': root, 'path': p, 'ext': ext_of(p), 'bytes': st.st_size, 'hash': meta['hash'],
                     'width': meta['w'] or '', 'height': meta['h'] or '', 'capture_date': date,
                     'date_source': 'exif' if meta['date'] else 'mtime', 'camera_model': meta['cam'],
                     'role': 'edited_output' if any(x in EDIT_DIRS for x in parts) else 'original',
                     'shoot': shoot_of(root, p)})
    write_json(idx_path, idx)

    # shoots for card/loose files: split by capture-date gaps
    loose = sorted((r for r in recs if r['shoot'] is None), key=lambda r: r['capture_date'])
    prev, label = None, None
    for r in loose:
        t = datetime.datetime.fromisoformat(r['capture_date'])
        if prev is None or (t - prev).total_seconds() > A.gap_hours * 3600:
            label = f"{os.path.basename(r['root'].rstrip(os.sep)) or 'card'}/{t:%Y-%m-%d_%H%M}"
        r['shoot'] = label
        prev = t

    # RAW+JPEG pairing within a folder; then collapse byte-identical copies
    by_stem = collections.defaultdict(list)
    for r in recs:
        by_stem[(os.path.dirname(r['path']), os.path.splitext(os.path.basename(r['path']))[0].lower())].append(r)
    photos = {}
    for group in by_stem.values():
        raws = [r for r in group if r['ext'] in RAW_EXT]
        stds = [r for r in group if r['ext'] not in RAW_EXT]
        prefer = sorted(stds, key=lambda r: (r['ext'] not in ('jpg', 'jpeg'), -r['bytes']))
        lead = prefer[0] if prefer else raws[0]
        row = dict(lead)
        row['raw_path'] = raws[0]['path'] if raws else ''
        if raws and not row['width']:
            row['width'], row['height'] = raws[0]['width'], raws[0]['height']
        if raws and lead is not raws[0]:
            row['camera_model'] = row['camera_model'] or raws[0]['camera_model']
            if row['date_source'] == 'mtime' and raws[0]['date_source'] == 'exif':
                row['capture_date'], row['date_source'] = raws[0]['capture_date'], 'exif'
        rid = row['hash']
        if rid in photos:
            photos[rid]['copies'] += 1
            if row['raw_path'] and not photos[rid]['raw_path']:
                photos[rid]['raw_path'] = row['raw_path']
            continue
        row['id'], row['copies'] = rid, 1
        photos[rid] = row

    rows = sorted(photos.values(), key=lambda r: (r['shoot'], r['capture_date'], r['path']))
    for r in rows:
        w, h = r['width'], r['height']
        r['orientation'] = '' if not w else 'square' if w == h else 'landscape' if int(w) > int(h) else 'portrait'
    cols = ['id', 'path', 'raw_path', 'ext', 'role', 'bytes', 'width', 'height', 'orientation',
            'capture_date', 'date_source', 'camera_model', 'shoot', 'copies']
    write_csv(os.path.join(W, 'inventory.csv'), rows, cols)

    sh = collections.defaultdict(list)
    for r in rows:
        sh[r['shoot']].append(r)
    srows = []
    for s, v in sorted(sh.items()):
        d = sorted(x['capture_date'] for x in v)
        cams = collections.Counter(x['camera_model'] for x in v if x['camera_model']).most_common(1)
        srows.append({'shoot': s, 'photos': len(v), 'with_raw': sum(bool(x['raw_path']) for x in v),
                      'edited_outputs': sum(x['role'] == 'edited_output' for x in v),
                      'first': d[0][:16], 'last': d[-1][:16], 'camera': cams[0][0] if cams else '',
                      'folder': os.path.dirname(v[0]['path'])})
    write_csv(os.path.join(W, 'shoots.csv'), srows)
    if not os.path.exists(os.path.join(W, 'config.json')):
        write_json(os.path.join(W, 'config.json'), {k: v for k, v in cfg.items()})
    pairs = sum(bool(r['raw_path']) and r['ext'] not in RAW_EXT for r in rows)
    print(f'{len(rows)} photos in {len(srows)} shoots ({len(recs)} files, {pairs} RAW+JPEG pairs, '
          f'{sum(r["copies"] - 1 for r in rows)} duplicate copies, {hashed} newly hashed)')
    for s in sorted(srows, key=lambda s: -s['photos'])[:15]:
        print(f"  {s['photos']:5d}  {s['shoot']}  ({s['first'][:10]})")


if __name__ == '__main__':
    main()
