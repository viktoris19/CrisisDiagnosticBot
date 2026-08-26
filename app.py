import os
import logging
import sqlite3
import threading
import asyncio
import csv
import io
from datetime import datetime
from flask import Flask, jsonify
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

# ======================================================
# 1. КОНФИГУРАЦИЯ
# ======================================================

# Токен берем из переменных окружения (безопасно для сервера)
TOKEN = os.environ.get("TELEGRAM_TOKEN")
if not TOKEN:
    raise ValueError("❌ Ошибка: переменная TELEGRAM_TOKEN не установлена!")

ADMIN_IDS = [866350593]  # Ваш Telegram ID
DB_NAME = "diagnostics.db"

# Включим логирование для отладки
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ======================================================
# 2. РАБОТА С БАЗОЙ ДАННЫХ (SQLite)
# ======================================================

def init_db():
    """Создаёт таблицы, если их нет"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
    # Основная таблица: все пользователи и их ответы
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS diagnostics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL UNIQUE,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            q1 TEXT,
            q2 TEXT,
            q3 TEXT,
            q4 TEXT,
            q5 TEXT,
            q6 TEXT,
            category TEXT,
            step INTEGER DEFAULT 0,
            completed BOOLEAN DEFAULT FALSE,
            started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Таблица для логов действий (по желанию)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            action TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    conn.commit()
    conn.close()
    logger.info("✅ База данных инициализирована")

def save_user_start(user_id, username, first_name, last_name):
    """Сохраняет или обновляет пользователя при /start"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
    cursor.execute('''
        INSERT INTO diagnostics (user_id, username, first_name, last_name, started_at)
        VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(user_id) DO UPDATE SET
            username = excluded.username,
            first_name = excluded.first_name,
            last_name = excluded.last_name,
            updated_at = CURRENT_TIMESTAMP
    ''', (user_id, username, first_name, last_name))
    
    conn.commit()
    conn.close()

def log_action(user_id, action):
    """Логирует действие пользователя (для аналитики)"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO user_actions (user_id, action) VALUES (?, ?)
    ''', (user_id, action))
    conn.commit()
    conn.close()

def update_user_step(user_id, step):
    """Обновляет текущий шаг пользователя"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        UPDATE diagnostics SET step = ?, updated_at = CURRENT_TIMESTAMP
        WHERE user_id = ?
    ''', (step, user_id))
    conn.commit()
    conn.close()

def save_diagnostic(user_id, answers, category):
    """Сохраняет финальные ответы и категорию"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
    cursor.execute('''
        UPDATE diagnostics SET
            q1 = ?, q2 = ?, q3 = ?, q4 = ?, q5 = ?, q6 = ?,
            category = ?,
            completed = TRUE,
            completed_at = CURRENT_TIMESTAMP,
            updated_at = CURRENT_TIMESTAMP
        WHERE user_id = ?
    ''', (
        answers.get("q1"),
        answers.get("q2"),
        answers.get("q3"),
        answers.get("q4"),
        answers.get("q5"),
        answers.get("q6"),
        category,
        user_id
    ))
    
    conn.commit()
    conn.close()
    logger.info(f"✅ Сохранён результат для user_id={user_id}, категория={category}")

def get_all_users():
    """Возвращает всех пользователей, кто нажал /start"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        SELECT user_id, username, first_name, last_name, 
               started_at, completed, category, step
        FROM diagnostics
        ORDER BY started_at DESC
    ''')
    rows = cursor.fetchall()
    conn.close()
    return rows

