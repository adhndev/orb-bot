# ORB Bot: an Alpaca paper-trading bot with a Claude news check

An intraday stock-trading bot for an **Alpaca paper account (virtual money only)**. It trades a rules-based strategy,
asks Claude to veto trades that are driven by bad news, enforces hard risk limits, and is controlled from a local
dashboard, a desktop app, or Telegram. Every trade is journaled and scored against success criteria that were
frozen before any results existed.

> **Paper trading only.** The bot refuses to run on a live account (the account number must start with `PA`).
> Nothing here is financial advice, and no strategy in this repository has been shown to make money.

---

## Contents
- [What it does](#what-it-does)
- [The strategy, honestly](#the-strategy-honestly)
- [Quick start (Python)](#quick-start-python)
- [Quick start (desktop app, no Python)](#quick-start-desktop-app-no-python)
- [Keys](#keys)
- [Daily schedule](#daily-schedule)
- [Dashboard and Telegram](#dashboard-and-telegram)
- [Safety limits](#safety-limits)
- [Scorecard: how it is judged](#scorecard-how-it-is-judged)
- [Research](#research)
- [Project layout](#project-layout)
- [Tests](#tests)
- [License](#license)

---

## What it does

| | |
|---|---|
| **Trades** | Long-only, intraday, bracket orders (entry + stop-loss + take-profit) on 24 liquid US stocks/ETFs. Flat by 15:55 New York every day. |
| **Strategy** | `gap_fade` (default): buy stocks that open ≥ 2 % below yesterday's close. `orb`: the original 15-minute opening-range breakout. Switch in `config.json`. |
| **Claude** | Before each entry Claude reads that stock's latest headlines and vetoes news-driven moves (earnings miss, guidance cut, offering, fraud …) or halves the size. The rules decide *what* to buy; Claude can only say *no* or *smaller*. |
| **Risk** | Max loss per trade and per day, max trades and open positions, one stock per sector, re-pricing before every order, a fast account monitor, an emergency flatten, and an independent watchdog. |
| **Control** | Web dashboard (`http://127.0.0.1:8765`), a one-window desktop app, and owner-only Telegram commands. |
| **Records** | Journal reconciled against Alpaca's fills, a "no-Claude" control group, a nightly forward test on new data, and a nightly Claude review that keeps a short list of lessons. |

## The strategy, honestly

The project started as an opening-range-breakout (ORB) bot. Before risking anything, it was tested properly
(see [Research](#research)):

1. **ORB on QQQ, 96 variants, 2016–2020 → none beat a coin flip** after costs (fees, regulatory fees, slippage).
2. **Same search on 2020–2026 → none made money**; all did worse than random entry times with the same stop/target.
3. **1,032 variants of 10 strategy families on 26 stocks, all day → the best idea was a gap-down reversion**, but it
   **failed the luck test** (White's Reality Check, p = 0.149; needed < 0.05).

So the default strategy (`gap_fade`) is the *most promising untested idea*, not a proven edge. It is being
paper-traded with pre-registered pass/fail criteria (`success_criteria_gap.json`, locked by `criteria_gap.lock`), and
the nightly forward test tracks it on data it was never chosen on.

**Gap-down rules** (`strategies.py`, identical to `research/lab.py` and checked by `test_strategies.py`):
- the stock opens at least **2 %** below yesterday's close;
- signal on the close of the **first minute**; entries only **09:31–09:34** New York;
- stop = signal price − **0.2 × ATR(14)**; target = **1R** above; flat by 15:55.

## Quick start (Python)

Windows, Python 3.10+ (tested on 3.13 and 3.15).

```bat
pip install -r requirements.txt
python -m pytest -q
open_dashboard.bat
```

In the dashboard: open **Settings** (bottom), paste your keys, **Save keys**, use the **Test** buttons, then press
**Start paper trading**. Or start without the dashboard: `start_dryrun.bat` (no orders) / `start_trading.bat`
(paper orders).

## Quick start (desktop app, no Python)

`build_app.bat` packs everything into **OrbBot.exe** (PyInstaller, one folder, ~140 MB). Build once on a PC with
Python; the result runs on any Windows 10/11 PC.

1. Double-click `build_app.bat` (5–10 minutes). Output: `%LOCALAPPDATA%\OrbBotBuild\dist\OrbBot\OrbBot.exe` and
   `OrbBot-windows.zip`.
2. Double-click **OrbBot.exe**. It opens in its own window (Microsoft Edge app mode); **OrbBotWorker.exe** runs the bot
   in the background without windows. Windows SmartScreen may warn once (unsigned app): *More info → Run anyway*.
3. In **Settings**: save your keys, and optionally **Import** your history from an old bot folder.

The app keeps everything that changes (journal, logs, scorecard, research data) in **`Documents\OrbBot`**, like a
game's save folder. Closing the window closes the app; a running bot keeps trading (stop it first if you want it
stopped). **Run only one copy at a time** (the Python version and the app keep separate files, so two copies would
both trade the same account).

## Keys

| Key | Where to get it |
|---|---|
| `ALPACA_API_KEY`, `ALPACA_SECRET_KEY` | alpaca.markets → switch to your **Paper** account → API Keys. Paper key IDs start with `PK`. |
| `ANTHROPIC_API_KEY` | console.anthropic.com → API Keys. |
| `TELEGRAM_BOT_TOKEN` | Telegram → @BotFather → `/newbot` (or `/mybots`). Optional. |
| `TELEGRAM_CHAT_ID` | Message your bot once, then `python notify.py setup` (or ask @userinfobot). Optional. |

Enter them in **Settings**: they are stored in **Windows Credential Manager** (via `keyring`), never in a file, and
never shown again. Environment variables also work and take priority (`setx ALPACA_API_KEY "PK..."`, then open a new
window). Also set `sec_user_agent` in `config.json` to your name and email (the SEC asks automated clients for it).

If Telegram fails with `CERTIFICATE_VERIFY_FAILED` (antivirus web scanning), the bot already checks certificates with
Windows' own list through `truststore`; run `python notify.py check` to diagnose.

## Daily schedule

New York time (India time is +10:30 in winter, +9:30 in summer).

| Time (NY) | What happens |
|---|---|
| 08:45 | Optional auto-start (off by default; `/autostart on`). Previous closes and ATRs are loaded. |
| 09:00 | Pre-market plan (gap mode: rules only, no Claude call; ORB mode: Claude shortlist). |
| 09:31–09:34 | Gap-down candidates, biggest gap first → risk checks → Claude news check → bracket order. |
| all day | Stops/targets at the broker, fast loss monitor, heartbeat, watchdog. |
| 15:55 | Flat: anything still open is closed. |
| ~15:57 | Journal reconciled with Alpaca, control group recorded, Telegram end-of-day summary + scorecard. |
| 16:20 | Nightly learning: new data appended, forward test, Claude review → lessons. |

## Dashboard and Telegram

The dashboard shows the bot's state, live P&L and positions from Alpaca, profit/loss to date, today's trades, Claude's
decisions, the scorecard, automation, the log, and Settings. If the bot crashes at start, the dashboard shows the error.

Telegram commands (only your chat is answered):

| | | |
|---|---|---|
| `/status` | `/pnl` | `/money` |
| `/positions` | `/trades` | `/plan` |
| `/score` | `/start` (paper) | `/stop` (close all + stop) |
| `/pause` | `/resume` | `/autostart on\|off` |
| `/lessons` | `/learn` | `/log` · `/help` |

## Optional: trader skills

`skills.py` can call a set of separate **trader-\* skill scripts** for a second opinion on each order: an independent
risk check (`trader-risk`), position sizing (`trader-budget`), news and SEC filings (`trader-news`), earnings
(`trader-analyst`), a setup scanner (`trader-scanner`) and a Monte-Carlo replay (`trader-timemachine`).
They are **not part of this repository**. To use them, put them in a `skills/` folder next to the bot files
(`skills/trader-risk/check.py`, …) or point `skills.dir` in `config.json` (or the `SKILLS_DIR` environment variable)
at them. Without them the bot runs normally: it logs that the skills are off and relies on its own built-in
risk limits, sizing and Claude news check. Their tests skip themselves when the scripts are absent.

## Safety limits

Defaults in `config.json`:

| Limit | Default |
|---|---|
| Max loss per trade | $250 |
| Max loss per day | $500 (new entries stop) |
| Trades per day / open positions | 3 / 2 |
| Position size cap | 40 % of the $25,000 budget, max $10,000 per order, 10 % cash kept |
| Sector rule | one open position per sector |
| Flat by | 15:55 New York |

Further protection: re-pricing just before each order (the stop never moves closer), Claude veto by default when its
answer can't be read, an emergency flatten at 1.5 × the planned loss, a watchdog that closes positions if the bot goes
silent, a `PAUSE` file (no new entries) and a `STOP` file (close everything and exit).

## Scorecard: how it is judged

`evaluate.py` compares paper results with criteria **written and hash-locked before trading started**:

- `success_criteria_gap.json` (gap strategy, from 2026-10-29): ≥ 60 paper trades over ≥ 30 days, average ≥ +0.10R,
  profit factor ≥ 1.2, max drawdown ≤ $2,000, ≥ 90 % bootstrap probability that the true average R is positive; plus
  the nightly forward test (≥ 100 trades, ≥ +0.05R) and **Claude-filtered vs unfiltered** on the same days.
- `success_criteria.json` for the ORB strategy.

The verdict is `KEEP RUNNING` until enough trades exist, then `MET` or `FAILED`. Editing a criteria file afterwards is
detected through its `.lock` hash.

## Research

`research/` holds the studies, each with its protocol written before the search:

| | |
|---|---|
| `PROTOCOL.md`, `search.py`, `sim.py` | Study 1: ORB variants on QQQ 2016–2020 |
| `PROTOCOL_2.md`, `search2.py` | Study 2: QQQ 2020–2026, test on other indices |
| `PROTOCOL_3.md`, `study3.py`, `lab.py` | Study 3: 1,032 variants × 26 stocks, discovery / luck test / validation / test |
| `fetch_data.py` | Downloads 1-minute bars (SIP, 2016 →) from Alpaca, several stocks in parallel |
| `daily.py` | Nightly learning: data update, forward test of the watch list, Claude review, lessons |
| `README.md` | Results of each study |

Costs are always included: slippage (max of 3 bps, 1¢) on every fill, SEC fee and FINRA TAF on sales. Minute bars
are split-adjusted. Data files (`research/data`, ~400 MB) are not in the repository: run `run_fetch_data_fast.bat`.

## Project layout

```
orb_bot.py          the bot: data, signals, risk checks, orders, flatten, journal hand-off
strategies.py       live strategy rules (gap_fade), matched to research/lab.py
engine.py           ORB rules, sizing, daily-risk checks, simulator shared with the backtest
advisor.py          Claude: pre-market plan, news veto at entry, position review, lessons
stream.py           websocket feeds (bars, news, order updates) with polling fallback
skills.py           runs the optional trader-* skill scripts (see "Optional: trader skills")
notify.py           Telegram alerts (rate-limited, never logs the token)
dashboard.py        local web dashboard, automation, Telegram commands, Settings
watchdog.py         independent guard: closes positions if the bot goes silent
journal.py          reconciles trades with Alpaca, control group, end-of-day and weekly reports
evaluate.py         scorecard against the locked success criteria
apphome.py          data folder, key storage, starting programs (Python or app)
orbbot_app.py       desktop-app entry point (window, worker, self-test, import)
backtest.py         ORB backtest with fees and slippage
research/           studies, data download, nightly learning
deploy/             optional Linux server setup (systemd units, orbctl)
OrbBot.spec, build_app.bat   desktop-app build
config.json         all settings (risk limits, strategy, universe, Claude, skills)
```

## Tests

```bat
python -m pytest -q
```

Everything runs offline with fakes for Alpaca, Claude and Telegram (no keys needed, no orders sent). Tests that need
the downloaded market data or the optional skill scripts skip themselves when those are absent.

## License

**Personal use, no redistribution.** You may download, run and modify this code for your own use. You may not
re-upload, publish, share or sell it, or modified versions of it, without written permission. See [LICENSE](LICENSE).

---

*Built for learning and paper trading. Markets are risky; past or simulated results do not predict future results.*
