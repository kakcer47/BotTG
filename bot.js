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

// Команды модерации (в ответ на сообщения)
bot.onText(/\/complain/, async (msg) => {
    if (msg.chat.id.toString() !== GROUP_ID || !msg.reply_to_message) return;
    
    const originalMessageId = msg.reply_to_message.message_id;
    const userId = msg.from.id;
    
    if (!messageComplaints.has(originalMessageId)) {
        messageComplaints.set(originalMessageId, new Set());
    }
    
    const complaints = messageComplaints.get(originalMessageId);
    
    if (complaints.has(userId)) {
        await bot.sendMessage(GROUP_ID, 'Вы уже жаловались на это сообщение', {
            reply_to_message_id: msg.message_id
        });
        return;
    }
    
    complaints.add(userId);
    
    if (complaints.size >= 5) {
        try {
            await bot.deleteMessage(GROUP_ID, originalMessageId);
            await bot.sendMessage(GROUP_ID, `Сообщение удалено по жалобам (${complaints.size} жалоб)`, {
                reply_to_message_id: msg.message_id
            });
            messageComplaints.delete(originalMessageId);
        } catch (error) {
            await bot.sendMessage(GROUP_ID, 'Не удалось удалить сообщение', {
                reply_to_message_id: msg.message_id
            });
        }
    } else {
        await bot.sendMessage(GROUP_ID, `Жалоба принята (${complaints.size}/5)`, {
            reply_to_message_id: msg.message_id
        });
    }
    
    // Удаляем команду жалобы
    try {
        await bot.deleteMessage(GROUP_ID, msg.message_id);
    } catch (error) {}
});

bot.onText(/\/share/, async (msg) => {
    if (msg.chat.id.toString() !== GROUP_ID || !msg.reply_to_message) return;
    
    const originalMessageId = msg.reply_to_message.message_id;
    const groupIdNum = GROUP_ID.replace('-100', '');
    const messageLink = `https://t.me/c/${groupIdNum}/${originalMessageId}`;
    
    try {
        await bot.sendMessage(msg.from.id, `Ссылка на сообщение:\n${messageLink}`);
        await bot.sendMessage(GROUP_ID, 'Ссылка отправлена в личные сообщения', {
            reply_to_message_id: msg.message_id
        });
    } catch (error) {
        await bot.sendMessage(GROUP_ID, 'Сначала напишите боту в личку: @' + (await bot.getMe()).username, {
            reply_to_message_id: msg.message_id
        });
    }
    
    // Удаляем команду
    try {
        await bot.deleteMessage(GROUP_ID, msg.message_id);
    } catch (error) {}
});

bot.onText(/\/author/, async (msg) => {
    if (msg.chat.id.toString() !== GROUP_ID || !msg.reply_to_message) return;
    
    const authorId = msg.reply_to_message.from.id;
    const authorName = msg.reply_to_message.from.first_name;
    
    try {
        await bot.sendMessage(msg.from.id, `Написать автору: @${authorName}\ntg://user?id=${authorId}`);
        await bot.sendMessage(GROUP_ID, 'Ссылка на автора отправлена в личные сообщения', {
            reply_to_message_id: msg.message_id
        });
    } catch (error) {
        await bot.sendMessage(GROUP_ID, 'Сначала напишите боту в личку: @' + (await bot.getMe()).username, {
            reply_to_message_id: msg.message_id
        });
    }
    
    // Удаляем команду
    try {
        await bot.deleteMessage(GROUP_ID, msg.message_id);
    } catch (error) {}
});

// Помощь по командам модерации
bot.onText(/\/help_moderation/, async (msg) => {
    if (msg.chat.id.toString() !== GROUP_ID) return;
    
    const helpText = `🔧 Команды модерации (в ответ на сообщения):

/complain - Пожаловаться (при 5 жалобах сообщение удаляется)
/share - Получить ссылку на сообщение
/author - Связаться с автором сообщения

Используйте эти команды в ответ на нужное сообщение.`;
    
    await bot.sendMessage(GROUP_ID, helpText, {
        reply_to_message_id: msg.message_id
    });
});

// Обработка выбора темы
bot.on('callback_query', async (query) => {
    const chatId = query.message.chat.id;
    const data = query.data;
    
    if (data.startsWith('topic_')) {
        const topicId = data.replace('topic_', '');
        const topicName = TOPICS[topicId];
        
        userSettings.set(chatId, { topicId, topicName });
        
        await bot.editMessageText(`Тема: ${topicName}\nТеперь ваши сообщения будут отправляться в эту тему.`, {
            chat_id: chatId,
            message_id: query.message.message_id
        });
        
        await bot.answerCallbackQuery(query.id);
    }
});

// Пересылка сообщений
bot.on('message', async (msg) => {
    // Игнорировать команды и сообщения из целевой группы
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
            errorMsg = `Тема не найдена. Используйте /setup для настройки`;
        } else if (error.message.includes('chat not found')) {
            errorMsg = 'Группа не найдена. Проверьте GROUP_ID';
        }
        
        await bot.sendMessage(chatId, errorMsg);
    }
});

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
