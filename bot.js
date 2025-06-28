const TelegramBot = require('node-telegram-bot-api');
const express = require('express');

// Конфигурация из переменных окружения
const BOT_TOKEN = process.env.BOT_TOKEN;
const GROUP_ID = process.env.GROUP_ID;
const PORT = process.env.PORT || 3000;
const WEBHOOK_URL = process.env.WEBHOOK_URL; // https://your-app.onrender.com

// Инициализация бота без polling (используем webhook)
const bot = new TelegramBot(BOT_TOKEN);
const app = express();

// Хранилище пользовательских настроек и жалоб
const userSettings = new Map();
const messageComplaints = new Map(); // messageId -> Set(userIds)
const groupSettings = new Map(); // chatId -> { authorLinksEnabled: true/false }
const messageCache = new Map(); // messageId -> { author, content, timestamp }

// Список доступных тем (замените на реальные ID из вашей группы)
const TOPICS = {
    '27': 'Тема 1',  // Замените на ваши реальные ID и названия
    '28': 'Тема 2',  // Получите из ссылок тем
    '29': 'Тема 3',
    '30': 'Тема 4'
};

// Настройка webhook
app.use(express.json());

if (WEBHOOK_URL) {
    // Настраиваем webhook для продакшена
    bot.setWebHook(`${WEBHOOK_URL}/bot${BOT_TOKEN}`);
    
    app.post(`/bot${BOT_TOKEN}`, (req, res) => {
        bot.processUpdate(req.body);
        res.sendStatus(200);
    });
    
    console.log('Webhook настроен:', `${WEBHOOK_URL}/bot${BOT_TOKEN}`);
} else {
    // Для локальной разработки используем polling
    bot.startPolling();
    console.log('Запущен в режиме polling (локальная разработка)');
}

// Проверка здоровья для Render
app.get('/', (req, res) => {
    res.send('Telegram Bot работает!');
});

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

// Команда для получения chat ID
bot.onText(/\/id/, async (msg) => {
    await bot.sendMessage(msg.chat.id, `Chat ID: ${msg.chat.id}`);
});

// Команда быстрой настройки тем
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
            await bot.sendMessage(chatId, `Настроены темы:\n${Object.entries(topics).map(([id, name]) => `${id}: ${name}`).join('\n')}`);
        } else {
            await bot.sendMessage(chatId, 'Неверный формат. Используйте: /setup ID:Название,ID:Название');
        }
    } catch (error) {
        await bot.sendMessage(chatId, 'Ошибка настройки тем');
    }
});

// Команда для включения/выключения панели управления
bot.onText(/\/buttons (on|off)/, async (msg, match) => {
    if (msg.chat.id.toString() !== GROUP_ID) return;
    
    // Проверяем права администратора
    try {
        const member = await bot.getChatMember(msg.chat.id, msg.from.id);
        if (!['creator', 'administrator'].includes(member.status)) {
            await bot.sendMessage(msg.chat.id, 'Только администраторы могут управлять панелью', {
                reply_to_message_id: msg.message_id
            });
            return;
        }
    } catch (error) {
        console.error('Ошибка проверки прав:', error);
        return;
    }
    
    const action = match[1];
    const settings = groupSettings.get(msg.chat.id) || {};
    settings.buttonsEnabled = (action === 'on');
    groupSettings.set(msg.chat.id, settings);
    
    await bot.sendMessage(msg.chat.id, `Панель управления ${action === 'on' ? 'включена' : 'выключена'}`, {
        reply_to_message_id: msg.message_id
    });
    
    // Удаляем команду
    try {
        await bot.deleteMessage(msg.chat.id, msg.message_id);
    } catch (error) {}
});

