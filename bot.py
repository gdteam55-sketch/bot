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
TICKET_SUBJECT, TICKET_DESCRIPTION, AI_CONVERSATION = range(3)

# Файлы для сохранения данных
TICKETS_FILE = "tickets.pkl"
USER_TICKETS_FILE = "user_tickets.pkl"
OPEN_TICKETS_FILE = "open_tickets.pkl"
SPAM_ATTEMPTS_FILE = "spam_attempts.pkl"
AI_CONVERSATIONS_FILE = "ai_conversations.pkl"

class SupportBot:
    def __init__(self):
        self.app = Application.builder().token(BOT_TOKEN).build()
        self.load_data()
        self.setup_handlers()
        
        # Запускаем задачу для проверки неактивных диалогов
        self.cleanup_task = None
        self.ai_timeout_task = None
        # Словарь для отслеживания активных диалогов с пользователями
        self.active_conversations = {}

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
                
            if os.path.exists(AI_CONVERSATIONS_FILE):
                with open(AI_CONVERSATIONS_FILE, 'rb') as f:
                    self.ai_conversations = pickle.load(f)
            else:
                self.ai_conversations = {}
                
        except Exception as e:
            logger.error(f"Ошибка при загрузке данных: {e}")
            self.tickets = {}
            self.user_tickets = defaultdict(list)
            self.open_tickets = set()
            self.user_spam_attempts = defaultdict(int)
            self.ai_conversations = {}

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
                
            with open(AI_CONVERSATIONS_FILE, 'wb') as f:
                pickle.dump(self.ai_conversations, f)
                
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
        
        # Обработчик сообщений для пользователей
        self.app.add_handler(MessageHandler(
            filters.TEXT & ~filters.COMMAND & ~filters.User(user_id=ADMIN_ID), 
            self.handle_user_message
        ))
        
        # Обработчик для сообщений от админа
        self.app.add_handler(MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.User(user_id=ADMIN_ID),
            self.handle_admin_message
        ))
        
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
        
        # Игнорируем ошибки, связанные с удаленными сообщениями
        if "message to edit" in str(context.error) or "message is not found" in str(context.error):
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

    async def ask_ai_for_help(self, ticket_id: str, user_message: str = None, is_followup: bool = False) -> tuple:
        """
        Запрашивает у ИИ ответ на сообщение в тикете.
        Возвращает (ответ_ии, флаг_эскалации_оператору, нужно_ли_задать_вопрос)
        """
        ticket_data = self.tickets.get(ticket_id)
        if not ticket_data:
            return None, False, False, False
        
        try:
            # Формируем контекст для ИИ
            context_messages = []
            
            # Добавляем историю переписки (последние 10 сообщений)
            for msg in ticket_data.get('messages', [])[-10:]:
                if msg.get('is_ai', False):
                    sender = "AI-ассистент"
                else:
                    sender = "Пользователь" if msg['from_user'] else "Оператор"
                context_messages.append(f"{sender}: {msg['text']}")
            
            # Добавляем описание проблемы
            problem_text = f"Тема: {ticket_data['subject']}\nОписание: {ticket_data['description']}"
            
            # Системный промпт
            system_prompt = """Ты - ИИ-ассистент технической поддержки. Твоя задача - помочь пользователю решить его проблему.

ПРАВИЛА:
1. Отвечай вежливо и профессионально на русском языке
2. Если информации недостаточно - задай 1-2 уточняющих вопроса
3. Если проблема решена и пользователь подтвердил - напиши "ПРОБЛЕМА РЕШЕНА"
4. Если пользователь просит оператора - напиши "ВЫЗВАН ОПЕРАТОР"
5. Никогда не вызывай оператора самостоятельно, только по просьбе пользователя
6. Если не знаешь ответа - задай уточняющие вопросы, чтобы лучше понять проблему
7. Дай конкретные, полезные советы

ВАЖНО: Ты должен вести диалог с пользователем, задавать вопросы и помогать решить проблему самостоятельно. Только если пользователь явно попросит оператора - вызывай его."""

            # Формируем сообщение для ИИ
            user_message_for_ai = f"""
Проблема пользователя:
{problem_text}

История диалога:
{chr(10).join(context_messages) if context_messages else 'Диалог только начинается'}

Последнее сообщение пользователя: {user_message if user_message else 'Нет новых сообщений'}

Ответь на русском языке. Если нужно задать уточняющий вопрос - задай его. Если проблема решена - подтверди это.
"""

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
                "max_tokens": 1000,
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
                
                # Проверяем, что хочет сделать ИИ
                wants_operator = "ВЫЗВАН ОПЕРАТОР" in ai_response
                problem_solved = "ПРОБЛЕМА РЕШЕНА" in ai_response
                needs_clarification = "?" in ai_response or "уточн" in ai_response.lower() or "пожалуйста, расскажите" in ai_response.lower()
                
                # Убираем служебные метки из ответа
                ai_response = ai_response.replace("ВЫЗВАН ОПЕРАТОР", "").replace("ПРОБЛЕМА РЕШЕНА", "").strip()
                
                return ai_response, wants_operator, needs_clarification, problem_solved
            else:
                logger.error(f"Ошибка API: {response.status_code} - {response.text}")
                return None, False, False, False
                
        except requests.exceptions.Timeout:
            logger.error("Таймаут при запросе к AI")
            return None, False, False, False
        except Exception as e:
            logger.error(f"Ошибка при обращении к AI: {e}")
            return None, False, False, False

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

