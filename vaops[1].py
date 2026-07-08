#!/usr/bin/env python3
"""
vaops.py — Virtual Airline Ops + ACARS core (VATSIM-driven)
Modeled on FlightLinq's flow: track -> PIREP -> finance -> fleet.

Run:
    python3 vaops.py setup        # one-time: create DB, set airline config
    python3 vaops.py track        # start the live VATSIM tracker loop
    python3 vaops.py status       # airline balance + active flights
    python3 vaops.py pireps       # recent completed flights
    python3 vaops.py fleet        # aircraft list + maintenance
    python3 vaops.py roster       # pilots + leaderboard

Config lives in vaops.db (SQLite). Edit AIRLINE defaults below or use `setup`.
The Discord bot and the SimConnect touchdown-precision module are separate
files that read/write this same DB — this is the brain they plug into.
"""

import sqlite3, json, time, math, sys, urllib.request
from datetime import datetime, timezone

DB = "vaops.db"
VATSIM_URL = "https://data.vatsim.net/v3/vatsim-data.json"
POLL_SECONDS = 20          # feed refreshes ~every 15s
UA = "vaops/1.0 (VA ops tracker)"

# ---- defaults, overridable via `setup` ----
AIRLINE = {
    "name": "My Virtual Airline",
    "icao": "XAX",          # callsign prefix, e.g. XAX123 -> matches your pilots
    "start_balance": 500000.0,
    "revenue_per_nm": 12.0,     # pax/cargo yield per nautical mile
    "fuel_cost_per_nm": 3.2,    # burn * fuel price, approximated per nm
    "crew_cost_per_hr": 220.0,
    "maint_cost_per_hr": 140.0,
    "maint_interval_hrs": 100.0,  # aircraft needs check after this many hrs
}

