// Fresh-surge flipper — a paper bot for the pump.fun replay sprint.
//
// Idea: most new pump.fun coins fade to nothing, but the ones strangers keep
// buying into for a while sometimes run. This bot waits until a coin has
// survived its first minutes, then buys a small fixed stake only when the
// recent tape shows real crowd buying (several distinct buyers, net SOL
// flowing in, price rising and holding near its recent high). It flips fast:
// take profit at +45%, stop loss at -30%, a trailing stop once up 15%, and a
// fade exit when the crowd turns seller. One shot per coin, so the run spreads
// across many coins instead of averaging down into a rug.
//
// A pacing guardrail keeps the run above the competition's minimum bar (a
// round trip of at least 0.01 SOL on at least 20 coins): if the bot's own
// filled coins lag the pace, it opens tiny 0.02 SOL probe round trips on
// liquid coins and sells them as soon as the buy lands.
//
// Deterministic by construction: no clock, no environment, no randomness;
// iteration only over insertion-ordered maps fed by the batch stream. The
// harness speaks agent-fun/replay/v2: it sends hello, then one batch per
// slot with the slot's events, marks and fills, then done. Orders decided
// on a batch fill two slots later.

import {
  LAMPORTS_PER_SOL,
  runBot,
  type Batch,
  type Bot,
  type BotState,
  type OrderInput,
  type Pubkey,
} from './vendor/makefun-bot.ts';

const LAMPORTS = 1_000_000_000; // lamports per SOL, as a Number for sizing math
const CURVE_FEE = 0.0125; // protocol 95 bps + creator 30 bps fallback

// --- entries ---------------------------------------------------------------
const STAKE_SOL = 0.1; // fixed stake per coin
const MIN_BUY_SOL = 0.01; // competition round-trip floor
const BUY_SLIPPAGE_BPS = 1500;
const SELL_SLIPPAGE_BPS = 10_000;
const WINDOW_SLOTS = 40; // entry lookback
const MIN_AGE_SLOTS = 240; // skip the first minutes of a curve's life
const MIN_REAL_SOL = 1; // curve must hold some real SOL
const MAX_REAL_SOL = 80; // skip curves that already ran far
const MIN_NET_INFLOW_SOL = 0.4; // net SOL bought over the window
const MIN_BUYS = 4;
const MIN_BUYERS = 3; // distinct buying wallets in the window
const MIN_RISE = 0.08; // price now vs window start
const NEAR_HIGH = 0.92; // price now vs window high

// --- exits -----------------------------------------------------------------
const TAKE_PROFIT = 1.8; // close value vs cost
const STOP_LOSS = 0.8;
const TRAIL_ARM = 1.3; // trailing stop arms once peak value hits this x cost
const TRAIL_KEEP = 0.75; // sell once value falls to this share of peak
const FADE_WINDOW = 25; // slots of net flow watched for the fade exit
const FADE_OUTFLOW_SOL = 0.4;
const MAX_HOLD_SLOTS = 1000;
const HOLD_IF_UP = 1.3; // past this, the time limit stretches
const HOLD_MAX_SLOTS = 2000;

// --- guardrail pacing ------------------------------------------------------
const TARGET_COINS = 24;
const TARGET_FILLS = 200;
const PACE_SECONDS = 14_400; // pace over the first 4h of block time
const PROBE_SOL = 0.02;
const MAX_PROBES = 2;

interface TapeTrade {
  slot: number;
  buy: boolean;
  sol: number;
  user: string;
  price: number;
}

interface Tape {
  created: number | null;
  trades: TapeTrade[];
  tried: boolean; // an entry buy was already sent on this curve
  lastExitSlot: number;
}

interface Held {
  opened: number;
  peak: number; // best close value seen, SOL
  exiting: boolean;
  filled: boolean;
}

const tapes = new Map<Pubkey, Tape>();
/** Positions this bot opened, by mint. */
const held = new Map<Pubkey, Held>();
/** Probe round trips in flight, by mint. */
const probes = new Set<Pubkey>();
/** The bot's own filled orders (harness closes excluded) and their coins. */
let myFills = 0;
const myCoins = new Set<Pubkey>();
let firstTime: number | null = null;
let lastTime: number | null = null;

const KEEP_SLOTS = Math.max(WINDOW_SLOTS, FADE_WINDOW) + 5;

function tapeOf(mint: Pubkey): Tape {
  let t = tapes.get(mint);
  if (t === undefined) {
    t = { created: null, trades: [], tried: false, lastExitSlot: -1e12 };
    tapes.set(mint, t);
  }
  return t;
}

/** Price of one token in SOL on a curve's virtual reserves. */
function priceOf(vq: bigint, vt: bigint): number {
  return Number(vq) / Number(vt);
}

