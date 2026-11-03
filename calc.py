"""
Калькулятор «Оцени покупку».
Читает состояние пула (V2: резервы getReserves(); V3: liquidity() и slot0().sqrtPriceX96)
и оценивает: сколько токенов получишь, комиссию, цену по резервам и по факту (price impact),
минимум на выходе при проскальзывании (amountOutMin), долю покупки в пуле.

Запуск отдельно:
    python calc.py <адрес_пула> <сумма> [--chain ethereum|avalanche] [--pay 0|1] [--slippage 1]
Сумма — в токенах (не в wei), например 0.5. --pay: каким токеном платишь (0 = token0, 1 = token1).
По умолчанию платишь "базовым" токеном (WETH/USDC/USDT/WAVAX), если он есть в паре.
"""
import argparse
import math
import os
import sys

from web3 import Web3

Q96 = 1 << 96


def _sel(sig):
    return "0x" + bytes(Web3.keccak(text=sig))[:4].hex()


SEL_SYMBOL = _sel("symbol()")
SEL_DECIMALS = _sel("decimals()")
SEL_RESERVES = _sel("getReserves()")
SEL_TOKEN0 = _sel("token0()")
SEL_TOKEN1 = _sel("token1()")
SEL_LIQUIDITY = _sel("liquidity()")
SEL_SLOT0 = _sel("slot0()")
SEL_FEE = _sel("fee()")

# токены, которыми обычно «платят» за новый токен
BASE_TOKENS = {a.lower() for a in [
    "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",   # WETH (Ethereum)
    "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",   # USDC (Ethereum)
    "0xdAC17F958D2ee523a2206206994597C13D831ec7",   # USDT (Ethereum)
    "0xB31f66AA3C1e785363F0875A1B74E27b85FD66c7",   # WAVAX
    "0xB97EF9Ef8734C71904D8002F8b6Bc66Dd9c48a6E",   # USDC (Avalanche)
    "0x9702230A8Ea53601f5cD2dc00fDBc13d4dF4A8c7",   # USDt (Avalanche)
]}


# ---------- чистая математика (без сети) ----------
def v2_swap(amount_in, r_in, r_out, fee_bps=30):
    """Constant product x*y=k с комиссией (по умолчанию 0,3% = 997/1000)."""
    eff = amount_in * (10000 - fee_bps)
    return eff * r_out // (r_in * 10000 + eff)


def v3_swap(amount_in, zero_for_one, liquidity, sqrt_p, fee_ppm):
    """Обмен внутри текущего ценового диапазона V3 (без пересечения тиков).
    Возвращает (получено, новый sqrtPriceX96)."""
    eff = amount_in * (1_000_000 - fee_ppm) // 1_000_000
    if zero_for_one:                                   # платим token0, получаем token1
        new = (liquidity << 96) * sqrt_p // ((liquidity << 96) + eff * sqrt_p)
        out = liquidity * (sqrt_p - new) // Q96
    else:                                              # платим token1, получаем token0
        new = sqrt_p + (eff << 96) // liquidity
        out = (liquidity << 96) * (new - sqrt_p) // (new * sqrt_p)
    return out, new


# ---------- чтение из сети ----------
def _call(w3, to, data):
    return bytes(w3.eth.call({"to": to, "data": data}))


def _token(w3, addr):
    try:
        raw = _call(w3, addr, SEL_SYMBOL)
        if len(raw) == 32:
            sym = raw.rstrip(b"\x00").decode("utf-8", "ignore")
        else:
            off = int.from_bytes(raw[0:32], "big")
            ln = int.from_bytes(raw[off:off + 32], "big")
            sym = raw[off + 32: off + 32 + ln].decode("utf-8", "ignore")
    except Exception:
        sym = "?"
    try:
        dec = int.from_bytes(_call(w3, addr, SEL_DECIMALS)[:32], "big")
        if dec > 77:
            dec = 18
    except Exception:
        dec = 18
    return sym or "?", dec


def _addr(raw):
    return Web3.to_checksum_address(raw[12:32])


def nfmt(x):
    """Число без научной записи: 4227460 -> '4,227,460', 0.00601 -> '0.00601'."""
    if x == 0:
        return "0"
    a = abs(x)
    if a >= 100000:
        return f"{x:,.0f}"
    if a >= 1:
        s = f"{x:,.2f}"
    else:
        s = f"{x:.{min(18, max(0, -int(math.floor(math.log10(a)))) + 3)}f}"
    return s.rstrip("0").rstrip(".") if "." in s else s


_num = nfmt


