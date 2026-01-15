#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Metro Shop Telegram Bot (Ultimate Edition)
Features:
- Full Shop & Order Management
- Worker Stats & Review System
- Referral System (Invite & Earn)
- User Balance & Partial Payment via Balance
- Promocode System
- Admin Broadcast
- Documentation & Legal
"""

import os
import sqlite3
import logging
from datetime import datetime
from typing import List, Optional

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InputMediaPhoto,
    Update,
)

from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telegram.error import BadRequest

# --- Configuration ---
TG_BOT_TOKEN = os.getenv('TG_BOT_TOKEN', '8269807126:AAFLKT39qdkKR81df5nEYuCFIk3z8kdZbSo')
OWNER_ID = int(os.getenv('OWNER_ID', '8473513085'))
ADMIN_CHAT_ID = int(os.getenv('ADMIN_CHAT_ID', '-1003448809517'))
DB_PATH = os.getenv('DB_PATH', 'metro_shop.db')

# Contact for support button
SUPPORT_CONTACT_USER = os.getenv('SUPPORT_CONTACT', '@wixyeez')

# Admin IDs
ADMIN_IDS: List[int] = [OWNER_ID]
if os.getenv('ADMIN_IDS'):
    ADMIN_IDS = [int(x) for x in os.getenv('ADMIN_IDS').split(',') if x.strip()]

# Settings
MAX_WORKERS_PER_ORDER = int(os.getenv('MAX_WORKERS_PER_ORDER', '3'))
WORKER_PERCENT = float(os.getenv('WORKER_PERCENT', '0.7'))  # 70% to workers
REFERRAL_PERCENT = float(os.getenv('REFERRAL_PERCENT', '0.05')) # 5% referral bonus

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- LEGAL TEXTS ---
PRIVACY_POLICY_TEXT = """
🔒 **Политика конфиденциальности**

1. **Сбор данных**: Мы собираем Telegram ID, Username и игровые ID (PUBG ID) для выполнения заказов.
2. **Использование**: Данные нужны только для связи и оказания услуг.
3. **Третьи лица**: Данные передаются только исполнителям заказа.
4. **Безопасность**: Мы защищаем ваши данные.
"""

USER_AGREEMENT_TEXT = """
📜 **Пользовательское соглашение**

