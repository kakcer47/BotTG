const TelegramBot = require('node-telegram-bot-api');
const { Pool } = require('pg');
const express = require('express');

// Конфигурация
const BOT_TOKEN = process.env.MODERATOR_BOT_TOKEN;
const GROUP_ID = process.env.GROUP_ID;
const DATABASE_URL = process.env.DATABASE_URL;
const PORT = process.env.PORT || 3001;
const WEBHOOK_URL = process.env.WEBHOOK_URL;

console.log('=== МОДЕРАТОРСКИЙ БОТ ===');
console.log('BOT_TOKEN:', BOT_TOKEN ? 'установлен' : 'НЕ УСТАНОВЛЕН');
console.log('GROUP_ID:', GROUP_ID);
console.log('DATABASE_URL:', DATABASE_URL ? 'установлен' : 'НЕ УСТАНОВЛЕН');

// Инициализация БД
const pool = new Pool({
    connectionString: DATABASE_URL,
    ssl: DATABASE_URL?.includes('localhost') ? false : { rejectUnauthorized: false }
});

// Инициализация бота
const bot = new TelegramBot(BOT_TOKEN);
const app = express();

app.use(express.json());

// Настройка webhook или polling
if (WEBHOOK_URL) {
    const webhookPath = `/moderator${BOT_TOKEN}`;
    bot.setWebHook(`${WEBHOOK_URL}${webhookPath}`);
    app.post(webhookPath, (req, res) => {
        bot.processUpdate(req.body);
        res.sendStatus(200);
    });
    console.log('✅ Webhook режим');
} else {
    bot.startPolling();
    console.log('✅ Polling режим');
}

// Создание таблицы при запуске
async function initDatabase() {
    try {
        await pool.query(`
            CREATE TABLE IF NOT EXISTS user_messages (
                user_id BIGINT PRIMARY KEY,
                group_id BIGINT NOT NULL,
                message_count INTEGER DEFAULT 0,
                is_restricted BOOLEAN DEFAULT FALSE,
                last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        `);
        console.log('✅ База данных инициализирована');
    } catch (error) {
        console.error('❌ Ошибка инициализации БД:', error);
    }
}

// Получить данные пользователя
async function getUserData(userId, groupId) {
    try {
        const result = await pool.query(
            'SELECT * FROM user_messages WHERE user_id = $1 AND group_id = $2',
            [userId, groupId]
        );
        return result.rows[0] || null;
    } catch (error) {
        console.error('Ошибка получения данных пользователя:', error);
        return null;
    }
}

// Создать запись пользователя
async function createUserRecord(userId, groupId) {
    try {
        await pool.query(
            'INSERT INTO user_messages (user_id, group_id, message_count) VALUES ($1, $2, 1)',
            [userId, groupId]
        );
        console.log(`[DB] Создана запись для пользователя ${userId}`);
        return { user_id: userId, group_id: groupId, message_count: 1, is_restricted: false };
    } catch (error) {
        console.error('Ошибка создания записи:', error);
        return null;
    }
}

// Обновить счетчик сообщений
async function updateMessageCount(userId, groupId, increment = 1) {
    try {
        const result = await pool.query(
            'UPDATE user_messages SET message_count = message_count + $1, last_updated = CURRENT_TIMESTAMP WHERE user_id = $2 AND group_id = $3 RETURNING *',
            [increment, userId, groupId]
        );
        return result.rows[0] || null;
    } catch (error) {
        console.error('Ошибка обновления счетчика:', error);
        return null;
    }
}

// Установить статус ограничения
async function setRestrictionStatus(userId, groupId, isRestricted) {
    try {
        await pool.query(
            'UPDATE user_messages SET is_restricted = $1 WHERE user_id = $2 AND group_id = $3',
            [isRestricted, userId, groupId]
        );
        console.log(`[DB] Пользователь ${userId} restriction: ${isRestricted}`);
    } catch (error) {
        console.error('Ошибка установки ограничения:', error);
    }
}

// Ограничить пользователя (запретить отправку сообщений)
async function restrictUser(chatId, userId) {
    try {
        await bot.restrictChatMember(chatId, userId, {
            can_send_messages: false,
            can_send_media_messages: false,
            can_send_polls: false,
            can_send_other_messages: false,
            can_add_web_page_previews: false
        });
        console.log(`[RESTRICT] Пользователь ${userId} ограничен`);
        return true;
    } catch (error) {
        console.error(`Ошибка ограничения пользователя ${userId}:`, error);
        return false;
    }
}

// Разрешить пользователю писать
async function unrestrictUser(chatId, userId) {
    try {
        await bot.restrictChatMember(chatId, userId, {
            can_send_messages: true,
            can_send_media_messages: true,
            can_send_polls: true,
            can_send_other_messages: true,
            can_add_web_page_previews: true
        });
        console.log(`[UNRESTRICT] Пользователь ${userId} разблокирован`);
        return true;
    } catch (error) {
        console.error(`Ошибка разблокировки пользователя ${userId}:`, error);
        return false;
    }
}

