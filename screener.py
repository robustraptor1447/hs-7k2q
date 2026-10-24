"""
Halal Strength Screener — a personal, EMC-style screener.

Every run:
  1. Builds the US stock universe (NASDAQ Trader symbol files)
  2. Downloads 2 years of daily prices (Yahoo Finance via yfinance)
  3. Refreshes fundamentals for stocks whose data is older than FUND_REFRESH_DAYS
  4. Applies the Shariah screen (business activity + financial ratios)
  5. Scores every stock 1-99 (IBD-style composite) and finds pullback setups
  6. Writes a static website to ./site (index.html + chart data)

Run locally:   python screener.py
Quick test:    LIMIT=300 python screener.py      (only the first 300 tickers)
"""

import datetime as dt
import io
import json
import math
import os
import time
from pathlib import Path

import numpy as np
import pandas as pd
import requests
import yfinance as yf

# ---------------------------------------------------------------- settings --

# Liquidity floor (keeps the universe to real, tradeable companies)
MIN_PRICE = 10.0              # USD
MIN_DOLLAR_VOLUME = 5_000_000  # 50-day average of price x volume, USD

# Shariah financial-ratio method:
#   "SC"   = Securities Commission Malaysia style: debt and cash vs TOTAL ASSETS
#   "MCAP" = DJIM / S&P style: debt and cash vs 24-month AVERAGE MARKET CAP
SHARIAH_METHOD = "SC"
DEBT_LIMIT = 0.33
CASH_LIMIT = 0.33

# Business-activity screen (matched against Yahoo sector / industry names)
EXCLUDED_SECTORS = {"Financial Services"}
EXCLUDED_INDUSTRY_KEYWORDS = [
    "bank", "insurance", "credit services", "capital markets", "mortgage",
    "asset management", "brewer", "winer", "distiller", "tobacco",
    "gambling", "casino",
]

# Your own overrides after checking a stock yourself (use Yahoo tickers, e.g. "BRK-B")
MANUAL_EXCLUDE = []   # always hide these
MANUAL_INCLUDE = []   # pass these even if the automatic screen fails them

# EMC-style score: weights for each 1-99 component rating
WEIGHTS = {
    "rs": 0.30,       # 12-month relative strength (recent quarter double-weighted)
    "eps": 0.20,      # latest quarter EPS growth, year on year
    "sales": 0.15,    # latest quarter revenue growth, year on year
    "volume": 0.10,   # up-volume vs down-volume, last 50 days
    "high": 0.15,     # closeness to the 52-week high
    "group": 0.10,    # industry group relative strength
}

# Setup rules (strong stock pulling back to a rising average)
SETUP_MIN_SCORE = 80
SETUP_MAX_RISK = 0.08        # skip setups where the stop is more than 8% away
SETUP_TARGET_R = 2.5         # target = entry + 2.5 x risk

# Market signal (built from the S&P 500 and Nasdaq 100 ETFs)
MARKET_TICKERS = ["SPY", "QQQ"]
DIST_DAY_LIMIT = 6           # this many distribution days in 25 sessions weakens the market

# Stock signals
BREAKOUT_VOL = 1.4           # breakout volume must be 1.4x the 50-day average
BREAKDOWN_VOL = 1.2          # breakdown volume must be 1.2x the 50-day average
SELL_SIGNAL_MIN_SCORE = 70   # only flag breakdowns in names strong enough to be on your radar

# Your holdings (sell signals). List them in holdings.csv, or in a GitHub secret named
# HOLDINGS written like  AAPL@185.50, MSFT@410
STOP_LOSS = 0.08             # sell if price falls 8% below your buy price
PROFIT_TAKE = 0.25           # suggest taking some profit at +25%

# Fundamentals refresh
FUND_REFRESH_DAYS = 30
MAX_FUND_FETCH_PER_RUN = 600
FUND_SLEEP = 0.3             # seconds between Yahoo requests

# Charts are saved for halal stocks scoring at least this (plus every setup)
CHART_MIN_SCORE = 70

ROOT = Path(__file__).parent
CACHE_FILE = ROOT / "cache" / "fundamentals.json"
SITE = ROOT / "site"
TEMPLATE = ROOT / "template.html"
HOLDINGS_FILE = ROOT / "holdings.csv"
LIMIT = int(os.environ.get("LIMIT", "0"))

