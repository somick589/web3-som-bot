"""
New Pool Bot (Avalanche + Ethereum)
Следит за созданием новых пулов на DEX (события PairCreated / PoolCreated у фабрик),
присылает уведомления в Telegram, считает оценку обмена по формуле x*y=k
и сообщает, когда в пустой пул добавили ликвидность.
Сети: Avalanche C-Chain (всегда) и Ethereum (если задан ETH_RPC).
"""
import html
import asyncio
import json
import logging
import os
import re
import threading
import time
from pathlib import Path

import requests
from dotenv import load_dotenv
from web3 import Web3

import calc

load_dotenv()

# ---------- общие настройки ----------
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "5"))
CONFIRMATIONS = int(os.getenv("CONFIRMATIONS", "2"))   # не читаем самые свежие блоки (запас прочности)
MAX_RANGE = int(os.getenv("MAX_RANGE", "100"))         # окно eth_getLogs; при ошибке RPC подстраивается автоматически
CATCHUP_SLEEP = float(os.getenv("CATCHUP_SLEEP", "0.5"))
WATCH_CHECK_SECONDS = 15                               # как часто проверять пустые пулы
WATCH_TTL_SECONDS = 6 * 3600                           # сколько ждём ликвидность в пустом пуле
WATCH_MAX = 100
HISTORY_HOURS = float(os.getenv("HISTORY_HOURS", "24"))  # как далеко назад искать последний пул после /start
STATE_FILE = Path(os.getenv("STATE_FILE", "state.json"))
POOLS_LOG = Path(os.getenv("POOLS_LOG", "pools.jsonl"))

# ---------- сети ----------
# Адреса фабрик при запуске проверяются автоматически (verify_factories): неподходящие отключаются.
CHAIN_CONFIGS = [
    {
        "key": "avalanche", "label": "Avalanche", "chain_id": 43114, "block_time": 2.0,
        "rpc_env": "AVAX_RPC", "rpc_default": "https://api.avax.network/ext/bc/C/rpc",
        "start_env": "START_BLOCK",
        "tx_url": "https://snowtrace.io/tx/", "dex_slug": "avalanche",
        "dexes": [
            {"name": "Trader Joe V1", "factory": "0x9Ad6C38BE94206cA50bb0d90783181662f0Cfa10", "kind": "v2"},
            {"name": "Pangolin",      "factory": "0xefa94DE7a4656D787667C749f7E1223D71E9FD88", "kind": "v2"},
            {"name": "Uniswap V3",    "factory": "0x740b1c1de25031C31FF4fC9A62f554A55cdC1baD", "kind": "v3"},
        ],
        "filter_tokens": None,            # присылаем все пулы
        "require_liquidity": False,
    },
    {
        "key": "ethereum", "label": "Ethereum", "chain_id": 1, "block_time": 12.0,
        "rpc_env": "ETH_RPC", "rpc_default": None,                    # без ETH_RPC сеть выключена
        "start_env": "START_BLOCK_ETH",
        "tx_url": "https://etherscan.io/tx/", "dex_slug": "ethereum",
        "dexes": [
            {"name": "Uniswap V2",   "factory": "0x5C69bEe701ef814a2B6a3EDD4B1652CB9cc5aA6f", "kind": "v2"},
            {"name": "SushiSwap V2", "factory": "0xC0AEe478e3658e2610c5F7A4A2E1777cE9e4f2Ac", "kind": "v2"},
            {"name": "Uniswap V3",   "factory": "0x1F98431c8aD98523631AE4a59f267346ea31F984", "kind": "v3"},
        ],
        # фильтр: только пулы, где есть WETH, USDC или USDT, и только с ликвидностью
        "filter_tokens": ["0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",   # WETH
                          "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",   # USDC
                          "0xdAC17F958D2ee523a2206206994597C13D831ec7"],  # USDT
        "require_liquidity": True,
    },
]

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("poolbot")


