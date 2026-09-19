# ExStravaTor

Pull GPX tracks of runs your friends did with you, using the Strava API.
Two parts: a Python script you run in Termux on Android, and a one-page
GitHub Pages site friends open on their own phones to connect.

No secrets or tokens are ever stored in this repo. They live in
`~/.exstravator/` on your phone.

## One-time setup

**Strava app** (in a browser, at https://www.strava.com/settings/api)
1. Create an app and note the Client ID and Client Secret.
2. Set *Authorization Callback Domain* to `brian-kujawski.github.io`.
   Strava also accepts `localhost` redirects, which the in-person flow uses.
   If Strava ever rejects the localhost redirect, switch the domain to
   `localhost` while connecting someone in person.
3. Upgrade the app from single-player mode to 10 athletes on the same page.

**Pages site**
1. In `docs/index.html`, set `CLIENT_ID` (and optionally `SEND_TO` to your phone number).
2. Repo Settings → Pages → Deploy from branch → `main` / `/docs`.
   Pages on a free account needs a public repo; that's safe, since the client ID is public by design.
3. The site will be at `https://brian-kujawski.github.io/ExStravaTor/`.

**Termux** (install from F-Droid; the Play Store build is outdated)
```sh
pkg install python git
termux-setup-storage          # lets GPX files land in Downloads
git clone https://github.com/brian-kujawski/ExStravaTor && cd ExStravaTor
python exstravator.py setup   # paste Client ID and secret
```
Optional: install the Termux:API app and `pkg install termux-api` so the
script can use your clipboard.

## Connecting friends

**Remotely:** text them the Pages link. They tap *Connect with Strava*,
authorize, then tap *Send code*. When the message arrives, copy it and run
```sh
python exstravator.py exchange          # reads the clipboard
python exstravator.py exchange 'ExStravaTor code: …'   # or paste it
```
Codes expire within minutes, so redeem them promptly. If one fails, ask
them to open the link again.

**In person:** run `python exstravator.py auth`, open the printed link in an
incognito tab, and hand your friend the phone.

Each friend connects once. The script refreshes their access automatically.

## Downloading tracks
```sh
python exstravator.py fetch --date 2026-09-13
python exstravator.py fetch --date 2026-09-13 --who sam --who alex
python exstravator.py fetch --all-types          # today, any sport
```
Files land in `Downloads/ExStravaTor/` as `DATE_name_activityid.gpx`.

`list` shows who's connected; `remove NAME` disconnects someone and deletes their token.
