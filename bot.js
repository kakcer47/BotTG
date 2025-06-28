const TelegramBot = require('node-telegram-bot-api');

// Конфигурация из переменных окружения
const BOT_TOKEN = process.env.BOT_TOKEN;
const GROUP_ID = process.env.GROUP_ID;

const bot = new TelegramBot(BOT_TOKEN, { polling: true });

// Хранилище пользовательских настроек
const userSettings = new Map();

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

// Команда для получения списка тем группы
bot.onText(/\/topics/, async (msg) => {
    const chatId = msg.chat.id;
    try {
        // Попробуем отправить тестовое сообщение без thread_id чтобы проверить доступ
        await bot.sendMessage(GROUP_ID, 'Тест доступа к группе');
        await bot.sendMessage(chatId, `Группа ID: ${GROUP_ID}\nДля получения ID тем:\n1. Откройте каждую тему в группе\n2. Скопируйте ссылку\n3. Последнее число в ссылке = ID темы\n\nПример: t.me/c/xxx/27 → ID темы: 27`);
    } catch (error) {
        await bot.sendMessage(chatId, `Ошибка доступа к группе: ${error.message}`);
    }
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
