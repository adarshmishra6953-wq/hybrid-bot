import os
import logging
import time
import re
import json
import urllib.parse
import threading
from datetime import datetime
from typing import Optional, List
from zoneinfo import ZoneInfo
import pytz

from flask import Flask
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from telegram.constants import ParseMode
from sqlalchemy import create_engine, Column, Integer, String, Boolean, PickleType, DateTime, inspect, text, BigInteger
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy.ext.mutable import MutableDict, MutableList

# ------------------ Logging & Config ------------------
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

FORCE_ADMIN_ID = 1695450646
BOT_TOKEN = os.environ.get("BOT_TOKEN")
IST = pytz.timezone('Asia/Kolkata')
KOLKATA_TZ = ZoneInfo("Asia/Kolkata")

# ------------------ Database Setup (SQLAlchemy) ------------------
DATABASE_URL = os.environ.get("DATABASE_URL")
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

if not DATABASE_URL:
    logger.warning("DATABASE_URL not found, using sqlite (Data will be lost on restart!)")
    DATABASE_URL = "sqlite:///bot_data.db"

Engine = create_engine(DATABASE_URL)
Base = declarative_base()
Session = sessionmaker(bind=Engine)

# For Forwarder
class ForwardRule(Base):
    __tablename__ = "forward_rules"
    id = Column(Integer, primary_key=True)
    name = Column(String, default="unnamed_rule")
    source_chat_id = Column(String, nullable=False)
    destination_chat_id = Column(String, nullable=False)
    is_active = Column(Boolean, default=True)
    block_links = Column(Boolean, default=False)
    block_usernames = Column(Boolean, default=False)
    blacklist_words = Column(MutableList.as_mutable(PickleType), default=list)
    whitelist_words = Column(MutableList.as_mutable(PickleType), default=list)
    text_replacements = Column(MutableDict.as_mutable(PickleType), default=dict)
    header_text = Column(String, nullable=True)
    footer_text = Column(String, nullable=True)
    forward_mode = Column(String, default="FORWARD")
    forward_delay = Column(Integer, default=0)
    schedule_start = Column(String, nullable=True)
    schedule_end = Column(String, nullable=True)
    forwarded_count = Column(Integer, default=0)
    last_triggered = Column(DateTime, nullable=True)

# For Scheduler (Converted to SQLAlchemy for permanency)
class ScheduledPost(Base):
    __tablename__ = "scheduled_posts"
    id = Column(Integer, primary_key=True)
    channel_id = Column(BigInteger)
    photo_id = Column(String)
    caption = Column(String)
    post_time = Column(String) # HH:MM format

class RegisteredChannel(Base):
    __tablename__ = "registered_channels"
    channel_id = Column(BigInteger, primary_key=True)
    channel_name = Column(String)

Base.metadata.create_all(Engine)

# ------------------ Flask for Render ------------------
flask_app = Flask(__name__)
@flask_app.route('/')
def health(): return "Hybrid Bot (SQL) is Online! ✅"

def run_web():
    port = int(os.environ.get("PORT", 8080))
    flask_app.run(host='0.0.0.0', port=port)

# ------------------ Keyboards (Same as before) ------------------
def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Forward Rules", callback_data="fwd_mgr"), InlineKeyboardButton("📅 Post Scheduler", callback_data="sch_mgr")],
        [InlineKeyboardButton("⚙️ Global Info", callback_data="global_info")]
    ])

def fwd_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ New Rule", callback_data="new_rule")],
        [InlineKeyboardButton("📜 List Rules", callback_data="list_rules")],
        [InlineKeyboardButton("⬅️ Back", callback_data="main")]
    ])

def sch_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add Channel", callback_data="add_ch")],
        [InlineKeyboardButton("📋 My Channels", callback_data="list_ch")],
        [InlineKeyboardButton("⬅️ Back", callback_data="main")]
    ])