def mask(text):
    """Прячем ключ из адреса RPC (…/v2/КЛЮЧ), чтобы он не попал в логи и сообщения."""
    text = re.sub(r"(/v2/)[^\s\"'<>)]+", r"\1***", str(text))        # ключ Alchemy
    return re.sub(r"(/bot)\d+:[\w-]+", r"\1***", text)               # токен Telegram


class _MaskFilter(logging.Filter):
    def filter(self, record):
        record.msg = mask(record.getMessage())
        record.args = ()
        return True


for _h in logging.getLogger().handlers:
    _h.addFilter(_MaskFilter())


def _sel(sig):
    return "0x" + bytes(Web3.keccak(text=sig))[:4].hex()


# topic0 событий и селекторы функций считаем из сигнатур
TOPIC_V2 = "0x" + bytes(Web3.keccak(text="PairCreated(address,address,address,uint256)")).hex()
TOPIC_V3 = "0x" + bytes(Web3.keccak(text="PoolCreated(address,address,uint24,int24,address)")).hex()
SEL_SYMBOL = _sel("symbol()")
SEL_DECIMALS = _sel("decimals()")
SEL_RESERVES = _sel("getReserves()")
SEL_V3_LIQUIDITY = _sel("liquidity()")
SEL_ALL_PAIRS = _sel("allPairsLength()")
SEL_TICK_SPACING = _sel("feeAmountTickSpacing(uint24)")
SEL_ALL_PAIRS_AT = _sel("allPairs(uint256)")
SEL_TOKEN0 = _sel("token0()")
SEL_TOKEN1 = _sel("token1()")
SEL_GET_POOL = _sel("getPool(address,address,uint24)")


def decode_string(raw):
    if len(raw) == 32:                       # старые токены возвращают bytes32
        return raw.rstrip(b"\x00").decode("utf-8", "ignore")
    if len(raw) >= 96:
        offset = int.from_bytes(raw[0:32], "big")
        length = int.from_bytes(raw[offset:offset + 32], "big")
        return raw[offset + 32: offset + 32 + length].decode("utf-8", "ignore")
    return "?"


# ---------- математика AMM (Speedrun Ethereum, Checkpoint 3: price()) ----------
def amm_out(x_in, x_res, y_res):
    """Constant product x*y=k с комиссией 0,3% (997/1000)."""
    x_in_fee = x_in * 997
    return x_in_fee * y_res // (x_res * 1000 + x_in_fee)


def estimate_text(r0, r1, d0, d1, s0, s1):
    if not r0 or not r1:
        return ""
    one = 10 ** d0
    out = amm_out(one, r0, r1)
    spot = (r1 / 10 ** d1) / (r0 / 10 ** d0)            # цена token0 в token1 по резервам
    out_h = out / 10 ** d1
    ideal = spot * 0.997                                # без влияния на цену, только комиссия
    impact = max(0.0, (1 - out_h / ideal) * 100) if ideal > 0 else 0.0
    return (f"Цена по резервам: 1 {s0} ≈ {calc.nfmt(spot)} {s1}\n"
            f"Оценка обмена 1 {s0}: ≈ {calc.nfmt(out_h)} {s1} (комиссия 0,3%, влияние на цену {impact:.2f}%)")


