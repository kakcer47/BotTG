import logging
import schedule
import time
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ConversationHandler,
    ContextTypes,
)
from threading import Thread

# Logging setup for debugging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Conversation states
CATEGORY, GENDER, LOCATION, DATE, ANNOUNCEMENT = range(5)

# Environment variables
import os
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
RENDER_URL = os.environ.get("RENDER_EXTERNAL_URL", "https://your-bot.onrender.com")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
WEBHOOK_PATH = "/webhook"

# Function to ping the bot to prevent Render from idling
def ping_self():
    try:
        response = requests.get(RENDER_URL)
        logger.info(f"Pinged {RENDER_URL}, status: {response.status_code}")
    except Exception as e:
        logger.error(f"Ping failed: {e}")

# Schedule ping every 5 minutes
schedule.every(5).minutes.do(ping_self)

# Run scheduler in a separate thread
def run_scheduler():
    while True:
        schedule.run_pending()
        time.sleep(60)

# Start scheduler in background
scheduler_thread = Thread(target=run_scheduler, daemon=True)
scheduler_thread.start()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Initiate the conversation, ask for category selection."""
    chat_id = update.effective_chat.id
    logger.info(f"Received /start command from chat_id: {chat_id}")
    try:
        # Simulate forum topics (replace with actual API call if available)
        topics = [
            {"name": "General", "thread_id": "1"},
            {"name": "Philosophy", "thread_id": "2"},
            {"name": "Meetups", "thread_id": "3"},
            {"name": "Discussions", "thread_id": "4"},
        ]

        keyboard = [
            [InlineKeyboardButton(topic["name"], callback_data=f"category_{topic['name']}_{topic['thread_id']}")]
            for topic in topics
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(
            "В какой категории вы хотите создать объявление?",
            reply_markup=reply_markup
        )
        return CATEGORY
    except Exception as e:
        logger.error(f"Error fetching topics: {e}")
        await update.message.reply_text("Ошибка получения категорий. Попробуйте снова.")
        return ConversationHandler.END

async def category_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle category selection, ask for gender."""
    query = update.callback_query
    await query.answer()
    data = query.data

    # Store selected category
    context.user_data["category"] = data.split("_")[1]
    context.user_data["category_id"] = data.split("_")[2]

    keyboard = [
        [InlineKeyboardButton("Мужской", callback_data="gender_male")],
        [InlineKeyboardButton("Женский", callback_data="gender_female")],
        [InlineKeyboardButton("Назад", callback_data="back_to_start")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        "Ваш пол:",
        reply_markup=reply_markup
    )
    return GENDER

async def gender_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle gender selection, ask for location."""
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "back_to_start":
        return await restart_conversation(update, context)

    # Store selected gender
    context.user_data["gender"] = "Мужской" if data == "gender_male" else "Женский"

    keyboard = [[InlineKeyboardButton("Назад", callback_data="back_to_category")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        "Напишите, где будет проходить встреча:",
        reply_markup=reply_markup
    )
    return LOCATION

async def location_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle location input, ask for date."""
    # Store location
    context.user_data["location"] = update.message.text

    keyboard = [
        [InlineKeyboardButton("Пропустить", callback_data="date_skip")],
        [InlineKeyboardButton("Назад", callback_data="back_to_gender")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        "Дата встречи, например: 06.05-10.05 (с-по), или точная дата, или пропустить:",
        reply_markup=reply_markup
    )
    return DATE

async def date_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle date input or callback."""
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        data = query.data

        if data == "back_to_gender":
            return await go_back_to_gender(update, context)
        elif data == "date_skip":
            context.user_data["date"] = ""
            
            keyboard = [[InlineKeyboardButton("Назад", callback_data="back_to_date")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.edit_message_text(
                "Напишите ваше объявление, например: Ищу друга для философских дискуссий на глубокие темы.",
                reply_markup=reply_markup
            )
            return ANNOUNCEMENT
    else:
        # Handle text message for date
        context.user_data["date"] = update.message.text

        keyboard = [[InlineKeyboardButton("Назад", callback_data="back_to_date")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(
            "Напишите ваше объявление, например: Ищу друга для философских дискуссий на глубокие темы.",
            reply_markup=reply_markup
        )
        return ANNOUNCEMENT

async def announcement_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle announcement text, post to chat, and send confirmation."""
    # Store announcement
    context.user_data["announcement"] = update.message.text

    # Format the announcement
    gender = context.user_data["gender"]
    location = context.user_data["location"]
    date = context.user_data["date"]
    announcement = context.user_data["announcement"]
    category_id = context.user_data["category_id"]

    # Create formatted message
    header_parts = [gender, location]
    if date:
        header_parts.append(date)
    
    header = ". ".join(header_parts)
    formatted_message = f">{header}\n{announcement}"

    # Post to the selected topic in the chat
    try:
        if not CHAT_ID:
            raise ValueError("CHAT_ID is not set")
        
        message = await context.bot.send_message(
            chat_id=CHAT_ID,
            text=formatted_message,
            message_thread_id=int(category_id) if category_id.isdigit() else None
        )

        # Generate a link to the message
        chat_id_clean = str(CHAT_ID).replace('-100', '')
        message_link = f"https://t.me/c/{chat_id_clean}/{message.message_id}"

        # Notify user of successful posting
        await update.message.reply_text(
            f"Объявление успешно опубликовано: {message_link}"
        )
    except Exception as e:
        logger.error(f"Error posting announcement: {e}")
        await update.message.reply_text("Ошибка при публикации объявления. Попробуйте снова.")

    # Clear user data
    context.user_data.clear()
    return ConversationHandler.END

async def handle_back_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle back button presses in announcement state."""
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "back_to_date":
        return await go_back_to_date(update, context)

# Helper functions for navigation
async def restart_conversation(update, context):
    """Restart conversation from beginning."""
    context.user_data.clear()
    return await start(update, context)

async def go_back_to_gender(update, context):
    """Go back to gender selection."""
    query = update.callback_query
    
    keyboard = [
        [InlineKeyboardButton("Мужской", callback_data="gender_male")],
        [InlineKeyboardButton("Женский", callback_data="gender_female")],
        [InlineKeyboardButton("Назад", callback_data="back_to_start")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        "Ваш пол:",
        reply_markup=reply_markup
    )
    return GENDER

async def go_back_to_date(update, context):
    """Go back to date selection."""
    query = update.callback_query
    
    keyboard = [
        [InlineKeyboardButton("Пропустить", callback_data="date_skip")],
        [InlineKeyboardButton("Назад", callback_data="back_to_gender")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        "Дата встречи, например: 06.05-10.05 (с-по), или точная дата, или пропустить:",
        reply_markup=reply_markup
    )
    return DATE

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel the conversation."""
    await update.message.reply_text("Операция отменена.")
    context.user_data.clear()
    return ConversationHandler.END

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle errors during conversation."""
    logger.error(f"Update {update} caused error {context.error}")
    if update and update.effective_message:
        try:
            await update.effective_message.reply_text("Произошла ошибка. Пожалуйста, попробуйте снова.")
        except Exception as e:
            logger.error(f"Failed to send error message: {e}")
    return ConversationHandler.END

def main():
    """Run the bot."""
    if not BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN is not set")
        raise ValueError("TELEGRAM_BOT_TOKEN is not set")

    application = Application.builder().token(BOT_TOKEN).build()

    # Conversation handler with per_message=True to avoid warnings
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            CATEGORY: [CallbackQueryHandler(category_selected)],
            GENDER: [CallbackQueryHandler(gender_selected)],
            LOCATION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, location_received),
                CallbackQueryHandler(gender_selected)  # Handle back button
            ],
            DATE: [
                CallbackQueryHandler(date_handler),
                MessageHandler(filters.TEXT & ~filters.COMMAND, date_handler)
            ],
            ANNOUNCEMENT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, announcement_received),
                CallbackQueryHandler(handle_back_buttons)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_message=True,  # This fixes the warning
    )

    # Add handlers
    application.add_handler(conv_handler)
    application.add_error_handler(error_handler)

    # Start the bot
    logger.info("Starting bot with webhooks")
    
    # Set webhook
    webhook_url = f"{RENDER_URL}{WEBHOOK_PATH}"
    
    # Use run_webhook method properly
    application.run_webhook(
        listen="0.0.0.0",
        port=int(os.environ.get("PORT", 10000)),
        url_path=WEBHOOK_PATH,
        webhook_url=webhook_url,
        secret_token=os.environ.get("WEBHOOK_SECRET_TOKEN")  # Optional but recommended
    )

if __name__ == "__main__":
    main()
