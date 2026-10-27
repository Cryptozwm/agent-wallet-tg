#!/usr/bin/env python3
"""End-to-end scenario tests for bot.py on a local anvil chain. Nothing touches BSC or the real wallets.

The baw CLI is replaced by a fake: contract-call preview/execute become eth_call / cast send on anvil with
impersonated accounts, and market orders follow a scripted state machine. Telegram is replaced by a recorder.
Run from WSL:  python3 tests/run_scenarios.py
"""

import json
import os
import re
import subprocess
import sys
import time
import uuid
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PORT = 8611
RPC = f'http://127.0.0.1:{PORT}'
os.environ['AWT_TEST_RPC'] = RPC
sys.path.insert(0, str(ROOT.parent))
import bot  # noqa: E402

FOUNDRY = Path.home() / '.foundry' / 'bin'
CAST, ANVIL, FORGE = str(FOUNDRY / 'cast'), str(FOUNDRY / 'anvil'), str(FOUNDRY / 'forge')
OWNER = 111
STRANGER = 999
DEPLOYER = '0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266'  # anvil's first dev account (public test account)
OTHER = '0x1111111111111111111111111111111111111111'  # someone else whose transaction gets copied
# anvil's public dev accounts stand in for the wallets; the real wallets.json is never read or written
TEST_WALLETS = {'wallets': [{'name': 'A', 'addr': '0x70997970C51812dc3A010C7d01b50e0d17dc79C8', 'baw_dir': ''},
                            {'name': 'B', 'addr': '0x3C44CdDdB6a900fa2b585dd299e03d12FA4293BC', 'baw_dir': '~/.baw-b'}],
                'payout': []}
C_ADDR = '0x90F79bf6EB2c4f870365E785982E1f101E93b906'
bot.WALLETS_FILE = Path('/tmp') / f'agent-wallet-tg-test-wallets-{os.getpid()}.json'
bot.WALLETS_FILE.write_text(json.dumps(TEST_WALLETS))
bot.WALLETS.clear()
bot.WALLETS.update(bot.load_wallets()[0])
A, B = bot.WALLETS['A']['addr'], bot.WALLETS['B']['addr']
ONE_BNB = 10 ** 18


def sh(*args, timeout=180, cwd=None):
    p = subprocess.run(args, capture_output=True, text=True, timeout=timeout, cwd=cwd)
    if p.returncode:
        raise RuntimeError((p.stderr or p.stdout).strip()[:400])
    return p.stdout.strip()


def c(*args):
    return sh(CAST, *args, '--rpc-url', RPC)


def num(to, sig, *args):
    return int(c('call', to, sig, *args).split()[0])


def unwrap(text):
    j = json.loads(text[text.find('{'):])
    return j.get('data', j) if 'schema_version' in j else j


def deploy(name):
    out = sh(FORGE, 'create', f'contracts/Mocks.sol:{name}', '--rpc-url', RPC, '--unlocked', '--from', DEPLOYER,
             '--broadcast', '--json', cwd=str(ROOT))
    return unwrap(out)['deployedTo']


# ---------------------------------------------------------------- fakes

SENT = []
DEVMODE = {'A': True, 'B': True}
PREVIEWS = {}
MARKET = {'quote_ok': True, 'statuses': ['PENDING', 'FINISHED'], 'holdings': {}}
ORDERS, SWAPS = {}, []
EXEC_ERR = []  # next contract-call execute fails with this message (e.g. a timeout)
APP_CONFIRM = []  # next contract-call execute waits for a second confirmation in the Binance App
LOCK = {}  # wallet -> list of tx-lock answers to give, then UNLOCKED
STATUS = {}  # wallet -> wallet status override (e.g. UNCONNECTED)
ADDRBOOK = set()  # lower-case addresses the fake Binance address book allows sending to
MAIN = '0x15d34AAf54267DB7D7c367839AAf71A00a2C6A65'  # anvil dev account standing in for the payout wallet


class Clock:
    """bot.time replacement: sleeping advances a virtual offset so polling loops finish instantly."""
    offset = 0.0

    def time(self):
        return time.time() + self.offset

    def sleep(self, seconds):
        self.offset += seconds

    def __getattr__(self, name):
        return getattr(time, name)


CLOCK = Clock()


EDITS = []


def fake_tg(method, **params):
    if method == 'sendMessage':
        SENT.append(params)
        return {'ok': True, 'result': {'message_id': len(SENT)}}
    if method in ('editMessageReplyMarkup', 'editMessageText'):
        EDITS.append(params)
    return {'ok': True, 'result': []}


LOGIN = {}  # wallet -> address baw pretends to be logged into (default: the configured one)
LIMITS = []  # limit orders placed through the fake
INFO = {}  # token -> market data override
DEFAULT_INFO = {'price': '0.001', 'liquidity': '50000', 'holders': '1234', 'marketCap': '1000000',
                'top10HoldersPercentage': '12.5', 'percentChange24h': '12.5', 'percentChange1h': '-3',
                'launchTime': str(int((time.time() - 3 * 86400 - 60) * 1000)), 'migrateStatus': 1,
                'bundlerHoldingPercent': '6.65', 'devHoldingPercent': '0'}
PROFILE = {'creator': '0x3333333333333333333333333333333333333333', 'created': None, 'platform': 'Flap',
           'links': [{'label': 'x', 'link': 'https://x.com/i/status/1'}, {'label': 'x', 'link': 'javascript:alert(1)'}],
           'buy_tax': '0', 'sell_tax': '3'}


def fake_token_info(token):
    return dict(INFO.get(token.lower(), DEFAULT_INFO))


def fake_baw(w, *args, timeout=120):
    a = list(args)

    def opt(name):
        return a[a.index(name) + 1] if name in a else None

    head = a[:2]
    if head == ['wallet', 'address']:
        addr = LOGIN.get(w, bot.WALLETS[w]['addr'])
        return {'success': True, 'data': {'addresses': [{'binanceChainId': 'CT_501', 'address': 'So1ana'},
                                                        {'binanceChainId': '56', 'address': addr}]}}
    if head in (['limit-order', 'buy'], ['limit-order', 'sell']):
        sid = str(len(LIMITS) + 1000)
        LIMITS.append({'w': w, 'strategyId': sid, 'side': a[1].upper(), 'status': 'WORKING',
                       'fromToken': opt('--fromToken'), 'toToken': opt('--toToken'), 'fromTokenQty': opt('--fromTokenQty'),
                       'fromTokenName': 'BNB' if a[1] == 'buy' else 'MOCK', 'triggerPrice': opt('--triggerPrice')})
        return {'success': True, 'data': {'strategyId': sid}}
    if head == ['limit-order', 'list']:
        rows = [o for o in LIMITS if o['w'] == w and o['status'] == (opt('--status') or o['status'])
                and (not opt('--fromToken') or o['fromToken'].lower() == opt('--fromToken').lower())
                and (not opt('--toToken') or o['toToken'].lower() == opt('--toToken').lower())]
        return {'success': True, 'data': {'list': rows}}
    if head == ['limit-order', 'cancel']:
        o = next((o for o in LIMITS if o['strategyId'] == opt('--strategyId') and o['w'] == w), None)
        if not o:
            return {'success': False, 'error': {'message': 'not found'}}
        o['status'] = 'CANCELED'
        return {'success': True, 'data': {'status': 'CANCELED'}}
    if head == ['wallet', 'settings']:
        return {'success': True, 'data': {'devMode': {'enabled': DEVMODE[w], 'expiresAt': 'x'},
                                          'inactiveSignOutTime': 'x', 'quotaLeft': 1}}
    if head == ['wallet', 'status']:
        return {'success': True, 'data': {'status': STATUS.get(w, 'CONNECTED')}}
    if head == ['wallet', 'tx-lock']:
        return {'success': True, 'data': {'status': LOCK[w].pop(0) if LOCK.get(w) else 'UNLOCKED'}}
    if head == ['wallet', 'balance']:
        return {'success': True, 'data': MARKET['holdings'].get(w, [])}
    if head == ['contract-call', 'preview']:
        value = int(opt('--value'))
        rid = uuid.uuid4().hex
        PREVIEWS[rid] = (w, opt('--to'), value, opt('--inputData'))
        changes = [{'tokenType': 'Native', 'change': str(-value)}] if value else []
        return {'success': True, 'data': {'requestId': rid, 'risks': {},
                                          'simulationResult': {'simulationCode': '000000000', 'balanceChanges': changes,
                                                               'authorityChanges': []}}}
    if head == ['contract-call', 'execute']:
        if EXEC_ERR:
            return {'success': False, 'error': {'message': EXEC_ERR.pop()}}
        if APP_CONFIRM:
            APP_CONFIRM.pop()
            PREVIEWS.pop(opt('--requestId'), None)
            return {'success': True, 'data': {'status': 'PENDING_CONFIRMATION', 'message': 'Please confirm in the App'}}
        item = PREVIEWS.pop(opt('--requestId'), None)
        if not item:
            return {'success': False, 'error': {'name': 'PREVIEW_EXPIRED', 'message': 'preview expired'}}
        w2, to, value, data = item
        try:
            tx = c('send', to, data, '--value', str(value), '--from', bot.WALLETS[w2]['addr'], '--unlocked', '--async')
        except RuntimeError as e:
            return {'success': False, 'error': {'name': 'SEND_FAILED', 'message': str(e)[:200]}}
        return {'success': True, 'data': {'status': 'BROADCASTED', 'txHash': tx.split()[-1]}}
    if head == ['wallet', 'send']:
        to, token = opt('--recipient'), opt('--tokenAddress')
        if to.lower() not in ADDRBOOK:
            return {'success': False, 'error': {'name': 'INVALID_RECIPIENT',
                                                'message': 'Recipient address is not in the address book'}}
        addr = bot.WALLETS[w]['addr']
        amount = num(token, 'balanceOf(address)(uint256)', addr)
        tx = c('send', token, 'transfer(address,uint256)', to, str(amount), '--from', addr, '--unlocked', '--async')
        return {'success': True, 'data': {'txHash': tx.split()[-1]}}
    if head == ['market-order', 'quote']:
        if not MARKET['quote_ok']:
            return {'success': False, 'error': {'name': 'NO_ROUTE', 'message': 'no route'}}
        return {'success': True, 'data': {'fromCoinSymbol': 'X', 'toCoinSymbol': 'MOCK', 'toCoinAmount': '123.45',
                                          'slippage': 0.01}}
    if head == ['market-order', 'swap']:
        oid = uuid.uuid4().hex[:10]
        ORDERS[oid] = list(MARKET['statuses'])
        SWAPS.append((w, opt('--fromToken'), opt('--toToken'), opt('--fromTokenQty')))
        return {'success': True, 'data': {'orderId': oid}}
    if head == ['market-order', 'list']:
        seq = ORDERS.get(opt('--orderId'), ['PENDING'])
        st = seq.pop(0) if len(seq) > 1 else seq[0]
        return {'success': True, 'data': {'list': [{'status': st, 'txHash': '0xabc' if st == 'FINISHED' else None,
                                                    'fromTokenQty': '1', 'fromTokenName': 'BNB', 'toTokenName': 'MOCK'}]}}
    return {'success': False, 'error': {'message': 'fake baw: unsupported ' + ' '.join(a)}}


