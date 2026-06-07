"""Debug script to see raw API responses"""
import requests
import json

MCP_URL = "https://datahub.noxiaohao.com/mcp"
MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}

def init_session():
    payload = {
        "jsonrpc": "2.0",
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "debug", "version": "1.0"},
        },
        "id": 1,
    }
    resp = requests.post(MCP_URL, headers=MCP_HEADERS, json=payload, timeout=45)
    return resp.headers.get("mcp-session-id")

def raw_call(session_id, tool_name, arguments, call_id=2):
    headers = {**MCP_HEADERS, "mcp-session-id": session_id}
    payload = {
        "jsonrpc": "2.0",
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": arguments},
        "id": call_id,
    }
    resp = requests.post(MCP_URL, headers=headers, json=payload, timeout=45)
    print(f"\n=== RAW RESPONSE: {tool_name} ===")
    print(resp.text[:2000])

session_id = init_session()
print(f"Session: {session_id}")

# Test sentiment_index
raw_call(session_id, "sentiment_index", {"action": "current"}, call_id=20)

# Test derivatives_sentiment
raw_call(session_id, "derivatives_sentiment", {"action": "long_short", "symbol": "BTCUSDT", "period": "4h"}, call_id=21)

# Test crypto_market global
raw_call(session_id, "crypto_market", {"action": "global"}, call_id=30)