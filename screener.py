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

def download_prices(tickers, chunk=200):
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
            if len(d) >= 260:
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


# ------------------------------------------------------------------- output --

def clean(v, nd=4):
    if v is None:
        return None
    if isinstance(v, (float, np.floating)):
        return None if math.isnan(v) or math.isinf(v) else round(float(v), nd)
    if isinstance(v, (np.integer,)):
        return int(v)
    return v


def write_chart(t, d):
    d = d.iloc[-300:]
    bars = [[i.strftime("%Y-%m-%d"), round(float(r.Open), 2), round(float(r.High), 2),
             round(float(r.Low), 2), round(float(r.Close), 2), int(r.Volume)]
            for i, r in d.iterrows()]
    (SITE / "data" / f"{t}.json").write_text(json.dumps(bars, separators=(",", ":")))


def main():
    (SITE / "data").mkdir(parents=True, exist_ok=True)

    universe = get_universe()
    if LIMIT:
        universe = universe[:LIMIT]
    log(f"universe: {len(universe)} symbols")

    prices = download_prices(universe)
    liquid = {t: d for t, d in prices.items() if is_liquid(d)}
    log(f"liquid with 1y+ history: {len(liquid)}")

    cache = load_cache()
    refresh_fundamentals(list(liquid), cache)

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
        setup = find_setup(liquid[t], r["score"])
        has_chart = bool(setup) or r["score"] >= CHART_MIN_SCORE
        if has_chart:
            write_chart(t, liquid[t])
        stocks.append({
            "t": t, "n": val(r, "name") or t, "sec": val(r, "sector"), "ind": val(r, "industry"),
            "score": int(r["score"]), "rs": int(r["r_rs"]), "eps": int(r["r_eps"]),
            "sales": int(r["r_sales"]), "vol": int(r["r_volume"]), "hi": int(r["r_high"]),
            "grp": int(r["r_group"]),
            "price": clean(r["price"], 2), "chg": clean(r["chg1d"]),
            "off": clean(r["off_high"] - 1), "pe": clean(r["pe"], 1),
            "epsg": clean(r["eps_growth"]), "salesg": clean(r["sales_growth"]),
            "debt": clean(r["debt_ratio"]), "cash": clean(r["cash_ratio"]),
            "checked": val(r, "checked"), "chart": has_chart, "setup": setup,
        })

    last_bar = max(d.index[-1] for d in liquid.values()).strftime("%Y-%m-%d")
    payload = {
        "asof": last_bar,
        "built": dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        "method": SHARIAH_METHOD, "debtLimit": DEBT_LIMIT, "cashLimit": CASH_LIMIT,
        "universe": len(df), "stocks": stocks,
    }
    data = json.dumps(payload, separators=(",", ":")).replace("</", "<\\/")
    (SITE / "index.html").write_text(TEMPLATE.read_text().replace("__DATA__", data))
    halal.to_csv(SITE / "screen.csv")
    n_setups = sum(1 for s in stocks if s["setup"])
    log(f"done: {len(stocks)} halal stocks, {n_setups} setups -> site/index.html")


if __name__ == "__main__":
    main()
