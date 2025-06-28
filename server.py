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

# Environment variable for bot token (set in Render)
import os
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
RENDER_URL = os.environ.get("RENDER_EXTERNAL_URL", "http://localhost:10000")

# Function to ping the bot itself to prevent Render from idling
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
    try:
        # Fetch chat topics (threads) from the chat
        forum_topics = await context.bot.get_forum_topic_icon_suggestions(chat_id)
        topics = []
        # Simulate topic fetching (replace with actual topic retrieval logic if needed)
        # For simplicity, assuming topics are predefined or fetched dynamically
        topic_names = ["General", "Philosophy", "Meetups", "Discussions"]  # Example topics
        for i, topic in enumerate(topic_names):
            topics.append(
                InlineKeyboardButton(topic, callback_data=f"category_{topic}_{i}")
            )

        topics.append(InlineKeyboardButton("Back", callback_data="back_start"))
        keyboard = [topics[i:i+2] for i in range(0, len(topics), 2)]  # 2 buttons per row
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
        return await category_selected(update, context)

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
        # Allow any format for simplicity, or add regex for stricter validation
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
    chat_id = update.effective_chat.id
    try:
        message = await context.bot.send_message(
            chat_id=chat_id,
            text=formatted_message,
            message_thread_id=int(category_id) if category_id.isdigit() else None
        )

        # Generate a link to the message (Telegram doesn't provide direct links easily, so approximate)
        message_link = f"https://t.me/c/{str(chat_id).replace('-100', '')}/{message.message_id}"

        # Notify user of successful posting
        await update.message.reply_text(
            f"Announcement posted successfully: {message_link}"
        )
    except Exception as e:
        logger.error(f"Error posting announcement: {e}")
        await update.message.reply_text("Error posting announcement. Try again.")

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
    if update.message:
        await update.message.reply_text("An error occurred. Please try again.")
    return ConversationHandler.END

def main():
    """Run the bot."""
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

    # Start the bot
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
