import os
import asyncio
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Updater, CommandHandler, CallbackQueryHandler, MessageHandler
from telegram.ext import filters
from sqlalchemy import create_engine, Column, Integer, String, ForeignKey
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, declarative_base
import time

# Инициализация бота и базы данных
bot_token = os.getenv('BOT_TOKEN')
group_id = os.getenv('GROUP_ID')
database_url = os.getenv('DATABASE_URL')
bot = Bot(token=bot_token)
engine = create_engine(database_url)
Base = declarative_base()
Session = sessionmaker(bind=engine)

# Модель базы данных
class User(Base):
    __tablename__ = 'users'
    id = Column(Integer, primary_key=True)
    language = Column(String)
    announcement_count = Column(Integer, default=0)

class Announcement(Base):
    __tablename__ = 'announcements'
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'))
    category = Column(String)
    gender = Column(String)
    city = Column(String)
    date = Column(String)
    message = Column(String)
    message_id = Column(Integer)
    complaint_count = Column(Integer, default=0)

Base.metadata.drop_all(engine, cascade=True)  # УДАЛЯЕТ все таблицы
Base.metadata.create_all(engine)  # СОЗДАЕТ заново

# Самопинг для поддержания активности
async def keep_alive():
    while True:
        await bot.get_me()
        await asyncio.sleep(25 * 60)  # Каждые 25 минут

# Начало работы с ботом
def start(update, context):
    keyboard = [
        [InlineKeyboardButton("Русский", callback_data='lang_ru')],
        [InlineKeyboardButton("English", callback_data='lang_en')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    update.message.reply_text('Выберите язык:', reply_markup=reply_markup)

# Обработка выбора языка
def button(update, context):
    query = update.callback_query
    query.answer()
    user_id = query.from_user.id
    session = Session()

    if query.data == 'lang_en':
        query.edit_message_text('Выберите язык:')
        return
    elif query.data == 'lang_ru':
        user = session.query(User).filter_by(id=user_id).first()
        if not user:
            user = User(id=user_id, language='ru')
            session.add(user)
            session.commit()
        select_category(query, context)
    elif query.data == 'change_lang':
        keyboard = [
            [InlineKeyboardButton("Русский", callback_data='lang_ru')],
            [InlineKeyboardButton("English", callback_data='lang_en')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        query.edit_message_text('Выберите язык:', reply_markup=reply_markup)
    elif query.data.startswith('cat_'):
        context.user_data['category'] = query.data[4:]
        select_gender(query, context)
    elif query.data == 'back_to_categories':
        select_category(query, context)
    elif query.data in ['gender_male', 'gender_female']:
        context.user_data['gender'] = 'Мужской' if query.data == 'gender_male' else 'Женский'
        ask_city(query, context)
    elif query.data == 'complain':
        handle_complaint(query, context)
    elif query.data.startswith('delete_'):
        delete_announcement(query, context, query.data[7:])
    session.close()

# Выбор категории
def select_category(query, context):
    # Пример категорий, можно получать из Telegram или базы
    categories = ['Тема1', 'Тема2', 'Тема3']
    keyboard = [[InlineKeyboardButton(cat, callback_data=f'cat_{cat}')] for cat in categories]
    keyboard.insert(0, [InlineKeyboardButton("Сменить язык", callback_data='change_lang')])
    reply_markup = InlineKeyboardMarkup(keyboard)
    query.edit_message_text('Выберите категорию:', reply_markup=reply_markup)

# Выбор пола
def select_gender(query, context):
    keyboard = [
        [InlineKeyboardButton("Мужской", callback_data='gender_male')],
        [InlineKeyboardButton("Женский", callback_data='gender_female')],
        [InlineKeyboardButton("Назад", callback_data='back_to_categories')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    query.edit_message_text('Выберите пол:', reply_markup=reply_markup)

# Ввод города
def ask_city(query, context):
    query.edit_message_text('Ваш город?')

def process_city(update, context):
    context.user_data['city'] = update.message.text
    update.message.reply_text('Когда будет происходить событие? (например, 05.07/20.07 или конкретная дата)')

# Ввод даты
def process_date(update, context):
    context.user_data['date'] = update.message.text
    update.message.reply_text('Введите ваше сообщение. Пример: "Всем привет, ищу друга для общения"')

# Публикация объявления
def post_announcement(update, context):
    session = Session()
    user_id = update.message.from_user.id
    user = session.query(User).filter_by(id=user_id).first()

    if user.announcement_count >= 3:
        announcements = session.query(Announcement).filter_by(user_id=user_id).all()
        keyboard = [[InlineKeyboardButton(f"Удалить из {ann.category}", callback_data=f'delete_{ann.category}')] for ann in announcements]
        reply_markup = InlineKeyboardMarkup(keyboard)
        update.message.reply_text('У вас уже есть 3 объявления. Удалите одно, чтобы создать новое.', reply_markup=reply_markup)
        session.close()
        return

    message = update.message.text
    category = context.user_data['category']
    gender = context.user_data['gender']
    city = context.user_data['city']
    date = context.user_data['date']
    
    announcement_text = f"{gender}. {city}. {date}\n\n{message}"
    sent_message = bot.send_message(chat_id=group_id, text=announcement_text, message_thread_id=category)
    
    keyboard = [
        [InlineKeyboardButton("Пожаловаться", callback_data='complain'),
         InlineKeyboardButton("Написать", url=f'tg://user?id={user_id}')]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    bot.send_message(chat_id=group_id, text="Действия:", reply_markup=reply_markup, message_thread_id=category)

    announcement = Announcement(user_id=user_id, category=category, gender=gender, city=city, date=date, message=message, message_id=sent_message.message_id)
    session.add(announcement)
    user.announcement_count += 1
    session.commit()
    session.close()

# Обработка жалоб
def handle_complaint(query, context):
    session = Session()
    announcement = session.query(Announcement).filter_by(message_id=query.message.message_id).first()
    if announcement:
        announcement.complaint_count += 1
        if announcement.complaint_count >= 5:
            bot.delete_message(chat_id=group_id, message_id=announcement.message_id)
            bot.send_message(chat_id=announcement.user_id, text="Ваше объявление удалено из-за нарушений.")
            session.delete(announcement)
        session.commit()
    session.close()

# Удаление объявления
def delete_announcement(query, context, category):
    session = Session()
    user_id = query.from_user.id
    announcement = session.query(Announcement).filter_by(user_id=user_id, category=category).first()
    if announcement:
        bot.delete_message(chat_id=group_id, message_id=announcement.message_id)
        session.delete(announcement)
        user = session.query(User).filter_by(id=user_id).first()
        user.announcement_count -= 1
        session.commit()
    session.close()
    query.edit_message_text('Объявление удалено. Можете создать новое.')

# Основная функция
def main():
    updater = Updater(bot_token, use_context=True)
    dp = updater.dispatcher
    dp.add_handler(CommandHandler('start', start))
    dp.add_handler(CallbackQueryHandler(button))
    dp.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, process_city, pass_user_data=True))
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command & Filters.chat_type.private, process_date, pass_user_data=True))
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command & Filters.chat_type.private, post_announcement, pass_user_data=True))

    updater.start_webhook(listen="0.0.0.0", port=int(os.environ.get("PORT", 5000)), url_path=bot_token)
    updater.bot.set_webhook(url=f"https://{os.environ['RENDER_EXTERNAL_HOSTNAME']}/{bot_token}")
    asyncio.ensure_future(keep_alive())
    updater.idle()

if __name__ == '__main__':
    main()
