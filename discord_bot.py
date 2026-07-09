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

def load_token():
    here = os.path.dirname(os.path.abspath(__file__))
    p = os.path.join(here, "token.txt")
    if os.path.exists(p):
        t = open(p, encoding="utf-8").read().strip()
        if t: return t
    return os.environ.get("BOT_TOKEN", "").strip()
BOT_TOKEN = load_token()

BRIDGE = os.environ.get("BRIDGE_URL", "http://127.0.0.1:80")

ROLE_CEO = "CEO"; ROLE_FM = "Fleet Manager"; ROLE_PILOT = "Pilot"; ROLE_STRIKE = "Strike One"
CHANNEL_ROUTES = 1491904248399794336
CHANNEL_PUBLIC = 0
CHANNEL_STAFF  = 0
CHANNEL_OPS    = 1490325655814930505

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

def load_airports():
    try:
        d = json.load(open("bot_airports.json", encoding="utf-8"))
        return d.get("db",{}), d.get("cityidx",{})
    except Exception as e:
        print("airport db not loaded:", e); return {}, {}
AIRPORTS, CITY_IDX = load_airports()

import unicodedata
def _norm(s): return ''.join(c for c in unicodedata.normalize('NFD', s) if unicodedata.category(c)!='Mn').lower().strip()

def suggest_airport(token):
    """Given a bad code or a city name, suggest a valid ICAO."""
    t = token.strip()

    key = _norm(t)
    if key in CITY_IDX: return CITY_IDX[key]

    for city,code in CITY_IDX.items():
        if key and (key in city or city in key) and len(key)>=3:
            return code
    return None

def rolenames(m): return {r.name for r in m.roles}

@bot.event
async def on_ready():
    try: await bot.tree.sync()
    except Exception as e: print("sync err:", e)
    print(f"Amal Airways bot online as {bot.user}")
    if not watch_flights.is_running():
        watch_flights.start()
    if not watch_new_pilots.is_running():
        watch_new_pilots.start()

@tasks.loop(seconds=10)
async def watch_new_pilots():
    state = bridge_get("/state")
    if not state: return
    queue = state.get("new_pilots", [])
    pending = [q for q in queue if not q.get("made")]
    if not pending: return

    guild = bot.guilds[0] if bot.guilds else None
    if not guild: return
    for q in pending:
        callsign = (q.get("username") or "").upper()
        if not callsign: continue

        existing = discord.utils.get(guild.roles, name=callsign)
        if existing:
            bridge_post("/rolemade", {"username": callsign})
            continue
        try:
            await guild.create_role(name=callsign, reason="New Amal Airways pilot created in the app")
            bridge_post("/rolemade", {"username": callsign})

            ops = bot.get_channel(CHANNEL_OPS) if CHANNEL_OPS else None
            if ops:
                nm = q.get("name") or callsign
                label = f" ({nm})" if nm and nm != callsign else ""
                await ops.send(f"🆕 New pilot **{callsign}**{label} added — Discord role created.")
        except discord.Forbidden:

            staff = bot.get_channel(CHANNEL_STAFF) if CHANNEL_STAFF else None
            target = staff or (bot.get_channel(CHANNEL_OPS) if CHANNEL_OPS else None)
            if target:
                await target.send(f"⚠️ Couldn't create role **{callsign}** — give me **Manage Roles** and move my role to the top in Server Settings, then I'll retry.")
            return
        except Exception as e:
            print("role create error:", e)
            return

_last_flight_id = [None]

