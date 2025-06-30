import asyncio
import logging
import os
from datetime import datetime
from typing import Optional, List, Dict
import asyncpg
import websockets
from websockets.server import WebSocketServerProtocol
import json
from collections import defaultdict
from contextlib import asynccontextmanager

# Configuration
class Config:
    DATABASE_URL = os.getenv("ACCOUNTS_DATABASE_URL")
    PORT = int(os.getenv("PORT", "10000"))
    RATE_LIMIT_WINDOW = 60
    RATE_LIMIT_MAX_REQUESTS = 10
    DB_MIN_SIZE = 2
    DB_MAX_SIZE = 8
    DB_COMMAND_TIMEOUT = 30

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Database pool
db_pool = None

# Rate limiting
rate_limiter = defaultdict(list)

# Database Service
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
        if not Config.DATABASE_URL:
            raise ValueError("ACCOUNTS_DATABASE_URL not set")
        
        db_pool = await asyncpg.create_pool(
            Config.DATABASE_URL,
            min_size=Config.DB_MIN_SIZE,
            max_size=Config.DB_MAX_SIZE,
            command_timeout=Config.DB_COMMAND_TIMEOUT
        )
        
        async with get_db_connection() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS accounts (
                    user_id BIGINT PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    photo_url TEXT,
                    favorites BIGINT[] DEFAULT '{}',
                    hidden BIGINT[] DEFAULT '{}',
                    liked BIGINT[] DEFAULT '{}',
                    posts BIGINT[] DEFAULT '{}',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_accounts_user_id ON accounts(user_id)")
        
        logger.info("Accounts database initialized")

    @staticmethod
    async def sync_user(user_data: Dict) -> Dict:
        async with get_db_connection() as conn:
            user = await conn.fetchrow(
                "SELECT * FROM accounts WHERE user_id = $1", user_data['user_id']
            )
            if not user:
                await conn.execute(
                    """INSERT INTO accounts (user_id, username, first_name, last_name, photo_url)
                       VALUES ($1, $2, $3, $4, $5)""",
                    user_data['user_id'], user_data['username'], user_data['first_name'],
                    user_data['last_name'], user_data['photo_url']
                )
            return user_data

    @staticmethod
    async def update_favorites(user_id: int, post_id: int, add: bool = True) -> bool:
        async with get_db_connection() as conn:
            if add:
                await conn.execute(
                    "UPDATE accounts SET favorites = array_append(favorites, $1) WHERE user_id = $2",
                    post_id, user_id
                )
            else:
                await conn.execute(
                    "UPDATE accounts SET favorites = array_remove(favorites, $1) WHERE user_id = $2",
                    post_id, user_id
                )
            return True

    @staticmethod
    async def update_hidden(user_id: int, post_id: int, add: bool = True) -> bool:
        async with get_db_connection() as conn:
            if add:
                await conn.execute(
                    "UPDATE accounts SET hidden = array_append(hidden, $1) WHERE user_id = $2",
                    post_id, user_id
                )
            else:
                await conn.execute(
                    "UPDATE accounts SET hidden = array_remove(hidden, $1) WHERE user_id = $2",
                    post_id, user_id
                )
            return True

    @staticmethod
    async def update_liked(user_id: int, post_id: int, add: bool = True) -> bool:
        async with get_db_connection() as conn:
            if add:
                await conn.execute(
                    "UPDATE accounts SET liked = array_append(liked, $1) WHERE user_id = $2",
                    post_id, user_id
                )
            else:
                await conn.execute(
                    "UPDATE accounts SET liked = array_remove(liked, $1) WHERE user_id = $2",
                    post_id, user_id
                )
            return True

    @staticmethod
    async def update_posts(user_id: int, post_id: int, add: bool = True) -> bool:
        async with get_db_connection() as conn:
            if add:
                await conn.execute(
                    "UPDATE accounts SET posts = array_append(posts, $1) WHERE user_id = $2",
                    post_id, user_id
                )
            else:
                await conn.execute(
                    "UPDATE accounts SET posts = array_remove(posts, $1) WHERE user_id = $2",
                    post_id, user_id
                )
            return True

# WebSocket Handler
async def handle_websocket(websocket: WebSocketServerProtocol):
    async for message in websocket:
        try:
            data = json.loads(message)
            action = data.get('type')
            
            if action == 'sync_user':
                user_data = await DatabaseService.sync_user(data)
                await websocket.send(json.dumps({'type': 'user_data', 'user_id': user_data['user_id']}))
            
            elif action == 'favorite_post':
                await DatabaseService.update_favorites(data['user_id'], data['post_id'])
            
            elif action == 'hide_post':
                await DatabaseService.update_hidden(data['user_id'], data['post_id'])
            
            elif action == 'update_post':
                await DatabaseService.update_posts(data['user_id'], data['post_id'], data.get('add', True))
        
        except Exception as e:
            logger.error(f"WebSocket error: {e}")
            await websocket.send(json.dumps({'type': 'error', 'message': str(e)}))

# Main
async def main():
    await DatabaseService.init_database()
    server = await websockets.serve(handle_websocket, '0.0.0.0', Config.PORT)
    logger.info(f"Accounts WebSocket server started on port {Config.PORT}")
    await asyncio.Future()  # Run forever

if __name__ == '__main__':
    asyncio.run(main())
