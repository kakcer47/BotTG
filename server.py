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
from telegram import Update, ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, 
    CommandHandler, 
    MessageHandler, 
    CallbackQueryHandler,
    ChatMemberHandler,
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
        # Добавляем множество для отслеживания пользователей, принявших правила
        self.accepted_users: Dict[int, Set[int]] = defaultdict(set)
        
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
        if not update.effective_chat or not update.effective_user:
            return
            
        chat_id = update.effective_chat.id
        user_id = update.effective_user.id
        
        # Если пользователь еще не принял правила, показываем их
        if user_id not in self.tracker.accepted_users[chat_id]:
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Принять правила", callback_data=f"accept_rules_{user_id}")
            ]])
            
            await update.message.reply_text(
                "👋 Добро пожаловать!\n\n"
                "📋 **Правила чата:**\n"
                "• Максимум 3 объявления на пользователя\n"
                "• При превышении лимита - автоблокировка\n"
                "• Удалите старые объявления для создания новых\n"
                "• Запрещен спам и реклама\n"
                "• Будьте вежливы и уважайте других участников\n\n"
                "🔒 Для участия в чате нажмите кнопку ниже:",
                reply_markup=keyboard,
                parse_mode='Markdown'
            )
        else:
            # Пользователь уже принял правила
            message_count = self.tracker.get_message_count(chat_id, user_id)
            await update.message.reply_text(
                "🤖 Бот-модератор активен!\n\n"
                f"📊 Ваш статус: {message_count}/3 объявлений\n\n"
                "📋 Правила:\n"
                "• Максимум 3 объявления на пользователя\n"
                "• При превышении лимита - автоблокировка\n"
                "• Удалите старые объявления для создания новых\n\n"
                "💡 Команды: /status - проверить статус"
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
        
        # Проверяем, принял ли пользователь правила
        if user_id not in self.tracker.accepted_users[chat_id]:
            try:
                await update.message.delete()
                # Отправляем напоминание о правилах
                keyboard = InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Принять правила", callback_data=f"accept_rules_{user_id}")
                ]])
                
                reminder_msg = await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"⚠️ {update.effective_user.first_name}, сначала примите правила чата!",
                    reply_markup=keyboard
                )
                
                # Удаляем напоминание через 30 секунд
                asyncio.create_task(self._delete_message_later(context.bot, chat_id, reminder_msg.message_id, 30))
                
            except Exception as e:
                logger.error(f"Ошибка при обработке сообщения от непринявшего правила: {e}")
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
    
    async def handle_new_member(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик новых участников чата"""
        if not update.chat_member or not update.effective_chat:
            return
            
        new_member = update.chat_member.new_chat_member
        chat_id = update.effective_chat.id
        user_id = new_member.user.id
        
        # Игнорируем ботов
        if new_member.user.is_bot:
            return
            
        # Проверяем, что пользователь действительно присоединился
        if (update.chat_member.old_chat_member.status in ['left', 'kicked'] and 
            new_member.status in ['member', 'restricted']):
            
            try:
                # Сразу блокируем нового участника
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
                
                # Создаем кнопку принятия правил
                keyboard = InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Принять правила", callback_data=f"accept_rules_{user_id}")
                ]])
                
                # Отправляем правила
                rules_text = (
                    f"👋 Добро пожаловать, {new_member.user.first_name}!\n\n"
                    f"📋 **Правила чата:**\n"
                    f"• Максимум 3 объявления на пользователя\n"
                    f"• При превышении лимита - автоблокировка\n"
                    f"• Удалите старые объявления для создания новых\n"
                    f"• Запрещен спам и реклама\n"
                    f"• Будьте вежливы и уважайте других участников\n\n"
                    f"🔒 Для участия в чате нажмите кнопку ниже:"
                )
                
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=rules_text,
                    reply_markup=keyboard,
                    parse_mode='Markdown'
                )
                
                logger.info(f"Новый участник {user_id} заблокирован, отправлены правила")
                
            except Exception as e:
                logger.error(f"Ошибка при обработке нового участника: {e}")
    
    async def handle_accept_rules(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик принятия правил"""
        if not update.callback_query:
            return
            
        query = update.callback_query
        chat_id = update.effective_chat.id
        
        # Парсим callback_data
        if not query.data.startswith("accept_rules_"):
            return
            
        target_user_id = int(query.data.split("_")[-1])
        current_user_id = update.effective_user.id
        
        # Проверяем, что кнопку нажал тот же пользователь
        if current_user_id != target_user_id:
            await query.answer("❌ Вы не можете принять правила за другого пользователя!", show_alert=True)
            return
        
        try:
            # Разблокируем пользователя
            await context.bot.restrict_chat_member(
                chat_id=chat_id,
                user_id=target_user_id,
                permissions=ChatPermissions(
                    can_send_messages=True,
                    can_send_audios=True,
                    can_send_documents=True,
                    can_send_photos=True,
                    can_send_videos=True,
                    can_send_video_notes=True,
                    can_send_voice_notes=True,
                    can_send_polls=True,
                    can_send_other_messages=True,
                    can_add_web_page_previews=True
                )
            )
            
            # Добавляем в список принявших правила
            self.tracker.accepted_users[chat_id].add(target_user_id)
            
            # Обновляем сообщение
            await query.edit_message_text(
                text=f"✅ Правила приняты!\n\n"
                     f"🎉 Добро пожаловать в чат, {update.effective_user.first_name}!\n"
                     f"📊 Вам доступно 3 объявления. Используйте их разумно.\n\n"
                     f"💡 Команды:\n"
                     f"/status - проверить количество объявлений",
                parse_mode='Markdown'
            )
            
            await query.answer("🎉 Добро пожаловать в чат!")
            
            logger.info(f"Пользователь {target_user_id} принял правила и разблокирован")
            
        except Exception as e:
            logger.error(f"Ошибка при принятии правил: {e}")
            await query.answer("❌ Произошла ошибка. Обратитесь к администратору.", show_alert=True)
    
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
        
        # Проверяем, принял ли пользователь правила
        if user_id not in self.tracker.accepted_users[chat_id]:
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Принять правила", callback_data=f"accept_rules_{user_id}")
            ]])
            
            await update.message.reply_text(
                "⚠️ Сначала примите правила чата для получения статуса!",
                reply_markup=keyboard
            )
            return
        
        message_count = self.tracker.get_message_count(chat_id, user_id)
        is_restricted = self.tracker.is_restricted(chat_id, user_id)
        
        status_text = f"📊 Ваш статус:\n"
        status_text += f"📝 Объявлений: {message_count}/3\n"
        status_text += f"🚫 Ограничен: {'Да' if is_restricted else 'Нет'}\n"
        status_text += f"✅ Правила приняты: Да\n"
        
        if message_count >= 3:
            status_text += "\n⚠️ Лимит достигнут! Удалите старые объявления для создания новых."
        elif is_restricted:
            status_text += "\n🔒 Вы ограничены. Удалите старые объявления для разблокировки."
        else:
            remaining = 3 - message_count
            status_text += f"\n✨ Осталось {remaining} объявлений"
        
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

