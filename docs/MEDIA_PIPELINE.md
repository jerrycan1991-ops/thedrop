# THE DROP — Media Pipeline

All generation runs on the desktop (RTX 4070 SUPER, 12 GB VRAM). The VPS stores bytes, serves them, and enforces rights rules. No model ever loads on the VPS.

---

## 1. Governing rules

1. **Original only.** We never copy, trace, or closely recreate another publisher's image, video, graphic or thumbnail. Source media is analysed for *concept* (what the story looks like), never reproduced.
2. **No synthetic photojournalism.** An AI image must never be presented as a documentary photograph of a real event. Enforced by treatment rules (§4) plus a mandatory visible label.
3. **Rights status gates publication.** Only `ORIGINAL_AI`, `LICENSED`, `PUBLIC_DOMAIN`, `VALIDATED_CC` can auto-publish. `UNKNOWN` and `PROHIBITED` are hard blocks.
4. **Real people are constrained.** No photoreal depictions of identifiable private individuals. Public figures appear only in clearly non-photographic treatments (§4.3), never in fabricated compromising situations.
5. **Alt text is required.** No asset publishes without meaningful alt text.
6. **Every asset is traceable**: model, prompt version, seed, params, timestamp, cost.

---

## 2. Concept derivation (not copying)

```
story evidence packet
   |
   +-- visual concept analysis  (Claude, text-only)
   |     * what is the subject?
   |     * what setting/objects carry meaning?
   |     * what is the emotional register?
   |     * what must NOT be depicted? (unconfirmed events, gore, minors, victims)
   |
   +-- treatment selection (rules, §4)
   |
   +-- prompt synthesis (templated, versioned)
   |
   +-- generation -> safety review -> asset record
```

Source thumbnails from `raw_articles.image_urls` are **links we may look at for context**, never inputs to an image model and never rehosted. There is no img2img path from third-party media. That is a code-level prohibition, not a guideline.

---

## 3. Asset set per article

| Role | Dimensions | Purpose |
|---|---|---|
| `hero` | 1600×900 (16:9) | article top, OpenGraph fallback |
| `social` | 1200×630 | OG/Twitter card, headline burned in |
| `vertical` | 1080×1350 (4:5) | Instagram/feed |
| `breaking_card` | 1080×1080 | breaking-news square, high-contrast |
| `video_poster` | 1080×1920 | vertical video thumbnail |

Not every article gets all five. `standard`-risk lifestyle/tech stories get hero + social. High-opportunity stories get the full set. Cost per article is bounded by config.

---

## 4. Treatments

The treatment is chosen by rules, not by the model, because it is a safety control.

### 4.1 `editorial_abstract` (default for `high` risk)
Geometric/abstract composition, brand palette, typographic emphasis, no depicted people or events. Impossible to mistake for a photograph.

### 4.2 `conceptual_illustration` (default for `standard`/`elevated`)
Stylized illustration — visibly rendered, painterly or vector, distinctive palette. Depicts objects, settings, symbols; people only as non-identifiable figures.

### 4.3 `stylized_portrait_ban`
Public figures are **not** generated photorealistically. Where a person is central, we use `editorial_abstract` with typographic treatment (name, role, story frame) or a clearly-illustrated silhouette. This avoids both deepfake risk and likeness disputes.

### 4.4 `data_graphic`
Charts, maps and timelines rendered deterministically (Satori/SVG → PNG) from verified numbers in `claim_evidence`. No model involved — so no hallucinated data points. Preferred for business/markets.

### 4.5 `breaking_card`
Template composition: brand ground, accent bar, headline, timestamp, THE DROP mark. Rendered deterministically from tokens, not generated.

Hard prohibitions across all treatments: depicting a death/injury/crime scene; depicting an unconfirmed event as having happened; minors; identifiable victims; fabricated documents, screenshots or logos of real organizations; recreating a known copyrighted composition.

---

## 5. Image generation

- **Engine:** ComfyUI headless on the desktop, driven over its local HTTP API by the `image` job handler.
- **Models:** Flux.1-schnell (fast, 4 steps) as default; SDXL + refiner for `hero` when quality matters. fp8/quantized weights to stay inside 12 GB VRAM.
- **Determinism:** every asset stores `seed`, `steps`, `cfg`, `sampler`, model hash, and `prompt_version_id` so any image can be regenerated exactly.
- **Throughput budget:** ~4–8 s per 1600×900 Flux-schnell image. 25 articles × 3 assets ≈ 75 images/day ≈ 10 minutes of GPU. Trivial; the GPU is mostly free for video.
- **Post-processing:** brand grade (LUT), grain, corner mark (the "D"), and — for any generated imagery — a visible `AI-GENERATED ILLUSTRATION` label baked into the frame plus `ai_disclosure_text` on the record.
- **Provenance:** C2PA-style metadata written where the toolchain supports it; at minimum, EXIF/XMP carries generator, model and timestamp.

