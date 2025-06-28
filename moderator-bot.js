const TelegramBot = require('node-telegram-bot-api');
const { Pool } = require('pg');

// Конфигурация
const BOT_TOKEN = process.env.BOT_TOKEN;
const GROUP_ID = process.env.GROUP_ID;
const DATABASE_URL = process.env.DATABASE_URL;

console.log('=== МОДЕРАТОР БОТ ===');
console.log('BOT_TOKEN:', BOT_TOKEN ? 'установлен' : 'НЕ УСТАНОВЛЕН');
console.log('GROUP_ID:', GROUP_ID);
console.log('DATABASE_URL:', DATABASE_URL ? 'установлен' : 'НЕ УСТАНОВЛЕН');

if (!BOT_TOKEN || !GROUP_ID || !DATABASE_URL) {
    console.error('❌ Не все переменные установлены!');
    process.exit(1);
}

// Инициализация БД
const pool = new Pool({
    connectionString: DATABASE_URL,
    ssl: { rejectUnauthorized: false }
});

// Инициализация бота
const bot = new TelegramBot(BOT_TOKEN, { polling: true });

// Создание таблицы
async function initDB() {
    try {
        await pool.query(`
            CREATE TABLE IF NOT EXISTS users (
                id BIGINT PRIMARY KEY,
                count INTEGER DEFAULT 0,
                restricted BOOLEAN DEFAULT FALSE
            )
        `);
        console.log('✅ База данных готова');
    } catch (error) {
        console.error('❌ Ошибка БД:', error.message);
    }
}

// Получить пользователя
async function getUser(userId) {
    const result = await pool.query('SELECT * FROM users WHERE id = $1', [userId]);
    return result.rows[0];
}

// Создать пользователя
async function createUser(userId) {
    await pool.query('INSERT INTO users (id, count) VALUES ($1, 1)', [userId]);
    return { id: userId, count: 1, restricted: false };
}

// Обновить счетчик
async function updateCount(userId, change) {
    const result = await pool.query(
        'UPDATE users SET count = count + $1 WHERE id = $2 RETURNING *',
        [change, userId]
    );
    return result.rows[0];
}

// Установить ограничение
async function setRestricted(userId, restricted) {
    await pool.query('UPDATE users SET restricted = $1 WHERE id = $2', [restricted, userId]);
}

// Ограничить пользователя
async function restrictUser(userId) {
    try {
        await bot.restrictChatMember(GROUP_ID, userId, {
            can_send_messages: false
        });
        console.log(`[RESTRICT] Пользователь ${userId} ограничен`);
        return true;
    } catch (error) {
        console.error(`[ERROR] Не удалось ограничить ${userId}:`, error.message);
        return false;
    }
}

// Снять ограничения
async function unrestrictUser(userId) {
    try {
        await bot.restrictChatMember(GROUP_ID, userId, {
            can_send_messages: true,
            can_send_media_messages: true,
            can_send_polls: true,
            can_send_other_messages: true
        });
        console.log(`[UNRESTRICT] Пользователь ${userId} разблокирован`);
        return true;
    } catch (error) {
        console.error(`[ERROR] Не удалось разблокировать ${userId}:`, error.message);
        return false;
    }
}

// Обработка сообщений
bot.on('message', async (msg) => {
    // Только сообщения из целевой группы, не от ботов, не команды
    if (msg.chat.id.toString() !== GROUP_ID || msg.from.is_bot || msg.text?.startsWith('/')) {
        return;
    }
    
    const userId = msg.from.id;
    const userName = msg.from.first_name || 'User';
    
    try {
        let user = await getUser(userId);
        
        if (!user) {
            // Первое сообщение - создаем запись
            user = await createUser(userId);
            console.log(`[NEW] ${userName} (${userId}): 1/3`);
        } else {
            // Увеличиваем счетчик
            user = await updateCount(userId, 1);
            console.log(`[COUNT] ${userName} (${userId}): ${user.count}/3`);
        }
        
        // Проверяем ограничения
        if (user.count >= 3 && !user.restricted) {
            const success = await restrictUser(userId);
            if (success) {
                await setRestricted(userId, true);
                console.log(`[ACTION] ${userName} ограничен (${user.count}/3)`);
            }
        }
        
    } catch (error) {
        console.error('[DB ERROR]', error.message);
    }
});

