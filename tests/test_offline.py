"""Offline checks for every part of the pipeline that does not need API keys.

Run with:  python tests/test_offline.py
"""
import sys, os, json, datetime, tempfile, xml.etree.ElementTree as ET

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import generate_episode as g

fails = []


def check(name, got, want):
    if got != want:
        fails.append(f"{name}\n   got:  {got!r}\n   want: {want!r}")
    else:
        print(f"  ok  {name}")


print("\n-- config sanity --")
cfg = g.load_config()
hosts = [h["name"] for h in cfg["hosts"]]
check("two hosts", len(hosts), 2)
check("segment words total matches target_word_count",
      sum(s["words"] for s in cfg["segments"]), cfg["target_word_count"])
sports_in_segments = {s for seg in cfg["segments"] for s in seg["sports"]}
check("segments only use known sports", sports_in_segments - set(cfg["sport_labels"]), set())
check("every feed has a known sport",
      {f["sport"] for f in cfg["feeds"]} - set(cfg["sport_labels"]), set())

print("\n-- number/speech normalisation --")
cases = [
    ("It dropped 12% overnight.", "It dropped twelve percent overnight."),
    ("The board costs £320.", "The board costs three hundred and twenty pounds."),
    ("A $1,250 frame.", "A one thousand two hundred and fifty dollars frame."),
    ("He took 3rd in 2026.", "He took third in twenty twenty six."),
    ("29.5 inches of travel.", "twenty nine point five inches of travel."),
    ("Back in 2009 and 2005.", "Back in two thousand and nine and two thousand and five."),
    ("A £2.5m deal.", "A two point five million pounds deal."),
    ("See https://example.com/x now", "See now"),
    ("**bold** and [x] gone", "bold and x gone"),
    ("Burton & Capita", "Burton and Capita"),
    ("140mm travel", "one hundred and forty millimetres travel"),
    ("A 29.5in wheel and 12 kg bike.", "A twenty nine point five inches wheel and twelve kilograms bike."),
    ("Hit 45mph on the 2.4km run.", "Hit forty five miles per hour on the two point four kilometres run."),
    ("Nothing to change here.", "Nothing to change here."),
    ("Turn 900 into words", "Turn nine hundred into words"),
]
for src, want in cases:
    check(repr(src), g.normalize_for_speech(src), want)

print("\n-- number_to_words --")
for n, want in [(0, "zero"), (7, "seven"), (21, "twenty one"), (100, "one hundred"),
                (115, "one hundred and fifteen"), (1000, "one thousand"),
                (1_250_000, "one million two hundred and fifty thousand")]:
    check(f"number_to_words({n})", g.number_to_words(n), want)

print("\n-- turn parsing --")
check("fenced json", g.parse_turns('```json\n[{"speaker":"Mackie","text":"Hi"}]\n```', "t"),
      [{"speaker": "Mackie", "text": "Hi"}])
check("prose either side",
      g.parse_turns('Sure!\n[{"speaker":"Tess","text":"Yo"}]\nHope that helps.', "t"),
      [{"speaker": "Tess", "text": "Yo"}])
check("drops empty turns",
      g.parse_turns('[{"speaker":"Tess","text":""},{"speaker":"Tess","text":"A"}]', "t"),
      [{"speaker": "Tess", "text": "A"}])

print("\n-- tts chunking --")
long_text = ("This is a sentence about snowboarding. " * 400).strip()
chunks = g.chunk_text(long_text)
check("all chunks under the byte limit",
      all(len(c.encode()) <= g.TTS_MAX_BYTES for c in chunks), True)
check("nothing dropped", "".join(chunks).replace(" ", ""), long_text.replace(" ", ""))
check("short text stays one chunk", len(g.chunk_text("Just one.")), 1)

print("\n-- interleave --")
check("round robin", g.interleave([[1, 2, 3], ["a"], ["x", "y"]]), [1, "a", "x", 2, "y", 3])

print("\n-- aggregator titles --")
check("google news publisher split",
      g.split_aggregator_title("Chloe Kim wins big air - ESPN", "'q' - Google News"),
      ("Chloe Kim wins big air", "ESPN"))
