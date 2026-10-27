#!/usr/bin/env python3
"""Launch-curve trading against the real BSC contracts on a local anvil fork. Nothing is sent to BSC.

Reuses the fakes of run_scenarios.py (baw, Telegram, clock); the wallet is an unused test address, impersonated and
given an Agent ID on the fork. Run from WSL:  python3 tests/run_fork.py
"""

import os
import subprocess
import sys
import time
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import run_scenarios as rs  # noqa: E402

bot = rs.bot
FORK_PORT = 8612
FORK = f'http://127.0.0.1:{FORK_PORT}'
UPSTREAM = os.environ.get('FORK_URL', 'https://bsc-dataseed.bnbchain.org')
# an address with no history on BSC (anvil's own dev accounts carry EIP-7702 code there and can't receive BNB)
HOLDER = '0x7e57000000000000000000000000000000007e57'
AGENT_BALANCE_SLOT = 3  # ERC721 _balances in the Agent ID registry
TOKENS = {
    'four_agent': '0xd47d886b39363337c2048c6e829adba05e5fffff',
    'four': '0x5dfdbd1c2a3229ba56fdae40f2038d01cc1e4444',
    'four_usdt': '0x52d729a5b8ac8b6a1cf0110ab53a1b72fbd34444',
    'gang_old': '0xc7fbba0a9f8bc7a3331f170589098c2beb1fffdd',
    'gang': '0xae496ca8c7fdc7352af93ece407a1b996783ce0b',
    'flap': '0xfd4db1117909eed32e14a62ea1dbcc97d5dd7777',
    'ac': '0x21f69ed2c22b99f383a17b4d76fbf2080d6e7777',  # predicted address: the token is deployed at graduation
    'dex': '0x5F6A1cE1733Fc4834a526798c0e8Cf85137eFFfF',  # OpenFour token that already graduated
}
check, send, text, inline = rs.check, rs.send, rs.text, rs.inline


def held(token):
    cv = bot.detect_curve(token)
    if cv and cv['kind'] == 'ac' and cv['state'] == 'curve':
        return bot.word(cv['market'], 'sharesOf(address)', HOLDER) or 0
    return bot.word(token, 'balanceOf(address)', HOLDER) or 0


def wbnb():
    return bot.word(bot.WBNB, 'balanceOf(address)', HOLDER) or 0


def confirm_all(msgs, rounds=4):
    """Press Confirm on each preview the bot shows (approve / wrap rounds bring the next preview by themselves)."""
    seen = list(msgs)
    for _ in range(rounds):
        prev = next((m for m in reversed(msgs) if inline(m) and '📝' in m.get('text', '')), None)
        if not prev:
            break
        bot.SENT_AT.clear()
        msgs = rs.press_data(inline(prev)[0][0]['callback_data'])
        seen += msgs
    return seen


