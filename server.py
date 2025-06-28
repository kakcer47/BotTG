import os
import asyncio
import logging
import psycopg2
import aiohttp
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
from telegram.constants import ParseMode
from aiohttp import web
import json

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Конфигурация
BOT_TOKEN = os.getenv('BOT_TOKEN')
DATABASE_URL = os.getenv('DATABASE_URL')
GROUP_ID = os.getenv('GROUP_ID')  # ID группы где публиковать
WEBHOOK_URL = os.getenv('WEBHOOK_URL')  # URL для webhook
PORT = int(os.getenv('PORT', 8443))

class Database:
    def __init__(self, database_url):
        self.database_url = database_url
        self.init_db()
    
    def get_connection(self):
        return psycopg2.connect(self.database_url)
    
    def init_db(self):
        """Инициализация базы данных"""
        conn = self.get_connection()
        try:
            with conn.cursor() as cur:
                # Таблица пользователей с лимитами
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS users (
                        user_id BIGINT PRIMARY KEY,
                        language VARCHAR(10) DEFAULT 'ru',
                        ads_count INTEGER DEFAULT 0,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                
                # Таблица объявлений
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS ads (
                        id SERIAL PRIMARY KEY,
                        user_id BIGINT NOT NULL,
                        message_id INTEGER NOT NULL,
                        topic_id INTEGER NOT NULL,
                        topic_name VARCHAR(255) NOT NULL,
                        complaints INTEGER DEFAULT 0,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY (user_id) REFERENCES users(user_id)
                    )
                """)
                
                # Индексы для оптимизации
                cur.execute("CREATE INDEX IF NOT EXISTS idx_ads_user_id ON ads(user_id)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_ads_message_id ON ads(message_id)")
                
            conn.commit()
        finally:
            conn.close()
    
    def get_user(self, user_id):
        """Получить данные пользователя"""
        conn = self.get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM users WHERE user_id = %s", (user_id,))
                return cur.fetchone()
        finally:
            conn.close()
    
    def create_or_update_user(self, user_id, language='ru'):
        """Создать или обновить пользователя"""
        conn = self.get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO users (user_id, language) 
                    VALUES (%s, %s) 
                    ON CONFLICT (user_id) 
                    DO UPDATE SET language = EXCLUDED.language
                """, (user_id, language))
            conn.commit()
        finally:
            conn.close()
    
    def get_user_ads(self, user_id):
        """Получить объявления пользователя"""
        conn = self.get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id, message_id, topic_id, topic_name, created_at 
                    FROM ads WHERE user_id = %s ORDER BY created_at DESC
                """, (user_id,))
                return cur.fetchall()
        finally:
            conn.close()
    
    def add_ad(self, user_id, message_id, topic_id, topic_name):
        """Добавить объявление"""
        conn = self.get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO ads (user_id, message_id, topic_id, topic_name) 
                    VALUES (%s, %s, %s, %s)
                """, (user_id, message_id, topic_id, topic_name))
                
                cur.execute("""
                    UPDATE users SET ads_count = ads_count + 1 
                    WHERE user_id = %s
                """, (user_id,))
            conn.commit()
        finally:
            conn.close()
    
    def delete_ad(self, ad_id, user_id):
        """Удалить объявление"""
        conn = self.get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM ads WHERE id = %s AND user_id = %s", (ad_id, user_id))
                if cur.rowcount > 0:
                    cur.execute("""
                        UPDATE users SET ads_count = ads_count - 1 
                        WHERE user_id = %s AND ads_count > 0
                    """, (user_id,))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()
    
    def add_complaint(self, message_id):
        """Добавить жалобу к объявлению"""
        conn = self.get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE ads SET complaints = complaints + 1 
                    WHERE message_id = %s RETURNING complaints, user_id, topic_name
                """, (message_id,))
                result = cur.fetchone()
            conn.commit()
            return result
        finally:
            conn.close()
    
    def delete_ad_by_message_id(self, message_id):
        """Удалить объявление по message_id"""
        conn = self.get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT user_id FROM ads WHERE message_id = %s", (message_id,))
                user_data = cur.fetchone()
                if user_data:
                    cur.execute("DELETE FROM ads WHERE message_id = %s", (message_id,))
                    cur.execute("""
                        UPDATE users SET ads_count = ads_count - 1 
                        WHERE user_id = %s AND ads_count > 0
                    """, (user_data[0],))
            conn.commit()
            return user_data[0] if user_data else None
        finally:
            conn.close()

class TelegramBot:
    def __init__(self):
        self.db = Database(DATABASE_URL)
        self.app = Application.builder().token(BOT_TOKEN).build()
        self.user_states = {}  # Хранение состояний пользователей в памяти
        
        # Тексты
        self.texts = {
            'ru': {
                'language': 'Язык',
                'choose_category': 'Выберите категорию',
                'change_language': 'Сменить язык',
                'create_ad': '''Создайте объявление. Ваш пол, Место где, Дату когда с/по (дату можно пропустить, либо укажите точный день)

Пример:
"Мужчина. Москва. 24.09/05.10 
Ищу друга для философских разговоров"

Напишите объявление, если оно корректно мы его опубликуем в группу.''',
                'limit_reached': 'Лимит 3 шт, удалите старые - чтобы создать новые.',
                'ad_published': 'Объявление опубликовано в группу',
                'ad_deleted': 'Объявление удалено из группы из-за нарушений',
                'complaint': 'Пожаловаться',
                'write': 'Написать',
                'no_ads': 'У вас нет объявлений для удаления'
            },
            'en': {
                'language': 'Language',
                'choose_category': 'Choose category',
                'change_language': 'Change language',
                'create_ad': 'Create advertisement...',
                'limit_reached': 'Limit 3 ads, delete old ones to create new.',
                'ad_published': 'Advertisement published to group',
                'ad_deleted': 'Advertisement deleted from group due to violations',
                'complaint': 'Report',
                'write': 'Write',
                'no_ads': 'You have no ads to delete'
            }
        }
    
    async def get_group_topics(self):
        """Получить темы из группы"""
        try:
            # Пытаемся получить темы через API (для групп с включенными темами)
            # В реальном API нужно использовать getForumTopicIconStickers или подобный метод
            # Пока используем статический список
            return [
                {'id': 1, 'name': 'Общение'},
                {'id': 2, 'name': 'Работа'},
                {'id': 3, 'name': 'Недвижимость'},
                {'id': 4, 'name': 'Услуги'},
                {'id': 5, 'name': 'Продажа'},
                {'id': 6, 'name': 'Знакомства'},
                {'id': 7, 'name': 'Обучение'},
                {'id': 8, 'name': 'Спорт'}
            ]
        except Exception as e:
            logger.error(f"Error getting topics: {e}")
            return [{'id': 1, 'name': 'Общение'}]
    
    def get_text(self, user_id, key):
        """Получить текст на языке пользователя"""
        user = self.db.get_user(user_id)
        lang = user[1] if user else 'ru'
        return self.texts.get(lang, self.texts['ru']).get(key, key)
    
    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда /start"""
        user_id = update.effective_user.id
        
        # Создаем пользователя если не существует
        self.db.create_or_update_user(user_id)
        
        # Клавиатура выбора языка
        keyboard = [
            [InlineKeyboardButton("Русский", callback_data="lang_ru")],
            [InlineKeyboardButton("English", callback_data="lang_en")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            self.get_text(user_id, 'language'),
            reply_markup=reply_markup
        )
    
    async def button_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработка нажатий кнопок"""
        query = update.callback_query
        user_id = query.from_user.id
        data = query.data
        
        await query.answer()
        
        if data.startswith('lang_'):
            # Выбор языка
            lang = data.split('_')[1]
            self.db.create_or_update_user(user_id, lang)
            
            # Показать категории
            await self.show_categories(query, user_id)
            
        elif data == 'change_language':
            # Смена языка
            keyboard = [
                [InlineKeyboardButton("Русский", callback_data="lang_ru")],
                [InlineKeyboardButton("English", callback_data="lang_en")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.edit_message_text(
                self.get_text(user_id, 'language'),
                reply_markup=reply_markup
            )
            
        elif data.startswith('topic_'):
            # Выбор темы
            topic_id = int(data.split('_')[1])
            
            # Проверить лимит
            user_ads = self.db.get_user_ads(user_id)
            if len(user_ads) >= 3:
                await self.show_delete_ads(query, user_id, user_ads)
                return
            
            # Сохранить выбранную тему
            self.user_states[user_id] = {'topic_id': topic_id}
            
            await query.edit_message_text(
                self.get_text(user_id, 'create_ad')
            )
            
        elif data.startswith('delete_'):
            # Удаление объявления
            ad_id = int(data.split('_')[1])
            if self.db.delete_ad(ad_id, user_id):
                # Удалить из группы
                try:
                    user_ads = self.db.get_user_ads(user_id)
                    for ad in user_ads:
                        if ad[0] == ad_id:
                            await context.bot.delete_message(GROUP_ID, ad[1])
                            break
                except:
                    pass
                
                # Показать обновленный список или категории
                remaining_ads = self.db.get_user_ads(user_id)
                if len(remaining_ads) >= 3:
                    await self.show_delete_ads(query, user_id, remaining_ads)
                else:
                    await self.show_categories(query, user_id)
            
        elif data.startswith('complaint_'):
            # Жалоба на объявление
            message_id = int(data.split('_')[1])
            result = self.db.add_complaint(message_id)
            
            if result and result[0] >= 5:  # 5 жалоб
                # Удалить объявление
                user_id_owner = self.db.delete_ad_by_message_id(message_id)
                try:
                    await context.bot.delete_message(GROUP_ID, message_id)
                    if user_id_owner:
                        await context.bot.send_message(
                            user_id_owner,
                            f"Ваше объявление в теме {result[2]} было удалено из-за нарушений"
                        )
                except:
                    pass
    
    async def show_categories(self, query, user_id):
        """Показать категории (темы)"""
        topics = await self.get_group_topics()
        
        keyboard = [[InlineKeyboardButton(self.get_text(user_id, 'change_language'), callback_data='change_language')]]
        
        for topic in topics:
            keyboard.append([InlineKeyboardButton(topic['name'], callback_data=f"topic_{topic['id']}")])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            self.get_text(user_id, 'choose_category'),
            reply_markup=reply_markup
        )
    
    async def show_delete_ads(self, query, user_id, user_ads):
        """Показать объявления для удаления"""
        text = self.get_text(user_id, 'limit_reached')
        
        if not user_ads:
            text = self.get_text(user_id, 'no_ads')
            await query.edit_message_text(text)
            return
        
        keyboard = []
        for ad in user_ads:
            ad_id, message_id, topic_id, topic_name, created_at = ad
            keyboard.append([InlineKeyboardButton(
                f"{topic_name} ({created_at.strftime('%d.%m')})",
                callback_data=f"delete_{ad_id}"
            )])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(text, reply_markup=reply_markup)
    
    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработка текстовых сообщений"""
        user_id = update.effective_user.id
        text = update.message.text
        
        if user_id in self.user_states and 'topic_id' in self.user_states[user_id]:
            # Это объявление
            topic_id = self.user_states[user_id]['topic_id']
            
            # Получить название темы
            topics = await self.get_group_topics()
            topic_name = next((t['name'] for t in topics if t['id'] == topic_id), 'Общение')
            
            # Создать кнопки для объявления
            keyboard = [
                [
                    InlineKeyboardButton(
                        self.get_text(user_id, 'complaint'),
                        callback_data=f"complaint_{0}"  # message_id будет заменен после публикации
                    ),
                    InlineKeyboardButton(
                        self.get_text(user_id, 'write'),
                        url=f"tg://user?id={user_id}"
                    )
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            try:
                # Публикация в группу
                message = await context.bot.send_message(
                    GROUP_ID,
                    text,
                    reply_markup=reply_markup,
                    message_thread_id=topic_id if topic_id > 1 else None
                )
                
                # Обновить кнопку жалобы с правильным message_id
                keyboard[0][0] = InlineKeyboardButton(
                    self.get_text(user_id, 'complaint'),
                    callback_data=f"complaint_{message.message_id}"
                )
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                await context.bot.edit_message_reply_markup(
                    GROUP_ID,
                    message.message_id,
                    reply_markup=reply_markup
                )
                
                # Сохранить в базу
                self.db.add_ad(user_id, message.message_id, topic_id, topic_name)
                
                await update.message.reply_text(self.get_text(user_id, 'ad_published'))
                
            except Exception as e:
                logger.error(f"Error publishing ad: {e}")
                await update.message.reply_text("Ошибка при публикации объявления")
            
            # Очистить состояние
            if user_id in self.user_states:
                del self.user_states[user_id]
    
    async def self_ping(self, context: ContextTypes.DEFAULT_TYPE):
        """Самопинг для предотвращения засыпания"""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(WEBHOOK_URL) as response:
                    logger.info(f"Self-ping status: {response.status}")
        except Exception as e:
            logger.error(f"Self-ping error: {e}")
    
    def setup_handlers(self):
        """Настройка обработчиков"""
        self.app.add_handler(CommandHandler("start", self.start))
        self.app.add_handler(CallbackQueryHandler(self.button_callback))
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))
        
        # Самопинг каждые 25 минут
        job_queue = self.app.job_queue
        job_queue.run_repeating(self.self_ping, interval=1500, first=10)
    
    async def health_check(self, request):
        """Health check endpoint для Render"""
        return web.Response(text="OK", status=200)
    
    async def run_webhook(self):
        """Запуск с webhook для Render"""
        await self.app.initialize()
        await self.app.start()
        
        # Установка webhook
        webhook_url = f"{WEBHOOK_URL}/webhook"
        await self.app.bot.set_webhook(webhook_url)
        
        # Создание дополнительных маршрутов
        async def setup_routes(application):
            application.router.add_get('/health', self.health_check)
        
        # Запуск webhook сервера
        await self.app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            webhook_url=webhook_url,
            drop_pending_updates=True,
            webhook_app_setup=setup_routes
        )
    
    async def run_polling(self):
        """Запуск с polling для разработки"""
        await self.app.run_polling(drop_pending_updates=True)

async def main():
    bot = TelegramBot()
    bot.setup_handlers()
    
    if WEBHOOK_URL:
        await bot.run_webhook()
    else:
        await bot.run_polling()

if __name__ == "__main__":
    asyncio.run(main())