# ---------- сеть ----------
class Chain:
    def __init__(self, cfg, rpc):
        self.cfg = cfg
        self.key = cfg["key"]
        self.label = cfg["label"]
        self.w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 20}))
        self.factories = {Web3.to_checksum_address(d["factory"]): dict(d) for d in cfg["dexes"]}
        self.filter = ({Web3.to_checksum_address(a) for a in cfg["filter_tokens"]}
                       if cfg["filter_tokens"] else None)
        self.cur_range = MAX_RANGE
        self.token_cache = {}
        self.fail = 0
        self.next_try = 0.0
        self.last_progress = 0.0
        self.last_watch = 0.0

    # --- чтение из сети ---
    def eth_call(self, to, data):
        return bytes(self.w3.eth.call({"to": to, "data": data}))

    def token_info(self, addr):
        """(symbol, decimals) с кэшем."""
        if addr in self.token_cache:
            return self.token_cache[addr]
        try:
            symbol = decode_string(self.eth_call(addr, SEL_SYMBOL)) or "?"
        except Exception:
            symbol = "?"
        try:
            decimals = int.from_bytes(self.eth_call(addr, SEL_DECIMALS)[:32], "big")
            if decimals > 77:
                decimals = 18
        except Exception:
            decimals = 18
        self.token_cache[addr] = (symbol, decimals)
        return self.token_cache[addr]

    def pool_state(self, ev):
        """{'has': bool, 'r0': int, 'r1': int}; None — если не удалось прочитать."""
        try:
            if ev["kind"] == "v2":
                raw = self.eth_call(ev["pool"], SEL_RESERVES)
                r0 = int.from_bytes(raw[0:32], "big")
                r1 = int.from_bytes(raw[32:64], "big")
                return {"has": r0 > 0 and r1 > 0, "r0": r0, "r1": r1}
            liq = int.from_bytes(self.eth_call(ev["pool"], SEL_V3_LIQUIDITY)[:32], "big")
            return {"has": liq > 0, "r0": None, "r1": None}
        except Exception:
            return None

    # --- разбор события ---
    def decode_log(self, lg):
        dex = self.factories[Web3.to_checksum_address(lg["address"])]
        topics = lg["topics"]
        data = bytes(lg["data"])
        token0 = Web3.to_checksum_address(bytes(topics[1])[-20:])
        token1 = Web3.to_checksum_address(bytes(topics[2])[-20:])
        fee = None
        index = None
        if dex["kind"] == "v2":
            pool = Web3.to_checksum_address(data[12:32])        # pair — первое слово data
            if len(data) >= 64:
                index = int.from_bytes(data[32:64], "big")      # номер пары в фабрике
        else:
            fee = int.from_bytes(bytes(topics[3]), "big")       # fee — третий indexed
            pool = Web3.to_checksum_address(data[-20:])         # pool — последнее слово data
        return {"dex": dex["name"], "kind": dex["kind"], "token0": token0, "token1": token1,
                "pool": pool, "fee": fee, "pair_index": index,
                "block": lg["blockNumber"], "tx": bytes(lg["transactionHash"]).hex(),
                "log_index": lg["logIndex"]}

    def allowed(self, ev):
        if self.filter is None:
            return True
        return ev["token0"] in self.filter or ev["token1"] in self.filter

    # --- проверки при старте ---
    def verify_factories(self):
        for addr, d in list(self.factories.items()):
            ok = False
            try:
                if len(self.w3.eth.get_code(addr)) == 0:
                    raise RuntimeError("по адресу нет контракта")
                if d["kind"] == "v2":
                    n = int.from_bytes(self.eth_call(addr, SEL_ALL_PAIRS)[:32], "big")
                    log.info("%s / %s: фабрика ок, пар создано: %s", self.label, d["name"], n)
                else:
                    tick = int.from_bytes(self.eth_call(addr, SEL_TICK_SPACING + (3000).to_bytes(32, "big").hex())[:32], "big")
                    if tick != 60:                               # для fee 0,3% шаг тика = 60
                        raise RuntimeError(f"неожиданный tickSpacing={tick}")
                    log.info("%s / %s: фабрика ок", self.label, d["name"])
                ok = True
            except Exception as e:
                log.warning("%s / %s (%s): проверка НЕ пройдена (%s), отключаю. Сверь адрес с документацией DEX.",
                            self.label, d["name"], addr, e)
            if not ok:
                del self.factories[addr]
        if not self.factories:
            raise RuntimeError("ни одна фабрика не прошла проверку")