UA = {"User-Agent": "Mozilla/5.0 (personal screener)"}


def log(*a):
    print(f"[{dt.datetime.utcnow():%H:%M:%S}]", *a, flush=True)


# ---------------------------------------------------------------- universe --

def get_universe():
    """All US-listed common stocks and ADRs, excluding ETFs, warrants, units, preferreds."""
    base = "https://www.nasdaqtrader.com/dynamic/SymDir/"
    frames = []
    for name, sym_col in [("nasdaqlisted.txt", "Symbol"), ("otherlisted.txt", "ACT Symbol")]:
        txt = requests.get(base + name, headers=UA, timeout=60).text
        df = pd.read_csv(io.StringIO(txt), sep="|")
        df = df[~df[sym_col].astype(str).str.startswith("File Creation")]
        df = df[(df["Test Issue"] == "N") & (df["ETF"] == "N")]
        df = df.rename(columns={sym_col: "symbol", "Security Name": "name"})
        frames.append(df[["symbol", "name"]])
    u = pd.concat(frames).dropna()
    bad = r"warrant|right|unit|preferred|notes|debenture|%|depositary share"
    u = u[~u["name"].str.contains(bad, case=False, regex=True)]
    u = u[~u["symbol"].str.contains(r"[\$\^=]", regex=True)]
    u["symbol"] = u["symbol"].str.replace(".", "-", regex=False)
    u = u.drop_duplicates("symbol")
    return u["symbol"].tolist()


# ------------------------------------------------------------------ prices --

def download_prices(tickers, chunk=200, min_len=260):
    out = {}
    for i in range(0, len(tickers), chunk):
        part = tickers[i:i + chunk]
        log(f"prices {i + 1}-{i + len(part)} of {len(tickers)}")
        try:
            df = yf.download(part, period="2y", interval="1d", auto_adjust=True,
                             group_by="ticker", threads=True, progress=False)
        except Exception as e:
            log("  download failed:", e)
            continue
        multi = isinstance(df.columns, pd.MultiIndex)
        level0 = set(df.columns.get_level_values(0)) if multi else set()
        for t in part:
            if multi:
                if t not in level0:
                    continue
                d = df[t]
            else:
                d = df
            d = d[["Open", "High", "Low", "Close", "Volume"]].dropna(subset=["Close"])
            if len(d) >= min_len:
                out[t] = d
        time.sleep(1)
    return out


def is_liquid(d):
    c, v = d["Close"], d["Volume"]
    return c.iloc[-1] >= MIN_PRICE and (c * v).iloc[-50:].mean() >= MIN_DOLLAR_VOLUME


def tech_metrics(d):
    c, h, v = d["Close"], d["High"], d["Volume"]

    def roc(n):
        return c.iloc[-1] / c.iloc[-1 - n] - 1

    rs_raw = 0.4 * roc(63) + 0.2 * roc(126) + 0.2 * roc(189) + 0.2 * roc(252)
    chg = c.diff().iloc[-50:]
    v50 = v.iloc[-50:]
    up, dn = v50[chg > 0].sum(), v50[chg < 0].sum()
    return {
        "price": float(c.iloc[-1]),
        "chg1d": float(c.iloc[-1] / c.iloc[-2] - 1),
        "rs_raw": float(rs_raw),
        "updown": float(up / dn) if dn > 0 else 3.0,
        "off_high": float(c.iloc[-1] / h.iloc[-252:].max()),
        "avg_close_2y": float(c.mean()),
    }


# ------------------------------------------------------------ fundamentals --

def load_cache():
    if CACHE_FILE.exists():
        return json.loads(CACHE_FILE.read_text())
    return {}


def save_cache(cache):
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(cache))