🤖 <b>Я бот технической поддержки с ИИ-помощником</b>

🚀 <b>Здесь вы сможете:</b>
• 🎫 Создавать тикеты для решения проблем
• 🤖 Общаться с ИИ-помощником
• 📋 Просматривать историю своих обращений
• 💬 Вызвать оператора при необходимости

💡 <b>Для начала работы нажмите кнопку ниже</b>
        """
        
        try:
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
        except Exception as e:
            if "Message is not modified" not in str(e):
                logger.error(f"Ошибка в start: {e}")

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
                ai_emoji = "🤖 " if ticket.get('ai_processed', False) else ""
                time_ago = self.get_time_ago(datetime.fromisoformat(ticket['created_at']))
                btn_text = f"{status_emoji} {ai_emoji}{time_ago} - {ticket['subject'][:20]}..."
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
        ai_text = "🤖 Обрабатывается AI" if ticket_data.get('ai_processed', False) else "👨‍💼 Ожидает оператора"
        
        message_text = f"""
🎫 <b>Ваш тикет:</b> <code>{ticket_id}</code>
📋 <b>Тема:</b> {html.escape(ticket_data['subject'])}
🕐 <b>Создан:</b> {created_time}
🔓 <b>Статус:</b> {status_text}
🤖 <b>Режим:</b> {ai_text}

📝 <b>Описание:</b>
{html.escape(ticket_data['description'])}

💬 <b>История переписки:</b>
        """
        
        for msg in ticket_data.get('messages', []):
            if msg.get('is_ai', False):
                sender = "🤖 AI-помощник"
            else:
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
            # Показываем кнопку вызова оператора только если было хотя бы 2 ответа AI
            ai_responses = sum(1 for msg in ticket_data.get('messages', []) if msg.get('is_ai', False))
            if ai_responses >= 2:
                keyboard.insert(0, [InlineKeyboardButton("📞 Позвать оператора", callback_data=f"user_call_operator_{ticket_id}")])
        
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
Нажмите кнопку "Создать тикет" и опишите вашу проблему.

🤖 <b>Работа с ИИ-помощником:</b>
• ИИ будет общаться с вами в диалоге
• Задавайте вопросы - ИИ поможет найти решение
• После 2-х ответов ИИ появится кнопка "Позвать оператора"

📞 <b>Вызов оператора:</b>
Если ИИ не может помочь, вы всегда можете вызвать оператора.

✅ <b>Закрытие тикета:</b>
Тикет закроется автоматически через 24 часа без активности.

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
            
        ai_tickets = sum(1 for t in self.tickets.values() if t.get('ai_processed', False))
        closed_by_ai = sum(1 for t in self.tickets.values() if t.get('ai_processed', False) and t['status'] == 'closed')
        
        stats_text = f"""
