#!/usr/bin/env python3
"""
server.py - Telegram Web App Server
Объединенный сервер для WebSocket, HTTP и Telegram Bot
Автор: Assistant 
Версия: 1.1
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
                    reported_posts BIGINT[] DEFAULT '{}',
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
            
            # Получаем актуальное количество опубликованных постов
            published_count = await conn.fetchval("""
                SELECT COUNT(*) FROM posts 
                WHERE user_id = $1 AND status = 'approved'
            """, user_data['user_id'])
            
            # Кешируем пользователя
            user_dict = dict(user)
            user_dict['published_posts'] = published_count
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
    async def get_posts(filters: Dict, page: int, limit: int, search: str = '', user_id: int = None) -> List[Dict]:
        async with get_db_connection() as conn:
            query = """
                SELECT p.*, 
                       (CASE WHEN $10::BIGINT = ANY(u.liked) THEN TRUE ELSE FALSE END) as user_liked
                FROM posts p
                LEFT JOIN users u ON u.user_id = $10
                WHERE p.status = 'approved'
            """
            params = [filters.get('category', '')]
            param_count = 1
            
            # Категория
            if filters.get('category'):
                param_count += 1
                query += f" AND p.category = ${param_count}"
                params.append(filters['category'])
            
            # Поиск
            if search:
                param_count += 1
                query += f" AND LOWER(p.description) LIKE LOWER(${param_count})"
                params.append(f"%{search}%")
            
            # Фильтры по тегам
            if filters.get('filters'):
                for filter_type, values in filters['filters'].items():
                    if values and filter_type != 'sort' and isinstance(values, list):
                        for value in values:
                            param_count += 1
                            query += f" AND p.tags @> ${param_count}"
                            params.append(json.dumps([f"{filter_type}:{value}"]))
            
            # Специальные фильтры
            if filters.get('filters', {}).get('sort'):
                sort_type = filters['filters']['sort']
                if sort_type == 'my' and user_id:
                    query += f" AND p.user_id = ${len(params) + 1}"
                    params.append(user_id)
                elif sort_type == 'favorites' and user_id:
                    query += f" AND p.id = ANY((SELECT favorites FROM users WHERE user_id = ${len(params) + 1}))"
                    params.append(user_id)
                elif sort_type == 'hidden' and user_id:
                    query += f" AND p.id = ANY((SELECT hidden FROM users WHERE user_id = ${len(params) + 1}))"
                    params.append(user_id)
            
            # Сортировка
            sort_type = filters.get('filters', {}).get('sort', 'new')
            if sort_type == 'old':
                query += " ORDER BY p.created_at ASC"
            elif sort_type == 'rating':
                query += " ORDER BY p.likes DESC, p.created_at DESC"
            else:
                query += " ORDER BY p.created_at DESC"
            
            # Добавляем недостающие параметры до $10
            while len(params) < 10:
                params.append(None)
            
            query += f" LIMIT {limit} OFFSET {(page - 1) * limit}"
            
            posts = await conn.fetch(query, *params[:9], user_id)
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
    async def like_post(post_id: int, user_id: int) -> Optional[Dict]:
        async with get_db_connection() as conn:
            # Проверяем, лайкал ли пользователь этот пост
            user = await conn.fetchrow("SELECT liked FROM users WHERE user_id = $1", user_id)
            if not user:
                return None
            
            liked_posts = user['liked'] or []
            
            if post_id in liked_posts:
                # Убираем лайк
                await conn.execute("""
                    UPDATE users SET liked = array_remove(liked, $1) WHERE user_id = $2
                """, post_id, user_id)
                await conn.execute("""
                    UPDATE posts SET likes = likes - 1 WHERE id = $1 AND status = 'approved'
                """, post_id)
                action = 'removed'
            else:
                # Ставим лайк
                await conn.execute("""
                    UPDATE users SET liked = array_append(liked, $1) WHERE user_id = $2
                """, post_id, user_id)
                await conn.execute("""
                    UPDATE posts SET likes = likes + 1 WHERE id = $1 AND status = 'approved'
                """, post_id)
                action = 'added'
            
            post = await conn.fetchrow("SELECT * FROM posts WHERE id = $1", post_id)
            if post:
                post_dict = dict(post)
                post_dict['like_action'] = action
                post_dict['user_liked'] = action == 'added'
                posts_cache[post_id] = post_dict
                return post_dict
            return None

    @staticmethod
    async def report_post(post_id: int, reporter_id: int, reason: str = None) -> Dict:
        async with get_db_connection() as conn:
            # Проверяем, жаловался ли пользователь на этот пост
            user = await conn.fetchrow("SELECT reported_posts FROM users WHERE user_id = $1", reporter_id)
            if user and user['reported_posts'] and post_id in user['reported_posts']:
                return {'success': False, 'message': 'already_reported'}
            
            # Добавляем жалобу
            await conn.execute("""
                INSERT INTO post_reports (post_id, reporter_id, reason) VALUES ($1, $2, $3)
            """, post_id, reporter_id, reason)
            
            # Добавляем пост в список пожалованных пользователем
            await conn.execute("""
                UPDATE users SET reported_posts = array_append(reported_posts, $1) WHERE user_id = $2
            """, post_id, reporter_id)
            
            return {'success': True, 'message': 'reported'}

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
    async def add_to_favorites(post_id: int, user_id: int) -> Dict:
        async with get_db_connection() as conn:
            user = await conn.fetchrow("SELECT favorites FROM users WHERE user_id = $1", user_id)
            if not user:
                return {'success': False, 'message': 'user_not_found'}
            
            favorites = user['favorites'] or []
            
            if post_id in favorites:
                # Убираем из избранного
                await conn.execute("""
                    UPDATE users SET favorites = array_remove(favorites, $1) WHERE user_id = $2
                """, post_id, user_id)
                return {'success': True, 'action': 'removed', 'message': 'removed_from_favorites'}
            else:
                # Добавляем в избранное
                await conn.execute("""
                    UPDATE users SET favorites = array_append(favorites, $1) WHERE user_id = $2
                """, post_id, user_id)
                return {'success': True, 'action': 'added', 'message': 'added_to_favorites'}

    @staticmethod
    async def hide_post(post_id: int, user_id: int) -> Dict:
        async with get_db_connection() as conn:
            user = await conn.fetchrow("SELECT hidden FROM users WHERE user_id = $1", user_id)
            if not user:
                return {'success': False, 'message': 'user_not_found'}
            
            hidden = user['hidden'] or []
            
            if post_id in hidden:
                # Показываем пост
                await conn.execute("""
                    UPDATE users SET hidden = array_remove(hidden, $1) WHERE user_id = $2
                """, post_id, user_id)
                return {'success': True, 'action': 'shown', 'message': 'post_shown'}
            else:
                # Скрываем пост
                await conn.execute("""
                    UPDATE users SET hidden = array_append(hidden, $1) WHERE user_id = $2
                """, post_id, user_id)
                return {'success': True, 'action': 'hidden', 'message': 'post_hidden'}

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

    @staticmethod
    async def get_user_published_posts_count(user_id: int) -> int:
        async with get_db_connection() as conn:
            result = await conn.fetchval("""
                SELECT COUNT(*) FROM posts 
                WHERE user_id = $1 AND status = 'approved'
            """, user_id)
            return result or 0

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

    async def send_report_for_moderation(self, post: Dict, reporter_data: Dict, reason: str = None):
        if not config.MODERATION_CHAT_ID:
            logger.warning("MODERATION_CHAT_ID not set, cannot send report")
            return
        
        try:
            creator = json.loads(post['creator']) if isinstance(post['creator'], str) else post['creator']
            
            text = (
                f"🚨 ЖАЛОБА НА ОБЪЯВЛЕНИЕ #{post['id']}\n\n"
                f"👤 Автор объявления: {creator['first_name']} {creator.get('last_name', '')}\n"
                f"🆔 ID автора: {creator['user_id']}\n"
                f"👤 Username автора: @{creator.get('username', 'нет')}\n\n"
                f"🚨 Жалобу подал: {reporter_data['first_name']} {reporter_data.get('last_name', '')}\n"
                f"🆔 ID жалобщика: {reporter_data['user_id']}\n"
                f"👤 Username жалобщика: @{reporter_data.get('username', 'нет')}\n\n"
                f"📂 Категория: {post['category']}\n"
                f"📄 Текст объявления:\n{post['description']}\n\n"
                f"🏷 Теги: {', '.join(json.loads(post['tags']) if post['tags'] else [])}\n\n"
                f"💬 Причина жалобы: {reason or 'Не указана'}"
            )
            
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("🗑 Удалить объявление", callback_data=f"delete_{post['id']}"),
                    InlineKeyboardButton("✅ Оставить", callback_data=f"keep_{post['id']}")
                ]
            ])
            
            await telegram_bot.send_message(
                chat_id=config.MODERATION_CHAT_ID,
                text=text,
                reply_markup=keyboard
            )
            
        except Exception as e:
            logger.error(f"Failed to send report message: {e}")

# WebSocket
async def broadcast_message(message: Dict, filter_data: Dict = None):
    if connected_clients:
        message_str = json.dumps(message)
        disconnected_clients = set()
        
        for client in connected_clients.copy():
            try:
                # Если это обновление поста, не отправляем broadcast
                # Пользователи сами обновят при смене фильтров
                if message.get('type') == 'post_updated' and filter_data:
                    continue
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
        published_count = await DatabaseService.get_user_published_posts_count(user_id)
        await websocket.send(json.dumps({
            'type': 'user_synced',
            'user_id': user_data['user_id'],
            'limits': {
                'used': published_count,
                'total': user_data.get('post_limit', config.DAILY_POST_LIMIT)
            },
            'is_banned': user_data.get('is_banned', False)
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
            'creator': data['creator_data']
        })
        
        # Отправляем на модерацию
        if telegram_bot:
            moderation_bot = ModerationBot()
            await moderation_bot.send_for_moderation(post)
        
        # Получаем обновленное количество опубликованных постов
        published_count = await DatabaseService.get_user_published_posts_count(user_id)
        limit = await DatabaseService.get_user_limit(user_id)
        
        await websocket.send(json.dumps({
            'type': 'post_created',
            'message': 'Объявление отправлено на модерацию',
            'limits': {
                'used': published_count,
                'total': limit
            }
        }))
    
    elif action == 'get_posts':
        posts = await DatabaseService.get_posts(
            data, data['page'], data['limit'], data.get('search', ''), user_id
        )
        await websocket.send(json.dumps({
            'type': 'posts',
            'posts': posts,
            'append': data.get('append', False)
        }))
    
    elif action == 'like_post':
        post = await DatabaseService.like_post(data['post_id'], user_id)
        if post:
            # Отправляем только этому пользователю обновление
            await websocket.send(json.dumps({'type': 'post_updated', 'post': post}))
    
    elif action == 'delete_post':
        success = await DatabaseService.delete_post(data['post_id'], user_id)
        if success:
            # Получаем обновленное количество опубликованных постов
            published_count = await DatabaseService.get_user_published_posts_count(user_id)
            limit = await DatabaseService.get_user_limit(user_id)
            
            await broadcast_message({'type': 'post_deleted', 'post_id': data['post_id']})
            await websocket.send(json.dumps({
                'type': 'limits_updated',
                'limits': {
                    'used': published_count,
                    'total': limit
                }
            }))
    
    elif action == 'report_post':
        post = await DatabaseService.get_post_by_id(data['post_id'])
        if post:
            result = await DatabaseService.report_post(
                data['post_id'], 
                user_id, 
                data.get('reason')
            )
            
            if result['success']:
                if result['message'] == 'already_reported':
                    await websocket.send(json.dumps({
                        'type': 'error',
                        'message': 'Вы уже отправляли жалобу на это объявление'
                    }))
                else:
                    # Отправляем жалобу модераторам
                    if telegram_bot:
                        moderation_bot = ModerationBot()
                        reporter_data = {
                            'user_id': user_id,
                            'first_name': data.get('reporter_first_name', ''),
                            'last_name': data.get('reporter_last_name', ''),
                            'username': data.get('reporter_username', '')
                        }
                        await moderation_bot.send_report_for_moderation(post, reporter_data, data.get('reason'))
                    
                    await websocket.send(json.dumps({
                        'type': 'report_sent',
                        'message': 'Жалоба отправлена модераторам'
                    }))
    
    elif action == 'add_to_favorites':
        result = await DatabaseService.add_to_favorites(data['post_id'], user_id)
        await websocket.send(json.dumps({
            'type': 'favorites_updated',
            'action': result['action'],
            'message': result['message']
        }))
    
    elif action == 'hide_post':
        result = await DatabaseService.hide_post(data['post_id'], user_id)
        await websocket.send(json.dumps({
            'type': 'hide_updated',
            'action': result['action'],
            'message': result['message']
        }))

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
