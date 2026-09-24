#!/usr/bin/env python3
"""ExStravaTor: pull friends' Strava run tracks as GPX files.

Standard library only. Runs on Windows (plain `python`, via PowerShell) or
in Termux on Android with just `pkg install python`.
Secrets and tokens live in ~/.exstravator/ (never in the repo), along with
pseudonyms.json, which maps Strava names to the pseudonyms written into GPX
files so real names never end up in the public repo.

Commands:
  setup                  Save your Strava app's client ID and secret
  auth                   Connect a friend on this device (localhost redirect)
  exchange [TEXT]        Redeem a code a friend sent from the Pages site
  list                   Show connected athletes
  fetch [--date D]       Download that day's runs as GPX for everyone
                         (prompts if someone has more than one match)
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
PSEUDONYMS = os.path.join(HOME, "pseudonyms.json")
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
    if os.name == "nt":
        # Passed via env var (not a CLI arg) so PowerShell never has to parse the text.
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "Set-Clipboard -Value $env:EXSTRAVATOR_CLIP"],
            env={**os.environ, "EXSTRAVATOR_CLIP": text}, check=False,
        )
        return True
    return False


def read_clipboard():
    if shutil.which("termux-clipboard-get"):
        out = subprocess.run(["termux-clipboard-get"], capture_output=True, check=False)
        return out.stdout.decode().strip()
    if os.name == "nt":
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", "Get-Clipboard"],
            capture_output=True, check=False,
        )
        return out.stdout.decode(errors="replace").strip()
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
    print("Open this link in an incognito/private tab, then hand the device to your friend"
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


def pseudonym(aid, name, table):
    """Look up by athlete ID first, then by Strava name (case-insensitive)."""
    if table.get(aid):
        return table[aid]
    for key, alias in table.items():
        if alias and key.strip().lower() == name.strip().lower():
            return alias
    return None


def cmd_list(args):
    tokens = load(TOKENS, {})
    table = load(PSEUDONYMS, {})
    if not tokens:
        print("No athletes connected yet. Use `auth` or `exchange`.")
    for aid, rec in tokens.items():
        alias = pseudonym(aid, rec["name"], table) or "(no pseudonym)"
        print(f"{aid:>12}  {rec['name']:<24}  {alias}")


def select(tokens, who):
    if not who:
        return list(tokens)
    picks = [aid for aid, rec in tokens.items()
             if any(w == aid or w.lower() in rec["name"].lower() for w in who)]
    if not picks:
        sys.exit(f"No connected athlete matches {', '.join(who)}.")
    return picks


def slug(s):
    # Case is kept so pseudonyms like "SAM" stay as written.
    return re.sub(r"[^A-Za-z0-9_]+", "-", s).strip("-") or "athlete"


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
        f"  <metadata><author><name>{escape(athlete_name)}</name></author>"
        f"<time>{start.strftime('%Y-%m-%dT%H:%M:%SZ')}</time></metadata>\n"
        f"  <trk><type>{escape(act.get('sport_type', ''))}</type>\n"
        "    <trkseg>\n" + "\n".join(pts) + "\n    </trkseg>\n  </trk>\n</gpx>\n"
    )


def default_out_dir():
    downloads = os.path.expanduser("~/storage/downloads")  # after termux-setup-storage
    if os.path.isdir(downloads):
        return os.path.join(downloads, "ExStravaTor")
    if os.name == "nt":
        downloads = os.path.join(os.path.expanduser("~"), "Downloads")
        if os.path.isdir(downloads):
            return os.path.join(downloads, "ExStravaTor")
    return os.path.abspath("gpx")


def describe_activity(act):
    start = (act.get("start_date_local") or "")[11:16]
    km = (act.get("distance") or 0) / 1000
    minutes = (act.get("moving_time") or 0) / 60
    sport = act.get("sport_type", "")
    return f"{start}  {sport:<10} {km:5.1f} km  {minutes:4.0f} min  {act.get('name') or ''}"


def disambiguate(name, matches, auto):
    matches = sorted(matches, key=lambda a: a.get("start_date_local", ""))
    if auto or not sys.stdin.isatty():
        if not auto:
            print(f"{name}: {len(matches)} matching activities, saving all "
                  "(non-interactive; pass --auto to silence this).")
        return matches
    print(f"\n{name} has {len(matches)} matching activities:")
    for i, act in enumerate(matches, 1):
        print(f"  {i}. {describe_activity(act)}")
    choice = input(f"  Save which for {name}? [a]ll / [s]kip / numbers e.g. 1,3 [a]: ").strip().lower() or "a"
    if choice in ("s", "skip"):
        return []
    if choice in ("a", "all"):
        return matches
    picks = [matches[int(tok) - 1] for tok in choice.replace(",", " ").split()
             if tok.isdigit() and 1 <= int(tok) <= len(matches)]
    if not picks:
        print(f"  Didn't understand that; saving all for {name}.")
        return matches
    return picks


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
    table = load(PSEUDONYMS, {})
    missing = []

    for aid in select(tokens, args.who):
        name = tokens[aid]["name"]
        alias = pseudonym(aid, name, table)
        if not alias:
            missing.append(name)
            print(f"{name}: no pseudonym set, skipped.")
            continue
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
        if len(matches) > 1:
            matches = disambiguate(name, matches, args.auto)
            if not matches:
                print(f"{name}: skipped.")
                continue
        for n, act in enumerate(matches, 1):
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
            # No activity ID or title in the file: either leads back to the real athlete.
            suffix = f"_{n}" if len(matches) > 1 else ""
            path = os.path.join(out_dir, f"{args.date}_{slug(alias)}{suffix}.gpx")
            with open(path, "w") as f:
                f.write(to_gpx(act, alias, streams))
            saved += 1
            print(f"{name}: saved {os.path.basename(path)} ({act.get('name')})")
    print(f"\n{saved} file(s) in {out_dir}")
    if missing:
        # Seed blank entries so filling them in is just typing the pseudonym.
        for name in missing:
            table.setdefault(name, "")
        save(PSEUDONYMS, table)
        print(f"\nNo pseudonym for {', '.join(missing)}. Fill in their entries in "
              f"{PSEUDONYMS}, then rerun with --who for them.")


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
    p = sub.add_parser("auth", help="connect a friend on this device")
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
    p.add_argument("--auto", action="store_true",
                   help="skip the picker when someone has multiple matches; save all of them")
    p.add_argument("--out", help="output folder")
    p.set_defaults(fn=cmd_fetch)
    p = sub.add_parser("remove", help="disconnect an athlete")
    p.add_argument("who", help="name or athlete ID")
    p.set_defaults(fn=cmd_remove)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
