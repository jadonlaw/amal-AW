#!/usr/bin/env python3
"""
Amal Airways — ACARS Client (MSFS / SimConnect)
================================================
One program that, while you fly, does all of this:

  1. SIM-VERIFIED PREFLIGHT CHECKLIST — engines/lights/brake/flaps must actually
     be set in the sim before the flight is armed. No honor system.
  2. SOP VIOLATION ENGINE — watches the aircraft continuously; the instant you
     break a rule it fires a siren + on-screen banner so you can fix it.
  3. LANDING CAPTURE — records touchdown FPM (+ bounce) and grades it by tier.
  4. FILES THE FLIGHT — writes the PIREP + every violation to vaops.db, the same
     database the dashboard reads.

RUN (on your flying PC, MSFS running):
    pip install SimConnect
    python acars_client.py --callsign ALA123 --dep KATL --arr KMCO

TEST WITHOUT MSFS (proves the logic, no sim needed):
    python acars_client.py --demo

Edit RULES and CHECKLIST below to tune your airline's procedures.
"""

import sqlite3, time, sys, math, argparse, datetime, os, json

DB = "vaops.db"
TICK = 1.0  # seconds between sim reads

# ----------------------------------------------------------------------
# LANDING TIERS  (locked earlier — narrow/regional vs wide-body)
# ----------------------------------------------------------------------
WIDE_KEYS = ["777", "787", "a350", "a330", "a340", "747", "767", "md-11", "a380"]
TIERS_NARROW = [(60,"Butter"),(180,"Smooth"),(300,"Firm"),(600,"Hard"),(10**9,"Unsafe")]
TIERS_WIDE   = [(60,"Butter"),(200,"Smooth"),(360,"Firm"),(480,"Hard"),(10**9,"Unsafe")]

def is_wide(title):
    t = (title or "").lower()
    return any(k in t for k in WIDE_KEYS)

def classify_landing(fpm, title):
    tiers = TIERS_WIDE if is_wide(title) else TIERS_NARROW
    a = abs(fpm)
    for limit, name in tiers:
        if a <= limit:
            return name
    return "Unsafe"

def landing_score(fpm, title):
    """0-100. Rewards firm-but-controlled; penalizes hard AND excessive float."""
    a = abs(fpm)
    if a < 5:   return 70          # suspiciously soft = probably floated
    if a <= 60: return 95
    if a <= 180: return 100
    if a <= 300: return 88
    if a <= 600: return 62
    return 35

# ----------------------------------------------------------------------
# PREFLIGHT CHECKLIST  — each item verified from the sim state dict.
# (name, function(state)->bool, applies(state)->bool)
# applies() lets an item skip on aircraft that don't have it (e.g. no APU).
# ----------------------------------------------------------------------
def has_apu(s):   return not is_light_ga(s)
def is_light_ga(s): return "cessna" in (s.get("title","").lower()) or "172" in (s.get("title","").lower())

CHECKLIST = [
    ("Parking brake SET",        lambda s: s["park_brake"] > 0.5,                 lambda s: True),
    ("Beacon light ON",          lambda s: s["beacon"] == 1,                      lambda s: True),
    ("Battery / avionics ON",    lambda s: s["battery"] == 1,                     lambda s: True),
    ("Engines RUNNING",          lambda s: s["eng1"] or s["eng2"],               lambda s: True),
    ("Flaps SET for takeoff",    lambda s: s["flaps_idx"] >= 1,                   lambda s: True),
    ("Taxi / Nav lights ON",     lambda s: s["nav_light"] == 1,                   lambda s: True),
    ("Transponder SET (>1200)",  lambda s: s["xpdr"] and s["xpdr"] != 1200,       lambda s: True),
]

