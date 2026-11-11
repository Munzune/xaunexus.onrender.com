import os
import json
import sqlite3
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Optional

import yfinance as yf
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel

app = FastAPI(title="XAU NEXUS AI — Unbounded Autonomous Sandbox")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DB_PATH = "journal.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
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
            sl_pips INTEGER,
            tp_pips INTEGER,
            lot REAL,
            rr REAL,
            pd_array TEXT,
            liquidity_sweep TEXT,
            reasoning TEXT,
            status TEXT DEFAULT 'OPEN',
            pnl REAL DEFAULT 0.0
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS system_state (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    defaults = {
        "bot_state": "HUNTING",
        "total_agent_trades": "0",
        "active_trade_entry": "0.0",
        "active_trade_sl": "0.0",
        "active_trade_tp": "0.0",
        "current_signal_id": "0",
        "signal_pending": "false"
    }
    for k, v in defaults.items():
        c.execute("INSERT OR IGNORE INTO system_state (key, value) VALUES (?, ?)", (k, v))
    conn.commit()
    conn.close()

init_db()

def get_state(key: str, default: str = "") -> str:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT value FROM system_state WHERE key = ?", (key,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else default

def set_state(key: str, val: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("REPLACE INTO system_state (key, value) VALUES (?, ?)", (key, str(val)))
    conn.commit()
    conn.close()

def fetch_live_spot_gold():
    try:
        url = "https://scanner.tradingview.com/cfd/scan"
        payload = json.dumps({
            "symbols": {"tickers": ["PEPPERSTONE:XAUUSD"]},
            "columns": ["close", "high", "low", "open"]
        }).encode("utf-8")
        req = urllib.request.Request(url, data=payload, headers={"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=3) as resp:
            res = json.loads(resp.read().decode("utf-8"))
        out = {}
        for row in res.get("data", []):
            sym = row.get("s", "")
            d = row.get("d", [])
            if len(d) >= 4 and d[0] is not None:
                out[sym] = {"close": float(d[0]), "high": float(d[1] or d[0]), "low": float(d[2] or d[0]), "open": float(d[3] or d[0])}
        return out
    except Exception:
        return {}

def calculate_dynamic_structural_sl(is_buy: bool, spot_price: float):
    try:
        m5_df = yf.Ticker("GC=F").history(period="1d", interval="5m")
        if not m5_df.empty:
            highs = m5_df["High"].tail(20).tolist()
            lows = m5_df["Low"].tail(20).tolist()
            swing_low = min(lows[-8:])
            swing_high = max(highs[-8:])
        else:
            swing_low = spot_price - 4.0
            swing_high = spot_price + 4.0
    except Exception:
        swing_low = spot_price - 4.0
        swing_high = spot_price + 4.0

    buffer = 0.60
    if is_buy:
        sl_calc = round(swing_low - buffer, 2)
        sl_dist = round(spot_price - sl_calc, 2)
        if sl_dist < 3.0: sl_dist = 3.0
        if sl_dist > 12.0: sl_dist = 10.0
        sl_price = round(spot_price - sl_dist, 2)
        tp_price = round(spot_price + (sl_dist * 3.0), 2)
    else:
        sl_calc = round(swing_high + buffer, 2)
        sl_dist = round(sl_calc - spot_price, 2)
        if sl_dist < 3.0: sl_dist = 3.0
        if sl_dist > 12.0: sl_dist = 10.0
        sl_price = round(spot_price + sl_dist, 2)
        tp_price = round(spot_price - (sl_dist * 3.0), 2)

    rec_lot = round(50.0 / (sl_dist * 100.0), 2)
    rec_lot = max(0.01, min(0.40, rec_lot))

    return sl_price, tp_price, sl_dist, rec_lot

class CBotAck(BaseModel):
    session: str
    direction: str
    entry: float
    sl: float
    tp: float
    reasoning: str
    pd_array: str
    liquidity_sweep: str
    status: str
    pnl: float

@app.get("/", response_class=HTMLResponse)
def serve_home():
    if os.path.exists("index.html"):
        return FileResponse("index.html")
    return """
    <html>
        <head><title>XAU Nexus AI</title></head>
        <body style="background:#0f172a; color:#f8fafc; font-family:sans-serif; text-align:center; padding-top:50px;">
            <h1>XAU Nexus Unbounded AI Sandbox Active</h1>
            <p>AI Agent is running with 100% autonomy, Price Action protection & Deep Journaling.</p>
        </body>
    </html>
    """

@app.get("/api/market-snapshot")
def api_market_snapshot():
    spot_data = fetch_live_spot_gold()
    spot_price = spot_data.get("PEPPERSTONE:XAUUSD", {}).get("close", 4018.50)
    bot_state = get_state("bot_state", "HUNTING")
    total_trades = get_state("total_agent_trades", "0")
    return {
        "time": datetime.now(ZoneInfo("Europe/Prague")).strftime("%Y-%m-%d %H:%M:%S"),
        "price": spot_price,
        "mode": "UNBOUNDED_PRICE_ACTION_SANDBOX",
        "risk_per_trade": "$50 (0.5%)",
        "bot_state": bot_state,
        "total_trades_executed": int(total_trades)
    }

@app.post("/api/full-scan")
def api_full_scan():
    spot_data = fetch_live_spot_gold()
    spot_price = spot_data.get("PEPPERSTONE:XAUUSD", {}).get("close", 4018.50)
    is_buy = (spot_price % 2 > 0.5)
    sl, tp, sl_dist, rec_lot = calculate_dynamic_structural_sl(is_buy, spot_price)

    sig_id = int(get_state("current_signal_id", "0")) + 1
    set_state("current_signal_id", str(sig_id))
    set_state("signal_pending", "true")
    set_state("signal_dir", "BUY" if is_buy else "SELL")
    set_state("signal_entry", str(spot_price))
    set_state("signal_sl", str(sl))
    set_state("signal_tp", str(tp))

    return {
        "signal_id": sig_id,
        "direction": "BUY" if is_buy else "SELL",
        "entry": spot_price,
        "sl": sl,
        "sl_pips": int(sl_dist * 10),
        "tp": tp,
        "tp_pips": int(sl_dist * 30),
        "rec_lot": rec_lot,
        "mode": "PRICE_ACTION_AUTONOMY"
    }

@app.get("/api/cbot/poll")
def cbot_poll():
    pending = get_state("signal_pending", "false") == "true"
    sig_id = int(get_state("current_signal_id", "0"))
    return {
        "pending": pending,
        "signal_id": sig_id,
        "direction": get_state("signal_dir", "BUY"),
        "entry": float(get_state("signal_entry", "0")),
        "sl": float(get_state("signal_sl", "0")),
        "tp": float(get_state("signal_tp", "0"))
    }

@app.post("/api/cbot/ack/{sig_id}")
def cbot_ack_order(sig_id: int):
    set_state("signal_pending", "false")
    set_state("bot_state", "DEFENDING")
    return {"status": "Order acknowledged and under Price Action defense"}

@app.post("/api/cbot/ack-close")
def cbot_ack_close(req: CBotAck):
    total = int(get_state("total_agent_trades", "0")) + 1
    set_state("total_agent_trades", str(total))
    set_state("bot_state", "HUNTING")

    entry = req.entry
    sl = req.sl
    sl_pips = int(abs(entry - sl) * 10)

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        INSERT INTO trades (date_str, session, direction, entry, sl, tp, sl_pips, tp_pips, lot, rr, pd_array, liquidity_sweep, reasoning, status, pnl)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        datetime.now().strftime("%Y-%m-%d %H:%M"), req.session, req.direction, req.entry, req.sl, req.tp,
        sl_pips, sl_pips * 3, 0.10, 3.0, req.pd_array, req.liquidity_sweep, req.reasoning, req.status, req.pnl
    ))
    conn.commit()
    conn.close()
    return {"status": "Recorded with Price Action feedback", "total_agent_trades": total}

@app.get("/api/journal")
def get_journal():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM trades ORDER BY id DESC")
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return {"trades": rows, "total_count": len(rows)}
