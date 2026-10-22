# Halal Strength Screener

A personal, EMC-style screener. Every weekday morning (6:30 am Malaysia time) it ranks
Shariah-compliant US stocks by strength, finds pullback setups with entry, stop and
target levels, and publishes everything to a private-ish web page you can open on your phone.

Cost: RM0. It runs on GitHub's free Actions and Pages.

---

## Setup (about 30-60 minutes, do it on a laptop browser)

### 1. Create a GitHub account
Sign up at github.com if you don't have one.

### 2. Create a repository
- Click **+** (top right) > **New repository**
- Name it something unguessable, e.g. `hs-7k2q` (the web address will contain this name)
- Choose **Public** (free GitHub Pages needs a public repo)
- Tick **Add a README file**, then **Create repository**

### 3. Upload the files
- In the repo, click **Add file > Upload files**
- Drag in `screener.py`, `template.html`, `requirements.txt` and `.gitignore`
- Click **Commit changes**

### 4. Add the workflow file
Folders starting with a dot don't always upload, so create this one by hand:
- **Add file > Create new file**
- In the name box type exactly: `.github/workflows/daily.yml`
- Paste the contents of `daily.yml` from this pack
- **Commit changes**

### 5. Turn on GitHub Pages
- **Settings > Pages**
- Under **Build and deployment > Source**, choose **GitHub Actions**

### 6. Run it the first time
- Go to the **Actions** tab. If asked, click **I understand my workflows, go ahead and enable them**
- Click **Daily screen** (left) > **Run workflow** > **Run workflow**
- The first run takes roughly 1-2 hours because it downloads fundamentals for every stock.
  Later runs take about 10-15 minutes because fundamentals are cached and refreshed monthly.

### 7. Open your site
When the run turns green, your page is at:

    https://YOUR-USERNAME.github.io/YOUR-REPO-NAME/

On your phone, open it in Safari or Chrome and choose **Add to Home Screen** so it works like an app.

---

## How it works

**Universe.** Every US-listed common stock and ADR from the NASDAQ Trader symbol files, minus ETFs,
warrants, units and preferreds. Then a liquidity floor: price at least $10 and at least $5M traded per day.

**Shariah screen.** A stock is shown only if it passes both:
- *Business activity*: excludes the Financial Services sector and industries matching banks,
  insurance, credit, capital markets, mortgage, asset management, brewers, wineries, distillers,
  tobacco, gambling and casinos.
- *Financial ratios*: total debt and cash each at most 33% of total assets
  (`SHARIAH_METHOD = "SC"`, Securities Commission Malaysia style). Switch to `"MCAP"` to use
  24-month average market cap instead (DJIM / S&P style).

Stocks without fundamentals yet are hidden, not assumed halal.

**Score (1-99).** Each stock gets a 1-99 rating against the whole liquid market on six factors,
then a weighted average:

| Factor | Weight | Measure |
|---|---|---|
| Relative strength | 30% | 3/6/9/12-month returns, latest quarter double-weighted |
| EPS growth | 20% | Latest quarter vs same quarter last year |
| Sales growth | 15% | Latest quarter vs same quarter last year |
| Near 52-week high | 15% | Price as % of 52-week high |
| Volume | 10% | Up-day volume vs down-day volume, 50 days |
| Group strength | 10% | Average RS of the stock's industry |

**Setups.** Score 80+, price above a rising 50-day average and the 200-day average, and within
-1% to +3% of the 21-day EMA (a pullback to support).
- Entry: just above today's high (buy only if it breaks out tomorrow)
- Stop: 1% below the lower of the 21 EMA and the 10-day low
- Target: entry + 2.5 x risk. Setups with more than 8% risk are skipped.

All of these numbers are settings at the top of `screener.py`. Edit the file on GitHub
(pencil icon), commit, and the next run uses them.

---

## Things to know

- **Verify before buying.** The business screen works at industry level, so it can't see mixed
  revenue (a supermarket selling alcohol, a hotel with bars, a media firm with music). Check a
  stock on Musaffa or Zoya before you commit money. Use `MANUAL_EXCLUDE` and `MANUAL_INCLUDE`
  in `screener.py` to record your own verdicts.
- **Data source.** Yahoo Finance through the `yfinance` library: free and fine for personal use,
  but unofficial, so it occasionally breaks. If a run fails, update `yfinance` in
  `requirements.txt` to the latest version.
- **Rate limits.** If Yahoo throttles the fundamentals download, the script stops early and
  continues where it left off the next day.
- **Your page is public but unlisted.** Search engines are told not to index it, and no one will
  find it unless you share the link.
- **Keep the schedule alive.** GitHub pauses scheduled workflows in a repo with no activity for
  60 days. You'll get an email; click the link to re-enable it, or make any small commit every
  couple of months.
- **Test the rules.** These setup rules are a reasonable starting point, not a proven edge.
  Paper-trade them for a few weeks before using real money.

Personal research tool. Not investment advice.