def fetch_fundamentals(t):
    tk = yf.Ticker(t)
    info = tk.info or {}
    bs = tk.quarterly_balance_sheet

    def latest(names):
        if bs is None or bs.empty:
            return None
        for n in names:
            if n in bs.index:
                s = bs.loc[n].dropna()
                if len(s):
                    return float(s.iloc[0])
        return None

    total_debt = latest(["Total Debt"])
    if total_debt is None:
        total_debt = info.get("totalDebt")
    cash = latest(["Cash Cash Equivalents And Short Term Investments",
                   "Cash And Cash Equivalents"])
    if cash is None:
        cash = info.get("totalCash")
    return {
        "name": info.get("shortName") or info.get("longName") or t,
        "sector": info.get("sector"),
        "industry": (info.get("industry") or "").replace("\u2014", " - "),
        "shares": info.get("sharesOutstanding"),
        "pe": info.get("trailingPE"),
        "eps_growth": info.get("earningsQuarterlyGrowth"),
        "sales_growth": info.get("revenueGrowth"),
        "total_assets": latest(["Total Assets"]),
        "total_debt": total_debt,
        "cash": cash,
    }


def refresh_fundamentals(tickers, cache):
    today = dt.date.today()

    def age(t):
        c = cache.get(t, {}).get("checked")
        return (today - dt.date.fromisoformat(c)).days if c else 10_000

    due = sorted([t for t in tickers if age(t) >= FUND_REFRESH_DAYS], key=age, reverse=True)
    due = due[:MAX_FUND_FETCH_PER_RUN]
    log(f"fundamentals: {len(due)} to refresh")
    fails_in_row = 0
    for i, t in enumerate(due):
        try:
            cache[t] = {**fetch_fundamentals(t), "checked": today.isoformat()}
            fails_in_row = 0
        except Exception as e:
            fails_in_row += 1
            log(f"  {t}: {e}")
            if fails_in_row >= 25:
                log("  too many failures in a row (likely rate-limited); stopping for today")
                break
        if i and i % 100 == 0:
            log(f"  {i}/{len(due)}")
            save_cache(cache)
        time.sleep(FUND_SLEEP)
    save_cache(cache)


# ----------------------------------------------------------------- shariah --

def val(r, k):
    v = r.get(k)
    return None if v is None or (not isinstance(v, str) and pd.isna(v)) else v


def shariah_check(r):
    if r.name in MANUAL_EXCLUDE:
        return False, "Manually excluded", None, None
    if not val(r, "checked"):
        return False, "Not checked yet", None, None
    industry = (val(r, "industry") or "").lower()
    if val(r, "sector") in EXCLUDED_SECTORS or any(k in industry for k in EXCLUDED_INDUSTRY_KEYWORDS):
        return r.name in MANUAL_INCLUDE, "Business activity", None, None

    if SHARIAH_METHOD == "SC":
        base = val(r, "total_assets")
    else:
        shares = val(r, "shares")
        base = shares * r["avg_close_2y"] if shares else None
    if not base or base <= 0 or pd.isna(base):
        return r.name in MANUAL_INCLUDE, "No balance sheet data", None, None

    debt = (val(r, "total_debt") or 0) / base
    cash = (val(r, "cash") or 0) / base
    ok = debt <= DEBT_LIMIT and cash <= CASH_LIMIT
    reason = "" if ok else ("Debt ratio" if debt > DEBT_LIMIT else "Cash ratio")
    return ok or r.name in MANUAL_INCLUDE, reason, debt, cash


# ------------------------------------------------------------------ scoring --

def rating(s):
    """Percentile rank to a 1-99 rating; missing values rank at the bottom."""
    return (s.rank(pct=True, na_option="bottom") * 98 + 1).round()


def score(df):
    df["r_rs"] = rating(df["rs_raw"])
    df["r_volume"] = rating(df["updown"])
    df["r_high"] = rating(df["off_high"])
    df["r_eps"] = rating(pd.to_numeric(df["eps_growth"], errors="coerce"))
    df["r_sales"] = rating(pd.to_numeric(df["sales_growth"], errors="coerce"))

    grp = df.dropna(subset=["industry"]).query("industry != ''")
    grp = grp.groupby("industry")["rs_raw"].agg(["mean", "count"])
    grp = grp[grp["count"] >= 3]
    grp_rating = rating(grp["mean"])
    df["r_group"] = df["industry"].map(grp_rating).fillna(50)

    total = sum(WEIGHTS.values())
    df["score"] = sum(df[f"r_{k}"] * w for k, w in WEIGHTS.items()) / total
    df["score"] = df["score"].round().clip(1, 99)
    return df


