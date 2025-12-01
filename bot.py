# bot.py — Напоминалка с PostgreSQL

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
import asyncio
import asyncpg
import os
from datetime import datetime, timedelta
from dotenv import load_dotenv

# === ЗАГРУЗКА ТОКЕНА И БД ===
load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

if not TOKEN:
    raise ValueError("❌ Не установлен BOT_TOKEN")
if not DATABASE_URL:
    raise ValueError("❌ Не установлен DATABASE_URL")

# === СОЗДАНИЕ БОТА И ДИСПЕТЧЕРА ===
bot = Bot(token=TOKEN)
dp = Dispatcher()

# === ПОДКЛЮЧЕНИЕ К БАЗЕ ===
db_pool = None

async def init_db():
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL)
    await db_pool.execute('''
        CREATE TABLE IF NOT EXISTS reminders (
            id SERIAL PRIMARY KEY,
            user_id BIGINT,
            message TEXT,
            remind_time TIMESTAMPTZ,
            repeat TEXT
        )
    ''')

# === РАБОТА С НАПОМИНАНИЯМИ ===
async def load_reminders():
    async with db_pool.acquire() as conn:
        return await conn.fetch("SELECT * FROM reminders ORDER BY remind_time")

async def save_reminder(user_id, message, remind_time, repeat):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO reminders (user_id, message, remind_time, repeat) VALUES ($1, $2, $3, $4)",
            user_id, message, remind_time, repeat
        )

async def delete_reminder_by_id(reminder_id):
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM reminders WHERE id = $1", reminder_id)

# === КНОПКИ ===
def get_main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="➕ Новое напоминание")],
            [KeyboardButton(text="📋 Мои напоминания")],
        ],
        resize_keyboard=True
    )

REPEAT_TYPES = {
    "daily": "🔁 Ежедневно",
    "weekly": "📅 Еженедельно",
    "monthly": "🗓️ Ежемесячно",
    "none": "🚫 Без повтора"
}

# === /start ===
@dp.message(Command("start"))
async def start(message: types.Message):
    await message.answer(
        "👋 Привет! Я бот-напоминалка с базой данных. Напоминания не пропадут!",
        reply_markup=get_main_keyboard()
    )

# === НОВОЕ НАПОМИНАНИЕ ===
user_state = {}

@dp.message(lambda m: m.text == "➕ Новое напоминание")
async def start_remind(message: types.Message):
    user_state[message.from_user.id] = {"step": "waiting_message"}
    await message.answer("📝 Введи сообщение:")

@dp.message(lambda m: user_state.get(m.from_user.id, {}).get("step") == "waiting_message")
async def get_message(message: types.Message):
    text = message.text.strip()
    if not text:
        await message.answer("❌ Пусто.")
        return
    user_id = message.from_user.id
    user_state[user_id] = {"step": "waiting_time", "message": text}
    await message.answer("⏰ Введи время (чч:мм), например: 15:30")

@dp.message(lambda m: user_state.get(m.from_user.id, {}).get("step") == "waiting_time")
async def get_time(message: types.Message):
    user_id = message.from_user.id
    try:
        h, m = map(int, message.text.split(":"))
        now = datetime.now()
        time = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if time < now:
            time += timedelta(days=1)

        user_state[user_id]["step"] = "waiting_repeat"
        user_state[user_id]["remind_time"] = time

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=REPEAT_TYPES["none"], callback_data="repeat_none")],
            [InlineKeyboardButton(text=REPEAT_TYPES["daily"], callback_data="repeat_daily")],
            [InlineKeyboardButton(text=REPEAT_TYPES["weekly"], callback_data="repeat_weekly")],
            [InlineKeyboardButton(text=REPEAT_TYPES["monthly"], callback_data="repeat_monthly")]
        ])
        await message.answer("🔁 Повтор:", reply_markup=kb)
    except:
        await message.answer("❌ Формат: чч:мм")

@dp.callback_query(lambda c: c.data.startswith("repeat_"))
async def set_repeat(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    data = user_state.get(user_id)
    if not data or data["step"] != "waiting_repeat":
        await callback.answer("❌ Сессия устарела.")
        return

    repeat = callback.data.replace("repeat_", "")
    await save_reminder(
        user_id=user_id,
        message=data["message"],
        remind_time=data["remind_time"],
        repeat=repeat
    )
    await callback.message.edit_text(f"✅ Напоминание добавлено!\n💬 {data['message']}\n⏰ {data['remind_time'].strftime('%d.%m %H:%M')}")
    user_state[user_id] = None
    await callback.answer()

# === ПОКАЗАТЬ НАПОМИНАНИЯ ===
@dp.message(lambda m: m.text == "📋 Мои напоминания")
async def show_reminders(message: types.Message):
    user_id = message.from_user.id
    rows = await db_pool.fetch("SELECT id, message, remind_time, repeat FROM reminders WHERE user_id = $1", user_id)
    
    if not rows:
        await message.answer("📌 Нет напоминаний.")
        return

    for row in rows:
        time_str = row["remind_time"].strftime("%d.%m %H:%M")
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Удалить", callback_data=f"delete_{row['id']}")]
        ])
        await message.answer(
            f"🔔 {row['message']}\n⏰ {time_str}\n🔄 {REPEAT_TYPES.get(row['repeat'], 'Без')}",
            reply_markup=kb
        )

# === УДАЛЕНИЕ ===
@dp.callback_query(lambda c: c.data.startswith("delete_"))
async def delete_rem(callback: types.CallbackQuery):
    rem_id = int(callback.data.split("_")[1])
    await delete_reminder_by_id(rem_id)
    await callback.answer("✅ Удалено")
    await callback.message.edit_text("❌ Удалено")

# === ФОН: ПРОВЕРКА НАПОМИНАНИЙ ===
async def check_reminders():
    while True:
        now = datetime.now()
        rows = await db_pool.fetch("SELECT * FROM reminders WHERE remind_time <= $1", now)
        for row in rows:
            try:
                await bot.send_message(row["user_id"], f"🔔 {row['message']}")
            except Exception as e:
                print(f"Ошибка: {e}")
                continue

            # Удаляем
            await delete_reminder_by_id(row["id"])

            # Повтор
            new_time = None
            if row["repeat"] == "daily":
                new_time = now + timedelta(days=1)
            elif row["repeat"] == "weekly":
                new_time = now + timedelta(weeks=1)
            elif row["repeat"] == "monthly":
                new_time = now + timedelta(days=30)

            if new_time:
                await save_reminder(row["user_id"], row["message"], new_time, row["repeat"])

        await asyncio.sleep(10)

# === ЗАПУСК ===
async def main():
    await init_db()  # Подключаем БД
    asyncio.create_task(check_reminders())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

