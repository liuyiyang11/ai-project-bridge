"""Safe error types for the market-data public boundary."""

from ..public_errors import INVALID_REQUEST, MARKET_DATA_SOURCE_FAILED, MARKET_DATA_TIMEOUT


class MarketDataRequestError(ValueError):
    public_error_code = INVALID_REQUEST
    public_message = "Invalid A-share symbol."

    def __init__(self) -> None:
        super().__init__(self.public_message)


class MarketDataTimeoutError(TimeoutError):
    public_error_code = MARKET_DATA_TIMEOUT
    public_message = "Market data request timed out."

    def __init__(self) -> None:
        super().__init__(self.public_message)


class MarketDataSourceError(RuntimeError):
    public_error_code = MARKET_DATA_SOURCE_FAILED
    public_message = "Market data source is temporarily unavailable."

    def __init__(self) -> None:
        super().__init__(self.public_message)
