#!/usr/bin/env python3
"""Telegram bot for one or more Binance Agentic Wallets (A, B, ...), driven through the baw CLI.

Every manual action that moves funds is quoted or previewed first and only runs after the owner taps Confirm.
Only the owner's private chat is served. Standard library only. UI language: BOT_LANG=en (default) or zh.

Dry run (prints instead of messaging, never executes):  python3 bot.py --dry /buy 0x... 0.01 A
"""

import html
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from pathlib import Path

HOME_DIR = Path.home()
CONF_DIR = HOME_DIR / '.config' / 'agent-wallet-tg'
ENV_FILE = CONF_DIR / 'env'
LOG_FILE = CONF_DIR / 'bot.log'


def load_env(path):
    env = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, value = line.split('=', 1)
            env[key.strip()] = value.strip().strip('"').strip("'")
    return env


CFG = load_env(ENV_FILE)
TOKEN = CFG.get('TG_BOT_TOKEN', '')
OWNER = int(CFG['TG_OWNER_ID']) if CFG.get('TG_OWNER_ID', '').isdigit() else None
MAX_BNB = Decimal(CFG.get('MAX_BNB_PER_WALLET', '0.1'))  # cap per wallet per action
SLIPPAGE = CFG.get('SLIPPAGE', 'auto')
BLOCK_RISK = int(CFG.get('BLOCK_RISK_LEVEL', '4'))  # audit risk level at or above this is refused
CONFIRM_TTL = int(CFG.get('CONFIRM_TTL_SECONDS', '90'))
MIN_LIQ = Decimal(CFG.get('MIN_LIQUIDITY_USD', '20000'))  # buys into a smaller pool are refused; 0 turns this off
GAS_RESERVE = Decimal('0.002')
DRY = '--dry' in sys.argv
LANG = (os.environ.get('BOT_LANG') or CFG.get('BOT_LANG') or 'en').lower()  # en, or zh for Simplified Chinese
ZH = LANG.startswith('zh')
TZ_HOURS = float(CFG.get('TZ_OFFSET_HOURS', '8'))  # times are shown in this UTC offset


def L(en, zh):
    """The text for the configured language."""
    return zh if ZH else en


def zh_word(*words):
    """Chinese words typed by the user that are accepted whatever BOT_LANG is."""
    return words


PAY_WORDS = ('pay',) + zh_word('付')


def pay_word(x):
    """The prefix that marks the BNB to send ("pay" or its Chinese word), or None."""
    return next((p for p in PAY_WORDS if str(x).lower().startswith(p)), None)

_baw = shutil.which('baw')
NODE_BIN = CFG.get('NODE_BIN') or (os.path.dirname(_baw) if _baw else str(HOME_DIR / '.local' / 'opt' / 'node' / 'bin'))
BAW = CFG.get('BAW_BIN') or _baw or NODE_BIN + '/baw'
CAST = CFG.get('CAST_BIN') or shutil.which('cast') or str(HOME_DIR / '.foundry' / 'bin' / 'cast')
RPC = os.environ.get('AWT_TEST_RPC') or CFG.get('RPC_URL') or 'https://bsc-dataseed.bnbchain.org'  # tests point this at anvil
CHAIN = '56'
NATIVE = '0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE'
AUDIT_URL = 'https://web3.binance.com/bapi/defi/v1/public/wallet-direct/security/token/audit'
DYNAMIC_URL = 'https://web3.binance.com/bapi/defi/v4/public/wallet-direct/buw/wallet/market/token/dynamic/info/ai'
META_URL = 'https://web3.binance.com/bapi/defi/v1/public/wallet-direct/buw/wallet/dex/market/token/meta/info/ai'
SEARCH_URL = 'https://web3.binance.com/bapi/defi/v5/public/wallet-direct/buw/wallet/market/token/search/ai'

WALLETS_FILE = CONF_DIR / 'wallets.json'
ADDR_RE = re.compile(r'^0x[0-9a-fA-F]{40}$')


def load_wallets():
    """wallets.json -> ({letter: {'addr', 'env', 'dir'}}, [{'name', 'addr'}] payout addresses).

    {"wallets": [{"name": "A", "addr": "0x…", "baw_dir": "~/.baw"}, …],
     "payout": [{"name": "Main", "addr": "0x…"}]}
    Payout addresses can only be edited on the computer, never from Telegram.
    """
    try:
        data = json.loads(WALLETS_FILE.read_text())
    except (OSError, ValueError):
        data = {}
    ws = {}
    for row in data.get('wallets') or []:
        name, addr = str(row.get('name', '')).upper(), str(row.get('addr', ''))
        if re.fullmatch('[A-Z]', name) and ADDR_RE.match(addr) and name not in ws:
            d = os.path.expanduser(str(row.get('baw_dir') or ''))
            ws[name] = {'addr': addr, 'dir': d, 'env': {'BINANCE_BAW_DIR': d} if d else {}}
    payout = [{'name': str(p.get('name') or p['addr'][:6]), 'addr': p['addr']}
              for p in data.get('payout') or [] if ADDR_RE.match(str(p.get('addr', '')))]
    return ws, payout


def save_wallet(name, addr, baw_dir):
    try:
        data = json.loads(WALLETS_FILE.read_text())
    except (OSError, ValueError):
        data = {}
    rows = [r for r in data.get('wallets') or [] if str(r.get('name', '')).upper() != name]
    data['wallets'] = sorted(rows + [{'name': name, 'addr': addr, 'baw_dir': baw_dir}], key=lambda r: r['name'])
    data.setdefault('payout', [])
    WALLETS_FILE.parent.mkdir(parents=True, exist_ok=True)
    WALLETS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    WALLETS.clear()
    WALLETS.update(load_wallets()[0])


WALLETS, PAYOUT = load_wallets()
BSCSCAN_KEY = CFG.get('BSCSCAN_API_KEY', '')  # optional: verified ABIs that Sourcify does not have
AGENT_ID_REGISTRY = '0x60B9262456dE011F7d15a54CdD8c48bAaa8B022F'  # .ai Agent ID handles
EIP1967_IMPL ='0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc'
TRUSTED = {  # common tokens that skip the audit: BNB, USDT, USDC, WBNB
    NATIVE.lower(),
    '0x55d398326f99059ff775485246999027b3197955',
    '0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d',
    '0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c',
}

PENDING = {}  # nonce -> {'expires': ts, 'action': callable}
PENDING_LOCK = threading.Lock()  # background threads (Gang step 2, presale step 2) add while the poller pops
ADDR_OK = {}  # wallet -> time baw was last seen logged into the expected address
ADDR_TTL = 600


class UsageError(Exception):
    pass


# ---------------------------------------------------------------- helpers

def dec(x):
    try:
        return Decimal(str(x))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(0)


def fmt(x, places=6):
    s = f'{dec(x).quantize(Decimal(1).scaleb(-places), rounding=ROUND_DOWN):f}'
    return s.rstrip('0').rstrip('.') if '.' in s else s


def to_wei(bnb):
    return int((dec(bnb) * 10 ** 18).to_integral_value(rounding=ROUND_DOWN))


def from_wei(wei):
    return Decimal(int(wei)) / Decimal(10 ** 18)


def run(cmd, env_extra=None, timeout=120):
    env = dict(os.environ)
    env['PATH'] = NODE_BIN + ':' + env.get('PATH', '')
    if env_extra:
        env.update(env_extra)
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=timeout)
    except subprocess.TimeoutExpired:  # callers treat this like any failed command; baw sends read it as "unknown"
        return 124, '', f'timed out after {timeout}s'
    return p.returncode, p.stdout, p.stderr


def write_json(path, obj):
    """Write via a temp file and rename, so a crash mid-write (or a reader in another thread) never sees half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f'{path.name}.{threading.get_ident()}.tmp')
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1))
    tmp.replace(path)


def baw(w, *args, timeout=120):
    _, out, err = run([BAW, *args, '--json'], WALLETS[w]['env'], timeout)
    return parse_json(out, err)


def parse_json(out, err=''):
    text = out.strip()
    start = text.find('{')
    try:
        return json.loads(text[start:]) if start >= 0 else {'success': False, 'error': {'message': (err or text)[:300]}}
    except json.JSONDecodeError:
        return {'success': False, 'error': {'message': text[:300]}}


def baw_raw(r):
    """baw's own error text (code name + English message), for logs and pattern checks."""
    e = r.get('error') or {}
    msg = f"{e.get('name') or e.get('code') or ''} {e.get('message') or ''}".strip()
    return msg or json.dumps(r, ensure_ascii=False)[:300]


# baw error code names -> what to tell the user
BAW_ERR_ZH = {
    'SESSION_EXPIRED': L('Login expired, please log in again (tap "🔑 Re-login" in /wallet)', '登录过期了，要重新登录（/wallet 里按「🔑 重新登录」）'),
    'SESSION_NOT_FOUND': L('No login data found, please log in again (tap "🔑 Re-login" in /wallet)', '找不到登录数据，要重新登录（/wallet 里按「🔑 重新登录」）'),
    'NOT_LOGGED_IN': L('Not logged in yet (tap "🔑 Re-login" in /wallet)', '还没登录（/wallet 里按「🔑 重新登录」）'),
    'UNAUTHORIZED': L('Login no longer valid, please log in again (tap "🔑 Re-login" in /wallet)', '登录失效了，要重新登录（/wallet 里按「🔑 重新登录」）'),
    'AUTH_REJECTED': L('The Binance app rejected the approval, or the QR code expired', '币安 App 拒绝了授权，或 QR 码过期'),
    'QR_CODE_EXPIRED': L('QR code expired', 'QR 码过期了'),
    'POLLING_TIMEOUT': L('Timed out waiting for confirmation in the Binance app', '等币安 App 确认等太久，逾时了'),
    'WALLET_NOT_EXIST': L('This Binance account has no Web3 wallet yet', '这个币安账号还没创建 Web3 钱包'),
    'WALLET_CREATING': L('Wallet is still being created, try again shortly', '钱包还在创建中，等一下再试'),
    'BALANCE_API_ERROR': L('Binance balance lookup failed', '币安查余额失败'),
    'INSUFFICIENT_BALANCE': L('Insufficient balance', '余额不够'),
    'INSUFFICIENT_GAS': L('Not enough BNB for gas', 'BNB 不够付手续费'),
    'INVALID_ADDRESS': L('Invalid address', '地址格式不对'),
    'INVALID_AMOUNT': L('Invalid amount', '数量格式不对'),
    'INVALID_TOKEN': L('Binance does not support this token', '币安不支持这个币'),
    'INVALID_PARAMS': L('Invalid parameters', '参数不对'),
    'COMPLIANCE_FAILED': L('Failed the Binance compliance check', '没通过币安的合规检查'),
    'APP_CONFIRMATION_REQUIRED': L('Confirm again in the Binance app', '要在币安 App 上再确认一次'),
    'QUOTE_API_ERROR': L('Binance quote service error', '币安报价服务出错'),
    'QUOTE_EXPIRED': L('Quote expired, go back to the card and tap again', '报价过期了，回卡片再按一次'),
    'QUOTE_AMOUNT_TOO_SMALL': L('Amount too small to quote', '金额太小，报不出价'),
    'NO_ROUTE_FOUND': L('No trade route found (maybe no DEX pool yet)', '找不到交易路线（可能还没有外盘池子）'),
    'ORDER_API_ERROR': L('Binance order service error', '币安下单服务出错'),
    'SLIPPAGE_EXCEEDED': L('Price moved too fast, exceeded slippage', '价格跑太快，超过滑点'),
    'GAS_PRICE_TOO_LOW': L('Gas price too low', 'Gas 价格太低'),
    'SIGN_FAILED': L('Signing failed', '签名失败'),
    'BROADCAST_FAILED': L('Transaction was not broadcast', '交易没广播出去'),
    'ORDER_TIMEOUT': L('Order timed out', '订单等太久，逾时了'),
    'ORDER_NOT_FOUND': L('Order not found', '找不到这张单'),
    'CANNOT_CANCEL_EXECUTED': L('Already filled, cannot cancel', '已经成交了，不能撤'),
    'CANCEL_TIMEOUT': L('Cancel order timed out', '撤单逾时'),
    'TX_NOT_FOUND': L('Transaction not found', '找不到这笔交易'),
    'DEFI_TX_SIMULATION_FAILED': L('On-chain simulation failed, this would fail if sent', '链上试跑失败，这笔送出去会失败'),
    'DEFI_SECURITY_RISK_BLOCKED': L('Blocked by Binance risk control (flagged as a security risk)', '币安风控挡下（判定有安全风险）'),
    'NETWORK_ERROR': L('Cannot reach Binance', '连不上币安'),
    'REQUEST_TIMEOUT': L('Binance response timed out', '币安回应逾时'),
    'SERVICE_UNAVAILABLE': L('Binance is busy (or too many requests), try again shortly', '币安服务暂时忙不过来（或查太频繁），等一下再试'),
    'DNS_RESOLVE_FAILED': L('Cannot resolve the Binance domain', '网络查不到币安网址'),
    'SSL_ERROR': L('Certificate error connecting to Binance', '连币安的凭证出错'),
    'UNKNOWN_ERROR': L('Unknown error', '未知错误'),
}
# server-side messages (code name SERVICE_ERROR or anything unmapped), first match wins
BAW_MSG_ZH = [
    (r'(?i)simulat', L('On-chain simulation failed: given the current chain state, this would fail if sent'
                     ' (usually the balance changed, e.g. the same tokens were just spent by another transaction, or the price moved too much)', '链上试跑失败：照现在的链上状态，这笔送出去会失败'
                     '（常见原因：余额已经变了，例如同一批币刚被另一笔交易用掉；或价格动太多）')),
    (r'(?i)address.?book|whitelist', L('Recipient is not in the Binance app address book', '收款地址不在币安 App 的地址簿里')),
    (r'(?i)daily.{0,10}(limit|quota)|quota', L("Exceeds today's limit", '超过今天的额度')),
    (r'(?i)insufficient', L('Insufficient balance', '余额不够')),
    (r'(?i)risk', L('Blocked by Binance risk control', '币安风控挡下')),
    (r'(?i)expired', L('Expired', '过期了')),
    (r'(?i)nonce', L('Nonce conflict (the previous tx may not be on-chain yet), try again shortly', '交易序号冲突（前一笔可能还没上链），等一下再试')),
    (r'(?i)rate.?limit|too many', L('Too many requests, try again shortly', '查太频繁，等一下再试')),
    (r'(?i)time.?out|timed out', L('Timed out', '逾时')),
    (r'(?i)ECONN|socket|network|EAI_AGAIN|fetch failed', L('Connection lost', '连接中断')),
]


def zh_err(text):
    """Translate a baw_raw() text; unknown messages keep the original after a Chinese lead-in."""
    text = str(text or '').strip()
    name = text.split(' ', 1)[0]
    if name in BAW_ERR_ZH:
        return BAW_ERR_ZH[name]
    for pat, zh in BAW_MSG_ZH:
        if re.search(pat, text):
            why = re.search(r'(?i)reverted:?\s*(.{1,80})', text)
            return L(f'{zh} (contract says: {why.group(1).strip()})', f'{zh}（合约说：{why.group(1).strip()}）') if why else zh
    return L(f'Binance replied: {text}', f'币安回复：{text}') if text else L('Binance gave no reason', '币安没有说原因')


def baw_err(r):
    """What went wrong, in Chinese, for messages to the user."""
    return zh_err(baw_raw(r))


def cast(*args, timeout=60):
    code, out, err = run([CAST, *args], timeout=timeout)
    if code != 0:
        lines = (err or out).strip().splitlines()
        raise RuntimeError(lines[0][:300] if lines else 'cast failed')
    return out.strip()


def each(items, fn):
    """fn(item) for every item at the same time; results keep the input order."""
    items = list(items)
    if len(items) < 2:
        return [fn(x) for x in items]
    with ThreadPoolExecutor(max_workers=len(items)) as ex:
        return list(ex.map(fn, items))


def logged_in_address(w):
    """(BSC address baw is logged into, error text)."""
    r = baw(w, 'wallet', 'address')
    if not r.get('success'):
        return None, baw_err(r)
    rows = (r.get('data') or {}).get('addresses') or []
    addr = next((x.get('address') for x in rows if str(x.get('binanceChainId')) == CHAIN), None) \
        or next((x.get('address') for x in rows if str(x.get('address', '')).startswith('0x')), None)
    return addr, None if addr else L('No BSC address in the response', '回传里没有 BSC 地址')


def wallet_mismatch(w):
    """None when baw for wallet w is logged into the configured address, else why not (re-checked every 10 minutes)."""
    if time.time() - ADDR_OK.get(w, 0) < ADDR_TTL:
        return None
    addr, err = logged_in_address(w)
    if not addr:  # one retry so a single network blip does not block trading
        time.sleep(2)
        addr, err = logged_in_address(w)
    if not addr:
        return L(f'Cannot find the account baw is logged into ({err})', f'查不到 baw 登录的账号（{err}）')
    if addr.lower() != WALLETS[w]['addr'].lower():
        ADDR_OK.pop(w, None)
        return L(f"baw is logged into {addr}, not the configured {WALLETS[w]['addr']}", f"baw 登录的是 {addr}，不是设置的 {WALLETS[w]['addr']}")
    ADDR_OK[w] = time.time()
    return None


def native_balance(w):
    return from_wei(int(cast('balance', WALLETS[w]['addr'], '--rpc-url', RPC).split()[0]))


def eth_call_error(w, to, value_wei, data):
    """None when the call simulates fine on chain, else the revert message."""
    try:
        cast('call', to, data, '--from', WALLETS[w]['addr'], '--value', str(value_wei), '--rpc-url', RPC)
        return None
    except RuntimeError as e:
        return readable_revert(str(e))


def readable_revert(msg):
    """Name a bare custom-error selector (e.g. data "0x3f5a0b1c…") through the public signature database."""
    known_err = next((cn for key, cn in CURVE_ERRORS.items() if key.lower() in msg.lower()), None)
    if known_err:
        return known_err
    m = re.search(r'(?:custom error|data:\s*"?)\s*(0x[0-9a-fA-F]{8})', msg)
    if not m or re.search(r'reverted: [A-Za-z]', msg):
        return msg
    try:
        name = cast('4byte', m.group(1), timeout=20).splitlines()[0]
        return L(f'{name} ({msg[:120]})', f'{name}（{msg[:120]}）')
    except (RuntimeError, IndexError):
        return msg


def receipt(tx):
    try:
        out = cast('receipt', tx, 'status', '--rpc-url', RPC, timeout=150)
        ok = re.match(r'(?i)(1\b|true|success)', out.strip())  # older cast prints 1 (success), newer prints true
        return L('✅ Confirmed on-chain', '✅ 上链成功') if ok else L(f'❌ On-chain but failed ({out})', f'❌ 上链但失败（{out}）')
    except Exception as e:  # noqa: BLE001 - report anything, never crash the loop
        return L(f'⏳ No receipt yet: {e}', f'⏳ 还查不到收据：{e}')


def wallet_list(s):
    """'AB' / ['A', 'B'] -> the configured wallets among them, in order; empty or unknown -> []."""
    return [c for c in dict.fromkeys(''.join(s or '').upper()) if c in WALLETS]


def default_wallets():
    return wallet_list(load_prefs()['wallets']) or list(WALLETS)


def all_names():
    return ''.join(WALLETS)


def wallet_hint():
    names = list(WALLETS)
    return L(', ', '、').join(names) + (L(f', or combine them ({all_names()} = all)', f'，或连在一起写（{all_names()}＝全部）') if len(names) > 1 else '')


def next_wallets(cur):
    """Wallet toggle on cards: all -> each one alone -> all."""
    cycle = [list(WALLETS)] + ([[w] for w in WALLETS] if len(WALLETS) > 1 else [])
    cur = list(cur or [])
    return cycle[(cycle.index(cur) + 1) % len(cycle)] if cur in cycle else cycle[0]


def parse_wallets(rest, default=None):
    if not WALLETS:
        raise UsageError(L('No wallets yet. Go to "📊 Wallet status" and tap "➕ Add wallet" to connect your AI wallet.', '还没有钱包。到「📊 钱包状态」按「➕ 添加钱包」连接你的 AI 钱包。'))
    if not rest:
        return wallet_list(default) or list(WALLETS)
    if len(rest) > 1:
        raise UsageError(L('Too many arguments.', '参数太多了。'))
    s = rest[0].upper()
    if s in ('ALL',) + zh_word('全部'):
        return list(WALLETS)
    if s and all(c in WALLETS for c in s):
        return list(dict.fromkeys(s))
    raise UsageError(L(f'Wallet must be {wallet_hint()}, got "{rest[0]}".', f'钱包要写 {wallet_hint()}，收到「{rest[0]}」。'))


def need_addr(s, what=L('contract address', '合约地址')):
    if not ADDR_RE.match(s or ''):
        raise UsageError(L(f'Invalid {what}: {s}', f'{what}格式不对：{s}'))
    return s


def need_amount(s, cap=True):
    d = dec(s.rstrip('%'))
    if d <= 0:
        raise UsageError(L(f'Amount must be greater than 0: {s}', f'金额要大于 0：{s}'))
    if cap and d > MAX_BNB:
        raise UsageError(L(f'Exceeds the per-wallet cap of {fmt(MAX_BNB)} BNB (change MAX_BNB_PER_WALLET in the config).', f'超过单一钱包上限 {fmt(MAX_BNB)} BNB（设置档 MAX_BNB_PER_WALLET 可以改）。'))
    return d


# ---------------------------------------------------------------- preferences

PREFS_FILE = CONF_DIR / 'prefs.json'
DEFAULT_PREFS = {
    'buy': ['0.01', '0.03', '0.05'],  # quick buy amounts, BNB per wallet
    'buy_pct': ['1', '5'],  # quick buys as a share of each wallet's own BNB
    'sell': ['50', '100'],
    'wallets': '',  # empty = every configured wallet
    'slippage': SLIPPAGE,
    'min_liq': fmt(MIN_LIQ, 0),  # USD; buys into a smaller pool are refused, 0 = off
    'auto_tpsl': False,  # arm take-profit / stop-loss on every filled buy
    'tp': ['100', '50'],  # +100% -> sell 50%
    'sl': ['30', '100'],  # -30% -> sell 100%
    'auto_send': 'off',  # off / fill (transfer right after a buy) / grad (once the token graduates)
    'send_to': '',  # one of the payout addresses in wallets.json; empty = the first
    'snipe': {
        'on': False,  # watch new launches at all
        'live': False,  # False = simulate only (log what it would buy)
        'tracks': ['gang', 'flap', 'ac', 'four'],  # which launchpads
        'pct': '1',  # % of supply per wallet (Gang/Four cap 1%, Flap 2%)
        'max_bnb': '0.05',  # per wallet per launch
        'day_bnb': '0.3',  # all wallets together, per day
        'day_tokens': 5,
        'max_delay': 8,  # seconds after the block; later is too late
        'max_sold': 15,  # % of the curve already sold; more is too late
        'wallets': '',  # empty = every wallet
        'tp_x': '2',  # sell tp_sell% once the position is worth this many times what it cost (0 = off)
        'tp_sell': '50',
        'sl': '50',  # sell everything once it has lost this many % (0 = off)
    },
    'keys': {},
}
BUILTIN_KEYS = {L('Overview', '总览'): '/me', L('Status', '状态'): '/status', L('Recent trades', '最近成交'): '/orders', L('Limit orders', '挂单'): '/limits', L('TP/SL', '止盈止损'): '/tpsl',
                L('Settings', '设置'): '/set', L('Help', '说明'): '/help'}
MAX_KEYS = 12


def load_prefs():
    p = json.loads(json.dumps(DEFAULT_PREFS))
    try:
        saved = json.loads(PREFS_FILE.read_text())
        p.update({k: ({**p[k], **v} if isinstance(v, dict) else v)
                  for k, v in saved.items() if k in p and isinstance(v, type(p[k]))})
    except (OSError, ValueError, AttributeError):
        pass  # missing or corrupted file: fall back to the defaults
    return p


def save_prefs(p):
    write_json(PREFS_FILE, p)


def key_board():
    names = list(BUILTIN_KEYS) + list(load_prefs()['keys'])
    return [names[i:i + 4] for i in range(0, len(names), 4)]


# ---------------------------------------------------------------- telegram

API = f'https://api.telegram.org/bot{TOKEN}/'


def tg(method, **params):
    req = urllib.request.Request(API + method, data=json.dumps(params).encode(),
                                 headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=70) as resp:
        return json.loads(resp.read())


def say(text, buttons=None, keyboard=None, rich=False):
    """buttons: inline buttons under the message; keyboard: rows of shortcut names pinned under the input box;
    rich: text is Telegram HTML (escaped with esc(), links as <a>)."""
    if DRY:
        print('\n' + text + (L('\n[Buttons] ', '\n[按钮] ') + ' / '.join(b['text'] for row in buttons for b in row) if buttons else '')
              + (L('\n[Keys] ', '\n[快捷键] ') + ' / '.join(n for row in keyboard for n in row) if keyboard else ''))
        return
    chunks = [text[i:i + 3900] for i in range(0, len(text), 3900)] or ['']
    mid = None
    for i, chunk in enumerate(chunks):
        params = {'chat_id': OWNER, 'text': chunk, 'disable_web_page_preview': True}
        if rich:
            params['parse_mode'] = 'HTML'
        if buttons and i == len(chunks) - 1:
            params['reply_markup'] = {'inline_keyboard': buttons}
        elif keyboard and i == len(chunks) - 1:
            params['reply_markup'] = {'keyboard': [[{'text': n} for n in row] for row in keyboard],
                                      'resize_keyboard': True, 'is_persistent': True}
        try:
            res = tg('sendMessage', **params).get('result')
        except urllib.error.HTTPError:
            if not rich:
                raise
            logging.exception('HTML message rejected, sending as plain text')
            params.pop('parse_mode')
            params['text'] = html.unescape(re.sub(r'<[^>]+>', '', chunk))
            res = tg('sendMessage', **params).get('result')
        mid = res.get('message_id') if isinstance(res, dict) else None
    return mid  # id of the last chunk, so a card can be edited in place later


def ask_confirm(summary, label, action, rich=False, extra=None):
    """extra: one more row of buttons under Confirm / Cancel (e.g. save this action as a shortcut)."""
    if DRY:
        say(summary + L('\n\n(Dry run: nothing will execute)', '\n\n（试跑模式：不会运行）'), [[{'text': f'✅ {label}'}, {'text': L('❌ Cancel', '❌ 取消')}]] + ([extra] if extra else []),
            rich=rich)
        return
    now = time.time()
    nonce = secrets.token_hex(8)
    with PENDING_LOCK:
        for k in [k for k, v in PENDING.items() if v['expires'] < now]:
            del PENDING[k]
        PENDING[nonce] = {'expires': now + CONFIRM_TTL, 'action': action}
    say(summary + L(f'\n\n<i>Not sent yet. Tap Confirm within {CONFIRM_TTL}s to send.</i>', f'\n\n<i>还没送出。{CONFIRM_TTL} 秒内按确认才会送。</i>') if rich else
        summary + L(f'\n\nNot sent yet. Tap Confirm within {CONFIRM_TTL}s to send.', f'\n\n还没送出。{CONFIRM_TTL} 秒内按确认才会送。'),
        [[{'text': f'✅ {label}', 'callback_data': 'ok:' + nonce}, {'text': L('❌ Cancel', '❌ 取消'), 'callback_data': 'no:' + nonce}]]
        + ([extra] if extra else []), rich=rich)


def on_callback(cq):
    if (cq.get('from') or {}).get('id') != OWNER:
        tg('answerCallbackQuery', callback_query_id=cq['id'])
        return
    if (cq.get('data') or '').startswith('p:'):
        tg('answerCallbackQuery', callback_query_id=cq['id'])
        on_panel(cq['data'], cq.get('message') or {})
        return
    if (cq.get('data') or '').startswith('o:'):
        tg('answerCallbackQuery', callback_query_id=cq['id'])
        on_overview(cq['data'], cq.get('message') or {})
        return
    if (cq.get('data') or '').startswith('q:'):
        tg('answerCallbackQuery', callback_query_id=cq['id'])
        guarded(lambda a: on_presale(cq['data'], cq.get('message') or {}), [])
        return
    if (cq.get('data') or '').startswith('n:'):
        tg('answerCallbackQuery', callback_query_id=cq['id'])
        guarded(lambda a: on_snipe(cq['data'], cq.get('message') or {}), [])
        return
    if (cq.get('data') or '').startswith('c:'):
        tg('answerCallbackQuery', callback_query_id=cq['id'])
        guarded(lambda a: on_contract(cq['data']), [])
        return
    if (cq.get('data') or '').startswith('m:'):  # a button that just runs a command, e.g. m:/status details
        tg('answerCallbackQuery', callback_query_id=cq['id'])
        command = cq['data'][2:]
        if command.startswith('#'):
            command = CMD_STORE.get(command[1:])
        if command:
            dispatch(command)
        else:
            say(L('This button has expired (the bot restarted), please paste again.', '这颗按钮过期了（机器人重启过），请重新贴一次。'))
        return
    if (cq.get('data') or '').startswith('k:'):  # save the command behind a button as a shortcut key
        tg('answerCallbackQuery', callback_query_id=cq['id'])
        cid = cq['data'][2:]
        if cid in CMD_STORE:
            ask_input(cid, 'keyname', L('Name this action (max 16 chars, e.g. "Claim airdrop")\n'
                                      'Then tap its button below the input box to get a preview right away', '帮这个动作取个名字（最多 16 字，例如「领空投」）\n'
                                      '之后按输入框下方的这颗按钮，就会直接出预览'))
        else:
            say(L('This button has expired (the bot restarted), please do it again.', '这颗按钮过期了（机器人重启过），请重新做一次。'))
        return
    kind, _, nonce = (cq.get('data') or '').partition(':')
    with PENDING_LOCK:
        item = PENDING.pop(nonce, None)
    tg('answerCallbackQuery', callback_query_id=cq['id'])
    msg = cq.get('message') or {}
    if msg:
        tg('editMessageReplyMarkup', chat_id=msg['chat']['id'], message_id=msg['message_id'],
           reply_markup={'inline_keyboard': []})
    if not item:
        say(L('This Confirm button was already used, nothing was done.', '这颗确认钮已经用过了，什么都没做。'))
    elif kind != 'ok':
        say(L('Cancelled, nothing was sent.', '已取消，什么都没送出。'))
    elif time.time() > item['expires']:
        say(L('Timed out, nothing was sent. Tap the buy/sell button again.', '超过时间了，什么都没送出。请重新按一次买卖按钮。'))
    else:
        logging.info('confirmed %s', nonce)
        run_bg(item['action'])


CMD_STORE = {}  # id -> command, for buttons whose command is longer than Telegram's 64-byte callback limit


def store_cmd(command):
    cid = secrets.token_hex(4)
    CMD_STORE[cid] = command
    while len(CMD_STORE) > 500:
        CMD_STORE.pop(next(iter(CMD_STORE)))
    return cid


def cmd_btn(label, command):
    """An inline button that runs a command (every money-moving command still previews first)."""
    data = 'm:' + command
    if len(data.encode()) > 64:
        data = 'm:#' + store_cmd(command)
    return {'text': label, 'callback_data': data}


def run_bg(fn):
    """Run a confirmed action off the polling loop, so waiting for a fill never blocks other buttons."""
    def wrapped():
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            logging.exception('background action failed')
            say(L(f'Error: {type(e).__name__}: {str(e)[:300]}', f'出错了：{type(e).__name__}: {str(e)[:300]}'))
    threading.Thread(target=wrapped, daemon=True).start()


# ---------------------------------------------------------------- send guards

REQUESTS = {}  # preview requestId -> (to, value, data), to spot the same call confirmed twice
SENT_AT = {}  # (kind, wallet, …) -> time it was last sent
DUP_WINDOW = 5
ORDERS_LOG = CONF_DIR / 'orders.jsonl'
SEND_LOCK = threading.Lock()


def is_repeat(key):
    """True when the identical order went out less than DUP_WINDOW seconds ago (a double tap on two previews).
    Otherwise the key is held as sent; call unsend(key) when the order was refused before reaching Binance."""
    with SEND_LOCK:
        now = time.time()
        if now - SENT_AT.get(key, 0) < DUP_WINDOW:
            return True
        SENT_AT[key] = now
        return False


def unsend(key):
    SENT_AT.pop(key, None)


def tx_lock(w):
    r = baw(w, 'wallet', 'tx-lock', '--binanceChainId', CHAIN, timeout=30)
    return str((r.get('data') or {}).get('status') or '') if r.get('success') else ''


def wait_unlocked(w, wait=30):
    """None when wallet w may send now; a pending transaction usually clears within a few blocks."""
    deadline = time.time() + wait
    while tx_lock(w) == 'LOCKED':
        if time.time() >= deadline:
            return L('Wallet is locked: a transaction is still going on-chain or awaiting confirmation in the Binance app. Handle it in the app, then tap again.', '钱包锁住了：有一笔还在上链，或在等币安 App 确认。先到 App 处理，再按一次。')
        time.sleep(5)
    return None


def notify_unlock(w, wait=330):
    deadline = time.time() + wait
    while time.time() < deadline:
        time.sleep(10)
        if tx_lock(w) != 'LOCKED':
            say(L(f'🔓 <b>{w}</b> unlocked (confirmed or rejected in the app). See "👤 Overview" for the result.', f'🔓 <b>{w}</b> 解锁了（App 已确认或拒绝）。结果看「👤 总览」。'),
                [[{'text': L('👤 Overview', '👤 看总览'), 'callback_data': 'm:/me'}]], rich=True)
            return
    say(L(f'⌛ <b>{w}</b> no app confirmation for over 5 minutes, that transaction has likely expired.', f'⌛ <b>{w}</b> 等 App 确认超过 5 分钟，那笔应该失效了。'), rich=True)


def log_order(kind, w, **fields):
    try:
        with ORDERS_LOG.open('a') as f:
            f.write(json.dumps({'time': int(time.time()), 'kind': kind, 'wallet': w, **fields},
                               ensure_ascii=False, default=str) + '\n')
    except OSError:
        logging.exception('order log')


# ---------------------------------------------------------------- audit

def audit(token):
    """(risk level or None when unavailable, summary text)."""
    body = json.dumps({'binanceChainId': CHAIN, 'contractAddress': token, 'requestId': str(uuid.uuid4())}).encode()
    req = urllib.request.Request(AUDIT_URL, data=body, headers={
        'Content-Type': 'application/json', 'Accept-Encoding': 'identity',
        'User-Agent': 'binance-web3/1.4 (Skill)', 'source': 'agent'})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            d = json.loads(resp.read()).get('data') or {}
    except Exception:  # noqa: BLE001
        return None, L('⚠️ Security check unavailable (connection failed).', '⚠️ 安全检查暂时查不到（连接失败）。')
    if not (d.get('hasResult') and d.get('isSupported')):
        return None, L('⚠️ No security check data for this token.', '⚠️ 这个代币没有安全检查数据。')
    level = int(dec(d.get('riskLevel')))
    ex = d.get('extraInfo') or {}
    details = [x for item in d.get('riskItems') or [] for x in item.get('details') or []]
    honeypot = any(x.get('isHit') for x in details if 'honeypot' in (x.get('title') or '').lower())
    verdict = L('✅ Low risk', '✅ 低风险') if level <= 1 else L('⚠️ Medium risk', '⚠️ 中风险') if level < 4 else L('⛔ High risk', '⛔ 高风险')
    # the audit reports taxes as fractions (0.03 = 3%), checked against the search API's taxRate on Flap tokens
    taxes = [(n, dec(t) * 100 if t is not None else None) for n, t in ((L('Buy', '买'), ex.get('buyTax')), (L('Sell', '卖'), ex.get('sellTax')))]
    lines = [L(f"{verdict} | {'⛔ Honeypot' if honeypot else 'Not a honeypot'} | {'Open source' if ex.get('isVerified') else '⚠️ Not open source'}", f"{verdict}｜{'⛔ 貔貅' if honeypot else '非貔貅'}｜{'开源' if ex.get('isVerified') else '⚠️ 没开源'}"),
             L('⚖️ Tax ', '⚖️ 税 ') + L(' | ', '｜').join(f"{n} {fmt(t, 2) + '%' if t is not None else '?'}" for n, t in taxes)]
    for n, t in taxes:
        if t is not None and t > 10:
            lines.append(L(f'⛔ {n} tax {fmt(t, 2)}%, over 10%', f'⛔ {n}税 {fmt(t, 2)}%，超过 10%'))
        elif t is not None and t > 5:
            lines.append(L(f'⚠️ {n} tax {fmt(t, 2)}%, over 5%', f'⚠️ {n}税 {fmt(t, 2)}%，超过 5%'))
    hits = list(dict.fromkeys(risk_cn(x.get('title')) for x in details if x.get('isHit')))
    if hits:
        lines.append('⚠️ ' + L(', ', '、').join(hits))
    return level, '\n'.join(lines)


