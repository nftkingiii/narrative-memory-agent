"""
perception.py — Narrative Memory Agent
Handles all Skill Hub API calls: news, sentiment, market intel
Single session per run, all data normalized for narrative detection
"""

import requests
import json
from datetime import datetime, timezone


MCP_URL = "https://datahub.noxiaohao.com/mcp"

MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


# ─────────────────────────────────────────────
# Session Management
# ─────────────────────────────────────────────

def init_session() -> str | None:
    """Initialize MCP session and return session ID."""
    payload = {
        "jsonrpc": "2.0",
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "narrative-memory-agent", "version": "1.0.0"},
        },
        "id": 1,
    }
    try:
        resp = requests.post(MCP_URL, headers=MCP_HEADERS, json=payload, timeout=45)
        session_id = resp.headers.get("mcp-session-id")
        if not session_id:
            print("[perception] ERROR: No session ID in response headers")
            return None
        print(f"[perception] Session initialized: {session_id[:12]}...")
        return session_id
    except Exception as e:
        print(f"[perception] ERROR initializing session: {e}")
        return None


def call_tool(session_id: str, tool_name: str, arguments: dict, call_id: int = 2) -> dict | None:
    """Call an MCP tool and return the parsed result."""
    headers = {**MCP_HEADERS, "mcp-session-id": session_id}
    payload = {
        "jsonrpc": "2.0",
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": arguments},
        "id": call_id,
    }
    try:
        resp = requests.post(MCP_URL, headers=headers, json=payload, timeout=45)
        raw = resp.text

        # Parse SSE format — extract the data: line
        for line in raw.splitlines():
            if line.startswith("data:"):
                data = json.loads(line[5:].strip())
                if "result" in data:
                    content = data["result"].get("content", [])
                    if content and content[0].get("type") == "text":
                        return json.loads(content[0]["text"])
                elif "error" in data:
                    print(f"[perception] Tool error ({tool_name}): {data['error']}")
                    return None
        return None
    except Exception as e:
        print(f"[perception] ERROR calling {tool_name}: {e}")
        return None


# ─────────────────────────────────────────────
# News Briefing
# ─────────────────────────────────────────────

def get_news(session_id: str, keyword: str = None, limit: int = 10) -> list[dict]:
    """
    Fetch latest crypto news. Optionally filter by keyword.
    Returns list of normalized article dicts.
    """
    feeds = "cointelegraph,coindesk,decrypt,blockworks,the_defiant"
    args = {"action": "latest", "feeds": feeds, "limit": limit}
    if keyword:
        args["keyword"] = keyword

    raw = call_tool(session_id, "news_feed", args, call_id=10)
    if not raw:
        return []

    articles = []
    for feed in raw:
        feed_name = feed.get("feed", "unknown")
        for item in feed.get("items", []):
            articles.append({
                "source": feed_name,
                "title": item.get("title", ""),
                "summary": item.get("summary") or item.get("description", ""),
                "url": item.get("url") or item.get("link", ""),
                "published": item.get("published") or item.get("pubDate", ""),
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            })

    print(f"[perception] News fetched: {len(articles)} articles")
    return articles


def get_kol_news(session_id: str, limit: int = 5) -> list[dict]:
    """Fetch KOL and researcher views (Hayes, Vitalik, Cobie, Messari)."""
    args = {"action": "latest", "feeds": "hayes,vitalik,cobie,messari", "limit": limit}
    raw = call_tool(session_id, "news_feed", args, call_id=11)
    if not raw:
        return []

    articles = []
    for feed in raw:
        for item in feed.get("items", []):
            articles.append({
                "source": feed.get("feed", "kol"),
                "title": item.get("title", ""),
                "summary": item.get("summary") or item.get("description", ""),
                "published": item.get("published", ""),
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            })
    return articles


# ─────────────────────────────────────────────
# Sentiment
# ─────────────────────────────────────────────

