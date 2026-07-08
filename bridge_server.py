#!/usr/bin/env python3
"""
Amal Airways — Bridge Server
============================
The glue between the ACARS client and the HTML dashboard.

- The ACARS client POSTs live position/telemetry here every second.
- The dashboard fetches from here to draw YOUR flight on the map,
  whether you're on VATSIM or flying fully offline.
- Also serves the dashboard itself, so one command runs everything.

RUN:
    python bridge_server.py
Then open http://localhost:8770  (the client opens it for you automatically).

No dependencies — pure Python standard library.
"""

import http.server, socketserver, json, os, threading, time

PORT = int(os.environ.get("PORT", "8770"))   # hosts set PORT; local default 8770
HOST = os.environ.get("HOST", "0.0.0.0")      # 0.0.0.0 so it's reachable when hosted
HERE = os.path.dirname(os.path.abspath(__file__))
HTML = os.path.join(HERE, "AmalAirways.html")

# in-memory store of live flights: callsign -> telemetry dict
LIVE = {}
ALERT = {"msg": "All clear", "level": "clear", "ts": 0}  # latest SOP alert for the dashboard

# ---- persistent airline state (saved to disk, shared by everyone) ----
import json as _json
STATE_FILE = os.path.join(HERE, "airline_state.json")

def _default_state():
    return {
        "fleet": [],          # aircraft owned: reg, type, loc, status, hours, maint
        "maintenance": [],    # maintenance log entries
        "flights": [],        # completed flight history (PIREPs)
        "pilots": {},         # username -> {name, role, type, flights, hours}
        "routes": [],         # live routes anyone can fly: {dep, arr, by, ts}
    }

def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            s = _json.load(f)
        for k, v in _default_state().items():
            s.setdefault(k, v)
        return s
    except Exception:
        return _default_state()

def save_state(s):
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            _json.dump(s, f, indent=2)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        print("save_state error:", e)

STATE = load_state()
LOCK = threading.Lock()
STALE_SECONDS = 30   # drop a flight if no update in this long


def prune():
    now = time.time()
    with LOCK:
        for cs in [k for k, v in LIVE.items() if now - v.get("_ts", 0) > STALE_SECONDS]:
            del LIVE[cs]


class Handler(http.server.SimpleHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        if isinstance(body, str):
            body = body.encode()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self._send(204, b"")

    def do_GET(self):
        if self.path in ("/", "/index.html", "/dashboard"):
            if os.path.exists(HTML):
                with open(HTML, "rb") as f:
                    self._send(200, f.read(), "text/html")
            else:
                self._send(404, "AmalAirways.html not found next to bridge_server.py")
            return
        if self.path == "/live":
            prune()
            with LOCK:
                self._send(200, json.dumps({"flights": list(LIVE.values())}))
            return
        if self.path == "/alerts":
            with LOCK:
                self._send(200, json.dumps(ALERT))
            return
        if self.path == "/state":
            with LOCK:
                self._send(200, json.dumps(STATE))
            return
        self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        if self.path == "/alert":
            length = int(self.headers.get("Content-Length", 0))
            try:
                data = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                self._send(400, json.dumps({"error": "bad json"})); return
            with LOCK:
                ALERT["msg"] = data.get("msg", "All clear")
                ALERT["level"] = data.get("level", "clear")
                ALERT["ts"] = time.time()
            self._send(200, json.dumps({"ok": True}))
            return
        if self.path == "/update":
            length = int(self.headers.get("Content-Length", 0))
            try:
                data = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                self._send(400, json.dumps({"error": "bad json"})); return
            cs = (data.get("callsign") or "UNKNOWN").upper()
            data["_ts"] = time.time()
            data["source"] = "acars"
            with LOCK:
                LIVE[cs] = data
            self._send(200, json.dumps({"ok": True, "tracking": cs}))
            return
        if self.path == "/end":
            length = int(self.headers.get("Content-Length", 0))
            try:
                data = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                data = {}
            cs = (data.get("callsign") or "").upper()
            with LOCK:
                LIVE.pop(cs, None)
            self._send(200, json.dumps({"ok": True, "ended": cs}))
            return

        # ---- persistent airline state saving ----
        if self.path in ("/buy", "/maintenance", "/flight", "/pilot", "/state", "/route"):
            length = int(self.headers.get("Content-Length", 0))
            try:
                data = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                self._send(400, json.dumps({"error": "bad json"})); return
            with LOCK:
                if self.path == "/buy":
                    STATE["fleet"].append(data)
                elif self.path == "/maintenance":
                    STATE["maintenance"].append(data)
                    # update the aircraft's hours_since_maint if reg matches
                    for a in STATE["fleet"]:
                        if a.get("reg") == data.get("reg"):
                            a["maint"] = 0
                elif self.path == "/flight":
                    # tag with a sequential id so the bot can see what's new
                    data["id"] = STATE.get("_flight_seq", 0) + 1
                    STATE["_flight_seq"] = data["id"]
                    STATE["flights"].append(data)
                    # roll the pilot's totals + detect rank-up
                    u = (data.get("pilot") or "").upper()
                    if u:
                        p = STATE["pilots"].setdefault(u, {"name": u, "role": "Pilot",
                             "type": data.get("type"), "flights": 0, "hours": 0})
                        old_hours = p.get("hours", 0)
                        p["flights"] = p.get("flights", 0) + 1
                        p["hours"] = round(old_hours + data.get("hours", 0), 1)
                        data["_old_hours"] = old_hours
                        data["_new_hours"] = p["hours"]
                        data["_pilot_name"] = p.get("name", u)
                elif self.path == "/pilot":
                    u = (data.get("username") or "").upper()
                    if u:
                        STATE["pilots"][u] = {**STATE["pilots"].get(u, {}), **data}
                elif self.path == "/route":
                    dep = (data.get("dep") or "").upper()
                    arr = (data.get("arr") or "").upper()
                    if len(dep)==4 and len(arr)==4:
                        # avoid duplicate routes
                        exists = any(r.get("dep")==dep and r.get("arr")==arr for r in STATE["routes"])
                        if not exists:
                            STATE["routes"].insert(0, {"dep":dep, "arr":arr,
                                "by": data.get("by",""), "ts": time.time(),
                                "f": "KLA"+str(int(time.time()))[-3:]})
                elif self.path == "/state":
                    # full overwrite (used to seed the roster)
                    for k in ("fleet", "maintenance", "flights", "pilots"):
                        if k in data:
                            STATE[k] = data[k]
                save_state(STATE)
            self._send(200, json.dumps({"ok": True}))
            return

        self._send(404, json.dumps({"error": "not found"}))

    def log_message(self, *a):
        pass  # quiet


def serve():
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer((HOST, PORT), Handler) as httpd:
        print(f"Bridge server running at http://localhost:{PORT}")
        print(f"Dashboard:  http://localhost:{PORT}/")
        print(f"Live feed:  http://localhost:{PORT}/live")
        httpd.serve_forever()


if __name__ == "__main__":
    serve()
