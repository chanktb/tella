# Tella

**Turn a topic into a narrated story video — fully on your own machine.**

Type a topic, answer a few questions, and Tella writes a scene-by-scene
story, narrates it, finds or generates the visuals, and renders a finished
MP4. It runs on a free-tier AI stack (no subscriptions), and every file
stays on your hard drive.

```
You type:        "the lighthouse keeper who learned to rest"
Tella gives you: video.mp4   (1080×1920 or 1920×1080, narrated, ready to post)
```

- **Two ways in**: type a **topic** (Tella writes the story) or drop a **`.txt`
  file** (Tella narrates *your* story word-for-word)
- **9:16** vertical (TikTok / Reels / Shorts) or **16:9** horizontal (YouTube)
- Visuals from **AI image**, **stock photo**, or **stock video**. AI image
  offers two styles: **cinematic** (realistic/filmic) or **cartoon**
  (kid-friendly illustration)
- **Reading pace adapts to the topic** — children's tales narrate slowly and
  warmly, explainers narrate at a clear steady clip
- **Consistent characters** — in AI image mode every recurring character *and*
  the setting are locked across scenes. A two-character fable keeps both
  characters consistent (a tortoise stays a tortoise, a hare stays a hare),
  instead of collapsing into one face
- **Length**: topic mode is short (~60–120s) or detailed (~4–6 min); a dropped
  story runs as long as it needs, split into scenes automatically
- **8 narration languages**: English, Vietnamese, Japanese, Korean, Chinese,
  German, French, Spanish — for a topic, pick the output language; for a `.txt`
  file the language is auto-detected
- Free narration via Edge TTS (no key needed); optional Google Chirp 3 HD upgrade

---

## Quick start

### 1. Requirements

