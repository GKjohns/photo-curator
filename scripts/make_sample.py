#!/usr/bin/env python3
"""Build a tiny synthetic dataset end to end, with no model calls and no real photos.

Usage: make_sample.py --work /tmp/pc-sample [--export viewer/sample]

1. Paints procedural "photos" (sunsets, ridgelines, night cities, colonnades, dunes, forests, colour
   fields) into a fake camera card (DCIM, two capture-time clusters) and a folder of named shoots,
   with deliberate problems: a burst of near-duplicates, a blurry frame, a blown-out frame, a tilted
   horizon, a colour cast and an undersized export.
2. Runs the real local stages: inventory -> metrics -> triage.
3. Writes stand-in tag and rubric scores derived from the image statistics (clearly marked
   "synthetic"), runs curate.py --manual for the shortlist, writes picks.json, then the real
   edit.py and build_index.py.
Used to test the scripts and to ship viewer/sample so the viewer runs out of the box.
"""
import argparse, csv, datetime, json, os, subprocess, sys
import numpy as np
from PIL import Image
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from common import fnum, read_csv, write_csv, write_json

W_, H_ = 1800, 1200   # painting canvas
OUT = (1200, 800)     # saved size: keeps the committed sample small


def grad(c1, c2, h=H_, w=W_, power=1.0):
    t = np.linspace(0, 1, h)[:, None, None] ** power
    return np.broadcast_to(np.array(c1) * (1 - t) + np.array(c2) * t, (h, w, 3)).copy()


def ridge(img, base, amp, freq, color, seed, tilt=0.0):
    rng = np.random.default_rng(seed)
    x = np.arange(img.shape[1])
    y = base + tilt * (x - img.shape[1] / 2)
    for k in range(1, 5):
        y = y + amp / k * np.sin(x * freq * k + rng.uniform(0, 6.28))
    yy = np.arange(img.shape[0])[:, None]
    m = yy > y[None, :]
    img[m] = np.array(color)
    return img


def disc(img, cx, cy, r, color, soft=0.0):
    yy, xx = np.mgrid[0:img.shape[0], 0:img.shape[1]]
    d = np.hypot(xx - cx, yy - cy)
    a = np.clip((r - d) / max(1, soft) + 0.5, 0, 1)[..., None] if soft else (d < r)[..., None]
    return img * (1 - a) + np.array(color) * a


def finish(img, seed, cast=(1, 1, 1), noise=0.012):
    rng = np.random.default_rng(seed)
    h, w = img.shape[:2]
    yy, xx = np.mgrid[0:h, 0:w]
    v = 1 - 0.25 * (np.hypot((xx - w / 2) / (w / 2), (yy - h / 2) / (h / 2)) / 1.41) ** 2
    img = img * v[..., None] * np.array(cast) + rng.normal(0, noise, img.shape)
    return Image.fromarray((np.clip(img, 0, 1) * 255).astype(np.uint8))


def sunset(seed, tilt=0.0):
    img = grad((0.98, 0.62, 0.30), (0.35, 0.18, 0.40), power=0.8)
    img = disc(img, W_ * 0.62, H_ * 0.52, 70, (1, 0.93, 0.7), soft=6)
    img = ridge(img, H_ * 0.62, 25, 0.004, (0.18, 0.08, 0.14), seed, tilt)
    return ridge(img, H_ * 0.78, 18, 0.007, (0.08, 0.03, 0.07), seed + 1, tilt)


def mountains(seed):
    img = grad((0.62, 0.78, 0.95), (0.92, 0.94, 0.97))
    for k, c in enumerate([(0.55, 0.64, 0.78), (0.38, 0.47, 0.62), (0.22, 0.29, 0.40), (0.10, 0.14, 0.18)]):
        img = ridge(img, H_ * (0.42 + 0.13 * k), 60 - 10 * k, 0.003 + 0.002 * k, c, seed + k)
    return img


