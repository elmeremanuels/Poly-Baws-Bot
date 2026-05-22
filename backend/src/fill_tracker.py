"""Fill rate tracking for limit order exit performance analysis."""
from .logger import _db


async def record_fill_attempt(
    trade_id: str,
    token_id: str,
    side: str,
    limit_price: float,
    filled: bool,
    fill_price: float | None = None,
) -> None:
    async with _db() as db:
        await db.execute(
            """INSERT INTO fill_history
               (trade_id, token_id, side, limit_price, filled, fill_price)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (trade_id, token_id, side, limit_price, int(filled), fill_price),
        )
        await db.commit()
