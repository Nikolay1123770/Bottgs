#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Metro Shop Tier-1 Bot (v2.1)
Updates:
- Legal documents now link to Telegraph
- Lava.top Integration (Webhook)
- Referral System & Promocodes
"""

import os
import sqlite3
import logging
import json
import hmac
import hashlib
import asyncio
from datetime import datetime
from typing import List, Optional

# Сторонние библиотеки (убедитесь, что установлен aiohttp: pip install aiohttp)
from aiohttp import web
import aiohttp
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
SUPPORT_CONTACT_USER = os.getenv('SUPPORT_CONTACT', '@YourAdminUsername')

# --- LAVA.TOP CONFIG ---
LAVA_SECRET_KEY = os.getenv('LAVA_SECRET_KEY', '5xRSR1dnermm7LYtMRICZclNxuEAteScAKXuWSuOdebuZvUoPnOTu12DgKYrcVvI')
LAVA_PROJECT_ID = os.getenv('LAVA_PROJECT_ID', 'YOUR_LAVA_PROJECT_ID_HERE')
WEBHOOK_HOST = os.getenv('WEBHOOK_HOST', 'http://YOUR_SERVER_IP:8080')
WEBHOOK_PORT = int(os.getenv('WEBHOOK_PORT', '8080'))

# --- Logic Config ---
ADMIN_IDS: List[int] = [OWNER_ID]
MAX_WORKERS_PER_ORDER = 3
WORKER_PERCENT = 0.7
REFERRAL_PERCENT = 0.10  # 10%

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- LEGAL LINKS (Telegraph) ---
PRIVACY_POLICY_URL = "https://telegra.ph/Politika-konfidencialnosti-08-15-17"
USER_AGREEMENT_URL = "https://telegra.ph/Polzovatelskoe-soglashenie-08-15-10"

# --- DB Helper ---
def init_db() -> None:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # Users
    cur.execute('''
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY,
        tg_id INTEGER UNIQUE,
        username TEXT,
        pubg_id TEXT,
        registered_at TEXT,
        balance REAL DEFAULT 0,
        invited_by INTEGER
    )
    ''')
    try: cur.execute("ALTER TABLE users ADD COLUMN balance REAL DEFAULT 0")
    except: pass
    try: cur.execute("ALTER TABLE users ADD COLUMN invited_by INTEGER")
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

    # Orders
    cur.execute('''
    CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        product_id INTEGER,
        price REAL,
        status TEXT,
        created_at TEXT,
        pubg_id TEXT,
        payment_id TEXT,
        promo_code TEXT,
        started_at TEXT,
        done_at TEXT
    )
    ''')
    try: cur.execute("ALTER TABLE orders ADD COLUMN payment_id TEXT")
    except: pass
    try: cur.execute("ALTER TABLE orders ADD COLUMN promo_code TEXT")
    except: pass

    # Promocodes
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

    # Workers stuff
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

# --- LAVA PAYMENT LOGIC ---
async def create_lava_invoice(order_id: int, amount: float):
    url = "https://api.lava.ru/business/invoice/create"
    
    data = {
        "sum": float(amount),
        "orderId": str(order_id),
        "shopId": LAVA_PROJECT_ID,
        "hookUrl": f"{WEBHOOK_HOST}/lava_webhook",
        "comment": f"Order {order_id}"
    }
    
    json_str = json.dumps(data)
    signature = hmac.new(
        bytes(LAVA_SECRET_KEY, 'utf-8'),
        msg=bytes(json_str, 'utf-8'),
        digestmod=hashlib.sha256
    ).hexdigest()
    
    headers = {
        "Signature": signature,
        "Content-Type": "application/json"
    }
    
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(url, json=data, headers=headers) as resp:
                result = await resp.json()
                if result.get('status') == 200 or result.get('success'):
                    return result['data']['url'], result['data']['id']
                else:
                    logger.error(f"Lava create error: {result}")
                    return None, None
        except Exception as e:
            logger.error(f"Lava connection error: {e}")
            return None, None

# --- WEBHOOK SERVER ---
async def handle_lava_webhook(request):
    try:
        data = await request.json()
        
        req_signature = request.headers.get('Authorization') or request.headers.get('Signature')
        body_bytes = await request.read()
        calc_signature = hmac.new(
            bytes(LAVA_SECRET_KEY, 'utf-8'),
            msg=body_bytes,
            digestmod=hashlib.sha256
        ).hexdigest()
        
        if req_signature and req_signature != calc_signature:
             return web.Response(status=403, text="Invalid signature")

        order_id_str = data.get('orderId')
        status = data.get('status')
        
        if status == 'success' or status == 'completed':
            await process_successful_payment(int(order_id_str))
            
        return web.Response(text="OK")
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return web.Response(status=500, text="Error")

async def process_successful_payment(order_id: int):
    row = db_execute('SELECT status, price, user_id, product_id, pubg_id FROM orders WHERE id=?', (order_id,), fetch=True)
    if not row or row[0][0] == 'paid':
        return
    
    status, price, user_id, prod_id, pubg_id = row[0]
    
    db_execute('UPDATE orders SET status=?, created_at=? WHERE id=?', ('paid', now_iso(), order_id))
    
    u_row = db_execute('SELECT invited_by, username, tg_id FROM users WHERE id=?', (user_id,), fetch=True)
    if u_row:
        inviter_id, buyer_username, buyer_tg_id = u_row[0]
        
        try:
            app = ApplicationBuilder().token(TG_BOT_TOKEN).build()
            await app.bot.send_message(buyer_tg_id, f"✅ Оплата заказа #{order_id} прошла успешно! Ищем исполнителей...")
            
            prod_row = db_execute('SELECT name FROM products WHERE id=?', (prod_id,), fetch=True)
            pname = prod_row[0][0] if prod_row else '?'
            
            admin_msg = (f"💰 НОВЫЙ ЗАКАЗ (LAVA) #{order_id}\n"
                         f"Товар: {pname}\nСумма: {price}₽\nPUBG: {pubg_id}\n"
                         f"Юзер: @{buyer_username}")
            
            kb = build_admin_keyboard_for_order(order_id, 'paid')
            await app.bot.send_message(ADMIN_CHAT_ID, admin_msg, reply_markup=kb)

        except Exception as e:
            logger.error(f"Notification error: {e}")

        if inviter_id:
            bonus = price * REFERRAL_PERCENT
            db_execute('UPDATE users SET balance = balance + ? WHERE tg_id=?', (bonus, inviter_id))
            try:
                await app.bot.send_message(inviter_id, f"🎉 Ваш реферал сделал заказ! Вам начислено +{bonus}₽")
            except: pass

# --- UI / Keyboards ---
MAIN_MENU = ReplyKeyboardMarkup(
    [
        [KeyboardButton('📦 Каталог'), KeyboardButton('🧾 Мои заказы')],
        [KeyboardButton('💰 Баланс'), KeyboardButton('🎮 Привязать PUBG ID')],
        [KeyboardButton('📞 Поддержка'), KeyboardButton('📄 Документы')]
    ], resize_keyboard=True
)

DOCS_MENU = ReplyKeyboardMarkup(
    [[KeyboardButton('📜 Пользовательское соглашение'), KeyboardButton('🔒 Политика конфиденциальности')], [KeyboardButton('↩️ Назад')]],
    resize_keyboard=True,
)
CANCEL_BUTTON = ReplyKeyboardMarkup([[KeyboardButton('↩️ Назад')]], resize_keyboard=True)
ADMIN_PANEL_KB = ReplyKeyboardMarkup(
    [[KeyboardButton('➕ Добавить товар'), KeyboardButton('📋 Список заказов')], [KeyboardButton('↩️ Назад')]],
    resize_keyboard=True,
)

# --- Helper Functions ---
def build_admin_keyboard_for_order(order_id: int, order_status: str) -> InlineKeyboardMarkup:
    if order_status == 'paid' or order_status == 'in_progress':
        return InlineKeyboardMarkup([
            [InlineKeyboardButton('🟢 Беру', callback_data=f'take:{order_id}'),
             InlineKeyboardButton('🔴 Сняться', callback_data=f'leave:{order_id}')],
            [InlineKeyboardButton('▶ Взял', callback_data=f'status:{order_id}:in_progress'),
             InlineKeyboardButton('🏁 Готово', callback_data=f'status:{order_id}:done')],
        ])
    return InlineKeyboardMarkup([[InlineKeyboardButton('ℹ️ Инфо', callback_data=f'detail_order:{order_id}')]])

# --- HANDLERS ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    args = context.args
    
    exists = db_execute('SELECT id FROM users WHERE tg_id=?', (user.id,), fetch=True)
    if not exists:
        referrer_id = None
        if args and args[0].isdigit():
            referrer_id = int(args[0])
            if referrer_id == user.id: referrer_id = None
        
        db_execute('INSERT INTO users (tg_id, username, registered_at, balance, invited_by) VALUES (?, ?, ?, 0, ?)',
                   (user.id, user.username or '', now_iso(), referrer_id))
        
        if referrer_id:
            try: await context.bot.send_message(referrer_id, f"👤 По вашей ссылке пришел новый пользователь!")
            except: pass
    
    text = f"Привет, {user.first_name}!\nДобро пожаловать в Metro Shop.\n\n🔗 Твоя реферальная ссылка:\nhttps://t.me/{context.bot.username}?start={user.id}"
    await update.message.reply_text(text, reply_markup=MAIN_MENU)

async def balance_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    row = db_execute('SELECT balance FROM users WHERE tg_id=?', (user.id,), fetch=True)
    balance = row[0][0] if row else 0.0
    
    refs = db_execute('SELECT COUNT(*) FROM users WHERE invited_by=?', (user.id,), fetch=True)
    ref_count = refs[0][0] if refs else 0
    
    await update.message.reply_text(
        f"💰 Ваш баланс: {balance}₽\n👥 Приглашено друзей: {ref_count}\n\nВы получаете {int(REFERRAL_PERCENT*100)}% от покупок рефералов!",
        reply_markup=MAIN_MENU
    )

async def promo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text.strip().split()
    if len(text) < 2:
        await update.message.reply_text("Введите: /promo КОД")
        return
    code = text[1].upper()
    
    row = db_execute('SELECT discount_percent, activations_left FROM promocodes WHERE code=?', (code,), fetch=True)
    if not row or row[0][1] <= 0:
        await update.message.reply_text("❌ Промокод недействителен.")
        return
    
    user = update.effective_user
    uid_row = db_execute('SELECT id FROM users WHERE tg_id=?', (user.id,), fetch=True)
    uid = uid_row[0][0]
    used = db_execute('SELECT 1 FROM used_promocodes WHERE user_id=? AND code=?', (uid, code), fetch=True)
    if used:
        await update.message.reply_text("❌ Вы уже использовали этот код.")
        return
        
    context.user_data['promo'] = {'code': code, 'percent': row[0][0]}
    await update.message.reply_text(f"✅ Промокод на {row[0][0]}% активирован на следующий заказ!")

async def buy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    pid = int(query.data.split(':')[1])
    user = query.from_user
    
    p = db_execute('SELECT id, name, price FROM products WHERE id=?', (pid,), fetch=True)
    if not p: return
    prod_id, name, base_price = p[0]
    
    price = base_price
    promo_data = context.user_data.get('promo')
    promo_code_used = None
    
    if promo_data:
        percent = promo_data['percent']
        price = price * (1 - percent / 100)
        promo_code_used = promo_data['code']
    
    u_row = db_execute('SELECT id, pubg_id FROM users WHERE tg_id=?', (user.id,), fetch=True)
    user_db_id, pubg_id = u_row[0]
    
    cur = db_execute('INSERT INTO orders (user_id, product_id, price, status, created_at, pubg_id, promo_code) VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING id',
               (user_db_id, prod_id, price, 'pending_payment', now_iso(), pubg_id, promo_code_used), fetch=True)
    order_id = cur[0][0]
    
    msg = await query.message.reply_text("⏳ Создаем ссылку на оплату...")
    
    if LAVA_PROJECT_ID == 'YOUR_LAVA_PROJECT_ID_HERE':
        await msg.edit_text("❌ Ошибка: Владелец бота не настроил LAVA_PROJECT_ID.")
        return

    pay_url, pay_id = await create_lava_invoice(order_id, price)
    
    if pay_url:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("💳 Оплатить", url=pay_url)]])
        await msg.edit_text(
            f"Заказ #{order_id}\nТовар: {name}\nК оплате: {price}₽\n\nНажмите кнопку ниже для оплаты:",
            reply_markup=kb
        )
        db_execute('UPDATE orders SET payment_id=? WHERE id=?', (pay_id, order_id))
        
        if promo_code_used:
             db_execute('INSERT INTO used_promocodes (user_id, code) VALUES (?, ?)', (user_db_id, promo_code_used))
             db_execute('UPDATE promocodes SET activations_left = activations_left - 1 WHERE code=?', (promo_code_used,))
             context.user_data.pop('promo', None)
    else:
        await msg.edit_text("Ошибка при создании платежа. Попробуйте позже.")

# --- Standard Handlers ---
async def products_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    prods = db_execute('SELECT id, name, price, photo FROM products', fetch=True)
    if not prods:
        await update.message.reply_text('Пусто.')
        return
    for pid, name, price, photo in prods:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton('Купить', callback_data=f'buy:{pid}')]])
        if photo: await update.message.reply_photo(photo, caption=f"{name} - {price}₽", reply_markup=kb)
        else: await update.message.reply_text(f"{name} - {price}₽", reply_markup=kb)

async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text
    
    # === ОСНОВНОЕ МЕНЮ ===
    if text == '💰 Баланс': 
        await balance_handler(update, context)
        
    elif text == '📦 Каталог': 
        await products_handler(update, context)
        
    elif text == '📄 Документы': 
        await update.message.reply_text('Выберите документ:', reply_markup=DOCS_MENU)

    # === ОБНОВЛЕННАЯ ЛОГИКА ДОКУМЕНТОВ ===
    elif text == '📜 Пользовательское соглашение': 
        # Отправляем ссылку на телеграф
        await update.message.reply_text(
            f"📜 *Пользовательское соглашение*\n\nОзнакомиться с документом можно по ссылке:\n{USER_AGREEMENT_URL}",
            parse_mode='Markdown',
            disable_web_page_preview=False 
        )
        
    elif text == '🔒 Политика конфиденциальности': 
        # Отправляем ссылку на телеграф
        await update.message.reply_text(
            f"🔒 *Политика конфиденциальности*\n\nОзнакомиться с документом можно по ссылке:\n{PRIVACY_POLICY_URL}",
            parse_mode='Markdown',
            disable_web_page_preview=False
        )
    # ======================================

    elif text == '↩️ Назад': 
        await update.message.reply_text('Меню', reply_markup=MAIN_MENU)
        
    elif text == '📞 Поддержка':
        contact = SUPPORT_CONTACT_USER
        if not contact.startswith('@') and not contact.startswith('http'): contact = '@' + contact
        await update.message.reply_text(f'Тех. поддержка: {contact}', reply_markup=MAIN_MENU)
        
    elif text == '🧾 Мои заказы':
        user = update.effective_user
        uid_row = db_execute('SELECT id FROM users WHERE tg_id=?', (user.id,), fetch=True)
        if not uid_row: return
        uid = uid_row[0][0]
        orders = db_execute('SELECT id, price, status FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 5', (uid,), fetch=True)
        if not orders: await update.message.reply_text("Нет заказов.")
        else:
            msg = "Ваши заказы:\n"
            for oid, p, s in orders: msg += f"#{oid} - {p}₽ ({s})\n"
            await update.message.reply_text(msg)
            
    # Admin commands (simple)
    elif text == '/admin' and is_admin_tg(update.effective_user.id):
        await update.message.reply_text("Админка", reply_markup=ADMIN_PANEL_KB)
    elif text == '📋 Список заказов' and is_admin_tg(update.effective_user.id):
         # logic for listing orders
         await update.message.reply_text("Используйте веб-интерфейс или базу данных для полного списка.")

# --- MAIN EXECUTION ---
async def run_bot_and_webserver():
    init_db()
    
    app = ApplicationBuilder().token(TG_BOT_TOKEN).build()
    
    app.add_handler(CommandHandler('start', start))
    app.add_handler(CommandHandler('promo', promo_handler))
    app.add_handler(CommandHandler('balance', balance_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))
    app.add_handler(CallbackQueryHandler(buy_callback, pattern=r'^buy:'))
    
    await app.initialize()
    await app.start()
    
    server = web.Application()
    server.router.add_post('/lava_webhook', handle_lava_webhook)
    runner = web.AppRunner(server)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', WEBHOOK_PORT)
    await site.start()
    
    print(f"🚀 Bot started. Webhook listening on port {WEBHOOK_PORT}")
    
    await app.updater.start_polling()
    
    try:
        while True:
            await asyncio.sleep(3600)
    except KeyboardInterrupt:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
        await runner.cleanup()

if __name__ == "__main__":
    try:
        asyncio.run(run_bot_and_webserver())
    except KeyboardInterrupt:
        pass