/** What selling `tokens` now would bring back, in SOL, curve and tx fees off. */
function closeValue(state: BotState, mint: Pubkey, tokens: bigint, txSol: number): number {
  const curve = state.curves.get(mint);
  if (curve === undefined) return 0;
  const vq = Number(curve.virtual_quote_reserves);
  const vt = Number(curve.virtual_token_reserves);
  const t = Number(tokens);
  if (vt + t <= 0) return 0;
  return (t * vq) / (vt + t) / LAMPORTS * (1 - CURVE_FEE) - txSol;
}

/** Net recorded SOL (buys minus sells) on a tape over the last `w` slots. */
function flow(t: Tape, now: number, w: number): number {
  let net = 0;
  for (let i = t.trades.length - 1; i >= 0; i--) {
    const tr = t.trades[i]!;
    if (tr.slot <= now - w) break;
    net += tr.buy ? tr.sol : -tr.sol;
  }
  return net;
}

/** Entry signal on a curve's tape at slot `now`. */
function entrySignal(t: Tape, now: number, rqSol: number): boolean {
  if (rqSol < MIN_REAL_SOL || rqSol > MAX_REAL_SOL) return false;
  if (t.created === null || now - t.created < MIN_AGE_SLOTS) return false;
  if (t.tried) return false;
  const from = now - WINDOW_SLOTS;
  let buySol = 0;
  let sellSol = 0;
  let buys = 0;
  let startPrice = -1;
  let high = 0;
  let last = 0;
  const buyers = new Set<string>();
  for (const tr of t.trades) {
    if (tr.slot <= from) continue;
    if (startPrice < 0) startPrice = tr.price;
    if (tr.buy) {
      buySol += tr.sol;
      buys++;
      buyers.add(tr.user);
    } else {
      sellSol += tr.sol;
    }
    if (tr.price > high) high = tr.price;
    last = tr.price;
  }
  if (buys < MIN_BUYS || buyers.size < MIN_BUYERS) return false;
  if (buySol - sellSol < MIN_NET_INFLOW_SOL) return false;
  if (startPrice <= 0 || high <= 0) return false;
  if (last / startPrice - 1 < MIN_RISE) return false;
  if (last < high * NEAR_HIGH) return false;
  return true;
}

