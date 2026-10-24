# Usage Report: Final Full-Dataset Run

This report covers the run that produced the submitted `output.csv`
(`python code/main.py`, 518 requests). The source is
`code/evaluation/usage_summary.json`, which `code/main.py` writes on every full
run, plus the per-image cache files in `code/cache/`.

## Where a model is used

| Pipeline stage | Model? |
|---|---|
| Intent classification (does the request authorize deletion?) | **No.** Multilingual rule lexicon with negation cues (`ghost_audit/intent.py`) |
| Identity resolution, evidence parsing (SQL, JSON, logs, text, PDF) | **No.** Deterministic parsers |
| Policy decisions, state at `audit_at`, remediation planning, step ordering | **No.** Deterministic engine (`ghost_audit/engine.py`) |
| **Screenshot transcription** (11 PNG files) | **Yes.** One vision call per image, result cached on disk |

The model never decides a case. It only turns screenshot pixels into structured
records (ids, identifiers, on-screen status, labelled fields), which then go
through the same rules as every other evidence file.

## Model calls and tokens

| Provider | Model | Purpose | Calls | Input tokens | Output tokens | Total tokens |
|---|---|---|---|---|---|---|
| Google Gemini (free tier) | `gemini-3.8-flash` | Screenshot vision | 11 | 15,261 | 9,892 | **25,153** |
| Anthropic | not used in this run | - | 0 | 0 | 0 | 0 |
| **Overall** | | | **11** | **15,261** | **9,892** | **25,153** |

| Average | Value |
|---|---|
| Tokens per screenshot (per model call) | 2,287 (1,387 in / 899 out) |
| Tokens per request (25,153 / 518) | 48.6 |
| Model calls per request | 0.021 |

### Per screenshot

| Evidence | File | Input | Output | Records transcribed |
|---|---|---|---|---|
| evidence_04 | account_admin_console_20260926.png | 1,387 | 1,123 | 4 |
| evidence_09 | search_index_inspector_20260901.png | 1,399 | 765 | 2 |
| evidence_14 | connector_management_console_20260807.png | 1,373 | 655 | 4 |
| evidence_19 | analytics_profile_view_20260713.png | 1,373 | 648 | 1 |
| evidence_23 | customer_export_console_20260618.png | 1,399 | 893 | 4 |
| evidence_28 | support_ticket_console_20260524.png | 1,387 | 778 | 2 |
| evidence_32 | backup_restore_console_20260429.png | 1,373 | 891 | 5 |
| evidence_37 | security_audit_explorer_20260905.png | 1,399 | 1,238 | 6 |
| evidence_42 | deleted_account_reference_20260310.png | 1,399 | 909 | 5 |
| evidence_46 | object_storage_browser_20260213.png | 1,373 | 1,526 | 8 |
| evidence_60 | marketing_profile_console.png | 1,399 | 466 | 1 |
| **Total** | 11 files | **15,261** | **9,892** | **42** |

## Cost

| | Cost |
|---|---|
| **Actual cost of this run (Gemini free tier)** | **$0.00** total, $0.00 per request |
| Reference: same tokens at `gemini-2.5-flash` paid-tier list price ($0.30 in / $2.50 out per 1M) | $0.0293 total, $0.000057 per request |
| Reference: same tokens at Claude Haiku 5.5 list price ($0.10 in / $0.50 out per 1M) | $0.0065 total, $0.000013 per request |

We have no published paid-tier price for `gemini-3.8-flash` to quote. The two
reference rows show the order of magnitude: under three cents for the whole
dataset at either paid price.

## Caching and re-runs

Each screenshot's transcription is stored in `code/cache/` under a key built
from the image's SHA-256, the provider, the model and the prompt version
(`vision-v1`). The 11 calls above ran once. The run that produced the final
`output.csv` read all 11 from the cache: `usage_summary.json` reports
`calls: 11, cache_hits: 11` and keeps the token counts of the original calls
for this report. **Every re-run costs 0 new tokens**, including
`python code/main.py --no-llm`, which never opens a network connection and
reuses cached transcriptions from any provider. The cache ships in `code.zip`,
so graders reproduce the exact output offline, with no API key.

## Why Gemini instead of Claude Haiku 5.5

* **Access.** The participant could get a free Gemini API key from Google AI Studio. A Claude.ai subscription does not include API access, and Anthropic API keys are billed per use.
* **Fit.** The task is plain transcription of 11 admin-console screenshots into JSON. A current "flash" vision model handles it well. `responseMimeType: application/json` with temperature 0 gives parseable, repeatable output.
* **No extra dependency.** The Gemini call uses only Python's standard library (`urllib`), so the solution still installs and runs with no packages.

Both providers stay supported behind one interface (`ghost_audit/llm.py`):

```bash
# .env (git-ignored; copy from .env.example)
GHOST_LLM_PROVIDER=auto        # auto | gemini | anthropic
GEMINI_API_KEY=...             # auto picks Gemini when this is set
GEMINI_MODEL=gemini-3.8-flash
ANTHROPIC_API_KEY=...          # used by auto when no Gemini key is set
GHOST_LLM_MODEL=claude-haiku-5-5
```

With `GHOST_LLM_PROVIDER=anthropic`, the same prompt goes through the Anthropic
Messages API (`pip install anthropic`). Its usage is metered and costed with the
Anthropic list prices in `PRICES`. Cache entries for the two providers sit side
by side, keyed by provider and model.

The API key is read only from the environment or `.env`. It is sent only in the
`x-goog-api-key` request header and is never logged, written to the cache, or
included in `code.zip`.