bot.tg = fake_tg
bot.baw = fake_baw
bot.time = CLOCK
bot.OWNER = OWNER
bot.DRY = False
bot.PREFS_FILE = Path('/tmp') / f'agent-wallet-tg-test-prefs-{os.getpid()}.json'  # never touch the real settings
bot.RULES_FILE = Path('/tmp') / f'agent-wallet-tg-test-rules-{os.getpid()}.json'
bot.ORDERS_LOG = Path('/tmp') / f'agent-wallet-tg-test-orders-{os.getpid()}.jsonl'
bot.SENDS_FILE = Path('/tmp') / f'agent-wallet-tg-test-sends-{os.getpid()}.json'
bot.SNIPE_FILE = Path('/tmp') / f'agent-wallet-tg-test-snipe-{os.getpid()}.json'
bot.SNIPE_LOG = Path('/tmp') / f'agent-wallet-tg-test-snipe-log-{os.getpid()}.jsonl'
bot.token_info = fake_token_info
bot.token_profile = lambda token: dict(PROFILE)
REAL_AUDIT = bot.audit  # scenarios swap bot.audit for canned answers; this one parses a canned API response
bot.run_bg = lambda fn: fn()  # confirmed actions run inline so each check sees their result

# ---------------------------------------------------------------- driving the bot

RESULTS = []


def check(name, ok, detail=''):
    RESULTS.append((name, bool(ok), detail))
    print(('PASS ' if ok else 'FAIL ') + name + ('' if ok else f'\n     {detail[:600]}'))


def send(text, uid=OWNER, chat_type='private', age=0):
    start = len(SENT)
    bot.handle({'update_id': 1, 'message': {'from': {'id': uid}, 'chat': {'id': uid, 'type': chat_type},
                                            'date': int(CLOCK.time() - age), 'text': text}})
    return SENT[start:]


def inline(m):
    return (m.get('reply_markup') or {}).get('inline_keyboard')


def press(msgs, kind='ok', uid=OWNER):
    bot.SENT_AT.clear()  # scenarios resend identical orders on purpose; guard_scenarios tests the 5-second block
    btn = next(m for m in reversed(msgs) if inline(m))
    data = inline(btn)[0][0 if kind == 'ok' else 1]['callback_data']
    return press_data(data, uid)


def press_data(data, uid=OWNER):
    start = len(SENT)
    bot.handle({'callback_query': {'id': 'q', 'from': {'id': uid}, 'data': data,
                                   'message': {'chat': {'id': uid}, 'message_id': 1}}})
    return SENT[start:]


def text(msgs):
    return '\n'.join(m.get('text', '') for m in msgs)


def buttons(msgs):
    """Inline buttons only (confirm or panel); the pinned shortcut keyboard does not count."""
    return any(inline(m) for m in msgs)


def ok_data(msgs):
    return inline(next(m for m in reversed(msgs) if inline(m)))[0][0]['callback_data']


def panel_data(msgs):
    """{button text: callback data} of the last inline keyboard."""
    rows = inline(next(m for m in reversed(msgs) if inline(m)))
    return {b['text']: b['callback_data'] for row in rows for b in row}


def view():
    """(text, {button: data}) of the card as it was last edited in place."""
    e = EDITS[-1]
    rows = (e.get('reply_markup') or {}).get('inline_keyboard') or []
    return e.get('text', ''), {b['text']: b['callback_data'] for row in rows for b in row}


def key_like(pd, start):
    return pd[next(k for k in pd if k.startswith(start))]


def keyboard_names(msgs):
    rows = next(((m.get('reply_markup') or {}).get('keyboard') for m in reversed(msgs)
                 if (m.get('reply_markup') or {}).get('keyboard')), [])
    return [b['text'] for row in rows for b in row]


def warp(seconds):
    c('rpc', 'evm_increaseTime', str(seconds))
    c('rpc', 'evm_mine')


# ---------------------------------------------------------------- scenarios