def find_setup(d, sc):
    if sc < SETUP_MIN_SCORE:
        return None
    c, low, high = d["Close"], d["Low"], d["High"]
    ema21 = c.ewm(span=21, adjust=False).mean()
    sma50 = c.rolling(50).mean()
    sma200 = c.rolling(200).mean()
    last = c.iloc[-1]
    rising = sma50.iloc[-1] > sma50.iloc[-21]
    dist = last / ema21.iloc[-1] - 1
    if not (rising and last > sma50.iloc[-1] and last > sma200.iloc[-1] and -0.01 <= dist <= 0.03):
        return None
    stop = min(low.iloc[-10:].min(), ema21.iloc[-1]) * 0.99
    entry = high.iloc[-1] * 1.001
    risk = entry - stop
    if risk <= 0 or risk / entry > SETUP_MAX_RISK:
        return None
    return {
        "entry": round(float(entry), 2),
        "stop": round(float(stop), 2),
        "target": round(float(entry + SETUP_TARGET_R * risk), 2),
        "risk": round(float(risk / entry), 4),
    }


# ----------------------------------------------------------------- signals --

def averages(d):
    c = d["Close"]
    return c.ewm(span=21, adjust=False).mean(), c.rolling(50).mean(), c.rolling(200).mean()


def market_regime(idx):
    """Daily market state from SPY and QQQ trend, plus distribution days on SPY.

    Each index earns up to 4 points: close above its 21 EMA, above its 50 SMA,
    50 SMA rising, close above its 200 SMA. Too many distribution days costs 2 points.
    Uptrend at 6+ of 8, Downtrend at 2 or less, with some hysteresis so it doesn't flicker.
    """
    spy = idx["SPY"]
    pts = pd.Series(0.0, index=spy.index)
    used = [idx[t] for t in MARKET_TICKERS if t in idx]
    for d in used:
        c = d["Close"]
        e21, s50, s200 = averages(d)
        p = (c > e21).astype(int) + (c > s50).astype(int) + (s50 > s50.shift(10)).astype(int) \
            + (c > s200).astype(int)
        pts = pts.add(p.reindex(spy.index).ffill().fillna(0), fill_value=0)
    pts = pts * (2 / len(used))
    c, v = spy["Close"], spy["Volume"]
    dist = ((c.pct_change() <= -0.002) & (v > v.shift(1))).astype(int).rolling(25).sum().fillna(0)
    pts = pts - (dist >= DIST_DAY_LIMIT) * 2

    states, st = [], "Caution"
    for i, p in enumerate(pts.values):
        if i < 200:
            states.append(None)
            continue
        if p >= 6:
            st = "Uptrend"
        elif p <= 2:
            st = "Downtrend"
        elif st == "Uptrend" and p <= 4:
            st = "Caution"
        elif st == "Downtrend" and p >= 5:
            st = "Caution"
        states.append(st)
    return pd.Series(states, index=spy.index), pts, dist


ADVICE = {
    "Uptrend": "Buy signals are active. Take them at full size and keep normal stops.",
    "Caution": "Mixed market. Take only the strongest buy signals at half size, and tighten stops.",
    "Downtrend": "Avoid new buys. Protect gains, act on sell signals and hold more cash.",
}


def market_markers(regime):
    prev = regime.shift(1)
    out = []
    for t, st in regime.items():
        p = prev.get(t)
        if not isinstance(st, str) or not isinstance(p, str) or st == p:
            continue
        code = {"Uptrend": "B", "Downtrend": "S", "Caution": "C"}[st]
        out.append([t.strftime("%Y-%m-%d"), code, st])
    return out


def market_summary(idx, regime, pts, dist):
    reg = regime.dropna()
    state = reg.iloc[-1]
    changed = reg[reg != reg.shift(1)]
    since = changed.index[-1]
    info = {"state": state, "since": since.strftime("%Y-%m-%d"),
            "days": int((reg.index > since).sum()) + 1,
            "points": round(float(pts.iloc[-1]), 1), "dist": int(dist.iloc[-1]),
            "distLimit": DIST_DAY_LIMIT, "advice": ADVICE[state], "idx": []}
    for t in MARKET_TICKERS:
        if t not in idx:
            continue
        d = idx[t]
        c = d["Close"]
        e21, s50, s200 = averages(d)
        info["idx"].append({"t": t, "price": round(float(c.iloc[-1]), 2),
                            "chg": clean(c.iloc[-1] / c.iloc[-2] - 1),
                            "vs50": clean(c.iloc[-1] / s50.iloc[-1] - 1),
                            "vs200": clean(c.iloc[-1] / s200.iloc[-1] - 1)})
    return info


