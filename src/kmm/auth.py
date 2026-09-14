"""Request signing for the Kalshi API.

Kalshi does not use a bearer token. You register a public key with them and keep
the private key locally, then sign every request. Three headers go on each call:

    KALSHI-ACCESS-KEY        your Key ID (a UUID they give you)
    KALSHI-ACCESS-TIMESTAMP  current time in MILLISECONDS since the epoch
    KALSHI-ACCESS-SIGNATURE  base64 RSA-PSS/SHA-256 signature

The string that gets signed is the plain concatenation, with no separators:

    str(timestamp_ms) + METHOD + path

Two details cause almost every "signature invalid" bug:

  1. The path must include the /trade-api/v2 prefix and must NOT include the
     query string. Sign "/trade-api/v2/markets", not "/trade-api/v2/markets?limit=5".
  2. The timestamp is milliseconds, not seconds.

Why a signature scheme rather than a static token: a leaked bearer token is
reusable forever by whoever finds it. A signature is bound to one method, one
path and one moment in time, and the private key never crosses the wire. The
cost is that clock skew on your machine turns into an auth failure, which is why
we read the system clock fresh on every request and cache nothing.
"""

from __future__ import annotations

import base64
import time
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa


@dataclass
class KalshiSigner:
    """Holds the private key and produces the three auth headers."""

    key_id: str
    private_key: rsa.RSAPrivateKey

    @classmethod
    def from_file(
        cls,
        key_id: str,
        pem_path: str | Path,
        password: bytes | None = None,
    ) -> "KalshiSigner":
        pem_path = Path(pem_path)
        if not pem_path.exists():
            raise FileNotFoundError(
                f"Kalshi private key not found at {pem_path}. Kalshi shows the private "
                "key exactly once when you create the API key; if it is lost you have "
                "to revoke it and make a new one."
            )
        key = serialization.load_pem_private_key(pem_path.read_bytes(), password=password)
        if not isinstance(key, rsa.RSAPrivateKey):
            raise TypeError(f"{pem_path} is not an RSA private key (Kalshi requires RSA).")
        return cls(key_id=key_id, private_key=key)

    def _sign(self, message: str) -> str:
        """RSA-PSS with SHA-256 and salt length equal to the digest length.

        PSS is randomised, so signing the same string twice gives different
        bytes. That is expected: the server verifies the signature, it does not
        compare it against a stored value.
        """
        signature = self.private_key.sign(
            message.encode("utf-8"),
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=hashes.SHA256.digest_size,
            ),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("ascii")

    def headers(self, method: str, path: str) -> dict[str, str]:
        """Build auth headers for one request.

        `path` is the full server path including /trade-api/v2 and excluding any
        query string. `method` is upper case, e.g. "GET".
        """
        if "?" in path:
            raise ValueError(
                f"Sign the path without query parameters. Got {path!r}. "
                "Strip everything from the '?' onward before signing."
            )
        timestamp_ms = str(int(time.time() * 1000))
        message = timestamp_ms + method.upper() + path
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
            "KALSHI-ACCESS-SIGNATURE": self._sign(message),
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