# ----------------------------------------------------------------------
# SOP VIOLATION RULES  — checked every tick while flying.
# (id, description, severity, condition(state)->True means VIOLATING)
# Add/remove freely. Severity: 'minor' | 'major' | 'critical'
# ----------------------------------------------------------------------
RULES = [
    ("LIGHTS_LANDING_HI", "Landing lights OFF... wait, ON above 10,000 ft (turn off)",
        "minor",   lambda s: s["landing_light"]==1 and s["alt"]>10000 and not s["on_ground"]),
    ("LIGHTS_LANDING_LO", "Landing lights OFF below 10,000 ft (should be ON)",
        "major",   lambda s: s["landing_light"]==0 and s["alt"]<10000 and not s["on_ground"]),
    ("STROBES_AIR",       "Strobe lights OFF while airborne",
        "minor",   lambda s: s["strobe"]==0 and not s["on_ground"]),
    ("BEACON_ENGINE",     "Engine running with beacon OFF",
        "major",   lambda s: (s["eng1"] or s["eng2"]) and s["beacon"]==0),
    ("OVERSPEED_250",     "Over 250 kt below 10,000 ft",
        "major",   lambda s: s["ias"]>250 and s["alt"]<10000 and not s["on_ground"]),
    ("TAXI_SPEED",        "Taxi speed over 30 kt on the ground",
        "minor",   lambda s: s["on_ground"] and 30 < s["gs"] <= 40 and (s["eng1"] or s["eng2"])),
    ("GEAR_HINT",         "Below 1,000 ft AGL fast with high sink (unstable approach)",
        "critical",lambda s: not s["on_ground"] and s["alt_agl"]<1000 and s["vs"]<-1200),
]

SEV_ORDER = {"minor":1, "major":2, "critical":3}

# ----------------------------------------------------------------------
# SIREN + OVERLAY  (Windows audio via winsound; overlay via Tkinter)
# Both are optional — the client still works headless (prints warnings).
# ----------------------------------------------------------------------
def siren(level):
    try:
        import winsound
        if level == "critical":
            for _ in range(2): winsound.Beep(880,180); winsound.Beep(440,180)
        elif level == "major":
            winsound.Beep(760,160); winsound.Beep(520,160)
        else:
            winsound.Beep(600,120)
    except Exception:
        sys.stdout.write("\a"); sys.stdout.flush()   # terminal bell fallback

class Overlay:
    """Always-on-top red banner over the sim. Falls back to console if no GUI."""
    def __init__(self):
        self.ok = False
        try:
            import tkinter as tk
            self.tk = tk
            self.root = tk.Tk()
            self.root.overrideredirect(True)
            self.root.attributes("-topmost", True)
            try: self.root.attributes("-alpha", 0.92)
            except Exception: pass
            w = self.root.winfo_screenwidth()
            self.root.geometry(f"{w}x64+0+0")
            self.lbl = tk.Label(self.root, text="", font=("Segoe UI", 20, "bold"),
                                fg="white", bg="#b00020")
            self.lbl.pack(fill="both", expand=True)
            self.root.withdraw()
            self.ok = True
        except Exception:
            self.ok = False
    def warn(self, text, level="major"):
        color = {"minor":"#c9a54a","major":"#b00020","critical":"#7a0016"}.get(level,"#b00020")
        if self.ok:
            self.lbl.config(text="  ⚠  "+text, bg=color)
            self.root.deiconify(); self.root.update()
        else:
            print(f"[WARN/{level}] {text}")
    def clear(self):
        if self.ok:
            self.root.withdraw(); self.root.update()
    def pump(self):
        if self.ok:
            try: self.root.update()
            except Exception: pass

