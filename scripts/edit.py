#!/usr/bin/env python3
"""Stage 7: develop and edit the picks from the best available source (RAW when present).

Usage:
  edit.py --work DIR                    # picks: auto params -> full-res + web + before
  edit.py --work DIR --alternates       # also the alternates
  edit.py --work DIR --ids S03,9f2c...  # only these (codes or ids)
  edit.py --work DIR --draft            # fast half-size renders of web + before only (critique loop)
  edit.py --work DIR --reauto           # recompute auto params (keeps files you marked "_auto": false)

For each photo, edits/<id>.json holds the params (auto-generated on first run: straighten from
detected lines, white balance from near-neutral pixels, exposure toward a healthy median without
clipping, gentle contrast, skin-protected vibrance, output sharpening; crop from the curator's
suggestion or config `aspect`). Edit the JSON, set "_auto": false so it is kept, and rerun.
Renders are skipped when params + source are unchanged.

Outputs (in <work>/edited/):
  NN-<title>.jpg           full resolution, q92, sRGB, camera EXIF (picks; alternates in alternates/)
  web/<id>.jpg             2048px long side, for the viewer
  before/<id>.jpg          same geometry, ungraded, for the before/after slider
  manifest.json            id -> files + params
Photos that were already edited exports (role=edited_output) get geometry + sharpening only.
"""
import argparse, hashlib, json, os, re, sys
import numpy as np, cv2
from PIL import Image, ImageCms, ImageFilter, ImageOps
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (RAW_EXT, add_work_arg, ext_of, load_config, read_csv, resolve_id, work_dir, write_json)

SRGB = ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes()
ASPECTS = {'4:5': 4 / 5, '3:2': 3 / 2, '2:3': 2 / 3, '1:1': 1.0, '16:9': 16 / 9, '5:7': 5 / 7, '8:10': 8 / 10}
WEB = 2048


def s2l(x): return np.where(x <= 0.04045, x / 12.92, ((x + 0.055) / 1.055) ** 2.4)
def l2s(x): x = np.clip(x, 0, 1); return np.where(x <= 0.0031308, x * 12.92, 1.055 * x ** (1 / 2.4) - 0.055)
def lum(a): return a[..., 0] * 0.2126 + a[..., 1] * 0.7152 + a[..., 2] * 0.0722


# ---------- loading ----------
def load(src, full):
    """(RGB PIL, icc). RAW develops with rawpy using camera WB and no auto-brighten."""
    if ext_of(src) in RAW_EXT:
        try:
            import rawpy
        except ImportError:
            raise RuntimeError('RAW source needs rawpy (pip install rawpy)')
        with rawpy.imread(src) as r:
            rgb = r.postprocess(use_camera_wb=True, no_auto_bright=True, bright=1.0, output_bps=8, half_size=not full)
        return Image.fromarray(rgb), SRGB
    im = Image.open(src)
    icc = im.info.get('icc_profile') or SRGB
    im = ImageOps.exif_transpose(im).convert('RGB')
    if not full:
        im = im.reduce(max(1, round(max(im.size) / 1600)))
    return im, icc


def exif_bytes(src, raw_sibling=None):
    cands = [c for c in (src if ext_of(src) not in RAW_EXT else None, raw_sibling) if c and ext_of(c) not in RAW_EXT]
    cands += [os.path.splitext(src)[0] + e for e in ('.JPG', '.jpg', '.jpeg', '.JPEG')]
    for c in cands:
        try:
            ex = Image.open(c).getexif()
            ex[0x0112] = 1
            return ex.tobytes()
        except Exception:
            continue
    return b''


# ---------- auto analysis ----------
def detect_tilt(im):
    """Degrees CCW to level the frame, from near-horizontal/vertical line segments; 0 if unsure."""
    g = np.asarray(im.convert('L').resize((1024, round(1024 * im.height / im.width)) if im.width >= im.height
                                          else (round(1024 * im.width / im.height), 1024)))
    e = cv2.Canny(cv2.GaussianBlur(g, (3, 3), 0), 60, 160)
    H, Wd = g.shape
    segs = cv2.HoughLinesP(e, 1, np.pi / 720, 80, minLineLength=int(0.15 * min(H, Wd)), maxLineGap=6)
    if segs is None:
        return 0.0
    devs, wts = [], []
    for x1, y1, x2, y2 in segs.reshape(-1, 4):
        th = np.degrees(np.arctan2(y2 - y1, x2 - x1))
        th = th - 180 if th > 90 else th + 180 if th <= -90 else th
        if abs(th) < 4:
            d = th
        elif abs(th) > 86:
            d = th - 90 if th > 0 else th + 90
        else:
            continue
        devs.append(d)
        wts.append(np.hypot(x2 - x1, y2 - y1))
    if len(devs) < 4 or sum(wts) < 0.6 * max(H, Wd):
        return 0.0
    o = np.argsort(devs)
    cw = np.cumsum(np.array(wts)[o])
    med = float(np.array(devs)[o][np.searchsorted(cw, cw[-1] / 2)])
    spread = np.average(np.abs(np.array(devs) - med), weights=wts)
    if spread > 1.2 or abs(med) < 0.15:
        return 0.0
    return round(med, 2)


