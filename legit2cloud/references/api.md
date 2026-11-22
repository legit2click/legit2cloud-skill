# legit2compute customer API — HTTP reference

Base URL: `https://api.legit2compute.com` (override via `L2C_API_URL`).
Auth: header **`x-api-key: <project-key>`** on every request (also accepts
`Authorization: Bearer <project-key>`). The key scopes billing + which pipelines
run; it only ever sees its own project's jobs.

All request/response bodies are JSON. Timestamps are ISO-8601. Job ids are UUIDs.

## Job lifecycle

`pending` → `processing` → terminal (`completed` | `failed` | `cancelled`). Submit
returns immediately; poll `GET /jobs/:id` until terminal, then read outputs.

---

## GET /me
Capabilities + credit for the calling key.
```json
{ "project": "legit2cloud", "name": "Legit2Cloud Media",
  "metered": false, "credit_balance_usd": null, "max_concurrent_jobs": 10,
  "pipelines": [ { "pipeline": "music_generate", "name": "Music Generation",
                   "timeout_seconds": 900, "est_cost_usd": 0.45 } ] }
```
`credit_balance_usd: null` → unmetered (no per-job charge). A number → prepaid USD
remaining; jobs 402 when it reaches 0.

## GET /workflows
Parameterized graph catalog.
```json
{ "workflows": [ { "slug": "...", "name": "...",
                   "default_task_type": "comfyui_workflow",
                   "param_map": { /* accepted params */ }, "workflow_json": {...} } ] }
```

## POST /jobs   (submit a pipeline)
```json
// request
{ "pipeline": "music_generate",
  "title": "optional label",
  "inputData": { "prompt": "upbeat lofi", "duration": 30 },
  "externalId": "your-own-id",      // optional; your own tag (does not de-duplicate)
  "priority": 3 }                   // optional 1-5, higher = sooner
// response 201
{ "job": { "job_id": "…", "status": "pending", "pipeline_slug": "music_generate",
           "external_id": "your-own-id", "created_at": "…" } }
```
A per-project key may only submit to its own project (the body's `project` is
ignored / forced). Charges `est_cost_usd` up front on metered projects.

Image to video and image edit take the source picture inside `inputData` as
`input_image`: a public URL, a previous job's output key, or a `data:image/...;base64,`
URL (keep the picture under ~3.5 MB; request bodies are capped at 5 MB). Video also
takes `width`/`height` (1280×704, 704×1280 or 960×960), `seconds` and an optional
`negative`:
```json
{ "pipeline": "video_i2v",
  "inputData": { "prompt": "a gator surges up beside the float and lunges at the lens",
                 "width": 704, "height": 1280, "seconds": 5,
                 "input_image": "data:image/jpeg;base64,/9j/4AAQ..." } }
```

Lip-sync video (`video_lipsync`): a character still + a song or speech; the character
sings or says it, for `seconds` of the audio starting at `start`, and the audio is the
soundtrack. `seconds` is required (1-300 per job; it is what the job is priced on; a longer song is
rendered in parts with `start`). Optional:
`input_vocals` (an isolated vocal to drive the mouth; otherwise the vocal is separated
from the mix), `isolate_vocals: false` (plain speech), `aspect` (`9:16`, `16:9`, `1:1`;
default follows the picture), `prompt`, `seed`. Output: one mp4, 480p, 25 fps.
Audio is too big for a request body, so upload local files first:
```json
// 1. POST /jobs/source-upload-url {"filename": "song.mp3"}
//    -> {"uploadUrl": "https://…", "sourceRef": "r2://…/song.mp3"}
// 2. PUT the file's bytes to uploadUrl (no extra headers)
// 3. submit with the sourceRef:
{ "pipeline": "video_lipsync",
  "inputData": { "input_image": "r2://…/singer.png", "input_audio": "r2://…/song.mp3",
                 "seconds": 42.5, "start": 0, "aspect": "9:16" } }
```
A job asking for more audio than the file holds fails before rendering (and is refunded).

