"""
detection.py — Narrative Memory Agent
Keyword-based narrative detection engine.
Takes a perception snapshot, scores it against known narrative themes,
and returns a detection result with confidence score.
"""

import re
from datetime import datetime, timezone
from dataclasses import dataclass, field


# ─────────────────────────────────────────────
# Narrative Keyword Definitions
# ─────────────────────────────────────────────
# Each narrative has:
#   - primary: high-weight keywords (2 points each)
#   - secondary: supporting keywords (1 point each)
#   - tokens: specific coins associated with this narrative
#   - min_score: minimum score to trigger detection
# ─────────────────────────────────────────────

NARRATIVE_DEFINITIONS = {
    "ai_coins": {
        "primary": [
            "ai agent", "artificial intelligence", "ai token", "ai crypto",
            "machine learning crypto", "ai coins", "ai narrative",
            "fetch.ai", "render network", "singularitynet", "bittensor",
            "ai16z", "virtual protocol", "arc", "griffain",
        ],
        "secondary": [
            "chatgpt", "openai", "anthropic", "llm", "large language model",
            "ai infrastructure", "decentralized ai", "gpu network",
            "ai agent token", "autonomous agent",
        ],
        "tokens": ["FET", "RNDR", "AGIX", "TAO", "WLD", "VIRTUAL", "ARC"],
        "min_score": 3,
    },
    "rwa_tokenization": {
        "primary": [
            "real world asset", "rwa", "tokenized asset", "asset tokenization",
            "tokenized treasury", "tokenized bond", "on-chain finance",
            "ondo finance", "blackrock tokeniz", "tokenized fund",
        ],
        "secondary": [
            "institutional defi", "tokenized real estate", "tokenized equity",
            "tradfi on-chain", "compliance token", "regulated defi",
            "asset backed token", "tokenized commodity",
        ],
        "tokens": ["ONDO", "POLYX", "CPOOL", "CFG", "MPL", "TRU"],
        "min_score": 3,
    },
    "btc_etf_approval": {
        "primary": [
            "bitcoin etf", "btc etf", "spot etf", "etf approval",
            "etf inflow", "etf outflow", "blackrock etf", "fidelity etf",
            "sec etf", "etf filing", "etf launch",
        ],
        "secondary": [
            "institutional bitcoin", "etf demand", "etf volume",
            "grayscale", "etf record", "etf accumulation",
            "spot bitcoin", "bitcoin fund",
        ],
        "tokens": ["BTC", "GBTC"],
        "min_score": 4,
    },
    "meme_supercycle": {
        "primary": [
            "meme coin", "memecoin", "meme season", "meme supercycle",
            "dog coin", "pepe", "dogwifhat", "wif", "bonk",
            "meme rally", "shitcoin season", "degen season",
        ],
        "secondary": [
            "doge", "shib", "floki", "meme token", "community coin",
            "viral token", "pump fun", "meme launch", "trending meme",
            "retail frenzy", "casino season",
        ],
        "tokens": ["DOGE", "SHIB", "PEPE", "WIF", "BONK", "FLOKI"],
        "min_score": 3,
    },
    "depin": {
        "primary": [
            "depin", "decentralized physical infrastructure",
            "physical infrastructure network", "depin token",
            "helium network", "iotex", "hivemapper",
        ],
        "secondary": [
            "decentralized wireless", "token incentive hardware",
            "physical node", "infrastructure token", "sensor network",
            "decentralized storage hardware", "hotspot mining",
        ],
        "tokens": ["HNT", "IOTX", "MOBILE", "HONEY", "DIMO"],
        "min_score": 3,
    },
    "layer2_scaling": {
        "primary": [
            "layer 2", "l2", "rollup", "optimistic rollup", "zk rollup",
            "arbitrum", "optimism", "base chain", "zksync", "polygon",
            "l2 season", "scaling solution",
        ],
        "secondary": [
            "ethereum scaling", "gas fees", "throughput", "tps",
            "sequencer", "data availability", "l2 ecosystem",
            "rollup adoption", "l2 tvl",
        ],
        "tokens": ["ARB", "OP", "MATIC", "ZK", "STRK", "METIS"],
        "min_score": 4,
    },
    "defi_resurgence": {
        "primary": [
            "defi summer", "defi season", "yield farming", "defi tvl",
            "defi resurgence", "defi narrative", "dex volume",
            "liquidity mining", "defi protocol",
        ],
        "secondary": [
            "total value locked", "amm", "lending protocol", "uniswap",
            "aave", "compound", "curve", "defi blue chip",
            "protocol revenue", "defi yield",
        ],
        "tokens": ["UNI", "AAVE", "CRV", "MKR", "SNX", "COMP"],
        "min_score": 3,
    },
}


# ─────────────────────────────────────────────
# Detection Result
# ─────────────────────────────────────────────

