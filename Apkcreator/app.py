#!/usr/bin/env python3
"""
APK Ads Injector - web app
Upload an APK, inject an ad (AdMob stub or self-hosted web banner) into it,
recompile and re-sign. Runs apktool + apksigner on the backend.

NOTE on real AdMob: serving real AdMob ads requires the Google Play Services
/ play-services-ads SDK classes bundled in the APK, plus an AdMob account
linked to the app's package. This tool injects the manifest + Activity +
layout wiring and the ad config (App ID / ad unit ID you supply). For a
guaranteed-rendering banner without the full SDK, use the "Web banner" mode
which loads your own ad HTML / a mediation URL in a WebView.
"""

import os
import re
import secrets
import shutil
import subprocess
import threading
import uuid
from pathlib import Path

from flask import (Flask, request, render_template_string, send_file,
                   redirect, url_for, flash, jsonify, session)

# in-memory job registry for async builds (jid -> {state,msg,file,name})
JOBS = {}

# ---- session quota (per-browser). 2 conversions + 1 injection free; watch a
# reward ad to earn 1 conversion + 1 injection; premium = unlimited. ----
FREE_CONV = 2
FREE_INJ = 1

BASE = Path(__file__).resolve().parent
WORK = BASE / "work"
UPLOAD = BASE / "uploads"
OUT = BASE / "outputs"
for p in (WORK, UPLOAD, OUT):
    p.mkdir(exist_ok=True)

KEYSTORE = BASE / "debug.keystore"
KS_ALIAS = "apkads"
KS_PASS = "apkads123"

# apktool is shipped as a .jar here (the `apktool` wrapper is a 0-byte stub)
import glob
_APKTOOL_JARS = sorted(glob.glob(str(BASE / "apktool*.jar"))) or \
    sorted(glob.glob("/data/data/com.termux/files/usr/bin/apktool*.jar"))
APKTOOL_JAR = _APKTOOL_JARS[0] if _APKTOOL_JARS else "apktool.jar"

def _apktool(args):
    return ["java", "-jar", str(APKTOOL_JAR)] + args

app = Flask(__name__)
app.secret_key = secrets.token_hex(16)

# ---------------------------------------------------------------------------
# Keystore (debug) so we can always re-sign
# ---------------------------------------------------------------------------
def ensure_keystore():
    if KEYSTORE.exists():
        return
    subprocess.run([
        "keytool", "-genkeypair", "-v",
        "-keystore", str(KEYSTORE),
        "-alias", KS_ALIAS,
        "-keyalg", "RSA", "-keysize", "2048", "-validity", "10000",
        "-storepass", KS_PASS, "-keypass", KS_PASS,
        "-dname", "CN=APKAds, OU=Dev, O=APKAds, L=Lagos, ST=Lagos, C=NG"
    ], check=True)

# ---------------------------------------------------------------------------
# APK processing
# ---------------------------------------------------------------------------
AD_ACTIVITY = "com.apkads.AdActivity"

def _run(cmd, cwd=None):
    r = subprocess.run(cmd, cwd=str(cwd) if cwd else None,
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"cmd failed: {' '.join(cmd)}\n{r.stderr[-2000:]}")
    return r

def _xml_escape(s):
    # aapt2 requires apostrophes/quotes in <string> values to be backslash-escaped
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', '&quot;')
            .replace("'", "\\'"))

def _package_name(manifest_path):
    txt = manifest_path.read_text(encoding="utf-8", errors="ignore")
    m = re.search(r'package="([^"]+)"', txt)
    return m.group(1) if m else "com.example.app"

def inject_ads(apk_path: Path, opts: dict) -> Path:
    """
    opts: {
      mode: 'web' | 'admob',
      ad_url: str (web mode),
      app_id: str (admob),
      ad_unit: str (admob),
      show_on_launch: bool
    }
    Returns path to the signed output APK.
    """
    ensure_keystore()
    job = WORK / uuid.uuid4().hex
    dec = job / "decoded"
    dec.mkdir(parents=True, exist_ok=True)

    _run(_apktool(["d", "-f", "-o", str(dec), str(apk_path)]))

    man = dec / "AndroidManifest.xml"
    pkg = _package_name(man)
    smali_dir = dec / "smali"
    if not smali_dir.exists():
        # some apks use smali_classes2..; pick first smali dir
        for d in dec.glob("smali*"):
            smali_dir = d
            break

    # ---- inject resources ----
    res = dec / "res"
    values = res / "values"
    values.mkdir(parents=True, exist_ok=True)

    # ad config xml (consumed by the injected AdActivity)
    def _xml_escape(s):
        # aapt2 requires apostrophes/quotes in <string> values to be backslash-escaped
        return (str(s).replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;").replace('"', '&quot;')
                .replace("'", "\\'"))

    ad_cfg = values / "apkads_config.xml"
    ad_cfg.write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<resources>\n'
        f'    <string name="apkads_network">{_xml_escape(opts.get("network", "web"))}</string>\n'
        f'    <string name="apkads_ad_url">{_xml_escape(opts.get("ad_url", ""))}</string>\n'
        f'    <string name="apkads_ad_html">{_xml_escape(opts.get("ad_html", ""))}</string>\n'
        f'    <string name="apkads_github">{_xml_escape(opts.get("github", ""))}</string>\n'
        # AdMob
        f'    <string name="apkads_admob_app">{_xml_escape(opts.get("admob_app", ""))}</string>\n'
        f'    <string name="apkads_admob_banner">{_xml_escape(opts.get("admob_banner", ""))}</string>\n'
        f'    <string name="apkads_admob_inter">{_xml_escape(opts.get("admob_inter", ""))}</string>\n'
        f'    <string name="apkads_admob_reward">{_xml_escape(opts.get("admob_reward", ""))}</string>\n'
        # Meta
        f'    <string name="apkads_meta_placement">{_xml_escape(opts.get("meta_placement", ""))}</string>\n'
        # Unity
        f'    <string name="apkads_unity_game">{_xml_escape(opts.get("unity_game", ""))}</string>\n'
        f'    <string name="apkads_unity_placement">{_xml_escape(opts.get("unity_placement", ""))}</string>\n'
        # AppLovin
        f'    <string name="apkads_al_sdk">{_xml_escape(opts.get("al_sdk", ""))}</string>\n'
        f'    <string name="apkads_al_zone">{_xml_escape(opts.get("al_zone", ""))}</string>\n'
        '</resources>\n')

    # banner layout (kept for compatibility; AdActivity builds its own UI)
    layout = res / "layout"
    layout.mkdir(parents=True, exist_ok=True)
    (layout / "apkads_banner.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<LinearLayout xmlns:android="http://schemas.android.com/apk/res/android"\n'
        '    android:id="@+id/apkads_banner_container"\n'
        '    android:layout_width="match_parent"\n'
        '    android:layout_height="wrap_content"\n'
        '    android:orientation="vertical"\n'
        '    android:background="#000000" />\n')

    # ---- compile AdActivity from Java source (no smali assembler needed) ----
    android_jar = _find_android_jar()
    new_dex = job / "adactivity.dex"
    _compile_ad_activity(opts, android_jar, new_dex)

    # keep the original app's dex(es) so we don't reassemble smali
    orig_dexes = _extract_original_dexes(apk_path, job)

    # ---- patch manifest ----
    man_txt = man.read_text(encoding="utf-8", errors="ignore")
    # add internet permission if missing
    if "android.permission.INTERNET" not in man_txt:
        man_txt = man_txt.replace(
            "<manifest",
            '<manifest\n    xmlns:tools="http://schemas.android.com/tools"')
        ins = '    <uses-permission android:name="android.permission.INTERNET" />\n'
        man_txt = man_txt.replace("</manifest>", ins + "</manifest>")

    # add AdActivity (launcher when show_on_launch, otherwise a plain activity)
    if opts.get("show_on_launch"):
        # capture the CURRENT launcher activity so "Open App" can start it
        # (getLaunchIntentForPackage would return AdActivity after the swap).
        orig_activity = ""
        for m in re.finditer(r'<activity\b[^>]*android:name="([^"]+)"[^>]*>(.*?)</activity>',
                             man_txt, flags=re.S):
            if "MAIN" in m.group(0) and "LAUNCHER" in m.group(0):
                orig_activity = m.group(1)
                break
        if orig_activity:
            # resolve relative names (.Foo / Foo) against the package
            if orig_activity.startswith("."):
                orig_activity = pkg + orig_activity
            elif "." not in orig_activity:
                orig_activity = pkg + "." + orig_activity
            (values / "apkads_launch.xml").write_text(
                '<?xml version="1.0" encoding="utf-8"?>\n<resources>\n'
                f'    <string name="apkads_orig_activity">{_xml_escape(orig_activity)}</string>\n'
                '</resources>\n')
        launcher_filter = (
            '    <activity android:name="%s"\n'
            '        android:exported="true"\n'
            '        android:configChanges="orientation|screenSize">\n'
            '        <intent-filter>\n'
            '            <action android:name="android.intent.action.MAIN" />\n'
            '            <category android:name="android.intent.category.LAUNCHER" />\n'
            '        </intent-filter>\n'
            '    </activity>\n' % AD_ACTIVITY)
        # remove the original launcher filter so only our AdActivity launches
        man_txt = re.sub(
            r'<activity\b[^>]*android:name="([^"]+)"[^>]*>.*?</activity>',
            lambda m: re.sub(r'<intent-filter>.*?</intent-filter>', '',
                            m.group(0), flags=re.S) if 'MAIN' in m.group(0) else m.group(0),
            man_txt, flags=re.S)
    else:
        launcher_filter = (
            '    <activity android:name="%s"\n'
            '        android:exported="true"\n'
            '        android:configChanges="orientation|screenSize" />\n' % AD_ACTIVITY)
    man_txt = man_txt.replace("</application>", launcher_filter + "</application>")

    # aapt2 (this Termux build) cannot parse <queries>; drop it for rebuild.
    # (Package-visibility queries are Android 11+ only; app still installs/runs.)
    man_txt = re.sub(r"<queries>.*?</queries>", "", man_txt, flags=re.S)
    man.write_text(man_txt, encoding="utf-8")

    # (show_on_launch handled via manifest launcher swap; no smali hook needed)

    # ---- rebuild (apktool's aapt2 launcher is broken on Termux; do it manually) ----
    built = job / "built.apk"
    _build_manual(dec, built, orig_dexes, new_dex)

    # ---- sign ----
    out_apk = OUT / (apk_path.stem + "_with_ads.apk")
    aligned = job / "aligned.apk"
    _run(["zipalign", "-p", "4", str(built), str(aligned)])
    _run(["apksigner", "sign", "--v1-signing-enabled", "true",
          "--v2-signing-enabled", "true", "--v3-signing-enabled", "false",
          "--ks", str(KEYSTORE), "--ks-key-alias", KS_ALIAS,
          "--ks-pass", f"pass:{KS_PASS}",
          "--out", str(out_apk), str(aligned)])
    return out_apk

