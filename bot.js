const TelegramBot = require('node-telegram-bot-api');

// Конфигурация из переменных окружения
const BOT_TOKEN = process.env.BOT_TOKEN;
const GROUP_ID = process.env.GROUP_ID;

// Инициализация бота с обработкой конфликтов
const bot = new TelegramBot(BOT_TOKEN, { 
    polling: {
        interval: 300,
        autoStart: true,
        params: {
            timeout: 10
        }
    }
});

// Обработка ошибок polling
bot.on('polling_error', (error) => {
    console.log('Polling error:', error.message);
    if (error.message.includes('409 Conflict')) {
        console.log('Останавливаю конфликтующий экземпляр...');
        setTimeout(() => {
            process.exit(1);
        }, 5000);
    }
});

// Хранилище пользовательских настроек и жалоб
const userSettings = new Map();
const messageComplaints = new Map(); // messageId -> Set(userIds)
const groupSettings = new Map(); // chatId -> { moderationEnabled: true/false }

// Список доступных тем (замените на реальные ID из вашей группы)
const TOPICS = {
    '27': 'Тема 1',  // Замените на ваши реальные ID и названия
    '28': 'Тема 2',  // Получите из ссылок тем
    '29': 'Тема 3',
    '30': 'Тема 4'
};

// Команда старт
bot.onText(/\/start/, async (msg) => {
    const chatId = msg.chat.id;
    
    const keyboard = {
        inline_keyboard: Object.entries(TOPICS).map(([id, name]) => [{
            text: name,
            callback_data: `topic_${id}`
        }])
    };
    
    await bot.sendMessage(chatId, 'Выберите тему:', { reply_markup: keyboard });
});

// Обработка выбора темы
bot.on('callback_query', async (query) => {
    const chatId = query.message.chat.id;
    const data = query.data;
    const userId = query.from.id;
    const messageId = query.message.message_id;
    
    if (data.startsWith('topic_')) {
        const topicId = data.replace('topic_', '');
        const topicName = TOPICS[topicId];
        
        userSettings.set(chatId, { topicId, topicName });
        
        await bot.editMessageText(`Тема: ${topicName}\nТеперь ваши сообщения будут отправляться в эту тему.`, {
            chat_id: chatId,
            message_id: messageId
        });
        
        await bot.answerCallbackQuery(query.id);
    }
    
    // Обработка кнопок под сообщениями в группе
    else if (data.startsWith('complain_')) {
        const originalMessageId = data.replace('complain_', '');
        
        // Инициализируем счетчик жалоб для сообщения
        if (!messageComplaints.has(originalMessageId)) {
            messageComplaints.set(originalMessageId, new Set());
        }
        
        const complaints = messageComplaints.get(originalMessageId);
        
        // Проверяем, не жаловался ли уже этот пользователь
        if (complaints.has(userId)) {
            await bot.answerCallbackQuery(query.id, { text: 'Вы уже пожаловались на это сообщение' });
            return;
        }
        
        complaints.add(userId);
        
        // Если жалоб 5 или больше - удаляем сообщение
        if (complaints.size >= 5) {
            try {
                await bot.deleteMessage(GROUP_ID, originalMessageId);
                await bot.answerCallbackQuery(query.id, { text: 'Сообщение удалено по жалобам' });
                messageComplaints.delete(originalMessageId);
            } catch (error) {
                await bot.answerCallbackQuery(query.id, { text: 'Не удалось удалить сообщение' });
            }
        } else {
            await bot.answerCallbackQuery(query.id, { text: `Жалоба принята (${complaints.size}/5)` });
        }
    }
    
    else if (data.startsWith('delete_')) {
        const originalMessageId = data.replace('delete_', '');
        
        try {
            // Удаляем панель модерации для пользователя
            await bot.deleteMessage(chatId, messageId);
            await bot.answerCallbackQuery(query.id, { text: 'Панель скрыта' });
        } catch (error) {
            await bot.answerCallbackQuery(query.id, { text: 'Не удалось скрыть панель' });
        }
    }
    
    else if (data.startsWith('forward_')) {
        const originalMessageId = data.replace('forward_', '');
        const groupIdNum = GROUP_ID.replace('-100', '');
        const messageLink = `https://t.me/c/${groupIdNum}/${originalMessageId}`;
        
        try {
            await bot.sendMessage(userId, `Ссылка на сообщение:\n${messageLink}`, {
                reply_markup: {
                    inline_keyboard: [[
                        { text: 'Переслать', url: `tg://msg_url?url=${encodeURIComponent(messageLink)}` }
                    ]]
                }
            });
            await bot.answerCallbackQuery(query.id, { text: 'Ссылка отправлена в личные сообщения' });
        } catch (error) {
            await bot.answerCallbackQuery(query.id, { text: 'Сначала напишите боту в личку' });
        }
    }
    
    else if (data.startsWith('write_')) {
        const authorId = data.replace('write_', '');
        
        try {
            await bot.sendMessage(userId, `Написать автору:`, {
                reply_markup: {
                    inline_keyboard: [[
                        { text: 'Открыть чат', url: `tg://user?id=${authorId}` }
                    ]]
                }
            });
            await bot.answerCallbackQuery(query.id, { text: 'Ссылка отправлена в личные сообщения' });
        } catch (error) {
            await bot.answerCallbackQuery(query.id, { text: 'Сначала напишите боту в личку' });
        }
    }
});