def quote(w3, pool, amount, pay=None, slippage=1.0, label=""):
    """Возвращает готовый текст расчёта. Бросает ValueError, если по адресу не пул V2/V3."""
    pool = Web3.to_checksum_address(pool)
    if len(w3.eth.get_code(pool)) == 0:
        raise ValueError("по этому адресу нет контракта")
    try:
        t0 = _addr(_call(w3, pool, SEL_TOKEN0))
        t1 = _addr(_call(w3, pool, SEL_TOKEN1))
    except Exception:
        raise ValueError("это не пул Uniswap V2/V3 (нет token0/token1)")
    s0, d0 = _token(w3, t0)
    s1, d1 = _token(w3, t1)

    if pay is None:                                    # платим базовым токеном, если он есть
        pay = 1 if (t1.lower() in BASE_TOKENS and t0.lower() not in BASE_TOKENS) else 0
    s_in, d_in, s_out, d_out = (s0, d0, s1, d1) if pay == 0 else (s1, d1, s0, d0)
    amount_in = int(round(amount * 10 ** d_in))
    if amount_in <= 0:
        raise ValueError("сумма слишком мала")

    kind = None
    try:
        raw = _call(w3, pool, SEL_RESERVES)
        r0, r1 = int.from_bytes(raw[0:32], "big"), int.from_bytes(raw[32:64], "big")
        kind = "v2"
    except Exception:
        pass

    head = f"🧮 Оценка покупки · {s0}/{s1}" + (f" ({label})" if label else "")
    if kind == "v2":
        if r0 == 0 or r1 == 0:
            return head + "\nПул V2 пустой: резервов нет, считать нечего."
        r_in, r_out = (r0, r1) if pay == 0 else (r1, r0)
        out = v2_swap(amount_in, r_in, r_out)
        spot = (r_out / 10 ** d_out) / (r_in / 10 ** d_in)             # цена по резервам
        out_h = out / 10 ** d_out
        exec_price = out_h / (amount_in / 10 ** d_in)
        ideal = spot * 0.997
        impact = max(0.0, (1 - exec_price / ideal) * 100) if ideal else 0.0
        fee_h = amount_in * 0.003 / 10 ** d_in
        share = amount_in / r_in * 100
        reserves = f"Резервы: {_num(r0 / 10 ** d0)} {s0} / {_num(r1 / 10 ** d1)} {s1}"
        note = ""
        kind_text = "V2 (x·y=k, комиссия 0,3%)"
    else:
        try:
            liq = int.from_bytes(_call(w3, pool, SEL_LIQUIDITY)[:32], "big")
            slot = _call(w3, pool, SEL_SLOT0)
            sqrt_p = int.from_bytes(slot[0:32], "big")
            fee_ppm = int.from_bytes(_call(w3, pool, SEL_FEE)[:32], "big")
        except Exception:
            raise ValueError("не удалось прочитать состояние пула (ни V2, ни V3)")
        if liq == 0 or sqrt_p == 0:
            return head + "\nПул V3 без активной ликвидности: считать нечего."
        out, new_sqrt = v3_swap(amount_in, pay == 0, liq, sqrt_p, fee_ppm)
        p01 = (sqrt_p / Q96) ** 2 * 10 ** (d0 - d1)                    # token1 за 1 token0
        spot = p01 if pay == 0 else 1 / p01
        out_h = out / 10 ** d_out
        exec_price = out_h / (amount_in / 10 ** d_in)
        ideal = spot * (1 - fee_ppm / 1_000_000)
        impact = max(0.0, (1 - exec_price / ideal) * 100) if ideal else 0.0
        fee_h = amount_in * fee_ppm / 1_000_000 / 10 ** d_in
        share = 0.0
        reserves = f"Ликвидность L = {liq:,} (текущий диапазон цен)"
        note = ("\n⚠️ V3: расчёт верен, пока цена остаётся в текущем диапазоне ликвидности. "
                "При большом влиянии на цену результат может быть хуже.") if impact > 2 else ""
        kind_text = f"V3 (концентрированная ликвидность, комиссия {fee_ppm / 10000:.2f}%)"

    min_out = out_h * (1 - slippage / 100)
    lines = [
        head,
        f"Тип: {kind_text}",
        f"Платишь: {_num(amount_in / 10 ** d_in)} {s_in}",
        f"Получишь ≈ {_num(out_h)} {s_out}",
        f"Цена по резервам: 1 {s_in} = {_num(spot)} {s_out}",
        f"Цена по факту: 1 {s_in} = {_num(exec_price)} {s_out}",
        f"Влияние на цену (price impact): {impact:.2f}%",
        f"Комиссия: ≈ {_num(fee_h)} {s_in}",
        f"amountOutMin при проскальзывании {slippage:g}%: {_num(min_out)} {s_out}",
    ]
    if kind == "v2":
        lines.append(f"Твоя покупка = {share:.3f}% резерва {s_in} в пуле")
    lines.append(reserves)
    return "\n".join(lines) + note


# ---------- запуск из терминала ----------
CHAIN_RPC = {"ethereum": ("ETH_RPC", None), "avalanche": ("AVAX_RPC", "https://api.avax.network/ext/bc/C/rpc")}


def main():
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    ap = argparse.ArgumentParser(description="Калькулятор «Оцени покупку»")
    ap.add_argument("pool")
    ap.add_argument("amount", type=float)
    ap.add_argument("--chain", choices=list(CHAIN_RPC))
    ap.add_argument("--pay", type=int, choices=[0, 1])
    ap.add_argument("--slippage", type=float, default=1.0)
    a = ap.parse_args()
    chains = [a.chain] if a.chain else list(CHAIN_RPC)
    err = None
    for name in chains:
        env, default = CHAIN_RPC[name]
        rpc = os.getenv(env) or default
        if not rpc:
            continue
        w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 20}))
        try:
            print(quote(w3, a.pool, a.amount, a.pay, a.slippage, label=name.capitalize()))
            return
        except Exception as e:
            err = e
    sys.exit(f"Не получилось: {err}")


if __name__ == "__main__":
    main()