Motion video (`video_motion`): a character picture + a video of someone moving; the
character performs the video's motion for `seconds` of the video starting at `start`, and
the video's sound is kept. `seconds` is required (1-20 per job; it is what the job is
priced on; a longer video is rendered in parts with `start`). Optional: `aspect` (`9:16`,
`16:9`, `1:1`; default follows the video), `prompt` (describes the character), `motion_prompt`
(the movement), `keep_audio: false`, `seed`. The look and the background come from the
picture, not from the video. Output: one mp4, 480p, 24 fps. Upload local files as above:
```json
{ "pipeline": "video_motion",
  "inputData": { "input_image": "r2://…/character.png", "input_video": "r2://…/dance.mp4",
                 "seconds": 8, "start": 0, "motion_prompt": "an energetic hip-hop dance" } }
```
A job asking for more video than the file holds fails before rendering (and is refunded).

## POST /workflows/:slug/run   (submit a workflow)
```json
// request
{ "params": { "prompt": "a fox in snow" },  // fills the workflow's param_map
  "title": "optional", "externalId": "optional",
  "inputImage": "https://… or an output key" }   // for image-edit / img2vid
// response 201
{ "job_id": "…", "job": { … } }
```

## Skill version   (GET /skill, no key)
```json
{ "name": "legit2cloud", "version": "2026.11.19", "download_url": "https://legit2compute.com/developers/skill.zip",
  "history": [ { "version": "2026.11.19", "changes": ["Saved voices and tones ...", "..."] } ] }
```
Send `X-L2C-Skill: legit2cloud/<your version>` on every call. Every response carries `X-L2C-Skill-Latest`;
an older copy also gets `X-L2C-Skill-Update` (download) and `X-L2C-Skill-Notice` (what is new). `GET /me`
returns `skill: {latest_version, your_version, up_to_date, changes, download_url}`.

## Saved voices + tones   (/voices)
A character's voice, saved once and named on every line: `"voice": "maple"` or `"maple:happy"`.
It keeps the recording, a fixed seed (the same line comes out the same way), its tones and
pronunciation fixes. Saving needs the voice-cloning terms accepted (`POST /me/voice-terms/accept`).
```json
// POST /voices  — sample: {"b64": "…"} | "data:audio/wav;base64,…" | "r2://…" (your upload) | {"job_id": "…"}
{ "name": "maple", "sample": "r2://…", "display_name": "Maple",
  "persona": "cheerful fox", "pronunciations": { "Siobhan": "Shi-vawn" } }
// PUT /voices/maple/styles/happy   — exactly one of emotion / emotion_text / emotion_sample
{ "emotion": { "happy": 0.8 }, "weight": 0.6 }          // weight default 0.6 keeps the voice recognisable
{ "emotion_text": "soft, hushed, secretive" }
{ "emotion_sample": { "job_id": "<a take you liked>" } } // the most consistent tone
// POST /voices/maple/audition  {"text": "…", "styles": ["normal", "happy"]}  -> one voice job per tone
// GET /voices · GET|PATCH|DELETE /voices/maple · DELETE /voices/maple/styles/happy
// then:
POST /jobs { "pipeline": "voice_generate", "inputData": { "voice": "maple:happy", "texts": ["Line one.", "Line two."] } }
```
`voice` replaces `voice_sample`, `seed` and the emotion fields (an explicit `seed` or emotion field you
send still wins). One tone per job; every line in `texts` comes back as its own file.

### `language` on a voice job
```json
POST /jobs { "pipeline": "voice_generate",
             "inputData": { "voice": "maple", "language": "es", "texts": ["Hola.", "Bienvenidos al programa."] } }
```
`language` is the language the text is WRITTEN in (nothing is translated). Left out, `"en"`, `"zh"` or
`"auto"`: the line is read as before. Other accepted values: `es`, `hi` (tested), and `ar da de el fi fr it ja ko
ms nl no pl pt sv sw tr` (accepted, not yet tested by us); a regional code (`es-MX`) or an English name
(`"Spanish"`) normalises to these. One language per job.