def _build_manual(dec, out_apk, orig_dexes, new_dex):
    """Build a fresh unsigned APK from an apktool-decoded dir using aapt2
    (apktool's aapt2 launcher is broken on Termux). The original app's dex
    files and our freshly compiled AdActivity dex are added into the APK."""
    android_jar = _find_android_jar()

    b = dec / "build"
    b.mkdir(parents=True, exist_ok=True)

    # 1) compile resources
    res_zip = b / "res.zip"
    _run(["aapt2", "compile", "--dir", str(dec / "res"),
          "-o", str(res_zip), "--legacy"])

    # 2) link -> base apk (resources.arsc + AndroidManifest)
    # apktool strips versionCode/versionName/sdk into apktool.yml; re-inject them
    # as aapt2 flags or the APK installs-rejects (empty versionCode, targetSdk<4).
    import re as _re
    yml = dec / "apktool.yml"
    meta = {}
    if yml.exists():
        t = yml.read_text(errors="ignore")
        for k in ("minSdkVersion", "targetSdkVersion", "versionCode", "versionName"):
            m = _re.search(rf"{k}:\s*['\"]?([^'\"\n]+)", t)
            if m:
                meta[k] = m.group(1).strip()
    link_cmd = ["aapt2", "link", "-o", str(out_apk),
                "-I", str(android_jar),
                "--manifest", str(dec / "AndroidManifest.xml"),
                "--auto-add-overlay", str(res_zip)]
    if meta.get("minSdkVersion"):
        link_cmd += ["--min-sdk-version", meta["minSdkVersion"]]
    if meta.get("targetSdkVersion"):
        link_cmd += ["--target-sdk-version", meta["targetSdkVersion"]]
    if meta.get("versionCode"):
        link_cmd += ["--version-code", meta["versionCode"]]
    if meta.get("versionName"):
        link_cmd += ["--version-name", meta["versionName"]]
    for extra in dec.glob("**/res/*.zip"):
        if extra != res_zip:
            link_cmd.append(str(extra))
    link_cmd += ["--no-version-vectors"]
    _run(link_cmd)

    # 3) add original dex(es) + our new AdActivity dex
    import zipfile, shutil as _sh
    tmp = b / "base_with_dex.apk"
    if tmp.exists():
        tmp.unlink()
    _sh.copy(str(out_apk), str(tmp))
    with zipfile.ZipFile(str(tmp), "a") as z:
        # original dex files
        for i, dex in enumerate(sorted(orig_dexes)):
            name = "classes.dex" if i == 0 else f"classes{i+1}.dex"
            z.write(str(dex), name)
        # our injected activity (place after originals)
        z.write(str(new_dex), f"classes{len(orig_dexes)+1}.dex")
        # carry over original assets (e.g. html web-app payloads) if present
        assets_dir = dec / "assets"
        if assets_dir.exists():
            for p in sorted(assets_dir.rglob("*")):
                if p.is_file():
                    z.write(str(p), "assets/" + str(p.relative_to(assets_dir)))
    _sh.move(str(tmp), str(out_apk))

def _find_android_jar():
    import glob as _glob
    ajars = (sorted(_glob.glob(str(BASE / "android*.jar"))) or
             sorted(_glob.glob("/data/data/com.termux/files/home/android-sdk/platforms/*/android.jar")))
    if not ajars:
        raise RuntimeError("android.jar not found (needed to compile AdActivity)")
    return ajars[-1]  # prefer highest API level present

def _extract_original_dexes(apk_path, job):
    """Pull classes*.dex out of the original APK so we can reuse them verbatim."""
    import zipfile
    dex_dir = job / "orig_dex"
    dex_dir.mkdir(parents=True, exist_ok=True)
    out = []
    with zipfile.ZipFile(str(apk_path)) as z:
        for n in z.namelist():
            if re.match(r"classes\d*\.dex$", n):
                p = dex_dir / n
                with z.open(n) as src, open(p, "wb") as dst:
                    dst.write(src.read())
                out.append(p)
    return out

