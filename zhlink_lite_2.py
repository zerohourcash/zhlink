import json
import os
import logging
import traceback
from datetime import datetime, timedelta
from typing import Optional
from fastapi import FastAPI, Header, Body, HTTPException, BackgroundTasks, Depends, Query
from pydantic import BaseModel
from ZHCashRPC import ZHCashRPC  # Убедитесь, что этот класс доступен
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from typing import Union
# Настройка приложения FastAPI
app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")

# Настройка логирования
LOG_FILE = "api_errors.log"

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.ERROR,
    format="%(asctime)s - %(levelname)s - %(message)s",
)

official_tokens = [
'a48d0ee7365ce1add8e595de4d54344239f8ca28', # USDZ
'545af9c41c78d77aa8f81d4dd24445db676628ba' # BLAGO
]

def log_error(e, trace='', data=''):
    #log_data = f"Exception: {e}\nTraceback:\n{trace}\nData:\n{data}\n{'-' * 10}"
    # print()
    logging.error(e)

# Константы
API_TOKEN = ["e625bfa59a0a9831433f03c7f42a562c",
             "a5f31e201c1552fcbbaa67cf305329dd",
             "b186b35dd64420fedbcf94fc32e7c937",
             "b186b35dd64420fedbcf94fc32e7c938",
             "b186b35dd64420fedbcqwefc32e7c938",
             "e625bfa59a0a9831433f03c7f42a562c"]  # OPEN
CACHE_FILE = "balance_cache.json"
CACHE_TTL = timedelta(minutes=10)  # Время жизни кэша
zhc = ZHCashRPC()


# Вспомогательные функции для работы с кэшем
def load_cache():
    """Загружает кэш из JSON-файла."""
    if not os.path.exists(CACHE_FILE):
        return {}
    try:
        with open(CACHE_FILE, "r") as file:
            return json.load(file)
    except json.JSONDecodeError as e:
        log_error(e, traceback.format_exc())
        return {}

def save_cache(cache):
    """Сохраняет кэш в JSON-файл."""
    try:
        with open(CACHE_FILE, "w") as file:
            json.dump(cache, file, indent=4)
    except Exception as e:
        log_error(e, traceback.format_exc())

def is_cache_expired(entry):
    """Проверяет, истекло ли время жизни данных в кэше."""
    if "timestamp" not in entry:
        return True
    try:
        cache_time = datetime.fromisoformat(entry["timestamp"])
        return datetime.now() - cache_time > CACHE_TTL
    except Exception as e:
        log_error(e, traceback.format_exc())
        return True

async def update_cache(address, cache):
    """Обновляет данные в кэше асинхронно."""
    try:
        response = await zhc.getbalance(address)
        if response.get("status") == "ok":
            cache[address] = {
                "balance": response["balance"],
                "utxo_len": response["utxo_len"],
                "timestamp": datetime.now().isoformat(),
            }
            save_cache(cache)
    except Exception as e:
        log_error(e, traceback.format_exc())

# Проверка токена
def check_token(token: Optional[str]):
    return token in API_TOKEN

# Модели для входных данных
class AddressRequest(BaseModel):
    address: str

class HexRequest(BaseModel):
    hex: str

class TransactionRequest(BaseModel):
    tx_id: str

class SendTokenRequest(BaseModel):
    contract: str
    from_address: str
    to_address: str
    amount_or_id:  Union[float, int, str]
    priv_key: str
    gas: Union[int,float, str] = 400000

class SendToAddressRequest(BaseModel):
    from_address: str
    to_address: str
    amount: Union[float, int, str]
    priv_key: str

class GetAddressBalanceRequest(BaseModel):
    address: str = "ZC3Fmgr3oS56Rg9vxZeVo2mwMMcTvhxKzc"

# Эндпоинты
class BalanceQuery(BaseModel):
    address: str


@app.get("/api/zhc/getaddressbalance")
async def get_address_balance(
    address: Optional[str] = Query("ZC3Fmgr3oS56Rg9vxZeVo2mwMMcTvhxKzc", description="ZHCash address to get balance for")):

    print(address)
    cache = load_cache()
    if address in cache and not is_cache_expired(cache[address]):
        background_tasks.add_task(update_cache, address, cache)
        return {
            "status": "ok",
            "balance": cache[address]["balance"],
            "utxo_len": cache[address]["utxo_len"],
            "zrc721Balances": [],

            "cached": True,
        }
    else:
        return {
            "status": "ok",
            "balance": 0,
            "utxo_len": 0,
            "zrc721Balances": [],

            "cached": False,
        }


