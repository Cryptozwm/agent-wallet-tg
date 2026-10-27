# FAQ

New here? Start with the [Quick start](../README.md#quick-start) or the step-by-step [tutorial (简体中文)](tutorial.zh-CN.md).
[简体中文 FAQ](FAQ.zh-CN.md)

> ⚠️ This software trades real funds and has not been audited. Nothing here is investment advice.

- [Basics](#basics)
- [Install and start](#install-and-start)
- [Wallets and login](#wallets-and-login)
- [Buying and selling](#buying-and-selling)
- [Bonding curves, Agent ID, project contracts](#bonding-curves-agent-id-project-contracts)
- [Automatic features](#automatic-features)
- [Maintenance](#maintenance)

---

## Basics

**Does the bot charge a fee?**
No. The code is all public and takes no cut. Gas, and whatever Binance itself charges, are paid as usual.

**Can the bot see my private key?**
No. Keys stay in the Binance Agentic Wallet. The bot only calls Binance's official `baw` CLI; signing happens on Binance's side.

**Someone knows my bot's username. Can they use it?**
No. The bot only answers private messages from `TG_OWNER_ID` and ignores everyone else, including groups.

**My bot token leaked.**
Whoever has it can impersonate the bot but cannot give it orders. Still, send `/revoke` to @BotFather, put the new token in the env file and restart.

**Which chains?**
BSC (BNB Chain) only.

**Does my computer have to stay on?**
The bot runs on your machine and stops when it sleeps or shuts down.
- **Limit orders** are kept by Binance and still fill.
- **TP/SL, the sniper and sweeps** only run while the bot runs. For 24/7, use an always-on computer or a Linux server.

## Install and start

**Windows?**
Yes, with WSL2. Clone the repo inside Ubuntu (e.g. `~/agent-wallet-tg`), not on the C: drive.

**macOS?**
Yes. `brew install node git`, everything else is the same. There is no autostart script; use `tmux` or `screen`.

**`./run.sh: Permission denied`**
Run `chmod +x run.sh` once, or start it with `bash run.sh`.

**`TG_BOT_TOKEN is missing`**
The env file must be at `~/.config/agent-wallet-tg/env` (not in the repo folder) with a line `TG_BOT_TOKEN=123456:ABC...`, no spaces around `=`.

**`warning: baw not found`**
Install the Binance CLI: `sudo npm install -g @binance/agentic-wallet`, then check `baw --version`. If it lives elsewhere, set `BAW_BIN=/full/path/baw` in the env file.

**`cast: command not found`**
After installing Foundry, open a new terminal (or `source ~/.bashrc`) and run `foundryup`. The bot also looks in `~/.foundry/bin/cast`.

**The bot does not answer at all.**
1. Is `run.sh` still running? Closing its terminal stops the bot.
2. Check `~/.config/agent-wallet-tg/bot.log` and `stdout.log`.
3. `401 Unauthorized` in the log means a wrong token.
4. It replies "Your Telegram ID is …": `TG_OWNER_ID` is empty, or you did not restart after filling it in.
5. Message it in a private chat, not a group.

**It ignored my first message.**
On start the bot skips messages that arrived while it was offline, and never acts on commands older than 2 minutes. Send it again.

**How do I restart?**
`Ctrl+C` in the `run.sh` terminal and run `./run.sh` again, or `pkill -f "python3 -u bot.py"` from another terminal (`run.sh` restarts it in 10 s).

**`bot is already running`**
One instance is already up (e.g. the autostart task). Only one runs at a time.

**`install-task.ps1` says "Access is denied".**
Open PowerShell as Administrator and run (replace `you` with your Ubuntu user name):
```powershell
cd \\wsl.localhost\Ubuntu\home\you\agent-wallet-tg
powershell -ExecutionPolicy Bypass -File .\install-task.ps1
```
Remove the task with `Unregister-ScheduledTask -TaskName agent-wallet-tg -Confirm:$false`.

## Wallets and login

**Do I need a Binance account?**
Yes, with the Web3 Wallet's **Agentic Wallet** enabled in the Binance app.

**More wallets?**
One Binance account per wallet. With another account, tap **📊 Wallet status → ➕ Add wallet** again to get wallet B, C… Adding the same account twice is caught.

**What do I do with the login link?**
Approve it in the Binance app and **check that the pairing code matches the one the bot shows**. Do it within 5 minutes.

**"Login expired"**
Tap **🔑 Re-login** and approve in the app. Wallet status (details) shows when an idle session will be signed out.

**I logged in again with the wrong account.**
The bot warns that the address changed, does not treat it as the old wallet, and pauses that wallet. Log in again with the original account.

**Remove a wallet?**
**📊 Wallet status** → the wallet → **Remove**. The Binance account and its funds are untouched; you can add it back later.

**How do I fund it?**
Send BNB **on BSC** to the address the bot shows. Buys and gas are paid in BNB.

**"Quota left today"**
The Agentic Wallet has its own daily limit, set in the Binance app. When it runs out, nothing more is sent that day.

## Buying and selling

**Why was my buy refused?**
- **High audit risk** (level ≥ `BLOCK_RISK_LEVEL`, default 4): refused.
- **Small pool** (below `MIN_LIQUIDITY_USD`, default $20,000): tap **🔓 Buy anyway (30 min)**.
- **Over the per-wallet cap** (`MAX_BNB_PER_WALLET`, default 0.1 BNB): change it in the env file; Telegram can't, on purpose.
- **Not enough BNB**: each wallet keeps 0.002 BNB for gas.

**The preview expired.**
Previews expire after 90 s (`CONFIRM_TTL_SECONDS`) so you never confirm a stale price. Tap the amount again.

**"On-chain simulation failed"**
The transaction was simulated before sending and would have failed, so nothing was sent. The contract's reason is shown in brackets.

**"result unknown"**
No clear result came back. The bot **never retries** (to avoid buying or selling twice). Check **📜 Recent trades** or BscScan later.

**What does `0.0(4)52` mean?**
Four zeros after the decimal point: `0.000052`.

**Cards are slow or empty.**
The default public BSC node can be slow or rate-limited. Set `RPC_URL=` to your own node in the env file and restart.

**My minted NFT is not in my positions.**
Cards and the overview list tokens (ERC20) only. Look the wallet up on BscScan.

## Bonding curves, Agent ID, project contracts

**What is a bonding-curve token?**
A token still on its launchpad curve (Four.meme, Flap, gang.fun, Agent Curve). Regular Binance swaps can't buy it; the bot detects it and trades on the curve directly, then switches to PancakeSwap after graduation.

**It asks for Developer mode.**
Curve trades and project contracts are contract calls. Turn on **Developer mode** in the Agentic Wallet settings in the Binance app. Regular swaps don't need it.
Developer mode expires; **📊 Wallet status → details** shows until when. Turn it on again in the app when it lapses.

**What is an Agent ID and where is it needed?**
A `.ai` identity (an on-chain NFT) that Agentic Wallets can claim. Some launches only sell to wallets holding one: Four.meme agent-only launches, Flap Agent-ID-gated launches, Agent Curve.
- Wallet status shows each wallet's Agent ID, or "no Agent ID".
- `baw wallet agent-id` shows it too.
- Claim it in the Agentic Wallet in the Binance app.
- **Every wallet needs its own.** If only A has one, B still can't buy.

**Can it mint NFTs?**
Yes (BSC). Paste the project contract, tap the mint action, fill in the amount; it is simulated before you confirm. Allowlist mints usually need a proof or a signature from the project's site, which you paste in. You can also paste someone's successful mint tx and copy it.

**A project gave "instructions for agents".**
Paste the whole text. The bot reads the contract and function from it and builds a presale/mint card.

**Is copying someone's tx safe?**
The bot swaps their address for yours and simulates first. Read the preview: what you pay, what you get, who receives it. Plain transfers and contract deployments can't be copied.

## Automatic features

**How often is TP/SL checked?**
Every 20 s against an on-chain quote. When a rule hits, it sells without asking again (you confirmed when you set it). Only while the bot runs.

**Will the sniper buy random tokens?**
It starts in **simulation** and only reports what it would have bought. It buys for real only after **⚡ Go live** and a confirmation. You set the amount per wallet, a daily BNB budget and a daily count.

**Sweeping profits to a main wallet?**
The payout address can only be set on the computer: `payout` in `~/.config/agent-wallet-tg/wallets.json` (see `wallets.example.json`). Add it to the Binance app address book first.

**Turn everything automatic off?**
**🎯 TP/SL → 🗑 Clear all**, switch the sniper off, and turn off **⚙️ Settings → 📤 Auto sweep**. Or stop the bot.

## Maintenance

**Update to a new version**
```bash
cd ~/agent-wallet-tg
git pull
pkill -f "python3 -u bot.py"   # run.sh restarts it on the new version in 10 s
```
Settings, wallets and rules live in `~/.config/agent-wallet-tg/` and are not touched.

**Language or time zone?**
`BOT_LANG=en` or `zh` (Simplified Chinese); `TZ_OFFSET_HOURS=0` for UTC, etc. Restart after changing.

**Moving to another computer?**
Install as usual, copy `~/.config/agent-wallet-tg/` over, and log each wallet in again.

**Uninstall**
1. Stop the bot (and remove the Windows task if you added it).
2. Delete the repo folder and `~/.config/agent-wallet-tg/`.
3. Send `/deletebot` to @BotFather.

Your Agentic Wallet and funds stay at Binance.

---

Still stuck? Open a GitHub issue. **Never post your token, wallet files or anything private.**