def _web_app_java():
    """A minimal WebView shell Activity: loads a URL or a bundled HTML asset."""
    return '''package com.apkads;

import android.app.Activity;
import android.content.res.AssetManager;
import android.os.Bundle;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.widget.FrameLayout;

public class WebViewAppActivity extends Activity {
    private String str(String name) {
        int id = getResources().getIdentifier(name, "string", getPackageName());
        if (id == 0) return "";
        try { return getString(id); } catch (Exception e) { return ""; }
    }

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        FrameLayout root = new FrameLayout(this);
        WebView wv = new WebView(this);
        wv.setBackgroundColor(0xFF0B0E1A);
        WebSettings ws = wv.getSettings();
        ws.setJavaScriptEnabled(true);
        ws.setDomStorageEnabled(true);
        ws.setLoadWithOverviewMode(true);
        ws.setUseWideViewPort(true);
        ws.setMixedContentMode(WebSettings.MIXED_CONTENT_ALWAYS_ALLOW);
        String html = str("apkads_src_html");
        if ("1".equals(html)) {
            wv.loadUrl("file:///android_asset/webapp.html");
        } else {
            String u = str("apkads_src_url");
            if (u == null || u.isEmpty()) u = "about:blank";
            wv.loadUrl(u);
        }
        root.addView(wv);
        setContentView(root);
    }
}
'''

def _compile_web_app(android_jar, out_dex):
    """Compile WebViewAppActivity from Java to a dex with javac + d8."""
    import tempfile, shutil as _sh
    src_dir = out_dex.parent / "websrc"
    if src_dir.exists():
        _sh.rmtree(str(src_dir))
    src_dir.mkdir(parents=True, exist_ok=True)
    pkg_dir = src_dir / "com" / "apkads"
    pkg_dir.mkdir(parents=True, exist_ok=True)
    (pkg_dir / "WebViewAppActivity.java").write_text(_web_app_java())
    class_file = pkg_dir / "WebViewAppActivity.class"
    _run(["javac", "-cp", str(android_jar), "-d", str(src_dir),
          str(pkg_dir / "WebViewAppActivity.java")])
    if not class_file.exists():
        raise RuntimeError("javac did not produce WebViewAppActivity.class")
    dex_dir = out_dex.parent / "webdex"
    if dex_dir.exists():
        _sh.rmtree(str(dex_dir))
    dex_dir.mkdir(parents=True, exist_ok=True)
    _run(["d8", "--release", "--min-api", "24", "--output", str(dex_dir), str(class_file)])
    produced = dex_dir / "classes.dex"
    if not produced.exists():
        raise RuntimeError("d8 did not produce classes.dex")
    _sh.move(str(produced), str(out_dex))

def build_web_apk(source_type, value, app_name="My App") -> Path:
    """Convert a web URL / raw HTML / GitHub link into a base WebView APK.

    source_type: 'web' (URL), 'html' (raw markup), 'github' (repo URL)
    Returns a signed base APK (no ads yet)."""
    import zipfile as _zf, shutil as _sh
    if source_type not in ("web", "html", "github"):
        raise ValueError("source_type must be web/html/github")
    if not value or not value.strip():
        raise ValueError("a source value is required")
    value = value.strip()
    job = WORK / f"web_{uuid.uuid4().hex}"
    job.mkdir(parents=True, exist_ok=True)
    dec = job / "dec"
    (dec / "res" / "values").mkdir(parents=True, exist_ok=True)
    (dec / "assets").mkdir(parents=True, exist_ok=True)
    _make_icon(dec)

    is_html = (source_type == "html")
    # strings: app name + either the URL or the html flag
    strings = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<resources>\n'
        f'    <string name="app_name">{_xml_escape(app_name)}</string>\n'
        f'    <string name="apkads_src_html">{"1" if is_html else "0"}</string>\n'
        f'    <string name="apkads_src_url">{_xml_escape("" if is_html else value)}</string>\n'
        '</resources>\n'
    )
    (dec / "res" / "values" / "strings.xml").write_text(strings)
    if is_html:
        # store raw html as asset
        (dec / "assets" / "webapp.html").write_text(value, encoding="utf-8")

    manifest = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<manifest xmlns:android="http://schemas.android.com/apk/res/android"\n'
        '    package="com.apkads.webapp"\n'
        '    android:versionCode="1" android:versionName="1.0">\n'
        '    <uses-sdk android:minSdkVersion="24" android:targetSdkVersion="34" />\n'
        '    <uses-permission android:name="android.permission.INTERNET" />\n'
        '    <application android:allowBackup="true"\n'
        '        android:label="@string/app_name"\n'
        '        android:icon="@drawable/ic_launcher"\n'
        '        android:theme="@android:style/Theme.Material.Light.NoActionBar">\n'
        '        <activity android:name="com.apkads.WebViewAppActivity"\n'
        '            android:exported="true"\n'
        '            android:configChanges="orientation|screenSize">\n'
        '            <intent-filter>\n'
        '                <action android:name="android.intent.action.MAIN" />\n'
        '                <category android:name="android.intent.category.LAUNCHER" />\n'
        '            </intent-filter>\n'
        '        </activity>\n'
        '    </application>\n'
        '</manifest>\n'
    )
    (dec / "AndroidManifest.xml").write_text(manifest)

    android_jar = _find_android_jar()
    new_dex = job / "webactivity.dex"
    _compile_web_app(android_jar, new_dex)

    built = job / "built.apk"
    _build_manual(dec, built, [], new_dex)

    out_apk = OUT / f"{_safe_name(app_name)}_base.apk"
    aligned = job / "aligned.apk"
    _run(["zipalign", "-p", "4", str(built), str(aligned)])
    _run(["apksigner", "sign", "--v1-signing-enabled", "true",
          "--v2-signing-enabled", "true", "--v3-signing-enabled", "false",
          "--ks", str(KEYSTORE), "--ks-key-alias", KS_ALIAS,
          "--ks-pass", f"pass:{KS_PASS}",
          "--out", str(out_apk), str(aligned)])
    return out_apk

def _safe_name(s):
    return re.sub(r"[^A-Za-z0-9_-]", "_", s)[:40] or "app"


def _make_icon(dec):
    """Generate a colorful launcher icon PNG into res/drawable/ic_launcher.png."""
    from pathlib import Path as _P
    svg = '''<svg xmlns="http://www.w3.org/2000/svg" width="512" height="512" viewBox="0 0 512 512">
  <defs>
    <linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#00E0C6"/>
      <stop offset="0.55" stop-color="#3A1C5A"/>
      <stop offset="1" stop-color="#0B6E6E"/>
    </linearGradient>
  </defs>
  <rect x="0" y="0" width="512" height="512" rx="116" fill="url(#g)"/>
  <circle cx="256" cy="256" r="150" fill="none" stroke="#ffffff" stroke-width="22" opacity="0.95"/>
  <ellipse cx="256" cy="256" rx="64" ry="150" fill="none" stroke="#ffffff" stroke-width="15" opacity="0.8"/>
  <line x1="106" y1="256" x2="406" y2="256" stroke="#ffffff" stroke-width="15" opacity="0.8"/>
  <path d="M232 196 L344 256 L232 316 Z" fill="#ffffff"/>
</svg>'''
    sv = dec / "icon.svg"
    sv.write_text(svg)
    out = dec / "res" / "drawable" / "ic_launcher.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    _run(["rsvg-convert", "-w", "512", "-h", "512", str(sv), "-o", str(out)])
    sv.unlink()
    return out.exists()


def _make_icon_web(out_path):
    """Render the standard launcher icon PNG for the website (same art as APK)."""
    svg = '''<svg xmlns="http://www.w3.org/2000/svg" width="512" height="512" viewBox="0 0 512 512">
  <defs>
    <linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#00E0C6"/>
      <stop offset="0.55" stop-color="#3A1C5A"/>
      <stop offset="1" stop-color="#0B6E6E"/>
    </linearGradient>
  </defs>
  <rect x="0" y="0" width="512" height="512" rx="116" fill="url(#g)"/>
  <circle cx="256" cy="256" r="150" fill="none" stroke="#ffffff" stroke-width="22" opacity="0.95"/>
  <ellipse cx="256" cy="256" rx="64" ry="150" fill="none" stroke="#ffffff" stroke-width="15" opacity="0.8"/>
  <line x1="106" y1="256" x2="406" y2="256" stroke="#ffffff" stroke-width="15" opacity="0.8"/>
  <path d="M232 196 L344 256 L232 316 Z" fill="#ffffff"/>
</svg>'''
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    sv = out_path.with_suffix(".svg")
    sv.write_text(svg)
    _run(["rsvg-convert", "-w", "512", "-h", "512", str(sv), "-o", str(out_path)])
    sv.unlink()


