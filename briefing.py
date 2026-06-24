#!/usr/bin/env python3
"""
Daily Crypto Financial Briefing Agent
Fetches top 10 cryptos + top 10 news, then generates an AI briefing via Claude.

Usage:
    export ANTHROPIC_API_KEY="sk-ant-..."
    python3 briefing.py

Schedule daily (cron example — runs at 7:00 AM):
    0 7 * * * /usr/bin/python3 /path/to/crypto-briefing/briefing.py >> ~/crypto-briefing.log 2>&1
"""

import io
import os
import re
import sys
from pathlib import Path
import requests
import feedparser
import anthropic
from datetime import datetime, timedelta
from textwrap import dedent

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

# Load .env file from the script's directory if present
_env_file = Path(__file__).parent / ".env"
if _env_file.exists():
    for line in _env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


# ── Data Fetching ──────────────────────────────────────────────────────────────

SPOTLIGHT_IDS = ["solana", "chainlink", "sui"]

def fetch_spotlight_coins() -> list[dict]:
    """Fetch Solana, Chainlink, and Sui data from CoinGecko."""
    url = "https://api.coingecko.com/api/v3/coins/markets"
    params = {
        "vs_currency": "usd",
        "ids": ",".join(SPOTLIGHT_IDS),
        "order": "market_cap_desc",
        "sparkline": False,
        "price_change_percentage": "24h,7d",
    }
    try:
        resp = requests.get(url, params=params, headers={"User-Agent": UA}, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        print(f"  ⚠  Spotlight fetch error: {e}", file=sys.stderr)
        return []


PORTFOLIO_SYMBOLS = ["NEE", "SPCX"]

def _fetch_single_stock(symbol: str) -> dict:
    try:
        import yfinance as yf
        info = yf.Ticker(symbol).info
        price      = info.get("currentPrice") or info.get("regularMarketPrice") or 0
        prev_close = info.get("previousClose") or 0
        change     = round(price - prev_close, 2)
        change_pct = round(info.get("regularMarketChangePercent", 0), 2)
        return {
            "symbol":      symbol,
            "name":        info.get("longName", symbol),
            "price":       price,
            "change":      change,
            "change_pct":  change_pct,
            "prev_close":  prev_close,
            "day_high":    info.get("dayHigh") or 0,
            "day_low":     info.get("dayLow") or 0,
            "volume":      info.get("volume") or 0,
            "market_cap":  info.get("marketCap") or 0,
            "week52_high": info.get("fiftyTwoWeekHigh") or 0,
            "week52_low":  info.get("fiftyTwoWeekLow") or 0,
        }
    except Exception as e:
        print(f"  ⚠  {symbol} stock fetch error: {e}", file=sys.stderr)
        return {"symbol": symbol, "name": symbol, "price": 0}


def fetch_portfolio_stock() -> list[dict]:
    """Fetch live price and key stats for every portfolio stock."""
    return [_fetch_single_stock(sym) for sym in PORTFOLIO_SYMBOLS]


def _fetch_single_news(symbol: str, limit: int) -> list[dict]:
    try:
        import yfinance as yf
        news = yf.Ticker(symbol).news or []
        items = []
        for item in news[:limit]:
            c = item.get("content", {})
            title = c.get("title", "")
            if not title:
                continue
            items.append({
                "symbol":    symbol,
                "title":     title,
                "summary":   c.get("summary", ""),
                "published": c.get("pubDate", "")[:10],
                "source":    c.get("provider", {}).get("displayName", ""),
                "url":       c.get("canonicalUrl", {}).get("url", ""),
            })
        return items
    except Exception as e:
        print(f"  ⚠  {symbol} news fetch error: {e}", file=sys.stderr)
        return []


def fetch_portfolio_news(limit_per_symbol: int = 3) -> list[dict]:
    """Fetch latest news for every portfolio stock (sorted newest first)."""
    items = []
    for sym in PORTFOLIO_SYMBOLS:
        items.extend(_fetch_single_news(sym, limit_per_symbol))
    items.sort(key=lambda x: x["published"], reverse=True)
    return items


def fetch_top_cryptos(limit: int = 10) -> list[dict]:
    """Fetch top cryptocurrencies by market cap from CoinGecko (free, no key)."""
    url = "https://api.coingecko.com/api/v3/coins/markets"
    params = {
        "vs_currency": "usd",
        "order": "market_cap_desc",
        "per_page": limit,
        "page": 1,
        "sparkline": False,
        "price_change_percentage": "24h,7d",
    }
    try:
        resp = requests.get(url, params=params, headers={"User-Agent": UA}, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        print(f"  ⚠  CoinGecko error: {e}", file=sys.stderr)
        return []


NEWS_FEEDS = [
    ("CoinDesk",      "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("CoinTelegraph", "https://cointelegraph.com/rss"),
    ("Decrypt",       "https://decrypt.co/feed"),
    ("Bitcoin.com",   "https://news.bitcoin.com/feed/"),
    ("CryptoNews",    "https://cryptonews.com/news/feed/"),
]

TECH_FEEDS = [
    ("TechCrunch AI",  "https://techcrunch.com/category/artificial-intelligence/feed/"),
    ("VentureBeat AI", "https://venturebeat.com/category/ai/feed/"),
    ("DC Dynamics",    "https://www.datacenterdynamics.com/en/rss/"),
    ("The Verge AI",   "https://www.theverge.com/ai-artificial-intelligence/rss/index.xml"),
    ("CoinDesk",       "https://www.coindesk.com/arc/outboundfeeds/rss/"),
]


def fetch_tech_news(limit: int = 10) -> list[dict]:
    """Fetch latest AI / blockchain / data-center news from RSS feeds."""
    items: list[dict] = []
    seen: set[str] = set()
    for source, url in TECH_FEEDS:
        if len(items) >= limit:
            break
        try:
            resp = requests.get(url, headers={"User-Agent": UA}, timeout=10)
            resp.raise_for_status()
            feed = feedparser.parse(io.BytesIO(resp.content))
            for entry in feed.entries[:5]:
                if len(items) >= limit:
                    break
                title = entry.get("title", "").strip()
                if not title or title in seen:
                    continue
                seen.add(title)
                raw_summary = entry.get("summary", entry.get("description", "")).strip()
                summary = re.sub(r"<[^>]+>", "", raw_summary)[:300]
                items.append({
                    "source":    source,
                    "title":     title,
                    "summary":   summary,
                    "published": entry.get("published", "")[:16],
                })
        except Exception as e:
            print(f"  ⚠  {source} RSS error: {e}", file=sys.stderr)
    return items[:limit]


HODL_BUCKETS = [
    ("10+ years",    "age_10y"),
    ("7–10 years",   "age_7y_10y"),
    ("5–7 years",    "age_5y_7y"),
    ("4–5 years",    "age_4y_5y"),
    ("3–4 years",    "age_3y_4y"),
    ("2–3 years",    "age_2y_3y"),
    ("1–2 years",    "age_1y_2y"),
    ("6–12 months",  "age_6m_1y"),
    ("3–6 months",   "age_3m_6m"),
    ("1–3 months",   "age_1m_3m"),
    ("1 week–1 mo",  "age_1w_1m"),
    ("1 day–1 wk",   "age_1d_1w"),
    ("<1 day",       "age_0d_1d"),
]

# bitcoin-data.com free tier is 10 requests/hour; HODL data updates daily.
# Cache on disk so the data survives server restarts AND rate-limit windows.
_HODL_CACHE_FILE = Path(__file__).parent / ".hodl_cache.json"
_HODL_TTL_SECONDS = 6 * 3600

def _load_hodl_cache() -> dict:
    if not _HODL_CACHE_FILE.exists():
        return {}
    try:
        import json
        return json.loads(_HODL_CACHE_FILE.read_text())
    except Exception:
        return {}

def _save_hodl_cache(payload: dict) -> None:
    try:
        import json
        _HODL_CACHE_FILE.write_text(json.dumps(payload))
    except Exception as e:
        print(f"  ⚠  HODL cache save error: {e}", file=sys.stderr)

def fetch_btc_hodl_waves() -> dict:
    """Fetch BTC HODL waves (supply % by age) from bitcoin-data.com — free, no key."""
    import time
    cache = _load_hodl_cache()
    now   = time.time()
    if cache.get("data") and (now - cache.get("ts", 0)) < _HODL_TTL_SECONDS:
        return cache["data"]

    url = "https://bitcoin-data.com/api/v1/realized-cap-hodl-waves/last"
    try:
        resp = requests.get(url, headers={"User-Agent": UA}, timeout=15)
        resp.raise_for_status()
        r = resp.json()
        buckets = []
        for label, key in HODL_BUCKETS:
            pct = float(r.get(key, 0)) * 100  # API returns decimal fractions
            buckets.append({"label": label, "pct": round(pct, 2)})
        result = {"as_of": r.get("d", ""), "buckets": buckets}
        _save_hodl_cache({"data": result, "ts": now})
        return result
    except Exception as e:
        print(f"  ⚠  HODL waves fetch error: {e}", file=sys.stderr)
        return cache.get("data") or {}


def fetch_btc_sentiment() -> dict:
    """Fetch BTC Fear & Greed Index + 24h volume/market data for hold-vs-sell read."""
    fg = {}
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=7", timeout=10)
        r.raise_for_status()
        data = r.json().get("data", [])
        if data:
            today = data[0]
            week  = data[-1] if len(data) > 1 else today
            fg = {
                "value":          int(today["value"]),
                "classification": today["value_classification"],
                "week_ago_value": int(week["value"]),
                "week_ago_class": week["value_classification"],
            }
    except Exception as e:
        print(f"  ⚠  Fear & Greed fetch error: {e}", file=sys.stderr)

    btc = {}
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/coins/bitcoin",
            params={"localization": "false", "tickers": "false",
                    "community_data": "false", "developer_data": "false"},
            headers={"User-Agent": UA}, timeout=15)
        r.raise_for_status()
        d = r.json()
        md = d.get("market_data", {})
        btc = {
            "price":          md.get("current_price", {}).get("usd", 0),
            "chg_24h":        md.get("price_change_percentage_24h", 0) or 0,
            "chg_7d":         md.get("price_change_percentage_7d", 0) or 0,
            "volume_24h":     md.get("total_volume", {}).get("usd", 0),
            "market_cap":     md.get("market_cap", {}).get("usd", 0),
            "ath":            md.get("ath", {}).get("usd", 0),
            "ath_change_pct": md.get("ath_change_percentage", {}).get("usd", 0) or 0,
        }
    except Exception as e:
        print(f"  ⚠  BTC sentiment fetch error: {e}", file=sys.stderr)

    return {"fear_greed": fg, "btc": btc}

def fetch_crypto_news(limit: int = 10) -> list[dict]:
    """Fetch top crypto news from multiple RSS feeds."""
    items: list[dict] = []
    seen_titles: set[str] = set()

    for source, url in NEWS_FEEDS:
        if len(items) >= limit:
            break
        try:
            resp = requests.get(url, headers={"User-Agent": UA}, timeout=10)
            resp.raise_for_status()
            feed = feedparser.parse(io.BytesIO(resp.content))

            for entry in feed.entries:
                if len(items) >= limit:
                    break
                title = entry.get("title", "Untitled").strip()
                if title in seen_titles:
                    continue
                seen_titles.add(title)

                raw_summary = entry.get("summary", entry.get("description", "")).strip()
                summary = re.sub(r"<[^>]+>", "", raw_summary)[:300]

                items.append({
                    "source":    source,
                    "title":     title,
                    "summary":   summary,
                    "published": entry.get("published", "")[:16],
                })
        except Exception as e:
            print(f"  ⚠  {source} RSS error: {e}", file=sys.stderr)

    return items[:limit]


# ── Data Formatting ────────────────────────────────────────────────────────────

def format_crypto_table(coins: list[dict]) -> str:
    rows = []
    for i, c in enumerate(coins, 1):
        price   = c.get("current_price", 0) or 0
        chg_24h = c.get("price_change_percentage_24h", 0) or 0
        chg_7d  = c.get("price_change_percentage_7d_in_currency", 0) or 0
        mkt_cap = c.get("market_cap", 0) or 0
        volume  = c.get("total_volume", 0) or 0
        symbol  = c.get("symbol", "???").upper()
        name    = c.get("name", "Unknown")

        rows.append(
            f"{i:>2}. {name} ({symbol})\n"
            f"    Price: ${price:>14,.4f}  |  24h: {chg_24h:+6.2f}%  |  7d: {chg_7d:+6.2f}%\n"
            f"    Mkt Cap: ${mkt_cap:>15,.0f}  |  24h Vol: ${volume:,.0f}"
        )
    return "\n\n".join(rows)


def format_news_list(news: list[dict]) -> str:
    rows = []
    for i, item in enumerate(news, 1):
        rows.append(
            f"{i:>2}. [{item['source']}]  {item['title']}\n"
            f"    {item['published']}\n"
            f"    {item['summary'][:250]}..."
        )
    return "\n\n".join(rows)


# ── Claude Briefing ────────────────────────────────────────────────────────────

SYSTEM_PROMPT = dedent("""\
    You are a professional crypto financial analyst delivering a concise daily market briefing.
    Your tone is sharp, data-driven, and insightful — think Bloomberg terminal meets CoinDesk.

    Structure every briefing with exactly these four sections and headers:

    ═══════════════════════════════════════
    📊  MARKET OVERVIEW
    ═══════════════════════════════════════
    2-3 sentences capturing overall crypto market sentiment and direction today.

    ═══════════════════════════════════════
    🏆  TOP MOVERS
    ═══════════════════════════════════════
    Top 2 gainers and top 2 losers from the data, with specific % moves and brief context.

    ═══════════════════════════════════════
    📰  NEWS HIGHLIGHTS
    ═══════════════════════════════════════
    4 bullet points. Each bullet: what happened + why it matters to markets.
    • ...
    • ...
    • ...
    • ...

    ═══════════════════════════════════════
    🔦  SPOTLIGHT: SOL / LINK / SUI
    ═══════════════════════════════════════
    One paragraph covering Solana, Chainlink, and Sui specifically — price action, % moves,
    and any notable narrative or catalyst for each. Be concise but specific to each coin.

    ═══════════════════════════════════════
    🧠  TECH PULSE: AI / BLOCKCHAIN / DATA CENTERS
    ═══════════════════════════════════════
    4 bullet points summarizing the most important developments across AI, blockchain,
    and data center infrastructure. Each bullet: what happened + why it matters.
    • ...
    • ...
    • ...
    • ...

    ═══════════════════════════════════════
    💎  BTC HODL vs SELL SENTIMENT
    ═══════════════════════════════════════
    2-3 sentences interpreting the Fear & Greed Index, 24h volume, and price action.
    Are holders accumulating or distributing? Is this a HODL environment or a sell-pressure environment?
    Be specific about what the numbers signal.

    ═══════════════════════════════════════
    💡  ANALYST INSIGHT
    ═══════════════════════════════════════
    One sharp, forward-looking observation: a trend, risk, or opportunity worth watching.

    Keep the full briefing under 650 words. No filler text. Be direct and specific.
""")


def generate_briefing(coins: list[dict], news: list[dict],
                      spotlight: list[dict] | None = None,
                      tech_news: list[dict] | None = None,
                      btc_sentiment: dict | None = None) -> str:
    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env

    crypto_txt    = format_crypto_table(coins)
    news_txt      = format_news_list(news) if news else "No news data available."
    spotlight_txt = format_crypto_table(spotlight) if spotlight else "Data unavailable."
    tech_txt      = format_news_list(tech_news) if tech_news else "No tech news available."

    btc = (btc_sentiment or {}).get("btc", {}) or {}
    fg  = (btc_sentiment or {}).get("fear_greed", {}) or {}
    sentiment_txt = dedent(f"""\
        Fear & Greed Index: {fg.get('value', 'N/A')} ({fg.get('classification', 'N/A')})
            7 days ago:    {fg.get('week_ago_value', 'N/A')} ({fg.get('week_ago_class', 'N/A')})
        BTC price:         ${btc.get('price', 0):,.2f}
        BTC 24h change:    {btc.get('chg_24h', 0):+.2f}%
        BTC 7d change:     {btc.get('chg_7d', 0):+.2f}%
        BTC 24h volume:    ${btc.get('volume_24h', 0):,.0f}
        BTC market cap:    ${btc.get('market_cap', 0):,.0f}
        From ATH:          {btc.get('ath_change_pct', 0):+.2f}%
    """)

    date_str = datetime.now().strftime("%A, %B %d, %Y  %H:%M")

    user_message = dedent(f"""\
        Date: {date_str}

        ─── TOP 10 CRYPTOCURRENCIES BY MARKET CAP ───
        {crypto_txt}

        ─── SPOTLIGHT COINS: SOLANA / CHAINLINK / SUI ───
        {spotlight_txt}

        ─── TOP 10 CRYPTO NEWS STORIES ───
        {news_txt}

        ─── AI / BLOCKCHAIN / DATA CENTER NEWS ───
        {tech_txt}

        ─── BTC SENTIMENT DATA ───
        {sentiment_txt}

        Generate today's daily crypto briefing.
    """)

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=3072,
        system=[
            {
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},  # stable system prompt is cached
            }
        ],
        messages=[{"role": "user", "content": user_message}],
    )

    usage         = response.usage
    cache_read    = getattr(usage, "cache_read_input_tokens", 0) or 0
    cache_created = getattr(usage, "cache_creation_input_tokens", 0) or 0

    print(
        f"  [tokens]  in: {usage.input_tokens}  "
        f"cache-write: {cache_created}  cache-read: {cache_read}  "
        f"out: {usage.output_tokens}"
    )

    return response.content[0].text