# ---------- сообщения ----------
def liquidity_block(chain, ev, stt):
    s0, d0 = chain.token_info(ev["token0"])
    s1, d1 = chain.token_info(ev["token1"])
    if stt is None:
        return "Ликвидность: не удалось проверить"
    if not stt["has"]:
        return "Ликвидность: пока нет (пул пустой)"
    if ev["kind"] == "v2":
        a0, a1 = stt["r0"] / 10 ** d0, stt["r1"] / 10 ** d1
        head = f"Ликвидность: есть\nРезервы: {calc.nfmt(a0)} {html.escape(s0)} / {calc.nfmt(a1)} {html.escape(s1)}"
        est = estimate_text(stt["r0"], stt["r1"], d0, d1, html.escape(s0), html.escape(s1))
        return head + ("\n" + est if est else "")
    return "Ликвидность: есть (V3, liquidity() > 0)"


def format_new_pool(chain, ev, stt, title="🆕 <b>Новый пул"):
    s0, _ = chain.token_info(ev["token0"])
    s1, _ = chain.token_info(ev["token1"])
    e = html.escape
    fee_line = f"\nКомиссия пула: {ev['fee'] / 10000:.2f}%" if ev["fee"] is not None else ""
    idx = f" · пара №{ev['pair_index']}" if ev.get("pair_index") is not None else ""
    block_line = f"Блок: {ev['block']}\n" if ev.get("block") is not None else ""
    links = f'<a href="https://dexscreener.com/{chain.cfg["dex_slug"]}/{ev["pool"]}">Dexscreener</a>'
    if ev.get("tx"):
        links = f'<a href="{chain.cfg["tx_url"]}0x{ev["tx"]}">транзакция</a> · ' + links
    return (
        f"{title} · {e(ev['dex'])} ({e(chain.label)})</b>{idx}\n"
        f"Пара: <b>{e(s0)}/{e(s1)}</b>{fee_line}\n"
        f"{liquidity_block(chain, ev, stt)}\n\n"
        f"token0: <code>{ev['token0']}</code>\n"
        f"token1: <code>{ev['token1']}</code>\n"
        f"Пул: <code>{ev['pool']}</code>\n"
        f"{block_line}{links}\n"
        f"Оценка покупки: <code>/calc {ev['pool']} 1</code>"
    )


def format_liquidity_added(chain, ev, stt):
    s0, _ = chain.token_info(ev["token0"])
    s1, _ = chain.token_info(ev["token1"])
    return (
        f"💧 <b>В пул добавили ликвидность · {html.escape(ev['dex'])} ({html.escape(chain.label)})</b>\n"
        f"Пара: <b>{html.escape(s0)}/{html.escape(s1)}</b>\n"
        f"{liquidity_block(chain, ev, stt)}\n\n"
        f"Пул: <code>{ev['pool']}</code>\n"
        f'<a href="https://dexscreener.com/{chain.cfg["dex_slug"]}/{ev["pool"]}">Dexscreener</a>'
    )


# ---------- состояние ----------
def load_state():
    base = {"subscribers": [], "tg_offset": 0, "chains": {}}
    if STATE_FILE.exists():
        base.update(json.loads(STATE_FILE.read_text()))
    if "last_block" in base and "avalanche" not in base["chains"]:      # перенос из старой версии
        base["chains"]["avalanche"] = {"last_block": base.get("last_block"),
                                       "seen": base.get("seen", []), "watch": base.get("watch", [])}
    for k in ("last_block", "seen", "watch", "recent"):
        base.pop(k, None)
    return base


def save_state(st):
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=2))
    tmp.replace(STATE_FILE)


def chain_state(st, chain):
    return st["chains"].setdefault(chain.key, {"last_block": None, "seen": [], "watch": []})


# ---------- Telegram ----------
def tg(method, **params):
    r = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/{method}", json=params, timeout=35)
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram {method}: {data}")
    return data["result"]