def scenarios(k):
    cap, amt, slot, minq, claim = k['CapMint'], k['AmountBuy'], k['SlotLaunch'], k['MinQtyTimed'], k['ClaimFor']

    m = send('/status')
    check('Agent ID 显示（A binancecoin、B zzz66）', 'binancecoin' in text(m) and 'zzz66' in text(m), text(m))

    m = send(f'/rules {cap}')
    check('/rules 读出每钱包上限 5、总量 8、价格', '= 5' in text(m) and '= 8' in text(m) and '0.0032' in text(m), text(m))
    check('/rules 读出 isAgent（A、B 都是 AI 钱包）', 'isAgent' in text(m) and 'A：是' in text(m) and 'B：是' in text(m), text(m))

    # per-unit mint with a wallet cap and a global supply
    m = send(f'/max A {cap} mint(uint256) ? value=?x0.0032')
    check('张数型 /max：A 最多 5（每钱包上限）', '最多 5 份' in text(m) and buttons(m), text(m))
    press(m)
    check('确认后链上 A 铸造 5', num(cap, 'mintedOf(address)(uint256)', A) == 5)
    m = send(f'/max B {cap} mint(uint256) ? value=?x0.0032')
    check('张数型 /max：B 只剩 3（总量 8 被 A 用掉 5）', '最多 3 份' in text(m), text(m))
    press(m)
    check('确认后链上 B 铸造 3、总量 8', num(cap, 'mintedOf(address)(uint256)', B) == 3
          and num(cap, 'totalMinted()(uint256)') == 8)
    m = send(f'/tx A {cap} mint(uint256) 1 value=0.0032')
    check('已买满再买：模拟被挡、没有确认钮', not buttons(m) and '模拟失败' in text(m), text(m))
    check('挡下原因有名称（不是只有代码）', 'ExceedsWalletCap' in text(m) or 'SoldOut' in text(m), text(m))

    # amount-based buy with a minimum and a cumulative cap
    m = send(f'/max AB {amt} buy(address) {OTHER} value=?')
    check('金额型 /max：最少 0.01（100 份）、最多 0.03（300 份）',
          text(m).count('最多 300') == 2 and text(m).count('最少要 100') == 2, text(m))
    press(m)
    check('确认后链上 A、B 各付 0.03', num(amt, 'paidOf(address)(uint256)', A) == 3 * ONE_BNB // 100
          and num(amt, 'paidOf(address)(uint256)', B) == 3 * ONE_BNB // 100)

    # fixed-price slot, refund after the deadline
    m = send(f'/max A {slot} fillSlot() value=?')
    check('固定金额平台：/max 说明要改用固定金额', '固定金额' in text(m) and not buttons(m[:-1]), text(m))
    m = send(f'/tx AB {slot} fillSlot() value=0.05')
    press(m)
    check('/tx 占席：A、B 都占到、共 2 席', num(slot, 'filled()(uint256)') == 2
          and c('call', slot, 'hasSlot(address)(bool)', A) == 'true')
    m = send(f'/tx A {slot} refundTo(address) me')
    check('截止前退款：被挡（NotExpired）', not buttons(m) and 'NotExpired' in text(m), text(m))

    # time window: not open yet
    m = send(f'/max A {minq} purchase(uint256) ? value=?x0.001')
    check('还没开卖：/max 回报 NotStarted、没有确认钮', 'NotStarted' in text(m) and not buttons(m), text(m))

    warp(2 * 3600)
    before = int(c('balance', A))
    m = send(f'/tx AB {slot} refundTo(address) me')
    press(m)
    check('截止后退款：A 拿回约 0.05', int(c('balance', A)) - before > 49 * ONE_BNB // 1000, str(int(c('balance', A)) - before))
    check('退款后链上 A、B 的席位清掉', c('call', slot, 'hasSlot(address)(bool)', B) == 'false')

    m = send(f'/max A {minq} purchase(uint256) ? value=?x0.001')
    check('最低购买量：/max 找到最少 3、最多 6', '最少要 3' in text(m) and '最多 6 份' in text(m), text(m))
    press(m)
    check('确认后链上 A 买 6', num(minq, 'boughtOf(address)(uint256)', A) == 6)

    # wallet without an Agent ID
    c('send', '0x60B9262456dE011F7d15a54CdD8c48bAaa8B022F', 'setHandle(address,string)', B, '', '--from', DEPLOYER, '--unlocked')
    m = send(f'/tx B {minq} purchase(uint256) 3 value=0.003')
    check('没有 Agent ID：被挡（NotAgent）、没有确认钮', not buttons(m) and 'NotAgent' in text(m), text(m))
    m = send('/status')
    check('/status 显示 B 没有 Agent ID', '没有 Agent ID' in text(m) and '有钱包要处理' in text(m), text(m))
    c('send', '0x60B9262456dE011F7d15a54CdD8c48bAaa8B022F', 'setHandle(address,string)', B, 'zzz66', '--from', DEPLOYER, '--unlocked')

    # copy someone else's transaction that carries their own address
    c('rpc', 'anvil_impersonateAccount', OTHER)
    c('rpc', 'anvil_setBalance', OTHER, hex(ONE_BNB))
    tx = c('send', claim, 'claim(address,uint256)', OTHER, '2', '--from', OTHER, '--unlocked', '--async').split()[-1]
    m = send(f'/copy {tx} A')
    check('/copy：看出原交易、地址换成自己', '换成你的' in text(m) and buttons(m), text(m))
    press(m)
    check('照抄后链上记在 A 名下 2、原发送者不变', num(claim, 'claimedOf(address)(uint256)', A) == 2
          and num(claim, 'claimedOf(address)(uint256)', OTHER) == 2)

    # guard rails
    m = send(f'/tx A {amt} buy(address) {OTHER} value=0.5')
    check('超过自设上限 0.1 BNB：直接拒绝', '超过单一钱包上限' in text(m) and not buttons(m), text(m))
    DEVMODE['A'] = False
    m = send(f'/tx A {claim} claim(address,uint256) me 1')
    check('开发者模式没开：略过、没有确认钮', '开发者模式没开' in text(m) and not buttons(m), text(m))
    DEVMODE['A'] = True

    # Telegram-side safety
    claimed = lambda: num(claim, 'claimedOf(address)(uint256)', A)  # noqa: E731
    check('陌生人传指令：完全不回应', send('/bal', uid=STRANGER) == [])
    check('群组里的指令：不回应', send('/bal', chat_type='group') == [])
    check('积压 5 分钟的旧指令：不运行', send(f'/tx A {claim} claim(address,uint256) me 1', age=300) == [])
    m = send(f'/tx A {claim} claim(address,uint256) me 1')
    data = ok_data(m)
    base = claimed()
    press_data(data, uid=STRANGER)
    check('陌生人按确认：不运行', claimed() == base)
    r = press(m, kind='no')
    check('按取消：不运行', claimed() == base and '已取消' in text(r), text(r))
    r = press_data(data)
    check('取消后再按确认：已失效、不运行', claimed() == base and '已经用过' in text(r), text(r))
    m = send(f'/tx A {claim} claim(address,uint256) me 1')
    CLOCK.offset += 120
    r = press(m)
    check('超过 90 秒才按：逾时、不运行', claimed() == base and '超过时间' in text(r), text(r))
    m = send(f'/tx A {claim} claim(address,uint256) me 1')
    data = ok_data(m)
    press_data(data)
    r = press_data(data)
    check('确认按两次：只运行一次', claimed() == base + 1 and '已经用过' in text(r), text(r))
    m = send(f'/tx A {claim} claim(address,uint256) me 1')
    PREVIEWS.clear()
    r = press(m)
    check('预览过期才确认：回报失败、不运行', claimed() == base + 1 and '没送出' in text(r), text(r))

    # token swaps through the (fake) wallet API
    token = '0x2222222222222222222222222222222222222222'
    bot.audit = lambda t: (1, '安全检查：LOW（等级 1）')
    SWAPS.clear()
    m = send(f'/buy {token} 0.01')
    press(m)
    check('买币：A、B 各下一单并回报成交', len(SWAPS) == 2 and 'A 买入成交' in text(SENT[-4:])
          and 'B 买入成交' in text(SENT[-4:]), text(SENT[-6:]))
    bot.audit = lambda t: (4, '安全检查：HIGH（等级 4）')
    m = send(f'/buy {token} 0.01')
    check('高风险代币（等级 4）：拒买、没有确认钮', '不能买' in text(m) and not buttons(m), text(m))
    bot.audit = lambda t: (None, '⚠️ 这个代币没有安全检查数据。')
    m = send(f'/buy {token} 0.01 A')
    check('查不到安全检查：提醒后仍可确认', '没有安全检查结果' in text(m) and buttons(m), text(m))
    MARKET['statuses'] = ['PENDING', 'FAILED']
    r = press(m)
    check('成交失败：回报失败，不说成功', 'A 买入失败' in text(r) and '成功' not in text(r), text(r))
    MARKET['statuses'] = ['PENDING']
    m = send(f'/buy {token} 0.01 A')
    r = press(m)
    check('一直没成交：回报处理中，不说成功', '还在处理' in text(r) and '成功' not in text(r), text(r))
    MARKET['statuses'] = ['PENDING', 'FINISHED']
    c('rpc', 'anvil_setBalance', B, hex(5 * ONE_BNB // 1000))
    m = send(f'/buy {token} 0.01')
    check('B 的 BNB 不够：只买 A、B 略过', '不买：BNB 不够' in text(m) and '确认买入（A）' in json.dumps(m, ensure_ascii=False),
          text(m))
    c('rpc', 'anvil_setBalance', B, hex(ONE_BNB))
    MARKET['holdings'] = {'A': [{'symbol': 'MOCK', 'address': token, 'balance': '123.456789', 'value': '10'}]}
    SWAPS.clear()
    m = send(f'/sell {token} 50 A')
    press(m)
    check('卖一半：数量正确（123.456789 的一半往下取）', SWAPS and SWAPS[-1][3] == '61.728394', str(SWAPS))
    m = send(f'/sell {token} 100 A')
    press(m)
    check('全卖：数量刚好等于持有量', SWAPS[-1][3] == '123.456789', str(SWAPS))
    m = send(f'/sell {token} 50 B')
    check('没有持仓：不卖、没有确认钮', '没有这个代币' in text(m) and not buttons(m), text(m))

    # quick amounts, the token panel and custom shortcut keys
    bot.audit = lambda t: (1, '安全检查：LOW（等级 1）')
    m = send('/set')
    check('/set：默认快捷金额 0.01／0.03／0.05、默认钱包 AB', '0.01／0.03／0.05' in text(m) and '默认钱包　AB' in text(m), text(m))
    m = send('/set buy 0.02 0.04 0.08')
    check('/set buy：改成 0.02／0.04／0.08 并存盘', '0.02／0.04／0.08' in text(m)
          and json.loads(bot.PREFS_FILE.read_text())['buy'] == ['0.02', '0.04', '0.08'], text(m))
    m = send('/set buy 0.02 0.5')
    check('/set buy 超过上限：拒绝、设置不变', '超过单一钱包上限' in text(m)
          and bot.load_prefs()['buy'] == ['0.02', '0.04', '0.08'], text(m))
    m = send('/set sell 30 60 100')
    check('/set sell：改成 30／60／100', bot.load_prefs()['sell'] == ['30', '60', '100'], text(m))
    m = send('/set sell 150')
    check('/set sell 超过 100：拒绝', '不能超过 100' in text(m) and bot.load_prefs()['sell'] == ['30', '60', '100'], text(m))
    m = send('/set wallets C')
    check('/set wallets 乱打：拒绝', '钱包要写' in text(m) and bot.load_prefs()['wallets'] in ('', 'AB'), text(m))

    # the trading card
    m = send(token)
    pd = panel_data(m)
    check('贴合约地址：出交易卡片（池子、持有人、持仓、余额、设置）',
          all(s in text(m) for s in ('池子 $50K', '👥 1,234 人持有｜前 10 大 12.5%', '💎 持仓　A 123.4567（$10）',
                                     '💳 BNB', '👛 用 AB 钱包')), text(m))
    check('卡片按钮齐全（刷新、钱包、买 3 档、买 %、买 x、卖 30／60／100%、卖 x%、挂单、止盈止损、设置）',
          all(k in pd for k in ('🔄 刷新', '买 0.02', '买 0.04', '买 0.08', '买 1%', '买 5%', '买 x ✏️', '🔴 卖 30%',
                                '🔴 卖 60%', '🔴 卖 100%', '🔴 卖 x% ✏️', '📌 挂单', '🎯 止盈止损', '⚙️ 设置'))
          and any(k.startswith('👛 钱包：AB') for k in pd), str(list(pd)))
    check('卡片按钮数据不超过 Telegram 的 64 字符', all(len(v.encode()) <= 64 for v in pd.values()))
    check('卡片解析：涨跌、市值、平台、已毕业、发币者链接、开盘 3 天前、税、捆绑、X 和 K 线链接',
          m[-1].get('parse_mode') == 'HTML'
          and all(s in text(m) for s in ('24时 +12.5%', '1时 -3%', '💰 市值 $1M', 'Flap｜已毕业',
                                         '>发币者</a> 还有 0%', '3 天前开盘', '⚖️ 税 买 0%｜卖 3%',
                                         '捆绑 6.6%', '>X</a>', 'K 线</a>'))
          and 'javascript:' not in text(m), text(m))
    bot.audit = lambda t: (1, '低风险 <script>｜非貔貅 ✅')
    m = send(token)
    check('卡片把代币数据里的 < > 跳脱，不会变成 HTML', '&lt;script&gt;' in text(m) and '<script>' not in text(m), text(m))
    bot.audit = lambda t: (1, '安全检查：LOW（等级 1）')
    check('小数价格用括号写零的个数：0.00001113 → 0.0(4)1113、0.00003943 → 0.0(4)3943、0.0016 不变',
          bot.tiny_price('0.00001113') == '0.0(4)1113' and bot.tiny_price('0.00003943') == '0.0(4)3943'
          and bot.tiny_price('0.0016') == '0.0016', bot.tiny_price('0.00001113'))

    import io
    real_urlopen = bot.urllib.request.urlopen
    bot.urllib.request.urlopen = lambda req, timeout=0: io.BytesIO(json.dumps({'data': {
        'hasResult': True, 'isSupported': True, 'riskLevel': 2,
        'extraInfo': {'buyTax': '0.03', 'sellTax': '0.12', 'isVerified': False},
        'riskItems': [{'details': [{'title': 'Honeypot Risk Found', 'isHit': True, 'riskType': 'RISK'}]}]}}).encode())
    try:
        lvl, at = REAL_AUDIT(token)
    finally:
        bot.urllib.request.urlopen = real_urlopen
    check('安全检查税率是小数：0.03／0.12 显示成 3%／12%、超过 10% 警告、命中貔貅',
          lvl == 2 and '买 3%｜卖 12%' in at and '卖税 12%，超过 10%' in at and '⛔ 貔貅' in at and '没开源' in at, at)

    # personal overview
    saved = MARKET['holdings']
    MARKET['holdings'] = {'A': [{'symbol': 'BNB', 'address': '', 'balance': '0.5', 'value': '300'},
                                {'symbol': 'MOCK', 'address': token, 'balance': '123.456789', 'value': '10'}],
                          'B': [{'symbol': 'BNB', 'address': '', 'balance': '0.25', 'value': '150'},
                                {'symbol': 'MOCK', 'address': token, 'balance': '20', 'value': '2'},
                                {'symbol': 'DUST', 'address': '0x' + '1' * 40, 'balance': '1', 'value': '0.001'}]}
    m = send('/me')
    od = panel_data(m)
    check('/me 个人总览：总资产、各钱包、BNB、持仓合计与 24h、小额不列', all(s in text(m) for s in (
        '总资产 ≈ $462', 'A $310｜B $152', 'BNB：A 0.5（$300）｜B 0.25（$150）', '持仓 2 个', '<b>MOCK</b>　$12',
        '🟢 +12.5%', 'A 123.4567（$10）｜B 20（$2）', '另有 1 个没列出')) and 'DUST' not in text(m), text(m))
    m = press_data(od['MOCK $12'])
    check('总览按持仓：直接开那颗币的交易卡片', token in text(m) and '💎 持仓' in text(m) and buttons(m), text(m))
    MARKET['holdings']['A'][0]['value'] = '400'
    press_data(od['🔄 刷新'])
    check('总览按刷新：原地更新', '个人总览' in view()[0] and '总资产 ≈ $562' in view()[0]
          and EDITS[-1].get('parse_mode') == 'HTML', view()[0])
    MARKET['holdings'] = saved
    m = send('/start')
    check('底部快捷键有「总览」', '总览' in keyboard_names(m), str(keyboard_names(m)))
    SWAPS.clear()
    m = press_data(pd['买 0.04'])
    check('按「买 0.04」：先出预览、还没下单', '每个钱包用 0.04 BNB' in text(m) and buttons(m) and not SWAPS, text(m))
    press(m)
    check('预览后按确认：A、B 同时各用 0.04 下单', sorted(s[0] for s in SWAPS) == ['A', 'B']
          and all(s[3] == '0.04' for s in SWAPS), str(SWAPS))

    from decimal import Decimal, ROUND_DOWN
    want = {w: bot.fmt(((Decimal(c('balance', addr)) / ONE_BNB - Decimal('0.002')) * 5 / 100)
                       .quantize(Decimal('0.000001'), rounding=ROUND_DOWN)) for w, addr in (('A', A), ('B', B))}
    SWAPS.clear()
    m = press_data(pd['买 5%'])
    check('按「买 5%」：预览写明用各自 BNB 的 5%', '自己 BNB 的 5%' in text(m) and buttons(m), text(m))
    press(m)
    check('买 5%：A、B 各自用自己余额的 5% 下单', {s[0]: s[3] for s in SWAPS} == want, f'{SWAPS} vs {want}')
    bot.MAX_BNB, cap = bot.dec('0.01'), bot.MAX_BNB
    m = press_data(pd['买 5%'])
    check('% 换算超过单一钱包上限：改用上限', '超过单一钱包上限，改用 0.01' in text(m), text(m))
    bot.MAX_BNB = cap

    press_data(key_like(pd, '👛 钱包'))
    vt, vb = view()
    check('点钱包切换：卡片原地改成只有 A', '👛 用 A 钱包' in vt and any(k.startswith('👛 钱包：A 🔁') for k in vb), vt)
    SWAPS.clear()
    press(press_data(pd['买 0.02']))
    check('切换后再买：只有 A 下单', [s[0] for s in SWAPS] == ['A'], str(SWAPS))
    press_data(key_like(pd, '👛 钱包'))
    press_data(key_like(pd, '👛 钱包'))
    check('再点两次：回到 AB', '👛 用 AB 钱包' in view()[0])
    n = len(EDITS)
    press_data(pd['🔄 刷新'])
    check('刷新：卡片原地更新', len(EDITS) == n + 1 and ' 更新' in view()[0])

    m = press_data(pd['买 x ✏️'])
    check('按「买 x」：请你输入金额或 %', '请输入每个钱包要买多少' in text(m), text(m))
    SWAPS.clear()
    m = send('0.025')
    check('输入 0.025：出预览、还没下单', '每个钱包用 0.025 BNB' in text(m) and buttons(m) and not SWAPS, text(m))
    press(m)
    check('确认后 A、B 各用 0.025 下单', len(SWAPS) == 2 and all(s[3] == '0.025' for s in SWAPS), str(SWAPS))
    m = send('0.03')
    check('输入只用一次：再打数字不会下单', not buttons(m) and len(SWAPS) == 2, text(m))
    press_data(pd['买 x ✏️'])
    m = send('3%')
    check('买 x 输入 3%：用余额比例预览', '自己 BNB 的 3%' in text(m) and buttons(m), text(m))
    press_data(pd['买 x ✏️'])
    m = send('0.5')
    check('自订金额超过上限：拒绝、没有确认钮', '超过单一钱包上限' in text(m) and not buttons(m), text(m))
    press_data(pd['买 x ✏️'])
    CLOCK.offset += 200
    m = send('0.02')
    check('输入超过 2 分钟：逾时、不下单', '逾时' in text(m) and not buttons(m), text(m))

    SWAPS.clear()
    m = press_data(pd['🔴 卖 60%'])
    check('卡片「卖 60%」：A 有持仓出预览、B 没有就略过', '不卖：没有这个代币' in text(m) and buttons(m), text(m))
    press(m)
    check('卖 60% 数量正确', SWAPS == [('A', token, bot.NATIVE, '74.074073')], str(SWAPS))
    press_data(pd['🔴 卖 x% ✏️'])
    SWAPS.clear()
    press(send('10'))
    check('卖 x% 输入 10：卖 12.345678', SWAPS == [('A', token, bot.NATIVE, '12.345678')], str(SWAPS))

    # settings, all through buttons
    press_data(pd['⚙️ 设置'])
    vt, vb = view()
    check('按设置：卡片原地换成设置页', '设置</b>' in vt and '💰 快捷买 ✏️' in vb and '⬅️ 返回' in vb, vt)
    press_data(vb['💰 快捷买 ✏️'])
    send('0.01 0.02')
    check('设置页改买入金额：存盘并更新设置页', bot.load_prefs()['buy'] == ['0.01', '0.02'] and '0.01／0.02 BNB' in view()[0],
          view()[0])
    press_data(key_like(vb, '🎚 滑点'))
    m = send('99')
    check('滑点打 99：拒绝', '0～50' in text(m) and bot.load_prefs()['slippage'] == 'auto', text(m))
    press_data(key_like(vb, '🎚 滑点'))
    send('7')
    check('滑点改成 7%', bot.load_prefs()['slippage'] == '7')
    SWAPS.clear()
    press(press_data(pd['买 0.02']))
    check('下单带新的滑点 7', bot.slippage() == '7' and SWAPS)
    press_data(vb['💧 池子门槛 ✏️'])
    send('100k')
    check('池子门槛改成 100k', bot.load_prefs()['min_liq'] == '100000')
    press_data(pd['🔄 刷新'])
    vt, vb2 = view()
    check('池子 $50K 低于门槛：卡片标示、没有买入钮', '低于门槛' in vt and not any(k.startswith('买') for k in vb2), vt)
    m = send(f'/buy {token} 0.01')
    check('池子太小：/buy 直接拒绝', '低于门槛' in text(m) and not buttons(m), text(m))
    m = send(f'/sell {token} 50 A')
    check('池子太小：卖出照样可以、但提醒滑点', '卖出滑点可能很大' in text(m) and buttons(m), text(m))
    m = press_data(vb2['🔓 这颗仍要买（30 分钟）'])
    vt, vb3 = view()
    check('池子太小：按「这颗仍要买」后只放行这颗、出现买入钮、门槛设置不变',
          '30 分钟' in text(m) and any(k.startswith('买') for k in vb3) and '低于门槛' not in vt
          and bot.load_prefs()['min_liq'] == '100000', text(m) + vt)
    m = send(f'/buy {token} 0.01 A')
    check('放行后 /buy 这颗可以预览', buttons(m) and '低于门槛' not in text(m), text(m))
    bot.LIQ_OK.clear()
    press_data(vb['💧 池子门槛 ✏️'])
    send('20k')
    press_data(key_like(vb, '👛 钱包：'))
    check('默认钱包切换 AB→A', bot.load_prefs()['wallets'] == 'A')
    press_data(key_like(vb, '👛 钱包：'))
    press_data(key_like(vb, '👛 钱包：'))
    check('再切两次回到 AB', bot.load_prefs()['wallets'] == 'AB')
    press_data(vb['止盈条件 ✏️'])
    send('50 50')
    press_data(key_like(vb, '🎯 自动止盈止损'))
    check('打开自动止盈止损、止盈改成涨 50% 卖 50%', bot.load_prefs()['auto_tpsl'] and bot.load_prefs()['tp'] == ['50', '50'])
    press_data(vb['⬅️ 返回'])
    check('设置页按返回：回到卡片', '💎 持仓' in view()[0] and '买完自动设止盈止损' in view()[0], view()[0])

    # automatic take-profit / stop-loss after a buy
    bot.save_rules([])
    SWAPS.clear()
    m = press_data(pd['买 0.02'])
    check('自动止盈止损开着：买入预览写明成交后会自动设置', '成交后自动挂止盈止损' in text(m), text(m))
    r = press(m)
    rules = bot.load_rules()
    check('买入成交后：A、B 各挂上止盈＋止损（共 4 条，基准价 0.001）',
          len(rules) == 4 and {x['kind'] for x in rules} == {'tp', 'sl'} and all(x['entry'] == '0.001' for x in rules)
          and '已设置止盈止损' in text(r), json.dumps(rules)[:300])
    SWAPS.clear()
    bot.check_rules()
    check('价格没动：不触发', not SWAPS)
    INFO[token.lower()] = dict(DEFAULT_INFO, price='0.0016')
    m0 = len(SENT)
    bot.check_rules()
    check('涨 60%（超过 +50%）：A 自动卖 50%、B 没持仓只回报', SWAPS == [('A', token, bot.NATIVE, '61.728394')]
          and '止盈没有卖出' in text(SENT[m0:]), text(SENT[m0:]))
    bot.check_rules()
    check('同一条止盈不会触发第二次', len(SWAPS) == 1)
    INFO[token.lower()] = dict(DEFAULT_INFO, price='0.0006')
    SWAPS.clear()
    bot.check_rules()
    check('跌 40%（超过 -30%）：A 自动全卖', SWAPS == [('A', token, bot.NATIVE, '123.456789')], str(SWAPS))
    check('全部规则都已结束（没有等待中的）', not [x for x in bot.load_rules() if x['status'] == 'armed'])
    p = bot.load_prefs()
    p['auto_tpsl'] = False
    bot.save_prefs(p)

    # manual take-profit / stop-loss from the card
    press_data(pd['🔄 刷新'])
    press_data(pd['🎯 止盈止损'])
    vt, vb = view()
    check('止盈止损页：显示现价、没有规则', '现价 $0.0006' in vt and '还没设置' in vt, vt)
    press_data(vb['➕ 止盈 ✏️'])
    m = send('100')
    check('止盈只打一个数字：拒绝', '要两个数字' in text(m) and not buttons(m), text(m))
    press_data(vb['➕ 止损 ✏️'])
    m = send('150 100')
    check('止损跌幅 150%：拒绝', '跌幅要小于 100%' in text(m), text(m))
    press_data(vb['➕ 止盈 ✏️'])
    m = send('200 25')
    check('手动止盈预览：A 有持仓、B 略过、涨到 $0.0018', 'B：没有持仓 → 略过' in text(m) and '$0.0018' in text(m)
          and buttons(m), text(m))
    check('手动止盈：还没按确认前不生效', not [x for x in bot.load_rules() if x['status'] == 'armed'])
    press(m)
    armed = [x for x in bot.load_rules() if x['status'] == 'armed']
    check('按确认后只替 A 挂上止盈', len(armed) == 1 and armed[0]['wallet'] == 'A' and armed[0]['sell'] == '25', str(armed))
    press_data(pd['🎯 止盈止损'])
    vt, vb = view()
    check('止盈止损页列出这条规则', '止盈｜钱包 A' in vt and '🗑 清除这颗币的规则' in vb, vt)
    m = send('/tpsl')
    check('/tpsl 列出等待中的规则', '止盈｜钱包 A' in text(m), text(m))
    press_data(vb['🗑 清除这颗币的规则'])
    check('清除后没有等待中的规则', not [x for x in bot.load_rules() if x['status'] == 'armed'])

    # limit orders
    press_data(pd['📌 挂单'])
    vt, vb = view()
    check('挂单页：目前没有挂单', '还没有挂单' in vt and '限价买 ✏️' in vb, vt)
    press_data(vb['限价买 ✏️'])
    m = send('0.002 0.01')
    check('限价买价格高于现价：拒绝', '要低于现价' in text(m) and not buttons(m), text(m))
    press_data(vb['限价买 ✏️'])
    m = send('0.0005 0.01')
    check('限价买预览：A、B 各 0.01 BNB、触发价 $0.0005', text(m).count('用 0.01 BNB 买') == 2 and '$0.0005' in text(m)
          and buttons(m), text(m))
    press(m)
    check('确认后 A、B 各挂一张限价买', [(o['w'], o['side'], o['fromTokenQty'], o['triggerPrice']) for o in LIMITS]
          == [('A', 'BUY', '0.01', '0.0005'), ('B', 'BUY', '0.01', '0.0005')] or
          sorted((o['w'], o['side']) for o in LIMITS) == [('A', 'BUY'), ('B', 'BUY')], str(LIMITS))
    press_data(vb['限价卖 ✏️'])
    m = send('0.0001 50')
    check('限价卖价格低于现价：拒绝并提示用止损', '要高于现价' in text(m) and '止损' in text(m), text(m))
    press_data(vb['限价卖 ✏️'])
    press(send('0.001 50'))
    sells = [o for o in LIMITS if o['side'] == 'SELL']
    check('限价卖：只有 A（有持仓）挂 61.728394', [(o['w'], o['fromTokenQty']) for o in sells] == [('A', '61.728394')],
          str(sells))
    press_data(pd['📌 挂单'])
    vt, vb = view()
    check('挂单页列出 3 张、各有撤单钮', vt.count('　到 $') == 3 and all(f'撤单 {i}' in vb for i in (1, 2, 3)), vt)
    press_data(vb['撤单 1'])
    vt, _ = view()
    check('撤单 1 后剩 2 张', vt.count('　到 $') == 2 and sum(o['status'] == 'CANCELED' for o in LIMITS) == 1, vt)
    m = send('/limits')
    check('/limits 列出所有挂单、各有撤单钮', text(m).count('　到 $') == 2 and '撤 1' in panel_data(m), text(m))
    INFO.clear()

    # baw logged into the wrong account
    LOGIN['A'] = OTHER
    bot.ADDR_OK.clear()
    m = send(f'/buy {token} 0.01')
    check('A 登录的账号不对：A 不买、只剩 B', 'baw 登录的是' in text(m) and '确认买入（B）' in json.dumps(m, ensure_ascii=False),
          text(m))
    m = send('/status')
    check('/status 标出登录账号有问题', 'baw 登录的是' in text(m) and '有钱包要处理' in text(m), text(m))
    LOGIN.clear()
    bot.ADDR_OK.clear()
    m = send('/status')
    check('登录正确时 /status 显示都正常', '2 个钱包都正常' in text(m), text(m))

    # pools the token API knows nothing about
    INFO[token.lower()] = {'price': None, 'liquidity': None}
    m = send(f'/buy {token} 0.01 A')
    check('查不到池子大小：提醒后仍可确认', '查不到池子大小' in text(m) and buttons(m), text(m))
    INFO.clear()

    before = len(SENT) + len(EDITS)
    press_data(pd['买 0.02'], uid=STRANGER)
    check('陌生人按卡片：不理', len(SENT) + len(EDITS) == before)
    CLOCK.offset += 7 * 3600
    m = press_data(pd['买 0.02'])
    check('卡片超过 6 小时没动：过期、不下单', '已过期' in text(m) and not buttons(m), text(m))

    bot.audit = lambda t: (4, '安全检查：HIGH（等级 4）')
    m = send(token)
    pd = panel_data(m)
    check('高风险代币的卡片：没有买入钮、只剩卖出', not any(k.startswith('买') for k in pd)
          and '🔴 卖 30%' in pd and '只能卖不能买' in text(m), str(list(pd)))
    press_data(pd['📌 挂单'])
    check('高风险代币的挂单页：只有限价卖', '限价买 ✏️' not in view()[1] and '限价卖 ✏️' in view()[1])
    m = send(f'/buy {token}')
    check('/buy 只给地址：也出卡片', buttons(m) and '只能卖不能买' in text(m), text(m))
    bot.audit = lambda t: (1, '安全检查：LOW（等级 1）')

    m = send(f'/key 领 /tx A {claim} claim(address,uint256) me 1')
    check('/key 添加「领」：底部快捷键多一颗', '领' in keyboard_names(m) and '总览' in keyboard_names(m), text(m))
    base = claimed()
    m = send('领')
    check('按快捷键「领」：一样先预览、还没送出', buttons(m) and claimed() == base, text(m))
    press(m)
    check('确认后链上 A 领到 1', claimed() == base + 1)
    m = send('总览')
    check('内置快捷键「总览」：出总览', '个人总览' in text(m), text(m))
    for bad, why in (('/key 总览 /bal', '跟内置重复'), ('/key x /key y /bal', '指向 /key'), ('/key x hello', '不是指令'),
                     ('/key 0.05 /bal', '名称是数字'), (f'/key {token} /bal', '名称是地址'), ('/key 不存在', '删除不存在的')):
        m = send(bad)
        check(f'/key 拒绝：{why}', not buttons(m) and 'keyboard' not in json.dumps(m) and m, text(m))
    m = send('/key 领')
    check('/key 删除「领」', '领' not in keyboard_names(m) and '领' not in bot.load_prefs()['keys'], text(m))
    m = send('领')
    check('删除后再按「领」：说看不懂、不送出', '看不懂' in text(m) and claimed() == base + 1, text(m))

    bot.PREFS_FILE.write_text('{not json')
    m = send('/set')
    check('设置档坏掉：退回默认，不当机', '0.01／0.03／0.05' in text(m), text(m))
    bot.PREFS_FILE.write_text(json.dumps({'buy': ['0.05', '0.5'], 'wallets': 'AB'}))
    pd = panel_data(send(token))
    check('设置档里超过上限的金额：面板不显示', '买 0.05' in pd and '买 0.5' not in pd, str(list(pd)))

    # bad input never crashes and never offers a button
    for cmd in ('/tx A notanaddress f()', f'/max A {cap} mint(uint256) ? ?', '/buy 0x2222 0.01', f'/buy {token} 0',
                f'/tx A {cap} mint(uint256) abc', '/sell', f'/copy 0x{"ab" * 32}'):
        m = send(cmd)
        check(f'错误输入不当机：{cmd[:40]}', m and not buttons(m), text(m))


PRESALE_TEXT = """Trade the PSM (PSM) presale on PSM, BNB Smart Chain (chain id 56).

Presale pool: {pool}
Token: {token}
Pay with: native BNB
Limit per agent: 30M PSM, bought minus sold back. All amounts are in wei (18 decimals).

BUY
1. quoteBuy(uint256 amountIn) returns (uint256 tokensOut, uint256 tax)
2. buy(uint256 amountIn, uint256 minTokensOut), sending amountIn as the BNB value (no approve).
minTokensOut = tokensOut from the quote, or buyLimit(address yourWallet) if that is smaller.

SELL
1. quoteSell(uint256 tokensIn) returns (uint256 amountOut, uint256 tax)
2. approve(address spender, uint256 amount) on the token: spender = the pool, amount = tokensIn.
3. sell(uint256 tokensIn, uint256 minAmountOut), with minAmountOut = amountOut. You receive BNB.
Check graduated() is false first: once true, trade on PancakeSwap."""


def presale_scenarios(pool, k):
    tok = c('call', pool, 'token()(address)').strip()
    bal = lambda w: num(tok, 'balanceOf(address)(uint256)', w)  # noqa: E731
    for addr in (A, B):
        c('rpc', 'anvil_setBalance', addr, hex(ONE_BNB))
    LOGIN.clear()
    DEVMODE.update(A=True, B=True)
    bot.save_prefs(dict(bot.DEFAULT_PREFS, buy=['0.01', '0.05'], sell=['50', '100'], wallets='AB'))
    bot.AWAIT.clear()

    m = send(PRESALE_TEXT.format(pool=pool, token=tok))
    qd = panel_data(m)
    check('贴预售说明：出预售卡片（池子、代币、买卖函数、额度、毕业、报价）',
          m[-1].get('parse_mode') == 'HTML' and all(s in text(m) for s in (
              'PSM 预售', f'address/{pool.lower()}', f'address/{tok.lower()}', '🟢 可以买',
              '你还能买　A 3,000 万｜B 3,000 万', '毕业了吗　否', '0.01 BNB 约换 <b>69.3 万</b>')), text(m))
    check('预售卡片按钮：刷新、钱包、买 0.01／0.05／x、卖 50／100／x%，数据不超过 64 字符',
          all(k in qd for k in ('🔄 刷新', '买 0.01', '买 0.05', '买 x ✏️', '🔴 卖 50%', '🔴 卖 100%', '🔴 卖 x% ✏️'))
          and all(len(v.encode()) <= 64 for v in qd.values()), str(list(qd)))

    lines = PRESALE_TEXT.format(pool=pool, token=tok).replace('Presale pool:', 'Address one:').replace('Token:', 'Address two:')
    m = send(lines)
    check('说明没写哪个是池子：从链上代码认出池子和代币', 'PSM 预售' in text(m) and f'address/{pool.lower()}' in text(m)
          and f'address/{tok.lower()}' in text(m), text(m))
    m = send('Please look at 0x' + '3' * 40 + ' and call hello(world) for me, thanks a lot friend')
    check('看不懂的说明：说找不到买卖函数、不出按钮', '找不到买、卖或铸造、领取' in text(m) and not buttons(m), text(m))

    m = press_data(qd['买 0.01'])
    check('按「买 0.01」：A、B 各出预览（预估 692,792、最少 98%），还没成交',
          text(m).count('约 69.3 万 PSM（最少 67.9 万）') == 2 and buttons(m) and bal(A) == 0, text(m))
    press(m)
    want = num(pool, 'quoteBuy(uint256)(uint256,uint256)', str(ONE_BNB // 100))
    check('确认后：A、B 各拿到报价的数量', bal(A) == want and bal(B) == want, f'{bal(A)} {want}')

    m = press_data(qd['买 x ✏️'])
    m = send('0.2')
    check('买 x 输入超过单一钱包上限：拒绝', '上限' in text(m) and not buttons(m), text(m))

    press_data(qd['👛 钱包：AB 🔁'])
    check('切换钱包：预售卡片原地更新成只用 A', '👛 用 A 钱包' in view()[0] and '69.3 万' in view()[0], view()[0])
    m = press_data(qd['🔴 卖 50%'])
    check('按「卖 50%」：先出授权预览（第 1 步），还没卖', '第 1／2 步' in text(m) and '允许预售池拿走 34.6 万' in text(m)
          and buttons(m) and bal(A) == want, text(m))
    before = int(c('balance', A))
    m = press(m)
    check('授权上链后：自动出卖出预览（预估拿回 BNB），还没卖', '预售卖出' in text(m) and '→ 约 0.0049' in text(m)
          and buttons(m) and bal(A) == want, text(m))
    press(m)
    check('确认卖出：A 剩一半、拿回约 0.0049 BNB', bal(A) == want - want // 2
          and int(c('balance', A)) - before > 48 * ONE_BNB // 10000, f'{bal(A)} {int(c("balance", A)) - before}')
    check('B 没被动到（卡片只用 A）', bal(B) == want, str(bal(B)))

    c('send', pool, 'graduate()', '--from', DEPLOYER, '--unlocked')
    press_data(qd['🔄 刷新'])
    vt, vb = view()
    check('毕业后刷新：标示已毕业、拿掉买卖、改成开交易卡片', '已毕业' in vt and not any(k.startswith('买') for k in vb)
          and '📈 开交易卡片（DEX）' in vb, vt + str(list(vb)))
    m = press_data(qd['买 0.01'])
    check('毕业后按旧的买入钮：拒绝', '已毕业' in text(m) and not buttons(m), text(m))

    # instructions for something other than buying and selling
    claim, slot = k['ClaimFor'], k['SlotLaunch']
    base = {w: num(claim, 'claimedOf(address)(uint256)', addr) for w, addr in (('A', A), ('B', B))}
    m = send(f'Claim your allocation.\nClaim contract: {claim}\n'
             'Call claim(address beneficiary, uint256 amount) with your own wallet as beneficiary.\n'
             'Check claimedOf(address you) afterwards.')
    cd = panel_data(m)
    check('贴领取说明：出卡片、⚡ claim ✏️ 按钮、读 claimedOf，没有买卖钮',
          '⚡ 领取（claim） ✏️' in cd and 'claimedOf　A' in text(m) and not any(k2.startswith(('买', '🔴')) for k2 in cd),
          text(m) + str(list(cd)))
    m = press_data(cd['⚡ 领取（claim） ✏️'])
    check('按 ⚡ claim：问参数（列出 beneficiary、amount）', '「beneficiary」' in text(m)
          and '「amount」' in text(m) and not buttons(m), text(m))
    m = send('me 7')
    check('输入「me 7」：A、B 各出预览，还没送', '⚡ 领取（claim）' in text(m) and text(m).count('领取（claim） 我 7') == 2
          and buttons(m) and num(claim, 'claimedOf(address)(uint256)', A) == base['A'], text(m))
    press(m)
    check('确认后：A、B 各领 7', num(claim, 'claimedOf(address)(uint256)', A) == base['A'] + 7
          and num(claim, 'claimedOf(address)(uint256)', B) == base['B'] + 7)
    m = send(f'Slot launch refunds.\nSlot contract: {slot}\nIf it did not fill, call refundTo(address to) to get 0.05 back.')
    sd = panel_data(m)
    m = press_data(sd['⚡ 退款（refundTo）'])
    check('只要地址的动作：不问参数、直接用自己的地址出预览', text(m).count('退款（refundTo） 我') == 2, text(m))


def guided_scenarios(k):
    """Everything reachable by pasting and pressing buttons, without typing a command."""
    claim = k['ClaimFor']
    got = lambda addr: num(claim, 'claimedOf(address)(uint256)', addr)  # noqa: E731
    bot.save_prefs(dict(bot.DEFAULT_PREFS, wallets='AB'))
    bot.AWAIT.clear()

    m = send('/start')
    hd = panel_data(m)
    check('主菜单：说明贴什么会怎样、有总览／状态／挂单／设置按钮',
          '直接贴给我就好' in text(m) and all(x in hd for x in ('👤 总览', '📊 钱包状态', '📌 挂单', '⚙️ 设置', '📖 全部指令'))
          and '总览' in keyboard_names(m), text(m))
    m = press_data(hd['📖 全部指令'])
    check('按「全部指令」：白话说明、有回主菜单', '全部指令' in text(m) and '⬅️ 回主菜单' in panel_data(m), text(m))
    m = send('/whatever')
    check('打错指令：说没有、带主菜单', '没有这个指令' in text(m) and buttons(m), text(m))

    m = press_data(hd['📊 钱包状态'])
    check('钱包状态：一行一个钱包、两个都正常', '2 个钱包都正常' in text(m) and 'binancecoin' in text(m)
          and '开发者模式到' not in text(m), text(m))
    m = press_data(panel_data(m)['📋 详细'])
    check('按「详细」：才显示完整地址、开发者模式、额度', '开发者模式到' in text(m) and A in text(m), text(m))

    # a project contract pasted on its own
    m = send(claim)
    cd = panel_data(m)
    check('贴项目合约地址：出合约卡片和中文动作按钮', '项目合约' in text(m)
          and any(x.startswith('领取（claim）') for x in cd) and all(len(v.encode()) <= 64 for v in cd.values()),
          text(m) + str(list(cd)))
    m = press_data(key_like(cd, '领取（claim）'))
    check('按动作：用中文一项项说要填什么、附例子', '请照顺序填' in text(m) and '打「我」' in text(m)
          and '例如：我 1' in text(m) and not buttons(m), text(m))
    m = send('我')
    check('少填一个：说要填 2 个、不出确认钮', '要填 2 个，你填了 1 个' in text(m) and not buttons(m), text(m))
    base = {w: got(addr) for w, addr in (('A', A), ('B', B))}
    press_data(key_like(cd, '领取（claim）'))
    m = send('我 2')
    pd = panel_data(m)
    check('打「我 2」：A、B 各出预览（中文），有存成快捷键', text(m).count('领取（claim） 我 2') == 2
          and '⭐ 存成快捷键' in pd and got(A) == base['A'], text(m))
    press(m)
    check('确认后：A、B 各领 2', got(A) == base['A'] + 2 and got(B) == base['B'] + 2)
    m = press_data(pd['⭐ 存成快捷键'])
    check('按「存成快捷键」：请你取名字', '取个名字' in text(m), text(m))
    m = send('领二')
    check('取名「领二」：输入框下方多一颗', '领二' in keyboard_names(m), text(m))
    m = send('领二')
    check('按「领二」：先出预览、还没送', buttons(m) and got(A) == base['A'] + 2, text(m))
    EXEC_ERR.append('request timed out')
    r = press(m)
    check('送出逾时：说结果未知、叫你先别重按', '结果未知' in text(r) and '先别重按' in text(r) and '再按一次' not in text(r),
          text(r))
    send('/key 领二')

    # someone else's transaction hash
    tx = c('send', claim, 'claim(address,uint256)', OTHER, '3', '--from', OTHER, '--unlocked', '--async').split()[-1]
    m = send(tx)
    xd = panel_data(m)
    check('贴交易 hash：说明他做了什么、有照抄按钮', '别人的一笔交易' in text(m) and '领取（claim） 0x1111…1111 3' in text(m)
          and '上链成功' in text(m)
          and '照抄（A）' in xd and all(len(v.encode()) <= 64 for v in xd.values()), text(m))
    before = got(A)
    m = press_data(xd['照抄（A）'])
    check('按「照抄（A）」：出预览、还没送', '照抄这笔交易' in text(m) and buttons(m) and got(A) == before, text(m))
    press(m)
    check('确认后 A 照抄领到 3', got(A) == before + 3)


def guard_scenarios(k):
    """Double sends, a wallet locked by a pending App confirmation, a dropped login."""
    claim = k['ClaimFor']
    got = lambda addr: num(claim, 'claimedOf(address)(uint256)', addr)  # noqa: E731
    CLOCK.offset += 60
    base = got(A)
    m1 = send(f'/tx A {claim} claim(address,uint256) 我 1')
    m2 = send(f'/tx A {claim} claim(address,uint256) 我 1')
    press(m1)
    r = press_data(next(inline(x) for x in reversed(m2) if inline(x))[0][0]['callback_data'])
    check('同一笔开两张预览、连按两次确认：只送一次', got(A) == base + 1 and '防止重复' in text(r), text(r))
    logged = [json.loads(x) for x in bot.ORDERS_LOG.read_text().splitlines()]
    check('送出的交易写进订单日志', any(x['kind'] == 'call' and x['wallet'] == 'A' and x['ok'] for x in logged))

    CLOCK.offset += 60
    APP_CONFIRM.append(1)
    LOCK['A'] = ['UNLOCKED', 'LOCKED', 'LOCKED', 'UNLOCKED']
    m = send(f'/tx A {claim} claim(address,uint256) 我 1')
    r = press(m)
    check('要在币安 App 再确认：讲清楚、等解锁后再通知', '币安 App 再确认一次' in text(r) and '解锁了' in text(r)
          and got(A) == base + 1, text(r))

    CLOCK.offset += 60
    m = send(f'/tx A {claim} claim(address,uint256) 我 1')
    LOCK['A'] = ['LOCKED'] * 20
    r = press(m)
    check('钱包被锁住（等 App 确认）：这次不送、叫你先去 App 处理', '钱包锁住了' in text(r) and got(A) == base + 1, text(r))
    LOCK.clear()

    STATUS['B'] = 'UNCONNECTED'
    start = len(SENT)
    bot.check_logins()
    first = text(SENT[start:])
    bot.check_logins()
    second = SENT[start:]
    bot.check_logins()
    check('钱包掉线：连续两次才私信一次，附重新登录按钮', first == '' and text(second).count('钱包 B 掉线了') == 1
          and '🔑 重新登录 B' in panel_data(second) and text(SENT[start:]).count('掉线了') == 1, text(SENT[start:]))
    m = send('/status')
    check('钱包状态：没登录的钱包有「重新登录」按钮', '🔑 重新登录 B' in panel_data(m) and 'UNCONNECTED' in text(m), text(m))
    STATUS.clear()
    start = len(SENT)
    bot.check_logins()
    check('重新连上：通知一次', '钱包 B 又连上了' in text(SENT[start:]), text(SENT[start:]))


def transfer_scenarios():
    """Moving tokens to the payout wallet: by hand from the card, right after a buy, and once the token graduates."""
    tok = deploy('MockToken')
    bal = lambda addr: num(tok, 'balanceOf(address)(uint256)', addr)  # noqa: E731
    for addr, n in ((A, 100), (B, 50)):
        c('send', tok, 'mint(address,uint256)', addr, str(n * ONE_BNB), '--from', DEPLOYER, '--unlocked')
    bot.save_prefs(dict(bot.DEFAULT_PREFS, wallets='AB'))
    real_audit, bot.audit = bot.audit, lambda t: (1, '✅ 低风险')
    bot.PAYOUT.clear()
    try:
        m = send(tok)
        pd = panel_data(m)
        check('交易卡片有「📤 转出」', '📤 转出' in pd, str(list(pd)))
        m = press_data(pd['📤 转出'])
        check('没设置目的地：说要在电脑 wallets.json 填、不出确认', 'wallets.json' in text(m) and not buttons(m), text(m))

        bot.PAYOUT.append({'name': '主钱包', 'addr': MAIN})
        m = press_data(pd['📤 转出'])
        check('转出预览：转到主钱包、A 全部 100、B 全部 50', '转出预览' in text(m) and '主钱包' in text(m)
              and 'A</b>　全部 100 颗' in text(m) and 'B</b>　全部 50 颗' in text(m) and buttons(m), text(m))
        r = press(m)
        check('目的地不在币安地址簿：说要先加、没转', text(r).count('地址簿') == 2 and bal(MAIN) == 0, text(r))
        ADDRBOOK.add(MAIN.lower())
        r = press(press_data(pd['📤 转出']))
        check('加进地址簿后确认：A、B 全部转到主钱包', bal(MAIN) == 150 * ONE_BNB and bal(A) == 0
              and text(r).count('已转到 主钱包') == 2, text(r))

        press_data(pd['⚙️ 设置'])
        st, sd = view()
        check('设置页有自动转出（目前关）', '📤 自动转出：关 🔁' in sd and '转到 主钱包' in st, st)
        press_data(sd['📤 自动转出：关 🔁'])
        check('按一下：改成买到就转', bot.load_prefs()['auto_send'] == 'fill')

        c('send', tok, 'mint(address,uint256)', A, str(10 * ONE_BNB), '--from', DEPLOYER, '--unlocked')
        m = send(f'/buy {tok} 0.01 A')
        check('买入预览写出会买到就转', '买到就转到 主钱包' in text(m), text(m))
        r = press(m)
        check('成交后马上把 A 的币转到主钱包', bal(MAIN) == 160 * ONE_BNB and '已转到 主钱包' in text(r), text(r))

        bot.save_prefs(dict(bot.load_prefs(), auto_send='grad'))
        INFO[tok.lower()] = dict(DEFAULT_INFO, migrateStatus=0)
        c('send', tok, 'mint(address,uint256)', B, str(5 * ONE_BNB), '--from', DEPLOYER, '--unlocked')
        r = press(send(f'/buy {tok} 0.01 B'))
        check('毕业后转：成交时先记下来、还没转', '毕业后自动把 MOCK 转到 主钱包' in text(r) and bal(B) == 5 * ONE_BNB, text(r))
        bot.run_sends()
        check('还在内盘：每分钟检查但不转', bal(B) == 5 * ONE_BNB)
        INFO[tok.lower()] = dict(DEFAULT_INFO, migrateStatus=1)
        start = len(SENT)
        bot.run_sends()
        check('毕业了：自动把 B 的币转到主钱包并通知', bal(MAIN) == 165 * ONE_BNB and '毕业了' in text(SENT[start:])
              and bot.load_sends()[-1]['status'] == 'done', text(SENT[start:]))
        start = len(SENT)
        bot.run_sends()
        check('转完不会再转一次', text(SENT[start:]) == '', text(SENT[start:]))
    finally:
        bot.audit = real_audit
        bot.PAYOUT.clear()
        INFO.pop(tok.lower(), None)
        bot.save_prefs(dict(bot.DEFAULT_PREFS, wallets='AB'))


def wallet_scenarios():
    """Adding a third wallet from Telegram (baw login faked), using it everywhere, then removing it."""
    login = {'addr': C_ADDR, 'verify': True}
    real_run = bot.run

    def fake_run(cmd, env_extra=None, timeout=120):
        if cmd[:1] != [bot.BAW]:
            return real_run(cmd, env_extra, timeout)
        a = cmd[1:3]
        if a == ['auth', 'signin']:
            return 0, json.dumps({'success': True, 'data': {'urlForWeb': 'https://web3.binance.com/en/agent-login?x=1',
                                                            'qrCodeId': 'q1', 'pairingCode': '123456'}}), ''
        if a == ['auth', 'verify']:
            ok = login['verify']
            return 0, json.dumps({'success': True, 'data': {'status': 'SUCCESS'}} if ok else
                                 {'success': False, 'error': {'name': 'AUTH_REJECTED', 'message': 'expired'}}), ''
        if a == ['wallet', 'address']:
            return 0, json.dumps({'success': True, 'data': {'addresses': [{'binanceChainId': '56',
                                                                           'address': login['addr']}]}}), ''
        return 1, '', 'unsupported'

    bot.run = fake_run
    DEVMODE['C'] = True
    c('send', bot.AGENT_ID_REGISTRY, 'setHandle(address,string)', C_ADDR, 'ccc', '--from', DEPLOYER, '--unlocked')
    try:
        m = send('/status')
        check('钱包状态有「➕ 添加钱包」', '➕ 添加钱包' in panel_data(m), text(m))
        login['verify'] = False
        m = send('/wallet add')
        check('添加钱包：给登录链接和配对码；App 没确认就说没连上、不加进来',
              '123456' in text(m) and 'agent-login' in text(m) and '没连上' in text(m) and 'C' not in bot.WALLETS, text(m))
        login['verify'] = True
        m = send('/wallet add')
        saved = json.loads(bot.WALLETS_FILE.read_text())
        check('App 确认后钱包 C 自动加入并写进设置档',
              '钱包 <b>C</b> 已加入' in text(m) and bot.WALLETS.get('C', {}).get('addr') == C_ADDR
              and bot.WALLETS['C']['dir'].endswith('/.baw-c') and any(r['name'] == 'C' for r in saved['wallets']), text(m))
        check('三个钱包：AC、ABC、全部 都认得', bot.parse_wallets(['ac']) == ['A', 'C']
              and bot.parse_wallets(['ABC']) == ['A', 'B', 'C'] and bot.parse_wallets(['全部']) == ['A', 'B', 'C'])
        cyc, cur = [], ['A', 'B', 'C']
        for _ in range(4):
            cur = bot.next_wallets(cur)
            cyc.append(''.join(cur))
        check('卡片钱包切换：ABC→A→B→C→ABC', cyc == ['A', 'B', 'C', 'ABC'], str(cyc))
        m = send('/status')
        check('钱包状态栏出 3 个', '3 个钱包都正常' in text(m) and '<b>C</b>' in text(m), text(m))
        login['addr'] = A
        m = send('/wallet add')
        check('同一个账号再登录一次：说已经是钱包 A、不重复加', '已经是钱包 A' in text(m) and 'D' not in bot.WALLETS, text(m))
        login['addr'] = B
        m = send('/wallet login B')
        check('从手机重新登录 B：同一个地址就接回来', '钱包 <b>B</b> 重新登录好了' in text(m), text(m))
        login['addr'] = C_ADDR
        m = send('/wallet login B')
        check('重新登录却换了账号：警告、不当成 B', '不是原本的' in text(m) and bot.WALLETS['B']['addr'] == B, text(m))
        m = send('/wallet del C')
        check('移除钱包要确认', '移除' in text(m) and buttons(m) and 'C' in bot.WALLETS, text(m))
        press(m)
        check('确认后 C 从清单和设置档拿掉', 'C' not in bot.WALLETS
              and all(r['name'] != 'C' for r in json.loads(bot.WALLETS_FILE.read_text())['wallets']))
    finally:
        bot.run = real_run
        bot.WALLETS_FILE.write_text(json.dumps(TEST_WALLETS))
        bot.WALLETS.clear()
        bot.WALLETS.update(bot.load_wallets()[0])


def patched(**fns):
    """Swap bot functions for the body of a with-block."""
    class Ctx:
        def __enter__(self):
            self.old = {k: getattr(bot, k) for k in fns}
            for k, v in fns.items():
                setattr(bot, k, v)

        def __exit__(self, *exc):
            for k, v in self.old.items():
                setattr(bot, k, v)
    return Ctx()


def review_fixes():
    """The automatic paths (sniped positions, TP/SL rules) against the failures found in the 10/27 review."""
    tok = '0x' + '7' * 40
    pos = lambda **kw: dict({'token': tok, 'symbol': 'X', 'wallet': 'A', 'cost': '1000', 'time': 1,  # noqa: E731
                             'tp_done': False, 'fails': 0, 'open': True}, **kw)
    sells = []
    with patched(detect_curve=lambda t: bot.DEX_CV, word=lambda *a: None):
        p = pos()
        bot.check_position(p, Decimal(2), Decimal(50), Decimal(50))
        check('狙击持仓：节点读余额失败不会关仓（下一轮再看）', p['open'], str(p))
    with patched(detect_curve=lambda t: bot.DEX_CV, word=lambda *a: 0):
        p = pos()
        start = len(SENT)
        bot.check_position(p, Decimal(2), Decimal(50), Decimal(50))
        check('狙击持仓：确定读到 0 才结束，并通知', not p['open'] and '没有持仓' in text(SENT[start:]), str(p))
    values = iter([2000, 900, 340])
    with patched(detect_curve=lambda t: bot.DEX_CV, position_value=lambda cv, t, w: next(values),
                 auto_sell=lambda t, w, pct, cv=None: (sells.append(pct), (True, '0x' + 'a' * 64))[1]):
        p = pos()
        tp_x, tp_sell, sl = Decimal('1.5'), Decimal(50), Decimal(30)
        bot.check_position(p, tp_x, tp_sell, sl)
        check('止盈卖 50% 后：成本跟着减半、标记止盈已做', sells == [50] and p['cost'] == '500' and p['tp_done'], str(p))
        bot.check_position(p, tp_x, tp_sell, sl)
        check('止盈后剩下部位还在 1.8 倍：不会被止损误卖', sells == [50] and p['open'], str(sells))
        bot.check_position(p, tp_x, tp_sell, sl)
        check('剩下部位跌到买价 -32%：止损全卖', sells == [50, 100] and not p['open'], str(sells))
    m = None
    try:
        bot.tp_multiple('0.5')
    except bot.UsageError as e:
        m = str(e)
    check('狙击止盈倍数 ≤ 1 会被拒绝（不然每 20 秒重复卖）', m and '大于 1' in m and bot.tp_multiple('2') == '2', str(m))
    check('数量是 JSON 小数（5e-05）也能算卖出数量', bot.share_of(5e-05, 100) == '0.00005'
          and bot.share_of(5e-05, 50) == '0.00002', bot.share_of(5e-05, 50))  # never more decimals than the balance
    code, _, err = bot.run(['sleep', '3'], timeout=1)
    check('外部指令逾时：回报失败、不让背景循环当掉', code == 124 and 'timed out' in err, err)
    # TP/SL rules: a failed sell goes back on watch, three strikes and it stops
    bot.save_rules([dict(bot.make_rule(tok, 'X', 'A', 'sl', Decimal(10), Decimal(100), Decimal(1)))])
    with patched(token_info=lambda t: {'price': '0.5'}, fire_rule=lambda r, p: 'retry'):
        for _ in range(3):
            bot.check_rules()
    r = bot.load_rules()[0]
    check('止损卖不掉：重新盯盘，连续 3 次后才停用', r['status'] == 'failed' and r['fails'] == 3, str(r))
    bot.save_rules([dict(bot.make_rule(tok, 'X', 'A', 'sl', Decimal(10), Decimal(100), Decimal(1)))])
    with patched(token_info=lambda t: {'price': '0.5'}, fire_rule=lambda r, p: 'unknown'):
        bot.check_rules()
        bot.check_rules()
    check('止损结果未知：不重试（避免卖两次）', bot.load_rules()[0]['status'] == 'fired', str(bot.load_rules()))
    # automatic TP/SL after each buy: a second buy replaces the pair instead of stacking another
    bot.save_rules([])
    prefs = dict(bot.load_prefs(), tp=['100', '50'], sl=['30', '100'])
    with patched(token_info=lambda t: {'price': '1'}, token_names=lambda t: ('X', 'X')):
        bot.arm_auto('A', tok, prefs)
        bot.arm_auto('A', tok, prefs)
    armed = [x for x in bot.load_rules() if x['status'] == 'armed']
    check('同一颗币买两次：自动止盈止损只留最新一组', len(armed) == 2, str(bot.load_rules()))
    bot.save_rules([])
    # a snipe whose result is unknown keeps its budget and is watched like a fill
    bot.save_snipe({'day': bot.local_day(), 'bnb': '0.01', 'tokens': 1, 'done': {}, 'positions': []})
    cv = {'kind': 'gang', 'name': 'Gang', 'state': 'curve', 'to': bot.GANG_V2_PAD, 'quote': None, 'progress': 1}
    with patched(detect_curve=lambda t: cv, auto_run=lambda w, steps, rounds=3: (False, f'{bot.UNKNOWN_MARK}（timed out）'),
                 token_names=lambda t: ('X', 'X')):
        bot.snipe_buy({'track': 'gang', 'token': tok, 'tx': None}, {'A': 10 ** 16})
    st = bot.load_snipe()
    check('狙击结果未知：不退额度、照样记成持仓自动盯', st['bnb'] == '0.01' and len(st['positions']) == 1
          and st['positions'][0].get('unknown'), str(st))
    with patched(detect_curve=lambda t: None, token_info=lambda t: {'price': '1'}):
        check('一般币（没有 migrateStatus）设「毕业后转」不会永远等', bot.is_graduated(tok))
    bot.SNIPE_FILE.unlink(missing_ok=True)


def main():
    anvil = subprocess.Popen([ANVIL, '--port', str(PORT), '--chain-id', '56', '--silent'])
    try:
        for _ in range(50):
            try:
                c('block-number')
                break
            except RuntimeError:
                time.sleep(0.2)
        for addr in (A, B):
            c('rpc', 'anvil_impersonateAccount', addr)
            c('rpc', 'anvil_setBalance', addr, hex(ONE_BNB))
        registry = deploy('MockAgentId')
        c('rpc', 'anvil_setCode', '0x60B9262456dE011F7d15a54CdD8c48bAaa8B022F', c('code', registry))
        for addr, handle in ((A, 'binancecoin'), (B, 'zzz66')):
            c('send', '0x60B9262456dE011F7d15a54CdD8c48bAaa8B022F', 'setHandle(address,string)', addr, handle,
              '--from', DEPLOYER, '--unlocked')
        k = {name: deploy(name) for name in ('CapMint', 'AmountBuy', 'SlotLaunch', 'MinQtyTimed', 'ClaimFor')}
        scenarios(k)
        presale_scenarios(deploy('PresalePool'), k)
        guided_scenarios(k)
        guard_scenarios(k)
        transfer_scenarios()
        wallet_scenarios()
        review_fixes()
        try:  # baw sends big simulated changes as JSON floats (seen live on a Gang V3 buy)
            shown = bot.change_text({'tokenType': 'Native', 'change': 1.1812190227722543e+24})
        except ValueError as e:
            shown = repr(e)
        check('预览：模拟变化量是科学记号（1.18e+24）也能显示', shown.startswith('BNB +'), shown)
        fail = lambda name, msg: {'success': False, 'error': {'name': name, 'message': msg}}  # noqa: E731
        expired = bot.baw_err(fail('SESSION_EXPIRED', 'Please log in first.'))
        check('错误中文：登录过期', '登录过期' in expired and 'Please' not in expired, expired)
        sim = bot.baw_err(fail('SERVICE_ERROR', 'simulation error'))
        check('错误中文：链上试跑失败说原因', '试跑失败' in sim and 'simulation' not in sim, sim)
        why = bot.baw_err(fail('SERVICE_ERROR', 'simulation failed: execution reverted: TooLate'))
        check('错误中文：试跑失败附合约原因', 'TooLate' in why, why)
        odd = bot.baw_err(fail('SERVICE_ERROR', 'something new'))
        check('错误中文：没见过的错误保留原文', odd.startswith('币安回复') and 'something new' in odd, odd)
        check('错误中文：判断结果未知仍看原文', bool(re.search(bot.UNKNOWN_RE, bot.baw_raw(fail('REQUEST_TIMEOUT', 'x')))))
    finally:
        anvil.terminate()
        bot.WALLETS_FILE.unlink(missing_ok=True)
        bot.ORDERS_LOG.unlink(missing_ok=True)
        bot.SENDS_FILE.unlink(missing_ok=True)
        bot.PREFS_FILE.unlink(missing_ok=True)
        bot.RULES_FILE.unlink(missing_ok=True)
    failed = [r for r in RESULTS if not r[1]]
    print(f'\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
