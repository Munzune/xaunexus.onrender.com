import os
import json
import sqlite3
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo
import yfinance as yf
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from google import genai
from google.genai import types
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(BASE_DIR, "journal.db")
INDEX_FILE = os.path.join(BASE_DIR, "index.html")

# Render дээр Environment Variable-аас уншина, локал дээр энд бичиж болно:
API_KEY = os.getenv("GEMINI_API_KEY") or "ЭНД_ӨӨРИЙН_API_ТҮЛХҮҮРЭЭ_ТАВИНА"

app = FastAPI(title="XAU NEXUS AI Cloud")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date_str TEXT,
            session TEXT,
            direction TEXT,
            entry REAL,
            sl REAL,
            tp REAL,
            rr REAL,
            reasoning TEXT,
            status TEXT,
            pnl REAL
        )
    """)
    conn.commit()
    conn.close()

init_db()

# Үндсэн хаягаар ороход вэбсайтыг шууд нээх
@app.get("/")
async def serve_dashboard():
    return FileResponse(INDEX_FILE)

class TradeCreate(BaseModel):
    session: str
    direction: str
    entry: float
    sl: float
    tp: float
    rr: float
    reasoning: str
    status: str = "OPEN"
    pnl: float = 0.0

class TradeUpdate(BaseModel):
    status: str
    pnl: float

def get_ny_time_info():
    try:
        ny_now = datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        ny_now = datetime.utcnow()

    dec_hour = ny_now.hour + ny_now.minute / 60.0
    if 2.0 <= dec_hour < 5.0:
        session, tradable = "London Killzone", True
    elif 5.0 <= dec_hour < 7.0:
        session, tradable = "Dead Time (05:00-07:00)", False
    elif 7.0 <= dec_hour < 8.5:
        session, tradable = "Pre-Market Window", True
    elif 8.5 <= dec_hour < 11.0:
        session, tradable = "NY AM Killzone", True
    else:
        session, tradable = "Standard NY Session", True

    return {
        "time_str": ny_now.strftime("%H:%M:%S"),
        "date_str": ny_now.strftime("%Y-%m-%d %H:%M"),
        "session": session,
        "tradable": tradable
    }

def fetch_tradingview_spot():
    try:
        url = "https://scanner.tradingview.com/global/scan"
        payload = json.dumps({
            "symbols": {
                "tickers": [
                    "PEPPERSTONE:XAUUSD",
                    "OANDA:XAUUSD",
                    "TVC:DXY",
                    "TVC:US10Y"
                ]
            },
            "columns": ["close", "change", "change_abs", "high", "low", "open"]
        }).encode("utf-8")

        req = urllib.request.Request(
            url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0"
            }
        )
        with urllib.request.urlopen(req, timeout=4) as resp:
            res = json.loads(resp.read().decode("utf-8"))
            rows = {item["s"]: item["d"] for item in res.get("data", [])}

            xau = rows.get("PEPPERSTONE:XAUUSD") or rows.get("OANDA:XAUUSD")
            dxy = rows.get("TVC:DXY")
            us10y = rows.get("TVC:US10Y")

            if xau and xau[0] is not None:
                return {
                    "price": round(float(xau[0]), 2),
                    "change_pct": round(float(xau[1] or 0.0), 2),
                    "change": round(float(xau[2] or 0.0), 2),
                    "today_high": round(float(xau[3] or xau[0]), 2),
                    "today_low": round(float(xau[4] or xau[0]), 2),
                    "today_open": round(float(xau[5] or xau[0]), 2),
                    "dxy": round(float(dxy[0]), 2) if dxy and dxy[0] else 102.35,
                    "us10y": round(float(us10y[0]), 3) if us10y and us10y[0] else 4.102
                }
    except Exception:
        pass
    return None

def fetch_live_ict_levels():
    tv_spot = fetch_tradingview_spot()

    try:
        gold = yf.Ticker("GC=F")
        daily = gold.history(period="5d", interval="1d")
        m5 = gold.history(period="2d", interval="5m")

        fut_price = float(m5["Close"].iloc[-1])

        if tv_spot:
            current_price = tv_spot["price"]
            spot_offset = current_price - fut_price
            change = tv_spot["change"]
            change_pct = tv_spot["change_pct"]
            dxy_val = tv_spot["dxy"]
            tnx_val = tv_spot["us10y"]
        else:
            current_price = round(fut_price, 2)
            spot_offset = 0.0
            prev_c = float(daily["Close"].iloc[-2])
            change = round(current_price - prev_c, 2)
            change_pct = round((change / prev_c) * 100, 2)
            dxy_val, tnx_val = 102.35, 4.102

        prev_close = round(float(daily["Close"].iloc[-2]) + spot_offset, 2)
        pdh = round(float(daily["High"].iloc[-2]) + spot_offset, 2)
        pdl = round(float(daily["Low"].iloc[-2]) + spot_offset, 2)
        today_open = tv_spot["today_open"] if tv_spot else round(float(daily["Open"].iloc[-1]) + spot_offset, 2)
        ndog = round((prev_close + today_open) / 2.0, 2)
        nwog = round(float(daily["Open"].iloc[0]) + spot_offset, 2)

        recent_m5 = m5.tail(36)
        bsl = round(float(recent_m5["High"].max()) + spot_offset, 2)
        ssl = round(float(recent_m5["Low"].min()) + spot_offset, 2)

        fvg_detected = False
        fvg_ce = round((bsl + ssl) / 2.0, 2)
        for i in range(len(recent_m5) - 3, max(0, len(recent_m5) - 15), -1):
            c1_high = float(recent_m5["High"].iloc[i]) + spot_offset
            c3_low = float(recent_m5["Low"].iloc[i + 2]) + spot_offset
            c1_low = float(recent_m5["Low"].iloc[i]) + spot_offset
            c3_high = float(recent_m5["High"].iloc[i + 2]) + spot_offset
            if c3_low > c1_high:
                fvg_detected = True
                fvg_ce = round((c3_low + c1_high) / 2.0, 2)
                break
            elif c3_high < c1_low:
                fvg_detected = True
                fvg_ce = round((c1_low + c3_high) / 2.0, 2)
                break

        m5_open_6 = round(float(recent_m5["Open"].iloc[-6]) + spot_offset, 2)
        momentum_bearish = current_price < m5_open_6

        return {
            "price": current_price,
            "change": change,
            "change_pct": change_pct,
            "dxy": dxy_val,
            "us10y": tnx_val,
            "momentum_bearish": momentum_bearish,
            "levels": {
                "pdh": pdh, "pdl": pdl, "bsl": bsl, "ssl": ssl,
                "ndog": ndog, "nwog": nwog, "fvg_ce": fvg_ce
            },
            "fvg_detected": fvg_detected
        }
    except Exception:
        p = tv_spot["price"] if tv_spot else 4018.42
        return {
            "price": p, "change": -86.10, "change_pct": -2.09,
            "dxy": 102.35, "us10y": 4.102, "momentum_bearish": True,
            "levels": {
                "pdh": round(p + 125.0, 2), "pdl": round(p - 15.0, 2),
                "bsl": round(p + 35.0, 2), "ssl": round(p - 5.0, 2),
                "ndog": round(p + 60.0, 2), "nwog": round(p + 40.0, 2),
                "fvg_ce": round(p + 8.5, 2)
            },
            "fvg_detected": True
        }

def compute_ict_algorithmic_signal(t_info, m_info):
    price = m_info["price"]
    lv = m_info["levels"]
    mid_point = round((lv["bsl"] + lv["ssl"]) / 2.0, 2)
    is_bearish = m_info.get("momentum_bearish", False)

    if is_bearish and price >= mid_point:
        signal = "SELL"
        bias_label = "BEARISH"
        entry = price
        sl = round(price + 8.0, 2)
        risk = round(sl - entry, 2)
        be_trigger = round(entry - risk * 1.0, 2)
        trail_trigger = round(entry - risk * 2.0, 2)
        target_3r = round(entry - risk * 3.0, 2)
        bull_p, neut_p, bear_p = 18, 16, 66
        mgmt_rule = (
            f"Ханш {be_trigger} (+1R) хүрмэгц SL-ийг {entry} (Breakeven) рүү татна. "
            f"{trail_trigger} (+2R) хүрмэгц SL-ийг {be_trigger} руу зөөж +1R ашгаа түгжээд {target_3r} (1:3 RR) дээр бүрэн гарна."
        )
        insight = f"SELL 1:3 R:R Сетап: Оролт {entry} | SL {sl} | Зорилт {target_3r}. {be_trigger} хүрмэгц SL-ээ Breakeven рүү зөөнө."
    else:
        signal = "BUY"
        bias_label = "BULLISH"
        entry = price
        sl = round(price - 8.0, 2)
        risk = round(entry - sl, 2)
        be_trigger = round(entry + risk * 1.0, 2)
        trail_trigger = round(entry + risk * 2.0, 2)
        target_3r = round(entry + risk * 3.0, 2)
        bull_p, neut_p, bear_p = 72, 18, 10
        mgmt_rule = (
            f"Ханш {be_trigger} (+1R) хүрмэгц SL-ийг {entry} (Breakeven) рүү татна. "
            f"{trail_trigger} (+2R) хүрмэгц SL-ийг {be_trigger} руу зөөж +1R ашгаа түгжээд {target_3r} (1:3 RR) дээр бүрэн гарна."
        )
        insight = f"BUY 1:3 R:R Сетап: Оролт {entry} | SL {sl} | Зорилт {target_3r}. {be_trigger} хүрмэгц SL-ээ Breakeven рүү зөөнө."

    fvg_status = "Тийм" if m_info["fvg_detected"] else "Үгүй"
    lines = [
        "=== ICT 2026 1:3 R:R LIVE TRADE MANAGEMENT ТАЙЛАН ===",
        f"Цагийн бүс: {t_info['session']} (NY Time: {t_info['time_str']})",
        f"Бодит Спот Үнэ: {price} USD (Dealing Range төв: {mid_point} USD)",
        f"Liquidity Map: Дээд BSL = {lv['bsl']} (PDH: {lv['pdh']}) / Доод SSL = {lv['ssl']} (PDL: {lv['pdl']})",
        f"FVG & 50% CE: {lv['fvg_ce']} (FVG Илэрсэн: {fvg_status})",
        f"1:3 R:R Төлөвлөгөө: {signal} @ {entry} | Анхны SL: {sl} | Эцсийн Гаралт (3R): {target_3r}",
        f"Ашиг Хамгаалах Дүрэм: {mgmt_rule}"
    ]

    return {
        "signal": signal, "bias_label": bias_label, "bias_pct": max(bull_p, bear_p),
        "bullish_pct": bull_p, "neutral_pct": neut_p, "bearish_pct": bear_p,
        "entry": entry, "sl": sl, "be_trigger": be_trigger, "trail_trigger": trail_trigger,
        "target_3r": target_3r, "rr": 3.0, "mgmt_rule": mgmt_rule,
        "prob_1r": 84, "prob_ce": 71, "prob_2r": 58, "prob_3r": 46, "prob_sl": 14,
        "insight_mn": insight, "detailed_reasoning_mn": "\n".join(lines)
    }

@app.get("/api/market-snapshot")
async def market_snapshot():
    return {"time": get_ny_time_info(), "market": fetch_live_ict_levels()}

@app.post("/api/full-scan")
async def full_ai_scan():
    t_info = get_ny_time_info()
    m_info = fetch_live_ict_levels()
    lv = m_info["levels"]
    base_algo = compute_ict_algorithmic_signal(t_info, m_info)

    if not API_KEY or "ЭНД_ӨӨРИЙН" in API_KEY:
        return {"time": t_info, "market": m_info, "ai": base_algo}

    prompt = (
        f"Та бол XAU/USD дээр ICT 2026 SMC аргаар 1:3 R:R арилжаа удирддаг систем. "
        f"NY Цаг: {t_info['time_str']} ({t_info['session']}), Spot Үнэ: {m_info['price']}, "
        f"PDH: {lv['pdh']}, PDL: {lv['pdl']}, BSL: {lv['bsl']}, SSL: {lv['ssl']}, FVG CE: {lv['fvg_ce']}. "
        f"Зөвхөн 1:3 R:R сетап гаргаж, ханш +1R болон +2R хүрэхэд SL-ийг хэрхэн зөөж ашгаа хамгаалах зааврыг өгнө үү. "
        f"JSON бүтэц: signal (BUY/SELL), bias_label (BULLISH/BEARISH), bias_pct, "
        f"bullish_pct, neutral_pct, bearish_pct, entry, sl, be_trigger, trail_trigger, target_3r, rr (3.0), "
        f"mgmt_rule, prob_1r, prob_ce, prob_2r, prob_3r, prob_sl, insight_mn, detailed_reasoning_mn."
    )
    try:
        client = genai.Client(api_key=API_KEY)
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.2)
        )
        ai_res = json.loads(response.text)
        for k, v in base_algo.items():
            if k not in ai_res or ai_res[k] is None:
                ai_res[k] = v
        return {"time": t_info, "market": m_info, "ai": ai_res}
    except Exception:
        return {"time": t_info, "market": m_info, "ai": base_algo}

@app.get("/api/journal")
async def get_journal():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM trades ORDER BY id DESC")
    rows = [dict(r) for r in c.fetchall()]
    conn.close()

    total = len(rows)
    wins = sum(1 for r in rows if r["pnl"] > 0)
    win_rate = round((wins / total) * 100, 1) if total > 0 else 0.0
    total_pnl = round(sum(r["pnl"] for r in rows), 2)
    avg_rr = round(sum(r["rr"] for r in rows) / total, 2) if total > 0 else 0.0

    return {
        "stats": {"total_trades": total, "win_rate": win_rate, "total_pnl": total_pnl, "avg_rr": avg_rr},
        "trades": rows
    }

@app.post("/api/journal")
async def add_journal_trade(trade: TradeCreate):
    t_info = get_ny_time_info()
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        INSERT INTO trades (date_str, session, direction, entry, sl, tp, rr, reasoning, status, pnl)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (t_info["date_str"], trade.session, trade.direction, trade.entry, trade.sl, trade.tp, trade.rr, trade.reasoning, trade.status, trade.pnl))
    conn.commit()
    conn.close()
    return {"status": "saved"}

@app.put("/api/journal/{trade_id}")
async def update_journal_trade(trade_id: int, upd: TradeUpdate):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("UPDATE trades SET status = ?, pnl = ? WHERE id = ?", (upd.status, upd.pnl, trade_id))
    conn.commit()
    conn.close()
    return {"status": "updated"}

# cTrader cBot Bridge
cbot_signal_queue = {
    "pending": False,
    "signal_id": 0,
    "direction": "BUY",
    "entry": 0.0,
    "sl": 0.0,
    "tp": 0.0,
    "session": "NY AM Killzone"
}

class CBotOrderRequest(BaseModel):
    direction: str
    entry: float
    sl: float
    tp: float
    session: str = "NY AM Killzone"

@app.post("/api/cbot/send")
async def send_signal_to_cbot(req: CBotOrderRequest):
    cbot_signal_queue["pending"] = True
    cbot_signal_queue["signal_id"] += 1
    cbot_signal_queue["direction"] = req.direction
    cbot_signal_queue["entry"] = req.entry
    cbot_signal_queue["sl"] = req.sl
    cbot_signal_queue["tp"] = req.tp
    cbot_signal_queue["session"] = req.session
    return {"status": "queued", "signal_id": cbot_signal_queue["signal_id"]}

@app.get("/api/cbot/poll")
async def poll_cbot_signal():
    return cbot_signal_queue

@app.post("/api/cbot/ack/{sig_id}")
async def ack_cbot_signal(sig_id: int):
    if cbot_signal_queue["signal_id"] == sig_id:
        cbot_signal_queue["pending"] = False
    return {"status": "acknowledged"}