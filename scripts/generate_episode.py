#!/usr/bin/env python3
"""
The Flat Spot — weekly episode generator.

1. Pulls the past week's items from the RSS feeds in config.json, bucketed by sport
2. Drops stories already covered in recent episodes (docs/covered_links.json)
3. Asks Claude (Anthropic API) to write the episode one segment at a time, so a
   ~30 minute (~4,700 word) script never gets truncated mid-JSON
4. Turns each turn into audio with Google Cloud Text-to-Speech (Chirp 3: HD voices)
5. Stitches it into one mp3 in docs/episodes/
6. Updates docs/episodes.json, regenerates docs/feed.xml and docs/index.html

Run with:
    ANTHROPIC_API_KEY=... GOOGLE_TTS_API_KEY=... python scripts/generate_episode.py

Useful flags for local testing:
    --feeds-only    fetch and print the story buckets, no API keys needed
    --script-only   write the script to docs/last_script.json, skip audio (needs Anthropic key)
"""

import os
import re
import sys
import json
import time
import base64
import datetime
import xml.sax.saxutils as saxutils

import feedparser
import requests
from pydub import AudioSegment

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT, "config.json")
DOCS_DIR = os.path.join(ROOT, "docs")
EPISODES_DIR = os.path.join(DOCS_DIR, "episodes")
EPISODES_JSON = os.path.join(DOCS_DIR, "episodes.json")
COVERED_JSON = os.path.join(DOCS_DIR, "covered_links.json")
FEED_XML = os.path.join(DOCS_DIR, "feed.xml")
INDEX_HTML = os.path.join(DOCS_DIR, "index.html")
TRANSCRIPTS_DIR = os.path.join(DOCS_DIR, "transcripts")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
GOOGLE_TTS_API_KEY = os.environ.get("GOOGLE_TTS_API_KEY")
# Only needed when the Anthropic key is organisation-scoped rather than tied to a
# single workspace. A workspace-scoped key does not need this at all.
ANTHROPIC_WORKSPACE_ID = os.environ.get("ANTHROPIC_WORKSPACE_ID", "").strip()
# The public base URL where docs/ ends up being served, e.g.
# https://yourusername.github.io/the-flat-spot
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")

USER_AGENT = "Mozilla/5.0 (compatible; TheFlatSpotBot/1.0; podcast feed reader)"
TTS_MAX_BYTES = 4000  # Google's hard limit is 5000 bytes per request


def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


# ---------------------------------------------------------------- HTTP helpers

def post_with_retry(url, *, headers, json_body, timeout=180, attempts=5, label="request"):
    """POST with exponential backoff on rate limits and transient server errors."""
    delay = 4
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            resp = requests.post(url, headers=headers, json=json_body, timeout=timeout)
        except requests.RequestException as e:
            last_error = f"{type(e).__name__}: {e}"
        else:
            if resp.status_code < 400:
                return resp
            if resp.status_code == 402:
                raise SystemExit(
                    f"{label} returned 402 Payment Required. This is almost always a billing "
                    "problem, not a code bug: check the Google Cloud project has billing enabled "
                    "and the Text-to-Speech API is turned on, or that the Anthropic account has "
                    "credit."
                )
            if resp.status_code in (408, 429) or resp.status_code >= 500:
                last_error = f"HTTP {resp.status_code}: {resp.text[:400]}"
            elif "anthropic-workspace-id" in resp.text:
                raise SystemExit(
                    f"{label}: this Anthropic key is organisation-scoped, not workspace-scoped. "
                    "Either create a new key inside a workspace at console.anthropic.com, or set "
                    "the ANTHROPIC_WORKSPACE_ID repo variable to your workspace id."
                )
            else:
                raise SystemExit(f"{label} failed with HTTP {resp.status_code}: {resp.text[:800]}")
        if attempt < attempts:
            print(f"  {label} attempt {attempt} failed ({last_error}); retrying in {delay}s")
            time.sleep(delay)
            delay = min(delay * 2, 60)
    raise SystemExit(f"{label} failed after {attempts} attempts. Last error: {last_error}")


# ------------------------------------------------------------------- RSS input

def strip_html(text):
    text = re.sub(r"<[^<]+?>", " ", text or "")
    text = (text.replace("&nbsp;", " ").replace("&amp;", "&")
                .replace("&#8217;", "'").replace("&#8216;", "'")
                .replace("&#8220;", '"').replace("&#8221;", '"'))
    return re.sub(r"\s+", " ", text).strip()


def entry_published(entry):
    for key in ("published_parsed", "updated_parsed"):
        value = entry.get(key)
        if value:
            try:
                return datetime.datetime(*value[:6])
            except (TypeError, ValueError):
                continue
    return None


def fetch_feed(url):
    """Fetch with a browser-ish user agent — several of these publishers 403 the default one."""
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=45)
        resp.raise_for_status()
        return feedparser.parse(resp.content)
    except Exception as e:
        print(f"  ! could not read {url}: {type(e).__name__}: {e}")
        return None


