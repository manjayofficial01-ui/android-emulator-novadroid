# ◈ NovaDroid — Best Android Emulator Manager for Windows

**Live dashboard:** https://novadroid-rho.vercel.app (static UI on Vercel —
paste your tunnel URL + token in its 🔌 Backend bar; full runbook in `DEPLOY.md`)

**Source:** https://github.com/manjayofficial01-ui/android-emulator-novadroid —
linked to Vercel, so every push to `main` auto-redeploys the dashboard.

Built from deep 2026 research (`RESEARCH.md`) on BlueStacks 5, LDPlayer 9, MuMu 12, Nox 7, MEmu 9, GameLoop, Genymotion, and the Google Android Emulator engine.

**Strategy:** don't reinvent the hypervisor. Ship the industry-best *engine* (Google Emulator 37.x — forked QEMU + ranchu + gfxstream + WHPX, Android 16/17, free, no ads) and beat everyone on the *manager layer*: multi-instance sync, macros, perf dashboard, sensor injection, quickboot-first UX.

## Quick start (Windows)

```powershell
# 1) one-time: enable WHPX (Admin + reboot)
powershell -ExecutionPolicy Bypass -File scripts\setup-whpx.ps1

# 2) create baseline fleet (phone+tablet+desktop)
powershell -ExecutionPolicy Bypass -File scripts\create-avds.ps1
# or: android emulator create medium_phone

# 3) launch (double-click or terminal)
.\start.bat
# live dashboard → http://localhost:8080
```

Requirements: Python 3.10+, Android SDK with `emulator` + `platform-tools` + an `x86_64` Play image, `android` CLI on PATH, BIOS VT-x/AMD-V on.

## What you get