| Case | Answer |
|---|---|
| any other language | `400` `language 'xx' is not supported for voice. Supported: en, zh, ar, …` — nothing queued or charged |
| a line over 900 characters in a non-English, non-Chinese language | `400` `a line in language 'es' can be at most 900 characters (this one is N). Split the text into shorter lines with "texts": [...]` |
| `emotion`, `emotion_text`, `emotion_audio` or a tone (`maple:happy`) with such a language | accepted; not applied; listed in the finished job's `output_data.params.ignored` |

The finished job's `output_data` carries `language` (as sent) and `params.language` (the normalised code).
These lines take longer than English ones: allow a minute or two for a short line.

## GET /jobs   (list your jobs)
Query: `?status=&externalId=&limit=` (default 50, max 500). Returns
`{ "jobs": [ { job_id, pipeline_slug, title, status, progress, current_stage,
              error_message, created_at, started_at, completed_at } ] }`.
Note: the list projection does **not** include `external_id`; use `GET /jobs/:id`
for the full row (which does).

## GET /jobs/:id   (status + detail)
Full job row plus its tasks: `{ "job": { job_id, status, progress, current_stage,
input_data, output_data, external_id, error_message, … }, "tasks": [ … ] }`
(some deployments return the job row at the top level — handle both).

## GET /jobs/:id/outputs   (artifacts)
```json
{ "outputs": [ { "bucket": "legit2tune",
                 "key": "<job>/<uuid>.wav",
                 "filename": "<uuid>.wav",
                 "downloadUrl": "/jobs/<job>/download?key=<url-encoded-key>" } ] }
```

## GET /jobs/:id/download?key=   (stream an artifact)
Streams the file **through the orchestrator** — the caller needs only the API key,
no object-store credentials. `?key=` selects which artifact (must be one of this
job's outputs); omit it for the first. Returns the raw bytes with `Content-Type`
and `Content-Disposition`. `410` if the artifact predates the download proxy or
expired; `404` if the job isn't done yet.

## POST /jobs/:id/cancel  ·  POST /jobs/:id/retry
Cancel a running job / retry a failed one. Both return the updated `{ job }`.

## GET /jobs/:id/events
Audit trail for the job: `{ "events": [ … ] }`.

---

## Credits / wallet (optional, if your integration manages billing)
- `GET /credits/pricing` · `GET /credits/balance` · `GET /credits/packs`
- `POST /credits/checkout` — Stripe Checkout for a pack → `{ checkout_url }`
- `GET /credits/ledger?account=&limit=` — transaction history
- `GET /credits/resolve?platform=&externalId=&create=1&email=` — get-or-create the
  wallet (Account) for a user identity → `{ account_id, balance_credits }`

## Errors
| Status | Meaning |
|--------|---------|
| 400 | bad request (e.g. malformed id, missing `pipeline`) |
| 401 / 403 | missing/invalid key, or accessing another project's job |
| 402 | out of prepaid credit |
| 404 | unknown job / pipeline / workflow, or artifact not ready |
| 410 | artifact no longer proxiable (predates proxy / expired) |

Error bodies are `{ "error": "message", "code"?: "...", "hint"?: "..." }`.

---

## LLM gateway (OpenAI-compatible chat)

Same base URL and key. The gateway speaks the OpenAI chat API, so an OpenAI SDK works
with `base_url = https://api.legit2compute.com/nodes/llm/v1` and the project key as the
API key (`x-api-key: <key>` or `Authorization: Bearer <key>` — both accepted).

- `POST /nodes/llm/v1/chat/completions` — OpenAI chat shape: `{ "model", "messages",
  "max_tokens"?, "temperature"?, "stream"?, "tools"?, "response_format"?, ... }`. Response is
  the OpenAI shape (`choices[0].message.content`, `usage.prompt_tokens` /
  `completion_tokens`).
- `POST /nodes/llm/api/chat` — Ollama-native shape (`messages`, `images` as base64 strings,
  `think`). Use the OpenAI path unless you already have Ollama-shaped code.
- `GET /nodes/llm/v1/models` — the OpenAI model list: every model the key can call plus
  `l2c/auto`, each with `context_length`, `supports_tools`, `modality` and `pricing`. Agent
  frameworks (Hermes Agent, OpenClaw, Odysseus) discover models here; setup guides for each
  are at https://legit2compute.com/docs.
- `GET /me` — the price list and the auto ladder:
  - `pricing[]`: `{ model, modality: "text"|"text,image", usd_per_1m_prompt,
    usd_per_1m_completion, usd_per_1m_cached }` for every model the key can call.
  - `auto`: `{ model: "l2c/auto", tiers: [{ tier: 1|2|3, models: [{ model, modality,
    context_window, supports_tools }] }] }`.
  - `allow_external`, `private_inference`: routing posture of this project (below).

**Models.** `"model"` is a name from `pricing[]`, or `"l2c/auto"`. Auto scores the request
(images, tools, structured output, code, maths, reasoning language, length) into a tier —
1 small, 2 mid, 3 large — and serves it from the cheapest resident model in that tier whose
modality, tool support and context window fit, stepping up a tier if nothing there is
loaded. The bill is the chosen model's price. Cap or floor it per request with
`"route": { "max_tier": 1..3, "min_tier": 1..3 }`. The response carries
`X-L2C-Routed-Model` and `X-L2C-Auto-Tier`.

**Cold models.** A model that is not currently loaded answers `503` with
`{ "warming": true, ... }`; it is being started on a node. Retry after a minute or two
(the CLI waits up to five minutes). Popular models are kept warm.

**Images.** Vision models (`modality` includes `image`) take a message whose `content` is
an array of `{ "type": "text", "text": "..." }` and `{ "type": "image_url", "image_url":
{ "url": "data:image/jpeg;base64,..." } }` parts; an `https` URL also works. Bodies are
limited to **5 MB** (`413` otherwise), so resize large photos before encoding. Image tokens
bill as prompt tokens at the model's input rate.

**Reasoning.** Off by default at the gateway, so a short `max_tokens` buys an answer rather
than hidden thinking. Turn it on per request with `"reasoning_effort": "low"|"medium"|"high"`
(OpenAI path) or `"think": true` (Ollama path) and give the model token headroom; reasoning
comes back in the message's `reasoning` field beside `content` and bills as output tokens.

**Routing and privacy.** Requests are served by the platform's own fleet first. A project
with `allow_external: true` may be served by an external provider when no node serves that
exact model at that moment or every node fails — the same model, never a substitute, and
only through a provider priced at or under the platform's own rate. Every response carries
`X-L2C-Provider: self` or the provider's name; send `X-L2C-Route: self-only` (or
`"route": "self-only"` in the body) to forbid external routing for one request.
Private-inference projects are served only by operator-owned or contracted hosts and never
routed externally; every response carries `X-L2C-Privacy: private|shared`.

