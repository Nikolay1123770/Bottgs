#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Metro Shop Telegram Bot (bot.py)
Features:
- Button-based menu
- User registration (PUBG ID)
- Browse shop and buy products with payment screenshot
- Admin panel: confirm/reject payments (only admins)
- Performer flow: after payment confirmation performers press "Беру"/"Сняться"
- Up to MAX_WORKERS_PER_ORDER performers per order
Requires: python-telegram-bot v20+
"""

import os
import sqlite3
import logging
from datetime import datetime
from typing import List, Optional

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
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
TG_BOT_TOKEN = os.getenv('TG_BOT_TOKEN', '8269807126:AAGnM0QssM3NganDmQXHftxfu9itaOujvWA')
OWNER_ID = int(os.getenv('OWNER_ID', '8473513085'))
ADMIN_CHAT_ID = int(os.getenv('ADMIN_CHAT_ID', '-1003448809517'))
NOTIFY_CHAT_IDS = [int(x) for x in os.getenv('NOTIFY_CHAT_IDS', '-1003448809517').split(',') if x.strip()]
DB_PATH = os.getenv('DB_PATH', 'metro_shop.db')

# bot-level admin ids (owner + optional extra)
ADMIN_IDS: List[int] = [OWNER_ID]
if os.getenv('ADMIN_IDS'):
    ADMIN_IDS = [int(x) for x in os.getenv('ADMIN_IDS').split(',') if x.strip()]

# Maximum number of performers per order
MAX_WORKERS_PER_ORDER = 3

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


# --- DB helpers ---
def init_db() -> None:
    """Create tables. products now has `photo` column that stores Telegram file_id (TEXT)."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute('''
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY,
        tg_id INTEGER UNIQUE,
        username TEXT,
        pubg_id TEXT,
        registered_at TEXT
    )
    ''')

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
    CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        product_id INTEGER,
        price REAL,
        status TEXT,
        created_at TEXT,
        payment_screenshot_file_id TEXT,
        pubg_id TEXT,
        admin_notes TEXT
    )
    ''')

    cur.execute('''
    CREATE TABLE IF NOT EXISTS order_workers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id INTEGER,
        worker_id INTEGER,
        worker_username TEXT,
        taken_at TEXT
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
    [[KeyboardButton('📦 Каталог'), KeyboardButton('🧾 Мои заказы')],
     [KeyboardButton('🎮 Привязать PUBG ID'), KeyboardButton('📞 Поддержка')]],
    resize_keyboard=True,
)

CANCEL_BUTTON = ReplyKeyboardMarkup([[KeyboardButton('↩️ Назад')]], resize_keyboard=True)

ADMIN_PANEL_KB = ReplyKeyboardMarkup(
    [[KeyboardButton('➕ Добавить товар'), KeyboardButton('✏️ Редактировать товар'), KeyboardButton('🗑️ Удалить товар')],
     [KeyboardButton('📋 Список заказов'), KeyboardButton('↩️ Назад')]],
    resize_keyboard=True,
)


# --- Helper functions for order messages & performer list ---
def format_performers_for_caption(order_id: int) -> str:
    rows = db_execute('SELECT worker_id, worker_username FROM order_workers WHERE order_id=? ORDER BY id', (order_id,), fetch=True)
    if not rows:
        return 'Исполнители: —'
    parts = []
    for worker_id, worker_username in rows:
        if worker_username:
            parts.append(f'@{worker_username}' if not worker_username.startswith('@') else worker_username)
        else:
            parts.append(str(worker_id))
    return 'Исполнители: ' + ', '.join(parts)


def build_admin_keyboard_for_order(order_id: int, order_status: str) -> InlineKeyboardMarkup:
    """
    Build inline keyboard for admin-group order message.
    - If order_status is 'paid' -> show performer take/leave.
    - Otherwise -> admin confirm/reject.
    """
    if order_status == 'paid':
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton('🟢 Беру', callback_data=f'take:{order_id}'),
             InlineKeyboardButton('🔴 Сняться', callback_data=f'leave:{order_id}')],
        ])
    else:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton('✅ Подтвердить оплату', callback_data=f'confirm:{order_id}'),
             InlineKeyboardButton('❌ Отклонить', callback_data=f'reject:{order_id}')],
        ])
    return kb


def build_caption_for_admin_message(order_id: int, buyer_tg: str, pubg_id: Optional[str], product: str, price: float, created_at: str, status: str) -> str:
    base_lines = [
        f'📦 Заказ #{order_id}',
        f'Пользователь: {buyer_tg}',
        f'PUBG ID: {pubg_id or "не указан"}',
        f'Товар: {product}',
        f'Сумма: {price}₽',
        f'Статус: {status}',
        f'Время: {created_at}',
        format_performers_for_caption(order_id),
    ]
    return '\n'.join(base_lines)


# --- Special handler: ignore any messages in admin group (so bot doesn't reply to normal texts there) ---
async def ignore_admin_group(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    return


# --- Admin product flow helpers (per-user state in context.user_data) ---
def start_product_flow(user_data: dict) -> None:
    user_data['product_flow'] = {'stage': 'name', 'data': {}}


def clear_product_flow(user_data: dict) -> None:
    user_data.pop('product_flow', None)


# --- Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None:
        return
    db_execute('INSERT OR IGNORE INTO users (tg_id, username, registered_at) VALUES (?, ?, ?)',
               (user.id, user.username or '', now_iso()))
    text = (
        f"Привет, {user.first_name}!\n"
        "Добро пожаловать в Metro Shop — быстрый способ заказать сопровождение в Metro Royale.\n\n"
        "Привяжите PUBG ID через кнопку в меню ниже."
    )
    if update.message:
        await update.message.reply_text(text, reply_markup=MAIN_MENU)


async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # ignore admin group messages
    if update.effective_chat and update.effective_chat.id == ADMIN_CHAT_ID:
        return

    if update.message is None or update.message.text is None:
        return
    text = update.message.text.strip()
    user = update.effective_user

    # If admin is in product add/edit flow, route to handlers for text inputs
    if context.user_data.get('product_flow'):
        await handle_add_product_flow(update, context)
        return
    if context.user_data.get('edit_flow'):
        await handle_edit_product_flow(update, context)
        return

    # admin command
    if text == '/admin':
        await admin_menu(update, context)
        return

    if text == '📦 Каталог':
        await products_handler(update, context)
        return
    if text == '🧾 Мои заказы':
        await my_orders(update, context)
        return
    if text == '🎮 Привязать PUBG ID':
        await update.message.reply_text('Отправьте ваш PUBG ID (ник или цифры), или нажмите ↩️ Назад.', reply_markup=CANCEL_BUTTON)
        return
    if text == '📞 Поддержка':
        bot_username = context.bot.username or 'админ'
        await update.message.reply_text('Свяжитесь с владельцем: @' + bot_username, reply_markup=MAIN_MENU)
        return
    if text == '↩️ Назад':
        await update.message.reply_text('Вернулись в меню.', reply_markup=MAIN_MENU)
        return

    # Admin panel buttons
    if text == '➕ Добавить товар' and is_admin_tg(user.id):
        start_product_flow(context.user_data)
        await update.message.reply_text('Добавление товара — шаг 1/4.\nВведите название товара или нажмите /cancel для отмены.', reply_markup=CANCEL_BUTTON)
        return

    if text == '✏️ Редактировать товар' and is_admin_tg(user.id):
        # start edit flow: ask product id (text flow)
        context.user_data['edit_flow'] = {'stage': 'select', 'product_id': None}
        prods = db_execute('SELECT id, name, price FROM products ORDER BY id', fetch=True)
        if not prods:
            await update.message.reply_text('Нет товаров для редактирования.', reply_markup=ADMIN_PANEL_KB)
            context.user_data.pop('edit_flow', None)
            return
        lines = [f'ID {pid}: {name} — {price}₽' for pid, name, price in prods]
        await update.message.reply_text('Выберите ID товара для редактирования:\n\n' + '\n'.join(lines), reply_markup=CANCEL_BUTTON)
        return

    if text == '🗑️ Удалить товар' and is_admin_tg(user.id):
        prods = db_execute('SELECT id, name, price FROM products ORDER BY id', fetch=True)
        if not prods:
            await update.message.reply_text('Нет товаров для удаления.', reply_markup=ADMIN_PANEL_KB)
            return
        lines = [f'ID {pid}: {name} — {price}₽' for pid, name, price in prods]
        await update.message.reply_text('Отправьте ID товара для удаления:\n\n' + '\n'.join(lines), reply_markup=CANCEL_BUTTON)
        context.user_data['awaiting_delete_id'] = True
        return

    if text == '📋 Список заказов' and is_admin_tg(user.id):
        await list_orders_admin(update, context)
        return

    # Heuristic: user sending PUBG ID
    if text and len(text) <= 32 and ' ' not in text and text != '/start':
        db_execute('INSERT OR IGNORE INTO users (tg_id, username, registered_at) VALUES (?, ?, ?)',
                   (user.id, user.username or '', now_iso()))
        db_execute('UPDATE users SET pubg_id=? WHERE tg_id=?', (text, user.id))
        await update.message.reply_text(f'PUBG ID сохранён: {text}', reply_markup=MAIN_MENU)
        return

    # Admin delete id handling
    if context.user_data.pop('awaiting_delete_id', False) and is_admin_tg(user.id):
        try:
            did = int(text)
        except Exception:
            await update.message.reply_text('Неверный ID.', reply_markup=ADMIN_PANEL_KB)
            return
        row = db_execute('SELECT name FROM products WHERE id=?', (did,), fetch=True)
        if not row:
            await update.message.reply_text('Товар с таким ID не найден.', reply_markup=ADMIN_PANEL_KB)
            return
        db_execute('DELETE FROM products WHERE id=?', (did,))
        await update.message.reply_text(f'Товар #{did} удалён.', reply_markup=ADMIN_PANEL_KB)
        return

    # Admin add-product quick-format (legacy)
    if '|' in text and is_admin_tg(user.id):
        await add_product_text_handler(update, context)
        return

    await update.message.reply_text('Неизвестная команда. Выберите действие в меню.', reply_markup=MAIN_MENU)


# --- Add product interactive flow ---
async def handle_add_product_flow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles per-step interactive product addition: name -> price -> desc -> photo"""
    msg = update.message
    if msg is None:
        return
    user = update.effective_user
    if not is_admin_tg(user.id):
        clear_product_flow(context.user_data)
        return

    flow = context.user_data.get('product_flow')
    if not flow:
        return

    stage = flow.get('stage')

    # Cancel
    if msg.text and msg.text.strip().lower() in ['/cancel', '↩️ назад']:
        clear_product_flow(context.user_data)
        await msg.reply_text('Добавление товара отменено.', reply_markup=ADMIN_PANEL_KB)
        return

    # Stage handlers
    if stage == 'name':
        name = (msg.text or '').strip()
        if not name:
            await msg.reply_text('Название не может быть пустым. Введите название товара.')
            return
        flow['data']['name'] = name
        flow['stage'] = 'price'
        await msg.reply_text('Шаг 2/4. Введите цену (числом), например: 300', reply_markup=CANCEL_BUTTON)
        return

    if stage == 'price':
        text = (msg.text or '').strip()
        try:
            price = float(text)
            if price < 0:
                raise ValueError()
        except Exception:
            await msg.reply_text('Неверная цена. Введите цену числом, например: 300')
            return
        flow['data']['price'] = price
        flow['stage'] = 'desc'
        await msg.reply_text('Шаг 3/4. Введите описание товара (короткое).', reply_markup=CANCEL_BUTTON)
        return

    if stage == 'desc':
        desc = (msg.text or '').strip()
        flow['data']['description'] = desc
        flow['stage'] = 'photo'
        await msg.reply_text('Шаг 4/4. Отправьте фото товара (как фото).', reply_markup=CANCEL_BUTTON)
        return

    if stage == 'photo':
        # This function can be triggered by photo_router when admin sends photo
        if not msg.photo:
            await msg.reply_text('Пожалуйста, отправьте изображение (как фото).')
            return
        photo = msg.photo[-1].file_id
        data = flow['data']
        name = data.get('name')
        price = data.get('price')
        desc = data.get('description')
        created = now_iso()
        db_execute('INSERT INTO products (name, description, price, photo, created_at) VALUES (?, ?, ?, ?, ?)',
                   (name, desc, price, photo, created))
        clear_product_flow(context.user_data)
        await msg.reply_text(f'Товар добавлен: {name} — {price}₽', reply_markup=ADMIN_PANEL_KB)
        return


# --- Edit product interactive flow ---
async def handle_edit_product_flow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    edit_flow stages:
      - select : expecting product id (text)
      - choose_field : present inline buttons (handled via callback)
      - editing_name / editing_price / editing_desc / editing_photo : awaiting input
    """
    msg = update.message
    if msg is None:
        return
    user = update.effective_user
    if not is_admin_tg(user.id):
        context.user_data.pop('edit_flow', None)
        return

    flow = context.user_data.get('edit_flow', {})
    stage = flow.get('stage')

    # cancel
    if msg.text and msg.text.strip().lower() in ['/cancel', '↩️ назад']:
        context.user_data.pop('edit_flow', None)
        await msg.reply_text('Редактирование отменено.', reply_markup=ADMIN_PANEL_KB)
        return

    if stage == 'select':
        # expect product id (text path)
        try:
            pid = int((msg.text or '').strip())
        except Exception:
            await msg.reply_text('Неверный ID. Отправьте числовой ID товара, который хотите редактировать.')
            return
        row = db_execute('SELECT id, name, price, description FROM products WHERE id=?', (pid,), fetch=True)
        if not row:
            await msg.reply_text('Товар не найден. Попробуйте другой ID.')
            context.user_data.pop('edit_flow', None)
            return
        context.user_data['edit_flow']['product_id'] = pid
        context.user_data['edit_flow']['stage'] = 'choose_field'
        # show inline buttons for fields
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton('Название', callback_data=f'editfield:name:{pid}'),
             InlineKeyboardButton('Цена', callback_data=f'editfield:price:{pid}')],
            [InlineKeyboardButton('Описание', callback_data=f'editfield:desc:{pid}'),
             InlineKeyboardButton('Фото', callback_data=f'editfield:photo:{pid}')],
            [InlineKeyboardButton('Отмена', callback_data=f'editfield:cancel:{pid}')]
        ])
        await msg.reply_text(f'Выбран товар #{pid}. Выберите поле для редактирования.', reply_markup=kb)
        return

    # handle awaited text/photo for editing fields
    if stage in ('editing_name', 'editing_price', 'editing_desc'):
        pid = flow.get('product_id')
        if pid is None:
            context.user_data.pop('edit_flow', None)
            await msg.reply_text('Ошибка состояния. Попробуйте заново.', reply_markup=ADMIN_PANEL_KB)
            return
        if stage == 'editing_name':
            name = (msg.text or '').strip()
            if not name:
                await msg.reply_text('Название не может быть пустым. Введите название.')
                return
            db_execute('UPDATE products SET name=? WHERE id=?', (name, pid))
            await msg.reply_text(f'Название обновлено для #{pid}.', reply_markup=ADMIN_PANEL_KB)
        elif stage == 'editing_price':
            try:
                price = float((msg.text or '').strip())
                if price < 0:
                    raise ValueError()
            except Exception:
                await msg.reply_text('Неверная цена. Введите число, например: 300')
                return
            db_execute('UPDATE products SET price=? WHERE id=?', (price, pid))
            await msg.reply_text(f'Цена обновлена для #{pid}.', reply_markup=ADMIN_PANEL_KB)
        elif stage == 'editing_desc':
            desc = (msg.text or '').strip()
            db_execute('UPDATE products SET description=? WHERE id=?', (desc, pid))
            await msg.reply_text(f'Описание обновлено для #{pid}.', reply_markup=ADMIN_PANEL_KB)
        context.user_data.pop('edit_flow', None)
        return

    if stage == 'editing_photo':
        # expect photo (this will be routed from photo_router)
        if not msg.photo:
            await msg.reply_text('Пожалуйста, отправьте фото (в виде фото).')
            return
        pid = flow.get('product_id')
        if pid is None:
            context.user_data.pop('edit_flow', None)
            await msg.reply_text('Ошибка состояния. Попробуйте заново.', reply_markup=ADMIN_PANEL_KB)
            return
        file_id = msg.photo[-1].file_id
        db_execute('UPDATE products SET photo=? WHERE id=?', (file_id, pid))
        await msg.reply_text(f'Фото обновлено для #{pid}.', reply_markup=ADMIN_PANEL_KB)
        context.user_data.pop('edit_flow', None)
        return


# Callback to handle which field admin chooses to edit
async def editfield_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if q is None:
        return
    await q.answer()
    data = q.data or ''
    if not data.startswith('editfield:'):
        return
    _, field, pid_str = data.split(':', 2)
    try:
        pid = int(pid_str)
    except ValueError:
        await q.edit_message_text('Неверный product id.')
        return
    user = q.from_user
    if not is_admin_tg(user.id):
        await q.answer(text='Только админы.', show_alert=True)
        return

    if field == 'cancel':
        context.user_data.pop('edit_flow', None)
        try:
            await q.edit_message_text('Редактирование отменено.', reply_markup=None)
        except Exception:
            pass
        return

    # set edit_flow state and ask for value accordingly
    context.user_data['edit_flow'] = {'stage': None, 'product_id': pid}
    if field == 'name':
        context.user_data['edit_flow']['stage'] = 'editing_name'
        try:
            await q.message.reply_text('Введите новое название товара (текст).', reply_markup=CANCEL_BUTTON)
        except Exception:
            pass
    elif field == 'price':
        context.user_data['edit_flow']['stage'] = 'editing_price'
        try:
            await q.message.reply_text('Введите новую цену (число).', reply_markup=CANCEL_BUTTON)
        except Exception:
            pass
    elif field == 'desc':
        context.user_data['edit_flow']['stage'] = 'editing_desc'
        try:
            await q.message.reply_text('Введите новое описание.', reply_markup=CANCEL_BUTTON)
        except Exception:
            pass
    elif field == 'photo':
        context.user_data['edit_flow']['stage'] = 'editing_photo'
        try:
            await q.message.reply_text('Отправьте новое фото (в виде фото).', reply_markup=CANCEL_BUTTON)
        except Exception:
            pass


# Callback to delete product by inline button (optional)
async def delete_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if q is None:
        return
    await q.answer()
    data = q.data or ''
    if not data.startswith('delete:'):
        return
    _, pid_str = data.split(':', 1)
    try:
        pid = int(pid_str)
    except ValueError:
        await q.edit_message_text('Неверный product id.')
        return
    user = q.from_user
    if not is_admin_tg(user.id):
        await q.answer(text='Только админы.', show_alert=True)
        return
    row = db_execute('SELECT name FROM products WHERE id=?', (pid,), fetch=True)
    if not row:
        await q.edit_message_text('Товар не найден.')
        return
    db_execute('DELETE FROM products WHERE id=?', (pid,))
    try:
        await q.edit_message_text(f'Товар #{pid} удалён.')
    except Exception:
        pass


# New: callback to start edit flow (from product detail)
async def edit_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if q is None:
        return
    await q.answer()
    data = q.data or ''
    if not data.startswith('edit:'):
        return
    _, pid_str = data.split(':', 1)
    try:
        pid = int(pid_str)
    except ValueError:
        await q.edit_message_text('Неверный product id.')
        return
    user = q.from_user
    if not is_admin_tg(user.id):
        await q.answer(text='Только админы.', show_alert=True)
        return

    row = db_execute('SELECT id, name, price FROM products WHERE id=?', (pid,), fetch=True)
    if not row:
        try:
            await q.edit_message_text('Товар не найден.')
        except Exception:
            pass
        return

    # set edit_flow and show inline choose-field keyboard
    context.user_data['edit_flow'] = {'stage': 'choose_field', 'product_id': pid}
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton('Название', callback_data=f'editfield:name:{pid}'),
         InlineKeyboardButton('Цена', callback_data=f'editfield:price:{pid}')],
        [InlineKeyboardButton('Описание', callback_data=f'editfield:desc:{pid}'),
         InlineKeyboardButton('Фото', callback_data=f'editfield:photo:{pid}')],
        [InlineKeyboardButton('Отмена', callback_data=f'editfield:cancel:{pid}')]
    ])
    try:
        await q.message.reply_text(f'Редактирование товара #{pid}. Выберите поле для редактирования.', reply_markup=kb)
    except Exception:
        pass


# --- Products display and buy flows ---
async def products_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    products = db_execute('SELECT id, name, description, price, photo FROM products ORDER BY id', fetch=True)
    if not products:
        await update.message.reply_text('Каталог пуст. Админ может добавить товары.', reply_markup=MAIN_MENU)
        return

    for pid, name, desc, price, photo in products:
        caption = f"🛒 *{name}*\n{desc or ''}\n\n💰 Цена: *{price}₽*"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(text=f'Купить — {price}₽', callback_data=f'buy:{pid}'),
             InlineKeyboardButton(text='ℹ️ Подробнее', callback_data=f'detail:{pid}')]
        ])
        try:
            if photo:
                if update.message:
                    await update.message.reply_photo(photo=photo, caption=caption, reply_markup=kb, parse_mode='Markdown')
                else:
                    await context.bot.send_photo(chat_id=update.effective_chat.id, photo=photo, caption=caption, reply_markup=kb, parse_mode='Markdown')
            else:
                if update.message:
                    await update.message.reply_markdown(caption, reply_markup=kb)
                else:
                    await context.bot.send_message(chat_id=update.effective_chat.id, text=caption, reply_markup=kb)
        except Exception:
            try:
                await context.bot.send_message(chat_id=update.effective_chat.id, text=caption, reply_markup=kb)
            except Exception:
                logger.exception("Failed to send product %s", pid)

    if update.message:
        await update.message.reply_text('Выберите товар, чтобы купить, или вернитесь в меню.', reply_markup=MAIN_MENU)


# Product details callback
async def product_detail_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if q is None:
        return
    await q.answer()
    data = q.data or ''
    if not data.startswith('detail:'):
        return
    _, pid_str = data.split(':', 1)
    try:
        pid = int(pid_str)
    except ValueError:
        return
    row = db_execute('SELECT name, description, price, photo FROM products WHERE id=?', (pid,), fetch=True)
    if not row:
        try:
            await q.edit_message_text('Товар не найден.')
        except Exception:
            pass
        return
    name, desc, price, photo = row[0]
    caption = f"*{name}*\n\n{desc or ''}\n\n💰 Цена: *{price}₽*"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(text=f'Купить — {price}₽', callback_data=f'buy:{pid}'),
         InlineKeyboardButton(text='Редактировать', callback_data=f'edit:{pid}'),
         InlineKeyboardButton(text='Удалить', callback_data=f'delete:{pid}')]
    ])
    try:
        if photo:
            await q.message.reply_photo(photo=photo, caption=caption, parse_mode='Markdown', reply_markup=kb)
        else:
            await q.message.reply_markdown(caption, reply_markup=kb)
    except Exception:
        try:
            await q.edit_message_text(caption)
        except Exception:
            pass


async def my_orders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None:
        return
    row = db_execute('SELECT id FROM users WHERE tg_id=?', (user.id,), fetch=True)
    if not row:
        await update.message.reply_text('Вы ещё не зарегистрированы.', reply_markup=MAIN_MENU)
        return
    user_db_id = row[0][0]
    rows = db_execute(
        'SELECT o.id, p.name, o.price, o.status FROM orders o JOIN products p ON o.product_id=p.id WHERE o.user_id=? ORDER BY o.id DESC LIMIT 50',
        (user_db_id,), fetch=True)
    if not rows:
        await update.message.reply_text('У вас пока нет заказов.', reply_markup=MAIN_MENU)
        return
    lines = []
    for oid, pname, price, status in rows:
        perf_rows = db_execute('SELECT worker_username FROM order_workers WHERE order_id=? ORDER BY id', (oid,), fetch=True)
        perflist = ', '.join([r[0] or str(r[0]) for r in perf_rows]) if perf_rows else '-'
        lines.append(f'#{oid} {pname} — {price}₽ — {status} — Исполнители: {perflist}')
    await update.message.reply_text('\n'.join(lines), reply_markup=MAIN_MENU)


# User pressed "Купить" inline button
async def buy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    try:
        await query.answer()
    except BadRequest:
        pass

    data = query.data or ''
    if not data.startswith('buy:'):
        return
    _, pid_str = data.split(':', 1)
    try:
        pid = int(pid_str)
    except ValueError:
        return

    p = db_execute('SELECT id, name, price FROM products WHERE id=?', (pid,), fetch=True)
    if not p:
        try:
            await query.edit_message_text('Товар не найден.')
        except Exception:
            pass
        return
    prod_id, name, price = p[0]

    user = query.from_user
    db_execute('INSERT OR IGNORE INTO users (tg_id, username, registered_at) VALUES (?, ?, ?)',
               (user.id, user.username or '', now_iso()))
    user_row = db_execute('SELECT id, pubg_id FROM users WHERE tg_id=?', (user.id,), fetch=True)
    user_db_id = user_row[0][0]
    pubg_id = user_row[0][1]

    # create order awaiting screenshot
    db_execute('INSERT INTO orders (user_id, product_id, price, status, created_at, pubg_id) VALUES (?, ?, ?, ?, ?, ?)',
               (user_db_id, prod_id, price, 'awaiting_screenshot', now_iso(), pubg_id))

    try:
        await query.message.reply_text(
            f'Вы выбрали: {name} — {price}₽\n\n'
            'Отправьте скриншот оплаты (перевод/квитанция) в этот чат.\n'
            'Если вы не указали PUBG ID — добавьте его в сообщении.'
        )
    except Exception:
        pass


# --- Photo routing: either admin product-photo flows OR payment screenshots ---
async def photo_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Route incoming photos:
    - If the user is an admin and is in product_flow or edit_flow expecting a photo -> forward to those handlers.
    - Otherwise treat as payment screenshot.
    """
    msg = update.message
    if msg is None:
        return
    user = msg.from_user
    if user is None:
        return

    # If admin is adding a product and expecting photo -> handle there
    if is_admin_tg(user.id) and context.user_data.get('product_flow'):
        flow = context.user_data.get('product_flow', {})
        if flow.get('stage') == 'photo':
            await handle_add_product_flow(update, context)
            return

    # If admin is editing and expecting photo -> handle edit flow
    if is_admin_tg(user.id) and context.user_data.get('edit_flow'):
        flow = context.user_data.get('edit_flow', {})
        if flow.get('stage') == 'editing_photo':
            await handle_edit_product_flow(update, context)
            return

    # Otherwise treat photo as payment screenshot
    await payment_photo_handler(update, context)


async def payment_photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # ignore if in admin chat (prevents users spamming there)
    if update.effective_chat and update.effective_chat.id == ADMIN_CHAT_ID:
        return

    if update.message is None:
        return
    message = update.message
    user = update.effective_user
    if user is None:
        return
    tg_id = user.id

    user_row = db_execute('SELECT id, pubg_id FROM users WHERE tg_id=?', (tg_id,), fetch=True)
    if not user_row:
        await message.reply_text('Сначала выберите товар в каталоге.', reply_markup=MAIN_MENU)
        return
    user_db_id, pubg_id = user_row[0]
    order_row = db_execute('SELECT id, product_id, price, created_at FROM orders WHERE user_id=? AND status=? ORDER BY id DESC LIMIT 1',
                           (user_db_id, 'awaiting_screenshot'), fetch=True)
    if not order_row:
        await message.reply_text('У вас нет активных заказов, ожидающих скриншота.', reply_markup=MAIN_MENU)
        return
    order_id, product_id, price, created_at = order_row[0]

    if not message.photo:
        await message.reply_text('Пожалуйста, отправьте изображение (скриншот оплаты).', reply_markup=MAIN_MENU)
        return

    photo = message.photo[-1]
    file_id = photo.file_id
    db_execute('UPDATE orders SET payment_screenshot_file_id=?, status=? WHERE id=?', (file_id, 'pending_verification', order_id))

    product = db_execute('SELECT name FROM products WHERE id=?', (product_id,), fetch=True)[0][0]
    tg_username = user.username or f'{user.first_name} {user.last_name or ""}'.strip()

    # Build caption and keyboard (confirm/reject)
    caption = build_caption_for_admin_message(order_id, f'@{tg_username}' if user.username else str(tg_id), pubg_id, product, price, created_at, 'pending_verification')
    kb = build_admin_keyboard_for_order(order_id, 'pending_verification')

    # Send to admin group. If bot not in group -> log and notify owner
    try:
        await context.bot.send_photo(chat_id=ADMIN_CHAT_ID, photo=file_id, caption=caption, reply_markup=kb)
        for nid in NOTIFY_CHAT_IDS:
            try:
                await context.bot.send_message(chat_id=nid, text=f'Новый заказ #{order_id} ожидает проверки. Проверьте в админ-группе.')
            except Exception:
                pass
        await message.reply_text('Скриншот отправлен админам для проверки. Ожидайте подтверждения.', reply_markup=MAIN_MENU)
    except Exception as e:
        logger.exception('Failed to send to admin group: %s', e)
        try:
            await context.bot.send_message(chat_id=OWNER_ID, text=f'Не удалось отправить заказ #{order_id} в админ-группу. Ошибка: {e}')
        except Exception:
            pass
        await message.reply_text('Не удалось отправить заказ в админ-группу. Свяжитесь с поддержкой.', reply_markup=MAIN_MENU)


# Admin decision: confirm or reject payment
async def admin_decision(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    try:
        await query.answer()
    except BadRequest:
        pass

    data = query.data or ''
    if not (data.startswith('confirm:') or data.startswith('reject:')):
        return
    action, oid_str = data.split(':', 1)
    try:
        order_id = int(oid_str)
    except ValueError:
        return

    user = query.from_user
    if not is_admin_tg(user.id):
        try:
            await query.answer(text='Только админы могут подтверждать/отклонять оплату.', show_alert=True)
        except Exception:
            pass
        return

    order = db_execute('SELECT user_id, product_id, price, payment_screenshot_file_id, created_at FROM orders WHERE id=?', (order_id,), fetch=True)
    if not order:
        try:
            await query.answer(text='Заказ не найден.', show_alert=True)
        except Exception:
            pass
        return

    user_id, product_id, price, file_id, created_at = order[0]
    buyer_row = db_execute('SELECT tg_id, username, pubg_id FROM users WHERE id=?', (user_id,), fetch=True)
    if not buyer_row:
        buyer_tg = str(user_id)
        pubg_id = None
    else:
        buyer_tg = f"@{buyer_row[0][1]}" if buyer_row[0][1] else str(buyer_row[0][0])
        pubg_id = buyer_row[0][2]

    product_name = db_execute('SELECT name FROM products WHERE id=?', (product_id,), fetch=True)[0][0]

    if action == 'confirm':
        db_execute('UPDATE orders SET status=?, admin_notes=? WHERE id=?', ('paid', f'Оплачен и подтверждён админом {user.id}', order_id))
        caption = build_caption_for_admin_message(order_id, buyer_tg, pubg_id, product_name, price, created_at, 'paid')
        kb = build_admin_keyboard_for_order(order_id, 'paid')
        try:
            await query.edit_message_caption(caption, reply_markup=kb)
        except Exception:
            try:
                await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=caption, reply_markup=kb)
            except Exception:
                logger.exception('Failed to update admin message after confirm')
        try:
            await context.bot.send_message(chat_id=buyer_row[0][0], text=(f'Ваш заказ #{order_id} на \"{product_name}\" оплачен и подтверждён. Ожидайте исполнителей.'))
        except Exception:
            logger.warning('Failed to notify buyer')
        for nid in NOTIFY_CHAT_IDS:
            try:
                await context.bot.send_message(chat_id=nid, text=f'Заказ #{order_id} подтверждён. Ожидаем исполнителей.')
            except Exception:
                pass

    else:  # reject
        db_execute('UPDATE orders SET status=?, admin_notes=? WHERE id=?', ('rejected', f'Отклонён админом {user.id}', order_id))
        caption = build_caption_for_admin_message(order_id, buyer_tg, pubg_id, product_name, price, created_at, 'rejected')
        try:
            await query.edit_message_caption(caption)
        except Exception:
            try:
                await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=caption)
            except Exception:
                pass
        try:
            await context.bot.send_message(chat_id=buyer_row[0][0], text=(f'Ваш заказ #{order_id} был отклонён администратором. Пожалуйста, свяжитесь с поддержкой.'))
        except Exception:
            logger.warning('Failed to notify buyer')


# Performer actions: take or leave
async def performer_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    try:
        await query.answer()
    except BadRequest:
        pass

    data = query.data or ''
    if not (data.startswith('take:') or data.startswith('leave:')):
        return
    action, oid_str = data.split(':', 1)
    try:
        order_id = int(oid_str)
    except ValueError:
        return

    user = query.from_user
    worker_id = user.id
    worker_username = user.username or f'{user.first_name} {user.last_name or ""}'.strip()

    order_row = db_execute('SELECT status, product_id, price, created_at FROM orders WHERE id=?', (order_id,), fetch=True)
    if not order_row:
        try:
            await query.answer(text='Заказ не найден.', show_alert=True)
        except Exception:
            pass
        return
    status, product_id, price, created_at = order_row[0]
    if status != 'paid':
        try:
            await query.answer(text='Этот функционал доступен только после подтверждения оплаты.', show_alert=True)
        except Exception:
            pass
        return

    current = db_execute('SELECT worker_id FROM order_workers WHERE order_id=?', (order_id,), fetch=True) or []
    current_ids = [r[0] for r in current]

    if action == 'take':
        if worker_id in current_ids:
            try:
                await query.answer(text='Вы уже взяли этот заказ.', show_alert=True)
            except Exception:
                pass
            return
        if len(current_ids) >= MAX_WORKERS_PER_ORDER:
            try:
                await query.answer(text=f'Невозможно взять — максимум {MAX_WORKERS_PER_ORDER} исполнителей уже заняты.', show_alert=True)
            except Exception:
                pass
            return
        db_execute('INSERT INTO order_workers (order_id, worker_id, worker_username, taken_at) VALUES (?, ?, ?, ?)',
                   (order_id, worker_id, worker_username, now_iso()))
        try:
            await query.answer(text='Вы добавлены в исполнители.', show_alert=False)
        except Exception:
            pass

    else:  # leave
        if worker_id not in current_ids:
            try:
                await query.answer(text='Вы не являетесь исполнителем этого заказа.', show_alert=True)
            except Exception:
                pass
            return
        db_execute('DELETE FROM order_workers WHERE order_id=? AND worker_id=?', (order_id, worker_id))
        try:
            await query.answer(text='Вы сняты с выполнения заказа.', show_alert=False)
        except Exception:
            pass

    buyer_row = db_execute('SELECT u.tg_id, u.username, u.pubg_id, p.name FROM orders o JOIN users u ON o.user_id=u.id JOIN products p ON o.product_id=p.id WHERE o.id=?', (order_id,), fetch=True)
    if buyer_row:
        buyer_tg_id, buyer_username, pubg_id, product_name = buyer_row[0]
        buyer_tg = f'@{buyer_username}' if buyer_username else str(buyer_tg_id)
    else:
        buyer_tg = 'неизвестен'
        pubg_id = None
        product_name = db_execute('SELECT name FROM products WHERE id=(SELECT product_id FROM orders WHERE id=?)', (order_id,), fetch=True)[0][0]
    caption = build_caption_for_admin_message(order_id, buyer_tg, pubg_id, product_name, price, created_at, 'paid')
    kb = build_admin_keyboard_for_order(order_id, 'paid')

    try:
        await query.edit_message_caption(caption, reply_markup=kb)
    except Exception:
        try:
            await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=caption, reply_markup=kb)
        except Exception:
            logger.exception('Failed to update admin message after performer action')


# Admin panel and small admin helpers
async def admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not is_admin_tg(user.id):
        if update.message:
            await update.message.reply_text('Только админам.')
        return
    if update.message:
        await update.message.reply_text('Панель администратора:', reply_markup=ADMIN_PANEL_KB)


async def add_product_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # legacy 'price|name|desc' format
    if update.message is None:
        return
    user = update.effective_user
    if not is_admin_tg(user.id):
        return
    text = (update.message.text or '').strip()
    if not text or '|' not in text:
        await update.message.reply_text('Использование для админа: <цена>|<название>|<описание>', reply_markup=ADMIN_PANEL_KB)
        return
    try:
        price_str, name, desc = [x.strip() for x in text.split('|', 2)]
        price = float(price_str)
    except Exception:
        await update.message.reply_text('Неверный формат. Пример: 300|Сопровождение|Быстрое сопровождение', reply_markup=ADMIN_PANEL_KB)
        return
    db_execute('INSERT INTO products (name, description, price, created_at) VALUES (?, ?, ?, ?)',
               (name, desc, price, now_iso()))
    await update.message.reply_text(f'Товар добавлен: {name} — {price}₽', reply_markup=MAIN_MENU)


async def list_orders_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not is_admin_tg(user.id):
        if update.message:
            await update.message.reply_text('Только админам.')
        return
    rows = db_execute(
        'SELECT o.id, u.tg_id, u.pubg_id, p.name, o.price, o.status, o.created_at FROM orders o JOIN users u ON o.user_id=u.id JOIN products p ON o.product_id=p.id ORDER BY o.id DESC LIMIT 50',
        fetch=True)
    if not rows:
        await update.message.reply_text('Заказов нет.', reply_markup=MAIN_MENU)
        return
    text_lines = []
    for r in rows:
        oid, tg_id, pubg_id, pname, price, status, created = r
        perf_rows = db_execute('SELECT worker_username FROM order_workers WHERE order_id=? ORDER BY id', (oid,), fetch=True)
        perflist = ', '.join([pr[0] or str(pr[0]) for pr in perf_rows]) if perf_rows else '-'
        text_lines.append(f'#{oid} {pname} {price}₽ {status} tg:{tg_id} pubg:{pubg_id or "-"} — Исполнители: {perflist} — {created}')
    big = '\n'.join(text_lines)
    if len(big) <= 4000:
        await update.message.reply_text(big, reply_markup=MAIN_MENU)
    else:
        parts = [big[i:i+3500] for i in range(0, len(big), 3500)]
        for p in parts:
            await update.message.reply_text(p)
        await update.message.reply_text('Конец списка.', reply_markup=MAIN_MENU)


# New admin helper: set photo for product (reply to photo with /setphoto <id>)
async def setphoto_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not is_admin_tg(user.id):
        return
    msg = update.message
    if msg is None:
        return
    if not msg.reply_to_message or not msg.reply_to_message.photo:
        await msg.reply_text('Ответьте командой на сообщение с фото товара, например: /setphoto 3')
        return

    args = context.args or []
    if not args:
        await msg.reply_text('Использование: /setphoto <product_id> (в ответ на фото)')
        return
    try:
        pid = int(args[0])
    except ValueError:
        await msg.reply_text('Неверный product_id')
        return

    photo = msg.reply_to_message.photo[-1]
    file_id = photo.file_id

    db_execute('UPDATE products SET photo=? WHERE id=?', (file_id, pid))
    await msg.reply_text(f'Фото установлено для товара {pid}', reply_markup=ADMIN_PANEL_KB)


# Command /add <name> <price> <description> (admin only)
async def add_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not is_admin_tg(user.id):
        return
    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text('Использование: /add <название> <цена> [описание]')
        return
    name = args[0]
    try:
        price = float(args[1])
    except Exception:
        await update.message.reply_text('Цена должна быть числом')
        return
    desc = ' '.join(args[2:]) if len(args) > 2 else ''
    db_execute('INSERT INTO products (name, description, price, created_at) VALUES (?, ?, ?, ?)', (name, desc, price, now_iso()))
    await update.message.reply_text(f'Товар добавлен: {name} — {price}₽', reply_markup=ADMIN_PANEL_KB)


# Global error handler
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error(msg="Exception while handling an update:", exc_info=context.error)
    try:
        app = context.application
        await app.bot.send_message(chat_id=OWNER_ID, text=f'Error: {context.error}')
    except Exception:
        pass


def build_app():
    init_db()
    app = ApplicationBuilder().token(TG_BOT_TOKEN).build()

    # ignore messages in admin group (keeps bot quiet there)
    app.add_handler(MessageHandler(filters.Chat(ADMIN_CHAT_ID) & filters.ALL, ignore_admin_group), group=0)

    # user flows
    app.add_handler(CommandHandler('start', start), group=1)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router), group=1)

    # photo router (routes admin product photos -> product flows, else -> payment handler)
    app.add_handler(MessageHandler(filters.PHOTO & ~filters.COMMAND, photo_router), group=1)

    # callbacks for product browsing / buy
    app.add_handler(CallbackQueryHandler(buy_callback, pattern=r'^buy:'), group=1)
    app.add_handler(CallbackQueryHandler(product_detail_callback, pattern=r'^detail:'), group=1)

    # admin / performer callbacks
    app.add_handler(CallbackQueryHandler(admin_decision, pattern=r'^(confirm:|reject:)'), group=2)
    app.add_handler(CallbackQueryHandler(performer_action, pattern=r'^(take:|leave:)'), group=2)

    # product edit/delete callbacks
    app.add_handler(CallbackQueryHandler(editfield_callback, pattern=r'^editfield:'), group=2)
    app.add_handler(CallbackQueryHandler(delete_callback, pattern=r'^delete:'), group=2)
    app.add_handler(CallbackQueryHandler(edit_callback, pattern=r'^edit:'), group=2)  # opens edit flow from detail

    # admin flows / commands
    app.add_handler(CommandHandler('admin', admin_menu), group=1)
    app.add_handler(CommandHandler('add', add_command_handler), group=1)
    app.add_handler(CommandHandler('setphoto', setphoto_handler), group=1)
    # legacy quick-add (kept)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, add_product_text_handler), group=1)

    app.add_error_handler(error_handler)
    return app


if __name__ == "__main__":
    init_db()
    application = build_app()
    application.run_polling()
