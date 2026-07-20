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
DEEPSEEK_API_KEY = "sk-f8fe99fc78514aa2a303a053fe0ae5a7"
DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"

# Состояния для ConversationHandler
TICKET_SUBJECT, TICKET_DESCRIPTION = range(2)

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
            allow_reentry=True
        )
        self.app.add_handler(conv_handler)
        
        # Обработчики callback
        self.app.add_handler(CallbackQueryHandler(self.button_handler))
        
        # Обработчик для обычных сообщений
        self.app.add_handler(MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            self.handle_all_messages
        ))
        
        # Обработчик для автоматического /start
        self.app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, self.welcome_new_user))
        
        # Добавляем обработчик ошибок - просто логируем
        self.app.add_error_handler(self.error_handler)

    async def error_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обрабатывает ошибки бота - только логируем"""
        error = context.error
        logger.error(f"Ошибка в боте: {error}", exc_info=error)
        return

    async def welcome_new_user(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Приветствует новых пользователей автоматически"""
        try:
            for user in update.message.new_chat_members:
                if user.is_bot and user.id == context.bot.id:
                    await self.start(update, context)
                elif not user.is_bot:
                    await context.bot.send_message(
                        chat_id=update.effective_chat.id,
                        text=f"👋 Добро пожаловать, {user.first_name}!",
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton("🚀 Начать", callback_data="start")]
                        ])
                    )
        except Exception as e:
            logger.error(f"Ошибка в welcome_new_user: {e}")

    def is_clearly_spam(self, text: str) -> bool:
        """Проверяет явный спам"""
        try:
            text_lower = text.lower()
            spam_phrases = [
                'купить', 'продать', 'заказать', 'скидка', 'акция', 'распродажа',
                'http://', 'https://', 'www.', '.com', '.ru', '.net',
                'заработок', 'бизнес', 'инвестиц', 'криптовалют',
                'рассылка', 'распространить', 'реклам', 'бесплатно',
                'партнерк', 'маркетинг', 'продвижен'
            ]
            if any(phrase in text_lower for phrase in spam_phrases):
                return True
            url_pattern = r'http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\\(\\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+'
            urls = re.findall(url_pattern, text)
            if len(urls) > 2:
                return True
        except Exception as e:
            logger.error(f"Ошибка в is_clearly_spam: {e}")
        return False

    def is_gibberish(self, text: str) -> bool:
        """Проверяет бессмысленный текст"""
        try:
            clean_text = text.replace(' ', '').lower()
            if len(clean_text) < 8:
                return False
            if any(clean_text.count(substr) > len(clean_text) // 2 for substr in [clean_text[i:i+3] for i in range(len(clean_text)-2)]):
                return True
            vowels = 'аеёиоуыэюяaeiou'
            consonant_streak = 0
            for char in clean_text:
                if char in vowels:
                    consonant_streak = 0
                else:
                    consonant_streak += 1
                    if consonant_streak > 10:
                        return True
            unique_chars = len(set(clean_text))
            if unique_chars < len(clean_text) * 0.3:
                return True
        except Exception as e:
            logger.error(f"Ошибка в is_gibberish: {e}")
        return False

    async def ask_ai_for_help(self, ticket_id: str, user_message: str = None) -> tuple:
        """Запрашивает у ИИ ответ"""
        try:
            ticket_data = self.tickets.get(ticket_id)
            if not ticket_data:
                return None, False, False, False
            
            context_messages = []
            for msg in ticket_data.get('messages', [])[-10:]:
                if msg.get('is_ai', False):
                    sender = "AI-ассистент"
                else:
                    sender = "Пользователь" if msg['from_user'] else "Оператор"
                context_messages.append(f"{sender}: {msg['text']}")
            
            problem_text = f"Тема: {ticket_data['subject']}\nОписание: {ticket_data['description']}"
            
            system_prompt = """Ты - оффициальный ИИ-ассистент технической поддержки компании FlatGame. Твоя задача - помочь пользователю решить его проблему.

ПРАВИЛА:
1. Ты оффициальный бот технической поддержки компании FlatGame
2. У компании есть свой лаунчер и игра поэтому все заявки будут по ним
3. Отвечай вежливо и профессионально на русском языке
4. Если информации недостаточно - задай 1-2 уточняющих вопроса
5. Если проблема решена и пользователь подтвердил - напиши "ПРОБЛЕМА РЕШЕНА"
6. Если пользователь просит оператора - напиши "ВЫЗВАН ОПЕРАТОР"
7. Никогда не вызывай оператора самостоятельно, только по просьбе пользователя
8. Если не знаешь ответа - задай уточняющие вопросы
9. Дай конкретные, полезные советы"""

            user_message_for_ai = f"""
Проблема пользователя:
{problem_text}

История диалога:
{chr(10).join(context_messages) if context_messages else 'Диалог только начинается'}

Последнее сообщение пользователя: {user_message if user_message else 'Нет новых сообщений'}

Ответь на русском языке. Если нужно задать уточняющий вопрос - задай его. Если проблема решена - подтверди это.
"""

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
            
            response = requests.post(
                DEEPSEEK_API_URL,
                headers=headers,
                json=payload,
                timeout=15
            )
            
            if response.status_code == 200:
                result = response.json()
                ai_response = result['choices'][0]['message']['content']
                
                wants_operator = "ВЫЗВАН ОПЕРАТОР" in ai_response
                problem_solved = "ПРОБЛЕМА РЕШЕНА" in ai_response
                needs_clarification = "?" in ai_response or "уточн" in ai_response.lower()
                
                ai_response = ai_response.replace("ВЫЗВАН ОПЕРАТОР", "").replace("ПРОБЛЕМА РЕШЕНА", "").strip()
                
                return ai_response, wants_operator, needs_clarification, problem_solved
            else:
                logger.error(f"Ошибка API: {response.status_code}")
                return None, False, False, False
                
        except Exception as e:
            logger.error(f"Ошибка при обращении к AI: {e}")
            return None, False, False, False

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            user = update.effective_user
            
            keyboard = [
                [InlineKeyboardButton("🎫 Создать тикет", callback_data="create_ticket")],
                [InlineKeyboardButton("📋 Мои тикеты", callback_data="my_tickets")],
                [InlineKeyboardButton("📋 Помощь", callback_data="help")]
            ]
            
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
        except Exception as e:
            logger.error(f"Ошибка в start: {e}")

    async def show_user_tickets(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
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
                        "📭 <b>У вас пока нет тикетов</b>",
                        parse_mode='HTML',
                        reply_markup=reply_markup
                    )
                else:
                    await update.message.reply_text(
                        "📭 <b>У вас пока нет тикетов</b>",
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
                    "📋 <b>Ваши тикеты:</b>",
                    parse_mode='HTML',
                    reply_markup=reply_markup
                )
            else:
                await update.message.reply_text(
                    "📋 <b>Ваши тикеты:</b>",
                    parse_mode='HTML',
                    reply_markup=reply_markup
                )
        except Exception as e:
            logger.error(f"Ошибка в show_user_tickets: {e}")

    async def show_ticket_to_user(self, query, ticket_id):
        try:
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
                ai_responses = sum(1 for msg in ticket_data.get('messages', []) if msg.get('is_ai', False))
                if ai_responses >= 2:
                    keyboard.insert(0, [InlineKeyboardButton("📞 Позвать оператора", callback_data=f"user_call_operator_{ticket_id}")])
            
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.edit_message_text(
                message_text,
                reply_markup=reply_markup,
                parse_mode='HTML'
            )
        except Exception as e:
            logger.error(f"Ошибка в show_ticket_to_user: {e}")

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
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
        except Exception as e:
            logger.error(f"Ошибка в help_command: {e}")

    async def stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
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
        except Exception as e:
            logger.error(f"Ошибка в stats: {e}")

    async def create_ticket(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            context.user_data.pop('ticket_subject', None)
            context.user_data.pop('ticket_description', None)
            
            if update.callback_query:
                query = update.callback_query
                await query.answer()
                await query.message.reply_text(
                    "🎫 <b>Создание нового тикета</b>\n\n📋 <b>Введите заголовок тикета:</b>",
                    parse_mode='HTML'
                )
            else:
                await update.message.reply_text(
                    "🎫 <b>Создание нового тикета</b>\n\n📋 <b>Введите заголовок тикета:</b>",
                    parse_mode='HTML'
                )
        except Exception as e:
            logger.error(f"Ошибка в create_ticket: {e}")
            try:
                await update.message.reply_text("🎫 Создание нового тикета\n\nВведите заголовок тикета:")
            except:
                pass
        
        return TICKET_SUBJECT

    async def get_ticket_subject(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            user = update.effective_user
            subject = update.message.text
            
            logger.info(f"Получен заголовок от {user.id}: {subject}")
            
            spam_attempts = self.user_spam_attempts.get(user.id, 0)
            
            if spam_attempts > 5:
                await update.message.reply_text("❌ Слишком много попыток. Попробуйте позже.")
                return TICKET_SUBJECT
            
            if len(subject.strip()) < 3:
                self.user_spam_attempts[user.id] = spam_attempts + 1
                self.save_data()
                await update.message.reply_text("❌ Заголовок слишком короткий. Введите более подробный заголовок:")
                return TICKET_SUBJECT
            
            if len(subject) > 200:
                await update.message.reply_text("❌ Заголовок слишком длинный. Введите более короткий заголовок:")
                return TICKET_SUBJECT
            
            if self.is_clearly_spam(subject) or self.is_gibberish(subject):
                self.user_spam_attempts[user.id] = spam_attempts + 1
                self.save_data()
                await update.message.reply_text("❌ Заголовок содержит недопустимый контент. Введите другой заголовок:")
                return TICKET_SUBJECT
            
            self.user_spam_attempts[user.id] = 0
            self.save_data()
            
            context.user_data['ticket_subject'] = subject
            
            await update.message.reply_text(
                "📝 <b>Теперь опишите проблему подробно:</b>\n\n"
                "• <b>Что произошло?</b>\n"
                "• <b>Когда это случилось?</b>\n"
                "• <b>Все детали</b>\n\n"
                "<i>Напишите описание:</i>",
                parse_mode='HTML'
            )
        except Exception as e:
            logger.error(f"Ошибка в get_ticket_subject: {e}")
            try:
                await update.message.reply_text("📝 Теперь опишите проблему подробно:")
            except:
                pass
        
        return TICKET_DESCRIPTION

    async def get_ticket_description(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            user = update.effective_user
            description = update.message.text
            
            logger.info(f"Получено описание от {user.id}: {description[:50]}...")
            
            spam_attempts = self.user_spam_attempts.get(user.id, 0)
            
            if spam_attempts > 5:
                await update.message.reply_text("❌ Слишком много попыток. Попробуйте позже.")
                return TICKET_DESCRIPTION
            
            if len(description.strip()) < 10:
                self.user_spam_attempts[user.id] = spam_attempts + 1
                self.save_data()
                await update.message.reply_text("❌ Описание слишком короткое. Опишите проблему подробнее:")
                return TICKET_DESCRIPTION
            
            if len(description) > 2000:
                await update.message.reply_text("❌ Описание слишком длинное. Максимум 2000 символов:")
                return TICKET_DESCRIPTION
            
            if self.is_clearly_spam(description) or self.is_gibberish(description):
                self.user_spam_attempts[user.id] = spam_attempts + 1
                self.save_data()
                await update.message.reply_text("❌ Ваше сообщение содержит недопустимый контент.")
                return TICKET_DESCRIPTION
            
            self.user_spam_attempts[user.id] = 0
            self.save_data()
            
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
            
            self.tickets[ticket_id] = ticket_data
            self.user_tickets[user.id].append(ticket_id)
            self.open_tickets.add(ticket_id)
            self.save_data()
            
            # Получаем ответ от AI
            ai_response, wants_operator, needs_clarification, problem_solved = await self.ask_ai_for_help(ticket_id, description)
            
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
            
            # Создаем клавиатуру
            keyboard = [
                [InlineKeyboardButton("📋 Мои тикеты", callback_data="my_tickets")],
                [InlineKeyboardButton("🔙 На главную", callback_data="back_to_start")]
            ]
            
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            # Отправляем ответ пользователю
            if ai_response:
                await update.message.reply_text(
                    f"🎫 <b>Тикет создан!</b> <code>{ticket_id}</code>\n\n"
                    f"🤖 <b>AI-помощник:</b>\n\n{html.escape(ai_response)}",
                    parse_mode='HTML',
                    reply_markup=reply_markup
                )
            else:
                ticket_data['waiting_for_operator'] = True
                ticket_data['ai_processed'] = False
                self.save_data()
                
                await update.message.reply_text(
                    f"🎫 <b>Тикет создан!</b> <code>{ticket_id}</code>\n\n"
                    f"⏳ <b>ИИ временно недоступен</b>\n"
                    f"Мы передали ваш запрос оператору.",
                    parse_mode='HTML',
                    reply_markup=reply_markup
                )
                
                await self.notify_admin_about_ticket(ticket_id)
                
        except Exception as e:
            logger.error(f"Ошибка в get_ticket_description: {e}")
            # Пытаемся отправить хотя бы базовое сообщение
            try:
                await update.message.reply_text(
                    f"✅ <b>Тикет создан!</b>",
                    parse_mode='HTML'
                )
            except:
                pass
        
        # Очищаем данные и завершаем диалог
        context.user_data.pop('ticket_subject', None)
        context.user_data.pop('ticket_description', None)
        return ConversationHandler.END

    async def notify_admin_about_ticket(self, ticket_id):
        """Уведомляет админа о новом тикете"""
        try:
            ticket_data = self.tickets.get(ticket_id)
            if not ticket_data:
                return
            
            admin_message = f"""
🚨 <b>НОВЫЙ ТИКЕТ ТРЕБУЕТ ОПЕРАТОРА!</b>

🎫 <b>ID:</b> <code>{ticket_id}</code>
👤 <b>Пользователь:</b> {ticket_data['user_full_name']} (@{ticket_data['username']})
📋 <b>Тема:</b> {html.escape(ticket_data['subject'])}
📝 <b>Описание:</b> {html.escape(ticket_data['description'][:300])}
⏰ <b>Время:</b> {datetime.now().strftime('%d.%m.%Y %H:%M')}
            """
            
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
        try:
            keyboard = [
                [InlineKeyboardButton("🎫 Создать тикет", callback_data="create_ticket")],
                [InlineKeyboardButton("🔙 На главную", callback_data="back_to_start")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            context.user_data.pop('ticket_subject', None)
            context.user_data.pop('ticket_description', None)
            
            await update.message.reply_text(
                "❌ <b>Создание тикета отменено.</b>",
                parse_mode='HTML',
                reply_markup=reply_markup
            )
        except Exception as e:
            logger.error(f"Ошибка в cancel: {e}")
        return ConversationHandler.END

    async def admin_panel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            user = update.effective_user
            if user.id != ADMIN_ID:
                if update.callback_query:
                    await update.callback_query.answer("❌ У вас нет прав.", show_alert=True)
                else:
                    await update.message.reply_text("❌ У вас нет прав.")
                return
            
            if update.callback_query:
                query = update.callback_query
                await query.answer()
                message = query
            else:
                message = update.message
            
            keyboard = []
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
            
            waiting_operator = sum(1 for t in self.tickets.values() if t.get('waiting_for_operator', False) and t['status'] == 'open')
            
            text = f"""
👨‍💼 <b>Панель администратора</b>

📊 <b>Статистика:</b>
• 🔓 Открытых тикетов: <b>{len(self.open_tickets)}</b>
• 📞 Ожидают оператора: <b>{waiting_operator}</b>
• ✅ Закрытых тикетов: <b>{len(self.tickets) - len(self.open_tickets)}</b>
• 👤 Пользователей: <b>{len(self.user_tickets)}</b>
            """
            
            if update.callback_query:
                await query.edit_message_text(text, parse_mode='HTML', reply_markup=reply_markup)
            else:
                await message.reply_text(text, parse_mode='HTML', reply_markup=reply_markup)
        except Exception as e:
            logger.error(f"Ошибка в admin_panel: {e}")

    def get_time_ago(self, dt):
        try:
            now = datetime.now()
            diff = now - dt
            minutes = diff.total_seconds() // 60
            hours = minutes // 60
            days = hours // 24
            
            if days > 0:
                return f"{int(days)}д"
            elif hours > 0:
                return f"{int(hours)}ч"
            elif minutes > 0:
                return f"{int(minutes)}м"
            else:
                return "только что"
        except:
            return "недавно"

    async def button_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
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
        except Exception as e:
            logger.error(f"Ошибка в button_handler: {e}")

    async def call_operator(self, query, ticket_id):
        """Вызывает оператора"""
        try:
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
            
            ticket_data['waiting_for_operator'] = True
            ticket_data['ai_processed'] = False
            self.save_data()
            
            await self.notify_admin_about_ticket(ticket_id)
            
            keyboard = [
                [InlineKeyboardButton("📋 Мои тикеты", callback_data="my_tickets")],
                [InlineKeyboardButton("🔙 На главную", callback_data="back_to_start")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.edit_message_text(
                f"📞 <b>Оператор вызван!</b>\n\n"
                f"Ваш запрос передан оператору.",
                parse_mode='HTML',
                reply_markup=reply_markup
            )
        except Exception as e:
            logger.error(f"Ошибка в call_operator: {e}")

    async def show_user_tickets_to_admin(self, query, user_id):
        """Показывает тикеты пользователя админу"""
        try:
            if user_id not in self.user_tickets or not self.user_tickets[user_id]:
                keyboard = [[InlineKeyboardButton("↩️ Назад", callback_data="admin_panel")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await query.edit_message_text("📭 <b>У этого пользователя нет тикетов</b>", parse_mode='HTML', reply_markup=reply_markup)
                return
            
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
                    btn_text = f"{status_emoji} {time_ago} - {ticket['subject'][:20]}..."
                    keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"admin_view_{ticket_id}")])
            
            keyboard.append([InlineKeyboardButton("↩️ Назад", callback_data="admin_panel")])
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            user_stats_text = f"""
👤 <b>Пользователь:</b> {full_name} (@{username})
📊 <b>Всего тикетов:</b> {len(tickets_list)}
📋 <b>Тикеты:</b>
            """
            
            await query.edit_message_text(user_stats_text, parse_mode='HTML', reply_markup=reply_markup)
        except Exception as e:
            logger.error(f"Ошибка в show_user_tickets_to_admin: {e}")

    async def show_user_tickets_from_callback(self, query):
        try:
            user_id = query.from_user.id
            
            if user_id not in self.user_tickets or not self.user_tickets[user_id]:
                keyboard = [
                    [InlineKeyboardButton("🎫 Создать тикет", callback_data="create_ticket")],
                    [InlineKeyboardButton("🔙 На главную", callback_data="back_to_start")]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await query.edit_message_text("📭 <b>У вас пока нет тикетов</b>", parse_mode='HTML', reply_markup=reply_markup)
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
            
            await query.edit_message_text("📋 <b>Ваши тикеты:</b>", parse_mode='HTML', reply_markup=reply_markup)
        except Exception as e:
            logger.error(f"Ошибка в show_user_tickets_from_callback: {e}")

    async def show_admin_stats(self, query):
        try:
            ai_tickets = sum(1 for t in self.tickets.values() if t.get('ai_processed', False))
            closed_by_ai = sum(1 for t in self.tickets.values() if t.get('ai_processed', False) and t['status'] == 'closed')
            waiting_operator = sum(1 for t in self.tickets.values() if t.get('waiting_for_operator', False) and t['status'] == 'open')
            
            stats_text = f"""
📊 <b>Детальная статистика</b>

🎫 <b>Всего тикетов:</b> {len(self.tickets)}
🔓 <b>Открытых:</b> {len(self.open_tickets)}
✅ <b>Закрытых:</b> {len(self.tickets) - len(self.open_tickets)}
👤 <b>Пользователей:</b> {len(self.user_tickets)}
🤖 <b>Обработано AI:</b> {ai_tickets}
✅ <b>Закрыто AI:</b> {closed_by_ai}
📞 <b>Ожидают оператора:</b> {waiting_operator}
            """
            keyboard = [
                [InlineKeyboardButton("↩️ Назад", callback_data="admin_panel")],
                [InlineKeyboardButton("🔄 Обновить", callback_data="admin_stats")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(stats_text, parse_mode='HTML', reply_markup=reply_markup)
        except Exception as e:
            logger.error(f"Ошибка в show_admin_stats: {e}")

    async def show_admin_users(self, query):
        try:
            users_text = "👥 <b>Список пользователей:</b>\n\n"
            
            for user_id, tickets_list in list(self.user_tickets.items())[:15]:
                user_tickets = [self.tickets[tid] for tid in tickets_list if tid in self.tickets]
                open_count = sum(1 for t in user_tickets if t['status'] == 'open')
                closed_count = len(user_tickets) - open_count
                
                user_info = next((t for t in user_tickets if t), {})
                username = user_info.get('username', 'Unknown')
                full_name = user_info.get('user_full_name', 'Unknown')
                
                users_text += f"👤 {full_name} (@{username})\n"
                users_text += f"   🎫 {len(user_tickets)} тикетов (🔓{open_count} ✅{closed_count})\n"
                users_text += f"   <code>admin_user_tickets_{user_id}</code>\n\n"
            
            keyboard = [
                [InlineKeyboardButton("↩️ Назад", callback_data="admin_panel")],
                [InlineKeyboardButton("🔄 Обновить", callback_data="admin_users")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(users_text, parse_mode='HTML', reply_markup=reply_markup)
        except Exception as e:
            logger.error(f"Ошибка в show_admin_users: {e}")

    async def show_ticket_to_admin(self, query, ticket_id):
        try:
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
📞 <b>Ожидает оператора:</b> {'✅' if ticket_data.get('waiting_for_operator', False) else '❌'}

📝 <b>Описание:</b>
{html.escape(ticket_data['description'])}

💬 <b>История:</b>
            """
            
            for msg in ticket_data.get('messages', []):
                if msg.get('is_ai', False):
                    sender = "🤖 AI"
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
            
            keyboard.append([InlineKeyboardButton("👤 Все тикеты пользователя", callback_data=f"admin_user_tickets_{ticket_data['user_id']}")])
            keyboard.append([InlineKeyboardButton("↩️ Назад", callback_data="admin_panel")])
            
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.edit_message_text(message_text, reply_markup=reply_markup, parse_mode='HTML')
        except Exception as e:
            logger.error(f"Ошибка в show_ticket_to_admin: {e}")

    async def close_ticket(self, query, ticket_id, closed_by_ai=False, closed_by_timeout=False):
        try:
            ticket_data = self.tickets.get(ticket_id)
            if not ticket_data:
                if query:
                    await query.edit_message_text("❌ Тикет не найден!")
                return
                
            ticket_data['status'] = 'closed'
            if ticket_id in self.open_tickets:
                self.open_tickets.remove(ticket_id)
            self.save_data()
            
            try:
                keyboard = [
                    [InlineKeyboardButton("📋 Мои тикеты", callback_data="my_tickets")],
                    [InlineKeyboardButton("🎫 Новый тикет", callback_data="create_ticket")],
                    [InlineKeyboardButton("🔙 На главную", callback_data="back_to_start")]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                close_message = f"✅ <b>Тикет закрыт</b>\n\nВаш тикет <code>{ticket_id}</code> был закрыт."
                if closed_by_ai:
                    close_message += "\n\n🤖 Проблема решена с помощью AI."
                elif closed_by_timeout:
                    close_message += "\n\n⏰ Тикет закрыт из-за отсутствия активности (24 часа)."
                
                if ticket_data.get('messages'):
                    last_message = ticket_data['messages'][-1]
                    if not last_message.get('is_ai', False) and not last_message['from_user']:
                        close_message += f"\n\n📝 <b>Последний ответ:</b>\n{html.escape(last_message['text'])}"
                
                close_message += "\n\nСпасибо за обращение!"
                
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
                    await query.edit_message_text(f"✅ <b>Тикет закрыт!</b>\n\n<code>{ticket_id}</code>", parse_mode='HTML', reply_markup=reply_markup)
                except Exception as e:
                    logger.error(f"Ошибка при редактировании: {e}")
        except Exception as e:
            logger.error(f"Ошибка в close_ticket: {e}")

    async def check_inactive_ai_conversations(self):
        """Проверяет неактивные диалоги и закрывает через 24 часа"""
        while True:
            try:
                await asyncio.sleep(3600)
                
                now = datetime.now()
                timeout_hours = 24
                
                for ticket_id, ticket_data in list(self.tickets.items()):
                    if ticket_data['status'] != 'open':
                        continue
                    
                    if ticket_data.get('waiting_for_operator', False):
                        continue
                    
                    last_activity = datetime.fromisoformat(ticket_data.get('last_activity', ticket_data['created_at']))
                    time_diff = (now - last_activity).total_seconds() / 3600
                    
                    if time_diff >= timeout_hours:
                        logger.info(f"Автоматическое закрытие тикета {ticket_id} ({time_diff:.1f} часов)")
                        await self.close_ticket(None, ticket_id, closed_by_timeout=True)
                        
            except Exception as e:
                logger.error(f"Ошибка в check_inactive_ai_conversations: {e}")

    async def handle_all_messages(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Единый обработчик для всех сообщений"""
        try:
            # Проверяем, не находится ли пользователь в диалоге создания тикета
            if context.user_data.get('ticket_subject') is not None:
                logger.info("Пользователь в диалоге создания тикета, пропускаем")
                return
            
            user_id = update.effective_user.id
            user_message = update.message.text
            
            logger.info(f"Получено сообщение от {user_id}: {user_message[:50]}...")
            
            # Если это админ и он отвечает на тикет
            if user_id == ADMIN_ID and 'admin_replying_to' in context.user_data:
                ticket_id = context.user_data['admin_replying_to']
                ticket_data = self.tickets.get(ticket_id)
                
                if not ticket_data:
                    await update.message.reply_text("❌ Тикет не найден!")
                    del context.user_data['admin_replying_to']
                    return
                
                ticket_data['messages'].append({
                    'text': user_message,
                    'from_user': False,
                    'timestamp': datetime.now().isoformat()
                })
                ticket_data['last_activity'] = datetime.now().isoformat()
                self.save_data()
                
                try:
                    keyboard = [
                        [InlineKeyboardButton("📋 Мои тикеты", callback_data="my_tickets")],
                        [InlineKeyboardButton("🔙 На главную", callback_data="back_to_start")]
                    ]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    
                    await context.bot.send_message(
                        chat_id=ticket_data['user_id'],
                        text=f"👨‍💼 <b>Ответ поддержки:</b>\n\n{html.escape(user_message)}",
                        parse_mode='HTML',
                        reply_markup=reply_markup
                    )
                except Exception as e:
                    logger.error(f"Не удалось отправить сообщение: {e}")
                
                del context.user_data['admin_replying_to']
                
                keyboard = [
                    [InlineKeyboardButton("📊 Панель админа", callback_data="admin_panel")],
                    [InlineKeyboardButton("👀 Посмотреть тикет", callback_data=f"admin_view_{ticket_id}")]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await update.message.reply_text("✅ Ответ отправлен!", reply_markup=reply_markup)
                return
            
            # Если это админ и он просто пишет
            if user_id == ADMIN_ID:
                keyboard = [
                    [InlineKeyboardButton("👨‍💼 Панель админа", callback_data="admin_panel")],
                    [InlineKeyboardButton("📊 Статистика", callback_data="admin_stats")],
                    [InlineKeyboardButton("🔙 На главную", callback_data="back_to_start")]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await update.message.reply_text(
                    "👨‍💼 <b>Администратор</b>\n\nИспользуйте панель администратора.",
                    parse_mode='HTML',
                    reply_markup=reply_markup
                )
                return
            
            # Обычный пользователь - ищем активный тикет
            active_ticket_id = None
            if user_id in self.user_tickets:
                for ticket_id in reversed(self.user_tickets[user_id]):
                    ticket = self.tickets.get(ticket_id)
                    if ticket and ticket['status'] == 'open':
                        active_ticket_id = ticket_id
                        break
            
            if active_ticket_id:
                ticket_data = self.tickets.get(active_ticket_id)
                
                # Если тикет ожидает оператора
                if ticket_data.get('waiting_for_operator', False):
                    ticket_data['messages'].append({
                        'text': user_message,
                        'from_user': True,
                        'timestamp': datetime.now().isoformat()
                    })
                    ticket_data['last_activity'] = datetime.now().isoformat()
                    self.save_data()
                    
                    try:
                        keyboard = [
                            [InlineKeyboardButton("💬 Ответить", callback_data=f"admin_reply_{active_ticket_id}")],
                            [InlineKeyboardButton("👀 Посмотреть тикет", callback_data=f"admin_view_{active_ticket_id}")]
                        ]
                        reply_markup = InlineKeyboardMarkup(keyboard)
                        
                        await context.bot.send_message(
                            chat_id=ADMIN_ID,
                            text=f"📞 <b>Ответ пользователя</b>\n\nТикет: {active_ticket_id}\nСообщение: {html.escape(user_message)}",
                            parse_mode='HTML',
                            reply_markup=reply_markup
                        )
                    except Exception as e:
                        logger.error(f"Не удалось отправить админу: {e}")
                    
                    await update.message.reply_text("✅ Ваш ответ отправлен оператору!")
                    return
                
                # Обрабатываем через AI
                ticket_data['messages'].append({
                    'text': user_message,
                    'from_user': True,
                    'timestamp': datetime.now().isoformat()
                })
                ticket_data['last_activity'] = datetime.now().isoformat()
                self.save_data()
                
                ai_response, wants_operator, needs_clarification, problem_solved = await self.ask_ai_for_help(active_ticket_id, user_message)
                
                if problem_solved:
                    await self.close_ticket(None, active_ticket_id, closed_by_ai=True)
                    
                    keyboard = [
                        [InlineKeyboardButton("📋 Мои тикеты", callback_data="my_tickets")],
                        [InlineKeyboardButton("🎫 Новый тикет", callback_data="create_ticket")],
                        [InlineKeyboardButton("🔙 На главную", callback_data="back_to_start")]
                    ]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    
                    await update.message.reply_text(
                        f"✅ <b>Проблема решена!</b>\n\nТикет закрыт.",
                        parse_mode='HTML',
                        reply_markup=reply_markup
                    )
                    return
                
                if wants_operator:
                    ticket_data['waiting_for_operator'] = True
                    ticket_data['ai_processed'] = False
                    self.save_data()
                    
                    await self.notify_admin_about_ticket(active_ticket_id)
                    
                    keyboard = [
                        [InlineKeyboardButton("📋 Мои тикеты", callback_data="my_tickets")],
                        [InlineKeyboardButton("🔙 На главную", callback_data="back_to_start")]
                    ]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    
                    await update.message.reply_text("📞 <b>Оператор вызван!</b>", parse_mode='HTML', reply_markup=reply_markup)
                    return
                
                if ai_response:
                    ticket_data['messages'].append({
                        'text': ai_response,
                        'from_user': False,
                        'timestamp': datetime.now().isoformat(),
                        'is_ai': True
                    })
                    ticket_data['ai_response_count'] = ticket_data.get('ai_response_count', 0) + 1
                    ticket_data['last_activity'] = datetime.now().isoformat()
                    self.save_data()
                    
                    keyboard = [
                        [InlineKeyboardButton("📋 Мои тикеты", callback_data="my_tickets")],
                        [InlineKeyboardButton("🔙 На главную", callback_data="back_to_start")]
                    ]
                    
                    if ticket_data['ai_response_count'] >= 2:
                        keyboard.insert(0, [InlineKeyboardButton("📞 Позвать оператора", callback_data=f"user_call_operator_{active_ticket_id}")])
                    
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    
                    await update.message.reply_text(
                        f"🤖 <b>AI-помощник:</b>\n\n{html.escape(ai_response)}",
                        parse_mode='HTML',
                        reply_markup=reply_markup
                    )
                else:
                    ticket_data['waiting_for_operator'] = True
                    ticket_data['ai_processed'] = False
                    self.save_data()
                    
                    await self.notify_admin_about_ticket(active_ticket_id)
                    
                    keyboard = [
                        [InlineKeyboardButton("📋 Мои тикеты", callback_data="my_tickets")],
                        [InlineKeyboardButton("🔙 На главную", callback_data="back_to_start")]
                    ]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    
                    await update.message.reply_text(
                        "⏳ <b>ИИ недоступен</b>\n\nПередано оператору.",
                        parse_mode='HTML',
                        reply_markup=reply_markup
                    )
                return
            
            # Нет активного тикета
            spam_attempts = self.user_spam_attempts.get(user_id, 0)
            if spam_attempts > 5:
                await update.message.reply_text("❌ Слишком много сообщений. Создайте тикет.")
                return
            
            if len(user_message.strip()) < 3:
                self.user_spam_attempts[user_id] = spam_attempts + 1
                self.save_data()
                await update.message.reply_text("❌ Сообщение слишком короткое.")
                return
            
            if self.is_clearly_spam(user_message) or self.is_gibberish(user_message):
                self.user_spam_attempts[user_id] = spam_attempts + 1
                self.save_data()
                await update.message.reply_text("❌ Недопустимый контент.")
                return
            
            self.user_spam_attempts[user_id] = 0
            self.save_data()
            
            keyboard = [
                [InlineKeyboardButton("🎫 Создать тикет", callback_data="create_ticket")],
                [InlineKeyboardButton("📋 Мои тикеты", callback_data="my_tickets")],
                [InlineKeyboardButton("🔙 На главную", callback_data="back_to_start")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.message.reply_text(
                "💬 <b>Создайте тикет для обращения в поддержку:</b>",
                parse_mode='HTML',
                reply_markup=reply_markup
            )
        except Exception as e:
            logger.error(f"Ошибка в handle_all_messages: {e}")

    async def start_cleanup_task(self):
        """Запускает задачи для очистки"""
        asyncio.create_task(self.check_inactive_ai_conversations())
        logger.info("Запущена проверка неактивных диалогов (24 часа)")

    def run(self):
        logger.info("Бот запущен!")
        asyncio.get_event_loop().run_until_complete(self.start_cleanup_task())
        self.app.run_polling()

if __name__ == "__main__":
    bot = SupportBot()
    bot.run()