def signal_series(d, regime):
    """Breakouts (buy) and 50-day breakdowns (sell) for every day in the history."""
    c, h, v = d["Close"], d["High"], d["Volume"]
    e21, s50, s200 = averages(d)
    av50 = v.rolling(50).mean().shift(1)
    pivot = h.shift(1).rolling(50).max()
    reg = regime.reindex(d.index).ffill()
    buy = (c > pivot) & (v > BREAKOUT_VOL * av50) & (c > s50) & (s50 > s50.shift(10)) \
        & (reg != "Downtrend")
    sell = (c < s50) & (c.shift(1) >= s50.shift(1)) & (v > BREAKDOWN_VOL * av50)
    return buy.fillna(False), sell.fillna(False)


def today_signal(d, sc, buy, sell, state):
    c, low, v = d["Close"], d["Low"], d["Volume"]
    av = v.rolling(50).mean().iloc[-2]
    vr = float(v.iloc[-1] / av) if av else 0.0
    if sell.iloc[-1] and sc >= SELL_SIGNAL_MIN_SCORE:
        return {"k": "sell", "why": f"Closed below its 50-day average on {vr:.1f}x normal volume"}
    if state == "Downtrend" or sc < SETUP_MIN_SCORE:
        return None
    size = "Full size" if state == "Uptrend" else "Half size, market in caution"
    last = float(c.iloc[-1])
    if buy.iloc[-1]:
        stop = max(float(low.iloc[-5:].min()) * 0.99, last * (1 - STOP_LOSS))
        why = f"Broke out to a 50-day high on {vr:.1f}x normal volume"
    else:
        prev = find_setup(d.iloc[:-1], sc)
        if not (prev and d["High"].iloc[-1] >= prev["entry"] and last >= prev["entry"] * 0.995):
            return None
        stop = prev["stop"]
        why = f"Pullback setup triggered above {prev['entry']:.2f}"
    risk = last - stop
    if risk <= 0 or risk / last > SETUP_MAX_RISK + 0.01:
        return None
    return {"k": "buy", "why": why, "size": size, "entry": round(last, 2), "stop": round(stop, 2),
            "target": round(last + SETUP_TARGET_R * risk, 2), "risk": round(risk / last, 4)}


def load_holdings():
    found = {}
    if HOLDINGS_FILE.exists():
        h = pd.read_csv(HOLDINGS_FILE, comment="#", skipinitialspace=True)
        h.columns = [str(c).strip().lower() for c in h.columns]
        for _, r in h.iterrows():
            t = str(r.get("ticker", "")).strip().upper().replace(".", "-")
            if t and t != "NAN":
                bp = pd.to_numeric(r.get("buy_price"), errors="coerce")
                found[t] = None if pd.isna(bp) else float(bp)
    for part in os.environ.get("HOLDINGS", "").replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        t, _, bp = part.partition("@")
        t = t.strip().upper().replace(".", "-")
        try:
            found[t] = float(bp) if bp.strip() else None
        except ValueError:
            found[t] = None
    return found