# ---------------------------------------------------------------- swaps

def wait_order(w, oid, label):
    deadline = time.time() + 90
    while time.time() < deadline:
        r = baw(w, 'market-order', 'list', '--orderId', str(oid))
        rows = ((r.get('data') or {}).get('list') or []) if r.get('success') else []
        if rows:
            o = rows[0]
            if o.get('status') == 'FINISHED':
                return (L(f"✅ <b>{w} {label} filled</b>  {esc(fmt(o.get('fromTokenQty'), 6))} {esc(o.get('fromTokenName'))}"
                        f" → {esc(o.get('toTokenName'))}  {tx_link(o.get('txHash'))}", f"✅ <b>{w} {label}成交</b>　{esc(fmt(o.get('fromTokenQty'), 6))} {esc(o.get('fromTokenName'))}"
                        f" → {esc(o.get('toTokenName'))}　{tx_link(o.get('txHash'))}"))
            if o.get('status') == 'FAILED':
                return L(f"❌ <b>{w} {label} failed</b>  {tx_link(o.get('txHash')) or 'not on-chain'}", f"❌ <b>{w} {label}失败</b>　{tx_link(o.get('txHash')) or '没有上链'}")
        time.sleep(4)
    return L(f'⏳ <b>{w} {label} still pending</b>  check "Recent trades" later', f'⏳ <b>{w} {label}还在处理</b>　晚点按「最近成交」再看')


def do_swap(ws, from_token, to_token, qty, label, on_fill=None):
    """Market orders for all wallets at the same time; on_fill(w) may add a line after a confirmed fill."""
    def one(w):
        dkey = ('swap', w, from_token.lower(), to_token.lower(), str(qty[w]))
        if is_repeat(dkey):
            return L(f'⚠️ <b>{w}</b> the same order was already sent within {DUP_WINDOW}s, not sending again (duplicate guard).', f'⚠️ <b>{w}</b> 同一笔 {DUP_WINDOW} 秒内已经送过，这次不送（防止重复）。')
        locked = wait_unlocked(w)
        if locked:
            unsend(dkey)
            return L(f'🔒 <b>{w} {label} not sent</b>  {esc(locked)}', f'🔒 <b>{w} {label}没送出</b>　{esc(locked)}')
        r = baw(w, 'market-order', 'swap', '--fromTokenQty', qty[w], '--fromToken', from_token,
                '--toToken', to_token, '--binanceChainId', CHAIN, '--slippage', slippage())
        oid = (r.get('data') or {}).get('orderId') if r.get('success') else None
        logging.info('swap %s %s -> %s qty %s: %s', w, from_token, to_token, qty[w], oid or baw_raw(r))
        log_order('swap', w, from_token=from_token, to_token=to_token, qty=str(qty[w]), ok=bool(oid), order=oid,
                  error=None if oid else baw_raw(r))
        if not oid:
            err = baw_err(r)
            unknown = re.search(UNKNOWN_RE, baw_raw(r))
            if not unknown:
                unsend(dkey)
            if unknown:
                return (L(f'⚠️ <b>{w} {label} result unknown</b>  no reply after sending ({esc(err)})\n'
                        'Do not tap again yet: wait a minute, then check Overview or "Recent trades" for it', f'⚠️ <b>{w} {label}结果未知</b>　送出后没收到回复（{esc(err)}）\n'
                        '先别重按：等一分钟看总览或「最近成交」有没有这笔'))
            return L(f'❌ <b>{w} {label} not sent</b>  {esc(err)}', f'❌ <b>{w} {label}没送出</b>　{esc(err)}')
        result = wait_order(w, oid, label)
        if on_fill and result.startswith('✅'):
            extra = on_fill(w)
            result += f'\n{esc(extra)}' if extra else ''
        return result
    say(L(f"⏳ {', '.join(ws)} {label} sent, waiting for fill…", f"⏳ {'、'.join(ws)} {label}已送出，等成交…"))
    lines = each(ws, one)
    for line in lines:
        say(line, [[{'text': L('👤 Overview', '👤 看总览'), 'callback_data': 'm:/me'}]], rich=True)
    return lines


def slippage():
    return load_prefs()['slippage']


def slip_text(q):
    s = q.get('slippage')
    return f'{fmt(dec(s) * 100, 2)}%' if s is not None else slippage()


def binance_data(url):
    req = urllib.request.Request(url, headers={'Accept-Encoding': 'identity', 'User-Agent': 'binance-web3/2.0 (Skill)'})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read()).get('data')
    except Exception:  # noqa: BLE001
        return None


def token_info(token):
    """Market data from the Binance token API (price, marketCap, liquidity, holders…); {} when unavailable."""
    return binance_data(f'{DYNAMIC_URL}?chainId={CHAIN}&contractAddress={token}') or {}


def token_profile(token):
    """Facts that rarely change: creator, creation time, launch platform, taxes (%), social links; {} when unavailable."""
    meta, found = each([f'{META_URL}?chainId={CHAIN}&contractAddress={token}',
                        f'{SEARCH_URL}?keyword={token}&chainIds={CHAIN}'], binance_data)
    meta = meta if isinstance(meta, dict) else {}
    found = next((t for t in found if (t.get('contractAddress') or '').lower() == token.lower()), {}) \
        if isinstance(found, list) else {}
    platforms = (found.get('tagsInfo') or {}).get('Launch Platform') or []
    return {'creator': meta.get('creatorAddress'), 'created': meta.get('createTime') or found.get('createTime'),
            'platform': L(', ', '、').join(t['tagName'] for t in platforms if t.get('tagName')),
            'links': meta.get('links') or found.get('links') or [],
            'buy_tax': found.get('taxRateBuy'), 'sell_tax': found.get('taxRateSell')}


def esc(x):
    return html.escape(str(x), quote=False)


def link(url, label):
    return f'<a href="{html.escape(url, quote=True)}">{esc(label)}</a>'


def short(addr):
    return f'{addr[:6]}…{addr[-4:]}' if addr and len(addr) > 12 else addr or '?'


def cn_num(v):
    """Token amounts the way people read them: 693,000 -> 693K (English) or 69.3 wan (Chinese)."""
    v = dec(v)
    if not ZH:
        for unit, size in (('B', 10 ** 9), ('M', 10 ** 6), ('K', 10 ** 3)):
            if v >= size:
                return f"{f'{v / size:,.2f}'.rstrip('0').rstrip('.')}{unit}"
        return fmt(v, 4)
    if v >= 10 ** 8:
        return L(f'{fmt(v / 10 ** 8, 2)} ×100M', f'{fmt(v / 10 ** 8, 2)} 亿')
    if v >= 10 ** 4:
        return L(f"{f'{v / 10 ** 4:,.1f}'.rstrip('0').rstrip('.')} ×10K", f"{f'{v / 10 ** 4:,.1f}'.rstrip('0').rstrip('.')} 万")
    return fmt(v, 4)


def tx_link(tx, label=L('View tx', '查看交易')):
    return link(f'https://bscscan.com/tx/{tx}', label) if tx else ''


def nice_time(s):
    """'2026-11-03T19:50:28+08:00' or epoch ms → '11/03 19:50' (local time)."""
    s = str(s or '')
    if s.isdigit():
        return local_time(int(s) / 1000 if len(s) > 11 else int(s))
    m = re.search(r'\d{4}-(\d\d)-(\d\d)[T ](\d\d:\d\d)', s)
    return f'{m.group(1)}/{m.group(2)} {m.group(3)}' if m else s or '?'


VERB_CN = {'buy': L('Buy', '买入'), 'sell': L('Sell', '卖出'), 'mint': L('Mint', '铸造'), 'claim': L('Claim', '领取'), 'refund': L('Refund', '退款'), 'withdraw': L('Withdraw', '提领'),
           'redeem': L('Redeem', '赎回'), 'stake': L('Stake', '质押'), 'unstake': L('Unstake', '解除质押'), 'harvest': L('Harvest', '收成'), 'deposit': L('Deposit', '存入'),
           'join': L('Join', '加入'), 'enter': L('Enter', '进场'), 'register': L('Register', '登记'), 'fill': L('Fill', '占位'), 'reserve': L('Reserve', '预约'), 'commit': L('Commit', '认购'),
           'contribute': L('Commit', '认购'), 'participate': L('Participate', '参加'), 'exit': L('Exit', '退出'), 'collect': L('Claim', '领取'), 'lock': L('Lock', '锁仓'),
           'unlock': L('Unlock', '解锁'), 'bid': L('Bid', '出价'), 'reveal': L('Reveal', '揭晓'), 'open': L('Open', '打开'), 'settle': L('Settle', '结算'), 'approve': L('Approve', '授权'),
           'transfer': L('Transfer', '转账'), 'purchase': L('Purchase', '购买'), 'swap': L('Swap', '兑换'), 'cancel': L('Cancel', '取消'), 'add': L('Join', '加入'), 'remove': L('Remove', '移除')}
READ_CN = {'buylimit': L('You can still buy', '你还能买'), 'graduated': L('Graduated?', '毕业了吗'), 'maxperagent': L('Max per wallet', '每个钱包上限'), 'tokenssold': L('Tokens sold', '已卖出'),
           'boughtby': L('You bought', '你已买'), 'maxperwallet': L('Max per wallet', '每个钱包上限'), 'totalsupply': L('Total supply', '总量'), 'price': L('Price', '价格'),
           'starttime': L('Start time', '开始时间'), 'endtime': L('End time', '结束时间'), 'deadline': L('Deadline', '截止时间'), 'paused': L('Paused', '已暂停')}


def fn_label(name):
    """'refundTo' → 'Refund (refundTo)'; unknown names stay as they are."""
    verb = next((cn for en, cn in VERB_CN.items() if re.match(f'(?i:{en})(?![a-z])', name)), None)
    return L(f'{verb} ({name})', f'{verb}（{name}）') if verb and ZH else name


def param_hint(t, n):
    """What to type for one parameter, in plain words."""
    name = L(f' "{n}"', f'「{n}」') if n else ''
    if t == 'address':
        return L(f'Address{name}: type "me" to use each wallet\'s own address, or paste a 0x address', f'地址{name}：打「我」＝用各自钱包的地址，或贴一个 0x 地址')
    if t.startswith(('uint', 'int')):
        return L(f'Number{name}: just type a number; for a BNB amount add bnb, e.g. 0.01bnb', f'数字{name}：直接打数字；如果是 BNB 金额，在后面加 bnb，例如 0.01bnb')
    if t == 'bool':
        return L(f'Yes/no{name}: type "yes" or "no"', f'是或否{name}：打「是」或「否」')
    if t == 'string':
        return L(f'Text{name}: type it directly (no spaces)', f'文本{name}：直接打（不能有空白）')
    return L(f'Data{name} ({t}): paste content starting with 0x', f'数据{name}（{t}）：贴 0x 开头的内容')


SAMPLE_ARG = {'address': L('me', '我'), 'bool': L('yes', '是'), 'string': 'abc'}


def action_prompt(label, types, names, pays):
    """The question asked before calling a function: each parameter in plain words plus a typed example."""
    lines = [f'⚡ {label}']
    if types:
        lines.append(L('Fill in order, separated by spaces:', '请照顺序填，用空白隔开：'))
        lines += [f'{i}. {param_hint(t, n)}' for i, (t, n) in enumerate(zip(types, names), 1)]
    example = [SAMPLE_ARG.get(t, '1' if t.startswith(('uint', 'int')) else '0x…') for t in types]
    if pays:
        lines.append((L('Finally, ', '最后') if types else L('This one costs BNB: ', '这个要付 BNB，')) + L('type "pay 0.01" to pay 0.01 BNB (leave it out if no payment)', '打「付 0.01」＝付 0.01 BNB（不用付就不写）'))
        example += [L('pay', '付'), '0.01']
    if not types and not pays:
        lines.append(L('No parameters needed, just type -', '这个不用填参数，打 - 就好'))
        example = ['-']
    lines.append(L(f"e.g. {' '.join(example)}", f"例如：{' '.join(example)}"))
    return '\n'.join(lines)


RISK_CN = [('honeypot', L("Honeypot (can't sell)", '貔貅（买了卖不掉）')), ('mint', L('Mintable', '可以增发')), ('blacklist', L('Blacklist', '有黑名单')), ('whitelist', L('Whitelist', '有白名单')),
           ('suspension', L('Trading can be paused', '可以暂停交易')), ('modifiable tax', L('Tax can be changed', '可以改税')), ('upgrade', L('Upgradeable contract', '合约可以升级')),
           ('self-destruct', L('Can self-destruct', '可以自毁')), ('renounce', L('Ownership not renounced', '没有放弃权限')), ('rug', L('Possible rug pull', '可能卷款')), ('hidden owner', L('Hidden owner', '有隐藏拥有者')),
           ('liquidity', L('Liquidity risk', '流动性风险')), ('wash', L('Wash trading', '刷量')), ('airdrop', L('Abnormal airdrop', '异常空投')), ('balance manipulation', L('Balances can be modified', '可以改余额')),
           ('freez', L('Accounts can be frozen', '可以冻结账户')), ('cooling', L('Trade cooldown', '有交易冷却')), ('anti-whale', L('Anti-whale limit', '限制大额交易')), ('sale restriction', L('Sell restriction', '限制卖出')),
           ('gas abuse', L('Gas abuse', '恶意手续费')), ('fake', L('Fake token', '假冒代币')), ('scam', L('Scam', '诈骗')), ('spam', L('Spam token', '垃圾币')),
           ('malicious creator', L('Malicious creator', '发币者有前科')), ('hacking', L('Hacking risk', '黑客风险')), ('verified', L('Contract not verified', '合约没开源')), ('tax', L('Abnormal tax', '税率异常'))]


def risk_cn(title):
    low = (title or '').lower()
    return next((cn for en, cn in RISK_CN if en in low), title)


def local_time(ts):
    return time.strftime('%m/%d %H:%M', time.gmtime(ts + TZ_HOURS * 3600))


def ago(ms):
    s = time.time() - int(dec(ms)) / 1000
    for unit, size in ((L('yr', '年'), 365 * 86400), (L('d', '天'), 86400), (L('h', '小时'), 3600), (L('min', '分钟'), 60)):
        if s >= size:
            return L(f'{int(s // size)} {unit} ago', f'{int(s // size)} {unit}前')
    return L('just now', '刚刚')


def tiny_price(p):
    """0.00001113 → 0.0(4)1113 (count of zeros in brackets, the way the owner reads it)."""
    p = dec(p)
    if p <= 0 or p >= Decimal('0.0001'):
        return price_text(p)
    digits = price_text(p).split('.')[1].lstrip('0')
    return f'0.0({-p.adjusted() - 1}){digits}'


def change(x):
    v = dec(x)
    return f"{'+' if v >= 0 else ''}{fmt(v, 2)}%"


def price_text(p):
    """Four significant digits without exponents: 0.00002107, 2.109, 1234.56."""
    p = dec(p)
    if p <= 0:
        return '?'
    return fmt(p, max(2, 3 - p.adjusted()))


def usd(x):
    if x in (None, ''):
        return '?'
    v = dec(x)
    for unit, size in (('B', 10 ** 9), ('M', 10 ** 6), ('K', 10 ** 3)):
        if v >= size:
            return f'${fmt(v / size, 2)}{unit}'
    return f'${fmt(v, 2)}' if v >= 1 else f'${price_text(v)}'


def known(x):
    return x not in (None, '')


LIQ_OK = {}  # token -> until when the owner chose to buy it anyway despite the pool floor
LIQ_OK_TTL = 1800


def liquidity_block(info, token=None):
    """Why a buy is refused because the pool is too small, else None. Unknown liquidity is allowed with a warning."""
    floor = dec(load_prefs()['min_liq'])
    liq = info.get('liquidity')
    if token and LIQ_OK.get(token.lower(), 0) > time.time():
        return None
    if floor > 0 and known(liq) and dec(liq) < floor:
        return L(f"Pool is only {usd(liq)}, below the {usd(floor)} minimum, so the bot won't buy (change it in ⚙️ Settings).", f'池子只有 {usd(liq)}，低于门槛 {usd(floor)}，机器人不提供买入（⚙️ 设置可以调整门槛）。')
    return None


def liq_line(info):
    liq = info.get('liquidity')
    return L(f'💧 Pool {usd(liq)}', f'💧 池子 {usd(liq)}') if known(liq) else L('💧 Pool unknown (maybe still on the curve)', '💧 池子查不到（可能还在内盘）')


def buy_size(arg):
    """('pct', share of each wallet's BNB) for '5%', else ('bnb', fixed amount)."""
    if str(arg).endswith('%'):
        share = need_amount(arg, cap=False)
        if share > 100:
            raise UsageError(L('Percentage cannot exceed 100.', '百分比不能超过 100。'))
        return 'pct', share
    return 'bnb', need_amount(arg)


def buy_amount(w, mode, size):
    """(BNB wallet w spends, note) or (None, why not)."""
    bad = wallet_mismatch(w)
    if bad:
        return None, bad
    bal = native_balance(w)
    amt, note = size, ''
    if mode == 'pct':
        amt = (max(bal - GAS_RESERVE, Decimal(0)) * size / 100).quantize(Decimal('0.000001'), rounding=ROUND_DOWN)
        if amt > MAX_BNB:
            amt, note = MAX_BNB, L(f' (over the per-wallet cap, using {fmt(MAX_BNB)})', f'（超过单一钱包上限，改用 {fmt(MAX_BNB)}）')
        if amt <= 0:
            return None, L(f'Too little BNB, {fmt(size, 2)}% is below a buyable amount', f'BNB 太少，{fmt(size, 2)}% 不到能买的金额')
    if bal < amt + GAS_RESERVE:
        return None, L(f'Not enough BNB (have {fmt(bal, 4)}, need {fmt(amt)} plus gas)', f'BNB 不够（有 {fmt(bal, 4)}，要 {fmt(amt)} 再加手续费）')
    return amt, note


def cmd_buy(args):
    if len(args) == 1:
        show_panel(need_addr(args[0], L('token address', '代币地址')))
        return
    if len(args) < 2:
        raise UsageError(L('Usage: /buy token_address BNB_or_% [A|B|AB], e.g. /buy 0x… 0.01 or /buy 0x… 5% (address only shows the trade card)', '用法：/buy 代币地址 BNB或百分比 [A|B|AB]，例如 /buy 0x… 0.01 或 /buy 0x… 5%（只给地址会出交易卡片）'))
    token = need_addr(args[0], L('token address', '代币地址'))
    mode, size = buy_size(args[1])
    prefs = load_prefs()
    ws = parse_wallets(args[2:], prefs['wallets'])
    trusted = token.lower() in TRUSTED
    if trusted:
        (level, atext), info, cv = (0, L('Common token, security check skipped.', '目标是常用代币，略过安全检查。')), {}, None
    else:
        (level, atext), info, cv = each([audit, token_info, detect_curve], lambda f: f(token))
    if level is not None and level >= BLOCK_RISK:
        say(L(f'⛔ <b>Cannot buy</b>  risk level {level} (no buys at {BLOCK_RISK}+)\n<code>{token}</code>\n{esc(atext)}', f'⛔ <b>不能买</b>　风险等级 {level}（{BLOCK_RISK} 以上不买）\n<code>{token}</code>\n{esc(atext)}'), rich=True)
        return
    if cv and cv['state'] == 'wait':
        say(L(f"⏳ {esc(curve_stage(cv))}{'' if cv.get('fair') else ', wait a few minutes before buying'}.\n<code>{token}</code>", f"⏳ {esc(curve_stage(cv))}{'' if cv.get('fair') else '，等几分钟再买'}。\n<code>{token}</code>"), rich=True)
        return
    on_curve = bool(cv and cv['state'] == 'curve')
    block = None if trusted or on_curve else liquidity_block(info, token)
    if block:
        say(L(f'⛔ <b>Cannot buy</b>  {esc(block)}\n<code>{token}</code>', f'⛔ <b>不能买</b>　{esc(block)}\n<code>{token}</code>'), rich=True)
        return
    head = (L(f'{fmt(size)} BNB per wallet', f'每个钱包用 {fmt(size)} BNB') if mode == 'bnb'
            else L(f'Each wallet uses {fmt(size, 2)}% of its own BNB ({fmt(GAS_RESERVE)} kept for gas)', f'每个钱包用自己 BNB 的 {fmt(size, 2)}%（先留 {fmt(GAS_RESERVE)} 当手续费）'))
    amounts = each(ws, lambda w: buy_amount(w, mode, size))
    for w, (amt, why) in zip(ws, amounts):
        if not amt:
            say(L(f'⚠️ <b>{w}</b>  not buying: {esc(why)}', f'⚠️ <b>{w}</b>　不买：{esc(why)}'), rich=True)
    plan = {w: amt for w, (amt, _) in zip(ws, amounts) if amt}
    if not plan:
        return
    notes = ['🔔 ' + esc(atext)] + ([L('⚠️ No security check result; confirming means you accept this and still want to buy', '⚠️ 没有安全检查结果，按确认代表你知道这点还是要买')] if level is None else [])
    if on_curve:
        lines = [head, L(f"📍 {esc(curve_stage(cv))} | buying directly from the curve contract | slippage {fmt(curve_slip() * 100, 0)}%", f"📍 {esc(curve_stage(cv))}｜直接跟内盘合约买｜滑点 {fmt(curve_slip() * 100, 0)}%")] + notes
        curve_buy(token, cv, {w: to_wei(a) for w, a in plan.items()}, lines, buy_hooks(token, prefs, lines))
        return

    def prep(w):
        q = baw(w, 'market-order', 'quote', '--fromTokenQty', fmt(plan[w]), '--fromToken', NATIVE,
                '--toToken', token, '--binanceChainId', CHAIN, '--slippage', prefs['slippage'])
        if not q.get('success'):
            return None, L(f'⚠️ <b>{w}</b>  not buying: quote failed ({esc(baw_err(q))})', f'⚠️ <b>{w}</b>　不买：报价失败（{esc(baw_err(q))}）'), None
        d = q['data']
        return plan[w], (L(f"<b>{w}</b>  {fmt(plan[w])} BNB{amounts[ws.index(w)][1]} → ~"
                         f"<b>{cn_num(d.get('toCoinAmount'))}</b> {esc(d.get('toCoinSymbol'))}", f"<b>{w}</b>　{fmt(plan[w])} BNB{amounts[ws.index(w)][1]} → 约 "
                         f"<b>{cn_num(d.get('toCoinAmount'))}</b> {esc(d.get('toCoinSymbol'))}")), d.get('toCoinSymbol')

    results = each(list(plan), prep)
    ready = {w: amt for w, (amt, _, _) in zip(plan, results) if amt}
    if not ready and not trusted and pancake_out(10 ** 15, [WBNB] + ([cv['quote']] if cv and cv['quote'] else [])
                                                + [token]):
        lines = [head, L('📍 No Binance quote, going direct to PancakeSwap (common for just-graduated tokens)', '📍 币安报不出价，改直连 PancakeSwap（刚毕业的币常这样）')] + notes
        curve_buy(token, cv or DEX_CV, {w: to_wei(a) for w, a in plan.items()}, lines, buy_hooks(token, prefs, lines))
        return
    sym = next((s for _, _, s in results if s), None) or short(token)
    lines = [L(f'🟢 <b>Buy preview | {esc(sym)}</b>', f'🟢 <b>买入预览｜{esc(sym)}</b>'), head, ''] + [t for _, t, _ in results]
    if not ready:
        say('\n'.join(lines + ['', L('No wallet can place this order.', '没有可以下单的钱包。')]), rich=True)
        return
    lines += [''] + notes
    if not trusted and not known(info.get('liquidity')):
        lines.append(L('⚠️ Pool size unknown; confirming means you accept this and still want to buy', '⚠️ 查不到池子大小，按确认代表你知道这点还是要买'))
    on_fill = buy_hooks(token, prefs, lines)
    slip = L('auto', '自动') if prefs['slippage'] == 'auto' else prefs['slippage'] + '%'
    lines.append(L(f'Market buy | slippage {slip} | anti-MEV', f'市价买｜滑点 {slip}｜防夹单'))
    ask_confirm('\n'.join(lines), L(f"Confirm buy ({', '.join(ready)})", f"确认买入（{'、'.join(ready)}）"),
                lambda: do_swap(list(ready), NATIVE, token, {w: fmt(a) for w, a in ready.items()}, L('Buy', '买入'), on_fill),
                rich=True)


def buy_hooks(token, prefs, lines):
    """What runs after each filled buy (auto take-profit / stop-loss, auto transfer); adds a line per hook to lines."""
    hooks = []
    if prefs['auto_tpsl']:
        lines.append(L(f"🎯 Auto TP/SL after fill ({esc(tpsl_text(prefs))}), sells at target without asking", f"🎯 成交后自动挂止盈止损（{esc(tpsl_text(prefs))}），到价直接卖、不再问你"))
        hooks.append(lambda w: arm_auto(w, token, prefs))
    to = payout_addr(prefs)
    if prefs['auto_send'] != 'off' and to:
        lines.append(L(f"📤 {SEND_WHEN[prefs['auto_send']]} → {esc(payout_name(to))}", f"📤 {SEND_WHEN[prefs['auto_send']]}到 {esc(payout_name(to))}"))
        if prefs['auto_send'] == 'fill' and prefs['auto_tpsl']:
            lines.append(L('⚠️ Sweeping on fill leaves nothing for TP/SL to sell', '⚠️ 买到就转走，止盈止损会卖不到'))
        hooks.append(lambda w: after_buy_send(w, token, prefs))
    if not hooks:
        return None
    return lambda w: '\n'.join(x for x in (h(w) for h in hooks) if x)


def share_of(balance, pct):
    whole = f'{dec(balance):f}'  # baw may hand a JSON number like 5e-05
    if pct == 100:
        return whole
    frac = whole.split('.')[1] if '.' in whole else ''
    q = (dec(whole) * pct / 100).quantize(Decimal(1).scaleb(-len(frac)), rounding=ROUND_DOWN)
    return f'{q:f}'


def cmd_sell(args):
    if len(args) == 1:
        show_panel(need_addr(args[0], L('token address', '代币地址')))
        return
    if len(args) < 2:
        raise UsageError(L('Usage: /sell TOKEN PERCENT [A|B|AB], e.g. /sell 0x... 50 A', '用法：/sell 代币地址 百分比 [A|B|AB]，例如 /sell 0x... 50 A'))
    token = need_addr(args[0], L('token address', '代币地址'))
    pct = need_amount(args[1], cap=False)
    if pct > 100:
        raise UsageError(L('Percentage cannot exceed 100.', '百分比不能超过 100。'))
    ws = parse_wallets(args[2:], load_prefs()['wallets'])
    cv = None if token.lower() in TRUSTED else detect_curve(token)
    if cv and cv['state'] == 'wait':
        say(L(f"⏳ {esc(curve_stage(cv))}{'' if cv.get('fair') else ', wait a few minutes before selling'}.\n<code>{token}</code>", f"⏳ {esc(curve_stage(cv))}{'' if cv.get('fair') else '，等几分钟再卖'}。\n<code>{token}</code>"), rich=True)
        return
    if cv and (cv['state'] == 'curve' or cv['kind'] == 'ac'):  # Agent Curve shares need a claim even after graduation
        curve_sell(token, cv, ws, pct)
        return
    info, results = each([lambda: {} if token.lower() in TRUSTED else token_info(token),
                          lambda: each(ws, lambda w: sell_prep(w, token, pct, quote=True))], lambda f: f())
    if not any(amount for amount, _ in results) and any(L('quote failed', '报价失败') in t for _, t in results) \
            and pancake_out(10 ** 18, [token] + ([cv['quote']] if cv and cv['quote'] else []) + [WBNB]):
        say(L('📍 No Binance quote, going direct to PancakeSwap (common for just-graduated tokens)', '📍 币安报不出价，改直连 PancakeSwap（刚毕业的币常这样）'))
        curve_sell(token, cv or DEX_CV, ws, pct)
        return
    sym = next((t.split()[2] for amount, t in results if amount), short(token))
    lines = [L(f'🔴 <b>Sell preview | {esc(sym)}</b>', f'🔴 <b>卖出预览｜{esc(sym)}</b>'), L(f'Sell {fmt(pct, 2)}% of position for BNB', f'卖掉持仓的 {fmt(pct, 2)}%，换成 BNB'), '']
    lines += [('⚠️ ' if not amount else '') + L(f'<b>{t[0]}</b>  {esc(t[2:])}', f'<b>{t[0]}</b>　{esc(t[2:])}') for amount, t in results]
    qty = {w: amount for w, (amount, _) in zip(ws, results) if amount}
    if not qty:
        say('\n'.join(lines + ['', L('No wallet can sell.', '没有可以卖的钱包。')]), rich=True)
        return
    floor = dec(load_prefs()['min_liq'])
    if known(info.get('liquidity')) and dec(info['liquidity']) < floor:
        lines.append(L(f"\n⚠️ Pool is only {usd(info['liquidity'])}, sell slippage may be high", f"\n⚠️ 池子只有 {usd(info['liquidity'])}，卖出滑点可能很大"))
    slip = L('auto', '自动') if slippage() == 'auto' else slippage() + '%'
    lines.append(L(f'\nMarket sell | slippage {slip} | anti-MEV', f'\n市价卖｜滑点 {slip}｜防夹单'))
    ready = list(qty)
    ask_confirm('\n'.join(lines), L(f"Confirm sell ({', '.join(ready)})", f"确认卖出（{'、'.join(ready)}）"),
                lambda: do_swap(ready, token, NATIVE, qty, L('Sell', '卖出')), rich=True)


def holding(w, token):
    r = baw(w, 'wallet', 'balance', '--binanceChainId', CHAIN)
    if not r.get('success'):
        return None
    return next((t for t in r.get('data') or [] if (t.get('address') or '').lower() == token.lower()), None)


def sell_prep(w, token, pct, quote=False):
    """(token amount to sell or None, one preview line) for selling pct% of wallet w's holding."""
    # every line starts with the wallet letter and a space; cmd_sell bolds that letter
    bad = wallet_mismatch(w)
    if bad:
        return None, L(f'{w} skip: {bad}', f'{w} 不卖：{bad}')
    hold = holding(w, token)
    if not hold or dec(hold.get('balance')) <= 0:
        return None, L(f'{w} skip: not holding this token', f'{w} 不卖：没有这个代币')
    if native_balance(w) < Decimal('0.0005'):
        return None, L(f'{w} skip: not enough BNB for gas', f'{w} 不卖：BNB 不够付手续费')
    amount = share_of(hold['balance'], pct)
    text = L(f"{w} sell {hold.get('symbol')} {cn_num(amount)} (~${fmt(dec(hold.get('value')) * pct / 100, 2)})", f"{w} 卖 {hold.get('symbol')} {cn_num(amount)} 颗（约 ${fmt(dec(hold.get('value')) * pct / 100, 2)}）")
    if not quote:
        return amount, text
    q = baw(w, 'market-order', 'quote', '--fromTokenQty', amount, '--fromToken', token,
            '--toToken', NATIVE, '--binanceChainId', CHAIN, '--slippage', slippage())
    if not q.get('success'):
        return None, L(f'{w} skip: quote failed ({baw_err(q)})', f'{w} 不卖：报价失败（{baw_err(q)}）')
    d = q['data']
    return amount, L(f"{text} → ~{fmt(d.get('toCoinAmount'), 6)} BNB", f"{text} → 约 {fmt(d.get('toCoinAmount'), 6)} BNB")


# ---------------------------------------------------------------- quick panel and shortcuts

PANELS = {}  # card id -> {'token', 'wallets', 'can_buy', 'expires', 'msg': (chat, message id), 'data', 'names'}
AWAIT = {}  # typed input the owner was asked for: {'kind', 'pid', 'expires'}
PANEL_TTL = 6 * 3600
AWAIT_TTL = 120
SET_PROMPTS = {
    'buy': L('Enter quick-buy amounts (BNB per wallet, space-separated, 1-6 values), e.g. 0.01 0.03 0.05', '请输入买入快捷金额（每个钱包的 BNB，用空白隔开，1～6 个），例如 0.01 0.03 0.05'),
    'buy_pct': L("Enter quick % buys (% of each wallet's own BNB, 1-5 values), e.g. 1 5", '请输入买入比例快捷（各钱包自己 BNB 的 %，1～5 个），例如 1 5'),
    'sell': L('Enter quick-sell percentages (1-5 values), e.g. 50 100', '请输入卖出比例快捷（%，1～5 个），例如 50 100'),
    'slippage': L('Enter slippage: type "auto", or a percentage 0-50, e.g. 5', '请输入滑点：打「自动」，或 0～50 的百分比，例如 5'),
    'min_liq': L('Enter the pool minimum (USD), e.g. 20000 or 20k; 0 = off', '请输入池子门槛（美元），例如 20000 或 20k；输入 0＝关闭'),
    'tp': L('Enter auto TP as "rise% sell%", e.g. 100 50 = sell half at +100%', '请输入自动止盈条件「涨幅% 卖出%」，例如 100 50＝涨 100% 时卖一半'),
    'sl': L('Enter auto SL as "drop% sell%", e.g. 30 100 = sell all at -30%', '请输入自动止损条件「跌幅% 卖出%」，例如 30 100＝跌 30% 时全卖'),
}
INPUT_PROMPTS = {
    'X': ('buy', L('How much should each wallet buy? A BNB amount (e.g. 0.02) or a % of its own BNB (e.g. 3%).', '请输入每个钱包要买多少：BNB 数量（例如 0.02）或自己 BNB 的百分比（例如 3%）。')),
    'S': ('sell', L('What % to sell? e.g. 30', '请输入要卖掉几 %，例如 30')),
    'P': ('tp', L('Enter "rise% sell%", e.g. 100 50 = sell half at 100% above the current price', '请输入「涨幅% 卖出%」，例如 100 50＝比现价涨 100% 时卖一半')),
    'Q': ('sl', L('Enter "drop% sell%", e.g. 30 100 = sell all at 30% below the current price', '请输入「跌幅% 卖出%」，例如 30 100＝比现价跌 30% 时全卖')),
    'y': ('lbuy', L('Enter "price BNB", e.g. 0.00001 0.02 = each wallet buys 0.02 BNB when price drops to $0.00001', '请输入「价格 BNB数量」，例如 0.00001 0.02＝价格跌到 $0.00001 时每个钱包用 0.02 BNB 买')),
    'z': ('lsell', L('Enter "price sell%", e.g. 0.0001 50 = sell half when price rises to $0.0001', '请输入「价格 卖出%」，例如 0.0001 50＝价格涨到 $0.0001 时卖一半')),
}


def btn(text, pid, op, arg=''):
    return {'text': text, 'callback_data': f'p:{pid}:{op}:{arg}'}


def rows_of(items, n=3):
    return [items[i:i + n] for i in range(0, len(items), n)]


def safe(fn):
    try:
        return fn()
    except Exception:  # noqa: BLE001 - one missing piece must not break the whole card
        logging.exception('card data')
        return None


def token_names(token):
    def call(sig):
        try:
            return cast('call', token, sig, '--rpc-url', RPC, timeout=20).strip('"')
        except RuntimeError:
            return ''
    sym, name = each(['symbol()(string)', 'name()(string)'], call)
    return sym or '?', name


def limit_orders(w, token):
    """Working limit orders of wallet w that buy or sell this token."""
    out = []
    for flag in ('--fromToken', '--toToken'):
        r = baw(w, 'limit-order', 'list', '--status', 'WORKING', flag, token, '--binanceChainId', CHAIN)
        out += ((r.get('data') or {}).get('list') or []) if r.get('success') else []
    return out


def new_panel(token):
    now = time.time()
    for k in [k for k, v in PANELS.items() if v['expires'] < now]:
        del PANELS[k]
    pid = secrets.token_hex(3)
    PANELS[pid] = {'token': token, 'wallets': default_wallets(), 'can_buy': True,
                   'expires': now + PANEL_TTL, 'data': {}}
    return pid