def night_city(seed):
    rng = np.random.default_rng(seed)
    img = grad((0.02, 0.03, 0.10), (0.10, 0.08, 0.20))
    x = 0
    while x < W_:
        bw, bh = rng.integers(90, 220), rng.integers(300, 900)
        img[H_ - bh:, x:x + bw] = (0.04, 0.04, 0.07)
        for wy in range(H_ - bh + 20, H_ - 20, 34):
            for wx in range(x + 12, x + bw - 16, 26):
                if rng.random() < 0.45:
                    img[wy:wy + 16, wx:wx + 12] = (1.0, 0.82, 0.45) if rng.random() < 0.8 else (0.7, 0.85, 1.0)
        x += bw + rng.integers(6, 30)
    return disc(img, W_ * 0.2, H_ * 0.18, 40, (0.95, 0.95, 0.85), soft=4)


def colonnade(seed, tilt=0.0):
    img = grad((0.93, 0.86, 0.74), (0.74, 0.62, 0.48))
    for k in range(9):
        x0 = 80 + k * 200
        img[150:1080, x0:x0 + 70] = (0.97, 0.94, 0.88)
        img[150:1080, x0 + 70:x0 + 90] = (0.55, 0.45, 0.36)
    img[1080:] = (0.45, 0.36, 0.28)
    img[90:150] = (0.85, 0.78, 0.68)
    im = Image.fromarray((img * 255).astype(np.uint8))
    if tilt:
        im = im.rotate(tilt, resample=Image.BICUBIC, expand=False, fillcolor=(190, 160, 125))
    return np.asarray(im, dtype=np.float32) / 255


def dunes(seed):
    img = grad((0.55, 0.75, 0.95), (0.85, 0.90, 0.95), power=1.5)
    for k, c in enumerate([(0.86, 0.62, 0.38), (0.78, 0.50, 0.28), (0.66, 0.38, 0.20)]):
        img = ridge(img, H_ * (0.45 + 0.17 * k), 70, 0.0025 + 0.001 * k, c, seed + k)
    sand = np.random.default_rng(seed).normal(0, 0.035, img.shape[:2])[..., None]
    return img + sand * (img[..., 2:3] < 0.5)   # grain in the sand, not the sky


def forest(seed):
    rng = np.random.default_rng(seed)
    img = grad((0.75, 0.85, 0.70), (0.20, 0.32, 0.18))
    for _ in range(26):
        x, w = rng.integers(0, W_), rng.integers(18, 70)
        img[:, x:x + w] = np.array((0.16, 0.12, 0.08)) * rng.uniform(0.7, 1.3)
    return disc(img, W_ * 0.5, -200, 700, (0.95, 0.98, 0.85), soft=400)


def color_field(seed):
    rng = np.random.default_rng(seed)
    img = np.ones((H_, W_, 3)) * np.array((0.55, 0.12, 0.10))
    img[120:560, 150:-150] = rng.choice([(0.85, 0.45, 0.12), (0.12, 0.25, 0.45), (0.92, 0.80, 0.30)])
    img[640:1080, 150:-150] = (0.30, 0.05, 0.08)
    return img


