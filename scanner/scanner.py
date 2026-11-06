#!/usr/bin/env python3
"""Vinted vintage scanner.

Hourly pipeline (the Claude cloud environment cannot reach vinted.fr, so
GitHub Actions does the searching):

  GitHub Actions:  python3 scanner.py feed --data DATA_DIR
      Queries Vinted and refreshes DATA_DIR/feed.json + seen.json, pushed to
      the `radar-data` branch.

  Claude task:     python3 -I scanner.py from-feed --feed feed.json --state STATE_DIR --out OUT_DIR
      Same outputs as `scan` below, built from the feed instead of Vinted.

Direct mode (when this machine can reach vinted.fr):

  python3 -I scanner.py scan --state STATE_DIR --out OUT_DIR
      Reads the artifact's current rows (saved by ArtifactData with out_dir),
      queries Vinted for the newest listings, keeps the ones that match the
      sizes and quality criteria in config.json, and writes:
        OUT_DIR/candidates.json   compact list for Claude to review
        OUT_DIR/docs/<id>.json    full row per candidate (with thumbnail)
        OUT_DIR/prune.json        ids of old rows to delete

  python3 -I scanner.py finalize --out OUT_DIR --review review.json [--versions v.json]
      review.json = {"<id>": "avis en une phrase", ...}  (ids to keep)
      Merges the avis into the docs and writes OUT_DIR/batches/batch_N.json,
      each a ready-to-send `writes` array for ArtifactData action "batch".
"""
import argparse
import base64
import datetime as dt
import glob
import http.cookiejar
import io
import json
import os
import random
import re
import sys
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = "https://www.vinted.fr"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/141.0 Safari/537.36")


def now_iso():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def load_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def dump_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)


# --------------------------------------------------------------------------
# State saved by ArtifactData (out_dir). Tolerant about the file layout:
# either the bare document, or {"data": {...}, "version": n}.
# --------------------------------------------------------------------------
def read_state_docs(state_dir, collection):
    docs = {}
    for path in glob.glob(os.path.join(state_dir, "**", collection, "*.json"), recursive=True):
        raw = load_json(path, {})
        doc_id = os.path.splitext(os.path.basename(path))[0]
        body = raw.get("data") if isinstance(raw.get("data"), dict) else raw
        version = raw.get("version") or raw.get("__version__") or body.get("version")
        docs[doc_id] = {"body": body, "version": version}
    return docs


# --------------------------------------------------------------------------
# Vinted HTTP
# --------------------------------------------------------------------------
class Vinted:
    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def _get(self, url, accept="application/json, text/plain, */*", timeout=25):
        req = urllib.request.Request(url, headers={
            "User-Agent": UA,
            "Accept": accept,
            "Accept-Language": "fr-FR,fr;q=0.9",
            "Referer": BASE + "/",
        })
        with self.opener.open(req, timeout=timeout) as r:
            return r.read()

    def warm_up(self):
        self._get(BASE + "/", accept="text/html,application/xhtml+xml")
        if not any(c.name == "access_token_web" for c in self.jar):
            # The session cookie alone is often enough; keep going.
            pass

    def search(self, params):
        q = {"page": 1, "per_page": 96, "order": "newest_first"}
        q.update(params)
        url = BASE + "/api/v2/catalog/items?" + urllib.parse.urlencode(q, doseq=True)
        data = json.loads(self._get(url).decode("utf-8"))
        return data.get("items", [])

    def image(self, url):
        return self._get(url, accept="image/avif,image/webp,image/*,*/*", timeout=20)


LABEL_RE = re.compile(
    r"^(?P<title>.*?), Marque: (?P<brand>.*?), État: (?P<cond>.*?)"
    r"(?:, Taille: (?P<size>.*?))?, (?P<price>[\d\s.,]+) €(?:, (?P<total>[\d\s.,]+) €)?\s*$")


