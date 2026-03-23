# TrumpQuant

Python analysis tool that correlates Trump public statements with stock market movements to identify patterns and generate short-term trading signals.

**DISCLAIMER:** This is for educational and research purposes only. Past correlations do not guarantee future results. Do your own due diligence before trading.

## Setup

```bash
pip install -r requirements.txt
```

## Usage

Run each script in order:

### 1. Fetch Trump Posts
```bash
python fetch_posts.py
```
Fetches Trump statements from Truth Social / Trump Archive. Falls back to a curated dataset of 20 major market-moving statements if APIs are unavailable.

Output: `data/posts.json`

### 2. Fetch Market Data
```bash
python fetch_market.py
```
Downloads daily and hourly OHLCV data via yfinance for: SPY, QQQ, DJI, NVDA, GME, DJT, TSLA, META, COIN, GLD, BTC-USD.

Output: `data/market_data/*.csv`

### 3. Categorize Posts
```bash
python categorize.py
```
Categorizes each post by topic (TARIFFS, CRYPTO, FED_ATTACK, etc.) and detects specific ticker mentions. Computes a simple sentiment score.

Output: `data/posts_categorized.json`

### 4. Correlation Analysis
```bash
python correlate.py
```
For each post, calculates market returns at multiple windows (same-day, next-day, 1-week) and computes statistical significance per category. Identifies posts that caused >1% SPY moves.

Output: `data/correlation_results.json`

### 5. Signal Playbook
```bash
python signals.py
```
Generates a human-readable trading signal playbook with confidence levels, suggested instruments, and hold durations based on the correlation analysis.

### 6. Live Monitor
```bash
python monitor.py
```
Runs continuously, checking for new Trump posts every 15 minutes via Google News RSS. When a new post is detected, it categorizes it and prints the historical signal for that category.

## Interpreting Signal Output

- **HIGH confidence**: p < 0.01, sample size >= 10 — statistically significant pattern
- **MEDIUM confidence**: p < 0.05, sample size >= 5 — meaningful pattern worth watching
- **LOW confidence**: p < 0.10, sample size >= 5 — weak signal, use with caution
- **SPECULATIVE**: insufficient data or high p-value — not reliable

Key metrics:
- **Mean return**: average market move after posts in this category
- **Win rate**: percentage of times the market moved in the predicted direction
- **P-value**: probability this pattern is due to random chance (lower = more significant)

## Bot Detector (Microstructure Trading)

The Bot Detector extends TrumpQuant into a microstructure-aware trading system. It observes algorithmic bot reactions to Trump posts in real-time and trades the confirmed momentum.

### Setup

```bash
pip install -r requirements.txt
export ALPACA_API_KEY="your-paper-api-key"
export ALPACA_SECRET_KEY="your-paper-secret-key"
```

Paper mode is **always the default**. No live trading without explicit toggle.

### Usage

```bash
# Run as daemon (long-running WebSocket listener)
python -m botdetector daemon

# One-shot detection (called automatically by signal_check.py)
python -m botdetector arm --post-id POST123 --text "Trump tariff announcement" --categories TARIFFS

# Run historical backtest
python -m botdetector backtest

# Check risk state
python -m botdetector status

# Emergency kill switch
python -m botdetector kill --reason "emergency"
python -m botdetector unkill
```

### How It Works

1. `signal_check.py` detects a new Trump post matching a signal category
2. Bot Detector is armed with a 120-second detection window
3. Alpaca WebSocket streams real-time trades and quotes
4. Three criteria are checked: volume spike (3x), price velocity (0.3%), spread widening (50%)
5. If all 3 confirm within 120s, a trade is executed via Alpaca paper trading
6. Risk controls enforce: $500 daily loss limit, 5 trades/day, 2 max positions, kill switch

### Risk Controls

| Control | Default |
|---------|---------|
| Paper mode | Always on |
| Kill switch | `data/kill_switch.flag` |
| Max position | 5% equity or $2,500 |
| Stop loss | 0.5% |
| Take profit | 1.5% |
| Daily loss limit | $500 |
| Max trades/day | 5 |
| Max concurrent positions | 2 |
| Post-loss cooldown | 30 min |

## Tickers Tracked

| Ticker | Description |
|--------|-------------|
| SPY | S&P 500 ETF |
| QQQ | Nasdaq 100 ETF |
| DJI | Dow Jones Industrial Average |
| NVDA | NVIDIA |
| GME | GameStop |
| DJT | Trump Media & Technology |
| TSLA | Tesla |
| META | Meta Platforms |
| COIN | Coinbase |
| GLD | Gold ETF |
| BTC-USD | Bitcoin |