def get_sentiment(session_id: str) -> dict:
    """
    Fetch current market sentiment snapshot.
    Returns normalized dict with fear_greed, long_short, funding, taker.
    """
    result = {
        "fear_greed_value": None,
        "fear_greed_label": None,
        "btc_long_ratio": None,
        "btc_short_ratio": None,
        "btc_funding_rate": None,
        "btc_taker_ratio": None,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }

    # Fear & Greed Index
    fg = call_tool(session_id, "sentiment_index", {"action": "current"}, call_id=20)
    if fg:
        # Handle list or dict response
        entry = fg[0] if isinstance(fg, list) else fg
        raw_fear_greed = entry.get("value") or entry.get("score")
        try:
            result["fear_greed_value"] = float(raw_fear_greed)
        except (TypeError, ValueError):
            result["fear_greed_value"] = None
        result["fear_greed_label"] = entry.get("value_classification") or entry.get("label")

    # Long/Short Ratio
    ls = call_tool(
        session_id, "derivatives_sentiment",
        {"action": "long_short", "symbol": "BTCUSDT", "period": "4h"},
        call_id=21,
    )
    if ls:
        entry = ls[0] if isinstance(ls, list) else ls
        result["btc_long_ratio"] = entry.get("longAccount") or entry.get("longRatio")
        result["btc_short_ratio"] = entry.get("shortAccount") or entry.get("shortRatio")

    # Funding Rate (via taker ratio call — funding embedded in some responses)
    taker = call_tool(
        session_id, "derivatives_sentiment",
        {"action": "taker_ratio", "symbol": "BTCUSDT", "period": "4h"},
        call_id=22,
    )
    if taker:
        entry = taker[0] if isinstance(taker, list) else taker
        result["btc_taker_ratio"] = entry.get("buyRatio") or entry.get("takerRatio")
        result["btc_funding_rate"] = entry.get("fundingRate")

    print(f"[perception] Sentiment: F&G={result['fear_greed_value']}, "
          f"L/S={result['btc_long_ratio']}/{result['btc_short_ratio']}")
    return result


# ─────────────────────────────────────────────
# Market Intel
# ─────────────────────────────────────────────

def get_market_intel(session_id: str) -> dict:
    """
    Fetch global market structure data.
    Returns BTC dominance, total market cap, trending DEX tokens.
    """
    result = {
        "total_market_cap_usd": None,
        "btc_dominance": None,
        "eth_dominance": None,
        "market_cap_change_24h": None,
        "dex_trending": [],
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }

    # Global market data
    global_data = call_tool(session_id, "crypto_market", {"action": "global"}, call_id=30)
    if global_data:
        d = global_data.get("data", global_data)
        result["total_market_cap_usd"] = d.get("total_market_cap", {}).get("usd")
        result["btc_dominance"] = d.get("market_cap_percentage", {}).get("btc")
        result["eth_dominance"] = d.get("market_cap_percentage", {}).get("eth")
        result["market_cap_change_24h"] = d.get("market_cap_change_percentage_24h_usd")

    # DEX trending tokens (narrative signal — what retail is trading)
    dex = call_tool(session_id, "dex_market", {"action": "trending", "limit": 10}, call_id=31)
    if dex:
        tokens = dex if isinstance(dex, list) else dex.get("data", [])
        result["dex_trending"] = [
            {
                "name": t.get("name") or t.get("attributes", {}).get("name", ""),
                "symbol": t.get("symbol") or t.get("attributes", {}).get("symbol", ""),
                "price_change_24h": t.get("price_change_24h") or
                                    t.get("attributes", {}).get("price_change_percentage", {}).get("h24"),
            }
            for t in tokens[:10]
        ]

    print(f"[perception] Market intel: BTC dom={result['btc_dominance']}, "
          f"DEX trending={len(result['dex_trending'])} tokens")
    return result


# ─────────────────────────────────────────────
# Full Perception Snapshot
# ─────────────────────────────────────────────

def get_full_snapshot() -> dict:
    """
    Run a complete perception cycle.
    Returns all data needed for narrative detection in one dict.
    """
    print(f"\n[perception] Starting snapshot at {datetime.now(timezone.utc).isoformat()}")

    session_id = init_session()
    if not session_id:
        return {"error": "Failed to initialize MCP session"}

    snapshot = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "news": get_news(session_id),
        "kol_news": get_kol_news(session_id),
        "sentiment": get_sentiment(session_id),
        "market_intel": get_market_intel(session_id),
    }

    print(f"[perception] Snapshot complete — "
          f"{len(snapshot['news'])} news, "
          f"{len(snapshot['kol_news'])} KOL articles")
    return snapshot


# ─────────────────────────────────────────────
# Test
# ─────────────────────────────────────────────

if __name__ == "__main__":
    snapshot = get_full_snapshot()
    print("\n=== SNAPSHOT SUMMARY ===")
    print(f"News articles:     {len(snapshot.get('news', []))}")
    print(f"KOL articles:      {len(snapshot.get('kol_news', []))}")
    print(f"Sentiment:         {json.dumps(snapshot.get('sentiment', {}), indent=2)}")
    print(f"Market Intel:      {json.dumps(snapshot.get('market_intel', {}), indent=2)}")
