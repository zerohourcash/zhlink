import asyncio
import uuid
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command

# Конфигурация
BOT_TOKEN = ""

# Инициализация бота
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


def generate_api_key():
    """Генерировать уникальный API ключ"""
    api_key = str(uuid.uuid4())
    with open("key.txt", "a", encoding="utf-8") as f:
        f.write(api_key + "\n")
    print(read_all_keys())
    return api_key

def read_all_keys():
    """Считать все API ключи из файла key.txt и вернуть список"""
    try:
        with open("key.txt", "r", encoding="utf-8") as f:
            keys = [line.strip() for line in f if line.strip()]
        return keys
    except FileNotFoundError:
        return []
    except Exception as e:
        print(f"Ошибка при чтении key.txt: {e}")
        return []

def save_user_key(user_id, api_key):
    """Сохранить связь пользователя и API ключа"""
    with open('user_key.txt', 'a') as f:
        f.write(f"{user_id}:{api_key}\n")


def get_user_key(user_id):
    """Получить API ключ пользователя"""
    try:
        with open('user_key.txt', 'r') as f:
            for line in f:
                if line.strip():
                    saved_user_id, api_key = line.strip().split(':', 1)
                    if saved_user_id == str(user_id):
                        return api_key
        return None
    except FileNotFoundError:
        return None


def user_exists(user_id):
    """Проверить, существует ли пользователь в базе"""
    try:
        with open('user_key.txt', 'r') as f:
            for line in f:
                if line.strip():
                    saved_user_id, _ = line.strip().split(':', 1)
                    if saved_user_id == str(user_id):
                        return True
        return False
    except FileNotFoundError:
        return False


@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer("Привет! Используй /get_key чтобы получить API ключ")


@dp.message(Command("get_key"))
async def cmd_get_key(message: types.Message):
    user_id = message.from_user.id

    # Проверяем, есть ли уже ключ у пользователя
    if user_exists(user_id):
        existing_key = get_user_key(user_id)
        await message.answer(f"У вас уже есть API ключ:\n<code>{existing_key}</code>", parse_mode="HTML")
        return

    # Генерируем новый ключ
    api_key = generate_api_key()
    print(user_id,":",api_key)
    save_user_key(user_id, api_key)
    await message.answer(f"Ваш новый API ключ:\n<code>{api_key}</code>\nСохраните его!", parse_mode="HTML")


@dp.message(Command("my_key"))
async def cmd_my_key(message: types.Message):
    user_id = message.from_user.id
    api_key = get_user_key(user_id)

    if api_key:
        await message.answer(f"Ваш API ключ:\n<code>{api_key}</code>", parse_mode="HTML")
    else:
        await message.answer("У вас пока нет API ключа. Используйте /get_key")


async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())