@tasks.loop(seconds=8)
async def watch_flights():
    state = bridge_get("/state")
    if not state: return
    flights = state.get("flights", [])
    if not flights: return

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
    ops = bot.get_channel(CHANNEL_OPS) if CHANNEL_OPS else None
    cs = f.get("pilot","KLA"); dep=f.get("dep","----"); arr=f.get("arr","----")
    tier = f.get("tier",""); fpm = f.get("landing_fpm",0); score=f.get("score",0)
    viol = f.get("violations",0); ac=f.get("aircraft","")
    hrs = f.get("hours",0)

    if str(tier).upper().startswith("SLEW") or f.get("slew"):
        if staff:
            await staff.send(f"🚫 **{cs}** — FLIGHT VOIDED (SLEW DETECTED) on {dep} → {arr}. "
                             f"Score 0. A Fleet Manager should review and strike if confirmed.")
        if ops:
            await ops.send(f"🚫 **{cs}** {dep} → {arr} — **VOIDED (slew)**")
        return

    if ops:
        emoji = "🧈" if str(tier).lower()=="butter" else "🛬"
        summary = (f"{emoji} **Flight Report — {cs}**\n"
                   f"**Route:** {dep} → {arr}\n"
                   f"**Aircraft:** {ac}\n"
                   f"**Block time:** {hrs:.1f} h\n"
                   f"**Landing:** {fpm} fpm ({tier})\n"
                   f"**Score:** {score}/100\n"
                   f"**Violations:** {viol}")
        await ops.send(summary)

    if pub:
        line = f"🛬 **{cs}** {dep} → {arr} · {ac}\nLanding: **{fpm} fpm** ({tier}) · Score: **{score}**"
        if tier and tier.lower()=="butter":
            line = f"🧈 **BUTTER!** {cs} greased {dep} → {arr} at **{fpm} fpm** — smooth as it gets."
        await pub.send(line)

    if viol and staff:
        await staff.send(f"⚠️ **{cs}** logged **{viol}** SOP violation(s) on {dep} → {arr}.")

    old_r = rank_for(f.get("_old_hours",0)); new_r = rank_for(f.get("_new_hours",0))
    if old_r != new_r and pub:
        name = f.get("_pilot_name", cs)
        await pub.send(f"🎉 Congratulations **{name}** — promoted to **{new_r}**!")

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

        bad = []
        if AIRPORTS and dep not in AIRPORTS: bad.append(dep)
        if AIRPORTS and arr not in AIRPORTS: bad.append(arr)
        if bad:
            tips = []
            for code in bad:
                s = suggest_airport(code)
                if s: tips.append(f"**{code}** isn't valid — did you mean **{s}** ({AIRPORTS[s]['name']})?")
                else: tips.append(f"**{code}** isn't a valid airport code.")
            await message.channel.send("⚠️ " + " ".join(tips) + "\nPost it again with valid ICAO codes (e.g. `KMIA-KPBI`).")
            await bot.process_commands(message); return
        ok = bridge_post("/route", {"dep":dep, "arr":arr, "by":str(message.author)})
        if ok:
            dn = AIRPORTS.get(dep,{}).get('city',dep); an = AIRPORTS.get(arr,{}).get('city',arr)
            await message.channel.send(
                f"✈️ **{dep} → {arr}** ({dn} to {an}) is now a live route — anyone can fly it! "
                f"It's in the app under Routes & Hubs.")
        else:
            await message.channel.send(
                f"Got **{dep} → {arr}**, but I couldn't reach the airline server. Try again shortly.")

    elif AIRPORTS:
        cm = _re.search(r'route:?\s+([A-Za-z .]{3,25})\s+(?:to|-|>)\s+([A-Za-z .]{3,25})', message.content or '', _re.IGNORECASE)
        if cm:
            d = suggest_airport(cm.group(1)); a = suggest_airport(cm.group(2))
            if d and a:
                await message.channel.send(f"Did you mean **{d} → {a}**? Post `{d}-{a}` to make it a live route.")
    await bot.process_commands(message)

import re as _re
ID_ROLE = _re.compile(r'^(CEO0?1|FM\d{1,2}|KLA\d{1,3})$', _re.IGNORECASE)

def type_from_roles(member):
    """Read a pilot's type from their Discord role names. Returns the type or None."""
    names = [r.name.lower() for r in getattr(member, "roles", [])]
    for t in ("cargo", "commercial", "charter", "tour"):
        if any(t in n for n in names):
            return t
    return None

@bot.tree.command(description="Get your Amal Airways sign-in code by DM")
async def getcode(interaction: discord.Interaction):

    member = interaction.user
    if interaction.guild and not isinstance(member, discord.Member):
        member = interaction.guild.get_member(member.id) or member
    if interaction.guild and (not getattr(member, "roles", None) or len(member.roles) <= 1):
        try:
            member = await interaction.guild.fetch_member(interaction.user.id)
        except Exception as e:
            print("fetch_member failed:", e)
    all_roles = [r.name for r in getattr(member, "roles", [])]
    print(f"[getcode] {interaction.user} roles seen: {all_roles}")
    ids = [r.name.upper() for r in getattr(member, "roles", []) if ID_ROLE.match(r.name)]
    if not ids:
        await interaction.response.send_message(
            "You don't have an ID role yet (like KLA002 or FM01) — ask a Fleet Manager to assign one.\n"
            f"(Roles I can see: {', '.join(all_roles) if all_roles else 'none'})",
            ephemeral=True); return

    myid = ids[0]
    if myid in ("CEO1","CEO01"): myid = "CEO01"
    # sync this pilot's type from their Discord roles to the app
    if myid.startswith("KLA"):
        t = type_from_roles(member)
        if t:
            bridge_post("/pilot", {"username": myid, "ptype": t})
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

@bot.tree.command(description="Apply Strike One (staff only)")
@app_commands.describe(pilot="Pilot to strike", reason="Reason")
async def strike(interaction: discord.Interaction, pilot: discord.Member, reason: str="SOP violation"):
    member = interaction.user
    if interaction.guild and (not getattr(member,"roles",None) or len(member.roles)<=1):
        try: member = await interaction.guild.fetch_member(interaction.user.id)
        except Exception: pass
    rn = {r.name for r in getattr(member,"roles",[])}
    STAFF = {ROLE_FM, ROLE_CEO, "COO", "Amal Airways | Fleet Manager", "CEO", "Fleet Manager"}
    is_staff = bool(rn & STAFF) or any(r.name.upper() in ("CEO01","FM01","FM02") for r in getattr(member,"roles",[]))
    if not is_staff:
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