**Response headers.** Every LLM answer carries audit headers so a caller can check, per
request, who answered and how — without inferring it from latency or price:

| Header | Values | Meaning |
|--------|--------|---------|
| `X-L2C-Provider` | `self` or a provider name | Served by the platform's own fleet, or by which external provider |
| `X-L2C-Privacy` | `private` / `shared` | The host tier that answered (private = operator-owned or contracted only) |
| `X-L2C-Residency` | `reserved` / `shared` | Whether the project's own reserved host served it, or shared capacity |
| `X-L2C-Routed-Model` | model name | `l2c/auto` only: the model auto chose |
| `X-L2C-Auto-Tier` | `1` / `2` / `3` | `l2c/auto` only: the tier the request was scored into |
| `X-L2C-Served-Model` | model name | Present only if the answering endpoint served a different model than requested; absent otherwise |

Request headers: `X-L2C-Route: self-only` forbids external routing for that request
(equivalent to `"route": "self-only"` in the body). The CLI prints the audit headers as a
one-line `[l2c] ...` summary on stderr, with or without `--json`.

**Retention.** For LLM requests the platform records metadata only (model, token counts,
latency, status, an error category). Prompts and outputs are not stored or logged.

**Usage.** `GET /me/usage/llm?days=30` — this key's LLM spend and request counts by model.

| Status | Meaning (LLM paths) |
|--------|---------------------|
| 400 | bad request body / unknown model |
| 402 | out of prepaid credit |
| 413 | request body over 5 MB |
| 503 + `warming: true` | model is starting; retry in a minute or two |
