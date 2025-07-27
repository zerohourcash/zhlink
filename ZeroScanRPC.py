# ZeroScanRPC.py

import json
import httpx
import asyncio
import logging
from pprint import pprint

ZEROSCAN_API_URL = "https://ws.zeroscan.io"

logging.basicConfig(level=logging.ERROR)
logger = logging.getLogger(__name__)

class ZeroScanRPC:
    def __init__(self):
        self.client = httpx.AsyncClient(timeout=30.0)

    async def close(self):
        await self.client.aclose()
        self.client = httpx.AsyncClient(timeout=30.0)

    async def get_utxos(self, address: str):
        url = f"{ZEROSCAN_API_URL}/address/{address}/utxo"
        try:
            response = await self.client.get(url)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error("ZeroScan API error: %s", e)
            return {"status": "error", "reason": str(e)}

    async def get_balance(self, address: str):
        url = f"{ZEROSCAN_API_URL}/address/{address}"
        try:
            response = await self.client.get(url)
            response.raise_for_status()
            return round(int(response.json()['balance']) / 10 ** 8 , 2)
        except Exception as e:
            logger.error("ZeroScan API error: %s", e)
            return {"status": "error", "reason": str(e)}    

    async def send_raw_transaction(self, raw_tx: str):
        url = f"{ZEROSCAN_API_URL}tx/send"
        try:
            response = await self.client.post(url, json={"rawtx": raw_tx})
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error("Error sending transaction: %s", e)
            return {"status": "error", "reason": str(e)}

async def main():
    zero_rpc = ZeroScanRPC()
    test_address = "ZQAMwkm4gVji26UgrExKxNhN1FcJfoCzMs"
    # balance = await zero_rpc.get_balance(test_address)
    utxos = await zero_rpc.get_utxos(test_address)
    # pprint(balance)
    pprint(utxos)

if __name__ == '__main__':
    asyncio.run(main())
