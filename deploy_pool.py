"""
Шаги 2–3 Sniper Simulator (Sepolia): кошелёк A
  1) деплоит тестовый ERC-20 токен,
  2) создаёт пул на Uniswap V2 и добавляет ликвидность (addLiquidityETH).
Результат (адреса токена, пары, роутера) сохраняется в sniper_state.json.

Нужно в .env: SEPOLIA_RPC, SNIPER_KEY_A (создаётся setup_wallets.py).
Необязательно: UNISWAP_V2_ROUTER, TOKEN_SUPPLY, LIQ_TOKENS, LIQ_ETH.
Запуск: python deploy_pool.py
"""
import json
import os
import time
from pathlib import Path

from dotenv import load_dotenv
from solcx import compile_source, install_solc
from web3 import Web3

load_dotenv()

RPC = os.environ["SEPOLIA_RPC"]
KEY_A = os.environ["SNIPER_KEY_A"]
# Адрес Router02 в Sepolia — сверь с документацией Uniswap или спроси куратора.
# Скрипт сам проверит: у настоящего роутера есть factory() и WETH().
ROUTER = os.getenv("UNISWAP_V2_ROUTER", "0xeE567Fe1712Faf6149d80dA1E6934E354124CfE3")
SUPPLY = int(os.getenv("TOKEN_SUPPLY", "10000000"))        # сколько токенов выпускаем
LIQ_TOKENS = int(os.getenv("LIQ_TOKENS", "1000000"))        # сколько токенов кладём в пул
LIQ_ETH = float(os.getenv("LIQ_ETH", "0.02"))               # сколько ETH кладём в пул
STATE = Path("sniper_state.json")
CHAIN_ID = 11155111

SOURCE = """
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;
contract TestToken {
    string public name = "Sniper Test Token";
    string public symbol = "SNTT";
    uint8 public constant decimals = 18;
    uint256 public totalSupply;
    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;
    event Transfer(address indexed from, address indexed to, uint256 value);
    event Approval(address indexed owner, address indexed spender, uint256 value);
    constructor(uint256 supply) {
        totalSupply = supply;
        balanceOf[msg.sender] = supply;
        emit Transfer(address(0), msg.sender, supply);
    }
    function transfer(address to, uint256 v) external returns (bool) {
        balanceOf[msg.sender] -= v; balanceOf[to] += v;
        emit Transfer(msg.sender, to, v); return true;
    }
    function approve(address sp, uint256 v) external returns (bool) {
        allowance[msg.sender][sp] = v; emit Approval(msg.sender, sp, v); return true;
    }
    function transferFrom(address from, address to, uint256 v) external returns (bool) {
        uint256 a = allowance[from][msg.sender];
        if (a != type(uint256).max) { allowance[from][msg.sender] = a - v; }
        balanceOf[from] -= v; balanceOf[to] += v;
        emit Transfer(from, to, v); return true;
    }
}
"""

ROUTER_ABI = [
    {"name": "factory", "type": "function", "stateMutability": "view", "inputs": [], "outputs": [{"type": "address"}]},
    {"name": "WETH", "type": "function", "stateMutability": "view", "inputs": [], "outputs": [{"type": "address"}]},
    {"name": "addLiquidityETH", "type": "function", "stateMutability": "payable",
     "inputs": [{"name": "token", "type": "address"}, {"name": "amountTokenDesired", "type": "uint256"},
                {"name": "amountTokenMin", "type": "uint256"}, {"name": "amountETHMin", "type": "uint256"},
                {"name": "to", "type": "address"}, {"name": "deadline", "type": "uint256"}],
     "outputs": [{"type": "uint256"}, {"type": "uint256"}, {"type": "uint256"}]},
]
FACTORY_ABI = [
    {"name": "getPair", "type": "function", "stateMutability": "view",
     "inputs": [{"type": "address"}, {"type": "address"}], "outputs": [{"type": "address"}]},
]

