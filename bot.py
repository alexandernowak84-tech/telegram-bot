import os
import re
import sqlite3
from io import BytesIO
from datetime import datetime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
import matplotlib.pyplot as plt

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# =========================
# 1. НАСТРОЙКИ
# =========================

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
BOT_TIMEZONE = os.getenv("BOT_TIMEZONE", "Europe/Berlin").strip()
ALLOWED_USER_IDS_RAW = os.getenv("ALLOWED_USER_IDS", "").strip()

if not BOT_TOKEN:
    raise ValueError("В .env не найден BOT_TOKEN")

if not ALLOWED_USER_IDS_RAW:
    raise ValueError("В .env не найден ALLOWED_USER_IDS")

ALLOWED_USER_IDS = set()
for item in ALLOWED_USER_IDS_RAW.split(","):
    item = item.strip()
    if item.isdigit():
        ALLOWED_USER_IDS.add(int(item))

if len(ALLOWED_USER_IDS) < 1:
    raise ValueError("ALLOWED_USER_IDS пустой или заполнен неправильно")

TZ = ZoneInfo(BOT_TIMEZONE)
DB_NAME = "accounting.db"


# =========================
# 2. БАЗА ДАННЫХ
# =========================

def get_connection():
    return sqlite3.connect(DB_NAME)


def init_db():
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS operations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT,
            full_name TEXT,
            op_type TEXT NOT NULL,      -- income / expense / withdraw
            amount REAL NOT NULL,
            currency TEXT DEFAULT 'EUR',
            comment TEXT DEFAULT '',
            created_at TEXT NOT NULL
        )
    """)

    conn.commit()
    conn.close()


# =========================
# 3. КНОПКИ
# =========================

def main_keyboard():
    keyboard = [
        [KeyboardButton("➕ Доход"), KeyboardButton("➖ Расход")],
        [KeyboardButton("💶 Снял наличные"), KeyboardButton("📊 Баланс")],
        [KeyboardButton("🧾 История"), KeyboardButton("📅 Итог за месяц")],
        [KeyboardButton("📈 График"), KeyboardButton("ℹ️ Помощь")],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


# =========================
# 4. ПРОВЕРКА ДОСТУПА
# =========================

def is_allowed(update: Update) -> bool:
    user = update.effective_user
    if not user:
        return False
    return user.id in ALLOWED_USER_IDS


async def access_denied(update: Update):
    await update.message.reply_text(
        "⛔ У тебя нет доступа к этому боту.",
        reply_markup=main_keyboard()
    )


# =========================
# 5. ПОЛЕЗНЫЕ ФУНКЦИИ
# =========================

def now_str():
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")


def get_user_display_name(user) -> str:
    if user.full_name:
        return user.full_name
    if user.username:
        return user.username
    return str(user.id)


def add_operation(user_id: int, username: str, full_name: str,
                  op_type: str, amount: float,
                  currency: str = "EUR", comment: str = ""):
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO operations (
            user_id, username, full_name, op_type, amount, currency, comment, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (user_id, username, full_name, op_type, amount, currency, comment, now_str()))

    conn.commit()
    conn.close()


def get_balance():
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
        SELECT
            COALESCE(SUM(CASE WHEN op_type='income' THEN amount ELSE 0 END), 0),
            COALESCE(SUM(CASE WHEN op_type='expense' THEN amount ELSE 0 END), 0),
            COALESCE(SUM(CASE WHEN op_type='withdraw' THEN amount ELSE 0 END), 0)
        FROM operations
    """)
    income, expense, withdraw = cur.fetchone()
    conn.close()

    balance = income - expense - withdraw
    return income, expense, withdraw, balance


