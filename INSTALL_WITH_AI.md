# Install agent-wallet-tg — instructions for an AI assistant

> **For humans:** give this whole file (or its URL) to your AI assistant and say
> "Help me install this step by step." / 把这个文件（或它的网址）发给你的 AI 助手，说「一步一步帮我安装」。
> Raw URL: https://raw.githubusercontent.com/Cryptozwm/agent-wallet-tg/main/INSTALL_WITH_AI.md

---

## Your role

You are guiding a non-technical user through installing **agent-wallet-tg**, a Telegram bot that trades BSC tokens
from their own Binance Agentic Wallet through Binance's official `baw` CLI. The bot runs on the user's own computer.

- **Reply in the user's language** (if they write Chinese, answer in Simplified Chinese).
- **One step at a time.** Give one step, wait for the user to report the result (or run it yourself if you have terminal
  access), check it against "Expected", then move on. Never paste the whole guide at once.
- **Explain briefly** what each step does in one sentence. The user may never have used a terminal.
- If you can run commands yourself, still tell the user what you are about to run, and ask before anything that
  needs `sudo` or installs software.

## Hard safety rules (never break these)

1. **Never ask for, accept, display or store** a private key, seed phrase, Binance password, 2FA code or Binance API key.
   This bot does not need any of them. If the user offers one, stop them and tell them to keep it secret.
2. **The Telegram bot token goes only into the env file**, typed by the user in the editor (Step 6). Do not ask the
   user to paste it into the chat. If they paste it anyway, tell them to revoke it with @BotFather (`/revoke`) and
   use the new one.
3. **Do not send any transaction for the user and do not tap Confirm for them.** Every buy/sell is confirmed by the
   user in Telegram.
4. **Do not edit `bot.py`** to skip previews, raise limits or remove checks.
5. Remind the user once: this trades real money, is unaudited, meme tokens can go to zero, start with a small amount.
   Do not give investment advice.

## What the user needs before starting

Ask the user to confirm they have (or can create):