// Команда для проверки статуса панели
bot.onText(/\/buttons_status/, async (msg) => {
    if (msg.chat.id.toString() !== GROUP_ID) return;
    
    const settings = groupSettings.get(msg.chat.id) || { buttonsEnabled: true };
    const status = settings.buttonsEnabled ? 'включена ✅' : 'выключена ❌';
    
    await bot.sendMessage(GROUP_ID, `Панель управления: ${status}`, {
        reply_to_message_id: msg.message_id
    });
    
    // Удаляем команду через 3 секунды
    setTimeout(async () => {
        try {
            await bot.deleteMessage(msg.chat.id, msg.message_id);
        } catch (error) {}
    }, 3000);
});

// Помощь по функциям бота
bot.onText(/\/help_buttons/, async (msg) => {
    if (msg.chat.id.toString() !== GROUP_ID) return;
    
    const helpText = `🔧 Панель управления сообщениями:

Под каждым сообщением автоматически появляются кнопки:
• Пожаловаться - при 5 жалобах сообщение удаляется
• Удалить для себя - скрывает панель управления
• Переслать - отправляет ссылку на сообщение в личку
• Написать автору - отправляет контакт автора в личку

🔗 Управление панелью (только админы):
/buttons on - Включить панель управления
/buttons off - Выключить панель управления
/buttons_status - Проверить статус панели

Все ссылки отправляются в личные сообщения с ботом.`;
    
    await bot.sendMessage(GROUP_ID, helpText, {
        reply_to_message_id: msg.message_id
    });
});

// Обработка выбора темы и кнопок модерации
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
    
    // Обработка кнопок модерации
    else if (data.startsWith('complain_')) {
        const botMessageId = data.replace('complain_', '');
        const cachedMessage = messageCache.get(botMessageId);
        
        if (!cachedMessage) {
            await bot.answerCallbackQuery(query.id, { text: 'Сообщение не найдено в кеше' });
            return;
        }
        
        if (!messageComplaints.has(botMessageId)) {
            messageComplaints.set(botMessageId, new Set());
        }
        
        const complaints = messageComplaints.get(botMessageId);
        
        if (complaints.has(userId)) {
            await bot.answerCallbackQuery(query.id, { text: 'Вы уже пожаловались на это сообщение' });
            return;
        }
        
        complaints.add(userId);
        
        if (complaints.size >= 5) {
            try {
                // Удаляем сообщение от бота (которое содержит контент пользователя)
                await bot.deleteMessage(GROUP_ID, botMessageId);
                messageComplaints.delete(botMessageId);
                messageCache.delete(botMessageId);
                await bot.answerCallbackQuery(query.id, { text: 'Сообщение удалено по жалобам' });
            } catch (error) {
                await bot.answerCallbackQuery(query.id, { text: 'Не удалось удалить сообщение' });
            }
        } else {
            await bot.answerCallbackQuery(query.id, { text: `Жалоба принята (${complaints.size}/5)` });
            
            // Обновляем кнопки с новым счетчиком
            const keyboard = {
                inline_keyboard: [[
                    { text: `Пожаловаться (${complaints.size}/5)`, callback_data: `complain_${botMessageId}` },
                    { text: 'Удалить для себя', callback_data: `delete_${botMessageId}` }
                ], [
                    { text: 'Переслать', callback_data: `forward_${botMessageId}` },
                    { text: 'Написать автору', callback_data: `write_${botMessageId}` }
                ]]
            };
            
            try {
                await bot.editMessageReplyMarkup(keyboard, {
                    chat_id: chatId,
                    message_id: messageId
                });
            } catch (error) {}
        }
    }
    
    else if (data.startsWith('delete_')) {
        try {
            // Удаляем сообщение от бота (с кнопками)
            await bot.deleteMessage(chatId, messageId);
            await bot.answerCallbackQuery(query.id, { text: 'Сообщение скрыто для вас' });
        } catch (error) {
            await bot.answerCallbackQuery(query.id, { text: 'Не удалось скрыть сообщение' });
        }
    }
    
    else if (data.startsWith('forward_')) {
        const botMessageId = data.replace('forward_', '');
        const cachedMessage = messageCache.get(botMessageId);
        
        if (!cachedMessage || !cachedMessage.originalMessageId) {
            await bot.answerCallbackQuery(query.id, { text: 'Ссылка недоступна' });
            return;
        }
        
        const groupIdNum = GROUP_ID.replace('-100', '');
        const messageLink = `https://t.me/c/${groupIdNum}/${cachedMessage.originalMessageId}`;
        
        try {
            await bot.sendMessage(userId, `Ссылка на сообщение:\n${messageLink}`);
            await bot.answerCallbackQuery(query.id, { text: 'Ссылка отправлена в личные сообщения' });
        } catch (error) {
            await bot.answerCallbackQuery(query.id, { text: 'Сначала напишите боту в личку' });
        }
    }
    
    else if (data.startsWith('write_')) {
        const botMessageId = data.replace('write_', '');
        const cachedMessage = messageCache.get(botMessageId);
        
        if (!cachedMessage) {
            await bot.answerCallbackQuery(query.id, { text: 'Информация об авторе не найдена' });
            return;
        }
        
        const author = cachedMessage.author;
        let authorLink = '';
        
        if (author.username) {
            authorLink = `@${author.username}\nhttps://t.me/${author.username}`;
        } else {
            authorLink = `${author.first_name}\ntg://user?id=${author.id}`;
        }
        
        try {
            await bot.sendMessage(userId, `Написать автору:\n${authorLink}`);
            await bot.answerCallbackQuery(query.id, { text: 'Ссылка на автора отправлена в личку' });
        } catch (error) {
            await bot.answerCallbackQuery(query.id, { text: 'Сначала напишите боту в личку' });
        }
    }
});

