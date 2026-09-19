#!/usr/bin/env python3
"""ExStravaTor: pull friends' Strava run tracks as GPX files.

Standard library only, so it runs in Termux with just `pkg install python`.
Secrets and tokens live in ~/.exstravator/ (never in the repo).

Commands:
  setup                  Save your Strava app's client ID and secret
  auth                   Connect a friend on this phone (localhost redirect)
  exchange [TEXT]        Redeem a code a friend sent from the Pages site
  list                   Show connected athletes
  fetch [--date D]       Download that day's runs as GPX for everyone
  remove NAME_OR_ID      Disconnect an athlete and delete their token
"""
import argparse
import datetime as dt
import getpass
import http.server
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from xml.sax.saxutils import escape

HOME = os.path.expanduser("~/.exstravator")
CONFIG = os.path.join(HOME, "config.json")
TOKENS = os.path.join(HOME, "tokens.json")
API = "https://www.strava.com/api/v3"
TOKEN_URL = "https://www.strava.com/oauth/token"
SCOPE = "activity:read_all"
LOCAL_PORT = 8000
DEFAULT_TYPES = "Run,TrailRun"


# ---------- storage ----------

def load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def save(path, data):
    os.makedirs(HOME, exist_ok=True)
    os.chmod(HOME, 0o700)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def need_config():
    cfg = load(CONFIG, {})
    if not cfg.get("client_id") or not cfg.get("client_secret"):
        sys.exit("Run `exstravator.py setup` first.")
    return cfg


# ---------- http ----------

class ApiError(Exception):
    def __init__(self, status, body):
        super().__init__(f"HTTP {status}: {body[:300]}")
        self.status = status


def request(method, url, token=None, form=None):
    data = urllib.parse.urlencode(form).encode() if form else None
    req = urllib.request.Request(url, data=data, method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise ApiError(e.code, e.read().decode(errors="replace")) from None


# ---------- tokens ----------

def store_athlete(res):
    ath = res.get("athlete") or {}
    aid = str(ath.get("id"))
    name = f"{ath.get('firstname', '')} {ath.get('lastname', '')}".strip() or aid
    tokens = load(TOKENS, {})
    tokens[aid] = {
        "name": name,
        "refresh_token": res["refresh_token"],
        "access_token": res["access_token"],
        "expires_at": res["expires_at"],
    }
    save(TOKENS, tokens)
    print(f"Connected {name} (athlete {aid}).")


def exchange_code(code):
    cfg = need_config()
    try:
        res = request("POST", TOKEN_URL, form={
            "client_id": cfg["client_id"],
            "client_secret": cfg["client_secret"],
            "code": code,
            "grant_type": "authorization_code",
        })
    except ApiError as e:
        if e.status in (400, 401):
            sys.exit("Strava rejected that code. It may have expired or been used "
                     "already; ask your friend to open the link and connect again.")
        raise
    store_athlete(res)


def access_token(aid, tokens):
    rec = tokens[aid]
    if rec["expires_at"] - 300 > time.time():
        return rec["access_token"]
    cfg = need_config()
    res = request("POST", TOKEN_URL, form={
        "client_id": cfg["client_id"],
        "client_secret": cfg["client_secret"],
        "refresh_token": rec["refresh_token"],
        "grant_type": "refresh_token",
    })
    rec.update(access_token=res["access_token"],
               refresh_token=res["refresh_token"],  # Strava may rotate it
               expires_at=res["expires_at"])
    save(TOKENS, tokens)
    return rec["access_token"]


# ---------- phone helpers ----------

def copy_to_clipboard(text):
    if shutil.which("termux-clipboard-set"):  # needs the Termux:API app
        subprocess.run(["termux-clipboard-set"], input=text.encode(), check=False)
        return True
    return False


def read_clipboard():
    if shutil.which("termux-clipboard-get"):
        out = subprocess.run(["termux-clipboard-get"], capture_output=True, check=False)
        return out.stdout.decode().strip()
    return ""


def open_url(url):
    if shutil.which("termux-open-url"):
        subprocess.run(["termux-open-url", url], check=False)
    else:
        webbrowser.open(url)


# ---------- commands ----------

def cmd_setup(args):
    cfg = load(CONFIG, {})
    cid = input(f"Client ID [{cfg.get('client_id', '')}]: ").strip() or cfg.get("client_id")
    secret = getpass.getpass("Client secret (hidden, Enter to keep current): ").strip() \
        or cfg.get("client_secret")
    if not cid or not secret:
        sys.exit("Both values are required. Find them at https://www.strava.com/settings/api")
    save(CONFIG, {"client_id": cid, "client_secret": secret})
    print(f"Saved to {CONFIG}")


def cmd_auth(args):
    cfg = need_config()
    redirect = f"http://localhost:{LOCAL_PORT}/exchange"
    url = "https://www.strava.com/oauth/authorize?" + urllib.parse.urlencode({
        "client_id": cfg["client_id"],
        "redirect_uri": redirect,
        "response_type": "code",
        "approval_prompt": "force",
        "scope": SCOPE,
    })
    result = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if "code" not in q and "error" not in q:
                self.send_response(404)
                self.end_headers()
                return
            result.update({k: v[0] for k, v in q.items()})
            msg = "Connected. You can close this tab." if "code" in q \
                else "Access was declined. Nothing was shared."
            body = (f"<!doctype html><meta name=viewport content='width=device-width'>"
                    f"<p style='font:20px sans-serif;padding:2em'>{msg}</p>").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("127.0.0.1", LOCAL_PORT), Handler)
    copied = copy_to_clipboard(url)
    print("Open this link in an incognito tab, then hand the phone to your friend"
          + (" (it's on your clipboard):" if copied else ":"))
    print(f"\n{url}\n")
    if args.open:
        open_url(url)
    print("Waiting for Strava to redirect back... (Ctrl-C to cancel)")
    try:
        while not result:
            server.handle_request()
    except KeyboardInterrupt:
        sys.exit("\nCancelled.")
    finally:
        server.server_close()

    if "error" in result:
        sys.exit("Your friend declined access.")
    granted = result.get("scope", "")
    if "activity:read" not in granted:
        sys.exit("The activity permission wasn't granted. Run auth again and leave "
                 "the activity boxes ticked.")
    if "activity:read_all" not in granted:
        print("Note: private activities weren't allowed; only visible runs will download.")
    exchange_code(result["code"])


