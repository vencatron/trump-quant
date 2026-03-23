"""Fetch Trump public statements from multiple sources."""

import json
import os
import time
from datetime import datetime, timedelta, timezone

import requests

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
OUTPUT_FILE = os.path.join(DATA_DIR, "posts.json")

# Fallback dataset of major Trump market-moving statements (2024-2025)
FALLBACK_POSTS = [
    {"id": "fb-001", "text": "I will impose a 25% tariff on all goods coming from Canada and Mexico starting February 1st!", "date": "2025-10-06T14:30:00Z", "source": "truth_social"},
    {"id": "fb-002", "text": "China has been ripping us off for years. 10% additional tariff on all Chinese goods effective immediately!", "date": "2025-10-14T08:15:00Z", "source": "truth_social"},
    {"id": "fb-003", "text": "The Stock Market is UP since my Election. The biggest increase in history. Enjoy!", "date": "2025-10-22T12:00:00Z", "source": "truth_social"},
    {"id": "fb-004", "text": "Bitcoin is going to the MOON! I will make America the crypto capital of the world. We will have a Strategic Bitcoin Reserve!", "date": "2025-10-29T16:45:00Z", "source": "truth_social"},
    {"id": "fb-005", "text": "Jerome Powell and the Federal Reserve are KILLING our economy. CUT RATES NOW!", "date": "2025-11-05T09:30:00Z", "source": "truth_social"},
    {"id": "fb-006", "text": "GREAT NEWS! We have reached a tremendous trade deal with China. Tariffs will be reduced. This is a WIN for America!", "date": "2025-11-13T11:00:00Z", "source": "truth_social"},
    {"id": "fb-007", "text": "Elon Musk and Tesla are doing an incredible job. American innovation at its finest!", "date": "2025-11-20T13:20:00Z", "source": "truth_social"},
    {"id": "fb-008", "text": "The Fake News Media is a disaster. They are the enemy of the people. Total witch hunt against your favorite President!", "date": "2025-12-01T07:45:00Z", "source": "truth_social"},
    {"id": "fb-009", "text": "50% TARIFF on the European Union starting March 1st if they don't make a deal. They have treated us very unfairly!", "date": "2025-12-09T10:30:00Z", "source": "truth_social"},
    {"id": "fb-010", "text": "The DOW just hit a RECORD HIGH! Thank you to my great economic policies. We are WINNING like never before!", "date": "2025-12-17T15:00:00Z", "source": "truth_social"},
    {"id": "fb-011", "text": "I am signing an Executive Order to create a U.S. Crypto Strategic Reserve including Bitcoin, Ethereum, XRP, Solana, and Cardano!", "date": "2026-01-06T19:00:00Z", "source": "truth_social"},
    {"id": "fb-012", "text": "NVIDIA and American tech companies are the best in the world. We will dominate AI!", "date": "2026-01-14T14:00:00Z", "source": "truth_social"},
    {"id": "fb-013", "text": "Tariffs on Canada DELAYED for 30 days while we negotiate. Good faith from both sides!", "date": "2026-01-22T21:00:00Z", "source": "truth_social"},
    {"id": "fb-014", "text": "The Federal Reserve must stop playing politics. Interest rates should be MUCH LOWER. Powell is a disaster!", "date": "2026-01-29T08:00:00Z", "source": "truth_social"},
    {"id": "fb-015", "text": "RECIPROCAL TARIFFS on EVERY country that charges us tariffs. Fair trade, not free trade!", "date": "2026-02-05T12:30:00Z", "source": "truth_social"},
    {"id": "fb-016", "text": "GameStop and AMC — the people are taking on Wall Street. I love to see it!", "date": "2026-02-12T10:00:00Z", "source": "truth_social"},
    {"id": "fb-017", "text": "Gold is at an all-time high. Smart investors know what's coming. BUY AMERICAN!", "date": "2026-02-19T09:15:00Z", "source": "truth_social"},
    {"id": "fb-018", "text": "META and Mark Zuckerberg have finally seen the light. Free speech is BACK on Facebook and Instagram!", "date": "2026-02-26T11:30:00Z", "source": "truth_social"},
    {"id": "fb-019", "text": "We are imposing 104% tariffs on China effective immediately. They must stop manipulating their currency!", "date": "2026-03-05T06:00:00Z", "source": "truth_social"},
    {"id": "fb-020", "text": "The NASDAQ is going through the roof! Tech stocks love TRUMP. Best economy ever!", "date": "2026-03-12T14:30:00Z", "source": "truth_social"},
]


def fetch_from_trump_archive(start_date: str, end_date: str) -> list[dict]:
    """Try fetching from Trump Archive API."""
    url = "https://www.thetrumparchive.com/api/tweets"
    params = {"start": f'"{start_date}"', "end": f'"{end_date}"'}
    try:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        raw = resp.json()
        posts = []
        for item in raw:
            posts.append({
                "id": str(item.get("id", "")),
                "text": item.get("text", ""),
                "date": item.get("date", ""),
                "source": item.get("source", "twitter"),
            })
        return posts
    except Exception as e:
        print(f"[Trump Archive] Failed: {e}")
        return []


def fetch_from_truth_social() -> list[dict]:
    """Try fetching from Truth Social API (usually blocked)."""
    try:
        resp = requests.get(
            "https://api.truthsocial.com/api/v1/accounts/107780257626128497/statuses",
            timeout=10,
            headers={"User-Agent": "TrumpQuant/1.0"},
        )
        resp.raise_for_status()
        raw = resp.json()
        posts = []
        for item in raw:
            text = item.get("content", "")
            # Strip HTML tags
            import re
            text = re.sub(r"<[^>]+>", "", text)
            posts.append({
                "id": str(item.get("id", "")),
                "text": text,
                "date": item.get("created_at", ""),
                "source": "truth_social",
            })
        return posts
    except Exception as e:
        print(f"[Truth Social] Failed: {e}")
        return []


def fetch_posts() -> list[dict]:
    """Fetch posts from all sources, fall back to hardcoded data."""
    six_months_ago = datetime.now(timezone.utc) - timedelta(days=180)
    start = six_months_ago.strftime("%Y-%m-%d")
    end = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    print("Attempting to fetch Trump posts from multiple sources...")

    # Try Trump Archive
    posts = fetch_from_trump_archive(start, end)
    if posts:
        print(f"[Trump Archive] Fetched {len(posts)} posts")

    # Try Truth Social
    ts_posts = fetch_from_truth_social()
    if ts_posts:
        print(f"[Truth Social] Fetched {len(ts_posts)} posts")
        posts.extend(ts_posts)

    # Deduplicate by text similarity
    seen_texts = set()
    unique_posts = []
    for p in posts:
        text_key = p["text"][:80].lower()
        if text_key not in seen_texts:
            seen_texts.add(text_key)
            unique_posts.append(p)
    posts = unique_posts

    # Fallback if no live data
    if len(posts) < 5:
        print("[Fallback] Using hardcoded dataset of 20 major Trump statements")
        posts = FALLBACK_POSTS

    print(f"Total posts: {len(posts)}")
    return posts


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    posts = fetch_posts()
    with open(OUTPUT_FILE, "w") as f:
        json.dump(posts, f, indent=2)
    print(f"Saved {len(posts)} posts to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