def _num(s):
    if not s:
        return None
    s = s.replace("\u202f", "").replace(" ", "")
    if "," in s and "." not in s:
        s = s.replace(",", ".")
    s = s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


def parse_card(card):
    """Turn a catalog card (from vinted_fetch.js) into the API-like item
    shape the scorer expects."""
    m = LABEL_RE.match(card.get("label") or card.get("alt") or "")
    if not m:
        return None
    href = (card.get("href") or "").split("?")[0]
    return {
        "id": card["id"],
        "title": m.group("title").strip(),
        "brand_title": m.group("brand").strip(),
        "status": m.group("cond").strip(),
        "size_title": (m.group("size") or "").strip(),
        "price": {"amount": _num(m.group("price")), "currency_code": "EUR"},
        "total_item_price": {"amount": _num(m.group("total"))},
        "url": href or "%s/items/%s" % (BASE, card["id"]),
        "photo": {"url": card.get("img") or None},
    }


def fetch_catalogs(cfg):
    """Run vinted_fetch.js once for every query (one browser session)."""
    import subprocess
    import tempfile
    qs = [{"text": q["text"], "catalog": q.get("catalog", cfg["catalog_men"])} for q in cfg["queries"]]
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(qs, f)
        qpath = f.name
    try:
        proc = subprocess.run(["node", os.path.join(HERE, "vinted_fetch.js"), qpath],
                              capture_output=True, text=True, timeout=900)
    finally:
        os.unlink(qpath)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or "navigateur en échec").strip().splitlines()[-1][:300])
    raw = json.loads(proc.stdout)
    out = {"_errors": raw.pop("_errors", {})}
    for k, cards in raw.items():
        out[k] = [it for it in (parse_card(c) for c in cards) if it]
    for k, v in out["_errors"].items():
        print("query failed:", k, v, file=sys.stderr)
    return out


# --------------------------------------------------------------------------
# Filtering and scoring
# --------------------------------------------------------------------------
def norm(s):
    return (s or "").lower()


def size_bucket(size_title, cfg):
    """Return 'chaussures', 'bas', 'haut' or None when the size does not fit."""
    s = (size_title or "").strip()
    if not s:
        return None
    sizes = cfg["sizes"]
    m = re.fullmatch(r"(\d{2})(?:[.,](5))?", s)
    if m:
        val = m.group(1) + (".5" if m.group(2) else "")
        return "chaussures" if val in sizes["shoes_eu"] else None
    tokens = re.split(r"[\s/|,()]+", s.upper())
    tokens = [t for t in tokens if t]
    if any(re.fullmatch(r"W\d{2}", t) for t in tokens):
        return "bas" if any(t in sizes["waist"] for t in tokens) else None
    if any(t in sizes["tops"] for t in tokens):
        return "haut"
    return None


def classify(item, bucket):
    if bucket == "chaussures":
        return "chaussures"
    t = norm(item.get("title"))
    if re.search(r"\b(jeans?|pantalon|pantalones|pants|chino|cargo|short|trousers?|salopette|overall|dungarees|501|505|517|615)\b", t):
        return "bas"
    if re.search(r"(veste|jacket|jacke|giacca|giubbotto|chaqueta|blouson|manteau|parka|coat|trench|bomber|caban|gilet|weste|waxed|wachs|duffle|harrington|anorak|doudoune|beaufort|bedale|border|blazer|sakko|sherpa)", t):
        return "vestes"
    return "hauts"