def send(chat_id, text):
    try:
        tg("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML", disable_web_page_preview=True)
    except Exception as e:
        log.warning("не удалось отправить в %s: %s", chat_id, e)


def broadcast(st, text):
    for chat in st["subscribers"]:
        send(chat, text)


CHAINS = []


def welcome_text():
    nets = ", ".join(c.label for c in CHAINS)
    lines = [
        "Готово, ты подписан. Дальше всё приходит само:",
        "🆕 новые пулы на DEX",
        "💧 сообщение, когда в пустой пул добавят ликвидность",
        f"Сети: {nets}.",
    ]
    for c in CHAINS:
        if c.filter is not None:
            lines.append(f"{c.label}: только пулы с WETH/USDC/USDT и с ликвидностью (иначе слишком много мусора).")
    lines += ["", "Сейчас пришлю последний пул каждой сети (поиск может занять до нескольких минут), "
                  "дальше будут приходить только новые.",
              "/calc — оценить покупку в пуле",
              "/stop — отписаться"]
    return "\n".join(lines)


def history_events(chain, upto_block, want=1):
    """Ищет назад от upto_block самые свежие подходящие пулы (не дальше HISTORY_HOURS часов).
    Возвращает [(событие, состояние пула)] от самых новых."""
    blocks = int(HISTORY_HOURS * 3600 / chain.cfg["block_time"])
    lowest = max(0, upto_block - blocks)
    end = upto_block
    found = []
    while end > lowest and len(found) < want:
        start = max(lowest, end - chain.cur_range + 1)
        logs = None
        shrink = False
        for _ in range(4):
            try:
                logs = chain.w3.eth.get_logs({"fromBlock": start, "toBlock": end,
                                              "address": list(chain.factories.keys()),
                                              "topics": [[TOPIC_V2, TOPIC_V3]]})
                break
            except Exception as e:
                msg = str(e)
                m = re.search(r"maximum of (\d+) blocks", msg)
                if m and int(m.group(1)) < chain.cur_range:
                    chain.cur_range = max(1, int(m.group(1)))
                    shrink = True
                    break
                if ("400" in msg or "Bad Request" in msg) and chain.cur_range > 10:
                    chain.cur_range = 10
                    shrink = True
                    break
                time.sleep(15 if "429" in msg else 3)
        if shrink:
            continue                                    # пересчитываем окно с новым размером
        if logs is None:
            raise RuntimeError("RPC не отвечает (лимит запросов)")
        for lg in sorted(logs, key=lambda x: (x["blockNumber"], x["logIndex"]), reverse=True):
            ev = chain.decode_log(lg)
            if not chain.allowed(ev):
                continue
            stt = chain.pool_state(ev)
            if chain.cfg["require_liquidity"] and not (stt and stt["has"]):
                continue
            found.append((ev, stt))
            if len(found) >= want:
                break
        end = start - 1
        time.sleep(CATCHUP_SLEEP)
    return found


HISTORY_BUSY = set()


def send_history(st, chat_id):
    """Фоновая задача после /start: присылает последний пул каждой сети."""
    try:
        for chain in CHAINS:
            try:
                upto = (st["chains"].get(chain.key) or {}).get("last_block") or (chain.w3.eth.block_number - CONFIRMATIONS)
                res = history_events(chain, upto)
                if not res:
                    send(chat_id, f"{chain.label}: за последние {HISTORY_HOURS:g} ч подходящих пулов не нашлось. Новые придут сами.")
                    continue
                ev, stt = res[0]
                minutes = max(0, int((upto - ev["block"]) * chain.cfg["block_time"] / 60))
                age = f"{minutes} мин назад" if minutes < 120 else f"{minutes // 60} ч назад"
                send(chat_id, format_new_pool(chain, ev, stt, title=f"🕘 <b>Последний пул ({age})"))
            except Exception as e:
                log.warning("история %s: %s", chain.label, mask(e))
                send(chat_id, f"{chain.label}: не удалось найти последний пул ({html.escape(mask(e))}). Новые придут сами.")
    finally:
        HISTORY_BUSY.discard(chat_id)