check("normal feed untouched",
      g.split_aggregator_title("HUF x Spitfire", "Free Skate Magazine"),
      ("HUF x Spitfire", "Free Skate Magazine"))

print("\n-- feed.xml / index.html / covered_links --")
tmp = tempfile.mkdtemp(prefix="flatspot-test-")
g.DOCS_DIR, g.FEED_XML = tmp, os.path.join(tmp, "feed.xml")
g.EPISODES_JSON = os.path.join(tmp, "episodes.json")
g.INDEX_HTML = os.path.join(tmp, "index.html")
g.COVERED_JSON = os.path.join(tmp, "covered_links.json")
g.PUBLIC_BASE_URL = "https://example.github.io/the-flat-spot"

ep = {
    "title": "6 August 2026 — The Flat Spot",
    "description": 'Ampersands & "quotes" <tags> should not break the XML',
    "pub_date": "Thu, 06 Aug 2026 05:40:00 GMT",
    "audio_url": "https://example.github.io/the-flat-spot/episodes/2026-08-06.mp3",
    "file_size": 28311552,
    "duration": "30:12",
    "guid": "the-flat-spot-2026-08-06",
}
episodes = g.save_episodes(ep)
g.save_episodes(dict(ep, guid="the-flat-spot-2026-08-06"))  # same guid twice
with open(g.EPISODES_JSON) as f:
    check("re-running the same day does not duplicate", len(json.load(f)), 1)

g.write_feed(cfg, episodes)
g.write_index(cfg, episodes)
root = ET.parse(g.FEED_XML).getroot()
check("feed parses as XML", root.tag, "rss")
ch = root.find("channel")
itunes = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"
check("channel title", ch.find("title").text, "The Flat Spot")
check("itunes:image present",
      ch.find(f"{itunes}image").get("href"),
      "https://example.github.io/the-flat-spot/cover.jpg")
check("itunes category", ch.find(f"{itunes}category").get("text"), "Sports")
check("one item", len(ch.findall("item")), 1)
enc = ch.find("item").find("enclosure")
check("enclosure type", enc.get("type"), "audio/mpeg")
check("enclosure length", enc.get("length"), "28311552")
check("item duration", ch.find("item").find(f"{itunes}duration").text, "30:12")
check("special chars survived escaping", ch.find("item").find("description").text,
      'Ampersands & "quotes" <tags> should not break the XML')
check("index.html written", os.path.exists(g.INDEX_HTML), True)

today = datetime.date.today()
old = (today - datetime.timedelta(days=90)).isoformat()
with open(g.COVERED_JSON, "w") as f:
    json.dump([{"link": "http://old", "date": old}, {"link": "http://keep", "date": today.isoformat()}], f)
g.save_covered(["http://new", "http://keep"], cfg["history_days"])
with open(g.COVERED_JSON) as f:
    links = {r["link"] for r in json.load(f)}
check("prunes stale links", "http://old" in links, False)
check("keeps recent + adds new", links, {"http://keep", "http://new"})

buckets = {"skate": [{"link": "http://a", "title": "A"}], "surf": [{"link": "http://b", "title": "B"}]}
covered = [{"link": "http://a", "date": today.isoformat()}]
out = g.drop_covered(buckets, covered, 21)
check("covered story dropped when alternatives exist... (skate had only one)",
      [i["link"] for i in out["skate"]], ["http://a"])  # falls back rather than emptying
buckets["skate"].append({"link": "http://c", "title": "C"})
out = g.drop_covered(buckets, covered, 21)
check("covered story dropped when alternatives exist",
      [i["link"] for i in out["skate"]], ["http://c"])

print("\n-- duration formatting --")
for ms, want in [(1000, "0:01"), (61_000, "1:01"), (1_812_000, "30:12"), (3_661_000, "1:01:01")]:
    check(f"format_duration({ms})", g.format_duration(ms), want)

print("\n" + ("=" * 60))
if fails:
    print(f"{len(fails)} FAILURE(S):")
    for f_ in fails:
        print(" - " + f_)
    sys.exit(1)
print("All offline checks passed.")
