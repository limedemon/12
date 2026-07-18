import os
import re
import logging
import requests
from datetime import datetime
from dotenv import load_dotenv

from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, BotCommand
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ConversationHandler,
    filters, ContextTypes, PicklePersistence,
)

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

TOKEN = os.getenv("BOT_TOKEN")
SCRIPT_URL = os.getenv("SCRIPT_URL")

CATEGORIES = {
    "Расход фирмы", "Офис", "ЗП Егор", "ЗП Гриша",
    "ЗП Степанов", "ЗП Нечаев", "ЗП Мастер",
    "Восполняемые расходы", "Бонус", "Не учтенные", "Оттингер", "Приход",
}

CHOOSING, AWAIT_FIO, AWAIT_AMOUNT, AWAIT_COMMENT, CONFIRM_FORMAT = range(5)


def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            ["Расход фирмы", "Офис"],
            ["ЗП Егор", "ЗП Гриша"],
            ["ЗП Степанов", "ЗП Нечаев"],
            ["ЗП Мастер", "Восполняемые расходы"],
            ["Бонус", "Не учтенные"],
            ["Оттингер", "Приход"],
        ],
        resize_keyboard=True,
    )


def extract_sheet_id(url: str) -> str | None:
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", url)
    return m.group(1) if m else None


def call_script(payload: dict) -> dict:
    r = requests.post(SCRIPT_URL, json=payload, timeout=25)
    return r.json()