# ----------------------------------------------------------------------
# SIMCONNECT  — reads MSFS. In --demo mode a scripted flight is generated.
# ----------------------------------------------------------------------
class Sim:
    def __init__(self, demo=False):
        self.demo = demo
        self.t = 0
        if not demo:
            from SimConnect import SimConnect, AircraftRequests
            self.sm = SimConnect()
            self.aq = AircraftRequests(self.sm, _time=0)

    def _q(self, name, default=0):
        v = self.aq.get(name)
        return default if v is None else v

    def state(self):
        if self.demo:
            return self._demo_state()
        title = self._q("TITLE","") or ""
        alt = self._q("PLANE_ALTITUDE")
        return {
            "title": title,
            "alt": alt,
            "alt_agl": self._q("PLANE_ALT_ABOVE_GROUND", alt),
            "ias": self._q("AIRSPEED_INDICATED"),
            "gs": self._q("GROUND_VELOCITY"),
            "vs": self._q("VERTICAL_SPEED")*60 if abs(self._q("VERTICAL_SPEED"))<200 else self._q("VERTICAL_SPEED"),
            "on_ground": int(self._q("SIM_ON_GROUND")) == 1,
            "beacon": int(self._q("LIGHT_BEACON")),
            "landing_light": int(self._q("LIGHT_LANDING")),
            "taxi_light": int(self._q("LIGHT_TAXI")),
            "nav_light": int(self._q("LIGHT_NAV")),
            "strobe": int(self._q("LIGHT_STROBE")),
            "park_brake": self._q("BRAKE_PARKING_POSITION"),
            "battery": int(self._q("ELECTRICAL_MASTER_BATTERY")),
            "eng1": int(self._q("GENERAL_ENG_COMBUSTION:1")),
            "eng2": int(self._q("GENERAL_ENG_COMBUSTION:2")),
            "flaps_idx": int(self._q("FLAPS_HANDLE_INDEX")),
            "xpdr": int(self._q("TRANSPONDER_CODE:1", 1200)),
            "heading": self._q("PLANE_HEADING_DEGREES_TRUE", 0),
            "slew": int(self._q("IS_SLEW_ACTIVE", 0)),
            "lat": self._q("PLANE_LATITUDE"),
            "lon": self._q("PLANE_LONGITUDE"),
        }

    def _demo_state(self):
        """Scripted: preflight -> takeoff -> climb -> cruise -> descent -> land."""
        self.t += 1
        # drift the aircraft KATL -> KMCO so it visibly moves on the map
        lat = 33.63 - (self.t * 0.16)
        lon = -84.42 + (self.t * 0.045)
        s = dict(title="Boeing 737-800", taxi_light=1, nav_light=1, battery=1,
                 eng1=1, eng2=1, beacon=1, park_brake=0, flaps_idx=2, xpdr=2000,
                 strobe=1, landing_light=1, lat=lat, lon=lon, heading=150, slew=0)
        t = self.t
        if t < 6:      # preflight on ground, brake set, some items not yet done
            s.update(park_brake=1, flaps_idx=0, strobe=0, landing_light=0, xpdr=1200,
                     alt=1026, alt_agl=0, ias=0, gs=0, vs=0, on_ground=True)
            if t>=3: s["flaps_idx"]=2          # pilot sets flaps at t=3
            if t>=4: s["xpdr"]=2000            # sets squawk
            if t>=5: s["strobe"]=1; s["landing_light"]=1
        elif t < 10:   # takeoff roll
            s.update(alt=1050, alt_agl=20, ias=120, gs=125, vs=200, on_ground=(t<8))
        elif t < 16:   # climb — inject a violation at t=12: landing lights off low? no, test overspeed
            s.update(alt=8000, alt_agl=7000, ias=260 if t==13 else 240, vs=2200, on_ground=False)
        elif t < 22:   # cruise
            s.update(alt=36000, alt_agl=35000, ias=280, vs=0, on_ground=False, landing_light=0)
            if t in (19,20,21): s["slew"]=1   # demo: slew 3 ticks -> warn then VOID
        elif t < 28:   # descent, unstable spike at t=25
            s.update(alt=900, alt_agl=800, ias=180, vs=(-1500 if t==25 else -700),
                     on_ground=False, landing_light=1)
        else:          # touchdown
            s.update(alt=1030, alt_agl=0, ias=130, gs=128,
                     vs=(-410 if t==28 else -30), on_ground=True)
        return s

# ----------------------------------------------------------------------
# DB — write PIREP + violations into vaops.db
# ----------------------------------------------------------------------
def ensure_db():
    c = sqlite3.connect(DB); c.execute("""
      CREATE TABLE IF NOT EXISTS client_pireps(
        id INTEGER PRIMARY KEY AUTOINCREMENT, callsign TEXT, dep TEXT, arr TEXT,
        aircraft TEXT, block_min REAL, landing_fpm INTEGER, landing_tier TEXT,
        score INTEGER, violations INTEGER, filed_at TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS client_violations(
        id INTEGER PRIMARY KEY AUTOINCREMENT, callsign TEXT, rule TEXT,
        description TEXT, severity TEXT, at TEXT)""")
    c.commit(); return c

def now(): return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")

def haversine_nm(lat1, lon1, lat2, lon2):
    try:
        R = 3440.065
        p1, p2 = math.radians(lat1), math.radians(lat2)
        dp = math.radians(lat2-lat1); dl = math.radians(lon2-lon1)
        a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
        return 2*R*math.asin(math.sqrt(a))
    except Exception:
        return 0.0

# ---- push live telemetry to the bridge server so the dashboard can draw it ----
BRIDGE = os.environ.get("BRIDGE_URL", "http://localhost:8770")
def bridge_post(path, payload):
    try:
        import urllib.request
        req = urllib.request.Request(BRIDGE + path,
              data=json.dumps(payload).encode(), method="POST",
              headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=1)
    except Exception:
        pass  # bridge not running is fine; flight still tracks locally

