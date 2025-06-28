import asyncio
import logging
import os
import time
import threading
from collections import defaultdict, deque
from datetime import datetime, timedelta
from typing import Dict, Set
import aiohttp
from aiohttp import web
from telegram import Update, ChatPermissions
from telegram.ext import (
    Application, 
    CommandHandler, 
    MessageHandler, 
    filters, 
    ContextTypes,
    CallbackContext
)
from telegram.error import TelegramError
import gc
import weakref

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

class MemoryOptimizedUserTracker:
    """Оптимизированный трекер пользователей с управлением памятью"""
    
    def __init__(self, max_users=1000, cleanup_interval=3600):
        # Используем defaultdict для автоматического создания записей
        self.user_messages: Dict[int, Dict[int, deque]] = defaultdict(lambda: defaultdict(lambda: deque(maxlen=3)))
        self.user_restrictions: Dict[int, Set[int]] = defaultdict(set)
        self.last_activity: Dict[int, Dict[int, float]] = defaultdict(lambda: defaultdict(float))
        
        self.max_users = max_users
        self.cleanup_interval = cleanup_interval
        self.last_cleanup = time.time()
        
        # Слабые ссылки для автоматической очистки
        self._weak_refs = weakref.WeakSet()
    
    def add_message(self, chat_id: int, user_id: int, message_id: int) -> bool:
        """Добавляет сообщение и возвращает True если превышен лимит"""
        current_time = time.time()
        
        # Обновляем время активности
        self.last_activity[chat_id][user_id] = current_time
        
        # Добавляем сообщение в очередь (максимум 3)
        user_queue = self.user_messages[chat_id][user_id]
        user_queue.append(message_id)
        
        # Проверяем лимит
        if len(user_queue) > 3:
            return True
        
        # Периодическая очистка памяти
        if current_time - self.last_cleanup > self.cleanup_interval:
            self._cleanup_old_data()
        
        return False
    
    def remove_message(self, chat_id: int, user_id: int, message_id: int):
        """Удаляет сообщение из трекера"""
        if chat_id in self.user_messages and user_id in self.user_messages[chat_id]:
            user_queue = self.user_messages[chat_id][user_id]
            try:
                # Удаляем конкретное сообщение
                temp_list = list(user_queue)
                if message_id in temp_list:
                    temp_list.remove(message_id)
                    user_queue.clear()
                    user_queue.extend(temp_list)
                    
                    # Если сообщений стало <= 3, снимаем ограничение
                    if len(user_queue) <= 3 and user_id in self.user_restrictions[chat_id]:
                        self.user_restrictions[chat_id].discard(user_id)
                        return True  # Пользователь разблокирован
            except ValueError:
                pass
        return False
    
    def is_restricted(self, chat_id: int, user_id: int) -> bool:
        """Проверяет, ограничен ли пользователь"""
        return user_id in self.user_restrictions[chat_id]
    
    def restrict_user(self, chat_id: int, user_id: int):
        """Ограничивает пользователя"""
        self.user_restrictions[chat_id].add(user_id)
    
    def get_message_count(self, chat_id: int, user_id: int) -> int:
        """Получает количество сообщений пользователя"""
        return len(self.user_messages[chat_id][user_id])
    
    def _cleanup_old_data(self):
        """Очищает старые данные для экономии памяти"""
        current_time = time.time()
        cutoff_time = current_time - (24 * 3600)  # 24 часа
        
        chats_to_remove = []
        for chat_id in list(self.last_activity.keys()):
            users_to_remove = []
            for user_id, last_time in list(self.last_activity[chat_id].items()):
                if last_time < cutoff_time:
                    users_to_remove.append(user_id)
            
            # Удаляем неактивных пользователей
            for user_id in users_to_remove:
                self.last_activity[chat_id].pop(user_id, None)
                self.user_messages[chat_id].pop(user_id, None)
                self.user_restrictions[chat_id].discard(user_id)
            
            # Удаляем пустые чаты
            if not self.last_activity[chat_id]:
                chats_to_remove.append(chat_id)
        
        for chat_id in chats_to_remove:
            self.last_activity.pop(chat_id, None)
            self.user_messages.pop(chat_id, None)
            self.user_restrictions.pop(chat_id, None)
        
        self.last_cleanup = current_time
        
        # Принудительная сборка мусора
        gc.collect()
        
        logger.info(f"Очистка памяти завершена. Активных чатов: {len(self.last_activity)}")

