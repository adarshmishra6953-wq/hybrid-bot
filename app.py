import os, logging, time, threading, pytz
from datetime import datetime
from flask import Flask
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
from telegram.constants import ParseMode
from sqlalchemy import create_engine, Column, Integer, String, Boolean, PickleType
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy.ext.mutable import MutableDict

# --- Config ---
FORCE_ADMIN_ID = 1695450646
BOT_TOKEN = os.environ.get("BOT_TOKEN")
DATABASE_URL = os.environ.get("DATABASE_URL")
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
else:
    DATABASE_URL = "sqlite:///local.db"

# --- DB Setup ---
Base = declarative_base()
Engine = create_engine(DATABASE_URL)
Session = sessionmaker(bind=Engine)

class ForwardRule(Base):
    __tablename__ = "forward_rules"
    id = Column(Integer, primary_key=True)
    name = Column(String)
    source_chat_id = Column(String)
    destination_chat_id = Column(String)
    is_active = Column(Boolean, default=True)
    block_links = Column(Boolean, default=False)
    text_replacements = Column(MutableDict.as_mutable(PickleType), default=dict)

Base.metadata.create_all(Engine)

# --- Flask ---
flask_app = Flask(__name__)
@flask_app.route('/')
def health(): return "Hybrid Bot Active ✅"

# --- Logic ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != FORCE_ADMIN_ID: return
    btn = [
        [InlineKeyboardButton("➕ New Rule", callback_data="new_rule")],
        [InlineKeyboardButton("📜 List Rules", callback_data="list_rules")]
    ]
    await update.message.reply_text("🤖 **Hybrid Bot Active**\nManage your rules below:", 
                                   reply_markup=InlineKeyboardMarkup(btn), parse_mode=ParseMode.MARKDOWN)

async def forward_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.channel_post or update.message
    if not msg: return
    
    session = Session()
    rules = session.query(ForwardRule).filter(ForwardRule.is_active == True).all()
    
    for r in rules:
        source_id = str(msg.chat.id)
        source_uname = f"@{msg.chat.username}" if msg.chat.username else ""
        
        if source_id == r.source_chat_id or source_uname == r.source_chat_id:
            text = msg.text or msg.caption or ""
            
            # Link Filter
            if r.block_links and ("http" in text or "t.me" in text): continue
            
            # Replacements
            for f, rep in r.text_replacements.items():
                text = text.replace(f, rep)
            
            try:
                if msg.photo:
                    await context.bot.send_photo(r.destination_chat_id, msg.photo[-1].file_id, caption=text)
                else:
                    await context.bot.send_message(r.destination_chat_id, text)
            except Exception as e:
                logging.error(f"Forward Error: {e}")
    session.close()

async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "new_rule":
        context.user_data['step'] = 'src'
        await query.message.reply_text("Step 1: Send Source Channel ID or Username (e.g. @mychannel):")

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != FORCE_ADMIN_ID: return
    step = context.user_data.get('step')
    session = Session()
    
    if step == 'src':
        context.user_data['src'] = update.message.text
        context.user_data['step'] = 'dest'
        await update.message.reply_text("Step 2: Send Destination Channel ID (e.g. -100xxx):")
    elif step == 'dest':
        rule = ForwardRule(source_chat_id=context.user_data['src'], 
                          destination_chat_id=update.message.text, 
                          name=f"Rule_{int(time.time())}")
        session.add(rule)
        session.commit()
        await update.message.reply_text("✅ Rule Added Successfully!")
        context.user_data.clear()
    session.close()

def main():
    threading.Thread(target=lambda: flask_app.run(host='0.0.0.0', port=os.environ.get("PORT", 8080)), daemon=True).start()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, forward_handler))
    app.run_polling()

if __name__ == "__main__":
    main()
