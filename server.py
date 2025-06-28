import os
import asyncio
import logging
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
GROUP_ID = os.getenv('GROUP_ID')  # ID группы где публиковать
WEBHOOK_URL = os.getenv('WEBHOOK_URL')  # URL для webhook
PORT = int(os.getenv('PORT', 10000))  # Render использует порт 10000 по умолчанию

class MemoryDatabase:
    def __init__(self):
        # Хранение данных в памяти
        self.users = {}  # {user_id: {'language': 'ru', 'ads_count': 0}}
        self.ads = {}    # {ad_id: {'user_id': int, 'message_id': int, 'topic_id': int, 'topic_name': str, 'complaints': 0, 'created_at': datetime}}
        self.ad_counter = 1
        logger.info("Memory database initialized successfully")
    
    def get_user(self, user_id):
        """Получить данные пользователя"""
        user_data = self.users.get(user_id)
        if user_data:
            return (user_id, user_data['language'], user_data['ads_count'], user_data.get('created_at'))
        return None
    
    def create_or_update_user(self, user_id, language='ru'):
        """Создать или обновить пользователя"""
        if user_id not in self.users:
            self.users[user_id] = {
                'language': language,
                'ads_count': 0,
                'created_at': datetime.now()
            }
            logger.info(f"Created new user: {user_id}")
        else:
            self.users[user_id]['language'] = language
            logger.info(f"Updated user language: {user_id} -> {language}")
    
    def get_user_ads(self, user_id):
        """Получить объявления пользователя"""
        user_ads = []
        for ad_id, ad_data in self.ads.items():
            if ad_data['user_id'] == user_id:
                user_ads.append((
                    ad_id,
                    ad_data['message_id'],
                    ad_data['topic_id'],
                    ad_data['topic_name'],
                    ad_data['created_at']
                ))
        # Сортировка по дате создания (новые первые)
        user_ads.sort(key=lambda x: x[4], reverse=True)
        return user_ads
    
    def add_ad(self, user_id, message_id, topic_id, topic_name):
        """Добавить объявление"""
        ad_id = self.ad_counter
        self.ad_counter += 1
        
        self.ads[ad_id] = {
            'user_id': user_id,
            'message_id': message_id,
            'topic_id': topic_id,
            'topic_name': topic_name,
            'complaints': 0,
            'created_at': datetime.now()
        }
        
        # Увеличить счетчик объявлений пользователя
        if user_id in self.users:
            self.users[user_id]['ads_count'] += 1
        
        logger.info(f"Added ad {ad_id} for user {user_id}")
    
    def delete_ad(self, ad_id, user_id):
        """Удалить объявление"""
        if ad_id in self.ads and self.ads[ad_id]['user_id'] == user_id:
            del self.ads[ad_id]
            # Уменьшить счетчик объявлений пользователя
            if user_id in self.users and self.users[user_id]['ads_count'] > 0:
                self.users[user_id]['ads_count'] -= 1
            logger.info(f"Deleted ad {ad_id} for user {user_id}")
            return True
        return False
    
    def add_complaint(self, message_id):
        """Добавить жалобу к объявлению"""
        for ad_id, ad_data in self.ads.items():
            if ad_data['message_id'] == message_id:
                ad_data['complaints'] += 1
                logger.info(f"Added complaint to ad {ad_id}, total: {ad_data['complaints']}")
                return (ad_data['complaints'], ad_data['user_id'], ad_data['topic_name'])
        return None
    
    def delete_ad_by_message_id(self, message_id):
        """Удалить объявление по message_id"""
        for ad_id, ad_data in self.ads.items():
            if ad_data['message_id'] == message_id:
                user_id = ad_data['user_id']
                del self.ads[ad_id]
                # Уменьшить счетчик объявлений пользователя
                if user_id in self.users and self.users[user_id]['ads_count'] > 0:
                    self.users[user_id]['ads_count'] -= 1
                logger.info(f"Deleted ad {ad_id} by message_id {message_id}")
                return user_id
        return None