def _ad_activity_java(opts):
    # Values are read from string resources injected at build time, so the
    # activity stays generic. Booleans/constants here are compile-time only.
    show = "true" if opts.get("show_on_launch") else "false"
    return '''package com.apkads;

import android.app.Activity;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.content.res.ColorStateList;
import android.graphics.Color;
import android.graphics.drawable.GradientDrawable;
import android.graphics.drawable.StateListDrawable;
import android.graphics.Typeface;
import android.net.Uri;
import android.os.Bundle;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.widget.Button;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.Space;
import android.widget.TextView;

public class AdActivity extends Activity {
    private static final boolean SHOW_ON_LAUNCH = __SHOW__;
    private WebView adView;

    // read a string from THIS app's resources by name (package-agnostic)
    private String str(String name) {
        int id = getResources().getIdentifier(name, "string", getPackageName());
        if (id == 0) return "";
        try { return getString(id); } catch (Exception e) { return ""; }
    }

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        // ---- premium animated gradient backdrop ----
        FrameLayout root = new FrameLayout(this);
        GradientDrawable bg = new GradientDrawable(
                GradientDrawable.Orientation.TL_BR,
                new int[]{0xFF0F1220, 0xFF1B2A4A, 0xFF3A1C5A, 0xFF0B6E6E});
        bg.setCornerRadius(0f);
        root.setBackground(bg);

        // ---- top bar with 3-dot overflow menu ----
        FrameLayout topbar = new FrameLayout(this);
        FrameLayout.LayoutParams tblp = new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, dp(54));
        tblp.gravity = Gravity.TOP;
        topbar.setLayoutParams(tblp);
        TextView dot = new TextView(this);
        dot.setText("\u22EE"); // ⋮
        dot.setTextColor(Color.parseColor("#EAF0FF"));
        dot.setTextSize(26);
        dot.setTypeface(Typeface.DEFAULT_BOLD);
        FrameLayout.LayoutParams dlp = new FrameLayout.LayoutParams(
                dp(48), dp(48), Gravity.END | Gravity.CENTER_VERTICAL);
        dlp.rightMargin = dp(8);
        dot.setLayoutParams(dlp);
        dot.setGravity(Gravity.CENTER);
        topbar.addView(dot);
        root.addView(topbar);

        // ---- glass card ----
        LinearLayout card = new LinearLayout(this);
        card.setOrientation(LinearLayout.VERTICAL);
        int pad = dp(20);
        card.setPadding(pad, pad, pad, pad);
        GradientDrawable glass = new GradientDrawable();
        glass.setColor(Color.parseColor("#14FFFFFF"));
        glass.setStroke(1, Color.parseColor("#33FFFFFF"));
        glass.setCornerRadius(dp(22));
        card.setBackground(glass);
        FrameLayout.LayoutParams clp = new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        clp.gravity = Gravity.CENTER;
        clp.leftMargin = dp(18); clp.rightMargin = dp(18);
        card.setLayoutParams(clp);

        // ---- header ----
        TextView hdr = new TextView(this);
        hdr.setText("SPONSORED");
        hdr.setTextColor(Color.parseColor("#9FE7FF"));
        hdr.setTextSize(11);
        hdr.setTypeface(Typeface.DEFAULT_BOLD);
        hdr.setLetterSpacing(0.28f);
        card.addView(hdr);

        Space s1 = new Space(this); s1.setMinimumHeight(dp(10)); card.addView(s1);

        // ---- creative (custom HTML > per-network tag > web URL) ----
        WebView wv = new WebView(this);
        adView = wv;
        wv.setBackgroundColor(Color.TRANSPARENT);
        WebSettings ws = wv.getSettings();
        ws.setJavaScriptEnabled(true);
        ws.setLoadWithOverviewMode(true);
        ws.setUseWideViewPort(true);
        String html = str("apkads_ad_html");
        if (html != null && !html.isEmpty()) {
            wv.loadDataWithBaseURL(null, html, "text/html", "UTF-8", null);
        } else {
            String url = buildCreative();
            if (url == null || url.isEmpty()) url = "about:blank";
            wv.loadUrl(url);
        }
        LinearLayout.LayoutParams wlp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, dp(220));
        wv.setLayoutParams(wlp);
        card.addView(wv);

        Space s2 = new Space(this); s2.setMinimumHeight(dp(14)); card.addView(s2);

        // ---- GitHub button ----
        String gh = str("apkads_github");
        if (gh != null && !gh.isEmpty()) {
            Button ghBtn = makeButton("View on GitHub", "#22232b", "#FFD1D5E0");
            ghBtn.setOnClickListener(v -> openLink(gh));
            card.addView(ghBtn);
            Space s3 = new Space(this); s3.setMinimumHeight(dp(10)); card.addView(s3);
        }

        // ---- primary "Open App" button (single refined default) ----
        Button open = makeButton("Open App", "#00E0C6", "#FF04121F");
        open.setOnClickListener(v -> launchOriginalApp());
        card.addView(open);

        // ---- 3-dot overflow menu (Open App / Reload Ad) ----
        FrameLayout menu = new FrameLayout(this);
        menu.setVisibility(View.GONE);
        FrameLayout.LayoutParams mlp = new FrameLayout.LayoutParams(
                dp(180), ViewGroup.LayoutParams.WRAP_CONTENT, Gravity.END | Gravity.TOP);
        mlp.topMargin = dp(54); mlp.rightMargin = dp(10);
        menu.setLayoutParams(mlp);
        GradientDrawable mg = new GradientDrawable();
        mg.setColor(Color.parseColor("#161B2E"));
        mg.setStroke(1, Color.parseColor("#2A3350"));
        mg.setCornerRadius(dp(14));
        menu.setBackground(mg);
        menu.setPadding(dp(6), dp(6), dp(6), dp(6));

        TextView miOpen = new TextView(this);
        miOpen.setText("Open App");
        miOpen.setTextColor(Color.parseColor("#EAF0FF"));
        miOpen.setTextSize(15);
        miOpen.setPadding(dp(16), dp(12), dp(16), dp(12));
        miOpen.setOnClickListener(v -> { menu.setVisibility(View.GONE); launchOriginalApp(); });
        TextView miReload = new TextView(this);
        miReload.setText("Reload Ad");
        miReload.setTextColor(Color.parseColor("#EAF0FF"));
        miReload.setTextSize(15);
        miReload.setPadding(dp(16), dp(12), dp(16), dp(12));
        miReload.setOnClickListener(v -> { menu.setVisibility(View.GONE); reloadAd(); });
        View sep = new View(this);
        sep.setBackgroundColor(Color.parseColor("#2A3350"));
        sep.setLayoutParams(new ViewGroup.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 1));
        menu.addView(miOpen);
        menu.addView(sep);
        menu.addView(miReload);
        root.addView(menu);

        dot.setOnClickListener(v -> menu.setVisibility(
                menu.getVisibility() == View.VISIBLE ? View.GONE : View.VISIBLE));
        // tap anywhere outside closes the menu
        root.setOnClickListener(v -> { if (menu.getVisibility() == View.VISIBLE) menu.setVisibility(View.GONE); });

        root.addView(card);
        setContentView(root);

        if (!SHOW_ON_LAUNCH) {
            launchOriginalApp();
            finish();
        }
    }

    // Build a per-network web creative from the injected IDs. Every network
    // renders through this WebView, so we emit the standard web tag/snippet.
    private String buildCreative() {
        String net = str("apkads_network");
        if (net == null) net = "web";
        if ("admob".equals(net)) {
            String app = str("apkads_admob_app");
            String banner = str("apkads_admob_banner");
            if (banner == null || banner.isEmpty()) banner = "ca-app-pub-0000000000000000/0000000000";
            String meta = (app != null && !app.isEmpty())
                ? "<meta name=\\"google-adsense-account\\" content=\\"" + app + "\\">" : "";
            return "<!doctype html><html><head>" + meta +
                "<style>html,body{margin:0;height:100%;background:#0b0e1a;display:flex;" +
                "align-items:center;justify-content:center;font-family:sans-serif}" +
                ".ad{color:#9aa;font-size:13px;text-align:center}</style></head>" +
                "<body><div class='ad'>AdMob banner<br><code>" + banner + "</code></div>" +
                "<script async src=\\"https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js\\"></script>" +
                "<ins class='adsbygoogle' style='display:block' data-ad-client='" + (app!=null?app:"") +
                "' data-ad-slot='" + banner + "' data-ad-format='auto'></ins>" +
                "<script>(adsbygoogle=window.adsbygoogle||[]).push({})</script></body></html>";
        }
        if ("meta".equals(net)) {
            String pid = str("apkads_meta_placement");
            if (pid == null || pid.isEmpty()) pid = "YOUR_PLACEMENT_ID";
            return "<!doctype html><html><head><style>html,body{margin:0;height:100%;" +
                "background:#0b0e1a;display:flex;align-items:center;justify-content:center;" +
                "color:#9aa;font-family:sans-serif}</style></head><body>" +
                "<div>Meta Audience Network<br><code>" + pid + "</code></div>" +
                "<script>(function(d,s,id){var j=d.getElementsByTagName(s)[0];" +
                "if(d.getElementById(id))return;var f=d.createElement(s);f.id=id;" +
                "f.src='https://connect.facebook.net/en_US/fbadSDK.js#placement=" + pid +
                "&format=banner';;j.parentNode.insertBefore(f,j);})(document,'script','fb-sdk');</script>" +
                "</body></html>";
        }
        if ("unity".equals(net)) {
            String game = str("apkads_unity_game");
            String place = str("apkads_unity_placement");
            if (game == null || game.isEmpty()) game = "GAME_ID";
            if (place == null || place.isEmpty()) place = "PLACEMENT_ID";
            return "<!doctype html><html><head><style>html,body{margin:0;height:100%;" +
                "background:#0b0e1a;display:flex;align-items:center;justify-content:center;" +
                "color:#9aa;font-family:sans-serif}</style></head><body>" +
                "<div>Unity Ads<br><code>" + game + " / " + place + "</code></div></body></html>";
        }
        if ("appLovin".equals(net)) {
            String sdk = str("apkads_al_sdk");
            String zone = str("apkads_al_zone");
            if (sdk == null || sdk.isEmpty()) sdk = "SDK_KEY";
            if (zone == null || zone.isEmpty()) zone = "ZONE_ID";
            return "<!doctype html><html><head><style>html,body{margin:0;height:100%;" +
                "background:#0b0e1a;display:flex;align-items:center;justify-content:center;" +
                "color:#9aa;font-family:sans-serif}</style></head><body>" +
                "<div>AppLovin<br><code>" + sdk + " / " + zone + "</code></div></body></html>";
        }
        // web mode
        String url = str("apkads_ad_url");
        return (url != null && !url.isEmpty()) ? url : "about:blank";
    }

    private Button makeButton(String text, String fill, String textColor) {
        Button b = new Button(this);
        b.setText(text);
        b.setTextColor(Color.parseColor(textColor));
        b.setTextSize(16);
        b.setTypeface(Typeface.DEFAULT_BOLD);
        b.setAllCaps(false);
        float r = dp(16);
        GradientDrawable normal = new GradientDrawable();
        normal.setColor(Color.parseColor(fill));
        normal.setCornerRadius(r);
        GradientDrawable pressed = new GradientDrawable();
        pressed.setColor(Color.parseColor("#55000000"));
        pressed.setCornerRadius(r);
        StateListDrawable st = new StateListDrawable();
        st.addState(new int[]{android.R.attr.state_pressed}, pressed);
        st.addState(new int[]{}, normal);
        b.setBackground(st);
        b.setStateListAnimator(null);
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, dp(52));
        b.setLayoutParams(lp);
        return b;
    }

    private int dp(int v) {
        return (int) (v * getResources().getDisplayMetrics().density);
    }

    private void openLink(String url) {
        Intent i = new Intent(Intent.ACTION_VIEW, Uri.parse(url));
        i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        startActivity(i);
    }

    private void reloadAd() {
        if (adView == null) return;
        String html = str("apkads_ad_html");
        if (html != null && !html.isEmpty()) {
            adView.loadDataWithBaseURL(null, html, "text/html", "UTF-8", null);
        } else {
            String url = buildCreative();
            if (url == null || url.isEmpty()) url = "about:blank";
            adView.loadUrl(url);
        }
    }

    private void launchOriginalApp() {
        // After the launcher swap, getLaunchIntentForPackage() returns THIS
        // AdActivity, so start the captured original activity explicitly.
        String orig = str("apkads_orig_activity");
        try {
            if (orig != null && !orig.isEmpty()) {
                Intent i = new Intent();
                i.setClassName(getPackageName(), orig);
                i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                startActivity(i);
                finish();
                return;
            }
        } catch (Exception ignored) {}
        PackageManager pm = getPackageManager();
        Intent i = pm.getLaunchIntentForPackage(getPackageName());
        if (i != null) {
            i.setComponent(null);
            i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            startActivity(i);
        }
        finish();
    }
}

''' .replace("__SHOW__", show)

