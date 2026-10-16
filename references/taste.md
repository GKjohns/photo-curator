# Taste rubric and curation rules

Used by `taste.py` (per photo) and `curate.py` (the set), and by Claude when curating by eye.

## Per-photo rubric (integers 1-10)
Grade hard: 5 is an ordinary competent frame, 7 is a keeper, 9+ is rare. Use the full range and don't cluster.
- **composition**: structure, subject placement, background, edges, negative space
- **light_color**: quality of light, colour harmony, tonal range
- **moment_emotion**: expression, gesture, story, mood
- **technical**: focus where it matters, noise, exposure, enough pixels for the goal
- **uniqueness**: not a generic snapshot; something only this photographer could have made
- **impact**: holds attention at the size it will be seen
- **overall**: the editor's gut call for this goal, not an average. Keeper means overall ≥ 7 with no exclusion.

## Goal adjustments
- **wall prints**: reads across a room, holds up at 11x14+, works next to the other picks. Check pixels: about 3,000 px long side for 8x10, 4,800+ for 16x20.
- **portfolio**: craft and a point of view, plus range. Penalise competent but generic frames hard.
- **instagram**: reads instantly at phone size and survives a 4:5 crop. Strong subject, clean background.
- **album**: moments and people first. Technical flaws matter less than a real expression.
- Anything else: take the user's words literally and say how you read them.

## Building the set (curator)
1. Quality first: start from the highest overall scores and look at every candidate yourself.
2. No near-twins: of two similar frames, keep the stronger one only.
3. Spread: no more than ~25% of picks from any one shoot unless it's clearly the strongest work, and a mix of subjects and scales (wide, mid, detail; people and places).
4. Coherence: the set should look like one body of work for the goal, with a few anchors that could stand alone.
5. Respect the run's exclusions and people settings exactly. When unsure whether an excluded person is in a frame, leave the frame out.
6. Rank picks best first. Each gets a short title, two specific sentences on why it was picked and its role in the set, a crop and a short edit plan. Each alternate gets one sentence on why it nearly made it. Notes say how the set is balanced and what's missing.
