"""
telegram_store_bot.py
Monolithic Telegram Store Bot (Inline-keyboard only)
Features:
- Admin: manage main buttons, sub-buttons (services), edit service name/desc/price/image,
         ban/unban, add/deduct balance (USD), broadcast, list users, search by id,
         lock/unlock service, toggle accepting orders, edit welcome/terms, maintenance mode
- Users: browse main/sub buttons, view service details (name/price/desc/image),
         order a service (collect data fields defined by admin), pay from balance,
         top-up via simulated external flow, view orders, cancel pending orders,
         receive notifications when balance changed or order status changed.
DB: SQLite (file: store_bot.db)
Library: pyTelegramBotAPI
All UI uses InlineKeyboardButtons (callbacks).
"""

import sqlite3
import os
import json
import time
from datetime import datetime
from functools import wraps
import telebot
from telebot import types

# ==========================
# CONFIG - اضف التوكن و آي دي الأدمن هنا
# ==========================
BOT_TOKEN = "REPLACE_WITH_BOT_TOKEN"
ADMIN_ID = 123456789  # استبدل برقم آي دي الأدمن (رقمي)
DB_PATH = "store_bot.db"
# ==========================

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

# --------------- Utilities & DB ----------------

def ensure_db():
    """Create tables if not exist"""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    # users: id (text), balance (real), banned (int), created_at
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY,
        balance REAL DEFAULT 0,
        banned INTEGER DEFAULT 0,
        created_at TEXT
    )""")
    # main buttons (categories)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS main_buttons (
        name TEXT PRIMARY KEY,
        image TEXT
    )""")
    # sub buttons mapping to service_id
    cur.execute("""
    CREATE TABLE IF NOT EXISTS sub_buttons (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        main_name TEXT,
        sub_name TEXT,
        service_id INTEGER
    )""")
    # services
    cur.execute("""
    CREATE TABLE IF NOT EXISTS services (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT,
        description TEXT,
        price_usd REAL DEFAULT 0,
        image TEXT,
        enabled INTEGER DEFAULT 1,
        collect_fields TEXT  -- JSON list of field names to ask user
    )""")
    # orders
    cur.execute("""
    CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT,
        service_id INTEGER,
        data TEXT,        -- JSON of collected data
        price REAL,
        status TEXT,      -- pending, processing, completed, rejected, cancelled
        created_at TEXT
    )""")
    # settings
    cur.execute("""
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )""")
    conn.commit()
    conn.close()
    # seed default settings if not present
    set_default_setting("welcome", "مرحباً! أهلاً بك في متجر الشحن. اختر من القائمة.")
    set_default_setting("terms", "شروط الاستخدام...")
    set_default_setting("accepting_orders", "1")
    set_default_setting("maintenance", "0")

def db_conn():
    return sqlite3.connect(DB_PATH)

def set_default_setting(key, value):
    conn = db_conn(); cur = conn.cursor()
    cur.execute("SELECT value FROM settings WHERE key = ?", (key,))
    if not cur.fetchone():
        cur.execute("INSERT INTO settings(key,value) VALUES(?,?)", (key, value))
        conn.commit()
    conn.close()

def get_setting(key):
    conn = db_conn(); cur = conn.cursor()
    cur.execute("SELECT value FROM settings WHERE key = ?", (key,))
    r = cur.fetchone()
    conn.close()
    return r[0] if r else None

def set_setting(key, value):
    conn = db_conn(); cur = conn.cursor()
    cur.execute("REPLACE INTO settings(key,value) VALUES(?,?)", (key, value))
    conn.commit(); conn.close()

# --------------- Helpers ----------------

def admin_only(func):
    @wraps(func)
    def wrapper(message, *args, **kwargs):
        uid = message.from_user.id if hasattr(message, "from_user") else None
        if uid != ADMIN_ID:
            try:
                bot.answer_callback_query(message.id, "غير مسموح.")
            except:
                pass
            return
        return func(message, *args, **kwargs)
    return wrapper

def user_exists_create(uid):
    conn = db_conn(); cur = conn.cursor()
    cur.execute("SELECT id FROM users WHERE id = ?", (str(uid),))
    if not cur.fetchone():
        cur.execute("INSERT INTO users(id,balance,banned,created_at) VALUES(?,?,?,?)",
                    (str(uid), 0.0, 0, datetime.utcnow().isoformat()))
        conn.commit()
    conn.close()

def is_banned(uid):
    conn = db_conn(); cur = conn.cursor()
    cur.execute("SELECT banned FROM users WHERE id = ?", (str(uid),))
    r = cur.fetchone()
    conn.close()
    return r and r[0] == 1