def trade(kind, buy='0.005'):
    token = TOKENS[kind]
    before = held(token)
    m = confirm_all(send(f'/buy {token} {buy} A'))
    after_buy = held(token)
    check(f'{kind}：买入后持有增加', after_buy > before, text(m)[-1500:])
    if kind == 'gang_v3':
        check('gang_v3：经路由直接付 BNB，一步买完（没有换 WBNB／授权那一步）',
              '直接付 BNB' in text(m) and '第 1／2 步' not in text(m) and wbnb() == 0, text(m)[-1500:])
    if after_buy <= before:
        return
    m = confirm_all(send(f'/sell {token} 50 A'))
    half = held(token)
    check(f'{kind}：卖 50% 后剩约一半', 0 < half < after_buy * 6 // 10, f'{after_buy} -> {half}\n' + text(m)[-1500:])
    m = confirm_all(send(f'/sell {token} 100 A'))
    check(f'{kind}：全部卖完', held(token) == 0, text(m)[-1500:])
    if kind in ('gang', 'gang_v3'):
        check('gang：卖出拿到的 WBNB 已换回 BNB', wbnb() == 0, str(wbnb()))


def refork():
    """Move the fork to the newest block: public nodes prune old state ("missing trie node") within minutes."""
    rs.c('rpc', 'anvil_reset', f'[{{"forking":{{"jsonRpcUrl":"{UPSTREAM}"}}}}]', '--raw')
    # the EIP-2935 blockhash contract makes the fork fetch remote storage on every gas estimate, which flakes
    rs.c('rpc', 'anvil_setCode', '0x0000F90827F1C53a10cb7A02335B175320002935', '0x')
    rs.c('rpc', 'anvil_impersonateAccount', HOLDER)
    rs.c('rpc', 'anvil_setBalance', HOLDER, hex(rs.ONE_BNB))
    # agent-only curves check the registry's balanceOf, so give the test address one Agent ID on the fork
    slot = rs.sh(rs.CAST, 'index', 'address', HOLDER, str(AGENT_BALANCE_SLOT))
    rs.c('rpc', 'anvil_setStorageAt', bot.AGENT_ID_REGISTRY, slot, '0x' + '1'.rjust(64, '0'))


def launch_v3():
    """Create a Gang V3 taxed-token curve on the fork (no V3 token exists on mainnet yet) and check the bot sees it."""
    import json
    out = rs.c('send', bot.GANG_V3_PAD, '--from', HOLDER, '--unlocked', '--json',
               'create(string,string,string,string,address,address,(uint16,uint16,uint16,address))',
               'Fork V3', 'FV3', '', '', bot.WBNB, bot.ZERO_ADDR, f'(100,100,0,{HOLDER})')
    rc = json.loads(out)
    log = next((dict(l, blockNumber=rc['blockNumber']) for l in rc.get('logs') or []
                if l['topics'][0].lower() == '0x416fe98c8f1ea2977674a1a4ee6252eb72597a8392fc2d7d0599a2bf5d8fdb63'), None)
    check('Gang V3：分叉上发出测试币、有 V3 Created 事件', log is not None, out[-800:])
    if not log:
        return None
    c = bot.decode_launch(log)
    check('Gang V3：狙击认得 V3 Created（token／发币者／计价币）', c and c['track'] == 'gang'
          and c['quote'].lower() == bot.WBNB.lower() and c['pad'].lower() == bot.GANG_V3_PAD.lower(), str(c))
    cv = bot.detect_curve(c['token'])
    check('Gang V3：判断成 Gang V3 内盘（WBNB 计价）', cv and cv['name'] == 'Gang V3' and cv['state'] == 'curve'
          and cv.get('wrap'), str(cv))
    return c['token']


def snipe():
    token = TOKENS['gang']
    s = dict(bot.DEFAULT_PREFS['snipe'], on=True, live=True, tp_x='0.5', tp_sell='50', sl='0')
    bot.save_prefs(dict(bot.load_prefs(), snipe=s))
    c = {'track': 'gang', 'token': token, 'pad': bot.GANG_V2_PAD, 'tx': None, 'block': 0, 'ts': None, 'delay': 1}
    start = len(rs.SENT)
    bot.snipe_buy(c, {'A': 3 * 10 ** 15})
    out = text(rs.SENT[start:])
    pos = bot.open_positions(bot.load_snipe())
    check('狙击：Gang WBNB 盘自动包 WBNB、授权、买入（不用按确认）', held(token) > 0 and '狙到' in out
          and len(pos) == 1, out)
    start = len(rs.SENT)
    bot.check_positions()
    after_tp = held(token)
    check('狙击持仓：到止盈倍数自动卖 50%', '自动卖 50%' in text(rs.SENT[start:]) and 0 < after_tp, text(rs.SENT[start:]))
    bot.save_prefs(dict(bot.load_prefs(), snipe=dict(s, tp_x='0', sl='1')))
    bot.SENT_AT.clear()  # live checks are 20 s apart; back to back here the same-size approval trips the 5 s guard
    start = len(rs.SENT)
    bot.check_positions()
    check('狙击持仓：跌破止损自动全卖、持仓结束', held(token) == 0 and not bot.open_positions(bot.load_snipe()),
          text(rs.SENT[start:]))


def main():
    anvil = subprocess.Popen([rs.ANVIL, '--port', str(FORK_PORT), '--chain-id', '56', '--fork-url', UPSTREAM, '--silent'])
    tmp = '/tmp/agent-wallet-tg-fork-'
    bot.SNIPE_FILE = rs.Path(f'{tmp}snipe-{os.getpid()}.json')
    bot.SNIPE_LOG = rs.Path(f'{tmp}snipe-log-{os.getpid()}.jsonl')
    try:
        rs.RPC = bot.RPC = FORK
        for _ in range(100):
            try:
                rs.c('block-number')
                break
            except RuntimeError:
                time.sleep(0.3)
        refork()
        bot.WALLETS.clear()
        bot.WALLETS['A'] = {'addr': HOLDER, 'dir': '', 'env': {}}
        bot.ADDR_OK.clear()
        bot.audit = lambda t: (1, '✅ 低风险')
        bot.save_prefs(dict(bot.DEFAULT_PREFS, wallets='A'))
        v3 = launch_v3()
        if v3:
            TOKENS['gang_v3'] = v3
            trade('gang_v3')
            rs.c('send', bot.WBNB, 'deposit()', '--value', str(10 ** 16), '--from', HOLDER, '--unlocked')
            rs.INFO[v3.lower()] = {'price': None, 'liquidity': None}  # a curve token has no DEX pool yet
            m = [x for x in send(v3) if inline(x)][-1:]
            card = text(m)
            check('卡片：V3 显示内盘阶段、税（内盘 0%／毕业后 1%）、剩下的 WBNB',
                  '内盘阶段' in card and '内盘 0%｜毕业后 买 1%' in card and '💱 WBNB' in card, card)
            m = confirm_all(rs.press_data(rs.key_like({b['text']: b['callback_data'] for r in inline(m[-1]) for b in r},
                                                      '💱 WBNB 换回 BNB')))
            check('卡片「WBNB 换回 BNB」：预览、确认后 WBNB 归 0', wbnb() == 0, text(m)[-1200:])
            m = send('发射台 0x15cfd2dc8278fb87a1783ef42111618e20874365 路由 0xe040445690aa56c8b68b7f7038c7b2562b5079c4')
            check('贴含发射台地址的公告：列出是什么＋打开按钮，不再说看不懂', 'Gang V3 发射台' in text(m)
                  and '看不懂' not in text(m), text(m))
        for kind in ('four_agent', 'four', 'four_usdt', 'gang_old', 'gang', 'flap', 'ac'):
            refork()
            trade(kind)
        refork()
        rs.MARKET['quote_ok'] = False  # Binance can't route it: buy and sell go straight to PancakeSwap
        token = TOKENS['dex']
        m = confirm_all(send(f'/buy {token} 0.005 A'))
        got = held(token)
        check('已毕业、币安报不出价：改直连 PancakeSwap 买到', got > 0 and 'PancakeSwap' in text(m), text(m)[-1500:])
        rs.MARKET['holdings']['A'] = [{'address': token, 'balance': str(Decimal(got) / 10 ** 18), 'symbol': '4AGENT',
                                       'value': '1'}]
        m = confirm_all(send(f'/sell {token} 100 A'))
        check('已毕业、币安报不出价：改直连 PancakeSwap 全部卖掉', held(token) == 0, text(m)[-1500:])
        rs.MARKET['quote_ok'], rs.MARKET['holdings'] = True, {}
        refork()
        snipe()
    finally:
        anvil.terminate()
        for f in (bot.PREFS_FILE, bot.RULES_FILE, bot.ORDERS_LOG, bot.SENDS_FILE, bot.WALLETS_FILE, bot.SNIPE_FILE,
                  bot.SNIPE_LOG):
            f.unlink(missing_ok=True)
    failed = [r for r in rs.RESULTS if not r[1]]
    print(f'\n{len(rs.RESULTS) - len(failed)}/{len(rs.RESULTS)} passed')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
