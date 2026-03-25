# JEEVES STRATEGIC MEMO — TrumpQuant
### Date: March 24, 2026
### Classification: CONFIDENTIAL — Vencat Capital Internal

---

Ron,

I've read every line of code and every data file. You've built something genuinely impressive in a short time — a working algorithmic trading system that ingests news, categorizes signals, routes trades, manages positions, and learns from outcomes. Most people never get past the idea stage. You have a live system on Railway with paper money flowing.

But I need to be direct: **the system has critical bugs that are losing you money right now**, and there are structural gaps that would destroy a real-money account. Let's fix the urgent stuff first, then talk about making this thing genuinely profitable.

---

## 🚨 PART 1: CRITICAL BUGS (Fix Tonight)

### Bug #1: The Dedup Is Broken — You're Buying UVIX 5x on the Same Story

Look at your last 5 trades in `bot_trades.json`. All five are UVIX IRAN_ESCALATION buys from the same 90-minute window (19:30-20:30 UTC today). The system bought $12,484 of UVIX on what is essentially the same Iran story reformulated by different news outlets.

**Root cause:** Your content-based dedup uses `abs(hash(normalized)) % 999999` which creates a 6-digit hash. That's only 1M possible values — hash collisions are guaranteed. But the deeper problem is that different headlines about the same *event* get different hashes. "Trump's trillion-dollar TACO" and "Pakistan offers to facilitate U.S.-Iran talks" are the same geopolitical event but produce completely different hashes.

**Fix:** Add a **signal-level cooldown**. After firing IRAN_ESCALATION, don't fire it again for 4 hours regardless of new headlines. This is how real desks work — you trade the *event*, not every article about the event.

```python
SIGNAL_COOLDOWN_HOURS = {
    "IRAN_ESCALATION": 4,
    "IRAN_DEESCALATION": 4,
    "TARIFFS": 2,
    "TRADE_DEAL": 2,
    "FED_ATTACK": 3,
    "WAR_ESCALATION": 4,
    "DEFAULT": 2,
}
```

### Bug #2: EOD Close Failed Silently — Positions Leaked

Your EOD log shows: `"positions_closed": 0, "had_active_scalps": 4`. You had 4 active scalps but closed zero positions. This means either:
- The Alpaca positions were already closed (by monitor) but `active_scalps.json` wasn't cleaned up, OR
- The positions are *still open* and carried overnight as unintended swing trades

Either way, `active_scalps.json` and actual Alpaca positions are out of sync. **This is dangerous with real money.** You need a reconciliation step at EOD:

```python
def reconcile_positions():
    """Sync active_scalps.json with actual Alpaca positions."""
    alpaca_positions = {p["symbol"] for p in get_alpaca_positions()}
    scalps = load_active_scalps()
    orphans = [s for s in scalps if s["actual_ticker"] not in alpaca_positions]
    if orphans:
        log_warning(f"Found {len(orphans)} orphaned scalps, cleaning up")
    # Only keep scalps that have matching Alpaca positions
    scalps = [s for s in scalps if s["actual_ticker"] in alpaca_positions]
    save_active_scalps(scalps)
```

### Bug #3: Learning Log Data Loss — 728 Trades, Only 11 Learned

`bot_trades.json` has 728 entries. `learning_log.jsonl` has 11 lines. Your learning engine is only getting fed by `close_eod_positions()`, which calls `record_outcome()`. But `monitor_open_positions()` closes positions mid-day (profit takes, stop losses) and writes to `learning_log.jsonl` directly with a different format — it writes raw fields like `pnl` and `pnl_pct` instead of going through `record_outcome()`. This means:
- Mid-day closes get partial logs
- The learning engine's weight calculations are based on 11 trades instead of hundreds
- Your "40% win rate on IRAN_ESCALATION" stat is based on 10 trades, not the actual history

**Fix:** Route ALL position closes through `record_outcome()` with consistent schema.

### Bug #4: The `MAX_CONCURRENT_POSITIONS = 2` Guard Is Bypassed

You set `MAX_CONCURRENT_POSITIONS = 2` at the top but the guard checks `held_tickers` (current Alpaca positions). The problem: Alpaca market orders take seconds to fill. If two signals fire in the same cron run (which they do — you process multiple posts per `main()`), the second trade's position check won't see the first trade's order yet because it hasn't filled. You need an **in-memory position tracker** within each run:

```python
positions_opened_this_run = set()  # Track within the run, not just Alpaca
```

---

## 📊 PART 2: RISK MANAGEMENT GAPS

### Gap #1: No Portfolio-Level Correlation Control

Right now you can hold UVIX + SQQQ + SPXU simultaneously. All three are "market goes down" bets. If the market rallies, *all three* lose money at once. This is called **concentration risk** — you think you have 3 positions but you really have 1 bet (short market) with 3x the size.

**Fix:** Group tickers into **risk buckets**:
```python
RISK_BUCKETS = {
    "SHORT_MARKET": ["UVIX", "SQQQ", "SPXU"],
    "LONG_MARKET": ["SPY", "QQQ"],
    "ENERGY": ["XLE", "USO"],
    "DEFENSE": ["LMT"],
    "SAFE_HAVEN": ["GLD", "TLT"],
    "CRYPTO": ["COIN"],
    "SINGLE_STOCK": ["TSLA", "NVDA"],
}
MAX_PER_BUCKET = 1  # Only 1 position per risk bucket
```

### Gap #2: No Max Drawdown Circuit Breaker

The botdetector `RiskManager` has a $500 daily loss limit and a kill switch. But `signal_check.py` — your *actual trading engine* — doesn't use the botdetector's RiskManager at all. It has `MAX_DAILY_EXPOSURE = $10,000` but no **cumulative loss limit**.

If the bot loses $500 on trade 1, $500 on trade 2, $500 on trade 3... nothing stops it. You need:

```python
MAX_DAILY_LOSS = 500  # Stop trading after $500 in realized losses today
MAX_WEEKLY_LOSS = 1500  # Stop trading for the week after $1,500 in losses
MAX_DRAWDOWN_FROM_PEAK = 5000  # Halt all trading if account drops $5k from peak
```

### Gap #3: No Overnight Risk Control

Your swing positions ($5,000 each, 3-10 day holds) carry overnight. Overnight gaps are the #1 account killer for small traders. A 5% overnight gap on a $5,000 position = $250 loss before you can react.

**Fix:** Add pre-market gap checks. If a swing position gaps down >2% overnight, close at market open rather than waiting for the wider stop.

### Gap #4: The Dip Buyer Is a Trap

`check_for_dips()` buys any ticker that drops >1% (SPY) to >5% (COIN) intraday. This sounds reasonable but **catching falling knives is how retail traders blow up accounts**. The function has no concept of *why* the dip happened. If SPY drops 1% because the Fed raised rates unexpectedly, buying the dip is the wrong trade.

**Fix:** Only buy dips when there's a bullish *catalyst* (Trump post, trade deal headline). Remove the standalone dip buyer or gate it behind a signal requirement.

---

## 🧠 PART 3: ALPHA SOURCES YOU HAVEN'T TAPPED

### Source #1: Congressional Trading Data (FREE, PROVEN ALPHA)

Members of Congress and their families trade stocks with access to non-public information. This is legal for them and public data for us. Studies show congressional trades outperform the S&P by 6-12% annually.

- **Quiver Quantitative API** (free tier): `https://api.quiverquant.com/beta/live/congresstrading`
- **Capitol Trades**: `https://www.capitoltrades.com/`
- **Signal**: When a Congress member who sits on Armed Services Committee buys LMT the week before an Iran escalation, that's an incredibly high-conviction signal

**Implementation priority: HIGH.** This is the single highest-ROI data source you can add. Congressional buys in sectors matching your signal categories (defense, energy, tech) should amplify your confidence level.

### Source #2: Options Flow / Unusual Options Activity

This is your Unusual Whales plan — but you don't need to wait. Alpaca's market data includes options chain data. Large options bets (unusual volume on calls/puts) often precede the move your headlines are predicting.

- When IRAN_ESCALATION fires AND you see unusual call volume on XLE → HIGH conviction (smart money agrees)
- When IRAN_ESCALATION fires but no unusual options activity → MEDIUM conviction (might be noise)

### Source #3: VIX Term Structure (FREE)

You track VIX level but not the *term structure*. When short-term VIX (VIXMO/VIX9D) is above longer-term VIX (VIX3M), the market is pricing in imminent chaos. This is called **backwardation** and it's the single best timing indicator for volatility trades like UVIX.