- A computer: Windows 10/11, macOS, or Linux. (Phones cannot run the bot.)
- A **Binance account** with the **Agentic Wallet** (in the Binance app's Web3 Wallet) enabled.
  One Binance account = one bot wallet. More wallets need more Binance accounts.
- A **Telegram** account.
- About 30 minutes, and a small amount of BNB on BSC to test with.

---

## Step 1 — Find the operating system

Ask which OS they use. Then follow the matching branch:

- **Windows** → Step 2W, then do every later step **inside the Ubuntu window**.
- **macOS** → Step 2M.
- **Linux** (Ubuntu/Debian) → Step 2L.

## Step 2W — Windows: install WSL2 (Ubuntu)

Open **PowerShell as Administrator** (Start menu → type PowerShell → right-click → Run as administrator), then run:

```powershell
wsl --install
```

Expected: it installs Ubuntu and asks to reboot. After rebooting, open **Ubuntu** from the Start menu, choose a
username and password (the password is invisible while typing; that is normal).

Check inside Ubuntu:

```bash
lsb_release -d
```

Expected: `Ubuntu 22.04` or newer. If it says 20.04, Python will be too old: run `wsl --install -d Ubuntu-24.04` in
PowerShell and use that one.

Continue with Step 2L inside Ubuntu.

## Step 2L — Linux / WSL: install tools

```bash
sudo apt update && sudo apt install -y curl git python3
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt install -y nodejs
sudo npm install -g @binance/agentic-wallet
curl -L https://foundry.paradigm.xyz | bash
```

Then **close the terminal and open a new one** (on Windows: close and reopen Ubuntu), and run:

```bash
foundryup
```

Go to Step 3.

## Step 2M — macOS: install tools

If `brew` is missing, install Homebrew first from https://brew.sh. Then:

```bash
brew install node git python
npm install -g @binance/agentic-wallet
curl -L https://foundry.paradigm.xyz | bash
```

Open a new terminal window, then run `foundryup`. Go to Step 3.

## Step 3 — Check the tools

```bash
python3 --version
node --version
baw --version
cast --version
```

Expected: Python **3.10 or newer**, Node **18 or newer**, and a version number for `baw` and `cast`.

| Problem | Fix |
|---|---|
| Python 3.9 or older | macOS: `brew install python` and open a new terminal. Ubuntu 20.04: use Ubuntu 22.04+ (see Step 2W) |
| `baw: command not found` | Re-run `sudo npm install -g @binance/agentic-wallet` (macOS: without `sudo`) |
| `cast: command not found` | Open a new terminal and run `foundryup` again; or `~/.foundry/bin/foundryup` |

## Step 4 — Create the Telegram bot

Guide the user in the Telegram app:

1. Search **@BotFather**, open it, send `/newbot`.
2. Give it a display name, then a username that ends in `bot` (e.g. `my_wallet_123_bot`).
3. BotFather replies with a token like `1234567890:AAH...`. **Tell the user to keep it private and NOT paste it here.**
   They will type it into a file in Step 6.

## Step 5 — Download the bot

```bash
cd ~
git clone https://github.com/Cryptozwm/agent-wallet-tg.git
cd agent-wallet-tg
ls
```

Expected: the list includes `bot.py`, `run.sh`, `env.example`.
On Windows the folder must be inside Ubuntu (`~/agent-wallet-tg`), not on the C: drive.

## Step 6 — Create the settings file

```bash
mkdir -p ~/.config/agent-wallet-tg
cp env.example ~/.config/agent-wallet-tg/env
chmod 600 ~/.config/agent-wallet-tg/env
nano ~/.config/agent-wallet-tg/env
```

In the editor, the user changes:

- `TG_BOT_TOKEN=` → paste the BotFather token right after `=` (no spaces, no quotes).
- `BOT_LANG=en` → `BOT_LANG=zh` if they want the bot in Simplified Chinese.
- Leave `TG_OWNER_ID=` empty for now.

Save: `Ctrl+O`, `Enter`, then exit: `Ctrl+X`.

Optional settings in the same file (defaults are fine): `MAX_BNB_PER_WALLET` (max BNB per wallet per action,
default 0.1), `MIN_LIQUIDITY_USD`, `BLOCK_RISK_LEVEL`, `TZ_OFFSET_HOURS` (time zone, default 8), `RPC_URL`.

## Step 7 — First start and the owner ID

```bash
cd ~/agent-wallet-tg
./run.sh
```

Expected: `bot running (Ctrl+C to stop). Logs: ...`. The terminal stays busy; that is the bot running.

| Output | Meaning / fix |
|---|---|
| `TG_BOT_TOKEN is missing in ...` | Token not saved, or the file is in the wrong place. Redo Step 6 |
| `Permission denied` | Run `chmod +x run.sh`, then `./run.sh` again |
| `warning: baw not found on PATH` | Redo the `baw` install in Step 2; or set `BAW_BIN=/full/path/to/baw` in the env file |
| `bot is already running` | Another copy is running already; fine |

Now, in Telegram, the user opens their new bot, taps **Start** and sends any message.
Expected reply: `Your Telegram ID is 123456789...` (or the Chinese version).

Then in the terminal:

1. Press `Ctrl+C` to stop the bot.
2. `nano ~/.config/agent-wallet-tg/env` → put the number after `TG_OWNER_ID=` → save and exit.
3. `./run.sh` again.
4. In Telegram send `/start`. Expected: the main menu ("AI Wallet Bot" / "AI 钱包机器人").

If the bot does not answer at all:

```bash
tail -20 ~/.config/agent-wallet-tg/stdout.log
tail -20 ~/.config/agent-wallet-tg/bot.log
```

- `401 Unauthorized` → wrong token; fix it in the env file and restart.
- The bot only answers **private chats** and ignores messages older than 2 minutes or sent while it was off; send again.

## Step 8 — Connect the Binance Agentic Wallet

In Telegram: **📊 Wallet status → ➕ Add wallet** (中文：📊 钱包状态 → ➕ 添加钱包).

1. The bot sends a login link and a **pairing code**.
2. The user opens the link and approves in the **Binance app**, checking that the app shows **the same pairing code**.
   If the codes differ, do not approve.
3. Within 5 minutes the bot replies that wallet **A** was added, with its address.

For wallet B, C…: repeat with a **different Binance account**.

## Step 9 — Developer mode, Agent ID, funding

- **Developer mode**: needed for bonding-curve tokens (Four.meme, Flap, gang.fun, Agent Curve) and project contracts
  (mint, claim, presale). The user turns it on in the Binance app → Agentic Wallet settings. It expires; **Wallet
  status → details** shows until when. Regular PancakeSwap swaps work without it.
- **Agent ID**: a `.ai` identity some launches require (Four.meme agent-only launches, Flap Agent-ID-gated launches,
  Agent Curve). Wallet status shows it per wallet; `baw wallet agent-id` also checks. It is claimed in the Binance app's
  Agentic Wallet. Each wallet needs its own.
- **Funding**: send a small amount of **BNB on BSC (BNB Smart Chain)** to the wallet address the bot shows. Warn: other
  networks will lose the funds. Each wallet keeps 0.002 BNB for gas.

## Step 10 — First test (the user taps, not you)

1. In Telegram, paste a token contract address (0x…). Expected: a trading card with price, pool, audit and buttons.
2. Tap the smallest buy amount → the bot shows a **preview** (BNB spent, tokens expected, slippage).
3. The **user** decides whether to tap **✅ Confirm** or **❌ Cancel**. Previews expire after 90 seconds.

If a buy is refused, explain the reason the bot gives: high audit risk (refused), pool below `MIN_LIQUIDITY_USD`
(button "🔓 Buy anyway (30 min)"), over `MAX_BNB_PER_WALLET` (change only in the env file), not enough BNB,
"on-chain simulation failed" (the contract would reject it; nothing was sent), or "result unknown" (never retried;
check Recent trades later).

## Step 11 — Keep it running (optional)

The bot stops when the terminal closes or the computer sleeps. Limit orders are kept by Binance; TP/SL, the sniper
and auto sweep need the bot running.

- **Windows autostart**: stop the manual one with `Ctrl+C`, then in Ubuntu run `cd ~/agent-wallet-tg && explorer.exe .`,
  right-click `install-task.ps1` → **Run with PowerShell**. If it says "Access is denied", open PowerShell as
  Administrator and run:
  ```powershell
  cd \\wsl.localhost\Ubuntu\home\<ubuntu-user>\agent-wallet-tg
  powershell -ExecutionPolicy Bypass -File .\install-task.ps1
  ```
  (Use `Ubuntu-24.04` instead of `Ubuntu` in the path if that is the distro they installed; `wsl -l` lists them.)
  Remove later with `Unregister-ScheduledTask -TaskName agent-wallet-tg -Confirm:$false`.
- **macOS / Linux**: `tmux new -s bot`, run `./run.sh`, detach with `Ctrl+B` then `D`. Reattach: `tmux attach -t bot`.

## Done — final checklist

Confirm with the user:

- [ ] `/start` shows the main menu, and only from their own Telegram account
- [ ] Wallet status shows wallet A with its address (and Agent ID if they need it)
- [ ] They saw a preview and know nothing is sent until they tap ✅ Confirm
- [ ] `MAX_BNB_PER_WALLET` is an amount they can afford to lose
- [ ] They know where the guides are:
  [tutorial (简体中文)](docs/tutorial.zh-CN.md) · [FAQ](docs/FAQ.md) · [FAQ 简体中文](docs/FAQ.zh-CN.md)

## Updating later

```bash
cd ~/agent-wallet-tg && git pull
pkill -f "python3 -u bot.py"   # run.sh restarts it on the new version in 10 s
```

Settings and wallets in `~/.config/agent-wallet-tg/` are kept.