'''
    "zrc20Balances": [
            {
                "address": "545af9c41c78d77aa8f81d4dd24445db676628ba",
                "addressHex": "545af9c41c78d77aa8f81d4dd24445db676628ba",
                "name": "BLAGODAR",
                "symbol": "DAR",
                "decimals": 8,
                "balance": (await zhc.get_smartcontract_balanceOf('545af9c41c78d77aa8f81d4dd24445db676628ba', address))['tokens_zrc20_balance']},
            {
                "address": "a48d0ee7365ce1add8e595de4d54344239f8ca28",
                "addressHex": "a48d0ee7365ce1add8e595de4d54344239f8ca28",
                "name": "United States Dollar ZHCHAIN",
                "symbol": "USDZ",
                "decimals": 8,
                "balance": (await zhc.get_smartcontract_balanceOf('a48d0ee7365ce1add8e595de4d54344239f8ca28', address))['tokens_zrc20_balance']}
            ],
    try:
        response = await zhc.getbalance(address)
        if response.get("status") == "ok":
            cache[address] = {
                "balance": response["balance"],
                "utxo_len": response["utxo_len"],
                "timestamp": datetime.now().isoformat(),
                "zrc721Balances": [],
            }
            save_cache(cache)
            return {
                "status": "success",
                "balance": response["balance"],
                "utxo_len": response["utxo_len"],
                "cached": False,
                "zrc721Balances": [],
            }
        else:
            return {"status": "error", "reason": "Failed to fetch balance", "details": response.get("status")}
    except Exception as e:
        log_error(e, traceback.format_exc(), data.json())
        return {"status": "error", "reason": "Failed to get balance", "details": str(e)}'''

@app.get("/api/zhc/getnewaddress")
async def get_new_address():
    return await zhc.getnewaddress()


@app.post("/api/zhc/sendtoken")
async def send_token(
    data: SendTokenRequest,
    x_api_token: Optional[str] = Header(None)
):
    if not check_token(x_api_token):
        return {"status": "error", "reason": "API Token error!"}
    try:
        res = await zhc.send_token(
            contract_address=data.contract,
            from_address=data.from_address,
            to_address=data.to_address,
            amount_or_id=data.amount_or_id,
            private_key=data.priv_key,
            gas=data.gas,
        )
        return res
    except Exception as e:
        # log_error(e, traceback.format_exc(), data.json())
        return {"status": "error", "reason": str(e)}

@app.post("/api/zhc/sendtoaddress")
async def send_to_address(
    data: SendToAddressRequest,
    x_api_token: Optional[str] = Header(None)
):
    if not check_token(x_api_token):
        return {"status": "error", "reason": "API Token error!"}

    if data.amount <= 0:
        return {"status": "error", "reason": "Invalid amount!"}

    try:
        res = await zhc.send_zhc(data.from_address, data.to_address, data.amount, data.priv_key)
        return res
    except Exception as e:
        # log_error(e, traceback.format_exc(), data.json())
        return {"status": "error", "reason": str(e)}

@app.post("/api/zhc/checkvalidaddress")
async def check_valid_address(
    data: AddressRequest,
    x_api_token: Optional[str] = Header(None)
):
    if not check_token(x_api_token):
        return {"status": "error", "reason": "API Token error!"}
    try:
        valid = await zhc.validateaddress(data.address)
        return {"status": "ok", "valid": valid}
    except Exception as e:
        #log_error(e, traceback.format_exc(), data.json())
        return {"status": "error", "reason": str(e)}

@app.post("/api/zhc/addresstohex")
async def address_to_hex(
    data: AddressRequest,
    x_api_token: Optional[str] = Header(None)
):
    if not check_token(x_api_token):
        return {"status": "error", "reason": "API Token error!"}
    try:
        hex_val = await zhc.gethexaddress(data.address)
        return {"status": "ok", "hex": hex_val}
    except Exception as e:
        log_error(e, traceback.format_exc(), data.json())
        return {"status": "error", "reason": str(e)}