| Tool | Why | Install |
|---|---|---|
| Python 3.12+ | Runs the pipeline | [python.org](https://www.python.org/downloads/) — check **"Add Python to PATH"** |
| ffmpeg | Audio + video composition, Ken Burns, transitions | Win: `winget install Gyan.FFmpeg` · Mac: `brew install ffmpeg` · Linux: `apt install ffmpeg` |

### 2. Setup (one-time)

```
Windows:    SETUP.bat
Mac/Linux:  ./SETUP.sh
```

SETUP creates a virtual env under `.venv/` and installs dependencies. Then copy
`.env.example` to `.env` and fill in your keys (see [Keys](#keys) below).

The only **required** key is `GEMINI_API_KEY` (free, generous quota) —
grab one at <https://aistudio.google.com/apikey> in about 30 seconds.

### 3. Run

```
Windows:    RUN.bat
Mac/Linux:  ./RUN.sh
```

With no arguments you get the guided wizard:

```
============================================================
  Tella - turn a topic (or your own story) into a video
============================================================

Step 1 - Your story
  Type a TOPIC for Tella to write about, e.g.
     the tortoise and the hare
  OR drop a .txt file here (your own finished story) and press Enter.
Topic or file: the tortoise and the hare

Step 2 - Narration language
  * 1) Tieng Viet   2) English   3) Japanese  ...
Choose [1]: 2

Step 3 - Aspect ratio
  * 1) Vertical short  (TikTok / Reels / YouTube Shorts)
    2) Horizontal      (YouTube / landscape)
Choose [1]:

Step 4 - Where do the visuals come from?
  * 1) AI image  - characters stay consistent across scenes
    2) Stock photo - real Pexels photographs, fast
    3) Stock video - real Pexels video clips, most motion
Choose [1]:

Step 5 - AI image style   (only if you picked AI image)
  * 1) Cinematic - realistic, filmic
    2) Cartoon   - colorful illustration, kid-friendly
Choose [1]:

Step 6 - How long?        (short ~90s / detailed 4-6 min)
Step 7 - Narrator voice   (male / female)

------------------------------------------------------------
  Ready to render:
    Topic     : the tortoise and the hare
    Language  : English
    Aspect    : 9:16
    Visuals   : AI image
    Length    : Short
    Voice     : Male voice
------------------------------------------------------------
Start? [Y/n]:
```

The finished MP4 lands in `out/<timestamp>_<slug>/video.mp4`.

### Bring your own story (drop a `.txt`)

At Step 1, instead of a topic, drag a `.txt` file into the window (or paste its
path) and press Enter. Tella then:

- narrates **your exact words** — it doesn't rewrite the story, only splits it
  into scenes and adds visuals + timing;
- **cleans the text for narration** first — ellipses (`...`), repeated `!!!`,
  smart quotes, stray dashes and markdown are normalised so the voice doesn't
  stumble on them;
- **auto-detects the language** and picks a matching voice;
- runs **as long as the story needs** — there's no fixed length; it splits into
  as many scenes as the pacing calls for, so no single scene drones on.

---

## CLI (for power users / automation)

Pass any flag to skip the wizard:

```bash
python -m tella \
  --topic "the lighthouse keeper who learned to rest" \
  --lang en \
  --aspect 9:16 \
  --media ai_image \
  --duration short \
  --theme cinematic \
  --out ./out
```

| Flag | Choices | Default |
|---|---|---|
| `--topic` | any text (required) | — |
| `--lang` | `vi en ja ko zh de fr es` (required) | — |
| `--aspect` | `9:16` `16:9` | `9:16` |
| `--media` | `ai_image` `stock_photo` `stock_video` | `ai_image` |
| `--duration` | `short` `detailed` | `short` |
| `--theme` | `cinematic` `parable` `playful` `mindfulness` | `cinematic` |
| `--gender` | `male` `female` | theme default |
| `--out` | output dir | `./out` (or `$TELLA_OUTPUT_DIR`) |

---

## How visuals work

**AI image** (`ai_image`) — the planner first writes a **cast** of character
briefs and a **setting brief** (e.g. *"the tortoise: a small green tortoise,
domed brown shell, slow steady eyes"* and *"the hare: a sleek brown hare, long
ears, cocky smirk"* in *"a sun-drenched forest path, golden hour"*). Each scene
declares which cast members appear in it, and those identities + the setting
are prepended to that scene's image prompt — so Cloudflare Workers AI (FLUX)
renders the **same characters in the same world across all scenes**. A
multi-character story keeps *every* character consistent, and the cast is drawn
faithfully (an animal stays an animal — it is never swapped for a human
stand-in). This is what keeps a story visually coherent instead of looking like
a pile of unrelated pictures.

**Stock photo / video** (`stock_photo`, `stock_video`) — pulls real Pexels
media per scene. Character locking isn't possible with random stock, so Tella
keeps coherence with consistent color grading, transitions, and overlay style
instead. Fast, realistic, and free.

---

## Themes (advanced)

The wizard always uses **cinematic** — a versatile filmic look that renders the
story's real subjects. If you want a different tone/style, pass `--theme` on the
CLI:

| Theme | Tone | Imagery |
|---|---|---|
| `cinematic` (default) | Vivid storyteller, filmic | Photorealistic, film grain, teal-orange grade |
| `parable` | Meditative third-person fable | Watercolor, Studio-Ghibli-inspired |
| `mindfulness` | Calm dharma-talk reflection | Recurring monk character, warm watercolor |
| `playful` | Upbeat children's-book read-aloud | Vibrant cartoon, bold colors |

---

## Keys

See `.env.example`. Only `GEMINI_API_KEY` is required; everything else
unlocks an upgrade in its provider chain.

| Key | Unlocks | Free? |
|---|---|---|
| `GEMINI_API_KEY` | **Required** — story planning + translation | ✅ Free tier |
| `CF_ACCOUNTS` (or `CF_ACCOUNT_ID`+`CF_AI_TOKEN`) | AI image mode (Cloudflare Workers AI / FLUX) | ✅ 10k images/day/account |
| `PEXELS_API_KEY` | Stock photo + stock video modes | ✅ 200 req/hr, 20k/mo |
| `GOOGLE_TTS_API_KEY` *or* `GOOGLE_APPLICATION_CREDENTIALS` | Studio-quality Chirp 3 HD voices | Pay-as-you-go (tiny) |

Narration falls back to **Edge TTS**, which needs no key at all — so the
minimum to render a complete video is just `GEMINI_API_KEY` + one image
source (`PEXELS_API_KEY` is the easiest).

**Cloudflare multi-account rotation:** `CF_ACCOUNTS` takes a
semicolon-separated list of `account_id:api_token` pairs. Each free account
gives ~2000 FLUX images/day, so a few accounts let you render all day for $0.

---

## Output

```
out/
└── 20260627_174751_the_lighthouse_keeper/
    ├── video.mp4          ← the finished video
    ├── plan.json          ← scene-by-scene plan (story, prompts, timing)
    ├── assets/            ← per-scene images + narration mp3s
    └── _render/           ← intermediate scene clips (safe to delete)
```

Delete a job's `assets/` and `_render/` once you have the `video.mp4` to
reclaim disk.

---

## Cost

A typical short with Gemini + Edge TTS + (Cloudflare FLUX **or** Pexels) +
ffmpeg costs **$0**. Adding Google Chirp 3 HD narration bumps it to a few
hundredths of a cent per video.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `python not found` during SETUP | Reinstall Python with "Add Python to PATH" checked |
| `ffmpeg not found` | Win: `winget install Gyan.FFmpeg` · Mac: `brew install ffmpeg` · Linux: `apt install ffmpeg` |
| `GEMINI_API_KEY missing` | Copy `.env.example` to `.env` and paste a free key from aistudio.google.com/apikey |
| AI images look generic / placeholder | Set `CF_ACCOUNTS` (or `CF_ACCOUNT_ID`+`CF_AI_TOKEN`) for Cloudflare FLUX |
| `WSServerHandshakeError 403` from Edge TTS | ISP/region block — set `GOOGLE_TTS_API_KEY` to use Google TTS instead |
| Vietnamese diacritics garbled in the terminal | RUN.bat/RUN.sh already set `PYTHONUTF8=1`; run through those wrappers |

---

## License

MIT — see [LICENSE](LICENSE).
