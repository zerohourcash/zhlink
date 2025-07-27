import requests
import json
import time

# Базовый URL API
BASE_URL = "https://zhlink.ru/api/zhc"

# Тестовые данные
API_TOKEN = "00000000"
HEADERS = {
    'X-API-Token': API_TOKEN,
    'Content-Type': 'application/json'
}

# Тестовые адреса и ключи (замените на реальные тестовые данные)
TEST_FROM_ADDRESS = ""
TEST_TO_ADDRESS = ""
TEST_PRIVATE_KEY = ""
TEST_CONTRACT = ""
TEST_HEX_ADDRESS = ""


def test_sendtoaddress():
    """Тест метода sendtoaddress"""
    url = f"{BASE_URL}/sendtoaddress"
    payload = json.dumps({
        "from_address": TEST_FROM_ADDRESS,
        "to_address": TEST_TO_ADDRESS,
        "amount": 0.1,
        "priv_key": TEST_PRIVATE_KEY
    })

    response = requests.post(url, headers=HEADERS, data=payload)
    print("=== sendtoaddress ===")
    print(f"Status Code: {response.status_code}")
    print(f"Response: {response.text}")
    print()


def test_sendtoken():
    """Тест метода sendtoken"""
    url = f"{BASE_URL}/sendtoken"
    payload = json.dumps({
        "contract": TEST_CONTRACT,
        "from_address": TEST_FROM_ADDRESS,
        "to_address": TEST_TO_ADDRESS,
        "amount_or_id": 0.1,
        "priv_key": TEST_PRIVATE_KEY,
        "gas": 400000
    })

    response = requests.post(url, headers=HEADERS, data=payload)
    print("=== sendtoken ===")
    print(f"Status Code: {response.status_code}")
    print(f"Response: {response.text}")
    print()


def test_getnewaddress():
    """Тест метода getnewaddress"""
    url = f"{BASE_URL}/getnewaddress"

    response = requests.get(url, headers=HEADERS)
    print("=== getnewaddress ===")
    print(f"Status Code: {response.status_code}")
    print(f"Response: {response.text}")
    print()


def test_checkvalidaddress():
    """Тест метода checkvalidaddress"""
    url = f"{BASE_URL}/checkvalidaddress"
    payload = json.dumps({
        "address": TEST_FROM_ADDRESS
    })

    response = requests.post(url, headers=HEADERS, data=payload)
    print("=== checkvalidaddress ===")
    print(f"Status Code: {response.status_code}")
    print(f"Response: {response.text}")
    print()


def test_addresstohex():
    """Тест метода addresstohex"""
    url = f"{BASE_URL}/addresstohex"
    payload = json.dumps({
        "address": TEST_FROM_ADDRESS
    })

    response = requests.post(url, headers=HEADERS, data=payload)
    print("=== addresstohex ===")
    print(f"Status Code: {response.status_code}")
    print(f"Response: {response.text}")
    print()


def test_hextoaddress():
    """Тест метода hextoaddress"""
    url = f"{BASE_URL}/hextoaddress"
    payload = json.dumps({
        "hex": TEST_HEX_ADDRESS
    })

    response = requests.post(url, headers=HEADERS, data=payload)
    print("=== hextoaddress ===")
    print(f"Status Code: {response.status_code}")
    print(f"Response: {response.text}")
    print()


def test_gettransaction():
    """Тест метода gettransaction"""
    url = f"{BASE_URL}/gettransaction"
    payload = json.dumps({
        "tx_id": "test_transaction_id"
    })

    response = requests.post(url, headers=HEADERS, data=payload)
    print("=== gettransaction ===")
    print(f"Status Code: {response.status_code}")
    print(f"Response: {response.text}")
    print()


def test_gettokens_zrc20_balance():
    """Тест метода gettokens_zrc20_balance"""
    url = f"{BASE_URL}/gettokens_zrc20_balance"
    payload = json.dumps({
        "contract": TEST_CONTRACT,
        "address": TEST_FROM_ADDRESS
    })

    response = requests.post(url, headers=HEADERS, data=payload)
    print("=== gettokens_zrc20_balance ===")
    print(f"Status Code: {response.status_code}")
    print(f"Response: {response.text}")
    print()


def test_balance():
    """Тест метода balance"""
    url = f"{BASE_URL}/balance"
    payload = json.dumps({
        "contract": TEST_CONTRACT,
        "address": TEST_FROM_ADDRESS
    })

    response = requests.post(url, headers=HEADERS, data=payload)
    print("=== balance ===")
    print(f"Status Code: {response.status_code}")
    print(f"Response: {response.text}")
    print()


def test_getaddressbalance():
    """Тест метода getaddressbalance"""
    url = f"{BASE_URL}/getaddressbalance?address={TEST_FROM_ADDRESS}"

    response = requests.get(url, headers=HEADERS)
    print("=== getaddressbalance ===")
    print(f"Status Code: {response.status_code}")
    print(f"Response: {response.text}")
    print()


def test_without_token():
    """Тест запроса без токена (должен вернуть ошибку)"""
    url = f"{BASE_URL}/getnewaddress"
    headers_no_token = {'Content-Type': 'application/json'}

    response = requests.get(url, headers=headers_no_token)
    print("=== Без токена ===")
    print(f"Status Code: {response.status_code}")
    print(f"Response: {response.text}")
    print()


def run_all_tests():
    """Запуск всех тестов"""
    print("Запуск тестов API...\n")

    test_getnewaddress()
    test_checkvalidaddress()
    test_addresstohex()
    test_hextoaddress()
    test_gettransaction()
    test_gettokens_zrc20_balance()
    test_balance()
    test_getaddressbalance()
    test_without_token()

    # Эти тесты отправляют транзакции, используйте с осторожностью
    test_sendtoaddress()
    time.sleep(10 * 60)
    test_sendtoken()


if __name__ == "__main__":
    run_all_tests()