def interleave(lists):
    """Round-robin so one prolific feed can't crowd out the rest of a sport."""
    out = []
    for row in range(max((len(l) for l in lists), default=0)):
        for items in lists:
            if row < len(items):
                out.append(items[row])
    return out


def split_aggregator_title(title, source):
    """Google News titles end in ' - Publisher'. Pull the publisher out so the prompt
    (and the source blocklist) sees who actually wrote the story."""
    if "Google News" not in source or " - " not in title:
        return title, source
    head, _, publisher = title.rpartition(" - ")
    return head.strip(), publisher.strip()


def collect_items_by_sport(config):
    """Return {sport: [item, ...]} for items published inside the lookback window."""
    cutoff = datetime.datetime.utcnow() - datetime.timedelta(hours=config["lookback_hours"])
    per_feed_cap = config.get("max_items_per_feed", 12)
    blocked = [b.lower() for b in config.get("blocked_sources", [])]
    per_sport = {sport: [] for sport in config["sport_labels"]}

    for feed in config["feeds"]:
        url, sport = feed["url"], feed["sport"]
        # Some feeds are general mountain/outdoor titles; require_any keeps only the
        # items actually relevant to this show's beat.
        required = [k.lower() for k in feed.get("require_any", [])]
        parsed = fetch_feed(url)
        if parsed is None:
            continue
        source = parsed.feed.get("title") or url
        kept = []
        for entry in parsed.entries:
            published = entry_published(entry)
            if published and published < cutoff:
                continue
            title = strip_html(entry.get("title", ""))
            summary = strip_html(entry.get("summary") or entry.get("description") or "")
            if required and not any(k in f"{title} {summary}".lower() for k in required):
                continue
            title, publisher = split_aggregator_title(title, source)
            if any(b in publisher.lower() for b in blocked):
                continue
            kept.append({
                "title": title,
                "summary": summary[:600],
                "link": entry.get("link", ""),
                "source": publisher,
                "sport": sport,
                "published": published.isoformat() if published else "",
            })
        kept.sort(key=lambda i: i["published"], reverse=True)
        kept = kept[:per_feed_cap]
        per_sport.setdefault(sport, []).append(kept)
        print(f"  {sport:5s} {len(kept):3d} recent  <- {source}")

    return {
        sport: interleave(lists)[: config["max_items_per_sport"]]
        for sport, lists in per_sport.items()
    }


def load_covered_links():
    if not os.path.exists(COVERED_JSON):
        return []
    try:
        with open(COVERED_JSON) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


def drop_covered(buckets, covered, history_days):
    """Filter out links used recently — but never empty a sport out entirely."""
    cutoff = datetime.date.today() - datetime.timedelta(days=history_days)
    recent_links = {
        row["link"] for row in covered
        if row.get("link") and row.get("date", "") >= cutoff.isoformat()
    }
    out = {}
    for sport, items in buckets.items():
        filtered = [i for i in items if i["link"] not in recent_links]
        out[sport] = filtered if filtered else items
    return out


# ------------------------------------------------------------- script writing

def format_stories(items):
    if not items:
        return "(No fresh stories in the feeds for this sport this week.)"
    return "\n\n".join(
        f"- {it['title']}\n  source: {it['source']}\n  {it['summary']}\n  {it['link']}"
        for it in items
    )


SHARED_STYLE = """\
STYLE RULES (these matter more than anything else):
- Write speech, not prose. Contractions, short sentences, the odd sentence fragment.
  Hosts interrupt, agree, push back, and finish each other's thoughts.
- Real enthusiasm, real scepticism. No press-release voice, no "in today's fast-moving world".
- Vary turn length a lot: some turns are two words, some run twenty seconds.
- Use the sports' own vocabulary correctly and unselfconsciously.
- Never invent results, scores, injuries, sponsorships or quotes that are not in the
  supplied stories. If something is unclear, have a host say so out loud.
- Spell out EVERY number, price, measurement and percentage in words, because a
  text-to-speech engine reads this aloud and mangles digits and symbols.
  "twelve percent", "three hundred and twenty pounds", "twenty twenty six",
  "twenty nine point five inches", "a hundred and forty millimetres of travel".
- No stage directions, no sound-effect cues, no square brackets, no asterisks,
  no emoji, no markdown, no URLs read aloud.
- Do not say "welcome back" or re-introduce the show mid-episode.

OUTPUT FORMAT:
Respond with ONLY a JSON array — no prose before or after, no markdown fences.
Each element is {"speaker": "<host name>", "text": "..."}
"""