def _compile_ad_activity(opts, android_jar, out_dex):
    """Compile the AdActivity Java source to a dex with javac + d8."""
    import tempfile
    src_dir = out_dex.parent / "adsrc"
    src_dir.mkdir(parents=True, exist_ok=True)
    java_file = src_dir / "AdActivity.java"
    pkg_dir = src_dir / "com" / "apkads"
    pkg_dir.mkdir(parents=True, exist_ok=True)
    (pkg_dir / "AdActivity.java").write_text(_ad_activity_java(opts))

    class_file = src_dir / "com" / "apkads" / "AdActivity.class"
    _run(["javac", "-cp", str(android_jar), "-d", str(src_dir),
          str(pkg_dir / "AdActivity.java")])
    if not class_file.exists():
        raise RuntimeError("javac did not produce AdActivity.class")
    # d8 requires output to be a .zip/.jar or an existing directory
    import shutil as _sh
    dex_dir = out_dex.parent / "addex"
    if dex_dir.exists():
        _sh.rmtree(str(dex_dir))
    dex_dir.mkdir(parents=True, exist_ok=True)
    _run(["d8", "--release", "--min-api", "24",
          "--output", str(dex_dir), str(class_file)])
    produced = dex_dir / "classes.dex"
    if not produced.exists():
        raise RuntimeError("d8 did not produce classes.dex")
    _sh.move(str(produced), str(out_dex))