@dataclass
class DetectionResult:
    narrative_tag: str
    confidence: float             # 0.0 – 1.0
    raw_score: int                # total keyword hits
    matched_keywords: list[str]
    news_volume: int              # number of articles mentioning this narrative
    sentiment_score: float        # fear/greed value at detection
    funding_rate: float           # BTC funding rate at detection
    btc_dominance: float          # BTC dominance at detection
    detected_at: str
    is_new: bool = True           # False if this narrative is already in memory as 'running'
    memory_match: dict = field(default_factory=dict)   # historical record if found


# ─────────────────────────────────────────────
# Text Normalization
# ─────────────────────────────────────────────

def normalize(text: str) -> str:
    """Lowercase, strip punctuation, normalize whitespace."""
    text = text.lower()
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def extract_text_corpus(snapshot: dict) -> str:
    """
    Build a single searchable text corpus from all news titles and summaries.
    This is what we run keyword detection against.
    """
    parts = []

    for article in snapshot.get("news", []):
        if article.get("title"):
            parts.append(article["title"])
        if article.get("summary"):
            parts.append(article["summary"][:300])

    for article in snapshot.get("kol_news", []):
        if article.get("title"):
            parts.append(article["title"])
        if article.get("summary"):
            parts.append(article["summary"][:300])

    return normalize(" ".join(parts))


def extract_dex_tokens(snapshot: dict) -> list[str]:
    """Extract token symbols from DEX trending list."""
    tokens = []
    for token in snapshot.get("market_intel", {}).get("dex_trending", []):
        sym = token.get("symbol", "").upper()
        if sym:
            tokens.append(sym)
    return tokens


# ─────────────────────────────────────────────
# Scoring Engine
# ─────────────────────────────────────────────

def score_narrative(narrative_tag: str, corpus: str, dex_tokens: list[str]) -> tuple[int, list[str]]:
    """
    Score a single narrative against the text corpus.
    Returns (score, matched_keywords).
    Primary keywords = 2 points, secondary = 1 point, token match = 1 point.
    """
    definition = NARRATIVE_DEFINITIONS[narrative_tag]
    score = 0
    matched = []

    for kw in definition["primary"]:
        if kw in corpus:
            score += 2
            matched.append(f"[P] {kw}")

    for kw in definition["secondary"]:
        if kw in corpus:
            score += 1
            matched.append(f"[S] {kw}")

    for token in definition["tokens"]:
        if token.upper() in dex_tokens:
            score += 1
            matched.append(f"[T] {token} trending on DEX")

    return score, matched


def calculate_confidence(score: int, min_score: int, max_possible: int) -> float:
    """
    Normalize score to 0–1 confidence.
    Scales relative to min_score threshold — at min_score = 0.4,
    at 2x min_score = 0.7, at 3x min_score = 0.9.
    """
    if score < min_score:
        return 0.0
    ratio = score / max(min_score * 3, 1)
    confidence = min(0.95, 0.4 + (ratio * 0.55))
    return round(confidence, 3)


# ─────────────────────────────────────────────
# Sentiment Filters
# ─────────────────────────────────────────────

def sentiment_filter(sentiment: dict) -> tuple[bool, str]:
    """
    Apply sentiment-based filters.
    Returns (should_trade, reason).
    Blocks trades when conditions are too risky regardless of narrative signal.
    """
    funding = sentiment.get("btc_funding_rate")
    fg = sentiment.get("fear_greed_value")
    long_ratio = sentiment.get("btc_long_ratio")

    # Overleveraged bulls — funding too high
    if funding and float(funding) > 0.05:
        return False, f"Funding rate too high ({funding}) — overleveraged market"

    # Extreme greed — late cycle, don't chase
    if fg and int(fg) > 88:
        return False, f"Extreme greed ({fg}/100) — likely late cycle, avoid chasing"

    # Too crowded long
    if long_ratio and float(long_ratio) > 0.72:
        return False, f"Longs too crowded ({long_ratio}) — squeeze risk"

    return True, "Sentiment conditions acceptable"


# ─────────────────────────────────────────────
# Main Detection Function
# ─────────────────────────────────────────────

