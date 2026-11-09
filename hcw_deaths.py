#!/usr/bin/env python3
"""OSRS Hardcore Worlds death feed.

Same idea as the HCIM_Deaths2 account, but for the Hardcore Worlds hiscores
(https://secure.runescape.com/m=hiscore_oldschool_hardcore_mains/).

How it works: dead players stay on the hiscores with a skull icon whose title
reads "Player died: DD-Mon-YYYY HH:MM". We scan the overall table, collect those
rows, and announce any (name, death time) pair we haven't seen before.

Usage:
  python hcw_deaths.py                 # scan, print new deaths (dry run, nothing posted)
  python hcw_deaths.py --post          # also tweet them (needs X_* env vars below)
  python hcw_deaths.py --loop 600      # repeat every 600 seconds
  python hcw_deaths.py --max-pages 10  # only scan the top pages (quick test)

The very first run only records existing deaths (no flood of old tweets);
pass --announce-existing to tweet them anyway.

Posting: if X_AUTH_TOKEN and X_CT0 (browser cookies) are set, uses twifork.
Otherwise the X API v2 (POST /2/tweets) with OAuth 1.0a user keys from env:
  X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_SECRET
"""
import argparse
import base64
import hashlib
import hmac
import html
import io
import json
import os
import random
import re
import secrets
import sys
import time
import urllib.parse
import urllib.request

BASE = "https://secure.runescape.com/m=hiscore_oldschool_hardcore_mains/overall?table=0&page=%d"
UA = "hcw-death-feed/1.0 (hiscores death tracker)"
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seen_deaths.json")
ROW_RE = re.compile(r'<tr class="personal-hiscores__row([^"]*)">(.*?)</tr>', re.S)
TD_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
NAME_RE = re.compile(r'user1=([^"]+)">')
DIED_RE = re.compile(r'title="Player died: ([^"]*?)\s*"')


def parse_rows(page_html):
    """Return (all_row_count, dead_rows) for one hiscores page (overall or an activity table)."""
    rows = ROW_RE.findall(page_html)
    dead = []
    num = lambda x: int(re.sub(r"[^\d]", "", x) or 0)
    for cls, body in rows:
        if "--dead" not in cls:
            continue
        tds = TD_RE.findall(body)
        name_m, died_m = NAME_RE.search(body), DIED_RE.search(body)
        if not name_m:
            continue
        full = len(tds) >= 5  # overall table: rank, name, level, xp; activity tables only have rank, name, score
        dead.append({
            "name": html.unescape(urllib.parse.unquote_plus(name_m.group(1))),
            "rank": num(tds[1]) if full else 0,
            "level": num(tds[3]) if full else 0,
            "xp": num(tds[4]) if full else 0,
            "died": died_m.group(1).strip() if died_m else "unknown",
        })
    return len(rows), dead