# ---------------------------------------------------------------------------
# Flask routes
# ---------------------------------------------------------------------------
INDEX = '''<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>APK Ads Injector</title>
<style>
 :root{--bg:#070912;--panel:#10152a;--panel2:#0c1122;--line:#222a44;--txt:#eaf0ff;
   --muted:#8a96bd;--accent:#5b8cff;--accent2:#00e0c6;--danger:#ff6b8a;--ok:#39d98a}
 *{box-sizing:border-box}
 body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;margin:0;
   background:radial-gradient(1100px 600px at 85% -15%,#16244a 0,transparent 55%),
              radial-gradient(900px 520px at -10% 110%,#2a1550 0,transparent 55%),var(--bg);
   color:var(--txt);min-height:100vh;padding:26px 18px 40px}
 .wrap{max-width:1080px;margin:auto}
 header{display:flex;align-items:center;gap:14px;margin-bottom:22px}
 .logo{width:42px;height:42px;border-radius:12px;flex:0 0 auto;
   background:linear-gradient(135deg,var(--accent),var(--accent2));
   display:flex;align-items:center;justify-content:center;font-weight:900;font-size:20px;color:#04121f;
   box-shadow:0 6px 22px #5b8cff55}
 .credbar{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-bottom:20px;
   background:#0c1122;border:1px solid var(--line);border-radius:14px;padding:12px 14px}
 .credbar .pill{font-size:13px;color:#cdd6f5;background:#121a30;border:1px solid var(--line);
   border-radius:999px;padding:7px 13px}
 .credbar .pill b{color:var(--accent2)}
 .credbar .pill.ok{color:#04121f;background:#00E0C6;border:0;font-weight:700}
 .credbar a.act{margin-left:auto;font-size:12.5px;font-weight:700;text-decoration:none;
   padding:8px 13px;border-radius:999px;background:#16305c;color:#dbe8ff;border:1px solid #2c4a86}
 .credbar a.act.prem{background:linear-gradient(90deg,var(--accent),var(--accent2));color:#04121f;border:0}
 .brand h1{font-size:19px;margin:0;letter-spacing:.2px}
 .brand p{margin:2px 0 0;color:var(--muted);font-size:12.5px}
 .grid{display:grid;grid-template-columns:1.15fr .85fr;gap:20px;align-items:start}
 @media(max-width:860px){.grid{grid-template-columns:1fr}}
 .panel{background:linear-gradient(180deg,rgba(20,26,46,.85),rgba(12,17,34,.85));
   border:1px solid var(--line);border-radius:18px;padding:22px;box-shadow:0 18px 50px #0008;
   backdrop-filter:blur(12px)}
 .panel h2{font-size:13px;text-transform:uppercase;letter-spacing:.14em;color:var(--muted);
   margin:0 0 14px;font-weight:700}
 fieldset{border:1px solid var(--line);border-radius:14px;padding:16px;margin:0 0 16px;background:var(--panel2)}
 legend{padding:0 8px;font-size:12px;color:var(--accent);font-weight:700;letter-spacing:.05em}
 label{display:block;margin:12px 0 6px;font-weight:600;font-size:12.5px;color:#cdd6f5}
 input[type=file],input[type=text],textarea{width:100%;padding:11px 12px;box-sizing:border-box;
   border-radius:10px;border:1px solid var(--line);background:#070b16;color:var(--txt);
   font-size:13.5px;font-family:inherit;transition:.15s}
 input:focus,textarea:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px #5b8cff2e}
 textarea{resize:vertical;min-height:84px}
 .row{display:flex;gap:12px}.row>div{flex:1}
 .hint{font-size:11.5px;color:var(--muted);margin-top:5px;line-height:1.45}
 .net{display:grid;grid-template-columns:repeat(5,1fr);gap:8px}
 .net label{margin:0;cursor:pointer}
 .net input{display:none}
 .net .sw{display:block;text-align:center;font-weight:700;font-size:11.5px;color:#cfd8f5;
   border:1px solid var(--line);border-radius:10px;padding:10px 2px;transition:.15s}
 .net input:checked+.sw{border-color:var(--accent);background:#16305c;box-shadow:0 0 0 2px #5b8cff44}
 .chk{display:flex;align-items:center;gap:9px;margin-top:6px;font-size:13px;color:#dbe3ff}
 .chk input{width:16px;height:16px;accent-color:var(--accent)}
 button.go{margin-top:6px;width:100%;padding:15px;border:0;border-radius:13px;cursor:pointer;
   background:linear-gradient(90deg,var(--accent),var(--accent2));color:#04121f;font-size:15.5px;
   font-weight:800;letter-spacing:.3px;transition:.15s}
 button.go:hover{filter:brightness(1.08);transform:translateY(-1px)}
 .flash{background:#3a2330;color:#ffb3c8;padding:10px 12px;border-radius:10px;margin-bottom:14px;font-size:13px}
 /* preview */
 .phone{width:240px;max-width:100%;margin:6px auto 0;border-radius:30px;padding:12px;
   background:#04060d;border:2px solid #1c2540;box-shadow:0 14px 40px #000a;position:relative}
 .phone .scr{border-radius:20px;overflow:hidden;background:linear-gradient(160deg,#0f1220,#1b2a4a,#3a1c5a,#0b6e6e);
   min-height:360px;padding:18px 14px;display:flex;flex-direction:column;align-items:center;justify-content:center}
 .tag{font-size:9px;letter-spacing:.26em;color:#9fe7ff;font-weight:800;margin-bottom:10px}
 .glass{width:100%;background:#14ffffff;border:1px solid #33ffffff;border-radius:16px;padding:12px;
   backdrop-filter:blur(6px);box-shadow:0 8px 24px #0006}
 .creative{width:100%;height:120px;border-radius:10px;background:#0a0e1a;border:1px dashed #2c3658;
   display:flex;align-items:center;justify-content:center;text-align:center;color:#7e8bb5;font-size:11px;padding:8px;word-break:break-word}
 .ghbtn{margin-top:10px;width:100%;padding:9px;border:0;border-radius:10px;background:#22232b;color:#d1d5e0;font-size:12px;font-weight:700}
 .openbtn{margin-top:8px;width:100%;padding:11px;border:0;border-radius:12px;background:#00e0c6;color:#04121f;font-size:13px;font-weight:800}
 .preview-note{text-align:center;color:var(--muted);font-size:11.5px;margin-top:14px}
 footer{text-align:center;color:var(--muted);font-size:11.5px;margin-top:26px;line-height:1.6}
 a{color:var(--accent2)}
</style></head>
<body><div class="wrap">
<header>
  <img class="logo" src="/icon.png" alt="Web2APK">
  <div class="brand">
    <h1>Web2APK</h1>
    <p>Upload an APK · pick a network · get a re-signed APK with your ad baked in</p>
  </div>
</header>
<div class="credbar">
  {% if premium %}
    <span class="pill ok">★ Premium — unlimited conversions &amp; injections</span>
  {% else %}
    <span class="pill">🔁 Conversions left: <b>{{ conv }}</b></span>
    <span class="pill">💉 Injections left: <b>{{ inj }}</b></span>
    <a class="act" href="/watch">🎁 Watch reward ad (+1 / +1)</a>
    <a class="act prem" href="/premium">⭐ Go Premium</a>
  {% endif %}
</div>
<div class="grid">
  <div class="panel">
    <h2>Configuration</h2>
    {% with msgs = get_flashed_messages() %}{% if msgs %}<div class="flash">{{ msgs[0] }}</div>{% endif %}{% endwith %}
    <form method="post" enctype="multipart/form-data" action="/convert">
      <fieldset>
        <legend>Source app (build from)</legend>
        <div class="net">
          <label><input type="radio" name="src" value="web" checked onchange="selSrc('web')"><span class="sw">Web URL</span></label>
          <label><input type="radio" name="src" value="html" onchange="selSrc('html')"><span class="sw">HTML</span></label>
          <label><input type="radio" name="src" value="github" onchange="selSrc('github')"><span class="sw">GitHub</span></label>
        </div>
        <div id="srcWeb" style="margin-top:12px">
          <label>Website URL</label>
          <input type="text" name="src_url" placeholder="https://your-site.com">
        </div>
        <div id="srcHtml" style="display:none;margin-top:12px">
          <label>Raw HTML (wrapped into the app)</label>
          <textarea name="src_html" placeholder="<html>...your page...</html>"></textarea>
        </div>
        <div id="srcGithub" style="display:none;margin-top:12px">
          <label>GitHub repo / page URL</label>
          <input type="text" name="src_github" placeholder="https://github.com/user/repo">
        </div>
        <label>App name</label>
        <input type="text" name="app_name" placeholder="My Web App">
        <button type="submit" class="go" style="margin-top:14px">Convert to APK</button>
        <div class="hint">Turns the URL / HTML / GitHub link above into a WebView APK, then injects your chosen ad network.</div>
      </fieldset>

      <fieldset>
        <legend>Or upload an existing APK</legend>
        <label>APK file (optional — skip if building from source above)</label>
        <input type="file" name="apk" accept=".apk">
      </fieldset>

      <input type="hidden" name="show_on_launch" value="on">

      <fieldset>
        <legend>Network ads</legend>
        <div class="net">
          <label><input type="radio" name="network" value="admob" checked onchange="selNet('admob')"><span class="sw">AdMob</span></label>
          <label><input type="radio" name="network" value="meta" onchange="selNet('meta')"><span class="sw">Meta</span></label>
          <label><input type="radio" name="network" value="unity" onchange="selNet('unity')"><span class="sw">Unity</span></label>
          <label><input type="radio" name="network" value="appLovin" onchange="selNet('appLovin')"><span class="sw">AppLovin</span></label>
        </div>

        <div id="admobFields" style="margin-top:14px">
          <div class="row"><div><label>AdMob App ID</label><input type="text" name="admob_app" placeholder="ca-app-pub-xxx~yyy"></div>
            <div><label>Banner unit ID</label><input type="text" name="admob_banner" placeholder="ca-app-pub-xxx/zzz"></div></div>
          <div class="row"><div><label>Interstitial unit ID</label><input type="text" name="admob_inter" placeholder="ca-app-pub-xxx/iii"></div>
            <div><label>Rewarded unit ID</label><input type="text" name="admob_reward" placeholder="ca-app-pub-xxx/rrr"></div></div>
          <div class="hint">App ID + unit IDs baked into the APK and rendered via AdMob's web tag.</div>
        </div>

        <div id="metaFields" style="display:none;margin-top:14px">
          <label>Meta Placement ID</label><input type="text" name="meta_placement" placeholder="YOUR_PLACEMENT_ID">
        </div>

        <div id="unityFields" style="display:none;margin-top:14px">
          <div class="row"><div><label>Unity Game ID</label><input type="text" name="unity_game" placeholder="GAME_ID"></div>
            <div><label>Placement ID</label><input type="text" name="unity_placement" placeholder="PLACEMENT_ID"></div></div>
        </div>

        <div id="appLovinFields" style="display:none;margin-top:14px">
          <div class="row"><div><label>AppLovin SDK Key</label><input type="text" name="al_sdk" placeholder="SDK_KEY"></div>
            <div><label>Zone / Ad Unit ID</label><input type="text" name="al_zone" placeholder="ZONE_ID"></div></div>
        </div>
      </fieldset>

      <button type="submit" class="go">Build &amp; Inject APK</button>
    </form>
  </div>

  <div class="panel">
    <h2>Live ad (Web / HTML creative)</h2>
    <div class="hint" style="margin-bottom:12px">This is what actually shows on the ad screen. Paste an <b>Ad URL</b> or your own <b>HTML</b> ad tag (banner, affiliate link, your network's web snippet). The network IDs on the left are saved into the APK for store uploads.</div>
    <label>Ad URL (renders live in the ad screen)</label>
    <input type="text" name="ad_url" placeholder="https://your-domain.com/ad.html" oninput="upd()">
    <label>Custom HTML ad creative (optional — overrides the URL)</label>
    <textarea name="ad_html" placeholder="<div style='...'>your ad markup</div>" oninput="upd()"></textarea>
    <div class="hint">Tip: paste your real ad tag here (e.g. an AdSense/AdMob <code>adsbygoogle</code> snippet, an affiliate banner, or any HTML) to see a real ad on device.</div>

    <h2 style="margin-top:24px">Live preview</h2>
    <div class="phone"><div class="scr">
      <div class="tag">SPONSORED</div>
      <div class="glass">
        <div class="creative" id="pvCreative">Your ad preview appears here</div>
        <div class="ghbtn" id="pvGh" style="display:none">View on GitHub</div>
        <div class="openbtn" id="pvOpen">Open App</div>
      </div>
    </div></div>
    <div class="preview-note">Preview updates as you type. Final screen renders inside the injected APK.</div>
  </div>
</div>
<footer>
  Web2APK · self-hosted on Termux · public access via trycloudflare tunnel<br>
  Build a WebView APK from web/HTML/GitHub, then inject AdMob / Meta / Unity / AppLovin. Ready to ship to Google Play or other stores.
</footer>
</div>
<script>
 function selSrc(n){
   ['web','html','github'].forEach(k=>{
     document.getElementById('src'+k.charAt(0).toUpperCase()+k.slice(1)).style.display=(k===n)?'block':'none';
   });
 }
 function selNet(n){
   ['admob','meta','unity','appLovin'].forEach(k=>{
     document.getElementById(k+'Fields').style.display=(k===n)?'block':'none';
   });
   upd();
 }
 function val(n){var e=document.querySelector('[name='+n+']');return e?e.value.trim():'';}
 function upd(){
   var net=val('network')||'admob';
   var c=document.getElementById('pvCreative');
   var h=val('ad_html'), u=val('ad_url');
   if(h){ c.textContent='HTML creative set ('+h.length+' chars)'; }
   else if(u){ c.textContent=u; }
   else { c.textContent = net==='admob'?'AdMob · '+(val('admob_banner')||'no banner'):
                       net==='meta'?'Meta · '+(val('meta_placement')||'no placement'):
                       net==='unity'?'Unity · '+(val('unity_game')||'no game id'):
                       'AppLovin · '+(val('al_sdk')||'no sdk key'); }
   var gh=val('github');
   document.getElementById('pvGh').style.display = gh? 'block':'none';
 }
 upd();
</script>
</body></html>'''