def build_segment_prompt(config, segment, stories_text, date_label, running_summary, previous_tail, is_first, is_last):
    host_a, host_b = config["hosts"][0]["name"], config["hosts"][1]["name"]

    if is_first:
        position = (
            "This is the OPENING of the episode. Start the show — cold open, then the "
            "welcome, the show name, and today's date."
        )
    elif is_last:
        position = (
            "This is the FINAL segment. Pick up mid-conversation from where the previous "
            "segment left off, and end the episode with a proper sign-off."
        )
    else:
        position = (
            "This is a MIDDLE segment. Pick up mid-conversation — a natural handoff line "
            "into this sport, not a fresh greeting. Do not sign off at the end; hand on to "
            "the next topic."
        )

    context = ""
    if running_summary:
        context += "\nAlready covered earlier in this episode (do not repeat these):\n" + running_summary + "\n"
    if previous_tail:
        context += "\nThe last few lines spoken, for continuity:\n" + previous_tail + "\n"

    return f"""You write {config['podcast_title']}, a weekly action-sports podcast covering \
skateboarding, mountain biking, snowboarding and surfing. The two hosts are {host_a} and {host_b}. \
They are British, they actually ride, and they know the scenes inside out. Today's date is {date_label}.

You are writing ONE segment of this week's episode: "{segment['label']}".
{position}

What this segment needs to do:
{segment['brief']}

Target length: about {segment['words']} words of spoken dialogue. Get close to that — it matters, \
because the episode has to land at thirty minutes.
{context}
Stories pulled from the feeds for this segment:

{stories_text}

Cover several distinct stories or angles rather than dwelling on one. Where a story is thin, \
give it the context a listener needs: why it matters, who the rider is, what it changes. If there \
are no fresh stories for this sport, talk about where the sport is in its season right now and what \
is coming up, and keep it honest — do not fabricate news.

{SHARED_STYLE}
"""


def call_anthropic(prompt, model, max_tokens, label):
    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    if ANTHROPIC_WORKSPACE_ID:
        headers["anthropic-workspace-id"] = ANTHROPIC_WORKSPACE_ID

    resp = post_with_retry(
        "https://api.anthropic.com/v1/messages",
        headers=headers,
        json_body={
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=300,
        label=label,
    )
    data = resp.json()
    if data.get("stop_reason") == "max_tokens":
        raise SystemExit(
            f"{label}: Claude hit the max_tokens ceiling and the JSON is truncated. "
            "Raise max_tokens_per_segment in config.json, or lower the segment's word target."
        )
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    return parse_turns(text.strip(), label)


def parse_turns(text, label):
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text).strip()
    # Be forgiving about any stray commentary either side of the array.
    start, end = text.find("["), text.rfind("]")
    if start != -1 and end > start:
        text = text[start:end + 1]
    try:
        turns = json.loads(text)
    except json.JSONDecodeError as e:
        raise SystemExit(f"{label}: could not parse Claude's reply as JSON ({e}).\nFirst 500 chars:\n{text[:500]}")
    if not isinstance(turns, list):
        raise SystemExit(f"{label}: expected a JSON array of turns, got {type(turns).__name__}.")
    return [
        {"speaker": str(t.get("speaker", "")).strip(), "text": str(t.get("text", "")).strip()}
        for t in turns
        if isinstance(t, dict) and str(t.get("text", "")).strip()
    ]


def write_script(config, buckets, date_label):
    """Generate the full episode script, one segment at a time."""
    host_names = [h["name"] for h in config["hosts"]]
    all_turns = []
    used_links = []
    running_summary_lines = []
    segments = config["segments"]

    for index, segment in enumerate(segments):
        items = []
        for sport in segment["sports"]:
            items.extend(buckets.get(sport, []))
        # For the wrap-up segments, a shortlist is enough context.
        if len(segment["sports"]) > 1:
            items = items[:20]

        prompt = build_segment_prompt(
            config,
            segment,
            format_stories(items),
            date_label,
            "\n".join(running_summary_lines),
            "\n".join(f"{t['speaker']}: {t['text']}" for t in all_turns[-3:]),
            is_first=(index == 0),
            is_last=(index == len(segments) - 1),
        )
        label = f"segment '{segment['id']}'"
        print(f"  writing {label} (~{segment['words']} words, {len(items)} stories)...")
        turns = call_anthropic(prompt, config["anthropic_model"], config["max_tokens_per_segment"], label)

        # Keep speaker names to the two configured hosts.
        for turn in turns:
            if turn["speaker"] not in host_names:
                match = next((n for n in host_names if n.lower() in turn["speaker"].lower()), None)
                turn["speaker"] = match or host_names[len(all_turns) % 2]
            turn["segment"] = segment["id"]

        words = sum(len(t["text"].split()) for t in turns)
        print(f"    -> {len(turns)} turns, {words} words")
        all_turns.extend(turns)
        used_links.extend(i["link"] for i in items if i["link"])
        running_summary_lines.append(
            f"- {segment['label']}: " + "; ".join(i["title"] for i in items[:8])
        )

    ad = (config.get("sponsor_ad_text") or "").strip()
    if ad:
        all_turns.append({"speaker": host_names[0], "text": ad, "segment": "sponsor"})

    return all_turns, sorted(set(used_links))


# ------------------------------------------------------- speech normalisation

ONES = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
        "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen",
        "eighteen", "nineteen"]
TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]
SCALES = [(1_000_000_000, "billion"), (1_000_000, "million"), (1_000, "thousand")]