```python
# VIX term structure check
vix_spot = get_vix()        # Current VIX
vix_3m = get_vix3m()        # 3-month VIX
if vix_spot > vix_3m * 1.05:  # Backwardation
    uvix_conviction = "VERY_HIGH"  # Front-month fear > long-term fear
else:
    uvix_conviction = "REDUCED"    # Contango eats UVIX alive
```

**Critical insight: UVIX loses money in contango (VIX declining or flat).** Your IRAN_ESCALATION → UVIX trade only works when VIX is actually spiking, not when it's already elevated and mean-reverting. Your 40% win rate on IRAN_ESCALATION is probably because half those trades were UVIX buys in contango.

### Source #4: Truth Social Direct Feed

You're getting Trump signal from Google News RSS, which has:
- 5-30 minute delay from the actual post
- Headline reformulation that changes meaning
- Missing posts that aren't picked up by mainstream media

The actual Truth Social posts are available via scraping or third-party APIs (there are several open-source Truth Social scrapers on GitHub). Being 10 minutes faster on a Trump post is worth significant alpha.

### Source #5: Satellite/Alternative Data (Free Sources)

- **NOAA tanker tracking**: Oil tanker movements through Strait of Hormuz (free, public). Useful for validating OIL_SHOCK signals.
- **FlightRadar24**: Military aircraft movements (useful for WAR_ESCALATION validation)
- These are confirmation signals, not primary — but they separate real escalation from rhetoric.

---

## 🤖 PART 4: MAKING THE LEARNING ENGINE ACTUALLY LEARN

Your current learning engine is a **weight adjuster**, not a learner. It tracks win rates per category and adjusts a size multiplier. This is better than nothing but it's level 1 of 5.

### Level 2: Feature-Based Outcome Prediction (BUILD THIS NEXT)

Instead of just "IRAN_ESCALATION → buy UVIX", you should predict the outcome based on *features*:

```python
features = {
    "signal_category": "IRAN_ESCALATION",
    "vix_level": 26.95,
    "vix_term_structure": "backwardation",  # or "contango"
    "time_of_day": "10:30",
    "day_of_week": "Monday",
    "market_regime": "HIGH_SENSITIVITY",
    "spy_intraday_change": -0.5,
    "post_timing": "market_hours",
    "options_flow_signal": "bullish",
    "hours_since_last_iran_signal": 48,
    "consecutive_iran_posts_today": 3,
}
# → Predict: probability of profit, expected return, optimal hold time
```

Use **logistic regression** or **gradient boosted trees** (XGBoost/LightGBM). With 728 trades worth of data, you have enough to train a basic model. The key features to start with:
1. VIX level at entry
2. Time of day
3. Day of week
4. Hours since last signal in same category
5. Market intraday direction at signal time (trending with or against?)

### Level 3: Outcome Clustering

After enough trades, cluster the outcomes: "When VIX > 25 AND signal fires before 11am AND market is already down 0.5%+, UVIX trades return +3.2% avg." vs "When VIX < 20 AND signal fires after 2pm, UVIX trades lose -1.1% avg."

This turns your 40% blended win rate into a conditional win rate: maybe it's 80% under the right conditions and 15% under the wrong ones. That's actionable.

### Level 4: Reinforcement Learning on Position Sizing

Once you have conditional win rates, optimize position sizing with Kelly Criterion:

```python
def kelly_fraction(win_prob, win_return, loss_return):
    """Optimal fraction of capital to risk."""
    # f* = (p * b - q) / b
    # where p = win prob, q = lose prob, b = win/loss ratio
    q = 1 - win_prob
    b = abs(win_return / loss_return)
    f = (win_prob * b - q) / b
    return max(0, min(f, 0.25))  # Cap at 25% of capital

# Example: 60% win rate, avg win +2%, avg loss -0.5%
# kelly = (0.6 * 4 - 0.4) / 4 = 0.5 → bet 50% of capital
# With half-Kelly (safer): 25%
```

### Level 5: Meta-Learning (Long-Term)

The system should learn *which conditions make its signals unreliable* and automatically widen stops or reduce size. Example: "After 3 consecutive IRAN_ESCALATION signals in one day, the 4th signal has a 20% win rate" → auto-reduce or skip.