def holding_signal(t, d, buy_price, state, shariah, buy, sell, name):
    c, low = d["Close"], d["Low"]
    e21, s50, s200 = averages(d)
    last = float(c.iloc[-1])
    gain = last / buy_price - 1 if buy_price else None
    reasons = []    # (severity, action, text)
    if shariah is not None and not shariah[0]:
        reasons.append((4, "Sell", f"No longer passes the Shariah screen ({shariah[1].lower()})"))
    if gain is not None and gain <= -STOP_LOSS:
        reasons.append((4, "Sell", f"Down {abs(gain):.0%} from your buy price, past the {STOP_LOSS:.0%} stop"))
    if sell.iloc[-1]:
        reasons.append((4, "Sell", "Broke below its 50-day average on heavy volume"))
    elif len(s50.dropna()) and last < s50.iloc[-1]:
        reasons.append((3, "Sell", "Trading below its 50-day average"))
    if gain is not None and gain >= PROFIT_TAKE:
        reasons.append((2, "Trim", f"Up {gain:.0%}; consider selling part to lock in profit"))
    if last < e21.iloc[-1] and not (len(s50.dropna()) and last < s50.iloc[-1]):
        reasons.append((1, "Watch", "Slipped below its 21-day average; tighten your stop"))
    if state == "Downtrend":
        reasons.append((2, "Trim", "Market in downtrend; consider reducing your position"))
    elif state == "Caution":
        reasons.append((1, "Watch", "Market in caution; keep stops tight"))
    if buy.iloc[-1]:
        reasons.append((0, "Hold", "Broke out to a new high today"))
    if shariah is None:
        reasons.append((1, "Watch", "Shariah status not checked yet; verify on Musaffa or Zoya"))
    reasons.sort(key=lambda r: -r[0])
    action = reasons[0][1] if reasons and reasons[0][0] > 0 else "Hold"
    if not reasons or reasons[0][0] == 0:
        reasons.append((0, "Hold", "Holding above its rising averages"))
    stop = max([x for x in [buy_price * (1 - STOP_LOSS) if buy_price else None,
                            float(s50.iloc[-1]) * 0.99 if len(s50.dropna()) else None] if x])
    return {"t": t, "n": name or t, "price": round(last, 2), "chg": clean(c.iloc[-1] / c.iloc[-2] - 1),
            "buy": buy_price, "gain": clean(gain), "action": action,
            "why": [r[2] for r in reasons], "stop": round(stop, 2),
            "halal": None if shariah is None else bool(shariah[0])}


# ------------------------------------------------------------------- output --

def clean(v, nd=4):
    if v is None:
        return None
    if isinstance(v, (float, np.floating)):
        return None if math.isnan(v) or math.isinf(v) else round(float(v), nd)
    if isinstance(v, (np.integer,)):
        return int(v)
    return v


def write_chart(t, d, buy=None, sell=None, markers=None):
    d = d.iloc[-300:]
    bars = [[i.strftime("%Y-%m-%d"), round(float(r.Open), 2), round(float(r.High), 2),
             round(float(r.Low), 2), round(float(r.Close), 2), int(r.Volume)]
            for i, r in d.iterrows()]
    m = list(markers or [])
    if buy is not None:
        first = buy & (buy.shift(1).rolling(10).sum().fillna(0) == 0)   # first buy in 10 days
        m += [[i.strftime("%Y-%m-%d"), "B", "Buy"] for i in first[first].index if i >= d.index[0]]
        m += [[i.strftime("%Y-%m-%d"), "S", "Sell"] for i in sell[sell].index if i >= d.index[0]]
    m = sorted(x for x in m if x[0] >= bars[0][0])
    (SITE / "data" / f"{t}.json").write_text(json.dumps({"b": bars, "m": m}, separators=(",", ":")))


