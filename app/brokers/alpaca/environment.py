from enum import StrEnum


class AlpacaEnvironment(StrEnum):
    DEMO = "alpaca_demo"
    LIVE = "alpaca_live"

    @property
    def api_url(self) -> str:
        host = "paper-api" if self == self.DEMO else "api"
        return f"https://{host}.alpaca.markets"

    @property
    def stream_url(self) -> str:
        return self.api_url.replace("https://", "wss://") + "/stream"

    @property
    def account_namespace(self) -> str:
        # Retain compatibility with the first paper-adapter checkpoint.
        return "paper" if self == self.DEMO else "live"
