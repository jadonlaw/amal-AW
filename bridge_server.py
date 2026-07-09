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

PORT = int(os.environ.get("PORT", "8770"))
HOST = os.environ.get("HOST", "0.0.0.0")
HERE = os.path.dirname(os.path.abspath(__file__))
HTML = os.path.join(HERE, "AmalAirways.html")

LIVE = {}
ALERT = {"msg": "All clear", "level": "clear", "ts": 0}

import json as _json
STATE_FILE = os.path.join(HERE, "airline_state.json")

def _default_state():
    return {
        "fleet": [],
        "maintenance": [],
        "flights": [],
        "pilots": {},
        "routes": [],
        "schedule": [],
        "new_pilots": [],
    }

CRED_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "CREDENTIALS.txt")

def _role_for(username):
    u = username.upper()
    if u in ("CEO1", "CEO01"): return "ceo"
    if u.startswith("FM"): return "fleet_manager"
    return "pilot"

def load_credentials():
    creds = {}
    try:
        for line in open(CRED_FILE, encoding="utf-8"):
            if ":" in line and "===" not in line:
                left = line.split("->")[0]
                name = ""
                if "->" in line:
                    name = line.split("->", 1)[1].strip()
                u, p = left.split(":", 1)
                u = u.strip().upper(); p = p.strip()
                if u and p:
                    creds[u] = {"pass": p, "role": _role_for(u), "name": name}
    except FileNotFoundError:
        pass
    return creds

CREDS = load_credentials()

def add_credential(username, passcode, name=""):
    u = username.upper()
    CREDS[u] = {"pass": passcode, "role": _role_for(u), "name": name}
    try:
        with open(CRED_FILE, "a", encoding="utf-8") as f:
            f.write(f"\n{u} : {passcode}" + (f"  ->  {name}" if name else ""))
    except Exception as e:
        print("could not append credential:", e)

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

def _parse_sched_time(tstr):
    import datetime, re
    if not tstr: return None
    m = re.search(r'(\d{1,2})(?::(\d{2}))?\s*([AaPp][Mm])?', tstr)
    if not m: return None
    hr = int(m.group(1)); mn = int(m.group(2) or 0)
    ap = (m.group(3) or '').lower()
    if ap=='pm' and hr<12: hr+=12
    if ap=='am' and hr==12: hr=0
    now = datetime.datetime.now()
    sched = now.replace(hour=hr%24, minute=mn, second=0, microsecond=0)
    return sched.timestamp()

def evaluate_schedule(state):
    """Flag claimed routes as Delayed / Missed Flight based on show-up vs scheduled time."""
    now = time.time()
    GRACE_DELAY = 15*60
    GRACE_MISS  = 60*60
    for r in state.get("schedule", []):
        if r.get("status") not in ("Claimed",):
            continue
        st = _parse_sched_time(r.get("time",""))
        if not st:
            continue
        if r.get("showed"):
            continue
        late = now - st
        if late > GRACE_MISS:
            r["status"] = "Missed Flight"
        elif late > GRACE_DELAY:
            r["status"] = "Delayed"