# ------------------------- DB -------------------------
def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    c = db()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS config(key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE IF NOT EXISTS aircraft(
        reg TEXT PRIMARY KEY, type TEXT, hours REAL DEFAULT 0,
        location TEXT, status TEXT DEFAULT 'available',
        hours_since_maint REAL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS pilots(
        cid TEXT PRIMARY KEY, name TEXT, flights INTEGER DEFAULT 0,
        hours REAL DEFAULT 0, earnings REAL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS active(
        callsign TEXT PRIMARY KEY, cid TEXT, dep TEXT, arr TEXT, actype TEXT,
        phase TEXT, spawn_lat REAL, spawn_lon REAL, dep_time TEXT,
        max_alt INTEGER DEFAULT 0, max_gs INTEGER DEFAULT 0,
        last_alt INTEGER, last_gs INTEGER, airborne INTEGER DEFAULT 0,
        last_seen TEXT, route TEXT, cruise_tas INTEGER);
    CREATE TABLE IF NOT EXISTS pireps(
        id INTEGER PRIMARY KEY AUTOINCREMENT, callsign TEXT, cid TEXT,
        dep TEXT, arr TEXT, actype TEXT, dep_time TEXT, arr_time TEXT,
        block_min REAL, dist_nm REAL, cruise_alt INTEGER, cruise_gs INTEGER,
        landing_fpm INTEGER, score INTEGER, revenue REAL, cost REAL, net REAL,
        filed_at TEXT);
    CREATE TABLE IF NOT EXISTS ledger(
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, memo TEXT, amount REAL, balance REAL);
    """)
    for k, v in AIRLINE.items():
        c.execute("INSERT OR IGNORE INTO config(key,value) VALUES(?,?)", (k, str(v)))
    if not c.execute("SELECT 1 FROM ledger LIMIT 1").fetchone():
        bal = float(cfg(c, "start_balance"))
        c.execute("INSERT INTO ledger(ts,memo,amount,balance) VALUES(?,?,?,?)",
                  (now(), "Opening balance", bal, bal))
    c.commit(); c.close()

def cfg(c, key):
    r = c.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
    return r["value"] if r else AIRLINE.get(key)

def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def balance(c):
    r = c.execute("SELECT balance FROM ledger ORDER BY id DESC LIMIT 1").fetchone()
    return r["balance"] if r else 0.0

def post(c, memo, amount):
    bal = balance(c) + amount
    if bal < 0:  # bankruptcy floor — the whole point
        bal = 0.0
    c.execute("INSERT INTO ledger(ts,memo,amount,balance) VALUES(?,?,?,?)",
              (now(), memo, amount, bal))
    return bal

# ------------------------- geo -------------------------
def nm(lat1, lon1, lat2, lon2):
    R = 3440.065
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1); dl = math.radians(lon2 - lon1)
    a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2 * R * math.asin(math.sqrt(a))

# ------------------------- feed -------------------------
def fetch():
    req = urllib.request.Request(VATSIM_URL, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)

# ------------------------- tracking -------------------------
def track():
    init_db()
    c = db()
    prefix = cfg(c, "icao").upper()
    print(f"Tracking {prefix}* on VATSIM. Ctrl-C to stop.\n")
    while True:
        try:
            data = fetch()
        except Exception as e:
            print(f"[feed] {type(e).__name__}: {e} — retrying"); time.sleep(POLL_SECONDS); continue

        seen = set()
        for p in data.get("pilots", []):
            cs = (p.get("callsign") or "").upper()
            if not cs.startswith(prefix):
                continue
            seen.add(cs)
            handle(c, p, cs)

        # close out flights that vanished from the feed (landed & disconnected)
        for row in c.execute("SELECT callsign FROM active").fetchall():
            if row["callsign"] not in seen:
                close_flight(c, row["callsign"])
        c.commit()
        time.sleep(POLL_SECONDS)

def handle(c, p, cs):
    fp = p.get("flight_plan") or {}
    alt = int(p.get("altitude") or 0)
    gs = int(p.get("groundspeed") or 0)
    lat = p.get("latitude"); lon = p.get("longitude")
    airborne = 1 if gs > 50 and alt > 1000 else 0
    row = c.execute("SELECT * FROM active WHERE callsign=?", (cs,)).fetchone()

    if not row:
        c.execute("""INSERT INTO active(callsign,cid,dep,arr,actype,phase,spawn_lat,
            spawn_lon,dep_time,max_alt,max_gs,last_alt,last_gs,airborne,last_seen,route,cruise_tas)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (cs, p.get("cid"), fp.get("departure",""), fp.get("arrival",""),
             fp.get("aircraft_short",""), "connected", lat, lon, "",
             alt, gs, alt, gs, airborne, now(), fp.get("route",""),
             int(fp.get("cruise_tas") or 0) if str(fp.get("cruise_tas","")).isdigit() else 0))
        print(f"[+] {cs} connected  {fp.get('departure','?')}->{fp.get('arrival','?')}  {fp.get('aircraft_short','')}")
        return

    phase = row["phase"]
    dep_time = row["dep_time"]
    if not row["airborne"] and airborne:            # just departed
        phase, dep_time = "departed", now()
        print(f"[>] {cs} airborne out of {row['dep']}")
    elif row["airborne"] and airborne and alt >= row["max_alt"]:
        phase = "cruise"
    elif row["airborne"] and gs < 40 and alt < 1000:  # back on ground
        phase = "arrived"

    c.execute("""UPDATE active SET phase=?,dep_time=?,max_alt=?,max_gs=?,
        last_alt=?,last_gs=?,airborne=?,last_seen=? WHERE callsign=?""",
        (phase, dep_time or row["dep_time"], max(alt, row["max_alt"]),
         max(gs, row["max_gs"]), alt, gs, airborne, now(), cs))

def close_flight(c, cs):
    r = c.execute("SELECT * FROM active WHERE callsign=?", (cs,)).fetchone()
    if not r:
        return
    # need a real departure to count it as a flight
    if not r["dep_time"] or not r["airborne"] and r["max_alt"] < 2000:
        c.execute("DELETE FROM active WHERE callsign=?", (cs,))
        print(f"[x] {cs} left without a tracked flight — discarded")
        return
    dep_t = datetime.fromisoformat(r["dep_time"])
    block_min = max(1.0, (datetime.now(timezone.utc) - dep_t).total_seconds() / 60)
    dist = est_distance(r)
    # approx landing rate from last feed descent — flagged as estimate
    fpm = estimate_landing_fpm(r)
    score = score_landing(fpm)
    rev = dist * float(cfg(c, "revenue_per_nm"))
    cost = (dist * float(cfg(c, "fuel_cost_per_nm"))
            + block_min/60 * float(cfg(c, "crew_cost_per_hr"))
            + block_min/60 * float(cfg(c, "maint_cost_per_hr")))
    net = rev - cost
    c.execute("""INSERT INTO pireps(callsign,cid,dep,arr,actype,dep_time,arr_time,
        block_min,dist_nm,cruise_alt,cruise_gs,landing_fpm,score,revenue,cost,net,filed_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (cs, r["cid"], r["dep"], r["arr"], r["actype"], r["dep_time"], now(),
         round(block_min,1), round(dist,1), r["max_alt"], r["max_gs"],
         fpm, score, round(rev,2), round(cost,2), round(net,2), now()))
    bal = post(c, f"PIREP {cs} {r['dep']}->{r['arr']}", net)
    # pilot + fleet updates
    if r["cid"]:
        c.execute("""INSERT INTO pilots(cid,name,flights,hours,earnings) VALUES(?,?,1,?,?)
            ON CONFLICT(cid) DO UPDATE SET flights=flights+1,
            hours=hours+?, earnings=earnings+?""",
            (r["cid"], r["cid"], block_min/60, net, block_min/60, net))
    c.execute("DELETE FROM active WHERE callsign=?", (cs,))
    print(f"[✓] PIREP {cs} {r['dep']}->{r['arr']} {round(dist)}nm "
          f"{round(block_min)}min land {fpm}fpm score {score} net ${net:,.0f} bal ${bal:,.0f}")

def est_distance(r):
    # feed doesn't give a track log here; approximate great-circle dep->arr later.
    # for now use block time * cruise speed as a floor estimate.
    gs = r["max_gs"] or r["cruise_tas"] or 400
    dep_t = datetime.fromisoformat(r["dep_time"])
    hrs = max(0.1, (datetime.now(timezone.utc) - dep_t).total_seconds()/3600)
    return gs * hrs * 0.85  # 0.85 factor for climb/descent vs block

def estimate_landing_fpm(r):
    # ESTIMATE ONLY — real value comes from the SimConnect module.
    # crude: if last alt was still descending fast, reflect that.
    return -(r["last_gs"] or 130) * 2  # placeholder proxy, replaced by Bridge

def score_landing(fpm):
    a = abs(fpm)
    if a <= 150: return 100
    if a <= 250: return 90
    if a <= 400: return 78
    if a <= 600: return 60
    return 40

# ------------------------- read commands -------------------------
def status():
    c = db()
    print(f"\n{cfg(c,'name')}  ({cfg(c,'icao')}*)")
    print(f"Balance: ${balance(c):,.2f}\n")
    rows = c.execute("SELECT callsign,dep,arr,phase,last_alt,last_gs FROM active").fetchall()
    if not rows:
        print("No active flights."); return
    print("ACTIVE FLIGHTS")
    for r in rows:
        print(f"  {r['callsign']:8} {r['dep']}->{r['arr']:5} {r['phase']:10} "
              f"FL{r['last_alt']//100:03d} {r['last_gs']}kt")

def pireps():
    c = db()
    rows = c.execute("SELECT * FROM pireps ORDER BY id DESC LIMIT 15").fetchall()
    if not rows:
        print("No PIREPs yet."); return
    print("\nRECENT PIREPS")
    for r in rows:
        print(f"  #{r['id']:<4} {r['callsign']:8} {r['dep']}->{r['arr']:5} "
              f"{r['dist_nm']:>6.0f}nm {r['block_min']:>5.0f}min "
              f"score {r['score']:>3} net ${r['net']:>10,.0f}")

def fleet():
    c = db()
    rows = c.execute("SELECT * FROM aircraft ORDER BY reg").fetchall()
    if not rows:
        print("No aircraft. Add with: python3 vaops.py addac <REG> <TYPE> <LOCATION>"); return
    print("\nFLEET")
    for r in rows:
        due = "  DUE" if r["hours_since_maint"] >= float(cfg(c,"maint_interval_hrs")) else ""
        print(f"  {r['reg']:8} {r['type']:8} {r['location']:5} {r['status']:10} "
              f"{r['hours']:.1f}h{due}")

def roster():
    c = db()
    rows = c.execute("SELECT * FROM pilots ORDER BY hours DESC LIMIT 20").fetchall()
    if not rows:
        print("No pilots logged yet."); return
    print("\nROSTER / LEADERBOARD")
    for i, r in enumerate(rows, 1):
        print(f"  {i:>2}. {r['cid']:10} {r['flights']:>3} flt {r['hours']:>6.1f}h "
              f"${r['earnings']:>10,.0f}")

def addac(reg, typ, loc):
    init_db(); c = db()
    c.execute("INSERT OR REPLACE INTO aircraft(reg,type,location) VALUES(?,?,?)",
              (reg.upper(), typ.upper(), loc.upper()))
    c.commit(); print(f"Added {reg.upper()} ({typ}) at {loc}")

def setup():
    init_db(); c = db()
    print("Leave blank to keep current value.")
    for k in ("name","icao","start_balance","revenue_per_nm","fuel_cost_per_nm",
              "crew_cost_per_hr","maint_cost_per_hr"):
        cur = cfg(c, k)
        v = input(f"  {k} [{cur}]: ").strip()
        if v:
            c.execute("UPDATE config SET value=? WHERE key=?", (v, k))
    c.commit(); print("Saved. Now run: python3 vaops.py track")

# ------------------------- cli -------------------------
if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "setup": setup()
    elif cmd == "track": track()
    elif cmd == "status": init_db(); status()
    elif cmd == "pireps": init_db(); pireps()
    elif cmd == "fleet": init_db(); fleet()
    elif cmd == "roster": init_db(); roster()
    elif cmd == "addac" and len(sys.argv) == 5: addac(*sys.argv[2:5])
    else:
        print(__doc__)