def score_item(item, cfg):
    title = norm(item.get("title"))
    brand = norm(item.get("brand_title"))
    text = title + " " + brand
    reasons = []

    for bad in cfg["exclude_words"]:
        if re.search(r"(?<!\w)" + re.escape(bad) + r"(?!\w)", text):
            return None, ["exclu: " + bad]

    score = 0.0
    tier = None
    for name, info in cfg["brands"].items():
        if norm(name) in brand or any(norm(a) in text for a in info.get("aliases", [])):
            tier = info["tier"]
            break
    if tier is None:
        return None, ["marque hors liste"]
    score += {"A": 4, "B": 2.5, "C": 1}[tier]
    reasons.append("marque " + tier)

    for word, pts in cfg["quality_words"].items():
        if re.search(r"(?<!\w)" + re.escape(norm(word)) + r"(?!\w)", text):
            score += pts
            reasons.append(word)

    cond = norm(item.get("status"))
    cond_pts = {"neuf avec étiquette": 2, "neuf sans étiquette": 1.5,
                "très bon état": 1.2, "bon état": 0.4, "satisfaisant": -1}
    for k, v in cond_pts.items():
        if cond.startswith(k):
            score += v
            break

    price = price_value(item)
    if price is not None:
        cap = cfg["max_price"]
        if price > cap:
            return None, ["trop cher"]
        if price <= cap * 0.25:
            score += 1
            reasons.append("bon prix")
        elif price > cap * 0.7:
            score -= 0.5

    fav = item.get("favourite_count") or 0
    if fav >= 5:
        score += 0.5
    return round(score, 2), reasons


def price_value(item):
    p = item.get("price")
    if isinstance(p, dict):
        p = p.get("amount")
    try:
        return float(p)
    except (TypeError, ValueError):
        return None


def thumb_url(item):
    photo = item.get("photo") or {}
    best = None
    for th in photo.get("thumbnails") or []:
        t = th.get("type", "")
        if "310x430" in t:
            return th.get("url")
        if "150x210" in t:
            best = th.get("url")
    return best or photo.get("url")


def listed_at(item):
    photo = item.get("photo") or {}
    ts = (photo.get("high_resolution") or {}).get("timestamp")
    if ts:
        return dt.datetime.fromtimestamp(int(ts), dt.timezone.utc).replace(microsecond=0).isoformat()
    return None


def make_thumb(api, url):
    if not url:
        return None
    raw = api.image(url)
    try:
        from PIL import Image  # optional
        im = Image.open(io.BytesIO(raw)).convert("RGB")
        im.thumbnail((210, 280))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=68, optimize=True)
        raw, mime = buf.getvalue(), "image/jpeg"
    except Exception:
        mime = "image/webp" if raw[8:12] == b"WEBP" else "image/jpeg"
    if len(raw) > 120_000:
        return None
    return "data:%s;base64,%s" % (mime, base64.b64encode(raw).decode("ascii"))


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------
def collect(cfg, skip_ids):
    """Query Vinted. Returns (status, docs) with docs best-first."""
    status = {"last_run": now_iso(), "ok": False, "scanned": 0, "matched": 0,
              "queries": len(cfg["queries"]), "error": None}
    api = Vinted()
    seen, candidates = set(), []
    try:
        results = fetch_catalogs(cfg)
    except Exception as e:  # network policy, anti-bot, browser missing, ...
        status["error"] = "Accès à vinted.fr impossible : %s" % e
        return status, []
    errors = len(results.pop("_errors", {}) or {})

    for q in cfg["queries"]:
        for it in results.get(q["text"], []):
            iid = str(it.get("id"))
            status["scanned"] += 1
            if iid in seen or iid in skip_ids:
                continue
            seen.add(iid)
            bucket = size_bucket(it.get("size_title"), cfg)
            if not bucket:
                continue
            sc, reasons = score_item(it, cfg)
            if sc is None or sc < cfg["min_score"]:
                continue
            candidates.append((sc, reasons, bucket, it))

    candidates.sort(key=lambda c: -c[0])
    candidates = candidates[: cfg["max_candidates"]]
    docs = []
    for sc, reasons, bucket, it in candidates:
        iid = str(it["id"])
        doc = {
            "vinted_id": iid,
            "title": it.get("title"),
            "brand": it.get("brand_title"),
            "size": it.get("size_title"),
            "condition": it.get("status"),
            "price": price_value(it),
            "total_price": price_value({"price": it.get("total_item_price")}),
            "currency": (it.get("price") or {}).get("currency_code", "EUR") if isinstance(it.get("price"), dict) else "EUR",
            "url": it.get("url") or "%s/items/%s" % (BASE, iid),
            "category": classify(it, bucket),
            "score": sc,
            "tags": reasons,
            "favourites": it.get("favourite_count") or 0,
            "seller": (it.get("user") or {}).get("login"),
            "listed_at": listed_at(it),
            "found_at": now_iso(),
            "avis": "",
            "thumb": None,
        }
        try:
            doc["thumb"] = make_thumb(api, thumb_url(it))
        except Exception as e:
            print("thumb failed", iid, e, file=sys.stderr)
        docs.append(doc)

    n = len(cfg["queries"])
    status.update(ok=errors < n, matched=len(docs),
                  error=None if errors < n else "Toutes les recherches Vinted ont échoué")
    if errors and status["ok"]:
        status["error"] = "%d recherche(s) sur %d en échec" % (errors, n)
    return status, docs


