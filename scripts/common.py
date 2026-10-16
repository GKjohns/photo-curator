"""Shared helpers for the photo-curator pipeline: work dir, config, hashing, caches,
image loading (JPEG/PNG/TIFF/HEIC/RAW), contact sheets and the AI Gateway client.

Every script takes --work DIR (or env PHOTO_CURATOR_DIR). Nothing is hardcoded.
"""
import base64, csv, hashlib, io, json, os, subprocess, sys, tempfile, time, urllib.error, urllib.request

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageOps

Image.MAX_IMAGE_PIXELS = None
try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except Exception:
    pass

RAW_EXT = {'arw', 'cr2', 'cr3', 'nef', 'nrw', 'raf', 'orf', 'rw2', 'dng', 'pef', 'srw', 'x3f', '3fr', 'iiq'}
STD_EXT = {'jpg', 'jpeg', 'png', 'tif', 'tiff', 'heic', 'heif', 'webp'}
IMG_EXT = RAW_EXT | STD_EXT
GATEWAY_URL = os.environ.get('AI_GATEWAY_URL', 'https://ai-gateway.vercel.sh/v1/chat/completions')

DEFAULTS = {
    'picks': 12,                 # how many final picks
    'alternates': 8,
    'goal': 'wall prints',       # wall prints | portfolio | instagram | album | free text
    'exclude_patterns': [],      # case-insensitive substrings or /regex/ on path + shoot name
    'exclude_descriptions': [],  # natural language, judged by the vision models, e.g. "client work"
    'people': [],                # optional people of interest: {"name", "ref": "/path/face.jpg", "want": "prefer|include|exclude"}
    'aspect': 'native',          # crop for edits: native | 4:5 | 3:2 | 2:3 | 1:1 | 16:9 | 5:7 | 8:10
    'budget_usd': 10.0,          # hard stop for gateway spend across all stages
    'models': {
        'tag': 'openai/gpt-6-luna',
        'taste': 'anthropic/claude-sonnet-5.5',
        'curator': 'anthropic/claude-opus-5.5',
    },
    'min_long_side': 1600,       # drop tiny files (screenshots, web exports)
    'taste_top': 300,            # how many tagged survivors get the rubric pass
    'shortlist': 60,             # how many rubric survivors the curator sees
    'per_shoot_cap': 0.25,       # max share of any stage's selection from one shoot
}


# ---------- work dir, config, csv/json io ----------
def work_dir(path=None):
    w = path or os.environ.get('PHOTO_CURATOR_DIR')
    if not w:
        sys.exit('set --work DIR or PHOTO_CURATOR_DIR')
    w = os.path.abspath(os.path.expanduser(w))
    os.makedirs(w, exist_ok=True)
    return w


def add_work_arg(ap):
    ap.add_argument('--work', help='output/work dir (default: $PHOTO_CURATOR_DIR)')
    return ap


def load_config(W):
    cfg = json.loads(json.dumps(DEFAULTS))
    p = os.path.join(W, 'config.json')
    if os.path.exists(p):
        user = json.load(open(p))
        for k, v in user.items():
            if k == 'models' and isinstance(v, dict):
                cfg['models'].update(v)
            elif not k.startswith('_'):
                cfg[k] = v
    for stage in ('tag', 'taste', 'curator'):   # env overrides, e.g. PHOTO_CURATOR_MODEL_TASTE
        e = os.environ.get(f'PHOTO_CURATOR_MODEL_{stage.upper()}')
        if e:
            cfg['models'][stage] = e
    return cfg


def read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, newline='', encoding='utf-8') as f:
        return list(csv.DictReader(f))


def write_csv(path, rows, cols=None):
    cols = cols or (list(rows[0].keys()) if rows else [])
    tmp = path + '.tmp'
    with open(tmp, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, cols, extrasaction='ignore')
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, path)


def write_json(path, obj):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(obj, f, indent=1, ensure_ascii=False)
    os.replace(tmp, path)