def _under_hundred(n):
    if n < 20:
        return ONES[n]
    tens, ones = divmod(n, 10)
    return TENS[tens] + (f" {ONES[ones]}" if ones else "")


def _under_thousand(n):
    hundreds, rest = divmod(n, 100)
    if not hundreds:
        return _under_hundred(rest)
    out = f"{ONES[hundreds]} hundred"
    return out + (f" and {_under_hundred(rest)}" if rest else "")


def number_to_words(n):
    if n == 0:
        return "zero"
    parts = []
    for value, name in SCALES:
        if n >= value:
            count, n = divmod(n, value)
            parts.append(f"{_under_thousand(count)} {name}")
    if n:
        parts.append(("and " if parts and n < 100 else "") + _under_thousand(n))
    return " ".join(parts)


ORDINALS = {1: "first", 2: "second", 3: "third", 5: "fifth", 8: "eighth", 9: "ninth", 12: "twelfth"}


def ordinal_to_words(n):
    if n in ORDINALS:
        return ORDINALS[n]
    words = number_to_words(n)
    last = words.rsplit(" ", 1)[-1]
    head = words[: len(words) - len(last)]
    suffixed = {"one": "first", "two": "second", "three": "third", "five": "fifth",
                "eight": "eighth", "nine": "ninth", "twelve": "twelfth"}.get(last)
    if suffixed:
        return head + suffixed
    if last.endswith("y"):
        return head + last[:-1] + "ieth"
    return head + last + "th"


def year_to_words(n):
    if 2000 <= n <= 2009:
        return "two thousand" + (f" and {ONES[n - 2000]}" if n % 10 else "")
    first, second = divmod(n, 100)
    if second == 0:
        return f"{_under_hundred(first)} hundred"
    return f"{_under_hundred(first)} {'oh ' + ONES[second] if second < 10 else _under_hundred(second)}"


CURRENCY = {"£": "pounds", "$": "dollars", "€": "euros"}
SUFFIXES = {"k": "thousand", "m": "million", "bn": "billion", "b": "billion"}
UNITS = {
    "mm": "millimetres", "cm": "centimetres", "km": "kilometres", "m": "metres",
    "kg": "kilograms", "lb": "pounds", "lbs": "pounds", "ft": "feet",
    "mph": "miles per hour", "kph": "kilometres per hour", "km/h": "kilometres per hour",
}


def _digits_to_words(raw):
    """'12,500' -> 'twelve thousand five hundred'; '9.5' -> 'nine point five'."""
    raw = raw.replace(",", "")
    if "." in raw:
        whole, frac = raw.split(".", 1)
        whole_words = number_to_words(int(whole)) if whole else "zero"
        frac_words = " ".join(ONES[int(d)] for d in frac if d.isdigit())
        return f"{whole_words} point {frac_words}"
    return number_to_words(int(raw))


def normalize_for_speech(text):
    """Safety net for anything Claude left as digits or symbols — TTS reads these badly."""
    text = re.sub(r"https?://\S+|www\.\S+", "", text)
    text = re.sub(r"[\*\_`#\[\]]", "", text)
    text = text.replace("&", " and ").replace("…", "...").replace("—", ", ").replace("–", "-")

    # Currency, optionally with a k/m/bn suffix: £2.5m -> two point five million pounds
    def _currency(m):
        unit = CURRENCY[m.group(1)]
        amount = _digits_to_words(m.group(2))
        scale = SUFFIXES.get((m.group(3) or "").lower())
        return f"{amount} {scale} {unit}" if scale else f"{amount} {unit}"

    # The suffix group must not swallow the following space when there is no suffix.
    text = re.sub(r"([£$€])\s?([\d,]+(?:\.\d+)?)(?:\s?(bn|[kmb])\b)?", _currency, text, flags=re.I)
    text = re.sub(r"([\d,]+(?:\.\d+)?)\s?%", lambda m: f"{_digits_to_words(m.group(1))} percent", text)

    # Units usually run straight into the digits ("140mm"), so handle them before
    # the generic number pass — \b would not fire between "0" and "m".
    unit_pattern = "|".join(sorted((re.escape(u) for u in UNITS), key=len, reverse=True))
    text = re.sub(
        rf"(?<![\w.])(\d[\d,]*(?:\.\d+)?)\s?({unit_pattern})(?![\w])",
        lambda m: f"{_digits_to_words(m.group(1))} {UNITS[m.group(2).lower()]}",
        text, flags=re.I,
    )

    # "in" only counts as inches when glued to the digits — "5 in the morning" is not.
    text = re.sub(r"(?<![\w.])(\d[\d,]*(?:\.\d+)?)in(?![\w])",
                  lambda m: f"{_digits_to_words(m.group(1))} inches", text, flags=re.I)

    text = re.sub(r"\b(\d+)(st|nd|rd|th)\b", lambda m: ordinal_to_words(int(m.group(1))), text, flags=re.I)
    text = re.sub(r"\b(19|20)(\d{2})\b", lambda m: year_to_words(int(m.group(0))), text)

    def _plain(m):
        try:
            words = _digits_to_words(m.group(0))
        except (ValueError, IndexError, KeyError):
            return m.group(0)
        # Keep a gap if the number was glued to a word, e.g. "50cc" -> "fifty cc".
        tail = m.string[m.end():m.end() + 1]
        return words + (" " if tail.isalpha() else "")

    # No trailing \b: it would miss digits glued to letters, e.g. "50cc", "10x".
    text = re.sub(r"(?<![\w.])\d[\d,]*(?:\.\d+)?", _plain, text)
    return re.sub(r"\s+", " ", text).strip()