def auto_params(im, aspect, crop, passthrough, raw=False):
    p = {'_auto': True, 'rotate': 0.0, 'aspect': crop or aspect, 'crop_scale': 1.0, 'crop_cx': 0.5, 'crop_cy': 0.5,
         'temp': 0.0, 'tint': 0.0, 'ev': 0.0, 'hl_knee': 0.82, 'contrast': 0.0, 'shadows': 0.0, 'highlights': 0.0,
         'fade': 0.0, 'white': 1.0, 'sat': 1.0, 'vib': 0.0, 'skin_protect': 0.7, 'chroma_nr': 0.0,
         'sharp_amt': 50, 'sharp_rad': 1.2}
    if passthrough:
        p['_note'] = 'source is already an edited export: geometry + sharpening only'
        return p
    p['rotate'] = detect_tilt(im)
    a = np.asarray(im.reduce(max(1, max(im.size) // 900)), dtype=np.float32) / 255
    lin = s2l(a)
    Y = lum(lin)
    s = a.max(2) - a.min(2)
    m = (Y > 0.03) & (Y < 0.7) & (s < 0.06)
    if m.mean() > 0.05:   # white balance from near-neutral midtones
        R, G, B = (lin[..., c][m].mean() for c in range(3))
        t, g = (B - R) / (B + R + 1e-6), 1 - (R + B) / (2 * G + 1e-6)
        if max(abs(t), abs(g)) < 0.12:   # bigger implied shifts mean the "neutrals" are coloured light: leave it
            p['temp'] = round(float(np.clip(0.4 * t, -0.04, 0.04)), 3)
            p['tint'] = round(float(np.clip(0.4 * g, -0.03, 0.03)), 3)
    med, p99 = float(np.median(Y)), float(np.percentile(Y, 99.5))
    ev = 0.0
    if med < 0.05:                 # clearly dark: lift halfway toward a normal median
        ev = 0.5 * np.log2(0.12 / max(med, 1e-4))
    elif med > 0.45 and float(np.percentile(Y, 5)) > 0.12:   # washed out, not just high key
        ev = 0.5 * np.log2(0.3 / med)
    if med < 0.05 and p99 > 0.45:  # dark frame with real highlights = low key on purpose
        ev = min(ev, 0.3)
    ev = min(ev, np.log2(0.92 / max(p99, 1e-4)) + 0.3)   # the knee absorbs a little extra
    p['ev'] = round(float(np.clip(ev, -0.7, 1.0 if raw else 0.7)), 2)
    p['contrast'] = 0.18 if float(np.percentile(Y, 95) - np.percentile(Y, 5)) < 0.5 else 0.1
    p['shadows'] = 0.03 if float(np.percentile(Y, 5)) < 0.01 else 0.0
    p['highlights'] = -0.03 if p99 > 0.9 else 0.0
    p['vib'] = 0.12
    p['chroma_nr'] = 1.0
    return p


# ---------- geometry ----------
def crop_box(W, H, P):
    th = np.radians(P.get('rotate', 0))
    c, s = abs(np.cos(th)), abs(np.sin(th))
    asp = P.get('aspect', 'native')
    a = W / H if asp in (None, '', 'native', 'none') else ASPECTS.get(asp, W / H)
    if asp in ASPECTS and (a < 1) != (W < H) and abs(a - 1) > 1e-6:
        a = 1 / a   # aspects are orientation-aware: 4:5 on a landscape frame means 5:4
    h = min(W / (a * c + s), H / (a * s + c)) * P.get('crop_scale', 1.0)
    w = a * h
    cx0, cy0 = W / 2, H / 2

    def inside(cx, cy):
        for dx in (-w / 2, w / 2):
            for dy in (-h / 2, h / 2):
                x, y = cx + dx - cx0, cy + dy - cy0
                xr, yr = x * np.cos(-th) - y * np.sin(-th), x * np.sin(-th) + y * np.cos(-th)
                if abs(xr) > W / 2 + 0.5 or abs(yr) > H / 2 + 0.5:
                    return False
        return True
    tx, ty = P.get('crop_cx', 0.5) * W, P.get('crop_cy', 0.5) * H
    lo, hi = 0.0, 1.0
    if inside(tx, ty):
        lo = 1.0
    else:
        for _ in range(24):
            mid = (lo + hi) / 2
            lo, hi = (mid, hi) if inside(cx0 + mid * (tx - cx0), cy0 + mid * (ty - cy0)) else (lo, mid)
    cx, cy = cx0 + lo * (tx - cx0), cy0 + lo * (ty - cy0)
    return tuple(int(round(v)) for v in (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2))


def geometry(im, P):
    if P.get('rotate'):
        im = im.rotate(P['rotate'], resample=Image.BICUBIC, expand=False)
    return im.crop(crop_box(im.width, im.height, P))


# ---------- grade (the natural develop from camera-portrait-edit, generalised) ----------
def grade(im, P, full):
    a = np.asarray(im, dtype=np.float32) / 255.0
    if P.get('chroma_nr', 0):
        ycc = cv2.cvtColor(a, cv2.COLOR_RGB2YCrCb)
        sig = max(0.6, P['chroma_nr'] * (1 if full else 0.4))
        for ch in (1, 2):
            ycc[..., ch] = cv2.GaussianBlur(ycc[..., ch], (0, 0), sig)
        a = cv2.cvtColor(ycc, cv2.COLOR_YCrCb2RGB)
    lin = s2l(np.clip(a, 0, 1))
    t, g = P.get('temp', 0), P.get('tint', 0)
    lin = lin * np.array([1 + t, 1 - g, 1 - t], dtype=np.float32) * 2 ** P.get('ev', 0)
    kn = P.get('hl_knee', 0.82)
    Y = lum(lin)[..., None]
    Yc = np.where(Y > kn, kn + (1 - kn) * np.tanh((Y - kn) / (1 - kn)), Y)
    lin = lin * np.where(Y > 1e-6, Yc / np.maximum(Y, 1e-6), 1)
    s = l2s(lin)
    x = np.clip(lum(s)[..., None], 0, 1)
    y = x + P.get('contrast', 0.0) * (x - x * x) * (2 * x - 1)
    y = y + P.get('shadows', 0.0) * np.exp(-((x - 0.18) / 0.14) ** 2)
    y = y + P.get('highlights', 0.0) * np.exp(-((x - 0.82) / 0.12) ** 2)
    y = P.get('fade', 0.0) + y * (P.get('white', 1.0) - P.get('fade', 0.0))
    s = s * np.where(x > 1e-4, y / np.maximum(x, 1e-4), 1)
    Ys = lum(s)[..., None]
    chroma = (s.max(2) - s.min(2))[..., None]
    hsv = cv2.cvtColor(np.clip(s, 0, 1).astype(np.float32), cv2.COLOR_RGB2HSV)
    h, sat = hsv[..., 0][..., None], hsv[..., 1][..., None]
    skin = ((h >= 5) & (h <= 45)).astype(np.float32) * np.clip((0.75 - sat) / 0.2, 0, 1) * np.clip(sat / 0.1, 0, 1)
    skin = cv2.GaussianBlur(skin[..., 0], (0, 0), 2)[..., None]
    f = P.get('sat', 1.0) * (1 + P.get('vib', 0.0) * (1 - np.clip(chroma / 0.6, 0, 1)))
    f = 1 + (f - 1) * (1 - skin * P.get('skin_protect', 0.7))
    s = Ys + (s - Ys) * f
    return Image.fromarray((np.clip(s, 0, 1) * 255 + 0.5).astype(np.uint8))


def sharpen(im, amt, rad):
    return im.filter(ImageFilter.UnsharpMask(radius=rad, percent=int(amt), threshold=2)) if amt else im


def slug(t, n=40):
    return re.sub(r'[^a-z0-9]+', '-', (t or '').lower()).strip('-')[:n].strip('-') or 'photo'


def main():
    ap = add_work_arg(argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter))
    ap.add_argument('--alternates', action='store_true')
    ap.add_argument('--ids')
    ap.add_argument('--draft', action='store_true')
    ap.add_argument('--reauto', action='store_true')
    ap.add_argument('--force', action='store_true')
    A = ap.parse_args()
    W = work_dir(A.work)
    cfg = load_config(W)
    pk = os.path.join(W, 'picks.json')
    if not os.path.exists(pk):
        sys.exit('no picks.json; run curate.py (or write it by hand)')
    P = json.load(open(pk))
    inv = {r['id']: r for r in read_csv(os.path.join(W, 'inventory.csv'))}
    taste = {r['id']: r for r in read_csv(os.path.join(W, 'taste.csv'))}
    jobs = [('pick', i, p) for i, p in enumerate(sorted(P.get('picks', []), key=lambda p: p.get('rank', 999)), 1)]
    if A.alternates or A.ids:
        jobs += [('alternate', i, p) for i, p in enumerate(P.get('alternates', []), 1)]
    if A.ids:
        want = {resolve_id(W, x.strip()) for x in A.ids.split(',')}
        jobs = [j for j in jobs if resolve_id(W, j[2].get('id') or j[2].get('code')) in want]
    for d in ('edits', 'edited/web', 'edited/before', 'edited/alternates'):
        os.makedirs(os.path.join(W, d), exist_ok=True)
    mpath = os.path.join(W, 'edited', 'manifest.json')
    manifest = json.load(open(mpath)) if os.path.exists(mpath) else {}
    for kind, n, p in jobs:
        rid = resolve_id(W, p.get('id') or p.get('code'))
        r = inv.get(rid)
        if not r:
            print('unknown id', rid)
            continue
        src = r['raw_path'] or r['path']
        if not os.path.exists(src):
            src = r['path']
        if not os.path.exists(src):
            print('source not reachable (drive unplugged?)', src)
            continue
        ppath = os.path.join(W, 'edits', f'{rid}.json')
        par = json.load(open(ppath)) if os.path.exists(ppath) else None
        if par is None or (A.reauto and par.get('_auto', True)):
            crop = p.get('crop') or taste.get(rid, {}).get('crop')
            im, _ = load(src, False)
            par = {**auto_params(im, cfg['aspect'], crop if crop in ASPECTS or crop == 'native' else None,
                                 r['role'] == 'edited_output' and not r['raw_path'], ext_of(src) in RAW_EXT), 'src': src}
            write_json(ppath, par)
        title = p.get('title') or taste.get(rid, {}).get('title') or rid[:8]
        full_name = (f'{n:02d}-{slug(title)}.jpg' if kind == 'pick' else f'alternates/{slug(title)}-{rid[:6]}.jpg')
        sig = hashlib.sha1(json.dumps([par, src, A.draft, full_name], sort_keys=True).encode()).hexdigest()[:12]
        prev = manifest.get(rid, {})
        outs = [os.path.join(W, 'edited', 'web', f'{rid}.jpg'), os.path.join(W, 'edited', 'before', f'{rid}.jpg')]
        if not A.draft:
            outs.append(os.path.join(W, 'edited', full_name))
        if not A.force and prev.get('sig') == sig and all(os.path.exists(o) for o in outs):
            print(f'cached  {kind} {n:2d} {title}')
            continue
        if prev.get('full') and prev['full'] != full_name and os.path.exists(os.path.join(W, 'edited', prev['full'])):
            os.remove(os.path.join(W, 'edited', prev['full']))   # rank or title changed
        im, icc = load(src, not A.draft)
        base = geometry(im, par)
        out = sharpen(grade(base, par, not A.draft), par.get('sharp_amt', 50), par.get('sharp_rad', 1.2))
        kw = dict(quality=92, icc_profile=SRGB if ext_of(src) in RAW_EXT else icc, exif=exif_bytes(src, r['path']), subsampling=0)
        web = lambda x: x.resize((round(x.width * WEB / max(x.size)), round(x.height * WEB / max(x.size))), Image.LANCZOS) if max(x.size) > WEB else x
        sharpen(web(out), 35, 0.7).save(outs[0], quality=88, icc_profile=kw['icc_profile'])
        web(base).save(outs[1], quality=85, icc_profile=kw['icc_profile'])
        if not A.draft:
            out.save(outs[2], **kw)
        manifest[rid] = {'kind': kind, 'title': title, 'full': None if A.draft else full_name,
                         'web': f'edited/web/{rid}.jpg', 'before': f'edited/before/{rid}.jpg', 'source': src,
                         'size': list(out.size), 'params': {k: v for k, v in par.items() if k != 'src'}, 'sig': sig}
        write_json(mpath, manifest)
        print(f'edited  {kind} {n:2d} {title}  rot {par["rotate"]:+.2f}  ev {par["ev"]:+.2f}  '
              f'wb {par["temp"]:+.3f}/{par["tint"]:+.3f}  {out.size[0]}x{out.size[1]}  <- {os.path.basename(src)}')
    print(f'edited files in {os.path.join(W, "edited")}')


if __name__ == '__main__':
    main()