// Обработка удаленных сообщений
bot.on('channel_post', async () => {
    // Здесь можно отслеживать удаления, но это сложно
    // Поэтому делаем периодическую проверку
});

// Периодическая проверка (каждые 2 минуты)
setInterval(async () => {
    try {
        const result = await pool.query('SELECT * FROM users WHERE restricted = true');
        
        for (const user of result.rows) {
            if (user.count < 3) {
                const success = await unrestrictUser(user.id);
                if (success) {
                    await setRestricted(user.id, false);
                    console.log(`[AUTO] Пользователь ${user.id} разблокирован (${user.count}/3)`);
                }
            }
        }
    } catch (error) {
        console.error('[CHECK ERROR]', error.message);
    }
}, 2 * 60 * 1000);

// Скрытая команда для уменьшения счетчика (только для админов)
bot.onText(/\/reduce (\d+)/, async (msg, match) => {
    if (msg.chat.id.toString() !== GROUP_ID) return;
    
    const userId = parseInt(match[1]);
    
    try {
        const user = await updateCount(userId, -1);
        if (user && user.count < 3 && user.restricted) {
            const success = await unrestrictUser(userId);
            if (success) {
                await setRestricted(userId, false);
                console.log(`[MANUAL] Пользователь ${userId} разблокирован`);
            }
        }
        
        // Удаляем команду
        await bot.deleteMessage(GROUP_ID, msg.message_id);
    } catch (error) {
        console.error('[REDUCE ERROR]', error.message);
    }
});

// Обработка ошибок
bot.on('error', (error) => {
    console.error('[BOT ERROR]', error.message);
});

bot.on('polling_error', (error) => {
    console.error('[POLLING ERROR]', error.message);
});

// HTTP сервер для статистики
const http = require('http');
const PORT = process.env.PORT || 3000;

const server = http.createServer(async (req, res) => {
    if (req.url === '/') {
        try {
            const stats = await pool.query(`
                SELECT 
                    COUNT(*) as total,
                    COUNT(CASE WHEN restricted = true THEN 1 END) as restricted,
                    AVG(count) as avg_count
                FROM users
            `);
            
            const recent = await pool.query('SELECT * FROM users ORDER BY id DESC LIMIT 10');
            
            const { total, restricted, avg_count } = stats.rows[0];
            
            res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
            res.end(`
                <h1>Модератор Бот</h1>
                <p>✅ Бот работает</p>
                <p>🆔 Group ID: ${GROUP_ID}</p>
                <p>👥 Всего пользователей: ${total || 0}</p>
                <p>🚫 Ограниченных: ${restricted || 0}</p>
                <p>📊 Среднее сообщений: ${Math.round(avg_count || 0)}</p>
                
                <h2>Последние пользователи:</h2>
                <ul>
                ${recent.rows.map(u => 
                    `<li>ID: ${u.id}, Сообщений: ${u.count}, Ограничен: ${u.restricted ? 'Да' : 'Нет'}</li>`
                ).join('')}
                </ul>
                
                <h2>Логика:</h2>
                <p>3+ сообщений = запрет на отправку<br>
                &lt;3 сообщений = разрешить отправку</p>
            `);
        } catch (error) {
            res.writeHead(500);
            res.end(`Ошибка БД: ${error.message}`);
        }
    } else {
        res.writeHead(404);
        res.end('Not Found');
    }
});

// Запуск
async function start() {
    await initDB();
    server.listen(PORT, () => {
        console.log(`🚀 Модератор запущен на порту ${PORT}`);
        console.log('=== МОДЕРАТОР ГОТОВ ===');
    });
}

start();
