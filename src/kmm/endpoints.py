"""Kalshi base URLs, taken from docs.kalshi.com/getting_started/api_environments.

Kalshi runs two completely separate exchanges:

  prod  -- the real one, real money, real order books
  demo  -- a sandbox with fake money. Same API surface, but the books are thin
           and the prices are not economically meaningful, so it is useful for
           testing plumbing and useless for measuring mispricings.

Both a newer hostname and an older one are accepted by Kalshi. We use the newer
one and keep the legacy value here so that a reader who finds old example code
online can see why the URLs differ.
"""

ENVIRONMENTS = {
    "prod": {
        "rest": "https://external-api.kalshi.com/trade-api/v2",
        "ws": "wss://external-api-ws.kalshi.com/trade-api/ws/v2",
        "rest_legacy": "https://api.elections.kalshi.com/trade-api/v2",
        "ws_legacy": "wss://api.elections.kalshi.com/trade-api/ws/v2",
    },
    "demo": {
        "rest": "https://external-api.demo.kalshi.co/trade-api/v2",
        "ws": "wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2",
        "rest_legacy": "https://demo-api.kalshi.co/trade-api/v2",
        "ws_legacy": "wss://demo-api.kalshi.co/trade-api/ws/v2",
    },
}

# The path prefix that has to be included in the signed string. The signature
# covers the full path as the server sees it, not just the bit after /v2.
REST_PATH_PREFIX = "/trade-api/v2"
WS_PATH = "/trade-api/ws/v2"


def rest_base(env: str) -> str:
    return ENVIRONMENTS[env]["rest"]


def ws_url(env: str) -> str:
    return ENVIRONMENTS[env]["ws"]
