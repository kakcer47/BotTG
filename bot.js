const TelegramBot = require('node-telegram-bot-api');
const express = require('express');

// Конфигурация из переменных окружения
const BOT_TOKEN = process.env.BOT_TOKEN;
const GROUP_ID = process.env.GROUP_ID;
const PORT = process.env.PORT || 3000;
const WEBHOOK_URL = process.env.WEBHOOK_URL;

console.log('=== НАСТРОЙКИ БОТА ===');
console.log('BOT_TOKEN:', BOT_TOKEN ? 'установлен' : 'НЕ УСТАНОВЛЕН');
console.log('GROUP_ID:', GROUP_ID);
console.log('WEBHOOK_URL:', WEBHOOK_URL || 'не установлен (будет polling)');
console.log('PORT:', PORT);

// Инициализация бота
const bot = new TelegramBot(BOT_TOKEN);
const app = express();

// Хранилище
const userSettings = new Map();
const messageComplaints = new Map();
const messageCache = new Map();

// Список тем
const TOPICS = {
    '27': 'Тема 1',
    '28': 'Тема 2', 
    '29': 'Тема 3',
    '30': 'Тема 4'
};

// Настройка webhook или polling
app.use(express.json());

if (WEBHOOK_URL) {
    bot.setWebHook(`${WEBHOOK_URL}/bot${BOT_TOKEN}`);
    app.post(`/bot${BOT_TOKEN}`, (req, res) => {
        bot.processUpdate(req.body);
        res.sendStatus(200);
    });
    console.log('✅ Webhook режим активен');
} else {
    bot.startPolling();
    console.log('✅ Polling режим активен');
}

app.get('/', (req, res) => {
    res.send(`
        <h1>Telegram Bot Status</h1>
        <p>✅ Бот работает</p>
        <p>🆔 Group ID: ${GROUP_ID}</p>
        <p>🤖 Bot Token: ${BOT_TOKEN ? 'установлен' : 'НЕ УСТАНОВЛЕН'}</p>
        <p>🌐 Webhook: ${WEBHOOK_URL ? 'установлен' : 'polling режим'}</p>
        <p>📊 Сообщений в кеше: ${messageCache.size}</p>
        <p>⚙️ Настроек пользователей: ${userSettings.size}</p>
    `);
});

// Тестовая команда для проверки работы
bot.onText(/\/test/, async (msg) => {
    const chatId = msg.chat.id;
    console.log(`[TEST] Команда /test от ${msg.from.first_name} в чате ${chatId}`);
    
    const keyboard = {
        inline_keyboard: [[
            { text: 'Тест 1', callback_data: 'test_1' },
            { text: 'Тест 2', callback_data: 'test_2' }
        ]]
    };
    
    await bot.sendMessage(chatId, '🧪 Тест кнопок:', { reply_markup: keyboard });
});

// Команда для получения информации о чате
bot.onText(/\/info/, async (msg) => {
    const chatId = msg.chat.id;
    const chatType = msg.chat.type;
    const userId = msg.from.id;
    
    console.log(`[INFO] Chat ID: ${chatId}, Type: ${chatType}, User: ${msg.from.first_name}`);
    
    let info = `📊 Информация о чате:
🆔 Chat ID: ${chatId}
📝 Тип чата: ${chatType}
👤 Пользователь: ${msg.from.first_name}
🎯 Целевая группа: ${GROUP_ID}
✅ Это целевая группа: ${chatId.toString() === GROUP_ID ? 'ДА' : 'НЕТ'}`;

    if (chatId.toString() === GROUP_ID) {
        try {
            const member = await bot.getChatMember(chatId, (await bot.getMe()).id);
            info += `\n🤖 Статус бота: ${member.status}`;
            info += `\n🔑 Права удаления: ${member.can_delete_messages ? 'ДА' : 'НЕТ'}`;
        } catch (error) {
            info += `\n❌ Ошибка проверки прав: ${error.message}`;
        }
    }
    
    await bot.sendMessage(chatId, info);
});

// Команда старт
bot.onText(/\/start/, async (msg) => {
    const chatId = msg.chat.id;
    console.log(`[START] Пользователь ${msg.from.first_name} запустил бота`);
    
    const keyboard = {
        inline_keyboard: Object.entries(TOPICS).map(([id, name]) => [{
            text: name,
            callback_data: `topic_${id}`
        }])
    };
    
    await bot.sendMessage(chatId, '🎯 Выберите тему для отправки сообщений:', { 
        reply_markup: keyboard 
    });
});

