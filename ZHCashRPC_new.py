import json
import httpx
import asyncio
import logging
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, List, Dict, Optional
from ZeroScanRPC import ZeroScanRPC
from address_generator_zhcash import BitcoinAddress
from pprint import pprint

from config import (
    RPC_USER, RPC_PASS, RPC_HOST, RPC_PORT,
    ADMIN_ADDRESS, ADMIN_FEE, EXTRA_FEE
)

logging.basicConfig(level=logging.ERROR)
logger = logging.getLogger(__name__)

class ZHCashRPC:
    def __init__(self):
        self.zhc_address = BitcoinAddress()
        self.admin_address = ADMIN_ADDRESS
        self.admin_fee = ADMIN_FEE
        self.extra_fee = EXTRA_FEE
        self.url = f"http://{RPC_USER}:{RPC_PASS}@{RPC_HOST}:{RPC_PORT}"
        self.client = httpx.AsyncClient(timeout=30.0)
        self.zero_rpc = ZeroScanRPC()
        self.scan_lock = asyncio.Lock()

    async def close(self):
        await self.client.aclose()
        await self.zero_rpc.close()
        self.client = httpx.AsyncClient(timeout=30.0)

    async def call_rpc(self, method: str, params: Optional[List[Any]] = None) -> Any:
        if params is None:
            params = []
        payload = {"method": method, "params": params, "jsonrpc": "2.0", "id": 0}
        try:
            response = await self.client.post(
                self.url, data=json.dumps(payload), headers={'content-type': 'application/json'}
            )
            response.raise_for_status()
            result = response.json()
            if 'error' in result and result['error'] is not None:
                raise RuntimeError(f"RPC Error: {result['error']}")
            return result.get('result', None)
        except Exception as e:
            logger.error("RPC connection error: %s", e)
            raise

    async def getblockcount(self) -> Any:
        return await self.call_rpc("getblockcount")

    async def getnewaddress(self) -> Dict[str, Any]:
        res = self.zhc_address.get_address_and_private_key()
        try:
            isvalid = await self.validateaddress(res['address'])
            if isvalid:
                return res
            return {"status": "error", "reason": "Invalid address", "data": res}
        except Exception as e:
            return {"status": "error", "reason": str(e), "data": res}

    async def validateaddress(self, address: str) -> bool:
        return (await self.call_rpc("validateaddress", [address]))["isvalid"]

    async def getbalance(self, address: str) -> Dict[str, Any]:
        balance = await self.zero_rpc.get_balance(address)
        if balance["status"] == "error":
            return await self.call_rpc("scantxoutset", [{"desc": f"addr({address})", "range": 1000}])
        return balance

    async def get_utxos(self, address: str) -> List[Dict[str, Any]]:
        utxos = await self.zero_rpc.get_utxos(address)
        if utxos["status"] == "error":
            utxos = await self.call_rpc("scantxoutset", [{"desc": f"addr({address})", "range": 1000}])
        return utxos.get("unspents", [])

    async def create_raw_transaction(self, utxos: List[Dict[str, Any]], outputs: Dict[str, Any], min_fee: Decimal,
                                     change_address: str) -> str:
        inputs = [{"txid": utxo['txid'], "vout": utxo['vout']} for utxo in utxos]
        total_input = sum(Decimal(str(utxo['amount'])) for utxo in utxos)
        total_output = sum(
            Decimal(str(amount)) for amount in outputs.values() if isinstance(amount, (float, int, str, Decimal)))
        min_fee = Decimal(min(min_fee, Decimal(1)))

        change = (total_input - total_output - min_fee).quantize(Decimal('0.00000001'), rounding=ROUND_HALF_UP)

        if change > 0:
            if change_address in outputs:
                outputs[change_address] = str(Decimal(outputs[change_address]) + change)
            else:
                outputs[change_address] = str(change)

        total_input = sum(Decimal(str(utxo['amount'])) for utxo in utxos)
        total_output = sum(
            Decimal(str(amount)) for amount in outputs.values() if isinstance(amount, (float, int, str, Decimal)))
        extra_fee = total_input - total_output - min_fee

        print('min_fee', min_fee)
        print('total_input', total_input)
        print('total_output', total_output)
        print('change', change)
        print('extra_fee', extra_fee)

        if extra_fee >= 1:
            outputs[self.admin_address] = str(Decimal("1") + extra_fee)

        print('outputs')
        pprint(outputs)
        try:
            raw_tx = await self.call_rpc('createrawtransaction', [inputs, outputs])
        except Exception as e:
            print('call_rpc createrawtransaction')
        return raw_tx

    async def sign_transaction(self, raw_transaction: str, private_key: str) -> str:
        sign_response = await self.call_rpc("signrawtransactionwithkey", [raw_transaction, [private_key]])
        signed_tx = sign_response.get("hex", "")
        if not signed_tx:
            raise ValueError("Failed to sign transaction.")
        return signed_tx

    async def select_utxos(self, utxos: List[Dict[str, Any]], min_fee: Decimal, amount: Decimal) -> List[Dict[str, Any]]:
        scriptPubKey_correct = '76a914'
        '''eligible_utxos = [
            utxo for utxo in utxos
            if utxo.get('scriptPubKey', '').startswith(scriptPubKey_correct) and
               not (await self.gettxout(utxo.get('txid'), utxo.get('vout'))).get('coinstake', True)
        ]'''
        eligible_utxos = [
            utxo for utxo in utxos
            if utxo.get('scriptPubKey', '').startswith(scriptPubKey_correct) and
               (
                       not (await self.gettxout(utxo.get('txid'), utxo.get('vout'))).get('coinstake', True) or
                       (utxo.get('isStake', False) and utxo.get('confirmations', 0) > 500)
               )
        ]
        if not eligible_utxos:
            raise ValueError(f"No suitable UTXOs available. Total UTXOs: {len(utxos)}")
        eligible_utxos.sort(key=lambda x: Decimal(str(x['amount'])))
        selected = []
        total = Decimal('0')
        min_fee = Decimal(min(min_fee, Decimal(1)))
        for utxo in eligible_utxos:
            selected.append(utxo)
            total += Decimal(str(utxo['amount']))
            if total >= min_fee + self.admin_fee + self.extra_fee + amount:
                break
        if total < min_fee + self.admin_fee + self.extra_fee + amount:
            raise ValueError(f"Insufficient funds. Total: {total}, Required: {min_fee + self.admin_fee + self.extra_fee + amount} min_fee {min_fee} self.admin_fee {self.admin_fee} self.extra_fee {self.extra_fee} amount {amount} lenUTXO {len(eligible_utxos)}")
        return selected

    async def send_transaction(self, signed_tx: str) -> str:
        try:
            tx_id = await self.zero_rpc.send_raw_transaction(signed_tx)
            if tx_id["status"] == "error":
                raise Exception("ZeroScan failed, switching to node")
            return tx_id
        except:
            return await self.call_rpc("sendrawtransaction", [signed_tx])

    async def send_to_contract(
            self,
            contract_address: str,
            from_address: str,
            to_address: str,
            amount: float,
            private_key: str,
            hex_command: str,
            gas: int

    ) -> Dict[str, Any]:
        try:
            amount = Decimal(str(amount))
            utxos = await self.get_utxos(from_address)
            min_fee_rate = await self.get_min_fee()
            min_fee = await self.calculate_fee_from_fee_rate(len(utxos))
            gas_price = Decimal('0.00000040')
            gas_fee = (Decimal(gas) * gas_price).quantize(Decimal('0.00000001'), rounding=ROUND_HALF_UP)
            total_needed = amount + gas_fee + min_fee

            selected_utxos = await self.select_utxos(utxos, min_fee + gas_fee + self.admin_fee, amount)

            pprint(selected_utxos)

            selected_amount = sum(Decimal(str(utxo['amount'])) for utxo in selected_utxos)
            if selected_amount < total_needed:
                raise ValueError("Insufficient funds for amount and fees.")

            common_fee = min(min_fee + gas_fee, 1)
            outputs = {
                from_address: str(selected_amount - common_fee - self.admin_fee),
                self.admin_address: str(self.admin_fee)  # str(gas_fee + min_fee)
            }
            contract_info = {
                "contract": {
                    "contractAddress": contract_address,
                    "data": hex_command,
                    "amount": str(amount),
                    "gasLimit": gas,
                    "gasPrice": '0.00000040'
                }
            }
            outputs.update(contract_info)
            print('outputs')
            pprint(outputs)
            raw_tx = await self.create_raw_transaction(
                utxos=selected_utxos,
                outputs=outputs,
                min_fee=min_fee + gas_fee,
                change_address=from_address
            )
            print('raw_tx', raw_tx)
            if not type(raw_tx) is str:
                return {'status': "error", "reason": raw_tx}
            tx_weight = self.calculate_tx_weight(len(selected_utxos))
            calculated_fee = (min_fee_rate * Decimal(tx_weight) / Decimal(4)).quantize(Decimal('0.00000001'),
                                                                                       rounding=ROUND_HALF_UP)
            signed_tx = await self.sign_transaction(raw_tx, private_key)
            print('signed_tx', signed_tx)
            txid = await self.send_transaction(signed_tx)
            logger.info(f"Contract transaction sent successfully. TXID: {txid}")
            return {'status': "ok", "tx_id": txid}
        except Exception as e:
            logger.error("Error sending to contract: %s", e)
            return {'status': "error", "reason": e}
        finally:
            logger.info("send_to_contract completed")

    async def send_token(
            self,
            contract_address: str,
            from_address: str,
            to_address: str,
            amount_or_id: float,
            private_key: str,
            gas: int = 25000
    ) -> Dict[str, Any]:
        # gas = 250000
        # try:
        hex_command = f"a9059cbb{await self.get_finish_hex_address(to_address)}{self.hexify_number(int(amount_or_id * 10 ** 8))}"
        print('hex_command', hex_command)
        return await self.send_to_contract(
            contract_address=contract_address,
            from_address=from_address,
            to_address=to_address,
            amount=0.0,  # Сколько ЗХ передать в контракт
            private_key=private_key,
            hex_command=hex_command,
            gas=gas
        )