// Команда для получения chat ID (для настройки)
bot.onText(/\/id/, async (msg) => {
    await bot.sendMessage(msg.chat.id, `Chat ID: ${msg.chat.id}`);
});

// Команда для перезапуска (для решения конфликтов)
bot.onText(/\/restart/, async (msg) => {
    const chatId = msg.chat.id;
    await bot.sendMessage(chatId, 'Перезапуск...');
    process.exit(0);
});

// Команды для управления панелью модерации
bot.onText(/\/moderation (on|off)/, async (msg, match) => {
    const chatId = msg.chat.id;
    const action = match[1];
    
    // Проверяем, что команда в группе
    if (chatId.toString() !== GROUP_ID) {
        await bot.sendMessage(chatId, 'Команда работает только в настроенной группе');
        return;
    }
    
    // Проверяем права администратора
    try {
        const member = await bot.getChatMember(chatId, msg.from.id);
        if (!['creator', 'administrator'].includes(member.status)) {
            await bot.sendMessage(chatId, 'Только администраторы могут управлять панелью модерации');
            return;
        }
    } catch (error) {
        console.error('Ошибка проверки прав:', error);
        return;
    }
    
    const settings = groupSettings.get(chatId) || {};
    settings.moderationEnabled = (action === 'on');
    groupSettings.set(chatId, settings);
    
    await bot.sendMessage(chatId, `Панель модерации ${action === 'on' ? 'включена' : 'выключена'}`);
});

// Команда быстрой настройки тем
bot.onText(/\/setup (.+)/, async (msg, match) => {
    const chatId = msg.chat.id;
    const topicsInput = match[1];
    
    try {
        // Формат: /setup 27:Общий,28:Разработка,29:Дизайн
        const topics = {};
        topicsInput.split(',').forEach(item => {
            const [id, name] = item.split(':');
            if (id && name) {
                topics[id.trim()] = name.trim();
            }
        });
        
        if (Object.keys(topics).length > 0) {
            // Сохраняем настройки (в реальном проекте лучше в БД)
            Object.assign(TOPICS, topics);
            await bot.sendMessage(chatId, `Настроены темы:\n${Object.entries(topics).map(([id, name]) => `${id}: ${name}`).join('\n')}`);
        } else {
            await bot.sendMessage(chatId, 'Неверный формат. Используйте: /setup ID:Название,ID:Название');
        }
    } catch (error) {
        await bot.sendMessage(chatId, 'Ошибка настройки тем');
    }
});

// Добавление кнопок к сообщениям в группе
bot.on('message', async (msg) => {
    // Если сообщение в целевой группе и не от бота
    if (msg.chat.id.toString() === GROUP_ID && !msg.from.is_bot && !msg.text?.startsWith('/')) {
        try {
            const keyboard = {
                inline_keyboard: [[
                    { text: '⚠️', callback_data: `complain_${msg.message_id}` },
                    { text: '🗑', callback_data: `delete_${msg.message_id}` },
                    { text: '↗️', callback_data: `forward_${msg.message_id}` },
                    { text: '✉️', callback_data: `write_${msg.from.id}` }
                ]]
            };
            
            // Отправляем невидимое сообщение с кнопками
            await bot.sendMessage(GROUP_ID, '‎', {
                reply_to_message_id: msg.message_id,
                reply_markup: keyboard,
                parse_mode: 'HTML'
            });
        } catch (error) {
            console.error('Ошибка добавления кнопок:', error);
        }
        return;
    }
});

// Пересылка сообщений
bot.on('message', async (msg) => {
    // Игнорировать команды, callback запросы и сообщения из целевой группы
    if (msg.text?.startsWith('/') || msg.data || msg.chat.id.toString() === GROUP_ID) return;
    
    const chatId = msg.chat.id;
    const userConfig = userSettings.get(chatId);
    
    if (!userConfig) {
        await bot.sendMessage(chatId, 'Используйте /start для настройки');
        return;
    }
    
    try {
        const messageOptions = {
            message_thread_id: parseInt(userConfig.topicId)
        };
        
        // Пересылка разных типов сообщений
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
        }
        
        // Подтверждение отправки
        await bot.sendMessage(chatId, '✓', { 
            reply_to_message_id: msg.message_id 
        });
        
    } catch (error) {
        console.error('Ошибка пересылки:', error);
        
        let errorMsg = 'Ошибка отправки';
        if (error.message.includes('message thread not found')) {
            errorMsg = `Тема не найдена. Используйте /topics для настройки или /setup для быстрой настройки`;
        } else if (error.message.includes('chat not found')) {
            errorMsg = 'Группа не найдена. Проверьте GROUP_ID';
        }
        
        await bot.sendMessage(chatId, errorMsg);
    }
});

// Keep-alive: пинг каждые 25 минут
setInterval(async () => {
    try {
        await bot.getMe();
        console.log('Ping:', new Date().toISOString());
    } catch (error) {
        console.error('Ping error:', error);
    }
}, 25 * 60 * 1000);

// Обработка ошибок
bot.on('error', (error) => {
    console.error('Bot error:', error);
});

process.on('unhandledRejection', (error) => {
    console.error('Unhandled rejection:', error);
});

console.log('Бот запущен');
