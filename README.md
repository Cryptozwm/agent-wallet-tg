# agent-wallet-tg

A Telegram bot that trades BSC tokens from your **Binance Agentic Wallet(s)** — on PancakeSwap and directly on
launchpad bonding curves (Four.meme, Flap, gang.fun V1–V3, Agent Curve) — through the official `baw` CLI.

[简体中文说明](README.zh-CN.md) · [Step-by-step tutorial (简体中文)](docs/tutorial.zh-CN.md) · [FAQ](docs/FAQ.md)

- **No private keys.** The bot never sees a key or seed phrase. Every transaction is signed on Binance's side by `baw`.
- **Preview, then confirm.** Every manual buy, sell, transfer or contract call is quoted or simulated first; nothing is
  sent until you tap ✅ Confirm. Previews expire after 90 s.
- **Owner only.** The bot answers one Telegram account (yours) in a private chat and ignores everyone else.
- **Several wallets at once.** Add wallets A, B, C… (one Binance account each) and buy/sell with all of them in one tap.

> ⚠️ This software trades real funds and has not been audited. Meme tokens can go to zero. Start with small amounts.
> Nothing here is investment advice.

## What it does

| Paste / tap | You get |
|---|---|
| A token address | A trading card: price, market cap, pool, holders, audit, your positions; buttons to buy, sell, limit order, TP/SL |
| A bonding-curve token (Four, Flap, gang.fun, Agent Curve) | The same card, but buys and sells go straight to the curve before graduation |
| A project contract address | Action buttons for its functions (mint, claim, refund…), each previewed before sending |
| A project's "instructions for agents" text | A presale/mint card built from it |
| Someone else's successful tx hash | "Copy this tx" with your own address swapped in |

Also: limit orders (kept by Binance), TP/SL rules the bot watches every 20 s, a new-launch sniper with daily quota,
optional sweep of profits to a main wallet, `/fn` `/tx` `/max` `/rules` for any contract.

Automatic features (TP/SL, sniper, sweep) are **off until you turn them on**, and are confirmed once when you arm them.

## Requirements

- Linux, macOS, or Windows with WSL2
- Python 3.10+ (standard library only — no `pip install`)
- Node.js 18+ and the Binance Agentic Wallet CLI: `npm install -g @binance/agentic-wallet`
- Foundry's `cast` (read-only chain calls): `curl -L https://foundry.paradigm.xyz | bash && foundryup`
- A Binance account with the Web3 Wallet's Agentic Wallet enabled (one Binance account per bot wallet)
- A Telegram bot token from [@BotFather](https://t.me/BotFather)

## Quick start

**Easiest:** give [INSTALL_WITH_AI.md](INSTALL_WITH_AI.md) to your AI assistant and ask it to install the bot with you
step by step. It checks each step and knows the usual errors.

Or by hand:

```bash
git clone https://github.com/Cryptozwm/agent-wallet-tg.git
cd agent-wallet-tg
mkdir -p ~/.config/agent-wallet-tg
cp env.example ~/.config/agent-wallet-tg/env && chmod 600 ~/.config/agent-wallet-tg/env
# edit the env file: TG_BOT_TOKEN=..., BOT_LANG=en or zh
./run.sh            # keeps the bot running; log in ~/.config/agent-wallet-tg/bot.log
```

1. Send any message to your bot. It replies with your Telegram ID.
2. Put it in `TG_OWNER_ID=` in the env file and restart (`pkill -f "python3 -u bot.py"`; `run.sh` restarts it in 10 s).
3. In the bot: **📊 Wallet status → ➕ Add wallet**, open the link and approve the login in the Binance app.
   Repeat with another Binance account for wallet B.
4. To buy on bonding curves or call contracts, turn on **Developer mode** for that wallet in the Binance app.
5. Paste a token address. Tap a buy amount → check the preview → **✅ Confirm**.

On Windows, run `install-task.ps1` from the repo folder to start the bot at logon without a window.

Stuck? See the [FAQ](docs/FAQ.md): install errors, login, Agent ID, refused buys, updating and uninstalling.

## Configuration

All settings live in `~/.config/agent-wallet-tg/env` (see [env.example](env.example)). Safety limits —
`MAX_BNB_PER_WALLET`, payout addresses in `wallets.json` — can only be changed on the computer, never from Telegram.
Everyday preferences (quick-buy amounts, slippage, default wallets, TP/SL) are changed with buttons under ⚙️ Settings.

## Safety notes

- Keep the env file private (`chmod 600`). Anyone with the bot token can impersonate the bot, but only `TG_OWNER_ID`
  can give it orders.
- The per-wallet cap applies to every manual and automatic action.
- Tokens with a high audit risk level are refused; pools below `MIN_LIQUIDITY_USD` need an extra tap.
- Binance may ask you to confirm some actions (e.g. sending to a new address) in the app; the bot tells you when.

## License

MIT
