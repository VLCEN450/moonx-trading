# -*- coding: utf-8 -*-
"""
Routine horaire de trading SMC — COMPTE DEMO MoonX uniquement.
Stratégie : sweep de liquidité M5 + CHoCH, filtré par biais H1.
Futures : 5% du wallet futures en marge, levier x20, RR 1:3.
Forex (XAUUSD) : risque ~2% du wallet forex, RR 1:3.
Gestion : break-even à +1R, prise partielle 25-50% à +2R (futures).
Rééquilibrage : USDT spot -> 50/50 futures/forex, puis équilibrage.
"""
import json, os, time, urllib.request, datetime

# Le token est lu depuis la variable d'environnement MOONX_TOKEN (secret GitHub).
# Ne JAMAIS écrire le token en dur ici.
TOKEN = os.environ.get("MOONX_TOKEN", "")
if not TOKEN:
    raise SystemExit("ERREUR: variable d'environnement MOONX_TOKEN manquante (secret GitHub).")
MCP_URL = "https://api.moon-x.io/mcp?token=" + TOKEN
FUTURES_SYMS = ["BTC", "ETH", "SOL", "HYPE", "INJ"]
FOREX_SYMS = ["XAUUSD"]
LEVERAGE = 20
FUT_MARGIN_PCT = 0.05      # 5% du wallet futures par trade
FOREX_RISK_PCT = 0.02      # ~2% du wallet forex risqué par trade
RR = 3.0
XAU_OZ_PER_LOT = 100.0
STALE_SECONDS = 15 * 60    # bougie M5 plus vieille que 15 min -> données périmées
REBAL_THRESHOLD = 0.20     # rééquilibre si écart > 20% du total (évite les micro-transferts coûteux)
MIN_TRANSFER = 1.0         # USDT

_actions, _errors = [], []

def log(msg):
    _actions.append(msg)

def err(msg):
    _errors.append(msg)

def mcp(tool, args=None, retries=2):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": args or {}}}).encode()
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(MCP_URL, data=body, headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) KimiWork/1.0"})
            with urllib.request.urlopen(req, timeout=40) as r:
                d = json.loads(r.read())
            if "error" in d:
                raise RuntimeError("%s: %s" % (tool, d["error"]))
            txt = d["result"]["content"][0]["text"]
            try:
                return json.loads(txt)
            except Exception:
                return {"raw": txt}
        except Exception as e:
            if attempt >= retries:
                raise
            time.sleep(2 * (attempt + 1))

def g(d, *keys, default=None):
    """Lecture défensive d'un champ parmi plusieurs noms possibles."""
    if not isinstance(d, dict):
        return default
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default

def fnum(x, default=0.0):
    try:
        return float(x)
    except Exception:
        return default

# ---------- état persistant ----------
# Dans GitHub Actions, l'état est stocké dans le dossier state/ du dépôt
# et re-commité par le workflow après chaque passage (persistance entre runs).
def state_path(ctx=None):
    base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state")
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, "trading_state.json")