def generate_summary(briefing_text: str) -> str:
    """Generate a short plain-text summary of the briefing, suitable for SMS/email."""
    client = anthropic.Anthropic()
    date_str = datetime.now().strftime("%A, %B %d, %Y")
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=300,
        messages=[{
            "role": "user",
            "content": dedent(f"""\
                Summarize this crypto briefing in 4-5 sentences of plain text.
                No markdown, no bullet points, no headers — just clean prose.
                Include: overall market direction, the biggest mover, a note on SOL/LINK/SUI, and one forward-looking point.
                Start with the date: {date_str}.

                BRIEFING:
                {briefing_text}
            """),
        }],
    )
    return response.content[0].text


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    if not os.getenv("ANTHROPIC_API_KEY"):
        print(
            "❌  ANTHROPIC_API_KEY is not set.\n"
            "    Run:  export ANTHROPIC_API_KEY='sk-ant-...'\n"
            "    Then re-run this script.",
            file=sys.stderr,
        )
        sys.exit(1)

    now = datetime.now()
    print(
        "\n╔══════════════════════════════════════════════════════════╗\n"
        "║         🪙  DAILY CRYPTO FINANCIAL BRIEFING  🪙           ║\n"
        f"║         {now.strftime('%A, %B %d, %Y  •  %H:%M'):<48}║\n"
        "╚══════════════════════════════════════════════════════════╝"
    )

    print("\n📡  Fetching top 10 cryptocurrencies from CoinGecko…")
    coins = fetch_top_cryptos()
    if not coins:
        print("  ❌  Could not fetch crypto data. Check your internet connection.")
        sys.exit(1)
    print(f"  ✓  {len(coins)} coins loaded")

    print("\n📰  Fetching latest crypto news from RSS feeds…")
    news = fetch_crypto_news()
    if not news:
        print("  ⚠  No news fetched — briefing will rely on market data only.")
    else:
        print(f"  ✓  {len(news)} news items loaded")

    print("\n🔦  Fetching spotlight coins (SOL / LINK / SUI)…")
    spotlight = fetch_spotlight_coins()
    print(f"  ✓  {len(spotlight)} spotlight coins loaded")

    print("\n🧠  Fetching AI / blockchain / data center news…")
    tech_news = fetch_tech_news()
    print(f"  ✓  {len(tech_news)} tech news items loaded")

    print("\n💎  Fetching BTC sentiment data…")
    btc_sentiment = fetch_btc_sentiment()
    fg_value = btc_sentiment.get("fear_greed", {}).get("value", "N/A")
    print(f"  ✓  Fear & Greed Index: {fg_value}")

    print("\n🤖  Generating AI briefing via Claude Sonnet…")
    briefing = generate_briefing(coins, news, spotlight, tech_news, btc_sentiment)

    print("\n" + "═" * 60)
    print(briefing)
    print("═" * 60 + "\n")


if __name__ == "__main__":
    main()
