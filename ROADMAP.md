# NovaDroid Roadmap — smoothness, robustness, and 2026-era features

Researched 2026-11-18 against official sources (developer.android.com,
Genymobile releases, Vercel docs, vendor announcements). Facts carry dates;
anything unverified is flagged. Ordered by impact; each item notes the
existing code it builds on.

---

## P0 — hard external deadline (43 days away)

### 1. AEHD → WHPX migration doctor
**Deadline: Dec 31, 2026.** Google: *"the Android Emulator hypervisor driver
will be sunset on December 31, 2026 … transition by enabling Windows
Hypervisor Platform"* — after that date the AEHD docs are removed and the
driver gets no further updates. ([emulator-acceleration docs](https://developer.android.com/studio/run/emulator-acceleration))

NovaDroid already detects this (`core/accel.py`: WHPX-first probing,
`sunset_note`, `whpx_usable`). What's missing is **action**, not detection:

- Parse `emulator -accel-check` for the AEHD/GVM keywords and `sc query aehd`/
  `gvm`/`intelhaxm` to name the exact legacy driver a user still runs.
- Add a guided migration flow: check WHPX feature state (`DISM /Online
  /Get-FeatureInfo /FeatureName:HypervisorPlatform`), offer the enable command
  + required-reboot warning, and link Android Studio's assisted migration
  (emulator 37.1.11 ships a converter).
- Label AEHD as **legacy** in the engine report (`/api/engine`) so the
  dashboard shows migration status at a glance, not just a tooltip note.

**Files:** `core/accel.py`, `core/engine.py`, `static/app.js` (engine panel).

## P1 — keep the fleet current (small, high-value)

### 2. Emulator pin bump + next-gen engine toggle
- Stable/patch pins (37.2.12 / 37.2.13, `core/engine.py:33-34`) are current —
  37.2.13 (Oct 28, 2026) fixed a host-side GLES memory leak. Keep.
- Canary pin 37.3.6 is 1–2 builds behind: 37.3.8 fixed an Apple-Silicon
  slowness bug; a third-party mirror reports 37.3.9 (Nov 10, 2026) —
  **unverified**. Verify on the official fixed-bugs page (the project's own
  convention, `core/engine.py:35-37`) before bumping.
- Google's next-gen emulator (QEMU v11 upstream, engine/UI process split,
  virtio models) is the default for **Phone AVDs on API 37.0+** in Android
  Studio Rabbit canaries. Add an opt-in "preview engine" note in
  `/api/engine` + a dashboard badge for API 37 fleets.
- Adopt the 37.1.11 niceties: AVD `config.ini` now accepts unit suffixes
  ("6G", "32M") for RAM/heap — `core/avd.py` config writers can emit and
  parse them.

### 3. API 37 fleet readiness
- Enforce the **4 GB RAM floor** at AVD creation for API 37 (emulator
  36.6.11 silently auto-overrides lower values — surface that in the UI
  instead of letting the emulator lie). `core/avd.py:create_avd` /
  `core/perf.py:autotune` (autotune already has a ≥4 GB API-37 floor —
  extend to creation).
- Android 17 (API 37, final Jun 16, 2026) is **adaptive-first**: orientation/
  resizing restrictions on apps are ignored when targeting 37. A cheap
  differentiator for the app manager: flag installed apps that declare fixed
  orientation so testers know what will behave differently on 37.

### 4. scrcpy integration refresh (5.0.1, Oct 2026)
`core/stream.py` already wraps scrcpy 5.x with hardware decoding. Add:
- `--flex-display` (scrcpy 4.0+) — resizable virtual displays, the natural
  partner of the existing `--new-display`/`--start-app` per-app windows.
- `--keep-active` for long dashboard sessions; explicit `--hwdec auto` with
  a software fallback for machines where vendor decode fails.
- Note Windows ARM64 builds exist now (5.0+) — the find_scrcpy hint text can
  mention ARM.
([scrcpy releases](https://github.com/Genymobile/scrcpy/releases))

### 5. adb / platform-tools alignment
- Require platform-tools ≥ **36.0.2** for the shared-folder feature (Windows
  push/pull last-byte truncation fix, #439152273) — `core/shared.py` should
  check `adb version` once and warn.
- `--fastdeploy` is deprecated upstream (37.0.3) in favor of the new
  `android install` — stop emitting it if any code path does.
- Adopt `adb pair --qr` (37.0.3) for wireless pairing UX in the network panel.

## P2 — feature frontier (what competitors just did)

### 6. Desktop-windowing mode as the BlueStacks/MuMu answer
BlueStacks Desktop 16 (Nov 11–13, 2026) and MuMu 6.0 (Jul 9, 2026) now sell
"multiple Android apps in resizable PC windows, for humans and AI agents".
A stock Google-Emulator manager matches this cheaply:
- One-click **"desktop mode" AVD template** on API 36/37 images: enable
  freeform + force-desktop-mode (`settings put global
  enable_freeform_support 1`) so the guest shows a real desktop.
- Pair with scrcpy `--flex-display`/`--new-display` windows (item 4) for the
  PC-side windowing. No emulator fork needed.

### 7. Agent story: interop with Google's `android` CLI
Google now ships an agent-first CLI (`android emulator create/list/start`,
boot-wait built in, `android device hub` = first-party mirroring window,
`android init` installs an agent skill). NovaDroid's agent API is
**structured HTTP** — differentiate, don't compete:
- Detect `android`/`android-cli` on PATH (`core/config.py:ToolPaths` already
  discovers `android_cli`) and surface it in `/api/info`.
- Extend `core/agentskill.py` skill text with a "when to use Google's CLI
  vs NovaDroid" note, and consider shipping NovaDroid's skill in the same
  installable format `android init` uses.

### 8. Multi-device networking exposure
Emulator 36.5.10 (Apr 2026) shipped a zero-config networking stack — Wi-Fi
Direct and NSD work between AVDs out of the box. Expose it: a fleet-network
panel toggling NSD/discovery is a unique feature no competitor offers on
stock images.

### 9. GPU option modernization
Emulator 36.4.9 deprecated `-gpu guest`, `swiftshader_indirect`,
`swangle_indirect` (plus older `angle*`/`mesa` values) and made **Lavapipe**
the default software renderer, adding `-gpu software`.
- Audit every `-gpu` value NovaDroid can write (`core/avd.py` raw flags,
  `core/gpu.py` winreg preferences, `core/perf.py:suggest_flags`) and stop
  emitting deprecated ones.
- `core/perf.py:classify_renderer` already knows "lavapipe" — add the new
  `software` kind and map deprecated modes to their replacements.

## P3 — robustness (engineering hygiene)

### 10. Keep stdlib-only — but satisfy HTTP/1.1 strictly
Verdict from research: the stdlib-only constraint remains right for a
local-first single-user tool (zero install friction, no dependency/CVE
churn, trivially auditable). Python's own docs still warn `http.server` is
"not recommended for production", so earn robustness instead of importing it:
- Every response already sets Content-Length (v5 did this for keep-alive);
  add explicit handler `timeout` tuning and a graceful `shutdown()` path from
  a watcher thread.
- If streaming endpoints ever multiply, SSE over the existing handler works
  without a framework.
- Do **not** adopt FastAPI/aiohttp for the local agent — it would break the
  "nothing to install" promise (requirements.txt exists to say exactly that).

### 11. Deployment split stays honest
Vercel's Python runtime hosts ASGI/WSGI apps only — it cannot reach a user's
localhost adb, so the current split (static dashboard on Vercel + local agent)
is the only correct architecture. Keep `vercel.json` static-only. If a cloud
API surface is ever needed, write a ~30-line WSGI adapter exposing the same
`core/` functions rather than shipping the socket server serverless.

### 12. Testing depth
- 301 logic unittests; the HTTP layer (auth gates, tickets, CORS, Host
  checks) is tested but the *streaming* endpoints (`_stream`, MJPEG,
  `_download` chunking) are only smoke-covered. Add socket-level tests with
  a real ThreadingHTTPServer on an ephemeral port.
- Route-surface staleness is now guarded: `tests/test_surface.py` (added in
  v5.1.1) fails the suite when a route loses all consumers or when the JS
  calls a nonexistent endpoint. Extend it as new surfaces appear.

---

## Sources
- Emulator release notes: https://developer.android.com/studio/releases/emulator
- AEHD sunset + WHPX guidance: https://developer.android.com/studio/run/emulator-acceleration
- AEHD repo: https://github.com/google/android-emulator-hypervisor-driver
- scrcpy releases: https://github.com/Genymobile/scrcpy/releases
- platform-tools notes: https://developer.android.com/tools/releases/platform-tools
- Android 17: https://developer.android.com/about/versions/17
- `android` CLI: https://developer.android.com/tools/agents/android-cli
- Vercel Python runtime: https://docs.vercel.com/functions/runtimes/python
- BlueStacks Desktop: https://www.bluestacks.com/blog/bluestacks-exclusives/bluestacks-desktop-en.html
- MuMu 6.0: https://www.mumuplayer.com/update

*Unverified/caveated: canary 37.3.9 existence (third-party mirror only);
post-deadline AEHD hard-block behavior (not documented anywhere official).*