w3 = Web3(Web3.HTTPProvider(RPC, request_kwargs={"timeout": 30}))
acct = w3.eth.account.from_key(KEY_A)


def send(fn, value=0):
    """Подписывает и отправляет транзакцию, ждёт подтверждения, печатает хеш и газ."""
    tip = w3.eth.max_priority_fee
    base = w3.eth.get_block("latest")["baseFeePerGas"]
    tx = fn.build_transaction({
        "from": acct.address, "nonce": w3.eth.get_transaction_count(acct.address),
        "chainId": CHAIN_ID, "value": value,
        "maxPriorityFeePerGas": tip, "maxFeePerGas": base * 2 + tip,
    })
    signed = acct.sign_transaction(tx)
    raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
    h = w3.eth.send_raw_transaction(raw)
    print("  tx:", "0x" + h.hex().removeprefix("0x"))
    rc = w3.eth.wait_for_transaction_receipt(h, timeout=180)
    if rc["status"] != 1:
        raise SystemExit("Транзакция отклонена (revert). См. хеш выше на sepolia.etherscan.io")
    print(f"  подтверждена в блоке {rc['blockNumber']}, газ {rc['gasUsed']}")
    return rc


def main():
    assert w3.is_connected(), "нет подключения к SEPOLIA_RPC"
    assert w3.eth.chain_id == CHAIN_ID, f"это не Sepolia (chain_id={w3.eth.chain_id})"
    bal = w3.from_wei(w3.eth.get_balance(acct.address), "ether")
    print(f"Кошелёк A: {acct.address}, баланс {bal} ETH")
    if bal < LIQ_ETH + 0.01:
        raise SystemExit(f"Мало тестового ETH: нужно минимум {LIQ_ETH + 0.01}. Получи его в faucet и запусти снова.")

    router = w3.eth.contract(address=Web3.to_checksum_address(ROUTER), abi=ROUTER_ABI)
    if len(w3.eth.get_code(router.address)) == 0:
        raise SystemExit(f"По адресу роутера {ROUTER} нет контракта. Уточни адрес Uniswap V2 Router02 в Sepolia.")
    try:
        factory_addr, weth = router.functions.factory().call(), router.functions.WETH().call()
    except Exception:
        raise SystemExit("Этот адрес не похож на Uniswap V2 Router02 (нет factory()/WETH()).")
    print("Роутер ок. Фабрика:", factory_addr, "WETH:", weth)

    print("Компилирую токен (первый раз скачается компилятор solc)...")
    install_solc("0.8.20")
    art = compile_source(SOURCE, output_values=["abi", "bin"], solc_version="0.8.20")["<stdin>:TestToken"]
    Token = w3.eth.contract(abi=art["abi"], bytecode=art["bin"])

    print("1) Деплой токена")
    rc = send(Token.constructor(SUPPLY * 10 ** 18))
    token = w3.eth.contract(address=rc["contractAddress"], abi=art["abi"])
    print("  токен:", token.address)

    print("2) approve: разрешаю роутеру взять токены для пула")
    send(token.functions.approve(router.address, LIQ_TOKENS * 10 ** 18))

    print("3) Создание пула и добавление ликвидности (addLiquidityETH)")
    deadline = int(time.time()) + 600
    send(router.functions.addLiquidityETH(token.address, LIQ_TOKENS * 10 ** 18, 0, 0, acct.address, deadline),
         value=w3.to_wei(LIQ_ETH, "ether"))

    factory = w3.eth.contract(address=factory_addr, abi=FACTORY_ABI)
    pair = factory.functions.getPair(token.address, weth).call()
    print("Пул (пара):", pair)
    STATE.write_text(json.dumps({"token": token.address, "pair": pair, "router": router.address,
                                 "factory": factory_addr, "weth": weth, "token_abi": art["abi"]},
                                indent=2), encoding="utf-8")
    print("Готово. Данные сохранены в sniper_state.json")


if __name__ == "__main__":
    main()