def get_balance(uid):
    conn = db_conn(); cur = conn.cursor()
    cur.execute("SELECT balance FROM users WHERE id = ?", (str(uid),))
    r = cur.fetchone(); conn.close()
    return r[0] if r else 0.0

def set_balance(uid, amount):
    conn = db_conn(); cur = conn.cursor()
    cur.execute("UPDATE users SET balance = ? WHERE id = ?", (float(amount), str(uid)))
    conn.commit(); conn.close()

def add_balance(uid, amount):
    conn = db_conn(); cur = conn.cursor()
    cur.execute("SELECT balance FROM users WHERE id = ?", (str(uid),))
    r = cur.fetchone()
    if not r:
        cur.execute("INSERT INTO users(id,balance,banned,created_at) VALUES(?,?,?,?)",
                    (str(uid), float(amount), 0, datetime.utcnow().isoformat()))
        conn.commit()
        conn.close()
        return float(amount)
    new = round(r[0] + float(amount), 2)
    cur.execute("UPDATE users SET balance = ? WHERE id = ?", (new, str(uid)))
    conn.commit(); conn.close()
    return new

def deduct_balance(uid, amount):
    conn = db_conn(); cur = conn.cursor()
    cur.execute("SELECT balance FROM users WHERE id = ?", (str(uid),))
    r = cur.fetchone()
    if not r:
        conn.close(); return False, "المستخدم غير موجود"
    if r[0] < float(amount) - 1e-9:
        conn.close(); return False, "رصيد غير كافٍ"
    new = round(r[0] - float(amount), 2)
    cur.execute("UPDATE users SET balance = ? WHERE id = ?", (new, str(uid)))
    conn.commit(); conn.close()
    return True, new

# --------------- Admin actions (DB wrappers) ----------------

def add_main_button(name, image=None):
    conn = db_conn(); cur = conn.cursor()
    try:
        cur.execute("INSERT INTO main_buttons(name,image) VALUES(?,?)", (name, image))
        conn.commit(); return True, "تم إضافة الزر الرئيسي."
    except sqlite3.IntegrityError:
        return False, "الزر موجود مسبقاً."
    finally:
        conn.close()

def remove_main_button(name):
    conn = db_conn(); cur = conn.cursor()
    cur.execute("DELETE FROM main_buttons WHERE name = ?", (name,))
    conn.commit(); conn.close()
    return True, "تم الحذف." 

def add_service(name, description, price_usd, image=None, collect_fields=None):
    conn = db_conn(); cur = conn.cursor()
    cf_json = json.dumps(collect_fields or [], ensure_ascii=False)
    cur.execute("INSERT INTO services(name,description,price_usd,image,enabled,collect_fields) VALUES(?,?,?,?,1,?)",
                (name, description, float(price_usd), image, cf_json))
    sid = cur.lastrowid
    conn.commit(); conn.close()
    return sid

def edit_service(sid, name=None, description=None, price_usd=None, image=None, enabled=None, collect_fields=None):
    conn = db_conn(); cur = conn.cursor()
    cur.execute("SELECT id,name,description,price_usd,image,enabled,collect_fields FROM services WHERE id = ?", (sid,))
    r = cur.fetchone()
    if not r:
        conn.close(); return False, "الخدمة غير موجودة."
    cur_name, cur_desc, cur_price, cur_image, cur_enabled, cur_cf = r[1], r[2], r[3], r[4], r[5], r[6]
    new_name = name if name is not None else cur_name
    new_desc = description if description is not None else cur_desc
    new_price = float(price_usd) if price_usd is not None else cur_price
    new_image = image if image is not None else cur_image
    new_enabled = int(enabled) if enabled is not None else cur_enabled
    new_cf = json.dumps(collect_fields, ensure_ascii=False) if collect_fields is not None else cur_cf
    cur.execute("""UPDATE services SET name=?,description=?,price_usd=?,image=?,enabled=?,collect_fields=? WHERE id=?""",
                (new_name,new_desc,new_price,new_image,new_enabled,new_cf,sid))
    conn.commit(); conn.close()
    return True, "تم تعديل الخدمة."

def remove_service(sid):
    conn = db_conn(); cur = conn.cursor()
    cur.execute("DELETE FROM services WHERE id = ?", (sid,))
    cur.execute("DELETE FROM sub_buttons WHERE service_id = ?", (sid,))
    conn.commit(); conn.close()
    return True, "تم حذف الخدمة."

def add_sub_button(main_name, sub_name, service_id):
    conn = db_conn(); cur = conn.cursor()
    cur.execute("INSERT INTO sub_buttons(main_name,sub_name,service_id) VALUES(?,?,?)", (main_name, sub_name, service_id))
    conn.commit(); conn.close()
    return True, "تم إضافة زر فرعي مرتبط بالخدمة."

