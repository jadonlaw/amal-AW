#!/usr/bin/env python3
"""
Amal Airways — Flight Management System

Run (dev):   pip install pywebview  &&  python AmalAirways.py
Build .exe:  python -m PyInstaller --onefile --windowed --name AmalAirways --collect-all SimConnect --add-data "AmalAirways.html;." --add-data "acars_client.py;." --add-data "airports.js;." AmalAirways.py
See BUILD_DESKTOP_APP.txt for the installer steps.
"""

import os, sys, threading, time, json

def resource_path(name):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)

HTML_PATH = resource_path("AmalAirways.html")

import http.server, socketserver

PORT = 8770
LIVE = {}

AMAZON_BRIDGE = os.environ.get("AMAZON_BRIDGE", "http://52.3.240.140")

def relay_to_amazon(path, payload):
    """Forward a local update up to the hosted bridge so the whole airline sees it."""
    def _go():
        try:
            import urllib.request
            urllib.request.urlopen(urllib.request.Request(
                AMAZON_BRIDGE + path, data=json.dumps(payload).encode(),
                method="POST", headers={"Content-Type": "application/json"}), timeout=8)
        except Exception:
            pass
    threading.Thread(target=_go, daemon=True).start()
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
        if self.path == "/airports.js":
            ap = resource_path("airports.js")
            if os.path.exists(ap):
                with open(ap, "rb") as f: self._send(200, f.read(), "application/javascript")
            else:
                self._send(200, "window.AIRPORTS=window.AIRPORTS||{};", "application/javascript")
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
            relay_to_amazon("/update", data)
            self._send(200, json.dumps({"ok": True})); return
        if self.path == "/end":
            cs = (data.get("callsign") or "").upper()
            with LOCK: LIVE.pop(cs, None)
            relay_to_amazon("/end", {"callsign": cs})
            self._send(200, json.dumps({"ok": True})); return
        self._send(404, json.dumps({"error": "not found"}))
    def log_message(self, *a): pass

def start_bridge():
    socketserver.TCPServer.allow_reuse_address = True
    try:
        httpd = socketserver.TCPServer(("127.0.0.1", PORT), _Handler)
    except OSError:
        return
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

def start_acars(callsign, dep, arr, demo=False):

    os.environ["BRIDGE_URL"] = AMAZON_BRIDGE
    def run():
        try:
            import acars_client as ac
            sim = ac.Sim(demo=demo)
            overlay = ac.Overlay()
            if not demo:
                ac.run_preflight(sim, overlay)
            ac.run_flight(sim, overlay, callsign, dep, arr)
        except Exception as e:

            try:
                import urllib.request
                urllib.request.urlopen(urllib.request.Request(
                    f"http://127.0.0.1:{PORT}/alert",
                    data=json.dumps({"msg": f"ACARS error: {e}", "level": "major"}).encode(),
                    method="POST", headers={"Content-Type": "application/json"}), timeout=1)
            except Exception:
                pass
    threading.Thread(target=run, daemon=True).start()

class Api:
    def start_flight(self, callsign, dep, arr):
        start_acars(callsign or "KLA000", dep or "----", arr or "----", demo=False)
        return {"ok": True, "mode": "sim"}
    def start_demo(self):
        start_acars("KLA123", "KATL", "KMCO", demo=True)
        return {"ok": True, "mode": "demo"}

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
