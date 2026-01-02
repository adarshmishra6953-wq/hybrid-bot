import os, logging, time, threading, pytz
from datetime import datetime
from flask import Flask
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
from telegram.constants import ParseMode
from sqlalchemy import create_engine, Column, Integer, String, Boolean, PickleType, BigInteger
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy.ext.mutable import MutableDict

# --- Setup & Config ---
FORCE_ADMIN_ID = 1695450646
BOT_TOKEN = os.environ.get("BOT_TOKEN")
DB_URL = os.environ.get("DATABASE_URL", "sqlite:///local.db").replace("postgres://", "postgresql://", 1)
IST = pytz.timezone('Asia/Kolkata')

Base = declarative_base()
Engine = create_engine(DB_URL)
Session = sessionmaker(bind=Engine)

class ForwardRule(Base):
    __tablename__ = "forward_rules"
    id = Column(Integer, primary_key=True)
    source_chat_id = Column(String)
    destination_chat_id = Column(String)
    is_active = Column(Boolean, default=True)
    block_links = Column(Boolean, default=False)
    text_replacements = Column(MutableDict.as_mutable(PickleType), default=dict)

class ScheduledPost(Base):
    __tablename__ = "scheduled_posts"
    id = Column(Integer, primary_key=True)
    channel_id = Column(BigInteger)
    photo_id = Column(String)
    caption = Column(String)
    post_time = Column(String) # HH:MM

Base.metadata.create_all(Engine)

# --- Flask for Render Health Check ---
flask_app = Flask(__name__)
@flask_app.route('/')
def health(): return "Bot is Online! ✅"

# --- Logic Functions ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != FORCE_ADMIN_ID: return
    keyboard = [
        [InlineKeyboardButton("🔄 Forward Rules", callback_data="fwd_mgr"), 
         InlineKeyboardButton("📅 Post Scheduler", callback_data="sch_mgr")]
    ]
    await update.message.reply_text("💎 **Hybrid Bot V3 Active**\nSaare options niche hain:", 
                                   reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)

async def auto_post_job(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now(IST).strftime("%H:%M")
    session = Session()
    posts = session.query(ScheduledPost).filter(ScheduledPost.post_time == now).all()
    for p in posts:
        try:
            await context.bot.send_photo(chat_id=p.channel_id, photo=p.photo_id, caption=p.caption)
        except Exception as e: logging.error(f"Schedule Error: {e}")
    session.close()

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data == "fwd_mgr":
        btn = [[InlineKeyboardButton("➕ New Rule", callback_data="new_rule")], [InlineKeyboardButton("⬅️ Back", callback_data="main")]]
        await query.edit_message_text("Forwarding Settings:", reply_markup=InlineKeyboardMarkup(btn))
    
    elif query.data == "sch_mgr":
        btn = [[InlineKeyboardButton("➕ Schedule Post", callback_data="add_post")], [InlineKeyboardButton("⬅️ Back", callback_data="main")]]
        await query.edit_message_text("Scheduler Settings:", reply_markup=InlineKeyboardMarkup(btn))
    
    elif query.data == "new_rule":
        context.user_data['step'] = 'src'
        await query.message.reply_text("Send Source Channel ID or Username (e.g. @ssc1234569):")

    elif query.data == "add_post":
        context.user_data['step'] = 'wait_photo'
        await query.message.reply_text("Send Photo with Caption to schedule:")

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != FORCE_ADMIN_ID: return
    step = context.user_data.get('step')
    session = Session()

    if step == 'src':
        context.user_data['src'] = update.message.text
        context.user_data['step'] = 'dest'
        await update.message.reply_text("Now Send Destination Channel ID (e.g. -100...):")
    
    elif step == 'dest':
        rule = ForwardRule(source_chat_id=context.user_data['src'], destination_chat_id=update.message.text)
        session.add(rule)
        session.commit()
        await update.message.reply_text("✅ Rule Added!")
        context.user_data.clear()

    elif step == 'wait_time':
        new_post = ScheduledPost(channel_id=context.user_data['sch_cid'], photo_id=context.user_data['photo'], 
                                caption=context.user_data['cap'], post_time=update.message.text)
        session.add(new_post)
        session.commit()
        await update.message.reply_text(f"✅ Scheduled for {update.message.text} IST")
        context.user_data.clear()
    
    session.close()

async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get('step') == 'wait_photo':
        context.user_data['photo'] = update.message.photo[-1].file_id
        context.user_data['cap'] = update.message.caption or ""
        context.user_data['sch_cid'] = update.message.chat_id # Default to current or ask ID
        context.user_data['step'] = 'wait_time'
        await update.message.reply_text("Send Time in HH:MM format (e.g. 15:30):")

async def forward_logic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.channel_post or update.message
    if not msg: return
    session = Session()
    rules = session.query(ForwardRule).filter(ForwardRule.is_active == True).all()
    for r in rules:
        source = str(msg.chat.id)
        uname = f"@{msg.chat.username}" if msg.chat.username else ""
        if source == r.source_chat_id or uname == r.source_chat_id:
            text = msg.text or msg.caption or ""
            try:
                if msg.photo: await context.bot.send_photo(r.destination_chat_id, msg.photo[-1].file_id, caption=text)
                else: await context.bot.send_message(r.destination_chat_id, text)
            except: pass
    session.close()

def main():
    threading.Thread(target=lambda: flask_app.run(host='0.0.0.0', port=os.environ.get("PORT", 8080)), daemon=True).start()
    app = Application.builder().token(BOT_TOKEN).build()
    app.job_queue.run_repeating(auto_post_job, interval=60)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.PHOTO, photo_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, forward_logic))
    app.run_polling()

if __name__ == "__main__": main()