def get_month_summary(year: int, month: int):
    month_prefix = f"{year:04d}-{month:02d}-"

    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
        SELECT
            COALESCE(SUM(CASE WHEN op_type='income' THEN amount ELSE 0 END), 0),
            COALESCE(SUM(CASE WHEN op_type='expense' THEN amount ELSE 0 END), 0),
            COALESCE(SUM(CASE WHEN op_type='withdraw' THEN amount ELSE 0 END), 0)
        FROM operations
        WHERE created_at LIKE ?
    """, (f"{month_prefix}%",))

    income, expense, withdraw = cur.fetchone()
    conn.close()

    balance = income - expense - withdraw
    return income, expense, withdraw, balance


def get_recent_history(limit: int = 20):
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
        SELECT created_at, full_name, username, op_type, amount, currency, comment
        FROM operations
        ORDER BY id DESC
        LIMIT ?
    """, (limit,))

    rows = cur.fetchall()
    conn.close()
    return rows


def get_all_months_data():
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
        SELECT
            substr(created_at, 1, 7) as ym,
            COALESCE(SUM(CASE WHEN op_type='income' THEN amount ELSE 0 END), 0) as income,
            COALESCE(SUM(CASE WHEN op_type='expense' THEN amount ELSE 0 END), 0) as expense,
            COALESCE(SUM(CASE WHEN op_type='withdraw' THEN amount ELSE 0 END), 0) as withdraw
        FROM operations
        GROUP BY ym
        ORDER BY ym
    """)

    rows = cur.fetchall()
    conn.close()
    return rows


def format_op_type(op_type: str) -> str:
    if op_type == "income":
        return "Доход"
    if op_type == "expense":
        return "Расход"
    if op_type == "withdraw":
        return "Снятие"
    return op_type


# =========================
# 6. ПАРСИНГ ТЕКСТА
# =========================

def parse_operation(text: str):
    text = text.strip().lower()

    # +1000
    m = re.fullmatch(r"\+\s*(\d+(?:[.,]\d+)?)", text)
    if m:
        amount = float(m.group(1).replace(",", "."))
        return {
            "op_type": "income",
            "amount": amount,
            "currency": "EUR",
            "comment": ""
        }

    # -1000
    m = re.fullmatch(r"\-\s*(\d+(?:[.,]\d+)?)", text)
    if m:
        amount = float(m.group(1).replace(",", "."))
        return {
            "op_type": "expense",
            "amount": amount,
            "currency": "EUR",
            "comment": ""
        }

    # снял 1000 евро
    m = re.fullmatch(
        r"(снял|сняла|снятие)\s+(\d+(?:[.,]\d+)?)(?:\s*(евро|eur|€|usd|доллар|доллара|\$|грн|uah))?",
        text
    )
    if m:
        amount = float(m.group(2).replace(",", "."))
        curr = m.group(3) or "EUR"
        currency = normalize_currency(curr)
        return {
            "op_type": "withdraw",
            "amount": amount,
            "currency": currency,
            "comment": "Снятие наличных"
        }

    # доход 1000 аренда
    m = re.fullmatch(r"(доход|приход)\s+(\d+(?:[.,]\d+)?)(?:\s+(.+))?", text)
    if m:
        amount = float(m.group(2).replace(",", "."))
        comment = m.group(3) or ""
        return {
            "op_type": "income",
            "amount": amount,
            "currency": "EUR",
            "comment": comment
        }

    # расход 500 продукты
    m = re.fullmatch(r"(расход|трата)\s+(\d+(?:[.,]\d+)?)(?:\s+(.+))?", text)
    if m:
        amount = float(m.group(2).replace(",", "."))
        comment = m.group(3) or ""
        return {
            "op_type": "expense",
            "amount": amount,
            "currency": "EUR",
            "comment": comment
        }

    return None


def normalize_currency(curr: str) -> str:
    curr = curr.lower()
    if curr in ["евро", "eur", "€"]:
        return "EUR"
    if curr in ["usd", "доллар", "доллара", "$"]:
        return "USD"
    if curr in ["грн", "uah"]:
        return "UAH"
    return curr.upper()


# =========================
# 7. КОМАНДЫ
# =========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return await access_denied(update)

    user = update.effective_user
    await update.message.reply_text(
        f"Привет, {get_user_display_name(user)} 👋\n\n"
        "Я общий бухгалтерический бот для 2 человек.\n"
        "Я считаю всё вместе.\n\n"
        "Примеры:\n"
        "+1000\n"
        "-250\n"
        "снял 300 евро\n"
        "доход 1500 зарплата\n"
        "расход 70 бензин\n\n"
        "Также можно пользоваться кнопками.",
        reply_markup=main_keyboard()
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return await access_denied(update)

    text = (
        "ℹ️ Как писать:\n\n"
        "1. Доход:\n"
        "   +1000\n"
        "   доход 1000 зарплата\n\n"
        "2. Расход:\n"
        "   -500\n"
        "   расход 500 продукты\n\n"
        "3. Снятие наличных:\n"
        "   снял 100 евро\n\n"
        "Кнопки:\n"
        "📊 Баланс — общий итог\n"
        "🧾 История — последние записи\n"
        "📅 Итог за месяц — итог текущего месяца\n"
        "📈 График — график по всем месяцам\n\n"
        "Важно: данные общие для 2 разрешённых пользователей."
    )
    await update.message.reply_text(text, reply_markup=main_keyboard())


async def show_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return await access_denied(update)

    income, expense, withdraw, balance = get_balance()

    text = (
        "📊 Общий баланс от 2 пользователей:\n\n"
        f"➕ Доходы: {income:.2f}\n"
        f"➖ Расходы: {expense:.2f}\n"
        f"💶 Снято: {withdraw:.2f}\n"
        f"🟰 Остаток: {balance:.2f}"
    )
    await update.message.reply_text(text, reply_markup=main_keyboard())


async def show_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return await access_denied(update)

    rows = get_recent_history(20)

    if not rows:
        return await update.message.reply_text(
            "🧾 История пока пустая.",
            reply_markup=main_keyboard()
        )

    lines = ["🧾 Последние 20 записей:\n"]
    for created_at, full_name, username, op_type, amount, currency, comment in rows:
        person = full_name or username or "Без имени"
        line = f"{created_at} | {person} | {format_op_type(op_type)} | {amount:.2f} {currency}"
        if comment:
            line += f" | {comment}"
        lines.append(line)

    await update.message.reply_text(
        "\n".join(lines),
        reply_markup=main_keyboard()
    )


async def show_month_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return await access_denied(update)

    now = datetime.now(TZ)
    income, expense, withdraw, balance = get_month_summary(now.year, now.month)

    text = (
        f"📅 Итог за {now.strftime('%m.%Y')}:\n\n"
        f"➕ Доходы: {income:.2f}\n"
        f"➖ Расходы: {expense:.2f}\n"
        f"💶 Снято: {withdraw:.2f}\n"
        f"🟰 Итог месяца: {balance:.2f}"
    )
    await update.message.reply_text(text, reply_markup=main_keyboard())


def build_chart():
    rows = get_all_months_data()

    if not rows:
        return None

    months = []
    incomes = []
    expenses = []
    withdraws = []

    for ym, income, expense, withdraw in rows:
        months.append(ym)
        incomes.append(income)
        expenses.append(expense)
        withdraws.append(withdraw)

    plt.figure(figsize=(12, 6))
    plt.plot(months, incomes, marker='o', label='Доходы')
    plt.plot(months, expenses, marker='o', label='Расходы')
    plt.plot(months, withdraws, marker='o', label='Снятия')
    plt.xticks(rotation=45)
    plt.title("Доходы, расходы и снятия по месяцам")
    plt.xlabel("Месяц")
    plt.ylabel("Сумма")
    plt.legend()
    plt.tight_layout()

    bio = BytesIO()
    bio.name = "chart.png"
    plt.savefig(bio, format="png")
    plt.close()
    bio.seek(0)
    return bio


async def show_chart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return await access_denied(update)

    chart = build_chart()
    if not chart:
        return await update.message.reply_text(
            "📈 Пока нет данных для графика.",
            reply_markup=main_keyboard()
        )

    await update.message.reply_photo(
        photo=chart,
        caption="📈 График по всем месяцам",
        reply_markup=main_keyboard()
    )


# =========================
# 8. АВТО-ИТОГ В НАЧАЛЕ МЕСЯЦА
# =========================

async def monthly_report_job(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now(TZ)

    # бот сработает 1 числа, покажем итог прошлого месяца
    year = now.year
    month = now.month - 1
    if month == 0:
        month = 12
        year -= 1

    income, expense, withdraw, balance = get_month_summary(year, month)

    text = (
        f"📅 Автоитог за {month:02d}.{year}:\n\n"
        f"➕ Доходы: {income:.2f}\n"
        f"➖ Расходы: {expense:.2f}\n"
        f"💶 Снято: {withdraw:.2f}\n"
        f"🟰 Итог месяца: {balance:.2f}"
    )

    for user_id in ALLOWED_USER_IDS:
        try:
            await context.bot.send_message(chat_id=user_id, text=text)
        except Exception as e:
            print(f"Не удалось отправить автоотчёт пользователю {user_id}: {e}")


# =========================
# 9. ОБРАБОТКА ТЕКСТА И КНОПОК
# =========================

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return await access_denied(update)

    text = (update.message.text or "").strip()
    user = update.effective_user

    # Кнопки
    if text == "➕ Доход":
        return await update.message.reply_text(
            "Напиши так:\n+1000\nили\nдоход 1000 зарплата",
            reply_markup=main_keyboard()
        )

    if text == "➖ Расход":
        return await update.message.reply_text(
            "Напиши так:\n-500\nили\nрасход 500 продукты",
            reply_markup=main_keyboard()
        )

    if text == "💶 Снял наличные":
        return await update.message.reply_text(
            "Напиши так:\nснял 100 евро",
            reply_markup=main_keyboard()
        )

    if text == "📊 Баланс":
        return await show_balance(update, context)

    if text == "🧾 История":
        return await show_history(update, context)

    if text == "📅 Итог за месяц":
        return await show_month_summary(update, context)

    if text == "📈 График":
        return await show_chart(update, context)

    if text == "ℹ️ Помощь":
        return await help_command(update, context)

    # Обычный ввод
    parsed = parse_operation(text)
    if not parsed:
        return await update.message.reply_text(
            "Я не понял запись 😕\n\n"
            "Примеры:\n"
            "+1000\n"
            "-300\n"
            "снял 100 евро\n"
            "доход 1500 зарплата\n"
            "расход 25 кофе",
            reply_markup=main_keyboard()
        )

    add_operation(
        user_id=user.id,
        username=user.username or "",
        full_name=user.full_name or "",
        op_type=parsed["op_type"],
        amount=parsed["amount"],
        currency=parsed["currency"],
        comment=parsed["comment"]
    )

    income, expense, withdraw, balance = get_balance()

    await update.message.reply_text(
        "✅ Записал:\n"
        f"👤 Кто: {get_user_display_name(user)}\n"
        f"📌 Тип: {format_op_type(parsed['op_type'])}\n"
        f"💰 Сумма: {parsed['amount']:.2f} {parsed['currency']}\n"
        f"🕒 Дата: {now_str()}\n\n"
        f"📊 Общий остаток теперь: {balance:.2f}",
        reply_markup=main_keyboard()
    )


# =========================
# 10. ЗАПУСК
# =========================

def main():
    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Автоитог каждый месяц 1 числа в 09:00
    app.job_queue.run_monthly(
        monthly_report_job,
        when=datetime.strptime("09:00", "%H:%M").time(),
        day=1
    )

    print("Бот запущен...")
    app.run_polling()


if __name__ == "__main__":
    main()