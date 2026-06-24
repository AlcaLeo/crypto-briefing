#!/usr/bin/env python3
"""
Web dashboard for the Daily Crypto Financial Briefing.
Run: python3 app.py
Then open: http://localhost:5000
"""

import os
import sys
import json
import threading
from pathlib import Path
from datetime import datetime
from flask import Flask, render_template, jsonify

# Load .env
_env_file = Path(__file__).parent / ".env"
if _env_file.exists():
    for line in _env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))

from briefing import (
    fetch_top_cryptos, fetch_crypto_news, fetch_spotlight_coins,
    fetch_portfolio_stock, fetch_portfolio_news,
    fetch_tech_news, fetch_btc_sentiment, fetch_btc_hodl_waves,
    generate_briefing, generate_summary, format_crypto_table,
)

app = Flask(__name__)

# Cache so refreshing the page doesn't re-run the briefing every time
_cache: dict = {}
_lock = threading.Lock()


def _run_briefing() -> dict:
    coins           = fetch_top_cryptos()
    news            = fetch_crypto_news()
    spotlight       = fetch_spotlight_coins()
    portfolio_stock = fetch_portfolio_stock()
    portfolio_news  = fetch_portfolio_news()
    tech_news       = fetch_tech_news()
    btc_sentiment   = fetch_btc_sentiment()
    hodl_waves      = fetch_btc_hodl_waves()
    briefing_text   = generate_briefing(coins, news, spotlight, tech_news, btc_sentiment)

    sections = parse_sections(briefing_text)

    def to_row(i, c):
        return {
            "rank": i,
            "name": c.get("name"),
            "symbol": c.get("symbol", "").upper(),
            "price": c.get("current_price", 0) or 0,
            "chg_24h": c.get("price_change_percentage_24h", 0) or 0,
            "chg_7d": c.get("price_change_percentage_7d_in_currency", 0) or 0,
            "market_cap": c.get("market_cap", 0) or 0,
        }

    return {
        "generated_at":   datetime.now().strftime("%A, %B %d, %Y  •  %H:%M"),
        "coins":          [to_row(i, c) for i, c in enumerate(coins, 1)],
        "spotlight":      [to_row(i, c) for i, c in enumerate(spotlight, 1)],
        "news":           news,
        "tech_news":      tech_news,
        "btc_sentiment":  btc_sentiment,
        "hodl_waves":     hodl_waves,
        "portfolio_stock": portfolio_stock,
        "portfolio_news":  portfolio_news,
        "sections":       sections,
        "raw":            briefing_text,
    }


def parse_sections(text: str) -> dict:
    import re
    labels = {
        "market_overview": r"MARKET OVERVIEW",
        "top_movers":      r"TOP MOVERS",
        "news_highlights": r"NEWS HIGHLIGHTS",
        "spotlight":       r"SPOTLIGHT:",
        "tech_pulse":      r"TECH PULSE",
        "btc_sentiment":   r"BTC HODL vs SELL",
        "analyst_insight": r"ANALYST INSIGHT",
    }
    # Splitting on ═══ puts headers and body in alternating parts:
    # [empty, header1, body1, header2, body2, ...]
    parts = re.split(r"[═=]{3,}", text)
    sections: dict = {}
    for i, part in enumerate(parts):
        stripped = part.strip()
        for key, pattern in labels.items():
            if re.search(pattern, stripped, re.IGNORECASE):
                # Body is the very next part
                body = parts[i + 1].strip() if i + 1 < len(parts) else ""
                # Strip the header line from body if it bled in
                body = re.sub(r".*" + pattern + r".*\n?", "", body, flags=re.IGNORECASE).strip()
                sections[key] = body
    return sections


@app.route("/")
def index():
    return render_template("dashboard.html")


@app.route("/api/briefing")
def api_briefing():
    with _lock:
        if not _cache:
            try:
                data = _run_briefing()
                _cache.update(data)
            except Exception as e:
                return jsonify({"error": str(e)}), 500
    return jsonify(_cache)


@app.route("/api/summary")
def api_summary():
    with _lock:
        raw = _cache.get("raw")
    if not raw:
        return jsonify({"error": "No briefing loaded yet. Refresh the dashboard first."}), 400
    try:
        summary = generate_summary(raw)
        return jsonify({"summary": summary})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    with _lock:
        _cache.clear()
        try:
            data = _run_briefing()
            _cache.update(data)
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    return jsonify(_cache)


if __name__ == "__main__":
    if not os.getenv("ANTHROPIC_API_KEY"):
        print("❌  ANTHROPIC_API_KEY is not set. Add it to your .env file.", file=sys.stderr)
        sys.exit(1)
    print("🌐  Dashboard running at http://localhost:8080")
    app.run(debug=False, port=8080)
