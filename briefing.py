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
    💡  ANALYST INSIGHT
    ═══════════════════════════════════════
    One sharp, forward-looking observation: a trend, risk, or opportunity worth watching.

    Keep the full briefing under 650 words. No filler text. Be direct and specific.
""")


def generate_briefing(coins: list[dict], news: list[dict], spotlight: list[dict] | None = None) -> str:
    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env

    crypto_txt   = format_crypto_table(coins)
    news_txt     = format_news_list(news) if news else "No news data available."
    spotlight_txt = format_crypto_table(spotlight) if spotlight else "Data unavailable."
    date_str     = datetime.now().strftime("%A, %B %d, %Y  %H:%M")

    user_message = dedent(f"""\
        Date: {date_str}

        ─── TOP 10 CRYPTOCURRENCIES BY MARKET CAP ───
        {crypto_txt}

        ─── SPOTLIGHT COINS: SOLANA / CHAINLINK / SUI ───
        {spotlight_txt}

        ─── TOP 10 CRYPTO NEWS STORIES ───
        {news_txt}

        Generate today's daily crypto briefing.
    """)

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
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

    print("\n🤖  Generating AI briefing via Claude Sonnet…")
    briefing = generate_briefing(coins, news, spotlight)

    print("\n" + "═" * 60)
    print(briefing)
    print("═" * 60 + "\n")


if __name__ == "__main__":
    main()