// Обработка сообщений в группе
bot.on('message', async (msg) => {
    // Игнорируем команды, сообщения от ботов и сообщения не из целевой группы
    if (msg.text?.startsWith('/') || msg.from.is_bot || msg.chat.id.toString() !== GROUP_ID) {
        return;
    }
    
    const userId = msg.from.id;
    const groupId = msg.chat.id;
    const userName = msg.from.first_name || 'Unknown';
    
    console.log(`[MESSAGE] ${userName} (${userId}) отправил сообщение`);
    
    try {
        // Получаем данные пользователя
        let userData = await getUserData(userId, groupId);
        
        if (!userData) {
            // Первое сообщение - создаем запись
            userData = await createUserRecord(userId, groupId);
            if (!userData) return;
            
            console.log(`[FIRST] Первое сообщение от ${userName} (1/3)`);
        } else {
            // Увеличиваем счетчик
            userData = await updateMessageCount(userId, groupId, 1);
            if (!userData) return;
            
            console.log(`[COUNT] ${userName}: ${userData.message_count} сообщений`);
        }
        
        // Проверяем нужно ли ограничить
        if (userData.message_count >= 3 && !userData.is_restricted) {
            const restricted = await restrictUser(groupId, userId);
            if (restricted) {
                await setRestrictionStatus(userId, groupId, true);
                console.log(`[ACTION] ${userName} ограничен (${userData.message_count}/3)`);
            }
        }
        
    } catch (error) {
        console.error('Ошибка обработки сообщения:', error);
    }
});

// Обработка удаления сообщений
bot.on('message', async (msg) => {
    // Следим за удалениями через webhook updates
    // В реальности это сложно отследить, поэтому делаем проверку периодически
});

// Обработка удаленных сообщений (альтернативный подход)
bot.on('edited_message', async (msg) => {
    // Telegram не всегда присылает уведомления об удалении
    // Поэтому будем делать периодическую проверку
});

// Периодическая проверка и снятие ограничений (каждые 5 минут)
setInterval(async () => {
    try {
        const result = await pool.query(
            'SELECT user_id, group_id, message_count FROM user_messages WHERE is_restricted = true'
        );
        
        for (const user of result.rows) {
            if (user.message_count < 3) {
                const unrestricted = await unrestrictUser(user.group_id, user.user_id);
                if (unrestricted) {
                    await setRestrictionStatus(user.user_id, user.group_id, false);
                    console.log(`[AUTO-UNRESTRICT] Пользователь ${user.user_id} разблокирован (${user.message_count}/3)`);
                }
            }
        }
    } catch (error) {
        console.error('Ошибка периодической проверки:', error);
    }
}, 5 * 60 * 1000); // 5 минут

// Команда для ручного уменьшения счетчика (скрытая, для админов)
bot.onText(/\/reduce (\d+)/, async (msg, match) => {
    // Проверяем что это админ (можно добавить проверку ID админов)
    const chatId = msg.chat.id;
    const targetUserId = parseInt(match[1]);
    
    if (chatId.toString() !== GROUP_ID) return;
    
    try {
        const userData = await updateMessageCount(targetUserId, chatId, -1);
        if (userData && userData.message_count < 3 && userData.is_restricted) {
            const unrestricted = await unrestrictUser(chatId, targetUserId);
            if (unrestricted) {
                await setRestrictionStatus(targetUserId, chatId, false);
                console.log(`[MANUAL-UNRESTRICT] Пользователь ${targetUserId} разблокирован`);
            }
        }
        
        // Удаляем команду
        await bot.deleteMessage(chatId, msg.message_id);
    } catch (error) {
        console.error('Ошибка ручного уменьшения:', error);
    }
});

// Минимальная обработка ошибок
bot.on('error', (error) => {
    console.error('[BOT ERROR]', error);
});

bot.on('polling_error', (error) => {
    console.error('[POLLING ERROR]', error);
});

// Веб-статус
app.get('/', async (req, res) => {
    try {
        const stats = await pool.query(`
            SELECT 
                COUNT(*) as total_users,
                COUNT(CASE WHEN message_count >= 3 THEN 1 END) as restricted_users,
                AVG(message_count) as avg_messages
            FROM user_messages 
            WHERE group_id = $1
        `, [GROUP_ID]);
        
        const recentUsers = await pool.query(`
            SELECT user_id, message_count, is_restricted, last_updated 
            FROM user_messages 
            WHERE group_id = $1 
            ORDER BY last_updated DESC 
            LIMIT 10
        `, [GROUP_ID]);
        
        const { total_users, restricted_users, avg_messages } = stats.rows[0];
        
        let usersList = '<h3>Последние пользователи:</h3><ul>';
        recentUsers.rows.forEach(user => {
            usersList += `<li>ID: ${user.user_id}, Сообщений: ${user.message_count}, Ограничен: ${user.is_restricted ? 'Да' : 'Нет'}</li>`;
        });
        usersList += '</ul>';
        
        res.send(`
            <h1>Moderator Bot Status</h1>
            <p>✅ Модераторский бот работает</p>
            <p>🆔 Group ID: ${GROUP_ID}</p>
            <p>🗄️ Database: ${DATABASE_URL ? 'подключена' : 'НЕ ПОДКЛЮЧЕНА'}</p>
            <p>🌐 Webhook: ${WEBHOOK_URL ? 'установлен' : 'polling режим'}</p>
            
            <h2>Статистика:</h2>
            <p>👥 Всего пользователей: ${total_users || 0}</p>
            <p>🚫 Ограниченных: ${restricted_users || 0}</p>
            <p>📊 Среднее сообщений: ${Math.round(avg_messages || 0)}</p>
            
            ${usersList}
            
            <h2>Логика работы:</h2>
            <ul>
                <li>3+ сообщений → ограничение на отправку</li>
                <li>&lt;3 сообщений → снятие ограничений</li>
                <li>Проверка каждые 5 минут</li>
                <li>Запись в БД только с первого сообщения</li>
            </ul>
        `);
    } catch (error) {
        console.error('Ошибка получения статистики:', error);
        res.send(`
            <h1>Moderator Bot Status</h1>
            <p>❌ Ошибка подключения к БД</p>
            <p>Error: ${error.message}</p>
        `);
    }
});

// Инициализация и запуск
async function start() {
    await initDatabase();
    app.listen(PORT, () => {
        console.log(`🚀 Модераторский бот запущен на порту ${PORT}`);
    });
}

start();