# ---------- Снайпер (Sepolia): управление только для владельца ----------
OWNER_ID = int(os.getenv("OWNER_CHAT_ID", "0") or 0)
SNIPER = {"obj": None}


def snipe_notify(text):
    send(OWNER_ID, html.escape(text))


def run_sniper(from_block):
    try:
        import sniper                                     # sniper.py лежит рядом с bot.py
        s = sniper.Sniper(from_block, notify=snipe_notify)
        SNIPER["obj"] = s
        asyncio.run(s.run())
        snipe_notify("🏁 Снайпер завершил работу.")
    except Exception as e:
        send(OWNER_ID, "Снайпер: ошибка — " + html.escape(mask(e)))
    finally:
        SNIPER["obj"] = None


def handle_snipe(chat_id, text):
    if not OWNER_ID or chat_id != OWNER_ID:
        send(chat_id, "Эта команда доступна только владельцу бота.")
        return
    cmd = text.split()[0]
    s = SNIPER["obj"]
    if cmd == "/snipe_status":
        send(chat_id, html.escape(s.status_text()) if s else "Снайпер не запущен. Запуск: /snipe")
    elif cmd == "/snipe_stop":
        if not s:
            send(chat_id, "Снайпер не запущен.")
        elif s.position:
            send(chat_id, "Позиция открыта, дождись продажи (TP/SL/таймаут): остановка сейчас оставила бы токены непроданными.")
        else:
            s.stop()
            send(chat_id, "Останавливаю снайпера.")
    else:                                                 # /snipe [номер_блока]
        if s:
            send(chat_id, "Снайпер уже запущен. Статус: /snipe_status")
            return
        parts = text.split()
        fb = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
        send(chat_id, "Запускаю снайпера" + (f" (с блока {fb})" if fb else " (ждёт новый пул)") + ".")
        threading.Thread(target=run_sniper, args=(fb,), daemon=True).start()


CALC_HELP = ("Калькулятор покупки: /calc &lt;адрес пула&gt; &lt;сумма&gt; [0|1] [проскальзывание %]\n"
             "Пример: <code>/calc 0xПУЛ 0.5</code> — платишь 0,5 базового токена (WETH/USDC/…).\n"
             "0 или 1 — каким токеном платишь (token0/token1), по умолчанию базовым.")


def run_calc(chat_id, text):
    parts = text.split()[1:]
    try:
        if len(parts) < 2:
            send(chat_id, CALC_HELP)
            return
        pool, amount = parts[0], float(parts[1].replace(",", "."))
        pay = int(parts[2]) if len(parts) > 2 and parts[2] in ("0", "1") else None
        slip = float(parts[3].replace(",", ".")) if len(parts) > 3 else 1.0
        last_err = None
        for ch in CHAINS:
            try:
                send(chat_id, html.escape(calc.quote(ch.w3, pool, amount, pay, slip, label=ch.label)))
                return
            except Exception as e:
                last_err = e
        send(chat_id, f"Не получилось: {html.escape(mask(last_err))}")
    except Exception as e:
        send(chat_id, f"Не получилось: {html.escape(mask(e))}\n\n{CALC_HELP}")