def card_data(pid):
    """Fetch everything the card shows, all at the same time."""
    panel = PANELS[pid]
    token = panel['token']
    trusted = token.lower() in TRUSTED
    jobs = {'audit': lambda: (0, L('Common token, security check skipped.', '常用代币，略过安全检查。')) if trusted else audit(token),
            'info': lambda: token_info(token),
            'names': lambda: panel.get('names') or token_names(token),
            'profile': lambda: panel.get('profile') or token_profile(token),
            'curve': lambda: None if trusted else detect_curve(token)}
    for w in WALLETS:
        jobs[f'hold{w}'] = lambda w=w: holding(w, token)
        jobs[f'bnb{w}'] = lambda w=w: native_balance(w)
        jobs[f'lim{w}'] = lambda w=w: limit_orders(w, token)
        jobs[f'wbnb{w}'] = lambda w=w: word(WBNB, 'balanceOf(address)', WALLETS[w]['addr'])  # left from a Gang buy
    d = dict(zip(jobs, each(list(jobs), lambda k: safe(jobs[k]))))
    panel['wbnb'] = {w: d.get(f'wbnb{w}') or 0 for w in WALLETS}
    d['info'] = d['info'] or {}
    panel['names'] = d['names'] or ('?', '')
    panel['profile'] = d['profile'] or None  # an empty answer is retried on the next refresh
    level = (d['audit'] or (None, ''))[0]
    cv = d['curve']
    if cv and cv['state'] == 'curve':  # curve holdings are on chain (Agent Curve: shares), not in the baw balance
        for w in WALLETS:
            n = (word(cv['market'], 'sharesOf(address)', WALLETS[w]['addr']) if cv['kind'] == 'ac'
                 else word(token, 'balanceOf(address)', WALLETS[w]['addr'])) or 0
            if n and not (d.get(f'hold{w}') and dec(d[f'hold{w}'].get('balance')) > 0):
                d[f'hold{w}'] = {'balance': str(from_wei(n)), 'value': None}
    panel['block'] = None if trusted or (cv and cv['state'] == 'curve') else liquidity_block(d['info'], token)
    panel['can_buy'] = (level is None or level < BLOCK_RISK) and not panel['block']
    panel['data'] = d


def card_text(pid):
    panel, p = PANELS[pid], load_prefs()
    d, token = panel['data'], panel['token']
    sym, name = panel['names']
    info, prof = d.get('info') or {}, d.get('profile') or {}
    level, atext = d.get('audit') or (None, L('⚠️ Security check unavailable right now.', '⚠️ 安全检查暂时查不到。'))
    title = esc(sym) + (L(f'  <i>{esc(name)}</i>', f'　<i>{esc(name)}</i>') if name and name != sym else '') if sym else esc(name)
    lines = [f'🪙 <b>{title}</b>', f'<code>{token}</code>', '']
    cv = d.get('curve')
    pool = (L('💧 On the bonding curve, no DEX pool yet', '💧 内盘阶段，还没有外盘池子') if cv and cv['state'] == 'curve' and not known(info.get('liquidity'))
            else liq_line(info))
    if known(info.get('price')):
        moves = [f'{label} {change(info[f"percentChange{k}"])}' for k, label in
                 (('5m', L('5m', '5分')), ('1h', L('1h', '1时')), ('4h', L('4h', '4时')), ('24h', L('24h', '24时'))) if known(info.get(f'percentChange{k}'))]
        lines.append(f"💲 <b>${tiny_price(info['price'])}</b>" + (L('  ', '　') + L(' | ', '｜').join(moves) if moves else ''))
        lines.append(L(f"💰 MC {usd(info.get('marketCap'))} | {pool.lstrip('💧 ')}", f"💰 市值 {usd(info.get('marketCap'))}｜{pool.lstrip('💧 ')}"))
    else:
        lines.append(pool)
    who = []
    if known(info.get('holders')):
        who.append(L(f"👥 {int(dec(info['holders'])):,} holders", f"👥 {int(dec(info['holders'])):,} 人持有"))
        if known(info.get('top10HoldersPercentage')):
            who.append(L(f"Top 10 {fmt(info.get('top10HoldersPercentage'), 1)}%", f"前 10 大 {fmt(info.get('top10HoldersPercentage'), 1)}%"))
    if prof.get('creator'):
        dev = L(f" holds {fmt(info['devHoldingPercent'], 1)}%", f" 还有 {fmt(info['devHoldingPercent'], 1)}%") if known(info.get('devHoldingPercent')) else ''
        who.append(L(f"{link('https://bscscan.com/address/' + prof['creator'], 'Dev')}{dev}", f"{link('https://bscscan.com/address/' + prof['creator'], '发币者')}{dev}"))
    if who:
        lines.append(L(' | ', '｜').join(who))
    born = info.get('launchTime') or prof.get('created')
    stage = (L('Graduated', '已毕业') if str(info.get('migrateStatus')) == '1'
             else L(f"Curve {fmt(info['progress'], 0)}%", f"内盘 {fmt(info['progress'], 0)}%") if known(info.get('progress')) else '')
    if d.get('curve'):
        stage = esc(curve_stage(d['curve']))
    when = [L(f'🕒 Launched {ago(born)}', f'🕒 {ago(born)}开盘')] if known(born) else []
    when += [esc(prof['platform'])] if prof.get('platform') else []
    when += [stage] if stage else []
    if when:
        lines.append(L(' | ', '｜').join(when))
    alines = [esc(x) for x in atext.splitlines() if 'DYOR' not in x and L('No hits', '没有命中') not in x] or ['?']
    lines += ['', '🔔 ' + alines[0]] + alines[1:]
    if cv and cv.get('tax'):  # Gang V3: taxes only apply after graduation, read from the pad
        bt, st, div = cv['tax']
        now = L('on curve 0% | after graduation: ', '内盘 0%｜毕业后 ') if cv['state'] == 'curve' else ''
        lines = [x for x in lines if not x.startswith('⚖️')]
        lines.append(L(f"⚖️ Tax {now}buy {fmt(Decimal(bt) / 100, 2)}% | sell {fmt(Decimal(st) / 100, 2)}%", f"⚖️ 税 {now}买 {fmt(Decimal(bt) / 100, 2)}%｜卖 {fmt(Decimal(st) / 100, 2)}%")
                     + (L(f" | {fmt(Decimal(div) / 100, 0)}% of tax paid to holders", f"｜税收 {fmt(Decimal(div) / 100, 0)}% 分红给持有人") if div else ''))
    elif not any(L('tax', '税') in x.lower() for x in alines) and (known(prof.get('buy_tax')) or known(prof.get('sell_tax'))):
        lines.append(L(f"⚖️ Tax buy {fmt(prof.get('buy_tax'), 2)}% | sell {fmt(prof.get('sell_tax'), 2)}%", f"⚖️ 税 买 {fmt(prof.get('buy_tax'), 2)}%｜卖 {fmt(prof.get('sell_tax'), 2)}%"))
    crowd = [f'{label} {fmt(info[k], 1)}%' for label, k in ((L('Bundlers', '捆绑'), 'bundlerHoldingPercent'), (L('Insiders', '老鼠仓'), 'insiderHoldingPercent'),
                                                         (L('Snipers', '狙击'), 'sniperHoldingPercent')) if known(info.get(k)) and dec(info[k]) > 0]
    crowd += [L(f'{label} {int(dec(info[k]))}', f'{label} {int(dec(info[k]))} 人') for label, k in ((L('Smart money', '聪明钱'), 'smartMoneyHolders'), ('KOL', 'kolHolders'))
              if known(info.get(k)) and dec(info[k]) > 0]
    if crowd:
        lines.append('🧩 ' + L(' | ', '｜').join(crowd))
    names = {'x': 'X', 'twitter': 'X', 'website': L('Website', '官网'), 'tg': 'TG', 'telegram': 'TG', 'whitepaper': L('Whitepaper', '白皮书')}
    socials = [link(x['link'], names.get((x.get('label') or '').lower(), x.get('label') or L('Link', '链接')))
               for x in prof.get('links') or [] if str(x.get('link') or '').startswith('http')]
    lines.append('🔗 ' + L(' | ', '｜').join(socials + [link(f'https://gmgn.ai/bsc/token/{token}', L('Chart', 'K 线')),
                                            link(f'https://bscscan.com/token/{token}', 'BscScan')]))
    held = []
    for w in WALLETS:
        h = d.get(f'hold{w}')
        if h and dec(h.get('balance')) > 0:
            held.append(f"{w} {cn_num(h['balance'])}" + (L(f" (${fmt(h['value'], 2)})", f"（${fmt(h['value'], 2)}）") if known(h.get('value')) else ''))
    bnbs = L(' | ', '｜').join(f"{w} {fmt(d[f'bnb{w}'], 4) if d.get(f'bnb{w}') is not None else '?'}" for w in WALLETS)
    orders = sum(len(d.get(f'lim{w}') or []) for w in WALLETS)
    rules = sum(1 for r in load_rules() if r.get('status') == 'armed' and r['token'].lower() == token.lower())
    lines += ['', L('💎 Position  ', '💎 持仓　') + (L(' | ', '｜').join(held) if held else L('none yet', '还没有')), L(f'💳 BNB  {bnbs}', f'💳 BNB　{bnbs}')]
    wbnbs = [f'{w} {fmt(from_wei(n), 4)}' for w, n in (panel.get('wbnb') or {}).items() if n]
    if wbnbs:
        lines.append(L('💱 WBNB  ', '💱 WBNB　') + L(' | ', '｜').join(wbnbs) + L(' (left over from a Gang buy, can swap back to BNB)', '（Gang 盘买剩的，可以换回 BNB）'))
    extras = ([L(f'📌 Limit orders {orders}', f'📌 挂单 {orders}')] if orders else []) + ([L(f'🎯 TP/SL {rules}', f'🎯 止盈止损 {rules}')] if rules else [])
    if extras:
        lines.append(L(' | ', '｜').join(extras))
    if level is not None and level >= BLOCK_RISK:
        lines.append(L(f'\n⛔ Risk too high: sell only, no buying', f'\n⛔ 风险太高，只能卖不能买'))
    if panel.get('block'):
        lines.append(f"\n⛔ {esc(panel['block'])}")
    auto = L(f" | auto TP/SL after buy", f"｜买完自动设止盈止损") if p['auto_tpsl'] else ''
    lines.append(L(f"\n👛 Wallets {''.join(panel['wallets'])} | slippage {slip_name(p)}{auto} | "
                 f"updated {time.strftime('%H:%M', time.gmtime(time.time() + 8 * 3600))}", f"\n👛 用 {''.join(panel['wallets'])} 钱包｜滑点 {slip_name(p)}{auto}｜"
                 f"{time.strftime('%H:%M', time.gmtime(time.time() + 8 * 3600))} 更新"))
    return '\n'.join(lines)


def card_buttons(pid):
    panel, p = PANELS[pid], load_prefs()
    rows = [[btn(L('🔄 Refresh', '🔄 刷新'), pid, 'r'), btn(L(f"👛 Wallets: {''.join(panel['wallets'])} 🔁", f"👛 钱包：{''.join(panel['wallets'])} 🔁"), pid, 'w')]]
    if panel['can_buy']:
        rows += rows_of([btn(L(f'Buy {a}', f'买 {a}'), pid, 'b', a) for a in p['buy'] if 0 < dec(a) <= MAX_BNB])
        rows += rows_of([btn(L(f'Buy {a}%', f'买 {a}%'), pid, 'B', a) for a in p['buy_pct']] + [btn(L('Buy x ✏️', '买 x ✏️'), pid, 'X')])
    elif panel.get('block'):  # only the pool floor is in the way (risk blocks never get this)
        rows.append([btn(L('🔓 Buy anyway (30 min)', '🔓 这颗仍要买（30 分钟）'), pid, 'F')])
    if any(d for d in panel.get('wbnb', {}).values()):
        rows.append([btn(L('💱 WBNB → BNB', '💱 WBNB 换回 BNB'), pid, 'U')])
    rows += rows_of([btn(L(f'🔴 Sell {s}%', f'🔴 卖 {s}%'), pid, 's', s) for s in p['sell']] + [btn(L('🔴 Sell x% ✏️', '🔴 卖 x% ✏️'), pid, 'S')])
    rows.append([btn(L('📌 Limit', '📌 挂单'), pid, 'L'), btn(L('🎯 TP/SL', '🎯 止盈止损'), pid, 'T'), btn(L('📤 Sweep', '📤 转出'), pid, 'O'), btn(L('⚙️ Settings', '⚙️ 设置'), pid, 'G')])
    return rows


def show_panel(token):
    pid = new_panel(token)
    card_data(pid)
    mid = say(card_text(pid), card_buttons(pid), rich=True)
    if mid:
        PANELS[pid]['msg'] = (OWNER, mid)


def edit_or_send(where, text, buttons, rich=False):
    """Replace message where=(chat, id) in place, else send a new one; returns the (chat, id) now showing it."""
    if not DRY and where:
        chat, mid = where
        try:
            tg('editMessageText', chat_id=chat, message_id=mid, text=text[:4000], disable_web_page_preview=True,
               reply_markup={'inline_keyboard': buttons}, **({'parse_mode': 'HTML'} if rich else {}))
            return where
        except urllib.error.HTTPError as e:
            if 'not modified' in e.read().decode(errors='replace'):
                return where
    mid = say(text, buttons, rich=rich)
    return (OWNER, mid) if mid else where


def edit_view(pid, text, buttons, rich=True):
    """Replace the card message (Telegram HTML) in place; falls back to a new message."""
    PANELS[pid]['msg'] = edit_or_send(PANELS[pid].get('msg'), text, buttons, rich)


def home(pid, refresh=True):
    if refresh or not PANELS[pid].get('data'):
        card_data(pid)
    edit_view(pid, card_text(pid), card_buttons(pid), rich=True)


def slip_name(p):
    return L('auto', '自动') if p['slippage'] == 'auto' else p['slippage'] + '%'


def settings_text(p):
    return '\n'.join([
        L('⚙️ <b>Settings</b> (tap a button below to change)', '⚙️ <b>设置</b>（按下面的按钮改）'), '',
        L(f"💰 Quick buy  {'/'.join(p['buy'])} BNB (per wallet)", f"💰 快捷买　{'／'.join(p['buy'])} BNB（每个钱包各买）"),
        L(f"📊 % buy  {'/'.join(x + '%' for x in p['buy_pct'])} (% of each wallet's own BNB)", f"📊 比例买　{'／'.join(x + '%' for x in p['buy_pct'])}（各钱包自己 BNB 的几 %）"),
        L(f"🔴 Quick sell  {'/'.join(s + '%' for s in p['sell'])}", f"🔴 快捷卖　{'／'.join(s + '%' for s in p['sell'])}"),
        L(f"👛 Default wallets  {''.join(wallet_list(p['wallets']) or WALLETS)} | 🎚 Slippage  {slip_name(p)}", f"👛 默认钱包　{''.join(wallet_list(p['wallets']) or WALLETS)}｜🎚 滑点　{slip_name(p)}"),
        L(f"💧 Min pool  {usd(p['min_liq']) if dec(p['min_liq']) > 0 else 'off'}", f"💧 池子太小不买　{usd(p['min_liq']) if dec(p['min_liq']) > 0 else '关闭'}"),
        L(f"🎯 Auto TP/SL after buy  {'on' if p['auto_tpsl'] else 'off'} ({esc(tpsl_text(p))})", f"🎯 买完自动设止盈止损　{'开' if p['auto_tpsl'] else '关'}（{esc(tpsl_text(p))}）"),
        L(f"📤 Auto sweep  {SEND_WHEN[p['auto_send']]}", f"📤 自动转出　{SEND_WHEN[p['auto_send']]}") + (L(f" | to {esc(payout_name(payout_addr(p)))}", f"｜转到 {esc(payout_name(payout_addr(p)))}") if PAYOUT
                                                     else L(' (set payout to your main wallet in wallets.json on the computer first)', '（先在电脑 wallets.json 的 payout 填主钱包）')),
        L(f"⭐ Shortcuts  {esc(', '.join(p['keys'])) if p['keys'] else 'none yet (tap ⭐ Save shortcut on a preview)'}", f"⭐ 快捷键　{esc('、'.join(p['keys'])) if p['keys'] else '还没有（在预览上按「⭐ 存成快捷键」）'}"),
        L(f'🔒 Max {fmt(MAX_BNB)} BNB per trade (only editable in the config file on the computer)', f'🔒 每次最多 {fmt(MAX_BNB)} BNB（只能改电脑上的设置档）')])


def settings_view(pid):
    p = load_prefs()
    rows = [[btn(L('💰 Quick buy ✏️', '💰 快捷买 ✏️'), pid, 'e', 'buy'), btn(L('📊 % buy ✏️', '📊 比例买 ✏️'), pid, 'e', 'buy_pct')],
            [btn(L('🔴 Quick sell ✏️', '🔴 快捷卖 ✏️'), pid, 'e', 'sell'), btn(L(f"👛 Wallets: {''.join(wallet_list(p['wallets']) or WALLETS)} 🔁", f"👛 钱包：{''.join(wallet_list(p['wallets']) or WALLETS)} 🔁"), pid, 'v')],
            [btn(L(f'🎚 Slippage: {slip_name(p)} ✏️', f'🎚 滑点：{slip_name(p)} ✏️'), pid, 'e', 'slippage'), btn(L('💧 Min pool ✏️', '💧 池子门槛 ✏️'), pid, 'e', 'min_liq')],
            [btn(L(f"🎯 Auto TP/SL: {'on' if p['auto_tpsl'] else 'off'} 🔁", f"🎯 自动止盈止损：{'开' if p['auto_tpsl'] else '关'} 🔁"), pid, 't')],
            [btn(L('TP rule ✏️', '止盈条件 ✏️'), pid, 'e', 'tp'), btn(L('SL rule ✏️', '止损条件 ✏️'), pid, 'e', 'sl')],
            [btn(L(f"📤 Auto sweep: {SEND_WHEN[p['auto_send']]} 🔁", f"📤 自动转出：{SEND_WHEN[p['auto_send']]} 🔁"), pid, 'a')]
            + ([btn(L(f'📍 To: {payout_name(payout_addr(p))} 🔁', f'📍 转到：{payout_name(payout_addr(p))} 🔁'), pid, 'd')] if len(PAYOUT) > 1 else [])]
    rows += rows_of([cmd_btn(L(f'🗑 Delete "{n}"', f'🗑 删「{n}」'), f'/key {n}') for n in p['keys']], 3)
    if PANELS[pid]['token']:
        rows.append([btn(L('⬅️ Back', '⬅️ 返回'), pid, 'h')])
    return settings_text(p), rows


def current_price(pid):
    return dec(((PANELS[pid].get('data') or {}).get('info') or {}).get('price'))


def tpsl_view(pid):
    panel = PANELS[pid]
    token, price = panel['token'], current_price(pid)
    mine = [r for r in load_rules() if r.get('status') == 'armed' and r['token'].lower() == token.lower()]
    lines = [L(f"🎯 <b>TP/SL | {esc(panel['names'][0])}</b>", f"🎯 <b>止盈止损｜{esc(panel['names'][0])}</b>"),
             L(f'Price ${price_text(price)}', f'现价 ${price_text(price)}') if price > 0 else L('⚠️ No current price, cannot watch', '⚠️ 查不到现价，没办法盯盘'), '']
    lines += [f'{i}. {esc(rule_text(r))}' for i, r in enumerate(mine, 1)] or [L('None set.', '还没设置。')]
    lines += ['', L(f'The bot checks the price every {WATCH_EVERY}s and sells automatically when hit, without asking', f'机器人每 {WATCH_EVERY} 秒看一次价格，到了就自动卖，不会再问你'), L('(the computer must stay on)', '（电脑要开着）')]
    rows = [[btn(L('➕ TP ✏️', '➕ 止盈 ✏️'), pid, 'P'), btn(L('➕ SL ✏️', '➕ 止损 ✏️'), pid, 'Q')]]
    if mine:
        rows.append([btn(L('🗑 Clear rules', '🗑 清除这颗币的规则'), pid, 'C')])
    rows.append([btn(L('⬅️ Back', '⬅️ 返回'), pid, 'h')])
    return '\n'.join(lines), rows


def orders_view(pid):
    panel = PANELS[pid]
    token, price = panel['token'], current_price(pid)
    found = each(list(WALLETS), lambda w: [(w, o) for o in limit_orders(w, token)])
    orders = [x for part in found for x in part]
    lines = [L(f"📌 <b>Limit orders | {esc(panel['names'][0])}</b>", f"📌 <b>挂单｜{esc(panel['names'][0])}</b>"), L(f'Price ${price_text(price)}', f'现价 ${price_text(price)}') if price > 0 else L('Price unavailable', '现价查不到'), '']
    for i, (w, o) in enumerate(orders, 1):
        buy = o.get('side') == 'BUY' or (o.get('fromToken') or '').lower() == NATIVE.lower()
        lines.append(L(f"{i}. <b>{w}</b> Limit {'buy' if buy else 'sell'}  {esc(fmt(o.get('fromTokenQty'), 6))} "
                     f"{esc(o.get('fromTokenName'))}  at ${esc(o.get('triggerPrice'))}", f"{i}. <b>{w}</b> 限价{'买' if buy else '卖'}　{esc(fmt(o.get('fromTokenQty'), 6))} "
                     f"{esc(o.get('fromTokenName'))}　到 ${esc(o.get('triggerPrice'))}"))
    if not orders:
        lines.append(L('No limit orders yet.', '还没有挂单。'))
    lines += ['', L('Limit buy = buy when price drops to it; limit sell = sell when price rises to it', '限价买＝跌到才买；限价卖＝涨到才卖'), L('Run by Binance, fills even with the computer off; for "sell if it drops" use 🎯 SL', '交给币安运行，电脑关着也会成交；「跌了就卖」请用 🎯 止损')]
    rows = [[btn(L('Limit buy ✏️', '限价买 ✏️'), pid, 'y'), btn(L('Limit sell ✏️', '限价卖 ✏️'), pid, 'z')] if panel['can_buy'] else [btn(L('Limit sell ✏️', '限价卖 ✏️'), pid, 'z')]]
    rows += rows_of([btn(L(f'Cancel {i}', f'撤单 {i}'), pid, 'x', f"{w}.{o.get('strategyId')}") for i, (w, o) in enumerate(orders, 1)])
    rows.append([btn(L('🔄 Refresh', '🔄 刷新'), pid, 'L'), btn(L('⬅️ Back', '⬅️ 返回'), pid, 'h')])
    return '\n'.join(lines), rows


def ask_input(pid, kind, prompt):
    AWAIT.clear()
    AWAIT.update(kind=kind, pid=pid, expires=time.time() + AWAIT_TTL)
    wallets = ''.join((PANELS.get(pid) or PRESALES.get(pid) or CONTRACTS.get(pid) or {}).get('wallets') or [])
    say(L(f'{prompt}\n\nJust type your reply | valid for {AWAIT_TTL // 60} min', f'{prompt}\n\n直接打字回我｜{AWAIT_TTL // 60} 分钟内有效') + (L(f' | wallets {wallets}', f'｜用 {wallets} 钱包') if wallets else ''))


def on_panel(data, msg):
    _, pid, op, arg = (data.split(':', 3) + ['', '', ''])[:4]
    panel = PANELS.get(pid)
    if not panel or panel['expires'] < time.time():
        say(L('This card has expired, please paste the contract address again.', '这张卡片已过期，请重新贴一次合约地址。'))
        return
    if msg:
        panel['msg'] = (msg['chat']['id'], msg['message_id'])
    panel['expires'] = time.time() + PANEL_TTL
    token, ws = panel['token'], ''.join(panel['wallets'])
    if op == 'G':
        edit_view(pid, *settings_view(pid))
    elif op == 'e' and arg in SET_PROMPTS:
        ask_input(pid, 'set:' + arg, SET_PROMPTS[arg])
    elif op == 'v':
        p = load_prefs()
        p['wallets'] = ''.join(next_wallets(wallet_list(p['wallets']) or list(WALLETS)))
        save_prefs(p)
        edit_view(pid, *settings_view(pid))
    elif op == 't':
        p = load_prefs()
        p['auto_tpsl'] = not p['auto_tpsl']
        save_prefs(p)
        edit_view(pid, *settings_view(pid))
    elif op == 'a':
        if not PAYOUT:
            say(L('No payout address yet: set "payout" in wallets.json on the computer to your main wallet address (computer only), then restart the bot.', '还没有转出目的地：在电脑上的 wallets.json 的 "payout" 填主钱包地址（只能在电脑上改），重启机器人后再开。'))
            return
        p = load_prefs()
        modes = list(SEND_WHEN)
        p['auto_send'] = modes[(modes.index(p['auto_send']) + 1) % len(modes)] if p['auto_send'] in modes else 'off'
        save_prefs(p)
        edit_view(pid, *settings_view(pid))
    elif op == 'd' and PAYOUT:
        p = load_prefs()
        addrs = [x['addr'] for x in PAYOUT]
        cur = payout_addr(p)
        p['send_to'] = addrs[(addrs.index(cur) + 1) % len(addrs)]
        save_prefs(p)
        edit_view(pid, *settings_view(pid))
    elif not token:
        return  # a settings-only card (from /set) has nothing else
    elif op in ('r', 'h'):
        home(pid)
    elif op == 'w':
        panel['wallets'] = next_wallets(panel['wallets'])
        home(pid, refresh=False)
    elif op == 'F' and panel.get('block'):
        level = (panel['data'].get('audit') or (None, ''))[0]
        if level is not None and level >= BLOCK_RISK:
            say(L('High-risk tokens cannot be unlocked.', '风险太高的币不能解锁。'))
            return
        LIQ_OK[token.lower()] = time.time() + LIQ_OK_TTL
        say(L('🔓 This token skips the pool minimum for 30 min; other tokens unchanged.\nSmall pool: buys and sells move the price, and sells may return less than expected.', '🔓 这颗币 30 分钟内不挡池子门槛，其他币照旧。\n池子小：买卖都会推动价格，卖的时候也可能卖不到预期的数量。'))
        home(pid)
    elif op == 'U':
        back = [(w, *x) for w in WALLETS for x in unwrap_steps(w)]
        if back:
            guarded(lambda _: preview_calls(L('💱 WBNB → BNB', '💱 WBNB 换回 BNB'), back), [])
        else:
            say(L('No WBNB to swap.', '没有 WBNB 要换。'))
    elif op == 'b' and panel['can_buy']:
        guarded(cmd_buy, [token, arg, ws])
    elif op == 'B' and panel['can_buy']:
        guarded(cmd_buy, [token, arg + '%', ws])
    elif op == 's':
        guarded(cmd_sell, [token, arg, ws])
    elif op in INPUT_PROMPTS and (panel['can_buy'] or op not in ('X', 'y')):
        ask_input(pid, *INPUT_PROMPTS[op])
    elif op == 'T':
        edit_view(pid, *tpsl_view(pid))
    elif op == 'C':
        n = len(update_rules(lambda r: r.get('status') == 'armed' and r['token'].lower() == token.lower()
                             and set_status(r, 'cancelled')))
        say(L(f'🗑 Cleared {n} TP/SL rules.', f'🗑 已清除 {n} 条止盈止损。'))
        edit_view(pid, *tpsl_view(pid))
    elif op == 'L':
        edit_view(pid, *orders_view(pid))
    elif op == 'O':
        guarded(lambda _: manual_send(token, list(panel['wallets'])), [])
    elif op == 'x':
        w, _, sid = arg.partition('.')
        if w in WALLETS and sid:
            r = baw(w, 'limit-order', 'cancel', '--strategyId', sid)
            say(L(f'🗑 {w} order cancelled', f'🗑 {w} 已撤单') if r.get('success') else L(f'❌ {w} cancel failed: {baw_err(r)}', f'❌ {w} 撤单失败：{baw_err(r)}'))
            edit_view(pid, *orders_view(pid))


def on_input(item, text):
    pid, kind = item['pid'], item['kind']
    if kind == 'keyname':
        command = CMD_STORE.get(pid)
        if not command:
            raise UsageError(L('This action expired, please redo it before saving.', '这个动作过期了，请重新做一次再存。'))
        cmd_key([text.strip().replace(' ', ''), *command.split()])
        return
    if kind.startswith('snp:'):
        field = kind[4:]
        try:
            value = SNIPE_PROMPTS[field][1](text.strip().replace('%', '').replace('％', ''))
        except (ValueError, InvalidOperation):
            raise UsageError(L('Please enter a number.', '请打数字。'))
        s = set_snipe(**{field: value})
        say(L('✅ Sniper settings updated', '✅ 狙击设置已更新'))
        say(snipe_text(s), snipe_buttons(s), rich=True)
        return
    if kind.startswith('cfn:'):
        if pid not in CONTRACTS:
            say(L('This contract card has expired, please paste the contract address again.', '这张合约卡片已过期，请重新贴一次合约地址。'))
        else:
            contract_fn(pid, int(kind[4:]), text)
        return
    if kind in ('qbuy', 'qsell') or kind.startswith('qact:'):
        if pid not in PRESALES:
            say(L('This presale card has expired, please paste the instructions again.', '这张预售卡片已过期，请重新贴一次说明。'))
        elif kind.startswith('qact:'):
            presale_action(pid, int(kind[5:]), text)
        elif kind == 'qbuy':
            presale_buy(pid, text.strip().lower().replace('bnb', ''))
        else:
            presale_sell(pid, text.strip().replace('％', '%').rstrip('%'))
        return
    panel = PANELS.get(pid)
    if not panel:
        say(L('This card has expired, please paste the contract address again.', '这张卡片已过期，请重新贴一次合约地址。'))
        return
    token, ws = panel['token'], ''.join(panel['wallets'])
    vals = text.replace('％', '%').replace(',', ' ').split()
    if not vals:
        raise UsageError(L('No number received.', '没有收到数字。'))
    if kind == 'buy':
        cmd_buy([token, vals[0].lower().replace('bnb', ''), ws])
    elif kind == 'sell':
        cmd_sell([token, vals[0].rstrip('%'), ws])
    elif kind in ('tp', 'sl'):
        arm_manual(pid, kind, vals)
    elif kind in ('lbuy', 'lsell'):
        limit_preview(pid, kind[1:], vals)
    elif kind.startswith('set:'):
        set_pref(kind[4:], vals)
        say(L('✅ Settings updated', '✅ 设置已更新'))
        edit_view(pid, *settings_view(pid), rich=True)


def set_pref(field, vals):
    p = load_prefs()
    if field == 'buy':
        if not 1 <= len(vals) <= 6:
            raise UsageError(L('Enter 1-6 buy amounts, e.g. 0.01 0.03 0.05', '买入金额要 1～6 个，例如 0.01 0.03 0.05'))
        p['buy'] = [fmt(need_amount(v)) for v in vals]
    elif field in ('buy_pct', 'sell'):
        if not 1 <= len(vals) <= 5:
            raise UsageError(L('Enter 1-5 percentages, e.g. 50 100', '比例要 1～5 个，例如 50 100'))
        pcts = [need_amount(v, cap=False) for v in vals]
        if any(x > 100 for x in pcts):
            raise UsageError(L('Percent cannot exceed 100.', '比例不能超过 100。'))
        p[field] = [fmt(x, 2) for x in pcts]
    elif field == 'wallets':
        if len(vals) != 1:
            raise UsageError(L(f'Wallet must be {wallet_hint()}.', f'钱包要写 {wallet_hint()}。'))
        p['wallets'] = ''.join(parse_wallets(vals))
    elif field == 'slippage':
        v = vals[0].lower().rstrip('%').replace(*zh_word('自动'), 'auto')
        if v != 'auto' and not 0 < dec(v) <= 50:
            raise UsageError(L('Slippage must be "auto" or a percentage between 0 and 50.', '滑点要打「自动」，或 0～50 之间的百分比。'))
        p['slippage'] = 'auto' if v == 'auto' else fmt(dec(v), 2)
    elif field == 'min_liq':
        v = vals[0].lower().replace('$', '').replace(',', '')
        num = v.rstrip('km')
        n = dec(num) * (1000 if v.endswith('k') else 10 ** 6 if v.endswith('m') else 1)
        if n < 0 or (n == 0 and num not in ('0', '0.0')):
            raise UsageError(L('Pool minimum must be a number, e.g. 20000 or 20k; 0 = off.', '池子门槛要写数字，例如 20000、20k；0＝关闭。'))
        p['min_liq'] = fmt(n, 0)
    elif field in ('tp', 'sl'):
        move, sell = move_sell(field, vals)
        p[field] = [fmt(move, 2), fmt(sell, 2)]
    elif field == 'auto':
        p['auto_tpsl'] = vals[0].lower() in ('on', '1', 'true') + zh_word('开')
    else:
        raise UsageError(L('Unknown setting.', '不认得的设置。'))
    save_prefs(p)
    return p


SET_ALIASES = {'buy': 'buy', 'pct': 'buy_pct', 'buy_pct': 'buy_pct', 'sell': 'sell', 'wallets': 'wallets',
               'wallet': 'wallets', 'slip': 'slippage', 'slippage': 'slippage', 'liq': 'min_liq', 'tp': 'tp',
               'sl': 'sl', 'auto': 'auto'}
SET_ALIASES.update(zip(zh_word('买', '比例', '卖', '钱包', '滑点', '池子', '止盈', '止损'),
                       ('buy', 'buy_pct', 'sell', 'wallets', 'slippage', 'min_liq', 'tp', 'sl')))


def cmd_set(args):
    if not args:
        pid = new_panel(None)
        edit_view(pid, *settings_view(pid))
        return
    field = SET_ALIASES.get(args[0].lower())
    if not field or not args[1:]:
        raise UsageError(L('Usage: /set buy 0.01 0.03 0.05 | /set sell 50 100 | /set wallets AB | /set slip 5 | /set liq 20k'
                         ' | /set tp 100 50 | /set sl 30 100 | /set auto on (or just /set for buttons)', '用法：/set buy 0.01 0.03 0.05｜/set sell 50 100｜/set wallets AB｜/set slip 5｜/set liq 20k'
                         '｜/set tp 100 50｜/set sl 30 100｜/set auto on（也可以直接 /set 用按钮改）'))
    p = set_pref(field, args[1:])
    say(L('✅ Updated\n\n', '✅ 已更新\n\n') + settings_text(p), rich=True)


# ---------------------------------------------------------------- take-profit / stop-loss watcher

RULES_FILE = CONF_DIR / 'rules.json'
RULES_LOCK = threading.Lock()
WATCH_EVERY = 20


def load_rules():
    try:
        rules = json.loads(RULES_FILE.read_text())
        return rules if isinstance(rules, list) else []
    except (OSError, ValueError):
        return []


def save_rules(rules):
    write_json(RULES_FILE, rules)


def add_rules(new):
    with RULES_LOCK:
        save_rules(load_rules() + new)


def update_rules(fn):
    """Apply fn(rule) -> True-if-changed under the lock; returns the changed rules."""
    with RULES_LOCK:
        rules = load_rules()
        changed = [r for r in rules if fn(r)]
        if changed:
            save_rules(rules)
        return changed


def set_status(r, status, **extra):
    r.update(status=status, **extra)
    return True


def move_sell(kind, vals):
    if len(vals) != 2:
        raise UsageError(L('Need two numbers: "move% sell%", e.g. 100 50.', '要两个数字：「幅度% 卖出%」，例如 100 50。'))
    move, sell = need_amount(vals[0], cap=False), need_amount(vals[1], cap=False)
    if sell > 100:
        raise UsageError(L('Sell percent cannot exceed 100.', '卖出比例不能超过 100。'))
    if kind == 'sl' and move >= 100:
        raise UsageError(L('Drop must be under 100%.', '跌幅要小于 100%。'))
    return move, sell


def make_rule(token, symbol, w, kind, move, sell, entry):
    trigger = entry * (1 + move / 100) if kind == 'tp' else entry * (1 - move / 100)
    return {'id': secrets.token_hex(4), 'token': token, 'symbol': symbol, 'wallet': w, 'kind': kind,
            'move': fmt(move, 2), 'sell': fmt(sell, 2), 'entry': str(entry), 'trigger': str(trigger),
            'status': 'armed', 'created': int(time.time())}


def tpsl_text(p):
    return L(f"up {p['tp'][0]}% sell {p['tp'][1]}% | down {p['sl'][0]}% sell {p['sl'][1]}%", f"涨 {p['tp'][0]}% 卖 {p['tp'][1]}%｜跌 {p['sl'][0]}% 卖 {p['sl'][1]}%")


def rule_text(r):
    tp = r['kind'] == 'tp'
    return (L(f"{'TP' if tp else 'SL'} | wallet {r['wallet']} | {'up' if tp else 'down'} to ${price_text(r['trigger'])}"
            f" ({'+' if tp else '-'}{r['move']}%, base ${price_text(r['entry'])}) sell {r['sell']}%", f"{'止盈' if tp else '止损'}｜钱包 {r['wallet']}｜{'涨' if tp else '跌'}到 ${price_text(r['trigger'])}"
            f"（{'+' if tp else '-'}{r['move']}%，基准 ${price_text(r['entry'])}）卖 {r['sell']}%"))


