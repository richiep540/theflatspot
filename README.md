# The Flat Spot

A fully automated weekly podcast covering **skateboarding, mountain biking, snowboarding and
surfing** — competition results, the week's news, new gear and tech, and the riders on the way up.

Every Monday morning a GitHub Action pulls the week's stories from fifteen RSS feeds, has Claude
write a thirty-minute two-host script, voices it with Google Cloud Chirp 3: HD voices, and
publishes the episode to a podcast RSS feed on GitHub Pages. No manual work after setup.

Hosts: **Mackie** and **Tess** (en-GB Chirp 3: HD).

---

## How it works

```
RSS feeds ──▶ bucket by sport ──▶ drop stories covered in the last 3 weeks
      │
      ▼
Claude writes the episode in 6 segments (open, skate, MTB, snow, surf, wrap)
      │
      ▼
Google Cloud TTS voices each turn ──▶ pydub stitches one mp3
      │
      ▼
docs/feed.xml + docs/episodes.json + docs/index.html ──▶ GitHub Pages
```

The script is generated **one segment at a time** rather than in a single call. A thirty-minute
episode is roughly 4,700 words; asking for that in one response runs into the token ceiling and
gets truncated mid-JSON. Six calls of ~900 words each are well within limits, and each call is
given the running story list and the previous few lines so the conversation still flows.

### Files

| Path | What it is |
| --- | --- |
| `config.json` | Everything tunable: feeds, segments, hosts, voices, word targets |
| `scripts/generate_episode.py` | The whole pipeline |
| `scripts/make_cover.py` | Generates `docs/cover.jpg` (run locally, result is committed) |
| `tests/test_offline.py` | Checks that need no API keys; runs in CI before generation |
| `.github/workflows/weekly-podcast.yml` | The Monday 05:30 UTC schedule |
| `docs/feed.xml` | The podcast feed — **generated, do not hand-edit** |
| `docs/episodes.json` | Episode history — generated |
| `docs/covered_links.json` | Story links already used, so the show doesn't repeat itself |
| `docs/transcripts/` | The written script for each episode |
| `docs/episodes/` | The mp3 files |

---

## One-time setup

### 1. Accounts and keys

**Google Cloud** — create a project, enable **Cloud Text-to-Speech API**, add a billing card
(required even for free usage). Then **APIs & Services → Credentials → Create Credentials →
API key**, and restrict it to the Text-to-Speech API.

One thirty-minute episode is about 27,000 characters. Chirp 3: HD gives you 1 million
characters a month free, so a weekly show uses roughly **11% of the free tier**.

**Anthropic** — create an API key at [console.anthropic.com](https://console.anthropic.com) and
add a small amount of pay-as-you-go credit. Each episode is six calls of a few thousand tokens.

### 2. GitHub repo

Create a **public** repo (Pages needs public on the free tier) called `the-flat-spot` and push
everything in this folder to `main`.

### 3. Secrets and variables

**Settings → Secrets and variables → Actions → Secrets tab**, add:

- `ANTHROPIC_API_KEY`
- `GOOGLE_TTS_API_KEY`

### 4. Pages

**Settings → Pages → Deploy from a branch → `main` / `/docs`**. Wait for the URL to appear —
it'll be `https://<your-username>.github.io/the-flat-spot`.

### 5. The base URL variable

**Settings → Secrets and variables → Actions → Variables tab**, add `PUBLIC_BASE_URL` set to that
Pages URL, **no trailing slash**. The feed uses it to build absolute audio and cover URLs, so
episodes won't play in podcast apps until it's set.

### 6. First run

**Actions tab → Weekly podcast → Run workflow.** Takes roughly ten to fifteen minutes, most of it
text-to-speech. When it finishes, check `https://<PUBLIC_BASE_URL>/index.html` and play the episode.

### 7. Subscribe

In any podcast app, use **Add a Show by URL** with `https://<PUBLIC_BASE_URL>/feed.xml`.

To get properly listed rather than just link-accessible, submit that same feed URL at
[podcastsconnect.apple.com](https://podcastsconnect.apple.com) (Add → New Show → Add a show with
an RSS feed) and [podcasters.spotify.com](https://podcasters.spotify.com).

---

## Running it locally

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
brew install ffmpeg   # pydub needs it to write mp3
```

Check the feeds without touching any API (no keys needed):

```bash
python scripts/generate_episode.py --feeds-only
```

Write the script but skip the audio (Anthropic key only):

```bash
ANTHROPIC_API_KEY=... python scripts/generate_episode.py --script-only
```

Run the offline checks:

```bash
python tests/test_offline.py
```

Regenerate the cover art:

```bash
python scripts/make_cover.py
```

---

## Tuning it

**Episode length** — the six `segments[].words` values in `config.json` add up to 4,700 words,
which lands at about thirty minutes at Chirp 3: HD's natural pace. Scale them all up or down
together to change the runtime. Roughly 155 words per minute.

**Voices** — `hosts[].voice_name`. Preview at
[cloud.google.com/text-to-speech](https://cloud.google.com/text-to-speech). Any `en-GB-Chirp3-HD-*`
voice works; keep `google_tts_language_code` in step if you switch locale. `tts_speaking_rate`
nudges the pace (1.0 is natural, 1.05–1.1 gives it a bit more urgency).

**Segments** — add, remove or reorder entries in `segments`. Each one needs `sports` (which feed
buckets it draws from), a `words` target, and a `brief` telling the hosts what that segment is for.

**Feeds** — add to `feeds` with a `sport` tag. Two optional fields help keep quality up:

- `require_any` — only keep items whose title or summary contains one of these strings. Used on
  the general mountain-news feeds so the snowboarding segment doesn't fill up with hiking stories,
  and on the Google News feeds, which return loosely-related results.
- `blocked_sources` (top level) — publishers to drop entirely; content farms and SEO spam.

**Schedule** — the cron in the workflow. `30 5 * * 1` is Monday 05:30 UTC.

**Sponsor read** — put copy in `sponsor_ad_text` and it's appended verbatim as a final turn from
the first host, identical every week, never regenerated by the model.

### Off-season

Snowboarding goes quiet in the northern summer. The snow segment's `brief` tells the hosts to lean
into southern-hemisphere riding, gear launches and athlete news when there's no racing on, and the
prompt forbids inventing news to fill the gap — so an August snow segment is honest about where the
season is rather than padded.

---

## Known gotchas

- **"This API key is not scoped to a workspace"** — the Anthropic key is organisation-scoped.
  Either create a new key *inside a workspace* at console.anthropic.com (simplest, no config
  change), or add an Actions **variable** `ANTHROPIC_WORKSPACE_ID` set to your workspace id,
  which the workflow passes through as the `anthropic-workspace-id` header.
- **402 from the TTS API** — billing isn't enabled on the Google Cloud project, or the
  Text-to-Speech API isn't turned on. Not a code bug. The script says so explicitly and stops.
- **Truncated JSON from Claude** — the script now fails loudly with a clear message if a segment
  hits the token ceiling. Raise `max_tokens_per_segment` or lower that segment's `words`.
- **"failed to push some refs / rejected"** — the workflow runs `git pull --rebase origin main`
  before pushing, which handles it.
- **"Node.js 20 is deprecated"** in the Actions log is informational. Ignore it.
- **Opening `feed.xml` in a browser shows raw XML** — that's correct. Feed URLs go into a podcast
  app's "Add by URL", they aren't web pages.
- **Episodes not playing in a podcast app** — almost always `PUBLIC_BASE_URL` missing or wrong, so
  the `<enclosure>` URLs are relative. Fix the variable and re-run the workflow.