def handle_commands(st):
    updates = tg("getUpdates", offset=st["tg_offset"], timeout=0)
    for u in updates:
        st["tg_offset"] = u["update_id"] + 1
        msg = u.get("message") or {}
        text = (msg.get("text") or "").strip().lower()
        chat_id = (msg.get("chat") or {}).get("id")
        if chat_id is None:
            continue
        log.info("команда %r от чата %s", text[:20], chat_id)
        if text.startswith("/start"):
            new = chat_id not in st["subscribers"]
            if new:
                st["subscribers"].append(chat_id)
            save_state(st)
            send(chat_id, welcome_text())
            log.info("подписчик %s, новый: %s", chat_id, new)
            if chat_id not in HISTORY_BUSY:                       # последний пул каждой сети — при каждом /start
                HISTORY_BUSY.add(chat_id)
                threading.Thread(target=send_history, args=(st, chat_id), daemon=True).start()
        elif text.startswith("/snipe"):
            handle_snipe(chat_id, text)
        elif text.startswith("/calc"):
            threading.Thread(target=run_calc, args=(chat_id, text), daemon=True).start()
        elif text.startswith("/stop"):
            if chat_id in st["subscribers"]:
                st["subscribers"].remove(chat_id)
            send(chat_id, "Подписка отключена. Чтобы включить снова, напиши /start.")
        else:
            send(chat_id, "Я присылаю новые пулы сам. Команды: /start — подписаться, /calc — оценить покупку, /stop — отписаться.")
    if updates:
        save_state(st)


# ---------- наблюдение за пустыми пулами ----------
def check_watch(st, chain):
    c = chain_state(st, chain)
    now = time.time()
    keep = []
    for item in c["watch"]:
        if now - item["created_ts"] > WATCH_TTL_SECONDS:
            continue
        stt = chain.pool_state(item["ev"])
        if stt and stt["has"]:
            broadcast(st, format_liquidity_added(chain, item["ev"], stt))
            log.info("%s: в пул %s добавлена ликвидность", chain.label, item["ev"]["pool"])
        else:
            keep.append(item)
    if len(keep) != len(c["watch"]):
        c["watch"] = keep
        save_state(st)


# ---------- сканирование ----------
def scan(st, chain):
    """True, если нужно сразу продолжать (мы отстаём от головы сети)."""
    c = chain_state(st, chain)
    latest = chain.w3.eth.block_number - CONFIRMATIONS
    if c["last_block"] is None:
        sb = os.getenv(chain.cfg["start_env"])
        c["last_block"] = int(sb) if sb else latest
        save_state(st)
        log.info("%s: старт с блока %s", chain.label, c["last_block"])
        return False
    start = c["last_block"] + 1
    if start > latest:
        return False
    end = min(latest, start + chain.cur_range - 1)
    try:
        logs = chain.w3.eth.get_logs({
            "fromBlock": start, "toBlock": end,
            "address": list(chain.factories.keys()),
            "topics": [[TOPIC_V2, TOPIC_V3]],
        })
    except Exception as e:
        msg = str(e)
        m = re.search(r"maximum of (\d+) blocks", msg)             # RPC сам называет допустимое окно
        if m and int(m.group(1)) < chain.cur_range:
            chain.cur_range = max(1, int(m.group(1)))
            log.warning("%s: RPC разрешает окно максимум %s блоков, подстраиваюсь", chain.label, chain.cur_range)
            return True
        if ("400" in msg or "Bad Request" in msg) and chain.cur_range > 10:   # лимит окна у провайдера (бесплатный тариф)
            chain.cur_range = 10
            log.warning("%s: RPC отклонил запрос (400), ставлю окно 10 блоков", chain.label)
            return True
        if "range" in msg.lower() and chain.cur_range > 10:
            chain.cur_range = max(10, chain.cur_range // 2)
            log.warning("%s: get_logs не прошёл, уменьшаю окно до %s блоков", chain.label, chain.cur_range)
            return True
        raise
    for lg in sorted(logs, key=lambda x: (x["blockNumber"], x["logIndex"])):
        ev = chain.decode_log(lg)
        key = f"{ev['tx']}:{ev['log_index']}"
        if key in c["seen"]:
            continue                                              # защита от дублей
        c["seen"] = (c["seen"] + [key])[-2000:]
        allowed = chain.allowed(ev)
        with POOLS_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(dict(ev, chain=chain.key, filtered_out=not allowed), ensure_ascii=False) + "\n")
        if not allowed:
            continue                                              # не проходит фильтр токенов
        stt = chain.pool_state(ev)
        empty = stt is not None and not stt["has"]
        if empty:
            c["watch"] = (c["watch"] + [{"ev": ev, "created_ts": time.time()}])[-WATCH_MAX:]
            if chain.cfg["require_liquidity"]:
                continue                                          # сообщим позже, когда появится ликвидность
        broadcast(st, format_new_pool(chain, ev, stt))
        log.info("%s: новый пул %s %s", chain.label, ev["dex"], ev["pool"])
    c["last_block"] = end                                         # checkpoint
    save_state(st)
    if time.time() - chain.last_progress > 60:
        chain.last_progress = time.time()
        log.info("%s: проверено до блока %s, до головы сети осталось %s блоков",
                 chain.label, end, max(0, latest - end))
    return end < latest