@app.post("/api/zhc/hextoaddress")
async def hex_to_address(
    data: HexRequest,
    x_api_token: Optional[str] = Header(None)
):
    if not check_token(x_api_token):
        return {"status": "error", "reason": "API Token error!"}
    try:
        address = await zhc.fromhexaddress(data.hex)
        return {"status": "ok", "address": address}
    except Exception as e:
        log_error(e, traceback.format_exc(), data.json())
        return {"status": "error", "reason": str(e)}

@app.post("/api/zhc/gettransaction")
async def get_transaction(
    data: TransactionRequest,
    x_api_token: Optional[str] = Header(None)
):
    if not check_token(x_api_token):
        return {"status": "error", "reason": "API Token error!"}
    try:
        res = await zhc.gettransaction(data.tx_id)
        return res
    except Exception as e:
        log_error(e, traceback.format_exc(), data.json())
        return {"status": "error", "reason": str(e)}

@app.post("/api/zhc/gettokens_zrc20_balance")
async def tokens_zrc20_balance(
    contract: str = Body(...),
    address: str = Body(...),
    x_api_token: Optional[str] = Header(None)
):
    if not check_token(x_api_token):
        return {"status": "error", "reason": "API Token error!"}

    return await zhc.get_smartcontract_balanceOf(contract, address)

@app.post("/api/zhc/balance")
async def tokens_zrc20_balance(
    contract: str = Body(...),
    address: str = Body(...),
    x_api_token: Optional[str] = Header(None)
):
    if not check_token(x_api_token):
        return {"status": "error", "reason": "API Token error!"}

    return await zhc.get_smartcontract_balanceOf(contract, address)


@app.get("/")
async def serve_index():
    if os.path.exists("index.html"):
        return FileResponse("index.html")
    else:
        # Создаем базовый index.html если его нет
        default_html = """
        <!DOCTYPE html>
        <html>
        <head>
            <title>ZHCash Link Service</title>
            <meta charset="utf-8">
        </head>
        <body>
            <h1>ZHCash Link Service</h1>
            <p>Service is running successfully!</p>
            <ul>
                <li><a href="/docs">API Documentation</a></li>
                <li><a href="/redoc">Alternative API Docs</a></li>
            </ul>
        </body>
        </html>
        """
        with open("index.html", "w") as f:
            f.write(default_html)
        return FileResponse("index.html")


'''
{
  "balance": "4661882912263933",
  "totalReceived": "4666980946231675",
  "totalSent": "5098033967742",
  "unconfirmed": "0",
  "staking": "112972643862029",
  "mature": "4548910268401904",
  "zrc20Balances": [
    {
      "address": "545af9c41c78d77aa8f81d4dd24445db676628ba",
      "addressHex": "545af9c41c78d77aa8f81d4dd24445db676628ba",
      "name": "BLAGODAR",
      "symbol": "DAR",
      "decimals": 8,
      "balance": "500000000",
      "unconfirmed": {
        "received": "0",
        "sent": "0"
      }
    },
    {
      "address": "a48d0ee7365ce1add8e595de4d54344239f8ca28",
      "addressHex": "a48d0ee7365ce1add8e595de4d54344239f8ca28",
      "name": "United States Dollar ZHCHAIN",
      "symbol": "USDZ",
      "decimals": 8,
      "balance": "50000000",
      "unconfirmed": {
        "received": "0",
        "sent": "0"
      }
    },
    {
      "address": "b01bccb5f180f7173dce66a03d977c49be5ed22a",
      "addressHex": "b01bccb5f180f7173dce66a03d977c49be5ed22a",
      "name": "DAO ZHCASH TESTNET MASTER CONTRACT",
      "symbol": "DZTMC",
      "decimals": 0,
      "balance": "1",
      "unconfirmed": {
        "received": "0",
        "sent": "0"
      }
    },
    {
      "address": "cac514476670462d258c72285924dc2bc8fbfcc8",
      "addressHex": "cac514476670462d258c72285924dc2bc8fbfcc8",
      "name": "ZHC TEST",
      "symbol": "ZHC",
      "decimals": 8,
      "balance": "200000000",
      "unconfirmed": {
        "received": "0",
        "sent": "0"
      }
    }
  ],
  "zrc721Balances": [],
  "ranking": 44,
  "transactionCount": 19105,
  "blocksMined": 1899
}
'''