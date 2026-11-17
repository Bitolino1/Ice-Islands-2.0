"""
Ice Islands — servidor de jogo 2D multiplayer (estilo Club Penguin, mas nosso).
Roda em Python 3 + aiohttp. Serve o jogo (index.html) e o WebSocket no MESMO endereco.

Como rodar:
    pip install aiohttp
    python server.py
Depois abra http://localhost:8080 no navegador.
Amigos na mesma rede entram em  http://SEU_IP_LOCAL:8080
"""

import os
import math
import json
import random
import pathlib
import hashlib
import secrets
from aiohttp import web, WSMsgType

ROOMS = ["vila", "porto", "ilha"]
COLORS = ["#3b82f6", "#ef4444", "#10b981", "#f59e0b",
          "#8b5cf6", "#ec4899", "#14b8a6", "#f97316"]

COINS_START = 100000

# Catalogo da loja: id -> {nome, preco, slot, icone}
CATALOG = {
    "bone":     {"name": "Boné",             "price": 80,  "slot": "hat",     "icon": "🧢"},
    "cartola":  {"name": "Cartola",          "price": 200, "slot": "hat",     "icon": "🎩"},
    "coroa":    {"name": "Coroa",            "price": 600, "slot": "hat",     "icon": "👑"},
    "gorro":    {"name": "Gorro de Inverno", "price": 120, "slot": "hat",     "icon": "🧶"},
    "capacete": {"name": "Capacete",         "price": 180, "slot": "hat",     "icon": "⛑️"},
    "festa":    {"name": "Chapéu de Festa",  "price": 90,  "slot": "hat",     "icon": "🎉"},
    "oculos":   {"name": "Óculos Escuros",   "price": 100, "slot": "glasses", "icon": "🕶️"},
    "neve":     {"name": "Óculos de Neve",   "price": 130, "slot": "glasses", "icon": "🥽"},
    "nerd":     {"name": "Óculos de Grau",   "price": 70,  "slot": "glasses", "icon": "👓"},
    "cachecol": {"name": "Cachecol",         "price": 110, "slot": "neck",    "icon": "🧣"},
    "laco":     {"name": "Laço",             "price": 60,  "slot": "neck",    "icon": "🎀"},
    "medalha":  {"name": "Medalha",          "price": 250, "slot": "neck",    "icon": "🏅"},
    # Skins: trocam o corpo do personagem (as roupas continuam servindo)
    "urso":     {"name": "Urso Polar",       "price": 1000, "slot": "skin",   "icon": "🐻"},
    "alce":     {"name": "Alce",             "price": 1500, "slot": "skin",   "icon": "🦌"},
    "castor":   {"name": "Castor",           "price": 1200, "slot": "skin",   "icon": "🦫"},
}
SLOTS = ("hat", "glasses", "neck", "skin")

clients = {}      # ws -> player dict
next_id = 1


def pub(p):
    """Versão pública de um jogador (sem dados privados como moedas/inventário)."""
    return {"id": p["id"], "name": p["name"], "color": p["color"],
            "x": p["x"], "y": p["y"], "outfit": p["outfit"]}


# ---------- Contas (registro + login, salvas pra sempre em disco) ----------
ACCOUNTS_FILE = pathlib.Path(__file__).parent / "accounts.json"
accounts = {}   # usuario_minusculo -> {name, pass, color, coins, owned, outfit}


def load_accounts():
    global accounts
    try:
        accounts = json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8"))
    except Exception:
        accounts = {}
    # Ajuste unico: contas criadas antes do aumento das moedas iniciais sobem
    # para COINS_START. Quem ja recebeu fica marcado e nao recebe de novo.
    changed = False
    for rec in accounts.values():
        if not rec.get("topup_100k"):
            rec["coins"] = max(int(rec.get("coins", 0)), COINS_START)
            rec["topup_100k"] = True
            changed = True
    if changed:
        save_accounts()


def save_accounts():
    try:
        ACCOUNTS_FILE.write_text(json.dumps(accounts), encoding="utf-8")
    except Exception as e:
        print("Erro ao salvar contas:", e)