def cmd_exchange(args):
    text = " ".join(args.text).strip() or read_clipboard()
    if not text:
        sys.exit("Paste the message your friend sent: exstravator.py exchange '<message>'")
    m = (re.search(r"\b([0-9a-fA-F]{40})\b", text)
         or re.search(r"code=([^&\s]+)", text))
    code = m.group(1) if m else text.split()[-1]
    exchange_code(code)


def cmd_list(args):
    tokens = load(TOKENS, {})
    if not tokens:
        print("No athletes connected yet. Use `auth` or `exchange`.")
    for aid, rec in tokens.items():
        print(f"{aid:>12}  {rec['name']}")


def select(tokens, who):
    if not who:
        return list(tokens)
    picks = [aid for aid, rec in tokens.items()
             if any(w == aid or w.lower() in rec["name"].lower() for w in who)]
    if not picks:
        sys.exit(f"No connected athlete matches {', '.join(who)}.")
    return picks


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "athlete"


def to_gpx(act, athlete_name, streams):
    latlng = streams["latlng"]["data"]
    times = streams.get("time", {}).get("data") or []
    eles = streams.get("altitude", {}).get("data") or []
    start = dt.datetime.fromisoformat(act["start_date"].replace("Z", "+00:00"))
    pts = []
    for i, (lat, lon) in enumerate(latlng):
        p = f'      <trkpt lat="{lat:.7f}" lon="{lon:.7f}">'
        if i < len(eles) and eles[i] is not None:
            p += f"<ele>{eles[i]:.1f}</ele>"
        if i < len(times):
            t = start + dt.timedelta(seconds=times[i])
            p += f"<time>{t.strftime('%Y-%m-%dT%H:%M:%SZ')}</time>"
        pts.append(p + "</trkpt>")
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<gpx version="1.1" creator="ExStravaTor" '
        'xmlns="http://www.topografix.com/GPX/1/1">\n'
        f"  <metadata><name>{escape(act.get('name', ''))}</name>"
        f"<author><name>{escape(athlete_name)}</name></author>"
        f"<time>{start.strftime('%Y-%m-%dT%H:%M:%SZ')}</time></metadata>\n"
        f"  <trk><name>{escape(act.get('name', ''))}</name>"
        f"<type>{escape(act.get('sport_type', ''))}</type>\n"
        "    <trkseg>\n" + "\n".join(pts) + "\n    </trkseg>\n  </trk>\n</gpx>\n"
    )


