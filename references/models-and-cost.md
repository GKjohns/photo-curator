# Models, gateway and cost

## How calls are made
All model calls go through the Vercel AI Gateway's OpenAI-compatible endpoint (`https://ai-gateway.vercel.sh/v1/chat/completions`, override with `AI_GATEWAY_URL`) using one `AI_GATEWAY_API_KEY`. The model is just a `provider/model` string, so any vision model the gateway serves can be swapped in. Each call asks for structured output (`response_format: json_schema`, strict). If a provider rejects that, the client falls back to `json_object` and then to plain JSON parsing. It retries 429/5xx with backoff, retries truncated replies with a larger `max_tokens`, and logs cost from the gateway's `usage.cost` into `costs.jsonl`. The client is stdlib `urllib` in `scripts/common.py`, so there's no SDK dependency.

## Default tiers
| stage | default model | what it sees | why this tier |
|---|---|---|---|
| tag | `openai/gpt-6-luna` | every candidate, 8 per call, 512px | about $0.0001-0.0002 per photo; good enough to sort obvious keepers from snapshots and to flag exclusions |
| taste | `anthropic/claude-sonnet-5.5` | top `taste_top` (300), one per call, 1024px | real critique and calibrated scores; about $0.01 per photo |
| curator | `anthropic/claude-opus-5.5` | the `shortlist` (60) together at 512px, one call | needs to compare frames side by side and balance a set; one request |

Override per run in `config.json` → `models`, or with env `PHOTO_CURATOR_MODEL_TAG`, `..._TASTE`, `..._CURATOR`. Both `claude-sonnet-5.5` and `claude-sonnet-5-5` id forms work on the gateway. A GPT-6 model used for the rubric needs no reasoning setting; if you add one, there is no `minimal` level.

## Budget
- `budget_usd` (default 10) is checked against the ledger before every call. When it's reached, the stage stops cleanly and later stages work with what was scored.
- Rough cost for N photos after triage: `N x 0.00015 + taste_top x 0.011 + ~0.5` dollars. For 5,000 photos with the defaults, that's about $4-5. Halving `taste_top` roughly halves the bill.
- Reruns are free: every answer is cached per photo (and per shortlist for the curator) under a key that includes the model and prompt.
- To preview cost before spending, run through `triage`, read the candidate count, and apply the formula.

## No key: Claude as the model
Without `AI_GATEWAY_API_KEY` the model stages are skipped and Claude does the looking itself, in the session:
1. `pipeline.py` stops after triage and runs `sheets.py`: the best ~240 candidates by technical score (per-shoot cap), 30 per sheet, each labelled with a code.
2. Claude (or subagents, one sheet each, returning codes + one-line notes) views the sheets and keeps ~3x the target.
3. `sheets.py --codes ... --big` renders the shortlist at 1024px, 12 per sheet, for a closer look.
4. Claude writes `picks.json` using the rubric in `taste.md`, then `pipeline.py --from edit`.

This uses the Claude Code session's own usage rather than gateway credit and works well up to a few thousand candidates. Beyond that, raise the cut with `sheets.py --top`, or review shoot by shoot (`--shoot`).