def remove_sub_button_by_name(main_name, sub_name):
    conn = db_conn(); cur = conn.cursor()
    cur.execute("DELETE FROM sub_buttons WHERE main_name = ? AND sub_name = ?", (main_name, sub_name))
    conn.commit(); conn.close()
    return True, "تم حذف الزر الفرعي."

# --------------- Orders ----------------

def create_order(user_id, service_id, data_dict, price):
    conn = db_conn(); cur = conn.cursor()
    now = datetime.utcnow().isoformat()
    cur.execute("INSERT INTO orders(user_id,service_id,data,price,status,created_at) VALUES(?,?,?,?,?,?)",
                (str(user_id), int(service_id), json.dumps(data_dict, ensure_ascii=False), float(price), "pending", now))
    oid = cur.lastrowid
    conn.commit(); conn.close()
    return oid

def set_order_status(oid, status):
    conn = db_conn(); cur = conn.cursor()
    cur.execute("UPDATE orders SET status = ? WHERE id = ?", (status, int(oid)))
    conn.commit(); conn.close()
    return True

def get_order(oid):
    conn = db_conn(); cur = conn.cursor()
    cur.execute("SELECT id,user_id,service_id,data,price,status,created_at FROM orders WHERE id = ?", (int(oid),))
    r = cur.fetchone(); conn.close()
    return r

# --------------- Keyboards (inline) ----------------

def mk_main_menu():
    conn = db_conn(); cur = conn.cursor()
    cur.execute("SELECT name FROM main_buttons")
    rows = cur.fetchall(); conn.close()
    kb = types.InlineKeyboardMarkup(row_width=2)
    for r in rows:
        kb.add(types.InlineKeyboardButton(r[0], callback_data=f"main:{r[0]}"))
    kb.add(types.InlineKeyboardButton("رصيدي 💰", callback_data="my_balance"))
    kb.add(types.InlineKeyboardButton("سجل الطلبات 📜", callback_data="my_orders"))
    kb.add(types.InlineKeyboardButton("الشروط 📜", callback_data="show_terms"))
    return kb

def mk_sub_menu(main_name):
    conn = db_conn(); cur = conn.cursor()
    cur.execute("SELECT sub_name,service_id FROM sub_buttons WHERE main_name = ?", (main_name,))
    rows = cur.fetchall(); conn.close()
    kb = types.InlineKeyboardMarkup(row_width=1)
    for sub_name, sid in rows:
        kb.add(types.InlineKeyboardButton(sub_name, callback_data=f"service:{sid}"))
    kb.add(types.InlineKeyboardButton("🔙 رجوع", callback_data="back_main"))
    return kb

def mk_service_kb(sid):
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(types.InlineKeyboardButton("🛒 شراء الآن (من الرصيد)", callback_data=f"buy_bal:{sid}"))
    kb.add(types.InlineKeyboardButton("💳 دفع خارجي (شحن رصيد/دفع)", callback_data=f"payext:{sid}"))
    kb.add(types.InlineKeyboardButton("🔙 رجوع", callback_data="back_main"))
    return kb

def mk_admin_kb():
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(types.InlineKeyboardButton("➕ إضافة زر رئيسي", callback_data="adm:add_main"))
    kb.add(types.InlineKeyboardButton("➖ حذف زر رئيسي", callback_data="adm:del_main"))
    kb.add(types.InlineKeyboardButton("➕ إضافة زر فرعي", callback_data="adm:add_sub"))
    kb.add(types.InlineKeyboardButton("➖ حذف زر فرعي", callback_data="adm:del_sub"))
    kb.add(types.InlineKeyboardButton("🛠 إدارة خدمة (تعديل/سعر/صورة)", callback_data="adm:edit_service"))
    kb.add(types.InlineKeyboardButton("💰 إضافة/خصم رصيد", callback_data="adm:balance"))
    kb.add(types.InlineKeyboardButton("🚫 حظر/إلغاء حظر", callback_data="adm:ban"))
    kb.add(types.InlineKeyboardButton("📣 إرسال إعلان جماعي", callback_data="adm:broadcast"))
    kb.add(types.InlineKeyboardButton("🔒 قفل/فتح خدمة", callback_data="adm:toggle_service"))
    kb.add(types.InlineKeyboardButton("🛰 صيانة (تشغيل/إيقاف)", callback_data="adm:maintenance"))
    return kb

# --------------- State Management for multi-step flows --------------
# We'll store temporary states in memory (dictionary) keyed by user id.
# Not persistent across restart (acceptable for admin flows); could be extended to DB if needed.

pending = {}  # {user_id: {"action": str, ...}}

def set_pending(uid, obj):
    pending[str(uid)] = obj

def get_pending(uid):
    return pending.get(str(uid))

