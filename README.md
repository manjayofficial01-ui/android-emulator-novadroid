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
| Shared folder PC ↔ emulator (APKs, photos, files) | ✅ per-file Export/Import/Delete, Sync-now, exact mirrors either way, Refresh, auto-sync status | manual `adb push/pull` |
| APK compatibility check (aapt2 + ARM-translation verdict) | ✅ Analyze before install | blind install |
| WHPX readiness + AEHD-sunset guidance | ✅ banner | `emulator-check` raw |

## Project layout

```
novadroid/
  novadroid.py        # stdlib-only server (REST + static), no pip needed
  core/config.py      # SDK discovery, 4CPU/4GB perf baseline
  core/runner.py      # hardened subprocess helpers
  core/accel.py       # WHPX/emulator-check probing
  core/avd.py         # AVD lifecycle + quickboot/raw/wipe
  core/adb.py         # devices, delta-install, screencap, geo/batt/perf/macros
  core/display.py     # tablet presets, wm size/density, orientation, skins
  core/window.py      # off-screen rescue: ini reset + strict live-move
  core/shared.py      # NovaDroid-Shared <-> /sdcard sync, upload, auto-sync
  core/compat.py      # aapt2 badging + ARM-translation verdicts
  static/             # dashboard (index.html/app.js/styles.css, offline-safe)
  scripts/            # setup-whpx.ps1, create-avds.ps1
  tests/              # stdlib unittests (core + display)
```

## API (all JSON except /api/screen → image/png)

`GET /api/info|/api/avds|/api/profiles|/api/devices|/api/screen?serial=|/api/layout|/api/perf|/api/macros|/api/jobs`
`POST /api/avds|/api/avds/start{,cold}|/api/avds/start-raw|/api/avds/stop|/api/avds/wipe|/api/apk/install|/api/geo|/api/battery|/api/shell|/api/macros|/api/macros/run|/api/sync-tap`
`DELETE /api/avds?name=`

## Performance cheat-sheet (from research)

- Always x86_64 image + WHPX (8–12× TCG). 4 vCPU / ≥4GB (API 37 floor).
- `-gpu host` first; fallback `swiftshader_indirect`/`lavapipe`; `-gpu software` = best software stack.
- Vulkan + snapshots don't mix → `-no-snapshot` when testing Vulkan paths.
- First boot = minutes (Play provisioning); pin quickboot after that (6–15s).

## Verify

```powershell
python -m unittest discover -s tests -v
python novadroid.py --no-browser &
curl http://localhost:8080/api/health
curl http://localhost:8080/api/info
```
