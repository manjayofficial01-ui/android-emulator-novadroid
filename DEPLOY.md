# ☁ NovaDroid on Vercel — deploy runbook (v5.0.0)

**Architecture (read this first):** Vercel is serverless Linux — it cannot run
QEMU, the Android SDK, or ADB. So the split is:

```
Vercel (public)                    Your PC (Windows, stays on)
─────────────────                  ──────────────────────────
static dashboard                   python novadroid.py  (agent)
(index/app/styles)  ──tunnel────── emulator.exe · adb · SDK
                    + Bearer token
                    + --origin <vercel-url>
```

The emulator keeps running at home; the dashboard reaches it from anywhere.

> **v5 changed the security model.** If you are upgrading from v4, read
> [Authentication in v5](#authentication-in-v5) before step 2. Two things
> break old setups: `?token=` is gone, and a browser dashboard on another
> origin is refused unless you pass `--origin`.

## 1) Deploy the dashboard

```powershell
cd "D:\0000 App Building with Gemini\Android Apk Project\android emulator\novadroid"
vercel --prod
```

Only `static/` uploads (see `.vercelignore`). You get `https://<project>.vercel.app`.

## 2) Start the agent for remote access

```powershell
python novadroid.py --gen-token          # e.g. abCDeF...  (keep secret!)

# Prefer the environment variable or a token FILE: --token is visible in the
# process list to any other local user.
$env:NOVADROID_TOKEN = "<secret>"
python novadroid.py --host 0.0.0.0 --port 8080 --no-browser `
  --origin https://<project>.vercel.app
```

**`--origin` is required for a Vercel-hosted dashboard.** v5 removed the
wildcard `Access-Control-Allow-Origin: *`. Every request from the Vercel page
to your tunnel is cross-origin, so without an explicit allowlist the agent
answers `403 origin not allowed — add it with --origin` for *every* API call.
Repeat `--origin` for extra URLs (preview deploys, a custom domain), or set
`NOVADROID_ORIGINS` as a comma-separated list.

```
NOVADROID_ORIGINS=https://my-app.vercel.app,https://preview.my-app.vercel.app
```

Without a token, binding to a non-loopback address is **refused** outright.

Useful optional flags:

| Flag | Default | Why you would use it |
|---|---|---|
| `--strict-host` | off | Validate the `Host` header too (DNS-rebinding protection) |
| `--allowed-host NAME` | — | Extra Host to permit with `--strict-host` (tunnel hostname) |
| `--allow-shell` | **off** | Re-enables `POST /api/shell` (arbitrary `adb shell`) |
| `--allow-gui` | **off** | Re-enables `POST /api/shared/reveal` (opens Explorer on the PC) |
| `--token-file PATH` | — | Read the Bearer token from a file instead of argv |
| `--max-body-mb N` | 320 | Cap JSON bodies (base64 APK uploads need the headroom) |

`--strict-token` is kept as a deprecated no-op: in v5 the token is **always**
required when set. There is no loopback exemption, because a tunnel reaches
the agent as `127.0.0.1` — trusting loopback would trust the whole internet.

## 3) Open the tunnel

```powershell
powershell -ExecutionPolicy Bypass -File scripts\expose-tunnel.ps1 -Port 8080
```

Copy the `https://xxx.trycloudflare.com` URL. If you enable `--strict-host`,
also pass `--allowed-host xxx.trycloudflare.com`.

## 4) Connect

Open your Vercel URL → top **🔌 Backend** bar → paste tunnel URL + token →
**Connect**. The dashboard first calls the public `/api/health` to confirm the
agent and its feature flags, then sends `Authorization: Bearer <token>` on
every API request. Screenshots, the live stream and recording downloads use a
short-lived **session ticket** (`POST /api/session`, 300 s) instead, because
`<img>`/`<video>` tags cannot set an Authorization header.

The AI layer is fully offline, so no key or egress is needed on either side.

## Authentication in v5

| v4 behaviour | v5 behaviour |
|---|---|
| Loopback requests bypassed the token | **No bypass** — a tunnel arrives as loopback |
| `?token=` worked on any endpoint | **Removed** — tokens never enter a URL, log, history or Referer |
| `Access-Control-Allow-Origin: *` | Explicit `--origin` allowlist, plus same-origin matching |
| `/api/shell` and `/api/shared/reveal` always on | **Off by default**, opt in with `--allow-shell` / `--allow-gui` |
| Full host paths in `/api/info` | Booleans / names only (no SDK path) |
| Binary views used `?token=` | `?ticket=` on `/api/screen`, `/api/screen/stream`, `/api/record/download` only |

Tickets are 256-bit random, expire after 300 s, are capped at 128 live entries,
and are **scoped to those three binary paths** — a ticket cannot read
`/api/info`, start AVDs or run a shell. The frontend refreshes them while a
stream is open and re-acquires one on a 401.

`/api/health` is deliberately public but carries no host details: it only
reports the version, `auth: token|open`, and whether shell/GUI/tickets are
enabled.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `403 origin not allowed` | Add `--origin https://<project>.vercel.app` (exact scheme + host; trailing slashes are normalised, paths are **not** allowed) |
| `401 unauthorized` everywhere | Paste the token in the 🔌 Backend bar; it is no longer accepted in the URL |
| Live view shows 401 but the rest works | Session ticket expired/blocked — reload; the dashboard re-mints automatically |
| "ADB shell is disabled by default" | Restart with `--allow-shell` (only if you accept arbitrary shell access) |
| "Opening Explorer is disabled" | Restart with `--allow-gui` |
| `403 host not allowed` | With `--strict-host`, add `--allowed-host <tunnel-host>` |

## Notes

- **⚡ Stream (MJPEG) is the one feature that degrades over a tunnel.** It holds a
  long-lived connection and is bandwidth-hungry; a quick tunnel may buffer or
  stall it. The dashboard falls back to periodic single-frame polling where
  streaming is unavailable. For reliable remote live view, use a Cloudflare
  **named** tunnel and expect a lower effective frame rate over mobile data.
- Quick-tunnel URLs change on restart — re-paste after reboot. For a stable
  address, make a Cloudflare **named** tunnel once and reuse it.
- Keep the PC awake while accessing remotely; Quickboot keeps VMs warm.
- Rotate the token any time: set a new `NOVADROID_TOKEN` and restart the agent.
  Existing tickets are kept in memory and are invalidated by the restart.
- Local use is unchanged: `.\start.bat` still serves everything on localhost
  with no token needed.