def pop_pending(uid):
    return pending.pop(str(uid), None)

# --------------- Bot Handlers ----------------

ensure_db()

@bot.message_handler(commands=['start'])
def cmd_start(m):
    if get_setting("maintenance") == "1" and m.from_user.id != ADMIN_ID:
        bot.send_message(m.chat.id, "⚠️ البوت في وضع الصيانة حالياً. حاول لاحقاً.")
        return
    user_exists_create(m.from_user.id)
    if is_banned(m.from_user.id):
        bot.send_message(m.chat.id, "🚫 أنت محظور من استخدام البوت.")
        return
    welcome = get_setting("welcome") or "مرحباً!"
    bot.send_message(m.chat.id, welcome, reply_markup=mk_main_menu())

@bot.message_handler(commands=['admin'])
def cmd_admin(m):
    if m.from_user.id != ADMIN_ID:
        bot.reply_to(m, "غير مسموح.")
        return
    bot.send_message(m.chat.id, "لوحة تحكم الأدمن:", reply_markup=mk_admin_kb())

# text handlers for simple admin commands via message (optionally)
@bot.message_handler(commands=['myid'])
def cmd_myid(m):
    bot.reply_to(m, f"Your id: {m.from_user.id}")

# --------------- Callback Query Handling ----------------