# ── Handlers ────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not context.chat_data.get("sheet_id"):
        await update.message.reply_text(
            "👋 *Добро пожаловать!*\n\n"
            "Для начала работы привяжите Google Таблицу:\n"
            "`/connect <ссылка на таблицу>`",
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    await update.message.reply_text("Выберите категорию:", reply_markup=main_keyboard())
    return CHOOSING


async def cmd_connect(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not context.args:
        await update.message.reply_text(
            "Укажите ссылку на таблицу:\n`/connect <ссылка>`",
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    sheet_id = extract_sheet_id(context.args[0])
    if not sheet_id:
        await update.message.reply_text(
            "❌ Не удалось распознать ссылку.\n"
            "Скопируйте её прямо из адресной строки Google Таблицы."
        )
        return ConversationHandler.END

    try:
        result = call_script({"sheetId": sheet_id, "action": "check"})
    except Exception:
        await update.message.reply_text(
            "❌ Нет доступа к таблице.\n"
            "Убедитесь, что таблица открыта для редактирования аккаунту, "
            "с которого был задеплоен Apps Script."
        )
        return ConversationHandler.END

    if "error" in result:
        await update.message.reply_text(f"❌ {result['error']}")
        return ConversationHandler.END

    context.chat_data["pending_sheet_id"] = sheet_id

    if result.get("isEmpty"):
        return await _format_and_connect(update, context, sheet_id)

    await update.message.reply_text(
        "⚠️ *Таблица уже содержит данные*\n\n"
        "Форматирование *удалит всё содержимое* и создаст нужную структуру.\n\n"
        "Продолжить? Напишите *да* или *нет*",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove(),
    )
    return CONFIRM_FORMAT


async def _format_and_connect(update, context, sheet_id) -> int:
    try:
        call_script({"sheetId": sheet_id, "action": "format"})
        context.chat_data["sheet_id"] = sheet_id
        await update.message.reply_text(
            "✅ *Таблица подключена и отформатирована!*\n\nВыберите категорию:",
            parse_mode="Markdown",
            reply_markup=main_keyboard(),
        )
        return CHOOSING
    except Exception:
        await update.message.reply_text("❌ Ошибка при форматировании таблицы.")
        return ConversationHandler.END


async def confirm_format(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip().lower()
    sheet_id = context.chat_data.get("pending_sheet_id")

    if text in ("да", "д", "yes", "y"):
        return await _format_and_connect(update, context, sheet_id)

    kb = main_keyboard() if context.chat_data.get("sheet_id") else ReplyKeyboardRemove()
    await update.message.reply_text("Подключение отменено.", reply_markup=kb)
    return CHOOSING if context.chat_data.get("sheet_id") else ConversationHandler.END


async def category_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text

    if text not in CATEGORIES:
        return CHOOSING

    if not context.chat_data.get("sheet_id"):
        await update.message.reply_text("Сначала привяжите таблицу: /connect <ссылка>")
        return CHOOSING

    context.user_data.update({"category": text, "fio": None, "amount": None})
    await update.message.reply_text("✏️ Введите ФИО:", reply_markup=ReplyKeyboardRemove())
    return AWAIT_FIO


async def got_fio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["fio"] = update.message.text.strip()
    await update.message.reply_text("💰 Введите сумму (числом):")
    return AWAIT_AMOUNT


async def got_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = update.message.text.strip().replace(",", ".").replace(" ", "")
    try:
        context.user_data["amount"] = float(raw)
        await update.message.reply_text("📝 Введите комментарий:")
        return AWAIT_COMMENT
    except ValueError:
        await update.message.reply_text(
            "❌ Нужно число. Например: *1500* или *2500.50*\n\nВведите сумму:",
            parse_mode="Markdown",
        )
        return AWAIT_AMOUNT


async def got_comment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    ud = context.user_data
    sheet_id = context.chat_data["sheet_id"]
    category = ud["category"]
    fio = ud["fio"]
    amount = ud["amount"]
    comment = update.message.text.strip()
    date = datetime.now().strftime("%d.%m.%Y")

    try:
        if category == "Приход":
            call_script({
                "sheetId": sheet_id,
                "action": "write_income",
                "data": {"date": date, "fio": fio, "amount": amount, "comment": comment},
            })
            text = (
                f"✅ *Приход записан*\n\n"
                f"📅 {date}\n"
                f"👤 {fio}\n"
                f"💰 {amount:g} ₽\n"
                f"💬 {comment}"
            )
        else:
            call_script({
                "sheetId": sheet_id,
                "action": "write_expense",
                "data": {
                    "date": date, "fio": fio, "amount": amount,
                    "comment": comment, "category": category,
                },
            })
            text = (
                f"✅ *Записано*\n\n"
                f"📅 {date}\n"
                f"📂 {category}\n"
                f"👤 {fio}\n"
                f"💰 {amount:g} ₽\n"
                f"💬 {comment}"
            )
    except Exception as e:
        logging.error(e)
        text = "❌ Ошибка при записи в таблицу. Попробуйте ещё раз."

    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=main_keyboard())
    return CHOOSING


async def ignore_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pass  # silently drop unknown commands


# ── Bot setup ────────────────────────────────────────────────────────────────

async def post_init(app: Application) -> None:
    await app.bot.set_my_commands([
        BotCommand("start", "Открыть главное меню"),
        BotCommand("connect", "Привязать Google Таблицу"),
    ])


def main() -> None:
    persistence = PicklePersistence(filepath="bot_data.pkl")
    app = (
        Application.builder()
        .token(TOKEN)
        .persistence(persistence)
        .post_init(post_init)
        .build()
    )

    connect_h = CommandHandler("connect", cmd_connect)

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", cmd_start),
            connect_h,
            MessageHandler(filters.TEXT & ~filters.COMMAND, category_chosen),
        ],
        states={
            CHOOSING: [
                connect_h,
                MessageHandler(filters.TEXT & ~filters.COMMAND, category_chosen),
            ],
            CONFIRM_FORMAT: [
                connect_h,
                MessageHandler(filters.TEXT & ~filters.COMMAND, confirm_format),
            ],
            AWAIT_FIO: [
                connect_h,
                MessageHandler(filters.TEXT & ~filters.COMMAND, got_fio),
            ],
            AWAIT_AMOUNT: [
                connect_h,
                MessageHandler(filters.TEXT & ~filters.COMMAND, got_amount),
            ],
            AWAIT_COMMENT: [
                connect_h,
                MessageHandler(filters.TEXT & ~filters.COMMAND, got_comment),
            ],
        },
        fallbacks=[
            CommandHandler("start", cmd_start),
            MessageHandler(filters.COMMAND, ignore_cmd),
        ],
        persistent=True,
        name="main_conv",
    )

    app.add_handler(conv)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