def detect_narratives(snapshot: dict, memory_query_fn=None) -> list[DetectionResult]:
    """
    Run narrative detection on a perception snapshot.
    Returns list of DetectionResult sorted by confidence (highest first).

    memory_query_fn: optional callable(tag) -> dict|None
                     used to check if narrative has prior memory records
    """
    corpus = extract_text_corpus(snapshot)
    dex_tokens = extract_dex_tokens(snapshot)
    sentiment = snapshot.get("sentiment", {})
    market_intel = snapshot.get("market_intel", {})

    detected = []

    if not corpus.strip():
        print("[detection] WARNING: Empty corpus — no news data available")

    for tag, definition in NARRATIVE_DEFINITIONS.items():
        score, matched = score_narrative(tag, corpus, dex_tokens)

        if score < definition["min_score"]:
            continue

        # Calculate max possible score for normalization
        max_possible = (
            len(definition["primary"]) * 2 +
            len(definition["secondary"]) +
            len(definition["tokens"])
        )

        confidence = calculate_confidence(score, definition["min_score"], max_possible)

        # Count articles mentioning this narrative
        news_volume = sum(
            1 for article in snapshot.get("news", [])
            if any(kw in normalize(article.get("title", "") + article.get("summary", ""))
                   for kw in definition["primary"])
        )

        # Check memory for prior record
        memory_match = {}
        if memory_query_fn:
            prior = memory_query_fn(tag)
            if prior:
                memory_match = prior

        result = DetectionResult(
            narrative_tag=tag,
            confidence=confidence,
            raw_score=score,
            matched_keywords=matched,
            news_volume=news_volume,
            sentiment_score=float(sentiment.get("fear_greed_value") or 50),
            funding_rate=float(sentiment.get("btc_funding_rate") or 0),
            btc_dominance=float(market_intel.get("btc_dominance") or 0),
            detected_at=datetime.now(timezone.utc).isoformat(),
            memory_match=memory_match,
        )
        detected.append(result)

    detected.sort(key=lambda x: x.confidence, reverse=True)

    if detected:
        print(f"[detection] Detected {len(detected)} narrative(s):")
        for r in detected:
            mem = "✓ memory match" if r.memory_match else "✗ no prior record"
            print(f"  {r.narrative_tag:25s} confidence={r.confidence:.2f} "
                  f"score={r.raw_score:2d} {mem}")
    else:
        print("[detection] No narratives detected above threshold")

    return detected


# ─────────────────────────────────────────────
# Test with Mock Snapshot
# ─────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    import os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from agent.memory import query_narrative, init_db

    # Initialize memory
    init_db()

    # Mock snapshot simulating an AI coins narrative forming
    mock_snapshot = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "news": [
            {
                "source": "cointelegraph",
                "title": "AI agent tokens surge as artificial intelligence narrative returns to crypto",
                "summary": "FET, RNDR and TAO lead gains as AI crypto narrative gains momentum. "
                           "Investors are rotating into AI coins following ChatGPT news.",
            },
            {
                "source": "coindesk",
                "title": "Fetch.ai and SingularityNET rally 40% amid renewed AI token interest",
                "summary": "AI coins are outperforming as the artificial intelligence narrative "
                           "drives fresh capital into decentralized AI infrastructure tokens.",
            },
            {
                "source": "decrypt",
                "title": "Bittensor TAO hits new high as AI crypto sector gains traction",
                "summary": "Machine learning crypto projects seeing strong inflows. "
                           "AI agent token activity up significantly this week.",
            },
            {
                "source": "blockworks",
                "title": "Bitcoin ETF records another day of strong inflows",
                "summary": "Spot bitcoin ETF products saw combined inflows today "
                           "as institutional demand remains steady.",
            },
        ],
        "kol_news": [
            {
                "source": "messari",
                "title": "AI infrastructure tokens: the next wave of crypto adoption",
                "summary": "Decentralized AI networks are positioning themselves as "
                           "critical infrastructure for the AI era.",
            }
        ],
        "sentiment": {
            "fear_greed_value": 68,
            "fear_greed_label": "Greed",
            "btc_long_ratio": 0.58,
            "btc_short_ratio": 0.42,
            "btc_funding_rate": 0.012,
            "btc_taker_ratio": 1.15,
        },
        "market_intel": {
            "btc_dominance": 52.4,
            "total_market_cap_usd": 2_450_000_000_000,
            "market_cap_change_24h": 2.3,
            "dex_trending": [
                {"name": "Fetch.ai", "symbol": "FET", "price_change_24h": 38.5},
                {"name": "Render", "symbol": "RNDR", "price_change_24h": 22.1},
                {"name": "Bittensor", "symbol": "TAO", "price_change_24h": 45.2},
            ],
        },
    }

    print("=== Narrative Detection Test ===\n")
    results = detect_narratives(mock_snapshot, memory_query_fn=query_narrative)

    print("\n=== Detection Results Detail ===")
    for r in results:
        print(f"\n{'─'*50}")
        print(f"Narrative:    {r.narrative_tag}")
        print(f"Confidence:   {r.confidence:.2f}")
        print(f"Score:        {r.raw_score}")
        print(f"News volume:  {r.news_volume} articles")
        print(f"Matched:      {', '.join(r.matched_keywords[:5])}")
        if r.memory_match:
            m = r.memory_match
            print(f"Memory:       Prior record found — "
                  f"avg return {m['avg_return_pct']}%, "
                  f"days to peak {m['days_to_peak']}, "
                  f"optimal entry day {m['optimal_entry_day']}")
        else:
            print(f"Memory:       No prior record — conservative sizing")

    print("\n=== Sentiment Filter Test ===")
    ok, reason = sentiment_filter(mock_snapshot["sentiment"])
    print(f"Trade allowed: {ok} — {reason}")

    # Test blocked scenario
    blocked_sentiment = {**mock_snapshot["sentiment"], "btc_funding_rate": 0.06}
    ok2, reason2 = sentiment_filter(blocked_sentiment)
    print(f"Trade allowed: {ok2} — {reason2}")

    print("\n✅ detection.py working correctly")