def chunk_text(text, max_bytes=TTS_MAX_BYTES):
    """Split on sentence boundaries so no single TTS request exceeds the byte limit."""
    if len(text.encode("utf-8")) <= max_bytes:
        return [text]
    sentences = re.split(r"(?<=[.!?])\s+", text)
    chunks, current = [], ""
    for sentence in sentences:
        candidate = f"{current} {sentence}".strip()
        if len(candidate.encode("utf-8")) > max_bytes and current:
            chunks.append(current)
            current = sentence
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


# -------------------------------------------------------------------- audio

def batch_turns(turns, max_bytes):
    """Group consecutive turns into multi-speaker requests.

    Gemini-TTS voices a whole exchange in one call, which is what makes it sound
    like a conversation rather than a queue of announcements: intonation carries
    across turns and the model paces the gaps itself. Batches never straddle a
    segment boundary, so the segment pauses stay deliberate.
    """
    batches, current, size = [], [], 0
    for turn in turns:
        cost = len(turn["spoken"].encode("utf-8")) + len(turn["speaker"]) + 20
        crosses_segment = current and turn.get("segment") != current[-1].get("segment")
        if current and (size + cost > max_bytes or crosses_segment):
            batches.append(current)
            current, size = [], 0
        current.append(turn)
        size += cost
    if current:
        batches.append(current)
    return batches


def synthesize_multispeaker(batch, config):
    """One request, many turns — returns LINEAR16 WAV bytes."""
    aliases = {h["name"]: re.sub(r"[^A-Za-z0-9]", "", h["name"]) for h in config["hosts"]}
    resp = post_with_retry(
        f"https://texttospeech.googleapis.com/v1/text:synthesize?key={GOOGLE_TTS_API_KEY}",
        headers={"content-type": "application/json"},
        json_body={
            "input": {
                "prompt": config["gemini_tts_style_prompt"],
                "multiSpeakerMarkup": {
                    "turns": [
                        {"speaker": aliases[t["speaker"]], "text": t["spoken"]}
                        for t in batch
                    ]
                },
            },
            "voice": {
                "languageCode": config["google_tts_language_code"],
                "modelName": config["gemini_tts_model"],
                "multiSpeakerVoiceConfig": {
                    "speakerVoiceConfigs": [
                        {"speakerAlias": aliases[h["name"]], "speakerId": h["gemini_voice"]}
                        for h in config["hosts"]
                    ]
                },
            },
            "audioConfig": {"audioEncoding": "LINEAR16", "sampleRateHertz": 24000},
        },
        timeout=300,
        label="Gemini TTS",
    )
    return base64.b64decode(resp.json()["audioContent"])


def synthesize(text, voice_name, language_code, speaking_rate):
    resp = post_with_retry(
        f"https://texttospeech.googleapis.com/v1/text:synthesize?key={GOOGLE_TTS_API_KEY}",
        headers={"content-type": "application/json"},
        json_body={
            "input": {"text": text},
            "voice": {"languageCode": language_code, "name": voice_name},
            "audioConfig": {"audioEncoding": "MP3", "speakingRate": speaking_rate},
        },
        timeout=180,
        label="Google TTS",
    )
    return base64.b64decode(resp.json()["audioContent"])


def build_episode_audio(turns, config, out_path):
    if config.get("tts_engine", "gemini") == "gemini":
        return build_episode_audio_gemini(turns, config, out_path)
    return build_episode_audio_chirp3(turns, config, out_path)