def arm_auto(w, token, prefs):
    """After a filled buy: arm the default take-profit and stop-loss for that wallet."""
    price = dec(token_info(token).get('price'))
    if price <= 0:
        return L('⚠️ No price found, TP/SL not set for this buy.', '⚠️ 查不到价格，这笔没有设置止盈止损。')
    sym = token_names(token)[0]
    rules = [dict(make_rule(token, sym, w, 'tp', dec(prefs['tp'][0]), dec(prefs['tp'][1]), price), auto=True),
             dict(make_rule(token, sym, w, 'sl', dec(prefs['sl'][0]), dec(prefs['sl'][1]), price), auto=True)]
    # each rule sells a share of the whole holding, so buying the same token again replaces the automatic pair
    # (from the newest price) instead of stacking a second pair on top
    with RULES_LOCK:
        old = load_rules()
        for x in old:
            if x.get('auto') and x.get('status') == 'armed' and x['wallet'] == w and x['token'].lower() == token.lower():
                x['status'] = 'replaced'
        save_rules(old + rules)
    return L('🎯 TP/SL set:\n', '🎯 已设置止盈止损：\n') + '\n'.join(rule_text(r) for r in rules)


def arm_manual(pid, kind, vals):
    move, sell = move_sell(kind, vals)
    panel = PANELS[pid]
    token, ws, sym = panel['token'], list(panel['wallets']), panel['names'][0]
    price = dec(token_info(token).get('price'))
    if price <= 0:
        raise UsageError(L('No price for this token, cannot set TP/SL.', '查不到这颗币的价格，没办法设置止盈止损。'))
    holds = each(ws, lambda w: holding(w, token))
    have = [w for w, h in zip(ws, holds) if h and dec(h.get('balance')) > 0]
    sample = make_rule(token, sym, ws[0], kind, move, sell, price)
    name = L('tp', '止盈') if kind == 'tp' else L('sl', '止损')
    lines = [L(f'🎯 Set {name} preview: {sym}', f'🎯 设置{name}预览：{sym}'), L(f'Price ${price_text(price)}', f'现价 ${price_text(price)}'), '']
    lines += [L(f"Wallet {w}: {'holding ✅' if w in have else 'no position → skip'}", f"钱包 {w}：{'有持仓 ✅' if w in have else '没有持仓 → 略过'}") for w in ws]
    if not have:
        say('\n'.join(lines + ['', L('No wallet holds this token.', '没有钱包持有这颗币。')]))
        return
    lines += ['', L(f"Rule: when price goes {'up' if kind == 'tp' else 'down'} to ${price_text(sample['trigger'])}"
                  f" ({'+' if kind == 'tp' else '-'}{fmt(move, 2)}%), auto market-sell {fmt(sell, 2)}%.", f"条件：价格{'涨' if kind == 'tp' else '跌'}到 ${price_text(sample['trigger'])}"
                  f"（{'+' if kind == 'tp' else '-'}{fmt(move, 2)}%）时，自动市价卖出 {fmt(sell, 2)}%。"),
              L('⚠️ After you confirm, the bot sells as soon as the rule hits, without asking; the computer and bot must stay on to watch.', '⚠️ 按确认后，条件到了机器人会直接卖，不会再问你；电脑和机器人要开着才会盯盘。')]

    def arm():
        add_rules([make_rule(token, sym, w, kind, move, sell, price) for w in have])
        say(L(f"🎯 {name} set ({', '.join(have)}), now watching.", f"🎯 {name}已设置（{'、'.join(have)}），开始盯盘。"))
    ask_confirm('\n'.join(lines), L(f'Confirm {name}', f'确认设置{name}'), arm)


def check_rules():
    armed = [r for r in load_rules() if r.get('status') == 'armed']
    if not armed:
        return
    tokens = sorted({r['token'] for r in armed})
    prices = dict(zip(tokens, each(tokens, lambda t: dec(token_info(t).get('price')))))
    for r in armed:
        p = prices.get(r['token'])
        if not p or p <= 0:
            continue
        trig = dec(r['trigger'])
        if not (p >= trig if r['kind'] == 'tp' else p <= trig):
            continue
        # claim the rule before selling so it can never fire twice
        if update_rules(lambda x: x['id'] == r['id'] and x.get('status') == 'armed'
                        and set_status(x, 'fired', fired_price=str(p), fired_at=int(time.time()))):
            try:
                out = fire_rule(r, p)
            except Exception as e:  # noqa: BLE001 - report it and let the rule try again
                logging.exception('rule %s', r['id'])
                say(L(f"❌ {r['symbol']} TP/SL sell error: {str(e)[:200]}", f"❌ {r['symbol']} 止盈止损卖出时出错：{str(e)[:200]}"))
                out = 'retry'
            if out == 'retry':
                fails = int(r.get('fails') or 0) + 1
                update_rules(lambda x: x['id'] == r['id'] and set_status(
                    x, 'armed' if fails < RULE_FAILS else 'failed', fails=fails))
                say(L(f'🎯 Will retry next round (in ~{WATCH_EVERY}s), attempt {fails}/{RULE_FAILS}.', f'🎯 这条会在下一轮（约 {WATCH_EVERY} 秒后）再试（第 {fails}／{RULE_FAILS} 次）。') if fails < RULE_FAILS
                    else L(f'🎯 Failed {RULE_FAILS} times in a row; this TP/SL is disabled, please handle it on the card.', f'🎯 连续失败 {RULE_FAILS} 次，这条止盈止损停用了，请到卡片手动处理。'))


RULE_FAILS = 3  # a take-profit / stop-loss that couldn't sell goes back on watch this many times


def fire_rule(r, price):
    """Sell for a triggered rule: 'ok', 'retry' (nothing was sent, try again), 'unknown' or 'done' (don't retry)."""
    name = L('tp', '止盈') if r['kind'] == 'tp' else L('sl', '止损')
    w, token, pct = r['wallet'], r['token'], dec(r['sell'])
    say(L(f"🎯 {name} triggered | {r['symbol']} price ${price_text(price)} (target ${price_text(r['trigger'])})"
        f"\n{w} auto-selling {r['sell']}%", f"🎯 {name}触发｜{r['symbol']} 现价 ${price_text(price)}（设置 ${price_text(r['trigger'])}）"
        f"\n{w} 自动卖 {r['sell']}%"))
    cv = detect_curve(token)
    if cv and (cv['state'] == 'curve' or cv['kind'] == 'ac'):
        ok, res = auto_sell(token, w, pct, cv)
        say(L(f'✅ <b>{w} {name} sold</b>  {tx_link(res)}', f'✅ <b>{w} {name}卖出</b>　{tx_link(res)}') if ok else L(f'❌ <b>{w} {name} not sold</b>  {esc(res)}', f'❌ <b>{w} {name}没有卖出</b>　{esc(res)}'), rich=True)
        if ok and pct >= 100:
            close_rules(w, token)
        return 'ok' if ok else 'unknown' if UNKNOWN_MARK in res else 'retry'
    amount, text = sell_prep(w, token, pct)
    if not amount:
        say(L(f'🎯 {name} not sold: {text}', f'🎯 {name}没有卖出：{text}'))
        if L('not holding this token', '没有这个代币') in text:  # nothing left to sell: retrying can't help
            close_rules(w, token)
            return 'done'
        return 'retry'
    lines = do_swap([w], token, NATIVE, {w: amount}, L(f'{name} sell', f'{name}卖出')) or ['']
    if lines[0].startswith('✅'):
        if pct >= 100:  # the position is gone, so the wallet's other rules on this token are moot
            close_rules(w, token)
        return 'ok'
    return 'unknown' if L('result unknown', '结果未知') in lines[0] or L('still pending', '还在处理') in lines[0] else 'retry'


def close_rules(w, token):
    update_rules(lambda x: x.get('status') == 'armed' and x['token'].lower() == token.lower()
                 and x['wallet'] == w and set_status(x, 'done'))


# ---------------------------------------------------------------- new launches: watch and snipe

LOGS_RPC = CFG.get('LOGS_RPC_URL') or 'https://bsc-rpc.publicnode.com'  # bsc-dataseed refuses eth_getLogs
GANG_V2_PAD = '0x1B8F3f69DFa0a7F63d308C7EB0Fd38D8b099966E'  # new Gang V2 curves are all created here
GANG_V3_PAD = '0x15cfd2dc8278fb87a1783ef42111618e20874365'  # Gang V3: taxed-token curves (kindOf 1) + fair mints (2)
GANG_V3_ROUTER = '0xe040445690aa56c8b68b7f7038c7b2562b5079c4'
FOUR_CORE = '0xebe7b6c1089d9f72ad07f34e36d898e44e5e27f3'  # Four.meme Agent ID launches (OpenFour)
FOUR_AGENT_PRESET = 1778027615735  # OpenFour preset of the Agent-ID-only template
FLAP_PORTAL = '0xe2cE6ab80874Fa9Fa2aAE65D277Dd6B8e65C9De0'
AC_FACTORY = '0xC55820f24015bE7c4f18949D9e1363A3609B300e'  # Flap Agent Curve markets
AC_PORTAL = '0x49387D2E6cbF8FefE856e3FBc3E98a01aC3af062'
WBNB = '0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c'
LAUNCH_TOPICS = {
    '0xd23a447045c377a966a79f59983bae21db7b92fead61df8ace4aea5628043791': 'gang',  # Gang V2 Created
    '0x416fe98c8f1ea2977674a1a4ee6252eb72597a8392fc2d7d0599a2bf5d8fdb63': 'gang',  # Gang V3 Created (same topics)
    '0x7dae4dcd1b20eb0445e7cc1d02b3e2e7f32fa63081a0e764732c269f2b8f228f': 'four',  # OpenFour TokenCreated
    '0x2179156b3d2ade0f0de96eff28a7c29d924002784e113e69108d91a02cbf9098': 'flap',  # FlapNFTGated(token, nft)
    '0x4a726772d8e983c0a65f54774e6e6da08311f9c8ce3cba0c4fd725f09dcf780e': 'ac',  # Agent Curve market created
}
LAUNCH_ADDRS = [GANG_V2_PAD, GANG_V3_PAD, FOUR_CORE, FLAP_PORTAL, AC_FACTORY]
TRACK_NAME = {'gang': 'Gang', 'four': L('Four exclusive', 'Four 专属'), 'flap': L('Flap gated', 'Flap 门槛盘'), 'ac': 'Flap Agent Curve'}
TRACK_CAP = {'gang': 1, 'four': 1, 'flap': 2, 'ac': 2}  # % of supply one wallet may hold during the curve
SUPPLY = 10 ** 27
SNIPE_FILE = CONF_DIR / 'snipe.json'  # today's spend and the launches already handled
SNIPE_LOG = CONF_DIR / 'snipe-log.jsonl'
SNIPE_LOCK = threading.Lock()
WATCH = {'cursor': None}


