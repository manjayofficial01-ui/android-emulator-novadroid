# ☁ NovaDroid on Vercel — deploy runbook

**Architecture (read this first):** Vercel is serverless Linux — it cannot run
QEMU, the Android SDK, or ADB. So the split is:

```
Vercel (public)                    Your PC (Windows, stays on)
─────────────────                  ──────────────────────────
static dashboard                   python novadroid.py  (agent)
(index/app/styles)  ──tunnel────── emulator.exe · adb · SDK
                    + Bearer token
```

The emulator keeps running at home; the dashboard reaches it from anywhere.

## 1) Deploy the dashboard

```powershell
cd "D:\0000 App Building with Gemini\Android Apk Project\android emulator\novadroid"
vercel --prod
```

Only `static/` uploads (see `.vercelignore`). You get `https://<project>.vercel.app`.

## 2) Start the agent for remote access

```powershell
python novadroid.py --gen-token          # e.g. abCDeF...  (keep secret!)
python novadroid.py --host 0.0.0.0 --port 8080 --token <secret> --no-browser
```

Without `--token`, remote binding is **refused** (the API can run shell
commands — never expose it openly).

## 3) Open the tunnel

```powershell
powershell -ExecutionPolicy Bypass -File scripts\expose-tunnel.ps1 -Port 8080
```

Copy the `https://xxx.trycloudflare.com` URL.

## 4) Connect

Open your Vercel URL → top **🔌 Backend** bar → paste tunnel URL + token →
**Connect**. Everything (AVDs, screenshots, shared folder, APK check) works
remotely. Screenshots/APKs ride the tunnel as normal HTTPS.

## Notes

- Quick-tunnel URLs change on restart — re-paste after reboot. For a stable
  address, make a Cloudflare **named** tunnel once and reuse it.
- Keep the PC awake while accessing remotely; Quickboot keeps VMs warm.
- Rotate the token any time: restart the agent with a new `--token`.
- Local use is unchanged: `.\start.bat` still serves everything on localhost
  with no token needed.