def default_out_dir():
    downloads = os.path.expanduser("~/storage/downloads")  # after termux-setup-storage
    if os.path.isdir(downloads):
        return os.path.join(downloads, "ExStravaTor")
    return os.path.abspath("gpx")


def cmd_fetch(args):
    need_config()
    tokens = load(TOKENS, {})
    if not tokens:
        sys.exit("No athletes connected yet.")
    day = dt.date.fromisoformat(args.date)
    # Wide UTC window, then filter on each activity's own local start date.
    after = int(dt.datetime.combine(day - dt.timedelta(days=1), dt.time()).timestamp())
    before = int(dt.datetime.combine(day + dt.timedelta(days=2), dt.time()).timestamp())
    types = None if args.all_types else set(args.types.split(","))
    out_dir = args.out or default_out_dir()
    os.makedirs(out_dir, exist_ok=True)
    saved = 0

    for aid in select(tokens, args.who):
        name = tokens[aid]["name"]
        try:
            token = access_token(aid, tokens)
            acts = request("GET", f"{API}/athlete/activities?"
                           + urllib.parse.urlencode({"after": after, "before": before,
                                                     "per_page": 100}), token)
        except ApiError as e:
            if e.status == 429:
                sys.exit("Strava rate limit hit. Wait 15 minutes and try again.")
            if e.status in (400, 401):
                print(f"{name}: access was revoked. Run `remove {aid}` and reconnect them.")
                continue
            raise
        matches = [a for a in acts
                   if a.get("start_date_local", "")[:10] == args.date
                   and (types is None or a.get("sport_type") in types)]
        if not matches:
            print(f"{name}: no matching activity on {args.date}.")
            continue
        for act in matches:
            try:
                streams = request("GET", f"{API}/activities/{act['id']}/streams?"
                                  + urllib.parse.urlencode({"keys": "latlng,time,altitude",
                                                            "key_by_type": "true"}), token)
            except ApiError as e:
                if e.status == 429:
                    sys.exit("Strava rate limit hit. Wait 15 minutes and try again.")
                raise
            if "latlng" not in streams:
                print(f"{name}: '{act.get('name')}' has no GPS track, skipped.")
                continue
            path = os.path.join(out_dir, f"{args.date}_{slug(name)}_{act['id']}.gpx")
            with open(path, "w") as f:
                f.write(to_gpx(act, name, streams))
            saved += 1
            print(f"{name}: saved {os.path.basename(path)} ({act.get('name')})")
    print(f"\n{saved} file(s) in {out_dir}")


def cmd_remove(args):
    tokens = load(TOKENS, {})
    for aid in select(tokens, [args.who]):
        name = tokens[aid]["name"]
        try:
            request("POST", "https://www.strava.com/oauth/deauthorize",
                    form={"access_token": access_token(aid, tokens)})
        except ApiError:
            pass  # already revoked on their side
        del tokens[aid]
        save(TOKENS, tokens)
        print(f"Removed {name}.")


def main():
    ap = argparse.ArgumentParser(prog="exstravator.py",
                                 description="Pull friends' Strava runs as GPX.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup", help="save client ID and secret").set_defaults(fn=cmd_setup)
    p = sub.add_parser("auth", help="connect a friend on this phone")
    p.add_argument("--open", action="store_true", help="open the link in the browser")
    p.set_defaults(fn=cmd_auth)
    p = sub.add_parser("exchange", help="redeem a code sent from the Pages site")
    p.add_argument("text", nargs="*", help="the message or code (default: clipboard)")
    p.set_defaults(fn=cmd_exchange)
    sub.add_parser("list", help="show connected athletes").set_defaults(fn=cmd_list)
    p = sub.add_parser("fetch", help="download a day's runs as GPX")
    p.add_argument("--date", default=dt.date.today().isoformat(), help="YYYY-MM-DD (default today)")
    p.add_argument("--who", action="append", help="name or athlete ID; repeatable")
    p.add_argument("--types", default=DEFAULT_TYPES, help=f"sport types (default {DEFAULT_TYPES})")
    p.add_argument("--all-types", action="store_true", help="include every sport type")
    p.add_argument("--out", help="output folder")
    p.set_defaults(fn=cmd_fetch)
    p = sub.add_parser("remove", help="disconnect an athlete")
    p.add_argument("who", help="name or athlete ID")
    p.set_defaults(fn=cmd_remove)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
