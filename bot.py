import os
import re
import logging
import requests
from datetime import datetime
from dotenv import load_dotenv

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, TypeHandler, ApplicationHandlerStop,
    filters, ContextTypes, PicklePersistence,
)

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

TOKEN = os.getenv("BOT_TOKEN")
SCRIPT_URL = os.getenv("SCRIPT_URL")
PASSWORD = os.getenv("BOT_PASSWORD", "33555")

# Кто ввёл пароль — храним в памяти, поэтому после перезапуска бота
# авторизация сбрасывается и пароль требуется заново
AUTHORIZED: set[int] = set()

CATEGORIES = [
    "Расход фирмы", "Офис", "ЗП Егор", "ЗП Гриша",
    "ЗП Степанов", "ЗП Нечаев", "ЗП Мастер",
    "Восполняемые расходы", "Бонус", "Не учтенные", "Оттингер", "Приход",
]
INCOME = "Приход"

RU_MONTHS = [
    "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
]


def current_month() -> str:
    return RU_MONTHS[datetime.now().month - 1]


(CHOOSING, AWAIT_FIO, AWAIT_AMOUNT, AWAIT_COMMENT,
 DEL_SECTION, DEL_ROW) = range(6)


# ── Клавиатуры ───────────────────────────────────────────────────────────────

def categories_keyboard() -> InlineKeyboardMarkup:
    kb, row = [], []
    for i, name in enumerate(CATEGORIES):
        if name == INCOME:
            continue
        # у категорий без своего эмодзи — значок 📝
        row.append(InlineKeyboardButton(f"📝 {name}", callback_data=f"cat:{i}"))
        if len(row) == 2:
            kb.append(row)
            row = []
    if row:
        kb.append(row)
    kb.append([InlineKeyboardButton("💰 Приход", callback_data=f"cat:{CATEGORIES.index(INCOME)}")])
    kb.append([InlineKeyboardButton("🗑 Удалить отчёт 🗑", callback_data="delete")])
    return InlineKeyboardMarkup(kb)


def delete_section_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("📝 Расход", callback_data="del:expense"),
        InlineKeyboardButton("💰 Приход", callback_data="del:income"),
    ]])


# ── Утилиты ──────────────────────────────────────────────────────────────────

def extract_sheet_id(url: str) -> str | None:
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", url)
    return m.group(1) if m else None


def call_script(payload: dict) -> dict:
    r = requests.post(SCRIPT_URL, json=payload, timeout=25)
    try:
        return r.json()
    except ValueError:
        snippet = r.text[:300].replace("\n", " ").strip()
        raise RuntimeError(f"Apps Script вернул не JSON (HTTP {r.status_code}): {snippet}")


async def show_menu(message, text: str = "Выберите категорию:") -> None:
    await message.reply_text(text, reply_markup=categories_keyboard())


# ── /start ───────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not context.bot_data.get("sheet_id"):
        await update.message.reply_text(
            "👋 Добро пожаловать!\n\n"
            "Для начала работы привяжите Google Таблицу:\n"
            "/connect <ссылка на таблицу>"
        )
        return ConversationHandler.END
    await show_menu(update.message)
    return CHOOSING


# ── /connect ─────────────────────────────────────────────────────────────────