def fetch(url):
    relay = os.environ.get("HCW_RELAY")  # Cloudflare Worker that fetches the list pages (blocked from GitHub IPs)
    if relay and "index_lite" not in url:
        req = urllib.request.Request(relay + "?u=" + urllib.parse.quote(url, safe=""), headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.read().decode("utf-8", "replace")
    try:
        from curl_cffi import requests as cr  # browser TLS fingerprint; needed from datacenter IPs
        r = cr.get(url, impersonate="chrome", timeout=30)
        r.raise_for_status()
        return r.text
    except ImportError:
        pass
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


ACT_URL = "https://secure.runescape.com/m=hiscore_oldschool_hardcore_mains/overall?category_type=1&table=%d&page=1"
FIRST_BOSS_TABLE = 20  # tables 3-19 are BH / clues / minigames; 20+ are the bosses


def boss_tables(page_html):
    ids = sorted({int(t) for t in re.findall(r"category_type=1&(?:amp;)?table=(\d+)", page_html)})
    return [t for t in ids if t >= FIRST_BOSS_TABLE]


def scan(max_pages, delay=0.5, bosses=True):
    """Dead players on the first max_pages overall pages (25 per page, so 4 = top 100) and, if
    bosses is set, on the front page of every boss hiscore. Deduplicated by name."""
    found, first = {}, ""
    def add(dead):
        for d in dead:
            cur = found.get(d["name"].lower())
            if cur is None or (cur["died"] == "unknown" and d["died"] != "unknown"):
                found[d["name"].lower()] = d
    for page in range(1, max_pages + 1):
        text = fetch(BASE % page)
        first = first or text
        count, dead = parse_rows(text)
        if count == 0:
            break
        add(dead)
        time.sleep(delay)
    if bosses:
        for t in boss_tables(first or fetch(BASE % 1)):
            try:
                add(parse_rows(fetch(ACT_URL % t))[1])
            except Exception as e:
                print("boss table %d failed: %s" % (t, e), file=sys.stderr)
            time.sleep(delay)
    return list(found.values())


def death_key(d):
    return "%s|%s" % (d["name"].lower(), d["died"] if d["died"] != "unknown" else "")


LITE = "https://secure.runescape.com/m=hiscore_oldschool_hardcore_mains/index_lite.json?player="
BOSS_TOP = 25  # list activities/bosses where the player ranks this high or better


def lookup(name):
    """Fetch a player's index_lite record; returns {} if the lookup fails."""
    try:
        return json.loads(fetch(LITE + urllib.parse.quote(name)))
    except Exception as e:
        print("lookup failed for %s: %s" % (name, e), file=sys.stderr)
        return {}


def top_activities(data):
    """[(rank, activity, score)] for bosses/activities ranked BOSS_TOP or better."""
    acts = [(a["rank"], a["name"], a["score"]) for a in data.get("activities", [])
            if 0 < a.get("rank", -1) <= BOSS_TOP and a.get("score", 0) > 0]
    return sorted(acts, key=lambda t: t[0])


# --- stats card image (needs Pillow; without it tweets are text-only) -------------------
# Layout copied from HCIM_Deaths2: 3 skill columns (+ EHP / EHB / total boxes), then the
# non-zero hiscores activities (clues, minigames, bosses) filled column by column.
ICON_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icons")
ICON_URL = "https://oldschool.runescape.wiki/images/"
CLOG_SIZE = 1609  # total collection log slots shown in the caption; bump when the game adds more
ROWS, ACT_COLS = 9, 7
# In-game stats tab order, 3 columns, row by row.
SKILL_GRID = ["Attack", "Hitpoints", "Mining", "Strength", "Agility", "Smithing", "Defence", "Herblore",
              "Fishing", "Ranged", "Thieving", "Cooking", "Prayer", "Crafting", "Firemaking", "Magic",
              "Fletching", "Woodcutting", "Runecraft", "Slayer", "Farming", "Construction", "Hunter", "Sailing"]
SKIP_ACTIVITIES = {"Grid Points", "League Points", "Deadman Points", "Bounty Hunter - Hunter",
                   "Bounty Hunter - Rogue", "Bounty Hunter (Legacy) - Hunter", "Bounty Hunter (Legacy) - Rogue",
                   "Clue Scrolls (all)", "LMS - Rank", "PvP Arena - Rank", "Collections Logged"}
ICON_FILES = {  # wiki file names where "<Name>_icon.png" doesn't exist
    "Clue Scrolls (beginner)": ["Clue_scroll_(beginner).png"], "Clue Scrolls (easy)": ["Clue_scroll_(easy).png"],
    "Clue Scrolls (medium)": ["Clue_scroll_(medium).png"], "Clue Scrolls (hard)": ["Clue_scroll_(hard).png"],
    "Clue Scrolls (elite)": ["Clue_scroll_(elite).png"], "Clue Scrolls (master)": ["Clue_scroll_(master).png"],
    "Chambers of Xeric": ["Olmlet.png"], "Chambers of Xeric: Challenge Mode": ["Olmlet.png"],
    "Tombs of Amascut": ["Tumeken's_guardian.png"], "Tombs of Amascut: Expert Mode": ["Tumeken's_guardian.png"],
    "The Gauntlet": ["Youngllef.png"],
    "Soul Wars Zeal": ["Soul_fragment.png"], "Rifts closed": ["Guardian_fragments.png"],
    "Colosseum Glory": ["Smol_Heredit.png"], "Artio": ["Callisto_cub.png"], "Spindel": ["Venenatis_spiderling.png"],
    "Calvar'ion": ["Vet'ion_Jr..png"], "Brutus": ["Brutus_slippers.png", "Beef.png"],
    "Barrows Chests": ["Dharok's_helm.png"], "Lunar Chests": ["Moon_key.png"],
    "The Corrupted Gauntlet": ["Corrupted_Youngllef.png"],
    "Theatre of Blood": ["Lil'_Zik.png"], "Theatre of Blood: Hard Mode": ["Lil'_Zik.png"],
    "Mimic": ["The_Mimic_icon.png"], "Nightmare": ["The_Nightmare_icon.png"],
    "Phosani's Nightmare": ["The_Nightmare_icon.png"],
}


def wiki_icon(name, files):
    """Cached wiki icon (PIL image) trying each candidate file name; None if none exists."""
    from PIL import Image
    os.makedirs(ICON_DIR, exist_ok=True)
    path = os.path.join(ICON_DIR, re.sub(r"[^\w.-]", "_", name) + ".png")
    if not os.path.exists(path):
        data = b""
        for f in files:
            try:
                data = fetch_bytes(ICON_URL + urllib.parse.quote(f))
                break
            except Exception:
                continue
        with open(path, "wb") as fh:  # an empty file caches a miss
            fh.write(data)
    try:
        return Image.open(path).convert("RGBA")
    except Exception:
        return None


HS_ICON_URL = "https://www.runescape.com/img/rsp777/game_icon_%s.png"  # the hiscores' own activity logos


def hs_icon(name):
    """Official hiscores logo for an activity (cached in icons_hs/); None if the site has none."""
    from PIL import Image
    d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icons_hs")
    os.makedirs(d, exist_ok=True)
    key = re.sub(r"[^a-z0-9]", "", name.lower())
    path = os.path.join(d, key + ".png")
    if not os.path.exists(path):
        try:
            data = fetch_bytes(HS_ICON_URL % key)
        except Exception:
            data = b""
        with open(path, "wb") as fh:
            fh.write(data)
    try:
        return Image.open(path).convert("RGBA")
    except Exception:
        return None


def icon_for(name, skill=False):
    base = name.replace(" ", "_")
    files = ICON_FILES.get(name, []) + [base + "_icon.png", base[:1].upper() + base[1:].lower() + "_icon.png"]
    return wiki_icon(("skill_" if skill else "act_") + name, files)


def fetch_bytes(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def _load_font(size, pixel_only=False):
    """Pixel-style font: drop a RuneScape font file named runescape_uf.ttf next to the script for
    the exact in-game look; otherwise fall back to a bold system font drawn without anti-aliasing."""
    from PIL import ImageFont
    here = os.path.dirname(os.path.abspath(__file__))
    for path in ((os.path.join(here, "runescape_small.ttf"),) if pixel_only else
                 (os.path.join(here, "runescape_small.ttf"), "arialbd.ttf", "DejaVuSans-Bold.ttf")):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return None if pixel_only else ImageFont.load_default()


ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
CELL_W, CELL_H, PITCH_Y = 61, 29, 30   # in-game stats-tab cell, cut from a real screenshot
ACT_W = 80                             # activity cells are the same cell, stretched to fit big numbers
SCALE = 3  # whole-number NEAREST upscale of the native-pixel board (never fractional)
_ig = {}


def _xp_for(level):
    pts = 0
    for l in range(1, level):
        pts += int(l + 300 * 2 ** (l / 7.0))
    return pts // 4


def _assets():
    if _ig:
        return _ig
    from PIL import Image
    A = lambda n: Image.open(os.path.join(ASSETS, n)).convert("RGBA")
    _ig["cell"] = A("ig_cell2.png")
    _ig["slash"] = A("ig_slash.png")
    _ig["atlas"] = A("ig_glyphs.png")
    _ig["label"] = A("ig_label.png")
    with open(os.path.join(ASSETS, "ig_glyphs.json")) as f:
        _ig["meta"] = json.load(f)
    c = _ig["cell"]
    wide = Image.new("RGBA", (ACT_W, CELL_H))
    wide.paste(c.crop((0, 0, 28, CELL_H)), (0, 0))
    mid = c.crop((8, 0, 40, CELL_H))
    for x in range(28, ACT_W - 28, 32):
        wide.paste(mid.crop((0, 0, min(32, ACT_W - 28 - x), CELL_H)), (x, 0))
    wide.paste(c.crop((CELL_W - 28, 0, CELL_W, CELL_H)), (ACT_W - 28, 0))
    _ig["wide"] = wide
    return _ig


def _digits(img, x, y, txt, right=False, center=False):
    """Draw OSRS small-font digits (and commas); x is the left edge, or the right edge if right=True."""
    from PIL import Image
    g = _assets()
    items = []
    for ch in txt:
        if ch == ",":
            items.append((None, 4, 3))
        elif ch in g["meta"]:
            w = g["meta"][ch]["w"]
            items.append((ch, w if ch == "1" else w + 1, w))  # the narrow "1" advances one less
    total = sum(a for _, a, _ in items) - (items[-1][1] - items[-1][2] if items else 0)
    if right:
        x -= total
    elif center:  # x is the middle of the yellow pixels (shadow and trailing gap not counted)
        x = int(x - (total - 1) / 2 + 0.5)
    for ch, adv, _w in items:
        if ch is None:
            for px, py, col in ((1, 7, (255, 255, 0)), (1, 8, (255, 255, 0)), (0, 9, (255, 255, 0)),
                                (2, 8, (0, 0, 0)), (2, 9, (0, 0, 0)), (1, 10, (0, 0, 0))):
                img.putpixel((int(x) + px, int(y) + py), col + (255,))
        else:
            m = g["meta"][ch]
            img.alpha_composite(g["atlas"].crop((m["x"], 0, m["x"] + m["w"], m["h"])), (int(x), int(y)))
        x += adv
    return total


def _bar_color(f):
    stops = [(0.0, (255, 40, 0)), (0.3, (255, 150, 0)), (0.55, (220, 210, 0)), (0.8, (110, 200, 0)), (1.0, (40, 200, 0))]
    for (a, ca), (b, cb) in zip(stops, stops[1:]):
        if f <= b:
            t = (f - a) / (b - a)
            return tuple(int(ca[i] + (cb[i] - ca[i]) * t) for i in range(3))
    return stops[-1][1]


def _board(ncols, nrows):
    """In-game stats tab (wiki Skills_tab.png) stretched to ncols x nrows cells: real frame, rivets, total bar."""
    from PIL import Image
    t = Image.open(os.path.join(ASSETS, "skills_tab.png")).convert("RGBA")
    cols = [(0, 72), (72, 135), (135, 204)]
    rows = [(0, 39), (39, 69), (219, 275)]
    W = 72 + 63 * (ncols - 2) + 69
    H = 39 + 30 * (nrows - 2) + 56
    out = Image.new("RGBA", (W, H))
    ys = [0, 39 + 30 * (nrows - 2)]
    y = 0
    for ri, (y0, y1) in enumerate(rows):
        reps = (nrows - 2) if ri == 1 else 1
        for _ in range(reps):
            x = 0
            for ci, (x0, x1) in enumerate(cols):
                creps = (ncols - 2) if ci == 1 else 1
                for _ in range(creps):
                    out.paste(t.crop((x0, y0, x1, y1)), (x, y))
                    x += x1 - x0
            y += y1 - y0
    return out


def render_card(d, data):
    """PNG bytes of the in-game OSRS stats tab (original sprite from the wiki) extended with the player's
    activity cells, or None if Pillow/assets/data are missing."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None
    skills = {x["name"]: x for x in data.get("skills", [])}
    if not skills:
        return None
    try:
        g = _assets()
        ROWS_ = 8
        acts = [(a["name"], a["score"]) for a in data.get("activities", [])
                if a.get("score", 0) > 0 and a["name"] not in SKIP_ACTIVITIES][:ROWS_ * 8]
        n = max(2, -(-len(acts) // ROWS_))  # at least 5 columns so the bar has room for the caption
        board = _board(3 + n, ROWS_)
    except OSError:
        return None
    img = board
    dr = ImageDraw.Draw(img)
    W, H = img.size
    BLANK = g["cell"]
    ox = lambda j: 9 if j == 0 else 72 + 63 * (j - 1)
    oy = lambda r: 9 + 30 * r

    def icon(ic, x, y, w, h):
        if ic is None:
            return
        ic = ic.convert("RGBA")
        # never resample: icons are drawn 1:1 (centre-cropped if oversized); only the final whole-card
        # upscale below happens, as an integer NEAREST multiple
        if ic.width > w or ic.height > h:
            l, t = max(0, (ic.width - w) // 2), max(0, (ic.height - h) // 2)
            ic = ic.crop((l, t, l + min(w, ic.width), t + min(h, ic.height)))
        img.alpha_composite(ic, (x + (w - ic.width) // 2, y + (h - ic.height) // 2))

    for r in range(ROWS_):
        for j in range(3 + n):
            img.alpha_composite(BLANK, (ox(j), oy(r)))
    dr.rectangle([14, 253, W - 15, 262], fill=(0, 0, 1, 255))  # wipe the sample "Total level" text
    for i, name in enumerate(SKILL_GRID):
        x, y = ox(i % 3), oy(i // 3)
        if y > oy(ROWS_ - 1):
            break
        sk = skills.get(name)
        lvl = sk["level"] if sk else 1
        img.alpha_composite(g["slash"], (x, y))
        try:
            img.paste(Image.open(os.path.join(ASSETS, "skill_icons", name + ".png")).convert("RGBA"), (x + 3, y + 3))
        except OSError:
            icon(icon_for(name, skill=True), x + 3, y + 2, 30, 24)
        _digits(img, x + 38, y + 4, str(lvl), center=True)
        _digits(img, x + 50, y + 16, str(lvl), center=True)
    for i, (name, score) in enumerate(acts):
        x, y = ox(3 + i // ROWS_), oy(i % ROWS_)
        icon(hs_icon(name) or icon_for(name), x + 3, y + 2, 26, 25)
        _digits(img, x + 56, y + 10, f"{score:,}", right=True)
    # Bar: "<name> - Rank N - Clog x/y" then "Total level: N", the whole line centred in the bar
    tot = str(skills.get('Overall', {}).get('level', d['level']))
    lab = g["label"]
    nw = _digits(Image.new("RGBA", (200, 14)), 0, 0, tot)
    tw = lab.width + 4 + nw
    clog = next((a["score"] for a in data.get("activities", []) if a["name"] == "Collections Logged" and a["score"] > 0), None)
    rank = f"{d['rank']:,}"
    f = _load_font(16, pixel_only=True)
    dr.fontmode = "1"
    cap = ""
    if f is not None:
        for cand in ("%s - Rank %s - Clog %d/%d" % (d["name"], rank, clog or 0, CLOG_SIZE) if clog else None,
                     "%s - Rank %s" % (d["name"], rank), d["name"]):
            if cand and dr.textlength(cand, font=f) + 14 + tw <= W - 30:
                cap = cand
                break
    capw = int(dr.textlength(cap, font=f)) if cap else 0
    gap = 14 if cap else 0
    x0 = (W - (capw + gap + tw)) // 2
    if cap:
        dr.text((x0 + 1, 254), cap, font=f, fill=(0, 0, 0, 255))
        dr.text((x0, 253), cap, font=f, fill=(255, 255, 0, 255))
    sx = x0 + capw + gap
    img.alpha_composite(lab, (sx, 254))
    _digits(img, sx + lab.width + 4, 254, tot)
    img = img.resize((W * SCALE, H * SCALE), Image.NEAREST)
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "PNG")
    return buf.getvalue()


def tweet_text(d):
    head = "%s has died!\n Rank %s Overall with %s XP\n--" % (d["name"], f"{d['rank']:,}", f"{d['xp']:,}")
    lines = ["Rank %d %s - %s KC" % (r, n, f"{k:,}") for r, n, k in d.get("activities", [])]
    while lines and len("\n".join([head] + lines + ["--"])) > 280:
        lines.pop()  # drop the lowest-ranked lines rather than cutting mid-line
    return "\n".join([head] + lines + ["--"])


def _pct(s):
    return urllib.parse.quote(str(s), safe="~")


def _oauth_header(method, url):
    keys = {k: os.environ.get(k) for k in ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_SECRET")}
    missing = [k for k, v in keys.items() if not v]
    if missing:
        raise RuntimeError("missing env vars: " + ", ".join(missing))
    oauth = {
        "oauth_consumer_key": keys["X_API_KEY"],
        "oauth_nonce": secrets.token_hex(16),
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": str(int(time.time())),
        "oauth_token": keys["X_ACCESS_TOKEN"],
        "oauth_version": "1.0",
    }
    # JSON and multipart bodies are not part of the signature base string.
    param_str = "&".join("%s=%s" % (_pct(k), _pct(v)) for k, v in sorted(oauth.items()))
    base = "&".join([method, _pct(url), _pct(param_str)])
    signing_key = "%s&%s" % (_pct(keys["X_API_SECRET"]), _pct(keys["X_ACCESS_SECRET"]))
    oauth["oauth_signature"] = base64.b64encode(hmac.new(signing_key.encode(), base.encode(), hashlib.sha1).digest()).decode()
    return "OAuth " + ", ".join('%s="%s"' % (_pct(k), _pct(v)) for k, v in sorted(oauth.items()))


def upload_image(png):
    """Upload a PNG via the X media endpoint; returns the media id string."""
    url = "https://api.x.com/2/media/upload"
    boundary = secrets.token_hex(16)
    crlf = b"\r\n"
    body = b"".join([
        b"--" + boundary.encode() + crlf,
        b'Content-Disposition: form-data; name="media_category"' + crlf + crlf + b"tweet_image" + crlf,
        b"--" + boundary.encode() + crlf,
        b'Content-Disposition: form-data; name="media"; filename="card.png"' + crlf,
        b"Content-Type: image/png" + crlf + crlf + png + crlf,
        b"--" + boundary.encode() + b"--" + crlf,
    ])
    req = urllib.request.Request(url, data=body, headers={
        "Authorization": _oauth_header("POST", url),
        "Content-Type": "multipart/form-data; boundary=" + boundary, "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        resp = json.load(r)
    return str(resp.get("data", {}).get("id") or resp.get("media_id_string"))


def _post_cookies(text, png):
    """Unofficial posting via twifork (pip install twifork==2.4.0) using browser cookies."""
    import asyncio
    import tempfile
    from twikit import Client

    async def go():
        c = Client("en-US")
        c.set_cookies({"auth_token": os.environ["X_AUTH_TOKEN"], "ct0": os.environ["X_CT0"]})
        ids = None
        if png:
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
                f.write(png)
            try:
                ids = [await c.upload_media(f.name)]
            finally:
                os.unlink(f.name)
        await c.create_tweet(text=text, media_ids=ids)

    asyncio.run(go())


def post_tweet(text, png=None):
    if os.environ.get("X_AUTH_TOKEN") and os.environ.get("X_CT0"):
        return _post_cookies(text, png)
    media_id = upload_image(png) if png else None
    url = "https://api.twitter.com/2/tweets"
    body = {"text": text}
    if media_id:
        body["media"] = {"media_ids": [media_id]}
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"Authorization": _oauth_header("POST", url), "Content-Type": "application/json", "User-Agent": UA},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return set(json.load(f))
    except FileNotFoundError:
        return None


def save_state(seen):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(seen), f)


def run_once(args):
    deaths = scan(args.max_pages, bosses=not args.no_bosses)
    seen = load_state()
    first_run = seen is None
    seen = seen or set()
    new = [d for d in deaths if death_key(d) not in seen]
    # Oldest first so the feed reads chronologically.
    def when(d):
        try:
            return time.strptime(d["died"], "%d-%b-%Y %H:%M")
        except ValueError:
            return time.gmtime(0)
    new.sort(key=when)
    announce = new if (not first_run or args.announce_existing) else []
    print("scanned: %d dead players, %d new%s" % (
        len(deaths), len(new), " (first run: recording only)" if first_run and not announce else ""))
    for d in announce:
        data = lookup(d["name"])
        ov = next((x for x in data.get("skills", []) if x["name"] == "Overall"), None)
        if ov and not d["rank"]:  # found on a boss table: take overall figures from the player's record
            d["rank"], d["level"], d["xp"] = ov["rank"], ov["level"], ov["xp"]
        d["activities"] = top_activities(data)
        text = tweet_text(d)
        png = render_card(d, data)
        if args.save_cards and png:
            os.makedirs("cards", exist_ok=True)
            with open(os.path.join("cards", re.sub(r"[^\w-]", "_", d["name"]) + ".png"), "wb") as f:
                f.write(png)
        print("-" * 40 + "\n" + text)
        if args.post:
            try:
                post_tweet(text, png)
            except Exception as e:  # keep it unseen so we retry next scan
                print("post failed for %s: %s" % (d["name"], e), file=sys.stderr)
                continue
        seen.add(death_key(d))
    if first_run and not announce:
        seen.update(death_key(d) for d in new)  # baseline: don't announce old deaths
        save_state(seen)
    elif args.post:
        save_state(seen)  # dry runs never mark deaths as seen


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--post", action="store_true", help="actually tweet (default: dry run)")
    ap.add_argument("--loop", type=int, metavar="SECONDS", help="repeat forever")
    ap.add_argument("--max-pages", type=int, default=4, help="overall hiscore pages to scan, 25 players each (4 = top 100)")
    ap.add_argument("--no-bosses", action="store_true", help="skip the front page of each boss hiscore")
    ap.add_argument("--save-cards", action="store_true", help="write each stats card PNG to ./cards")
    ap.add_argument("--test-tweet", metavar="NAME", help="post one test tweet with NAME's card, then exit")
    ap.add_argument("--announce-existing", action="store_true")
    args = ap.parse_args()
    if args.test_tweet:
        data = lookup(args.test_tweet)
        d = {"name": args.test_tweet, "rank": 0, "level": 0, "xp": 0, "died": "test"}
        ov = next((x for x in data.get("skills", []) if x.get("name") == "Overall"), None)
        if ov:
            d["rank"], d["level"], d["xp"] = ov["rank"], ov["level"], ov["xp"]
        d["activities"] = top_activities(data)
        post_tweet("Test tweet - HCW death bot is live.", render_card(d, data))
        print("test tweet posted")
        return
    sys.stdout.reconfigure(encoding="utf-8")  # Windows consoles choke on the skull emoji
    while True:
        run_once(args)
        if not args.loop:
            break
        time.sleep(args.loop)


if __name__ == "__main__":
    main()
