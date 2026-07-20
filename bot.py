import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes, ConversationHandler
from datetime import datetime, timedelta
from collections import defaultdict
import html
import requests
import json
import pickle
import os
import asyncio
import re
from typing import Dict, List, Set

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Настройки
BOT_TOKEN = "8155852297:AAGIvZskPEacRTlgYL8TaS0hb8EhoPHo58E"
ADMIN_ID = 5274157154  # Ваш ID
DEEPSEEK_API_KEY = "sk-f8fe99fc78514aa2a303a053fe0ae5a7"  # Замените на ваш API ключ
DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"

# Состояния для ConversationHandler
TICKET_SUBJECT, TICKET_DESCRIPTION, AI_REPLY = range(3)

# Файлы для сохранения данных
TICKETS_FILE = "tickets.pkl"
USER_TICKETS_FILE = "user_tickets.pkl"
OPEN_TICKETS_FILE = "open_tickets.pkl"
SPAM_ATTEMPTS_FILE = "spam_attempts.pkl"

class SupportBot:
    def __init__(self):
        self.app = Application.builder().token(BOT_TOKEN).build()
        self.load_data()
        self.setup_handlers()
        
        # Запускаем задачу для ежедневной очистки переписки
        self.cleanup_task = None

    def load_data(self):
        """Загружает данные из файлов"""
        try:
            if os.path.exists(TICKETS_FILE):
                with open(TICKETS_FILE, 'rb') as f:
                    self.tickets = pickle.load(f)
            else:
                self.tickets = {}
                
            if os.path.exists(USER_TICKETS_FILE):
                with open(USER_TICKETS_FILE, 'rb') as f:
                    self.user_tickets = pickle.load(f)
            else:
                self.user_tickets = defaultdict(list)
                
            if os.path.exists(OPEN_TICKETS_FILE):
                with open(OPEN_TICKETS_FILE, 'rb') as f:
                    self.open_tickets = pickle.load(f)
            else:
                self.open_tickets = set()
                
            if os.path.exists(SPAM_ATTEMPTS_FILE):
                with open(SPAM_ATTEMPTS_FILE, 'rb') as f:
                    self.user_spam_attempts = pickle.load(f)
            else:
                self.user_spam_attempts = defaultdict(int)
                
        except Exception as e:
            logger.error(f"Ошибка при загрузке данных: {e}")
            self.tickets = {}
            self.user_tickets = defaultdict(list)
            self.open_tickets = set()
            self.user_spam_attempts = defaultdict(int)

    def save_data(self):
        """Сохраняет данные в файлы"""
        try:
            with open(TICKETS_FILE, 'wb') as f:
                pickle.dump(self.tickets, f)
                
            with open(USER_TICKETS_FILE, 'wb') as f:
                pickle.dump(dict(self.user_tickets), f)
                
            with open(OPEN_TICKETS_FILE, 'wb') as f:
                pickle.dump(self.open_tickets, f)
                
            with open(SPAM_ATTEMPTS_FILE, 'wb') as f:
                pickle.dump(dict(self.user_spam_attempts), f)
                
        except Exception as e:
            logger.error(f"Ошибка при сохранении данных: {e}")

    def setup_handlers(self):
        # Обработчики команд
        self.app.add_handler(CommandHandler("start", self.start))
        self.app.add_handler(CommandHandler("help", self.help_command))
        self.app.add_handler(CommandHandler("admin", self.admin_panel))
        self.app.add_handler(CommandHandler("stats", self.stats))
        self.app.add_handler(CommandHandler("mytickets", self.show_user_tickets))
        
        # Обработчик для создания тикетов
        conv_handler = ConversationHandler(
            entry_points=[CommandHandler("create", self.create_ticket),
                         CallbackQueryHandler(self.create_ticket, pattern="^create_ticket$")],
            states={
                TICKET_SUBJECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.get_ticket_subject)],
                TICKET_DESCRIPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.get_ticket_description)],
            },
            fallbacks=[CommandHandler("cancel", self.cancel)],
        )
        self.app.add_handler(conv_handler)
        
        # Обработчики callback
        self.app.add_handler(CallbackQueryHandler(self.button_handler))
        
        # Обработчик сообщений
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))
        
        # Обработчик для автоматического /start
        self.app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, self.welcome_new_user))
        
        # Добавляем обработчик ошибок
        self.app.add_error_handler(self.error_handler)

    async def error_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обрабатывает ошибки бота"""
        logger.error(f"Exception while handling an update: {context.error}", exc_info=context.error)
        
        # Игнорируем ошибку "Message is not modified"
        if "Message is not modified" in str(context.error):
            return
        
        # Для других ошибок можно отправить сообщение пользователю
        try:
            if update and update.effective_user:
                await context.bot.send_message(
                    chat_id=update.effective_user.id,
                    text="❌ Произошла ошибка. Пожалуйста, попробуйте еще раз."
                )
        except Exception as e:
            logger.error(f"Ошибка при отправке сообщения об ошибке: {e}")

    async def welcome_new_user(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Приветствует новых пользователей автоматически"""
        for user in update.message.new_chat_members:
            if user.is_bot and user.id == context.bot.id:
                # Бот добавлен в чат
                await self.start(update, context)
            elif not user.is_bot:
                # Новый пользователь
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text=f"👋 Добро пожаловать, {user.first_name}!",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🚀 Начать", callback_data="start")]
                    ])
                )

    def is_clearly_spam(self, text: str) -> bool:
        """Проверяет явный спам без использования AI"""
        text_lower = text.lower()
        
        # Очевидные спам-фразы
        spam_phrases = [
            'купить', 'продать', 'заказать', 'скидка', 'акция', 'распродажа',
            'http://', 'https://', 'www.', '.com', '.ru', '.net',
            'заработок', 'бизнес', 'инвестиц', 'криптовалют',
            'рассылка', 'распространить', 'реклам', 'бесплатно',
            'партнерк', 'маркетинг', 'продвижен'
        ]
        
        if any(phrase in text_lower for phrase in spam_phrases):
            return True
            
        # Множественные ссылки
        url_pattern = r'http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\\(\\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+'
        urls = re.findall(url_pattern, text)
        if len(urls) > 2:
            return True
            
        return False

    def is_gibberish(self, text: str) -> bool:
        """
        Проверяет, является ли текст бессмысленным набором символов
        """
        # Удаляем пробелы и приводим к нижнему регистру
        clean_text = text.replace(' ', '').lower()
        
        if len(clean_text) < 8:
            return False
            
        # Проверяем на повторяющиеся последовательности
        if any(clean_text.count(substr) > len(clean_text) // 2 for substr in [clean_text[i:i+3] for i in range(len(clean_text)-2)]):
            return True
            
        # Проверяем на отсутствие гласных в длинных последовательностях
        vowels = 'аеёиоуыэюяaeiou'
        consonant_streak = 0
        for char in clean_text:
            if char in vowels:
                consonant_streak = 0
            else:
                consonant_streak += 1
                if consonant_streak > 10:
                    return True
                    
        # Проверяем соотношение уникальных символов
        unique_chars = len(set(clean_text))
        if unique_chars < len(clean_text) * 0.3:
            return True
            
        return False

    async def ask_ai_for_help(self, ticket_data: dict, user_message: str = None) -> tuple:
        """
        Запрашивает у ИИ ответ на тикет.
        Возвращает (ответ_ии, флаг_эскалации_оператору)
        """
        try:
            # Формируем контекст для ИИ
            context_messages = []
            
            # Добавляем историю переписки
            for msg in ticket_data.get('messages', [])[-5:]:  # Последние 5 сообщений
                sender = "Пользователь" if msg['from_user'] else "Оператор"
                context_messages.append(f"{sender}: {msg['text']}")
            
            # Добавляем описание проблемы
            problem_text = f"Тема: {ticket_data['subject']}\nОписание: {ticket_data['description']}"
            
            # Системный промпт
            system_prompt = """Ты - ИИ-ассистент технической поддержки. Твоя задача - помочь пользователю решить его проблему.
            
Правила:
1. Отвечай вежливо и профессионально
2. Если проблема сложная или требует действий оператора - напиши в конце "ТРЕБУЕТСЯ ОПЕРАТОР"
3. Давай конкретные, полезные советы
4. Если не знаешь ответа - честно скажи об этом и передай оператору
5. Отвечай на русском языке

Тикеты, требующие оператора:
- Финансовые вопросы
- Технические сбои сервера
- Доступ к личным данным
- Проблемы с оплатой
- Блокировка аккаунта
- Юридические вопросы"""

            user_message_for_ai = f"""
Проблема пользователя:
{problem_text}

История переписки:
{chr(10).join(context_messages) if context_messages else 'Нет истории'}

Дополнительное сообщение пользователя: {user_message if user_message else 'Нет'}

Ответь на русском языке. Если проблема требует оператора, укажи это в конце. Помни: если это финансовые вопросы, технические сбои, доступ к данным, оплата, блокировка или юридические вопросы - передай оператору!"""

            # Запрос к DeepSeek API
            headers = {
                "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                "Content-Type": "application/json"
            }
            
            payload = {
                "model": "deepseek-chat",
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message_for_ai}
                ],
                "max_tokens": 800,
                "temperature": 0.7
            }
            
            # Добавляем таймаут
            response = requests.post(
                DEEPSEEK_API_URL,
                headers=headers,
                json=payload,
                timeout=15
            )
            
            if response.status_code == 200:
                result = response.json()
                ai_response = result['choices'][0]['message']['content']
                
                # Проверяем, требует ли ответ оператора
                requires_operator = any(phrase in ai_response.lower() for phrase in [
                    'требуется оператор',
                    'передать оператору',
                    'оператору',
                    'требуется помощь оператора',
                    'свяжитесь с оператором',
                    'администратору',
                    'технический специалист'
                ])
                
                # Также проверяем по ключевым словам в проблеме
                critical_keywords = ['финанс', 'оплат', 'денег', 'блокировк', 'доступ', 'юридическ', 'судебн', 
                                   'личные данные', 'сервер не работ', 'сбой', 'ошибк', 'взлом']
                
                problem_text_lower = problem_text.lower()
                for keyword in critical_keywords:
                    if keyword in problem_text_lower:
                        requires_operator = True
                        break
                
                return ai_response, requires_operator
            else:
                logger.error(f"Ошибка API: {response.status_code} - {response.text}")
                return None, True
                
        except requests.exceptions.Timeout:
            logger.error("Таймаут при запросе к AI")
            return None, True
        except Exception as e:
            logger.error(f"Ошибка при обращении к AI: {e}")
            return None, True

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        
        # Создаем клавиатуру с кнопками (без кнопки выхода)
        keyboard = [
            [InlineKeyboardButton("🎫 Создать тикет", callback_data="create_ticket")],
            [InlineKeyboardButton("📋 Мои тикеты", callback_data="my_tickets")],
            [InlineKeyboardButton("📋 Помощь", callback_data="help")]
        ]
        
        # Если это админ, добавляем админ-кнопки
        if user.id == ADMIN_ID:
            keyboard.append([InlineKeyboardButton("👨‍💼 Панель админа", callback_data="admin_panel")])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        welcome_text = f"""
✨ <b>Добро пожаловать, {user.first_name}!</b>

🤖 <b>Я бот технической поддержки</b>

🚀 <b>Здесь вы сможете:</b>
• 🎫 Создавать тикеты для решения проблем
• 📋 Просматривать историю своих обращений
• 💬 Общаться с поддержкой

💡 <b>Для начала работы нажмите кнопку ниже</b>
        """
        
        if update.callback_query:
            await update.callback_query.edit_message_text(
                welcome_text, 
                reply_markup=reply_markup,
                parse_mode='HTML'
            )
        else:
            await update.message.reply_text(
                welcome_text, 
                reply_markup=reply_markup,
                parse_mode='HTML'
            )

    async def show_user_tickets(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        user_id = user.id
        
        if user_id not in self.user_tickets or not self.user_tickets[user_id]:
            keyboard = [
                [InlineKeyboardButton("🎫 Создать тикет", callback_data="create_ticket")],
                [InlineKeyboardButton("🔙 На главную", callback_data="back_to_start")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            if update.callback_query:
                await update.callback_query.edit_message_text(
                    "📭 <b>У вас пока нет тикетов</b>\n\nСоздайте первый тикет для обращения в поддержку!",
                    parse_mode='HTML',
                    reply_markup=reply_markup
                )
            else:
                await update.message.reply_text(
                    "📭 <b>У вас пока нет тикетов</b>\n\nСоздайте первый тикет для обращения в поддержку!",
                    parse_mode='HTML',
                    reply_markup=reply_markup
                )
            return
        
        tickets_list = self.user_tickets[user_id]
        keyboard = []
        
        for ticket_id in tickets_list[-10:]:
            ticket = self.tickets.get(ticket_id)
            if ticket:
                status_emoji = "🔓" if ticket['status'] == 'open' else "✅"
                time_ago = self.get_time_ago(datetime.fromisoformat(ticket['created_at']))
                btn_text = f"{status_emoji} {time_ago} - {ticket['subject'][:20]}..."
                keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"user_view_{ticket_id}")])
        
        keyboard.append([InlineKeyboardButton("🔙 На главную", callback_data="back_to_start")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        if update.callback_query:
            await update.callback_query.edit_message_text(
                "📋 <b>Ваши тикеты:</b>\n\nВыберите тикет для просмотра:",
                parse_mode='HTML',
                reply_markup=reply_markup
            )
        else:
            await update.message.reply_text(
                "📋 <b>Ваши тикеты:</b>\n\nВыберите тикет для просмотра:",
                parse_mode='HTML',
                reply_markup=reply_markup
            )

    async def show_ticket_to_user(self, query, ticket_id):
        ticket_data = self.tickets.get(ticket_id)
        if not ticket_data or ticket_data['user_id'] != query.from_user.id:
            await query.answer("❌ Тикет не найден!", show_alert=True)
            return
        
        created_time = datetime.fromisoformat(ticket_data['created_at']).strftime('%d.%m.%Y %H:%M')
        status_text = "🔓 Открыт" if ticket_data['status'] == 'open' else "✅ Закрыт"
        
        message_text = f"""
🎫 <b>Ваш тикет:</b> <code>{ticket_id}</code>
📋 <b>Тема:</b> {html.escape(ticket_data['subject'])}
🕐 <b>Создан:</b> {created_time}
🔓 <b>Статус:</b> {status_text}

📝 <b>Описание:</b>
{html.escape(ticket_data['description'])}

💬 <b>История переписки:</b>
        """
        
        for msg in ticket_data.get('messages', []):
            sender = "👤 Вы" if msg['from_user'] else "👨‍💼 Поддержка"
            msg_time = datetime.fromisoformat(msg['timestamp']).strftime('%d.%m.%Y %H:%M')
            message_text += f"\n{sender} ({msg_time}):\n{html.escape(msg['text'])}\n"
        
        if not ticket_data.get('messages'):
            message_text += "\n📭 Сообщений пока нет"
        
        keyboard = [
            [InlineKeyboardButton("📋 Мои тикеты", callback_data="my_tickets")],
            [InlineKeyboardButton("🔙 На главную", callback_data="back_to_start")]
        ]
        
        if ticket_data['status'] == 'open':
            keyboard.insert(0, [InlineKeyboardButton("💬 Ответить", callback_data=f"user_reply_{ticket_id}")])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        try:
            await query.edit_message_text(
                message_text,
                reply_markup=reply_markup,
                parse_mode='HTML'
            )
        except Exception as e:
            if "Message is not modified" not in str(e):
                raise e

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        keyboard = [
            [InlineKeyboardButton("🎫 Создать тикет", callback_data="create_ticket")],
            [InlineKeyboardButton("📋 Мои тикеты", callback_data="my_tickets")],
            [InlineKeyboardButton("🔙 На главную", callback_data="back_to_start")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        help_text = """
📋 <b>Справка по боту техподдержки</b>

🎫 <b>Создание тикета:</b>
Нажмите кнопку "Создать тикет" и опишите вашу проблему максимально подробно.

📝 <b>Как правильно описать проблему:</b>
1. <b>Что произошло?</b> - Опишите ситуацию
2. <b>Когда случилось?</b> - Укажите время возникновения
3. <b>Детали проблемы</b> - Все что может помочь нам понять проблему

⏱ <b>Время ответа:</b>
Мы отвечаем в течение 24 часов в рабочее время.

💬 <b>После создания тикета:</b>
• Вы получите ID тикета
• Администратор свяжется с вами
• Вы можете отвечать на сообщения поддержки

📋 <b>Просмотр истории:</b>
Используйте команду /mytickets для просмотра всех ваших тикетов
        """
        
        if update.callback_query:
            await update.callback_query.edit_message_text(
                help_text, 
                parse_mode='HTML', 
                reply_markup=reply_markup
            )
        else:
            await update.message.reply_text(
                help_text, 
                parse_mode='HTML', 
                reply_markup=reply_markup
            )

    async def stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != ADMIN_ID:
            await update.message.reply_text("❌ У вас нет прав доступа.")
            return
            
        stats_text = f"""
📊 <b>Статистика бота</b>

🎫 Всего тикетов: <b>{len(self.tickets)}</b>
🔓 Открытых тикетов: <b>{len(self.open_tickets)}</b>
✅ Закрытых тикетов: <b>{len(self.tickets) - len(self.open_tickets)}</b>
👤 Уникальных пользователей: <b>{len(self.user_tickets)}</b>
        """
        await update.message.reply_text(stats_text, parse_mode='HTML')

    async def create_ticket(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        # Очищаем предыдущие данные
        context.user_data.pop('ticket_subject', None)
        
        if update.callback_query:
            query = update.callback_query
            await query.answer()
            await query.message.reply_text(
                "🎫 <b>Создание нового тикета</b>\n\n📋 <b>Введите заголовок тикета:</b>\n<code>Кратко опишите основную проблему</code>",
                parse_mode='HTML'
            )
        else:
            await update.message.reply_text(
                "🎫 <b>Создание нового тикета</b>\n\n📋 <b>Введите заголовок тикета:</b>\n<code>Кратко опишите основную проблему</code>",
                parse_mode='HTML'
            )
        
        return TICKET_SUBJECT

    async def get_ticket_subject(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        subject = update.message.text
        
        # Упрощенная проверка
        spam_attempts = self.user_spam_attempts.get(user.id, 0)
        
        if spam_attempts > 5:
            await update.message.reply_text(
                "❌ Слишком много попыток создания тикетов. Попробуйте позже.",
                parse_mode='HTML'
            )
            return TICKET_SUBJECT
        
        if len(subject.strip()) < 3:
            self.user_spam_attempts[user.id] = spam_attempts + 1
            self.save_data()
            await update.message.reply_text(
                "❌ Заголовок слишком короткий. Введите более подробный заголовок:",
                parse_mode='HTML'
            )
            return TICKET_SUBJECT
        
        if len(subject) > 200:
            await update.message.reply_text(
                "❌ Заголовок слишком длинный. Введите более короткий заголовок:",
                parse_mode='HTML'
            )
            return TICKET_SUBJECT
        
        # Проверка на спам
        if self.is_clearly_spam(subject):
            self.user_spam_attempts[user.id] = spam_attempts + 1
            self.save_data()
            await update.message.reply_text(
                "❌ Заголовок содержит недопустимый контент. Введите другой заголовок:",
                parse_mode='HTML'
            )
            return TICKET_SUBJECT
        
        # Проверка на бессмысленный текст
        if self.is_gibberish(subject):
            self.user_spam_attempts[user.id] = spam_attempts + 1
            self.save_data()
            await update.message.reply_text(
                "❌ Заголовок содержит бессмысленный текст. Введите другой заголовок:",
                parse_mode='HTML'
            )
            return TICKET_SUBJECT
        
        # Сбрасываем счетчик спама при успешном вводе
        self.user_spam_attempts[user.id] = 0
        self.save_data()
        
        context.user_data['ticket_subject'] = subject
        await update.message.reply_text(
            "📝 <b>Теперь опишите проблему подробно:</b>\n\n"
            "• <b>Что произошло?</b>\n"
            "• <b>Когда это случилось?</b>\n"
            "• <b>Все детали и подробности</b>\n\n"
            "<i>Опишите всё максимально подробно, это поможет нам быстрее решить вашу проблему</i>",
            parse_mode='HTML'
        )
        return TICKET_DESCRIPTION

    async def get_ticket_description(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        description = update.message.text
        
        # Упрощенная проверка описания
        spam_attempts = self.user_spam_attempts.get(user.id, 0)
        
        if spam_attempts > 5:
            await update.message.reply_text(
                "❌ Слишком много попыток. Попробуйте позже.",
                parse_mode='HTML'
            )
            return TICKET_DESCRIPTION
        
        if len(description.strip()) < 10:
            self.user_spam_attempts[user.id] = spam_attempts + 1
            self.save_data()
            await update.message.reply_text(
                "❌ Описание слишком короткое. Опишите проблему подробнее:",
                parse_mode='HTML'
            )
            return TICKET_DESCRIPTION
        
        if len(description) > 2000:
            await update.message.reply_text(
                "❌ Описание слишком длинное. Максимум 2000 символов:",
                parse_mode='HTML'
            )
            return TICKET_DESCRIPTION
        
        # Проверка на явный спам
        if self.is_clearly_spam(description):
            self.user_spam_attempts[user.id] = spam_attempts + 1
            self.save_data()
            await update.message.reply_text(
                "❌ Ваше сообщение содержит недопустимый контент. Опишите проблему корректно:",
                parse_mode='HTML'
            )
            return TICKET_DESCRIPTION
        
        # Проверка на бессмысленный текст
        if self.is_gibberish(description):
            self.user_spam_attempts[user.id] = spam_attempts + 1
            self.save_data()
            await update.message.reply_text(
                "❌ Сообщение содержит бессмысленный текст. Опишите проблему корректно:",
                parse_mode='HTML'
            )
            return TICKET_DESCRIPTION
        
        # Сбрасываем счетчик спама
        self.user_spam_attempts[user.id] = 0
        self.save_data()
        
        # Создаем тикет
        ticket_id = f"ticket_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{user.id}"
        subject = context.user_data.get('ticket_subject', 'Без темы')
        
        ticket_data = {
            'id': ticket_id,
            'user_id': user.id,
            'username': user.username or user.first_name,
            'user_full_name': user.full_name,
            'subject': subject,
            'description': description,
            'status': 'open',
            'created_at': datetime.now().isoformat(),
            'messages': [],
            'content_checked': True,
            'ai_processed': False  # Флаг, что ИИ уже обрабатывал этот тикет
        }
        
        # Сохраняем тикет
        self.tickets[ticket_id] = ticket_data
        self.user_tickets[user.id].append(ticket_id)
        self.open_tickets.add(ticket_id)
        self.save_data()
        
        # Пытаемся получить ответ от ИИ
        ai_response, requires_operator = await self.ask_ai_for_help(ticket_data)
        
        if ai_response and not requires_operator:
            # ИИ успешно ответил
            ticket_data['messages'].append({
                'text': ai_response,
                'from_user': False,
                'timestamp': datetime.now().isoformat(),
                'is_ai': True
            })
            ticket_data['ai_processed'] = True
            self.save_data()
            
            # Отправляем ответ ИИ пользователю
            keyboard = [
                [InlineKeyboardButton("💬 Ответить", callback_data=f"user_reply_{ticket_id}")],
                [InlineKeyboardButton("📋 Мои тикеты", callback_data="my_tickets")],
                [InlineKeyboardButton("🔙 На главную", callback_data="back_to_start")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.message.reply_text(
                f"🤖 <b>AI-помощник ответил на ваш тикет</b>\n\n{html.escape(ai_response)}\n\n"
                f"<i>Если ответ не помог, вы можете ответить на это сообщение - тогда подключится оператор.</i>",
                parse_mode='HTML',
                reply_markup=reply_markup
            )
            
            # Отправляем уведомление админу
            admin_message = f"""
🤖 <b>ИИ ответил на тикет</b>

🎫 <b>ID:</b> <code>{ticket_id}</code>
👤 <b>Пользователь:</b> {user.full_name} (@{user.username or 'нет_username'})
📋 <b>Тема:</b> {html.escape(subject)}

🤖 <b>Ответ ИИ:</b>
{html.escape(ai_response[:300])}{'...' if len(ai_response) > 300 else ''}

<i>Пользователь может ответить, если нужна помощь оператора</i>
            """
            
            try:
                admin_keyboard = [
                    [InlineKeyboardButton("👀 Посмотреть тикет", callback_data=f"admin_view_{ticket_id}")],
                    [InlineKeyboardButton("📊 Панель админа", callback_data="admin_panel")]
                ]
                admin_reply_markup = InlineKeyboardMarkup(admin_keyboard)
                
                await context.bot.send_message(
                    chat_id=ADMIN_ID,
                    text=admin_message,
                    parse_mode='HTML',
                    reply_markup=admin_reply_markup
                )
            except Exception as e:
                logger.error(f"Не удалось отправить сообщение админу: {e}")
            
        else:
            # ИИ не справился - передаем оператору
            if ai_response:
                # ИИ ответил, но требует оператора
                ticket_data['messages'].append({
                    'text': f"🤖 AI: {ai_response} (Требуется оператор)",
                    'from_user': False,
                    'timestamp': datetime.now().isoformat(),
                    'is_ai': True
                })
                self.save_data()
                
                await update.message.reply_text(
                    f"🤖 <b>AI-помощник попытался ответить:</b>\n\n{html.escape(ai_response)}\n\n"
                    f"🔄 <b>Проблема требует вмешательства оператора.</b>\n"
                    f"Скоро с вами свяжутся!",
                    parse_mode='HTML'
                )
            
            # Отправляем уведомление админу
            admin_message = f"""
🚨 <b>НОВЫЙ ТИКЕТ ТРЕБУЕТ ОПЕРАТОРА!</b>

🎫 <b>ID:</b> <code>{ticket_id}</code>
👤 <b>Пользователь:</b> {user.full_name} (@{user.username or 'нет_username'})
📋 <b>Тема:</b> {html.escape(subject)}
📝 <b>Описание:</b> {html.escape(description[:300])}{'...' if len(description) > 300 else ''}

⏰ <b>Время создания:</b> {datetime.now().strftime('%d.%m.%Y %H:%M')}

❓ <b>Причина эскалации:</b>
• ИИ не смог дать точный ответ
• Проблема требует личного участия оператора
            """
            
            try:
                admin_keyboard = [
                    [InlineKeyboardButton("👀 Посмотреть тикет", callback_data=f"admin_view_{ticket_id}")],
                    [InlineKeyboardButton("💬 Ответить", callback_data=f"admin_reply_{ticket_id}")],
                    [InlineKeyboardButton("📊 Панель админа", callback_data="admin_panel")]
                ]
                admin_reply_markup = InlineKeyboardMarkup(admin_keyboard)
                
                await context.bot.send_message(
                    chat_id=ADMIN_ID,
                    text=admin_message,
                    parse_mode='HTML',
                    reply_markup=admin_reply_markup
                )
            except Exception as e:
                logger.error(f"Не удалось отправить сообщение админу: {e}")
            
            # Сообщение пользователю
            user_keyboard = [
                [InlineKeyboardButton("📋 Мои тикеты", callback_data="my_tickets")],
                [InlineKeyboardButton("🎫 Новый тикет", callback_data="create_ticket")],
                [InlineKeyboardButton("🔙 На главную", callback_data="back_to_start")]
            ]
            user_reply_markup = InlineKeyboardMarkup(user_keyboard)
            
            await update.message.reply_text(
                f"✅ <b>Тикет успешно создан!</b>\n\n"
                f"🎫 <b>ID:</b> <code>{ticket_id}</code>\n"
                f"📋 <b>Тема:</b> {html.escape(subject)}\n\n"
                f"⏳ <b>Мы свяжемся с вами в ближайшее время!</b>\n"
                f"💬 <b>Ожидайте ответа от оператора поддержки</b>",
                parse_mode='HTML',
                reply_markup=user_reply_markup
            )
        
        # Очищаем данные
        context.user_data.pop('ticket_subject', None)
        
        return ConversationHandler.END

    async def cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        keyboard = [
            [InlineKeyboardButton("🎫 Создать тикет", callback_data="create_ticket")],
            [InlineKeyboardButton("🔙 На главную", callback_data="back_to_start")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        # Очищаем данные
        context.user_data.pop('ticket_subject', None)
        context.user_data.pop('ticket_description', None)
        
        await update.message.reply_text(
            "❌ <b>Создание тикета отменено.</b>",
            parse_mode='HTML',
            reply_markup=reply_markup
        )
        return ConversationHandler.END

    async def admin_panel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if user.id != ADMIN_ID:
            if update.callback_query:
                await update.callback_query.answer("❌ У вас нет прав доступа к админ-панели.", show_alert=True)
            else:
                await update.message.reply_text("❌ У вас нет прав доступа к админ-панели.")
            return
        
        if update.callback_query:
            query = update.callback_query
            await query.answer()
            message = query
        else:
            message = update.message
        
        keyboard = []
        
        # Показываем открытые тикеты
        open_tickets_list = list(self.open_tickets)
        if open_tickets_list:
            for ticket_id in open_tickets_list[:8]:
                ticket_data = self.tickets.get(ticket_id)
                if ticket_data:
                    time_ago = self.get_time_ago(datetime.fromisoformat(ticket_data['created_at']))
                    ai_label = "🤖 " if ticket_data.get('ai_processed', False) else ""
                    btn_text = f"{ai_label}🎫 {time_ago} - {ticket_data['subject'][:25]}..."
                    keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"admin_view_{ticket_id}")])
        else:
            keyboard.append([InlineKeyboardButton("✅ Все тикеты обработаны", callback_data="none")])
        
        keyboard.extend([
            [InlineKeyboardButton("📊 Статистика", callback_data="admin_stats")],
            [InlineKeyboardButton("👤 Пользователи", callback_data="admin_users")],
            [InlineKeyboardButton("🔄 Обновить", callback_data="admin_panel")]
        ])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        text = f"""
👨‍💼 <b>Панель администратора</b>

📊 <b>Статистика:</b>
• 🔓 Открытых тикетов: <b>{len(self.open_tickets)}</b>
• ✅ Закрытых тикетов: <b>{len(self.tickets) - len(self.open_tickets)}</b>
• 👤 Всего пользователей: <b>{len(self.user_tickets)}</b>
• 🤖 Обработано AI: <b>{sum(1 for t in self.tickets.values() if t.get('ai_processed', False))}</b>

🎫 <b>Открытые тикеты:</b>
        """
        
        try:
            if update.callback_query:
                await query.edit_message_text(text, parse_mode='HTML', reply_markup=reply_markup)
            else:
                await message.reply_text(text, parse_mode='HTML', reply_markup=reply_markup)
        except Exception as e:
            if "Message is not modified" not in str(e):
                raise e

    def get_time_ago(self, dt):
        now = datetime.now()
        diff = now - dt
        minutes = diff.total_seconds() // 60
        hours = minutes // 60
        
        if hours > 0:
            return f"{int(hours)}ч назад"
        elif minutes > 0:
            return f"{int(minutes)}м назад"
        else:
            return "только что"

    async def button_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        
        data = query.data
        
        if data == "create_ticket":
            await self.create_ticket(update, context)
            
        elif data == "help":
            await self.help_command(update, context)
            
        elif data == "back_to_start":
            await self.start(update, context)
            
        elif data == "admin_panel":
            await self.admin_panel(update, context)
            
        elif data == "admin_stats":
            await self.show_admin_stats(query)
            
        elif data == "admin_users":
            await self.show_admin_users(query)
            
        elif data == "my_tickets":
            await self.show_user_tickets_from_callback(query)
            
        elif data.startswith("user_view_"):
            ticket_id = data.replace("user_view_", "")
            await self.show_ticket_to_user(query, ticket_id)
            
        elif data.startswith("user_reply_"):
            ticket_id = data.replace("user_reply_", "")
            ticket_data = self.tickets.get(ticket_id)
            if ticket_data and ticket_data.get('ai_processed', False):
                # Если AI уже отвечал на тикет и пользователь отвечает - эскалируем оператору
                await query.edit_message_text(
                    f"💬 <b>Ответ на тикет {ticket_id}</b>\n\n"
                    f"⚠️ <b>Внимание!</b> На этот тикет уже отвечал AI-помощник.\n"
                    f"Ваш ответ будет направлен оператору для личного рассмотрения.\n\n"
                    f"Введите ваш ответ:",
                    parse_mode='HTML'
                )
            else:
                await query.edit_message_text(
                    f"💬 <b>Ответ на тикет {ticket_id}</b>\n\nВведите ваш ответ:",
                    parse_mode='HTML'
                )
            context.user_data['user_replying_to'] = ticket_id
            
        elif data.startswith("admin_view_"):
            ticket_id = data.replace("admin_view_", "")
            await self.show_ticket_to_admin(query, ticket_id)
        
        elif data.startswith("admin_close_"):
            ticket_id = data.replace("admin_close_", "")
            await self.close_ticket(query, ticket_id)
        
        elif data.startswith("admin_reply_"):
            ticket_id = data.replace("admin_reply_", "")
            context.user_data['admin_replying_to'] = ticket_id
            keyboard = [[InlineKeyboardButton("❌ Отмена", callback_data=f"admin_view_{ticket_id}")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(
                f"💬 <b>Ответ на тикет {ticket_id}</b>\n\nВведите ваш ответ:",
                parse_mode='HTML',
                reply_markup=reply_markup
            )
        
        elif data.startswith("admin_user_tickets_"):
            user_id = int(data.replace("admin_user_tickets_", ""))
            await self.show_user_tickets_to_admin(query, user_id)
        
        elif data == "none":
            await query.answer("🎉 Все тикеты обработаны!", show_alert=True)

    async def show_user_tickets_to_admin(self, query, user_id):
        """Показывает все тикеты конкретного пользователя администратору"""
        if user_id not in self.user_tickets or not self.user_tickets[user_id]:
            keyboard = [
                [InlineKeyboardButton("↩️ Назад", callback_data="admin_panel")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.edit_message_text(
                "📭 <b>У этого пользователя нет тикетов</b>",
                parse_mode='HTML',
                reply_markup=reply_markup
            )
            return
        
        # Получаем информацию о пользователе
        user_tickets = [self.tickets[tid] for tid in self.user_tickets[user_id] if tid in self.tickets]
        user_info = user_tickets[0] if user_tickets else {}
        username = user_info.get('username', 'Unknown')
        full_name = user_info.get('user_full_name', 'Unknown')
        
        tickets_list = self.user_tickets[user_id]
        keyboard = []
        
        for ticket_id in tickets_list[-15:]:
            ticket = self.tickets.get(ticket_id)
            if ticket:
                status_emoji = "🔓" if ticket['status'] == 'open' else "✅"
                time_ago = self.get_time_ago(datetime.fromisoformat(ticket['created_at']))
                ai_label = "🤖 " if ticket.get('ai_processed', False) else ""
                btn_text = f"{status_emoji} {ai_label}{time_ago} - {ticket['subject'][:20]}..."
                keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"admin_view_{ticket_id}")])
        
        keyboard.append([InlineKeyboardButton("↩️ Назад", callback_data="admin_panel")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        user_stats_text = f"""
👤 <b>Пользователь:</b> {full_name} (@{username})
📊 <b>Статистика:</b>
• 🎫 Всего тикетов: {len(tickets_list)}
• 🔓 Открытых: {sum(1 for t in user_tickets if t['status'] == 'open')}
• ✅ Закрытых: {sum(1 for t in user_tickets if t['status'] == 'closed')}

📋 <b>Тикеты пользователя:</b>
Выберите тикет для просмотра:
        """
        
        await query.edit_message_text(
            user_stats_text,
            parse_mode='HTML',
            reply_markup=reply_markup
        )

    async def show_user_tickets_from_callback(self, query):
        user_id = query.from_user.id
        
        if user_id not in self.user_tickets or not self.user_tickets[user_id]:
            keyboard = [
                [InlineKeyboardButton("🎫 Создать тикет", callback_data="create_ticket")],
                [InlineKeyboardButton("🔙 На главную", callback_data="back_to_start")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.edit_message_text(
                "📭 <b>У вас пока нет тикетов</b>\n\nСоздайте первый тикет для обращения в поддержку!",
                parse_mode='HTML',
                reply_markup=reply_markup
            )
            return
        
        tickets_list = self.user_tickets[user_id]
        keyboard = []
        
        for ticket_id in tickets_list[-10:]:
            ticket = self.tickets.get(ticket_id)
            if ticket:
                status_emoji = "🔓" if ticket['status'] == 'open' else "✅"
                time_ago = self.get_time_ago(datetime.fromisoformat(ticket['created_at']))
                btn_text = f"{status_emoji} {time_ago} - {ticket['subject'][:20]}..."
                keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"user_view_{ticket_id}")])
        
        keyboard.append([InlineKeyboardButton("🔙 На главную", callback_data="back_to_start")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            "📋 <b>Ваши тикеты:</b>\n\nВыберите тикет для просмотра:",
            parse_mode='HTML',
            reply_markup=reply_markup
        )

    async def show_admin_stats(self, query):
        stats_text = f"""
📊 <b>Детальная статистика</b>

🎫 <b>Всего тикетов:</b> {len(self.tickets)}
🔓 <b>Открытых:</b> {len(self.open_tickets)}
✅ <b>Закрытых:</b> {len(self.tickets) - len(self.open_tickets)}
👤 <b>Пользователей:</b> {len(self.user_tickets)}
🤖 <b>Обработано AI:</b> {sum(1 for t in self.tickets.values() if t.get('ai_processed', False))}

📈 <b>Активность:</b>
• Новых тикетов за сегодня: {self.get_today_tickets_count()}
• Активных пользователей: {self.get_active_users_count()}
        """
        keyboard = [
            [InlineKeyboardButton("↩️ Назад", callback_data="admin_panel")],
            [InlineKeyboardButton("🔄 Обновить", callback_data="admin_stats")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(stats_text, parse_mode='HTML', reply_markup=reply_markup)

    async def show_admin_users(self, query):
        users_text = "👥 <b>Список пользователей:</b>\n\n"
        
        for user_id, tickets_list in list(self.user_tickets.items())[:15]:
            user_tickets = [self.tickets[tid] for tid in tickets_list if tid in self.tickets]
            open_count = sum(1 for t in user_tickets if t['status'] == 'open')
            closed_count = len(user_tickets) - open_count
            ai_processed = sum(1 for t in user_tickets if t.get('ai_processed', False))
            
            user_info = next((t for t in user_tickets if t), {})
            username = user_info.get('username', 'Unknown')
            full_name = user_info.get('user_full_name', 'Unknown')
            
            users_text += f"👤 {full_name} (@{username})\n"
            users_text += f"   🎫 Тикетов: {len(user_tickets)} (🔓{open_count} ✅{closed_count}) 🤖{ai_processed}\n"
            users_text += f"   🔗 <a href='tg://user?id={user_id}'>Написать</a> | "
            users_text += f"<code>admin_user_tickets_{user_id}</code>\n\n"
        
        keyboard = [
            [InlineKeyboardButton("↩️ Назад", callback_data="admin_panel")],
            [InlineKeyboardButton("🔄 Обновить", callback_data="admin_users")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(users_text, parse_mode='HTML', reply_markup=reply_markup)

    def get_today_tickets_count(self):
        today = datetime.now().date()
        count = 0
        for ticket in self.tickets.values():
            if datetime.fromisoformat(ticket['created_at']).date() == today:
                count += 1
        return count

    def get_active_users_count(self):
        week_ago = datetime.now() - timedelta(days=7)
        active_users = set()
        for ticket in self.tickets.values():
            if datetime.fromisoformat(ticket['created_at']) > week_ago:
                active_users.add(ticket['user_id'])
        return len(active_users)

    async def show_ticket_to_admin(self, query, ticket_id):
        ticket_data = self.tickets.get(ticket_id)
        if not ticket_data:
            await query.edit_message_text("❌ Тикет не найден!")
            return
        
        created_time = datetime.fromisoformat(ticket_data['created_at']).strftime('%d.%m.%Y %H:%M')
        time_ago = self.get_time_ago(datetime.fromisoformat(ticket_data['created_at']))
        
        message_text = f"""
🎫 <b>Тикет:</b> <code>{ticket_id}</code>
👤 <b>Пользователь:</b> {ticket_data['user_full_name']} (ID: {ticket_data['user_id']})
📋 <b>Тема:</b> {html.escape(ticket_data['subject'])}
🕐 <b>Создан:</b> {created_time} ({time_ago})
🔓 <b>Статус:</b> {'✅ Открыт' if ticket_data['status'] == 'open' else '❌ Закрыт'}
🤖 <b>AI обработан:</b> {'✅ Да' if ticket_data.get('ai_processed', False) else '❌ Нет'}

📝 <b>Описание:</b>
{html.escape(ticket_data['description'])}

💬 <b>История сообщений:</b>
        """
        
        for msg in ticket_data.get('messages', []):
            if msg.get('is_ai', False):
                sender = "🤖 AI-помощник"
            else:
                sender = "👤 Пользователь" if msg['from_user'] else "👨‍💼 Админ"
            msg_time = datetime.fromisoformat(msg['timestamp']).strftime('%H:%M')
            message_text += f"\n{sender} ({msg_time}):\n{html.escape(msg['text'])}\n"
        
        if not ticket_data.get('messages'):
            message_text += "\n📭 Сообщений пока нет"
        
        keyboard = []
        if ticket_data['status'] == 'open':
            keyboard.append([InlineKeyboardButton("💬 Ответить", callback_data=f"admin_reply_{ticket_id}")])
            keyboard.append([InlineKeyboardButton("✅ Закрыть тикет", callback_data=f"admin_close_{ticket_id}")])
        
        # Кнопка для просмотра всех тикетов пользователя
        keyboard.append([InlineKeyboardButton("👤 Все тикеты пользователя", callback_data=f"admin_user_tickets_{ticket_data['user_id']}")])
        keyboard.append([InlineKeyboardButton("↩️ Назад", callback_data="admin_panel")])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        try:
            await query.edit_message_text(
                message_text,
                reply_markup=reply_markup,
                parse_mode='HTML'
            )
        except Exception as e:
            if "Message is not modified" not in str(e):
                raise e

    async def close_ticket(self, query, ticket_id):
        ticket_data = self.tickets.get(ticket_id)
        if not ticket_data:
            await query.edit_message_text("❌ Тикет не найден!")
            return
            
        ticket_data['status'] = 'closed'
        if ticket_id in self.open_tickets:
            self.open_tickets.remove(ticket_id)
        self.save_data()
        
        # Уведомляем пользователя
        try:
            keyboard = [
                [InlineKeyboardButton("📋 Мои тикеты", callback_data="my_tickets")],
                [InlineKeyboardButton("🎫 Новый тикет", callback_data="create_ticket")],
                [InlineKeyboardButton("🔙 На главную", callback_data="back_to_start")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.bot.send_message(
                chat_id=ticket_data['user_id'],
                text=f"✅ <b>Тикет закрыт</b>\n\nВаш тикет <code>{ticket_id}</code> был закрыт администратором.\n\nСпасибо за обращение!",
                parse_mode='HTML',
                reply_markup=reply_markup
            )
        except Exception as e:
            logger.error(f"Не удалось уведомить пользователя: {e}")
        
        keyboard = [
            [InlineKeyboardButton("📊 Панель админа", callback_data="admin_panel")],
            [InlineKeyboardButton("🔄 Обновить", callback_data="admin_stats")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            f"✅ <b>Тикет закрыт!</b>\n\n<code>{ticket_id}</code>",
            parse_mode='HTML',
            reply_markup=reply_markup
        )

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        
        # Если админ отвечает на тикет
        if user_id == ADMIN_ID and 'admin_replying_to' in context.user_data:
            ticket_id = context.user_data['admin_replying_to']
            ticket_data = self.tickets.get(ticket_id)
            
            if not ticket_data:
                await update.message.reply_text("❌ Тикет не найден!")
                del context.user_data['admin_replying_to']
                return
            
            # Добавляем сообщение в историю
            if 'messages' not in ticket_data:
                ticket_data['messages'] = []
            
            ticket_data['messages'].append({
                'text': update.message.text,
                'from_user': False,
                'timestamp': datetime.now().isoformat()
            })
            self.save_data()
            
            # Отправляем сообщение пользователю
            try:
                keyboard = [
                    [InlineKeyboardButton("💬 Ответить", callback_data=f"user_reply_{ticket_id}")],
                    [InlineKeyboardButton("📋 Мои тикеты", callback_data="my_tickets")]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                await context.bot.send_message(
                    chat_id=ticket_data['user_id'],
                    text=f"👨‍💼 <b>Ответ поддержки:</b>\n\n{html.escape(update.message.text)}",
                    parse_mode='HTML',
                    reply_markup=reply_markup
                )
            except Exception as e:
                logger.error(f"Не удалось отправить сообщение пользователю: {e}")
                await update.message.reply_text("❌ Не удалось отправить сообщение пользователю.")
            
            del context.user_data['admin_replying_to']
            
            # Показываем кнопки админу после ответа
            keyboard = [
                [InlineKeyboardButton("📊 Панель админа", callback_data="admin_panel")],
                [InlineKeyboardButton("👀 Посмотреть тикет", callback_data=f"admin_view_{ticket_id}")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.message.reply_text("✅ Ответ отправлен пользователю!", reply_markup=reply_markup)
            
        # Если пользователь отвечает на тикет
        elif 'user_replying_to' in context.user_data:
            ticket_id = context.user_data['user_replying_to']
            ticket_data = self.tickets.get(ticket_id)
            
            if not ticket_data or ticket_data['user_id'] != user_id:
                await update.message.reply_text("❌ Тикет не найден!")
                del context.user_data['user_replying_to']
                return
            
            if ticket_data['status'] != 'open':
                await update.message.reply_text("❌ Этот тикет уже закрыт!")
                del context.user_data['user_replying_to']
                return
            
            # Добавляем сообщение в историю
            if 'messages' not in ticket_data:
                ticket_data['messages'] = []
            
            ticket_data['messages'].append({
                'text': update.message.text,
                'from_user': True,
                'timestamp': datetime.now().isoformat()
            })
            self.save_data()
            
            # Если тикет был обработан AI, отправляем оператору с пометкой
            if ticket_data.get('ai_processed', False):
                # Отправляем админу с пометкой, что пользователь ответил на AI-ответ
                try:
                    keyboard = [
                        [InlineKeyboardButton("💬 Ответить", callback_data=f"admin_reply_{ticket_id}")],
                        [InlineKeyboardButton("👀 Посмотреть тикет", callback_data=f"admin_view_{ticket_id}")]
                    ]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    
                    await context.bot.send_message(
                        chat_id=ADMIN_ID,
                        text=f"⚠️ <b>Пользователь ответил на AI-ответ по тикету {ticket_id}</b>\n\n"
                             f"👤 Пользователь: {ticket_data['user_full_name']}\n"
                             f"📝 Ответ: {html.escape(update.message.text)}\n\n"
                             f"<i>Требуется внимание оператора!</i>",
                        parse_mode='HTML',
                        reply_markup=reply_markup
                    )
                except Exception as e:
                    logger.error(f"Не удалось отправить сообщение админу: {e}")
                
                await update.message.reply_text(
                    "✅ Ваш ответ отправлен оператору!\n\n"
                    "📞 Оператор скоро свяжется с вами."
                )
            else:
                # Отправляем сообщение админу (обычный случай)
                try:
                    keyboard = [
                        [InlineKeyboardButton("💬 Ответить", callback_data=f"admin_reply_{ticket_id}")],
                        [InlineKeyboardButton("👀 Посмотреть тикет", callback_data=f"admin_view_{ticket_id}")]
                    ]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    
                    await context.bot.send_message(
                        chat_id=ADMIN_ID,
                        text=f"👤 <b>Ответ пользователя по тикету {ticket_id}:</b>\n\n{html.escape(update.message.text)}",
                        parse_mode='HTML',
                        reply_markup=reply_markup
                    )
                except Exception as e:
                    logger.error(f"Не удалось отправить сообщение админу: {e}")
                
                await update.message.reply_text("✅ Ваш ответ отправлен поддержке!")
            
            del context.user_data['user_replying_to']
            
        # Обычное сообщение пользователя
        elif user_id != ADMIN_ID:
            # Базовая проверка на спам
            spam_attempts = self.user_spam_attempts.get(user_id, 0)
            
            if spam_attempts > 5:
                await update.message.reply_text(
                    "❌ Слишком много сообщений. Пожалуйста, создайте тикет для обращения в поддержку.",
                    parse_mode='HTML'
                )
                return
            
            if len(update.message.text.strip()) < 3:
                self.user_spam_attempts[user_id] = spam_attempts + 1
                self.save_data()
                await update.message.reply_text(
                    "❌ Сообщение слишком короткое.",
                    parse_mode='HTML'
                )
                return
            
            # Проверка на явный спам
            if self.is_clearly_spam(update.message.text):
                self.user_spam_attempts[user_id] = spam_attempts + 1
                self.save_data()
                await update.message.reply_text(
                    "❌ Ваше сообщение содержит недопустимый контент.",
                    parse_mode='HTML'
                )
                return
            
            # Проверка на бессмысленный текст
            if self.is_gibberish(update.message.text):
                self.user_spam_attempts[user_id] = spam_attempts + 1
                self.save_data()
                await update.message.reply_text(
                    "❌ Сообщение содержит бессмысленный текст.",
                    parse_mode='HTML'
                )
                return
            
            # Сбрасываем счетчик при нормальном сообщении
            self.user_spam_attempts[user_id] = 0
            self.save_data()
            
            keyboard = [
                [InlineKeyboardButton("🎫 Создать тикет", callback_data="create_ticket")],
                [InlineKeyboardButton("📋 Мои тикеты", callback_data="my_tickets")],
                [InlineKeyboardButton("🔙 На главную", callback_data="back_to_start")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.message.reply_text(
                "💬 <b>Ваше сообщение получено!</b>\n\nДля обращения в поддержку создайте тикет:",
                parse_mode='HTML',
                reply_markup=reply_markup
            )

    async def start_cleanup_task(self):
        """Запускает задачу для ежедневной очистки переписки"""
        async def cleanup_conversation():
            while True:
                try:
                    # Ждем до следующего дня 3:00 утра
                    now = datetime.now()
                    next_run = now.replace(hour=3, minute=0, second=0, microsecond=0)
                    if now.hour >= 3:
                        next_run += timedelta(days=1)
                    
                    wait_seconds = (next_run - now).total_seconds()
                    await asyncio.sleep(wait_seconds)
                    
                    logger.info("Запуск ежедневной очистки переписки...")
                    
                    # Здесь можно добавить логику очистки, если нужно
                    # Например, очистка временных данных или кэша
                    
                    logger.info("Ежедневная очистка завершена")
                    
                except Exception as e:
                    logger.error(f"Ошибка в задаче очистки: {e}")
                    await asyncio.sleep(3600)
        
        self.cleanup_task = asyncio.create_task(cleanup_conversation())

    def run(self):
        logger.info("Бот запущен!")
        # Запускаем задачу очистки
        asyncio.get_event_loop().run_until_complete(self.start_cleanup_task())
        self.app.run_polling()

if __name__ == "__main__":
    bot = SupportBot()
    bot.run()
