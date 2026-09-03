"""NovaDroid core package — best-in-class Android emulator manager for Windows.

Engine choice (from deep research):
  Google Android Emulator 37.x (forked QEMU + ranchu board + gfxstream +
  WHPX hypervisor) — the only actively maintained, ad-free, Android 16/17
  capable core. AEHD sunsets Dec 31 2026, so WHPX is mandatory on Windows.
Manager adds what stock lacks vs BlueStacks/LDPlayer/MuMu:
  multi-instance sync, macros/keymap, perf dashboard, one-click APK,
  snapshots/quickboot control, sensor injection, zero-config networking.
Stdlib only — no pip install, no Electron bloat.
"""