@app.route("/")
def index():
    q = _quota()
    return render_template_string(INDEX, conv=q["conv"], inj=q["inj"], premium=q["premium"])

@app.route("/inject", methods=["POST"])
def inject():
    if not _spend("inj"):
        flash("Injection limit reached. Watch a reward ad or unlock Premium for more.")
        return redirect(url_for("index"))
    f = request.files.get("apk")
    if not f or not (f.filename or "").endswith(".apk"):
        flash("Upload a valid .apk file.")
        return redirect(url_for("index"))
    opts = _collect_opts()
    apk_path = UPLOAD / f"{uuid.uuid4().hex}_{f.filename}"
    f.save(str(apk_path))
    try:
        out = inject_ads(apk_path, opts)
    except Exception as e:
        flash(f"Build failed: {e}")
        return redirect(url_for("index"))
    return send_file(str(out), as_attachment=True,
                     download_name=out.name)

@app.route("/convert", methods=["POST"])
def convert():
    """Async: start the build in a background thread, return a progress page
    immediately (long builds otherwise time out the tunnel/browser)."""
    opts = _collect_opts()
    src = (request.form.get("src", "web") or "web").strip()
    app_name = (request.form.get("app_name", "My App") or "My App").strip()

    value = ""
    if src in ("web", "html", "github"):
        raw = (request.form.get("src_url") if src == "web"
               else request.form.get("src_html") if src == "html"
               else request.form.get("src_github"))
        value = (raw or "").strip()

    # need either a source value or an uploaded APK up front
    up_path = None
    f = request.files.get("apk")
    if f and (f.filename or "").endswith(".apk"):
        up_path = UPLOAD / f"{uuid.uuid4().hex}_{f.filename}"
        f.save(str(up_path))
    if not value and up_path is None:
        flash("Provide a source (web/HTML/GitHub) or upload an APK.")
        return redirect(url_for("index"))

    if not _spend("conv"):
        flash("Conversion limit reached. Watch a reward ad or unlock Premium for more.")
        return redirect(url_for("index"))

    jid = uuid.uuid4().hex
    JOBS[jid] = {"state": "running", "msg": "Building base APK…", "file": None, "name": None}

    def worker():
        try:
            apk_path = up_path
            if value:
                JOBS[jid]["msg"] = "Converting source to APK…"
                apk_path = build_web_apk(src, value, app_name)
            JOBS[jid]["msg"] = "Injecting ads & signing…"
            out = inject_ads(apk_path, opts)
            JOBS[jid].update(state="done", msg="Ready", file=str(out), name=out.name)
        except Exception as e:
            JOBS[jid].update(state="error", msg=str(e))

    threading.Thread(target=worker, daemon=True).start()
    return render_template_string(PROGRESS, jid=jid)