---

## ☢️ PART 5: THE IRAN TRADE — Next 2 Weeks

Here's my honest assessment of the current situation:

### The Setup
- Active US military operations against Iran (strikes ongoing)
- Peace talk cycle in play (Pakistan mediating, Trump saying "Tehran is talking sense")
- VIX at 26.95 (elevated, HIGH_SENSITIVITY regime)
- Your data shows: IRAN posts correlate with major moves

### The Problem With Your Current Iran Trades
Your IRAN_ESCALATION signal is **too blunt**. It fires on any Iran-related headline, but:
- "Trump says Iran is talking sense" is DEESCALATION (bullish SPY/QQQ), not escalation
- "Traders placed $580mn in oil bets ahead of Trump's Iran post" is ALREADY PRICED IN, not a new signal
- Your categorizer put all of these as IRAN_ESCALATION because it matched keywords like "iran" and "military"

**Result: You bought UVIX (short market) on a peace-talk headline.** That's the opposite of what you should have done.

### Highest Conviction Iran Trades (Next 2 Weeks)

**Trade 1: GLD Swing (HIGHEST CONVICTION)**
- Entry: Now or any Iran headline
- Direction: LONG GLD
- Size: $5,000
- Target: +3% (7-10 days)
- Stop: -1.5%
- Thesis: Gold wins in *both* escalation AND deescalation scenarios during active conflict. Your own data shows +2.3% weekly on ANY Trump post with 95% win rate. During active war, gold is the safest bet regardless of direction. This is your best risk/reward trade.
- Conviction: VERY HIGH

**Trade 2: Escalation Pair (If Real Strike Occurs)**
- Trigger: Confirmed new military strike (not just rhetoric)
- Entry: XLE LONG + LMT LONG (split $5,000 between them)
- Target: +5% each (5-7 days)
- Stop: -2%
- Thesis: Actual strikes (not talk) drive oil and defense multi-day. The key is CONFIRMED ACTION, not posts about possible action.
- Conviction: HIGH (conditional on trigger)

**Trade 3: Peace Snap Rally (If Deal Announced)**
- Trigger: Actual ceasefire or framework agreement
- Entry: QQQ LONG
- Size: $5,000
- Target: +4% (3 days)
- Stop: -1.5%
- Thesis: Real peace deal = massive risk-on rally. QQQ leads recoveries.
- Conviction: HIGH (conditional on trigger)

**DO NOT trade: UVIX on Iran headlines.** UVIX is a wasting asset (contango decay). At VIX 27, much of the fear is already priced in. UVIX is only profitable if VIX *spikes further* (to 35+), which requires a genuine escalation shock, not another "talks are ongoing" headline.

---

## 🏗️ PART 6: PORTFOLIO CONSTRUCTION

### How Scalps and Swings Should Interact

**Current problem:** Your scalp and swing engines operate independently. Both can open positions in the same ticker. Both draw from the same $100k account. There's no coordination.

**Correct architecture:**

```
TOTAL CAPITAL: $100,000
├── SCALP ALLOCATION: $15,000 (15%)
│   ├── Max 2 concurrent positions
│   ├── $2,500 each, max $7,500 deployed
│   └── Same-day exits only
├── SWING ALLOCATION: $25,000 (25%)
│   ├── Max 3 concurrent positions
│   ├── $5,000-$8,000 each
│   └── 3-10 day holds
├── CASH RESERVE: $60,000 (60%)
│   └── Dry powder for high-conviction opportunities
```

**Rules:**
1. Scalps and swings NEVER hold the same ticker simultaneously
2. Total portfolio exposure never exceeds 40% ($40k)
3. Cash reserve exists to survive drawdowns and buy major dips
4. Swing positions get priority over scalps (bigger edge, lower cost)