// Настройка тем
bot.onText(/\/setup (.+)/, async (msg, match) => {
    const chatId = msg.chat.id;
    const topicsInput = match[1];
    
    try {
        const topics = {};
        topicsInput.split(',').forEach(item => {
            const [id, name] = item.split(':');
            if (id && name) {
                topics[id.trim()] = name.trim();
            }
        });
        
        if (Object.keys(topics).length > 0) {
            Object.assign(TOPICS, topics);
            await bot.sendMessage(chatId, `✅ Настроены темы:\n${Object.entries(topics).map(([id, name]) => `• ${id}: ${name}`).join('\n')}`);
        } else {
            await bot.sendMessage(chatId, '❌ Неверный формат. Используйте: /setup ID:Название,ID:Название');
        }
    } catch (error) {
        console.error('[SETUP ERROR]', error);
        await bot.sendMessage(chatId, '❌ Ошибка настройки тем');
    }
});

// Обработка callback кнопок
bot.on('callback_query', async (query) => {
    const chatId = query.message.chat.id;
    const data = query.data;
    const userId = query.from.id;
    
    console.log(`[CALLBACK] ${data} от ${query.from.first_name}`);
    
    if (data.startsWith('topic_')) {
        const topicId = data.replace('topic_', '');
        const topicName = TOPICS[topicId];
        
        userSettings.set(chatId, { topicId, topicName });
        
        await bot.editMessageText(`✅ Выбрана тема: ${topicName}\n\nТеперь ваши сообщения будут отправляться в эту тему.`, {
            chat_id: chatId,
            message_id: query.message.message_id
        });
        
        await bot.answerCallbackQuery(query.id);
    }
    else if (data.startsWith('test_')) {
        await bot.answerCallbackQuery(query.id, { text: `Кнопка ${data} работает!` });
    }
    else if (data.startsWith('complain_')) {
        const messageId = data.replace('complain_', '');
        
        if (!messageComplaints.has(messageId)) {
            messageComplaints.set(messageId, new Set());
        }
        
        const complaints = messageComplaints.get(messageId);
        
        if (complaints.has(userId)) {
            await bot.answerCallbackQuery(query.id, { text: '⚠️ Вы уже пожаловались' });
            return;
        }
        
        complaints.add(userId);
        await bot.answerCallbackQuery(query.id, { text: `⚠️ Жалоба принята (${complaints.size}/5)` });
        
        if (complaints.size >= 5) {
            try {
                await bot.deleteMessage(chatId, messageId);
                console.log(`[DELETE] Сообщение ${messageId} удалено по жалобам`);
            } catch (error) {
                console.error('[DELETE ERROR]', error);
            }
        }
    }
    else if (data.startsWith('share_')) {
        const messageId = data.replace('share_', '');
        const groupIdNum = GROUP_ID.replace('-100', '');
        const messageLink = `https://t.me/c/${groupIdNum}/${messageId}`;
        
        try {
            await bot.sendMessage(userId, `🔗 Ссылка на сообщение:\n${messageLink}`);
            await bot.answerCallbackQuery(query.id, { text: '✅ Ссылка отправлена в личку' });
        } catch (error) {
            await bot.answerCallbackQuery(query.id, { text: '❌ Сначала напишите боту в личку' });
        }
    }
    else if (data.startsWith('author_')) {
        const messageId = data.replace('author_', '');
        const cached = messageCache.get(messageId);
        
        if (cached) {
            const author = cached.author;
            let authorInfo = `👤 Автор сообщения:\n${author.first_name}`;
            if (author.last_name) authorInfo += ` ${author.last_name}`;
            if (author.username) authorInfo += `\n@${author.username}\nhttps://t.me/${author.username}`;
            else authorInfo += `\ntg://user?id=${author.id}`;
            
            try {
                await bot.sendMessage(userId, authorInfo);
                await bot.answerCallbackQuery(query.id, { text: '✅ Контакт автора отправлен в личку' });
            } catch (error) {
                await bot.answerCallbackQuery(query.id, { text: '❌ Сначала напишите боту в личку' });
            }
        } else {
            await bot.answerCallbackQuery(query.id, { text: '❌ Информация об авторе не найдена' });
        }
    }
    else if (data.startsWith('delete_')) {
        try {
            await bot.deleteMessage(chatId, query.message.message_id);
            await bot.answerCallbackQuery(query.id, { text: '✅ Сообщение скрыто' });
        } catch (error) {
            await bot.answerCallbackQuery(query.id, { text: '❌ Не удалось скрыть' });
        }
    }
});

