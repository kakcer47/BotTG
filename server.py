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
from uuid import uuid4

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
Thread(target=run_scheduler, daemon=True).start()

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
        keyboard.append([InlineKeyboardButton("Back", callback_data="back_start")])
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(
            "In which category would you like to create an announcement?",
            reply_markup=reply_markup
        )
        return CATEGORY
    except Exception as e:
        logger.error(f"Error fetching topics: {e}")
        await update.message.reply_text("Error fetching categories. Try again.")
        return ConversationHandler.END

async def category_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle category selection, ask for gender."""
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "back_start":
        await query.message.delete()
        return await start(update, context)

    # Store selected category
    context.user_data["category"] = data.split("_")[1]
    context.user_data["category_id"] = data.split("_")[2]

    keyboard = [
        [InlineKeyboardButton("Male", callback_data="gender_male")],
        [InlineKeyboardButton("Female", callback_data="gender_female")],
        [InlineKeyboardButton("Back", callback_data="back_category")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.message.delete()
    await query.message.reply_text(
        "Your gender:",
        reply_markup=reply_markup
    )
    return GENDER

async def gender_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle gender selection, ask for location."""
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "back_category":
        await query.message.delete()
        return await start(update, context)

    # Store selected gender
    context.user_data["gender"] = "Male" if data == "gender_male" else "Female"

    keyboard = [[InlineKeyboardButton("Back", callback_data="back_gender")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.message.delete()
    await query.message.reply_text(
        "Write where the meeting will take place:",
        reply_markup=reply_markup
    )
    return LOCATION

async def location_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle location input, ask for date."""
    if update.message.text == "Back":
        await update.message.delete()
        return await gender_selected(update, context)

    # Store location
    context.user_data["location"] = update.message.text

    keyboard = [
        [InlineKeyboardButton("Skip", callback_data="date_skip")],
        [InlineKeyboardButton("Back", callback_data="back_location")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.delete()
    await update.message.reply_text(
        "Date of the meeting, example: 06.05-10.05 (from-to), or exact date, or skip:",
        reply_markup=reply_markup
    )
    return DATE

async def date_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle date input or skip, ask for announcement text."""
    query = update.callback_query
    if query:
        await query.answer()
        data = query.data

        if data == "back_location":
            await query.message.delete()
            return await gender_selected(update, context)
        elif data == "date_skip":
            context.user_data["date"] = ""
            await query.message.delete()
            keyboard = [[InlineKeyboardButton("Back", callback_data="back_date")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.message.reply_text(
                "Write your announcement, example: Looking for a friend for philosophical discussions on deep topics.",
                reply_markup=reply_markup
            )
            return ANNOUNCEMENT
    else:
        # Validate date format (basic check)
        date_text = update.message.text
        if date_text == "Back":
            await update.message.delete()
            return await gender_selected(update, context)
        # Allow any format for simplicity
        context.user_data["date"] = date_text

        keyboard = [[InlineKeyboardButton("Back", callback_data="back_date")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.delete()
        await update.message.reply_text(
            "Write your announcement, example: Looking for a friend for philosophical discussions on deep topics.",
            reply_markup=reply_markup
        )
        return ANNOUNCEMENT

async def announcement_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle announcement text, post to chat, and send confirmation."""
    if update.message.text == "Back":
        await update.message.delete()
        return await date_selected(update, context)

    # Store announcement
    context.user_data["announcement"] = update.message.text

    # Format the announcement
    gender = context.user_data["gender"]
    location = context.user_data["location"]
    date = context.user_data["date"]
    announcement = context.user_data["announcement"]
    category_id = context.user_data["category_id"]

    # Create formatted message
    header = f"{gender}. {location}. {date}".strip()
    if header.endswith("."):
        header = header[:-1]  # Remove trailing dot if date is empty
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
        message_link = f"https://t.me/c/{str(CHAT_ID).replace('-100', '')}/{message.message_id}"

        # Notify user of successful posting
        await update.message.reply_text(
            f"Announcement posted successfully: {message_link}"
        )
    except Exception as e:
        logger.error(f"Error posting announcement: {e}")
        await update.message.reply_text("Error posting announcement. Try again.")
        return ConversationHandler.END

    # Clear user data
    context.user_data.clear()
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel the conversation."""
    await update.message.reply_text("Operation cancelled.")
    context.user_data.clear()
    return ConversationHandler.END

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle errors during conversation."""
    logger.error(f"Update {update} caused error {context.error}")
    if update and update.message:
        await update.message.reply_text("An error occurred. Please try again.")
    else:
        logger.warning("No update.message available, skipping reply")
    return ConversationHandler.END

async def set_webhook():
    """Set the webhook for the bot."""
    webhook_url = f"{RENDER_URL}{WEBHOOK_PATH}"
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook?url={webhook_url}"
    response = requests.get(url)
    logger.info(f"Set webhook response: {response.json()}")

def main():
    """Run the bot."""
    if not BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN is not set")
        raise ValueError("TELEGRAM_BOT_TOKEN is not set")

    application = Application.builder().token(BOT_TOKEN).build()

    # Conversation handler
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            CATEGORY: [CallbackQueryHandler(category_selected)],
            GENDER: [CallbackQueryHandler(gender_selected)],
            LOCATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, location_received)],
            DATE: [
                CallbackQueryHandler(date_selected),
                MessageHandler(filters.TEXT & ~filters.COMMAND, date_selected)
            ],
            ANNOUNCEMENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, announcement_received)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    # Add handlers
    application.add_handler(conv_handler)
    application.add_error_handler(error_handler)

    # Set webhook and start the bot
    logger.info("Setting webhook and starting bot")
    import asyncio
    asyncio.run(set_webhook())
    application.run_webhook(
        listen="0.0.0.0",
        port=10000,
        url_path=WEBHOOK_PATH,
        webhook_url=f"{RENDER_URL}{WEBHOOK_PATH}"
    )

if __name__ == "__main__":
    main()