| Area | NovaDroid | Stock emulator CLI |
|---|---|---|
| AVD create/list/start/stop/delete | ✅ one-click + auto-tune (4 CPU/4GB/gpu auto) | manual commands |
| Quickboot vs cold | ✅ toggle + wipe-data helper | flags only |
| Raw power flags (`-gpu host/lavapipe`, `-feature VulkanNativeSwapchain`, `-cores`) | ✅ raw-boot box | must memorize |
| Multi-instance operation synchronizer | ✅ sync-tap all | ❌ |
| Macros (tap/swipe/text/key/sleep) | ✅ record→save→replay | ❌ |
| APK install | ✅ delta-install + drag path | manual adb |
| Screenshot/live view/layout inspect | ✅ 1-click + auto-refresh | separate cmds |
| GPS presets, battery, guarded shell, guest perf | ✅ | adb spelunking |
| Tablet & display (presets, live `wm` resize, orientation, skins) | ✅ one-tap, no reboot | manual `wm` + config edits |
| Off-screen window rescue (saved-pos reset + live move) | ✅ ⌖ Fix window | hand-edit ini / unreachable title bar |
| Shared folder PC ↔ emulator (APKs, photos, files) | ✅ per-file Export/Import/Delete, **Export to real Windows Desktop**, Sync-now, exact mirrors either way, Refresh, auto-sync status | manual `adb push/pull` |
| APK compatibility check (aapt2 + ARM-translation verdict) | ✅ Analyze before install | blind install |
| WHPX readiness + AEHD-sunset guidance | ✅ banner | `emulator-check` raw |
| **Smoothness engine** (jank score, render-path probe, bottleneck ranking, auto-tune) | ✅ measure → diagnose → apply | ❌ no measurement at all |
| **Live MJPEG stream** (⚡ Stream, duplicate-frame skip, fps pacing) | ✅ continuous | reload PNG by hand |
| **AI assistant** (natural-language control + logcat triage + on-device AI status) | ✅ offline, stdlib-only, no API key | ❌ |
| **AVD toolkit** (disk usage breakdown, reclaim, zip backup/restore, rename) | ✅ BlueStacks' Disk Cleanup — but honest and portable | hidden folders |
| **Port forwarding manager** (`adb forward`/`reverse` incl. abstract sockets) | ✅ one table, PC⇄device | hand-typed rules |
| **High-FPS mode** (60/90/120 Hz, live + persisted) | ✅ verified against the image, not claimed | marketing number |
| **Keyboard control / keymaps** (key → tap/swipe, WASD preset) | ✅ BlueStacks-class manager layer | ❌ |
| **Eco mode** (guest animations off + cache trim) | ✅ one click | slider, no explanation |
| **Batch fleet install** (one APK → every instance, parallel) | ✅ | N rounds of pick+install |
| **Deep links** (https / myapp:// / market:// → device) | ✅ | manual am start |
| **Live HTTP proxy** + wireless ADB (`adb tcpip`) + root toggle w/ honest Play-image verdict | ✅ | flags only |
| **🤖 Agent skill file** (`/api/agent-skill` — drive the fleet from Claude/Codex; BlueStacks-Desktop pattern) | ✅ one URL + token | ❌ |
| **🪞 scrcpy wrapper** (hardware-decoded mirror + per-app virtual-display windows) | ✅ wrap-only, honest fallback | vendor fork |
| **📦 Smart App Archive** (remove APK, keep data, restore with state — BlueStacks 16 parity) | ✅ one click | hidden folders |
| **🧵 Hybrid-CPU P-core guard** (37.3.x E-core throttling: detect + warn + affinity pin) | ✅ measured, honest | ❌ no warning at all |
| **🎮 Per-instance GPU preference** (iGPU/dGPU pin via Windows registry — MuMu parity) | ✅ | menu, no verify note |

## Project layout

```
novadroid/
  novadroid.py        # stdlib-only server (REST + static), no pip needed
  core/config.py      # SDK discovery, 4CPU/4GB perf baseline
  core/safe.py        # v5: one place for quoting, allowlists, path confinement
  core/engine.py      # v5: /api/engine release-note compatibility report
  core/environment.py # v5: 36.6.11 "environment" camera mode
  core/runner.py      # hardened subprocess helpers (empty-arg guard, 127)
  core/accel.py       # WHPX/emulator-check probing
  core/avd.py         # AVD lifecycle + quickboot/raw/wipe + multi-instance map
                      #   + v4: disk usage, cleanup, backup/restore, rename, root
  core/adb.py         # devices, delta-install, raw screenshots, geo/batt/macros
                      #   + v4: batch install, macro loops, safe input text
  core/apps.py        # app launch/stop/clear/uninstall/grant + clipboard + boot state
                      #   + v4: deep links, proxy, eco mode, wireless ADB
  core/host.py        # Windows host telemetry (CPU/RAM/disk/emulator procs)
  core/inifile.py     # single comment-aware .ini upsert (kills 3 divergent copies)
  core/display.py     # tablet presets, wm size/density, orientation, skins
                      #   + v4: high-FPS (60/90/120) live + persist
  core/window.py      # off-screen rescue: ini reset + strict live-move
  core/shared.py      # NovaDroid-Shared <-> /sdcard sync, upload, auto-sync
  core/compat.py      # aapt2 badging + ARM-translation verdicts (version sort fixed)
  core/perf.py        # smoothness engine: gfxinfo, render-path probe, bottlenecks, auto-tune
  core/ports.py       # v4: adb forward/reverse manager (incl. abstract sockets)
  core/ai.py          # NL command interpreter, logcat triage, on-device AI (AICore) status
  static/             # dashboard (index.html/app.js/styles.css, offline-safe)
                      #   v4: tabs, light/dark theme, toasts, keymaps, drag-drop
  scripts/            # setup-whpx.ps1, create-avds.ps1
  tests/              # 241 stdlib unittests (core + display + avd + adb + apps + host +
                      #   inifile + compat + perf + ai + regressions + v4 + v5)
```

## API (all JSON except `/api/screen` → image/png, `/api/record/download` → file, `/api/screen/stream` → MJPEG)

**GET**
`/api/health` · `/api/info` · `/api/avds` · `/api/profiles` · `/api/devices` ·
`/api/jobs` · `/api/screen?serial=` · `/api/screen/stream?serial=` ·
`/api/layout?serial=` · `/api/perf?serial=` ·
`/api/host` · `/api/boot?serial=` · `/api/macros` · `/api/apps` · `/api/apps/info` ·
`/api/logcat?serial=` · `/api/clipboard?serial=` · `/api/record` · `/api/record/download` ·
`/api/sensors?serial=` · `/api/network?serial=` · `/api/snapshots?serial=` ·
`/api/display/presets` · `/api/display?serial=` · `/api/skins` · `/api/avds/window?name=` ·
`/api/shared` · `/api/shared/device`

*Smoothness + AI (v3):*
`/api/smoothness?serial=` · `/api/smoothness/diagnose?serial=` ·
`/api/smoothness/history?limit=` · `/api/smoothness/renderer?serial=` ·
`/api/tune?name=` · `/api/ai?serial=` · `/api/ai/triage?serial=` · `/api/ai/nnapi?serial=`

*v4 (AVD toolkit · ports · power ops):*
`/api/avds/disk?name=` · `/api/ports?serial=` · `/api/ports/reverse?serial=` ·
`/api/refresh-rates`

*v5.1 (agent skill · scrcpy · GPU · hybrid CPU · smart archive):*
`/api/agent-skill` (markdown) · `/api/scrcpy` · `/api/gpu` · `/api/cpu` ·
`/api/apps/archived`

**POST**
`/api/avds` · `/api/avds/start` · `/api/avds/start-raw` · `/api/avds/stop` · `/api/avds/wipe` ·
`/api/avds/clone` · `/api/fleet` · `/api/snapshots` · `/api/apk/install` · `/api/apk/analyze` ·
`/api/geo` · `/api/battery` · `/api/battery/reset` · `/api/shell` · `/api/input/tap` ·
`/api/input/swipe` · `/api/input/text` · `/api/input/key` · `/api/macros` · `/api/macros/run` ·
`/api/apps/launch` · `/api/apps/stop` · `/api/apps/clear` · `/api/apps/uninstall` ·
`/api/apps/grant` · `/api/logcat/clear` · `/api/clipboard` · `/api/record/start` ·
`/api/record/stop` · `/api/sensor` · `/api/network` · `/api/biometric` · `/api/sms` ·
`/api/call` · `/api/signal` · `/api/power` · `/api/rotate` · `/api/display/apply` ·
`/api/display/reset` · `/api/display/orientation` · `/api/display/save` · `/api/avds/skin` ·
`/api/avds/window/fix` · `/api/shared/upload` · `/api/shared/push` · `/api/shared/pull` ·
`/api/shared/autosync` · `/api/shared/sync` · `/api/shared/mirror` · `/api/shared/delete-pc` ·
`/api/shared/delete-device` · `/api/shared/delete` · `/api/shared/reveal` · `/api/shared/export` · `/api/sync-tap`

*Smoothness + AI (v3):*
`/api/input/long-press` · `/api/tune/apply` · `/api/ai/command` · `/api/ai/interpret` · `/api/ai/triage`

*v4 (AVD toolkit · ports · power ops):*
`/api/avds/cleanup` · `/api/avds/backup` · `/api/avds/restore` · `/api/avds/rename` ·
`/api/avds/root` · `/api/ports/add` · `/api/ports/remove` · `/api/open-url` ·
`/api/proxy` · `/api/proxy/clear` · `/api/eco` · `/api/adb/tcpip` ·
`/api/display/refresh` · `/api/display/refresh/save` · `/api/apk/install-all` ·
`/api/sync-input`

*v5 (engine intelligence · camera environment):*
`/api/health` (public) · `/api/engine` · `/api/smoothness/flags?serial=` ·
`/api/avds/environment?name=` (GET) · `/api/avds/environment` (POST) ·
`/api/session` (POST, mints a binary-view ticket)

*v5.1 (agent skill · scrcpy · GPU · smart archive):*
`/api/apps/archive` · `/api/apps/restore-archive` · `/api/scrcpy/mirror` ·
`/api/scrcpy/app-window` · `/api/gpu` (POST) · `/api/avds/start-raw` now
accepts `affinity` (P-core mask)

> **Auth:** with a token set, all `/api/*` except `/api/health` require
> `Authorization: Bearer …`; binary views use `?ticket=`. See `DEPLOY.md`.

**DELETE**
`/api/avds?name=` · `/api/macros?name=`

## v5.1.0 — the Nov 2026 audit + competitive refresh (current)

A second full audit (three parallel review passes: bugs, security, dead code)
plus fresh competitive research (BlueStacks Desktop/16, LDPlayer 14, MuMu
V6.8, Genymotion 3.11, scrcpy 5.x, Emulator 37.3.x canary). **302 unittests
pass**; the new ones live in `tests/test_v51.py`.

### Security fixes

- **`input text` device-shell injection (P1).** The v5.0.0 code escaped the
  right four characters but never wrapped the payload in the double quotes
  its own docstring promised — `x; reboot` typed via the live view ran
  `reboot` as a second device-shell command. The payload is now a quoted
  shell word. Also fixed: `%` is no longer doubled (AOSP `Input.sendText`
  has no `%%` unescape — `50%% done` used to be typed literally).
- **DNS rebinding in tokenless mode (P1).** The `Origin == Host` fallback now
  additionally requires the Host to be one the server actually serves
  (loopback or `--allowed-host`), and the Host header is validated whenever
  no token is configured — a rebinding page controls both values by
  construction, so matching each other proved nothing.
- **`-qemu` passthrough refused.** `start-raw` no longer accepts
  `-qemu/-hda/-drive/-sdcard/-initdata/…` — those mounted arbitrary host
  files into the guest (a read primitive, and QEMU may write back).
- **Executable-lure blocklist.** Uploads to and exports from the shared
  folder refuse `.lnk .hta .vbs .js .wsf .pif .msi …` (a malicious app could
  stage `Cleanup Tool.lnk` and hope for a double-click on the real Desktop);
  symlinked sources are never exported; re-exports no longer clobber the
  earlier `(NovaDroid)` copy.
- **Hardening.** `/api/health` hides version/feature flags from
  unauthenticated callers; `compare_digest` compares bytes (non-ASCII
  headers no longer 500); CSP + `X-Frame-Options` + `Referrer-Policy` on
  every response; `Server:` banner carries no version; `log_request` prints
  path + status only (a `?ticket=` can no longer reach the console);
  downloads send `Cache-Control: no-store`; HTML revalidates and assets are
  `?v=`-versioned so an agent upgrade is picked up without a hard refresh.

### Bug fixes (all pinned by tests)

- **Reverse port rules were inverted twice.** `adb reverse` takes the
  DEVICE-side port first, and `reverse --list` prints `(reverse)` instead of
  the serial — v5.0.0 created tunnels backwards and could never list them.
  Rule removal now targets the device-side port for reverse.
- `wipe_data` refuses a running AVD and reports locked files instead of
  hard-coding `ok: true`; `stop_avd` works on CLI-less machines (adb
  `emu kill` as primary path); out-of-range ports are rejected, not clamped.
- Engine report treats missing `hw.ramSize`/`hw.cpu.ncore` as *unknown*
  (healthy AVDs no longer flagged below the RAM floor); `Infinity` in JSON
  no longer 500s input routes; `settings get` output that is not a number no
  longer 500s the refresh-rate check; macro save survives `OSError` and
  reserved Windows device names (`CON`, `NUL`, …); same-second backups get
  `-2` suffixes; `rename_avd` rolls back a half-moved AVD; `inifile.upsert`
  deduplicates legacy double keys and writes atomically under a lock;
  `send_sms` rejects filtered messages instead of silently sending
  "NovaDroid test"; sensor values are stripped; the `_ls` stat race no
  longer 500s the shared folder.

### New features (Nov 2026 competitive refresh)

- **🤖 Agent skill file — `GET /api/agent-skill`** (BlueStacks Desktop's
  "bring your agent" pattern, for the stock Google emulator): a markdown
  package teaching Claude/Codex/any agent the REST API, the screenshot
  ticket flow and safety rules. Button in the AI card.
- **🪞 scrcpy wrapper — `core/stream.py`** (scrcpy 5.x: D3D11VA hardware
  decode ≈10× lower CPU than software; 4.x `--new-display` + `--start-app`):
  hardware-decoded mirror window and per-app virtual-display windows — the
  manager-layer equivalent of BlueStacks' True Desktop Windowing / MuMu's
  Desktop Window Mode. Wrap-only: if scrcpy is not installed the dashboard
  says so honestly and the MJPEG stream stays.
- **📦 Smart App Archive** (BlueStacks 16 parity): 📦 on any installed app
  pulls its base APK to `NovaDroid-Shared\Archived` then
  `pm uninstall -k --user 0` — APK bytes gone, app data kept; reinstall
  from the Archived list restores the app with its state.
- **🧵 Hybrid-CPU P-core guard** (`core/perf.detect_hybrid`): detects Intel
  12th–14th Gen / Core Ultra silicon via the registry clock split, warns on
  the 37.3.x E-core throttling bug for headless instances, and offers a
  P-core affinity mask on Raw boot (`SetProcessAffinityMask`, stdlib ctypes).
  When the split can't be measured, it says so instead of inventing a mask.
- **🎮 Per-instance GPU preference** (`core/gpu.py`, MuMu V6.4.7 parity):
  pin emulator.exe + qemu-system-x86_64.exe to the iGPU or dGPU via the
  Windows `UserGpuPreferences` registry key — next launch.
- **Honest verdicts**: Windows-on-ARM hosts get a hard "unsupported" from
  the acceleration probe (no Google emulator WoA build; Prism has no nested
  virtualization) instead of WHPX setup steps; the APK pre-check now names
  the Play Integrity tiers (Basic may pass; Device/Strong fail on ANY
  emulator — platform root cert rotated Feb 2026).
- **Engine report 37.3.x awareness**: canary channel detection
  (`latest_canary: 37.3.6`), the next-generation QEMU-v11 engine advisory,
  and the hybrid-CPU note wired into `/api/engine`.

### Dead code removed

Guest-column header + console (never written), dead `refresh-rates` ID,
unused `dl,dd` and `.toast.warn` CSS rules, stale footer version, README
`?serial=` doc bugs for `/api/avds/window` and `/api/smoothness/history`.

## v5.0.0 — security hardening + engine intelligence

A full audit of the v4 request path found that **remote access was not actually
authenticated**: `_auth_ok()` exempted loopback, and a Cloudflare tunnel reaches
the agent as `127.0.0.1`. Combined with `?token=` URLs (logged, cached, sent in
`Referer`) and a wildcard CORS header, a public dashboard URL plus a tunnel was
enough to reach `adb shell` on the host. All 241 unittests pass; new v5 checks
live in `tests/test_v5.py` and `tests/test_regressions.py`.

### Security model (breaking change — see `DEPLOY.md`)

- **Strict Bearer auth.** If a token is set, *every* `/api/*` request needs
  `Authorization: Bearer …`. No loopback exemption, no `--strict-token` escape
  hatch (kept as a no-op for old scripts).
- **No tokens in URLs.** `?token=` is gone. `/api/health` is public but leaks no
  host details. Access logs print the path only, never the query string.
- **Short-lived session tickets.** `POST /api/session` mints a 256-bit,
  300-second ticket accepted **only** on `/api/screen`, `/api/screen/stream`
  and `/api/record/download` — the three endpoints an `<img>`/`<video>` tag
  cannot authenticate with a header. Tickets cannot start AVDs or run a shell.
- **Explicit CORS / CSRF.** Wildcard `*` replaced by an `--origin` allowlist
  (or `NOVADROID_ORIGINS`); same-origin POSTs still work; `Sec-Fetch-Site:
  cross-site` without an allowed Origin is refused. `--strict-host` adds
  DNS-rebinding protection.
- **Dangerous endpoints opt-in.** `/api/shell` and `/api/shared/reveal` now
  return 403 unless launched with `--allow-shell` / `--allow-gui`.
- **Input hardening in one place.** `core/safe.py` centralises shell quoting,
  serial/AVD-name/package allowlists, APK path confinement and RAM-suffix
  parsing. Every module that shells out to `adb` now validates its serial.
- **Error redaction.** `/api/info` no longer returns the SDK path; subprocess
  timeouts no longer echo host paths; unhandled exceptions return a generic
  message and log locally.
- **Single-flight cache.** Concurrent cache misses share one slow SDK call
  instead of all running it (`_cached()` + `_INFLIGHT`).

### New: engine intelligence & 2026 emulator compatibility

- **`/api/engine` (Engine & compatibility card)** — reads `emulator -version`,
  classifies Stable/Patch channel against the researched 37.2.12 / 37.2.13
  releases (verified 2026-11-05), checks WHPX readiness, the API 37 4 GB RAM
  floor, the 4-vCPU ceiling, Lavapipe detection, Vulkan/snapshot conflicts and
  the AEHD Dec 31 2026 sunset, and produces per-AVD readiness rows.
- **An `outdated` verdict is actionable.** `/api/engine` returns a structured
  `engine.update` object (`available`, `installed`, `target`, `command`,
  `alt_command`, `note`) and the dashboard shows a warning with the exact
  copyable command: `sdkmanager "emulator"`. NovaDroid never mutates the SDK
  over HTTP — this is informational only. After updating, restart the agent;
  `emulator -version` is re-read on demand. Verified on the dev host by
  upgrading `37.1.11.0 → 37.2.13.0` (build 16740729), after which the card
  reported `channel: current` / `update.available: false`.
- **`/api/smoothness/flags`** — wires `runtime_flags()` into a "Suggest raw
  flags" button that always returns usable, conservative `-cores` / `-memory` /
  `-gpu host` advice (never below the API 37 RAM floor, never Vulkan + snapshot).
- **`/api/avds/environment` (GET/POST)** — emulator 36.6.11 `environment`
  camera mode. Sets `hw.camera.back`, maps the legacy `virtualscene` value, and
  creates a comment-only `environment.ini` placeholder. It deliberately does
  **not** invent an unpublished scene schema and never overwrites user scenes.
- **37.1.11 RAM unit suffixes** — `core/safe.memory_mb()` parses `6G`, `4096M`,
  `4194304K` and bare `4096`, so auto-tune and the engine report stop
  misreading modern `config.ini` values.

### Defects fixed (all pinned by tests)

- `getprops()` interpolated property names raw into a device-side `for` loop;
  `ai._pkg_versions()` did the same with package names — both are now allowlisted
  and POSIX-quoted.
- `screenshot_bytes()` used the fixed `/sdcard/s.png` fallback, so a live MJPEG
  stream and a one-off capture raced each other. Now per-call names + `finally`
  cleanup.
- `fix_window()` still moved a live window when the AVD name was invalid.
- `runner.run()` timeouts echoed the full command line (host paths) to clients.
- `assist()` returned raw `str(e)` to the dashboard; now logged locally only.
- Zip-Slip/decompression-bomb guards on AVD restore, AVD-name validation at
  `avd_dir()` (lowest layer), locked job/record/autosync state, and removal of
  the last dead code (`inifile.delete`, `ai._installed`, `runner._err`).
- `sh_quote()` now strips **all** C0 controls (the first v5 draft missed `\t`,
  `\n`, `\r`), with a separate `has_text_control()` so multi-line clipboard text
  still works.

## v4.0.0 — competitive gap-closure + perf

Researched every 2026 competitor (BlueStacks 5/10/Air, LDPlayer 9–14, MuMu 12,
Nox, MEmu 9, GameLoop, Genymotion) and closed the manager-layer gaps that
mattered. Full audit of the v3 code first — every fix below is pinned by a
test in `tests/test_v4.py`.

### Bugs fixed (all pinned by tests)

- **HTTP 413 wrote two responses on one socket** — `jread` sent
  `send_error(413)` and then returned `{}` so `do_POST` wrote a 200 JSON after
  it; the browser received garbage. Oversized uploads now yield exactly one
  clean error (`TestWaitBoot*` neighbours; raw-socket verified live).
- **`input text` deleted apostrophes** — "it's a test" arrived as "its a
  test". Payload is now double-quoted with only `$ ` \\ `"` escaped (and `%`
  doubled for `input`'s own escape), so spaces and apostrophes survive
  (`TestInputTextEscaping`).
- **`wait_boot` reported `waited_s: 0`** — hardcoded, never measured.
- **Recording download loaded the whole mp4 into RAM** — a 3-minute 8 Mbps
  recording is ~180 MB per click; `_download` now streams 1 MiB chunks.
- **Base64 round-trip on every frame** — `/api/screen` and the MJPEG stream
  encoded each PNG then immediately decoded it (~8 ms CPU per frame at
  10 fps). New `adb.screenshot_bytes()` serves raw bytes.
- **Backup zips were Windows-only** — arcnames used `os.sep`; on any other
  machine the entries extracted as literal `name\file` paths. Now
  forward-slash (`as_posix`), and `restore` streams entries under the NEW
  instance name instead of extracting the old one.
- **`ai_status` N+1 round-trips** — six `pm list packages | grep` calls →
  one `pm list packages` filtered in Python; versions batched into one
  round-trip too.
- **`adb push --sync` had no fallback** — older platform-tools reject the
  flag; push now retries once without it.
- **Dead code removed** — `inifile.get`/`inifile.lines_of`,
  `apps.LOGCAT_LEVELS`, unused `_unauthorized(path)` param
  (the last stale `inifile.delete` was removed in v5);
  `display`/`window` now build AVD paths through `avd.avd_dir()` instead of
  three private copies.

### New: competitive features the stock engine (and most rivals) lack

- **AVD toolkit** — per-AVD disk usage with a breakdown (userdata vs
  snapshots vs cache), Reclaim (drops snapshots+cache, keeps apps/data, fstrim
  if running), zip **Backup/Restore** into `NovaDroid-Shared\Backups`
  (portable across machines), **Rename**, and an honest **root toggle**
  (Play images refuse by design — the answer says so).
- **Port forwarding manager** — `adb forward`/`reverse` with a rules table,
  abstract-socket targets (Chrome DevTools!), remove-one/remove-all.
- **High-FPS mode** — 60/90/120 Hz live via global refresh settings, *read
  back to verify the image accepted it* (competitors claim 120; we prove it),
  plus persist-to-AVD (`hw.lcd.refresh`).
- **Keyboard control / keymaps** — map host keys → tap/swipe/keyevent in the
  browser ("Map next key → next screen tap"), WASD preset, saved locally.
  This is the BlueStacks Controls idea as a pure manager layer over the
  official engine.
- **Eco mode** — guest animation scales to 0 + `pm trim-caches`: less idle
  CPU/RAM for background instances; one click to restore.
- **Batch fleet install** — one APK onto every online emulator in parallel.
- **Deep links** — open https/app/market URIs on the device from the
  dashboard.
- **Live HTTP proxy** (set/clear with verification) and **wireless ADB**
  (`adb tcpip` + guest IP reported).
- **Multi-instance synchronizer upgraded** — mirror tap *or* swipe *or* text
  *or* key to all instances in parallel (`/api/sync-input`; old
  `/api/sync-tap` still works).
- **Macros with loops** — repeat N × with a pause between passes
  (farm-choreography shaped).

### Dashboard redesign

Tab navigation (System · Display · Apps · Simulation · AI · Files ·
Automation · Tools), light + dark theme with persistence, toast notifications
replacing blocking `alert()` calls in the hot paths (the old build froze the
live view whenever it told you something worked), drag-drop an .apk anywhere
→ upload → install, Save-frame button (works mid-stream), focus rings,
custom scrollbars, and empty consoles now explain themselves instead of
looking like rendering glitches.

## v3.0.0 — AI era + smoothness

### Research refresh (2026)

Re-checked the engine landscape (`RESEARCH.md` updated): **37.1.11 (Jul 30 2026)**
is current stable. Notables that changed decisions:

- **AEHD sunsets Dec 31 2026** → WHPX is no longer "recommended", it is mandatory.
- **API 37 hard-floors RAM at 4 GB** → auto-tune must refuse to go below it.
- **Lavapipe is now the default software Vulkan renderer** (36.4.9) → the render
  probe has to recognise it, not just SwiftShader.
- **Zero-config multi-AVD networking** (36.5.10) → no port-forward scripts.
- **Gemini Nano / AICore is NOT shipped in emulator system images** → any "AI"
  feature that claims to run Gemini Nano in an emulator is lying. NovaDroid
  reports this honestly (see below).

### Bugs fixed (verified by the 163-test suite)

- **HTTP 500 on every dashboard load with a partial SDK** — `avd.list_avds()`
  referenced `r` after the disk scan, but `r` was only assigned inside the
  `if android_cli` / `elif emulator` branches. With an AVD on disk and neither
  CLI present, the server raised `UnboundLocalError`. Reproduced and fixed;
  `tests/test_regressions.py::TestListAvdsWithNoCli` pins it.
- **`stop_avd` always reported success** — the source of the zombie-QEMU lock
  noted in project memory. It now tracks the real outcome, falls back to
  `adb emu kill`, and re-checks that the device actually left before declaring
  victory (`TestStopAvdHonesty`).
- **Headless QEMU was invisible to process detection** — matching was exact-name,
  so `qemu-system-x86_64-headless.exe` (the exact zombie binary) never showed up
  in host telemetry *or* the off-screen window rescue. Both now prefix-match
  `qemu-system-*` while keeping `emulator` exact, and `window.py` keeps its
  case-sensitive `^Android Emulator - ` title guard (`TestHeadlessQemuDetection`).
- **Truncated screenshots were accepted as valid** — `_is_png` used a fragile
  `and`/`or` chain that passed any ≥1 KB blob with the magic bytes. Now requires
  magic + ≥1024 bytes + `IEND` in the last 16 bytes (`TestScreenshotIntegrity`).
- **N+1 adb round-trips** — device props went 6 calls → 1 batched `getprops`
  (which also fixes a latent positional-shift bug when a property is unset),
  `compat.device_cpu` 6 → 1, and `apps.grant_all` ~35 → 1 shell loop.
- **Thread safety** — `_serial_map` mutated outside its lock and returned a live
  reference; now locked across the whole rebuild and returns copies.
- **TTL cache stamped the *start* time** — entries were born expired under load,
  causing duplicate concurrent work. Now stamped after the call.
- **Test suite polluted the CWD** — `test_compat` wrote `__fakesdk` into the
  working directory with best-effort cleanup, so an interrupted run broke every
  later run with `FileExistsError`. Now uses `tempfile`.

### Smoothness engine (`core/perf.py`)

The "runs seamlessly smooth" deliverable. It doesn't guess — it measures the
guest, the render path, and the host, then names the bottleneck.

- Parses `dumpsys gfxinfo` (janky %, p50/p90/p95/p99) into a **0–100 score**
  against the real 16.7 ms budget, with a verdict (smooth / acceptable / janky).
- Probes the **render path** and classifies it: `host` (gfxstream on real GPU) >
  ANGLE > `lavapipe`/`swiftshader` (software). Software rendering is the single
  biggest cause of emulator jank, and it's now called out by name.
- **Ranks bottlenecks** with severity and a concrete fix for each.
- **Auto-tunes** the AVD: 4 vCPU ceiling, ≥4 GB RAM floor for API 37+,
  `hw.gpu.mode=auto`, `fastboot.forceColdBoot=no`, heap aware of RAM.
- Keeps a 120-sample rolling history for the dashboard sparkline.
- Endpoints: `GET /api/smoothness{,/diagnose,/history,/renderer}`,
  `GET /api/tune`, `POST /api/tune/apply`.

### AI layer (`core/ai.py`)

Deliberately **offline, stdlib-only, no API key**. Three parts:

1. **On-device AI status** — layers AICore → Private Compute Services → GMS →
   NNAPI → ML runtimes, and reports plainly that **Gemini Nano cannot run in an
   emulator** because AICore isn't in the system image. It then lists what *can*
   be validated here (NNAPI delegation, LiteRT/TFLite, ML Kit non-GenAI). An AI
   feature that quietly can't work is worse than no AI feature.
2. **Logcat triage** — pure-function dedup by crash signature, ranked by
   severity, surfacing crashes / ANRs / native tombstones / OOM kills.
3. **Natural-language control** — deterministic intent dispatch over ~20 intents
   ("start pixel 7", "tap 500 900", "take a screenshot", "what's slow?").
   Missing arguments are reported rather than guessed.
- Endpoints: `GET /api/ai{,/triage,/nnapi}`,
  `POST /api/ai/command`, `/api/ai/interpret`, `/api/ai/triage`.

### Live view

`/api/screen/stream` is a `multipart/x-mixed-replace` MJPEG stream with
duplicate-frame skipping and frame-budget pacing — replacing per-frame `<img>`
polling in the ⚡ Stream button (falls back to polling where streaming is unsafe).

## v2.0.0 — bugs fixed & hardening

Found and fixed during deep code review (verified by the 94-test suite):

- **Battery set was a no-op** — `adb shell battery set status <word>` was rejected
  by newer emulators; now sends the numeric code (`unknown=1 … full=5`) and
  reads the value back to confirm. `battery/reset` re-arms.
- **Clipboard reported success on unsupported consoles** — when the emulator
  console has no clipboard implementation it still exits 0; now detected and
  reported honestly (`{"ok":false,"supported":false,…}`) so the UI shows a
  "use Send instead" hint instead of a silent failure.
- **Pre-release version sort bug** (`core/compat._version_key`) — `"36.0.0-rc1"`
  sorted *above* its release, so `find_aapt2` could pick a candidate build-tools
  over the stable one. Releases now rank above pre-releases.
- **Three divergent `.ini` updaters** (`window.py`/`display.py`/`avd.py`) — merged
  into one comment-aware `core/inifile.upsert` that never rewrites commented-out
  keys, preserves order, appends missing keys, and never duplicates.
- **Stale multi-instance serial map** — `_serial_map` caches per-instance
  `adb emu avd name` resolution with a TTL; callers now force-refresh when
  stopping a specific AVD so killing AVD B can't accidentally kill AVD A.
- **Dead/unreachable shell defense** — `run()` hardens empty args (returns a
  clean error instead of spawning) and maps `FileNotFoundError` → code 127.
- **Fast, binary-safe screenshot** — `adb exec-out screencap -p` first (1
  process, no temp file), validated by PNG magic before falling back.

## New feature: Export files to Desktop

Files shuttled through the shared folder live in `NovaDroid-Shared/Device` and
`NovaDroid-Shared/Desktop` (buried in your home dir). **Export to Desktop**
copies selected files out to your *real* Windows Desktop in one click:

- **Per-file**: each row in the PC and Device lists has a 🖥 Export button.
- **Bulk**: `🖥 Export PC → Desktop` and `🖥 Export Device → Desktop` copy the
  whole list at once.
- `source=pc` copies straight from the staging folder; `source=device` pulls the
  named files from the emulator first (needs the device online), then copies.
- The destination is resolved via the shell (CSIDL_DESKTOPDIRECTORY), so it lands
  on your real Desktop even when it's OneDrive-redirected or localized.
- **Copy, never move** — the source stays put. On a name clash the copy is kept
  with a `(NovaDroid)` suffix so nothing is ever clobbered.
- Backend: `core/shared.export_to_desktop` + `core/shared.windows_desktop`;
  route `POST /api/shared/export`. Covered by `tests/test_shared_export.py`.

## Performance cheat-sheet (from research)

- Always x86_64 image + WHPX (8–12× TCG). 4 vCPU / ≥4GB (API 37 floor).
- `-gpu host` first; fallback `swiftshader_indirect`/`lavapipe`; `-gpu software` = best software stack.
- Vulkan + snapshots don't mix → `-no-snapshot` when testing Vulkan paths.
- First boot = minutes (Play provisioning); pin quickboot after that (6–15s).
- **Don't hand-tune** — hit **Check tune** / **Apply tune** in the Smoothness
  panel; it applies the rules above to the specific AVD and verifies them.

## Verify

```powershell
# full regression + smoothness + AI + v4/v5 suite (241 tests, no device/SDK needed)
python -m unittest discover -s tests -v
node --check static/app.js
python -m compileall -q novadroid.py core tests
# smoke-test a running agent
python novadroid.py --no-browser &
curl http://localhost:8080/api/health
curl http://localhost:8080/api/info
curl http://localhost:8080/api/engine
curl http://localhost:8080/api/smoothness
curl http://localhost:8080/api/ai
```

Lint (dev-only venv, not needed to run): `pip install pyflakes` →
`python -m pyflakes novadroid.py core/*.py tests/*.py` should be silent.
