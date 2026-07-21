# Web2APK

Build WebView APKs with ad injection (AdMob / Meta / Unity / AppLovin) — self-hosted on Android/Termux.

## How it works

1. Enter a URL, HTML, or GitHub Pages link
2. Select ad network
3. Get a signed APK ready for sideloading or Play Store

## Tech

- **Backend:** Flask (Python) on Termux
- **Tunnel:** Cloudflare (trycloudflare quick tunnel)
- **Build:** javac + d8 + aapt2 + apksigner (no Gradle)

## Live

Current tunnel: https://horses-trees-russia-burst.trycloudflare.com
