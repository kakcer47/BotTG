#!/usr/bin/env python3
"""
server.py - Telegram Web App Server
Объединенный сервер для WebSocket, HTTP и Telegram Bot
Автор: Assistant 
Версия: 1.0
"""

import asyncio
import logging
import os
import json
from datetime import datetime, timedelta
from typing import Optional, List, Dict
import asyncpg
import websockets
from websockets.server import WebSocketServerProtocol
from collections import defaultdict
from contextlib import asynccontextmanager
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
import aiohttp
from dataclasses import dataclass

# Конфигурация
@dataclass
class Config:
    DATABASE_URL: str = os.getenv("DATABASE_URL")
    BOT_TOKEN: str = os.getenv("BOT_TOKEN")
    MODERATION_CHAT_ID: int = int(os.getenv("MODERATION_CHAT_ID", "0"))
    PORT: int = int(os.getenv("PORT", "10000"))
    DAILY_POST_LIMIT: int = 60
    DB_MIN_SIZE: int = 1
    DB_MAX_SIZE: int = 3
    DB_COMMAND_TIMEOUT: int = 30

config = Config()

# Логирование
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Глобальные переменные
db_pool = None
telegram_bot = None
connected_clients = set()
post_limits = defaultdict(list)  # Кеш лимитов в памяти
posts_cache = {}  # Кеш постов в памяти
user_cache = {}   # Кеш пользователей в памяти

# База данных
@asynccontextmanager
async def get_db_connection():
    async with db_pool.acquire() as connection:
        try:
            yield connection
        except Exception as e:
            logger.error(f"Database error: {e}")
            raise

