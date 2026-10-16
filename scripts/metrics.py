#!/usr/bin/env python3
"""Stage 2 (local, free): thumbnails + technical metrics + near-duplicate clusters.

Usage: metrics.py --work DIR [--workers 8] [--dupe-dist 6]

Per photo (cached by content hash in cache/metrics/<id>.json and thumbs/<id>_{512,1024}.jpg):
sharpness (Laplacian variance: whole frame, centre, and peak = 90th percentile of 6x6 tiles), exposure (mean luma, p1/p99, clipped %),
colorfulness, a 64-bit perceptual hash, face count, long side and print feasibility.
Then clusters near-duplicates within each shoot (pHash Hamming distance <= --dupe-dist, i.e. bursts
and re-exports) and marks the best frame in each cluster. Writes metrics.csv and dupes.csv.
"""
import argparse, os, sys, time
from multiprocessing import Pool
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (add_work_arg, cache_get, cache_put, fnum, open_image, read_csv, resized,
                    thumb_path, work_dir, write_csv)

V = 'm2'   # bump when metric definitions change
COLS = ['id', 'ok', 'err', 'long_side', 'print_8x10', 'print_16x20', 'sharpness', 'sharp_center', 'sharp_peak',
        'mean_luma', 'p1', 'p99', 'clip_lo_pct', 'clip_hi_pct', 'colorfulness', 'phash', 'faces',
        'largest_face_frac', 'tech_score']
_cas = None
W = None


def cascade():
    """Haar face detector, or False when this OpenCV build lacks it (OpenCV 5 moved it out)."""
    global _cas
    if _cas is None:
        import cv2
        try:
            _cas = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_frontalface_default.xml')
        except AttributeError:
            _cas = False
    return _cas


def phash(gray):
    import cv2
    a = cv2.resize(gray.astype(np.float32), (32, 32), interpolation=cv2.INTER_AREA)
    d = cv2.dct(a)[:8, :8].flatten()
    bits = d > np.median(d[1:])
    return f'{int("".join("1" if b else "0" for b in bits), 2):016x}'


def colorfulness(a):
    a = a.astype(np.float32)
    rg = a[..., 0] - a[..., 1]
    yb = 0.5 * (a[..., 0] + a[..., 1]) - a[..., 2]
    return float(np.hypot(rg.std(), yb.std()) + 0.3 * np.hypot(rg.mean(), yb.mean()))


def tech_score(m):
    """0-10 heuristic used only for ordering (dupe winners, no-key contact sheets). Not taste."""
    s = 5 + 1.6 * np.log10(max(m['sharp_peak'], 1) / 120)
    s -= max(0, m['clip_hi_pct'] - 1) * 0.25 + max(0, m['clip_lo_pct'] - 3) * 0.12
    s -= abs(m['mean_luma'] - 0.45) * 3
    s += min(m['colorfulness'], 60) / 60
    return round(float(np.clip(s, 0, 10)), 2)


def init(w):
    global W
    W = w
    import cv2
    cv2.setNumThreads(1)