1. **Услуги**: Мы предоставляем посреднические услуги в Metro Royale.
2. **Оплата**: Услуга считается оказанной после завершения рейда.
3. **Баланс**: Бонусные средства за рефералов можно использовать для скидки до 100% на товары.
4. **Ответственность**: Мы не несем ответственности за игровые блокировки, вызванные действиями пользователя.
"""

# --- DB helpers ---
def init_db() -> None:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # Users with Balance and Referrer
    cur.execute('''
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY,
        tg_id INTEGER UNIQUE,
        username TEXT,
        pubg_id TEXT,
        registered_at TEXT,
        balance REAL DEFAULT 0,
        invited_by INTEGER,
        referrals_count INTEGER DEFAULT 0
    )
    ''')
    
    # Ensure columns exist (migration)
    try: cur.execute("ALTER TABLE users ADD COLUMN balance REAL DEFAULT 0")
    except: pass
    try: cur.execute("ALTER TABLE users ADD COLUMN invited_by INTEGER")
    except: pass
    try: cur.execute("ALTER TABLE users ADD COLUMN referrals_count INTEGER DEFAULT 0")
    except: pass

    # Products
    cur.execute('''
    CREATE TABLE IF NOT EXISTS products (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        description TEXT,
        price REAL NOT NULL,
        photo TEXT,
        created_at TEXT
    )
    ''')

    cur.execute('''
    CREATE TABLE IF NOT EXISTS product_photos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        product_id INTEGER,
        file_id TEXT,
        created_at TEXT
    )
    ''')

    # Orders with discount tracking
    cur.execute('''
    CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        product_id INTEGER,
        price REAL,
        status TEXT,
        created_at TEXT,
        payment_screenshot_file_id TEXT,
        pubg_id TEXT,
        started_at TEXT,
        done_at TEXT,
        balance_deducted REAL DEFAULT 0,
        promo_code TEXT,
        discount_amount REAL DEFAULT 0
    )
    ''')
    
    try: cur.execute("ALTER TABLE orders ADD COLUMN balance_deducted REAL DEFAULT 0")
    except: pass
    try: cur.execute("ALTER TABLE orders ADD COLUMN promo_code TEXT")
    except: pass
    try: cur.execute("ALTER TABLE orders ADD COLUMN discount_amount REAL DEFAULT 0")
    except: pass

    cur.execute('''
    CREATE TABLE IF NOT EXISTS order_workers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id INTEGER,
        worker_id INTEGER,
        worker_username TEXT,
        taken_at TEXT
    )
    ''')

    cur.execute('''
    CREATE TABLE IF NOT EXISTS reviews (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id INTEGER,
        buyer_id INTEGER,
        worker_id INTEGER,
        rating INTEGER,
        text TEXT,
        created_at TEXT
    )
    ''')
    
    # Promocodes system
    cur.execute('''
    CREATE TABLE IF NOT EXISTS promocodes (
        code TEXT PRIMARY KEY,
        discount_percent INTEGER,
        activations_left INTEGER
    )
    ''')
    
    cur.execute('''
    CREATE TABLE IF NOT EXISTS used_promocodes (
        user_id INTEGER,
        code TEXT,
        UNIQUE(user_id, code)
    )
    ''')

    cur.execute('''
    CREATE TABLE IF NOT EXISTS worker_payouts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id INTEGER,
        worker_id INTEGER,
        amount REAL,
        created_at TEXT
    )
    ''')

    conn.commit()
    conn.close()

def db_execute(query: str, params: tuple = (), fetch: bool = False):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(query, params)
    data = None
    if fetch:
        data = cur.fetchall()
    else:
        conn.commit()
    conn.close()
    return data

def now_iso() -> str:
    return datetime.utcnow().isoformat()

def is_admin_tg(tg_id: int) -> bool:
    return tg_id in ADMIN_IDS

# --- UI / Keyboards ---
MAIN_MENU = ReplyKeyboardMarkup(
    [
        [KeyboardButton('📦 Каталог'), KeyboardButton('👤 Профиль')],
        [KeyboardButton('🧾 Мои заказы'), KeyboardButton('🎮 PUBG ID')],
        [KeyboardButton('📄 Документы'), KeyboardButton('📞 Поддержка')]
    ],
    resize_keyboard=True,
)

DOCS_MENU = ReplyKeyboardMarkup(
    [
        [KeyboardButton('📜 Пользовательское соглашение'), KeyboardButton('🔒 Политика конфиденциальности')],
        [KeyboardButton('↩️ Назад')]
    ],
    resize_keyboard=True,
)

CANCEL_BUTTON = ReplyKeyboardMarkup([[KeyboardButton('↩️ Назад')]], resize_keyboard=True)

ADMIN_PANEL_KB = ReplyKeyboardMarkup(
    [[KeyboardButton('➕ Добавить товар'), KeyboardButton('✏️ Редактировать товар'), KeyboardButton('🗑️ Удалить товар')],
     [KeyboardButton('📋 Список заказов'), KeyboardButton('↩️ Назад')]],
    resize_keyboard=True,
)

# --- Format Helpers ---
def format_performers_for_caption(order_id: int) -> str:
    rows = db_execute('SELECT worker_id, worker_username FROM order_workers WHERE order_id=? ORDER BY id', (order_id,), fetch=True)
    if not rows: return 'Исполнители: —'
    parts = []
    for wid, wname in rows:
        parts.append(f'@{wname}' if wname else str(wid))
    return 'Исполнители: ' + ', '.join(parts)

def build_admin_keyboard_for_order(order_id: int, order_status: str) -> InlineKeyboardMarkup:
    if order_status in ('pending_verification', 'awaiting_screenshot'):
        return InlineKeyboardMarkup([
            [InlineKeyboardButton('✅ Подтвердить', callback_data=f'confirm:{order_id}'),
             InlineKeyboardButton('❌ Отклонить', callback_data=f'reject:{order_id}')],
        ])
    elif order_status in ('paid', 'in_progress', 'delivering'):
        return InlineKeyboardMarkup([
            [InlineKeyboardButton('🟢 Беру', callback_data=f'take:{order_id}'),
             InlineKeyboardButton('🔴 Сняться', callback_data=f'leave:{order_id}')],
            [InlineKeyboardButton('▶ Начать', callback_data=f'status:{order_id}:in_progress'),
             InlineKeyboardButton('📦 Выдача', callback_data=f'status:{order_id}:delivering'),
             InlineKeyboardButton('🏁 Готово', callback_data=f'status:{order_id}:done')],
        ])
    return InlineKeyboardMarkup([[InlineKeyboardButton('ℹ️ Инфо', callback_data=f'detail_order:{order_id}')]])

def build_caption_for_admin_message(order_id: int, buyer_tg: str, pubg: str, prod: str, price: float, created: str, status: str, started: str = None, done: str = None, balance_deducted: float = 0) -> str:
    lines = [
        f'📦 Заказ #{order_id}',
        f'Пользователь: {buyer_tg}',
        f'PUBG ID: {pubg or "не указан"}',
        f'Товар: {prod}',
        f'💰 К оплате: {price}₽',
    ]
    if balance_deducted > 0:
        lines.append(f'💎 Оплачено балансом: {balance_deducted}₽')
        
    lines.extend([
        f'Статус: {status}',
        f'Время: {created}'
    ])
    if started: lines.append(f'Начат: {started}')
    if done: lines.append(f'Выполнен: {done}')
    lines.append(format_performers_for_caption(order_id))
    return '\n'.join(lines)

# --- Handlers ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    args = context.args
    
    # Check/Register user
    exists = db_execute('SELECT id FROM users WHERE tg_id=?', (user.id,), fetch=True)
    
    if not exists:
        referrer_id = None
        if args and args[0].isdigit():
            referrer_tg = int(args[0])
            if referrer_tg != user.id:
                # Resolve referrer DB ID
                ref_row = db_execute('SELECT id FROM users WHERE tg_id=?', (referrer_tg,), fetch=True)
                if ref_row:
                    referrer_id = ref_row[0][0]
                    # Increment referrer count
                    db_execute('UPDATE users SET referrals_count = referrals_count + 1 WHERE id=?', (referrer_id,))
                    # Notify referrer
                    try:
                        await context.bot.send_message(referrer_tg, f'🎉 По вашей ссылке зарегистрировался новый пользователь: {user.first_name}')
                    except: pass
        
        db_execute('INSERT INTO users (tg_id, username, registered_at, balance, invited_by) VALUES (?, ?, ?, 0, ?)',
                   (user.id, user.username or '', now_iso(), referrer_id))
        
    await update.message.reply_text(
        f"Привет, {user.first_name}!\n\n"
        "🔥 **Metro Shop Ultimate** — лучший сервис для Metro Royale.\n"
        "Используйте меню для навигации.", 
        reply_markup=MAIN_MENU, parse_mode='Markdown'
    )

async def profile_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    row = db_execute('SELECT id, balance, referrals_count, registered_at FROM users WHERE tg_id=?', (user.id,), fetch=True)
    if not row:
        return
    uid, balance, ref_count, reg_date = row[0]
    reg_clean = reg_date.split('T')[0]
    
    ref_link = f"https://t.me/{context.bot.username}?start={user.id}"
    
    text = (
        f"👤 **Личный кабинет**\n\n"
        f"🆔 Ваш ID: `{user.id}`\n"
        f"📅 В сервисе с: {reg_clean}\n\n"
        f"💰 **Баланс: {balance}₽**\n"
        f"👥 Приглашено друзей: {ref_count}\n\n"
        f"🔗 **Ваша реферальная ссылка:**\n`{ref_link}`\n\n"
        f"Приглашайте друзей и получайте {int(REFERRAL_PERCENT*100)}% от их покупок на баланс!"
    )
    await update.message.reply_text(text, parse_mode='Markdown', reply_markup=MAIN_MENU)

async def promo_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # /promo CODE
    user = update.effective_user
    if not context.args:
        await update.message.reply_text("Введите промокод. Пример: `/promo SALE10`", parse_mode='Markdown')
        return
    code = context.args[0].upper().strip()
    
    # Check code
    p = db_execute('SELECT discount_percent, activations_left FROM promocodes WHERE code=?', (code,), fetch=True)
    if not p:
        await update.message.reply_text("❌ Промокод не найден.")
        return
    percent, left = p[0]
    if left <= 0:
        await update.message.reply_text("❌ Промокод закончился.")
        return
        
    # Check if used
    u_row = db_execute('SELECT id FROM users WHERE tg_id=?', (user.id,), fetch=True)
    uid = u_row[0][0]
    used = db_execute('SELECT 1 FROM used_promocodes WHERE user_id=? AND code=?', (uid, code), fetch=True)
    if used:
        await update.message.reply_text("❌ Вы уже использовали этот код.")
        return
        
    # Activate in session
    context.user_data['active_promo'] = {'code': code, 'percent': percent}
    await update.message.reply_text(f"✅ Промокод `{code}` активирован! Скидка {percent}% применится к следующему заказу.", parse_mode='Markdown')

# Admin: Add Promo
async def add_promo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # /addpromo CODE PERCENT LIMIT
    if not is_admin_tg(update.effective_user.id): return
    args = context.args
    if len(args) < 3:
        await update.message.reply_text("Формат: /addpromo <КОД> <ПРОЦЕНТ> <ЛИМИТ>")
        return
    code, percent, limit = args[0].upper(), args[1], args[2]
    try:
        db_execute('INSERT INTO promocodes VALUES (?, ?, ?)', (code, int(percent), int(limit)))
        await update.message.reply_text(f"✅ Промокод {code} создан.")
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")

# Admin: Broadcast
async def broadcast_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # /broadcast text
    if not is_admin_tg(update.effective_user.id): return
    msg = update.message.text.partition(' ')[2]
    if not msg:
        await update.message.reply_text("Введите текст рассылки.")
        return
    
    users = db_execute('SELECT tg_id FROM users', fetch=True)
    count = 0
    await update.message.reply_text(f"🚀 Начинаю рассылку для {len(users)} пользователей...")
    for (tid,) in users:
        try:
            await context.bot.send_message(tid, f"📢 **ВАЖНОЕ СООБЩЕНИЕ**\n\n{msg}", parse_mode='Markdown')
            count += 1
        except: pass
    await update.message.reply_text(f"✅ Рассылка завершена. Доставлено: {count}")

async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.id == ADMIN_CHAT_ID: return
    msg = update.message
    if not msg or not msg.text: return
    text = msg.text.strip()
    user = update.effective_user

    # Admin flows check
    if context.user_data.get('product_flow'):
        await handle_add_product_flow(update, context)
        return
    if context.user_data.get('edit_flow'):
        await handle_edit_product_flow(update, context)
        return
    if context.user_data.get('review_flow'):
        await handle_review_flow(update, context)
        return

    # Menu
    if text == '👤 Профиль':
        await profile_handler(update, context)
        return
    if text == '📦 Каталог':
        await products_handler(update, context)
        return
    if text == '🧾 Мои заказы':
        await my_orders(update, context)
        return
    if text == '🎮 PUBG ID':
        await update.message.reply_text('Отправьте ваш PUBG ID (ник или цифры), или ↩️ Назад.', reply_markup=CANCEL_BUTTON)
        return
    if text == '📄 Документы':
        await update.message.reply_text('Выберите документ:', reply_markup=DOCS_MENU)
        return
    if text == '📜 Пользовательское соглашение':
        await update.message.reply_text(USER_AGREEMENT_TEXT, parse_mode='Markdown')
        return
    if text == '🔒 Политика конфиденциальности':
        await update.message.reply_text(PRIVACY_POLICY_TEXT, parse_mode='Markdown')
        return
    if text == '📞 Поддержка':
        contact = SUPPORT_CONTACT_USER
        if not contact.startswith(('@', 'http')): contact = '@' + contact
        await update.message.reply_text(f'Для связи с администрацией: {contact}', reply_markup=MAIN_MENU)
        return
    if text == '↩️ Назад':
        await update.message.reply_text('Меню:', reply_markup=MAIN_MENU)
        return
    
    # Admin commands via buttons
    if is_admin_tg(user.id):
        if text == '/admin':
            await admin_menu(update, context)
            return
        if text == '➕ Добавить товар':
            context.user_data['product_flow'] = {'stage': 'name', 'data': {}}
            await update.message.reply_text('Шаг 1. Введите название товара.', reply_markup=CANCEL_BUTTON)
            return
        if text == '📋 Список заказов':
            await list_orders_admin(update, context)
            return
        if text == '✏️ Редактировать товар':
            # Simplified flow starter
            await products_handler(update, context) # Admin sees edit buttons there
            return
        if text == '🗑️ Удалить товар':
            await products_handler(update, context)
            return

    # Heuristic PUBG ID capture
    if len(text) <= 32 and ' ' not in text and not text.startswith('/'):
        db_execute('UPDATE users SET pubg_id=? WHERE tg_id=?', (text, user.id))
        await update.message.reply_text(f'✅ PUBG ID сохранён: {text}', reply_markup=MAIN_MENU)
        return

    await update.message.reply_text("Неизвестная команда.", reply_markup=MAIN_MENU)

# --- Product Flow & Catalog ---
async def products_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    products = db_execute('SELECT id, name, price, photo FROM products ORDER BY id DESC', fetch=True)
    if not products:
        await update.message.reply_text('В каталоге пока пусто.')
        return
    
    await update.message.reply_text('📦 Каталог товаров:')
    for pid, name, price, photo in products:
        done_cnt_row = db_execute('SELECT COUNT(*) FROM orders WHERE product_id=? AND status=?', (pid, 'done'), fetch=True)
        done_cnt = done_cnt_row[0][0] if done_cnt_row else 0
        caption = f"🔸 {name}\n💸 Цена: {price}₽\n🏆 Выполнено: {done_cnt}"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton('🔍 Подробнее / Купить', callback_data=f'detail:{pid}')]])
        if photo:
            try: await update.message.reply_photo(photo=photo, caption=caption, reply_markup=kb)
            except: await update.message.reply_text(caption, reply_markup=kb)
        else:
            await update.message.reply_text(caption, reply_markup=kb)

async def product_detail_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    pid = int(q.data.split(':')[1])
    row = db_execute('SELECT name, description, price, photo FROM products WHERE id=?', (pid,), fetch=True)
    if not row: return
    name, desc, price, photo = row[0]
    
    extras = db_execute('SELECT file_id FROM product_photos WHERE product_id=?', (pid,), fetch=True)
    text = f"🧬 **{name}**\n\n📝 {desc}\n\n💸 Цена: {price}₽"
    
    btns = [[InlineKeyboardButton(f'🛒 Купить за {price}₽', callback_data=f'buy:{pid}')]]
    if is_admin_tg(q.from_user.id):
        btns.append([InlineKeyboardButton('🗑️ Удалить', callback_data=f'delete:{pid}')])
    kb = InlineKeyboardMarkup(btns)
    
    if extras:
        media = []
        if photo: media.append(InputMediaPhoto(photo, caption=text, parse_mode='Markdown'))
        for (fid,) in extras: media.append(InputMediaPhoto(fid))
        if not photo and media: media[0].caption = text; media[0].parse_mode='Markdown'
        await q.message.reply_media_group(media)
        await q.message.reply_text('👆 Действия:', reply_markup=kb)
    else:
        try: await q.message.delete()
        except: pass
        if photo: await q.message.reply_photo(photo, caption=text, parse_mode='Markdown', reply_markup=kb)
        else: await q.message.reply_text(text, parse_mode='Markdown', reply_markup=kb)

async def buy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    pid = int(q.data.split(':')[1])
    
    p = db_execute('SELECT id, name, price FROM products WHERE id=?', (pid,), fetch=True)
    if not p: return
    prod_id, name, price = p[0]
    
    user = q.from_user
    u_row = db_execute('SELECT id, pubg_id, balance FROM users WHERE tg_id=?', (user.id,), fetch=True)
    if not u_row: return # Should not happen
    uid, pubg_id, balance = u_row[0]
    
    # Logic for price calculation
    final_price = price
    promo_code = None
    discount_val = 0.0
    
    # 1. Apply Promo
    active_promo = context.user_data.get('active_promo')
    if active_promo:
        percent = active_promo['percent']
        code = active_promo['code']
        # Re-verify availability
        p_check = db_execute('SELECT activations_left FROM promocodes WHERE code=?', (code,), fetch=True)
        if p_check and p_check[0][0] > 0:
            discount_val = price * (percent / 100.0)
            final_price -= discount_val
            promo_code = code
        else:
            await q.message.reply_text("⚠️ Срок действия промокода истек.")
            context.user_data.pop('active_promo', None)

    # 2. Apply Balance (Deduct immediately from balance to lock it)
    balance_used = 0.0
    if balance > 0 and final_price > 0:
        if balance >= final_price:
            balance_used = final_price
            final_price = 0
        else:
            balance_used = balance
            final_price -= balance_used
    
    # Update DB: Lock balance
    if balance_used > 0:
        db_execute('UPDATE users SET balance = balance - ? WHERE id=?', (balance_used, uid))

    # Insert Order
    cursor = db_execute(
        'INSERT INTO orders (user_id, product_id, price, status, created_at, pubg_id, balance_deducted, promo_code, discount_amount) '
        'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id',
        (uid, prod_id, final_price, 'awaiting_screenshot', now_iso(), pubg_id, balance_used, promo_code, discount_val),
        fetch=True
    )
    if not cursor:
        r = db_execute('SELECT last_insert_rowid()', fetch=True)
        oid = r[0][0]
    else:
        oid = cursor[0][0]
        
    # Clear promo from session
    context.user_data.pop('active_promo', None)
    
    msg_text = f"🛍️ **Оформление заказа #{oid}**\nТовар: {name}\n"
    if discount_val > 0:
        msg_text += f"🏷️ Скидка по коду {promo_code}: -{discount_val}₽\n"
    if balance_used > 0:
        msg_text += f"💎 Оплачено балансом: -{balance_used}₽\n"
        
    msg_text += f"\n💰 **ИТОГО К ОПЛАТЕ: {final_price}₽**\n"
    
    if final_price <= 0:
        # Fully paid by balance
        db_execute('UPDATE orders SET status=? WHERE id=?', ('pending_verification', oid))
        # Notify Admin immediately
        caption = build_caption_for_admin_message(oid, f"@{user.username}", pubg_id, name, final_price, now_iso(), 'pending_verification', balance_deducted=balance_used)
        kb = build_admin_keyboard_for_order(oid, 'pending_verification')
        await context.bot.send_message(ADMIN_CHAT_ID, f"⚡ **Заказ полностью оплачен балансом!**\n\n{caption}", reply_markup=kb)
        await q.message.reply_text(f"{msg_text}\n✅ Заказ полностью оплачен бонусами! Ожидайте подтверждения админом.")
    else:
        await q.message.reply_text(
            f"{msg_text}\n"
            "💳 **Реквизиты:** Сбербанк `+79002535363` (Николай М)\n"
            "📸 **Отправьте скриншот** перевода в этот чат."
        )

# --- Photo Router (Screenshots & Admin) ---
async def photo_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if not msg: return
    user = update.effective_user
    
    # Admin Product Photo logic
    if is_admin_tg(user.id) and context.user_data.get('product_flow', {}).get('stage') == 'photo':
        await handle_add_product_flow(update, context)
        return
        
    # Payment Screenshot Logic
    u_row = db_execute('SELECT id FROM users WHERE tg_id=?', (user.id,), fetch=True)
    if not u_row: return
    uid = u_row[0][0]
    
    # Find active order expecting screen
    pending = db_execute('SELECT id, price, pubg_id, product_id, balance_deducted FROM orders WHERE user_id=? AND status=? ORDER BY id DESC LIMIT 1', 
                         (uid, 'awaiting_screenshot'), fetch=True)
    
    if pending:
        oid, price, pubg, pid, bal = pending[0]
        file_id = msg.photo[-1].file_id
        
        db_execute('UPDATE orders SET status=?, payment_screenshot_file_id=? WHERE id=?', 
                   ('pending_verification', file_id, oid))
        
        await msg.reply_text('✅ Скриншот принят! Ожидайте подтверждения.', reply_markup=MAIN_MENU)
        
        prod_row = db_execute('SELECT name FROM products WHERE id=?', (pid,), fetch=True)
        pname = prod_row[0][0] if prod_row else '?'
        
        caption = build_caption_for_admin_message(oid, f"@{user.username}", pubg, pname, price, now_iso(), 'pending_verification', balance_deducted=bal)
        kb = build_admin_keyboard_for_order(oid, 'pending_verification')
        
        try:
            await context.bot.send_photo(chat_id=ADMIN_CHAT_ID, photo=file_id, caption=caption, reply_markup=kb)
        except:
            await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=caption + "\n(Скрин не грузится)", reply_markup=kb)

# --- Admin Decision Logic ---
async def admin_decision(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    action, oid_str = q.data.split(':')
    oid = int(oid_str)
    
    if action == 'confirm':
        # 1. Update status
        db_execute('UPDATE orders SET status=? WHERE id=?', ('paid', oid))
        
        # 2. Finalize Promocode usage (decrement)
        row = db_execute('SELECT user_id, promo_code, price, balance_deducted FROM orders WHERE id=?', (oid,), fetch=True)
        if row:
            uid, promo, paid_amount, bal_deducted = row[0]
            if promo:
                db_execute('UPDATE promocodes SET activations_left = activations_left - 1 WHERE code=?', (promo,))
                db_execute('INSERT OR IGNORE INTO used_promocodes (user_id, code) VALUES (?, ?)', (uid, promo))
            
            # 3. REFERRAL BONUS Logic
            # Total value of order = paid_amount + bal_deducted.
            # Usually bonus is on REAL money paid. Let's do that.
            if paid_amount > 0:
                inv_row = db_execute('SELECT invited_by FROM users WHERE id=?', (uid,), fetch=True)
                if inv_row and inv_row[0][0]:
                    referrer_id = inv_row[0][0]
                    bonus = paid_amount * REFERRAL_PERCENT
                    db_execute('UPDATE users SET balance = balance + ? WHERE id=?', (bonus, referrer_id))
                    # Notify referrer
                    ref_tg = db_execute('SELECT tg_id FROM users WHERE id=?', (referrer_id,), fetch=True)
                    if ref_tg:
                        try:
                            await context.bot.send_message(ref_tg[0][0], f"💰 Вам начислено +{bonus}₽ за покупку реферала!")
                        except: pass
            
            # Notify User
            tg_row = db_execute('SELECT tg_id FROM users WHERE id=?', (uid,), fetch=True)
            if tg_row:
                try: await context.bot.send_message(tg_row[0][0], f'✅ Заказ #{oid} подтвержден! Скоро начнем.')
                except: pass

        await q.message.edit_caption(caption=q.message.caption + '\n\n✅ ОПЛАТА ПОДТВЕРЖДЕНА', reply_markup=build_admin_keyboard_for_order(oid, 'paid'))

    elif action == 'reject':
        # Refund balance if it was deducted
        row = db_execute('SELECT user_id, balance_deducted FROM orders WHERE id=?', (oid,), fetch=True)
        if row:
            uid, bal = row[0]
            if bal > 0:
                db_execute('UPDATE users SET balance = balance + ? WHERE id=?', (bal, uid))
                
        db_execute('UPDATE orders SET status=? WHERE id=?', ('rejected', oid))
        await q.message.edit_caption(caption=q.message.caption + '\n\n❌ ОТКЛОНЕНО (Средства возвращены на баланс, если были списаны)')

# --- Worker Logic ---
async def performer_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    user = q.from_user
    action, oid_str = q.data.split(':')
    oid = int(oid_str)
    
    if action == 'take':
        cnt = db_execute('SELECT COUNT(*) FROM order_workers WHERE order_id=?', (oid,), fetch=True)[0][0]
        if cnt >= MAX_WORKERS_PER_ORDER:
            await q.answer('Максимум исполнителей.', show_alert=True)
            return
        exists = db_execute('SELECT 1 FROM order_workers WHERE order_id=? AND worker_id=?', (oid, user.id), fetch=True)
        if exists:
            await q.answer('Вы уже в заказе.')
            return
        db_execute('INSERT INTO order_workers (order_id, worker_id, worker_username, taken_at) VALUES (?, ?, ?, ?)',
                   (oid, user.id, user.username or '', now_iso()))
        await q.answer('Взял!')
        
    elif action == 'leave':
        db_execute('DELETE FROM order_workers WHERE order_id=? AND worker_id=?', (oid, user.id))
        await q.answer('Снялся.')
        
    await update_admin_message(q.message, oid)

async def order_progress_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    _, oid_str, status = q.data.split(':')
    oid = int(oid_str)
    
    updates = ["status=?"]
    params = [status]
    
    if status == 'in_progress':
        # set started_at if null
        row = db_execute('SELECT started_at FROM orders WHERE id=?', (oid,), fetch=True)
        if not row[0][0]:
            updates.append("started_at=?")
            params.append(now_iso())
            
    elif status == 'done':
        updates.append("done_at=?")
        params.append(now_iso())
        
        # Payout calculation (Simple: based on full price, ignoring balance/promos for workers? Or net? 
        # Usually workers get % of Full Price regardless of discounts, Owner absorbs cost. Let's do Full Price)
        orow = db_execute('SELECT price, discount_amount, balance_deducted FROM orders WHERE id=?', (oid,), fetch=True)
        # Reconstruct "Real Value" of order for worker payout
        # If we want workers to get % of REAL money received: (price) [since price in DB is amount TO PAY]. 
        # But if price was 0 (full balance), workers get 0? No.
        # Let's reconstruct original price: Price + Balance + Discount
        paid, disc, bal = orow[0]
        original_value = paid + disc + bal
        
        total_payout = original_value * WORKER_PERCENT
        ws = db_execute('SELECT worker_id FROM order_workers WHERE order_id=?', (oid,), fetch=True)
        if ws:
            per_worker = total_payout / len(ws)
            for (wid,) in ws:
                db_execute('INSERT INTO worker_payouts (order_id, worker_id, amount, created_at) VALUES (?, ?, ?, ?)',
                           (oid, wid, per_worker, now_iso()))
    
    params.append(oid)
    db_execute(f"UPDATE orders SET {', '.join(updates)} WHERE id=?", tuple(params))
    
    await update_admin_message(q.message, oid)
    
    # Notify User
    status_map = {'in_progress': '▶ Заказ выполняется.', 'delivering': '📦 Выдача товара.', 'done': '✅ Заказ выполнен!'}
    if status in status_map:
        row = db_execute('SELECT user_id FROM orders WHERE id=?', (oid,), fetch=True)
        if row:
            tg_row = db_execute('SELECT tg_id FROM users WHERE id=?', (row[0][0],), fetch=True)
            if tg_row:
                kb = None
                if status == 'done':
                    kb = InlineKeyboardMarkup([[InlineKeyboardButton('⭐ Оценить', callback_data=f'leave_review:{oid}')]])
                try: await context.bot.send_message(tg_row[0][0], status_map[status], reply_markup=kb)
                except: pass

async def update_admin_message(message, oid):
    row = db_execute('SELECT o.user_id, o.pubg_id, p.name, o.price, o.created_at, o.status, o.started_at, o.done_at, u.username, o.balance_deducted FROM orders o JOIN products p ON o.product_id=p.id JOIN users u ON o.user_id=u.id WHERE o.id=?', (oid,), fetch=True)
    if not row: return
    uid, pubg, pname, price, created, status, start, done, uname, bal = row[0]
    buyer = f"@{uname}" if uname else f"ID {uid}"
    caption = build_caption_for_admin_message(oid, buyer, pubg, pname, price, created, status, start, done, bal)
    kb = build_admin_keyboard_for_order(oid, status)
    try: await message.edit_caption(caption=caption, reply_markup=kb)
    except: pass

# --- Review System ---
async def handle_review_flow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    flow = context.user_data.get('review_flow')
    if not flow or not msg: return
    
    if msg.text and msg.text.lower() in ['/cancel', '↩️ назад']:
        context.user_data.pop('review_flow', None)
        await msg.reply_text("Отменено.", reply_markup=MAIN_MENU)
        return

    stage = flow['stage']
    if stage == 'rating':
        try:
            r = int(msg.text)
            if not 1 <= r <= 5: raise ValueError
        except:
            await msg.reply_text("Введите число от 1 до 5.")
            return
        flow['rating'] = r
        flow['stage'] = 'text'
        await msg.reply_text("Напишите отзыв (или 'skip'):")
        
    elif stage == 'text':
        txt = msg.text if msg.text.lower() != 'skip' else ''
        db_execute('INSERT INTO reviews (order_id, buyer_id, worker_id, rating, text, created_at) VALUES (?, (SELECT id FROM users WHERE tg_id=?), ?, ?, ?, ?)',
                   (flow['oid'], update.effective_user.id, flow['wid'], flow['rating'], txt, now_iso()))
        
        context.user_data.pop('review_flow', None)
        await msg.reply_text("Спасибо за отзыв!", reply_markup=MAIN_MENU)

async def leave_review_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    oid = int(q.data.split(':')[1])
    workers = db_execute('SELECT worker_id, worker_username FROM order_workers WHERE order_id=?', (oid,), fetch=True)
    if not workers:
        await q.message.reply_text("Некого оценивать.")
        return
    # Simple flow: review first worker
    wid, wname = workers[0]
    context.user_data['review_flow'] = {'stage': 'rating', 'oid': oid, 'wid': wid}
    await q.message.reply_text(f"Оцените @{wname or wid} (1-5):", reply_markup=CANCEL_BUTTON)

# --- Admin Products Add/Edit Helpers ---
async def handle_add_product_flow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if not msg: return
    flow = context.user_data['product_flow']
    stage = flow['stage']
    
    if msg.text == '/cancel' or msg.text == '↩️ Назад':
        context.user_data.pop('product_flow', None)
        await msg.reply_text('Отмена.', reply_markup=ADMIN_PANEL_KB)
        return

    if stage == 'name':
        flow['data']['name'] = msg.text
        flow['stage'] = 'price'
        await msg.reply_text('Введите цену (число):')
    elif stage == 'price':
        try: flow['data']['price'] = float(msg.text)
        except: await msg.reply_text('Нужно число.'); return
        flow['stage'] = 'desc'
        await msg.reply_text('Введите описание:')
    elif stage == 'desc':
        flow['data']['desc'] = msg.text
        flow['stage'] = 'photo'
        await msg.reply_text('Отправьте фото товара:')
    elif stage == 'photo':
        if not msg.photo: await msg.reply_text('Жду фото.'); return
        d = flow['data']
        db_execute('INSERT INTO products (name, description, price, photo, created_at) VALUES (?, ?, ?, ?, ?)',
                   (d['name'], d['desc'], d['price'], msg.photo[-1].file_id, now_iso()))
        context.user_data.pop('product_flow', None)
        await msg.reply_text('✅ Товар добавлен!', reply_markup=ADMIN_PANEL_KB)

# --- Small Handlers ---
async def delete_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    pid = int(q.data.split(':')[1])
    db_execute('DELETE FROM products WHERE id=?', (pid,))
    await q.message.delete()
    await q.message.reply_text("Удалено.")

async def admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Админка:", reply_markup=ADMIN_PANEL_KB)

async def list_orders_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rows = db_execute("SELECT id, price, status FROM orders ORDER BY id DESC LIMIT 5", fetch=True)
    text = "\n".join([f"#{r[0]} - {r[1]}₽ - {r[2]}" for r in rows]) if rows else "Пусто"
    await update.message.reply_text(f"Последние заказы:\n{text}")

async def my_orders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    uid = db_execute('SELECT id FROM users WHERE tg_id=?', (user.id,), fetch=True)[0][0]
    rows = db_execute("SELECT id, status, price FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 5", (uid,), fetch=True)
    text = "\n".join([f"#{r[0]} - {r[2]}₽ ({r[1]})" for r in rows]) if rows else "Нет заказов"
    await update.message.reply_text(f"Ваши заказы:\n{text}")

async def worker_stats_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    wid = update.effective_user.id
    cnt = db_execute('SELECT COUNT(*) FROM order_workers WHERE worker_id=?', (wid,), fetch=True)[0][0]
    earnings = db_execute('SELECT SUM(amount) FROM worker_payouts WHERE worker_id=?', (wid,), fetch=True)[0][0] or 0
    await update.message.reply_text(f"👨‍🔧 **Статистика воркера**\n\nВыполнено заказов: {cnt}\nЗаработано: {int(earnings)}₽", parse_mode='Markdown')

# --- Main ---
def build_app():
    init_db()
    app = ApplicationBuilder().token(TG_BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler('start', start))
    app.add_handler(CommandHandler('admin', admin_menu))
    app.add_handler(CommandHandler('worker', worker_stats_handler))
    app.add_handler(CommandHandler('promo', promo_command))
    app.add_handler(CommandHandler('addpromo', add_promo_handler))
    app.add_handler(CommandHandler('broadcast', broadcast_handler))
    
    # Text & Photo
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))
    app.add_handler(MessageHandler(filters.PHOTO, photo_router))
    
    # Callbacks
    app.add_handler(CallbackQueryHandler(buy_callback, pattern=r'^buy:'))
    app.add_handler(CallbackQueryHandler(product_detail_callback, pattern=r'^detail:'))
    app.add_handler(CallbackQueryHandler(admin_decision, pattern=r'^(confirm|reject):'))
    app.add_handler(CallbackQueryHandler(performer_action, pattern=r'^(take|leave):'))
    app.add_handler(CallbackQueryHandler(order_progress_callback, pattern=r'^status:'))
    app.add_handler(CallbackQueryHandler(leave_review_callback, pattern=r'^leave_review:'))
    app.add_handler(CallbackQueryHandler(delete_callback, pattern=r'^delete:'))

    return app

if __name__ == "__main__":
    application = build_app()
    print("Bot is running...")
    application.run_polling()