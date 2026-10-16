# Pipeline reference

All paths are inside the work dir (`--work`, or `$PHOTO_CURATOR_DIR`). Every stage reads the previous stage's CSV and can be rerun on its own.

| stage | script | reads | writes | cost |
|---|---|---|---|---|
| inventory | `inventory.py SRC...` | the source | `inventory.csv`, `shoots.csv`, `cache/inventory_index.json` | local |
| metrics | `metrics.py` | inventory | `thumbs/xx/<id>_{512,1024}.jpg`, `cache/metrics/`, `metrics.csv`, `dupes.csv` | local |
| triage | `triage.py` | inventory, metrics, dupes, config | `triage.csv` (every photo + reject reason), `candidates.csv` | local |
| tag | `tag.py` | candidates | `cache/tag/`, `tags.csv` | cheap model |
| taste | `taste.py` | candidates, tags | `cache/taste/`, `taste.csv` | Sonnet-class |
| curate | `curate.py` | taste | `curate_in.json`, `sheets/shortlist_NN.jpg`, `codes.json`, `cache/curator/`, `picks.json` | one top-tier call |
| sheets | `sheets.py` | candidates | `sheets/review_NN.jpg` or `big_NN.jpg`, `codes.json` | local |
| edit | `edit.py` | picks, inventory | `edits/<id>.json`, `edited/` | local |
| index | `build_index.py` | everything | `index.json`, `viewer.html` | local |

`pipeline.py` chains them; `--from STAGE` resumes. `make_sample.py` builds a synthetic run (procedural images, stand-in scores) for testing.

## Identity and caching
- **id** = 16-hex blake2b over file size + three 256 KB samples (start, middle, end). It stays the same when a file is copied, renamed or moved to another drive, and it's fast on 50 MB RAW files.
- Inventory remembers path + size + mtime → id, so unchanged files are never re-hashed.
- Per-photo caches live in `cache/<stage>/<id[:2]>/<id>.json`. Model caches carry a `_v` key (hash of model + prompt + the run's exclusions/people), so a cached answer is reused only if those are unchanged. Changing the goal or exclusions re-scores; rerunning doesn't.
- `costs.jsonl` is the spend ledger (one line per call: stage, model, images, tokens, cost).

## Inventory rules
- Extensions: jpg jpeg png tif tiff heic heif webp + RAW (arw cr2 cr3 nef nrw raf orf rw2 dng pef srw x3f 3fr iiq). Files under 50 KB, hidden files and `.Trashes`-style dirs are skipped.
- **Pairing**: same folder + same stem → one photo. `path` = the JPEG/HEIC (fast previews and metrics), `raw_path` = the RAW (used for editing). RAW-only photos use the embedded preview for thumbnails.
- **Shoots**: first sub-folder under the source (`source/2024_lake_trip/...` → `source/2024_lake_trip`). Camera-card folders (`DCIM/100XXXXX`) and loose files are split by capture-time gaps (`--gap-hours`, default 3).
- **role**: `edited_output` if any parent folder is named like an export (edited, export, final, ready, selects, instagram, ...). Those stay candidates, since a photographer's own exports are often their best work. The edit stage only crops and sharpens them unless a RAW is paired.

## Triage rules (`triage.csv` reject column)
`excluded:<pattern>` › `error` › `small` (long side < `min_long_side`) › `dupe` (not the cluster winner) › `exposure` (no RAW and >12% blown, >35% crushed, or mean luma outside 0.06-0.94) › `blurry` (peak tile sharpness below half of `--blur-floor`, or in the shoot's bottom `--blur-pct` and below the floor). Peak sharpness looks at the sharpest 10% of a 6x6 tile grid, so a sharp subject against a smooth sky passes. For minimalist or foggy work, pass `--blur-floor 40`. Near-duplicates: pHash Hamming ≤ 6 within a shoot; the winner has the highest technical score.

## config.json
```json
{
  "picks": 12, "alternates": 8, "goal": "wall prints",
  "exclude_patterns": ["client_", "/(wedding|corporate)/"],
  "exclude_descriptions": ["client portrait sessions", "screenshots"],
  "people": [{"name": "Sam", "ref": "/path/to/sam.jpg", "want": "prefer"}],
  "aspect": "native", "budget_usd": 10, "min_long_side": 1600,
  "taste_top": 300, "shortlist": 60, "per_shoot_cap": 0.25,
  "models": {"tag": "openai/gpt-6-luna", "taste": "anthropic/claude-sonnet-5.5", "curator": "anthropic/claude-opus-5.5"}
}
```
- `exclude_patterns`: substrings or `/regex/`, matched against path + shoot, applied locally before any model sees anything. This is the reliable way to skip client work or whole shoots.
- `exclude_descriptions`: plain-language rules the vision models apply ("photos of my ex", "anything with a license plate"). Best effort, so prefer patterns where you can.
- `people`: optional, per run, set by the user. `want` is `prefer` (small boost), `include` (just recognise), or `exclude` (drop). A `ref` image lets the models match faces. Leave it empty for no identity work at all.
- `per_shoot_cap`: max share of any selection (rubric pool, shortlist, picks) from one shoot.
- `aspect`: default crop for edits. The curator's per-photo `crop` wins. Aspects follow the frame's orientation, so `4:5` on a landscape frame means 5:4.

## picks.json
```json
{
  "goal": "wall prints", "curator": "manual", "notes": "How the set is balanced.",
  "picks": [{"id": "S07", "rank": 1, "title": "Short title", "why": "Two specific sentences.",
             "crop": "native", "edit_plan": "Lift shadows, cool slightly",
             "scores": {"composition": 8, "light_color": 7, "moment_emotion": 9, "technical": 7, "uniqueness": 8, "impact": 8, "overall": 8}}],
  "alternates": [{"id": "C112", "why": "Nearly made it because..."}],
  "close": [{"id": "9f2c1a...", "why": "..."}]
}
```
`id` may be a content-hash id or a sheet code (`S07`, `C112`; see `codes.json`). `scores` is optional; when it's omitted, rubric scores from `taste.csv` are used. When `close` is omitted, the best-scoring non-picks fill it.

## index.json / viewer.html
`build_index.py` merges inventory, metrics, tags, taste, picks and `edited/manifest.json` into `index.json` and inlines it into `viewer.html` (template: `viewer/template.html`). Image paths are relative to the work dir, so the page works from disk. `--export DIR [--no-full]` writes a portable copy holding only the referenced images; `viewer/sample/` was made this way from `make_sample.py`.
