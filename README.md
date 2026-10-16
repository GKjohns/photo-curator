# photo-curator

A [Claude Code](https://claude.com/claude-code) skill that finds the good photos. Plug in a drive or camera card (or point it at a folder), say "find the good photos, edit them, and show me", and you get a local gallery of edited picks chosen with taste, with alternates, close calls and the reasons for each.

It works like a photo editor with an assistant. Cheap local checks go first: technical metrics, blur and exposure, near-duplicate bursts, and RAW+JPEG pairing. Vision models then look at shrinking sets: a cheap model tags every survivor, a Sonnet-class model scores the best few hundred on a six-part rubric (composition, light, moment, technical, uniqueness, impact), and a top-tier curator sees the shortlist together and picks a balanced set across shoots and subjects. The picks are developed from RAW where possible (auto straighten, white balance, exposure, gentle contrast and vibrance, sharpening, crop) and shown in a self-contained `viewer.html` with a before/after slider, scores and the curator's reasoning. Every stage is cached per photo by content hash, so reruns are free and an interrupted run resumes.

## Install

```
git clone https://github.com/GKjohns/photo-curator ~/.claude/skills/photo-curator
cd ~/.claude/skills/photo-curator
python3 -m venv .venv && .venv/bin/pip install -r scripts/requirements.txt
```

Requirements: Python 3.10+, with numpy, Pillow, OpenCV, rawpy (RAW) and pillow-heif (HEIC) from `requirements.txt`. Optional: `exiftool` for RAW capture dates (`brew install exiftool`). For the model stages, set `AI_GATEWAY_API_KEY` (a [Vercel AI Gateway](https://vercel.com/ai-gateway) key). Without one, Claude looks at contact sheets itself and curates by eye.

## Use

Ask Claude something like "go through my SD card and find the best 15 for prints", or run it directly:

```
.venv/bin/python scripts/inventory.py --detect                       # find drives and cards
.venv/bin/python scripts/pipeline.py /Volumes/CARD --work ~/Pictures/curated --picks 15 --goal "wall prints"
open ~/Pictures/curated/viewer.html
```

`scripts/make_sample.py --work /tmp/pc-sample` runs the whole pipeline on procedurally generated images, with no photos and no API calls. To see the viewer straight away, open `viewer/sample/viewer.html`.

See `SKILL.md` for the workflow and `references/` for stage details, file formats, the rubric, edit params and cost.

## Cost

Model calls go through the AI Gateway, so the models are swappable strings. A library of about 5,000 photos typically costs a few dollars, most of it in the rubric pass (about a cent per photo for the few hundred that reach it). `budget_usd` in the run config is a hard cap (default $10), and reruns cost nothing.

## Privacy

Everything runs locally except the optional model calls. Those send downsized thumbnails (512-1024px) to the gateway and nothing else. No uploads, no accounts beyond the optional gateway key. The skill only reads from your drive or card; all output goes to the work folder you choose. Exclusions ("skip client work", "skip shoots named X") and optional people-of-interest references are set per run in that folder's `config.json` and never stored anywhere else.

Related: [camera-portrait-edit](https://github.com/GKjohns/camera-portrait-edit) turns one shoot into finished Instagram portraits with style presets. Open source under MIT.
