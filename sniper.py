"""
Sniper Simulator (Sepolia, Uniswap V2).

Event-driven: слушатель (listen) ловит события и кладёт их в очередь, обработчик (trade)
ведёт сделку как машину состояний:
    WAITING -> PAIR_FOUND (PairCreated) -> LIQUIDITY (Mint) -> QUOTED -> BOUGHT -> SOLD
                                                          или SKIPPED / FAILED
Покупка и продажа выполняются кошельком B. Перед отправкой каждая транзакция
сначала симулируется (eth_call). Выход: TP, SL или таймаут. В конце — лог и PnL.

Нужно: sniper_state.json (его создаёт deploy_pool.py), в .env: SEPOLIA_RPC, SNIPER_KEY_B.
Запуск:
    python sniper.py                      # ждать новый пул (запусти ДО deploy_pool.py)
    python sniper.py --from-block 12049650  # разобрать уже созданный пул (повтор по истории)
Настройки (.env, необязательно): BUY_ETH=0.003 SLIPPAGE=5 TP=10 SL=10 TIMEOUT=120
MAX_IMPACT=25 MIN_LIQ_ETH=0.005 POLL_SECONDS=3 MAX_RANGE=10
"""
import argparse
import asyncio
import json
import logging
import os
import time
from enum import Enum
from pathlib import Path

from dotenv import load_dotenv
from web3 import Web3

load_dotenv()
CHAIN_ID = 11155111
EXPLORER = "https://sepolia.etherscan.io/tx/"
STATE_FILE = Path("sniper_state.json")

TOPIC_PAIR = "0x" + bytes(Web3.keccak(text="PairCreated(address,address,address,uint256)")).hex()
TOPIC_MINT = "0x" + bytes(Web3.keccak(text="Mint(address,uint256,uint256)")).hex()

ROUTER_ABI = [
    {"name": "getAmountsOut", "type": "function", "stateMutability": "view",
     "inputs": [{"type": "uint256"}, {"type": "address[]"}], "outputs": [{"type": "uint256[]"}]},
    {"name": "swapExactETHForTokens", "type": "function", "stateMutability": "payable",
     "inputs": [{"name": "amountOutMin", "type": "uint256"}, {"name": "path", "type": "address[]"},
                {"name": "to", "type": "address"}, {"name": "deadline", "type": "uint256"}],
     "outputs": [{"type": "uint256[]"}]},
    {"name": "swapExactTokensForETH", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "amountIn", "type": "uint256"}, {"name": "amountOutMin", "type": "uint256"},
                {"name": "path", "type": "address[]"}, {"name": "to", "type": "address"},
                {"name": "deadline", "type": "uint256"}],
     "outputs": [{"type": "uint256[]"}]},
]
PAIR_ABI = [
    {"name": "token0", "type": "function", "stateMutability": "view", "inputs": [], "outputs": [{"type": "address"}]},
    {"name": "getReserves", "type": "function", "stateMutability": "view", "inputs": [],
     "outputs": [{"type": "uint112"}, {"type": "uint112"}, {"type": "uint32"}]},
]
ERC20_ABI = [
    {"name": "balanceOf", "type": "function", "stateMutability": "view", "inputs": [{"type": "address"}], "outputs": [{"type": "uint256"}]},
    {"name": "allowance", "type": "function", "stateMutability": "view", "inputs": [{"type": "address"}, {"type": "address"}], "outputs": [{"type": "uint256"}]},
    {"name": "approve", "type": "function", "stateMutability": "nonpayable", "inputs": [{"type": "address"}, {"type": "uint256"}], "outputs": [{"type": "bool"}]},
]

log = logging.getLogger("sniper")


class St(Enum):
    WAITING = "WAITING"
    PAIR_FOUND = "PAIR_FOUND"
    LIQUIDITY = "LIQUIDITY"
    QUOTED = "QUOTED"
    BOUGHT = "BOUGHT"
    SOLD = "SOLD"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"