// Основная обработка сообщений
bot.on('message', async (msg) => {
    // Игнорируем команды и сообщения от ботов
    if (msg.text?.startsWith('/') || msg.from.is_bot) return;
    
    const chatId = msg.chat.id;
    const messageId = msg.message_id;
    
    console.log(`[MESSAGE] От ${msg.from.first_name} в чате ${chatId}: ${msg.text || 'медиа'}`);
    
    // Обработка сообщений в целевой группе
    if (chatId.toString() === GROUP_ID) {
        console.log('[GROUP] Сообщение в целевой группе');
        
        try {
            // Сохраняем в кеш
            messageCache.set(messageId, {
                author: {
                    id: msg.from.id,
                    username: msg.from.username,
                    first_name: msg.from.first_name,
                    last_name: msg.from.last_name
                },
                content: msg.text || 'медиа',
                timestamp: Date.now()
            });
            
            // Создаем кнопки
            const keyboard = {
                inline_keyboard: [[
                    { text: 'Пожаловаться', callback_data: `complain_${messageId}` },
                    { text: 'Удалить для себя', callback_data: `delete_${messageId}` }
                ], [
                    { text: 'Поделиться', callback_data: `share_${messageId}` },
                    { text: 'Автор', callback_data: `author_${messageId}` }
                ]]
            };
            
            // Информация об авторе
            let authorName = msg.from.first_name || 'Пользователь';
            if (msg.from.last_name) authorName += ` ${msg.from.last_name}`;
            if (msg.from.username) authorName += ` (@${msg.from.username})`;
            
            // Отправляем панель управления
            await bot.sendMessage(GROUP_ID, `🔧 Управление сообщением от ${authorName}:`, {
                reply_to_message_id: messageId,
                reply_markup: keyboard,
                disable_notification: true
            });
            
            console.log('[BUTTONS] Кнопки добавлены к сообщению');
            
        } catch (error) {
            console.error('[GROUP ERROR]', error);
        }
        
        return;
    }
    
    // Пересылка сообщений в темы (из личных чатов)
    const userConfig = userSettings.get(chatId);
    
    if (!userConfig) {
        await bot.sendMessage(chatId, '❌ Используйте /start для настройки темы');
        return;
    }
    
    try {
        const messageOptions = {
            message_thread_id: parseInt(userConfig.topicId)
        };
        
        console.log(`[FORWARD] Пересылка в тему ${userConfig.topicId}`);
        
        // Пересылка в зависимости от типа сообщения
        if (msg.text) {
            await bot.sendMessage(GROUP_ID, msg.text, messageOptions);
        } else if (msg.photo) {
            await bot.sendPhoto(GROUP_ID, msg.photo[msg.photo.length - 1].file_id, {
                ...messageOptions,
                caption: msg.caption || ''
            });
        } else if (msg.document) {
            await bot.sendDocument(GROUP_ID, msg.document.file_id, {
                ...messageOptions,
                caption: msg.caption || ''
            });
        } else if (msg.video) {
            await bot.sendVideo(GROUP_ID, msg.video.file_id, {
                ...messageOptions,
                caption: msg.caption || ''
            });
        } else if (msg.voice) {
            await bot.sendVoice(GROUP_ID, msg.voice.file_id, messageOptions);
        } else if (msg.sticker) {
            await bot.sendSticker(GROUP_ID, msg.sticker.file_id, messageOptions);
        } else if (msg.audio) {
            await bot.sendAudio(GROUP_ID, msg.audio.file_id, {
                ...messageOptions,
                caption: msg.caption || ''
            });
        }
        
        // Подтверждение
        await bot.sendMessage(chatId, '✅ Отправлено', { 
            reply_to_message_id: messageId 
        });
        
        console.log('[FORWARD] Успешно переслано');
        
    } catch (error) {
        console.error('[FORWARD ERROR]', error);
        
        let errorMsg = '❌ Ошибка отправки';
        if (error.message.includes('thread not found')) {
            errorMsg = '❌ Тема не найдена. Используйте /setup для настройки';
        }
        
        await bot.sendMessage(chatId, errorMsg);
    }
});

// Обработка ошибок
bot.on('error', (error) => {
    console.error('[BOT ERROR]', error);
});

bot.on('polling_error', (error) => {
    console.error('[POLLING ERROR]', error);
});

process.on('unhandledRejection', (error) => {
    console.error('[UNHANDLED REJECTION]', error);
});

// Запуск сервера
app.listen(PORT, () => {
    console.log(`🚀 Сервер запущен на порту ${PORT}`);
    console.log('=== БОТ ГОТОВ К РАБОТЕ ===');
});
