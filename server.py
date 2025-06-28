const TelegramBot = require('node-telegram-bot-api');

// Конфигурация из переменных окружения
const BOT_TOKEN = process.env.BOT_TOKEN;
const GROUP_ID = process.env.GROUP_ID;

const bot = new TelegramBot(BOT_TOKEN, { polling: true });

// Хранилище пользовательских настроек
const userSettings = new Map();

// Список доступных тем (message_thread_id)
const TOPICS = {
    '1': 'Общий',
    '2': 'Разработка', 
    '3': 'Дизайн',
    '4': 'Маркетинг'
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

// Команда для получения chat ID (для настройки)
bot.onText(/\/id/, async (msg) => {
    await bot.sendMessage(msg.chat.id, `Chat ID: ${msg.chat.id}`);
});

// Пересылка сообщений
bot.on('message', async (msg) => {
    // Игнорировать команды и callback запросы
    if (msg.text?.startsWith('/') || msg.data) return;
    
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
        await bot.sendMessage(chatId, 'Ошибка отправки');
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