# ---------- запуск ----------
def build_chains():
    chains = []
    for cfg in CHAIN_CONFIGS:
        rpc = os.getenv(cfg["rpc_env"]) or cfg["rpc_default"]
        if not rpc:
            log.info("%s выключена: не задан %s", cfg["label"], cfg["rpc_env"])
            continue
        ch = Chain(cfg, rpc)
        try:
            if not ch.w3.is_connected():
                raise RuntimeError("нет подключения к RPC")
            cid = ch.w3.eth.chain_id
            if cid != cfg["chain_id"]:
                raise RuntimeError(f"chain_id={cid}, ожидался {cfg['chain_id']}; проверь {cfg['rpc_env']}")
            log.info("подключено: %s, chain_id=%s, блок %s", ch.label, cid, ch.w3.eth.block_number)
            ch.verify_factories()
            chains.append(ch)
        except Exception as e:
            log.error("%s отключена: %s", cfg["label"], mask(e))
    if not chains:
        raise SystemExit("Нет доступных сетей. Проверь AVAX_RPC / ETH_RPC.")
    return chains


def main():
    global CHAINS
    CHAINS = build_chains()
    st = load_state()
    if os.getenv("RESUME_MISSED", "0") != "1":
        # Бот был выключен: пропущенные блоки не разбираем (последний пул придёт по /start, новые — дальше сами)
        for ch in CHAINS:
            try:
                chain_state(st, ch)["last_block"] = ch.w3.eth.block_number - CONFIRMATIONS
            except Exception as e:
                log.warning("%s: не удалось взять текущий блок (%s)", ch.label, mask(e))
        save_state(st)
        log.info("Старт с текущих блоков (пропущенное за время остановки не разбираю; RESUME_MISSED=1 включит разбор)")
    tg_fail, tg_next = 0, 0.0
    while True:
        behind = False
        if time.time() >= tg_next:
            try:
                handle_commands(st)
                tg_fail = 0
            except Exception as e:
                tg_fail += 1
                tg_next = time.time() + min(2 ** tg_fail, 60)
                log.error("Telegram: %s (повтор через %s с)", mask(e), min(2 ** tg_fail, 60))
        for ch in CHAINS:
            if time.time() < ch.next_try:
                continue
            try:
                if time.time() - ch.last_watch >= WATCH_CHECK_SECONDS:
                    check_watch(st, ch)
                    ch.last_watch = time.time()
                if scan(st, ch):
                    behind = True
                ch.fail = 0
            except Exception as e:
                ch.fail += 1
                delay = min(2 ** ch.fail, 60)
                if "429" in str(e):                               # лимит запросов RPC
                    delay = max(delay, 15)
                ch.next_try = time.time() + delay
                log.error("%s: %s (повтор через %s с)", ch.label, mask(e), delay)
        time.sleep(CATCHUP_SLEEP if behind else POLL_SECONDS)


if __name__ == "__main__":
    main()