def get_completed_users():
    """Возвращает только тех, кто завершил опрос"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        SELECT user_id, username, first_name, last_name, 
               category, completed_at
        FROM diagnostics
        WHERE completed = TRUE
        ORDER BY completed_at DESC
    ''')
    rows = cursor.fetchall()
    conn.close()
    return rows

def get_uncompleted_users():
    """Возвращает тех, кто начал, но не завершил"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        SELECT user_id, username, first_name, last_name, 
               step, started_at
        FROM diagnostics
        WHERE completed = FALSE
        ORDER BY started_at DESC
    ''')
    rows = cursor.fetchall()
    conn.close()
    return rows

def get_stats():
    """Возвращает статистику по категориям"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
    cursor.execute('''
        SELECT 
            category,
            COUNT(*) as count
        FROM diagnostics
        WHERE completed = TRUE
        GROUP BY category
    ''')
    cat_stats = cursor.fetchall()
    
    cursor.execute('''
        SELECT 
            COUNT(*) as total,
            SUM(CASE WHEN completed = TRUE THEN 1 ELSE 0 END) as completed,
            SUM(CASE WHEN completed = FALSE THEN 1 ELSE 0 END) as uncompleted
        FROM diagnostics
    ''')
    total_stats = cursor.fetchone()
    
    conn.close()
    return total_stats, cat_stats

# ======================================================
# 3. КНОПКИ ДЛЯ БОТА
# ======================================================

def get_hello_keyboard():
    keyboard = [
        [InlineKeyboardButton("✅ Да, поехали", callback_data="start_survey")],
        [InlineKeyboardButton("❌ Нет", callback_data="cancel")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_q1_keyboard():
    keyboard = [
        [InlineKeyboardButton("📈 Растёт", callback_data="q1_grow")],
        [InlineKeyboardButton("⏸️ Стоит на месте", callback_data="q1_stable")],
        [InlineKeyboardButton("📉 Падает", callback_data="q1_fall")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_q2_keyboard():
    keyboard = [
        [InlineKeyboardButton("🎯 Полная ясность", callback_data="q2_clear")],
        [InlineKeyboardButton("🔍 Частично", callback_data="q2_partial")],
        [InlineKeyboardButton("🌫️ Не понимаю", callback_data="q2_confused")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_q3_keyboard():
    keyboard = [
        [InlineKeyboardButton("⚡ Стабильно", callback_data="q3_stable")],
        [InlineKeyboardButton("🚀 Качает", callback_data="q3_good")],
        [InlineKeyboardButton("🪫 На нуле / выгораю", callback_data="q3_burnout")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_q4_keyboard():
    keyboard = [
        [InlineKeyboardButton("🏗️ Есть система", callback_data="q4_system")],
        [InlineKeyboardButton("🧩 Частично", callback_data="q4_partial")],
        [InlineKeyboardButton("🌀 Хаос / всё на мне", callback_data="q4_chaos")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_q5_keyboard():
    keyboard = [
        [InlineKeyboardButton("💰 Деньги / доход", callback_data="q5_money")],
        [InlineKeyboardButton("🧭 Потеря направления", callback_data="q5_direction")],
        [InlineKeyboardButton("🌪️ Хаос и перегруз", callback_data="q5_overload")],
        [InlineKeyboardButton("😨 Страх проявляться / расти", callback_data="q5_fear")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_q6_keyboard():
    keyboard = [
        [InlineKeyboardButton("🔥 В ближайшие 2 недели", callback_data="q6_soon")],
        [InlineKeyboardButton("📅 В течение месяца", callback_data="q6_month")],
        [InlineKeyboardButton("👀 Пока просто смотрю", callback_data="q6_later")]
    ]
    return InlineKeyboardMarkup(keyboard)

# ======================================================
# 4. ОБРАБОТЧИКИ КОМАНД И КНОПОК
# ======================================================

# Хранилище ответов пользователей в памяти
user_data = {}

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start"""
    user_id = update.effective_user.id
    user = update.effective_user
    
    # Сохраняем пользователя в базу
    save_user_start(
        user_id=user_id,
        username=user.username,
        first_name=user.first_name,
        last_name=user.last_name
    )
    log_action(user_id, "start")
    
    user_data[user_id] = {}
    
    await update.message.reply_text(
        "👋 Привет!\n\n"
        "Это короткий чек (2–3 минуты), который поможет понять:\n"
        "ты сейчас в кризисе, в потолке или в точке роста.\n\n"
        "Без тестов «про личность» — только по делу.\n"
        "Готов(а)?",
        reply_markup=get_hello_keyboard()
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик нажатий на кнопки"""
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    data = query.data
    
    # Логируем действие
    log_action(user_id, f"click_{data}")
    
    # ----- ПРИВЕТСТВИЕ -----
    if data == "start_survey":
        user_data[user_id] = {}
        user_data[user_id]["step"] = 1
        update_user_step(user_id, 1)
        await query.edit_message_text(
            "📊 **Вопрос 1 из 6**\n\n"
            "Как сейчас обстоят дела с результатами / доходом?",
            reply_markup=get_q1_keyboard(),
            parse_mode="Markdown"
        )
        return
    
    elif data == "cancel":
        log_action(user_id, "cancel")
        await query.edit_message_text(
            "❌ Понял. Если захочешь разобраться — просто напиши /start."
        )
        return
    
    # ----- ВОПРОС 1 -----
    elif data.startswith("q1_"):
        user_data[user_id]["q1"] = data.replace("q1_", "")
        user_data[user_id]["step"] = 2
        update_user_step(user_id, 2)
        await query.edit_message_text(
            "🧭 **Вопрос 2 из 6**\n\n"
            "Насколько тебе сейчас понятно, куда ты идёшь и зачем?",
            reply_markup=get_q2_keyboard(),
            parse_mode="Markdown"
        )
        return
    
    # ----- ВОПРОС 2 -----
    elif data.startswith("q2_"):
        user_data[user_id]["q2"] = data.replace("q2_", "")
        user_data[user_id]["step"] = 3
        update_user_step(user_id, 3)
        await query.edit_message_text(
            "⚡ **Вопрос 3 из 6**\n\n"
            "Как у тебя с фокусом и энергией?",
            reply_markup=get_q3_keyboard(),
            parse_mode="Markdown"
        )
        return
    
    # ----- ВОПРОС 3 -----
    elif data.startswith("q3_"):
        user_data[user_id]["q3"] = data.replace("q3_", "")
        user_data[user_id]["step"] = 4
        update_user_step(user_id, 4)
        await query.edit_message_text(
            "🏗️ **Вопрос 4 из 6**\n\n"
            "Насколько у тебя сейчас выстроена система (процессы, продажи, план)?",
            reply_markup=get_q4_keyboard(),
            parse_mode="Markdown"
        )
        return
    
    # ----- ВОПРОС 4 -----
    elif data.startswith("q4_"):
        user_data[user_id]["q4"] = data.replace("q4_", "")
        user_data[user_id]["step"] = 5
        update_user_step(user_id, 5)
        await query.edit_message_text(
            "💔 **Вопрос 5 из 6**\n\n"
            "Что сейчас больше всего беспокоит?",
            reply_markup=get_q5_keyboard(),
            parse_mode="Markdown"
        )
        return
    
    # ----- ВОПРОС 5 -----
    elif data.startswith("q5_"):
        user_data[user_id]["q5"] = data.replace("q5_", "")
        user_data[user_id]["step"] = 6
        update_user_step(user_id, 6)
        await query.edit_message_text(
            "⏳ **Вопрос 6 из 6**\n\n"
            "Насколько срочно ты хочешь с этим разобраться?",
            reply_markup=get_q6_keyboard(),
            parse_mode="Markdown"
        )
        return
    
    # ----- ВОПРОС 6 -> ФИНАЛ -----
    elif data.startswith("q6_"):
        user_data[user_id]["q6"] = data.replace("q6_", "")
        update_user_step(user_id, 99)
        await show_result(update, context)

# ======================================================
# 5. ЛОГИКА ОПРЕДЕЛЕНИЯ РЕЗУЛЬТАТА
# ======================================================

async def show_result(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Определяет категорию и отправляет результат"""
    query = update.callback_query
    user_id = update.effective_user.id
    answers = user_data.get(user_id, {})
    
    # Если нет данных — отправляем на старт
    if not answers:
        await query.edit_message_text(
            "⚠️ Что-то пошло не так. Начни заново: /start"
        )
        return
    
    # ----- ПОДСЧЁТ КАТЕГОРИЙ -----
    positive = 0   # Точка роста
    neutral = 0    # Потолок
    crisis = 0     # Кризис / хаос
    
    # Вопрос 1 (результаты)
    if answers.get("q1") == "grow":
        positive += 1
    elif answers.get("q1") == "stable":
        neutral += 1
    else:
        crisis += 1
    
    # Вопрос 2 (ясность)
    if answers.get("q2") == "clear":
        positive += 1
    elif answers.get("q2") == "partial":
        neutral += 1
    else:
        crisis += 1
    
    # Вопрос 3 (энергия)
    if answers.get("q3") == "good":
        positive += 1
    elif answers.get("q3") == "stable":
        neutral += 1
    else:
        crisis += 1
    
    # Вопрос 4 (система)
    if answers.get("q4") == "system":
        positive += 1
    elif answers.get("q4") == "partial":
        neutral += 1
    else:
        crisis += 1
    
    # Вопрос 5 (боль)
    if answers.get("q5") in ["money", "direction"]:
        neutral += 1
    elif answers.get("q5") in ["overload", "fear"]:
        crisis += 1
    
    # Вопрос 6 (срочность)
    if answers.get("q6") == "soon":
        crisis += 1
    elif answers.get("q6") == "month":
        neutral += 1
    else:
        positive += 1
    
    # ----- ОПРЕДЕЛЕНИЕ КАТЕГОРИИ -----
    if crisis >= 3:
        category = "crisis"
    elif positive >= 3:
        category = "growth"
    else:
        category = "stagnation"
    
    # ----- СОХРАНЯЕМ РЕЗУЛЬТАТ В БАЗУ -----
    save_diagnostic(user_id, answers, category)
    log_action(user_id, f"result_{category}")
    
    # ----- ФОРМИРУЕМ ОТВЕТ ПО КАТЕГОРИИ -----
    if category == "growth":
        text = (
            "✅ **ТОЧКА РОСТА**\n\n"
            "Ты не в кризисе.\n"
            "Ты в точке роста — когда старые решения уже тесны, а новые ещё не оформлены.\n\n"
            "В такие моменты важно не ускоряться вслепую, а зафиксировать направление.\n\n"
            "Если захочешь посмотреть на ситуацию со стороны — напиши «стратегия»."
        )
        keyboard = [
            [InlineKeyboardButton("📩 Написать «стратегия»", url="https://t.me/annbefree")]
        ]
    
    elif category == "stagnation":
        text = (
            "⚠️ **ПОТОЛОК / СТАГНАЦИЯ**\n\n"
            "Судя по ответам, ты не в кризисе, но и не в росте.\n"
            "Это состояние потолка:\n"
            "ты уже многое умеешь, но движение замедлилось, а ясность размыта.\n\n"
            "В этой точке попытки «сам(а) разберусь» чаще всего только затягивают процесс.\n\n"
            "В таких ситуациях я делаю **стратегическую диагностику** —\n"
            "разбираем, где ты застрял(а) и какой следующий шаг даст результат.\n\n"
            "Если откликается — напиши «стратегия»."
        )
        keyboard = [
            [InlineKeyboardButton("📩 Написать «стратегия»", url="https://t.me/annbefree")]
        ]
    
    else:  # crisis
        text = (
            "🔥 **КРИЗИС / ХАОС**\n\n"
            "Ты сейчас в кризисной точке.\n"
            "Это не про слабость — это про смену уровня.\n"
            "Когда дальше «по-старому» уже нельзя, а нового плана ещё нет.\n\n"
            "В этой точке особенно важно не оставаться в одиночку с хаосом.\n\n"
            "Здесь нужен не контент, а стратегия действий.\n"
            "Я работаю с такими состояниями через **стратегическую диагностику** (90 минут).\n\n"
            "Если готов(а) разбираться — напиши «стратегия»."
        )
        keyboard = [
            [InlineKeyboardButton("📩 Написать «стратегия»", url="https://t.me/annbefree")]
        ]
    
    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )
    
    # Очищаем данные пользователя из памяти
    if user_id in user_data:
        del user_data[user_id]

# ======================================================
# 6. АДМИНИСТРАТИВНЫЕ КОМАНДЫ
# ======================================================

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает статистику по опросам"""
    user_id = update.effective_user.id
    
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("⛔ У вас нет доступа к этой команде.")
        return
    
    total_stats, cat_stats = get_stats()
    
    if total_stats is None or total_stats[0] == 0:
        await update.message.reply_text("📊 Пока нет данных.")
        return
    
    total, completed, uncompleted = total_stats
    
    text = (
        "📊 **СТАТИСТИКА ОПРОСОВ**\n\n"
        f"👥 Всего пользователей: **{total}**\n"
        f"✅ Завершили опрос: **{completed}**\n"
        f"⏳ Не завершили: **{uncompleted}**\n\n"
        "**Распределение по категориям:**\n"
    )
    
    emoji_map = {
        "growth": "✅",
        "stagnation": "⚠️",
        "crisis": "🔥"
    }
    
    for category, count in cat_stats:
        emoji = emoji_map.get(category, "📌")
        text += f"{emoji} {category}: {count} человек\n"
    
    await update.message.reply_text(text, parse_mode="Markdown")

async def users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает список всех пользователей (кто нажал /start)"""
    user_id = update.effective_user.id
    
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("⛔ У вас нет доступа к этой команде.")
        return
    
    all_users = get_all_users()
    
    if not all_users:
        await update.message.reply_text("📭 Пока ни одного пользователя.")
        return
    
    text = "👥 **ВСЕ ПОЛЬЗОВАТЕЛИ**\n\n"
    for u in all_users[:30]:  # Показываем последние 30
        uid, username, first_name, last_name, started_at, completed, category, step = u
        status = "✅" if completed else f"⏳ шаг {step}"
        cat = category or "—"
        name = f"{first_name or ''} {last_name or ''}".strip() or "Без имени"
        uname = f"@{username}" if username else "нет"
        text += f"{status} {name} ({uname}) — {cat}\n"
    
    if len(all_users) > 30:
        text += f"\n... и ещё {len(all_users) - 30} пользователей"
    
    await update.message.reply_text(text, parse_mode="Markdown")

async def completed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает только тех, кто завершил опрос"""
    user_id = update.effective_user.id
    
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("⛔ У вас нет доступа к этой команде.")
        return
    
    completed_users = get_completed_users()
    
    if not completed_users:
        await update.message.reply_text("📭 Пока никто не завершил опрос.")
        return
    
    text = "✅ **ЗАВЕРШИЛИ ОПРОС**\n\n"
    for u in completed_users[:30]:
        uid, username, first_name, last_name, category, completed_at = u
        name = f"{first_name or ''} {last_name or ''}".strip() or "Без имени"
        uname = f"@{username}" if username else "нет"
        text += f"• {name} ({uname}) — {category}\n"
    
    await update.message.reply_text(text, parse_mode="Markdown")

async def uncompleted(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает тех, кто начал, но не завершил опрос"""
    user_id = update.effective_user.id
    
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("⛔ У вас нет доступа к этой команде.")
        return
    
    uncompleted_users = get_uncompleted_users()
    
    if not uncompleted_users:
        await update.message.reply_text("📭 Все пользователи завершили опрос! 🎉")
        return
    
    text = "⏳ **НЕ ЗАВЕРШИЛИ ОПРОС**\n\n"
    for u in uncompleted_users[:30]:
        uid, username, first_name, last_name, step, started_at = u
        name = f"{first_name or ''} {last_name or ''}".strip() or "Без имени"
        uname = f"@{username}" if username else "нет"
        text += f"• {name} ({uname}) — остановился на шаге {step}\n"
    
    await update.message.reply_text(text, parse_mode="Markdown")

async def export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Экспортирует все данные в CSV и отправляет файлом"""
    user_id = update.effective_user.id
    
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("⛔ У вас нет доступа к этой команде.")
        return
    
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        SELECT * FROM diagnostics ORDER BY id DESC
    ''')
    rows = cursor.fetchall()
    conn.close()
    
    if not rows:
        await update.message.reply_text("📭 Нет данных для экспорта.")
        return
    
    # Создаём CSV-файл в памяти
    output = io.StringIO()
    writer = csv.writer(output)
    
    # Заголовки
    writer.writerow([
        "ID", "User ID", "Username", "First Name", "Last Name",
        "Q1", "Q2", "Q3", "Q4", "Q5", "Q6",
        "Category", "Step", "Completed", "Started At", "Completed At", "Updated At"
    ])
    
    # Данные
    for row in rows:
        writer.writerow(row)
    
    output.seek(0)
    
    # Отправляем файл
    await update.message.reply_document(
        document=output.getvalue().encode('utf-8'),
        filename=f"diagnostics_export_{datetime.now().strftime('%Y%m%d')}.csv",
        caption="📊 Экспорт данных диагностики"
    )