### Optimal Position Count
- Scalps: 1-2 concurrent (you can't monitor more with a cron job)
- Swings: 2-3 concurrent (max diversification without diluting conviction)
- Total: 3-5 positions max at any time

---

## 🚪 PART 7: EXIT STRATEGY SOPHISTICATION

Your current exits: fixed % target OR time limit. Here's what a real quant desk uses:

### 1. Trailing Stop (MUST IMPLEMENT)
Instead of a fixed stop at -0.5%, use a trailing stop that follows the price up:

```python
def trailing_stop(entry_price, highest_price_since_entry, trail_pct=0.5):
    """Stop rises with price but never falls."""
    stop_price = highest_price_since_entry * (1 - trail_pct/100)
    return max(stop_price, entry_price * (1 - 0.5/100))  # Never below initial stop
```

This locks in profits as the trade works. If UVIX goes from $8.08 to $9.00 (+11%), your stop moves from $7.68 to $8.55. You capture the move without giving it all back.

### 2. Time-Weighted Exits
The value of a scalp signal decays over time. If your target is +1.5% and after 2 hours you're at +0.5%, that's a half-win — take it. Don't wait for the full target to hit.

```python
def time_adjusted_target(original_target_pct, hours_held, max_hours=4):
    """Reduce target over time — take what the market gives you."""
    decay = max(0.3, 1.0 - (hours_held / max_hours) * 0.5)
    return original_target_pct * decay
```

### 3. Volatility-Adjusted Stops
In HIGH_SENSITIVITY regime (VIX > 25), price swings are wider. Your stops should be wider too, or you'll get stopped out by normal noise:

```python
def vol_adjusted_stop(base_stop_pct, vix):
    """Widen stops in high-vol environments."""
    if vix > 30:
        return base_stop_pct * 1.5
    elif vix > 25:
        return base_stop_pct * 1.25
    return base_stop_pct
```

### 4. Partial Exits
Sell half at target 1, let the rest ride with a trailing stop. This is the single most effective exit strategy for directional trades:
- Hit +1%? Sell 50%, move stop to breakeven on the rest
- Hit +2%? Sell 25% more, trail the rest
- This guarantees you bank *some* profit on every winner

---

## 📋 PART 8: FEATURE ROADMAP (Ranked by P&L Impact)

| Priority | Feature | Expected Impact | Effort | Timeline |
|----------|---------|----------------|--------|----------|
| 🔴 1 | **Fix dedup / signal cooldown** | Stops bleeding (saves $500+/week) | 2 hours | Tonight |
| 🔴 2 | **Fix EOD reconciliation** | Prevents position leaks | 1 hour | Tonight |
| 🔴 3 | **Fix learning log routing** | Makes learning engine actually work | 2 hours | Tonight |
| 🟡 4 | **Add risk bucket limits** | Prevents correlated blowups | 3 hours | This week |
| 🟡 5 | **Improve categorizer (Iran escalation vs deescalation)** | Stops buying UVIX on peace news | 4 hours | This week |
| 🟡 6 | **Trailing stops + partial exits** | +15-25% improvement on win rate | 4 hours | This week |
| 🟡 7 | **VIX term structure check for UVIX trades** | Eliminates contango losers | 2 hours | This week |
| 🟢 8 | **Congressional trading data feed** | New alpha source, +5-10% edge | 6 hours | Next week |
| 🟢 9 | **Feature-based ML model (XGBoost)** | Conditional win rates, smarter sizing | 8 hours | Next week |
| 🟢 10 | **Truth Social direct feed** | 10-min faster signal, significant alpha | 4 hours | Next week |

---

## 💡 FINAL THOUGHTS

Ron, you have a real system. That matters. Most "algo traders" have a spreadsheet and a dream. You have live code, live data, and a learning loop. The foundation is solid.

But right now, the system is **trading volume, not trading well**. 728 trades with a 40% win rate on your primary signal means you're losing money on net. The fix isn't more trades or more signals — it's **fewer, higher-quality trades with better risk controls.**

The meta-lesson from your 35,613-post dataset is this: **the alpha isn't in the headlines, it's in the market structure around the headlines.** The headline tells you *what* to trade. VIX term structure, options flow, time of day, and congressional positioning tell you *whether* to trade.

Fix the three critical bugs tonight. Implement the risk bucket and trailing stop this week. Add congressional data and the ML model next week. By mid-April, you should have a system that trades 2-3 times per week (not per hour) with a 60%+ win rate and meaningful position sizing.

That's how you get to real money.

—Jeeves

---

*P.S. — That FT headline about "$580mn in oil bets ahead of Trump's Iran post" is actually the most important signal you received today. It means large players are already positioned. When you see that kind of headline, the move is largely priced in. Smart money bought oil BEFORE the post. You should be looking to sell into that strength, not buy more.*
