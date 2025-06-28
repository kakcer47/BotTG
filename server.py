import os
import asyncio
import logging
import sqlite3
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
from telegram.constants import ParseMode
import json

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Конфигурация
BOT_TOKEN = os.getenv('BOT_TOKEN')
GROUP_ID = os.getenv('GROUP_ID')
WEBHOOK_URL = os.getenv('WEBHOOK_URL')
PORT = int(os.getenv('PORT', 8443))

class Database:
    def __init__(self):
        self.db_path = 'bot_data.db'
        self.init_db()
    
    def get_connection(self):
        return sqlite3.connect(self.db_path)
    
    def init_db(self):
        """Инициализация базы данных"""
        try:
            conn = self.get_connection()
            cur = conn.cursor()
            
            # Таблица пользователей с лимитами
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    language TEXT DEFAULT 'ru',
                    ads_count INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # Таблица объявлений
            cur.execute("""
                CREATE TABLE IF NOT EXISTS ads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    topic_id INTEGER NOT NULL,
                    topic_name TEXT NOT NULL,
                    complaints INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(user_id)
                )
            """)
            
            # Индексы для оптимизации
            cur.execute("CREATE INDEX IF NOT EXISTS idx_ads_user_id ON ads(user_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_ads_message_id ON ads(message_id)")
            
            conn.commit()
            conn.close()
            logger.info("SQLite database initialized successfully")
        except Exception as e:
            logger.error(f"Database initialization failed: {e}")
    
    def get_user(self, user_id):
        """Получить данные пользователя"""
        try:
            conn = self.get_connection()
            cur = conn.cursor()
            cur.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
            result = cur.fetchone()
            conn.close()
            return result
        except Exception as e:
            logger.error(f"Database error in get_user: {e}")
            return None
    
    def create_or_update_user(self, user_id, language='ru'):
        """Создать или обновить пользователя"""
        try:
            conn = self.get_connection()
            cur = conn.cursor()
            cur.execute("""
                INSERT OR REPLACE INTO users (user_id, language) 
                VALUES (?, ?)
            """, (user_id, language))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Database error in create_or_update_user: {e}")
    
    def get_user_ads(self, user_id):
        """Получить объявления пользователя"""
        try:
            conn = self.get_connection()
            cur = conn.cursor()
            cur.execute("""
                SELECT id, message_id, topic_id, topic_name, created_at 
                FROM ads WHERE user_id = ? ORDER BY created_at DESC
            """, (user_id,))
            result = cur.fetchall()
            conn.close()
            return result
        except Exception as e:
            logger.error(f"Database error in get_user_ads: {e}")
            return []
    
    def add_ad(self, user_id, message_id, topic_id, topic_name):
        """Добавить объявление"""
        try:
            conn = self.get_connection()
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO ads (user_id, message_id, topic_id, topic_name) 
                VALUES (?, ?, ?, ?)
            """, (user_id, message_id, topic_id, topic_name))
            
            cur.execute("""
                UPDATE users SET ads_count = ads_count + 1 
                WHERE user_id = ?
            """, (user_id,))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Database error in add_ad: {e}")
    
    def delete_ad(self, ad_id, user_id):
        """Удалить объявление"""
        try:
            conn = self.get_connection()
            cur = conn.cursor()
            cur.execute("DELETE FROM ads WHERE id = ? AND user_id = ?", (ad_id, user_id))
            deleted = cur.rowcount > 0
            if deleted:
                cur.execute("""
                    UPDATE users SET ads_count = ads_count - 1 
                    WHERE user_id = ? AND ads_count > 0
                """, (user_id,))
            conn.commit()
            conn.close()
            return deleted
        except Exception as e:
            logger.error(f"Database error in delete_ad: {e}")
            return False
    
    def add_complaint(self, message_id):
        """Добавить жалобу к объявлению"""
        try:
            conn = self.get_connection()
            cur = conn.cursor()
            cur.execute("""
                UPDATE ads SET complaints = complaints + 1 
                WHERE message_id = ?
            """, (message_id,))
            cur.execute("""
                SELECT complaints, user_id, topic_name FROM ads 
                WHERE message_id = ?
            """, (message_id,))
            result = cur.fetchone()
            conn.commit()
            conn.close()
            return result
        except Exception as e:
            logger.error(f"Database error in add_complaint: {e}")
            return None
    
    def delete_ad_by_message_id(self, message_id):
        """Удалить объявление по message_id"""
        try:
            conn = self.get_connection()
            cur = conn.cursor()
            cur.execute("SELECT user_id FROM ads WHERE message_id = ?", (message_id,))
            user_data = cur.fetchone()
            if user_data:
                cur.execute("DELETE FROM ads WHERE message_id = ?", (message_id,))
                cur.execute("""
                    UPDATE users SET ads_count = ads_count - 1 
                    WHERE user_id = ? AND ads_count > 0
                """, (user_data[0],))
            conn.commit()
            conn.close()
            return user_data[0] if user_data else None
        except Exception as e:
            logger.error(f"Database error in delete_ad_by_message_id: {e}")
            return None

class TelegramBot:
    def __init__(self):
        self.db = Database()
        self.app = Application.builder().token(BOT_TOKEN).build()
        self.user_states = {}
        
        # Тексты (упрощенная версия)
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
            }
        }
    
    async def get_group_topics(self):
        """Получить темы из группы"""
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
    
    def get_text(self, user_id, key):
        """Получить текст на языке пользователя"""
        user = self.db.get_user(user_id)
        lang = user[1] if user else 'ru'
        return self.texts.get(lang, self.texts['ru']).get(key, key)
    
    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда /start"""
        user_id = update.effective_user.id
        
        self.db.create_or_update_user(user_id)
        
        keyboard = [
            [InlineKeyboardButton("Русский", callback_data="lang_ru")]
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
            await self.show_categories(query, user_id)
            
        elif data == 'change_language':
            keyboard = [
                [InlineKeyboardButton("Русский", callback_data="lang_ru")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.edit_message_text(
                self.get_text(user_id, 'language'),
                reply_markup=reply_markup
            )
            
        elif data.startswith('topic_'):
            topic_id = int(data.split('_')[1])
            
            user_ads = self.db.get_user_ads(user_id)
            if len(user_ads) >= 3:
                await self.show_delete_ads(query, user_id, user_ads)
                return
            
            self.user_states[user_id] = {'topic_id': topic_id}
            
            await query.edit_message_text(
                self.get_text(user_id, 'create_ad')
            )
            
        elif data.startswith('delete_'):
            ad_id = int(data.split('_')[1])
            if self.db.delete_ad(ad_id, user_id):
                try:
                    user_ads = self.db.get_user_ads(user_id)
                    for ad in user_ads:
                        if ad[0] == ad_id:
                            await context.bot.delete_message(GROUP_ID, ad[1])
                            break
                except:
                    pass
                
                remaining_ads = self.db.get_user_ads(user_id)
                if len(remaining_ads) >= 3:
                    await self.show_delete_ads(query, user_id, remaining_ads)
                else:
                    await self.show_categories(query, user_id)
            
        elif data.startswith('complaint_'):
            message_id = int(data.split('_')[1])
            result = self.db.add_complaint(message_id)
            
            if result and result[0] >= 5:
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
                f"{topic_name} (ID: {ad_id})",
                callback_data=f"delete_{ad_id}"
            )])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(text, reply_markup=reply_markup)
    
    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработка текстовых сообщений"""
        user_id = update.effective_user.id
        text = update.message.text
        
        if user_id in self.user_states and 'topic_id' in self.user_states[user_id]:
            topic_id = self.user_states[user_id]['topic_id']
            
            topics = await self.get_group_topics()
            topic_name = next((t['name'] for t in topics if t['id'] == topic_id), 'Общение')
            
            keyboard = [
                [
                    InlineKeyboardButton(
                        self.get_text(user_id, 'complaint'),
                        callback_data=f"complaint_{0}"
                    ),
                    InlineKeyboardButton(
                        self.get_text(user_id, 'write'),
                        url=f"tg://user?id={user_id}"
                    )
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            try:
                message = await context.bot.send_message(
                    GROUP_ID,
                    text,
                    reply_markup=reply_markup,
                    message_thread_id=topic_id if topic_id > 1 else None
                )
                
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
                
                self.db.add_ad(user_id, message.message_id, topic_id, topic_name)
                
                await update.message.reply_text(self.get_text(user_id, 'ad_published'))
                
            except Exception as e:
                logger.error(f"Error publishing ad: {e}")
                await update.message.reply_text("Ошибка при публикации объявления")
            
            if user_id in self.user_states:
                del self.user_states[user_id]
    
    async def self_ping(self, context: ContextTypes.DEFAULT_TYPE):
        """Самопинг для предотвращения засыпания"""
        try:
            def ping_sync():
                try:
                    with urllib.request.urlopen(f"{WEBHOOK_URL}/", timeout=10) as response:
                        return response.status
                except Exception:
                    return None
            
            import concurrent.futures
            loop = asyncio.get_event_loop()
            with concurrent.futures.ThreadPoolExecutor() as executor:
                status = await loop.run_in_executor(executor, ping_sync)
                if status:
                    logger.info(f"Self-ping successful: {status}")
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
    
    async def run_webhook(self):
        """Запуск с webhook для Render"""
        await self.app.initialize()
        await self.app.start()
        
        webhook_url = f"{WEBHOOK_URL}/webhook"
        await self.app.bot.set_webhook(webhook_url)
        
        await self.app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            webhook_url=webhook_url,
            drop_pending_updates=True
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