def fnum(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


# ---------- content hashing + per-image caches ----------
def content_hash(path, chunk=1 << 18):
    """Fast content hash: file size + three 256 KB samples (start, middle, end).
    Stable across copies, renames and drives; reading whole RAW files would be too slow."""
    st = os.stat(path)
    h = hashlib.blake2b(digest_size=8)
    h.update(str(st.st_size).encode())
    with open(path, 'rb') as f:
        for off in (0, max(0, st.st_size // 2 - chunk // 2), max(0, st.st_size - chunk)):
            f.seek(off)
            h.update(f.read(chunk))
    return h.hexdigest()


def cache_path(W, stage, rid):
    d = os.path.join(W, 'cache', stage, rid[:2])
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, rid + '.json')


def cache_get(W, stage, rid, version=None):
    p = cache_path(W, stage, rid)
    if not os.path.exists(p):
        return None
    try:
        d = json.load(open(p))
    except Exception:
        return None
    if version is not None and d.get('_v') != version:
        return None
    return d


def cache_put(W, stage, rid, obj, version=None):
    if version is not None:
        obj = {**obj, '_v': version}
    write_json(cache_path(W, stage, rid), obj)


def version_key(*parts):
    return hashlib.sha1(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:12]


# ---------- image loading ----------
def ext_of(path):
    return os.path.splitext(path)[1][1:].lower()


def _flatten(im):
    if im.mode in ('RGBA', 'LA', 'P'):
        im = im.convert('RGBA')
        bg = Image.new('RGB', im.size, (255, 255, 255))
        bg.paste(im, mask=im.split()[3])
        return bg
    if im.mode in ('I;16', 'I;16B', 'I', 'F'):
        a = np.asarray(im).astype(np.float32)
        return Image.fromarray((a / max(float(a.max()), 1) * 255).astype(np.uint8)).convert('RGB')
    return im.convert('RGB')


def raw_preview(path):
    """Embedded JPEG from a RAW file (fast). Falls back to a half-size rawpy develop."""
    try:
        import rawpy
    except ImportError:
        rawpy = None
    if rawpy is not None:
        try:
            with rawpy.imread(path) as r:
                flip = r.sizes.flip
                try:
                    t = r.extract_thumb()
                    if t.format == rawpy.ThumbFormat.JPEG:
                        im = Image.open(io.BytesIO(t.data))
                        im.load()
                        if max(im.size) >= 1000:
                            if not im.getexif().get(0x0112):
                                im = im.rotate({3: 180, 5: 90, 6: -90}.get(flip, 0), expand=True)
                            return ImageOps.exif_transpose(im).convert('RGB')
                except Exception:
                    pass
                rgb = r.postprocess(half_size=True, use_camera_wb=True, output_bps=8)
                return Image.fromarray(rgb)
        except Exception:
            pass
    for tag in ('JpgFromRaw', 'PreviewImage'):   # exiftool fallback when rawpy is missing
        try:
            b = subprocess.run(['exiftool', '-b', '-' + tag, path], capture_output=True, timeout=60).stdout
            if len(b) > 1000:
                im = Image.open(io.BytesIO(b))
                im.load()
                return ImageOps.exif_transpose(im).convert('RGB')
        except Exception:
            continue
    raise RuntimeError('cannot read RAW (pip install rawpy, or install exiftool)')


def open_image(path):
    """Oriented RGB PIL image for any supported file (RAW via embedded preview)."""
    if ext_of(path) in RAW_EXT:
        return raw_preview(path)
    try:
        im = Image.open(path)
        im.load()
    except Exception:
        if ext_of(path) in ('heic', 'heif') and sys.platform == 'darwin':
            with tempfile.TemporaryDirectory() as d:
                out = os.path.join(d, 'x.jpg')
                subprocess.run(['sips', '-s', 'format', 'jpeg', path, '--out', out], capture_output=True, timeout=120)
                im = Image.open(out)
                im.load()
        else:
            raise
    return _flatten(ImageOps.exif_transpose(im))


def resized(im, n):
    w, h = im.size
    s = n / max(w, h)
    if s >= 1:
        return im.copy()
    return im.resize((max(1, round(w * s)), max(1, round(h * s))), Image.LANCZOS)


def thumb_path(W, rid, size):
    return os.path.join(W, 'thumbs', rid[:2], f'{rid}_{size}.jpg')


def data_url(im, q=85):
    b = io.BytesIO()
    im.convert('RGB').save(b, 'JPEG', quality=q)
    return 'data:image/jpeg;base64,' + base64.b64encode(b.getvalue()).decode()


# ---------- contact sheets ----------
def _font(size):
    for f in ('/System/Library/Fonts/Helvetica.ttc', '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
              'C:/Windows/Fonts/arial.ttf'):
        try:
            return ImageFont.truetype(f, size)
        except Exception:
            continue
    return ImageFont.load_default()


def contact_sheet(items, out, cols=6, cell=320, label_h=26):
    """items: [(label, image_path)]. Writes one JPEG sheet with a label under each cell."""
    rows = (len(items) + cols - 1) // cols
    sheet = Image.new('RGB', (cols * cell, rows * (cell + label_h)), (24, 24, 24))
    d = ImageDraw.Draw(sheet)
    font = _font(int(label_h * 0.7))
    for k, (label, p) in enumerate(items):
        x, y = (k % cols) * cell, (k // cols) * (cell + label_h)
        try:
            im = Image.open(p).convert('RGB')
            im.thumbnail((cell - 8, cell - 8))
            sheet.paste(im, (x + (cell - im.width) // 2, y + 4 + (cell - 8 - im.height) // 2))
        except Exception:
            pass
        d.text((x + 6, y + cell), label, fill=(240, 240, 240), font=font)
    sheet.save(out, quality=85)


# ---------- AI Gateway ----------
def gateway_key():
    return os.environ.get('AI_GATEWAY_API_KEY', '').strip()


def ledger_add(W, stage, model, usage, cost, n_images):
    with open(os.path.join(W, 'costs.jsonl'), 'a') as f:
        f.write(json.dumps({'ts': time.strftime('%Y-%m-%dT%H:%M:%S'), 'stage': stage, 'model': model,
                            'images': n_images, 'prompt_tokens': usage.get('prompt_tokens'),
                            'completion_tokens': usage.get('completion_tokens'), 'cost': cost}) + '\n')


def spent(W, stage=None):
    p = os.path.join(W, 'costs.jsonl')
    tot = 0.0
    if os.path.exists(p):
        for line in open(p):
            try:
                d = json.loads(line)
                if stage is None or d.get('stage') == stage:
                    tot += float(d.get('cost') or 0)
            except Exception:
                pass
    return tot


def _parse_json(t):
    t = t.strip()
    if t.startswith('```'):
        t = t.strip('`')
        t = t[t.find('{'):] if '{' in t else t
    try:
        return json.loads(t)
    except Exception:
        return json.loads(t[t.index('{'):t.rindex('}') + 1])


def call_model(model, system, content, schema, max_tokens=1500, timeout=180):
    """One chat completion through the AI Gateway's OpenAI-compatible endpoint with structured output.
    content: list of OpenAI content parts (text / image_url). Returns (obj, usage, cost_usd)."""
    key = gateway_key()
    if not key:
        raise RuntimeError('AI_GATEWAY_API_KEY is not set')
    body = {'model': model, 'max_tokens': max_tokens,
            'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': content}],
            'response_format': {'type': 'json_schema',
                                'json_schema': {'name': schema.get('title', 'result'), 'strict': True, 'schema': schema}}}
    fallbacks = [{'type': 'json_object'}, None]
    err = None
    for attempt in range(6):
        req = urllib.request.Request(GATEWAY_URL, data=json.dumps(body).encode(), method='POST',
                                     headers={'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                j = json.loads(r.read())
            msg = j['choices'][0]['message'].get('content') or ''
            obj = _parse_json(msg)
            u = j.get('usage') or {}
            cost = u.get('cost') or (u.get('cost_details') or {}).get('upstream_inference_cost') or 0
            return obj, u, float(cost)
        except urllib.error.HTTPError as e:
            txt = e.read().decode(errors='replace')[:400]
            err = f'HTTP {e.code}: {txt}'
            if e.code == 400 and 'response_format' in body and fallbacks and ('response_format' in txt or 'schema' in txt):
                rf = fallbacks.pop(0)
                if rf:
                    body['response_format'] = rf
                else:
                    body.pop('response_format')
                continue
            if e.code in (408, 409, 429, 500, 502, 503, 504):
                time.sleep(min(60, 2 ** attempt * 2))
                continue
            break
        except (ValueError, KeyError) as e:   # truncated or non-JSON reply: retry with more room
            err = f'parse error: {e}'
            body['max_tokens'] = int(body['max_tokens'] * 1.5)
        except Exception as e:
            err = f'{type(e).__name__}: {e}'
            time.sleep(min(60, 2 ** attempt * 2))
    raise RuntimeError(err or 'gateway call failed')


def people_refs(cfg):
    """Content parts for optional people-of-interest reference images (configured per run by the user)."""
    parts, lines = [], []
    for p in cfg.get('people') or []:
        name, want = p.get('name', 'person'), p.get('want', 'prefer')
        lines.append(f'- {name}: {want}' + (f' ({p["note"]})' if p.get('note') else ''))
        ref = p.get('ref')
        if ref and os.path.exists(os.path.expanduser(ref)):
            im = open_image(os.path.expanduser(ref))
            im.thumbnail((512, 512))
            parts += [{'type': 'text', 'text': f'REFERENCE: {name} (want: {want})'},
                      {'type': 'image_url', 'image_url': {'url': data_url(im)}}]
    return parts, '\n'.join(lines)


def exclusion_text(cfg):
    ex = cfg.get('exclude_descriptions') or []
    people = [p['name'] for p in cfg.get('people') or [] if p.get('want') == 'exclude']
    ex = ex + [f'any photo showing {n}' for n in people]
    return '\n'.join(f'- {e}' for e in ex) if ex else '(none)'


def load_codes(W):
    p = os.path.join(W, 'codes.json')
    return json.load(open(p)) if os.path.exists(p) else {}


def save_codes(W, mapping):
    """Short labels (S07, A3-12) printed on contact sheets -> content-hash ids."""
    codes = load_codes(W)
    codes.update(mapping)
    write_json(os.path.join(W, 'codes.json'), codes)


def resolve_id(W, ref, _codes={}):
    if ref is None:
        return None
    if not _codes.get(W):
        _codes[W] = load_codes(W)
    return _codes[W].get(str(ref), str(ref))


def pick_with_cap(rows, n, cap, shoot_key='shoot'):
    """Take the first n rows (already sorted best-first) with at most cap*n from any one shoot,
    then top up from the remainder if the cap left the list short."""
    limit = max(1, int(round(n * cap))) if cap and cap < 1 else n
    out, per, rest = [], {}, []
    for r in rows:
        s = r.get(shoot_key, '')
        if per.get(s, 0) < limit and len(out) < n:
            out.append(r)
            per[s] = per.get(s, 0) + 1
        else:
            rest.append(r)
    for r in rest:
        if len(out) >= n:
            break
        out.append(r)
    return out