def rpc(method, params, url=None, timeout=10):
    body = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params}).encode()
    req = urllib.request.Request(url or RPC, data=body, headers={'Content-Type': 'application/json',
                                                                  'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        r = json.loads(resp.read())
    if r.get('error'):
        raise RuntimeError(f"{r['error'].get('code')} {r['error'].get('message')}")
    return r.get('result')


def words(raw):
    raw = raw[2:] if raw.startswith('0x') else raw
    return [int(raw[i:i + 64], 16) for i in range(0, len(raw) - 63, 64)]


def call_words(to, sig, *args, sender=None):
    """All 32-byte words an eth_call returns, or None when it reverts."""
    try:
        return words(cast('call', to, sig, *args, *(['--from', sender] if sender else []), '--rpc-url', RPC, timeout=20))
    except (RuntimeError, ValueError):
        return None


def word_addr(n):
    return '0x' + f'{n:064x}'[-40:]


def decode_launch(log):
    """A launch log -> {'track', 'token', 'creator', 'quote', 'market', 'pad', 'tx', 'block', 'ts'}; None when not ours."""
    t = [x.lower() for x in log.get('topics') or []]
    track = LAUNCH_TOPICS.get(t[0]) if t else None
    data = words(log.get('data') or '0x')
    c = {'track': track, 'tx': log.get('transactionHash'), 'block': int(log.get('blockNumber') or '0x0', 16),
         'ts': int(log.get('blockTimestamp') or '0x0', 16) or None}
    if track == 'gang' and len(t) >= 4:
        c.update(token='0x' + t[1][-40:], creator='0x' + t[2][-40:], quote='0x' + t[3][-40:], pad=log['address'])
    elif track == 'four' and len(t) >= 4 and int(t[1], 16) == FOUR_AGENT_PRESET:
        c.update(creator='0x' + t[2][-40:], token='0x' + t[3][-40:])
    elif track == 'flap' and len(data) >= 2 and word_addr(data[1]).lower() == AGENT_ID_REGISTRY.lower():
        c.update(token=word_addr(data[0]))
    elif track == 'ac' and len(t) >= 4:
        c.update(creator='0x' + t[1][-40:], market='0x' + t[2][-40:], token='0x' + t[3][-40:])
    else:
        return None
    return c


def launch_loop():
    """Poll the launchpads for new curves about once a second while sniping is on."""
    while True:
        if not load_prefs()['snipe']['on']:
            WATCH['cursor'] = None
            time.sleep(5)
            continue
        try:
            poll_launches()
        except Exception as e:  # noqa: BLE001 - public nodes hiccup; try again next round
            logging.warning('launch poll: %s', e)
            time.sleep(2)
        time.sleep(1)


def poll_launches():
    head = int(rpc('eth_blockNumber', []), 16) - 1  # one block back: the log node is often a block behind
    cur = WATCH['cursor']
    if cur is None or head - cur > 9000:  # (re)start: only launches from now on
        cur = WATCH['cursor'] = head - 1
    if head <= cur:
        return
    to = min(head, cur + 5000)
    flt = {'fromBlock': hex(cur + 1), 'toBlock': hex(to), 'address': LAUNCH_ADDRS, 'topics': [list(LAUNCH_TOPICS)]}
    try:
        logs = rpc('eth_getLogs', [flt], LOGS_RPC)
    except RuntimeError as e:
        m = re.search(r'head (\d+)', str(e))  # "block range extends beyond current head block"
        if not m or int(m.group(1)) <= cur:
            raise
        to = int(m.group(1))
        flt['toBlock'] = hex(to)
        logs = rpc('eth_getLogs', [flt], LOGS_RPC)
    WATCH['cursor'] = to
    for log in logs or []:
        c = decode_launch(log)
        if c:
            run_bg(lambda c=c: on_launch(c))


def local_day():
    return time.strftime('%Y-%m-%d', time.gmtime(time.time() + TZ_HOURS * 3600))


def load_snipe():
    try:
        st = json.loads(SNIPE_FILE.read_text())
    except (OSError, ValueError):
        st = {}
    if st.get('day') != local_day():
        st.update(day=local_day(), bnb='0', tokens=0)
    cutoff = time.time() - 3 * 86400
    st['done'] = {k: v for k, v in (st.get('done') or {}).items() if v > cutoff}
    st['positions'] = [p for p in st.get('positions') or [] if p.get('open') or p['time'] > cutoff - 4 * 86400]
    return st


def save_snipe(st):
    write_json(SNIPE_FILE, st)


def log_snipe(c, **fields):
    try:
        with SNIPE_LOG.open('a') as f:
            f.write(json.dumps({'time': int(time.time()), **{k: c.get(k) for k in ('track', 'token', 'market', 'tx')},
                                **fields}, ensure_ascii=False, default=str) + '\n')
    except OSError:
        logging.exception('snipe log')


def approx_cost(quote, target):
    """BNB (wei) that buys about target tokens, given quote(wei) -> tokens; proportional steps."""
    v = 10 ** 16
    for _ in range(6):
        out = quote(v)
        if not out:
            raise UsageError(L('No quote', '报不出价'))
        nv = v * target // out
        if abs(nv - v) * 500 < v:
            return nv
        v = nv
    return v


def launch_cost(c, w, pct):
    """(BNB wei one wallet pays for pct% of supply, % of the curve already sold or None)."""
    addr, target = WALLETS[w]['addr'], SUPPLY * int(Decimal(pct) * 100) // 10000
    if c['track'] == 'gang':
        cv = call_words(c['pad'], 'curves(address)', c['token'])
        if not cv or len(cv) < 8:
            raise UsageError(L('Cannot read curve', '读不到曲线'))
        quote, real_q, sold, grad_amt, vq, graduated = word_addr(cv[1]), cv[2], cv[3], cv[4], cv[5], cv[6]
        if graduated:
            raise UsageError(L('Already graduated', '已经毕业'))
        if quote.lower() != WBNB.lower():
            raise UsageError(L('Quote token is not BNB', '计价币不是 BNB'))
        room = (call_words(c['pad'], 'walletRoom(address,address)', c['token'], addr) or [target])[0]
        tokens = min(target, room)
        if tokens <= 0:
            raise UsageError(L('This wallet hit its buy cap', '这个钱包已经买满'))
        vt = 1066666666666666666666666667  # Gang's virtual token reserve
        q_r, t_r = vq + real_q, vt - sold
        net = -(-(vq * vt) // (t_r - tokens)) - q_r
        net = min(net, grad_amt - real_q)
        gross = -(-(net * 10000) // 9900)  # 1% platform fee
        return gross, Decimal(sold) * 100 / SUPPLY
    if c['track'] == 'flap':
        st = call_words(FLAP_PORTAL, 'getTokenV8Safe(address)', c['token'])
        if not st or len(st) < 16:
            raise UsageError(L('Cannot read Flap state', '读不到 Flap 状态'))
        if st[0] != 1:
            raise UsageError(L('Not on the curve', '不在内盘'))
        if st[9]:
            raise UsageError(L('Quote token is not BNB', '计价币不是 BNB'))
        used, cap = (call_words(FLAP_PORTAL, 'buyQuotaOf(address,address)', c['token'], addr) or [0, target])[:2]
        quota = cap - used
        if quota <= 0:
            raise UsageError(L('This wallet hit its buy cap', '这个钱包已经买满'))
        q = lambda v: (call_words(FLAP_PORTAL, 'quoteExactInput((address,address,uint256))',  # noqa: E731
                                  f"({ZERO_ADDR},{c['token']},{v})") or [0])[0]
        return approx_cost(q, min(target, quota)), Decimal(st[15]) / 10 ** 16
    if c['track'] == 'ac':
        q = lambda v: ((call_words(AC_PORTAL, 'quoteBuy(address,address,uint256)', c['market'], addr, str(v))  # noqa: E731
                        or [0, 0, 0, 0])[3])
        return approx_cost(q, target), None
    q = call_words(FOUR_TOOLS, 'estimateBuy(address,address,uint256,uint256,bytes)', c['token'], addr, str(target), '0', '0x')
    if not q or len(q) < 5 or not q[2]:
        raise UsageError(L("No quote (wallet has no Agent ID, or this launch doesn't take BNB)", '报不出价（钱包没有 Agent ID，或这盘不收 BNB）'))
    return q[2], None


def on_launch(c):
    s = load_prefs()['snipe']
    if not c.get('ts') and c.get('block'):  # some log nodes leave out blockTimestamp
        c['ts'] = safe(lambda: int(rpc('eth_getBlockByNumber', [hex(c['block']), False])['timestamp'], 16))
    c['delay'] = round(time.time() - c['ts'] + 0.5, 1) if c.get('ts') else None
    with SNIPE_LOCK:
        st = load_snipe()
        if c['token'].lower() in st['done']:
            return
        st['done'][c['token'].lower()] = time.time()
        save_snipe(st)
    sym = safe(lambda: token_names(c['token'])[0]) or '?'
    why, plan, cost, sold = snipe_decide(c, s)
    log_snipe(c, symbol=sym, delay=c['delay'], cost=cost and str(from_wei(cost)), sold=sold and str(sold),
              skip=why, plan={w: str(a) for w, a in (plan or {}).items()}, live=s['live'])
    facts = [L(f"delay {fmt(c['delay'], 1)}s", f"延迟 {fmt(c['delay'], 1)} 秒") if c['delay'] is not None else '',
             L(f"{'Grad progress' if c['track'] == 'flap' else 'Sold'} {fmt(sold, 1)}%", f"{'毕业进度' if c['track'] == 'flap' else '已卖'} {fmt(sold, 1)}%") if sold is not None else '',
             L(f"Buy {s['pct']}% ≈ {fmt(from_wei(cost), 4)} BNB", f"买 {s['pct']}% 约 {fmt(from_wei(cost), 4)} BNB") if cost else '']
    lines = [L(f"🆕 <b>New launch | {TRACK_NAME[c['track']]}</b>  {esc(sym)}  <code>{c['token']}</code>", f"🆕 <b>新盘｜{TRACK_NAME[c['track']]}</b>　{esc(sym)}　<code>{c['token']}</code>"),
             L(' | ', '｜').join(x for x in facts if x) or L('  ', '　')]
    if why:
        lines.append(L(f'⏭ Skip: {esc(why)}', f'⏭ 不买：{esc(why)}'))
        say('\n'.join(lines), [[cmd_btn(L('🪙 Open card', '🪙 开卡片'), f"/buy {c['token']}")]], rich=True)
        return
    each_txt = L(', ', '、').join(f'{w} {fmt(from_wei(a), 4)}' for w, a in plan.items())
    if not s['live']:
        lines.append(L(f'🧪 Sim: would buy with {each_txt} BNB (no order)', f'🧪 模拟：会用 {each_txt} BNB 买（没下单）'))
        say('\n'.join(lines), [[cmd_btn(L('🪙 Open card', '🪙 开卡片'), f"/buy {c['token']}")]], rich=True)
        return
    lines.append(L(f'⚡ Auto-buy: {each_txt} BNB', f'⚡ 自动买：{each_txt} BNB'))
    say('\n'.join(lines), rich=True)
    snipe_buy(c, plan)


def snipe_decide(c, s):
    """(why not, {wallet: wei}, cost per wallet, sold %). Reserves today's budget when it says yes."""
    if c['track'] not in s['tracks']:
        return L('Sniping is off for this platform', '这个平台没开狙击'), None, None, None
    if c['delay'] is None:  # could be a launch from hours ago replayed after the node came back
        return L('No block time; may be an old launch', '查不到出块时间，不知道是不是旧盘'), None, None, None
    if c['delay'] > s['max_delay']:
        return L(f"Spotted too late ({fmt(c['delay'], 1)}s, limit {s['max_delay']}s)", f"发现太晚（{fmt(c['delay'], 1)} 秒，上限 {s['max_delay']} 秒）"), None, None, None
    ws = wallet_list(s['wallets']) or list(WALLETS)
    pct = min(dec(s['pct']), Decimal(TRACK_CAP[c['track']]))
    try:
        cost, sold = launch_cost(c, ws[0], fmt(pct, 2))
    except UsageError as e:
        return str(e), None, None, None
    if sold is not None and sold > s['max_sold']:
        return L(f"Already {fmt(sold, 1)}% bought (limit {s['max_sold']}%)", f"已经被买走 {fmt(sold, 1)}%（上限 {s['max_sold']}%）"), None, cost, sold
    cap = to_wei(min(dec(s['max_bnb']), MAX_BNB))
    amt = min(cost * 105 // 100, cap)  # 5% buffer: someone in the same block may buy first; extra is refunded
    with SNIPE_LOCK:
        st = load_snipe()
        if st['tokens'] >= s['day_tokens']:
            return L(f"Already sniped {st['tokens']} today (limit {s['day_tokens']})", f"今天已经狙了 {st['tokens']} 个（上限 {s['day_tokens']}）"), None, cost, sold
        spend = from_wei(amt * len(ws))
        if dec(st['bnb']) + spend > dec(s['day_bnb']):
            return L(f"Daily quota too low (used {fmt(st['bnb'], 4)}/{s['day_bnb']} BNB)", f"今天额度不够（已用 {fmt(st['bnb'], 4)}／{s['day_bnb']} BNB）"), None, cost, sold
        if s['live']:
            st['tokens'] += 1
            st['bnb'] = str(dec(st['bnb']) + spend)
            save_snipe(st)
    return None, {w: amt for w in ws}, cost, sold


def snipe_buy(c, plan):
    """Buy straight away (no confirm: the owner switched sniping to live) and remember the positions for auto-sell."""
    token, prefs = c['token'], load_prefs()
    cv = detect_curve(token)

    def one(w):
        if not cv or cv['state'] != 'curve':
            return False, L("Can't read the curve state", '读不到内盘状态')
        try:
            return auto_run(w, lambda: curve_buy_steps(cv, token, w, plan[w]))
        except UsageError as e:
            return False, str(e)
    results = each(list(plan), one)
    sym = safe(lambda: token_names(token)[0]) or short(token)
    unused = 0
    with SNIPE_LOCK:
        st = load_snipe()
        for w, (ok, res) in zip(plan, results):
            if ok or UNKNOWN_MARK in res:  # an unknown result may be on chain: keep the budget spent and watch it
                st['positions'].append({'token': token, 'symbol': sym, 'wallet': w, 'cost': str(plan[w]),
                                        'time': int(time.time()), 'tp_done': False, 'fails': 0, 'open': True,
                                        **({} if ok else {'unknown': True})})
            else:
                unused += plan[w]
        if unused:
            st['bnb'] = str(max(dec(st['bnb']) - from_wei(unused), Decimal(0)))
            if not any(ok for ok, _ in results):
                st['tokens'] = max(st['tokens'] - 1, 0)
        save_snipe(st)
    for w, (ok, res) in zip(plan, results):
        log_snipe(c, wallet=w, ok=ok, result=res)
        if ok:
            say(L(f'⚡ <b>{w}</b> sniped {esc(sym)} ({fmt(from_wei(plan[w]), 4)} BNB)  {tx_link(res)}', f'⚡ <b>{w}</b> 狙到 {esc(sym)}（{fmt(from_wei(plan[w]), 4)} BNB）　{tx_link(res)}'), rich=True)
            extra = after_buy_send(w, token, prefs) if prefs['auto_send'] != 'off' else ''
            if extra:
                say(extra)
        elif UNKNOWN_MARK in res:
            say(L(f'⚠️ <b>{w}</b> snipe {esc(sym)}: {esc(res)}\nTracked as a position anyway; if nothing was bought on chain, it closes next round.', f'⚠️ <b>{w}</b> 狙 {esc(sym)} {esc(res)}\n先记成持仓照样自动盯；链上没买到的话，下一轮会自己结束。'), rich=True)
        else:
            say(L(f'❌ <b>{w}</b> missed {esc(sym)}: {esc(res)}', f'❌ <b>{w}</b> 没狙到 {esc(sym)}：{esc(res)}'), rich=True)


def tp_multiple(t):
    x = max(dec(t), Decimal(0))
    if 0 < x <= 1:
        raise UsageError(L('TP multiple must be above 1 (e.g. 1.5, 2); 0 turns TP off.', '止盈倍数要大于 1（例如 1.5、2）；打 0 是关掉止盈。'))
    return fmt(x, 2)


SNIPE_PROMPTS = {  # field -> (question, parse(text) -> value)
    'pct': (L('What % of supply should each wallet buy? (Gang, Four max 1; Flap max 2) e.g. 1', '每个钱包买总量的几 %？（Gang、Four 最多 1，Flap 最多 2）例如 1'), lambda t: fmt(min(max(dec(t), Decimal('0.01')), 2), 2)),
    'max_bnb': (L(f'Max BNB per wallet per new launch? (no more than {fmt(MAX_BNB)}) e.g. 0.05', f'每个钱包每个新盘最多花多少 BNB？（不能超过 {fmt(MAX_BNB)}）例如 0.05'),
                lambda t: fmt(min(need_amount(t), MAX_BNB))),
    'day_bnb': (L('Max BNB per day, all wallets combined? e.g. 0.3', '所有钱包加起来，一天最多花多少 BNB？例如 0.3'), lambda t: fmt(need_amount(t, cap=False))),
    'day_tokens': (L('Max new launches to snipe per day? e.g. 5', '一天最多狙几个新盘？例如 5'), lambda t: max(0, int(dec(t)))),
    'max_delay': (L('Skip if spotted more than how many seconds after the block? e.g. 8', '出块后超过几秒才发现就不买？例如 8'), lambda t: max(1, int(dec(t)))),
    'max_sold': (L('Skip if more than what % of the curve is already sold? e.g. 15', '发现时曲线已被买走超过几 % 就不买？例如 15'), lambda t: max(0, min(80, int(dec(t))))),
    'tp_x': (L('Sell part at what multiple of the buy price? e.g. 2 (above 1; 0 = off)', '涨到买价的几倍就先卖一部分？例如 2（要大于 1；打 0 关掉）'), lambda t: tp_multiple(t)),
    'tp_sell': (L('Sell what % at that multiple? e.g. 50', '到倍数时卖掉几 %？例如 50'), lambda t: fmt(min(max(dec(t), Decimal(1)), 100), 0)),
    'sl': (L('Sell everything after a drop of what %? e.g. 50 (0 = off)', '跌掉几 % 就全部卖掉？例如 50（打 0 关掉）'), lambda t: fmt(min(max(dec(t), Decimal(0)), 95), 0)),
}


def snipe_text(s):
    st = load_snipe()
    tracks = L(', ', '、').join(TRACK_NAME[t] for t in TRACK_NAME if t in s['tracks']) or L('none on', '都没开')
    mode = L('⚡ Live orders', '⚡ 真的下单') if s['live'] else L('🧪 Simulation (only reports what it would buy)', '🧪 模拟（只通知会买多少，不下单）')
    return '\n'.join([
        L('⚡ <b>New launch sniper</b>', '⚡ <b>新盘狙击</b>'), '',
        L(f"Status  {'🟢 Listening' if s['on'] else '⚪ Off'} | {mode}", f"状态　{'🟢 监听中' if s['on'] else '⚪ 关闭'}｜{mode}"),
        L(f'Platforms  {tracks}', f'平台　{tracks}'),
        L(f"Per wallet  buy {s['pct']}% | max {s['max_bnb']} BNB | wallets {''.join(wallet_list(s['wallets']) or WALLETS)}", f"每个钱包　买 {s['pct']}%｜最多 {s['max_bnb']} BNB｜钱包 {''.join(wallet_list(s['wallets']) or WALLETS)}"),
        L(f"Daily cap  {s['day_bnb']} BNB, {s['day_tokens']} tokens", f"每天上限　{s['day_bnb']} BNB、{s['day_tokens']} 个"),
        L(f"Skip  spotted after {s['max_delay']}s | over {s['max_sold']}% sold | quote not BNB", f"不买　发现晚于 {s['max_delay']} 秒｜已被买走超过 {s['max_sold']}%｜计价币不是 BNB"),
        L(f"Today  used {fmt(st['bnb'], 4)} BNB, {st['tokens']} tokens", f"今天　已用 {fmt(st['bnb'], 4)} BNB、{st['tokens']} 个"),
        L(f"Auto-sell  {'at ' + s['tp_x'] + 'x sell ' + s['tp_sell'] + '%' if dec(s['tp_x']) > 0 else 'TP off'}"
        f" | {'down ' + s['sl'] + '% sell all' if dec(s['sl']) > 0 else 'SL off'} | {len(open_positions(st))} open positions", f"自动卖　{'涨到 ' + s['tp_x'] + ' 倍卖 ' + s['tp_sell'] + '%' if dec(s['tp_x']) > 0 else '止盈关'}"
        f"｜{'跌 ' + s['sl'] + '% 全卖' if dec(s['sl']) > 0 else '止损关'}｜持仓 {len(open_positions(st))} 笔"),
        '', L('After buying, checks the on-chain quote every 20s and sells when hit, without asking', '买到后每 20 秒用链上报价看一次，到了直接卖、不会再问你')])


def snipe_buttons(s):
    def b(label, arg):
        return {'text': label, 'callback_data': f'n:{arg}'}
    rows = [[b(L('⏹ Stop listening', '⏹ 停止监听') if s['on'] else L('▶️ Start listening', '▶️ 开始监听'), 'on'),
             b(L('🧪 Switch to sim', '🧪 改成模拟') if s['live'] else L('⚡ Go live', '⚡ 改成真的下单'), 'live')],
            [b(f"{'✅' if t in s['tracks'] else '⬜'} {TRACK_NAME[t]}", 't.' + t) for t in ('gang', 'flap')],
            [b(f"{'✅' if t in s['tracks'] else '⬜'} {TRACK_NAME[t]}", 't.' + t) for t in ('ac', 'four')],
            [b(L(f"Buy {s['pct']}% ✏️", f"买 {s['pct']}% ✏️"), 'e.pct'), b(L(f"{s['max_bnb']} BNB each ✏️", f"单笔 {s['max_bnb']} ✏️"), 'e.max_bnb'),
             b(L(f"{s['day_bnb']} BNB/day ✏️", f"每天 {s['day_bnb']} ✏️"), 'e.day_bnb')],
            [b(L(f"{s['day_tokens']}/day ✏️", f"每天 {s['day_tokens']} 个 ✏️"), 'e.day_tokens'), b(L(f"{s['max_delay']}s ✏️", f"{s['max_delay']} 秒 ✏️"), 'e.max_delay'),
             b(L(f"Sold {s['max_sold']}% ✏️", f"买走 {s['max_sold']}% ✏️"), 'e.max_sold')],
            [b(L(f"TP {s['tp_x']}x ✏️", f"止盈 {s['tp_x']} 倍 ✏️"), 'e.tp_x'), b(L(f"Sell {s['tp_sell']}% ✏️", f"卖 {s['tp_sell']}% ✏️"), 'e.tp_sell'),
             b(L(f"SL -{s['sl']}% ✏️", f"止损 -{s['sl']}% ✏️"), 'e.sl')],
            [b(L(f"👛 Wallets: {''.join(wallet_list(s['wallets']) or WALLETS)} 🔁", f"👛 钱包：{''.join(wallet_list(s['wallets']) or WALLETS)} 🔁"), 'w'), b(L('🔄 Refresh', '🔄 刷新'), 'r')]]
    return rows


def cmd_snipe(args):
    s = load_prefs()['snipe']
    say(snipe_text(s), snipe_buttons(s), rich=True)


def set_snipe(**changes):
    p = load_prefs()
    p['snipe'] = {**p['snipe'], **changes}
    save_prefs(p)
    return p['snipe']


def on_snipe(data, msg):
    arg = data.split(':', 1)[1]
    s = load_prefs()['snipe']
    where = (msg['chat']['id'], msg['message_id']) if msg else None
    if arg == 'live' and not s['live']:
        ask_confirm(L('⚡ Switch to <b>live orders</b>?\nMatching new launches will be <b>bought with your wallets right away</b>, without asking.\n'
                    f"Max {s['max_bnb']} BNB per wallet per launch; max {s['day_bnb']} BNB and {s['day_tokens']} tokens per day.", '⚡ 改成<b>真的下单</b>？\n之后符合条件的新盘会<b>直接用你的钱包买</b>，不会再问你。\n'
                    f"每个钱包每盘最多 {s['max_bnb']} BNB、每天最多 {s['day_bnb']} BNB、{s['day_tokens']} 个。"),
                    L('Confirm going live', '确认改成真的下单'), lambda: (set_snipe(live=True), say(L('⚡ Now placing live orders.', '⚡ 已改成真的下单。'))), rich=True)
        return
    if arg == 'on':
        s = set_snipe(on=not s['on'])
    elif arg == 'live':
        s = set_snipe(live=False)
    elif arg.startswith('t.') and arg[2:] in TRACK_NAME:
        t = arg[2:]
        s = set_snipe(tracks=[x for x in s['tracks'] if x != t] if t in s['tracks'] else s['tracks'] + [t])
    elif arg == 'w':
        s = set_snipe(wallets=''.join(next_wallets(wallet_list(s['wallets']) or list(WALLETS))))
    elif arg.startswith('e.') and arg[2:] in SNIPE_PROMPTS:
        ask_input('snipe', 'snp:' + arg[2:], SNIPE_PROMPTS[arg[2:]][0])
        return
    edit_or_send(where, snipe_text(s), snipe_buttons(s), rich=True)


# ---------------------------------------------------------------- launch curves: trading before (and right after) graduation

FOUR_TOOLS = '0x97da82f56fef1a0b974587a48135fa9da2a4e905'  # OpenFour quotes
FOUR_TM2 = '0x5c952063c7fc8610FFDB798152D69F0B9550762b'  # four.meme TokenManager2
FOUR_HELPER = '0xF251F83e40a78868FcfA3FA4599Dad6494E46034'  # four.meme TokenManagerHelper3
GANG_OLD_PAD = '0x315Ba2e8952D06094CC636b2B84229FA32fb23A7'
GANG_PADS = (GANG_V2_PAD, '0xF81C4E51c48466c6025B0165b1Ea0F6978c712cb', '0x60aee38576035E757deD30fF8db8A8DA476430C1',
             GANG_V3_PAD)  # V3 curves: same formula, buy/sell and walletRoom as V2; curves() has 12 fields
GANG_VT = 1066666666666666666666666667  # Gang's virtual token reserve
AC_READER = '0x18Aa6C93f1887a243fdc348C9A77F5136555174a'
PANCAKE_ROUTER = '0x10ED43C718714eb63d5aA57B78B54704E256024E'
CURVE_NAME = {'four_agent': L('Four exclusive', 'Four 专属'), 'four': 'Four', 'gang_old': 'Gang', 'gang': 'Gang', 'flap': 'Flap',
              'ac': 'Flap Agent Curve', 'dex': 'PancakeSwap'}
DEX_CV = {'kind': 'dex', 'name': 'PancakeSwap', 'state': 'dex', 'to': PANCAKE_ROUTER, 'quote': None, 'progress': None}
CURVE_ERRORS = {  # revert data / messages of the launchpads -> plain words
    '0x96ea0b74': L('No Agent ID', '没有 Agent ID'), '0x0d9ab13f': L('No Agent ID', '没有 Agent ID'), 'Agent ID required': L('No Agent ID', '没有 Agent ID'),
    '0xf38fb4a7': L('Gated launches need an Agent ID', '门槛盘要有 Agent ID'), '0xe8c402c1': L('This wallet hit its buy cap', '这个钱包已买满上限'), '0x687d0d11': L('This wallet hit its buy cap', '这个钱包已买满上限'),
    '0xc866db66': L('Over the per-wallet buy cap', '超过每个钱包的购买上限'), '0xd15b1d74': L("Budget can't buy (no Agent ID or cap reached)", '这笔预算买不到（没有 Agent ID 或已买满）'),
    '0xc4e60bf5': L('Not in the curve trading phase', '不在内盘交易阶段'), '0xe6a0d45f': L('Already graduated', '已经毕业了'), 'buy quote': L("Already graduated, can't buy on the curve", '已经毕业了，不能在内盘买'),
    'NO_SHARES': L('No shares', '没有份额'), '0x09d703a0': L('Price moved too fast (slippage)', '价格变太快（滑点）'), '0x76baadda': L('Price moved too fast (slippage)', '价格变太快（滑点）'),
    'Disabled': L('Graduated or trading stopped', '已经毕业或停止交易')}
POS_FAILS = 3  # stop watching a sniped position after this many failed auto-sells in a row


def curve_slip():
    s = load_prefs()['slippage']
    return Decimal('0.1') if s == 'auto' else min(dec(s), Decimal(49)) / 100


def slip_min(x):
    return int(Decimal(x) * (1 - curve_slip()))


def detect_curve(token):
    """Which launch curve token is on, read on chain: {'kind', 'name', 'state': 'curve' | 'dex' | 'wait', 'to': the
    contract to trade with, 'quote': None for BNB else the quote token, 'progress': % to graduation, …}.
    None when it is none of them (an ordinary token: Binance market orders handle it)."""
    t = token
    jobs = [lambda: call_words(FOUR_CORE, 'tokens(address)', t), lambda: call_words(FOUR_HELPER, 'getTokenInfo(address)', t),
            lambda: call_words(GANG_OLD_PAD, 'curves(address)', t)]
    jobs += [lambda p=p: call_words(p, 'curves(address)', t) for p in GANG_PADS]
    jobs += [lambda: call_words(FLAP_PORTAL, 'getTokenV8Safe(address)', t),
             lambda: call_words(FLAP_PORTAL, 'nftGate(address)', t), lambda: call_words(AC_FACTORY, 'marketOfToken(address)', t),
             lambda: call_words(GANG_V3_PAD, 'kindOf(address)', t)]
    fa, four, old, *gangs, flap, gate, ac, v3kind = each(jobs, lambda f: f())
    pct = lambda a, b: Decimal(a) * 100 / b if b else None  # noqa: E731

    def cv(kind, state, to, quote, progress, **extra):
        q = word_addr(quote) if quote else None
        q = None if q and q.lower() == WBNB.lower() else q
        return {'kind': kind, 'name': CURVE_NAME[kind], 'state': state, 'to': to, 'quote': q, 'progress': progress, **extra}
    if fa and len(fa) >= 19 and fa[17] and fa[2] == FOUR_AGENT_PRESET:
        phase = word(t, 'tokenPhase()')
        raised = word(word_addr(fa[10]), 'totalRaised()') if fa[10] else None
        state = 'curve' if phase == 1 and not fa[18] else 'dex' if phase == 3 else 'wait'
        return cv('four_agent', state, FOUR_CORE, fa[9], pct(raised, fa[8]) if raised is not None else None)
    if four and len(four) >= 12 and four[1] and four[0] == 2:
        return cv('four', 'dex' if four[11] else 'curve', word_addr(four[1]), four[2], pct(four[9], four[10]))
    if old and len(old) >= 5 and old[0]:
        return cv('gang_old', 'dex' if old[3] else 'curve', GANG_OLD_PAD, 0, pct(old[1], 20 * 10 ** 18))
    if v3kind and v3kind[0] == 2 and not pancake_out(10 ** 15, [WBNB, t]):  # fair mint: fixed-price subscription
        return cv('gang', 'wait', GANG_V3_PAD, 0, None, name='Gang V3', fair=True)
    for pad, g in zip(GANG_PADS, gangs):
        if g and len(g) >= 8 and g[0] and (pad != GANG_V3_PAD or (v3kind and v3kind[0] == 1)):
            v3 = {'name': 'Gang V3', 'tax': tuple(g[7:10])} if pad == GANG_V3_PAD and len(g) >= 10 else {}
            return cv('gang', 'dex' if g[6] else 'curve', pad, g[1], pct(g[2], g[4]),
                      wrap=word_addr(g[1]).lower() == WBNB.lower(), **v3)
    if ac and ac[0]:  # before Flap: a graduated Agent Curve token is a Flap token too, but its shares need a claim
        market = word_addr(ac[0])
        d = call_words(AC_READER, 'marketDetail(address)', market)
        if d and len(d) >= 16:
            return cv('ac', {1: 'curve', 3: 'dex'}.get(d[8], 'wait'), AC_PORTAL, d[2], Decimal(d[7]) / 100,
                      market=market, real=word_addr(d[10]) if d[10] else None)
    if flap and len(flap) >= 16:
        state = 'curve' if flap[0] == 1 else 'dex' if flap[0] == 4 else 'wait'
        return cv('flap', state, FLAP_PORTAL, flap[9], Decimal(flap[15]) / 10 ** 16,
                  name=L('Flap gated', 'Flap 门槛盘') if gate and gate[0] else 'Flap')
    return None


def curve_stage(cv):
    if cv.get('fair'):
        return L('Gang V3 fair launch (fixed-price subscription, not a curve), not on DEX yet; subscribe at gang.fun', 'Gang V3 打新币（固定价认购，不是曲线），还没上外盘；认购请到 gang.fun')
    if cv['state'] == 'curve':
        return L(f"{cv['name']} on curve", f"{cv['name']} 内盘") + (f" {fmt(cv['progress'], 1)}%" if cv.get('progress') is not None else '')
    return L(f"{cv['name']} graduated", f"{cv['name']} 已毕业") if cv['state'] == 'dex' else L(f"{cv['name']} moving to DEX", f"{cv['name']} 正在上外盘")


def pancake_out(amount, path):
    r = call_words(PANCAKE_ROUTER, 'getAmountsOut(uint256,address[])', str(amount), '[' + ','.join(path) + ']')
    return r[-1] if r and len(r) >= 2 + len(path) else 0


def deadline():
    return str(int(time.time()) + 1200)


def gang_buy_quote(pad, token, addr, wei):
    """(WBNB to spend, tokens out) for up to wei on a Gang V2 curve, kept inside the wallet's 1% room."""
    g = call_words(pad, 'curves(address)', token)
    if not g or len(g) < 8 or not g[0]:
        raise UsageError(L("Can't read the Gang curve", '读不到 Gang 曲线'))
    if g[6]:
        raise UsageError(L('Already graduated', '已经毕业了'))
    if not word(pad, 'isAgent(address)', addr):
        raise UsageError(L('No Agent ID', '没有 Agent ID'))
    real_q, sold, grad, vq = g[2], g[3], g[4], g[5]
    room = word(pad, 'walletRoom(address,address)', token, addr) or 0
    if not room:
        raise UsageError(L('This wallet already bought its 1%', '这个钱包已买满 1%'))
    k, q_r, t_r = vq * GANG_VT, vq + real_q, GANG_VT - sold
    net = min(wei - wei * 100 // 10000, grad - real_q)
    out = t_r - -(-k // (q_r + net))
    if out > room:  # stay just under the cap rather than rely on how the pad treats the overflow
        out = room * 999 // 1000
        net = -(-k // (t_r - out)) - q_r
    return min(wei, -(-(net * 10000) // 9900)), out


def curve_buy_steps(cv, token, w, wei):
    """([(to, value wei, data, note)], final) that spend up to wei BNB of wallet w on token. final=False means the
    calls only prepare (Gang: wrap BNB to WBNB and approve) and the buy comes once they are mined.
    Raises UsageError with the reason when it can't buy."""
    addr, kind, sym = WALLETS[w]['addr'], cv['kind'], token_label(token)[0]

    def got(spend, n, unit=sym):
        return L(f'{fmt(from_wei(spend))} BNB → ~{cn_num(from_wei(n))} {unit} (min {cn_num(from_wei(slip_min(n)))})', f'{fmt(from_wei(spend))} BNB → 约 {cn_num(from_wei(n))} {unit}（最少 {cn_num(from_wei(slip_min(n)))}）')
    if cv['state'] == 'dex':
        path = [WBNB] + ([cv['quote']] if cv.get('quote') else []) + [cv.get('real') or token]
        out = pancake_out(wei, path)
        if not out:
            raise UsageError(L('No quote from PancakeSwap', 'PancakeSwap 报不出价'))
        data = cast('calldata', 'swapExactETHForTokensSupportingFeeOnTransferTokens(uint256,address[],address,uint256)',
                    str(slip_min(out)), '[' + ','.join(path) + ']', addr, deadline())
        return [(PANCAKE_ROUTER, wei, data, got(wei, out) + L(' | direct via PancakeSwap', '｜直连 PancakeSwap'))], True
    if cv['state'] != 'curve':
        raise UsageError(curve_stage(cv) if cv.get('fair') else L('Moving from curve to DEX, try again in a few minutes', '正在从内盘搬到外盘，等几分钟再试'))
    if cv.get('quote') and kind not in ('four', 'flap'):
        raise UsageError(L(f"Quote token is {token_label(cv['quote'])[0]}; this launch type only supports BNB", f"计价币是 {token_label(cv['quote'])[0]}，这种盘目前只支持 BNB 计价"))
    if kind == 'four_agent':
        q = call_words(FOUR_TOOLS, 'estimateBuyByBudget(address,address,uint256,uint256,bytes)', token, addr, str(wei),
                       '0', '0x')
        if not q or len(q) < 5 or not q[4]:
            raise UsageError(L("Can't buy (no Agent ID, or 1% cap reached)", '买不到（没有 Agent ID，或已买满 1%）'))
        data = cast('calldata', 'buyByBudget(address,uint256,uint256,uint256,bytes)', token, str(wei),
                    str(slip_min(q[4])), '0', '0x')
        return [(FOUR_CORE, wei, data, got(q[2] or wei, q[4]))], True
    if kind == 'four':
        funds = pancake_out(wei, [WBNB, cv['quote']]) if cv['quote'] else wei
        q = call_words(FOUR_HELPER, 'tryBuy(address,uint256,uint256)', token, '0', str(funds)) if funds else None
        if not q or len(q) < 8 or not q[2]:
            raise UsageError(L('No quote', '报不出价'))
        if cv['quote']:  # Helper3 swaps the BNB into the quote token itself
            data = cast('calldata', 'buyWithEth(uint256,address,address,uint256,uint256)', '0', token, addr, str(wei),
                        str(slip_min(q[2])))
            return [(FOUR_HELPER, wei, data, got(wei, q[2]))], True
        data = cast('calldata', 'buyTokenAMAP(address,uint256,uint256)', token, str(q[7]), str(slip_min(q[2])))
        return [(cv['to'], q[5], data, got(q[5], q[2]))], True
    if kind == 'gang_old':
        q = call_words(GANG_OLD_PAD, 'quoteBuy(address,address,uint256)', token, addr, str(wei))
        if not q or len(q) < 4 or not q[0]:
            raise UsageError(L("Can't buy (1% cap reached)", '买不到（已买满 1%）'))
        data = cast('calldata', 'buy(address,uint256)', token, str(slip_min(q[0])))
        note = got(wei, q[0]) + (L(f' | capped, extra {fmt(from_wei(q[3]))} BNB is refunded', f'｜到上限，多的 {fmt(from_wei(q[3]))} BNB 会退回') if q[3] else '')
        return [(GANG_OLD_PAD, wei, data, note)], True
    if kind == 'gang':
        spend, out = gang_buy_quote(cv['to'], token, addr, wei)
        have = word(WBNB, 'balanceOf(address)', addr) or 0
        ready = have >= spend and (word(WBNB, 'allowance(address,address)', addr, cv['to']) or 0) >= spend
        if cv['to'] == GANG_V3_PAD and not ready:  # the V3 router wraps native BNB itself: one transaction
            data = cast('calldata', 'v3BuyBNB(address,uint256,uint256)', token, str(slip_min(out)), deadline())
            return [(GANG_V3_ROUTER, spend, data, got(spend, out) + L(' | via Gang V3 router, pays BNB directly', '｜经 Gang V3 路由，直接付 BNB'))], True
        prep = []
        if have < spend:
            prep.append((WBNB, spend - have, cast('calldata', 'deposit()'),
                         L(f'Wrap {fmt(from_wei(spend - have))} BNB to WBNB (Gang takes WBNB)', f'把 {fmt(from_wei(spend - have))} BNB 换成 WBNB（Gang 盘收 WBNB）')))
        if (word(WBNB, 'allowance(address,address)', addr, cv['to']) or 0) < spend:
            prep.append((WBNB, 0, cast('calldata', 'approve(address,uint256)', cv['to'], str(spend)),
                         L(f'Approve Gang to spend {fmt(from_wei(spend))} WBNB', f'允许 Gang 拿走 {fmt(from_wei(spend))} WBNB')))
        if prep:
            return prep, False
        data = cast('calldata', 'buy(address,uint256,uint256)', token, str(spend), str(slip_min(out)))
        return [(cv['to'], 0, data, got(spend, out).replace(' BNB →', ' WBNB →', 1))], True
    if kind == 'flap':
        out = (call_words(FLAP_PORTAL, 'quoteExactInput((address,address,uint256))', f'({ZERO_ADDR},{token},{wei})')
               or [0])[0]
        if not out:
            raise UsageError(L('No quote', '报不出价'))
        bought, second = ((call_words(FLAP_PORTAL, 'buyQuotaOf(address,address)', token, addr) or []) + [0, 0])[:2]
        mx = (call_words(FLAP_PORTAL, 'maxBuyPerOrigin(address)', token) or []) + [0, 0]
        left = min(second, mx[1] - bought) if mx[1] else None
        note = ''
        if left is not None and left <= 0:
            raise UsageError(L('This wallet hit its buy cap', '这个钱包已买满上限'))
        if left is not None and out > left:
            out, note = left, L(' | over per-wallet cap: buys up to the cap, extra BNB refunded', '｜超过每个钱包上限，只买到上限、多的 BNB 退回')
        data = cast('calldata', 'swapExactInput((address,address,uint256,uint256,bytes))',
                    f'({ZERO_ADDR},{token},{wei},{slip_min(out)},0x)')
        return [(FLAP_PORTAL, wei, data, got(wei, out) + note)], True
    if kind == 'ac':
        q = call_words(AC_PORTAL, 'quoteBuy(address,address,uint256)', cv['market'], addr, str(wei))
        if not q or len(q) < 4 or not q[3]:
            raise UsageError(L("Can't buy (no Agent ID, or cap reached)", '买不到（没有 Agent ID，或已买满）'))
        data = cast('calldata', 'buy(address,uint256)', cv['market'], str(slip_min(q[3])))
        return [(AC_PORTAL, wei, data, got(wei, q[3], L(f'shares of {sym}', f'份 {sym}')) + L(' | claimed as tokens after graduation', '｜毕业后领成代币'))], True
    raise UsageError(L('Unsupported launch type', '不支持这种盘'))


def curve_sell_call(cv, token, w, amount):
    """(to, data, BNB out, what comes back) to sell amount of token (already approved) on its curve or PancakeSwap."""
    addr, kind = WALLETS[w]['addr'], cv['kind']
    if cv['state'] == 'dex' or kind == 'dex':
        path = [token] + ([cv['quote']] if cv.get('quote') else []) + [WBNB]
        out = pancake_out(amount, path)
        data = cast('calldata',
                    'swapExactTokensForETHSupportingFeeOnTransferTokens(uint256,uint256,address[],address,uint256)',
                    str(amount), str(slip_min(out)), '[' + ','.join(path) + ']', addr, deadline())
        return PANCAKE_ROUTER, data, out, 'BNB'
    if kind == 'four_agent':
        opts = '2' if cv['quote'] else '0'  # 2: swap a non-WBNB quote back into BNB
        q = call_words(FOUR_TOOLS, 'estimateSell(address,address,uint256,uint256,bytes)', token, addr, str(amount),
                       opts, '0x') or [0] * 6
        data = cast('calldata', 'sell(address,uint256,uint256,uint256,bytes)', token, str(amount), str(slip_min(q[3])),
                    opts, '0x')
        return FOUR_CORE, data, q[3], 'BNB'
    if kind == 'four':
        q = call_words(FOUR_HELPER, 'trySell(address,uint256)', token, str(amount)) or [0] * 4
        if cv['quote']:
            out = pancake_out(q[2], [cv['quote'], WBNB]) if q[2] else 0
            data = cast('calldata', 'sellForEth(uint256,address,uint256,uint256,uint256,address)', '0', token,
                        str(amount), str(slip_min(out)), '0', ZERO_ADDR)
            return FOUR_HELPER, data, out, 'BNB'
        data = cast('calldata', 'sellToken(uint256,address,uint256,uint256,uint256,address)', '0', token, str(amount),
                    str(slip_min(q[2])), '0', ZERO_ADDR)
        return cv['to'], data, q[2], 'BNB'
    if kind == 'gang_old':
        out = (call_words(GANG_OLD_PAD, 'quoteSell(address,uint256)', token, str(amount)) or [0])[0]
        return GANG_OLD_PAD, cast('calldata', 'sell(address,uint256,uint256)', token, str(amount), str(slip_min(out))), \
            out, 'BNB'
    if kind == 'gang':
        g = call_words(cv['to'], 'curves(address)', token) or [0] * 8
        real_q, sold, vq = g[2], g[3], g[5]
        gross = (vq + real_q) - -(-(vq * GANG_VT) // (GANG_VT - sold + amount)) if vq else 0
        out = max(gross - gross * 100 // 10000, 0)
        return cv['to'], cast('calldata', 'sell(address,uint256,uint256)', token, str(amount), str(slip_min(out))), \
            out, 'WBNB' if cv.get('wrap') else token_label(cv['quote'])[0]
    if kind == 'flap':
        out = (call_words(FLAP_PORTAL, 'quoteExactInput((address,address,uint256))', f'({token},{ZERO_ADDR},{amount})')
               or [0])[0]
        data = cast('calldata', 'swapExactInput((address,address,uint256,uint256,bytes))',
                    f'({token},{ZERO_ADDR},{amount},{slip_min(out)},0x)')
        return FLAP_PORTAL, data, out, 'BNB'
    raise UsageError(L('Unsupported launch type', '不支持这种盘'))


def curve_sell_steps(cv, token, w, pct):
    """([(to, value, data, note)], final) to sell pct% of wallet w's position. final=False: an approval (or an Agent
    Curve claim) that has to be mined first. Raises UsageError when there is nothing to sell."""
    addr, sym = WALLETS[w]['addr'], token_label(token)[0]
    if cv['kind'] == 'ac' and cv['state'] == 'curve':
        market = cv['market']
        shares = word(market, 'sharesOf(address)', addr) or 0
        if not shares:
            raise UsageError(L(f'No {sym}', f'没有 {sym}'))
        all_out = (call_words(AC_PORTAL, 'sellAll(address,uint256)', market, '0', sender=addr) or [0])[0]
        if not all_out:  # never send a sale with no minimum out
            raise UsageError(L('No quote', '报不出价'))
        if pct >= 100:
            data = cast('calldata', 'sellAll(address,uint256)', market, str(slip_min(all_out)))
            return [(AC_PORTAL, 0, data, L(f'All {cn_num(from_wei(shares))} shares → ~{fmt(from_wei(all_out))} BNB', f'全部 {cn_num(from_wei(shares))} 份 → 约 {fmt(from_wei(all_out))} BNB'))], True
        gross = int(Decimal(all_out) * pct / 100)
        r = call_words(AC_PORTAL, 'sell(address,uint256,uint256,uint256)', market, str(gross), str(shares), '0',
                       sender=addr)
        net = r[1] if r and len(r) > 1 else gross
        data = cast('calldata', 'sell(address,uint256,uint256,uint256)', market, str(gross), str(shares),
                    str(slip_min(net)))
        return [(AC_PORTAL, 0, data, L(f'{fmt(pct, 2)}% of shares → ~{fmt(from_wei(net))} BNB', f'{fmt(pct, 2)}% 的份额 → 约 {fmt(from_wei(net))} BNB'))], True
    if cv['kind'] == 'ac':  # graduated: shares become tokens only after a claim
        claim = (call_words(AC_PORTAL, 'claim(address)', cv['market'], sender=addr) or [0])[0]
        if claim:
            return [(AC_PORTAL, 0, cast('calldata', 'claim(address)', cv['market']),
                     L(f'Claim shares as tokens first (~{cn_num(from_wei(claim))})', f'先把份额领成代币（约 {cn_num(from_wei(claim))} 颗）'))], False
        token = cv.get('real') or token
    if cv['state'] == 'wait':
        raise UsageError(curve_stage(cv) if cv.get('fair') else L('Moving from curve to DEX, try again in a few minutes', '正在从内盘搬到外盘，等几分钟再试'))
    bal = word(token, 'balanceOf(address)', addr) or 0
    amount = bal if pct >= 100 else int(Decimal(bal) * pct / 100)
    if cv['kind'] == 'four' and cv['state'] == 'curve':
        amount -= amount % 10 ** 9  # four.meme only takes whole multiples of 1e9
    if not amount:
        raise UsageError(L(f'No {sym}', f'没有 {sym}'))
    spender = PANCAKE_ROUTER if cv['state'] == 'dex' else cv['to']
    if (word(token, 'allowance(address,address)', addr, spender) or 0) < amount:
        where = 'PancakeSwap' if spender == PANCAKE_ROUTER else cv['name']
        return [(token, 0, cast('calldata', 'approve(address,uint256)', spender, str(amount)),
                 L(f'Approve {where} to spend {cn_num(from_wei(amount))} {sym} (this amount only)', f'允许 {where} 拿走 {cn_num(from_wei(amount))} {sym}（只授权这次的数量）'))], False
    to, data, out, unit = curve_sell_call(cv, token, w, amount)
    if not out:
        raise UsageError(L('No quote', '报不出价'))
    return [(to, 0, data, L(f'{cn_num(from_wei(amount))} {sym} ({fmt(pct, 2)}%) → ~{fmt(from_wei(out))} {unit}'
             f' (min {fmt(from_wei(slip_min(out)))})', f'{cn_num(from_wei(amount))} {sym}（{fmt(pct, 2)}%）→ 约 {fmt(from_wei(out))} {unit}'
             f'（最少 {fmt(from_wei(slip_min(out)))}）'))], True


def unwrap_steps(w):
    bal = word(WBNB, 'balanceOf(address)', WALLETS[w]['addr']) or 0
    return [(WBNB, 0, cast('calldata', 'withdraw(uint256)', str(bal)), L(f'Unwrap {fmt(from_wei(bal))} WBNB to BNB', f'把 {fmt(from_wei(bal))} WBNB 换回 BNB'))] \
        if bal else []


def curve_try(fn, *args):
    try:
        return fn(*args)
    except UsageError as e:
        return str(e)


def round_calls(ws, results, verb):
    """Split per-wallet (calls, final) results: (calls for preview_calls, wallets whose calls finish the trade,
    wallets that need another round). Wallets that can't trade get a message."""
    calls, final, later = [], [], []
    for w, r in zip(ws, results):
        if isinstance(r, str):
            say(L(f"⚠️ <b>{w}</b>  can't {verb}: {esc(r)}", f'⚠️ <b>{w}</b>　不{verb}：{esc(r)}'), rich=True)
            continue
        calls += [(w, *x) for x in r[0]]
        (final if r[1] else later).append(w)
    return calls, final, later


def curve_buy(token, cv, plan, intro=(), on_fill=None):
    """Preview buying on the curve (or PancakeSwap): plan {wallet: wei}. Gang's WBNB pads take two rounds."""
    ws, sym = list(plan), token_label(token)[0]
    calls, final, later = round_calls(ws, each(ws, lambda w: curve_try(curve_buy_steps, cv, token, w, plan[w])), L('Buy', '买'))
    if not calls:
        return

    def after(done):
        for w in done:
            extra = on_fill(w) if on_fill and w in final else ''
            if extra:
                say(extra)
        nxt = {w: plan[w] for w in done if w in later}
        if nxt:
            curve_buy(token, cv, nxt, intro, on_fill)
    step = L('Step 1/2: wrap BNB + approve (step 2 follows once mined) | ', '第 1／2 步：BNB 换 WBNB＋授权（上链后自动出第 2 步）｜') if later else ''
    preview_calls(L(f"🟢 {cv['name']} buy | {step}{sym}", f"🟢 {cv['name']}买入｜{step}{sym}"), calls, after=after, intro=intro)


def curve_sell(token, cv, ws, pct):
    """Preview selling pct% on the curve (or PancakeSwap); approvals / claims first, Gang's WBNB unwrapped last."""
    sym = token_label(token)[0]
    calls, final, later = round_calls(ws, each(ws, lambda w: curve_try(curve_sell_steps, cv, token, w, pct)), L('Sell', '卖'))
    if not calls:
        return

    def after(done):
        nxt = [w for w in done if w in later]
        if nxt:
            curve_sell(token, cv, nxt, pct)
        if cv['kind'] == 'gang' and cv.get('wrap') and cv['state'] == 'curve':
            back = [(w, *x) for w in done if w in final for x in unwrap_steps(w)]
            if back:
                preview_calls(L('🔄 Last step: unwrap WBNB to BNB', '🔄 最后一步：WBNB 换回 BNB'), back)
    step = L('Approve first (next step follows once mined) | ', '先授权（上链后自动出下一步）｜') if later else ''
    preview_calls(L(f"🔴 {cv['name']} sell | {step}{sym}", f"🔴 {cv['name']}卖出｜{step}{sym}"), calls, after=after)


def send_now(w, to, value, data):
    """Simulate, preview and execute one call without asking (sniping, auto-sell); (True, tx) or (False, why)."""
    err = eth_call_error(w, to, value, data)
    if err:
        return False, L(f'On-chain simulation failed: {err}', f'链上模拟失败：{err}')
    r = baw(w, 'contract-call', 'preview', '--binanceChainId', CHAIN, '--from', WALLETS[w]['addr'], '--to', to,
            '--value', str(value), '--inputData', data)
    if not r.get('success'):
        return False, L(f'Preview failed: {baw_err(r)}', f'预览失败：{baw_err(r)}')
    rid = r['data']['requestId']
    sim = r['data'].get('simulationResult') or {}
    if sim.get('simulationErrorDetail'):
        return False, L(f"Preview simulation failed: {sim['simulationErrorDetail']}", f"预览模拟失败：{sim['simulationErrorDetail']}")
    dkey = ('call', w, to.lower(), int(value), data.lower())
    if is_repeat(dkey):
        return False, L(f'Same tx already sent within {DUP_WINDOW}s', f'{DUP_WINDOW} 秒内已经送过同一笔')
    locked = wait_unlocked(w)
    if locked:
        unsend(dkey)
        return False, locked
    r = baw(w, 'contract-call', 'execute', '--requestId', rid)
    d = (r.get('data') or {}) if r.get('success') else {}
    log_order('auto', w, to=to, value=str(value), ok=bool(d.get('txHash')), data=d or None,
              error=None if r.get('success') else baw_raw(r))
    if not r.get('success'):
        err = baw_err(r)
        if not re.search(UNKNOWN_RE, baw_raw(r)):
            unsend(dkey)
            return False, err
        return False, L(f'{UNKNOWN_MARK} (no reply after sending: {err})', f'{UNKNOWN_MARK}（送出后没收到回复：{err}）')
    if not d.get('txHash'):
        if d.get('status') == 'PENDING_CONFIRMATION':
            run_bg(lambda: notify_unlock(w))
            return False, L(f'{UNKNOWN_MARK} (confirm again in the Binance app)', f'{UNKNOWN_MARK}（要在币安 App 再确认一次）')
        return False, L(f"Status {d.get('status')}", f"状态 {d.get('status')}")
    res = receipt(d['txHash'])
    if res.startswith('✅'):
        return True, d['txHash']
    return False, res if res.startswith('❌') else L(f'{UNKNOWN_MARK} ({res}, tx {d["txHash"]})', f'{UNKNOWN_MARK}（{res}，交易 {d["txHash"]}）')


def auto_run(w, steps, rounds=3):
    """Send what steps() -> (calls, final) returns, round after round, until the final calls are mined."""
    res = ''
    for _ in range(rounds):
        calls, final = steps()
        for to, value, data, note in calls:
            ok, res = send_now(w, to, value, data)
            if not ok:
                return False, L(f'{note}: {res}', f'{note}：{res}')
        if final:
            return True, res
    return False, L('Prep steps done but still not tradable', '准备步骤做完还是不能交易')


def auto_sell(token, w, pct, cv=None):
    """Sell without asking (take-profit, stop-loss, sniped positions); (ok, message)."""
    cv = cv or detect_curve(token) or DEX_CV
    try:
        ok, res = auto_run(w, lambda: curve_sell_steps(cv, token, w, pct))
    except UsageError as e:
        return False, str(e)
    if ok and cv['kind'] == 'gang' and cv.get('wrap') and cv['state'] == 'curve':
        for to, value, data, _ in unwrap_steps(w):
            send_now(w, to, value, data)
    return ok, res


def position_value(cv, token, w):
    """BNB (wei) the whole position would sell for right now; None when it can't tell, 0 when nothing is left."""
    addr = WALLETS[w]['addr']
    # a failed read (rate limit, node hiccup) is None, never 0: 0 closes the position for good
    if cv['kind'] == 'ac' and cv['state'] == 'curve':
        shares = word(cv['market'], 'sharesOf(address)', addr)
        if not shares:
            return shares
        return (call_words(AC_PORTAL, 'sellAll(address,uint256)', cv['market'], '0', sender=addr) or [None])[0]
    if cv['kind'] == 'ac':
        claim = (call_words(AC_PORTAL, 'claim(address)', cv['market'], sender=addr) or [0])[0]
        token = cv.get('real') or token
        bal = word(token, 'balanceOf(address)', addr)
        bal = None if bal is None else bal + claim
    else:
        bal = word(token, 'balanceOf(address)', addr)
    if not bal:
        return bal
    if cv['state'] == 'wait':
        return None
    try:
        return curve_sell_call(cv, token, w, bal)[2] or None
    except UsageError:
        return None


def open_positions(st):
    return [p for p in st.get('positions') or [] if p.get('open')]


def check_positions():
    """Watch loop: sniped positions sell on their own at the take-profit multiple or the stop-loss."""
    s = load_prefs()['snipe']
    tp_x, tp_sell, sl = dec(s['tp_x']), dec(s['tp_sell']), dec(s['sl'])
    with SNIPE_LOCK:
        todo = [dict(p) for p in open_positions(load_snipe())]
    for p in todo:
        w, token = p['wallet'], p['token']
        try:
            check_position(p, tp_x, tp_sell, sl)
        except Exception as e:  # noqa: BLE001 - one bad position must not stop the others (or the save below)
            logging.exception('sniped position %s %s', w, token)
            p['fails'] = p.get('fails', 0) + 1
            p['open'] = p['fails'] < POS_FAILS
            say(L(f"❌ <b>{w}</b> {esc(p['symbol'])} auto-check error: {esc(str(e)[:200])}", f"❌ <b>{w}</b> {esc(p['symbol'])} 自动检查出错：{esc(str(e)[:200])}")
                + ('' if p['open'] else L(f'\n{POS_FAILS} failures in a row; auto-sell stopped for this one, handle it from the card', f'\n连续失败 {POS_FAILS} 次，这笔不再自动卖，请到卡片手动处理')), rich=True)
        finally:
            with SNIPE_LOCK:
                st = load_snipe()
                for q in st['positions']:
                    if q['token'] == token and q['wallet'] == w and q['time'] == p['time']:
                        q.update(p)
                save_snipe(st)


def check_position(p, tp_x, tp_sell, sl):
    """One sniped position: sell part at the take-profit multiple, all of it at the stop-loss (updates p)."""
    w, token = p['wallet'], p['token']
    if w not in WALLETS:
        p['open'] = False
        return
    cv = detect_curve(token) or DEX_CV
    value = position_value(cv, token, w)
    cost = int(p['cost'])
    if value == 0:  # read fine and nothing is left: sold by hand or moved out
        p['open'] = False
        say(f"ℹ️ <b>{w}</b> {esc(p['symbol'])} "
            + (L('On-chain check shows this was never bought; tracking ended.', '链上查到这笔其实没买到，结束追踪。') if p.get('unknown') else L('No position left (sold or moved by hand); auto-watch stopped.', '已经没有持仓（手动卖掉或转走了），不再自动盯。')),
            rich=True)
        return
    p['unknown'] = False  # (the saved copy is merged with update(), so clear the flag rather than drop the key)
    if not value or not cost:
        return  # couldn't read the value this round; try again next round
    x = Decimal(value) / cost
    sl_hit = sl > 0 and x <= 1 - sl / 100
    tp_hit = tp_x > 0 and x >= tp_x and not p['tp_done']
    pct = Decimal(100) if sl_hit else tp_sell if tp_hit else None
    if pct is None:
        return
    why = L(f'up {fmt(x, 2)}x', f'涨到 {fmt(x, 2)} 倍') if x >= 1 else L(f'down {fmt((1 - x) * 100, 0)}%', f'跌了 {fmt((1 - x) * 100, 0)}%')
    ok, res = auto_sell(token, w, pct, cv)
    if ok:
        p['fails'] = 0
        p['open'] = pct < 100
        if tp_hit and not sl_hit:
            p['tp_done'] = True
            p['cost'] = str(cost * int(100 - pct) // 100)  # the rest keeps its share of the cost: stop-loss stays vs entry
        say(L(f"⚡ <b>{w}</b> {esc(p['symbol'])} {why}, auto-sold {fmt(pct, 0)}%  {tx_link(res)}", f"⚡ <b>{w}</b> {esc(p['symbol'])} {why}，自动卖 {fmt(pct, 0)}%　{tx_link(res)}"), rich=True)
    elif UNKNOWN_MARK in res:
        say(L(f"⚠️ <b>{w}</b> {esc(p['symbol'])} {why}, auto-sell: {esc(res)}\nNext round re-checks using the on-chain balance.", f"⚠️ <b>{w}</b> {esc(p['symbol'])} {why}，自动卖{esc(res)}\n下一轮会用链上余额重新判断。"), rich=True)
    else:
        p['fails'] += 1
        p['open'] = p['fails'] < POS_FAILS
        tail = '' if p['open'] else L(f'\n{POS_FAILS} failures in a row; auto-sell stopped for this one, handle it from the card', f'\n连续失败 {POS_FAILS} 次，这笔不再自动卖，请到卡片手动处理')
        say(L(f"❌ <b>{w}</b> {esc(p['symbol'])} {why}, auto-sell failed: {esc(res)}{tail}", f"❌ <b>{w}</b> {esc(p['symbol'])} {why}，自动卖失败：{esc(res)}{tail}"), rich=True)


def claim_ac(token, w):
    """An Agent Curve position after graduation: claim the shares as tokens (no confirm; auto transfer asked for it).
    Returns the address the tokens are at."""
    cv = detect_curve(token)
    if not cv or cv['kind'] != 'ac':
        return token
    if cv['state'] == 'dex' and (call_words(AC_PORTAL, 'claim(address)', cv['market'], sender=WALLETS[w]['addr'])
                                 or [0])[0]:
        send_now(w, AC_PORTAL, 0, cast('calldata', 'claim(address)', cv['market']))
    return cv.get('real') or token


# ---------------------------------------------------------------- moving tokens to a payout wallet

SENDS_FILE = CONF_DIR / 'sends.json'  # waiting automatic transfers (e.g. until the token graduates)
SEND_WHEN = {'off': L('Off', '关'), 'fill': L('On fill', '买到就转'), 'grad': L('After graduation', '毕业后转')}
SENDS_LOCK = threading.Lock()
SEND_TRIES = 3
UNKNOWN_RE = r'(?i)time.?out|timed out|ECONN|socket|network|EAI_AGAIN'
UNKNOWN_MARK = L('result unknown', '结果未知')  # sent, but no answer: it may be on chain, so never refund or resend it


def payout_addr(p=None):
    p = p or load_prefs()
    addrs = [x['addr'] for x in PAYOUT]
    return next((a for a in addrs if a.lower() == p['send_to'].lower()), addrs[0] if addrs else None)


def payout_name(addr):
    return next((x['name'] for x in PAYOUT if x['addr'].lower() == (addr or '').lower()), short(addr))


def is_graduated(token):
    """True once the token trades on a DEX (left its launch curve)."""
    cv = detect_curve(token)
    if cv:
        return cv['state'] == 'dex'
    # not on a curve we know: only an explicit "still on its curve" (0) from Binance holds it back; ordinary DEX
    # tokens have no migrateStatus at all and would otherwise wait forever
    return str(token_info(token).get('migrateStatus')) not in ('0', 'False')


def send_all(w, token, to):
    """Send wallet w's whole balance of token to a payout address with baw (Binance also requires it in the
    App address book). (True, tx hash | note) or (False, why)."""
    if to.lower() not in {x['addr'].lower() for x in PAYOUT}:
        return False, L('Destination is not in the wallets.json payout list', '目的地不在 wallets.json 的转出清单里')
    bal = word(token, 'balanceOf(address)', WALLETS[w]['addr'])
    if not bal:
        return True, L('Nothing to send', '没有可以转的')
    dkey = ('send', w, token.lower(), to.lower(), str(bal))
    if is_repeat(dkey):
        return False, L(f'Same tx already sent within {DUP_WINDOW}s', f'{DUP_WINDOW} 秒内已经送过同一笔')
    locked = wait_unlocked(w)
    if locked:
        unsend(dkey)
        return False, locked
    r = baw(w, 'wallet', 'send', '--max', '--recipient', to, '--binanceChainId', CHAIN, '--tokenAddress', token)
    d = (r.get('data') or {}) if r.get('success') else {}
    tx = d.get('txHash')
    log_order('send', w, token=token, to=to, amount=str(bal), ok=bool(tx), tx=tx, error=None if r.get('success') else baw_raw(r))
    if tx:
        return True, tx
    if d.get('status') == 'PENDING_CONFIRMATION':
        run_bg(lambda: notify_unlock(w))
        return True, L('Confirm again in the Binance app', '要在币安 App 再确认一次')
    raw = baw_raw(r) if not r.get('success') else L(f"Status {d.get('status')}", f"状态 {d.get('status')}")
    err = baw_err(r) if not r.get('success') else raw
    if not re.search(UNKNOWN_RE, raw):
        unsend(dkey)
    if re.search(r'(?i)address.?book|whitelist|not.{0,20}(allow|book)', raw):
        err = L("Recipient isn't in the Binance app address book yet; add it first", '收款地址还没加进币安 App 的地址簿，要先加进去')
    return False, err


def send_line(w, sym, to, ok, res):
    if ok and res.startswith('0x'):
        return L(f"📤 <b>{w}</b>'s {esc(sym)} sent to {esc(payout_name(to))}  {tx_link(res)}", f'📤 <b>{w}</b> 的 {esc(sym)} 已转到 {esc(payout_name(to))}　{tx_link(res)}')
    if ok:
        return L(f"📤 <b>{w}</b>'s {esc(sym)}: {esc(res)}", f'📤 <b>{w}</b> 的 {esc(sym)}：{esc(res)}')
    return L(f"❌ <b>{w}</b>'s {esc(sym)} not sent: {esc(res)}", f'❌ <b>{w}</b> 的 {esc(sym)} 没转出：{esc(res)}')


def manual_send(token, ws):
    to = payout_addr()
    if not to:
        raise UsageError(L('No payout destination yet: set "payout" in wallets.json on the computer to your main wallet address (computer only).', '还没有转出目的地：在电脑上的 wallets.json 的 "payout" 填主钱包地址（只能在电脑上改）。'))
    sym, d = token_label(token)
    bals = each(ws, lambda w: word(token, 'balanceOf(address)', WALLETS[w]['addr']))
    have = {w: b for w, b in zip(ws, bals) if b}
    if not have:
        say(L(f"{', '.join(ws)} hold no {sym}.", f"{'、'.join(ws)} 都没有 {sym}。"))
        return
    lines = [L(f'📤 <b>Sweep preview | {esc(sym)}</b>', f'📤 <b>转出预览｜{esc(sym)}</b>'), L(f'To {esc(payout_name(to))}  <code>{to}</code>', f'转到 {esc(payout_name(to))}　<code>{to}</code>'), '']
    lines += [L(f'<b>{w}</b>  all {cn_num(Decimal(b) / Decimal(10) ** d)}', f'<b>{w}</b>　全部 {cn_num(Decimal(b) / Decimal(10) ** d)} 颗') for w, b in have.items()]
    lines += ['', L('Sent via Binance wallet; add the destination to the Binance app address book first', '用币安钱包转账；目的地要先加进币安 App 的地址簿')]

    def go():
        for w, (ok, res) in zip(have, each(list(have), lambda w: send_all(w, token, to))):
            say(send_line(w, sym, to, ok, res), rich=True)
    ask_confirm('\n'.join(lines), L(f"Confirm sweep ({', '.join(have)})", f"确认转出（{'、'.join(have)}）"), go, rich=True)


def load_sends():
    try:
        rows = json.loads(SENDS_FILE.read_text())
        return rows if isinstance(rows, list) else []
    except (OSError, ValueError):
        return []


def save_sends(rows):
    write_json(SENDS_FILE, rows[-300:])


def after_buy_send(w, token, prefs):
    """Called after a filled buy: transfer now, or remember to transfer once the token graduates."""
    to = payout_addr(prefs)
    if not to or prefs['auto_send'] == 'off':
        return ''
    sym = token_label(token)[0]
    when = prefs['auto_send']
    cv = detect_curve(token) if when == 'fill' else None
    if cv and (cv['state'] != 'dex' or cv['kind'] == 'ac'):  # curve tokens (and Agent Curve shares) can't be transferred before graduation
        when = 'grad'
    if when == 'fill':
        ok, res = send_all(w, token, to)
        if ok:
            return re.sub(r'<[^>]+>', '', send_line(w, sym, to, ok, res))
    with SENDS_LOCK:
        rows = load_sends()
        if not any(r['status'] == 'waiting' and r['wallet'] == w and r['token'].lower() == token.lower() for r in rows):
            rows.append({'id': secrets.token_hex(4), 'token': token, 'symbol': sym, 'wallet': w, 'to': to,
                         'when': when, 'status': 'waiting', 'tries': 0 if when == 'grad' else 1,
                         'created': int(time.time())})
            save_sends(rows)
    if when == 'fill':
        return L(f'❌ Sweep failed ({res}); retrying every minute, up to {SEND_TRIES} times', f'❌ 转出没成功（{res}），每分钟会再试，最多 {SEND_TRIES} 次')
    return L(f'📤 Will auto-send {sym} to {payout_name(to)} after graduation', f'📤 毕业后自动把 {sym} 转到 {payout_name(to)}')


def run_sends():
    """Watch loop: transfer the waiting tokens whose moment has come; give up after SEND_TRIES failures."""
    with SENDS_LOCK:
        rows = load_sends()
    due = [r for r in rows if r['status'] == 'waiting' and (r['when'] != 'grad' or is_graduated(r['token']))]
    for r in due:
        if r['wallet'] not in WALLETS:
            r['status'] = 'cancelled'
            continue
        token = claim_ac(r['token'], r['wallet']) if r['when'] == 'grad' else r['token']
        ok, res = send_all(r['wallet'], token, r['to'])
        r['tries'] += 0 if ok else 1
        if ok:
            r['status'], r['result'] = 'done', res
            head = L('🎓 Graduated, ', '🎓 毕业了，') if r['when'] == 'grad' else ''
            say(head + send_line(r['wallet'], r['symbol'], r['to'], ok, res), rich=True)
        elif r['tries'] >= SEND_TRIES:
            r['status'], r['result'] = 'failed', res
            say(send_line(r['wallet'], r['symbol'], r['to'], ok, res) + L(f'\nTried {SEND_TRIES} times, auto-sweep stopped; tap "📤 Sweep" on the card.', f'\n试了 {SEND_TRIES} 次，不再自动转；可以在卡片上按「📤 转出」。'),
                rich=True)
    if due:
        with SENDS_LOCK:
            latest = {r['id']: r for r in due}
            save_sends([latest.get(r['id'], r) for r in load_sends()])


def watch_loop():
    last_login_check = last_send_check = 0
    while True:
        try:
            check_rules()
        except Exception:  # noqa: BLE001 - keep watching
            logging.exception('rule check failed')
        try:
            check_positions()
        except Exception:  # noqa: BLE001
            logging.exception('sniped position check failed')
        if time.time() - last_login_check >= LOGIN_EVERY:
            last_login_check = time.time()
            try:
                check_logins()
            except Exception:  # noqa: BLE001
                logging.exception('login check failed')
        if time.time() - last_send_check >= 60:
            last_send_check = time.time()
            try:
                run_sends()
            except Exception:  # noqa: BLE001
                logging.exception('auto transfer failed')
        time.sleep(WATCH_EVERY)


LOGIN_FAILS = {}  # wallet -> consecutive checks that found it signed out
LOGIN_EVERY = 120


def check_logins():
    """Message the owner once when a wallet drops its baw login (two checks in a row), and once when it is back."""
    for w in list(WALLETS):
        if w in LOGINS:
            continue
        r = baw(w, 'wallet', 'status', timeout=30)
        state = str((r.get('data') or {}).get('status') or '') if r.get('success') else ''
        if not r.get('success'):
            continue  # network trouble is not a logout
        if state.upper() == 'CONNECTED':
            if LOGIN_FAILS.pop(w, 0) >= 2:
                say(L(f'✅ Wallet {w} is back online.', f'✅ 钱包 {w} 又连上了。'))
            continue
        LOGIN_FAILS[w] = LOGIN_FAILS.get(w, 0) + 1
        if LOGIN_FAILS[w] == 2:
            say(L(f"⚠️ <b>Wallet {w} disconnected</b> ({esc(state)})\nUntil re-login it can't trade, and TP/SL can't sell.", f'⚠️ <b>钱包 {w} 掉线了</b>（{esc(state)}）\n在这之前它不能交易，止盈止损也卖不掉。'),
                [[cmd_btn(L(f'🔑 Re-login {w}', f'🔑 重新登录 {w}'), f'/wallet login {w}')]], rich=True)


def cmd_tpsl(args):
    if args and args[0].lower() in ('clear',) + zh_word('清除'):
        def clear():
            n = len(update_rules(lambda r: r.get('status') == 'armed' and set_status(r, 'cancelled')))
            say(L(f'🗑 Cleared all {n} TP/SL rules', f'🗑 已清除全部 {n} 条止盈止损'))
        n = sum(1 for r in load_rules() if r.get('status') == 'armed')
        ask_confirm(L(f"Clear all {n} TP/SL rules? The bot won't auto-sell for you after that.", f'要清除全部 {n} 条止盈止损吗？清掉后机器人就不会帮你自动卖。'), L('Confirm clear', '确认清除'), clear)
        return
    armed = [r for r in load_rules() if r.get('status') == 'armed']
    lines = [L('🎯 <b>TP/SL</b> (checks price every 20s, auto-sells when hit)', '🎯 <b>止盈止损</b>（机器人每 20 秒看一次价格，到了就自动卖）'), '']
    lines += [L(f"{i}. <b>{esc(r['symbol'])}</b>  {esc(rule_text(r))}", f"{i}. <b>{esc(r['symbol'])}</b>　{esc(rule_text(r))}") for i, r in enumerate(armed, 1)]
    if not armed:
        lines.append(L('None yet. To set one: paste a token address → tap 🎯 TP/SL on the card', '目前没有。要设置：贴代币地址 → 卡片上按 🎯 止盈止损'))
    tokens = list(dict.fromkeys(r['token'] for r in armed))[:6]
    buttons = rows_of([{'text': L(f"Open {next(r['symbol'] for r in armed if r['token'] == t)}", f"开 {next(r['symbol'] for r in armed if r['token'] == t)}"), 'callback_data': f'o:t:{t}'}
                       for t in tokens], 3)
    if armed:
        buttons.append([cmd_btn(L('🗑 Clear all', '🗑 全部清除'), L('/tpsl clear', '/tpsl 清除'))])
    say('\n'.join(lines), buttons, rich=True)


# ---------------------------------------------------------------- limit orders

def limit_preview(pid, side, vals):
    panel = PANELS[pid]
    token, ws, sym = panel['token'], list(panel['wallets']), panel['names'][0]
    if len(vals) != 2:
        raise UsageError(L('Need two numbers, e.g. ', '要两个数字，例如 ') + ('0.00001 0.02' if side == 'buy' else '0.0001 50'))
    price = need_amount(vals[0].lstrip('$'), cap=False)
    info = token_info(token)
    now = dec(info.get('price'))
    if now > 0 and side == 'buy' and price >= now:
        raise UsageError(L(f'Limit buy price must be below the current ${price_text(now)} (buys when price drops).', f'限价买的价格要低于现价 ${price_text(now)}（价格跌到才买）。'))
    if now > 0 and side == 'sell' and price <= now:
        raise UsageError(L(f'Limit sell price must be above the current ${price_text(now)}; to "sell on a drop" use 🎯 SL.', f'限价卖的价格要高于现价 ${price_text(now)}；要「跌了就卖」请用 🎯 止损。'))
    if side == 'buy':
        if not panel['can_buy']:
            raise UsageError(L('This token is not available to buy.', '这颗币不提供买入。'))
        amt = need_amount(vals[1])
        block = liquidity_block(info, token)
        if block:
            raise UsageError(block)

        def prep(w):
            bad = wallet_mismatch(w)
            if bad:
                return None, L(f'{w} skipped: {bad}', f'{w} 不挂：{bad}')
            bal = native_balance(w)
            if bal < amt + GAS_RESERVE:
                return None, L(f'{w} skipped: not enough BNB (have {fmt(bal)}, need {fmt(amt)} + gas)', f'{w} 不挂：BNB 不够（有 {fmt(bal)}，要 {fmt(amt)} 加手续费）')
            return fmt(amt), L(f'{w} buys with {fmt(amt)} BNB ({fmt(bal, 4)} BNB left)', f'{w} 用 {fmt(amt)} BNB 买（剩 {fmt(bal, 4)} BNB）')
    else:
        pct = need_amount(vals[1], cap=False)
        if pct > 100:
            raise UsageError(L('Sell percent cannot exceed 100.', '卖出比例不能超过 100。'))

        def prep(w):
            return sell_prep(w, token, pct)
    results = each(ws, prep)
    ready = {w: q for w, (q, _) in zip(ws, results) if q}
    lines = [L(f"📌 <b>Limit {'buy' if side == 'buy' else 'sell'} preview | {esc(sym)}</b>", f"📌 <b>限价{'买' if side == 'buy' else '卖'}预览｜{esc(sym)}</b>"),
             L(f"Now ${price_text(now) if now > 0 else '?'}  →  fills at <b>${price_text(price)}</b>", f"现价 ${price_text(now) if now > 0 else '?'}　→　到 <b>${price_text(price)}</b> 才成交"), '']
    lines += [('⚠️ ' if not q else '') + L(f'<b>{t[0]}</b>  {esc(t[2:])}', f'<b>{t[0]}</b>　{esc(t[2:])}') for q, t in results]
    if not ready:
        say('\n'.join(lines + ['', L('No wallet can place this order.', '没有可以挂单的钱包。')]), rich=True)
        return
    lines.append(L('\nOrders are held by Binance and fill even with the computer off; cancel under 📌 Limit orders', '\n挂单交给币安，电脑关着也会成交；可在 📌 挂单 撤单'))
    ask_confirm('\n'.join(lines), L(f"Confirm order ({', '.join(ready)})", f"确认挂单（{'、'.join(ready)}）"),
                lambda: place_limits(side, token, price, ready), rich=True)


def place_limits(side, token, price, qty):
    def one(w):
        r = baw(w, 'limit-order', side, '--triggerPrice', fmt(price, 18), '--fromTokenQty', qty[w],
                '--fromToken', NATIVE if side == 'buy' else token, '--toToken', token if side == 'buy' else NATIVE,
                '--binanceChainId', CHAIN, '--slippage', slippage())
        sid = (r.get('data') or {}).get('strategyId') if r.get('success') else None
        logging.info('limit %s %s %s @ %s: %s', side, w, qty[w], price, sid or baw_raw(r))
        if not sid:
            return L(f'❌ {w} order failed: {baw_err(r)}', f'❌ {w} 挂单失败：{baw_err(r)}')
        return L(f'📌 {w} order placed, fills at ${price_text(price)} (ID {sid})', f'📌 {w} 已挂单，到 ${price_text(price)} 才成交（编号 {sid}）')
    for line in each(list(qty), one):
        say(line)


def cmd_limits(args):
    lines, buttons, n = [L('📌 <b>All limit orders</b> (Binance fills when price hits)', '📌 <b>所有挂单</b>（价格到了币安才成交）')], [], 0
    for w, rows in zip(WALLETS, each(list(WALLETS), lambda w: baw(w, 'limit-order', 'list', '--status', 'WORKING'))):
        lst = ((rows.get('data') or {}).get('list') or []) if rows.get('success') else []
        if not rows.get('success'):
            lines.append(L(f'⚠️ <b>{w}</b> lookup failed: {esc(baw_err(rows))}', f'⚠️ <b>{w}</b> 查不到：{esc(baw_err(rows))}'))
        for o in lst:
            n += 1
            buy = (o.get('fromToken') or '').lower() == NATIVE.lower()
            lines.append(L(f"{n}. <b>{w}</b> limit {'buy' if buy else 'sell'}  {esc(fmt(o.get('fromTokenQty'), 6))} "
                         f"{esc(o.get('fromTokenName'))} → {esc(o.get('toTokenName'))}  at ${esc(o.get('triggerPrice'))}", f"{n}. <b>{w}</b> 限价{'买' if buy else '卖'}　{esc(fmt(o.get('fromTokenQty'), 6))} "
                         f"{esc(o.get('fromTokenName'))} → {esc(o.get('toTokenName'))}　到 ${esc(o.get('triggerPrice'))}"))
            buttons.append(cmd_btn(L(f'Cancel {n}', f'撤 {n}'), f"/cancel {w} {o.get('strategyId')}"))
    if not n:
        lines.append(L('\nNo limit orders. To place one: paste a token address → tap 📌 Limit order on the card', '\n目前没有挂单。要挂单：贴代币地址 → 卡片上按 📌 挂单'))
    say('\n'.join(lines), rows_of(buttons, 4) + [[cmd_btn(L('🔄 Refresh', '🔄 刷新'), '/limits')]], rich=True)


def cmd_cancel(args):
    if len(args) != 2 or args[0] not in WALLETS:
        raise UsageError(L('Use the cancel buttons in the "Limit orders" list.', '请到「挂单」清单按撤单按钮。'))
    w, sid = args
    r = baw(w, 'limit-order', 'cancel', '--strategyId', sid)
    say(L(f'🗑 {w} order cancelled', f'🗑 {w} 已撤单') if r.get('success') else L(f'❌ {w} cancel failed: {baw_err(r)}', f'❌ {w} 撤单失败：{baw_err(r)}'), [[cmd_btn(L('📌 View orders', '📌 看挂单'), '/limits')]])


def cmd_key(args):
    p = load_prefs()
    if not args:
        say(settings_text(p), keyboard=key_board(), rich=True)
        return
    name, cmd = args[0], args[1:]
    if name in BUILTIN_KEYS or name.startswith('/') or len(name) > 16 or dec(name) > 0 or ADDR_RE.match(name):
        raise UsageError(L("Pick another name: no leading /, not a number or address, not a built-in button (Overview, Status...), max 16 chars.", '名字换一个：不能 / 开头、不能是数字或地址、不能跟内置按钮（总览、状态…）一样，最多 16 字。'))
    if not cmd:
        if p['keys'].pop(name, None) is None:
            raise UsageError(L(f'No shortcut named "{name}".', f'没有叫「{name}」的快捷键。'))
        save_prefs(p)
        say(L(f'🗑 Deleted shortcut "{name}"', f'🗑 已删掉快捷键「{name}」'), keyboard=key_board())
        return
    verb = cmd[0].lstrip('/').split('@')[0].lower()
    if not cmd[0].startswith('/') or verb not in COMMANDS or verb in ('key', 'keys', 'set'):
        raise UsageError(L('A shortcut needs an action. Easiest: tap "⭐ Save as shortcut" on a preview.', '快捷键要对应一个动作。最简单的做法：在预览上按「⭐ 存成快捷键」。'))
    if name not in p['keys'] and len(p['keys']) >= MAX_KEYS:
        raise UsageError(L(f'Max {MAX_KEYS} shortcuts; delete one in ⚙️ Settings first.', f'快捷键最多 {MAX_KEYS} 个，先到 ⚙️ 设置 删掉一个。'))
    p['keys'][name] = ' '.join(cmd)
    save_prefs(p)
    say(L(f'⭐ Saved shortcut "{name}", below the input box\nTapping it shows a preview first; you still confirm before sending', f'⭐ 已存成快捷键「{name}」，在输入框下方\n按下去会先出预览，一样要你按确认才送出'), keyboard=key_board())


def on_text(text):
    """Plain (non-command) messages: a token address, the input a card button asked for, or a shortcut key."""
    if ADDR_RE.match(text):
        AWAIT.clear()
        guarded(lambda a: open_address(text), [])
        return
    if TXHASH_RE.match(text):
        AWAIT.clear()
        guarded(lambda a: show_copy(text), [])
        return
    if len(text) > 60 and ADDR_ANY.search(text) and SIG_RE.search(text):
        AWAIT.clear()
        guarded(lambda a: show_presale(text), [])
        return
    keys = {**BUILTIN_KEYS, **load_prefs()['keys']}
    if AWAIT and text not in keys:
        item = dict(AWAIT)
        AWAIT.clear()
        if item['expires'] < time.time():
            say(L('Input timed out, nothing done; tap the button on the card again.', '输入已逾时，什么都没做；请在卡片上重按一次按钮。'))
            return
        guarded(lambda a: on_input(item, text), [])
        return
    cmd = keys.get(text)
    if cmd:
        dispatch(cmd)
        return
    if dec(text.replace('％', '%').rstrip('%')) > 0:  # a number nobody asked for (the input already timed out or was used)
        say(L('Not sure what this number is for: tap a ✏️ button on a card first, then type the number.', '这个数字我不知道要做什么：先在卡片上按一个 ✏️ 按钮，再打数字。'))
        return
    addrs = list(dict.fromkeys(a.lower() for a in ADDR_ANY.findall(text)))
    if addrs:
        AWAIT.clear()
        guarded(lambda a: show_addresses(addrs), [])
        return
    show_home(L("🤔 I don't understand this.", '🤔 这段我看不懂。'))


KNOWN_PLACES = {  # launchpads people paste from announcements: what they are, so the reply isn't a bare address
    GANG_V3_PAD.lower(): L('Gang V3 launchpad', 'Gang V3 发射台'), GANG_V3_ROUTER.lower(): L('Gang V3 router', 'Gang V3 路由'), GANG_V2_PAD.lower(): L('Gang V2 launchpad', 'Gang V2 发射台'),
    '0xf81c4e51c48466c6025b0165b1ea0f6978c712cb': L('Gang V2 launchpad', 'Gang V2 发射台'), '0x60aee38576035e757ded30ff8db8a8da476430c1': L('Gang V2 launchpad', 'Gang V2 发射台'),
    GANG_OLD_PAD.lower(): L('Gang old launchpad', 'Gang 旧发射台'), FOUR_CORE.lower(): L('Four Agent launchpad', 'Four 专属发射台'), FOUR_TM2.lower(): L('four.meme launchpad', 'four.meme 发射台'),
    FLAP_PORTAL.lower(): L('Flap launchpad', 'Flap 发射台'), AC_FACTORY.lower(): L('Flap Agent Curve factory', 'Flap Agent Curve 工厂'), AC_PORTAL.lower(): 'Flap Agent Curve',
    PANCAKE_ROUTER.lower(): L('PancakeSwap router', 'PancakeSwap 路由'), WBNB.lower(): 'WBNB'}


def cmd_open(args):
    if len(args) != 1 or not ADDR_RE.match(args[0]):
        raise UsageError(L('Just paste one address.', '贴一个地址给我就好。'))
    open_address(args[0])


def show_addresses(addrs):
    """Pasted text with addresses in it (an announcement, a chat message): name each one, a button opens it."""
    if len(addrs) == 1 and addrs[0] not in KNOWN_PLACES:
        open_address(addrs[0])
        return
    lines, rows, pads = [], [], []
    for a in addrs[:8]:
        name = KNOWN_PLACES.get(a)
        if not name:
            sym = word(a, 'decimals()') is not None and token_label(a)[0]
            name = L(f'Token {sym}', f'代币 {sym}') if sym else L('Contract', '合约') if is_contract(a) else L('Wallet address', '钱包地址')
        if L('launchpad', '发射台') in name or L('router', '路由') in name or L('factory', '工厂') in name:
            pads.append(name)
        lines.append(L(f'• {esc(name)}  <code>{a}</code>', f'• {esc(name)}　<code>{a}</code>'))
        rows.append([cmd_btn(L(f'Open {name}', f'打开 {name}'), f'/open {a}')])
    head = L(f'{len(addrs)} addresses in this text:', f'这段里有 {len(addrs)} 个地址：')
    note = ''
    if pads:
        note = (L("\n\nThese are <b>launchpads</b>, not tokens, and can't be bought."
                '\nTo buy, paste the <b>token address</b>: on the curve it buys from the launchpad; graduated goes via PancakeSwap.'
                '\nThe sniper already watches Gang (V2 + V3 tax launches), Four Agent, Flap gated, Agent Curve; '
                'Gang V3 fair launches are fixed-price subscriptions, subscribe yourself at gang.fun.', '\n\n这些是<b>发射台</b>，不是代币，本身不能买。'
                '\n要买就把<b>代币地址</b>贴给我：内盘会直接跟发射台买，已毕业走 PancakeSwap。'
                '\n新盘狙击已经会盯 Gang（V2＋V3 收税盘）、Four 专属、Flap 门槛盘、Agent Curve；'
                'Gang V3 打新是固定价认购，要到 gang.fun 自己认购。'))
    say(head + '\n' + '\n'.join(lines) + note, rows, rich=True)


# ---------------------------------------------------------------- pasted presale instructions
#
# Project pages hand agents a text like "buy(uint256 amountIn, uint256 minTokensOut), sending amountIn as the BNB
# value". The text is only used to find the pool, the token and the function names; prices, limits and quotes are
# read on chain, and every transaction is still simulated and previewed before the owner confirms.

PRESALES = {}  # card id -> parsed instructions: pool, token, functions, wallets, msg
TXHASH_RE = re.compile(r'^0x[0-9a-fA-F]{64}$')
SIG_RE = re.compile(r'\b([A-Za-z_]\w*)\(([^()]*)\)(?:\s*returns\s*\(([^()]*)\))?')
ADDR_ANY = re.compile(r'\b0x[0-9a-fA-F]{40}\b')
ABI_TYPE = re.compile(r'^(u?int\d*|address|bool|bytes\d*|string)$')
PRESALE_MIN = Decimal('0.98')  # minimum out = 98% of the on-chain quote
ZERO_ADDR = '0x' + '0' * 40
TRADE_NAMES = {'buy': ('buy', 'purchase', 'contribute', 'buytokens', 'buywithbnb', 'buywitheth'),
               'sell': ('sell', 'selltokens', 'redeem')}
POOL_WORDS = re.compile(r'(?i)pool|presale|sale|curve|contract|launch|market|router|mint|vault|farm|stak')
# other functions that change state (mint, claim, refund…); the lookahead keeps mintedOf / claimedBy as reads
ACTION_RE = re.compile(r'(?i:^(mint|claim|refund|withdraw|redeem|stake|unstake|harvest|deposit|join|enter|register'
                       r'|fill|reserve|commit|contribute|participate|exit|collect|lock|unlock|bid|reveal|open|settle))'
                       r'(?![a-z])')


def parse_params(s):
    out = []
    for part in (p.strip() for p in s.split(',') if p.strip()):
        words = part.split()
        out.append(({'uint': 'uint256', 'int': 'int256'}.get(words[0], words[0]), words[-1] if len(words) > 1 else ''))
    return out


def parse_functions(text):
    """{name: {'name', 'params': [(type, name)], 'sig'}} for every call written like name(type argName, …)."""
    fns = {}
    for name, params, _ in SIG_RE.findall(text):
        ps = parse_params(params)
        if all(ABI_TYPE.match(t) for t, _ in ps):
            fns.setdefault(name, {'name': name, 'params': ps, 'sig': f"{name}({','.join(t for t, _ in ps)})"})
    return fns


def trade_fn(fns, side):
    exact = [f for f in fns.values() if f['name'].lower() in TRADE_NAMES[side]]
    loose = [f for f in fns.values() if f['name'].lower().startswith(side)
             and not re.search(r'(?i)limit|max|min|price|left|by$|of$|ed$', f['name'])]
    return (exact or loose or [None])[0]


def helper_fn(fns, pattern, params):
    """A read function whose name matches pattern and whose parameter types are exactly params."""
    return next((f for f in fns.values() if re.search(pattern, f['name'], re.I)
                 and [t for t, _ in f['params']] == params), None)


def word(to, sig, *args):
    """First 32-byte word an eth_call returns, as an int (None when the call reverts)."""
    try:
        raw = cast('call', to, sig, *args, '--rpc-url', RPC, timeout=20)
        return int(raw[2:66], 16) if len(raw) >= 66 else None
    except (RuntimeError, ValueError):
        return None


def has_selector(addr, sig):
    try:
        return cast('sig', sig)[2:].lower() in cast('code', addr, '--rpc-url', RPC, timeout=20).lower()
    except RuntimeError:
        return False


def parse_presale(text):
    """The pool, token and functions a pasted instruction text describes; raises UsageError when it can't tell."""
    fns = parse_functions(text)
    buy, sell = trade_fn(fns, 'buy'), trade_fn(fns, 'sell')
    actions = [f for f in fns.values() if ACTION_RE.match(f['name']) and f not in (buy, sell)]
    for f in actions:  # pays BNB when its own line talks about value / price / BNB
        line = next((x for x in text.splitlines() if f['name'] + '(' in x), '')
        f['pays'] = bool(re.search(r'(?i)\bvalue\b|\bbnb\b|price|payable|\bpay', line))
    if not buy and not sell and not actions:
        raise UsageError(L('No buy, sell, mint or claim function found in the text (e.g. buy(uint256 amountIn, …), mint(uint256 qty)).\n'
                         'Use /fn contract_address to list its functions, then /tx to call one.', '说明里找不到买、卖或铸造、领取这类函数（例如 buy(uint256 amountIn, …)、mint(uint256 qty)）。\n'
                         '可以先用 /fn 合约地址 看有哪些函数，再用 /tx 调用。'))
    addrs = list(dict.fromkeys(a.lower() for a in ADDR_ANY.findall(text)))
    pool = token = None
    for line in text.splitlines():
        for a in ADDR_ANY.findall(line):
            label = line.lower().split(a.lower())[0]
            if 'token' in label and not POOL_WORDS.search(label.replace('token', '')) and not token:
                token = a.lower()
            elif POOL_WORDS.search(label) and not pool:
                pool = a.lower()
    trade_sig = (buy or sell or actions[0])['sig']
    if not pool:
        pool = next((a for a in addrs if a != token and has_selector(a, trade_sig)), None)
    if not pool:
        raise UsageError(L(f"Can't tell which address is the presale pool (none has on-chain code containing {trade_sig}).\nPaste the pool address directly and use the buttons.", f'找不到哪个地址是预售池（没有地址的链上代码含 {trade_sig}）。\n可以直接把池子地址贴给我，用按钮操作。'))
    if not token:
        token = next((a for a in addrs if a != pool and word(a, 'decimals()') is not None), None)
    sym = token_names(token)[0] if token else '?'
    first = text.strip().splitlines()[0][:80]
    return {'pool': pool, 'token': token, 'symbol': sym, 'decimals': (word(token, 'decimals()') if token else None) or 18,
            'buy': buy, 'sell': sell, 'title': first, 'actions': actions,
            'quote_buy': helper_fn(fns, r'^(quote|get|calc|preview)\w*buy|buy\w*(quote|out)', ['uint256']),
            'quote_sell': helper_fn(fns, r'^(quote|get|calc|preview)\w*sell|sell\w*(quote|out)', ['uint256']),
            'limit': helper_fn(fns, r'limit|left|remaining|allowance', ['address']),
            'grad': helper_fn(fns, r'graduat|migrat|launched|listed', []),
            'views': [f for f in fns.values() if [t for t, _ in f['params']] in ([], ['address'])
                      and f not in [buy, sell] + actions
                      and f['name'] not in ('approve', 'balanceOf', 'allowance', 'transfer')]}


def units(n, decimals):
    return cn_num(Decimal(n) / Decimal(10) ** decimals)


def fill_args(fn, w, amount, minimum):
    """Arguments for a buy/sell call: the amount, the minimum out, the wallet itself for addresses."""
    args, used = [], False
    for t, n in fn['params']:
        low = n.lower()
        if t == 'address':
            args.append(ZERO_ADDR if re.search(r'ref|invit|affil', low) else WALLETS[w]['addr'])
        elif t.startswith('uint') and 'min' in low:
            args.append(str(minimum))
        elif t.startswith('uint') and 'deadline' in low:
            args.append(str(int(time.time()) + 600))
        elif t.startswith('uint') and not used:
            args.append(str(amount))
            used = True
        else:
            raise UsageError(L(f"Can't read the {fn['name']} parameter \"{t} {n}\".\nPaste the pool address directly, tap \"{fn['name']}\" and fill it in yourself.", f"看不懂 {fn['name']} 的参数「{t} {n}」。\n可以直接把池子地址贴给我，按「{fn['name']}」再自己填。"))
    return args


def read_label(name):
    """'buyLimit' → 'how much you can still buy'; unknown names stay as they are."""
    return READ_CN.get(name.lower(), name)


def pretty_value(v, name, decimals):
    """A read_view result in plain words: timestamps as local time, big integers as token amounts."""
    m = re.match(L(r'^(\d+)(?:\s|[(\uff08]|$)', r'^(\d+)(?:\s|（|$)'), v or '')
    if not m:
        return v
    n = int(m.group(1))
    if re.search(r'(?i)time|deadline|start|end|open|close', name) and 10 ** 9 <= n < 4 * 10 ** 9:
        return local_time(n)
    if n >= 10 ** 9:
        return units(n, decimals)
    return f'{n:,}'


def graduated(ps):
    return bool(ps['grad'] and word(ps['pool'], ps['grad']['sig']))


def presale_view(pid):
    ps = PRESALES[pid]
    pool, token, sym, d = ps['pool'], ps['token'], ps['symbol'], ps['decimals']
    jobs = {f'bnb{w}': (lambda w=w: native_balance(w)) for w in WALLETS}
    if token:
        jobs.update({f'hold{w}': (lambda w=w: word(token, 'balanceOf(address)', WALLETS[w]['addr'])) for w in WALLETS})
    for i, f in enumerate(ps['views']):
        jobs[f'view{i}'] = (lambda f=f: pretty_value(read_view(pool, f), f['name'], d) if not f['params'] else
                            L(' | ', '｜').join(f"{w} {pretty_value(read_view(pool, f, WALLETS[w]['addr']), f['name'], d)}"
                                     for w in WALLETS))
    p = load_prefs()
    sample = next((a for a in p['buy'] if 0 < dec(a) <= MAX_BNB), None)
    if ps['quote_buy'] and sample:
        jobs['quote'] = lambda: word(pool, ps['quote_buy']['sig'], str(to_wei(sample)))
    jobs['grad'] = lambda: graduated(ps)
    r = dict(zip(jobs, each(list(jobs), lambda k: safe(jobs[k]))))
    ps['done'] = bool(r['grad'])
    addr_link = lambda a: link(f'https://bscscan.com/address/{a}', short(a))  # noqa: E731
    state = L('🎓 Graduated', '🎓 已毕业') if ps['done'] else (L('🟢 Open to buy', '🟢 可以买') if ps['buy'] else '')
    lines = [L(f'📋 <b>{esc(sym)} presale</b>  {state}', f'📋 <b>{esc(sym)} 预售</b>　{state}').rstrip(),
             L(f"Pool {addr_link(pool)} | Token {addr_link(token) if token else 'not found'}", f"池子 {addr_link(pool)}｜代币 {addr_link(token) if token else '找不到'}"), '']
    for i, f in enumerate(ps['views']):
        v = r.get(f'view{i}')
        lines.append(L(f"{esc(read_label(f['name']))}  {esc(v) if v is not None else 'unreadable'}", f"{esc(read_label(f['name']))}　{esc(v) if v is not None else '读不到'}"))
    if r.get('quote'):
        lines.append(L(f"💰 {sample} BNB ≈ <b>{units(r['quote'], d)}</b> {esc(sym)}", f"💰 {sample} BNB 约换 <b>{units(r['quote'], d)}</b> {esc(sym)}"))
    if ps['done']:
        lines.append(L('Presale pool is closed. Tap "Trade card (DEX)" below to trade on DEX', '预售池不能再买卖了，按下方「开交易卡片」到 DEX 交易'))
    lines += ['', L('💎 Position  ', '💎 持仓　') + L(' | ', '｜').join(f"{w} {units(r[f'hold{w}'], d) if r.get(f'hold{w}') is not None else '?'}"
                                        for w in WALLETS) if token else L('💎 Position: token address not found', '💎 持仓：找不到代币地址'),
              L('💳 BNB  ', '💳 BNB　') + L(' | ', '｜').join(f"{w} {fmt(r[f'bnb{w}'], 4) if r.get(f'bnb{w}') is not None else '?'}"
                                   for w in WALLETS),
              L(f"👛 Wallets {''.join(ps['wallets'])} | updated {time.strftime('%H:%M', time.gmtime(time.time() + 8 * 3600))}", f"👛 用 {''.join(ps['wallets'])} 钱包｜{time.strftime('%H:%M', time.gmtime(time.time() + 8 * 3600))} 更新")]
    q = lambda op, arg='': f'q:{pid}:{op}:{arg}'  # noqa: E731
    rows = [[{'text': L('🔄 Refresh', '🔄 刷新'), 'callback_data': q('r')},
             {'text': L(f"👛 Wallet: {''.join(ps['wallets'])} 🔁", f"👛 钱包：{''.join(ps['wallets'])} 🔁"), 'callback_data': q('w')}]]
    if ps['done']:
        rows.append([{'text': L('📈 Trade card (DEX)', '📈 开交易卡片（DEX）'), 'callback_data': f'o:t:{token}'}] if token else [])
    else:
        if ps['buy']:
            rows += rows_of([{'text': L(f'Buy {a}', f'买 {a}'), 'callback_data': q('b', a)} for a in p['buy'] if 0 < dec(a) <= MAX_BNB]
                            + [{'text': L('Buy x ✏️', '买 x ✏️'), 'callback_data': q('X')}])
        if ps['sell'] and token:
            rows += rows_of([{'text': L(f'🔴 Sell {s}%', f'🔴 卖 {s}%'), 'callback_data': q('s', s)} for s in p['sell']]
                            + [{'text': L('🔴 Sell x% ✏️', '🔴 卖 x% ✏️'), 'callback_data': q('S')}])
    # claim / refund / mint usually still matter after graduation, so these stay
    rows += rows_of([{'text': f"⚡ {fn_label(f['name'])}" + (' ✏️' if f['params'] and any(t != 'address' for t, _ in f['params'])
                                                               or f['pays'] else ''), 'callback_data': q('a', i)}
                     for i, f in enumerate(ps['actions'][:9])], 2)
    return '\n'.join(lines), [row for row in rows if row]


def presale_action(pid, i, typed=None):
    """Any other function the text names. Address-only, unpaid ones go straight to a preview; the rest ask for input."""
    ps = PRESALES[pid]
    f = ps['actions'][i]
    auto = all(t == 'address' for t, _ in f['params']) and not f['pays']
    if typed is None and not auto:
        ask_input(pid, f'qact:{i}', action_prompt(fn_label(f['name']), [t for t, _ in f['params']],
                                                  [n for _, n in f['params']], f['pays']))
        return
    args = ['me'] * len(f['params']) if typed is None else [x for x in typed.split() if x != '-']
    cmd_tx([''.join(ps['wallets']), ps['pool'], f['sig'], *args])


def show_presale(text):
    ps = parse_presale(text)
    now = time.time()
    for k in [k for k, v in PRESALES.items() if v['expires'] < now]:
        del PRESALES[k]
    pid = secrets.token_hex(3)
    PRESALES[pid] = dict(ps, wallets=default_wallets(), expires=now + PANEL_TTL, msg=None)
    body, buttons = presale_view(pid)
    mid = say(body, buttons, rich=True)
    PRESALES[pid]['msg'] = (OWNER, mid) if mid else None


def on_presale(data, msg):
    _, pid, op, arg = (data.split(':', 3) + ['', '', ''])[:4]
    ps = PRESALES.get(pid)
    if not ps or ps['expires'] < time.time():
        say(L('This presale card has expired, please paste the instructions again.', '这张预售卡片已过期，请重新贴一次说明。'))
        return
    if msg:
        ps['msg'] = (msg['chat']['id'], msg['message_id'])
    ps['expires'] = time.time() + PANEL_TTL
    if op == 'w':
        ps['wallets'] = next_wallets(ps['wallets'])
    if op in ('r', 'w'):
        ps['msg'] = edit_or_send(ps['msg'], *presale_view(pid), rich=True)
    elif op == 'b':
        presale_buy(pid, arg)
    elif op == 's':
        presale_sell(pid, arg)
    elif op == 'X':
        ask_input(pid, 'qbuy', L('How much BNB should each wallet spend? e.g. 0.02', '请输入每个钱包要用多少 BNB 买，例如 0.02'))
    elif op == 'S':
        ask_input(pid, 'qsell', L('What % of the position to sell? e.g. 30', '请输入要卖掉持仓的几 %，例如 30'))
    elif op == 'a' and arg.isdigit() and int(arg) < len(ps['actions']):
        presale_action(pid, int(arg))


def presale_buy(pid, amount_text):
    ps = PRESALES[pid]
    if not ps['buy']:
        raise UsageError(L('No buy function found in the instructions.', '说明里没有买的函数。'))
    if graduated(ps):
        raise UsageError(L('Already graduated; the presale pool no longer accepts buys. Paste the token address to open a trade card and buy on DEX.', '已毕业，预售池不能再买；请贴代币地址开交易卡片，到 DEX 买。'))
    amount = need_amount(amount_text)
    wei = to_wei(amount)
    sym, d = ps['symbol'], ps['decimals']

    def prep(w):
        out = word(ps['pool'], ps['quote_buy']['sig'], str(wei)) if ps['quote_buy'] else None
        left = word(ps['pool'], ps['limit']['sig'], WALLETS[w]['addr']) if ps['limit'] else None
        if left == 0:
            return L(f'⚠️ {w} skipped: quota used up', f'⚠️ {w} 不买：额度用完了')
        expect = min(x for x in (out, left) if x is not None) if out is not None else None
        minimum = int(Decimal(expect) * PRESALE_MIN) if expect else 0
        data = cast('calldata', ps['buy']['sig'], *fill_args(ps['buy'], w, wei, minimum))
        note = (L(f"{fmt(amount)} BNB → ~{units(expect, d)} {sym} (min {units(minimum, d)})", f"{fmt(amount)} BNB → 约 {units(expect, d)} {sym}（最少 {units(minimum, d)}）") if expect else
                L(f'Buy with {fmt(amount)} BNB (no quote available; check the balance changes below)', f'{fmt(amount)} BNB 买（没有报价可读，看下面的余额变化）'))
        if out is not None and left is not None and left < out:
            note += L(f"\n  ⚠️ Only {units(left, d)} quota left, less than the quote; the excess will be refunded or the tx will fail", f"\n　⚠️ 只剩 {units(left, d)} 额度，比报价少，多的会退回或整笔失败")
        return w, ps['pool'], wei, data, note
    results = each(ps['wallets'], prep)
    for r in results:
        if isinstance(r, str):
            say(r)
    calls = [r for r in results if not isinstance(r, str)]
    if calls:
        preview_calls(L(f'🟢 Presale buy | {sym}', f'🟢 预售买入｜{sym}'), calls)


def presale_sell(pid, pct_text):
    ps = PRESALES[pid]
    token, pool, sym, d = ps['token'], ps['pool'], ps['symbol'], ps['decimals']
    if not ps['sell'] or not token:
        raise UsageError(L('No sell function or token address found in the instructions.', '说明里没有卖的函数或代币地址。'))
    if graduated(ps):
        raise UsageError(L('Already graduated; the presale pool no longer accepts sells. Paste the token address to open a trade card and sell on DEX.', '已毕业，预售池不能再卖；请贴代币地址开交易卡片，到 DEX 卖。'))
    pct = need_amount(pct_text, cap=False)
    if pct > 100:
        raise UsageError(L('Percentage cannot exceed 100.', '百分比不能超过 100。'))

    def prep(w):
        addr = WALLETS[w]['addr']
        bal = word(token, 'balanceOf(address)', addr) or 0
        amount = int(Decimal(bal) * pct / 100)
        if amount <= 0:
            return 'skip', L(f'⚠️ {w} skipped: no {sym}', f'⚠️ {w} 不卖：没有 {sym}')
        if (word(token, 'allowance(address,address)', addr, pool) or 0) < amount:
            return 'approve', (w, token, 0, cast('calldata', 'approve(address,uint256)', pool, str(amount)),
                               L(f'Allow the presale pool to take {units(amount, d)} {sym}', f'允许预售池拿走 {units(amount, d)} {sym}'))
        out = word(pool, ps['quote_sell']['sig'], str(amount)) if ps['quote_sell'] else None
        minimum = int(Decimal(out) * PRESALE_MIN) if out else 0
        data = cast('calldata', ps['sell']['sig'], *fill_args(ps['sell'], w, amount, minimum))
        return 'sell', (w, pool, 0, data, L(f'{units(amount, d)} {sym} ({fmt(pct)}%)', f'{units(amount, d)} {sym}（{fmt(pct)}%）')
                        + (L(f' → ~{fmt(from_wei(out))} BNB (min {fmt(from_wei(minimum))})', f' → 约 {fmt(from_wei(out))} BNB（最少 {fmt(from_wei(minimum))}）') if out else
                           L(' (no quote available; check the balance changes below)', '（没有报价可读，看下面的余额变化）')))
    results = each(ps['wallets'], prep)
    for kind, item in results:
        if kind == 'skip':
            say(item)
    approvals = [item for kind, item in results if kind == 'approve']
    sells = [item for kind, item in results if kind == 'sell']
    if approvals:
        preview_calls(L(f'Sell step 1/2: approve {sym} first (step 2 appears once it confirms)', f'卖出第 1／2 步：先授权 {sym}（上链后自动出第 2 步）'), approvals,
                      after=lambda done: presale_sell(pid, pct_text))
    elif sells:
        preview_calls(L(f'🔴 Presale sell | {sym}', f'🔴 预售卖出｜{sym}'), sells)


# ---------------------------------------------------------------- contract calls

TOKEN_LABELS = {}


def token_label(addr):
    """(symbol, decimals) of a token, cached; falls back to the short address and 18."""
    key = (addr or '').lower()
    if key not in TOKEN_LABELS:
        sym = safe(lambda: token_names(addr)[0]) or short(addr)
        TOKEN_LABELS[key] = (sym, word(addr, 'decimals()') if addr else None)
    sym, d = TOKEN_LABELS[key]
    return sym, 18 if d is None else d


def change_text(c):
    raw = int(Decimal(str(c.get('change') or 0)))  # baw's JSON gives big changes as floats like 1.18e+24
    sign = '+' if raw >= 0 else '−'
    if c.get('tokenType') == 'Native':
        return f'BNB {sign}{fmt(from_wei(abs(raw)))}'
    sym, d = token_label(c.get('contractAddress'))
    return f'{sym} {sign}{cn_num(Decimal(abs(raw)) / Decimal(10) ** d)}'


def preview_calls(title, calls, after=None, extra=None, intro=()):
    """calls: [(wallet, to, value_wei, data, note)]. Each is simulated and previewed; one Confirm executes all that pass.
    after(done): run once those transactions are mined, with the wallets whose calls all succeeded (e.g. preview the
    sell that an approval was for). intro: extra lines (HTML) under the title."""
    def one(call):
        w, to, value, data, note = call
        skip = lambda why: (None, L(f'⚠️ <b>{w}</b>  not sent: {esc(why)}\n  ({esc(note)})', f'⚠️ <b>{w}</b>　不送：{esc(why)}\n　（{esc(note)}）'))  # noqa: E731
        bad = wallet_mismatch(w)
        if bad:
            return skip(bad)
        s = baw(w, 'wallet', 'settings')
        dev = ((s.get('data') or {}).get('devMode') or {}) if s.get('success') else {}
        if not dev.get('enabled'):
            return skip(L('Developer mode is off (enable it in the Binance app)', '开发者模式没开（要在币安 App 打开）'))
        err = eth_call_error(w, to, value, data)
        if err:
            return skip(L(f'On-chain simulation failed: {err}', f'链上模拟失败：{err}'))
        r = baw(w, 'contract-call', 'preview', '--binanceChainId', CHAIN, '--from', WALLETS[w]['addr'],
                '--to', to, '--value', str(value), '--inputData', data)
        if not r.get('success'):
            return skip(L(f'Preview failed: {baw_err(r)}', f'预览失败：{baw_err(r)}'))
        d = r['data']
        REQUESTS[d['requestId']] = (to.lower(), int(value), data.lower())
        sim = d.get('simulationResult') or {}
        risks = d.get('risks') or {}
        if sim.get('simulationErrorDetail'):
            return skip(L(f"Preview simulation failed: {sim.get('simulationErrorDetail')}", f"预览模拟失败：{sim.get('simulationErrorDetail')}"))
        changes = L(', ', '、').join(change_text(c) for c in sim.get('balanceChanges') or []) or L('no change', '没有变化')
        text = L(f'<b>{w}</b>  {esc(note)}\n  💱 {esc(changes)}', f'<b>{w}</b>　{esc(note)}\n　💱 {esc(changes)}')
        if value:
            text += L(f' | pay {fmt(from_wei(value))} BNB', f'｜付 {fmt(from_wei(value))} BNB')
        if sim.get('authorityChanges'):
            text += L(f"\n  🔑 Approval changes: {esc(sim['authorityChanges'])}", f"\n　🔑 授权变更：{esc(sim['authorityChanges'])}")
        if risks.get('riskDetails') or risks.get('riskBehaviors'):
            text += L(f"\n  ⚠️ Risk: {esc(risks.get('riskDetails') or '')} {esc(risks.get('riskBehaviors') or '')}", f"\n　⚠️ 风险：{esc(risks.get('riskDetails') or '')} {esc(risks.get('riskBehaviors') or '')}").rstrip()
        return (w, d['requestId']), text

    results = each(calls, one)
    targets = {to.lower() for _, to, *_ in calls}
    head = [f'📝 <b>{esc(title)}</b>']
    if len(targets) == 1:
        head.append(L(f'Contract {link("https://bscscan.com/address/" + calls[0][1], short(calls[0][1]))}', f'合约 {link("https://bscscan.com/address/" + calls[0][1], short(calls[0][1]))}'))
    lines = head + list(intro) + [''] + [t for _, t in results]
    ok = [item for item, _ in results if item]
    if not ok:
        say('\n'.join(lines + ['', L('No transactions to send.', '没有可以送出的交易。')]), rich=True)
        return
    lines.append(L('\n💱 = simulated balance changes; check that the numbers make sense', '\n💱 是模拟后的余额变化，请确认数字合理'))
    skipped = {w for (w, *_), (item, _) in zip(calls, results) if not item}

    def action():
        done = do_execute(ok)
        if after:
            after([w for w in done if w not in skipped])
    ask_confirm('\n'.join(lines), L(f"Confirm & send ({', '.join(w for w, _ in ok)})", f"确认送出（{'、'.join(w for w, _ in ok)}）"), action, rich=True, extra=extra)


def do_execute(items):
    """Execute previewed requests in order; returns the wallets whose transactions all went through."""
    good, bad = [], set()
    for w, request_id in items:
        bad.add(w)  # cleared below once this one is mined fine
        key = REQUESTS.get(request_id)
        dkey = ('call', w, *key) if key else None
        if dkey and is_repeat(dkey):
            say(L(f'⚠️ <b>{w}</b> the same order was already sent within {DUP_WINDOW}s, not sending again (duplicate guard).', f'⚠️ <b>{w}</b> 同一笔 {DUP_WINDOW} 秒内已经送过，这次不送（防止重复）。'), rich=True)
            continue
        locked = wait_unlocked(w)
        if locked:
            unsend(dkey)
            say(L(f'🔒 <b>{w}</b> not sent this time: {esc(locked)}', f'🔒 <b>{w}</b> 这次没送：{esc(locked)}'), rich=True)
            continue
        r = baw(w, 'contract-call', 'execute', '--requestId', request_id)
        logging.info('execute %s %s: %s', w, request_id, r.get('data') if r.get('success') else baw_raw(r))
        log_order('call', w, to=(key or ('',))[0], ok=r.get('success'), data=r.get('data'),
                  error=None if r.get('success') else baw_raw(r))
        if not r.get('success'):
            err = baw_err(r)
            unknown = re.search(UNKNOWN_RE, baw_raw(r))
            if not unknown:
                unsend(dkey)
            if unknown:
                # the request may have reached Binance: never invite a second press
                say(L(f'⚠️ <b>{w}</b> no response after sending, <b>result unknown</b> ({esc(err)})\n'
                    'Do not retry yet: wait a minute, then check "👤 Overview" or "Recent trades" for this tx.', f'⚠️ <b>{w}</b> 送出后没收到回复，<b>结果未知</b>（{esc(err)}）\n'
                    '先别重按：等一分钟看「👤 总览」或「最近成交」有没有这笔。'),
                    [[{'text': L('👤 Overview', '👤 看总览'), 'callback_data': 'm:/me'}]], rich=True)
            else:
                hint = '' if re.search(r'(?i)simulat', baw_raw(r)) else L('\nThe preview may have expired; go back to the card and tap again.', '\n预览可能过期了，回到卡片再按一次。')
                say(L(f'❌ <b>{w}</b> not sent: {esc(err)}{hint}', f'❌ <b>{w}</b> 没送出：{esc(err)}{hint}'), rich=True)
            continue
        d = r.get('data') or {}
        tx = d.get('txHash')
        if not tx and d.get('status') == 'PENDING_CONFIRMATION':
            say(L(f'📱 <b>{w}</b> needs <b>one more confirmation in the Binance app</b> (risk check, valid for 5 min)\n'
                'Until you confirm or reject it, this wallet cannot send other transactions. I will notify you when done.', f'📱 <b>{w}</b> 这笔要在<b>币安 App 再确认一次</b>（风控触发，5 分钟内有效）\n'
                '确认或拒绝之前，这个钱包不能送别的交易。处理完我会通知你。'), rich=True)
            run_bg(lambda w=w: notify_unlock(w))
            continue
        if not tx:
            say(L(f"⏳ <b>{w}</b> status: {esc(d.get('status'))}", f"⏳ <b>{w}</b> 状态：{esc(d.get('status'))}"), rich=True)
            continue
        say(L(f'⏳ <b>{w}</b> sent, waiting for confirmation…', f'⏳ <b>{w}</b> 已送出，等上链…'), rich=True)
        res = receipt(tx)
        say(L(f'{esc(res)} | <b>{w}</b>  {tx_link(tx)}', f'{esc(res)}｜<b>{w}</b>　{tx_link(tx)}'), [[{'text': L('👤 Overview', '👤 看总览'), 'callback_data': 'm:/me'}]], rich=True)
        if res.startswith('✅'):
            good.append(w)
            if [x for x, _ in items].count(w) == good.count(w):
                bad.discard(w)
    return [w for w in dict.fromkeys(good) if w not in bad]


def http_json(url, timeout=20):
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def implementation_of(addr):
    """EIP-1967 proxy implementation address, or None."""
    slot = cast('storage', addr, EIP1967_IMPL, '--rpc-url', RPC)
    return '0x' + slot[-40:] if int(slot, 16) else None


def abi_type(p):
    t = p.get('type', '')
    if t.startswith('tuple'):
        return '(' + ','.join(abi_type(c) for c in p.get('components') or []) + ')' + t[5:]
    return t


def verified_abi(addr):
    try:
        abi = http_json(f'https://sourcify.dev/server/v2/contract/{CHAIN}/{addr}?fields=abi').get('abi')
        if abi:
            return L('Verified source (Sourcify)', 'Sourcify 已验证原代码'), abi
    except Exception:  # noqa: BLE001 - fall through to the next source
        pass
    if BSCSCAN_KEY:
        try:
            r = http_json('https://api.etherscan.io/v2/api?chainid=56&module=contract&action=getabi'
                          f'&address={addr}&apikey={BSCSCAN_KEY}')
            if str(r.get('status')) == '1':
                return L('Verified source (BscScan)', 'BscScan 已验证原代码'), json.loads(r['result'])
        except Exception:  # noqa: BLE001
            pass
    return None, None


def contract_functions(addr):
    """(source label, [{sig, mut, names, out, selector}]) for every function a contract exposes."""
    source, abi = verified_abi(addr)
    if abi:
        fns = []
        for f in abi:
            if f.get('type') == 'function':
                inputs = f.get('inputs') or []
                sig = f"{f['name']}({','.join(abi_type(i) for i in inputs)})"
                fns.append({'sig': sig, 'mut': f.get('stateMutability'), 'selector': None,
                            'names': ', '.join(i.get('name') or '_' for i in inputs),
                            'out': ','.join(abi_type(o) for o in f.get('outputs') or [])})
        return source, fns
    # unverified: read selectors from the bytecode and look their names up in public signature databases
    text = cast('selectors', cast('code', addr, '--rpc-url', RPC), '--resolve', timeout=180)
    fns = []
    for line in text.splitlines():
        cols = [c.strip() for c in line.split('\t')]
        if len(cols) < 3:
            continue
        name = cols[3].split('|')[0] if len(cols) > 3 and cols[3] else None
        fns.append({'sig': name or L(f'unknown function {cols[0]}({cols[1]})', f'未知函数 {cols[0]}({cols[1]})'), 'mut': cols[2], 'selector': cols[0],
                    'names': '', 'out': None})
    return L('Not verified; decoded from bytecode (names are guesses, trust the preview)', '未验证，从链上代码反查（名称是猜的，以预览结果为准）'), fns


def resolve_target(addr):
    impl = implementation_of(addr)
    source, fns = contract_functions(impl or addr)
    return L(f"{'Proxy, implementation at ' + impl + ' | ' if impl else ''}{source}", f"{'代理合约，实际程序在 ' + impl + '｜' if impl else ''}{source}"), fns


def cmd_fn(args):
    if len(args) != 1:
        raise UsageError(L('Just paste the contract address and I will list the available actions.', '直接把合约地址贴给我就好，我会列出能按的动作。'))
    show_contract(need_addr(args[0]))


# ---------------------------------------------------------------- contract menu
#
# Paste any project contract: its write functions become buttons, and each button asks for its parameters in plain
# words, so nothing has to be typed as a command. Every call is still simulated and previewed before it is sent.

CONTRACTS = {}  # card id -> {addr, source, writes, reads, wallets, expires, msg}
SKIP_WRITES = {'approve', 'transfer', 'transferfrom', 'renounceownership', 'transferownership', 'setapprovalforall',
               'safetransferfrom', 'increaseallowance', 'decreaseallowance', 'permit', 'multicall', 'upgradeto',
               'upgradetoandcall', 'initialize'}
MAX_ACTIONS = 12


def plain_types(sig):
    """'claim(address,uint256)' → ['address', 'uint256']; None when a tuple or an unknown name can't be typed in."""
    m = re.match(r'^([A-Za-z_]\w*)\((.*)\)$', sig or '')
    if not m or '(' in m.group(2):
        return None
    return [t.strip() for t in m.group(2).split(',') if t.strip()]


def is_contract(addr):
    try:
        return cast('code', addr, '--rpc-url', RPC, timeout=20) not in ('', '0x')
    except RuntimeError:
        return False


def open_address(addr):
    """A pasted address: a token opens the trade card, any other contract opens the action menu."""
    if word(addr, 'decimals()') is None and is_contract(addr):
        show_contract(addr)
    else:
        show_panel(addr)


def show_contract(addr):
    label, fns = resolve_target(addr)
    writes, reads = [], []
    for f in fns:
        types = plain_types(f['sig'])
        if types is None:
            continue
        name = f['sig'].split('(')[0]
        names = [n.strip().strip('_') for n in f['names'].split(',')] if f['names'] else []
        item = dict(f, name=name, types=types, names=(names + [''] * len(types))[:len(types)])
        if f['mut'] in ('nonpayable', 'payable') and name.lower() not in SKIP_WRITES and not name.startswith('set'):
            writes.append(item)
        elif f['mut'] in ('view', 'pure') and types in ([], ['address']) and RULE_RE.search(name):
            reads.append(item)
    # the usual actions (buy, mint, claim, refund…) first
    writes.sort(key=lambda f: 0 if ACTION_RE.match(f['name']) or re.match(r'(?i)buy|purchase|sell', f['name']) else 1)
    now = time.time()
    for k in [k for k, v in CONTRACTS.items() if v['expires'] < now]:
        del CONTRACTS[k]
    cid = secrets.token_hex(3)
    CONTRACTS[cid] = {'addr': addr, 'source': label, 'writes': writes[:MAX_ACTIONS], 'more': len(writes) > MAX_ACTIONS,
                      'reads': reads[:8], 'wallets': default_wallets(), 'expires': now + PANEL_TTL,
                      'msg': None}
    mid = say(*contract_view(cid), rich=True)
    CONTRACTS[cid]['msg'] = (OWNER, mid) if mid else None


def contract_view(cid):
    c = CONTRACTS[cid]
    addr = c['addr']
    trusted = L('Verified', '已验证') in c['source']
    lines = [L(f"📜 <b>Project contract</b>  {link('https://bscscan.com/address/' + addr, short(addr))}", f"📜 <b>项目合约</b>　{link('https://bscscan.com/address/' + addr, short(addr))}"),
             L('✅ Source code verified', '✅ 原代码已公开') if trusted else L('⚠️ Source not verified; action names are guesses, trust the preview', '⚠️ 原代码没公开，动作名称是猜的，以预览为准')]
    if c['reads']:
        lines += ['', L('📖 On-chain settings', '📖 链上设置')]
        vals = each(c['reads'], lambda f: safe(
            lambda: pretty_value(read_view(addr, f), f['name'], 18) if not f['types'] else
            L(' | ', '｜').join(f"{w} {pretty_value(read_view(addr, f, WALLETS[w]['addr']), f['name'], 18)}" for w in WALLETS)))
        lines += [L(f"{esc(read_label(f['name']))}  {esc(v) if v is not None else 'unreadable'}", f"{esc(read_label(f['name']))}　{esc(v) if v is not None else '读不到'}") for f, v in zip(c['reads'], vals)]
    lines.append('')
    if c['writes']:
        lines.append(L('Tap an action below and I will ask what to fill in, step by step; 💰 = pays BNB', '按下面的动作，我会一步步问你要填什么；💰＝要付 BNB'))
        if c['more']:
            lines.append(L(f'(only the first {MAX_ACTIONS} common ones are listed)', f'（只列前 {MAX_ACTIONS} 个常用的）'))
    else:
        lines.append(L('This contract has no actions to tap.', '这个合约没有能按的动作。'))
    lines.append(L(f"👛 Wallets {''.join(c['wallets'])}", f"👛 用 {''.join(c['wallets'])} 钱包"))
    rows = rows_of([{'text': fn_label(f['name']) + (' 💰' if f['mut'] == 'payable' else ''),
                     'callback_data': f'c:{cid}:f:{i}'} for i, f in enumerate(c['writes'])], 2)
    rows.append([{'text': L(f"👛 Wallet: {''.join(c['wallets'])} 🔁", f"👛 钱包：{''.join(c['wallets'])} 🔁"), 'callback_data': f'c:{cid}:w:'},
                 {'text': L('🔄 Refresh', '🔄 刷新'), 'callback_data': f'c:{cid}:r:'}])
    return '\n'.join(lines), rows


def on_contract(data):
    _, cid, op, arg = (data.split(':', 3) + ['', '', ''])[:4]
    c = CONTRACTS.get(cid)
    if not c or c['expires'] < time.time():
        say(L('This contract card has expired, please paste the contract address again.', '这张合约卡片已过期，请重新贴一次合约地址。'))
        return
    c['expires'] = time.time() + PANEL_TTL
    if op == 'w':
        c['wallets'] = next_wallets(c['wallets'])
    if op in ('w', 'r'):
        c['msg'] = edit_or_send(c['msg'], *contract_view(cid), rich=True)
    elif op == 'f' and arg.isdigit() and int(arg) < len(c['writes']):
        contract_fn(cid, int(arg))


def contract_fn(cid, i, typed=None):
    c = CONTRACTS[cid]
    f = c['writes'][i]
    pays = f['mut'] == 'payable'
    ws = ''.join(c['wallets'])
    if typed is None:
        if not f['types'] and not pays:
            cmd_tx([ws, c['addr'], f['sig']])
            return
        hint = L('\nNot sure of the max? Type ? in that field (or "pay ?") and I will find the limit by on-chain simulation', '\n不知道最多能填多少：在那一格打 ?（或打「付 ?」），我会用链上模拟试出上限') if f['types'] or pays else ''
        ask_input(cid, f'cfn:{i}', action_prompt(fn_label(f['name']), f['types'], f['names'], pays) + hint)
        return
    args = [x for x in typed.replace(*zh_word('，'), ' ').split() if x != '-']
    if len(args) >= 2 and args[-2].lower() in PAY_WORDS:
        args[-2:] = [args[-2] + args[-1]]
    paid = args.pop() if args and pay_word(args[-1]) else None
    if len(args) != len(f['types']):
        raise UsageError(L(f"{fn_label(f['name'])} needs {len(f['types'])} values, you gave {len(args)}.\n\n", f"{fn_label(f['name'])} 要填 {len(f['types'])} 个，你填了 {len(args)} 个。\n\n")
                         + action_prompt(fn_label(f['name']), f['types'], f['names'], pays))
    if '?' in args or (paid and '?' in paid):
        cmd_max([ws, c['addr'], f['sig'], *args, 'value=' + (paid[len(pay_word(paid)):].lower().removesuffix('bnb') if paid else '0')])
        return
    cmd_tx([ws, c['addr'], f['sig'], *args, *([paid] if paid else [])])


def tx_param(p, w):
    p = p.strip('"\'「」')
    if p.lower() in ('me',) + zh_word('我'):
        return WALLETS[w]['addr']
    if p.lower() in ('yes', 'no') + zh_word('是', '否'):
        return 'true' if p.lower() in ('yes',) + zh_word('是') else 'false'
    if p.lower().endswith('bnb') and dec(p[:-3]) > 0:
        return str(to_wei(p[:-3]))
    return p


def pop_value(rest):
    """Takes the BNB to send off the end of the arguments: value=0.05, pay0.05 or pay 0.05."""
    if len(rest) >= 2 and rest[-2].lower() in PAY_WORDS:
        rest[-2:] = [rest[-2] + rest[-1]]
    if rest and (rest[-1].lower().startswith('value=') or pay_word(rest[-1])):
        last = rest.pop()
        v = dec(last[6:] if last.lower().startswith('value=') else last[len(pay_word(last)):].lower().removesuffix('bnb'))
        if v < 0:
            raise UsageError(L('BNB to pay cannot be negative.', '要付的 BNB 不能是负的。'))
        return to_wei(need_amount(str(v))) if v > 0 else 0
    return 0


def cmd_tx(args):
    if len(args) == 1 and ADDR_RE.match(args[0]):
        show_contract(args[0])
        return
    if len(args) < 3:
        raise UsageError(L('Just paste the project contract address and I will list the available actions (mint, claim, refund…), then ask what to fill in step by step.', '直接把项目的合约地址贴给我就好，我会列出能按的动作（铸造、领取、退款…），再一步步问你要填什么。'))
    ws = parse_wallets(args[:1])
    to = need_addr(args[1])
    sig = args[2].strip('"\'')
    if not re.match(r'^[A-Za-z_]\w*\(.*\)$', sig):
        raise UsageError(L(f'Function must be written as name(type,type) with no spaces: {sig}', f'函数要写成 名称(类型,类型)，不能有空格：{sig}'))
    rest = list(args[3:])
    value = pop_value(rest)
    calls = []
    for w in ws:
        params = [tx_param(p, w) for p in rest]
        data = cast('calldata', sig, *params)
        shown = ' '.join(L('me', '我') if p.lower() == WALLETS[w]['addr'].lower() else short(p) if ADDR_RE.match(p) else p
                         for p in params)
        calls.append((w, to, value, data, f"{fn_label(sig.split('(')[0])} {shown}".strip()))
    saved = store_cmd('/tx ' + ' '.join(args))
    preview_calls(f"⚡ {fn_label(sig.split('(')[0])}", calls,
                  extra=[{'text': L('⭐ Save shortcut', '⭐ 存成快捷键'), 'callback_data': f'k:{saved}'}])


def cmd_copy(args):
    if not args or not re.match(r'^0x[0-9a-fA-F]{64}$', args[0]):
        raise UsageError(L('Usage: /copy TX_HASH [A|B|AB] [value=BNB]\nCopies a successful tx someone made on this project (mint, refund, limit order…)', '用法：/copy 交易hash [A|B|AB] [value=BNB]\n拿别人在这个项目成功的一笔（铸造、退款、挂单…）照抄'))
    rest = list(args[1:])
    value_override = pop_value(rest) if rest and rest[-1].lower().startswith('value=') else None
    ws = parse_wallets(rest)
    t = json.loads(cast('tx', args[0], '--json', '--rpc-url', RPC))
    if 'schema_version' in t:  # newer cast wraps results in an envelope
        t = t.get('data') or {}
    to, data, sender = t.get('to'), (t.get('input') or '0x').lower(), (t.get('from') or '').lower()
    if not to or len(data) < 10:
        raise UsageError(L('This is a plain transfer or contract deployment; it cannot be copied.', '这笔是单纯转账或部署合约，不能照抄。'))
    value = value_override if value_override is not None else int(str(t.get('value') or '0'), 0)
    if value > 0:
        need_amount(str(from_wei(value)))
    decoded = decode_call(data)
    padded_sender = sender[2:].rjust(64, '0')
    calls = []
    for w in ws:
        mine = WALLETS[w]['addr'][2:].lower().rjust(64, '0')
        new_data = data.replace(padded_sender, mine)
        swapped = L(' (their address in the params is replaced with yours)', '（参数里他的地址换成你的）') if new_data != data else ''
        calls.append((w, to, value, new_data, L(f'Copy {decoded}{swapped}', f'照抄 {decoded}{swapped}')))
    preview_calls(L('📋 Copy this tx', '📋 照抄这笔交易'), calls)


def decode_call(data):
    """'mint(uint256) 3' style text for calldata, through the public signature database."""
    try:
        lines = cast('4byte-calldata', data, timeout=30).splitlines()  # 1) "claim(address,uint256)", then one arg per line
        name = fn_label(re.search(r'([A-Za-z_]\w*)\(', lines[0]).group(1))
        args = [short(x) if ADDR_RE.match(x) else x for x in (l.strip() for l in lines[1:]) if x]
        return ' '.join([name] + args)[:200]
    except (RuntimeError, IndexError, AttributeError):
        return L(f'unknown action {data[:10]}', f'未知动作 {data[:10]}')


def show_copy(tx):
    """A pasted transaction hash: say what it did and offer to repeat it from A, B or both."""
    t = json.loads(cast('tx', tx, '--json', '--rpc-url', RPC))
    if 'schema_version' in t:
        t = t.get('data') or {}
    to, data = t.get('to'), (t.get('input') or '0x').lower()
    if not to or len(data) < 10:
        raise UsageError(L('This is a plain transfer or contract deployment; it cannot be copied.', '这笔是单纯转账或部署合约，不能照抄。'))
    value = int(str(t.get('value') or '0'), 0)
    ok = receipt(tx)
    lines = [L("📋 <b>Another user's tx</b>", '📋 <b>别人的一笔交易</b>'),
             L(f"{esc(ok)} | {tx_link(tx)}", f"{esc(ok)}｜{tx_link(tx)}"),
             L(f"Contract {link('https://bscscan.com/address/' + to, short(to))}", f"合约 {link('https://bscscan.com/address/' + to, short(to))}"),
             L(f'Action {esc(decode_call(data))}', f'动作 {esc(decode_call(data))}'), L(f'Pays {fmt(from_wei(value))} BNB', f'付 {fmt(from_wei(value))} BNB') if value else L('No BNB paid', '没有付 BNB'), '',
             L('Copy it? I will simulate it with your wallet and show a preview; nothing is sent until you confirm', '要照抄吗？我会用你的钱包模拟一次、出预览，你按确认才送')]
    buttons = rows_of([cmd_btn(L(f'Copy ({x})', f'照抄（{x}）'), f'/copy {tx} {x}') for x in dict.fromkeys([all_names(), *WALLETS])])
    buttons.append([cmd_btn(L('📜 Contract actions', '📜 看这个合约能做什么'), f'/fn {to}')])
    say('\n'.join(lines), buttons, rich=True)


# ---------------------------------------------------------------- platform rules

RULE_RE = re.compile(r'(?i)(max|min|cap|limit|per|price|cost|fee|start|end|open|close|dead|time|window|supply|mint|sold'
                     r'|remain|left|pause|white|allow|agent|eligib|claim|refund|slot|fill|total|launch|phase|stage|round'
                     r'|bought|paid|quota)')


def show_value(text):
    text = text.replace('\n', ' / ')[:200]
    first = text.split()[0] if text.split() else ''
    if first.isdigit() and int(first) >= 10 ** 12:
        text += L(f' (÷1e18 = {fmt(from_wei(int(first)))})', f'（÷1e18 = {fmt(from_wei(int(first)))}）')
    return text


def read_view(to, f, arg=None):
    if f.get('out'):
        out = cast('call', to, f"{f['sig']}({f['out']})", *([arg] if arg else []), '--rpc-url', RPC)
        return {'true': L('yes', '是'), 'false': L('no', '否')}.get(out, show_value(out))
    selector = f.get('selector') or cast('sig', f['sig'])
    raw = cast('call', to, selector + (arg[2:].lower().rjust(64, '0') if arg else ''), '--rpc-url', RPC)[2:]
    words = [int(raw[i:i + 64], 16) for i in range(0, len(raw) - 63, 64)]
    if len(words) == 1 and words[0] in (0, 1) and re.match(
            r'(?i)(is|has|can|paused|allow|white|eligib|graduat|migrat|launched|listed|ended|finished|active)', f['sig']):
        return L('yes', '是') if words[0] else L('no', '否')
    if len(words) == 1 and 2 ** 100 < words[0] < 2 ** 160:
        return '0x' + f'{words[0]:040x}'
    return ' / '.join(show_value(str(n)) for n in words[:4]) or L('(empty)', '（空）')


def cmd_rules(args):
    if not args:
        raise UsageError(L('Usage: /rules CONTRACT [A|B|AB]\nReads contract settings related to limits, price, timing and slots', '用法：/rules 合约地址 [A|B|AB]\n读出合约里跟上限、价格、时间、名额有关的设置'))
    addr = need_addr(args[0])
    ws = parse_wallets(args[1:])
    source, fns = resolve_target(addr)
    picks = [f for f in fns if f['mut'] in ('view', 'pure') and RULE_RE.search(f['sig'])
             and (f['sig'].endswith('()') or f['sig'].endswith('(address)'))]
    lines = [addr, source, '']
    for f in picks[:30]:
        try:
            if f['sig'].endswith('(address)'):
                lines.append(f"{f['sig']}\n" + '\n'.join(L(f"  {w}: {read_view(addr, f, WALLETS[w]['addr'])}", f"  {w}：{read_view(addr, f, WALLETS[w]['addr'])}") for w in ws))
            else:
                lines.append(f"{f['sig']} = {read_view(addr, f)}")
        except RuntimeError as e:
            lines.append(L(f"{f['sig']}: unreadable ({str(e)[:80]})", f"{f['sig']}：读不到（{str(e)[:80]}）"))
    if not picks:
        lines.append(L('No rule-like settings found; try /max directly.', '没找到看起来跟规则有关的设置，可以直接用 /max 试。'))
    lines += ['', L('Names are guessed from the contract; judge the units yourself. A /max simulation is the most accurate.', '名称是从合约猜的，数字单位要自己判断；最准的还是 /max 实际模拟。')]
    say('\n'.join(lines))


def cmd_max(args):
    if len(args) < 3:
        raise UsageError(L('Usage: /max A|B|AB CONTRACT func(types) args… value=…\nMark the amount to test with ?:\n'
                         '  Per unit: /max AB 0x… mint(uint256) ? value=?x0.0032 (0.0032 each)\n'
                         '  By amount: /max AB 0x… buy(address) 0x… value=? (tests how much BNB you can pay)', '用法：/max A|B|AB 合约地址 函数(类型) 参数… value=…\n用 ? 标出要试的数量：\n'
                         '  张数型：/max AB 0x… mint(uint256) ? value=?x0.0032（每张 0.0032）\n'
                         '  金额型：/max AB 0x… buy(address) 0x… value=?（试能付多少 BNB）'))
    ws = parse_wallets(args[:1])
    to = need_addr(args[1])
    sig = args[2].strip('"\'')
    rest = list(args[3:])
    vexpr = rest.pop()[6:] if rest and rest[-1].lower().startswith('value=') else '0'
    marks = rest.count('?')
    if marks > 1:
        raise UsageError(L('Only one ? is allowed in the arguments.', '参数里只能有一个 ?。'))
    if vexpr == '?':
        if marks:
            raise UsageError(L('? can go in only one place: the arguments or value, not both.', '? 只能放一个地方：参数或 value 择一。'))
        unit = to_wei('0.0001')
        value_of, unit_name = (lambda q: q * unit), '× 0.0001 BNB'
    elif vexpr[:2] in ('?x', '?*'):
        price = to_wei(vexpr[2:])
        value_of, unit_name = (lambda q: q * price), L('units', '份')
    elif marks:
        const = to_wei(vexpr) if dec(vexpr) > 0 else 0
        value_of, unit_name = (lambda q: const), ''
    else:
        raise UsageError(L('Mark the amount to test with ? (in the arguments, or as value=?).', '要用 ? 标出要试的数量（放在参数，或写 value=?）。'))
    cap = to_wei(MAX_BNB)
    step = value_of(2) - value_of(1)
    qlimit = cap // step if step else 10 ** 9
    if value_of(1) > cap:
        raise UsageError(L(f'Paying for 1 unit already exceeds your limit of {fmt(MAX_BNB)} BNB.', f'付 1 份就超过你设的上限 {fmt(MAX_BNB)} BNB。'))

    report, calls = [L(f'{sig} limit test (simulated on-chain per wallet, nothing sent)', f'{sig} 上限试算（用各钱包地址在链上模拟，没有送出）'), ''], []
    for w in ws:
        def build(q, w=w):
            params = [str(q) if p == '?' else tx_param(p, w) for p in rest]
            return cast('calldata', sig, *params), params

        def fails(q, w=w):
            return eth_call_error(w, to, value_of(q), build(q)[0])

        good, low_note, why = 1, '', fails(1)
        if why:  # maybe a minimum quantity: probe upwards for the first amount that passes
            first_why, last_bad, probe, good = why, 1, 2, None
            while probe <= qlimit:
                if not fails(probe):
                    good = probe
                    break
                last_bad, probe = probe, probe * 2
            if good is None:
                report.append(L(f'Wallet {w}: fails even at {last_bad} {unit_name}: {first_why}'
                              '\n  If the platform needs a fixed amount (e.g. exactly 0.05 per seat), enter that amount instead of ?', f'钱包 {w}：试到 {last_bad} {unit_name}都不行：{first_why}'
                              '\n  如果平台要固定金额（例如每席刚好 0.05），直接填那个金额，不要用 ?'))
                continue
            while good - last_bad > 1:
                mid = (good + last_bad) // 2
                if fails(mid):
                    last_bad = mid
                else:
                    good = mid
            low_note = L(f'\n  Minimum {good} {unit_name} (less is rejected: {first_why})', f'\n  最少要 {good} {unit_name}（更少会被拒：{first_why}）')
        bad, capped = None, False
        while bad is None:
            nxt = min(good * 2, qlimit)
            if nxt <= good:
                capped = True
                break
            why = fails(nxt)
            if why:
                bad = nxt
            else:
                good = nxt
        while bad is not None and bad - good > 1:
            mid = (good + bad) // 2
            e = fails(mid)
            if e:
                bad, why = mid, e
            else:
                good = mid
        line = L(f'Wallet {w}: max {good} {unit_name}, pays {fmt(from_wei(value_of(good)))} BNB{low_note}', f'钱包 {w}：最多 {good} {unit_name}，付 {fmt(from_wei(value_of(good)))} BNB{low_note}')
        line += L(f'\n  Still passes at your limit of {fmt(MAX_BNB)} BNB; the platform limit may be higher', f'\n  到你设的上限 {fmt(MAX_BNB)} BNB 都还能过，平台上限可能更高') if capped else L(f'\n  More is rejected: {why}', f'\n  再多会被拒：{why}')
        report.append(line)
        data, params = build(good)
        calls.append((w, to, value_of(good), data, f"{sig} {' '.join(params)}".strip()))
    report.append(L('\nAssumes larger amounts are more likely to be rejected; if the platform has a minimum or tiers, trust the preview.', '\n假设数量越多越容易被拒；平台若有最低购买量或分段规则，结果以预览为准。'))
    say('\n'.join(report))
    if calls:
        preview_calls(L('Preview at found limit', '用试出的上限预览'), calls)


# ---------------------------------------------------------------- read-only

OVERVIEW_MAX = 8  # tokens listed (and given a button) on the overview


def is_bnb(t):
    a = (t.get('address') or '').lower()
    return not ADDR_RE.match(a) or a == NATIVE.lower()


def overview():
    """(HTML text, buttons): both wallets' value, BNB, every token held, open limit orders and armed TP/SL rules."""
    jobs = {}
    for w in WALLETS:
        jobs[f'bal{w}'] = lambda w=w: baw(w, 'wallet', 'balance', '--binanceChainId', CHAIN)
        jobs[f'lim{w}'] = lambda w=w: baw(w, 'limit-order', 'list', '--status', 'WORKING', '--binanceChainId', CHAIN)
    d = dict(zip(jobs, each(list(jobs), lambda k: safe(jobs[k]))))
    totals, bnb, tokens, failed = {}, {}, {}, []
    for w in WALLETS:
        r = d.get(f'bal{w}') or {}
        if not r.get('success'):
            failed.append(L(f'⚠️ Wallet {w} query failed: {esc(baw_err(r))}', f'⚠️ 钱包 {w} 查询失败：{esc(baw_err(r))}'))
            continue
        totals[w] = Decimal(0)
        for t in r.get('data') or []:
            amount, value = dec(t.get('balance')), dec(t.get('value'))
            totals[w] += value
            if is_bnb(t):
                bnb[w] = (amount, value)
            elif amount > 0:
                row = tokens.setdefault(t['address'].lower(), {'symbol': t.get('symbol') or '?', 'value': Decimal(0)})
                row[w] = (amount, value)
                row['value'] += value
    ranked = sorted(tokens.items(), key=lambda kv: -kv[1]['value'])
    shown = [kv for kv in ranked if kv[1]['value'] >= Decimal('0.01')][:OVERVIEW_MAX]
    infos = each([k for k, _ in shown], token_info)
    now = time.time()
    lines = [L(f'👤 <b>Overview</b>  {local_time(now)}', f'👤 <b>个人总览</b>　{local_time(now)}'),
             L(f"💰 Total ≈ ${fmt(sum(totals.values()), 2)} (", f"💰 总资产 ≈ ${fmt(sum(totals.values()), 2)}（") + L(' | ', '｜').join(f'{w} ${fmt(v, 2)}' for w, v in totals.items()) + L(')', '）'),
             L('💳 BNB: ', '💳 BNB：') + L(' | ', '｜').join(L(f'{w} {fmt(bnb[w][0], 4)} (${fmt(bnb[w][1], 2)})', f'{w} {fmt(bnb[w][0], 4)}（${fmt(bnb[w][1], 2)}）') if w in bnb else f'{w} 0'
                                    for w in totals)] + failed
    lines += ['', L(f'💎 {len(tokens)} positions', f'💎 持仓 {len(tokens)} 个') + (L(' (tap a button below to open a trade card)', '（按下方按钮开交易卡片）') if shown else '')]
    for i, ((addr, row), info) in enumerate(zip(shown, infos), 1):
        day = (info or {}).get('percentChange24h')
        lines.append(L(f"{i}. <b>{esc(row['symbol'])}</b>  ${fmt(row['value'], 2)}", f"{i}. <b>{esc(row['symbol'])}</b>　${fmt(row['value'], 2)}")
                     + (L(f"  {'🟢' if dec(day) >= 0 else '🔴'} {change(day)} (24h)", f"　{'🟢' if dec(day) >= 0 else '🔴'} {change(day)}（24h）") if known(day) else ''))
        lines.append(L('  ', '　') + L(' | ', '｜').join(L(f'{w} {fmt(row[w][0], 4)} (${fmt(row[w][1], 2)})', f'{w} {fmt(row[w][0], 4)}（${fmt(row[w][1], 2)}）') for w in WALLETS if w in row))
    rest = len(tokens) - len(shown)
    if rest:
        lines.append(L(f'{rest} more not listed (under $0.01 or ranked below #{OVERVIEW_MAX})', f'另有 {rest} 个没列出（价值不到 $0.01 或排在第 {OVERVIEW_MAX} 名之后）'))
    orders = sum(len(((d.get(f'lim{w}') or {}).get('data') or {}).get('list') or []) for w in WALLETS)
    rules = sum(1 for r in load_rules() if r.get('status') == 'armed')
    lines += ['', L(f'📌 {orders} limit orders | 🎯 {rules} TP/SL pending', f'📌 挂单 {orders} 笔｜🎯 止盈止损 {rules} 条等待中')]
    buttons = rows_of([{'text': f"{row['symbol']} ${fmt(row['value'], 2)}", 'callback_data': f'o:t:{addr}'}
                       for addr, row in shown], 2)
    return '\n'.join(lines), buttons + [[cmd_btn(L(f'📌 Limit orders {orders}', f'📌 挂单 {orders}'), '/limits'), cmd_btn(L(f'🎯 TP/SL {rules}', f'🎯 止盈止损 {rules}'), '/tpsl'),
                                         {'text': L('🔄 Refresh', '🔄 刷新'), 'callback_data': 'o:r'}]]


def on_overview(data, msg):
    _, op, arg = (data.split(':', 2) + ['', ''])[:3]
    if op == 't' and ADDR_RE.match(arg):
        show_panel(arg)
    elif op == 'r':
        edit_or_send((msg['chat']['id'], msg['message_id']) if msg else None, *overview(), rich=True)


def cmd_me(args):
    text, buttons = overview()
    say(text, buttons, rich=True)


def wallet_state(w):
    st = baw(w, 'wallet', 'status')
    s = baw(w, 'wallet', 'settings')
    d = (s.get('data') or {}) if s.get('success') else {}
    try:
        handle = cast('call', AGENT_ID_REGISTRY, 'handleOf(address)(string)', WALLETS[w]['addr'], '--rpc-url', RPC)
        handle = handle.strip('"') or None
    except RuntimeError:
        handle = L('lookup failed', '查不到')
    ADDR_OK.pop(w, None)
    return {'status': st, 'settings': d, 'handle': handle, 'bad': wallet_mismatch(w)}


def cmd_status(args):
    detail = bool(args) and args[0].lower() in ('all', 'detail', 'details') + zh_word('详细')
    ws = list(WALLETS)
    states = each(ws, wallet_state)
    problems, lines, relogin = 0, [], []
    for w, s in zip(ws, states):
        dev = s['settings'].get('devMode') or {}
        issues = []
        state = str((s['status'].get('data') or {}).get('status') or '')
        if not s['status'].get('success'):
            issues.append(L(f"Login status unavailable ({baw_err(s['status'])})", f"查不到登录状态（{baw_err(s['status'])}）"))
        elif state.upper() != 'CONNECTED':
            issues.append(L(f'Not logged in ({state}); tap "🔑 Re-login {w}" below', f'没登录（{state}），按下面「🔑 重新登录 {w}」'))
            relogin.append(w)
        if s['bad']:
            issues.append(s['bad'])
        if not dev.get('enabled'):
            issues.append(L('Developer mode off (needed for contract actions)', '开发者模式没开（合约动作要用）'))
        if not s['handle']:
            issues.append(L('No Agent ID', '没有 Agent ID'))
        problems += bool(issues)
        lines.append(L(f"{'✅' if not issues else '⚠️'} <b>{w}</b>  {esc(s['handle'] or 'no Agent ID')}"
                     f"  {esc(short(WALLETS[w]['addr']))}", f"{'✅' if not issues else '⚠️'} <b>{w}</b>　{esc(s['handle'] or '没有 Agent ID')}"
                     f"　{esc(short(WALLETS[w]['addr']))}"))
        lines += [L(f'  ⚠️ {esc(x)}', f'　⚠️ {esc(x)}') for x in issues]
        if detail:
            lines += [L(f"  Address <code>{WALLETS[w]['addr']}</code>", f"　地址 <code>{WALLETS[w]['addr']}</code>"),
                      L(f"  Dev mode until {esc(nice_time(dev.get('expiresAt')))} | idle logout at "
                      f"{esc(nice_time(s['settings'].get('inactiveSignOutTime')))} | quota left today {esc(s['settings'].get('quotaLeft'))}", f"　开发者模式到 {esc(nice_time(dev.get('expiresAt')))}｜闲置到 "
                      f"{esc(nice_time(s['settings'].get('inactiveSignOutTime')))} 会注销｜今日额度剩 {esc(s['settings'].get('quotaLeft'))}")]
    head = (L(f'✅ <b>All {len(ws)} wallets OK</b>', f'✅ <b>{len(ws)} 个钱包都正常</b>') if not problems else L('⚠️ <b>Some wallets need attention</b>', '⚠️ <b>有钱包要处理</b>')) if ws \
        else L('👛 <b>No wallets yet</b>\nTap "➕ Add wallet" below and confirm in the Binance app to connect your AI wallet', '👛 <b>还没有钱包</b>\n按下面「➕ 添加钱包」，用币安 App 确认就能连上你的 AI 钱包')
    if PAYOUT:
        lines += ['', L('📤 Payout destinations (edit wallets.json on the computer only)', '📤 转出目的地（只能在电脑上的 wallets.json 改）')] + \
                 [L(f"  {esc(p['name'])}  {esc(short(p['addr']))}", f"　{esc(p['name'])}　{esc(short(p['addr']))}") for p in PAYOUT]
    buttons = [[cmd_btn(L('🔄 Recheck', '🔄 重新检查'), '/status')] + ([] if detail or not ws else [cmd_btn(L('📋 Details', '📋 详细'), L('/status details', '/status 详细'))]),
               [cmd_btn(L('➕ Add wallet', '➕ 添加钱包'), '/wallet add')]]
    if relogin:
        buttons.insert(0, [cmd_btn(L(f'🔑 Re-login {w}', f'🔑 重新登录 {w}'), f'/wallet login {w}') for w in relogin])
    if detail and ws:
        buttons.append([cmd_btn(L(f'➖ Remove {w}', f'➖ 移除 {w}'), f'/wallet del {w}') for w in ws])
    say('\n'.join([head, ''] + lines), buttons, rich=True)


LOGINS = {}  # wallet letter -> login in progress (time started)


def cmd_wallet(args):
    op = (args[0].lower() if args else '')
    if op in ('del', 'rm') + zh_word('移除') and len(args) == 2 and args[1].upper() in WALLETS:
        w = args[1].upper()
        ask_confirm(L(f"Remove wallet <b>{w}</b> ({esc(short(WALLETS[w]['addr']))}) from the bot?\n"
                    'This only stops the bot using it; your Binance account is unaffected and you can add it back later.', f"从机器人移除钱包 <b>{w}</b>（{esc(short(WALLETS[w]['addr']))}）？\n"
                    '只是不再用它，币安账号本身不受影响，之后可以再加回来。'), L('Remove', '移除'), lambda: remove_wallet(w), rich=True)
        return
    if op in ('login',) + zh_word('登录') and len(args) == 2 and args[1].upper() in WALLETS:
        w = args[1].upper()
        start_login(w, WALLETS[w]['dir'] or str(HOME_DIR / '.baw'), relogin=True)
        return
    if op not in ('add',) + zh_word('添加'):
        raise UsageError(L('Usage: /wallet add (add a wallet), /wallet login X (re-login), /wallet del X (remove)', '用法：/wallet add（添加钱包）、/wallet login 字母（重新登录）、/wallet del 字母（移除）'))
    free = [c for c in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ' if c not in WALLETS and c not in LOGINS]
    if not free:
        raise UsageError(L('Wallet limit reached.', '钱包数量到上限了。'))
    w = free[0]
    start_login(w, str(HOME_DIR / ('.baw' if not WALLETS else f'.baw-{w.lower()}')))  # each wallet: its own baw login


def start_login(w, d, relogin=False):
    if w in LOGINS and time.time() - LOGINS[w] < 330:
        raise UsageError(L(f'Wallet {w} is waiting for your confirmation in the app (valid for 5 min).', f'钱包 {w} 正在等你在 App 确认，5 分钟内有效。'))
    _, out, err = run([BAW, 'auth', 'signin', '--json'], {'BINANCE_BAW_DIR': d}, 60)
    r = parse_json(out, err)
    data = r.get('data') or {}
    if not r.get('success'):
        raise UsageError(L(f'Failed to start login: {baw_err(r)}', f'开始登录失败：{baw_err(r)}'))
    if data.get('status') == 'ALREADY_CONNECTED':
        finish_login(w, d, relogin)
        return
    LOGINS[w] = time.time()
    say(L(f"{'🔑 <b>Re-login wallet' if relogin else '👛 <b>Add wallet'} {w}</b>\n\n"
        f"1. Open this link: {link(data.get('urlForWeb', ''), 'Binance AI Wallet login')}\n"
        f"2. Confirm in the Binance app; the pairing code must be <b>{esc(data.get('pairingCode'))}</b>\n"
        f"3. Once confirmed I will {'reconnect it' if relogin else 'add it'} automatically (valid for 5 min)", f"{'🔑 <b>重新登录钱包' if relogin else '👛 <b>添加钱包'} {w}</b>\n\n"
        f"1. 打开这个链接：{link(data.get('urlForWeb', ''), '币安 AI 钱包登录')}\n"
        f"2. 用币安 App 确认，配对码要是 <b>{esc(data.get('pairingCode'))}</b>\n"
        f"3. 确认完我会自动{'接回来' if relogin else '加进来'}（5 分钟内有效）"), rich=True)
    run_bg(lambda: wait_login(w, d, data.get('qrCodeId', ''), relogin))


def wait_login(w, d, qr, relogin=False):
    try:
        _, out, err = run([BAW, 'auth', 'verify', '--qrCodeId', qr, '--json'], {'BINANCE_BAW_DIR': d}, 330)
        r = parse_json(out, err)
        if r.get('success'):
            finish_login(w, d, relogin)
        else:
            again = L(f'🔑 Re-login {w}', f'🔑 重新登录 {w}') if relogin else L('➕ Add wallet', '➕ 添加钱包')
            say(L(f'❌ Wallet {w} not connected: {baw_err(r)}\nTap "{again}" again to retry.', f'❌ 钱包 {w} 没连上：{baw_err(r)}\n再按一次「{again}」重来。'))
    except Exception as e:  # noqa: BLE001 - report, never crash the loop
        say(L(f'❌ Wallet {w} not connected: {e}', f'❌ 钱包 {w} 没连上：{e}'))
    finally:
        LOGINS.pop(w, None)


def finish_login(w, d, relogin=False):
    _, out, err = run([BAW, 'wallet', 'address', '--json'], {'BINANCE_BAW_DIR': d}, 60)
    r = parse_json(out, err)
    rows = (r.get('data') or {}).get('addresses') or []
    addr = next((x.get('address') for x in rows if str(x.get('binanceChainId')) == CHAIN), None) \
        or next((x.get('address') for x in rows if str(x.get('address', '')).startswith('0x')), None)
    if not addr or not ADDR_RE.match(addr):
        say(L(f'❌ Logged in but could not read the BSC address: {baw_err(r)}', f'❌ 登录了但读不到 BSC 地址：{baw_err(r)}'))
        return
    if relogin:
        ADDR_OK.pop(w, None)
        LOGIN_FAILS.pop(w, None)
        if addr.lower() != WALLETS[w]['addr'].lower():
            say(L(f"⚠️ Wallet {w} logged in as {short(addr)}, not the original {short(WALLETS[w]['addr'])}.\n"
                'For safety this wallet will not trade for now; please re-login with the original Binance account.', f"⚠️ 钱包 {w} 登录的是 {short(addr)}，不是原本的 {short(WALLETS[w]['addr'])}。\n"
                '为了安全，这个钱包先不会下单；请用原本那个币安账号重新登录。'))
            return
        say(L(f'✅ Wallet <b>{w}</b> re-logged in', f'✅ 钱包 <b>{w}</b> 重新登录好了'), [[cmd_btn(L('📊 Wallet status', '📊 看钱包状态'), '/status')]], rich=True)
        return
    dup = next((x for x, v in WALLETS.items() if v['addr'].lower() == addr.lower()), None)
    if dup:
        say(L(f'This account is already wallet {dup} ({short(addr)}); no need to add it again. To add another wallet, log in with a different Binance account.', f'这个账号已经是钱包 {dup}（{short(addr)}），不用重复加。要加别的钱包，请换另一个币安账号登录。'))
        return
    save_wallet(w, addr, d)
    say(L(f'✅ Wallet <b>{w}</b> added  <code>{addr}</code>\n'
        'To buy on the bonding curve or use contract actions, enable "Developer mode" for this wallet in the Binance app.', f'✅ 钱包 <b>{w}</b> 已加入　<code>{addr}</code>\n'
        '要买内盘、做合约动作，记得在币安 App 打开这个钱包的「开发者模式」。'), [[cmd_btn(L('📊 Wallet status', '📊 看钱包状态'), '/status')]],
        rich=True)


def remove_wallet(w):
    try:
        data = json.loads(WALLETS_FILE.read_text())
    except (OSError, ValueError):
        data = {}
    data['wallets'] = [r for r in data.get('wallets') or [] if str(r.get('name', '')).upper() != w]
    WALLETS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    WALLETS.clear()
    WALLETS.update(load_wallets()[0])
    say(L(f'🗑 Wallet {w} removed.', f'🗑 钱包 {w} 已移除。'))


ORDER_STATE = {'FINISHED': '✅', 'FAILED': '❌', 'CANCELED': '🚫', 'CANCELLED': '🚫'}


def cmd_orders(args):
    lines = [L('📜 <b>Recent trades</b> (5 per wallet)', '📜 <b>最近成交</b>（每个钱包 5 笔）')]
    for w, r in zip(WALLETS, each(list(WALLETS), lambda w: baw(w, 'market-order', 'list', '--pageSize', '5'))):
        rows = ((r.get('data') or {}).get('list') or []) if r.get('success') else []
        lines += ['', f'<b>{w}</b>' + ('' if rows else L('  ', '　') + esc(baw_err(r) if not r.get('success') else L('none yet', '还没有')))]
        for o in rows:
            lines.append(L(f"{ORDER_STATE.get(o.get('status'), '⏳')} {esc(nice_time(o.get('bookTime')))}  "
                         f"{esc(fmt(o.get('fromTokenQty'), 6))} {esc(o.get('fromTokenName'))} → {esc(o.get('toTokenName'))}", f"{ORDER_STATE.get(o.get('status'), '⏳')} {esc(nice_time(o.get('bookTime')))}　"
                         f"{esc(fmt(o.get('fromTokenQty'), 6))} {esc(o.get('fromTokenName'))} → {esc(o.get('toTokenName'))}")
                         + (L(f"  {tx_link(o.get('txHash'), 'tx')}", f"　{tx_link(o.get('txHash'), '交易')}") if o.get('txHash') else ''))
    say('\n'.join(lines), [[cmd_btn(L('🔄 Refresh', '🔄 刷新'), '/orders'), cmd_btn(L('👤 Overview', '👤 总览'), '/me')]], rich=True)


HOME_TEXT = L("""👋 <b>AI Wallet Bot</b> ({wallets})

<b>Just paste it here, no commands needed:</b>
🪙 Token address → trade card (buy, sell, limit, TP/SL)
    Four, Gang, Flap, Agent Curve tokens trade directly on the bonding curve
📜 Project contract address → action buttons (mint, claim, refund…)
📋 A project's full agent instructions → presale card
🔁 Someone's successful tx hash → copy it

Every trade is previewed first; nothing is sent until you tap ✅ Confirm
Max {max_bnb} BNB per wallet per trade""", """👋 <b>AI 钱包机器人</b>（{wallets}）

<b>直接贴给我就好，不用打指令：</b>
🪙 代币地址 → 交易卡片（买、卖、挂单、止盈止损）
　　Four、Gang、Flap、Agent Curve 内盘币直接跟内盘买卖
📜 项目合约地址 → 动作按钮（铸造、领取、退款…）
📋 项目给 agent 的整段说明 → 预售卡片
🔁 别人成功的交易 hash → 照抄一笔

每笔都先预览，你按 ✅ 确认才送出
单一钱包每次最多 {max_bnb} BNB""")

HELP_ALL = L(f"""📖 <b>All commands</b> (buttons cover everyday use; these are backups)

<b>Info</b>
/me  Overview: assets, positions, orders
/status  Wallet status (add "details" for quota)
/orders  Recent trades
/limits  All limit orders | /tpsl  All TP/SL

<b>Trade</b> (fastest: paste a token address and tap the buttons)
/buy TOKEN 0.01  each wallet buys with 0.01 BNB
/buy TOKEN 5%  each wallet uses 5% of its BNB
/sell TOKEN 50  each wallet sells half
Add A or B at the end = that wallet only

<b>Project contracts</b> (fastest: paste the contract address and tap the buttons)
/tx WALLETS CONTRACT func(types) args…
  Type "me" for your own address; to pay BNB, add "pay 0.01" at the end
  e.g. /tx AB 0x… claim(address) me
/max  Like /tx, but put ? as the amount to find the most you can buy
/copy TX_HASH  copy someone's tx
/rules CONTRACT  read limit, price and timing settings

<b>Other</b>
/set  Settings (all via buttons)
/key NAME CMD  save as a button under the input box; /key NAME = delete
  or tap "⭐ Save shortcut" on a preview

Max {fmt(MAX_BNB)} BNB per wallet per trade (editable only in the config file on the computer)""", f"""📖 <b>全部指令</b>（平常用按钮就好，这些是备用）

<b>查询</b>
/me　总览：资产、持仓、挂单
/status　钱包状态（加「详细」看额度）
/orders　最近成交
/limits　所有挂单｜/tpsl　所有止盈止损

<b>买卖</b>（贴代币地址按按钮最快）
/buy 代币地址 0.01　每个钱包买 0.01 BNB
/buy 代币地址 5%　每个钱包用自己 BNB 的 5%
/sell 代币地址 50　每个钱包卖掉一半
最后加 A 或 B＝只用那个钱包

<b>项目合约</b>（贴合约地址按按钮最快）
/tx 钱包 合约地址 动作(类型) 参数…
　地址打「我」＝用自己的地址；要付 BNB 最后加「付 0.01」
　例：/tx AB 0x… claim(address) 我
/max　跟 /tx 一样，但在数量打 ?，会试出最多能买多少
/copy 交易hash　照抄别人的一笔
/rules 合约地址　读出上限、价格、时间这类设置

<b>其他</b>
/set　设置（全部用按钮改）
/key 名字 指令　存成输入框下方的按钮；/key 名字＝删掉
　也可以在预览上按「⭐ 存成快捷键」

单一钱包每次最多 {fmt(MAX_BNB)} BNB（只能改电脑上的设置档）""")


def home_buttons():
    return [[cmd_btn(L('👤 Overview', '👤 总览'), '/me'), cmd_btn(L('📊 Wallet status', '📊 钱包状态'), '/status'), cmd_btn(L('📜 Recent trades', '📜 最近成交'), '/orders')],
            [cmd_btn(L('📌 Limit orders', '📌 挂单'), '/limits'), cmd_btn(L('🎯 TP/SL', '🎯 止盈止损'), '/tpsl'), cmd_btn(L('⚙️ Settings', '⚙️ 设置'), '/set')],
            [cmd_btn(L('⚡ Sniper', '⚡ 新盘狙击'), '/snipe'), cmd_btn(L('📖 All commands', '📖 全部指令'), L('/help all', '/help 全部'))]]


def show_home(prefix=''):
    names = L(', ', '、').join(WALLETS)
    text = HOME_TEXT.format(wallets=L(f'{names}: {len(WALLETS)} wallets', f'{names} {len(WALLETS)} 个钱包') if WALLETS else L('no wallets yet; add one under Wallet status', '还没有钱包，先到钱包状态添加'),
                            max_bnb=fmt(MAX_BNB))
    say((prefix + '\n\n' if prefix else '') + text, home_buttons(), rich=True)
    say(L('⌨️ Quick buttons are below the input box', '⌨️ 常用按钮在输入框下方'), keyboard=key_board())


def cmd_help(args):
    if args and args[0].lower() in ('all',) + zh_word('全部'):
        say(HELP_ALL, [[cmd_btn(L('⬅️ Main menu', '⬅️ 回主菜单'), '/help')]], rich=True)
    else:
        show_home()


COMMANDS = {
    'start': cmd_help, 'help': cmd_help,
    'me': cmd_me, 'bal': cmd_me, 'status': cmd_status, 'orders': cmd_orders, 'wallet': cmd_wallet, 'snipe': cmd_snipe,
    'buy': cmd_buy, 'sell': cmd_sell, 'set': cmd_set, 'key': cmd_key, 'keys': cmd_key,
    'tpsl': cmd_tpsl, 'limits': cmd_limits, 'cancel': cmd_cancel,
    'fn': cmd_fn, 'tx': cmd_tx, 'copy': cmd_copy, 'rules': cmd_rules, 'max': cmd_max, 'open': cmd_open,
}
MENU = [('start', L('Main menu', '主菜单')), ('me', L('Overview', '总览')), ('status', L('Wallet status', '钱包状态')), ('orders', L('Recent trades', '最近成交')), ('limits', L('Limit orders', '挂单')),
        ('tpsl', L('TP/SL', '止盈止损')), ('snipe', L('Sniper', '新盘狙击')), ('set', L('Settings', '设置')), ('help', L('Help', '说明'))]


def guarded(fn, args):
    try:
        fn(args)
    except UsageError as e:
        say(str(e))
    except RuntimeError as e:  # cast rejected the input (bad parameter, unknown tx, revert while reading)
        say(L(f'⚠️ Failed to read on-chain data; nothing was sent.\n({str(e)[:300]})', f'⚠️ 读链上数据失败，什么都没送出。\n（{str(e)[:300]}）'))


def dispatch(text):
    cmd, *args = text.split()
    fn = COMMANDS.get(cmd.lstrip('/').split('@')[0].lower())
    if not fn:
        show_home(L('🤔 Unknown command.', '🤔 没有这个指令。'))
        return
    logging.info('command %s', text)
    guarded(fn, args)


def handle(update):
    if 'callback_query' in update:
        on_callback(update['callback_query'])
        return
    m = update.get('message') or {}
    chat = m.get('chat') or {}
    uid = (m.get('from') or {}).get('id')
    if chat.get('type') != 'private':
        return
    if OWNER is None:
        tg('sendMessage', chat_id=chat['id'],
           text=L(f'Your Telegram ID is {uid}. Put it in TG_OWNER_ID in the config file and restart the bot.', f'你的 Telegram ID 是 {uid}。把它填进设置档的 TG_OWNER_ID，重新启动机器人后就能用。'))
        return
    if uid != OWNER:
        logging.warning('ignored message from %s', uid)
        return
    if time.time() - m.get('date', 0) > 120:  # never act on stale commands queued while the bot was down
        return
    text = (m.get('text') or '').strip()
    if text.startswith('/'):
        dispatch(text)
    elif text:
        on_text(text)


def main():
    CONF_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=None if DRY else LOG_FILE, level=logging.INFO,
                        format='%(asctime)s %(levelname)s %(message)s')
    if DRY:
        args = [a for a in sys.argv[1:] if a != '--dry']
        dispatch(' '.join(args) or '/help')
        return 0
    if not TOKEN:
        print(f'TG_BOT_TOKEN is missing in {ENV_FILE}')
        return 1
    tg('setMyCommands', commands=[{'command': c, 'description': d} for c, d in MENU])
    threading.Thread(target=watch_loop, daemon=True).start()
    threading.Thread(target=launch_loop, daemon=True).start()
    backlog = tg('getUpdates', offset=-1, timeout=0).get('result') or []
    offset = backlog[-1]['update_id'] + 1 if backlog else None
    logging.info('bot started, owner %s', OWNER)
    while True:
        try:
            updates = tg('getUpdates', offset=offset, timeout=50,
                         allowed_updates=['message', 'callback_query']).get('result') or []
        except Exception as e:  # noqa: BLE001
            logging.warning('getUpdates failed: %s', e)
            time.sleep(5)
            continue
        for u in updates:
            offset = u['update_id'] + 1
            try:
                handle(u)
            except Exception as e:  # noqa: BLE001
                logging.exception('handler failed')
                if OWNER:
                    try:
                        say(L(f'Error: {type(e).__name__}: {str(e)[:300]}', f'出错了：{type(e).__name__}: {str(e)[:300]}'))
                    except Exception:  # noqa: BLE001
                        pass


if __name__ == '__main__':
    sys.exit(main())
