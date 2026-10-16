---
name: photo-curator
description: Find the best photos on a drive, camera card or folder, edit them, and show them in a local viewer. Runs cheap local culling first (blur, exposure, duplicates, RAW+JPEG pairing), then escalates through vision models on shrinking sets (cheap tagging, a taste rubric, a final curator pass that balances variety), develops the picks from RAW, and opens a gallery with before/after and the curator's reasoning. Use when the user says "find the good photos", "cull my shoot", "pick the best shots", "show me the keepers", "go through this drive / camera card", "choose photos to print", or plugs in a drive or SD card and wants the best photos picked and edited.
---

# Photo curator

Scripts live next to this file in `scripts/`; reference docs in `references/`. Every script takes `--work DIR` (the run's output dir) and is cached per photo by content hash, so reruns resume for free.
Setup once: `python3 -m venv .venv && .venv/bin/pip install -r scripts/requirements.txt` (rawpy for RAW, pillow-heif for HEIC are in there). Use `.venv/bin/python` below.

## 1. Find the source (read only)
- `python scripts/inventory.py --detect` lists mounted volumes, camera cards (`DCIM`) and `~/Pictures` with photo counts. Pick the obvious one; ask only if two are equally plausible.
- Never write, rename or delete anything on the source. All output goes to the work dir. Default work dir: `~/Pictures/photo-curator/<source-name>-<YYYY-MM-DD>`.

## 2. Ask one question, then go
Ask once, in one message: roughly how many picks, what they are for (wall prints, portfolio, Instagram, album, or anything), and anything to skip (folders or shoots by name, client work, people they name). Then proceed with defaults (12 picks, wall prints, nothing skipped). Write the answers to `<work>/config.json`; see `examples/config.example.json`. Exclusions and people of interest exist only for this run and only in that file.

## 3. Run the pipeline
```
python scripts/pipeline.py "/Volumes/CARD" --work "$WORK" --picks 12 --goal "wall prints" [--exclude client_ --exclude /wedding/]
```
Stages (details and file formats: `references/pipeline.md`):
1. **inventory**: one row per photo; RAW+JPEG siblings paired, copies across drives collapsed, shoots from folders or capture-time gaps.
2. **metrics** (local): thumbnails, sharpness, exposure, clipping, faces, perceptual hash; near-duplicate bursts clustered, best frame kept.
3. **triage** (local): drop exclusions, tiny files, non-best duplicates, hopeless exposure, blur relative to the shoot.
4. **tag** (cheap vision model, 8 images per call at 512px): scene, subjects, 1-10 keeper score, exclusions the user described in words.
5. **taste** (Sonnet-class, one image per call at 1024px): six-part rubric on the top ~300, per-shoot cap.
6. **curate** (top-tier model, one call): sees the ~60-image shortlist together, picks N + alternates, balances shoots and subjects, ranks, explains.
7. **edit**: develop picks from RAW when present (auto straighten, white balance, exposure, contrast, vibrance, sharpening, crop).
8. **index**: `<work>/viewer.html`.

Model calls go through the Vercel AI Gateway when `AI_GATEWAY_API_KEY` is set. Models are overridable in config or `PHOTO_CURATOR_MODEL_TAG|TASTE|CURATOR`. Stop and tell the user if a stage reports many errors.

## 4. No key? Claude curates by eye
Without a key the pipeline stops after triage and writes contact sheets (`sheets/review_NN.jpg`, 30 labelled thumbs each, best technical frames with a per-shoot cap). Then:
- View the sheets (delegate to a subagent if there are more than ~4; it returns codes + notes, not images).
- Shortlist ~3x the target, `python scripts/sheets.py --work "$WORK" --codes C004,C017,... --big` for a closer look.
- Write `<work>/picks.json` (schema in `references/pipeline.md`; codes are fine as ids), then `python scripts/pipeline.py --work "$WORK" --from edit`.
Apply the same rubric and variety rules as the curator (`references/taste.md`).

## 5. Edit, check, refine
`edit.py` writes `edits/<id>.json` with auto params, then full-res `edited/NN-title.jpg`, a 2048px `edited/web/` copy and an ungraded `edited/before/` copy. Look at the web renders downsized (subagent if more than a few). For any that look off, edit the params (set `"_auto": false`), rerun `python scripts/edit.py --work "$WORK" --ids S03`. Natural, never a heavy filter. Param table and critique checklist: `references/editing.md`.

## 6. Show the user
`python scripts/build_index.py --work "$WORK"` then `open "$WORK/viewer.html"` (`xdg-open` on Linux). It's a single self-contained page, so no server is needed. Picks show large, with alternates and close calls in tabs, plus a lightbox with edited/before-after/original, the curator's reasoning, scores and source paths. Tell the user where the edited JPEGs are and what it cost (`costs.jsonl`). Don't upload or post anything.

## Cost
The cheap model sees everything, the rubric model a few hundred photos, and the curator one request. A ~5,000-photo library typically costs $3-8, almost all in the rubric pass, which is about a cent per photo. `budget_usd` in config (default $10) is a hard stop checked before every call. Lower `taste_top` to spend less. Reruns cost $0. Details: `references/models-and-cost.md`.
