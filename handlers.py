import re
from telegram import Update
from telegram.ext import CommandHandler, MessageHandler, ContextTypes, filters
from deepseek import ask_deepseek
from reminder import schedule_reminders
from calendar_api import book_slot
import datetime

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        'Здравствуйте! 😊 Я — Ассистент салона "Непоседы". Подскажите, пожалуйста, на какую дату вам удобно записаться?'
    )
    context.user_data['history'] = []
    context.user_data['visit_time'] = None
    context.user_data['awaiting_name'] = False
    context.user_data['awaiting_phone'] = False
    context.user_data['client_name'] = None
    context.user_data['client_phone'] = None

async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    history = context.user_data.get('history', [])
    history = history[-8:]
    user_text = update.message.text.lower()

    # Проверка на просьбу о напоминании
    if re.search(r'напомн(и|ить|ание)', user_text):
        visit_time = context.user_data.get('visit_time')
        if visit_time:
            schedule_reminders(
                application=context.application,
                chat_id=update.effective_chat.id,
                visit_time=visit_time
            )
            await update.message.reply_text("Я обязательно напомню вам о визите — за день и за час до записи.")
        else:
            await update.message.reply_text("Пожалуйста, сначала укажите дату и время визита, чтобы я мог напомнить.")
        return

    # --- Парсим дату визита ---
    match = re.search(r'(\d{1,2})\s*(июля|августа|сентября|октября|ноября|декабря)\s*в\s*(\d{1,2}):(\d{2})', user_text)
    months = {
        'июля': 7, 'августа': 8, 'сентября': 9, 'октября': 10, 'ноября': 11, 'декабря': 12
    }
    if match:
        day = int(match.group(1))
        month = months.get(match.group(2))
        hour = int(match.group(3))
        minute = int(match.group(4))
        year = datetime.datetime.now().year
        visit_time = datetime.datetime(year, month, day, hour, minute)
        context.user_data['visit_time'] = visit_time
        context.user_data['awaiting_name'] = True
        context.user_data['client_name'] = None
        context.user_data['client_phone'] = None
        await update.message.reply_text(
            f"Записал ваш визит на {visit_time.strftime('%d.%m.%Y %H:%M')}. Пожалуйста, напишите ваше имя для подтверждения записи."
        )
        return

    # --- Если ждём имя ---
    if context.user_data.get('awaiting_name'):
        context.user_data['client_name'] = update.message.text.strip()
        context.user_data['awaiting_name'] = False
        context.user_data['awaiting_phone'] = True
        await update.message.reply_text("Спасибо! Теперь, пожалуйста, укажите ваш номер телефона для связи.")
        return

    # --- Если ждём телефон ---
    if context.user_data.get('awaiting_phone'):
        phone = re.sub(r"[^\d+]", "", update.message.text)
        if len(phone) < 10:
            await update.message.reply_text("Пожалуйста, укажите корректный номер телефона.")
            return
        context.user_data['client_phone'] = phone
        context.user_data['awaiting_phone'] = False
        visit_time = context.user_data.get('visit_time')
        name = context.user_data.get('client_name')
        
        # Создать событие в Google Calendar
        try:
            event_id = book_slot(visit_time, {
                'name': name,
                'phone': phone,
                'service': context.user_data.get('service', 'Не указана'),
                'child_age': context.user_data.get('child_age', '—')
            })
            if event_id:
                await update.message.reply_text(
                    f"Спасибо, {name}! Ваша запись на {visit_time.strftime('%d.%m.%Y %H:%M')} подтверждена и добавлена в календарь. Если нужно — могу напомнить о визите."
                )
            else:
                await update.message.reply_text(
                    f"Спасибо, {name}! Ваша запись на {visit_time.strftime('%d.%m.%Y %H:%M')} подтверждена. Если нужно — могу напомнить о визите."
                )
        except Exception as e:
            print(f"[ОШИБКА] При создании события в календаре: {e}")
            await update.message.reply_text(
                f"Спасибо, {name}! Ваша запись на {visit_time.strftime('%d.%m.%Y %H:%M')} подтверждена. Если нужно — могу напомнить о визите."
            )
        return

    # --- Обычный диалог ---
    # Показать "Печатает..." пока генерируется ответ
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    
    history.append({"role": "user", "content": update.message.text})
    response = ask_deepseek(update.message.text, history=history)
    history.append({"role": "assistant", "content": response})
    context.user_data['history'] = history
    await update.message.reply_text(response)

def setup_handlers(app):
    app.add_handler(CommandHandler('start', start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))