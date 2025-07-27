# config.py
import platform

system_type = platform.system()
# print(f"Operating System: {system_type}")

if system_type == "Linux":
    CONFIG_PATH = '/root/.zerohour/zerohour.conf'

from decimal import Decimal

'''with open(CONFIG_PATH) as f:
    print(f.read())'''
# Читаем файл конфигурации
from dotenv import dotenv_values

# Чтение конфигурационного файла
config = dotenv_values(CONFIG_PATH)

# Доступ к значениям
# print(config["rpcuser"])       # Вывод: dimaystinov
# print(config["rpcpassword"])   # Вывод: qwertyuiopasdfghjzxcvbnm12345678
# print(config["rpcport"])       # Вывод: 3889

RPC_USER = config["rpcuser"]
RPC_PASS = config["rpcpassword"]
print(RPC_USER, RPC_PASS)
RPC_HOST = 'localhost'
RPC_PORT = config["rpcport"]

# SSL конфигурация
SSL_KEYFILE = '/root/ssl/key.pem'
SSL_CERTFILE = '/root/ssl/cert.pem'

# Административные настройки
ADMIN_ADDRESS = 'ZGqDPGCds5CBRHLZZCnYWsYWYPF3i9NCvi'
ADMIN_FEE = Decimal('1')
EXTRA_FEE = Decimal('0.1')