def build_episode_audio_gemini(turns, config, out_path):
    """Multi-speaker path: a handful of conversational batches, not 175 fragments."""
    known = {h["name"] for h in config["hosts"]}
    prepared = []
    for turn in turns:
        if turn["speaker"] not in known:
            raise SystemExit(
                f"No voice configured for speaker '{turn['speaker']}'. "
                "Check the 'hosts' names in config.json."
            )
        spoken = normalize_for_speech(turn["text"])
        if spoken:
            prepared.append({**turn, "spoken": spoken})

    batches = batch_turns(prepared, config.get("multispeaker_batch_bytes", 3200))
    total_chars = sum(len(t["spoken"]) for t in prepared)
    print(f"  {len(prepared)} turns -> {len(batches)} multi-speaker requests")

    tmp_dir = os.path.join(ROOT, "_tmp_audio")
    os.makedirs(tmp_dir, exist_ok=True)

    combined = AudioSegment.silent(duration=400)
    # Google paces the turns inside a batch, so these joins are only for the seams.
    join_gap = AudioSegment.silent(duration=config.get("batch_gap_ms", 140))
    segment_gap = AudioSegment.silent(duration=config.get("segment_gap_ms", 900))
    previous_segment = batches[0][0].get("segment") if batches else None

    for index, batch in enumerate(batches):
        audio_bytes = synthesize_multispeaker(batch, config)
        wav_path = os.path.join(tmp_dir, f"batch_{index}.wav")
        with open(wav_path, "wb") as f:
            f.write(audio_bytes)
        clip = AudioSegment.from_file(wav_path, format="wav")
        os.remove(wav_path)

        if batch[0].get("segment") != previous_segment:
            combined += segment_gap
            previous_segment = batch[0].get("segment")
        elif index:
            combined += join_gap
        combined += clip
        print(f"    batch {index + 1}/{len(batches)}"
              f" ({len(batch)} turns, {len(combined) / 60000:.1f} min so far)")
        time.sleep(0.2)

    combined.export(out_path, format="mp3", bitrate="128k",
                    tags={"artist": config["podcast_author"], "album": config["podcast_title"]})
    print(f"  {total_chars:,} characters sent to TTS")
    return len(combined)


def build_episode_audio_chirp3(turns, config, out_path):
    """Original one-call-per-turn path. Kept as a fallback."""
    voices = {h["name"]: h["voice_name"] for h in config["hosts"]}
    language_code = config["google_tts_language_code"]
    speaking_rate = config.get("tts_speaking_rate", 1.0)
    turn_gap = AudioSegment.silent(duration=config.get("turn_gap_ms", 320))
    segment_gap = AudioSegment.silent(duration=config.get("segment_gap_ms", 900))

    tmp_dir = os.path.join(ROOT, "_tmp_audio")
    os.makedirs(tmp_dir, exist_ok=True)

    combined = AudioSegment.silent(duration=400)
    previous_segment = turns[0].get("segment") if turns else None
    total_chars = 0

    for index, turn in enumerate(turns):
        voice_name = voices.get(turn["speaker"])
        if not voice_name:
            raise SystemExit(
                f"No voice configured for speaker '{turn['speaker']}'. "
                "Check the 'hosts' names in config.json."
            )
        spoken = normalize_for_speech(turn["text"])
        if not spoken:
            continue
        total_chars += len(spoken)

        if turn.get("segment") != previous_segment:
            combined += segment_gap
            previous_segment = turn.get("segment")

        for part, chunk in enumerate(chunk_text(spoken)):
            audio_bytes = synthesize(chunk, voice_name, language_code, speaking_rate)
            seg_path = os.path.join(tmp_dir, f"seg_{index}_{part}.mp3")
            with open(seg_path, "wb") as f:
                f.write(audio_bytes)
            combined += AudioSegment.from_mp3(seg_path)
            os.remove(seg_path)
            time.sleep(0.15)  # be gentle on rate limits

        combined += turn_gap
        if (index + 1) % 25 == 0:
            print(f"    {index + 1}/{len(turns)} turns voiced ({len(combined) / 60000:.1f} min so far)")

    combined.export(out_path, format="mp3", bitrate="128k",
                    tags={"artist": config["podcast_author"], "album": config["podcast_title"]})
    print(f"  {total_chars:,} characters sent to TTS")
    return len(combined)  # milliseconds


