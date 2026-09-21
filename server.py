#!/usr/bin/env python3
"""
Casino Royale (Spielgeld) - Backend
-----------------------------------
Nur Python-Standardbibliothek, keine Installation noetig.
Starten:  python3 server.py   ->   http://localhost:8000

Alle Spiele laufen serverseitig. Guthaben, Konten, Spielstaende und der
Verlauf liegen in der SQLite-Datei casino.db.
"""
import hashlib
import itertools
import json
import os
import random
import re
import secrets
import sqlite3
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, "casino.db")
PORT = int(os.environ.get("PORT", 8000))

START_BALANCE = 1000      # Startguthaben
DAILY_BONUS = 500         # Tagesbonus
BONUS_COOLDOWN = 24 * 3600
RESCUE_AMOUNT = 200       # Notgroschen, wenn man pleite ist

LOCK = threading.RLock()  # ein Lock fuer alle DB-Operationen (einfach & sicher)


# --------------------------------------------------------------------------
# Datenbank
# --------------------------------------------------------------------------
def db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with LOCK, db() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE COLLATE NOCASE NOT NULL,
                pw_hash TEXT NOT NULL,
                salt TEXT NOT NULL,
                balance INTEGER NOT NULL,
                last_bonus REAL NOT NULL DEFAULT 0,
                poker_hands INTEGER NOT NULL DEFAULT 0,
                created REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                created REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                game TEXT NOT NULL,
                delta INTEGER NOT NULL,
                balance_after INTEGER NOT NULL,
                note TEXT,
                ts REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS active_games (
                user_id INTEGER NOT NULL,
                game TEXT NOT NULL,
                state TEXT NOT NULL,
                PRIMARY KEY (user_id, game)
            );
            """
        )


class ApiError(Exception):
    def __init__(self, msg, code=400):
        super().__init__(msg)
        self.msg, self.code = msg, code


def hash_pw(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 120_000).hex()


def get_balance(c, uid):
    return c.execute("SELECT balance FROM users WHERE id=?", (uid,)).fetchone()["balance"]


def adjust(c, uid, delta):
    """Guthaben aendern (ohne Verlaufseintrag). Verhindert negatives Guthaben."""
    bal = get_balance(c, uid)
    if bal + delta < 0:
        raise ApiError("Nicht genug Guthaben.")
    c.execute("UPDATE users SET balance=balance+? WHERE id=?", (delta, uid))
    return bal + delta


def log_tx(c, uid, game, net, note=""):
    c.execute(
        "INSERT INTO transactions(user_id,game,delta,balance_after,note,ts) VALUES(?,?,?,?,?,?)",
        (uid, game, net, get_balance(c, uid), note, time.time()),
    )


def load_state(c, uid, game):
    r = c.execute("SELECT state FROM active_games WHERE user_id=? AND game=?", (uid, game)).fetchone()
    return json.loads(r["state"]) if r else None


def save_state(c, uid, game, st):
    c.execute(
        "INSERT OR REPLACE INTO active_games(user_id,game,state) VALUES(?,?,?)",
        (uid, game, json.dumps(st)),
    )


def clear_state(c, uid, game):
    c.execute("DELETE FROM active_games WHERE user_id=? AND game=?", (uid, game))


def check_bet(c, uid, bet, allowed=None, minimum=1):
    if not isinstance(bet, int) or isinstance(bet, bool):
        raise ApiError("Ungueltiger Einsatz.")
    if bet < minimum or bet > 1_000_000:
        raise ApiError("Ungueltiger Einsatz.")
    if allowed and bet not in allowed:
        raise ApiError("Ungueltiger Einsatz.")
    if bet > get_balance(c, uid):
        raise ApiError("Nicht genug Guthaben.")


# --------------------------------------------------------------------------
# Karten (fuer Blackjack & Poker): "As" = Ass Pik, "Td" = Zehn Karo
# --------------------------------------------------------------------------
RANKS = "23456789TJQKA"
SUITS = "shdc"


def new_deck():
    d = [r + s for r in RANKS for s in SUITS]
    random.SystemRandom().shuffle(d)
    return d


def rv(card):
    return RANKS.index(card[0]) + 2


# --------------------------------------------------------------------------
# SLOTS  (3 Walzen x 3 Reihen, 5 Gewinnlinien)
# --------------------------------------------------------------------------
SLOT_SYMBOLS = ["🍒", "🍋", "🍊", "🔔", "⭐", "💎", "7️⃣"]
SLOT_WEIGHTS = [30, 24, 20, 12, 8, 4, 2]
SLOT_PAY3 = [11, 17, 25, 50, 110, 280, 900]   # Faktor x Linieneinsatz bei 3 gleichen
SLOT_PAY2_CHERRY = 1                          # zwei Kirschen von links
SLOT_BETS = [5, 10, 25, 50, 100, 250]
SLOT_LINES = [
    [(0, 0), (0, 1), (0, 2)],
    [(1, 0), (1, 1), (1, 2)],
    [(2, 0), (2, 1), (2, 2)],
    [(0, 0), (1, 1), (2, 2)],
    [(2, 0), (1, 1), (0, 2)],
]


def slots_spin(c, uid, data):
    bet = data.get("bet")
    check_bet(c, uid, bet, allowed=SLOT_BETS)
    rng = random.SystemRandom()
    grid = [[rng.choices(range(7), SLOT_WEIGHTS)[0] for _ in range(3)] for _ in range(3)]
    line_bet = bet / 5
    wins, total = [], 0
    for i, line in enumerate(SLOT_LINES):
        s = [grid[r][col] for r, col in line]
        mult = 0
        cells = line
        if s[0] == s[1] == s[2]:
            mult = SLOT_PAY3[s[0]]
        elif s[0] == s[1] == 0:
            mult = SLOT_PAY2_CHERRY
            cells = line[:2]
        if mult:
            amount = int(line_bet * mult)
            total += amount
            wins.append({"line": i, "symbol": SLOT_SYMBOLS[s[0]], "mult": mult,
                         "amount": amount, "cells": [list(x) for x in cells]})
    adjust(c, uid, total - bet)
    log_tx(c, uid, "Slots", total - bet, f"Einsatz {bet}, Gewinn {total}")
    return {
        "grid": [[SLOT_SYMBOLS[x] for x in row] for row in grid],
        "wins": wins, "win": total, "bet": bet, "balance": get_balance(c, uid),
    }


# --------------------------------------------------------------------------
# ROULETTE (europaeisch, eine Null)
# --------------------------------------------------------------------------
RED = {1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36}


def roulette_spin(c, uid, data):
    bets = data.get("bets")
    if not isinstance(bets, list) or not bets or len(bets) > 60:
        raise ApiError("Platziere mindestens einen Chip.")
    total = 0
    clean = []
    for b in bets:
        t, v, a = b.get("type"), b.get("value"), b.get("amount")
        if not isinstance(a, int) or isinstance(a, bool) or a < 1 or a > 100_000:
            raise ApiError("Ungueltiger Einsatz.")
        if t == "num" and not (isinstance(v, int) and 0 <= v <= 36):
            raise ApiError("Ungueltige Zahl.")
        if t == "dozen" and v not in (1, 2, 3):
            raise ApiError("Ungueltiges Dutzend.")
        if t not in ("num", "dozen", "red", "black", "even", "odd", "low", "high"):
            raise ApiError("Ungueltige Wette.")
        total += a
        clean.append((t, v, a))
    if total > get_balance(c, uid):
        raise ApiError("Nicht genug Guthaben.")

    n = random.SystemRandom().randint(0, 36)
    color = "green" if n == 0 else ("red" if n in RED else "black")
    payout, results = 0, []
    for t, v, a in clean:
        mult = 0
        if t == "num" and v == n:
            mult = 36
        elif n != 0:
            if (t == "red" and color == "red") or (t == "black" and color == "black") \
                    or (t == "even" and n % 2 == 0) or (t == "odd" and n % 2 == 1) \
                    or (t == "low" and n <= 18) or (t == "high" and n >= 19):
                mult = 2
            elif t == "dozen" and (n - 1) // 12 + 1 == v:
                mult = 3
        payout += a * mult
        results.append({"type": t, "value": v, "amount": a, "won": a * mult})
    adjust(c, uid, payout - total)
    log_tx(c, uid, "Roulette", payout - total, f"Zahl {n}, Einsatz {total}, Gewinn {payout}")
    return {"number": n, "color": color, "bet": total, "payout": payout,
            "results": results, "balance": get_balance(c, uid)}


# --------------------------------------------------------------------------
# BLACKJACK gegen Dealer, mit zwei KI-Mitspielern am Tisch
# --------------------------------------------------------------------------
def bj_card_value(card):
    r = card[0]
    if r == "A":
        return 11
    if r in "TJQK":
        return 10
    return int(r)


def bj_value(hand):
    total = sum(bj_card_value(x) for x in hand)
    aces = sum(1 for x in hand if x[0] == "A")
    while total > 21 and aces:
        total -= 10
        aces -= 1
    soft = aces > 0
    return total, soft


def bj_is_blackjack(hand):
    return len(hand) == 2 and bj_value(hand)[0] == 21


def bj_bot_play(bot, deck, dealer_up):
    """Grundstrategie fuer die KI-Spieler."""
    up = bj_card_value(dealer_up)
    while True:
        total, soft = bj_value(bot["hand"])
        if total >= 21:
            break
        if soft:
            hit = total <= 17
        elif total <= 11:
            hit = True
        elif total == 12:
            hit = not (4 <= up <= 6)
        elif total <= 16:
            hit = up >= 7
        else:
            hit = False
        if not hit:
            break
        bot["hand"].append(deck.pop())


def bj_outcome(hand, dealer):
    pt, dt = bj_value(hand)[0], bj_value(dealer)[0]
    if pt > 21:
        return "lose"
    if bj_is_blackjack(hand) and not bj_is_blackjack(dealer):
        return "blackjack"
    if bj_is_blackjack(dealer) and not bj_is_blackjack(hand):
        return "lose"
    if dt > 21 or pt > dt:
        return "win"
    if pt == dt:
        return "push"
    return "lose"


def bj_finish(c, uid, st):
    deck = st["deck"]
    for bot in st["bots"]:
        bj_bot_play(bot, deck, st["dealer"][0])
    need_dealer = bj_value(st["player"])[0] <= 21 or any(bj_value(b["hand"])[0] <= 21 for b in st["bots"])
    if need_dealer:
        while bj_value(st["dealer"])[0] < 17:
            st["dealer"].append(deck.pop())
    for bot in st["bots"]:
        bot["result"] = bj_outcome(bot["hand"], st["dealer"])
    res = bj_outcome(st["player"], st["dealer"])
    bet = st["bet"]
    payout = {"blackjack": bet + bet * 3 // 2, "win": bet * 2, "push": bet, "lose": 0}[res]
    if payout:
        adjust(c, uid, payout)
    log_tx(c, uid, "Blackjack", payout - bet, f"{res}, Einsatz {bet}")
    st["phase"] = "done"
    st["result"] = res
    st["payout"] = payout
    save_state(c, uid, "blackjack", st)


def bj_view(c, uid, st):
    if st is None:
        return {"active": False, "balance": get_balance(c, uid)}
    done = st["phase"] == "done"
    dealer = st["dealer"] if done else [st["dealer"][0], "??"]
    return {
        "active": True, "phase": st["phase"], "bet": st["bet"],
        "player": st["player"], "player_total": bj_value(st["player"])[0],
        "dealer": dealer, "dealer_total": bj_value(st["dealer"])[0] if done else bj_card_value(st["dealer"][0]),
        "bots": [{"name": b["name"], "hand": b["hand"], "total": bj_value(b["hand"])[0],
                  "result": b.get("result")} for b in st["bots"]],
        "result": st.get("result"), "payout": st.get("payout"),
        "can_double": (not done) and len(st["player"]) == 2 and get_balance(c, uid) >= st["bet"],
        "balance": get_balance(c, uid),
    }


def bj_start(c, uid, data):
    st = load_state(c, uid, "blackjack")
    if st and st["phase"] == "player":
        raise ApiError("Es laeuft noch eine Runde.")
    bet = data.get("bet")
    check_bet(c, uid, bet, minimum=5)
    adjust(c, uid, -bet)
    deck = new_deck()
    st = {
        "deck": deck, "bet": bet, "phase": "player", "doubled": False,
        "player": [deck.pop(), deck.pop()],
        "bots": [{"name": "Max", "hand": [deck.pop(), deck.pop()]},
                 {"name": "Luna", "hand": [deck.pop(), deck.pop()]}],
        "dealer": [deck.pop(), deck.pop()],
    }
    save_state(c, uid, "blackjack", st)
    if bj_is_blackjack(st["player"]) or bj_is_blackjack(st["dealer"]):
        bj_finish(c, uid, st)
    return bj_view(c, uid, load_state(c, uid, "blackjack"))


def bj_action(c, uid, data):
    st = load_state(c, uid, "blackjack")
    if not st or st["phase"] != "player":
        raise ApiError("Keine laufende Runde.")
    act = data.get("action")
    if act == "hit":
        st["player"].append(st["deck"].pop())
        total = bj_value(st["player"])[0]
        if total >= 21:
            bj_finish(c, uid, st)
        else:
            save_state(c, uid, "blackjack", st)
    elif act == "stand":
        bj_finish(c, uid, st)
    elif act == "double":
        if len(st["player"]) != 2:
            raise ApiError("Verdoppeln geht nur mit zwei Karten.")
        adjust(c, uid, -st["bet"])   # wirft ApiError, wenn Guthaben fehlt
        st["bet"] *= 2
        st["player"].append(st["deck"].pop())
        bj_finish(c, uid, st)
    else:
        raise ApiError("Unbekannte Aktion.")
    return bj_view(c, uid, load_state(c, uid, "blackjack"))


def bj_state(c, uid, _data=None):
    return bj_view(c, uid, load_state(c, uid, "blackjack"))


# --------------------------------------------------------------------------
# POKER: Texas Hold'em (Fixed Limit) gegen zwei KI-Gegner
# --------------------------------------------------------------------------
HAND_NAMES = ["Hohe Karte", "Ein Paar", "Zwei Paare", "Drilling", "Straße",
              "Flush", "Full House", "Vierling", "Straight Flush"]


def rank5(cards):
    vals = sorted((rv(x) for x in cards), reverse=True)
    flush = len({x[1] for x in cards}) == 1
    uniq = sorted(set(vals), reverse=True)
    sh = 0
    if len(uniq) == 5:
        if uniq[0] - uniq[4] == 4:
            sh = uniq[0]
        elif uniq == [14, 5, 4, 3, 2]:
            sh = 5
    counts = {}
    for v in vals:
        counts[v] = counts.get(v, 0) + 1
    groups = sorted(counts.items(), key=lambda kv: (kv[1], kv[0]), reverse=True)
    shape = [g[1] for g in groups]
    order = tuple(g[0] for g in groups)
    if sh and flush:
        return (8, (sh,))
    if shape == [4, 1]:
        return (7, order)
    if shape == [3, 2]:
        return (6, order)
    if flush:
        return (5, tuple(vals))
    if sh:
        return (4, (sh,))
    if shape == [3, 1, 1]:
        return (3, order)
    if shape == [2, 2, 1]:
        return (2, order)
    if shape == [2, 1, 1, 1]:
        return (1, order)
    return (0, tuple(vals))


def best7(cards):
    return max(rank5(combo) for combo in itertools.combinations(cards, 5))


HAND_DAT = ["hoher Karte", "einem Paar", "zwei Paaren", "einem Drilling", "einer Straße",
            "einem Flush", "einem Full House", "einem Vierling", "einem Straight Flush"]


def hand_name(score):
    if score[0] == 8 and score[1][0] == 14:
        return "Royal Flush"
    return HAND_NAMES[score[0]]


def hand_dat(score):
    if score[0] == 8 and score[1][0] == 14:
        return "einem Royal Flush"
    return HAND_DAT[score[0]]


def cat_partial(cards):
    counts = {}
    for x in cards:
        counts[x[0]] = counts.get(x[0], 0) + 1
    m = sorted(counts.values(), reverse=True)
    if m[0] >= 4:
        return 7
    if m[0] == 3:
        return 3
    if m[0] == 2 and len(m) > 1 and m[1] == 2:
        return 2
    if m[0] == 2:
        return 1
    return 0


def pre_strength(hole):
    a, b = sorted((rv(hole[0]), rv(hole[1])), reverse=True)
    if a == b:
        return 0.5 + a / 28
    s = (a + b) / 28 * 0.6
    if hole[0][1] == hole[1][1]:
        s += 0.06
    if a - b <= 2:
        s += 0.05
    if a >= 12:
        s += 0.05
    return s


def strength(hole, board):
    if not board:
        return pre_strength(hole)
    cards = hole + board
    score = best7(cards)
    cat = score[0]
    base = [0.12, 0.45, 0.72, 0.8, 0.88, 0.9, 0.95, 0.99, 1.0][cat]
    if cat == 0:
        base += max(rv(x) for x in hole) / 14 * 0.1
    if cat == 1:
        top_board = max(rv(x) for x in board)
        pair_rank = score[1][0]
        hole_ranks = {rv(x) for x in hole}
        if pair_rank not in hole_ranks:
            base = 0.28
        elif pair_rank >= top_board:
            base = 0.6
        else:
            base = 0.48
    if 0 < cat <= 3 and cat <= cat_partial(board):
        base -= 0.25   # Die Hand liegt nur auf dem Board
    if len(board) < 5 and cat < 5:
        for s in SUITS:
            if sum(1 for x in cards if x[1] == s) == 4 and any(h[1] == s for h in hole):
                base += 0.14
                break
    if len(board) < 5 and cat < 4:
        vals = {rv(x) for x in cards}
        if 14 in vals:
            vals.add(1)
        for start in range(1, 11):
            if len(set(range(start, start + 5)) & vals) == 4:
                base += 0.1
                break
    return min(base, 1.0)


BOTS = [{"name": "Max", "aggr": 0.65}, {"name": "Luna", "aggr": 0.3}]
SEATS = ["Du", "Max", "Luna"]
MAX_RAISES = 4


def p_bet_size(st):
    return st["unit"] if st["street"] < 2 else st["unit"] * 2


def p_alive(st):
    return [i for i in range(3) if not st["folded"][i]]


def p_order_from(st, start):
    return [(start + k) % 3 for k in range(3) if not st["folded"][(start + k) % 3]]


def p_pay(c, uid, st, seat, amt):
    st["bets"][seat] += amt
    st["total"][seat] += amt
    st["pot"] += amt
    if seat == 0:
        adjust(c, uid, -amt)


def p_log(st, msg):
    st["log"].append(msg)


def p_apply(c, uid, st, seat, action):
    """action: fold | call | raise"""
    name = SEATS[seat]
    you = seat == 0
    if action == "fold":
        st["folded"][seat] = True
        p_log(st, f"{name} {'steigst' if you else 'steigt'} aus.")
    elif action == "call":
        amt = st["current"] - st["bets"][seat]
        if amt:
            p_pay(c, uid, st, seat, amt)
            p_log(st, f"{name} {'gehst' if you else 'geht'} mit ({amt}).")
        else:
            p_log(st, f"{name} {'checkst' if you else 'checkt'}.")
    elif action == "raise":
        level = st["current"] + p_bet_size(st)
        amt = level - st["bets"][seat]
        p_pay(c, uid, st, seat, amt)
        if st["current"] == 0:
            p_log(st, f"{name} setzt {level}.")
        else:
            p_log(st, f"{name} {'erhöhst' if you else 'erhöht'} auf {level}.")
        st["current"] = level
        st["raises"] += 1
        others = [(seat + k) % 3 for k in range(1, 3) if not st["folded"][(seat + k) % 3]]
        st["to_act"] = [seat] + others   # seat wird unten wieder entfernt
    order = st["to_act"]
    if order and order[0] == seat:
        order.pop(0)


def p_next_street(st):
    st["street"] += 1
    st["bets"] = [0, 0, 0]
    st["current"] = 0
    st["raises"] = 0
    d = st["deck"]
    if st["street"] == 1:
        st["board"] += [d.pop(), d.pop(), d.pop()]
        p_log(st, "— Flop —")
    elif st["street"] == 2:
        st["board"].append(d.pop())
        p_log(st, "— Turn —")
    elif st["street"] == 3:
        st["board"].append(d.pop())
        p_log(st, "— River —")
    if st["street"] <= 3:
        st["to_act"] = p_order_from(st, (st["dealer"] + 1) % 3)


def bot_decide(st, seat, c, uid):
    bot = BOTS[seat - 1]
    s = strength(st["hands"][seat], st["board"]) + random.uniform(-0.08, 0.08)
    to_call = st["current"] - st["bets"][seat]
    # Darf erhoeht werden? (Der Spieler muss die Erhoehung bezahlen koennen.)
    can_raise = st["raises"] < MAX_RAISES
    if can_raise and not st["folded"][0]:
        need = st["current"] + p_bet_size(st) - st["bets"][0]
        if need > get_balance(c, uid):
            can_raise = False
    if to_call == 0:
        if can_raise and (s > 0.62 - bot["aggr"] * 0.1 or random.random() < 0.07 * bot["aggr"]):
            return "raise"
        return "call"
    odds = to_call / (st["pot"] + to_call)
    if can_raise and s > 0.78 and random.random() < 0.45 + 0.35 * bot["aggr"]:
        return "raise"
    needed = 0.25 + odds * 0.9 - bot["aggr"] * 0.08
    return "call" if s >= needed else "fold"


def p_showdown(c, uid, st):
    alive = p_alive(st)
    if len(alive) == 1:
        winners, reveal = alive, False
        scores = {}
    else:
        while len(st["board"]) < 5:
            st["board"].append(st["deck"].pop())
        scores = {i: best7(st["hands"][i] + st["board"]) for i in alive}
        top = max(scores.values())
        winners = [i for i in alive if scores[i] == top]
        reveal = True
    pot = st["pot"]
    share, rest = divmod(pot, len(winners))
    payouts = [0, 0, 0]
    for k, w in enumerate(winners):
        payouts[w] = share + (rest if k == 0 else 0)
    if payouts[0]:
        adjust(c, uid, payouts[0])
    net = payouts[0] - st["total"][0]
    log_tx(c, uid, "Poker", net, f"Pot {pot}")
    names = " & ".join(SEATS[w] for w in winners)
    if len(winners) > 1:
        verb = "gewinnen"
    else:
        verb = "gewinnst" if winners[0] == 0 else "gewinnt"
    if len(winners) == 1 and winners[0] in scores:
        p_log(st, f"{names} {verb} {pot} mit {hand_dat(scores[winners[0]])}.")
    else:
        p_log(st, f"{names} {verb} {pot}.")
    st["over"] = True
    st["reveal"] = reveal
    st["winners"] = winners
    st["payouts"] = payouts
    st["net"] = net
    st["hand_names"] = {str(i): hand_name(s) for i, s in scores.items()}
    save_state(c, uid, "poker", st)


def p_advance(c, uid, st):
    """Laesst die KIs spielen, bis der Spieler dran ist oder die Hand endet."""
    guard = 0
    while not st["over"] and guard < 200:
        guard += 1
        if len(p_alive(st)) == 1:
            p_showdown(c, uid, st)
            return
        if not st["to_act"]:
            if st["street"] >= 3:
                p_showdown(c, uid, st)
                return
            p_next_street(st)
            continue
        seat = st["to_act"][0]
        if seat == 0:
            if st["folded"][0]:
                st["to_act"].pop(0)
                continue
            break
        act = bot_decide(st, seat, c, uid)
        p_apply(c, uid, st, seat, act)
    save_state(c, uid, "poker", st)


def p_view(c, uid, st):
    bal = get_balance(c, uid)
    if st is None:
        return {"active": False, "balance": bal}
    over = st["over"]
    your_turn = (not over) and bool(st["to_act"]) and st["to_act"][0] == 0
    to_call = st["current"] - st["bets"][0]
    can_raise = your_turn and st["raises"] < MAX_RAISES and to_call + p_bet_size(st) <= bal
    seats = []
    for i in (1, 2):
        show = over and st.get("reveal") and not st["folded"][i]
        seats.append({
            "name": SEATS[i], "folded": st["folded"][i], "bet": st["bets"][i], "total": st["total"][i],
            "cards": st["hands"][i] if show else ["??", "??"],
            "hand_name": st.get("hand_names", {}).get(str(i)) if show else None,
            "winner": over and i in st["winners"],
            "dealer": st["dealer"] == i,
        })
    you_name = None
    if st["board"]:
        you_name = hand_name(best7(st["hands"][0] + st["board"]))
    return {
        "active": True, "over": over, "street": st["street"], "unit": st["unit"],
        "board": st["board"], "pot": st["pot"], "you": st["hands"][0],
        "you_folded": st["folded"][0], "you_bet": st["total"][0],
        "you_dealer": st["dealer"] == 0, "you_hand": you_name,
        "seats": seats, "your_turn": your_turn,
        "to_call": to_call if your_turn else 0,
        "can_raise": can_raise, "raise_size": p_bet_size(st),
        "can_afford_call": to_call <= bal,
        "log": st["log"][-40:], "net": st.get("net"), "you_won": over and 0 in st["winners"],
        "winners": [SEATS[w] for w in st["winners"]] if over else [],
        "balance": bal,
    }


def poker_start(c, uid, data):
    st = load_state(c, uid, "poker")
    if st and not st["over"]:
        raise ApiError("Es laeuft noch eine Hand.")
    unit = data.get("unit")
    if unit not in (10, 50, 250):
        raise ApiError("Ungueltige Stufe.")
    if get_balance(c, uid) < unit * 12:
        raise ApiError(f"Für diese Stufe brauchst du mindestens {unit * 12} Guthaben.")
    hands_played = c.execute("SELECT poker_hands FROM users WHERE id=?", (uid,)).fetchone()["poker_hands"]
    c.execute("UPDATE users SET poker_hands=poker_hands+1 WHERE id=?", (uid,))
    dealer = hands_played % 3
    deck = new_deck()
    st = {
        "deck": deck, "unit": unit, "dealer": dealer, "street": 0, "over": False,
        "hands": [[deck.pop(), deck.pop()] for _ in range(3)],
        "board": [], "pot": 0, "bets": [0, 0, 0], "total": [0, 0, 0],
        "folded": [False] * 3, "current": 0, "raises": 1, "to_act": [], "log": [],
    }
    sb, bb = (dealer + 1) % 3, (dealer + 2) % 3
    p_log(st, "Du hast den Dealer-Button." if dealer == 0 else f"{SEATS[dealer]} hat den Dealer-Button.")
    p_pay(c, uid, st, sb, unit // 2)
    p_pay(c, uid, st, bb, unit)
    p_log(st, f"{SEATS[sb]}: Small Blind {unit // 2}, {SEATS[bb]}: Big Blind {unit}.")
    st["current"] = unit
    st["to_act"] = p_order_from(st, (bb + 1) % 3)
    p_advance(c, uid, st)
    return p_view(c, uid, load_state(c, uid, "poker"))


def poker_action(c, uid, data):
    st = load_state(c, uid, "poker")
    if not st or st["over"]:
        raise ApiError("Keine laufende Hand.")
    if not st["to_act"] or st["to_act"][0] != 0:
        raise ApiError("Du bist nicht dran.")
    act = data.get("action")
    bal = get_balance(c, uid)
    to_call = st["current"] - st["bets"][0]
    if act == "fold":
        p_apply(c, uid, st, 0, "fold")
    elif act == "call":
        if to_call > bal:
            raise ApiError("Nicht genug Guthaben.")
        p_apply(c, uid, st, 0, "call")
    elif act == "raise":
        if st["raises"] >= MAX_RAISES:
            raise ApiError("Maximale Anzahl Erhöhungen erreicht.")
        if to_call + p_bet_size(st) > bal:
            raise ApiError("Nicht genug Guthaben.")
        p_apply(c, uid, st, 0, "raise")
    else:
        raise ApiError("Unbekannte Aktion.")
    p_advance(c, uid, st)
    return p_view(c, uid, load_state(c, uid, "poker"))


def poker_state(c, uid, _data=None):
    return p_view(c, uid, load_state(c, uid, "poker"))


# --------------------------------------------------------------------------
# Konto, Bonus, Rangliste, Verlauf
# --------------------------------------------------------------------------
def user_info(c, uid):
    u = c.execute("SELECT username,balance,last_bonus FROM users WHERE id=?", (uid,)).fetchone()
    wait = max(0, int(u["last_bonus"] + BONUS_COOLDOWN - time.time()))
    return {"username": u["username"], "balance": u["balance"], "bonus_in": wait,
            "bonus_amount": DAILY_BONUS, "rescue_amount": RESCUE_AMOUNT}


def make_session(c, uid):
    token = secrets.token_urlsafe(32)
    c.execute("INSERT INTO sessions(token,user_id,created) VALUES(?,?,?)", (token, uid, time.time()))
    return token


def register(c, _uid, data):
    name = str(data.get("username", "")).strip()
    pw = str(data.get("password", ""))
    if not re.fullmatch(r"[A-Za-z0-9_äöüÄÖÜß\-]{3,20}", name):
        raise ApiError("Benutzername: 3–20 Zeichen (Buchstaben, Zahlen, _ und -).")
    if len(pw) < 4:
        raise ApiError("Das Passwort braucht mindestens 4 Zeichen.")
    salt = secrets.token_hex(16)
    try:
        cur = c.execute(
            "INSERT INTO users(username,pw_hash,salt,balance,created) VALUES(?,?,?,?,?)",
            (name, hash_pw(pw, salt), salt, START_BALANCE, time.time()),
        )
    except sqlite3.IntegrityError:
        raise ApiError("Dieser Benutzername ist schon vergeben.")
    uid = cur.lastrowid
    log_tx(c, uid, "Konto", START_BALANCE, "Startguthaben")
    return {"token": make_session(c, uid), **user_info(c, uid)}


def login(c, _uid, data):
    name = str(data.get("username", "")).strip()
    pw = str(data.get("password", ""))
    u = c.execute("SELECT * FROM users WHERE username=?", (name,)).fetchone()
    if not u or not secrets.compare_digest(u["pw_hash"], hash_pw(pw, u["salt"])):
        raise ApiError("Benutzername oder Passwort stimmt nicht.", 401)
    return {"token": make_session(c, u["id"]), **user_info(c, u["id"])}


def logout(c, uid, data):
    return {"ok": True}


def me(c, uid, _data=None):
    return user_info(c, uid)


def bonus(c, uid, _data):
    u = c.execute("SELECT last_bonus FROM users WHERE id=?", (uid,)).fetchone()
    if time.time() - u["last_bonus"] < BONUS_COOLDOWN:
        raise ApiError("Der Tagesbonus ist noch nicht bereit.")
    adjust(c, uid, DAILY_BONUS)
    c.execute("UPDATE users SET last_bonus=? WHERE id=?", (time.time(), uid))
    log_tx(c, uid, "Bonus", DAILY_BONUS, "Tagesbonus")
    return user_info(c, uid)


def rescue(c, uid, _data):
    if get_balance(c, uid) >= 5:
        raise ApiError("Du hast noch genug Guthaben.")
    for g in ("blackjack", "poker"):
        st = load_state(c, uid, g)
        if st and not (st.get("over") or st.get("phase") == "done"):
            raise ApiError("Beende erst deine laufende Runde.")
    adjust(c, uid, RESCUE_AMOUNT)
    log_tx(c, uid, "Bonus", RESCUE_AMOUNT, "Notgroschen")
    return user_info(c, uid)


def leaderboard(c, uid, _data=None):
    rows = c.execute("SELECT username,balance FROM users ORDER BY balance DESC LIMIT 10").fetchall()
    return {"rows": [dict(r) for r in rows]}


def history(c, uid, _data=None):
    rows = c.execute(
        "SELECT game,delta,balance_after,note,ts FROM transactions WHERE user_id=? ORDER BY id DESC LIMIT 40",
        (uid,),
    ).fetchall()
    return {"rows": [dict(r) for r in rows]}


# Route -> (Funktion, braucht Login)
ROUTES = {
    ("POST", "/api/register"): (register, False),
    ("POST", "/api/login"): (login, False),
    ("POST", "/api/logout"): (logout, True),
    ("GET", "/api/me"): (me, True),
    ("POST", "/api/bonus"): (bonus, True),
    ("POST", "/api/rescue"): (rescue, True),
    ("GET", "/api/leaderboard"): (leaderboard, True),
    ("GET", "/api/history"): (history, True),
    ("POST", "/api/slots/spin"): (slots_spin, True),
    ("POST", "/api/roulette/spin"): (roulette_spin, True),
    ("POST", "/api/blackjack/start"): (bj_start, True),
    ("POST", "/api/blackjack/action"): (bj_action, True),
    ("GET", "/api/blackjack/state"): (bj_state, True),
    ("POST", "/api/poker/start"): (poker_start, True),
    ("POST", "/api/poker/action"): (poker_action, True),
    ("GET", "/api/poker/state"): (poker_state, True),
}


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "CasinoRoyale/1.0"

    def log_message(self, fmt, *args):
        pass  # ruhige Konsole

    def _send(self, code, payload, ctype="application/json; charset=utf-8"):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _api(self, method):
        path = urlparse(self.path).path
        route = ROUTES.get((method, path))
        if not route:
            return self._send(404, {"error": "Nicht gefunden."})
        fn, needs_auth = route
        try:
            data = {}
            if method == "POST":
                n = int(self.headers.get("Content-Length") or 0)
                if n > 100_000:
                    raise ApiError("Anfrage zu gross.")
                raw = self.rfile.read(n) if n else b"{}"
                try:
                    data = json.loads(raw or b"{}")
                except ValueError:
                    raise ApiError("Ungueltiges JSON.")
                if not isinstance(data, dict):
                    raise ApiError("Ungueltiges JSON.")
            with LOCK:
                conn = db()
                try:
                    uid = None
                    if needs_auth:
                        auth = self.headers.get("Authorization", "")
                        token = auth[7:] if auth.startswith("Bearer ") else ""
                        s = conn.execute("SELECT user_id FROM sessions WHERE token=?", (token,)).fetchone()
                        if not s:
                            raise ApiError("Bitte melde dich neu an.", 401)
                        uid = s["user_id"]
                    result = fn(conn, uid, data)
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
                finally:
                    conn.close()
            self._send(200, result)
        except ApiError as e:
            self._send(e.code, {"error": e.msg})
        except Exception as e:  # unerwarteter Fehler
            print("Fehler:", repr(e), file=sys.stderr)
            self._send(500, {"error": "Serverfehler."})

    def do_GET(self):
        path = urlparse(self.path).path
        if path.startswith("/api/"):
            return self._api("GET")
        if path in ("/", "/index.html"):
            with open(os.path.join(BASE, "index.html"), "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        self._send(404, {"error": "Nicht gefunden."})

    def do_POST(self):
        self._api("POST")


def main():
    init_db()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Casino läuft auf http://localhost:{PORT}  (Strg+C zum Beenden)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nBis bald!")


if __name__ == "__main__":
    main()