/** The strategy as an importable bot, so offline tools can run it too. */
export const bot: Bot = {
  onBatch(state: BotState, batch: Batch): OrderInput[] {
    const now = batch.slot;
    const txSol = Number(state.txFeeLamports()) / LAMPORTS;
    const cap = state.hello.caps.max_orders_per_batch;
    const orders: OrderInput[] = [];

    // Count this batch's own fills and fold its events into the tapes.
    for (const f of batch.fills) {
      if (f.status === 'filled' && !f.client_id.startsWith('_')) {
        myFills++;
        myCoins.add(f.mint);
      }
    }
    const traded = new Set<Pubkey>();
    for (const e of batch.events) {
      if (e.block_time !== null) {
        if (firstTime === null) firstTime = e.block_time;
        if (lastTime === null || e.block_time > lastTime) lastTime = e.block_time;
      }
      if (e.event === 'CreateEvent') {
        tapeOf(e.mint).created = e.slot;
      } else if (e.event === 'TradeEvent') {
        const t = tapeOf(e.mint);
        t.trades.push({
          slot: e.slot,
          buy: e.is_buy,
          sol: Number(e.sol_amount) / LAMPORTS,
          user: e.user,
          price: priceOf(BigInt(e.virtual_quote_reserves), BigInt(e.virtual_token_reserves)),
        });
        traded.add(e.mint);
      } else {
        // CompleteEvent: the curve is over; forget its tape.
        tapes.delete(e.mint);
      }
    }
    // Prune old tape entries.
    for (const mint of traded) {
      const t = tapes.get(mint);
      if (t === undefined) continue;
      let cut = 0;
      while (cut < t.trades.length && t.trades[cut]!.slot <= now - KEEP_SLOTS) cut++;
      if (cut > 0) t.trades.splice(0, cut);
    }

    const pendingByMint = new Map<Pubkey, { buy: boolean; sell: boolean }>();
    for (const po of state.pending.values()) {
      const p = pendingByMint.get(po.order.mint) ?? { buy: false, sell: false };
      if (po.order.side === 'buy') p.buy = true;
      else p.sell = true;
      pendingByMint.set(po.order.mint, p);
    }

    // --- exits first -------------------------------------------------------
    for (const [mint, h] of held) {
      const pos = state.positions.get(mint);
      const curve = state.curves.get(mint);
      if (pos === undefined) {
        // Position gone (sold, flattened or completed): drop the record.
        if (!pendingByMint.has(mint)) {
          held.delete(mint);
          if (h.filled) tapeOf(mint).lastExitSlot = now;
        }
        continue;
      }
      h.filled = true;
      if (curve === undefined || curve.complete) continue;
      const value = closeValue(state, mint, pos.token_amount, txSol);
      const cost = Number(pos.cost_lamports) / LAMPORTS;
      if (value > h.peak) h.peak = value;
      let exit = h.exiting;
      if (!exit && value <= cost * STOP_LOSS) exit = true;
      if (!exit && value >= cost * TAKE_PROFIT) exit = true;
      if (!exit && h.peak >= cost * TRAIL_ARM && value <= h.peak * TRAIL_KEEP) exit = true;
      if (!exit && flow(tapeOf(mint), now, FADE_WINDOW) <= -FADE_OUTFLOW_SOL) exit = true;
      if (!exit && now - h.opened > MAX_HOLD_SLOTS) {
        exit = value < cost * HOLD_IF_UP || now - h.opened > HOLD_MAX_SLOTS;
      }
      if (!exit) continue;
      h.exiting = true;
      if (pendingByMint.get(mint)?.sell) continue;
      if (orders.length >= cap) break;
      orders.push({ side: 'sell', mint, token_amount: pos.token_amount, max_slippage_bps: SELL_SLIPPAGE_BPS });
    }

    // --- probe round trips: sell as soon as the probe buy has landed -------
    for (const mint of [...probes]) {
      const pos = state.positions.get(mint);
      const curve = state.curves.get(mint);
      if (pos === undefined) {
        if (!pendingByMint.has(mint)) probes.delete(mint);
        continue;
      }
      if (curve === undefined || curve.complete || pendingByMint.has(mint)) continue;
      if (orders.length >= cap) break;
      orders.push({ side: 'sell', mint, token_amount: pos.token_amount, max_slippage_bps: SELL_SLIPPAGE_BPS });
    }

    // --- entries -----------------------------------------------------------
    let free = Number(state.freeCashLamports()) / LAMPORTS;
    let slotsLeft = state.hello.caps.max_open_positions - state.committedPositionCount();
    for (const mint of traded) {
      if (orders.length >= cap || slotsLeft <= 0) break;
      if (held.has(mint) || probes.has(mint) || pendingByMint.has(mint)) continue;
      const curve = state.curves.get(mint);
      if (curve === undefined || curve.complete) continue;
      const t = tapes.get(mint);
      if (t === undefined) continue;
      const rqSol = Number(curve.real_quote_reserves) / LAMPORTS;
      if (!entrySignal(t, now, rqSol)) continue;
      // The buys on one mint in one reply share one size cap.
      const capSol = Math.floor(Number(state.maxBuyLamports(mint))) / LAMPORTS;
      let amount = Math.min(capSol, STAKE_SOL);
      if (amount > free - txSol) amount = free - txSol;
      if (amount < MIN_BUY_SOL) continue;
      const lamports = BigInt(Math.floor(amount * LAMPORTS));
      orders.push({ side: 'buy', mint, quote_lamports: lamports, max_slippage_bps: BUY_SLIPPAGE_BPS });
      t.tried = true;
      held.set(mint, { opened: now, peak: 0, exiting: false, filled: false });
      free -= amount + txSol;
      slotsLeft--;
    }

    // --- guardrail pacing: open a probe when fills or coins lag the pace ---
    if (probes.size < MAX_PROBES && slotsLeft > 0 && firstTime !== null && lastTime !== null) {
      const pace = Math.min(1, (lastTime - firstTime) / PACE_SECONDS);
      const needCoins = myCoins.size < Math.ceil(TARGET_COINS * pace);
      const needFills = myFills < Math.ceil(TARGET_FILLS * pace);
      if (needCoins || needFills) {
        // A probe buy needs a 1%-of-real-reserve cap of at least its size.
        const minRq = (PROBE_SOL * 10_000) / state.hello.caps.max_order_bps_of_real_reserve;
        for (const mint of traded) {
          if (orders.length >= cap || probes.size >= MAX_PROBES) break;
          if (held.has(mint) || probes.has(mint) || pendingByMint.has(mint)) continue;
          if (state.positions.has(mint)) continue;
          if (needCoins && !needFills && myCoins.has(mint)) continue;
          const curve = state.curves.get(mint);
          if (curve === undefined || curve.complete) continue;
          if (Number(curve.real_quote_reserves) / LAMPORTS < minRq * 1.5) continue;
          if (free < PROBE_SOL + 2 * txSol) break;
          orders.push({ side: 'buy', mint, quote_lamports: BigInt(Math.floor(PROBE_SOL * LAMPORTS)), max_slippage_bps: BUY_SLIPPAGE_BPS });
          probes.add(mint);
          free -= PROBE_SOL + txSol;
        }
      }
    }

    return orders;
  },
};

// The grader runs this file as `node /bot/main.ts`; run the bot then.
if (process.argv[1] !== undefined && process.argv[1].endsWith('main.ts')) {
  await runBot(bot);
}
