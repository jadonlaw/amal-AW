#!/usr/bin/env python3
"""
Amal Airways — Discord Bot
==========================
Runs on the Lightsail server alongside the bridge, 24/7.

SETUP:
  1. Put your bot token in token.txt (same folder), OR set BOT_TOKEN env var.
  2. pip install discord.py
  3. python3 discord_bot.py

Enable in the Discord Developer Portal -> Bot -> Privileged Gateway Intents:
  - SERVER MEMBERS INTENT
  - MESSAGE CONTENT INTENT
"""
import os, re, json, urllib.request

# ---- token ----
def load_token():
    here = os.path.dirname(os.path.abspath(__file__))
    p = os.path.join(here, "token.txt")
    if os.path.exists(p):
        t = open(p, encoding="utf-8").read().strip()
        if t: return t
    return os.environ.get("BOT_TOKEN", "").strip()
BOT_TOKEN = load_token()

# ---- bridge (same server) ----
BRIDGE = os.environ.get("BRIDGE_URL", "http://127.0.0.1:80")

# ---- config ----
ROLE_CEO = "CEO"; ROLE_FM = "Fleet Manager"; ROLE_PILOT = "Pilot"; ROLE_STRIKE = "Strike One"
CHANNEL_ROUTES = 0     # route channel ID (0 = watch all channels)
CHANNEL_PUBLIC = 0     # public flight call-outs (0 = off until you set it)
CHANNEL_STAFF  = 0     # staff-only alerts: slew, violations (0 = off until you set it)

# rank thresholds (must match the app)
RANKS = [(0,"First Officer"),(25,"Senior First Officer"),(75,"Captain"),
         (150,"Senior Captain"),(300,"Training Captain")]
def rank_for(h):
    r = RANKS[0][1]
    for mn,name in RANKS:
        if h >= mn: r = name
    return r

import discord
from discord import app_commands
from discord.ext import commands, tasks

intents = discord.Intents.default()
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

def bridge_post(path, payload):
    try:
        req = urllib.request.Request(BRIDGE+path, data=json.dumps(payload).encode(),
            method="POST", headers={"Content-Type":"application/json"})
        urllib.request.urlopen(req, timeout=6)
        return True
    except Exception as e:
        print("bridge error:", e); return False

def bridge_get(path):
    try:
        return json.loads(urllib.request.urlopen(BRIDGE+path, timeout=6).read().decode())
    except Exception as e:
        print("bridge get error:", e); return None

def load_codes():
    codes = {}
    try:
        for line in open("CREDENTIALS.txt", encoding="utf-8"):
            if ":" in line and "===" not in line:
                left = line.split("->")[0]
                u,p = left.split(":",1)
                codes[u.strip().upper()] = p.strip()
    except FileNotFoundError:
        pass
    return codes
CODES = load_codes()

def rolenames(m): return {r.name for r in m.roles}

@bot.event
async def on_ready():
    try: await bot.tree.sync()
    except Exception as e: print("sync err:", e)
    print(f"Amal Airways bot online as {bot.user}")
    if not watch_flights.is_running():
        watch_flights.start()

# ===== AUTO WATCHER — posts flight events with NO slash commands =====
_last_flight_id = [None]

@tasks.loop(seconds=8)
async def watch_flights():
    state = bridge_get("/state")
    if not state: return
    flights = state.get("flights", [])
    if not flights: return
    # first run: remember where we are, don't spam old flights
    if _last_flight_id[0] is None:
        _last_flight_id[0] = max((f.get("id",0) for f in flights), default=0)
        return
    new = [f for f in flights if f.get("id",0) > _last_flight_id[0]]
    for f in sorted(new, key=lambda x: x.get("id",0)):
        _last_flight_id[0] = f.get("id",0)
        await announce_flight(f)

async def announce_flight(f):
    pub = bot.get_channel(CHANNEL_PUBLIC) if CHANNEL_PUBLIC else None
    staff = bot.get_channel(CHANNEL_STAFF) if CHANNEL_STAFF else None
    cs = f.get("pilot","KLA"); dep=f.get("dep","----"); arr=f.get("arr","----")
    tier = f.get("tier",""); fpm = f.get("landing_fpm",0); score=f.get("score",0)
    viol = f.get("violations",0); ac=f.get("aircraft","")

    # SLEW -> staff alert
    if str(tier).upper().startswith("SLEW") or f.get("slew"):
        if staff:
            await staff.send(f"🚫 **{cs}** — FLIGHT VOIDED (SLEW DETECTED) on {dep} → {arr}. "
                             f"Score 0. A Fleet Manager should review and strike if confirmed.")
        return

    # normal completed flight -> public call-out
    if pub:
        line = f"🛬 **{cs}** {dep} → {arr} · {ac}\nLanding: **{fpm} fpm** ({tier}) · Score: **{score}**"
        # butter shout-out
        if tier and tier.lower()=="butter":
            line = f"🧈 **BUTTER!** {cs} greased {dep} → {arr} at **{fpm} fpm** — smooth as it gets."
        await pub.send(line)

    # violations -> staff channel
    if viol and staff:
        await staff.send(f"⚠️ **{cs}** logged **{viol}** SOP violation(s) on {dep} → {arr}.")

    # rank-up -> public
    old_r = rank_for(f.get("_old_hours",0)); new_r = rank_for(f.get("_new_hours",0))
    if old_r != new_r and pub:
        name = f.get("_pilot_name", cs)
        await pub.send(f"🎉 Congratulations **{name}** — promoted to **{new_r}**!")