def write_out(out, cfg, status, docs, existing):
    """Write candidates/docs/prune/status for the review + finalize steps."""
    os.makedirs(os.path.join(out, "docs"), exist_ok=True)
    compact = []
    for doc in docs:
        if doc["vinted_id"] in existing:
            continue
        dump_json(os.path.join(out, "docs", doc["vinted_id"] + ".json"), doc)
        compact.append({k: doc.get(k) for k in ("vinted_id", "title", "brand", "size", "condition",
                                                 "price", "category", "score", "tags")})
    # Rows older than keep_days, or past max_rows, are pruned so the store
    # stays light (saved pieces are kept per viewer, with their own copy).
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=cfg["keep_days"])
    prune = []
    rows = sorted(existing.items(), key=lambda kv: kv[1]["body"].get("found_at") or "", reverse=True)
    for rank, (doc_id, d) in enumerate(rows):
        found = d["body"].get("found_at") or ""
        try:
            too_old = bool(found) and dt.datetime.fromisoformat(found) < cutoff
        except ValueError:
            too_old = False
        if too_old or rank >= cfg["max_rows"]:
            prune.append(doc_id)
    status["matched"] = len(compact)
    dump_json(os.path.join(out, "candidates.json"), compact)
    dump_json(os.path.join(out, "prune.json"), prune)
    dump_json(os.path.join(out, "status.json"), status)
    print(json.dumps({**status, "candidates": len(compact), "prune": len(prune)}, ensure_ascii=False))
    return 0 if status["ok"] else 2


def load_cfg(args):
    return load_json(args.config or os.path.join(HERE, "config.json"))


def cmd_scan(args):
    """Direct mode: this machine queries Vinted itself."""
    cfg = load_cfg(args)
    existing = read_state_docs(args.state, "items") if args.state else {}
    status, docs = collect(cfg, set(existing))
    return write_out(args.out, cfg, status, docs, existing)


def cmd_feed(args):
    """GitHub Actions mode: refresh DATA/feed.json (rolling window of recent
    candidates) and DATA/seen.json (ids already offered, for dedupe)."""
    cfg = load_cfg(args)
    data = args.data
    os.makedirs(data, exist_ok=True)
    seen = load_json(os.path.join(data, "seen.json"), {}) or {}
    feed = load_json(os.path.join(data, "feed.json"), {}) or {}
    status, docs = collect(cfg, set(seen))
    now = dt.datetime.now(dt.timezone.utc)
    window = now - dt.timedelta(hours=cfg.get("feed_hours", 12))
    keep = []
    for d in feed.get("items", []):
        try:
            if dt.datetime.fromisoformat(d.get("found_at", "")) >= window:
                keep.append(d)
        except ValueError:
            pass
    new_ids = {d["vinted_id"] for d in docs}
    items = docs + [d for d in keep if d["vinted_id"] not in new_ids]
    for d in docs:
        seen[d["vinted_id"]] = d["found_at"]
    horizon = (now - dt.timedelta(days=cfg["keep_days"] + 4)).isoformat()
    seen = {k: v for k, v in seen.items() if v >= horizon}
    dump_json(os.path.join(data, "feed.json"), {"generated_at": now_iso(), "status": status, "items": items})
    dump_json(os.path.join(data, "seen.json"), seen)
    print(json.dumps({**status, "new": len(docs), "feed_items": len(items)}, ensure_ascii=False))
    return 0  # always publish the feed, even when Vinted refused us