def get_amount_out(x_in, r_in, r_out):
    """Формула Uniswap V2: x*y=k с комиссией 0,3% (997/1000)."""
    fee = x_in * 997
    return fee * r_out // (r_in * 1000 + fee)


def eth(wei):
    return f"{wei / 1e18:.6f}".rstrip("0").rstrip(".") or "0"


def tok(units):
    return f"{units / 1e18:,.4f}"


class Sniper:
    def __init__(self, from_block=None, notify=None):
        st = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        self.w3 = Web3(Web3.HTTPProvider(os.environ["SEPOLIA_RPC"], request_kwargs={"timeout": 30}))
        assert self.w3.is_connected() and self.w3.eth.chain_id == CHAIN_ID, "SEPOLIA_RPC: это не Sepolia"
        self.acct = self.w3.eth.account.from_key(os.environ["SNIPER_KEY_B"])
        self.token = Web3.to_checksum_address(st["token"])
        self.factory = Web3.to_checksum_address(st["factory"])
        self.weth = Web3.to_checksum_address(st["weth"])
        self.router = self.w3.eth.contract(address=Web3.to_checksum_address(st["router"]), abi=ROUTER_ABI)
        self.erc20 = self.w3.eth.contract(address=self.token, abi=ERC20_ABI)
        self.from_block = from_block
        self.pairs = set()
        self.state = St.WAITING
        self.win = int(os.getenv("MAX_RANGE", "10"))
        self.poll = float(os.getenv("POLL_SECONDS", "3"))
        self.buy_wei = int(float(os.getenv("BUY_ETH", "0.003")) * 10 ** 18)
        self.slip = float(os.getenv("SLIPPAGE", "5"))
        self.tp = float(os.getenv("TP", "10"))
        self.sl = float(os.getenv("SL", "10"))
        self.timeout = float(os.getenv("TIMEOUT", "120"))
        self.max_impact = float(os.getenv("MAX_IMPACT", "25"))
        self.min_liq = int(float(os.getenv("MIN_LIQ_ETH", "0.005")) * 10 ** 18)
        self.gas_total = 0
        self.done = asyncio.Event()
        self.notify = notify            # функция(text): отправка сообщения (например, в Telegram)
        self.loop = None
        self.position = None            # {"amount", "spent", "start"} пока позиция открыта
        self.last_note = ""

    def say(self, text, tg=None):
        """В лог всегда; в Telegram (notify) — если задан callback."""
        log.info(text)
        self.last_note = text
        if self.notify:
            try:
                self.notify(tg or text)
            except Exception as e:
                log.info("notify: %s", e)

    def stop(self):
        """Остановить ожидание (из другого потока). Открытую позицию не бросаем."""
        if self.loop and not self.done.is_set():
            self.loop.call_soon_threadsafe(self.done.set)

    def status_text(self):
        lines = [f"Состояние: {self.state.value}", f"Токен: {self.token}"]
        if self.position:
            try:
                value = self.position_value(self.position["amount"])
                pnl = (value / self.position["spent"] - 1) * 100
                left = self.timeout - (time.time() - self.position["start"])
                lines.append(f"Позиция: {tok(self.position['amount'])} SNTT, стоит {eth(value)} ETH, "
                             f"PnL {pnl:+.2f}% (до таймаута {max(0, left):.0f} с)")
            except Exception as e:
                lines.append(f"Позиция открыта (не удалось оценить: {e})")
        elif self.last_note:
            lines.append(f"Последнее: {self.last_note}")
        return "\n".join(lines)

    def set_state(self, new, note=""):
        text = f"[состояние] {self.state.value} -> {new.value} {note}".strip()
        icon = {"SKIPPED": "⏭", "FAILED": "❌"}.get(new.value)
        if icon:
            self.say(text, f"{icon} {text}")
        else:
            log.info(text)
        self.state = new

    # ---------- транзакции ----------
    def send(self, fn, value=0, what=""):
        """Симуляция (eth_call) -> отправка -> ожидание. Возвращает чек; газ в ETH считается в self.gas_total."""
        try:
            fn.call({"from": self.acct.address, "value": value})        # transaction simulation
        except Exception as e:
            raise RuntimeError(f"симуляция {what} не прошла (транзакция не отправлена): {e}")
        tip = self.w3.eth.max_priority_fee
        base = self.w3.eth.get_block("latest")["baseFeePerGas"]
        tx = fn.build_transaction({
            "from": self.acct.address, "value": value, "chainId": CHAIN_ID,
            "nonce": self.w3.eth.get_transaction_count(self.acct.address, "pending"),
            "maxPriorityFeePerGas": tip, "maxFeePerGas": base * 2 + tip,
        })
        signed = self.acct.sign_transaction(tx)
        raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
        h = self.w3.eth.send_raw_transaction(raw)
        txh = "0x" + bytes(h).hex()
        self.say(f"  {what}: tx {EXPLORER}{txh}", f"📨 {what}: {EXPLORER}{txh}")
        rc = self.w3.eth.wait_for_transaction_receipt(h, timeout=180)
        gas_cost = rc["gasUsed"] * rc["effectiveGasPrice"]
        self.gas_total += gas_cost
        if rc["status"] != 1:
            raise RuntimeError(f"{what}: транзакция откатилась (revert), блок {rc['blockNumber']}")
        log.info("  %s: блок %s, газ %s (%s ETH)", what, rc["blockNumber"], rc["gasUsed"], eth(gas_cost))
        return rc

    # ---------- слушатель событий ----------
    def fetch(self, a, b):
        """События PairCreated (фабрика) и Mint (пары) в блоках a..b."""
        events = []
        logs = self.w3.eth.get_logs({"fromBlock": a, "toBlock": b, "address": self.factory, "topics": [TOPIC_PAIR]})
        for lg in logs:
            t0 = Web3.to_checksum_address(bytes(lg["topics"][1])[-20:])
            t1 = Web3.to_checksum_address(bytes(lg["topics"][2])[-20:])
            pair = Web3.to_checksum_address(bytes(lg["data"])[12:32])
            if {t0, t1} == {self.token, self.weth}:                     # нас интересует только наш токен
                self.pairs.add(pair)
                events.append({"type": "PairCreated", "pair": pair, "block": lg["blockNumber"],
                               "tx": "0x" + bytes(lg["transactionHash"]).hex()})
        if self.pairs:
            logs = self.w3.eth.get_logs({"fromBlock": a, "toBlock": b, "address": list(self.pairs), "topics": [TOPIC_MINT]})
            for lg in logs:
                d = bytes(lg["data"])
                events.append({"type": "Mint", "pair": Web3.to_checksum_address(lg["address"]),
                               "amount0": int.from_bytes(d[:32], "big"), "amount1": int.from_bytes(d[32:64], "big"),
                               "block": lg["blockNumber"], "tx": "0x" + bytes(lg["transactionHash"]).hex()})
        return events

    async def listen(self, queue):
        last = (self.from_block - 1) if self.from_block else await asyncio.to_thread(lambda: self.w3.eth.block_number)
        self.say(f"Слушаю события с блока {last + 1} (PairCreated фабрики и Mint пары для токена {self.token})",
                 f"👀 Снайпер ждёт новый пул токена {self.token} (с блока {last + 1})")
        while not self.done.is_set():
            try:
                latest = await asyncio.to_thread(lambda: self.w3.eth.block_number)
                while last < latest:
                    a, b = last + 1, min(latest, last + self.win)
                    try:
                        evs = await asyncio.to_thread(self.fetch, a, b)
                    except Exception as e:
                        msg = str(e)
                        if ("400" in msg or "range" in msg.lower()) and self.win > 1:
                            self.win = max(1, self.win // 2)
                            log.info("RPC ограничил окно, уменьшаю до %s блоков", self.win)
                            continue
                        raise
                    for ev in sorted(evs, key=lambda e: (e["block"], 0 if e["type"] == "PairCreated" else 1)):
                        await queue.put(ev)
                    last = b
            except Exception as e:
                log.info("слушатель: %s (повтор через 10 с)", e)
                await asyncio.sleep(10)
                continue
            await asyncio.sleep(self.poll)

    # ---------- логика сделки ----------
    def quote(self, pair):
        pc = self.w3.eth.contract(address=pair, abi=PAIR_ABI)
        token0 = pc.functions.token0().call()
        r0, r1, _ = pc.functions.getReserves().call()
        r_tok, r_eth = (r0, r1) if token0 == self.token else (r1, r0)
        return r_tok, r_eth

    def position_value(self, amount):
        return self.router.functions.getAmountsOut(amount, [self.token, self.weth]).call()[-1]

    def do_buy(self, pair):
        r_tok, r_eth = self.quote(pair)
        log.info("Резервы пула: %s SNTT / %s ETH", tok(r_tok), eth(r_eth))
        reserves_note = f"Резервы пула: {tok(r_tok)} SNTT / {eth(r_eth)} ETH"
        if r_eth < self.min_liq:
            self.set_state(St.SKIPPED, f"— в пуле только {eth(r_eth)} ETH (нужно ≥ {eth(self.min_liq)})")
            return None
        out = get_amount_out(self.buy_wei, r_eth, r_tok)
        min_out = int(out * (1 - self.slip / 100))
        spot = r_tok / r_eth
        impact = max(0.0, (1 - (out / self.buy_wei) / (spot * 0.997)) * 100)
        onchain = self.router.functions.getAmountsOut(self.buy_wei, [self.weth, self.token]).call()[-1]
        calc_text = (f"Расчёт: покупка на {eth(self.buy_wei)} ETH -> amountOut ≈ {tok(out)} SNTT (роутер: {tok(onchain)}), "
                     f"price impact {impact:.2f}%, slippage {self.slip:.1f}% -> minOut {tok(min_out)} SNTT")
        self.say(calc_text, "🧮 " + reserves_note + "\n" + calc_text)
        if impact > self.max_impact:
            self.set_state(St.SKIPPED, f"— price impact {impact:.1f}% выше лимита {self.max_impact:g}%")
            return None
        self.set_state(St.QUOTED)
        before = self.erc20.functions.balanceOf(self.acct.address).call()
        self.send(self.router.functions.swapExactETHForTokens(
            min_out, [self.weth, self.token], self.acct.address, int(time.time()) + 300),
            value=self.buy_wei, what="ПОКУПКА")
        got = self.erc20.functions.balanceOf(self.acct.address).call() - before
        self.say(f"Куплено: {tok(got)} SNTT за {eth(self.buy_wei)} ETH",
                 f"🟢 Куплено: {tok(got)} SNTT за {eth(self.buy_wei)} ETH")
        self.set_state(St.BOUGHT)
        return got

    def do_sell(self, amount, reason):
        self.say(f"Продаю ({reason})", f"🔴 Продаю ({reason})")
        allowance = self.erc20.functions.allowance(self.acct.address, self.router.address).call()
        if allowance < amount:                                          # allowance: разрешение роутеру
            self.send(self.erc20.functions.approve(self.router.address, 2 ** 256 - 1), what="APPROVE")
        value = self.position_value(amount)
        min_out = int(value * (1 - self.slip / 100))
        bal_before = self.w3.eth.get_balance(self.acct.address)
        gas_before = self.gas_total
        self.send(self.router.functions.swapExactTokensForETH(
            amount, min_out, [self.token, self.weth], self.acct.address, int(time.time()) + 300), what="ПРОДАЖА")
        return self.w3.eth.get_balance(self.acct.address) - bal_before + (self.gas_total - gas_before)

    async def run_position(self, amount):
        spent = self.buy_wei
        start = time.time()
        self.position = {"amount": amount, "spent": spent, "start": start}
        reason = None
        while reason is None:
            value = await asyncio.to_thread(self.position_value, amount)
            pnl = (value / spent - 1) * 100
            left = self.timeout - (time.time() - start)
            log.info("Позиция: стоит %s ETH, PnL %+.2f%% (TP +%g%%, SL -%g%%, до таймаута %.0f с)",
                     eth(value), pnl, self.tp, self.sl, max(0, left))
            if pnl >= self.tp:
                reason = f"TP: прибыль {pnl:+.2f}%"
            elif pnl <= -self.sl:
                reason = f"SL: убыток {pnl:+.2f}%"
            elif left <= 0:
                reason = f"таймаут {self.timeout:g} с, PnL {pnl:+.2f}%"
            else:
                await asyncio.sleep(min(self.poll * 2, max(1, left)))
        received = await asyncio.to_thread(self.do_sell, amount, reason)
        self.position = None
        self.set_state(St.SOLD)
        gross = received - spent
        net = gross - self.gas_total
        summary = [f"ИТОГ: вложено {eth(spent)} ETH, получено {eth(received)} ETH",
                   f"PnL без газа: {eth(gross)} ETH ({gross / spent * 100:+.2f}%)",
                   f"Газ всего: {eth(self.gas_total)} ETH",
                   f"PnL с газом: {eth(net)} ETH ({net / spent * 100:+.2f}%)"]
        log.info("=" * 60)
        for line in summary:
            log.info(line)
        log.info("=" * 60)
        self.last_note = summary[-1]
        if self.notify:
            self.notify("📊 " + "\n".join(summary))

    async def trade(self, queue):
        while not self.done.is_set():
            ev = await queue.get()
            try:
                if ev["type"] == "PairCreated" and self.state == St.WAITING:
                    self.say(f"Событие PairCreated: пара {ev['pair']} (блок {ev['block']}) {EXPLORER}{ev['tx']}",
                             f"🆕 PairCreated: создан пул {ev['pair']} (блок {ev['block']})\n{EXPLORER}{ev['tx']}")
                    self.set_state(St.PAIR_FOUND, "— пул создан, ликвидности может ещё не быть")
                elif ev["type"] == "Mint" and self.state == St.PAIR_FOUND:
                    self.say(f"Событие Mint: добавлена ликвидность (amount0={ev['amount0']}, amount1={ev['amount1']}, блок {ev['block']})",
                             f"💧 Mint: в пул добавлена ликвидность (блок {ev['block']})")
                    self.set_state(St.LIQUIDITY)
                    got = await asyncio.to_thread(self.do_buy, ev["pair"])
                    if got:
                        await self.run_position(got)
                    self.done.set()
            except Exception as e:
                self.set_state(St.FAILED, f"— {e}")
                self.done.set()

    async def run(self):
        bal = self.w3.eth.get_balance(self.acct.address)
        log.info("Снайпер (кошелёк B): %s, баланс %s ETH", self.acct.address, eth(bal))
        if bal < self.buy_wei + 2 * 10 ** 15:
            raise RuntimeError("Мало ETH на кошельке B для покупки и газа.")
        self.loop = asyncio.get_running_loop()
        queue = asyncio.Queue()
        tasks = [asyncio.create_task(self.listen(queue)), asyncio.create_task(self.trade(queue))]
        await self.done.wait()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler("sniper.log", encoding="utf-8")])
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-block", type=int, help="разобрать события начиная с этого блока (повтор по истории)")
    args = ap.parse_args()
    try:
        asyncio.run(Sniper(args.from_block).run())
    except KeyboardInterrupt:
        log.info("Остановлено")
