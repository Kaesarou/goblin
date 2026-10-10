import json
import os
import tempfile
import threading
from pathlib import Path
from urllib.parse import quote
from uuid import UUID

import requests


class AlpacaInstrumentCache:
    """Symbol -> asset UUID cache, never a cache of trading permissions."""

    def __init__(self, http, path: str) -> None:
        if not path.strip():
            raise ValueError("ALPACA_INSTRUMENT_ID_CACHE_PATH must name a cache file")
        self.http = http
        self.path = Path(path)
        self._lock = threading.Lock()
        self._ids: dict[str, str] = {}
        if self.path.exists():
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("Invalid Alpaca instrument cache")
            for symbol, asset_id in payload.items():
                if not isinstance(symbol, str) or not symbol or not isinstance(asset_id, str):
                    raise ValueError("Invalid Alpaca instrument cache entry")
                self._ids[symbol] = str(UUID(asset_id))

    def get_asset(self, symbol: str) -> dict:
        with self._lock:
            cached = self._ids.get(symbol)
            try:
                asset = self.http.request("GET", "/v2/assets/" + quote(cached or symbol, safe=""))
            except requests.HTTPError as exc:
                if not cached or exc.response is None or exc.response.status_code != 404:
                    raise
                asset = self.http.request("GET", "/v2/assets/" + quote(symbol, safe=""))
            if not isinstance(asset, dict) or asset.get("symbol") != symbol:
                raise ValueError("Alpaca asset does not match the requested symbol")
            asset_id = asset.get("id")
            if not isinstance(asset_id, str):
                raise ValueError("Alpaca asset ID is missing")
            asset_id = str(UUID(asset_id))
            if cached != asset_id:
                updated = {**self._ids, symbol: asset_id}
                self.path.parent.mkdir(parents=True, exist_ok=True)
                temporary = None
                try:
                    with tempfile.NamedTemporaryFile(
                        mode="w",
                        encoding="utf-8",
                        dir=self.path.parent,
                        prefix=self.path.name + ".",
                        suffix=".tmp",
                        delete=False,
                    ) as handle:
                        temporary = Path(handle.name)
                        json.dump(updated, handle, sort_keys=True)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(temporary, self.path)
                    self._ids = updated
                finally:
                    if temporary is not None:
                        temporary.unlink(missing_ok=True)
            return asset