# Функция для веб-сервера (keep-alive + webhook endpoint)
async def create_web_server(bot_application):
    """Создает веб-сервер с webhook endpoint"""
    
    async def health_check(request):
        return web.Response(text="Bot is alive!", status=200)
    
    async def webhook_handler(request):
        """Обработчик webhook от Telegram"""
        try:
            # Получаем JSON данные от Telegram
            json_data = await request.json()
            
            # Создаем Update объект и обрабатываем его
            update = Update.de_json(json_data, bot_application.bot)
            if update:
                await bot_application.process_update(update)
            
            return web.Response(status=200)
        except Exception as e:
            logger.error(f"Ошибка в webhook: {e}")
            return web.Response(status=500)
    
    app = web.Application()
    app.router.add_get('/', health_check)
    app.router.add_get('/health', health_check)
    app.router.add_post('/webhook', webhook_handler)  # Webhook endpoint
    
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
    
    try:
        # Создаем приложение
        bot.application = Application.builder().token(TOKEN).build()
        
        # Добавляем обработчики
        bot.application.add_handler(CommandHandler("start", bot.start_command))
        bot.application.add_handler(CommandHandler("status", bot.status_command))
        
        # Обработчик новых участников
        bot.application.add_handler(ChatMemberHandler(bot.handle_new_member, ChatMemberHandler.CHAT_MEMBER))
        
        # Обработчик кнопок
        bot.application.add_handler(CallbackQueryHandler(bot.handle_accept_rules, pattern="accept_rules_"))
        
        # Обработчики сообщений
        bot.application.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, bot.handle_message)
        )
        bot.application.add_handler(
            MessageHandler(
                filters.PHOTO | filters.VIDEO | filters.Document.ALL | 
                filters.AUDIO | filters.VOICE | filters.VIDEO_NOTE | filters.Sticker.ALL, 
                bot.handle_message
            )
        )
        
        # Обработчик ошибок
        bot.application.add_error_handler(bot.error_handler)
        
        # Инициализируем приложение
        await bot.application.initialize()
        
        # Создаем веб-сервер с webhook
        web_app = await create_web_server(bot.application)
        runner = web.AppRunner(web_app)
        await runner.setup()
        site = web.TCPSite(runner, '0.0.0.0', PORT)
        await site.start()
        
        logger.info(f"🌐 Веб-сервер запущен на порту {PORT}")
        
        # Настраиваем webhook URL
        webhook_url = f"{RENDER_URL}/webhook"
        await bot.application.bot.set_webhook(
            url=webhook_url,
            drop_pending_updates=True,
            allowed_updates=['message', 'edited_message', 'channel_post', 'edited_channel_post', 'chat_member', 'callback_query']
        )
        
        logger.info(f"🔗 Webhook установлен: {webhook_url}")
        
        # Запускаем keep-alive в фоне
        bot.keep_alive_task = asyncio.create_task(bot.keep_alive())
        
        logger.info("🚀 Бот успешно запущен с webhook!")
        logger.info("✅ Ожидание обновлений через webhook...")
        
        # Бесконечный цикл для поддержания работы сервера
        while True:
            await asyncio.sleep(3600)  # Спим час, веб-сервер работает в фоне
        
    except KeyboardInterrupt:
        logger.info("🛑 Остановка бота...")
    except Exception as e:
        logger.error(f"💥 Критическая ошибка: {e}")
        import traceback
        logger.error(traceback.format_exc())
    finally:
        try:
            # Удаляем webhook при завершении
            if bot.application:
                await bot.application.bot.delete_webhook()
                await bot.application.shutdown()
        except:
            pass
        
        if bot.keep_alive_task:
            bot.keep_alive_task.cancel()
        
        try:
            await runner.cleanup()
        except:
            pass

if __name__ == '__main__':
    asyncio.run(main())
