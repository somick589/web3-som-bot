"""
Помощник для демонстрации TP/SL: кошелёк A двигает цену токена в пуле.
    python pump.py buy 0.01    # A покупает токены за 0.01 ETH -> цена токена растёт (снайпер увидит +PnL)
    python pump.py sell 100000 # A продаёт 100000 токенов -> цена падает (снайпер увидит -PnL)
Нужны sniper_state.json и в .env: SEPOLIA_RPC, SNIPER_KEY_A.
"""
import json
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from web3 import Web3

load_dotenv()
CHAIN_ID = 11155111
st = json.loads(Path("sniper_state.json").read_text(encoding="utf-8"))
w3 = Web3(Web3.HTTPProvider(os.environ["SEPOLIA_RPC"], request_kwargs={"timeout": 30}))
acct = w3.eth.account.from_key(os.environ["SNIPER_KEY_A"])
token, weth = Web3.to_checksum_address(st["token"]), Web3.to_checksum_address(st["weth"])
router = w3.eth.contract(address=Web3.to_checksum_address(st["router"]), abi=[
    {"name": "swapExactETHForTokens", "type": "function", "stateMutability": "payable",
     "inputs": [{"type": "uint256"}, {"type": "address[]"}, {"type": "address"}, {"type": "uint256"}], "outputs": [{"type": "uint256[]"}]},
    {"name": "swapExactTokensForETH", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"type": "uint256"}, {"type": "uint256"}, {"type": "address[]"}, {"type": "address"}, {"type": "uint256"}], "outputs": [{"type": "uint256[]"}]},
])
erc20 = w3.eth.contract(address=token, abi=[
    {"name": "approve", "type": "function", "stateMutability": "nonpayable", "inputs": [{"type": "address"}, {"type": "uint256"}], "outputs": [{"type": "bool"}]},
])


def send(fn, value=0):
    fn.call({"from": acct.address, "value": value})
    tip = w3.eth.max_priority_fee
    base = w3.eth.get_block("latest")["baseFeePerGas"]
    tx = fn.build_transaction({"from": acct.address, "value": value, "chainId": CHAIN_ID,
                               "nonce": w3.eth.get_transaction_count(acct.address, "pending"),
                               "maxPriorityFeePerGas": tip, "maxFeePerGas": base * 2 + tip})
    signed = acct.sign_transaction(tx)
    h = w3.eth.send_raw_transaction(getattr(signed, "raw_transaction", None) or signed.rawTransaction)
    print("tx: https://sepolia.etherscan.io/tx/0x" + bytes(h).hex())
    rc = w3.eth.wait_for_transaction_receipt(h, timeout=180)
    assert rc["status"] == 1, "транзакция откатилась"


mode, amount = sys.argv[1], float(sys.argv[2])
dl = int(time.time()) + 300
if mode == "buy":
    send(router.functions.swapExactETHForTokens(0, [weth, token], acct.address, dl), value=w3.to_wei(amount, "ether"))
elif mode == "sell":
    a = int(amount * 10 ** 18)
    send(erc20.functions.approve(router.address, a))
    send(router.functions.swapExactTokensForETH(a, 0, [token, weth], acct.address, dl))
else:
    raise SystemExit("режим: buy или sell")
print("готово")