// Пересылка сообщений и панель управления
bot.on('message', async (msg) => {
    // Перехват и пересылка сообщений в группе от имени бота
    if (msg.chat.id.toString() === GROUP_ID && !msg.from.is_bot && !msg.text?.startsWith('/')) {
        const settings = groupSettings.get(msg.chat.id) || { buttonsEnabled: true };
        
        if (settings.buttonsEnabled) {
            try {
                // Сохраняем оригинальное сообщение в кеш
                const originalMessageId = msg.message_id;
                messageCache.set(originalMessageId, {
                    author: {
                        id: msg.from.id,
                        username: msg.from.username,
                        first_name: msg.from.first_name,
                        last_name: msg.from.last_name
                    },
                    content: msg.text || 'Медиа',
                    timestamp: Date.now()
                });
                
                // Создаем панель управления
                const keyboard = {
                    inline_keyboard: [[
                        { text: 'Пожаловаться', callback_data: `complain_${originalMessageId}` },
                        { text: 'Удалить для себя', callback_data: `delete_${originalMessageId}` }
                    ], [
                        { text: 'Переслать', callback_data: `forward_${originalMessageId}` },
                        { text: 'Написать автору', callback_data: `write_${originalMessageId}` }
                    ]]
                };
                
                // Определяем имя автора для отображения
                let authorName = msg.from.first_name || 'Пользователь';
                if (msg.from.last_name) {
                    authorName += ` ${msg.from.last_name}`;
                }
                if (msg.from.username) {
                    authorName = `${authorName} (@${msg.from.username})`;
                }
                
                let sentMessage;
                
                // Пересылаем сообщение от имени бота в зависимости от типа
                if (msg.text) {
                    sentMessage = await bot.sendMessage(GROUP_ID, `${authorName}:\n\n${msg.text}`, {
                        reply_markup: keyboard,
                        parse_mode: 'HTML'
                    });
                } else if (msg.photo) {
                    sentMessage = await bot.sendPhoto(GROUP_ID, msg.photo[msg.photo.length - 1].file_id, {
                        caption: `${authorName}:\n\n${msg.caption || ''}`,
                        reply_markup: keyboard,
                        parse_mode: 'HTML'
                    });
                } else if (msg.document) {
                    sentMessage = await bot.sendDocument(GROUP_ID, msg.document.file_id, {
                        caption: `${authorName}:\n\n${msg.caption || ''}`,
                        reply_markup: keyboard,
                        parse_mode: 'HTML'
                    });
                } else if (msg.video) {
                    sentMessage = await bot.sendVideo(GROUP_ID, msg.video.file_id, {
                        caption: `${authorName}:\n\n${msg.caption || ''}`,
                        reply_markup: keyboard,
                        parse_mode: 'HTML'
                    });
                } else if (msg.voice) {
                    sentMessage = await bot.sendVoice(GROUP_ID, msg.voice.file_id, {
                        caption: `${authorName}:`,
                        reply_markup: keyboard,
                        parse_mode: 'HTML'
                    });
                } else if (msg.sticker) {
                    // Для стикеров отправляем отдельное сообщение с информацией об авторе
                    await bot.sendSticker(GROUP_ID, msg.sticker.file_id);
                    sentMessage = await bot.sendMessage(GROUP_ID, `${authorName} отправил стикер`, {
                        reply_markup: keyboard,
                        parse_mode: 'HTML'
                    });
                } else {
                    // Для других типов медиа
                    sentMessage = await bot.sendMessage(GROUP_ID, `${authorName} отправил медиа`, {
                        reply_markup: keyboard,
                        parse_mode: 'HTML'
                    });
                }
                
                // Обновляем кеш с новым ID сообщения от бота
                if (sentMessage) {
                    messageCache.delete(originalMessageId);
                    messageCache.set(sentMessage.message_id, {
                        author: {
                            id: msg.from.id,
                            username: msg.from.username,
                            first_name: msg.from.first_name,
                            last_name: msg.from.last_name
                        },
                        content: msg.text || msg.caption || 'Медиа',
                        timestamp: Date.now(),
                        originalMessageId: originalMessageId
                    });
                    
                    // Обновляем callback data на новый message_id
                    const newKeyboard = {
                        inline_keyboard: [[
                            { text: 'Пожаловаться', callback_data: `complain_${sentMessage.message_id}` },
                            { text: 'Удалить для себя', callback_data: `delete_${sentMessage.message_id}` }
                        ], [
                            { text: 'Переслать', callback_data: `forward_${sentMessage.message_id}` },
                            { text: 'Написать автору', callback_data: `write_${sentMessage.message_id}` }
                        ]]
                    };
                    
                    await bot.editMessageReplyMarkup(newKeyboard, {
                        chat_id: GROUP_ID,
                        message_id: sentMessage.message_id
                    });
                }
                
                // Удаляем оригинальное сообщение пользователя
                try {
                    await bot.deleteMessage(GROUP_ID, originalMessageId);
                } catch (error) {
                    console.error('Не удалось удалить оригинальное сообщение:', error);
                }
                
            } catch (error) {
                console.error('Ошибка пересылки сообщения от бота:', error);
            }
        }
        return;
    }
    
    // Игнорировать команды и сообщения из целевой группы для пересылки в темы
    if (msg.text?.startsWith('/') || msg.chat.id.toString() === GROUP_ID) return;
    
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
        
        // Пересылка разных типов сообщений в темы
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
            errorMsg = `Тема не найдена. Используйте /setup для настройки`;
        } else if (error.message.includes('chat not found')) {
            errorMsg = 'Группа не найдена. Проверьте GROUP_ID';
        }
        
        await bot.sendMessage(chatId, errorMsg);
    }
});

// Очистка кеша каждые 30 минут (удаляем сообщения старше 1 часа)
setInterval(() => {
    const now = Date.now();
    const oneHour = 60 * 60 * 1000;
    
    for (const [messageId, data] of messageCache.entries()) {
        if (now - data.timestamp > oneHour) {
            messageCache.delete(messageId);
            messageComplaints.delete(messageId);
        }
    }
    
    console.log(`Кеш очищен. Сообщений в кеше: ${messageCache.size}`);
}, 30 * 60 * 1000);

// Обработка ошибок
bot.on('error', (error) => {
    console.error('Bot error:', error);
});

process.on('unhandledRejection', (error) => {
    console.error('Unhandled rejection:', error);
});

// Запуск сервера
app.listen(PORT, () => {
    console.log(`Бот запущен на порту ${PORT}`);
});