def cmd_from_feed(args):
    """Claude mode: turn the feed published by GitHub Actions into rows."""
    cfg = load_cfg(args)
    existing = read_state_docs(args.state, "items") if args.state else {}
    feed = load_json(args.feed, None)
    if not isinstance(feed, dict):
        status = {"last_run": now_iso(), "ok": False, "scanned": 0, "matched": 0,
                  "error": "Flux GitHub illisible ou absent : le scan GitHub Actions n'a peut-être pas encore tourné."}
        return write_out(args.out, cfg, status, [], existing)
    status = dict(feed.get("status") or {})
    status["feed_generated_at"] = feed.get("generated_at")
    try:
        age = dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(feed.get("generated_at"))
        if age > dt.timedelta(hours=3):
            status["ok"] = False
            status["error"] = ("Le scan GitHub Actions n'a pas tourné depuis %d h : vérifie l'onglet Actions du dépôt."
                               % (age.total_seconds() // 3600))
    except (TypeError, ValueError):
        pass
    return write_out(args.out, cfg, status, feed.get("items", []), existing)


def cmd_finalize(args):
    out = args.out
    review = load_json(args.review, {}) or {}
    status = load_json(os.path.join(out, "status.json"), {}) or {}
    prune = load_json(os.path.join(out, "prune.json"), []) or []
    writes = []
    for iid, avis in review.items():
        path = os.path.join(out, "docs", "%s.json" % iid)
        doc = load_json(path)
        if not doc:
            continue
        doc["avis"] = (avis or "").strip()[:280]
        dump_json(path, doc)
        writes.append({"op": "set", "collection": "items", "doc_id": str(iid), "file_path": path})
    # ArtifactData's out_dir files carry no version; the list result prints
    # one per document. --versions is {"<doc_id>": version} copied from it.
    versions = load_json(args.versions, {}) if args.versions else {}
    for doc_id in prune:
        if versions.get(doc_id):
            writes.append({"op": "delete", "collection": "items", "doc_id": doc_id,
                           "if_version": int(versions[doc_id])})
    status["added"] = len(review)
    dump_json(os.path.join(out, "status.json"), status)

    bdir = os.path.join(out, "batches")
    os.makedirs(bdir, exist_ok=True)
    size = 20  # thumbnails make docs ~15-40 KB; stay well under 1 MiB per batch
    n = 0
    for i in range(0, len(writes), size):
        n += 1
        dump_json(os.path.join(bdir, "batch_%02d.json" % n), writes[i:i + size])
    print(json.dumps({"batches": n, "writes": len(writes), "status_file": os.path.join(out, "status.json")}))
    return 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan")
    s.add_argument("--state")
    s.add_argument("--out", required=True)
    s.add_argument("--config")
    fd = sub.add_parser("feed")
    fd.add_argument("--data", required=True)
    fd.add_argument("--config")
    ff = sub.add_parser("from-feed")
    ff.add_argument("--feed", required=True)
    ff.add_argument("--state")
    ff.add_argument("--out", required=True)
    ff.add_argument("--config")
    f = sub.add_parser("finalize")
    f.add_argument("--out", required=True)
    f.add_argument("--review", required=True)
    f.add_argument("--versions")
    a = ap.parse_args()
    run = {"scan": cmd_scan, "feed": cmd_feed, "from-feed": cmd_from_feed, "finalize": cmd_finalize}[a.cmd]
    sys.exit(run(a))


if __name__ == "__main__":
    main()
