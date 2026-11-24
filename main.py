"""Pump.fun replay paper bot: early-momentum scalper.

Speaks agent-fun/replay/v2 over stdin/stdout through the vendored shim.
The harness sends a hello first (protocol, seed, capital of 10 SOL, caps,
fees, latency_slots), then one batch per step: each batch carries a seq, a
slot, the pump.fun events seen since the last batch (CreateEvent /
TradeEvent / CompleteEvent), marks, fills for our earlier orders, and the
account (cash + positions). We must answer every batch with exactly one
reply line echoing its seq, carrying our orders for that slot; orders are
executed 2 slots later (latency_slots), so entries must not chase a price
that has already run away.

Strategy, in plain terms
------------------------
Dev-window data (128k events, 1222 coins) shows the typical new coin pumps
hard in its first few trades -- median +34% over the create price after 5
trades -- and then gives most of it back as sellers arrive (median final
price back near the create price). So the edge is a fast flip, not a hold:
buy a coin only while its early momentum is confirmed but not yet
exhausted, and sell quickly on a take-profit, a stop-loss, or a trailing
stop. Nothing is held to completion on purpose; the harness flattens
whatever is left at the end of the window.

Entry (one buy per coin, ever):
  * the coin was created in this window and has traded at least 4 times;
  * its price is 10%..80% above its create price (momentum confirmed,
    not yet a blow-off top) and above its price 3 trades ago;
  * real SOL reserves are at least 1 SOL, so the 1%-of-real-reserve size
    cap lets us buy at least 0.01 SOL (the guardrail's minimum round trip);
  * fewer than 5 positions are open (caps.max_open_positions) and we
    have the free cash.
  Stake is 0.03 SOL, clipped to the size cap and to free cash.

Exit (sell the whole position):
  * take-profit at +30% over our average entry cost;
  * stop-loss at -10%;
  * trailing stop: 12% below the best price seen since entry;
  * stale exit: still held after 60 batches with no take-profit, sell at
    market -- prices revert to the create price, so a stale loser only
    gets worse while it occupies one of the 5 slots.
Sells use 1000 bps of slippage so a fast-moving curve still fills.

Determinism: no randomness, no clock, no environment reads. All iteration
follows batch event order and dict insertion order, so the two graded
executions produce byte-identical action logs. Stdlib only, no network.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))

import makefun_bot  # noqa: E402

SOL = makefun_bot.LAMPORTS_PER_SOL

# --- tunables -------------------------------------------------------------
STAKE_LAMPORTS = 3 * SOL // 100          # 0.03 SOL per coin
MIN_BUY_LAMPORTS = SOL // 100            # 0.01 SOL: guardrail round trip
MIN_REAL_SOL = SOL                       # need real reserves >= 1 SOL
MIN_TRADES = 4                           # momentum needs a few prints
ENTRY_LOW = 1.10                         # price / create price, low end
ENTRY_HIGH = 1.80                        # price / create price, high end
TAKE_PROFIT = 0.30                       # sell at +30% over entry cost
STOP_LOSS = -0.10                        # sell at -10%
TRAIL_DROP = 0.12                        # sell 12% below peak since entry
MAX_HOLD_BATCHES = 60                    # stale exit after this many batches
SELL_SLIPPAGE_BPS = 1000
BUY_SLIPPAGE_BPS = 1000


def _price(curve):
    """Current price in SOL per token from virtual reserves."""
    if curve.virtual_token_reserves <= 0:
        return 0.0
    return curve.virtual_quote_reserves / curve.virtual_token_reserves


class Coin:
    """Per-mint tracking, kept in first-seen order (deterministic)."""

    __slots__ = ("create_price", "trades", "prev_prices", "decided",
                 "peak", "entry_cost", "held_since")

    def __init__(self, create_price):
        self.create_price = create_price
        self.trades = 0
        self.prev_prices = []      # last few trade prices, oldest first
        self.decided = False       # entry already attempted / bought
        self.peak = 0.0            # best price since we bought
        self.entry_cost = 0.0      # SOL per token paid (from fills)
        self.held_since = -1       # batch seq when the position opened


coins = {}


def on_batch(state, batch):
    orders = []
    caps = state.hello["caps"]
    tx = state.tx_fee_lamports()
    free = state.free_cash_lamports()
    slots_free = caps["max_open_positions"] - state.committed_position_count()

    # --- exits first: free slots before deciding new entries --------------
    for mint in sorted(state.positions):
        holding = state.positions[mint]
        curve = state.curves.get(mint)
        coin = coins.get(mint)
        if curve is None or curve.complete or holding.token_amount <= 0:
            continue
        price = _price(curve)
        if price <= 0.0:
            continue
        cost = holding.cost_lamports / holding.token_amount
        if coin is not None:
            if coin.entry_cost <= 0.0:
                coin.entry_cost = cost
            if price > coin.peak:
                coin.peak = price
            if coin.held_since < 0:
                coin.held_since = state.seq
            pnl = price / cost - 1.0
            stale = state.seq - coin.held_since >= MAX_HOLD_BATCHES
            if (pnl >= TAKE_PROFIT or pnl <= STOP_LOSS
                    or price <= coin.peak * (1.0 - TRAIL_DROP) or stale):
                orders.append({"side": "sell", "mint": mint,
                               "token_amount": holding.token_amount,
                               "max_slippage_bps": SELL_SLIPPAGE_BPS})
                free += 0  # sells do not consume cash (tx fee is tiny)
        else:
            # Position we did not track (should not happen): sell on a
            # plain take-profit / stop-loss against its cost.
            pnl = price / cost - 1.0
            if pnl >= TAKE_PROFIT or pnl <= STOP_LOSS:
                orders.append({"side": "sell", "mint": mint,
                               "token_amount": holding.token_amount,
                               "max_slippage_bps": SELL_SLIPPAGE_BPS})

    # --- entries: walk this batch's events in order -----------------------
    for event in batch["events"]:
        mint = event["mint"]
        kind = event["event"]
        curve = state.curves.get(mint)
        if curve is None or curve.complete:
            continue
        coin = coins.get(mint)
        if kind == "CreateEvent":
            if coin is None:
                coins[mint] = Coin(_price(curve))
            continue
        if kind != "TradeEvent":
            continue
        if coin is None:
            coin = Coin(_price(curve))
            coins[mint] = coin
        coin.trades += 1
        price = _price(curve)
        coin.prev_prices.append(price)
        if len(coin.prev_prices) > 4:
            coin.prev_prices.pop(0)
        if coin.decided or mint in state.positions:
            continue
        if coin.trades < MIN_TRADES:
            continue
        if len(orders) >= caps["max_orders_per_batch"] or slots_free <= 0:
            continue
        if coin.create_price <= 0.0:
            continue
        ratio = price / coin.create_price
        if ratio < ENTRY_LOW or ratio > ENTRY_HIGH:
            continue
        if len(coin.prev_prices) >= 4 and price <= coin.prev_prices[0]:
            continue  # momentum must still be rising over recent trades
        if curve.real_quote_reserves < MIN_REAL_SOL:
            continue
        cap = state.max_buy_lamports(mint)
        if cap < MIN_BUY_LAMPORTS:
            continue
        amount = min(STAKE_LAMPORTS, cap)
        if free < amount + tx:
            continue
        orders.append({"side": "buy", "mint": mint,
                       "quote_lamports": amount,
                       "max_slippage_bps": BUY_SLIPPAGE_BPS})
        coin.decided = True
        coin.peak = price
        coin.held_since = state.seq
        free -= amount + tx
        slots_free -= 1

    return orders


if __name__ == "__main__":
    makefun_bot.run(on_batch)