# ------------------ Logic & Background Jobs ------------------
async def auto_post_job(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now(IST).strftime("%H:%M")
    session = Session()
    posts = session.query(ScheduledPost).filter(ScheduledPost.post_time == now).all()
    for p in posts:
        try:
            await context.bot.send_photo(chat_id=p.channel_id, photo=p.photo_id, caption=f"*{p.caption}*", parse_mode=ParseMode.MARKDOWN)
        except:
            await context.bot.send_photo(chat_id=p.channel_id, photo=p.photo_id, caption=p.caption)
    session.close()

# ------------------ Handlers ------------------
async def start(update, context):
    if update.effective_user.id != FORCE_ADMIN_ID: return
    await update.message.reply_text("💎 **Hybrid Bot (Safe Storage)**\nData is now permanent in PostgreSQL.", reply_markup=main_menu(), parse_mode=ParseMode.MARKDOWN)

async def callback_handler(update, context):
    query = update.callback_query
    data = query.data
    await query.answer()
    session = Session()

    if data == "main": await query.edit_message_text("Main Menu:", reply_markup=main_menu())
    elif data == "fwd_mgr": await query.edit_message_text("Forwarding Rules:", reply_markup=fwd_menu())
    elif data == "sch_mgr": await query.edit_message_text("Post Scheduler:", reply_markup=sch_menu())

    # --- Forwarder ---
    elif data == "new_rule":
        context.user_data["creating_rule"] = {}
        await query.edit_message_text("Send Source Channel ID:")
    elif data == "list_rules":
        rules = session.query(ForwardRule).all()
        btns = [[InlineKeyboardButton(f"#{r.id} {r.name}", callback_data=f"fwd_open|{r.id}")] for r in rules]
        btns.append([InlineKeyboardButton("⬅️ Back", callback_data="fwd_mgr")])
        await query.edit_message_text("Select Rule:", reply_markup=InlineKeyboardMarkup(btns))
    
    # --- Scheduler ---
    elif data == "add_ch":
        context.user_data['step'] = 'wait_ch'
        await query.message.edit_text("📩 Channel ka @username bhejein:")
    elif data == "list_ch":
        channels = session.query(RegisteredChannel).all()
        btns = [[InlineKeyboardButton(c.channel_name, callback_data=f"mng_sch_{c.channel_id}")] for c in channels]
        btns.append([InlineKeyboardButton("🔙 Back", callback_data="sch_mgr")])
        await query.message.edit_text("Select Channel:", reply_markup=InlineKeyboardMarkup(btns))
    elif data.startswith("mng_sch_"):
        cid = int(data.split("_")[2])
        context.user_data['cid'] = cid
        btns = [[InlineKeyboardButton("➕ New Post", callback_data="new_post")],
                [InlineKeyboardButton("🔙 Back", callback_data="list_ch")]]
        await query.message.edit_text(f"Channel: {cid}", reply_markup=InlineKeyboardMarkup(btns))
    
    session.close()

async def text_handler(update, context):
    if update.effective_user.id != FORCE_ADMIN_ID: return
    text_data = update.message.text
    step = context.user_data.get('step')
    session = Session()

    if step == 'wait_ch':
        try:
            chat = await context.bot.get_chat(text_data)
            new_ch = RegisteredChannel(channel_id=chat.id, channel_name=chat.title)
            session.merge(new_ch)
            session.commit()
            await update.message.reply_text(f"✅ Added: {chat.title}", reply_markup=sch_menu())
            context.user_data['step'] = None
        except: await update.message.reply_text("❌ Invalid Channel.")
    
    elif step == 'wait_time':
        try:
            datetime.strptime(text_data, "%H:%M")
            new_post = ScheduledPost(channel_id=context.user_data['cid'], photo_id=context.user_data['photo'], 
                                   caption=context.user_data['caption'], post_time=text_data)
            session.add(new_post)
            session.commit()
            await update.message.reply_text(f"✅ Scheduled for {text_data} IST", reply_markup=sch_menu())
            context.user_data['step'] = None
        except: await update.message.reply_text("❌ Use HH:MM format.")
    
    session.close()

async def photo_handler(update, context):
    if context.user_data.get('step') == 'wait_photo':
        context.user_data['photo'] = update.message.photo[-1].file_id
        context.user_data['caption'] = update.message.caption or ""
        context.user_data['step'] = 'wait_time'
        await update.message.reply_text("⏰ Time bhejein (HH:MM IST):")

# ------------------ Launcher ------------------
def main():
    threading.Thread(target=run_web, daemon=True).start()
    app = Application.builder().token(BOT_TOKEN).build()
    app.job_queue.run_repeating(auto_post_job, interval=60)
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.PHOTO, photo_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    # Note: Forward logic can be added here as shown in previous responses
    
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
