from bitcoinrpc.authproxy import AuthServiceProxy
import logging
from typing import Any, List, Dict, Optional
from decimal import Decimal, ROUND_HALF_UP
import traceback
import asyncio
from address_generator_zhcash import BitcoinAddress
from ZeroScanRPC import ZeroScanRPC
from config import (
    RPC_HOST, RPC_PORT, RPC_USER, RPC_PASS,
    ADMIN_ADDRESS, ADMIN_FEE, EXTRA_FEE
)

# Настройка логирования
logging.basicConfig(
    level=logging.ERROR,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)


class ZHCashRPC:
    def __init__(self):
        # Создаем URL для подключения к RPC-серверу с использованием rpc_user и rpc_password
        rpc_url = f"http://{RPC_USER}:{RPC_PASS}@{RPC_HOST}:{RPC_PORT}"

        # Создаем соединение с RPC-сервером через AuthServiceProxy
        self.rpc_connection = AuthServiceProxy(
            rpc_url,
            timeout=30  # Timeout в секундах
        )

        # Инициализация других параметров
        self.zhc_address = BitcoinAddress()
        self.admin_address = ADMIN_ADDRESS
        self.admin_fee = ADMIN_FEE
        self.extra_fee = EXTRA_FEE
        self.zero_rpc = ZeroScanRPC()
        self.scan_lock = asyncio.Lock()  # Блокировка выполнения параллельных вызовов

    async def getblockcount(self) -> int:
        return self.rpc_connection.getblockcount()

    async def gettransactionreceipt(self, tx_hash: str) -> Any:
        return self.rpc_connection.gettransactionreceipt(tx_hash)

    async def gettxout(self, txid: str, n: int, include_mempool: bool = True) -> Any:
        res = self.rpc_connection.gettxout(txid, n, include_mempool)
        return {"coinstake": True} if res is None else res

    async def gettxoutproof(self, txids: List[str], blockhash: Optional[str] = None) -> Any:
        params = [txids]
        if blockhash:
            params.append(blockhash)
        return self.rpc_connection.gettxoutproof(params)

    async def scantxoutset(self, address: str) -> Any:
        scan_object = {"desc": f"addr({address})", "range": 1000}
        params = ["start", [scan_object]]
        async with self.scan_lock:  # Блокировка выполнения параллельных вызовов
            return self.rpc_connection.scantxoutset(*params)

    async def gettxoutsetinfo(self) -> Any:
        return self.rpc_connection.gettxoutsetinfo()

    async def fromhexaddress(self, hex_address: str) -> str:
        return self.rpc_connection.fromhexaddress(hex_address)

    async def gethexaddress(self, address: str) -> str:
        return self.rpc_connection.gethexaddress(address)

    async def decodescript(self, script_hex: str) -> Any:
        return self.rpc_connection.decodescript(script_hex)

    async def validateaddress(self, address: str) -> bool:
        return self.rpc_connection.validateaddress(address)['isvalid']

    async def decoderawtransaction(self, hexstring: str, iswitness: Optional[bool] = None) -> Any:
        params = [hexstring]
        if iswitness is not None:
            params.append(iswitness)
        return self.rpc_connection.decoderawtransaction(*params)

    async def get_utxos(self, address: str) -> Any:
        scriptPubKey_correct = '76a914'
        try:
            utxos = self.zero_rpc.get_utxos(address)
            res = []
            for el in utxos:
                new_el = {}
                if el['scriptPubKey'].startswith(scriptPubKey_correct) and (
                        not el['isStake'] or el['confirmations'] > 500):
                    new_el['amount'] = int(el['value']) / 10 ** 8
                    new_el['txid'] = el['transactionId']
                    new_el['vout'] = el['outputIndex']
                    res.append(new_el)
            return res
        except Exception as e:
            print(traceback.format_exc())
            print(e)
        res = await self.scantxoutset(address)
        utxos = res.get('unspents', [])
        utxos = [
            utxo for utxo in utxos
            if utxo.get('scriptPubKey', '').startswith(scriptPubKey_correct) and
               (
                       not (await self.gettxout(utxo.get('txid'), utxo.get('vout'))).get('coinstake', True) or
                       (utxo.get('isStake', False) and utxo.get('confirmations', 0) > 500)
               )
        ]
        return utxos


async def main():
    zhc = ZHCashRPC()
    print(await zhc.getblockcount())


if __name__ == '__main__':
    asyncio.run(main())