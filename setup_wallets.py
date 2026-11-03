"""
Шаг 1 для Sniper Simulator: создаёт 2 ТЕСТОВЫХ кошелька и дописывает их ключи в .env.
Кошелёк A: деплоит токен, создаёт пул и добавляет ликвидность.
Кошелёк B: «снайпер», покупает и продаёт токен.

ТОЛЬКО для тестнета. Никогда не используй эти ключи с реальными деньгами
и не присылай приватные ключи в чат или куратору.
Запуск: python setup_wallets.py
"""
from pathlib import Path

from eth_account import Account

ENV = Path(".env")
text = ENV.read_text(encoding="utf-8") if ENV.exists() else ""

if "SNIPER_KEY_A" in text or "SNIPER_KEY_B" in text:
    raise SystemExit("В .env уже есть SNIPER_KEY_A / SNIPER_KEY_B. Ничего не меняю.")

a, b = Account.create(), Account.create()
add = ("\n" if text and not text.endswith("\n") else "") + (
    f"SNIPER_KEY_A={a.key.hex()}\n"
    f"SNIPER_KEY_B={b.key.hex()}\n"
)
ENV.write_text(text + add, encoding="utf-8")

print("Созданы два тестовых кошелька (ключи записаны в .env):")
print("  A (создаёт пул):", a.address)
print("  B (снайпер):    ", b.address)
print("\nТеперь получи Sepolia ETH на ОБА адреса через faucet (нужно хотя бы 0.05 на каждый).")
