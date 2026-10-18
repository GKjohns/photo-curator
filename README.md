# photo-curator

A [Claude Code](https://claude.com/claude-code) skill that goes through a drive, camera card or folder, picks the best photos, edits them, and shows them to you in a local gallery.

Say "find the good photos, edit them, and show me" and you get a set of edited picks, plus alternates, close calls and the reasoning behind each one.

## How it works

Each stage looks at fewer photos than the one before it, so the expensive models only see what survived the cheap checks.

1. **Local culling.** Inventory the source, pair RAW+JPEG siblings, collapse copies, and group photos into shoots. Measure sharpness, exposure, clipping and faces, cluster near-duplicate bursts, and drop blurry, badly exposed and duplicate frames. No model calls.
2. **Cheap tagging.** A small vision model tags every survivor (scene, subjects, a 1-10 keeper score), eight images per call at 512px.
3. **Rubric scoring.** A Sonnet-class model scores the top ~300 one at a time at 1024px on six criteria: composition, light, moment, technical, uniqueness and impact.
4. **Curator pass.** A top-tier model sees the ~60-photo shortlist together in one call, picks the final set plus alternates, and balances it across shoots and subjects.
5. **RAW develop.** Picks are developed from RAW when one exists: auto straighten, white balance, exposure, gentle contrast and vibrance, sharpening and crop.
6. **Viewer.** A single self-contained `viewer.html` with a before/after slider, scores and the curator's reasoning. No server needed.

Every stage caches its results per photo by content hash. Reruns are free and an interrupted run picks up where it stopped.

## Install

```
git clone https://github.com/GKjohns/photo-curator ~/.claude/skills/photo-curator
cd ~/.claude/skills/photo-curator
python3 -m venv .venv && .venv/bin/pip install -r scripts/requirements.txt
```

Requirements: Python 3.10+, with numpy, Pillow, OpenCV, rawpy (RAW) and pillow-heif (HEIC) from `requirements.txt`. Optional: `exiftool` for RAW capture dates (`brew install exiftool`).

For the model stages, set `AI_GATEWAY_API_KEY` (a [Vercel AI Gateway](https://vercel.com/ai-gateway) key). Without one, the pipeline stops after local culling and writes contact sheets, and Claude curates from those by eye.

## Use

Ask Claude something like "go through my SD card and find the best 15 for prints", or run it directly:

```
.venv/bin/python scripts/inventory.py --detect                       # find drives and cards
.venv/bin/python scripts/pipeline.py /Volumes/CARD --work ~/Pictures/curated --picks 15 --goal "wall prints"
open ~/Pictures/curated/viewer.html
```

To see the viewer without running anything, open `viewer/sample/viewer.html`. To try the whole pipeline with no photos and no API calls, run `.venv/bin/python scripts/make_sample.py --work /tmp/pc-sample`, which uses procedurally generated images.

`SKILL.md` has the full workflow. `references/` covers stage details, file formats, the rubric, edit parameters and cost.

## Cost

Model calls go through the AI Gateway, so each model is a swappable string in the config. A library of about 5,000 photos typically costs a few dollars, most of it in the rubric pass (about a cent per photo for the few hundred that reach it). `budget_usd` in the run config is a hard cap (default $10), and reruns cost nothing.

## Privacy

Everything runs locally except the optional model calls, which send downsized thumbnails (512-1024px) to the gateway and nothing else. The skill only reads from your drive or card and writes all output to the work folder you choose. Exclusions ("skip client work", "skip shoots named X") and optional people-of-interest references are set per run in that folder's `config.json` and stored nowhere else.

## Related

[camera-portrait-edit](https://github.com/GKjohns/camera-portrait-edit) turns one shoot into finished Instagram portraits with style presets.

## License

MIT
