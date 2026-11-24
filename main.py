"""Pump.fun replay paper bot: launch sniper with fast exits.

What the dev window (1222 coins, 24h) taught, measured on the real harness
(grade-local) and on a faithful offline replay of the same engine:
- Buying a coin after it has already pumped +25% loses: the median realized
  multiple was 0.77x, and no entry-time feature (buy share, trade count,
  price level, creator behaviour, buyer counts) separated winners from losers
  among those coins. The pump is usually the creator's own buy followed by
  their dump, and filters built on those features removed winners as often
  as losers, so this bot runs without them: the launch entry is unfiltered
  except for the size cap.
- What loses least is being there at the launch itself and getting out fast:
  buy in the coin's first 3 slots/trades, once the curve holds >= 1 SOL of
  real reserves (so a >= 0.01 SOL buy fits the 1%-of-reserves size cap and
  counts toward the 20-coin guardrail); take profit at +15%, cut losers at
  -12%, never hold past 10 slots, and leave at once if the creator sells
  while held. Longer holds and wider targets all scored worse on the dev
  window; holding to the end was worst of all. The kit's own sniper pilot
  shows the same shape (fast exits, tiny drawdown).
- One buy per coin, at most 5 open positions, 0.02 SOL per coin, so the
  10 SOL of paper capital turns over across hundreds of launches and the
  result rides on the few runners instead of any single coin.
- Fully deterministic: no clock, no environment reads, no randomness, no set
  iteration. Iteration is always batch order or dict insertion order.

The entrypoint speaks agent-fun/replay/v2 on stdin/stdout through the kit
shim in vendor/makefun_bot.py: one hello, one batch per slot, one reply per
batch, done at the end of the window. Run with no arguments.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))

import makefun_bot  # noqa: E402  (after the sys.path line)

LAMPORTS_PER_SOL = makefun_bot.LAMPORTS_PER_SOL

# --- tuning knobs (launch sniper; see the docstring for why) ---
# Measured on the dev window with the kit's grader: this set lost least
# (-0.20 SOL over 316 coins; every slower/wider/filtered variant lost more).
STAKE_LAMPORTS = 20_000_000          # 0.02 SOL per coin
MIN_STAKE_LAMPORTS = 10_000_000      # 0.01 SOL: the guardrail round-trip floor
ENTRY_MAX_AGE_SLOTS = 3              # only the launch moment counts
ENTRY_MAX_TRADES = 3                 # ... and only while the tape is young
ENTRY_MAX_RUNUP = 100.0              # off: a run-up cap removed winners on dev
ENTRY_BUY_SHARE = 0.0                # off: a buy-share filter removed winners
ENTRY_MAX_CREATOR_BUY = 100_000_000_000  # off: a creator-buy cap removed winners
ENTRY_MIN_REAL_SOL = 1_000_000_000   # 1 SOL real reserves -> cap >= 0.01 SOL
ENTRY_MIN_BUYERS = 0                 # off: a buyer-count filter removed winners
ENTRY_MAX_BUYERS = 100               # off: see above
TP1_MULT = 1.15                      # take profit: sell the whole position here
TP1_PART = 1                         # sell 1/TP1_PART of the tokens at TP1
SL_MULT = 0.88                       # hard stop: -12%
BREAKEVEN_MULT = 1.00                # stop for the remainder after TP1 (unused: TP1 sells all)
TRAIL_ARM_MULT = 1.25                # arm the trailing stop once peak hits this
TRAIL_DROP = 0.80                    # sell if price falls to this share of peak
MAX_HOLD_TRADES = 60                 # exit after this many trades since entry
MAX_HOLD_SLOTS = 10                  # or this many slots since entry (~4 s)
BUY_SLIPPAGE_BPS = 1500              # 15%: launches move in 2 slots of latency
SELL_SLIPPAGE_BPS = 2500             # 25%: exits must go through
MAX_BUY_ATTEMPTS = 2                 # retries after a failed/rejected buy fill


class Coin:
    """Tape state for one mint, from its CreateEvent onward."""

    __slots__ = (
        "mint", "p0", "slot0", "creator", "trades", "buy_sol", "sell_sol",
        "creator_buy_sol", "creator_sell_sol", "buyers", "decided", "attempts",
    )

    def __init__(self, mint, p0, slot0, creator):
        self.mint = mint
        self.p0 = p0
        self.slot0 = slot0
        self.creator = creator
        self.trades = []          # (slot, price) in arrival order, pruned
        self.buy_sol = 0
        self.sell_sol = 0
        self.creator_buy_sol = 0
        self.creator_sell_sol = 0
        self.buyers = {}          # non-creator buyers, insertion ordered
        self.decided = False      # bought, gave up, or aged out: never buy
        self.attempts = 0         # buy orders sent that came back failed/rejected

    def price(self):
        return self.trades[-1][1] if self.trades else self.p0

    def buy_share(self):
        total = self.buy_sol + self.sell_sol
        return self.buy_sol / total if total > 0 else 0.0

    def dev_sold(self):
        return self.creator_sell_sol > 0


class Holding:
    """A live position, tracked from the account the harness reports."""

    __slots__ = (
        "mint", "entry_price", "peak_price", "entry_slot", "entry_trades",
        "took_tp1", "creator_sell_at_entry",
    )

    def __init__(self, mint, entry_price, slot, trades, creator_sell_at_entry):
        self.mint = mint
        self.entry_price = entry_price
        self.peak_price = entry_price
        self.entry_slot = slot
        self.entry_trades = trades
        self.took_tp1 = False
        self.creator_sell_at_entry = creator_sell_at_entry


# Module state, reset only by a fresh process (one run = one process).
coins = {}
held = {}
young = []  # undecided coins still inside the entry age window, in creation order


def _price_of(curve):
    return curve.virtual_quote_reserves / curve.virtual_token_reserves


def _note_trade(coin, slot, price, is_buy, sol_amount, user):
    coin.trades.append((slot, price))
    if len(coin.trades) > 80:
        del coin.trades[: len(coin.trades) - 80]
    if is_buy:
        coin.buy_sol += sol_amount
        if user == coin.creator:
            coin.creator_buy_sol += sol_amount
        elif user is not None:
            coin.buyers[user] = True
    else:
        coin.sell_sol += sol_amount
        if user == coin.creator:
            coin.creator_sell_sol += sol_amount


def on_batch(state, batch):
    orders = []
    slot = batch["slot"]

    # 1. Fills: a buy that came back failed/rejected may be retried while the
    #    launch window still holds; after MAX_BUY_ATTEMPTS give up on the coin.
    for fill in batch["fills"]:
        if fill.get("side") != "buy":
            continue
        if fill.get("status") in ("rejected", "failed"):
            coin = coins.get(fill.get("mint"))
            if coin is not None:
                coin.attempts += 1
                # The send marked the coin decided so no duplicate buy goes
                # out while this one is in flight; a failed one may retry
                # while the window still holds, then the coin is done.
                coin.decided = coin.attempts >= MAX_BUY_ATTEMPTS

    # 2. Fold this batch's events into the per-coin tape, in batch order.
    for event in batch["events"]:
        kind = event.get("event")
        mint = event.get("mint")
        if kind == "CreateEvent":
            if mint not in coins:
                p0 = int(event["virtual_quote_reserves"]) / int(event["virtual_token_reserves"])
                coin = Coin(mint, p0, event["slot"], event.get("creator"))
                coins[mint] = coin
                young.append(mint)
            continue
        coin = coins.get(mint)
        if coin is None:
            # First sight through a trade: seed from the curve the shim
            # already tracks so the coin still gets a creation price.
            curve = state.curves.get(mint)
            if curve is None:
                continue
            coin = Coin(mint, _price_of(curve), event["slot"], None)
            coins[mint] = coin
            young.append(mint)
        if kind == "TradeEvent":
            price = int(event["virtual_quote_reserves"]) / int(event["virtual_token_reserves"])
            _note_trade(
                coin, event["slot"], price,
                bool(event.get("is_buy")), int(event.get("sol_amount", 0)),
                event.get("user"),
            )
        elif kind == "CompleteEvent":
            coin.decided = True

    # 3. Reconcile holdings with the account: new positions record their entry
    #    price from cost/tokens; closed ones drop out.
    for mint, holding in list(state.positions.items()):
        if mint not in held:
            tokens = holding.token_amount
            entry_price = holding.cost_lamports / tokens if tokens > 0 else 0.0
            coin = coins.get(mint)
            trades = len(coin.trades) if coin is not None else 0
            sold = coin.creator_sell_sol if coin is not None else 0
            held[mint] = Holding(mint, entry_price, slot, trades, sold)
    for mint in list(held):
        if mint not in state.positions:
            del held[mint]

    # 4. Exits first, so a sale frees a position slot and cash for entries.
    #    A mint with a sell already in flight gets no duplicate sell order.
    selling = set()
    for pending in state.pending.values():
        if pending.order["side"] == "sell":
            selling.add(pending.order["mint"])
    caps = state.hello["caps"]
    for mint in list(held):
        if mint in selling:
            continue
        position = state.positions.get(mint)
        if position is None:
            continue
        curve = state.curves.get(mint)
        if curve is None or curve.complete:
            continue
        record = held[mint]
        price = _price_of(curve)
        if price > record.peak_price:
            record.peak_price = price
        coin = coins.get(mint)
        trades_since = (len(coin.trades) - record.entry_trades) if coin is not None else 0
        sell_all = False
        sell_half = False
        if not record.took_tp1 and price >= TP1_MULT * record.entry_price:
            sell_half = True  # take profit (TP1_PART = 1: the whole position)
        elif price <= SL_MULT * record.entry_price:
            sell_all = True  # hard stop
        elif record.took_tp1 and price <= BREAKEVEN_MULT * record.entry_price:
            sell_all = True  # remainder stops at breakeven after TP1
        elif (
            record.peak_price >= TRAIL_ARM_MULT * record.entry_price
            and price <= TRAIL_DROP * record.peak_price
        ):
            sell_all = True  # trailing stop off the peak
        elif trades_since >= MAX_HOLD_TRADES:
            sell_all = True  # stale position: recycle the capital
        elif slot - record.entry_slot >= MAX_HOLD_SLOTS:
            sell_all = True  # time exit
        elif coin is not None and coin.creator_sell_sol > record.creator_sell_at_entry:
            sell_all = True  # the creator is selling: leave at once
        if sell_half:
            amount = position.token_amount // TP1_PART
            if amount > 0:
                orders.append({
                    "side": "sell",
                    "mint": mint,
                    "token_amount": amount,
                    "max_slippage_bps": SELL_SLIPPAGE_BPS,
                })
                record.took_tp1 = True
        elif sell_all:
            orders.append({
                "side": "sell",
                "mint": mint,
                "token_amount": position.token_amount,
                "max_slippage_bps": SELL_SLIPPAGE_BPS,
            })

    # 5. Entries: young undecided coins, in creation order, while position
    #    slots and cash last. A buy in flight blocks a second buy on the coin.
    pending_buys = set()
    for pending in state.pending.values():
        if pending.order["side"] == "buy":
            pending_buys.add(pending.order["mint"])
    tx_fee = state.tx_fee_lamports()
    free = state.free_cash_lamports()
    still_young = []
    for mint in young:
        coin = coins.get(mint)
        if coin is None:
            continue
        if slot - coin.slot0 > ENTRY_MAX_AGE_SLOTS:
            coin.decided = True
            continue
        still_young.append(mint)
        if len(orders) >= caps["max_orders_per_batch"]:
            continue
        if coin.decided or mint in held or mint in pending_buys:
            continue
        if len(coin.trades) > ENTRY_MAX_TRADES:
            coin.decided = True
            continue
        curve = state.curves.get(mint)
        if curve is None or curve.complete:
            continue
        price = _price_of(curve)
        if price >= ENTRY_MAX_RUNUP * coin.p0:
            continue  # already spiked past the window: never chase it
        if coin.dev_sold():
            coin.decided = True
            continue
        if coin.creator_buy_sol > ENTRY_MAX_CREATOR_BUY:
            coin.decided = True
            continue
        n_buyers = len(coin.buyers)
        if n_buyers < ENTRY_MIN_BUYERS or n_buyers > ENTRY_MAX_BUYERS:
            continue
        if coin.buy_share() < ENTRY_BUY_SHARE:
            continue
        if curve.real_quote_reserves < ENTRY_MIN_REAL_SOL:
            continue
        if state.committed_position_count() >= caps["max_open_positions"]:
            break
        cap = state.max_buy_lamports(mint)
        amount = min(cap, STAKE_LAMPORTS)
        if amount < MIN_STAKE_LAMPORTS:
            continue
        if free < amount + tx_fee:
            continue
        orders.append({
            "side": "buy",
            "mint": mint,
            "quote_lamports": amount,
            "max_slippage_bps": BUY_SLIPPAGE_BPS,
        })
        pending_buys.add(mint)
        coin.decided = True
        free -= amount + tx_fee
    young[:] = still_young

    return orders


if __name__ == "__main__":
    makefun_bot.run(on_batch)