def load_state(ctx):
    try:
        with open(state_path(ctx), "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {"positions": {}, "planned": {}}

def save_state(ctx, st):
    try:
        with open(state_path(ctx), "w", encoding="utf-8") as fh:
            json.dump(st, fh, ensure_ascii=False, indent=1)
    except Exception as e:
        err("sauvegarde état: %s" % e)

# ---------- bougies & structure ----------
def parse_candles(raw):
    if isinstance(raw, dict):
        raw = g(raw, "candles", "data", "items", default=[])
    out = []
    for c in raw or []:
        try:
            if isinstance(c, dict):
                t = g(c, "timestamp", "time", "t", "openTime")
                out.append({"t": fnum(t), "open": fnum(g(c, "open", "o")),
                            "high": fnum(g(c, "high", "h")), "low": fnum(g(c, "low", "l")),
                            "close": fnum(g(c, "close", "c"))})
            else:
                out.append({"t": fnum(c[0]), "open": fnum(c[1]), "high": fnum(c[2]),
                            "low": fnum(c[3]), "close": fnum(c[4])})
        except Exception:
            pass
    return out

def fresh(candles, interval="5m"):
    if not candles:
        return False
    t = candles[-1]["t"]
    if t < 1e12:
        t *= 1000.0
    age = time.time() * 1000.0 - t
    # la dernière bougie est la bougie EN COURS : son timestamp peut dater
    # du début de la période (1h pour H1, 5min pour M5) + marge réseau
    period_ms = {"5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240}.get(interval, 60) * 60 * 1000.0
    return age < period_ms + STALE_SECONDS * 1000.0

def swings(c, n=3):
    hi, lo = [], []
    for i in range(n, len(c) - n):
        w = c[i - n:i + n + 1]
        if c[i]["high"] == max(x["high"] for x in w):
            hi.append((i, c[i]["high"]))
        if c[i]["low"] == min(x["low"] for x in w):
            lo.append((i, c[i]["low"]))
    return hi, lo

def h1_bias(c):
    if len(c) < 60:
        return "indéterminé"
    closes = [x["close"] for x in c]
    ema20 = sum(closes[-20:]) / 20.0
    ema50 = sum(closes[-50:]) / 50.0
    hi, lo = swings(c, 3)
    struct = "neutre"
    if len(hi) >= 2 and len(lo) >= 2:
        if hi[-1][1] > hi[-2][1] and lo[-1][1] > lo[-2][1]:
            struct = "haussière"
        elif hi[-1][1] < hi[-2][1] and lo[-1][1] < lo[-2][1]:
            struct = "baissière"
    px = closes[-1]
    if struct == "haussière" and px > ema20 > ema50:
        return "HAUSSIER"
    if struct == "baissière" and px < ema20 < ema50:
        return "BAISSIER"
    return "neutre"

def m5_setup(c, lookback_candles=12):
    """Sweep de liquidité + CHoCH détecté sur les N dernières bougies M5.
    Retourne un ordre limite au niveau de retracement (50% de la jambe d'impulsion),
    ou None si aucun setup valide / déjà trop tard pour entrer."""
    if len(c) < 45:
        return None
    best = None
    # on cherche le sweep le plus récent parmi les N dernières bougies
    for k in range(2, lookback_candles + 1):
        idx = len(c) - k
        if idx < 22:
            break
        sig = c[idx]
        prior = c[idx - 21:idx]          # 20 bougies avant le sweep
        prev_hi = max(x["high"] for x in prior)
        prev_lo = min(x["low"] for x in prior)
        after = c[idx + 1:]              # bougies post-sweep

        # Sweep des plus hauts + rejet -> SHORT
        if sig["high"] > prev_hi and sig["close"] < prev_hi:
            minor_lo = min(x["low"] for x in c[max(0, idx - 5):idx + 1])
            choch = any(x["close"] < minor_lo for x in after)  # CHoCH baissier
            if choch:
                sl = max([sig["high"]] + [x["high"] for x in after]) * 1.001
                impulse_lo = min(x["low"] for x in after) if after else sig["low"]
                entry = impulse_lo + 0.5 * (sig["high"] - impulse_lo)  # retracement 50%
                entry = min(entry, prev_hi * 0.9995)
                cur = c[-1]["close"]
                tp = entry - RR * (sl - entry)
                # trop tard si le prix a déjà dépassé l'entrée vers le TP ou touché le SL
                if cur < entry * 1.002 and cur > tp * 1.001 and c[-1]["high"] < sl:
                    best = {"dir": "short", "entry": entry, "sl": sl, "tp": tp}
                    break

        # Sweep des plus bas + rejet -> LONG
        if sig["low"] < prev_lo and sig["close"] > prev_lo:
            minor_hi = max(x["high"] for x in c[max(0, idx - 5):idx + 1])
            choch = any(x["close"] > minor_hi for x in after)  # CHoCH haussier
            if choch:
                sl = min([sig["low"]] + [x["low"] for x in after]) * 0.999
                impulse_hi = max(x["high"] for x in after) if after else sig["high"]
                entry = impulse_hi - 0.5 * (impulse_hi - sig["low"])
                entry = max(entry, prev_lo * 1.0005)
                cur = c[-1]["close"]
                tp = entry + RR * (entry - sl)
                if cur > entry * 0.998 and cur < tp * 0.999 and c[-1]["low"] > sl:
                    best = {"dir": "long", "entry": entry, "sl": sl, "tp": tp}
                    break
    return best

# ---------- routine ----------
def run(ctx):
    ran_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    st = load_state(ctx)
    bias_map, positions_out, orders_placed = {}, [], []

    # 1. COMPTE ------------------------------------------------------
    try:
        ov = mcp("get_account_overview")
    except Exception as e:
        return {"artifact": {"summary": "Échec lecture compte: %s" % e, "ranAt": ran_at,
                             "actions": [], "errors": [str(e)], "balances": {}, "bias": {},
                             "positions": [], "ordersPlaced": []}}
    spot = ov.get("spotBalances") or {}
    fut_bal = ov.get("futuresBalances") or {}
    fx = ov.get("forexWallet") or {}
    spot_usdt = fnum(g(spot, "USDT", default=0))
    fut_free = fnum(g(fut_bal, "USDT", default=fnum(g(fut_bal, "usdt", default=0))))
    fx_free = fnum(g(fx, "freeMargin", default=0))
    fx_equity = fnum(g(fx, "equity", default=0))
    fx_margin = fnum(g(fx, "margin", default=0))

    # positions / ordres existants
    try:
        fut_pos = mcp("list_futures_positions") or []
    except Exception as e:
        fut_pos = []; err("list_futures_positions: %s" % e)
    try:
        fx_pos = mcp("list_forex_positions") or []
    except Exception as e:
        fx_pos = []; err("list_forex_positions: %s" % e)
    try:
        fut_orders = mcp("list_futures_orders") or []
    except Exception as e:
        fut_orders = []; err("list_futures_orders: %s" % e)
    try:
        fx_orders = mcp("list_forex_orders") or []
    except Exception as e:
        fx_orders = []; err("list_forex_orders: %s" % e)

    fut_margin_used = sum(fnum(g(p, "margin", "initialMargin", "usedMargin")) for p in fut_pos)
    fut_equity = fut_free + fut_margin_used

    # 2. RÉÉQUILIBRAGE ----------------------------------------------
    if spot_usdt >= MIN_TRANSFER * 2:
        half = round(spot_usdt / 2.0, 2)
        for dest in ("futures", "forex"):
            try:
                mcp("transfer_funds", {"amount": half, "from": "spot", "to": dest})
                log("transfert %.2f USDT spot -> %s" % (half, dest))
                if dest == "futures":
                    fut_free += half; fut_equity += half
                else:
                    fx_free += half; fx_equity += half
            except Exception as e:
                err("transfert spot->%s: %s" % (dest, e))
        spot_usdt = 0.0

    total_eq = fut_equity + fx_equity
    if total_eq > 0:
        diff = (fut_equity - fx_equity) / 2.0  # >0 : futures trop lourd
        if abs(diff) > REBAL_THRESHOLD * total_eq and abs(diff) >= MIN_TRANSFER:
            move = round(abs(diff), 2)
            if diff > 0 and fut_free >= move:
                try:
                    mcp("transfer_funds", {"amount": move, "from": "futures", "to": "forex"})
                    log("rééquilibrage %.2f USDT futures -> forex" % move)
                    fut_free -= move; fx_free += move
                except Exception as e:
                    err("rééquilibrage futures->forex: %s" % e)
            elif diff < 0 and fx_free >= move:
                try:
                    mcp("transfer_funds", {"amount": move, "from": "forex", "to": "futures"})
                    log("rééquilibrage %.2f USDT forex -> futures" % move)
                    fx_free -= move; fut_free += move
                except Exception as e:
                    err("rééquilibrage forex->futures: %s" % e)

    # 3. GESTION DES POSITIONS FUTURES ------------------------------
    try:
        tpsl = mcp("list_futures_tp_sl_orders", {"status": "active"}) or []
    except Exception:
        tpsl = []
    sl_by_pos = {}
    for o in (tpsl if isinstance(tpsl, list) else []):
        pid = g(o, "positionId", "position_id")
        typ = str(g(o, "type", "kind", default="")).lower()
        if pid and ("sl" in typ or "stop" in typ):
            sl_by_pos[pid] = fnum(g(o, "triggerPrice", "stopLossPrice", "price"))

    for p in fut_pos:
        pid = str(g(p, "positionId", "id", "position_id", "_id", default=""))
        sym = str(g(p, "symbol", "token", default="")).upper().replace("USDT", "").replace("-PERP", "")
        side = str(g(p, "side", "direction", default="")).lower()
        entry = fnum(g(p, "entryPrice", "avgEntryPrice", "openPrice"))
        mark = fnum(g(p, "markPrice", "currentPrice", "lastPrice"))
        if not mark and sym:
            try:
                mark = fnum(g(mcp("get_price", {"symbol": sym}), "price"))
            except Exception:
                pass
        pnl = fnum(g(p, "unrealizedPnl", "pnl", "unrealizedPnlUsdt"))
        margin = fnum(g(p, "margin", "initialMargin", "usedMargin"))
        positions_out.append({"kind": "futures", "symbol": sym, "side": side,
                              "entry": entry, "mark": mark, "pnl": pnl})
        if not pid or not entry or not mark:
            continue
        pst = st["positions"].setdefault(pid, {"be": False, "partial": False, "sl0": None, "sym": sym})
        sl0 = pst.get("sl0") or sl_by_pos.get(pid)
        if sl0 and not pst.get("sl0"):
            pst["sl0"] = sl0
        if not sl0:
            # pas de SL connu -> on en attache un à -0.8% / TP +2.4% (RR 1:3)
            sl0 = entry * (0.992 if side == "long" else 1.008)
            tp = entry + (RR * (entry - sl0) if side == "long" else -RR * (sl0 - entry))
            try:
                mcp("set_futures_tp_sl", {"positionId": pid, "stopLossPrice": round(sl0, 8),
                                          "takeProfitPrice": round(tp, 8)})
                log("%s: SL/TP attachés (SL %.6g / TP %.6g)" % (sym, sl0, tp))
                pst["sl0"] = sl0
            except Exception as e:
                err("set TP/SL %s: %s" % (sym, e))
            continue
        risk = abs(entry - sl0)
        r_mult = ((mark - entry) if side == "long" else (entry - mark)) / risk if risk > 0 else 0
        # +1R -> break-even
        if r_mult >= 1.0 and not pst.get("be"):
            try:
                mcp("set_futures_tp_sl", {"positionId": pid, "stopLossPrice": entry})
                log("%s: SL déplacé au break-even (+%.1fR)" % (sym, r_mult))
                pst["be"] = True
            except Exception as e:
                err("break-even %s: %s" % (sym, e))
        # +2R -> prise partielle 40%
        if r_mult >= 2.0 and not pst.get("partial"):
            try:
                mcp("close_futures_position", {"positionId": pid, "percentage": 40})
                log("%s: prise partielle 40%% (+%.1fR), reste vers TP 1:3" % (sym, r_mult))
                pst["partial"] = True
            except Exception as e:
                err("prise partielle %s: %s" % (sym, e))

    # gestion forex : break-even seulement (pas de clôture partielle dispo)
    for p in fx_pos:
        pid = str(g(p, "positionId", "id", "ticket", "_id", default=""))
        pair = str(g(p, "pairId", "symbol", "pair", default="")).upper()
        side = str(g(p, "side", "direction", default="")).lower()
        entry = fnum(g(p, "entryPrice", "openPrice", "entry"))
        cur = fnum(g(p, "currentPrice", "markPrice", "closePrice"))
        if not cur and pair:
            try:
                cur = fnum(g(mcp("get_price", {"symbol": pair}), "price"))
            except Exception:
                pass
        pnl = fnum(g(p, "pnl", "profit", "unrealizedPnl"))
        positions_out.append({"kind": "forex", "symbol": pair, "side": side,
                              "entry": entry, "mark": cur, "pnl": pnl})
        if not pid or not entry or not cur:
            continue
        pst = st["positions"].setdefault(pid, {"be": False, "sl0": None, "sym": pair})
        sl0 = pst.get("sl0")
        if not sl0:
            sl0 = entry * (0.998 if side == "buy" else 1.002)
            pst["sl0"] = sl0
            try:
                mcp("set_forex_tp_sl", {"positionId": pid, "stopLoss": sl0,
                                        "takeProfit": entry + (RR * (entry - sl0) if side == "buy" else -RR * (sl0 - entry))})
                log("%s: SL/TP forex attachés" % pair)
            except Exception as e:
                err("TP/SL forex %s: %s" % (pair, e))
            continue
        risk = abs(entry - sl0)
        r_mult = ((cur - entry) if side == "buy" else (entry - cur)) / risk if risk > 0 else 0
        if r_mult >= 1.0 and not pst.get("be"):
            try:
                mcp("set_forex_tp_sl", {"positionId": pid, "stopLoss": entry})
                log("%s: SL forex au break-even (+%.1fR)" % (pair, r_mult))
                pst["be"] = True
            except Exception as e:
                err("break-even forex %s: %s" % (pair, e))

    open_syms_fut = {str(g(p, "symbol", "token", default="")).upper().replace("USDT", "").replace("-PERP", "") for p in fut_pos}
    open_pairs_fx = {str(g(p, "pairId", "symbol", "pair", default="")).upper() for p in fx_pos}
    order_syms_fut = {str(g(o, "symbol", "token", default="")).upper().replace("USDT", "").replace("-PERP", "") for o in fut_orders}
    order_pairs_fx = {str(g(o, "pairId", "symbol", "pair", default="")).upper() for o in fx_orders}

    # positions issues d'ordres limites remplis : attacher SL/TP planifiés
    for pid, pst in list(st["positions"].items()):
        pass  # géré ci-dessus via sl0

    # 4. SCAN SETUPS + ORDRES LIMITES --------------------------------
    for sym in FUTURES_SYMS + FOREX_SYMS:
        is_fx = sym in FOREX_SYMS
        try:
            h1 = parse_candles(mcp("get_candles", {"symbol": sym, "interval": "1h", "limit": 120}))
            m5 = parse_candles(mcp("get_candles", {"symbol": sym, "interval": "5m", "limit": 60}))
        except Exception as e:
            err("bougies %s: %s" % (sym, e))
            bias_map[sym] = "erreur données"
            continue
        if not fresh(h1, "1h") or not fresh(m5, "5m"):
            bias_map[sym] = "données périmées"
            continue
        bias = h1_bias(h1)
        bias_map[sym] = bias
        has_pos = sym in (open_pairs_fx if is_fx else open_syms_fut)
        has_order = sym in (order_pairs_fx if is_fx else order_syms_fut)
        if has_pos or has_order:
            continue
        setup = m5_setup(m5)
        if not setup:
            continue
        blocked = (bias == "HAUSSIER" and setup["dir"] == "short") or \
                  (bias == "BAISSIER" and setup["dir"] == "long")
        if blocked:
            log("%s: setup %s ignoré (biais H1 %s opposé)" % (sym, setup["dir"], bias))
            continue
        if is_fx:
            if fx_free < 0.5:
                log("XAUUSD: setup %s mais wallet forex insuffisant (%.2f)" % (setup["dir"], fx_free))
                continue
            risk_usd = fx_equity * FOREX_RISK_PCT
            sl_dist = abs(setup["entry"] - setup["sl"])
            lots = round(risk_usd / (sl_dist * XAU_OZ_PER_LOT), 2) if sl_dist > 0 else 0
            if lots < 0.01:
                log("XAUUSD: lots calculés < 0.01, ordre ignoré")
                continue
            try:
                mcp("open_forex_limit_order", {"pairId": sym, "side": "buy" if setup["dir"] == "long" else "sell",
                                               "lots": lots, "limitPrice": round(setup["entry"], 3),
                                               "takeProfit": round(setup["tp"], 3), "stopLoss": round(setup["sl"], 3)})
                log("XAUUSD: ordre limite %s %.2f lots @ %.3f (SL %.3f / TP %.3f)" %
                    (setup["dir"], lots, setup["entry"], setup["sl"], setup["tp"]))
                orders_placed.append({"symbol": sym, "dir": setup["dir"], "entry": setup["entry"],
                                      "sl": setup["sl"], "tp": setup["tp"], "lots": lots})
                st["planned"][sym] = setup
            except Exception as e:
                err("ordre limite XAUUSD: %s" % e)
        else:
            if fut_free < 0.5:
                log("%s: setup %s mais wallet futures insuffisant (%.2f USDT)" % (sym, setup["dir"], fut_free))
                continue
            margin = round(max(fut_free * FUT_MARGIN_PCT, 0.5), 2)
            if margin > fut_free:
                margin = round(fut_free, 2)
            try:
                mcp("open_futures_limit_order", {"symbol": sym, "side": setup["dir"],
                                                 "marginUsdt": margin, "leverage": LEVERAGE,
                                                 "limitPrice": round(setup["entry"], 8)})
                log("%s: ordre limite %s @ %.6g, marge %.2f USDT x%d (SL/TP seront attachés au remplissage: SL %.6g / TP %.6g)" %
                    (sym, setup["dir"], setup["entry"], margin, LEVERAGE, setup["sl"], setup["tp"]))
                orders_placed.append({"symbol": sym, "dir": setup["dir"], "entry": setup["entry"],
                                      "sl": setup["sl"], "tp": setup["tp"], "margin": margin})
                st["planned"][sym] = setup
            except Exception as e:
                err("ordre limite %s: %s" % (sym, e))

    # 5. ENTRETIEN : annule les ordres invalidés ----------------------
    for o in fut_orders:
        oid = g(o, "orderId", "id", "_id")
        sym = str(g(o, "symbol", "token", default="")).upper().replace("USDT", "").replace("-PERP", "")
        plan = st["planned"].get(sym)
        bias = bias_map.get(sym, "")
        oside = str(g(o, "side", default="")).lower()
        invalid = (bias == "HAUSSIER" and oside == "short") or (bias == "BAISSIER" and oside == "long") \
                  or (plan is None) or (bias == "données périmées")
        if invalid and oid:
            try:
                mcp("cancel_futures_order", {"orderId": oid})
                log("ordre futures %s annulé (setup invalidé / biais %s)" % (sym, bias))
                st["planned"].pop(sym, None)
            except Exception as e:
                err("annulation ordre %s: %s" % (sym, e))
    for o in fx_orders:
        oid = g(o, "orderId", "id", "ticket", "_id")
        pair = str(g(o, "pairId", "symbol", "pair", default="")).upper()
        bias = bias_map.get(pair, "")
        oside = str(g(o, "side", default="")).lower()
        invalid = (bias == "HAUSSIER" and oside == "sell") or (bias == "BAISSIER" and oside == "buy") \
                  or (bias == "données périmées")
        if invalid and oid:
            try:
                mcp("cancel_forex_order", {"orderId": oid})
                log("ordre forex %s annulé (setup invalidé / biais %s)" % (pair, bias))
            except Exception as e:
                err("annulation ordre forex %s: %s" % (pair, e))

    save_state(ctx, st)

    # nettoie l'état des positions fermées
    live_ids = {str(g(p, "positionId", "id", "position_id", "ticket", "_id", default="")) for p in fut_pos + fx_pos}
    for pid in list(st["positions"].keys()):
        if pid not in live_ids:
            st["positions"].pop(pid, None)
    save_state(ctx, st)

    balances = {"spotUsdt": round(spot_usdt, 2),
                "futuresLibre": round(fut_free, 2), "futuresMargeEngagée": round(fut_margin_used, 2),
                "forexLibre": round(fx_free, 2), "forexEquity": round(fx_equity, 2)}
    summary = ("Passage %s | %d action(s), %d ordre(s) placé(s), %d erreur(s). "
               "Positions: %d. Soldes: fut %.2f / fx %.2f USDT." %
               (ran_at, len(_actions), len(orders_placed), len(_errors),
                len(positions_out), fut_free, fx_equity))

    # ---- historique persistant des passages (consultable à tout moment) ----
    history = []
    try:
        hpath = os.path.join(os.path.dirname(state_path(ctx)), "trading_history.json")
        if os.path.exists(hpath):
            with open(hpath, "r", encoding="utf-8") as fh:
                history = json.load(fh)
    except Exception:
        history = []
    history.append({"ranAt": ran_at, "summary": summary, "actions": list(_actions),
                    "errors": list(_errors), "ordersPlaced": orders_placed,
                    "positions": positions_out, "balances": balances})
    history = history[-200:]  # garde les 200 derniers passages (~8 jours)
    try:
        with open(hpath, "w", encoding="utf-8") as fh:
            json.dump(history, fh, ensure_ascii=False, indent=1)
    except Exception as e:
        err("sauvegarde historique: %s" % e)

    return {"artifact": {"summary": summary, "ranAt": ran_at, "actions": _actions,
                         "errors": _errors, "balances": balances, "bias": bias_map,
                         "positions": positions_out, "ordersPlaced": orders_placed,
                         "history": history[::-1][:24]}}  # 24 derniers passages, plus récent d'abord


if __name__ == "__main__":
    result = run(None)
    art = result["artifact"]
    print("=" * 60)
    print(art["summary"])
    print("=" * 60)
    for a in art.get("actions", []):
        print("  [action]", a)
    for o in art.get("ordersPlaced", []):
        print("  [ordre]", json.dumps(o, ensure_ascii=False))
    for e in art.get("errors", []):
        print("  [ERREUR]", e)
    print(json.dumps(art, ensure_ascii=False, indent=1))