def main():
    (SITE / "data").mkdir(parents=True, exist_ok=True)

    idx = download_prices(MARKET_TICKERS)
    if "SPY" not in idx:
        raise SystemExit("Could not download SPY prices, so the market signal can't be built.")
    regime, mpts, mdist = market_regime(idx)
    market = market_summary(idx, regime, mpts, mdist)
    write_chart("_market", idx["SPY"], markers=market_markers(regime))
    log(f"market: {market['state']} since {market['since']}")

    universe = get_universe()
    if LIMIT:
        universe = universe[:LIMIT]
    log(f"universe: {len(universe)} symbols")

    prices = download_prices(universe)
    liquid = {t: d for t, d in prices.items() if is_liquid(d)}
    log(f"liquid with 1y+ history: {len(liquid)}")

    holdings = load_holdings()
    missing = [t for t in holdings if t not in prices]
    if missing:
        prices.update(download_prices(missing, min_len=60))

    cache = load_cache()
    refresh_fundamentals(list(liquid) + [t for t in holdings if t in prices and t not in liquid], cache)

    rows = []
    for t, d in liquid.items():
        rows.append({"ticker": t, **tech_metrics(d), **cache.get(t, {})})
    df = pd.DataFrame(rows).set_index("ticker")
    for col in ["name", "sector", "industry", "checked", "shares", "pe", "eps_growth",
                "sales_growth", "total_assets", "total_debt", "cash"]:
        if col not in df:
            df[col] = None

    df = score(df)  # ratings are relative to the whole liquid market, halal or not

    checks = df.apply(shariah_check, axis=1, result_type="expand")
    df[["halal", "fail_reason", "debt_ratio", "cash_ratio"]] = checks
    halal = df[df["halal"]].sort_values("score", ascending=False)
    log(f"halal: {len(halal)}  (failed or unchecked: {len(df) - len(halal)})")

    stocks = []
    for t, r in halal.iterrows():
        d = liquid[t]
        setup = find_setup(d, r["score"])
        buy, sell = signal_series(d, regime)
        sig = today_signal(d, r["score"], buy, sell, market["state"])
        has_chart = bool(setup) or bool(sig) or r["score"] >= CHART_MIN_SCORE
        if has_chart:
            write_chart(t, d, buy, sell)
        stocks.append({
            "t": t, "n": val(r, "name") or t, "sec": val(r, "sector"), "ind": val(r, "industry"),
            "score": int(r["score"]), "rs": int(r["r_rs"]), "eps": int(r["r_eps"]),
            "sales": int(r["r_sales"]), "vol": int(r["r_volume"]), "hi": int(r["r_high"]),
            "grp": int(r["r_group"]),
            "price": clean(r["price"], 2), "chg": clean(r["chg1d"]),
            "off": clean(r["off_high"] - 1), "pe": clean(r["pe"], 1),
            "epsg": clean(r["eps_growth"]), "salesg": clean(r["sales_growth"]),
            "debt": clean(r["debt_ratio"]), "cash": clean(r["cash_ratio"]),
            "checked": val(r, "checked"), "chart": has_chart, "setup": setup, "sig": sig,
        })

    held = []
    for t, bp in holdings.items():
        if t not in prices:
            held.append({"t": t, "n": t, "action": "Watch", "chart": False,
                         "why": ["No price data found for this ticker; check the spelling"]})
            continue
        d = prices[t]
        if t in df.index:
            r = df.loc[t]
            shariah = (bool(r["halal"]), r["fail_reason"] or "") if val(r, "checked") else None
        else:
            row = pd.Series({"avg_close_2y": float(d["Close"].mean()), **cache.get(t, {})}, name=t)
            ok, reason, _, _ = shariah_check(row)
            shariah = (ok, reason) if val(row, "checked") else None
        buy, sell = signal_series(d, regime)
        name = cache.get(t, {}).get("name")
        h = holding_signal(t, d, bp, market["state"], shariah, buy, sell, name)
        mk = [[d.index[-1].strftime("%Y-%m-%d"), "S" if h["action"] == "Sell" else "C", h["action"]]] \
            if h["action"] in ("Sell", "Trim") else []
        write_chart(t, d, buy, sell, mk)
        h["chart"] = True
        held.append(h)
    order = {"Sell": 0, "Trim": 1, "Watch": 2, "Hold": 3}
    held.sort(key=lambda h: order[h["action"]])

    last_bar = max(d.index[-1] for d in liquid.values()).strftime("%Y-%m-%d")
    payload = {
        "asof": last_bar,
        "built": dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        "method": SHARIAH_METHOD, "debtLimit": DEBT_LIMIT, "cashLimit": CASH_LIMIT,
        "universe": len(df), "stocks": stocks, "market": market, "holdings": held,
        "stopLoss": STOP_LOSS,
    }
    data = json.dumps(payload, separators=(",", ":")).replace("</", "<\\/")
    (SITE / "index.html").write_text(TEMPLATE.read_text().replace("__DATA__", data))
    halal.to_csv(SITE / "screen.csv")
    n_setups = sum(1 for s in stocks if s["setup"])
    n_buy = sum(1 for s in stocks if s["sig"] and s["sig"]["k"] == "buy")
    n_sell = sum(1 for s in stocks if s["sig"] and s["sig"]["k"] == "sell")
    log(f"done: {len(stocks)} halal stocks, {n_setups} setups, {n_buy} buy and {n_sell} sell "
        f"signals, {len(held)} holdings -> site/index.html")


if __name__ == "__main__":
    main()
