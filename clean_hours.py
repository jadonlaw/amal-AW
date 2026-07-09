#!/usr/bin/env python3
"""
One-time cleanup: rebuild aircraft hours and pilot totals from the actual
flights list, removing phantom hours from old testing. Safe to run once.
Run on the server:  python3 clean_hours.py
"""
import json, os, shutil, sys

STATE = "airline_state.json"
if not os.path.exists(STATE):
    print(f"{STATE} not found — run this in the amal-AW folder."); sys.exit(1)

# back up first, so nothing is ever lost
shutil.copy(STATE, STATE + ".backup")
print(f"Backed up to {STATE}.backup")

s = json.load(open(STATE))

ac = {}
p_hours = {}
p_flights = {}
for f in s.get("flights", []):
    reg = f.get("reg", "")
    hrs = f.get("hours", 0)
    if reg:
        ac[reg] = ac.get(reg, 0) + hrs
    u = (f.get("pilot") or "").upper()
    if u:
        p_hours[u] = p_hours.get(u, 0) + hrs
        p_flights[u] = p_flights.get(u, 0) + 1

for a in s.get("fleet", []):
    real = round(ac.get(a.get("reg", ""), 0), 1)
    a["hours"] = real
    a["maint"] = real
    a["status"] = "Grounded — maintenance" if real >= 100 else "Available"

for u, pl in s.get("pilots", {}).items():
    pl["hours"] = round(p_hours.get(u, 0), 1)
    pl["flights"] = p_flights.get(u, 0)

json.dump(s, open(STATE, "w"), indent=2)
print("Done. Aircraft hours and pilot totals rebuilt from actual flights.")
print("Restart the bridge:  sudo systemctl restart amal-bridge")