def hash_pw(password, salt=None):
    if salt is None:
        salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100000).hex()
    return salt + "$" + h


def check_pw(password, stored):
    try:
        salt, _ = stored.split("$", 1)
    except (ValueError, AttributeError):
        return False
    return secrets.compare_digest(hash_pw(password, salt), stored)


def persist(player):
    """Salva o progresso do jogador logado na conta dele."""
    u = player.get("account")
    if not u or u not in accounts:
        return
    rec = accounts[u]
    rec["name"] = player["name"]
    rec["color"] = player["color"]
    rec["coins"] = player["coins"]
    rec["owned"] = player["owned"]
    rec["outfit"] = player["outfit"]
    save_accounts()


def num(value, default, lo, hi):
    """Converte para numero dentro de [lo, hi]; se vier lixo, usa o default."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(v):
        return default
    return max(lo, min(hi, v))


def players_in(room, exclude=None):
    return [p for w, p in clients.items() if p["room"] == room and w is not exclude]


async def send(ws, obj):
    try:
        await ws.send_str(json.dumps(obj))
    except Exception:
        pass


async def broadcast(room, obj, exclude=None):
    for w, p in list(clients.items()):
        if p["room"] == room and w is not exclude:
            await send(w, obj)


async def login_player(ws, player, ul):
    """Carrega os dados da conta no jogador e avisa todo mundo da sala."""
    rec = accounts[ul]
    player["account"] = ul
    player["name"] = rec["name"]
    player["color"] = rec["color"]
    player["coins"] = rec["coins"]
    player["owned"] = list(rec.get("owned", []))
    player["outfit"] = dict(rec.get("outfit", {}))
    await send(ws, {"t": "auth_ok", "user": rec["name"]})
    await send(ws, {"t": "wallet", "coins": player["coins"], "owned": player["owned"]})
    await broadcast(player["room"], {"t": "update", "id": player["id"],
                                     "name": player["name"], "color": player["color"]})
    await broadcast(player["room"], {"t": "outfit", "id": player["id"], "outfit": player["outfit"]})


async def ws_handler(request):
    global next_id
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)

    pid = next_id
    next_id += 1
    player = {
        "id": pid,
        "name": f"Pinguim{pid}",
        "color": random.choice(COLORS),
        "room": "vila",
        "x": random.randint(380, 520),
        "y": random.randint(300, 360),
        "coins": COINS_START,
        "owned": [],
        "outfit": {},
        "account": None,
    }
    clients[ws] = player

    # Catalogo da loja + estado inicial (voce + quem ja esta na sala) + carteira
    await send(ws, {"t": "catalog", "items": CATALOG})
    await send(ws, {"t": "init", "id": pid, "room": player["room"],
                    "players": [pub(player)] + [pub(p) for p in players_in("vila", exclude=ws)]})
    await send(ws, {"t": "wallet", "coins": player["coins"], "owned": player["owned"]})
    await broadcast("vila", {"t": "join", "player": pub(player)}, exclude=ws)

    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                data = json.loads(msg.data)
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            t = data.get("t")

            if t == "hello":
                name = str(data.get("name", "")).strip()[:16]
                if name:
                    player["name"] = name
                if data.get("color") in COLORS:
                    player["color"] = data["color"]
                await broadcast(player["room"], {
                    "t": "update", "id": pid,
                    "name": player["name"], "color": player["color"]})
                persist(player)

            elif t == "move":
                player["x"] = num(data.get("x"), player["x"], 0, 900)
                player["y"] = num(data.get("y"), player["y"], 0, 540)
                await broadcast(player["room"], {
                    "t": "move", "id": pid,
                    "x": player["x"], "y": player["y"]}, exclude=ws)

            elif t == "chat":
                text = str(data.get("text", ""))[:120].strip()
                if text:
                    await broadcast(player["room"], {"t": "chat", "id": pid, "text": text})

            elif t == "emote":
                await broadcast(player["room"], {
                    "t": "emote", "id": pid, "e": str(data.get("e", ""))[:12]})

            elif t == "ball":
                await broadcast(player["room"], {
                    "t": "ball", "id": pid,
                    "x": num(data.get("x"), 450, 0, 900),
                    "y": num(data.get("y"), 270, 0, 540)})

            elif t == "buy":
                iid = data.get("item")
                it = CATALOG.get(iid) if isinstance(iid, str) else None
                if it and iid not in player["owned"] and player["coins"] >= it["price"]:
                    player["coins"] -= it["price"]
                    player["owned"].append(iid)
                    await send(ws, {"t": "wallet", "coins": player["coins"],
                                    "owned": player["owned"]})
                    persist(player)

            elif t == "equip":
                slot = data.get("slot")
                iid = data.get("item")
                if slot in SLOTS:
                    if not iid:
                        player["outfit"].pop(slot, None)
                    elif isinstance(iid, str) and iid in player["owned"] and CATALOG.get(iid, {}).get("slot") == slot:
                        player["outfit"][slot] = iid
                    await broadcast(player["room"], {
                        "t": "outfit", "id": pid, "outfit": player["outfit"]})
                    persist(player)

            elif t == "register":
                user = str(data.get("user", "")).strip()
                pw = str(data.get("pass", ""))
                ul = user.lower()
                if len(user) < 3 or len(user) > 16 or not user.replace("_", "").isalnum():
                    await send(ws, {"t": "auth_err", "reason": "Usuário inválido (3-16 letras/números)."})
                elif len(pw) < 4:
                    await send(ws, {"t": "auth_err", "reason": "Senha curta demais (mínimo 4)."})
                elif ul in accounts:
                    await send(ws, {"t": "auth_err", "reason": "Esse usuário já existe. Faça login."})
                else:
                    accounts[ul] = {
                        "name": user, "pass": hash_pw(pw), "color": player["color"],
                        "coins": player["coins"], "owned": player["owned"],
                        "outfit": player["outfit"], "topup_100k": True,
                    }
                    save_accounts()
                    await login_player(ws, player, ul)

            elif t == "login":
                user = str(data.get("user", "")).strip()
                pw = str(data.get("pass", ""))
                ul = user.lower()
                rec = accounts.get(ul)
                if not rec or not check_pw(pw, rec.get("pass", "")):
                    await send(ws, {"t": "auth_err", "reason": "Usuário ou senha incorretos."})
                else:
                    await login_player(ws, player, ul)

            elif t == "room":
                newroom = data.get("room")
                if newroom in ROOMS and newroom != player["room"]:
                    old = player["room"]
                    await broadcast(old, {"t": "leave", "id": pid}, exclude=ws)
                    player["room"] = newroom
                    player["x"] = random.randint(380, 520)
                    player["y"] = random.randint(300, 360)
                    await send(ws, {"t": "init", "id": pid, "room": newroom,
                                    "players": [pub(player)] + [pub(p) for p in players_in(newroom, exclude=ws)]})
                    await broadcast(newroom, {"t": "join", "player": pub(player)}, exclude=ws)
    finally:
        clients.pop(ws, None)
        await broadcast(player["room"], {"t": "leave", "id": pid})

    return ws


async def index(request):
    html = (pathlib.Path(__file__).parent / "index.html").read_text(encoding="utf-8")
    return web.Response(text=html, content_type="text/html")


def static_file(fname):
    """Serve um arquivo do projeto (manifest, service worker, icones)."""
    async def handler(request):
        return web.FileResponse(pathlib.Path(__file__).parent / fname)
    return handler


def main():
    load_accounts()
    print(f"Contas carregadas: {len(accounts)}")
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/ws", ws_handler)
    # Arquivos da PWA (pra instalar o app no Mac/Windows)
    for fn in ("manifest.json", "sw.js", "icon-192.png", "icon-512.png"):
        app.router.add_get("/" + fn, static_file(fn))
    # Na nuvem (Render/Railway/Replit) a porta vem na variavel PORT.
    # No seu PC, usa 8080 por padrao.
    port = int(os.environ.get("PORT", 8080))
    print(f"Ice Islands rodando!  ->  http://localhost:{port}")
    web.run_app(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
