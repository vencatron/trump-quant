"""Categorize Trump posts by topic using keyword matching and simple NLP."""

import json
import os
import re

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
INPUT_FILE = os.path.join(DATA_DIR, "posts.json")
OUTPUT_FILE = os.path.join(DATA_DIR, "posts_categorized.json")

CATEGORIES = {
    "TARIFFS": [
        "tariff", "tariffs", "trade war", "china", "canada", "mexico",
        "import", "duty", "duties", "reciprocal", "european union",
    ],
    "CRYPTO": [
        "bitcoin", "crypto", "digital currency", "coin", "blockchain",
        "ethereum", "xrp", "solana", "cardano", "btc", "crypto reserve",
    ],
    "STOCKS_BULLISH": [
        "great", "winning", "up", "best ever", "deal", "agreement",
        "record", "incredible", "tremendous", "love", "amazing",
        "through the roof", "record high",
    ],
    "STOCKS_BEARISH": [
        "fake news", "rigged", "disaster", "witch hunt", "attack",
        "enemy", "hoax", "terrible", "worst",
    ],
    "FED_ATTACK": [
        "powell", "federal reserve", "interest rate", "cut rates",
        "rates should be", "fed ", "the fed",
    ],
    "TRADE_DEAL": [
        "trade deal", "deal", "agreement", "signed", "negotiated",
        "good faith", "delayed",
    ],
    "MARKET_PUMP": [
        "stock market", "dow", "nasdaq", "s&p", "record high",
        "all-time high", "market is up", "markets",
    ],
    "IRAN_ESCALATION": [
        "iran", "tehran", "hormuz", "ayatollah", "nuclear",
        "persian gulf", "military operation", "middle east",
        "hostilities", "bombing", "strike",
    ],
    "IRAN_DEESCALATION": [
        "iran deal", "iran peace", "winding down", "cease",
        "negotiate with iran", "iran agreement", "iran surrender",
    ],
    "OIL_SHOCK": [
        "oil", "crude", "barrel", "opec", "energy prices",
        "gasoline", "petroleum",
    ],
    "WAR_GENERAL": [
        "military", "troops", "bombs", "strike", "attack",
        "airstrike", "warship", "navy", "pentagon",
    ],
    "WAR_ESCALATION": [
        "war", "invasion", "deploy troops", "bombs", "airstrike",
        "warship", "carrier group", "mobilize", "military strike",
    ],
    "MUSK_TRUMP": [
        "musk", "tesla", "elon", "doge", "spacex", "x.com",
    ],
}

# Ticker / company name mapping for SPECIFIC_TICKER detection
TICKER_MENTIONS = {
    "DJT": ["djt", "truth social", "trump media"],
    "TSLA": ["tesla", "tsla", "elon musk", "elon"],
    "META": ["meta", "facebook", "instagram", "zuckerberg"],
    "NVDA": ["nvidia", "nvda"],
    "GME": ["gamestop", "gme"],
    "COIN": ["coinbase", "coin"],
    "GLD": ["gold", "gld"],
    "BTC-USD": ["bitcoin", "btc"],
    "SPY": ["s&p", "s&p 500", "spy"],
    "QQQ": ["nasdaq", "qqq"],
}


def categorize_post(text: str) -> dict:
    """Return categories and mentioned tickers for a post."""
    text_lower = text.lower()

    categories = []
    scores = {}

    for cat, keywords in CATEGORIES.items():
        matches = [kw for kw in keywords if kw in text_lower]
        if matches:
            categories.append(cat)
            scores[cat] = len(matches)

    # Detect specific ticker mentions
    mentioned_tickers = []
    for ticker, keywords in TICKER_MENTIONS.items():
        for kw in keywords:
            if kw in text_lower:
                mentioned_tickers.append(ticker)
                break

    if mentioned_tickers and "SPECIFIC_TICKER" not in categories:
        categories.append("SPECIFIC_TICKER")

    # Post-process IRAN_ESCALATION: require military words AND no peace words
    MILITARY_WORDS = ['bomb', 'strike', 'airstrike', 'troops', 'military operation', 'attack', 'explosion', 'missiles', 'bombing', 'hostilities']
    PEACE_BLOCKERS = ['deal', 'peace', 'postpone', 'ceasefire', 'negotiations', 'talks', 'resolve', 'wind down', 'agreement', 'surrender', 'winding down']
    if 'IRAN_ESCALATION' in categories:
        has_military = any(w in text_lower for w in MILITARY_WORDS)
        has_peace = any(w in text_lower for w in PEACE_BLOCKERS)
        if has_peace or not has_military:
            categories.remove('IRAN_ESCALATION')
            if 'IRAN_DEESCALATION' not in categories:
                categories.append('IRAN_DEESCALATION')

    # Sentiment score: simple positive/negative word count
    positive_words = [
        "great", "best", "winning", "love", "incredible", "tremendous",
        "record", "high", "up", "amazing", "beautiful", "fantastic",
        "moon", "win", "success",
    ]
    negative_words = [
        "disaster", "terrible", "worst", "fake", "rigged", "enemy",
        "witch hunt", "attack", "killing", "ripping", "unfairly",
    ]
    pos_count = sum(1 for w in positive_words if w in text_lower)
    neg_count = sum(1 for w in negative_words if w in text_lower)
    sentiment = (pos_count - neg_count) / max(pos_count + neg_count, 1)

    return {
        "categories": categories,
        "category_scores": scores,
        "mentioned_tickers": list(set(mentioned_tickers)),
        "sentiment": round(sentiment, 3),
    }


def main():
    with open(INPUT_FILE) as f:
        posts = json.load(f)

    categorized = []
    for post in posts:
        result = categorize_post(post["text"])
        categorized.append({
            **post,
            **result,
        })

    with open(OUTPUT_FILE, "w") as f:
        json.dump(categorized, f, indent=2)

    # Print summary
    cat_counts = {}
    for p in categorized:
        for c in p["categories"]:
            cat_counts[c] = cat_counts.get(c, 0) + 1

    print(f"Categorized {len(categorized)} posts:")
    for cat, count in sorted(cat_counts.items(), key=lambda x: -x[1]):
        print(f"  {cat}: {count} posts")
    print(f"\nSaved to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