STALE_SECONDS = 30

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
        if self.path == "/airports.js":
            ap = os.path.join(HERE, "airports.js")
            if os.path.exists(ap):
                with open(ap, "rb") as f:
                    self._send(200, f.read(), "application/javascript")
            else:
                self._send(200, "window.AIRPORTS=window.AIRPORTS||{};", "application/javascript")
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
                evaluate_schedule(STATE)
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

                pilot = (data.get("callsign") or "").upper()
                dep = (data.get("dep") or "").upper()
                for r in STATE.get("schedule", []):
                    if r.get("status")=="Claimed" and not r.get("showed"):
                        if (r.get("claimed_by","").upper()==pilot or pilot.startswith(r.get("claimed_by","").upper())) \
                           and r.get("dep","").upper()==dep:
                            r["showed"] = True
                            r["status"] = "Flown"
                save_state(STATE)
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

        if self.path == "/login":
            length = int(self.headers.get("Content-Length", 0))
            try:
                data = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                self._send(400, json.dumps({"error": "bad json"})); return
            u = (data.get("username") or "").strip().upper()
            p = (data.get("passcode") or "").strip()
            acct = CREDS.get(u)
            if not acct or acct["pass"].upper() != p.upper():
                self._send(200, json.dumps({"ok": False})); return
            with LOCK:
                pinfo = STATE["pilots"].get(u, {})
            self._send(200, json.dumps({
                "ok": True, "username": u, "role": acct["role"],
                "name": acct.get("name") or pinfo.get("name") or u,
                "ptype": pinfo.get("ptype", "commercial")}))
            return

        if self.path in ("/buy", "/maintenance", "/flight", "/pilot", "/state", "/route", "/schedule", "/claim", "/accept", "/newpilot", "/rolemade"):
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

                    for a in STATE["fleet"]:
                        if a.get("reg") == data.get("reg"):
                            a["maint"] = 0
                elif self.path == "/flight":

                    data["id"] = STATE.get("_flight_seq", 0) + 1
                    STATE["_flight_seq"] = data["id"]
                    STATE["flights"].append(data)
                    fl_hours = data.get("hours", 0)

                    u = (data.get("pilot") or "").upper()
                    if u:
                        p = STATE["pilots"].setdefault(u, {"name": u, "role": "Pilot",
                             "type": data.get("type"), "flights": 0, "hours": 0})
                        old_hours = p.get("hours", 0)
                        p["flights"] = p.get("flights", 0) + 1
                        p["hours"] = round(old_hours + fl_hours, 1)
                        data["_old_hours"] = old_hours
                        data["_new_hours"] = p["hours"]
                        data["_pilot_name"] = p.get("name", u)

                    reg = data.get("reg") or data.get("aircraft_reg") or ""
                    if reg:
                        for a in STATE["fleet"]:
                            if a.get("reg")==reg:
                                a["hours"] = round(a.get("hours",0) + fl_hours, 1)
                                a["maint"] = round(a.get("maint",0) + fl_hours, 1)

                                if a["maint"] >= 100:
                                    a["status"] = "Grounded — maintenance"
                                break
                elif self.path == "/pilot":
                    u = (data.get("username") or "").upper()
                    if u:
                        STATE["pilots"][u] = {**STATE["pilots"].get(u, {}), **data}
                elif self.path == "/newpilot":

                    u = (data.get("username") or "").upper()
                    if u:
                        STATE["pilots"][u] = {**STATE["pilots"].get(u, {}),
                            "name": data.get("name", u), "role": "Pilot",
                            "ptype": data.get("ptype", "commercial"),
                            "flights": STATE["pilots"].get(u, {}).get("flights", 0),
                            "hours": STATE["pilots"].get(u, {}).get("hours", 0)}

                        pc = data.get("passcode")
                        if pc and u not in CREDS:
                            add_credential(u, pc, data.get("name", u))
                        if not any(q.get("username")==u for q in STATE["new_pilots"]):
                            STATE["new_pilots"].append({"username": u,
                                "name": data.get("name", u), "made": False, "ts": time.time()})
                elif self.path == "/rolemade":

                    u = (data.get("username") or "").upper()
                    for q in STATE["new_pilots"]:
                        if q.get("username")==u:
                            q["made"] = True
                            break
                elif self.path == "/accept":

                    u = (data.get("username") or "").upper()
                    if u:
                        p = STATE["pilots"].setdefault(u, {"name":u,"role":"Pilot","flights":0,"hours":0})
                        p["accepted_tos"] = True
                elif self.path == "/route":
                    dep = (data.get("dep") or "").upper()
                    arr = (data.get("arr") or "").upper()
                    if len(dep)==4 and len(arr)==4:

                        exists = any(r.get("dep")==dep and r.get("arr")==arr for r in STATE["routes"])
                        if not exists:
                            STATE["routes"].insert(0, {"dep":dep, "arr":arr,
                                "by": data.get("by",""), "ts": time.time(),
                                "f": "KLA"+str(int(time.time()))[-3:]})
                elif self.path == "/schedule":

                    dep=(data.get("dep") or "").upper(); arr=(data.get("arr") or "").upper()
                    if len(dep)==4 and len(arr)==4:
                        sid = STATE.get("_sched_seq",0)+1; STATE["_sched_seq"]=sid
                        STATE["schedule"].insert(0, {
                            "id": sid, "dep": dep, "arr": arr,
                            "ac": data.get("ac",""), "cls": data.get("cls","commercial"),
                            "time": data.get("time",""), "claimed_by": "", "claimed_name":"",
                            "status": "Open"})
                elif self.path == "/claim":

                    sid = data.get("id")
                    for r in STATE["schedule"]:
                        if r.get("id")==sid:
                            if data.get("remove"):
                                STATE["schedule"] = [x for x in STATE["schedule"] if x.get("id")!=sid]
                            else:
                                r["claimed_by"]=data.get("by",""); r["claimed_name"]=data.get("name","")
                                r["status"]="Claimed"
                                r["claimed_ts"]=time.time()
                                r["showed"]=False
                            break
                elif self.path == "/state":

                    for k in ("fleet", "maintenance", "flights", "pilots"):
                        if k in data:
                            STATE[k] = data[k]
                save_state(STATE)
            self._send(200, json.dumps({"ok": True}))
            return

        self._send(404, json.dumps({"error": "not found"}))

    def log_message(self, *a):
        pass

def serve():
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer((HOST, PORT), Handler) as httpd:
        print(f"Bridge server running at http://localhost:{PORT}")
        print(f"Dashboard:  http://localhost:{PORT}/")
        print(f"Live feed:  http://localhost:{PORT}/live")
        httpd.serve_forever()

if __name__ == "__main__":
    serve()