async def cmd_connect(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not context.args:
        await update.message.reply_text("Укажите ссылку:\n/connect <ссылка>")
        return ConversationHandler.END

    sheet_id = extract_sheet_id(context.args[0])
    if not sheet_id:
        await update.message.reply_text(
            "❌ Не удалось распознать ссылку.\n"
            "Скопируйте её из адресной строки Google Таблицы."
        )
        return ConversationHandler.END

    month = current_month()
    try:
        # connect создаёт лист текущего месяца, если его ещё нет (данные не трогает)
        result = call_script({"sheetId": sheet_id, "action": "connect", "month": month})
    except Exception as e:
        logging.error(f"call_script error: {e}")
        await update.message.reply_text(f"❌ Ошибка подключения:\n{str(e)[:400]}")
        return ConversationHandler.END

    if "error" in result:
        await update.message.reply_text(f"❌ {result['error']}")
        return ConversationHandler.END

    context.bot_data["sheet_id"] = sheet_id
    await update.message.reply_text(f"✅ Таблица подключена.\nТекущий лист: {month}")
    await show_menu(update.message)
    return CHOOSING


# ── Выбор категории (инлайн) ─────────────────────────────────────────────────

async def category_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if not context.bot_data.get("sheet_id"):
        await query.edit_message_text("Сначала привяжите таблицу: /connect <ссылка>")
        return ConversationHandler.END

    idx = int(query.data.split(":")[1])
    category = CATEGORIES[idx]
    context.user_data.clear()
    context.user_data["category"] = category

    await query.edit_message_text(f"📂 {category}\n\n✏️ Введите ФИО:")
    return AWAIT_FIO


async def got_fio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["fio"] = update.message.text.strip()
    await update.message.reply_text("💰 Введите сумму (числом):")
    return AWAIT_AMOUNT


async def got_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = update.message.text.strip().replace(",", ".").replace(" ", "")
    try:
        context.user_data["amount"] = float(raw)
    except ValueError:
        await update.message.reply_text("❌ Нужно число. Например: 1500\n\nВведите сумму:")
        return AWAIT_AMOUNT
    await update.message.reply_text("📝 Введите комментарий:")
    return AWAIT_COMMENT


async def got_comment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    ud = context.user_data
    sheet_id = context.bot_data["sheet_id"]
    category = ud["category"]
    fio = ud["fio"]
    amount = ud["amount"]
    comment = update.message.text.strip()
    now = datetime.now()
    date = now.strftime("%d.%m.%Y")
    month = current_month()

    try:
        if category == INCOME:
            call_script({
                "sheetId": sheet_id, "action": "write_income", "month": month,
                "data": {"date": date, "fio": fio, "amount": amount, "comment": comment},
            })
            text = (
                f"✅ Приход записан\n\n"
                f"📅 {date}\n👤 {fio}\n💰 {amount:g} ₽\n💬 {comment}"
            )
        else:
            call_script({
                "sheetId": sheet_id, "action": "write_expense", "month": month,
                "data": {"date": date, "fio": fio, "amount": amount,
                         "comment": comment, "category": category},
            })
            text = (
                f"✅ Записано\n\n"
                f"📅 {date}\n📂 {category}\n👤 {fio}\n💰 {amount:g} ₽\n💬 {comment}"
            )
    except Exception as e:
        logging.error(e)
        text = "❌ Ошибка при записи в таблицу. Попробуйте ещё раз."

    await update.message.reply_text(text)
    await show_menu(update.message)
    return CHOOSING


# ── Удаление отчёта ──────────────────────────────────────────────────────────

async def start_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if not context.bot_data.get("sheet_id"):
        await query.edit_message_text("Сначала привяжите таблицу: /connect <ссылка>")
        return ConversationHandler.END
    await query.edit_message_text(
        "🗑 Из какой таблицы удалить запись?",
        reply_markup=delete_section_keyboard(),
    )
    return DEL_SECTION


async def delete_section_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    section = query.data.split(":")[1]  # expense | income
    context.user_data["del_section"] = section
    name = "Расход" if section == "expense" else "Приход"
    await query.edit_message_text(f"🗑 {name}\n\nОтправьте № записи, которую нужно удалить:")
    return DEL_ROW


async def delete_row_got(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = update.message.text.strip()
    if not raw.isdigit():
        await update.message.reply_text("❌ Нужен номер (число). Отправьте № записи:")
        return DEL_ROW

    num = int(raw)
    section = context.user_data.get("del_section", "expense")
    sheet_id = context.bot_data["sheet_id"]

    try:
        result = call_script({
            "sheetId": sheet_id, "action": "delete", "month": current_month(),
            "data": {"section": section, "num": num},
        })
    except Exception as e:
        logging.error(e)
        await update.message.reply_text("❌ Ошибка при удалении.")
        await show_menu(update.message)
        return CHOOSING

    if result.get("deleted"):
        await update.message.reply_text(f"✅ Запись №{num} удалена, нумерация обновлена.")
    else:
        await update.message.reply_text(f"❌ Запись №{num} не найдена.")
    await show_menu(update.message)
    return CHOOSING


async def auth_gate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Пропускает дальше только авторизованных. Иначе просит пароль."""
    user = update.effective_user
    if user is None:
        return
    if user.id in AUTHORIZED:
        return  # уже вошёл — пропускаем к обычным обработчикам

    msg = update.effective_message
    text = (msg.text or "").strip() if (msg and msg.text) else ""

    if text == PASSWORD:
        AUTHORIZED.add(user.id)
        if context.bot_data.get("sheet_id"):
            await msg.reply_text("✅ Доступ разрешён.")
            await show_menu(msg)
        else:
            await msg.reply_text("✅ Доступ разрешён.\n\nПривяжите таблицу:\n/connect <ссылка>")
        raise ApplicationHandlerStop

    # не авторизован и это не пароль — блокируем и просим пароль
    if update.callback_query:
        await update.callback_query.answer("🔒 Сначала введите пароль", show_alert=True)
    elif msg:
        await msg.reply_text("🔒 Доступ к боту закрыт.\nВведите пароль:")
    raise ApplicationHandlerStop


async def ignore_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    pass  # тихо игнорируем неизвестные команды


# ── Настройка бота ───────────────────────────────────────────────────────────

async def post_init(app: Application) -> None:
    await app.bot.set_my_commands([
        BotCommand("start", "Открыть меню"),
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
    cat_h = CallbackQueryHandler(category_chosen, pattern=r"^cat:")
    del_h = CallbackQueryHandler(start_delete, pattern=r"^delete$")

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", cmd_start),
            connect_h,
            cat_h,
            del_h,
        ],
        states={
            CHOOSING: [connect_h, cat_h, del_h],
            AWAIT_FIO: [connect_h, MessageHandler(filters.TEXT & ~filters.COMMAND, got_fio)],
            AWAIT_AMOUNT: [connect_h, MessageHandler(filters.TEXT & ~filters.COMMAND, got_amount)],
            AWAIT_COMMENT: [connect_h, MessageHandler(filters.TEXT & ~filters.COMMAND, got_comment)],
            DEL_SECTION: [connect_h, CallbackQueryHandler(delete_section_chosen, pattern=r"^del:")],
            DEL_ROW: [connect_h, MessageHandler(filters.TEXT & ~filters.COMMAND, delete_row_got)],
        },
        fallbacks=[
            CommandHandler("start", cmd_start),
            MessageHandler(filters.COMMAND, ignore_cmd),
        ],
        persistent=True,
        name="main_conv",
    )

    # Пароль на вход — проверяется раньше всех остальных обработчиков
    app.add_handler(TypeHandler(Update, auth_gate), group=-1)

    app.add_handler(conv)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