class TelegramLimitBot:
    """Основной класс бота с системой лимитов"""
    
    def __init__(self, token: str, render_url: str = None):
        self.token = token
        self.render_url = render_url
        self.application = None
        self.tracker = MemoryOptimizedUserTracker()
        self.keep_alive_task = None
        
    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда /start"""
        await update.message.reply_text(
            "🤖 Бот-модератор активирован!\n\n"
            "📋 Правила:\n"
            "• Максимум 3 объявления на пользователя\n"
            "• При превышении лимита - автоблокировка\n"
            "• Удалите старые объявления для создания новых\n\n"
            "⚡ Бот работает автономно и не требует настройки."
        )
    
    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик всех сообщений"""
        if not update.message or not update.effective_chat:
            return
        
        chat_id = update.effective_chat.id
        user_id = update.effective_user.id
        message_id = update.message.message_id
        
        # Игнорируем сообщения от ботов
        if update.effective_user.is_bot:
            return
        
        # Игнорируем команды
        if update.message.text and update.message.text.startswith('/'):
            return
        
        # Проверяем, не ограничен ли уже пользователь
        if self.tracker.is_restricted(chat_id, user_id):
            try:
                await update.message.delete()
                # Отправляем предупреждение в личку, если возможно
                try:
                    await context.bot.send_message(
                        user_id,
                        "🚫 Лимит 3 объявления. Удалите старые - чтобы создать новые."
                    )
                except:
                    # Если не удалось отправить в личку, отправляем в чат и удаляем через 10 сек
                    warning_msg = await context.bot.send_message(
                        chat_id,
                        f"🚫 @{update.effective_user.username or update.effective_user.first_name}, "
                        f"лимит 3 объявления. Удалите старые - чтобы создать новые.",
                        reply_to_message_id=message_id
                    )
                    # Удаляем предупреждение через 10 секунд
                    asyncio.create_task(self._delete_message_later(context.bot, chat_id, warning_msg.message_id, 10))
            except TelegramError as e:
                logger.error(f"Ошибка при удалении сообщения: {e}")
            return
        
        # Добавляем сообщение в трекер
        if self.tracker.add_message(chat_id, user_id, message_id):
            # Лимит превышен - ограничиваем пользователя
            self.tracker.restrict_user(chat_id, user_id)
            
            try:
                # Удаляем сообщение, которое превысило лимит
                await update.message.delete()
                
                # Пытаемся ограничить права пользователя в чате
                try:
                    await context.bot.restrict_chat_member(
                        chat_id=chat_id,
                        user_id=user_id,
                        permissions=ChatPermissions(
                            can_send_messages=False,
                            can_send_audios=False,
                            can_send_documents=False,
                            can_send_photos=False,
                            can_send_videos=False,
                            can_send_video_notes=False,
                            can_send_voice_notes=False,
                            can_send_polls=False,
                            can_send_other_messages=False,
                            can_add_web_page_previews=False
                        )
                    )
                except TelegramError:
                    # Если не получилось ограничить права, просто логируем
                    logger.warning(f"Не удалось ограничить права пользователя {user_id} в чате {chat_id}")
                
                # Отправляем предупреждение
                try:
                    await context.bot.send_message(
                        user_id,
                        "🚫 Лимит 3 объявления превышен!\n"
                        "Удалите старые объявления, чтобы создать новые."
                    )
                except:
                    warning_msg = await context.bot.send_message(
                        chat_id,
                        f"🚫 @{update.effective_user.username or update.effective_user.first_name}, "
                        f"лимит 3 объявления превышен! Удалите старые - чтобы создать новые."
                    )
                    asyncio.create_task(self._delete_message_later(context.bot, chat_id, warning_msg.message_id, 15))
                    
            except TelegramError as e:
                logger.error(f"Ошибка при обработке превышения лимита: {e}")
    
    async def handle_message_deletion(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик удаления сообщений"""
        if not update.edited_message:
            return
            
        # Это довольно сложно отследить удаление через API
        # Можно использовать chat_member updates или channel_post updates
        pass
    
    async def _delete_message_later(self, bot, chat_id: int, message_id: int, delay: int):
        """Удаляет сообщение через заданное время"""
        await asyncio.sleep(delay)
        try:
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
        except TelegramError:
            pass  # Сообщение уже могло быть удалено
    
    async def status_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда для проверки статуса пользователя"""
        if not update.effective_chat or not update.effective_user:
            return
            
        chat_id = update.effective_chat.id
        user_id = update.effective_user.id
        
        message_count = self.tracker.get_message_count(chat_id, user_id)
        is_restricted = self.tracker.is_restricted(chat_id, user_id)
        
        status_text = f"📊 Ваш статус:\n"
        status_text += f"📝 Объявлений: {message_count}/3\n"
        status_text += f"🚫 Ограничен: {'Да' if is_restricted else 'Нет'}\n"
        
        if message_count >= 3:
            status_text += "\n⚠️ Лимит достигнут! Удалите старые объявления для создания новых."
        
        await update.message.reply_text(status_text)
    
    async def keep_alive(self):
        """Поддерживает бота активным, делая периодические запросы"""
        while True:
            try:
                if self.render_url:
                    async with aiohttp.ClientSession() as session:
                        async with session.get(self.render_url, timeout=30) as response:
                            logger.info(f"Keep-alive ping: {response.status}")
                
                # Также пингуем Telegram API
                if self.application and self.application.bot:
                    await self.application.bot.get_me()
                    
            except Exception as e:
                logger.error(f"Ошибка keep-alive: {e}")
            
            # Ждем 25 минут (Render засыпает через 30 минут неактивности)
            await asyncio.sleep(25 * 60)
    
    async def error_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик ошибок"""
        logger.error(f"Ошибка: {context.error}")
        
        if update and update.effective_message:
            try:
                await update.effective_message.reply_text(
                    "🔧 Произошла техническая ошибка. Бот продолжает работать."
                )
            except:
                pass

# Функция для веб-сервера (keep-alive endpoint)
async def create_web_server():
    """Создает простой веб-сервер для keep-alive"""
    
    async def health_check(request):
        return web.Response(text="Bot is alive!", status=200)
    
    app = web.Application()
    app.router.add_get('/', health_check)
    app.router.add_get('/health', health_check)
    
    return app

async def main():
    """Основная функция"""
    # Получаем токен из переменных окружения
    TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
    RENDER_URL = os.getenv('RENDER_URL', 'https://your-app.onrender.com')
    PORT = int(os.getenv('PORT', 8000))
    
    if not TOKEN:
        logger.error("❌ Не указан TELEGRAM_BOT_TOKEN в переменных окружения!")
        return
    
    # Создаем бота
    bot = TelegramLimitBot(TOKEN, RENDER_URL)
    
    # Запускаем веб-сервер для keep-alive
    web_app = await create_web_server()
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    
    logger.info(f"🌐 Веб-сервер запущен на порту {PORT}")
    
    # Запускаем бота
    try:
        # Создаем приложение
        bot.application = Application.builder().token(TOKEN).build()
        
        # Добавляем обработчики
        bot.application.add_handler(CommandHandler("start", bot.start_command))
        bot.application.add_handler(CommandHandler("status", bot.status_command))
        bot.application.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, bot.handle_message)
        )
        bot.application.add_handler(
            MessageHandler(
                filters.PHOTO | filters.VIDEO | filters.Document.ALL | 
                filters.AUDIO | filters.VOICE | filters.VIDEO_NOTE | filters.STICKER, 
                bot.handle_message
            )
        )
        
        # Обработчик ошибок
        bot.application.add_error_handler(bot.error_handler)
        
        # Запускаем keep-alive в фоне
        bot.keep_alive_task = asyncio.create_task(bot.keep_alive())
        
        logger.info("🚀 Запуск бота...")
        logger.info("✅ Бот успешно запущен!")
        
        # Запускаем бота (блокирующий вызов)
        await bot.application.run_polling(
            drop_pending_updates=True,
            allowed_updates=['message', 'edited_message', 'channel_post', 'edited_channel_post']
        )
        
    except KeyboardInterrupt:
        logger.info("🛑 Остановка бота...")
    except Exception as e:
        logger.error(f"💥 Критическая ошибка: {e}")
        import traceback
        logger.error(traceback.format_exc())
    finally:
        if bot.keep_alive_task:
            bot.keep_alive_task.cancel()
        await runner.cleanup()

if __name__ == '__main__':
    asyncio.run(main())