📊 <b>Статистика бота</b>

🎫 Всего тикетов: <b>{len(self.tickets)}</b>
🔓 Открытых тикетов: <b>{len(self.open_tickets)}</b>
✅ Закрытых тикетов: <b>{len(self.tickets) - len(self.open_tickets)}</b>
👤 Уникальных пользователей: <b>{len(self.user_tickets)}</b>
🤖 Обработано AI: <b>{ai_tickets}</b>
✅ Закрыто AI: <b>{closed_by_ai}</b>
        """
        await update.message.reply_text(stats_text, parse_mode='HTML')

    async def create_ticket(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        # Проверяем, является ли пользователь админом
        user = update.effective_user
        
        # Разрешаем создание тикетов и админам, и обычным пользователям
        # Просто очищаем предыдущие данные
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
            'ai_processed': True,
            'last_activity': datetime.now().isoformat(),
            'waiting_for_operator': False,
            'ai_response_count': 0
        }
        
        # Сохраняем тикет
        self.tickets[ticket_id] = ticket_data
        self.user_tickets[user.id].append(ticket_id)
        self.open_tickets.add(ticket_id)
        self.save_data()
        
        # Начинаем диалог с ИИ
        ai_response, wants_operator, needs_clarification, problem_solved = await self.ask_ai_for_help(ticket_id, description)
        
        # Сохраняем первый ответ ИИ
        if ai_response:
            ticket_data['messages'].append({
                'text': ai_response,
                'from_user': False,
                'timestamp': datetime.now().isoformat(),
                'is_ai': True
            })
            ticket_data['ai_response_count'] = 1
            ticket_data['last_activity'] = datetime.now().isoformat()
            self.save_data()
        
        # Отправляем сообщение пользователю с ответом ИИ
        keyboard = [
            [InlineKeyboardButton("📋 Мои тикеты", callback_data="my_tickets")],
            [InlineKeyboardButton("🔙 На главную", callback_data="back_to_start")]
        ]
        
        # Добавляем кнопку вызова оператора только после 2-х ответов AI
        if ticket_data['ai_response_count'] >= 2:
            keyboard.insert(0, [InlineKeyboardButton("📞 Позвать оператора", callback_data=f"user_call_operator_{ticket_id}")])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        if ai_response:
            await update.message.reply_text(
                f"🎫 <b>Тикет создан!</b> <code>{ticket_id}</code>\n\n"
                f"🤖 <b>AI-помощник отвечает:</b>\n\n{html.escape(ai_response)}\n\n"
                f"<i>Продолжайте диалог с AI. Просто пишите свои сообщения, не нужно нажимать кнопку \"Ответить\" каждый раз.</i>",
                parse_mode='HTML',
                reply_markup=reply_markup
            )
        else:
            # Если ИИ не ответил, сразу вызываем оператора
            ticket_data['waiting_for_operator'] = True
            ticket_data['ai_processed'] = False
            self.save_data()
            
            await update.message.reply_text(
                f"🎫 <b>Тикет создан!</b> <code>{ticket_id}</code>\n\n"
                f"⏳ <b>ИИ временно недоступен</b>\n"
                f"Мы передали ваш запрос оператору.\n"
                f"Ожидайте ответа в ближайшее время!",
                parse_mode='HTML',
                reply_markup=reply_markup
            )
            
            # Уведомляем админа
            await self.notify_admin_about_ticket(ticket_id)
        
        # Очищаем данные
        context.user_data.pop('ticket_subject', None)
        
        return ConversationHandler.END

    async def notify_admin_about_ticket(self, ticket_id):
        """Уведомляет админа о новом тикете, требующем оператора"""
        ticket_data = self.tickets.get(ticket_id)
        if not ticket_data:
            return
        
        admin_message = f"""