def format_duration(ms):
    total_seconds = round(ms / 1000)
    hours, rest = divmod(total_seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"


# ------------------------------------------------------------ feed publishing

def write_episode_description(config, buckets, date_label):
    bits = []
    for sport, label in config["sport_labels"].items():
        titles = [i["title"] for i in buckets.get(sport, [])[:3]]
        if titles:
            bits.append(f"{label}: " + "; ".join(titles))
    body = " | ".join(bits) if bits else config["podcast_description"]
    return f"This week ({date_label}) across skate, MTB, snow and surf — {body}"


def save_episodes(episode_meta):
    episodes = []
    if os.path.exists(EPISODES_JSON):
        try:
            with open(EPISODES_JSON) as f:
                episodes = json.load(f)
        except (json.JSONDecodeError, OSError):
            episodes = []
    episodes = [e for e in episodes if e.get("guid") != episode_meta["guid"]]
    episodes.insert(0, episode_meta)
    with open(EPISODES_JSON, "w") as f:
        json.dump(episodes, f, indent=2)
    return episodes


def write_feed(config, episodes):
    esc = saxutils.escape
    base = PUBLIC_BASE_URL or "."
    cover_url = f"{base}/{config['cover_image']}"
    site_link = config.get("podcast_link") or base
    now = datetime.datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S GMT")

    items_xml = ""
    for ep in episodes:
        items_xml += f"""
    <item>
      <title>{esc(ep['title'])}</title>
      <link>{esc(ep['audio_url'])}</link>
      <description>{esc(ep['description'])}</description>
      <itunes:summary>{esc(ep['description'])}</itunes:summary>
      <itunes:author>{esc(config['podcast_author'])}</itunes:author>
      <itunes:duration>{esc(ep.get('duration', ''))}</itunes:duration>
      <itunes:episodeType>full</itunes:episodeType>
      <itunes:explicit>false</itunes:explicit>
      <itunes:image href="{esc(cover_url)}" />
      <pubDate>{ep['pub_date']}</pubDate>
      <enclosure url="{esc(ep['audio_url'])}" length="{ep['file_size']}" type="audio/mpeg" />
      <guid isPermaLink="false">{esc(ep['guid'])}</guid>
    </item>"""

    feed_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"
     xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"
     xmlns:atom="http://www.w3.org/2005/Atom"
     xmlns:content="http://purl.org/rss/1.0/modules/content/">
  <channel>
    <title>{esc(config['podcast_title'])}</title>
    <link>{esc(site_link)}</link>
    <atom:link href="{esc(base)}/feed.xml" rel="self" type="application/rss+xml" />
    <description>{esc(config['podcast_description'])}</description>
    <language>{esc(config['podcast_language'])}</language>
    <copyright>{esc(config['podcast_author'])}</copyright>
    <lastBuildDate>{now}</lastBuildDate>
    <generator>The Flat Spot pipeline</generator>
    <image>
      <url>{esc(cover_url)}</url>
      <title>{esc(config['podcast_title'])}</title>
      <link>{esc(site_link)}</link>
    </image>
    <itunes:author>{esc(config['podcast_author'])}</itunes:author>
    <itunes:subtitle>{esc(config.get('podcast_subtitle', ''))}</itunes:subtitle>
    <itunes:summary>{esc(config['podcast_description'])}</itunes:summary>
    <itunes:type>episodic</itunes:type>
    <itunes:explicit>false</itunes:explicit>
    <itunes:image href="{esc(cover_url)}" />
    <itunes:owner>
      <itunes:name>{esc(config['podcast_author'])}</itunes:name>
      <itunes:email>{esc(config.get('podcast_owner_email', ''))}</itunes:email>
    </itunes:owner>
    <itunes:category text="{esc(config.get('podcast_category', 'Sports'))}">
      <itunes:category text="{esc(config.get('podcast_subcategory', 'Wilderness'))}" />
    </itunes:category>{items_xml}
  </channel>
</rss>
"""
    with open(FEED_XML, "w") as f:
        f.write(feed_xml)


def write_index(config, episodes):
    esc = saxutils.escape
    base = PUBLIC_BASE_URL or "."
    rows = "\n".join(
        f"""      <li>
        <h2>{esc(ep['title'])}</h2>
        <p class="meta">{esc(ep['pub_date'])} &middot; {esc(ep.get('duration', ''))}</p>
        <audio controls preload="none" src="{esc(ep['audio_url'])}"></audio>
        <p>{esc(ep['description'])}</p>
      </li>"""
        for ep in episodes[:20]
    )
    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(config['podcast_title'])}</title>
<style>
  :root {{ color-scheme: dark; }}
  body {{ margin:0; background:#0a0a0a; color:#f2f2f2;
         font:16px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif; }}
  .wrap {{ max-width:820px; margin:0 auto; padding:48px 20px 80px; }}
  img.cover {{ width:min(320px,70vw); border-radius:14px; display:block; }}
  h1 {{ font-size:clamp(34px,7vw,60px); margin:28px 0 6px; letter-spacing:-.02em; }}
  .sub {{ color:#ccff00; font-weight:700; text-transform:uppercase; letter-spacing:.14em; font-size:13px; }}
  .lede {{ color:#b9b9b9; max-width:60ch; }}
  .subscribe {{ display:inline-block; margin:22px 0 40px; padding:12px 20px; border-radius:999px;
                background:#ccff00; color:#0a0a0a; font-weight:700; text-decoration:none; }}
  ul {{ list-style:none; padding:0; }}
  li {{ border-top:1px solid #222; padding:26px 0; }}
  li h2 {{ font-size:20px; margin:0 0 4px; }}
  .meta {{ color:#8a8a8a; font-size:13px; margin:0 0 12px; text-transform:uppercase; letter-spacing:.08em; }}
  audio {{ width:100%; margin-bottom:10px; }}
  p {{ color:#c9c9c9; }}
</style>
</head>
<body>
  <div class="wrap">
    <img class="cover" src="{esc(config['cover_image'])}" alt="{esc(config['podcast_title'])} cover art">
    <h1>{esc(config['podcast_title'])}</h1>
    <p class="sub">{esc(config.get('podcast_subtitle', ''))}</p>
    <p class="lede">{esc(config['podcast_description'])}</p>
    <a class="subscribe" href="{esc(base)}/feed.xml">RSS feed</a>
    <ul>
{rows}
    </ul>
  </div>
</body>
</html>
"""
    with open(INDEX_HTML, "w") as f:
        f.write(html)


def save_covered(used_links, history_days):
    today = datetime.date.today()
    keep_after = (today - datetime.timedelta(days=history_days * 2)).isoformat()
    covered = [row for row in load_covered_links() if row.get("date", "") >= keep_after]
    known = {row.get("link") for row in covered}
    covered.extend({"link": link, "date": today.isoformat()} for link in used_links if link not in known)
    with open(COVERED_JSON, "w") as f:
        json.dump(covered, f, indent=2)


# ---------------------------------------------------------------------- main

def smoke_test(config):
    """Voice a short scripted exchange so the TTS settings can be judged by ear
    without generating (or paying for) a whole episode. Needs only the Google key."""
    a, b = config["hosts"][0]["name"], config["hosts"][1]["name"]
    sample = [
        {"speaker": a, "text": "Right, before we do anything else, I need you to hear this one line.", "segment": "demo"},
        {"speaker": b, "text": "Go on.", "segment": "demo"},
        {"speaker": a, "text": "Santa Cruz redesigned the Blur. Completely. More travel, slacker geometry, the lot. And they did it without telling anyone first.", "segment": "demo"},
        {"speaker": b, "text": "Hang on. Without telling anyone? That's either enormous confidence or someone's had a very long week.", "segment": "demo"},
        {"speaker": a, "text": "Bit of both, I reckon.", "segment": "demo"},
        {"speaker": b, "text": "So what's it actually like to ride? Because the spec sheet says one thing and the trail usually says another.", "segment": "demo"},
    ]
    out = os.path.join(ROOT, "smoke_test.mp3")
    engine = config.get("tts_engine", "gemini")
    print(f"Voicing a {len(sample)}-turn sample with the '{engine}' engine...")
    ms = build_episode_audio(sample, config, out)
    print(f"\nWrote {out} ({format_duration(ms)}). Have a listen.")


def main():
    args = set(sys.argv[1:])
    feeds_only = "--feeds-only" in args
    script_only = "--script-only" in args

    config = load_config()
    os.makedirs(EPISODES_DIR, exist_ok=True)

    if "--smoke-test" in args:
        if not GOOGLE_TTS_API_KEY:
            raise SystemExit("GOOGLE_TTS_API_KEY is not set.")
        return smoke_test(config)

    if not feeds_only and not ANTHROPIC_API_KEY:
        raise SystemExit("ANTHROPIC_API_KEY is not set.")
    if not (feeds_only or script_only) and not GOOGLE_TTS_API_KEY:
        raise SystemExit("GOOGLE_TTS_API_KEY is not set.")

    today = datetime.date.today()
    date_label = today.strftime("%A %-d %B %Y")

    print(f"Fetching feeds (last {config['lookback_hours']} hours)...")
    buckets = collect_items_by_sport(config)
    buckets = drop_covered(buckets, load_covered_links(), config["history_days"])
    print("Story counts: " + ", ".join(f"{s}={len(v)}" for s, v in buckets.items()))

    if feeds_only:
        for sport, items in buckets.items():
            print(f"\n== {config['sport_labels'][sport]} ==")
            for item in items:
                print(f"  - {item['title']}  [{item['source']}]")
        return

    print(f"\nWriting the script with {config['anthropic_model']}...")
    turns, used_links = write_script(config, buckets, date_label)
    total_words = sum(len(t["text"].split()) for t in turns)
    print(f"Script complete: {len(turns)} turns, {total_words} words (~{total_words / 155:.0f} min)")

    os.makedirs(TRANSCRIPTS_DIR, exist_ok=True)
    transcript_path = os.path.join(TRANSCRIPTS_DIR, f"{today.isoformat()}.json")
    with open(transcript_path, "w") as f:
        json.dump(turns, f, indent=2)
    if script_only:
        print(f"Script written to {transcript_path}. Skipping audio.")
        return

    mp3_name = f"{today.isoformat()}.mp3"
    mp3_path = os.path.join(EPISODES_DIR, mp3_name)

    print("\nGenerating audio with Google Cloud TTS...")
    duration_ms = build_episode_audio(turns, config, mp3_path)
    file_size = os.path.getsize(mp3_path)
    print(f"  {mp3_name}: {format_duration(duration_ms)}, {file_size / 1_048_576:.1f} MB")

    audio_url = f"{PUBLIC_BASE_URL}/episodes/{mp3_name}" if PUBLIC_BASE_URL else f"episodes/{mp3_name}"
    episode_meta = {
        "title": f"{today.strftime('%-d %B %Y')} — {config['podcast_title']}",
        "description": write_episode_description(config, buckets, date_label),
        "pub_date": datetime.datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S GMT"),
        "audio_url": audio_url,
        "file_size": file_size,
        "duration": format_duration(duration_ms),
        "guid": f"the-flat-spot-{today.isoformat()}",
    }

    print("Updating feed.xml, episodes.json and index.html...")
    episodes = save_episodes(episode_meta)
    write_feed(config, episodes)
    write_index(config, episodes)
    save_covered(used_links, config["history_days"])
    print("Done.")


if __name__ == "__main__":
    main()
