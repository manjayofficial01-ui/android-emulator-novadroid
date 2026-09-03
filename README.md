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

## Project layout

```
novadroid/
  novadroid.py        # stdlib-only server (REST + static), no pip needed
  core/config.py      # SDK discovery, 4CPU/4GB perf baseline
  core/runner.py      # hardened subprocess helpers (empty-arg guard, 127)
  core/accel.py       # WHPX/emulator-check probing
  core/avd.py         # AVD lifecycle + quickboot/raw/wipe + multi-instance map
  core/adb.py         # devices, delta-install, screencap, geo/batt/perf/macros
  core/apps.py        # app launch/stop/clear/uninstall/grant + capability-aware clipboard + boot state
  core/host.py        # Windows host telemetry (CPU/RAM/disk/emulator procs)
  core/inifile.py     # single comment-aware .ini upsert (kills 3 divergent copies)
  core/display.py     # tablet presets, wm size/density, orientation, skins
  core/window.py      # off-screen rescue: ini reset + strict live-move
  core/shared.py      # NovaDroid-Shared <-> /sdcard sync, upload, auto-sync
  core/compat.py      # aapt2 badging + ARM-translation verdicts (version sort fixed)
  static/             # dashboard (index.html/app.js/styles.css, offline-safe)
  scripts/            # setup-whpx.ps1, create-avds.ps1
  tests/              # 88 stdlib unittests (core + display + avd + adb + apps + host + inifile + compat)
```

## API (all JSON except `/api/screen` → image/png, `/api/record/download` → file)

**GET**
`/api/health` · `/api/info` · `/api/avds` · `/api/profiles` · `/api/devices` ·
`/api/jobs` · `/api/screen?serial=` · `/api/layout?serial=` · `/api/perf?serial=` ·
`/api/host` · `/api/boot?serial=` · `/api/macros` · `/api/apps` · `/api/apps/info` ·
`/api/logcat?serial=` · `/api/clipboard?serial=` · `/api/record` · `/api/record/download` ·
`/api/sensors?serial=` · `/api/network?serial=` · `/api/snapshots?serial=` ·
`/api/display/presets` · `/api/display?serial=` · `/api/skins` · `/api/avds/window?serial=` ·
`/api/shared` · `/api/shared/device`

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

**DELETE**
`/api/avds?name=` · `/api/macros?name=`

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

## Verify

```powershell
# full regression + new-module suite (94 tests, no device/SDK needed)
python -m unittest discover -s tests -v
# smoke-test a running agent
python novadroid.py --no-browser &
curl http://localhost:8080/api/health
curl http://localhost:8080/api/info
```