🚨 <b>НОВЫЙ ТИКЕТ ТРЕБУЕТ ОПЕРАТОРА!</b>

🎫 <b>ID:</b> <code>{ticket_id}</code>
👤 <b>Пользователь:</b> {ticket_data['user_full_name']} (@{ticket_data['username']})
📋 <b>Тема:</b> {html.escape(ticket_data['subject'])}
📝 <b>Описание:</b> {html.escape(ticket_data['description'][:300])}{'...' if len(ticket_data['description']) > 300 else ''}

⏰ <b>Время создания:</b> {datetime.now().strftime('%d.%m.%Y %H:%M')}

❓ <b>Причина:</b>
• ИИ не смог ответить
• Пользователь вызвал оператора
        """
        
        try:
            admin_keyboard = [
                [InlineKeyboardButton("👀 Посмотреть тикет", callback_data=f"admin_view_{ticket_id}")],
                [InlineKeyboardButton("💬 Ответить", callback_data=f"admin_reply_{ticket_id}")],
                [InlineKeyboardButton("📊 Панель админа", callback_data="admin_panel")]
            ]
            admin_reply_markup = InlineKeyboardMarkup(admin_keyboard)
            
            await self.app.bot.send_message(
                chat_id=ADMIN_ID,
                text=admin_message,
                parse_mode='HTML',
                reply_markup=admin_reply_markup
            )
        except Exception as e:
            logger.error(f"Не удалось отправить сообщение админу: {e}")

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
                    operator_label = "📞 " if ticket_data.get('waiting_for_operator', False) else ""
                    btn_text = f"{operator_label}{ai_label}🎫 {time_ago} - {ticket_data['subject'][:25]}..."
                    keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"admin_view_{ticket_id}")])
        else:
            keyboard.append([InlineKeyboardButton("✅ Все тикеты обработаны", callback_data="none")])
        
        keyboard.extend([
            [InlineKeyboardButton("📊 Статистика", callback_data="admin_stats")],
            [InlineKeyboardButton("👤 Пользователи", callback_data="admin_users")],
            [InlineKeyboardButton("🔄 Обновить", callback_data="admin_panel")]
        ])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        ai_tickets = sum(1 for t in self.tickets.values() if t.get('ai_processed', False))
        waiting_operator = sum(1 for t in self.tickets.values() if t.get('waiting_for_operator', False) and t['status'] == 'open')
        
        text = f"""
👨‍💼 <b>Панель администратора</b>