# ===== ROUTE DETECTION (no slash command needed) =====
# Detects "KATL-KMCO", "KATL KMCO", "KATL to KMCO" and makes it a live route.
ICAO = r'([A-Za-z]{4})'
ROUTE_RE = re.compile(ICAO + r'\s*(?:-|to|>|→|/)\s*' + ICAO, re.IGNORECASE)

@bot.event
async def on_message(message):
    if message.author.bot:
        return
    if CHANNEL_ROUTES and message.channel.id != CHANNEL_ROUTES:
        await bot.process_commands(message); return
    m = ROUTE_RE.search(message.content or "")
    if m:
        dep, arr = m.group(1).upper(), m.group(2).upper()
        ok = bridge_post("/route", {"dep":dep, "arr":arr, "by":str(message.author)})
        if ok:
            await message.channel.send(
                f"✈️ **{dep} → {arr}** is now a live route — anyone can fly it! "
                f"It's in the app under Routes & Hubs.")
        else:
            await message.channel.send(
                f"Got **{dep} → {arr}**, but I couldn't reach the airline server. Try again shortly.")
    await bot.process_commands(message)

# ===== /getcode — matches the user's ID role (CEO01 / FM## / KLA###) =====
import re as _re
ID_ROLE = _re.compile(r'^(CEO0?1|FM\d{1,2}|KLA\d{1,3})$', _re.IGNORECASE)

@bot.tree.command(description="Get your Amal Airways sign-in code by DM")
async def getcode(interaction: discord.Interaction):
    # find the role that matches a credential ID (e.g. FM01, KLA002, CEO01)
    all_roles = [r.name for r in interaction.user.roles]
    print(f"[getcode] {interaction.user} roles seen: {all_roles}")
    ids = [r.name.upper() for r in interaction.user.roles if ID_ROLE.match(r.name)]
    if not ids:
        await interaction.response.send_message(
            "You don't have an ID role yet (like KLA002 or FM01) — ask a Fleet Manager to assign one.\n"
            f"(Roles I can see: {', '.join(all_roles) if all_roles else 'none'})",
            ephemeral=True); return
    # normalize CEO1 -> CEO01
    myid = ids[0]
    if myid in ("CEO1","CEO01"): myid = "CEO01"
    code = CODES.get(myid)
    if not code:
        await interaction.response.send_message(
            f"Your ID **{myid}** isn't in the credentials list yet — tell the CEO.", ephemeral=True); return
    try:
        await interaction.user.send(
            f"**Amal Airways — your sign-in**\nUsername: `{myid}`\nPasscode: `{code}`\n\n"
            f"⚠️ This is yours alone. Don't share it.")
        await interaction.response.send_message(f"Sent your code for **{myid}** by DM ✈️", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message("Enable DMs from server members and try again.", ephemeral=True)

# ===== /strike =====
@bot.tree.command(description="Apply Strike One (staff only)")
@app_commands.describe(pilot="Pilot to strike", reason="Reason")
async def strike(interaction: discord.Interaction, pilot: discord.Member, reason: str="SOP violation"):
    rn = rolenames(interaction.user)
    if not (ROLE_FM in rn or ROLE_CEO in rn):
        await interaction.response.send_message("Only staff can strike.", ephemeral=True); return
    role = discord.utils.get(interaction.guild.roles, name=ROLE_STRIKE)
    if not role:
        await interaction.response.send_message(f"Create a '{ROLE_STRIKE}' role first.", ephemeral=True); return
    try:
        await pilot.add_roles(role, reason=f"By {interaction.user}: {reason}")
    except discord.Forbidden:
        await interaction.response.send_message("Move my role above 'Strike One' in Server Settings.", ephemeral=True); return
    await interaction.response.send_message(f"Strike One applied to {pilot.mention} — {reason}")

if __name__ == "__main__":
    if not BOT_TOKEN:
        print("NO TOKEN. Put your bot token in token.txt and run again.")
    else:
        bot.run(BOT_TOKEN)