class DatabaseService:
    @staticmethod
    async def init_database():
        global db_pool
        if not config.DATABASE_URL:
            raise ValueError("DATABASE_URL not set")
        
        db_pool = await asyncpg.create_pool(
            config.DATABASE_URL,
            min_size=config.DB_MIN_SIZE,
            max_size=config.DB_MAX_SIZE,
            command_timeout=config.DB_COMMAND_TIMEOUT
        )
        
        async with get_db_connection() as conn:
            # Создаем таблицы если не существуют
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS posts (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    description TEXT NOT NULL,
                    category TEXT NOT NULL,
                    tags JSONB NOT NULL DEFAULT '[]',
                    likes INTEGER DEFAULT 0,
                    status TEXT DEFAULT 'pending',
                    moderation_message_id INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    creator JSONB NOT NULL
                )
            """)
            
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS post_reports (
                    id SERIAL PRIMARY KEY,
                    post_id INTEGER REFERENCES posts(id),
                    reporter_id BIGINT NOT NULL,
                    reason TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    photo_url TEXT,
                    favorites BIGINT[] DEFAULT '{}',
                    hidden BIGINT[] DEFAULT '{}',
                    liked BIGINT[] DEFAULT '{}',
                    posts BIGINT[] DEFAULT '{}',
                    is_banned BOOLEAN DEFAULT FALSE,
                    ban_reason TEXT,
                    post_limit INTEGER DEFAULT 60,
                    last_post_count_reset DATE DEFAULT CURRENT_DATE,
                    posts_today INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # Индексы
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_posts_user_id ON posts(user_id)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_posts_category ON posts(category)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_posts_status ON posts(status)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_posts_created_at ON posts(created_at DESC)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_users_user_id ON users(user_id)")
        
        logger.info("Database initialized")

    @staticmethod
    async def sync_user(user_data: Dict) -> Dict:
        async with get_db_connection() as conn:
            # Сброс счетчика постов если нужно
            await conn.execute("""
                UPDATE users 
                SET posts_today = 0, last_post_count_reset = CURRENT_DATE
                WHERE user_id = $1 AND last_post_count_reset < CURRENT_DATE
            """, user_data['user_id'])
            
            user = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", user_data['user_id'])
            
            if not user:
                await conn.execute("""
                    INSERT INTO users (user_id, username, first_name, last_name, photo_url)
                    VALUES ($1, $2, $3, $4, $5)
                """, user_data['user_id'], user_data['username'], user_data['first_name'],
                    user_data['last_name'], user_data['photo_url'])
                user = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", user_data['user_id'])
            else:
                # Обновляем информацию
                await conn.execute("""
                    UPDATE users SET username = $2, first_name = $3, last_name = $4, photo_url = $5
                    WHERE user_id = $1
                """, user_data['user_id'], user_data['username'], user_data['first_name'],
                    user_data['last_name'], user_data['photo_url'])
            
            # Кешируем пользователя
            user_dict = dict(user)
            user_cache[user_data['user_id']] = user_dict
            return user_dict

    @staticmethod
    async def create_post(post_data: Dict) -> Dict:
        async with get_db_connection() as conn:
            post_id = await conn.fetchval("""
                INSERT INTO posts (user_id, description, category, tags, creator, status)
                VALUES ($1, $2, $3, $4, $5, 'pending') RETURNING id
            """, post_data['user_id'], post_data['description'], post_data['category'],
                json.dumps(post_data['tags']), json.dumps(post_data['creator']))
            
            # Увеличиваем счетчик постов пользователя
            await conn.execute("""
                UPDATE users SET posts_today = posts_today + 1 WHERE user_id = $1
            """, post_data['user_id'])
            
            post = await conn.fetchrow("SELECT * FROM posts WHERE id = $1", post_id)
            post_dict = dict(post)
            
            # Кешируем пост
            posts_cache[post_id] = post_dict
            return post_dict

    @staticmethod
    async def get_posts(filters: Dict, page: int, limit: int, search: str = '') -> List[Dict]:
        async with get_db_connection() as conn:
            query = """
                SELECT * FROM posts
                WHERE status = 'approved'
                AND category = $1
            """
            params = [filters.get('category', '')]
            param_count = 1
            
            # Поиск
            if search:
                param_count += 1
                query += f" AND LOWER(description) LIKE LOWER($${param_count})"
                params.append(f"%{search}%")
            
            # Фильтры по тегам
            if filters.get('filters'):
                for filter_type, values in filters['filters'].items():
                    if values and filter_type != 'sort' and isinstance(values, list):
                        for value in values:
                            param_count += 1
                            query += f" AND tags @> $${param_count}"
                            params.append(json.dumps([f"{filter_type}:{value}"]))
            
            # Сортировка
            sort_type = filters.get('filters', {}).get('sort', 'new')
            if sort_type == 'old':
                query += " ORDER BY created_at ASC"
            elif sort_type == 'rating':
                query += " ORDER BY likes DESC, created_at DESC"
            else:
                query += " ORDER BY created_at DESC"
            
            query += f" LIMIT {limit} OFFSET {(page - 1) * limit}"
            
            posts = await conn.fetch(query, *params)
            result = [dict(post) for post in posts]
            
            # Кешируем полученные посты
            for post in result:
                posts_cache[post['id']] = post
            
            return result

    @staticmethod
    async def approve_post(post_id: int) -> Optional[Dict]:
        async with get_db_connection() as conn:
            await conn.execute("UPDATE posts SET status = 'approved' WHERE id = $1", post_id)
            post = await conn.fetchrow("SELECT * FROM posts WHERE id = $1", post_id)
            if post:
                post_dict = dict(post)
                posts_cache[post_id] = post_dict
                return post_dict
            return None

    @staticmethod
    async def reject_post(post_id: int) -> Optional[Dict]:
        async with get_db_connection() as conn:
            await conn.execute("UPDATE posts SET status = 'rejected' WHERE id = $1", post_id)
            post = await conn.fetchrow("SELECT * FROM posts WHERE id = $1", post_id)
            if post:
                # Удаляем из кеша
                posts_cache.pop(post_id, None)
                return dict(post)
            return None

    @staticmethod
    async def delete_post(post_id: int, user_id: int = None) -> bool:
        async with get_db_connection() as conn:
            if user_id:
                result = await conn.execute("DELETE FROM posts WHERE id = $1 AND user_id = $2", post_id, user_id)
            else:
                result = await conn.execute("DELETE FROM posts WHERE id = $1", post_id)
            
            # Удаляем из кеша
            posts_cache.pop(post_id, None)
            return result.split()[-1] == '1'

    @staticmethod
    async def like_post(post_id: int) -> Optional[Dict]:
        async with get_db_connection() as conn:
            await conn.execute("UPDATE posts SET likes = likes + 1 WHERE id = $1 AND status = 'approved'", post_id)
            post = await conn.fetchrow("SELECT * FROM posts WHERE id = $1", post_id)
            if post:
                post_dict = dict(post)
                posts_cache[post_id] = post_dict
                return post_dict
            return None

    @staticmethod
    async def report_post(post_id: int, reporter_id: int, reason: str = None) -> bool:
        async with get_db_connection() as conn:
            await conn.execute("""
                INSERT INTO post_reports (post_id, reporter_id, reason) VALUES ($1, $2, $3)
            """, post_id, reporter_id, reason)
            return True

    @staticmethod
    async def get_post_by_id(post_id: int) -> Optional[Dict]:
        # Сначала проверяем кеш
        if post_id in posts_cache:
            return posts_cache[post_id]
        
        async with get_db_connection() as conn:
            post = await conn.fetchrow("SELECT * FROM posts WHERE id = $1", post_id)
            if post:
                post_dict = dict(post)
                posts_cache[post_id] = post_dict
                return post_dict
            return None

    @staticmethod
    async def is_user_banned(user_id: int) -> bool:
        # Проверяем кеш
        if user_id in user_cache:
            return user_cache[user_id].get('is_banned', False)
        
        async with get_db_connection() as conn:
            result = await conn.fetchval("SELECT is_banned FROM users WHERE user_id = $1", user_id)
            return result or False

    @staticmethod
    async def get_user_posts_today(user_id: int) -> int:
        # Проверяем кеш
        if user_id in user_cache:
            return user_cache[user_id].get('posts_today', 0)
        
        async with get_db_connection() as conn:
            result = await conn.fetchval("SELECT posts_today FROM users WHERE user_id = $1", user_id)
            return result or 0

    @staticmethod
    async def get_user_limit(user_id: int) -> int:
        # Проверяем кеш
        if user_id in user_cache:
            return user_cache[user_id].get('post_limit', config.DAILY_POST_LIMIT)
        
        async with get_db_connection() as conn:
            result = await conn.fetchval("SELECT post_limit FROM users WHERE user_id = $1", user_id)
            return result or config.DAILY_POST_LIMIT

# Система лимитов (в памяти)
class PostLimitService:
    @staticmethod
    async def check_user_limit(user_id: int) -> bool:
        posts_today = await DatabaseService.get_user_posts_today(user_id)
        limit = await DatabaseService.get_user_limit(user_id)
        return posts_today < limit

# Telegram Bot
class ModerationBot:
    def __init__(self):
        self.app = None

    async def init_bot(self):
        if not config.BOT_TOKEN:
            raise ValueError("BOT_TOKEN not set")
        
        self.app = Application.builder().token(config.BOT_TOKEN).build()
        
        # Команды
        self.app.add_handler(CommandHandler("start", self.start_command))
        self.app.add_handler(CommandHandler("delete", self.delete_command))
        self.app.add_handler(CallbackQueryHandler(self.handle_moderation_callback))
        
        await self.app.initialize()
        await self.app.start()
        
        global telegram_bot
        telegram_bot = self.app.bot
        
        logger.info("Moderation bot initialized")

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "🤖 Бот модерации объявлений\n\n"
            "Команды:\n"
            "/delete <post_id> - Удалить объявление"
        )

    async def delete_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args:
            await update.message.reply_text("Использование: /delete <post_id>")
            return
        
        try:
            post_id = int(context.args[0])
            post = await DatabaseService.get_post_by_id(post_id)
            
            if not post:
                await update.message.reply_text("Объявление не найдено")
                return
            
            success = await DatabaseService.delete_post(post_id)
            if success:
                await broadcast_message({
                    'type': 'post_deleted',
                    'post_id': post_id
                })
                
                try:
                    creator = json.loads(post['creator']) if isinstance(post['creator'], str) else post['creator']
                    await telegram_bot.send_message(
                        chat_id=creator['user_id'],
                        text="🗑 Ваше объявление было удалено модератором"
                    )
                except Exception as e:
                    logger.error(f"Failed to notify user: {e}")
                
                await update.message.reply_text(f"✅ Объявление {post_id} удалено")
            else:
                await update.message.reply_text("❌ Ошибка при удалении объявления")
                
        except ValueError:
            await update.message.reply_text("❌ Неверный ID объявления")
        except Exception as e:
            logger.error(f"Delete command error: {e}")
            await update.message.reply_text("❌ Произошла ошибка")

    async def handle_moderation_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        
        data = query.data.split("_")
        action = data[0]
        post_id = int(data[1])
        
        post = await DatabaseService.get_post_by_id(post_id)
        if not post:
            await query.edit_message_text("❌ Объявление не найдено")
            return
        
        if action == "approve":
            approved_post = await DatabaseService.approve_post(post_id)
            if approved_post:
                await broadcast_message({
                    'type': 'post_updated',
                    'post': approved_post
                })
                await query.edit_message_text("✅ Объявление одобрено и опубликовано")
            else:
                await query.edit_message_text("❌ Ошибка при одобрении")
                
        elif action == "reject":
            await DatabaseService.reject_post(post_id)
            
            try:
                creator = json.loads(post['creator']) if isinstance(post['creator'], str) else post['creator']
                await telegram_bot.send_message(
                    chat_id=creator['user_id'],
                    text="❌ Ваше объявление было отклонено модератором за нарушение правил"
                )
            except Exception as e:
                logger.error(f"Failed to notify user: {e}")
            
            await query.edit_message_text("❌ Объявление отклонено")

    async def send_for_moderation(self, post: Dict):
        if not config.MODERATION_CHAT_ID:
            logger.warning("MODERATION_CHAT_ID not set, auto-approving post")
            return await DatabaseService.approve_post(post['id'])
        
        try:
            creator = json.loads(post['creator']) if isinstance(post['creator'], str) else post['creator']
            
            text = (
                f"📝 Новое объявление #{post['id']}\n\n"
                f"👤 От: {creator['first_name']} {creator.get('last_name', '')}\n"
                f"🆔 ID: {creator['user_id']}\n"
                f"👤 Username: @{creator.get('username', 'нет')}\n"
                f"📂 Категория: {post['category']}\n\n"
                f"📄 Текст:\n{post['description']}\n\n"
                f"🏷 Теги: {', '.join(json.loads(post['tags']) if post['tags'] else [])}"
            )
            
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ Принять", callback_data=f"approve_{post['id']}"),
                    InlineKeyboardButton("❌ Отклонить", callback_data=f"reject_{post['id']}")
                ]
            ])
            
            await telegram_bot.send_message(
                chat_id=config.MODERATION_CHAT_ID,
                text=text,
                reply_markup=keyboard
            )
            
        except Exception as e:
            logger.error(f"Failed to send moderation message: {e}")
            return await DatabaseService.approve_post(post['id'])

# WebSocket
async def broadcast_message(message: Dict):
    if connected_clients:
        message_str = json.dumps(message)
        disconnected_clients = set()
        
        for client in connected_clients.copy():
            try:
                await client.send(message_str)
            except websockets.exceptions.ConnectionClosed:
                disconnected_clients.add(client)
            except Exception as e:
                logger.error(f"Error broadcasting to client: {e}")
                disconnected_clients.add(client)
        
        connected_clients -= disconnected_clients

async def handle_websocket(websocket: WebSocketServerProtocol):
    connected_clients.add(websocket)
    logger.info(f"Client connected. Total clients: {len(connected_clients)}")
    
    try:
        async for message in websocket:
            try:
                data = json.loads(message)
                await handle_websocket_message(websocket, data)
            except json.JSONDecodeError:
                await websocket.send(json.dumps({'type': 'error', 'message': 'Invalid JSON'}))
            except Exception as e:
                logger.error(f"WebSocket message error: {e}")
                await websocket.send(json.dumps({'type': 'error', 'message': str(e)}))
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        connected_clients.discard(websocket)
        logger.info(f"Client disconnected. Total clients: {len(connected_clients)}")

async def handle_websocket_message(websocket: WebSocketServerProtocol, data: Dict):
    action = data.get('type')
    user_id = data.get('user_id')
    
    # Проверяем бан
    if user_id and await DatabaseService.is_user_banned(user_id):
        await websocket.send(json.dumps({
            'type': 'banned',
            'message': 'Ваш аккаунт заблокирован'
        }))
        return
    
    if action == 'sync_user':
        user_data = await DatabaseService.sync_user(data)
        await websocket.send(json.dumps({
            'type': 'user_data',
            'user_id': user_data['user_id'],
            'first_name': user_data['first_name'],
            'last_name': user_data['last_name'],
            'username': user_data['username'],
            'photo_url': user_data['photo_url']
        }))
    
    elif action == 'create_post':
        # Проверка лимита
        if not await PostLimitService.check_user_limit(user_id):
            await websocket.send(json.dumps({
                'type': 'limit_exceeded',
                'message': f'Достигнут дневной лимит объявлений'
            }))
            return
        
        # Создание поста
        post = await DatabaseService.create_post({
            'user_id': user_id,
            'description': data['description'],
            'category': data['category'],
            'tags': data['tags'],
            'creator': data['creator']
        })
        
        # Отправляем на модерацию
        if telegram_bot:
            moderation_bot = ModerationBot()
            await moderation_bot.send_for_moderation(post)
        
        await websocket.send(json.dumps({
            'type': 'post_created',
            'message': 'Объявление отправлено на модерацию'
        }))
    
    elif action == 'get_posts':
        posts = await DatabaseService.get_posts(
            data, data['page'], data['limit'], data.get('search', '')
        )
        await websocket.send(json.dumps({
            'type': 'posts',
            'posts': posts,
            'append': data.get('append', False)
        }))
    
    elif action == 'like_post':
        post = await DatabaseService.like_post(data['post_id'])
        if post:
            await broadcast_message({'type': 'post_updated', 'post': post})
    
    elif action == 'delete_post':
        success = await DatabaseService.delete_post(data['post_id'], data['user_id'])
        if success:
            await broadcast_message({'type': 'post_deleted', 'post_id': data['post_id']})
    
    elif action == 'report_post':
        await DatabaseService.report_post(
            data['post_id'], 
            data['user_id'], 
            data.get('reason')
        )

# Основная функция для запуска HTTP сервера статических файлов
async def serve_static_files():
    """Обслуживание статических файлов для фронтенда"""
    from aiohttp import web
    
    async def index_handler(request):
        """Возвращает index.html для всех маршрутов"""
        try:
            with open('index.html', 'r', encoding='utf-8') as f:
                content = f.read()
            return web.Response(text=content, content_type='text/html')
        except FileNotFoundError:
            return web.Response(text="index.html not found", status=404)
    
    async def health_handler(request):
        """Health check endpoint"""
        return web.Response(text="OK", status=200)
    
    app = web.Application()
    app.router.add_get('/health', health_handler)
    app.router.add_get('/', index_handler)
    app.router.add_get('/{path:.*}', index_handler)  # Catch-all для SPA
    
    runner = web.AppRunner(app)
    await runner.setup()
    
    # Используем порт на 1 больше чем WebSocket
    http_port = config.PORT + 1
    site = web.TCPSite(runner, '0.0.0.0', http_port)
    await site.start()
    logger.info(f"HTTP server started on port {http_port}")

# Основная функция
async def main():
    # Инициализация базы данных
    await DatabaseService.init_database()
    
    # Инициализация бота
    moderation_bot = ModerationBot()
    await moderation_bot.init_bot()
    
    # Запуск HTTP сервера для статических файлов
    await serve_static_files()
    
    # Запуск WebSocket сервера
    server = await websockets.serve(handle_websocket, '0.0.0.0', config.PORT)
    logger.info(f"WebSocket server started on port {config.PORT}")
    
    # Запуск бота
    await moderation_bot.app.updater.start_polling()
    
    try:
        await asyncio.Future()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    finally:
        await moderation_bot.app.stop()
        server.close()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Application stopped by user")
    except Exception as e:
        logger.error(f"Application error: {e}")
        raise