def process(job):
    import cv2
    rid, path, w, h = job
    try:
        im = open_image(path)
        t1024, t512 = resized(im, 1024), resized(im, 512)
        for t, n in ((t1024, 1024), (t512, 512)):
            p = thumb_path(W, rid, n)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            t.save(p, 'JPEG', quality=86)
        a = np.asarray(t1024)
        g = cv2.cvtColor(a, cv2.COLOR_RGB2GRAY)
        H, Wd = g.shape
        lap = lambda x: float(cv2.Laplacian(x, cv2.CV_64F).var())
        L = cv2.Laplacian(g, cv2.CV_64F)
        th, tw = H // 6, Wd // 6   # peak = sharpest tiles, so a sharp subject on a smooth sky still counts
        tiles = [L[i:i + th, j:j + tw].var() for i in range(0, H - th + 1, th) for j in range(0, Wd - tw + 1, tw)]
        gf = g / 255.0
        p1, p99 = np.percentile(gf, [1, 99])
        faces = cascade().detectMultiScale(g, 1.1, 5, minSize=(40, 40)) if cascade() else []
        ls = max(int(w or 0), int(h or 0)) or max(im.size)
        m = dict(id=rid, ok=1, err='', long_side=ls, print_8x10=int(ls >= 3000), print_16x20=int(ls >= 4800),
                 sharpness=round(lap(g), 2), sharp_center=round(lap(g[H // 4:3 * H // 4, Wd // 4:3 * Wd // 4]), 2),
                 sharp_peak=round(float(np.percentile(tiles, 90)), 2),
                 mean_luma=round(float(gf.mean()), 4), p1=round(float(p1), 4), p99=round(float(p99), 4),
                 clip_lo_pct=round(float((g <= 3).mean() * 100), 3), clip_hi_pct=round(float((g >= 252).mean() * 100), 3),
                 colorfulness=round(colorfulness(np.asarray(t512)), 2), phash=phash(g), faces=len(faces) if cascade() else '',
                 largest_face_frac=round(float(max((f[2] for f in faces), default=0)) / Wd, 4))
        m['tech_score'] = tech_score(m)
        cache_put(W, 'metrics', rid, m, V)
        return m
    except Exception as e:
        return {'id': rid, 'ok': 0, 'err': f'{type(e).__name__}: {e}'[:200]}


def dupes(rows, metrics, dist):
    par = {}

    def find(x):
        while par[x] != x:
            par[x] = par[par[x]]
            x = par[x]
        return x
    groups = {}
    for r in rows:
        m = metrics.get(r['id'])
        if m and str(m.get('ok')) == '1':
            par[r['id']] = r['id']
            groups.setdefault(r['shoot'], []).append(r['id'])
    for ids in groups.values():
        if len(ids) < 2:
            continue
        bits = np.array([[int(b) for b in bin(int(metrics[i]['phash'], 16))[2:].zfill(64)] for i in ids], dtype=np.uint8)
        for k in range(len(ids) - 1):
            d = (bits[k + 1:] != bits[k]).sum(1)
            for j in np.nonzero(d <= dist)[0]:
                a, b = find(ids[k]), find(ids[k + 1 + j])
                if a != b:
                    par[a] = b
    cl = {}
    for i in par:
        cl.setdefault(find(i), []).append(i)
    out, n = [], 0
    for ids in cl.values():
        n += 1
        best = max(ids, key=lambda i: (fnum(metrics[i]['tech_score']), fnum(metrics[i]['sharpness'])))
        for i in ids:
            out.append({'id': i, 'cluster_id': f'c{n:05d}', 'cluster_size': len(ids), 'is_best': int(i == best)})
    return out, sum(len(v) > 1 for v in cl.values())


def main():
    ap = add_work_arg(argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter))
    ap.add_argument('--workers', type=int, default=max(2, (os.cpu_count() or 4) - 1))
    ap.add_argument('--dupe-dist', type=int, default=6)
    A = ap.parse_args()
    Wd = work_dir(A.work)
    rows = read_csv(os.path.join(Wd, 'inventory.csv'))
    if not rows:
        sys.exit('no inventory.csv; run inventory.py first')
    metrics, jobs = {}, []
    for r in rows:
        m = cache_get(Wd, 'metrics', r['id'], V)
        if m and os.path.exists(thumb_path(Wd, r['id'], 512)) and os.path.exists(thumb_path(Wd, r['id'], 1024)):
            metrics[r['id']] = m
        else:
            jobs.append((r['id'], r['path'], r['width'], r['height']))
    print(f'{len(rows)} photos, {len(metrics)} cached, {len(jobs)} to process', flush=True)
    t0 = time.time()
    if jobs:
        with Pool(A.workers, initializer=init, initargs=(Wd,)) as pool:
            for k, m in enumerate(pool.imap_unordered(process, jobs, chunksize=2), 1):
                metrics[m['id']] = m
                if k % 200 == 0:
                    print(f'  {k}/{len(jobs)} {time.time() - t0:.0f}s', flush=True)
    write_csv(os.path.join(Wd, 'metrics.csv'), [metrics[r['id']] for r in rows if r['id'] in metrics], COLS)
    d, nclust = dupes(rows, metrics, A.dupe_dist)
    write_csv(os.path.join(Wd, 'dupes.csv'), d, ['id', 'cluster_id', 'cluster_size', 'is_best'])
    err = sum(str(m.get('ok')) != '1' for m in metrics.values())
    print(f'done in {time.time() - t0:.0f}s: ok={len(metrics) - err} errors={err} near-dupe clusters={nclust}')
    for m in [m for m in metrics.values() if str(m.get('ok')) != '1'][:5]:
        print('  error', m['id'], m.get('err'))


if __name__ == '__main__':
    main()