### Safety review (automated, before the asset can be used)
1. NSFW/gore classifier — fail closes.
2. Text-in-image check — garbled AI text is a common failure; OCR the render, reject if unintended text appears.
3. Face detection when the treatment forbids identifiable people.
4. Logo/trademark detection heuristic.
5. Near-duplicate check against our own recent assets (embedding similarity) so the site does not look repetitive.

Failures retry with an adjusted prompt up to 2 times, then the article falls back to a deterministic `breaking_card`/`data_graphic` — never to a third-party image.

---

## 6. Video engine

Format 1080×1920, 30–60 s, H.264 + AAC, ≤ 8 Mbps.

### 6.1 Structure

| Time | Beat |
|---|---|
| 0–3 s | Hook — the single most striking verified fact |
| 3–15 s | What happened — corroborated claims only |
| 15–35 s | Why it matters — impact, stakes, who is affected |
| 35–50 s | Context / labeled analysis (optional) |
| 50–60 s | Branding + CTA ("Full story at thedrop.channel") |

### 6.2 Pipeline

```
evidence packet
  -> script generation (Claude, scene beats, each beat carries claim_ids)
  -> script fact verification (every beat's claim_ids must be corroborated/authoritative)
  -> voiceover (TTS provider abstraction)
  -> visuals (generated stills + motion, data graphics, typographic cards)
  -> captions (forced alignment -> WebVTT, burned-in + sidecar)
  -> render (ffmpeg / Remotion composition)
  -> QA (duration, loudness, caption sync, safety, claim coverage)
  -> distribution queue
```

**No generative video models.** Text-to-video at 2026 consumer-GPU quality is not fit for news, and it makes fabricated-footage risk unmanageable. Motion comes from deterministic compositions: Ken Burns over generated stills, animated typography, animated data graphics, transitions. This is honest, fast, cheap and on-brand. Revisit only with an ADR.

**No third-party footage** without documented usage rights. There is no "grab the clip" path in the code.

### 6.3 Voiceover abstraction

```python
class TTSProvider(Protocol):
    def synthesize(self, text: str, voice: str, ssml: bool = False) -> AudioResult: ...
    def voices(self) -> list[Voice]: ...
```

Phase 6 default: **Piper** (CPU, fast, permissive) or **Kokoro** (GPU, higher quality) locally. ElevenLabs or another hosted voice slots in as a config change if quality demands it. Voice identity is a brand asset — one primary news voice, one secondary for analysis.

### 6.4 Render

Remotion (React compositions, TypeScript, shares design tokens with the site — so video and site cannot drift visually) rendered headless, or a pure-ffmpeg filtergraph path for simple compositions. Target ≤ 90 s render per 45 s video on the 4070 SUPER with NVENC.

### 6.5 Video QA

Duration in range; audio loudness −14 LUFS ±1; captions present and aligned within 200 ms; every spoken factual sentence maps to a verified claim id; no prohibited visual content; brand mark present; first frame is not black.

---

## 7. Storage and serving

- Path: `/var/www/thedrop/media/{yyyy}/{mm}/{asset_public_id}/{role}-{w}x{h}.{ext}`
- Served by Next.js from a symlinked `public/media` (no nginx change required in Phase 1).
- Derivatives: AVIF + WebP + JPEG fallback, generated at ingest of the asset, not on request.
- Long cache headers; the `asset_public_id` in the path makes assets immutable, so cache busting is free.
- Volume estimate: ~100 images/day × ~600 KB across derivatives ≈ 60 MB/day; video ~15/day × 8 MB ≈ 120 MB/day. **≈ 5.5 GB/month.** Retention sweep archives video source files after 60 days; a disk alert fires at 75 %.

Storage is behind an interface:

```python
class MediaStorage(Protocol):
    def put(self, key: str, data: bytes, content_type: str) -> str: ...
    def url_for(self, key: str) -> str: ...
    def delete(self, key: str) -> None: ...
```

`LocalDiskStorage` in Phase 1; `S3CompatibleStorage` is a config swap when volume or CDN needs justify it (ADR-0007).

---

## 8. Upload path from desktop

The desktop posts assets to `POST /api/v1/worker/artifacts` with a content hash, size and declared MIME. The API validates magic bytes (not the declared type), enforces a size cap, re-encodes images through a decode/encode cycle to strip anything embedded, writes to storage, and creates the `media_assets` row with `usage_status='draft'`. Only the publish gate promotes it to `approved`.

---

## 9. Cost accounting

Local generation has no API cost but is tracked in GPU-seconds so cost-per-article stays meaningful, and so a runaway loop is visible. If a hosted image or voice provider is enabled, its cost is recorded in `ai_runs` and counts against the same budgets as Claude usage.