@app.route("/status/<jid>")
def status(jid):
    j = JOBS.get(jid)
    if not j:
        return jsonify({"state": "error", "msg": "unknown job"}), 404
    return jsonify({"state": j["state"], "msg": j["msg"], "name": j.get("name")})


@app.route("/download/<jid>")
def download(jid):
    j = JOBS.get(jid)
    if not j or j["state"] != "done" or not j["file"]:
        flash("Build not ready.")
        return redirect(url_for("index"))
    return send_file(j["file"], as_attachment=True, download_name=j["name"])


# ---------------------------------------------------------------------------
# Session quota: conversions + injections, reward ad, premium
# ---------------------------------------------------------------------------
def _quota():
    """Return session quota dict, initializing the free allowance."""
    q = session.get("quota")
    if not q:
        q = {"conv": FREE_CONV, "inj": FREE_INJ, "premium": False}
        session["quota"] = q
    return q


def _spend(kind):
    q = _quota()
    if q.get("premium"):
        return True
    if q.get(kind, 0) <= 0:
        return False
    q[kind] = q[kind] - 1
    session["quota"] = q
    return True


@app.route("/watch")
def watch():
    """Demo rewarded-ad page. A real integration would embed AdMob/House
    rewarded video; here the user confirms they watched, which grants +1."""
    q = _quota()
    return render_template_string(REWARD_PAGE, conv=q["conv"], inj=q["inj"])


@app.route("/claim")
def claim():
    """Grant +1 conversion and +1 injection after 'watching' the reward ad."""
    q = _quota()
    if not q.get("premium"):
        q["conv"] = q.get("conv", 0) + 1
        q["inj"] = q.get("inj", 0) + 1
        session["quota"] = q
    flash("Reward collected: +1 conversion and +1 injection added.")
    return redirect(url_for("index"))


@app.route("/premium")
def premium():
    """Demo unlock. Wire a real gateway (Paystack/Selar) here later."""
    q = _quota()
    q["premium"] = True
    session["quota"] = q
    flash("Premium unlocked — unlimited conversions & injections.")
    return redirect(url_for("index"))


@app.route("/icon.png")
def icon_png():
    """Serve the same gradient launcher icon shown in generated APKs."""
    icon = BASE / "icon_web.png"
    if not icon.exists():
        _make_icon_web(icon)
    return send_file(str(icon), mimetype="image/png")


PROGRESS = '''<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Building…</title>
<style>body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
background:#070912;color:#eaf0ff;font-family:system-ui,sans-serif}
.box{text-align:center;max-width:460px;padding:32px}
.spin{width:54px;height:54px;margin:0 auto 22px;border:5px solid #1b2340;border-top-color:#00E0C6;
border-radius:50%;animation:r 1s linear infinite}@keyframes r{to{transform:rotate(360deg)}}
h1{font-size:20px;margin:0 0 8px}#msg{color:#9fb0d0;font-size:15px;min-height:22px}
a.btn,button.btn{display:inline-block;margin-top:22px;background:#00E0C6;color:#04121f;
text-decoration:none;font-weight:700;padding:13px 26px;border:0;border-radius:14px;font-size:16px;cursor:pointer}
.err{color:#ff8a8a}a.back{display:block;margin-top:16px;color:#7f8db0;font-size:13px}</style></head>
<body><div class="box">
<div class="spin" id="spin"></div>
<h1 id="ttl">Building your APK…</h1>
<div id="msg">Starting…</div>
<div id="act"></div>
<a class="back" href="/">← Back</a>
</div>
<script>
var jid="{{ jid }}";
function poll(){
 fetch("/status/"+jid).then(r=>r.json()).then(j=>{
  document.getElementById("msg").textContent=j.msg||"";
  if(j.state==="done"){
   document.getElementById("spin").style.display="none";
   document.getElementById("ttl").textContent="APK ready ✓";
   document.getElementById("act").innerHTML='<a class="btn" href="/download/'+jid+'">Download APK</a>';
   window.location="/download/"+jid;
  }else if(j.state==="error"){
   document.getElementById("spin").style.display="none";
   document.getElementById("ttl").textContent="Build failed";
   document.getElementById("ttl").className="err";
   document.getElementById("msg").className="err";
  }else{ setTimeout(poll,1500); }
 }).catch(_=>setTimeout(poll,2000));
}
poll();
</script></body></html>'''


REWARD_PAGE = '''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Watch reward ad · Web2APK</title>
<style>
 body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
   background:radial-gradient(900px 520px at -10% 110%,#2a1550 0,transparent 55%),#070912;
   color:#eaf0ff;font-family:system-ui,sans-serif}
 .box{width:min(440px,92vw);background:#10152a;border:1px solid #222a44;border-radius:20px;
   padding:30px;text-align:center;box-shadow:0 20px 60px #0009}
 h1{font-size:20px;margin:0 0 6px}
 p{color:#a6b2d8;font-size:14px;margin:6px 0 18px}
 .ad{height:240px;border-radius:14px;background:linear-gradient(135deg,#15203a,#241140,#0b3a3a);
   border:1px solid #2c3658;display:flex;align-items:center;justify-content:center;color:#9fb0d0;
   font-size:14px;text-align:center;padding:18px}
 .row{display:flex;gap:12px;margin-top:18px}
 a.btn{flex:1;text-decoration:none;font-weight:800;padding:14px;border-radius:13px;font-size:15px;cursor:pointer}
 .claim{background:#00E0C6;color:#04121f}
 .skip{background:#1a2138;color:#cdd6f5;border:1px solid #2c3658}
 .cred{margin-top:14px;font-size:12.5px;color:#8a96bd}
</style></head>
<body><div class="box">
 <h1>Reward ad</h1>
 <p>Watch the full ad to earn <b>+1 conversion</b> and <b>+1 injection</b>.</p>
 <div class="ad">[ Demo rewarded ad plays here ]<br>In production this embeds your AdMob / House rewarded video unit.</div>
 <div class="row">
   <a class="btn claim" href="/claim">I watched it — collect reward</a>
 </div>
 <a class="btn skip" href="/" style="display:block;margin-top:12px">Skip</a>
 <div class="cred">Your credits now: {{ conv }} conversion(s) · {{ inj }} injection(s)</div>
</div></body></html>'''


def _collect_opts():
    return {
        "network": request.form.get("network", "web").strip() or "web",
        "ad_url": request.form.get("ad_url", "").strip(),
        "ad_html": request.form.get("ad_html", "").strip(),
        "github": request.form.get("github", "").strip(),
        "admob_app": request.form.get("admob_app", "").strip(),
        "admob_banner": request.form.get("admob_banner", "").strip(),
        "admob_inter": request.form.get("admob_inter", "").strip(),
        "admob_reward": request.form.get("admob_reward", "").strip(),
        "meta_placement": request.form.get("meta_placement", "").strip(),
        "unity_game": request.form.get("unity_game", "").strip(),
        "unity_placement": request.form.get("unity_placement", "").strip(),
        "al_sdk": request.form.get("al_sdk", "").strip(),
        "al_zone": request.form.get("al_zone", "").strip(),
        "show_on_launch": "show_on_launch" in request.form,
    }

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False, threaded=True)