# ======================================================
# 7. ЗАПУСК БОТА В ОТДЕЛЬНОМ ПОТОКЕ
# ======================================================

async def run_bot():
    """Запускает бота в режиме polling."""
    # Инициализируем базу
    init_db()
    
    # Создаём приложение
    application = Application.builder().token(TOKEN).build()
    
    # Регистрируем команды
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("stats", stats))
    application.add_handler(CommandHandler("users", users))
    application.add_handler(CommandHandler("completed", completed))
    application.add_handler(CommandHandler("uncompleted", uncompleted))
    application.add_handler(CommandHandler("export", export))
    
    # Регистрируем обработчик кнопок
    application.add_handler(CallbackQueryHandler(button_handler))
    
    # Запускаем бота
    logger.info("🚀 Бот запущен и готов к работе!")
    await application.initialize()
    await application.start()
    await application.updater.start_polling()
    
    # Держим бота активным
    while True:
        await asyncio.sleep(1)

def start_bot_thread():
    """Запускает бота в отдельном потоке"""
    try:
        asyncio.run(run_bot())
    except Exception as e:
        logger.error(f"❌ Ошибка в боте: {e}")

# ======================================================
# 8. FLASK-СЕРВЕР ДЛЯ RENDER
# ======================================================

app = Flask(__name__)

@app.route('/')
def home():
    return jsonify({"status": "running", "service": "diagnostic-bot"})

@app.route('/health')
def health():
    return jsonify({"status": "ok"})

# ======================================================
# 9. ЗАПУСК
# ======================================================

if __name__ == "__main__":
    # Запускаем бота в фоновом потоке
    bot_thread = threading.Thread(target=start_bot_thread)
    bot_thread.start()
    logger.info("🌐 Flask-сервер запускается...")
    
    # Запускаем Flask-сервер
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