📊 <b>Статистика:</b>
• 🔓 Открытых тикетов: <b>{len(self.open_tickets)}</b>
• 📞 Ожидают оператора: <b>{waiting_operator}</b>
• 🤖 Обрабатывает AI: <b>{len(self.open_tickets) - waiting_operator}</b>
• ✅ Закрытых тикетов: <b>{len(self.tickets) - len(self.open_tickets)}</b>
• 👤 Всего пользователей: <b>{len(self.user_tickets)}</b>

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
        days = hours // 24
        
        if days > 0:
            return f"{int(days)}д назад"
        elif hours > 0:
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
            
        elif data.startswith("user_call_operator_"):
            ticket_id = data.replace("user_call_operator_", "")
            await self.call_operator(query, ticket_id)
            
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

    async def call_operator(self, query, ticket_id):
        """Вызывает оператора по требованию пользователя"""
        ticket_data = self.tickets.get(ticket_id)
        if not ticket_data:
            await query.answer("❌ Тикет не найден!", show_alert=True)
            return
        
        if ticket_data['status'] != 'open':
            await query.answer("❌ Тикет уже закрыт!", show_alert=True)
            return
        
        if ticket_data.get('waiting_for_operator', False):
            await query.answer("📞 Оператор уже вызван!", show_alert=True)
            return
        
        # Помечаем, что нужен оператор
        ticket_data['waiting_for_operator'] = True
        ticket_data['ai_processed'] = False
        self.save_data()
        
        # Уведомляем админа
        await self.notify_admin_about_ticket(ticket_id)
        
        # Отправляем подтверждение пользователю
        keyboard = [
            [InlineKeyboardButton("📋 Мои тикеты", callback_data="my_tickets")],
            [InlineKeyboardButton("🔙 На главную", callback_data="back_to_start")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            f"📞 <b>Оператор вызван!</b>\n\n"
            f"Ваш запрос передан оператору поддержки.\n"
            f"Ожидайте ответа в ближайшее время.\n\n"
            f"<i>Вы можете продолжать общение, все сообщения будут направлены оператору.</i>",
            parse_mode='HTML',
            reply_markup=reply_markup
        )

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
                operator_label = "📞 " if ticket.get('waiting_for_operator', False) else ""
                btn_text = f"{status_emoji} {operator_label}{ai_label}{time_ago} - {ticket['subject'][:20]}..."
                keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"admin_view_{ticket_id}")])
        
        keyboard.append([InlineKeyboardButton("↩️ Назад", callback_data="admin_panel")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        user_stats_text = f"""
👤 <b>Пользователь:</b> {full_name} (@{username})
📊 <b>Статистика:</b>
• 🎫 Всего тикетов: {len(tickets_list)}
• 🔓 Открытых: {sum(1 for t in user_tickets if t['status'] == 'open')}
• ✅ Закрытых: {sum(1 for t in user_tickets if t['status'] == 'closed')}
• 📞 Ожидают оператора: {sum(1 for t in user_tickets if t.get('waiting_for_operator', False))}

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
                ai_emoji = "🤖 " if ticket.get('ai_processed', False) else ""
                time_ago = self.get_time_ago(datetime.fromisoformat(ticket['created_at']))
                btn_text = f"{status_emoji} {ai_emoji}{time_ago} - {ticket['subject'][:20]}..."
                keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"user_view_{ticket_id}")])
        
        keyboard.append([InlineKeyboardButton("🔙 На главную", callback_data="back_to_start")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            "📋 <b>Ваши тикеты:</b>\n\nВыберите тикет для просмотра:",
            parse_mode='HTML',
            reply_markup=reply_markup
        )

    async def show_admin_stats(self, query):
        ai_tickets = sum(1 for t in self.tickets.values() if t.get('ai_processed', False))
        closed_by_ai = sum(1 for t in self.tickets.values() if t.get('ai_processed', False) and t['status'] == 'closed')
        waiting_operator = sum(1 for t in self.tickets.values() if t.get('waiting_for_operator', False) and t['status'] == 'open')
        
        stats_text = f"""
📊 <b>Детальная статистика</b>

🎫 <b>Всего тикетов:</b> {len(self.tickets)}
🔓 <b>Открытых:</b> {len(self.open_tickets)}
✅ <b>Закрытых:</b> {len(self.tickets) - len(self.open_tickets)}
👤 <b>Пользователей:</b> {len(self.user_tickets)}

🤖 <b>Работа AI:</b>
• Обработано AI: {ai_tickets}
• Закрыто AI: {closed_by_ai}
• 📞 Ожидают оператора: {waiting_operator}

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
            waiting_operator = sum(1 for t in user_tickets if t.get('waiting_for_operator', False))
            
            user_info = next((t for t in user_tickets if t), {})
            username = user_info.get('username', 'Unknown')
            full_name = user_info.get('user_full_name', 'Unknown')
            
            users_text += f"👤 {full_name} (@{username})\n"
            users_text += f"   🎫 Тикетов: {len(user_tickets)} (🔓{open_count} ✅{closed_count}) 🤖{ai_processed} 📞{waiting_operator}\n"
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
📞 <b>Ожидает оператора:</b> {'✅ Да' if ticket_data.get('waiting_for_operator', False) else '❌ Нет'}

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

    async def close_ticket(self, query, ticket_id, closed_by_ai=False, closed_by_timeout=False):
        ticket_data = self.tickets.get(ticket_id)
        if not ticket_data:
            if query:
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
            
            close_message = f"✅ <b>Тикет закрыт</b>\n\nВаш тикет <code>{ticket_id}</code> был закрыт."
            
            if closed_by_ai:
                close_message += "\n\n🤖 Проблема была решена с помощью AI-помощника."
            elif closed_by_timeout:
                close_message += "\n\n⏰ Тикет был автоматически закрыт из-за отсутствия активности в течение 24 часов."
            
            # Добавляем последний ответ, если есть
            if ticket_data.get('messages'):
                last_message = ticket_data['messages'][-1]
                if not last_message.get('is_ai', False) and not last_message['from_user']:
                    close_message += f"\n\n📝 <b>Последний ответ поддержки:</b>\n{html.escape(last_message['text'])}"
            
            close_message += "\n\nСпасибо за обращение!"
            
            # Отправляем сообщение пользователю
            await self.app.bot.send_message(
                chat_id=ticket_data['user_id'],
                text=close_message,
                parse_mode='HTML',
                reply_markup=reply_markup
            )
        except Exception as e:
            logger.error(f"Не удалось уведомить пользователя: {e}")
        
        if query:
            keyboard = [
                [InlineKeyboardButton("📊 Панель админа", callback_data="admin_panel")],
                [InlineKeyboardButton("🔄 Обновить", callback_data="admin_stats")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            try:
                await query.edit_message_text(
                    f"✅ <b>Тикет закрыт!</b>\n\n<code>{ticket_id}</code>",
                    parse_mode='HTML',
                    reply_markup=reply_markup
                )
            except Exception as e:
                logger.error(f"Ошибка при редактировании сообщения: {e}")

    async def check_inactive_ai_conversations(self):
        """Проверяет неактивные AI-диалоги и закрывает их через 24 часа"""
        while True:
            try:
                await asyncio.sleep(3600)  # Проверяем каждый час
                
                now = datetime.now()
                timeout_hours = 24  # 24 часа
                
                for ticket_id, ticket_data in list(self.tickets.items()):
                    if ticket_data['status'] != 'open':
                        continue
                    
                    if ticket_data.get('waiting_for_operator', False):
                        continue
                    
                    last_activity = datetime.fromisoformat(ticket_data.get('last_activity', ticket_data['created_at']))
                    time_diff = (now - last_activity).total_seconds() / 3600  # В часах
                    
                    if time_diff >= timeout_hours:
                        # Закрываем тикет автоматически
                        logger.info(f"Автоматическое закрытие тикета {ticket_id} из-за неактивности ({time_diff:.1f} часов)")
                        
                        await self.close_ticket(None, ticket_id, closed_by_timeout=True)
                        
            except Exception as e:
                logger.error(f"Ошибка в проверке неактивных диалогов: {e}")

    async def handle_user_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обрабатывает сообщения от обычных пользователей (не админа)"""
        user_id = update.effective_user.id
        user_message = update.message.text
        
        # Ищем открытый тикет пользователя
        active_ticket_id = None
        if user_id in self.user_tickets:
            for ticket_id in reversed(self.user_tickets[user_id]):
                ticket = self.tickets.get(ticket_id)
                if ticket and ticket['status'] == 'open':
                    active_ticket_id = ticket_id
                    break
        
        # Если есть активный тикет
        if active_ticket_id:
            ticket_data = self.tickets.get(active_ticket_id)
            
            # Если тикет ожидает оператора, отправляем сообщение админу
            if ticket_data.get('waiting_for_operator', False):
                # Добавляем сообщение в историю
                ticket_data['messages'].append({
                    'text': user_message,
                    'from_user': True,
                    'timestamp': datetime.now().isoformat()
                })
                ticket_data['last_activity'] = datetime.now().isoformat()
                self.save_data()
                
                # Отправляем админу
                try:
                    keyboard = [
                        [InlineKeyboardButton("💬 Ответить", callback_data=f"admin_reply_{active_ticket_id}")],
                        [InlineKeyboardButton("👀 Посмотреть тикет", callback_data=f"admin_view_{active_ticket_id}")]
                    ]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    
                    await context.bot.send_message(
                        chat_id=ADMIN_ID,
                        text=f"📞 <b>Ответ пользователя по тикету {active_ticket_id}</b>\n\n"
                             f"👤 Пользователь: {ticket_data['user_full_name']}\n"
                             f"📝 Сообщение: {html.escape(user_message)}",
                        parse_mode='HTML',
                        reply_markup=reply_markup
                    )
                except Exception as e:
                    logger.error(f"Не удалось отправить сообщение админу: {e}")
                
                await update.message.reply_text("✅ Ваш ответ отправлен оператору!")
                return
            
            # Иначе продолжаем с AI
            ticket_id = active_ticket_id
            
            # Добавляем сообщение в историю
            ticket_data['messages'].append({
                'text': user_message,
                'from_user': True,
                'timestamp': datetime.now().isoformat()
            })
            ticket_data['last_activity'] = datetime.now().isoformat()
            self.save_data()
            
            # Обрабатываем через AI
            ai_response, wants_operator, needs_clarification, problem_solved = await self.ask_ai_for_help(ticket_id, user_message)
            
            if problem_solved:
                # Проблема решена - закрываем тикет
                await self.close_ticket(None, ticket_id, closed_by_ai=True)
                
                keyboard = [
                    [InlineKeyboardButton("📋 Мои тикеты", callback_data="my_tickets")],
                    [InlineKeyboardButton("🎫 Новый тикет", callback_data="create_ticket")],
                    [InlineKeyboardButton("🔙 На главную", callback_data="back_to_start")]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                await update.message.reply_text(
                    f"✅ <b>Проблема решена!</b>\n\n"
                    f"Тикет <code>{ticket_id}</code> закрыт.\n\n"
                    f"<i>Спасибо за использование нашего сервиса!</i>",
                    parse_mode='HTML',
                    reply_markup=reply_markup
                )
                
                # Уведомляем админа
                try:
                    await context.bot.send_message(
                        chat_id=ADMIN_ID,
                        text=f"✅ <b>Тикет закрыт AI</b>\n\nТикет <code>{ticket_id}</code>\nПользователь подтвердил решение проблемы.",
                        parse_mode='HTML'
                    )
                except Exception as e:
                    logger.error(f"Не удалось уведомить админа: {e}")
                return
            
            if wants_operator:
                # Пользователь попросил оператора
                ticket_data['waiting_for_operator'] = True
                ticket_data['ai_processed'] = False
                self.save_data()
                
                await self.notify_admin_about_ticket(ticket_id)
                
                keyboard = [
                    [InlineKeyboardButton("📋 Мои тикеты", callback_data="my_tickets")],
                    [InlineKeyboardButton("🔙 На главную", callback_data="back_to_start")]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                await update.message.reply_text(
                    f"📞 <b>Оператор вызван!</b>\n\n"
                    f"Ваш запрос передан оператору поддержки.\n"
                    f"Ожидайте ответа в ближайшее время.",
                    parse_mode='HTML',
                    reply_markup=reply_markup
                )
                return
            
            if ai_response:
                # Сохраняем ответ AI
                ticket_data['messages'].append({
                    'text': ai_response,
                    'from_user': False,
                    'timestamp': datetime.now().isoformat(),
                    'is_ai': True
                })
                ticket_data['ai_response_count'] = ticket_data.get('ai_response_count', 0) + 1
                ticket_data['last_activity'] = datetime.now().isoformat()
                self.save_data()
                
                # Отправляем ответ пользователю
                keyboard = [
                    [InlineKeyboardButton("📋 Мои тикеты", callback_data="my_tickets")],
                    [InlineKeyboardButton("🔙 На главную", callback_data="back_to_start")]
                ]
                
                # Добавляем кнопку вызова оператора только если было 2+ ответов AI
                if ticket_data['ai_response_count'] >= 2:
                    keyboard.insert(0, [InlineKeyboardButton("📞 Позвать оператора", callback_data=f"user_call_operator_{ticket_id}")])
                
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                await update.message.reply_text(
                    f"🤖 <b>AI-помощник:</b>\n\n{html.escape(ai_response)}",
                    parse_mode='HTML',
                    reply_markup=reply_markup
                )
            else:
                # Если AI не ответил, вызываем оператора
                ticket_data['waiting_for_operator'] = True
                ticket_data['ai_processed'] = False
                self.save_data()
                
                await self.notify_admin_about_ticket(ticket_id)
                
                keyboard = [
                    [InlineKeyboardButton("📋 Мои тикеты", callback_data="my_tickets")],
                    [InlineKeyboardButton("🔙 На главную", callback_data="back_to_start")]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                await update.message.reply_text(
                    f"⏳ <b>ИИ временно недоступен</b>\n\n"
                    f"Мы передали ваш запрос оператору.\n"
                    f"Ожидайте ответа в ближайшее время!",
                    parse_mode='HTML',
                    reply_markup=reply_markup
                )
            return
        
        # Если нет активного тикета, предлагаем создать
        # Базовая проверка на спам
        spam_attempts = self.user_spam_attempts.get(user_id, 0)
        
        if spam_attempts > 5:
            await update.message.reply_text(
                "❌ Слишком много сообщений. Пожалуйста, создайте тикет для обращения в поддержку.",
                parse_mode='HTML'
            )
            return
        
        if len(user_message.strip()) < 3:
            self.user_spam_attempts[user_id] = spam_attempts + 1
            self.save_data()
            await update.message.reply_text(
                "❌ Сообщение слишком короткое.",
                parse_mode='HTML'
            )
            return
        
        # Проверка на явный спам
        if self.is_clearly_spam(user_message):
            self.user_spam_attempts[user_id] = spam_attempts + 1
            self.save_data()
            await update.message.reply_text(
                "❌ Ваше сообщение содержит недопустимый контент.",
                parse_mode='HTML'
            )
            return
        
        # Проверка на бессмысленный текст
        if self.is_gibberish(user_message):
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

    async def handle_admin_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обрабатывает сообщения от администратора"""
        user_id = update.effective_user.id
        
        # Проверяем, что это действительно админ
        if user_id != ADMIN_ID:
            return
        
        # Если админ отвечает на тикет
        if 'admin_replying_to' in context.user_data:
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
            ticket_data['last_activity'] = datetime.now().isoformat()
            self.save_data()
            
            # Отправляем сообщение пользователю
            try:
                keyboard = [
                    [InlineKeyboardButton("📋 Мои тикеты", callback_data="my_tickets")],
                    [InlineKeyboardButton("🔙 На главную", callback_data="back_to_start")]
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
        
        else:
            # Если админ просто пишет в бота без активного ответа на тикет
            keyboard = [
                [InlineKeyboardButton("👨‍💼 Панель админа", callback_data="admin_panel")],
                [InlineKeyboardButton("📊 Статистика", callback_data="admin_stats")],
                [InlineKeyboardButton("🔙 На главную", callback_data="back_to_start")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.message.reply_text(
                "👨‍💼 <b>Администратор</b>\n\n"
                "Вы находитесь в режиме администратора.\n"
                "Используйте панель администратора для работы с тикетами.",
                parse_mode='HTML',
                reply_markup=reply_markup
            )

    async def start_cleanup_task(self):
        """Запускает задачи для очистки"""
        # Запускаем проверку неактивных диалогов
        asyncio.create_task(self.check_inactive_ai_conversations())
        logger.info("Запущена задача проверки неактивных диалогов (24 часа)")

    def run(self):
        logger.info("Бот запущен!")
        # Запускаем задачи очистки
        asyncio.get_event_loop().run_until_complete(self.start_cleanup_task())
        self.app.run_polling()

if __name__ == "__main__":
    bot = SupportBot()
    bot.run()
