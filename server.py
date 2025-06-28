import os
import asyncio
import threading
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Updater, CommandHandler, CallbackQueryHandler, MessageHandler, filters, CallbackContext
from sqlalchemy import Column, Integer, String, ForeignKey, create_engine, text
from sqlalchemy.orm import sessionmaker, declarative_base

# Инициализация переменных окружения
bot_token = os.getenv('BOT_TOKEN')
group_id = int(os.getenv('GROUP_ID'))  # убедись, что это число
database_url = os.getenv('DATABASE_URL')
bot = Bot(token=bot_token)

# SQLAlchemy настройка
engine = create_engine(database_url)
Base = declarative_base()
Session = sessionmaker(bind=engine)

# Удаляем таблицы
with engine.connect() as conn:
    conn.execute(text("DROP TABLE IF EXISTS announcements CASCADE"))
    conn.execute(text("DROP TABLE IF EXISTS users CASCADE"))
    conn.commit()

# Модели
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

Base.metadata.create_all(engine)

# Соответствие категорий к thread_id в супергруппе
CATEGORY_TO_THREAD_ID = {
    "Тема1": 101,  # Замените на реальные ID топиков
    "Тема2": 102,
    "Тема3": 103
}

# Keep-alive
async def keep_alive():
    while True:
        await bot.get_me()
        await asyncio.sleep(25 * 60)

def start_keep_alive():
    asyncio.run(keep_alive())