@bot.callback_query_handler(func=lambda c: True)
def on_callback(c):
    data = c.data or ""
    uid = c.from_user.id
    # maintenance check
    if get_setting("maintenance") == "1" and uid != ADMIN_ID:
        bot.answer_callback_query(c.id, "البوت في وضع الصيانة.")
        return

    # Admin flows
    if data.startswith("adm:"):
        if uid != ADMIN_ID:
            bot.answer_callback_query(c.id, "غير مسموح.")
            return
        action = data.split(":",1)[1]
        if action == "add_main":
            bot.send_message(uid, "أرسل اسم الزر الرئيسي الجديد:")
            set_pending(uid, {"action":"adm_add_main"})
            bot.answer_callback_query(c.id)
            return
        if action == "del_main":
            bot.send_message(uid, "أرسل اسم الزر الرئيسي للحذف:")
            set_pending(uid, {"action":"adm_del_main"})
            bot.answer_callback_query(c.id)
            return
        if action == "add_sub":
            bot.send_message(uid, "أرسل اسم الزر الرئيسي الذي تود إضافة فرعي إليه:")
            set_pending(uid, {"action":"adm_add_sub_step","step":1})
            bot.answer_callback_query(c.id)
            return
        if action == "del_sub":
            bot.send_message(uid, "أرسل: <اسم الزر الرئيسي>|<اسم الزر الفرعي> (مثال: ألعاب|PUBG)")
            set_pending(uid, {"action":"adm_del_sub"})
            bot.answer_callback_query(c.id)
            return
        if action == "edit_service":
            bot.send_message(uid, "أرسل رقم الخدمة (service id) لتعديلها:")
            set_pending(uid, {"action":"adm_edit_service","step":1})
            bot.answer_callback_query(c.id)
            return
        if action == "balance":
            bot.send_message(uid, "أرسل الأمر بصيغة: add <user_id> <amount> أو deduct <user_id> <amount>")
            set_pending(uid, {"action":"adm_balance"})
            bot.answer_callback_query(c.id)
            return
        if action == "ban":
            bot.send_message(uid, "أرسل الأمر: ban <user_id> أو unban <user_id>")
            set_pending(uid, {"action":"adm_ban"})
            bot.answer_callback_query(c.id)
            return
        if action == "broadcast":
            bot.send_message(uid, "أرسل نص الإعلان الذي تريد إرساله لجميع المستخدمين:")
            set_pending(uid, {"action":"adm_broadcast"})
            bot.answer_callback_query(c.id)
            return
        if action == "toggle_service":
            bot.send_message(uid, "أرسل: lock <service_id> أو unlock <service_id>")
            set_pending(uid, {"action":"adm_toggle_service"})
            bot.answer_callback_query(c.id)
            return
        if action == "maintenance":
            cur = get_setting("maintenance")
            new = "0" if cur == "1" else "1"
            set_setting("maintenance", new)
            bot.send_message(uid, f"تم تغيير وضعية الصيانة: {new}")
            bot.answer_callback_query(c.id)
            return

    # User menu callbacks
    if data == "my_balance":
        bal = get_balance(uid)
        bot.answer_callback_query(c.id, f"رصيدك الحالي: {bal}$")
        return
    if data == "my_orders":
        conn = db_conn(); cur = conn.cursor()
        cur.execute("SELECT id,status,price,created_at FROM orders WHERE user_id = ? ORDER BY id DESC", (str(uid),))
        rows = cur.fetchall(); conn.close()
        if not rows:
            bot.send_message(uid, "لا توجد طلبات لديك.")
            bot.answer_callback_query(c.id)
            return
        text = "سجل طلباتك:\n" + "\n".join([f"#{r[0]} - {r[1]} - {r[2]}$ - {r[3][:19]}" for r in rows])
        bot.send_message(uid, text)
        bot.answer_callback_query(c.id)
        return
    if data == "show_terms":
        bot.send_message(uid, get_setting("terms") or "لا توجد شروط محددة.")
        bot.answer_callback_query(c.id)
        return

    if data == "back_main":
        bot.send_message(uid, "القائمة الرئيسية:", reply_markup=mk_main_menu())
        bot.answer_callback_query(c.id)
        return

    if data.startswith("main:"):
        main_name = data.split(":",1)[1]
        bot.send_message(uid, f"القسم: {main_name}", reply_markup=mk_sub_menu(main_name))
        bot.answer_callback_query(c.id)
        return

    if data.startswith("service:"):
        sid = int(data.split(":",1)[1])
        conn = db_conn(); cur = conn.cursor()
        cur.execute("SELECT id,name,description,price_usd,image,enabled,collect_fields FROM services WHERE id = ?", (sid,))
        r = cur.fetchone(); conn.close()
        if not r:
            bot.answer_callback_query(c.id, "الخدمة غير موجودة.")
            return
        if r[5] == 0:
            bot.answer_callback_query(c.id, "هذه الخدمة مغلقة مؤقتاً.")
            return
        name = r[1]; desc = r[2]; price = r[3]; img = r[4]; cf = json.loads(r[6] or "[]")
        text = f"<b>{name}</b>\nالسعر: {price}$\n{desc}"
        if img:
            try:
                bot.send_photo(uid, img, caption=text, reply_markup=mk_service_kb(sid))
            except Exception:
                bot.send_message(uid, text, reply_markup=mk_service_kb(sid))
        else:
            bot.send_message(uid, text, reply_markup=mk_service_kb(sid))
        bot.answer_callback_query(c.id)
        return

    if data.startswith("buy_bal:"):
        sid = int(data.split(":",1)[1])
        # check service & price & user balance
        conn = db_conn(); cur = conn.cursor()
        cur.execute("SELECT price_usd,collect_fields,name FROM services WHERE id = ?", (sid,))
        r = cur.fetchone(); conn.close()
        if not r:
            bot.answer_callback_query(c.id, "الخدمة غير موجودة.")
            return
        price = float(r[0]); collect_fields = json.loads(r[1] or "[]")
        if get_balance(uid) < price:
            bot.answer_callback_query(c.id, "رصيدك غير كافٍ. اشحن رصيدك.")
            return
        # begin collect fields if necessary
        if collect_fields:
            # store pending purchase state
            set_pending(uid, {"action":"purchase_collect","sid":sid,"price":price,"fields":collect_fields,"collected":{}, "step":0})
            bot.send_message(uid, f"أرسل قيمة الحقل التالي: {collect_fields[0]}")
            bot.answer_callback_query(c.id)
            return
        # else directly deduct & create order
        ok, res = deduct_balance(uid, price)
        if not ok:
            bot.answer_callback_query(c.id, res)
            return
        oid = create_order(uid, sid, {}, price)
        bot.answer_callback_query(c.id, "تم سحب المبلغ وإنشاء الطلب. سيتم إبلاغك بتحديث الحالة.")
        bot.send_message(ADMIN_ID, f"طلب جديد #{oid} من {uid} بقيمة {price}$")
        bot.send_message(uid, f"✅ تم إنشاء الطلب #{oid}. رصيدك الآن {get_balance(uid)}$")
        return

    if data.startswith("payext:"):
        sid = int(data.split(":",1)[1])
        bot.send_message(uid, "تم توجيهك لطريقة الدفع الخارجي (محاكاة). أرسل /topup_ext <amount> لشحن رصيدك أو /buy_ext {service_id} لإتمام الدفع الخارجي.")
        bot.answer_callback_query(c.id)
        return

    # admin: more interactions could be handled here
    bot.answer_callback_query(c.id)

# --------------- Message handler for pending states and admin inputs ---------------