class TelegramBot:
    def __init__(self):
        self.db = MemoryDatabase()
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
        logger.info(f"User {user_id} sent /start command")
        
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
        logger.info(f"Sent language selection to user {user_id}")
    
    async def button_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработка нажатий кнопок"""
        query = update.callback_query
        user_id = query.from_user.id
        data = query.data
        
        logger.info(f"User {user_id} pressed button: {data}")
        
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
                except Exception as e:
                    logger.error(f"Error deleting message from group: {e}")
                
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
                except Exception as e:
                    logger.error(f"Error processing complaint: {e}")
    
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
        
        logger.info(f"User {user_id} sent message: {text[:50]}...")
        
        if user_id in self.user_states and 'topic_id' in self.user_states[user_id]:
            # Это объявление
            topic_id = self.user_states[user_id]['topic_id']
            
            # Получить название темы
            topics = await self.get_group_topics()
            topic_name = next((t['name'] for t in topics if t['id'] == topic_id), 'Общение')
            
            try:
                # Сначала публикуем без кнопок
                message = await context.bot.send_message(
                    GROUP_ID,
                    text,
                    message_thread_id=topic_id if topic_id > 1 else None
                )
                
                # Теперь добавляем кнопки с правильным message_id
                keyboard = [
                    [
                        InlineKeyboardButton(
                            self.get_text(user_id, 'complaint'),
                            callback_data=f"complaint_{message.message_id}"
                        ),
                        InlineKeyboardButton(
                            self.get_text(user_id, 'write'),
                            url=f"tg://user?id={user_id}"
                        )
                    ]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                await context.bot.edit_message_reply_markup(
                    GROUP_ID,
                    message.message_id,
                    reply_markup=reply_markup
                )
                
                # Сохранить в базу
                self.db.add_ad(user_id, message.message_id, topic_id, topic_name)
                
                await update.message.reply_text(self.get_text(user_id, 'ad_published'))
                logger.info(f"Published ad for user {user_id} in topic {topic_name}")
                
            except Exception as e:
                logger.error(f"Error publishing ad: {e}")
                await update.message.reply_text("Ошибка при публикации объявления")
            
            # Очистить состояние
            if user_id in self.user_states:
                del self.user_states[user_id]
    
    async def webhook_handler(self, request):
        """Обработчик webhook запросов"""
        try:
            # Получаем JSON из запроса
            json_data = await request.json()
            logger.info(f"Received webhook: {json_data}")
            
            # Создаем Update объект
            update = Update.de_json(json_data, self.app.bot)
            
            # Обрабатываем update
            await self.app.process_update(update)
            
            return {"status": "ok"}
        except Exception as e:
            logger.error(f"Webhook error: {e}")
            return {"status": "error", "message": str(e)}
    
    async def self_ping_loop(self):
        """Бесконечный цикл самопинга для предотвращения засыпания"""
        while True:
            try:
                await asyncio.sleep(1500)  # 25 минут
                
                def ping_sync():
                    try:
                        with urllib.request.urlopen(f"{WEBHOOK_URL}/health", timeout=10) as response:
                            return response.status
                    except Exception:
                        return None
                
                # Выполняем синхронный запрос в отдельном потоке
                import concurrent.futures
                loop = asyncio.get_event_loop()
                with concurrent.futures.ThreadPoolExecutor() as executor:
                    status = await loop.run_in_executor(executor, ping_sync)
                    if status:
                        logger.info(f"🔄 Self-ping successful: {status}")
                    else:
                        logger.info("🔄 Self-ping executed (no status)")
            except Exception as e:
                logger.error(f"Self-ping error: {e}")
                await asyncio.sleep(60)  # При ошибке ждем минуту и пробуем снова
    
    def setup_handlers(self):
        """Настройка обработчиков"""
        self.app.add_handler(CommandHandler("start", self.start))
        self.app.add_handler(CallbackQueryHandler(self.button_callback))
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))
        
        logger.info("Handlers configured successfully")
    
    async def run_webhook(self):
        """Запуск с webhook для Render"""
        try:
            await self.app.initialize()
            await self.app.start()
            
            # Установка webhook
            webhook_url = f"{WEBHOOK_URL}/webhook"
            await self.app.bot.set_webhook(webhook_url)
            logger.info(f"🌐 Webhook set to: {webhook_url}")
            
            # Запуск самопинга в отдельной задаче
            if WEBHOOK_URL:
                asyncio.create_task(self.self_ping_loop())
                logger.info("🔄 Self-ping task started")
            
            # Запуск webhook сервера на правильном порту
            await self.app.run_webhook(
                listen="0.0.0.0",
                port=PORT,
                url_path="/webhook",
                webhook_url=webhook_url,
                drop_pending_updates=True
            )
        except Exception as e:
            logger.error(f"Webhook startup error: {e}")
            raise
    
    async def run_polling(self):
        """Запуск с polling для разработки"""
        logger.info("Starting polling mode...")
        await self.app.run_polling(drop_pending_updates=True)

async def main():
    if not BOT_TOKEN:
        logger.error("❌ BOT_TOKEN не задан!")
        return
    
    if not GROUP_ID:
        logger.error("❌ GROUP_ID не задан!")
        return
    
    logger.info("🚀 Запуск Telegram бота...")
    logger.info(f"BOT_TOKEN: {'*' * 20}{BOT_TOKEN[-10:] if BOT_TOKEN else 'None'}")
    logger.info(f"GROUP_ID: {GROUP_ID}")
    logger.info(f"WEBHOOK_URL: {WEBHOOK_URL}")
    logger.info(f"PORT: {PORT}")
    
    bot = TelegramBot()
    bot.setup_handlers()
    
    # Проверим что бот работает
    try:
        async with bot.app:
            bot_info = await bot.app.bot.get_me()
            logger.info(f"✅ Bot connected: @{bot_info.username}")
    except Exception as e:
        logger.error(f"❌ Bot connection failed: {e}")
        return
    
    if WEBHOOK_URL:
        logger.info("🌐 Режим: Webhook (для продакшена)")
        await bot.run_webhook()
    else:
        logger.info("🔄 Режим: Polling (для разработки)")
        await bot.run_polling()

if __name__ == "__main__":
    asyncio.run(main())