# ----------------------------------------------------------------------
# PREFLIGHT GATE
# ----------------------------------------------------------------------
def run_preflight(sim, overlay):
    print("\n=== PREFLIGHT CHECKLIST — complete all items in the sim to arm the flight ===")
    done = set()
    while len(done) < len([i for i in CHECKLIST]):
        s = sim.state()
        newly = []
        for name, check, applies in CHECKLIST:
            if name in done: continue
            if not applies(s): done.add(name); continue
            try:
                if check(s):
                    done.add(name); newly.append(name)
            except Exception:
                pass
        # render
        os.system("cls" if os.name=="nt" else "clear")
        print("=== PREFLIGHT CHECKLIST ===")
        for name, check, applies in CHECKLIST:
            mark = "✓" if name in done else "…"
            print(f"  [{mark}] {name}")
        overlay.pump()
        if len(done) >= len(CHECKLIST):
            break
        time.sleep(TICK)
    print("\n✓ Preflight complete — flight ARMED. Cleared to fly.\n")

# ----------------------------------------------------------------------
# MAIN FLIGHT LOOP
# ----------------------------------------------------------------------
def run_flight(sim, overlay, callsign, dep, arr):
    c = ensure_db()
    start = time.time()
    prev_on_ground = True
    took_off = False
    touchdown_fpm = None
    min_vs_near_ground = 0
    active_breaches = {}   # rule_id -> first seen ts (debounce)
    logged = []            # (rule_id, desc, sev, ts)
    title = sim.state().get("title","")
    # --- slew tracking (warn first -> zero if sustained/repeated) ---
    slew_ticks = 0         # consecutive ticks slewing
    slew_events = 0        # separate occurrences
    slew_flagged = False   # True once the flight is zeroed for slew
    prev_slew = False
    prev_lat = prev_lon = None

    print("=== FLIGHT ACTIVE ===  (Ctrl-C to end)\n")
    try:
        while True:
            s = sim.state()
            title = s.get("title", title)

            # --- violation checks ---
            worst = None
            for rid, desc, sev, cond in RULES:
                try: violating = cond(s)
                except Exception: violating = False
                if violating:
                    if rid not in active_breaches:
                        active_breaches[rid] = now()
                        logged.append((rid, desc, sev, now()))
                        if worst is None or SEV_ORDER[sev] > SEV_ORDER[worst[2]]:
                            worst = (rid, desc, sev)
                else:
                    active_breaches.pop(rid, None)
            if worst:
                siren(worst[2]); overlay.warn(worst[1], worst[2])
                bridge_post("/alert", {"msg": worst[1], "level": worst[2]})
            else:
                overlay.clear()
                bridge_post("/alert", {"msg": "All clear", "level": "clear"})
            overlay.pump()

            # --- SLEW DETECTION: simvar + impossible-physics cross-check ---
            slewing = bool(s.get("slew"))
            # physics cross-check: position jump implying absurd groundspeed
            if prev_lat is not None and s.get("lat") is not None:
                jump_nm = haversine_nm(prev_lat, prev_lon, s["lat"], s["lon"])
                # >10 nm in one ~1s tick == >36,000 kt: not real flight
                if jump_nm > 10 and not slewing:
                    slewing = True
            prev_lat, prev_lon = s.get("lat"), s.get("lon")

            if slewing:
                slew_ticks += 1
                if not prev_slew:
                    slew_events += 1
                if slew_ticks == 1 and not slew_flagged:
                    # STEP 1: warn first — no penalty yet
                    siren("critical"); overlay.warn("SLEW DETECTED — exit slew now", "critical")
                    bridge_post("/alert", {"msg": "SLEW DETECTED — exit slew now", "level": "critical"})
                elif (slew_ticks >= 3 or slew_events >= 2) and not slew_flagged:
                    # STEP 2: sustained or repeated -> zero the flight, stamp it
                    slew_flagged = True
                    logged.append(("SLEW", "Slew mode used in flight (score voided)", "critical", now()))
                    siren("critical"); overlay.warn("FLIGHT VOIDED — SLEW", "critical")
                    bridge_post("/alert", {"msg": "FLIGHT VOIDED — SLEW USED", "level": "critical"})
            else:
                slew_ticks = 0
            prev_slew = slewing

            # --- push live position to the dashboard bridge ---
            bridge_post("/update", {
                "callsign": callsign, "dep": dep, "arr": arr,
                "lat": s.get("lat"), "lon": s.get("lon"),
                "altitude": int(s.get("alt", 0)), "groundspeed": int(s.get("gs", 0)),
                "heading": int(s.get("heading", 0)) if s.get("heading") else 0,
                "aircraft": title, "on_ground": s.get("on_ground", True),
                "vs": int(s.get("vs", 0))
            })

            # --- takeoff / landing detection ---
            if not s["on_ground"]: took_off = True
            if not s["on_ground"] and s["alt_agl"] < 200:
                min_vs_near_ground = min(min_vs_near_ground, s["vs"])
            # touchdown = airborne -> on ground after having taken off
            if took_off and s["on_ground"] and not prev_on_ground:
                touchdown_fpm = int(round(min_vs_near_ground if min_vs_near_ground<0 else s["vs"]))
                tier = classify_landing(touchdown_fpm, title)
                print(f"\n🛬 TOUCHDOWN: {touchdown_fpm} fpm — {tier}")
                if tier in ("Hard","Unsafe"):
                    siren("critical"); overlay.warn(f"{tier} landing: {touchdown_fpm} fpm", "critical")
                break
            prev_on_ground = s["on_ground"]
            time.sleep(TICK)
    except KeyboardInterrupt:
        print("\nFlight ended by pilot.")

    block_min = round((time.time()-start)/60, 1)
    fpm = touchdown_fpm if touchdown_fpm is not None else 0
    tier = classify_landing(fpm, title)
    score = landing_score(fpm, title)
    # apply a per-violation penalty to the flight score
    penalty = sum({"minor":3,"major":8,"critical":20}[v[2]] for v in logged)
    final = max(0, score - penalty)
    # SLEW: void the flight entirely — score 0, flagged for Fleet Manager to strike
    if slew_flagged:
        final = 0
        tier = "SLEW VOID"

    c.execute("""INSERT INTO client_pireps(callsign,dep,arr,aircraft,block_min,
        landing_fpm,landing_tier,score,violations,filed_at)
        VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (callsign, dep, arr, title, block_min, fpm, tier, final, len(logged), now()))
    for rid, desc, sev, ts in logged:
        c.execute("""INSERT INTO client_violations(callsign,rule,description,severity,at)
            VALUES(?,?,?,?,?)""", (callsign, rid, desc, sev, ts))
    c.commit()
    bridge_post("/end", {"callsign": callsign})
    # persist the completed flight so it shows in history + rolls pilot totals
    bridge_post("/flight", {
        "pilot": callsign, "dep": dep, "arr": arr, "aircraft": title,
        "hours": round(block_min/60, 2), "landing_fpm": fpm, "tier": tier,
        "score": final, "violations": len(logged), "filed_at": now()
    })

    print("\n================= FLIGHT SUMMARY =================")
    print(f"  {callsign}  {dep} -> {arr}  ({title})")
    print(f"  Block time : {block_min} min")
    print(f"  Landing    : {fpm} fpm  [{tier}]")
    print(f"  Base score : {score}   Penalty: -{penalty}   FINAL: {final}")
    if slew_flagged:
        print(f"  *** SLEW DETECTED — flight VOIDED (score 0). Flagged for Fleet Manager review to apply Strike One. ***")
    print(f"  Violations : {len(logged)}")
    for rid, desc, sev, ts in logged:
        print(f"     - [{sev.upper()}] {desc}")
    print("=================================================")
    print("Filed to vaops.db — results are recorded to the airline database.")

# ----------------------------------------------------------------------
def open_dashboard():
    """Start the bridge server (serves dashboard + live feed) and open it."""
    try:
        import webbrowser, subprocess, urllib.request
        here = os.path.dirname(os.path.abspath(__file__))
        bridge = os.path.join(here, "bridge_server.py")
        url = "http://localhost:8770/"
        # is the bridge already up?
        up = False
        try:
            urllib.request.urlopen(url, timeout=1); up = True
        except Exception:
            up = False
        if not up and os.path.exists(bridge):
            subprocess.Popen([sys.executable, bridge],
                             cwd=here, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(1.5)  # give it a moment to bind the port
        webbrowser.open(url)
        print(f"Dashboard opening at {url}")
    except Exception as e:
        print(f"(Could not open dashboard: {e})")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--callsign", default="ALA000")
    ap.add_argument("--dep", default="----")
    ap.add_argument("--arr", default="----")
    ap.add_argument("--demo", action="store_true", help="run scripted flight, no MSFS needed")
    ap.add_argument("--skip-preflight", action="store_true")
    ap.add_argument("--no-dashboard", action="store_true", help="don't auto-open the HTML")
    args = ap.parse_args()

    if not args.no_dashboard:
        open_dashboard()

    sim = Sim(demo=args.demo)
    overlay = Overlay()
    if not args.demo and not args.skip_preflight:
        run_preflight(sim, overlay)
    run_flight(sim, overlay, args.callsign, args.dep, args.arr)

if __name__ == "__main__":
    main()