def make_card(root, start):
    """The shapes the pipeline must handle, with EXIF capture times."""
    scenes = [
        ('IMG_0101', 'sunset', sunset(1), {}), ('IMG_0102', 'sunset', sunset(1), {'shift': 3}),
        ('IMG_0103', 'sunset', sunset(1), {'shift': 6}), ('IMG_0104', 'sunset', sunset(2, tilt=0.03), {}),
        ('IMG_0105', 'mountains', mountains(3), {}), ('IMG_0106', 'mountains', mountains(4), {'blur': 9}),
        ('IMG_0107', 'dunes', dunes(5), {'cast': (1.10, 1.0, 0.86)}), ('IMG_0108', 'dunes', dunes(6), {'over': 2.6}),
        ('IMG_0201', 'night', night_city(7), {}), ('IMG_0202', 'night', night_city(8), {}),
        ('IMG_0203', 'colonnade', colonnade(9, tilt=2.2), {}), ('IMG_0204', 'forest', forest(10), {}),
        ('IMG_0205', 'field', color_field(11), {}),
    ]
    truth = {}
    d = os.path.join(root, 'card', 'DCIM', '100TEST')
    os.makedirs(d, exist_ok=True)
    for k, (name, kind, img, fx) in enumerate(scenes):
        truth[name + '.JPG'] = kind
        if fx.get('shift'):
            img = np.roll(img, fx['shift'] * 4, axis=1)
        im = finish(img, k, cast=fx.get('cast', (1, 1, 1))).resize(OUT, Image.LANCZOS)
        if fx.get('blur'):
            from PIL import ImageFilter
            im = im.filter(ImageFilter.GaussianBlur(fx['blur'] * 2 / 3))
            a = np.asarray(im, dtype=np.float32) + np.random.default_rng(k).normal(0, 3, (OUT[1], OUT[0], 3))
            im = Image.fromarray(np.clip(a, 0, 255).astype(np.uint8))   # sensor noise survives real blur
        if fx.get('over'):
            im = Image.fromarray(np.clip(np.asarray(im, dtype=np.float32) * fx['over'], 0, 255).astype(np.uint8))
        t = start + datetime.timedelta(minutes=7 * k) + (datetime.timedelta(hours=26) if name.startswith('IMG_02') else datetime.timedelta())
        ex = Image.Exif()
        ex[272] = 'Synthetic Camera'
        ex.get_ifd(0x8769)[36867] = t.strftime('%Y:%m:%d %H:%M:%S')
        im.save(os.path.join(d, name + '.JPG'), quality=90, exif=ex.tobytes())
    f = os.path.join(root, 'folders')
    for shoot, items in {'coast_walk': [('sunset', sunset(21)), ('sunset', sunset(22, tilt=-0.02)), ('mountains', mountains(23))],
                         'city_blocks': [('night', night_city(24)), ('colonnade', colonnade(25)), ('field', color_field(26))]}.items():
        os.makedirs(os.path.join(f, shoot), exist_ok=True)
        for k, (kind, img) in enumerate(items):
            finish(img, 100 + k).resize(OUT, Image.LANCZOS).save(os.path.join(f, shoot, f'frame_{k + 1:02d}.jpg'), quality=90)
            truth[f'{shoot}/frame_{k + 1:02d}.jpg'] = kind
    finish(forest(30), 200).resize((700, 467)).save(os.path.join(f, 'city_blocks', 'web_export_small.jpg'), quality=98)
    return [os.path.join(root, 'card'), f], truth


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--work', required=True)
    ap.add_argument('--export')
    A = ap.parse_args()
    W = os.path.abspath(A.work)
    os.makedirs(W, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k != 'AI_GATEWAY_API_KEY'}
    py = lambda s, *a: subprocess.run([sys.executable, os.path.join(HERE, s), *a, '--work', W], check=True, env=env)
    srcs, truth = make_card(W, datetime.datetime(2026, 5, 2, 18, 5))
    write_json(os.path.join(W, 'config.json'), {'_sample': 'synthetic dataset', 'picks': 6, 'alternates': 3,
                                                 'goal': 'wall prints', 'min_long_side': 1000, 'shortlist': 14,
                                                 'per_shoot_cap': 0.5})
    py('inventory.py', *srcs)
    py('metrics.py')
    py('triage.py')

    # stand-in model output (synthetic, derived from image statistics; no API calls)
    titles = {'sunset': ['Sun sinking behind layered ridges', 'Last light over the far hills', 'Ridges stacked in a copper dusk'],
              'mountains': ['Blue ridgelines fading into haze', 'Morning haze between the ranges'],
              'night': ['Lit windows in a night skyline', 'Blocks of light after dark'],
              'colonnade': ['Colonnade in warm afternoon light', 'Columns and long shadows'],
              'dunes': ['Wind-shaped dunes under a pale sky'], 'forest': ['Light falling through a stand of trunks'],
              'field': ['Stacked colour fields', 'Red ground, a band of colour']}
    used = {}
    met = {m['id']: m for m in read_csv(os.path.join(W, 'metrics.csv'))}

    def kind(r):
        rel = os.path.relpath(r['path'], W)
        return truth.get(os.path.basename(rel)) or truth.get('/'.join(rel.split(os.sep)[-2:]), 'sunset')

    def title(k):
        used[k] = used.get(k, -1) + 1
        return titles[k][used[k] % len(titles[k])]
    tags, taste = [], []
    rng = np.random.default_rng(0)
    for r in read_csv(os.path.join(W, 'candidates.csv')):
        m = met[r['id']]
        k = kind(r)
        t = title(k)
        base = 4 + fnum(m['tech_score']) * 0.35 + rng.uniform(0, 2.2)
        base += 2.5 if os.path.basename(r['path']) in ('IMG_0203.JPG', 'IMG_0107.JPG') else 0   # show straighten + WB
        sc = {x: int(np.clip(round(base + rng.uniform(-1.4, 1.4)), 1, 10)) for x in
              ('composition', 'light_color', 'moment_emotion', 'technical', 'uniqueness', 'impact')}
        sc['overall'] = int(np.clip(round(base + 0.6), 1, 10))
        tags.append({'id': r['id'], 'scene': 'landscape' if k in ('sunset', 'mountains', 'dunes') else 'cityscape' if k == 'night' else 'abstract' if k == 'field' else 'architecture' if k == 'colonnade' else 'nature',
                     'subjects': t.lower(), 'people_count': 0, 'people_of_interest': '', 'keeper_score': sc['overall'],
                     'technical': 'sharp; well_exposed', 'one_line': t, 'exclude': False, 'exclude_reason': '', 'model': 'synthetic'})
        taste.append({'id': r['id'], **sc, 'keeper': sc['overall'] >= 7, 'exclude_reason': '', 'category': tags[-1]['scene'],
                      'crop': 'native', 'edit_notes': 'Level the horizon, neutralise the cast, lift midtones slightly.',
                      'best_print_size': '11x14', 'title': t,
                      'why': f'Synthetic rubric note: a procedurally generated {k} frame scored from its sharpness and colour. '
                             'Real runs put a vision model\'s two-sentence critique here.',
                     'shoot': r['shoot'], 'model': 'synthetic'})
    from tag import COLS as TAG_COLS
    from taste import COLS as TASTE_COLS
    write_csv(os.path.join(W, 'tags.csv'), tags, TAG_COLS)
    taste.sort(key=lambda d: -d['overall'])
    write_csv(os.path.join(W, 'taste.csv'), taste, TASTE_COLS)
    py('curate.py', '--manual')
    sl = json.load(open(os.path.join(W, 'curate_in.json')))
    picks = [{'code': s['code'], 'rank': i + 1, 'title': s['title'],
              'why': 'Synthetic curator note: chosen for the sample dataset to exercise the viewer. '
                     'A real run explains the frame and its role in the set here.',
              'crop': '4:5' if i == 1 else 'native', 'edit_plan': s['edit_notes']} for i, s in enumerate(sl[:6])]
    alts = [{'code': s['code'], 'why': 'Synthetic: nearly made it.'} for s in sl[6:9]]
    close = [{'code': s['code'], 'why': s['why']} for s in sl[9:]]
    write_json(os.path.join(W, 'picks.json'), {'goal': 'wall prints', 'curator': 'synthetic sample', 'picks': picks,
                                               'alternates': alts, 'close': close,
                                               'notes': 'Sample dataset: procedurally generated images and stand-in scores. '
                                                        'Nothing here came from a model or a real photo.'})
    py('edit.py', '--alternates')
    py('build_index.py', '--title', 'Sample keepers', *(['--export', os.path.abspath(A.export), '--no-full'] if A.export else []))


if __name__ == '__main__':
    main()