@bot.message_handler(func=lambda m: True)
def all_text(m):
    uid = m.from_user.id
    text = (m.text or "").strip()
    # maintenance check
    if get_setting("maintenance") == "1" and uid != ADMIN_ID:
        bot.reply_to(m, "⚠️ البوت في وضع الصيانة.")
        return

    # if admin has pending action
    pending_obj = get_pending(uid)
    if pending_obj and uid == ADMIN_ID:
        action = pending_obj.get("action")
        # Add main button
        if action == "adm_add_main":
            name = text
            ok, msg = add_main_button(name)
            bot.send_message(uid, msg)
            pop_pending(uid); return
        # Delete main
        if action == "adm_del_main":
            name = text
            ok, msg = remove_main_button(name)
            bot.send_message(uid, msg)
            pop_pending(uid); return
        # Add sub multi-step
        if action == "adm_add_sub_step":
            step = pending_obj.get("step",1)
            if step == 1:
                main_name = text
                # check exists
                conn = db_conn(); cur = conn.cursor()
                cur.execute("SELECT name FROM main_buttons WHERE name = ?", (main_name,))
                if not cur.fetchone():
                    bot.send_message(uid, "لا يوجد زر رئيسي بهذا الاسم. أعد المحاولة أو إلغاء.")
                    pop_pending(uid); return
                pending_obj["main_name"] = main_name
                pending_obj["step"] = 2
                set_pending(uid, pending_obj)
                bot.send_message(uid, "أرسل اسم الزر الفرعي الجديد:")
                return
            if step == 2:
                pending_obj["sub_name"] = text
                pending_obj["step"] = 3
                set_pending(uid, pending_obj)
                bot.send_message(uid, "أرسل اسم الخدمة (سيظهر للمستخدم):")
                return
            if step == 3:
                pending_obj["svc_name"] = text
                pending_obj["step"] = 4
                set_pending(uid, pending_obj)
                bot.send_message(uid, "أرسل وصف الخدمة:")
                return
            if step == 4:
                pending_obj["svc_desc"] = text
                pending_obj["step"] = 5
                set_pending(uid, pending_obj)
                bot.send_message(uid, "أرسل سعر الخدمة بالدولار (مثال: 1.5):")
                return
            if step == 5:
                try:
                    price = float(text)
                except:
                    bot.send_message(uid, "سعر غير صالح. العملية ملغاة.")
                    pop_pending(uid); return
                # create service
                sid = add_service(pending_obj["svc_name"], pending_obj["svc_desc"], price)
                # link sub button
                add_sub_button(pending_obj["main_name"], pending_obj["sub_name"], sid)
                bot.send_message(uid, f"تم إنشاء الخدمة برقم {sid} وربطها بالزر الفرعي.")
                pop_pending(uid); return
        if action == "adm_del_sub":
            try:
                main, sub = text.split("|",1)
                main = main.strip(); sub = sub.strip()
                remove_sub_button_by_name(main, sub)
                bot.send_message(uid, "تم الحذف إذا كان موجوداً.")
            except Exception:
                bot.send_message(uid, "المدخل غير صالح. الصيغة: MainName|SubName")
            pop_pending(uid); return
        if action == "adm_edit_service":
            step = pending_obj.get("step",1)
            if step == 1:
                try:
                    sid = int(text)
                except:
                    bot.send_message(uid, "أدخل رقم خدمة صالح.")
                    pop_pending(uid); return
                # load service
                conn = db_conn(); cur = conn.cursor(); cur.execute("SELECT id,name,description,price_usd,image,enabled,collect_fields FROM services WHERE id = ?", (sid,)); r = cur.fetchone(); conn.close()
                if not r:
                    bot.send_message(uid, "الخدمة غير موجودة.")
                    pop_pending(uid); return
                # show current values and ask which field to edit
                bot.send_message(uid, f"الخدمة #{sid}\nالاسم: {r[1]}\nالوصف: {r[2]}\nالسعر: {r[3]}$\nأرسل: name|description|price|image|collect_fields (اختر الحقل لتعديله) أو 'all' لتعديل كل شيء.")
                pending_obj["sid"] = sid; pending_obj["step"] = 2; set_pending(uid, pending_obj); return
            if step == 2:
                field = text.strip()
                pending_obj["field"] = field
                if field == "name":
                    bot.send_message(uid, "أرسل الاسم الجديد:")
                    pending_obj["step"] = 3; set_pending(uid, pending_obj); return
                if field == "description":
                    bot.send_message(uid, "أرسل الوصف الجديد:")
                    pending_obj["step"] = 3; set_pending(uid, pending_obj); return
                if field == "price":
                    bot.send_message(uid, "أرسل السعر بالدولار:")
                    pending_obj["step"] = 3; set_pending(uid, pending_obj); return
                if field == "image":
                    bot.send_message(uid, "أرسل رابط الصورة (URL):")
                    pending_obj["step"] = 3; set_pending(uid, pending_obj); return
                if field == "collect_fields":
                    bot.send_message(uid, "أرسل قائمة الحقول مفصولة بفاصلة (مثال: id,username,phone) أو ارسل فارغ لتعطيلها:")
                    pending_obj["step"] = 3; set_pending(uid, pending_obj); return
                if field == "all":
                    bot.send_message(uid, "أرسل البيانات مفصولة بـ | على شكل: name|description|price|image|fields(comma-separated)")
                    pending_obj["step"] = 4; set_pending(uid, pending_obj); return
                bot.send_message(uid, "خيار غير معروف. ملغى."); pop_pending(uid); return
            if step == 3:
                sid = pending_obj["sid"]; field = pending_obj["field"]
                if field == "name":
                    edit_service(sid, name=text); bot.send_message(uid, "تم التعديل."); pop_pending(uid); return
                if field == "description":
                    edit_service(sid, description=text); bot.send_message(uid, "تم التعديل."); pop_pending(uid); return
                if field == "price":
                    try:
                        p = float(text)
                        edit_service(sid, price_usd=p); bot.send_message(uid, "تم التعديل."); pop_pending(uid); return
                    except:
                        bot.send_message(uid, "سعر غير صالح."); pop_pending(uid); return
                if field == "image":
                    edit_service(sid, image=text); bot.send_message(uid, "تم التعديل."); pop_pending(uid); return
                if field == "collect_fields":
                    fields = [s.strip() for s in text.split(",")] if text else []
                    edit_service(sid, collect_fields=fields); bot.send_message(uid, "تم التعديل."); pop_pending(uid); return
            if step == 4:
                try:
                    sid = pending_obj["sid"]
                    name, desc, price, image, fields = text.split("|",4)
                    price = float(price)
                    fields_list = [s.strip() for s in fields.split(",")] if fields else []
                    edit_service(sid, name=name.strip(), description=desc.strip(), price_usd=price, image=image.strip(), collect_fields=fields_list)
                    bot.send_message(uid, "تم التعديل الشامل.")
                except Exception as e:
                    bot.send_message(uid, f"خطأ في الصيغة: {e}")
                pop_pending(uid); return
        if action == "adm_balance":
            try:
                parts = text.split()
                cmd = parts[0].lower()
                target = parts[1]; amount = float(parts[2])
                if cmd == "add":
                    new = add_balance(target, amount)
                    bot.send_message(uid, f"تم إضافة {amount}$ للمستخدم {target}. رصيده الآن {new}$.")
                    try:
                        bot.send_message(int(target), f"💰 تم إضافة {amount}$ إلى رصيدك. رصيدك الآن {new}$.")
                    except:
                        pass
                elif cmd == "deduct":
                    ok,res = deduct_balance(target, amount)
                    if ok:
                        bot.send_message(uid, f"تم خصم {amount}$ من {target}. رصيده الآن {res}$.")
                        try:
                            bot.send_message(int(target), f"⚠️ تم خصم {amount}$ من رصيدك. رصيدك الآن {res}$.")
                        except:
                            pass
                    else:
                        bot.send_message(uid, f"فشل: {res}")
                else:
                    bot.send_message(uid, "الأمر غير معروف. استخدم add/deduct")
            except Exception as e:
                bot.send_message(uid, "صيغة خاطئة. مثال: add 123456789 5.0")
            pop_pending(uid); return
        if action == "adm_ban":
            try:
                parts = text.split()
                cmd = parts[0].lower(); target = parts[1]
                conn = db_conn(); cur = conn.cursor()
                if cmd == "ban":
                    cur.execute("UPDATE users SET banned = 1 WHERE id = ?", (str(target),))
                    conn.commit(); bot.send_message(uid, f"تم حظر {target}")
                    try: bot.send_message(int(target), "🚫 تم حظرك من البوت.") 
                    except: pass
                elif cmd == "unban":
                    cur.execute("UPDATE users SET banned = 0 WHERE id = ?", (str(target),))
                    conn.commit(); bot.send_message(uid, f"تم إلغاء الحظر عن {target}")
                    try: bot.send_message(int(target), "✅ تم رفع الحظر عنك.") 
                    except: pass
                else:
                    bot.send_message(uid, "استخدم ban/unban <user_id>")
                conn.close()
            except Exception:
                bot.send_message(uid, "صيغة خاطئة.")
            pop_pending(uid); return
        if action == "adm_broadcast":
            conn = db_conn(); cur = conn.cursor()
            cur.execute("SELECT id FROM users"); rows = cur.fetchall(); conn.close()
            count = 0
            for r in rows:
                try:
                    bot.send_message(int(r[0]), text)
                    count += 1
                except:
                    pass
            bot.send_message(uid, f"تم إرسال الإعلان إلى {count} مستخدم.")
            pop_pending(uid); return
        if action == "adm_toggle_service":
            try:
                parts = text.split()
                cmd = parts[0].lower(); sid = int(parts[1])
                if cmd == "lock":
                    edit_service(sid, enabled=0); bot.send_message(uid, "تم قفل الخدمة.")
                else:
                    edit_service(sid, enabled=1); bot.send_message(uid, "تم فتح الخدمة.")
            except:
                bot.send_message(uid, "صيغة خاطئة. استخدم lock/unlock <service_id>")
            pop_pending(uid); return

    # if user has pending purchase collection
    pending_obj = get_pending(uid)
    if pending_obj and pending_obj.get("action") == "purchase_collect":
        step = pending_obj["step"]
        fields = pending_obj["fields"]
        collected = pending_obj["collected"]
        # store the input for current field
        field_name = fields[step]
        collected[field_name] = text
        pending_obj["collected"] = collected
        step += 1
        if step < len(fields):
            pending_obj["step"] = step
            set_pending(uid, pending_obj)
            bot.send_message(uid, f"أرسل قيمة الحقل التالي: {fields[step]}")
            return
        # else done collecting
        sid = pending_obj["sid"]; price = pending_obj["price"]
        # deduct balance and create order
        ok,res = deduct_balance(uid, price)
        if not ok:
            bot.send_message(uid, f"فشل في خصم الرصيد: {res}")
            pop_pending(uid); return
        oid = create_order(uid, sid, collected, price)
        bot.send_message(uid, f"✅ تم إنشاء الطلب #{oid}. رصيدك الآن {get_balance(uid)}$")
        bot.send_message(ADMIN_ID, f"طلب جديد #{oid} من {uid} بقيمة {price}$")
        pop_pending(uid); return

    # handle simple commands from users:
    if text.startswith("/topup_ext"):
        # usage: /topup_ext 5.0
        try:
            parts = text.split()
            amt = float(parts[1])
            # simulate external payment: add as pending TX (not implemented)
            # For demo, we immediately add to balance
            new = add_balance(uid, amt)
            bot.send_message(uid, f"✅ تم شحن رصيدك بمقدار {amt}$. رصيدك الآن {new}$.")
            return
        except:
            bot.send_message(uid, "استخدم: /topup_ext <amount>")
            return

    if text.startswith("/buy_ext"):
        # /buy_ext <service_id> - simulate external payment and create order (no balance)
        try:
            sid = int(text.split()[1])
            conn = db_conn(); cur = conn.cursor(); cur.execute("SELECT price_usd,collect_fields FROM services WHERE id = ?", (sid,)); r = cur.fetchone(); conn.close()
            if not r:
                bot.send_message(uid, "الخدمة غير موجودة.")
                return
            price = float(r[0]); collect_fields = json.loads(r[1] or "[]")
            if collect_fields:
                # start collect and after collection simulate payment then create order
                set_pending(uid, {"action":"buyext_collect","sid":sid,"price":price,"fields":collect_fields,"collected":{},"step":0})
                bot.send_message(uid, f"أرسل قيمة الحقل التالي: {collect_fields[0]}")
                return
            # no fields, create order and notify admin
            oid = create_order(uid, sid, {}, price)
            bot.send_message(uid, f"تم إنشاء طلب خارجي #{oid}. سيتم إشعارك عند التفعيل.")
            bot.send_message(ADMIN_ID, f"[دفع خارجي] طلب جديد #{oid} من {uid} بقيمة {price}$")
            return
        except Exception:
            bot.send_message(uid, "الصيغة: /buy_ext <service_id>")
            return

    # pending from buyext_collect
    pending_obj = get_pending(uid)
    if pending_obj and pending_obj.get("action") == "buyext_collect":
        step = pending_obj["step"]; fields = pending_obj["fields"]; collected = pending_obj["collected"]
        field_name = fields[step]; collected[field_name] = text
        step += 1
        if step < len(fields):
            pending_obj["step"] = step; pending_obj["collected"] = collected; set_pending(uid, pending_obj)
            bot.send_message(uid, f"أرسل قيمة الحقل التالي: {fields[step]}"); return
        # done collecting: create order and simulate external payment accepted
        sid = pending_obj["sid"]; price = pending_obj["price"]
        oid = create_order(uid, sid, collected, price)
        # Here we assume external payment processed; admin should verify in real integration.
        bot.send_message(uid, f"✅ تم إنشاء الطلب الخارجي #{oid}. سيتم إكماله بعد الدفع (محاكاة).")
        bot.send_message(ADMIN_ID, f"[دفع خارجي] طلب جديد #{oid} من {uid} بقيمة {price}$")
        pop_pending(uid); return

    # fallback: send main menu
    bot.send_message(uid, "استخدم الأزرار أدناه:", reply_markup=mk_main_menu())

# --------------- Run ----------------

if __name__ == "__main__":
    print("Starting bot...")
    bot.infinity_polling()