# Команда /start
def start(update: Update, context: CallbackContext):
    keyboard = [
        [InlineKeyboardButton("Русский", callback_data='lang_ru')],
        [InlineKeyboardButton("English", callback_data='lang_en')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    update.message.reply_text('Выберите язык:', reply_markup=reply_markup)

# Обработка кнопок
def button(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    user_id = query.from_user.id
    session = Session()

    data = query.data

    if data == 'lang_en':
        query.edit_message_text('Select your language:')
    elif data == 'lang_ru':
        user = session.query(User).filter_by(id=user_id).first()
        if not user:
            user = User(id=user_id, language='ru')
            session.add(user)
            session.commit()
        select_category(query)
    elif data == 'change_lang':
        keyboard = [
            [InlineKeyboardButton("Русский", callback_data='lang_ru')],
            [InlineKeyboardButton("English", callback_data='lang_en')]
        ]
        query.edit_message_text('Выберите язык:', reply_markup=InlineKeyboardMarkup(keyboard))
    elif data.startswith('cat_'):
        context.user_data['category'] = data[4:]
        select_gender(query)
    elif data == 'back_to_categories':
        select_category(query)
    elif data in ['gender_male', 'gender_female']:
        context.user_data['gender'] = 'Мужской' if data == 'gender_male' else 'Женский'
        context.user_data['step'] = 'city'
        query.edit_message_text('Ваш город?')
    elif data == 'complain':
        handle_complaint(query)
    elif data.startswith('delete_'):
        delete_announcement(query, data[7:])
    session.close()

def select_category(query):
    keyboard = [[InlineKeyboardButton(cat, callback_data=f'cat_{cat}')] for cat in CATEGORY_TO_THREAD_ID]
    keyboard.insert(0, [InlineKeyboardButton("Сменить язык", callback_data='change_lang')])
    query.edit_message_text('Выберите категорию:', reply_markup=InlineKeyboardMarkup(keyboard))

def select_gender(query):
    keyboard = [
        [InlineKeyboardButton("Мужской", callback_data='gender_male')],
        [InlineKeyboardButton("Женский", callback_data='gender_female')],
        [InlineKeyboardButton("Назад", callback_data='back_to_categories')]
    ]
    query.edit_message_text('Выберите пол:', reply_markup=InlineKeyboardMarkup(keyboard))

# Обработка пользовательского ввода по шагам
def process_input(update: Update, context: CallbackContext):
    step = context.user_data.get('step')
    text = update.message.text

    if step == 'city':
        context.user_data['city'] = text
        context.user_data['step'] = 'date'
        update.message.reply_text('Когда будет происходить событие?')
    elif step == 'date':
        context.user_data['date'] = text
        context.user_data['step'] = 'message'
        update.message.reply_text('Введите ваше сообщение:')
    elif step == 'message':
        context.user_data['message'] = text
        context.user_data['step'] = None
        post_announcement(update, context)

# Публикация объявления
def post_announcement(update: Update, context: CallbackContext):
    session = Session()
    user_id = update.message.from_user.id
    user = session.query(User).filter_by(id=user_id).first()

    if user.announcement_count >= 3:
        announcements = session.query(Announcement).filter_by(user_id=user_id).all()
        keyboard = [[InlineKeyboardButton(f"Удалить из {ann.category}", callback_data=f'delete_{ann.category}')] for ann in announcements]
        update.message.reply_text('У вас уже есть 3 объявления. Удалите одно, чтобы создать новое.', reply_markup=InlineKeyboardMarkup(keyboard))
        session.close()
        return

    category = context.user_data['category']
    thread_id = CATEGORY_TO_THREAD_ID.get(category)
    text_msg = f"{context.user_data['gender']}. {context.user_data['city']}. {context.user_data['date']}\n\n{context.user_data['message']}"
    
    sent_message = bot.send_message(chat_id=group_id, text=text_msg, message_thread_id=thread_id)
    
    keyboard = [
        [InlineKeyboardButton("Пожаловаться", callback_data='complain'),
         InlineKeyboardButton("Написать", url=f'tg://user?id={user_id}')]]
    bot.send_message(chat_id=group_id, text="Действия:", reply_markup=InlineKeyboardMarkup(keyboard), message_thread_id=thread_id)

    ann = Announcement(
        user_id=user_id,
        category=category,
        gender=context.user_data['gender'],
        city=context.user_data['city'],
        date=context.user_data['date'],
        message=context.user_data['message'],
        message_id=sent_message.message_id
    )
    session.add(ann)
    user.announcement_count += 1
    session.commit()
    session.close()
    update.message.reply_text("Объявление опубликовано!")

# Жалоба
def handle_complaint(query):
    session = Session()
    ann = session.query(Announcement).filter_by(message_id=query.message.message_id).first()
    if ann:
        ann.complaint_count += 1
        if ann.complaint_count >= 5:
            bot.delete_message(chat_id=group_id, message_id=ann.message_id)
            bot.send_message(chat_id=ann.user_id, text="Ваше объявление удалено из-за жалоб.")
            session.delete(ann)
        session.commit()
    session.close()
    query.edit_message_text("Жалоба учтена.")

# Удаление объявления
def delete_announcement(query, category):
    session = Session()
    user_id = query.from_user.id
    ann = session.query(Announcement).filter_by(user_id=user_id, category=category).first()
    if ann:
        bot.delete_message(chat_id=group_id, message_id=ann.message_id)
        session.delete(ann)
        user = session.query(User).filter_by(id=user_id).first()
        user.announcement_count -= 1
        session.commit()
    session.close()
    query.edit_message_text("Объявление удалено. Можете создать новое.")

# Основная функция
def main():
    updater = Updater(bot_token, use_context=True)
    dp = updater.dispatcher

    dp.add_handler(CommandHandler('start', start))
    dp.add_handler(CallbackQueryHandler(button))
    dp.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, process_input))

    # Запуск webhook
    updater.start_webhook(listen="0.0.0.0", port=int(os.environ.get("PORT", 5000)), url_path=bot_token)
    updater.bot.set_webhook(url=f"https://{os.environ['RENDER_EXTERNAL_HOSTNAME']}/{bot_token}")

    # Keep-alive
    threading.Thread(target=start_keep_alive, daemon=True).start()

    updater.idle()

if __name__ == '__main__':
    main()
