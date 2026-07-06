#!/usr/bin/env python3
"""
Amal Airways — Flight Management System (fused client)
======================================================
ONE app. Native window (not a browser). Everything welded together:

  • The dashboard (map, login, fleet, create-flight, dark mode, SOP alert box)
  • The live bridge (feeds your flight to the map, on or off VATSIM)
  • The ACARS engine (SimConnect preflight checklist, violation siren,
    landing capture, slew detection) — started from the Create Flight screen

This is the file PyInstaller turns into AmalAirways.exe.

RUN (dev):
    pip install pywebview
    python AmalAirways.py

BUILD THE .EXE (one line):
    python -m PyInstaller --onefile --windowed --name AmalAirways ^
        --add-data "AmalAirways.html;." AmalAirways.py

Then post dist\\AmalAirways.exe for pilots. They double-click it — no Python,
no browser, no terminal. Opens like a normal desktop app.
"""

import os, sys, threading, time, json

# ---------- locate bundled files (works both as .py and as PyInstaller .exe) ----------
def resource_path(name):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)

HTML_PATH = resource_path("AmalAirways.html")

# ---------- bring in the bridge server (embedded, same process) ----------
# We reuse the standalone bridge logic but run it in a background thread.
import http.server, socketserver

PORT = 8770
LIVE = {}
ALERT = {"msg": "All clear", "level": "clear", "ts": 0}
LOCK = threading.Lock()
STALE_SECONDS = 30

def _prune():
    now = time.time()
    with LOCK:
        for cs in [k for k, v in LIVE.items() if now - v.get("_ts", 0) > STALE_SECONDS]:
            del LIVE[cs]

class _Handler(http.server.SimpleHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        if isinstance(body, str): body = body.encode()
        self.wfile.write(body)
    def do_OPTIONS(self): self._send(204, b"")
    def do_GET(self):
        if self.path in ("/", "/index.html", "/dashboard"):
            if os.path.exists(HTML_PATH):
                with open(HTML_PATH, "rb") as f: self._send(200, f.read(), "text/html")
            else:
                self._send(404, "AmalAirways.html not found")
            return
        if self.path == "/live":
            _prune()
            with LOCK: self._send(200, json.dumps({"flights": list(LIVE.values())}))
            return
        if self.path == "/alerts":
            with LOCK: self._send(200, json.dumps(ALERT))
            return
        self._send(404, json.dumps({"error": "not found"}))
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        try: data = json.loads(self.rfile.read(length) or b"{}")
        except Exception: data = {}
        if self.path == "/alert":
            with LOCK:
                ALERT["msg"] = data.get("msg", "All clear")
                ALERT["level"] = data.get("level", "clear")
                ALERT["ts"] = time.time()
            self._send(200, json.dumps({"ok": True})); return
        if self.path == "/update":
            cs = (data.get("callsign") or "UNKNOWN").upper()
            data["_ts"] = time.time(); data["source"] = "acars"
            with LOCK: LIVE[cs] = data
            self._send(200, json.dumps({"ok": True})); return
        if self.path == "/end":
            cs = (data.get("callsign") or "").upper()
            with LOCK: LIVE.pop(cs, None)
            self._send(200, json.dumps({"ok": True})); return
        self._send(404, json.dumps({"error": "not found"}))
    def log_message(self, *a): pass

def start_bridge():
    socketserver.TCPServer.allow_reuse_address = True
    try:
        httpd = socketserver.TCPServer(("127.0.0.1", PORT), _Handler)
    except OSError:
        return  # already running
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

# ---------- ACARS engine bridge (optional; used by Create Flight "Start ACARS") ----------
# The heavy SimConnect engine lives in acars_client.py. We import it lazily so the
# app still opens even if SimConnect isn't installed (e.g. no MSFS on this PC yet).
def start_acars(callsign, dep, arr, demo=False):
    def run():
        try:
            import acars_client as ac
            sim = ac.Sim(demo=demo)
            overlay = ac.Overlay()
            if not demo:
                ac.run_preflight(sim, overlay)
            ac.run_flight(sim, overlay, callsign, dep, arr)
        except Exception as e:
            # push the error to the dashboard alert box instead of crashing the app
            try:
                import urllib.request
                urllib.request.urlopen(urllib.request.Request(
                    f"http://127.0.0.1:{PORT}/alert",
                    data=json.dumps({"msg": f"ACARS error: {e}", "level": "major"}).encode(),
                    method="POST", headers={"Content-Type": "application/json"}), timeout=1)
            except Exception:
                pass
    threading.Thread(target=run, daemon=True).start()

# ---------- JS <-> Python bridge exposed to the dashboard ----------
class Api:
    def start_flight(self, callsign, dep, arr):
        start_acars(callsign or "ALA000", dep or "----", arr or "----", demo=False)
        return {"ok": True, "mode": "sim"}
    def start_demo(self):
        start_acars("ALA123", "KATL", "KMCO", demo=True)
        return {"ok": True, "mode": "demo"}

# ---------- launch the native window ----------
def main():
    start_bridge()
    time.sleep(0.4)
    try:
        import webview
        webview.create_window(
            "Amal Airways — Flight Management System",
            f"http://127.0.0.1:{PORT}/",
            width=1280, height=820, min_size=(1000, 680),
            js_api=Api())
        webview.start()
    except Exception as e:
        # fallback: if pywebview can't start (rare), open in default browser
        print(f"(Native window unavailable: {e} — opening in browser instead.)")
        import webbrowser
        webbrowser.open(f"http://127.0.0.1:{PORT}/")
        print("Dashboard running at http://127.0.0.1:8770/  — press Ctrl-C to quit.")
        try:
            while True: time.sleep(1)
        except KeyboardInterrupt:
            pass

if __name__ == "__main__":
    main()
