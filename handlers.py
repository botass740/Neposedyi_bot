import re
import os
import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CommandHandler, MessageHandler, ContextTypes, filters, CallbackQueryHandler
from deepseek import ask_deepseek
from reminder import schedule_reminders, schedule_monthly_reminder, schedule_rating_request
from calendar_api import book_slot, list_events_for_date, delete_event, update_event_time, is_slot_free, merge_client_into_event
from dotenv import load_dotenv
from zoneinfo import ZoneInfo
from typing import Optional
import logging
import json
from state_store import get_user_state, update_user_state
from db import upsert_client, add_booking, get_last_master_for_client, add_rating
from textwrap import wrap
import dateparser
from datetime import time as dtime
import json
from reminder import scheduler
from calendar_api import get_free_slots
from masters_config import MASTERS, get_master_by_id, get_all_masters, get_master_name, get_master_by_name
from promotions_config import check_promotion

async def send_chunked(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str, chunk_size: int = 3500) -> None:
    if text is None:
        return
    if len(text) <= chunk_size:
        await context.bot.send_message(chat_id=chat_id, text=text)
        return
    paragraphs = text.split("\n\n")
    for para in paragraphs:
        if len(para) <= chunk_size:
            await context.bot.send_message(chat_id=chat_id, text=para)
        else:
            for piece in wrap(para, width=chunk_size, replace_whitespace=False, break_long_words=False):
                await context.bot.send_message(chat_id=chat_id, text=piece)

# Загружаем переменные окружения
load_dotenv()
logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler('bot.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
TZ = ZoneInfo('Europe/Moscow')

# --- Цены (подгрузка из распарсенного файла) ---
PRICES_FILE = 'prices.json'

def _load_prices() -> dict:
    try:
        with open(PRICES_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}

def _find_price_entry(query_text: str) -> tuple[str | None, str | None]:
    prices = _load_prices()
    if not prices:
        return None, None
    t = query_text.lower()
    # Примитивное сопоставление по ключевым словам
    service_filters = []
    if 'мужск' in t or 'взросл' in t:
        service_filters.append('муж')
    if 'детск' in t or 'ребён' in t or 'ребен' in t:
        service_filters.append('дет')
    if 'женск' in t:
        service_filters.append('жен')
    if 'стриж' in t:
        service_filters.append('стриж')
    if 'уклад' in t:
        service_filters.append('уклад')
    if 'плетен' in t:
        service_filters.append('плет')
    if 'окраш' in t or 'колор' in t:
        service_filters.append('окраш')

    # Ищем по ключам ценника
    best_key = None
    for name in prices.keys():
        name_l = name.lower()
        if all(sf in name_l for sf in service_filters) if service_filters else True:
            # предпочтем более конкретные совпадения
            if best_key is None or len(name_l) > len(best_key.lower()):
                best_key = name
    if not best_key:
        # fallback: любая "стрижка"
        for name in prices.keys():
            if 'стриж' in name.lower():
                best_key = name
                break
    if not best_key:
        return None, None
    return best_key, prices.get(best_key)

# ID чата администратора
ADMIN_CHAT_ID_ENV = os.getenv('ADMIN_CHAT_ID')
if not ADMIN_CHAT_ID_ENV:
    raise ValueError('ADMIN_CHAT_ID не найден в переменных окружения')
try:
    ADMIN_CHAT_ID = int(ADMIN_CHAT_ID_ENV)
except ValueError:
    raise ValueError('ADMIN_CHAT_ID должен быть числом (chat_id администратора)')

# --- Валидация и нормализация телефона РФ ---
def normalize_ru_phone(raw_phone: str) -> Optional[str]:
    """Пытается привести номер к формату +7XXXXXXXXXX (ровно 10 цифр после +7). Возвращает None, если номер некорректен."""
    if not raw_phone:
        return None
    
    # Убираем все нецифровые символы
    digits = re.sub(r'\D', '', raw_phone)
    
    # Проверяем длину и корректность
    if len(digits) == 11:
        # 11 цифр: 7XXXXXXXXXX или 8XXXXXXXXXX
        if digits[0] in ('7', '8'):
            phone = '+7' + digits[1:]
            # Проверяем, что после +7 ровно 10 цифр
            if len(phone) == 12:  # +7 (2 символа) + 10 цифр = 12
                return phone
    elif len(digits) == 10:
        # 10 цифр: 9XXXXXXXXX (без кода страны)
        if digits[0] == '9':
            return '+7' + digits
    
    # Если длина не подходит, возвращаем None
    return None

def suggest_time_slots(for_date: datetime.date, preference: Optional[str] = None) -> list[str]:
    """
    Возвращает 2–3 рекомендованных слота времени в зависимости от предпочтения,
    но только те, что ещё не прошли (если дата — сегодня).
    """
    morning = ["10:00", "11:30"]
    day = ["14:00", "15:30"]
    evening = ["18:00", "19:00"]
    slots = []
    if preference == 'morning':
        slots = morning + [day[0]]
    elif preference == 'day':
        slots = day + [evening[0]]
    elif preference == 'evening':
        slots = evening + [day[0]]
    else:
        slots = [morning[0], day[0], evening[0]]

    # Фильтруем слоты, если дата — сегодня
    now = datetime.datetime.now(tz=TZ)
    if for_date == now.date():
        filtered = []
        for s in slots:
            hour, minute = map(int, s.split(':'))
            slot_dt = datetime.datetime.combine(for_date, datetime.time(hour, minute), tzinfo=TZ)
            if slot_dt > now:
                filtered.append(s)
        return filtered
    # Если дата в прошлом — не предлагать ничего
    if for_date < now.date():
        return []
    return slots

def detect_time_preference(text: str) -> Optional[str]:
    t = text.lower()
    if 'утр' in t:
        return 'morning'
    if 'дн' in t:
        return 'day'
    if 'вечер' in t:
        return 'evening'
    return None

def parse_child_age(text: str) -> Optional[int]:
    m = re.search(r'(\d{1,2})\s*(год|года|лет)', text.lower())
    if m:
        try:
            return int(m.group(1))
        except Exception:
            return None
    return None

def _save_context_state(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    now_iso = datetime.datetime.now(tz=TZ).isoformat()
    state = {
        'visit_time': context.user_data.get('visit_time').isoformat() if context.user_data.get('visit_time') else None,
        'client_name': context.user_data.get('client_name'),
        'client_phone': context.user_data.get('client_phone'),
        'service': context.user_data.get('service'),
        'child_age': context.user_data.get('child_age'),
        'date': context.user_data.get('date'),
        'time': context.user_data.get('time'),
        'greeted': context.user_data.get('greeted', False),
        'last_interaction': now_iso,
        'pending_date': context.user_data.get('pending_date'),
        'master_id': context.user_data.get('master_id'),
        'master_name': context.user_data.get('master_name'),
        'master_selection_shown': context.user_data.get('master_selection_shown', False),
        'phone_refused': context.user_data.get('phone_refused', False),
        'recent_booking': context.user_data.get('recent_booking'),
        'last_visit_time': context.user_data.get('last_visit_time'),
        'last_service': context.user_data.get('last_service')
    }
    update_user_state(chat_id, state)

def _load_context_state(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = get_user_state(chat_id)
    if not state:
        return
    visit_time = state.get('visit_time')
    context.user_data['visit_time'] = datetime.datetime.fromisoformat(visit_time) if visit_time else None
    context.user_data['client_name'] = state.get('client_name')
    context.user_data['client_phone'] = state.get('client_phone')
    context.user_data['service'] = state.get('service')
    context.user_data['child_age'] = state.get('child_age')
    context.user_data['date'] = state.get('date')
    context.user_data['time'] = state.get('time')
    context.user_data['greeted'] = state.get('greeted', False)
    context.user_data['last_interaction'] = state.get('last_interaction')
    context.user_data['pending_date'] = state.get('pending_date')
    context.user_data['master_id'] = state.get('master_id')
    context.user_data['master_name'] = state.get('master_name')
    context.user_data['master_selection_shown'] = state.get('master_selection_shown', False)
    context.user_data['phone_refused'] = state.get('phone_refused', False)
    context.user_data['recent_booking'] = state.get('recent_booking')
    context.user_data['last_visit_time'] = state.get('last_visit_time')
    context.user_data['last_service'] = state.get('last_service')

def _reset_context(context: ContextTypes.DEFAULT_TYPE, keep_client_info: bool = False) -> None:
    """Сбрасывает контекст диалога.
    
    Args:
        keep_client_info: если True, сохраняет имя, телефон и дату для возможности дозаписи
    """
    if keep_client_info:
        # Сохраняем важную информацию о клиенте
        saved_name = context.user_data.get('client_name')
        saved_phone = context.user_data.get('client_phone')
        saved_tg_first_name = context.user_data.get('tg_first_name')
        saved_pending_date = context.user_data.get('pending_date')
        saved_visit_time = context.user_data.get('visit_time')  # Сохраняем время последней записи
        saved_service = context.user_data.get('service')  # Сохраняем услугу последней записи
        
        # Сбрасываем только информацию о конкретной записи
        for key in ['visit_time', 'service', 'child_age', 'date', 'time', 'history', 'time_checked', 'master_id', 'master_name', 'master_selection_shown', 'favorite_master_id', 'favorite_master_name', 'favorite_master_offered', 'promotion_mentioned', 'promotion_id', 'phone_refused']:
            context.user_data.pop(key, None)
        
        # Восстанавливаем сохранённую информацию
        if saved_name:
            context.user_data['client_name'] = saved_name
        if saved_phone:
            context.user_data['client_phone'] = saved_phone
        if saved_tg_first_name:
            context.user_data['tg_first_name'] = saved_tg_first_name
        if saved_pending_date:
            context.user_data['pending_date'] = saved_pending_date
        
        # Сохраняем время и услугу последней записи для возможной дозаписи
        if saved_visit_time:
            context.user_data['last_visit_time'] = saved_visit_time.isoformat()
        if saved_service:
            context.user_data['last_service'] = saved_service
        
        # Устанавливаем флаг "недавно записался" с таймстампом
        context.user_data['recent_booking'] = datetime.datetime.now(tz=TZ).isoformat()
    else:
        # Полный сброс (как раньше)
        for key in ['visit_time', 'client_name', 'client_phone', 'service', 'child_age', 'date', 'time', 'pending_date', 'history', 'time_checked', 'master_id', 'master_name', 'master_selection_shown', 'favorite_master_id', 'favorite_master_name', 'favorite_master_offered', 'promotion_mentioned', 'promotion_id', 'phone_refused', 'recent_booking']:
            context.user_data.pop(key, None)
        context.user_data['greeted'] = False

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    # Полностью очищаем состояние пользователя
    context.user_data.clear()
    # Удаляем сохраненное состояние из файла
    update_user_state(chat_id, {})
    context.user_data['greeted'] = True
    greeting = (
        "Здравствуйте! 😊 Я — ассистент администратора салона «Непоседы». Чем могу помочь?"
    )
    await update.message.reply_text(greeting)
    _save_context_state(chat_id, context)

# --- Напоминание о себе после 2 минут молчания ---
def schedule_inactivity_reminder(context, chat_id):
    job_id = f'inactivity_reminder_{chat_id}'
    # Удаляем старое напоминание, если есть
    try:
        scheduler.remove_job(job_id)
    except Exception:
        pass
    # Планируем новое напоминание через 2 минуты
    def send_reminder():
        try:
            context.bot.send_message(
                chat_id,
                "Я на связи, если что — подскажу по услугам и помогу записаться 😊"
            )
        except Exception:
            pass
    scheduler.add_job(send_reminder, 'date', run_date=datetime.datetime.now(TZ) + datetime.timedelta(minutes=2), id=job_id)

async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    _load_context_state(chat_id, context)
    
    # Проверяем "свежесть" сохранённого контекста (15 минут после последней записи)
    recent_booking_time = context.user_data.get('recent_booking')
    if recent_booking_time:
        try:
            booking_dt = datetime.datetime.fromisoformat(recent_booking_time)
            now = datetime.datetime.now(tz=TZ)
            time_since_booking = (now - booking_dt).total_seconds() / 60  # в минутах
            
            # Если прошло больше 15 минут, сбрасываем сохранённую информацию о клиенте
            if time_since_booking > 15:
                logger.info(f"[DEBUG] Контекст устарел ({time_since_booking:.1f} мин), сбрасываем информацию о клиенте")
                _reset_context(context, keep_client_info=False)
                _save_context_state(chat_id, context)
        except Exception as e:
            logger.error(f"[ОШИБКА] При проверке свежести контекста: {e}")
    
    history = context.user_data.get('history', [])[-8:]
    user_text_raw = update.message.text
    user_text = user_text_raw.lower()

    # Сохраняем идентификаторы Telegram для персонализации
    tg_user = update.effective_user
    context.user_data['tg_user_id'] = tg_user.id
    context.user_data['tg_username'] = getattr(tg_user, 'username', None)
    context.user_data['tg_first_name'] = getattr(tg_user, 'first_name', None)
    
    # --- Проверка на запрос администратора (обрабатываем ДО всего остального) ---
    admin_request_keywords = [
        'позовите администратора', 'позови администратора', 'вызовите администратора',
        'хочу поговорить с администратором', 'нужен администратор', 'живой человек',
        'реальный человек', 'не бот', 'хочу с человеком', 'свяжитесь со мной',
        'перезвоните', 'позвоните мне'
    ]
    
    if any(keyword in user_text for keyword in admin_request_keywords):
        client_name = context.user_data.get('client_name', tg_user.first_name or 'Неизвестно')
        client_username = f"@{tg_user.username}" if tg_user.username else "нет username"
        client_phone = context.user_data.get('client_phone', 'не указан')
        
        admin_notification = (
            f"🔔 КЛИЕНТ ПРОСИТ АДМИНИСТРАТОРА!\n\n"
            f"👤 {client_name}\n"
            f"📱 Telegram: {client_username}\n"
            f"📞 Телефон: {client_phone}\n"
            f"💬 Chat ID: {chat_id}\n\n"
            f"Сообщение клиента:\n{user_text_raw}\n\n"
            f"Для ответа используйте: /reply {chat_id} <текст>"
        )
        await send_chunked(context, ADMIN_CHAT_ID, admin_notification)
        await update.message.reply_text(
            "Сейчас передам ваш запрос администратору. "
            "Он свяжется с вами в ближайшее время! 😊"
        )
        logger.info(f"[ВЫЗОВ АДМИНИСТРАТОРА] Прямой запрос от {client_name} (chat_id={chat_id})")
        return
    
    # --- Админ-команды (обрабатываем ДО всего остального) ---
    if user_id == ADMIN_CHAT_ID:
        if user_text.startswith('/admin_today'):
            today = datetime.datetime.now(tz=TZ).date()
            events = list_events_for_date(today)
            await update.message.reply_text('\n'.join(
                f"{datetime.datetime.fromisoformat(e['start'].get('dateTime')).strftime('%H:%M')} — {e.get('summary', 'Запись')}" for e in events
            ) or 'Сегодня записей нет.')
            return
        if user_text.startswith('/admin_date'):
            parts = user_text.split()
            if len(parts) != 2:
                await update.message.reply_text('Использование: /admin_date YYYY-MM-DD')
                return
            date = datetime.date.fromisoformat(parts[1])
            events = list_events_for_date(date)
            await update.message.reply_text('\n'.join(
                f"{datetime.datetime.fromisoformat(e['start'].get('dateTime')).strftime('%H:%M')} — {e.get('summary', 'Запись')}" for e in events
            ) or 'Записей нет.')
            return
        if user_text.startswith('/admin_cancel'):
            parts = user_text.split()
            if len(parts) == 2 and delete_event(parts[1]):
                await update.message.reply_text('Отменено.')
            else:
                await update.message.reply_text('Не удалось отменить.')
            return
        if user_text.startswith('/admin_move'):
            parts = user_text.split()
            if len(parts) != 4:
                await update.message.reply_text('Использование: /admin_move <event_id> YYYY-MM-DD HH:MM')
                return
            date = datetime.date.fromisoformat(parts[2])
            hour, minute = map(int, parts[3].split(':'))
            ok = update_event_time(parts[1], datetime.datetime.combine(date, datetime.time(hour, minute)))
            await update.message.reply_text('Перенесено.' if ok else 'Не удалось перенести.')
            return

    # --- Управление памятью: исправление и сброс ---
    if 'удалить данные' in user_text or 'сбросить' in user_text:
        context.user_data.clear()
        await update.message.reply_text("Все ваши данные удалены. Начнём заново!")
        return

    if 'изменить номер' in user_text or 'новый телефон' in user_text or 'мой телефон' in user_text:
        phone_match = re.search(r'(\+?\d[\d\s\-\(\)]{8,})', user_text_raw)
        if phone_match:
            new_phone = normalize_ru_phone(phone_match.group(1))
            if new_phone:
                context.user_data['client_phone'] = new_phone
                _save_context_state(chat_id, context)
                await update.message.reply_text(f"Ваш номер телефона обновлён: {new_phone}")
            else:
                await update.message.reply_text("Не удалось распознать номер. Пожалуйста, укажите его в формате +7 9ХХ ХХХ-ХХ-ХХ.")
        else:
            await update.message.reply_text("Пожалуйста, напишите новый номер телефона.")
        return

    # --- ШАГ 1: ИЗВЛЕЧЕНИЕ ДАННЫХ В ФОНЕ ---
    
    # Проверка на "дозапись" (клиент просит записать ещё кого-то)
    additional_booking_keywords = ['еще', 'ещё', 'тоже', 'также', 'со мной', 'вместе']
    is_additional_booking = False
    
    # Определяем дозапись по ключевым словам + наличию флага recent_booking
    if context.user_data.get('recent_booking') and any(keyword in user_text for keyword in additional_booking_keywords):
        is_additional_booking = True
        logger.info(f"[DEBUG] Обнаружена попытка дозаписи")
        
        # КЛЮЧЕВОЕ ИЗМЕНЕНИЕ: Восстанавливаем время и услугу АВТОМАТИЧЕСКИ при дозаписи
        # ЕСЛИ клиент НЕ указал новое время/дату явно
        last_visit_time_str = context.user_data.get('last_visit_time')
        last_service = context.user_data.get('last_service')
        
        # Проверяем, указал ли клиент новое время в текущем сообщении
        # Ищем паттерны времени: "в 15.00", "на 16:00", "завтра в 11"
        new_time_patterns = [
            r'в\s+\d{1,2}[:\.]?\d{0,2}',
            r'на\s+\d{1,2}[:\.]?\d{0,2}',
            r'(завтра|сегодня|послезавтра)\s+в',
            r'(понедельник|вторник|среду|четверг|пятницу|субботу|воскресенье)'
        ]
        has_new_time = any(re.search(pattern, user_text) for pattern in new_time_patterns)
        
        # Если клиент НЕ указал новое время — восстанавливаем из последней записи
        if not has_new_time and last_visit_time_str:
            try:
                context.user_data['visit_time'] = datetime.datetime.fromisoformat(last_visit_time_str)
                _save_context_state(chat_id, context)
                logger.info(f"[DEBUG] ✅ Автоматически восстановлено время: {last_visit_time_str}")
            except Exception as e:
                logger.error(f"[ОШИБКА] При восстановлении времени: {e}")
        
        # Восстанавливаем услугу, если не указана новая
        if not has_new_time and last_service and not context.user_data.get('service'):
            context.user_data['service'] = last_service
            _save_context_state(chat_id, context)
            logger.info(f"[DEBUG] ✅ Автоматически восстановлена услуга: {last_service}")
        
        # ВАЖНО: Сбрасываем имя клиента, чтобы бот спросил имя РЕБЁНКА (или другого человека)
        # НО сохраняем телефон родителя
        if 'дочь' in user_text or 'сын' in user_text or 'ребенок' in user_text or 'ребёнок' in user_text:
            context.user_data.pop('client_name', None)
            _save_context_state(chat_id, context)
            logger.info(f"[DEBUG] ✅ Сброшено имя клиента для запроса имени ребёнка")
    
    # Распознавание отказа от предоставления телефона
    phone_refusal_keywords = ['не хочу', 'не буду', 'не хотел бы', 'не могу', 'без номера', 'без телефона', 'не оставлять', 'не давать', 'не указывать']
    if any(keyword in user_text for keyword in phone_refusal_keywords):
        if not context.user_data.get('phone_refused'):
            context.user_data['phone_refused'] = True
            context.user_data['client_phone'] = None  # Убираем телефон, если был
            _save_context_state(chat_id, context)
            logger.info(f"[DEBUG] Клиент отказался предоставлять номер телефона")
    
    # Извлекаем имя и телефон (паттерн: "Имя, +7..." или "Имя +7..." или "Имя 8...")
    # Сначала проверяем с запятой, потом без
    name_phone_match = re.match(r'^\s*([А-Яа-яA-Za-zЁё\-\s]{2,})[,;\s]+(\+?\d[\d\s\-\(\)]{8,})\s*$', user_text_raw)
    if not name_phone_match:
        # Попробуем без запятой (например, "Максим 89787574470")
        name_phone_match = re.match(r'^\s*([А-Яа-яA-Za-zЁё\-]{2,})\s+(\+?\d[\d\s\-\(\)]{8,})\s*$', user_text_raw)
    
    if name_phone_match:
        context.user_data['client_name'] = name_phone_match.group(1).strip()
        phone_norm = normalize_ru_phone(name_phone_match.group(2).strip())
        if phone_norm:
            context.user_data['client_phone'] = phone_norm
            _save_context_state(chat_id, context)
        else:
            # Номер некорректный - сообщаем пользователю
            await send_chunked(context, chat_id, 
                "Спасибо! Имя записала, но не могу распознать номер телефона. Пожалуйста, укажите его в формате +7 9ХХ ХХХ-ХХ-ХХ (11 цифр).")
            _save_context_state(chat_id, context)
            return

    # Извлекаем только телефон
    phone_match = re.search(r'(\+?\d[\d\s\-\(\)]{8,})', user_text_raw)
    if phone_match and not context.user_data.get('client_phone'):
        phone_norm = normalize_ru_phone(phone_match.group(1))
        if phone_norm:
            context.user_data['client_phone'] = phone_norm
            _save_context_state(chat_id, context)
        else:
            # Номер некорректный - сообщаем пользователю
            await send_chunked(context, chat_id, 
                "Извините, не могу распознать номер телефона. Пожалуйста, укажите его в формате +7 9ХХ ХХХ-ХХ-ХХ (11 цифр).")
            return

    # Извлекаем дату/время (улучшенный парсер)
    if not context.user_data.get('visit_time'):
        now = datetime.datetime.now(tz=TZ)
        parsed_dt = None
        pending_date = context.user_data.get('pending_date')  # Дата из предыдущего контекста
        
        # --- СПЕЦИАЛЬНАЯ ОБРАБОТКА ДНЕЙ НЕДЕЛИ ---
        weekday_map = {
            'понедельник': 0, 'пн': 0,
            'вторник': 1, 'вт': 1,
            'среду': 2, 'среда': 2, 'ср': 2,
            'четверг': 3, 'чт': 3,
            'пятницу': 4, 'пятница': 4, 'пт': 4,
            'субботу': 5, 'суббота': 5, 'сб': 5,
            'воскресенье': 6, 'вс': 6
        }
        
        # Ищем паттерны типа "в ближайшую пятницу в 15.00" или "в пятницу на 16:00"
        weekday_time_pattern = r'(?:в|на)?\s*(?:ближайш[уюя]+\s+)?(' + '|'.join(weekday_map.keys()) + r')\s+(?:в|на)\s+(\d{1,2})[:\.]?(\d{2})?'
        weekday_match = re.search(weekday_time_pattern, user_text, re.IGNORECASE)
        
        if weekday_match:
            day_name = weekday_match.group(1).lower()
            hour = int(weekday_match.group(2))
            minute = int(weekday_match.group(3)) if weekday_match.group(3) else 0
            
            target_weekday = weekday_map.get(day_name)
            if target_weekday is not None:
                # Находим ближайший день недели
                current_weekday = now.weekday()
                days_ahead = target_weekday - current_weekday
                
                # Если день уже прошёл, добавляем неделю
                # НО! Если это сегодняшний день (days_ahead == 0), берём сегодня
                if days_ahead < 0:
                    days_ahead += 7
                elif days_ahead == 0 and hour < now.hour:  # Если время уже прошло сегодня
                    days_ahead = 7
                elif days_ahead == 0 and hour == now.hour and minute <= now.minute:  # Если точное время прошло
                    days_ahead = 7
                
                target_date = now.date() + datetime.timedelta(days=days_ahead)
                try:
                    parsed_dt = datetime.datetime.combine(target_date, datetime.time(hour, minute, tzinfo=TZ))
                    logger.info(f"[DEBUG] Распознан день недели: {day_name} -> {target_date}, время {hour}:{minute:02d}")
                    context.user_data['pending_date'] = target_date.isoformat()  # Сохраняем как строку ISO
                except ValueError:
                    pass
        
        # Сохраняем ожидаемую дату, если распознан только день недели без времени
        if not parsed_dt and not context.user_data.get('pending_date'):
            for day_name, weekday in weekday_map.items():
                if day_name in user_text:
                    current_weekday = now.weekday()
                    days_ahead = weekday - current_weekday
                    
                    # Если день уже прошёл, добавляем неделю
                    # НО! Если это сегодняшний день (days_ahead == 0), берём сегодня (бот спросит время, и если оно прошло, предложит другие варианты)
                    if days_ahead < 0:
                        days_ahead += 7
                    elif days_ahead == 0 and now.hour >= 20:  # Если сегодня поздний вечер, берём следующую неделю
                        days_ahead = 7
                    
                    target_date = now.date() + datetime.timedelta(days=days_ahead)
                    context.user_data['pending_date'] = target_date.isoformat()  # Сохраняем как строку ISO
                    logger.info(f"[DEBUG] Сохранена ожидаемая дата: {day_name} -> {target_date}")
                    _save_context_state(chat_id, context)
                    break
        
        # Если не нашли день недели, пробуем ручной парсинг для "завтра/сегодня в/на ЧЧ:ММ" ИЛИ просто "на 11.00"
        if not parsed_dt:
            time_patterns = [
                r'(завтра|сегодня|послезавтра)\s+(?:в|на)\s+(\d{1,2})[:\.](\d{2})',
                r'(завтра|сегодня|послезавтра)\s+(?:в|на)\s+(\d{1,2})',
                r'(?:давайте\s+)?(?:в|на)\s+(\d{1,2})[:\.](\d{2})',
                r'(?:давайте\s+)?(?:в|на)\s+(\d{1,2})\s*(?:час|ч)?',
            ]
            logger.info(f"[DEBUG] Пробуем ручной парсинг для: '{user_text_raw}'")
            for pattern in time_patterns:
                match = re.search(pattern, user_text, re.IGNORECASE)
                if match:
                    groups = match.groups()
                    
                    # Определяем дату
                    if len(groups) > 0 and groups[0] in ['завтра']:
                        target_date = now.date() + datetime.timedelta(days=1)
                    elif len(groups) > 0 and groups[0] in ['послезавтра']:
                        target_date = now.date() + datetime.timedelta(days=2)
                    elif len(groups) > 0 and groups[0] in ['сегодня']:
                        target_date = now.date()
                    elif pending_date:
                        # Если есть ожидаемая дата из контекста (например, "воскресенье"), используем её
                        # pending_date хранится как строка ISO, конвертируем в date объект
                        target_date = datetime.date.fromisoformat(pending_date)
                        logger.info(f"[DEBUG] Используем ожидаемую дату из контекста: {pending_date}")
                    else:
                        # Если день не указан, но есть время, берём ближайшее будущее
                        target_date = now.date()
                    
                    # Определяем время
                    # Проверяем, есть ли день недели в первой группе
                    has_day_in_first_group = len(groups) > 0 and groups[0] in ['завтра', 'сегодня', 'послезавтра']
                    
                    if has_day_in_first_group:
                        # Формат: "(завтра|сегодня) на ЧЧ:ММ" → groups[0]=день, groups[1]=час, groups[2]=минуты
                        if len(groups) >= 3 and groups[2]:
                            hour, minute = int(groups[1]), int(groups[2])
                        elif len(groups) >= 2 and groups[1]:
                            hour, minute = int(groups[1]), 0
                        else:
                            continue
                    else:
                        # Формат: "на ЧЧ:ММ" → groups[0]=час, groups[1]=минуты
                        if len(groups) >= 2 and groups[1]:
                            hour, minute = int(groups[0]), int(groups[1])
                        elif len(groups) >= 1 and groups[0]:
                            hour, minute = int(groups[0]), 0
                        else:
                            continue
                    
                    try:
                        parsed_dt = datetime.datetime.combine(target_date, datetime.time(hour, minute, tzinfo=TZ))
                        # Если время уже прошло сегодня, переносим на завтра
                        if parsed_dt <= now:
                            parsed_dt = parsed_dt + datetime.timedelta(days=1)
                        logger.info(f"[DEBUG] Ручной парсинг успешен! Паттерн: {pattern}, Результат: {parsed_dt}")
                        break
                    except ValueError as e:
                        logger.warning(f"[DEBUG] Ошибка парсинга времени: {e}")
                        continue
        
        # Если ручной парсинг не сработал, пробуем dateparser как последний вариант
        if not parsed_dt:
            logger.info(f"[DEBUG] Ручной парсинг не сработал, пробуем dateparser для: '{user_text_raw}'")
            parsed_dt = dateparser.parse(
                user_text_raw, 
                languages=['ru'], 
                settings={
                    'PREFER_DATES_FROM': 'future', 
                    'RELATIVE_BASE': now,
                    'TIMEZONE': 'Europe/Moscow',
                    'RETURN_AS_TIMEZONE_AWARE': True
                }
            )
            if parsed_dt:
                logger.info(f"[DEBUG] dateparser вернул: {parsed_dt}")
        
        if parsed_dt:
            if parsed_dt.tzinfo is None:
                parsed_dt = parsed_dt.replace(tzinfo=TZ)
            # Проверяем, что время не в прошлом
            if parsed_dt > now:
                context.user_data['visit_time'] = parsed_dt
                context.user_data['date'] = parsed_dt.date().isoformat()
                context.user_data['time'] = parsed_dt.strftime('%H:%M')
                logger.info(f"[DEBUG] Успешно распознано время: {parsed_dt}")
                _save_context_state(chat_id, context)

    # Извлекаем услугу по ключевым словам
    # Проверяем контекст: есть ли дата/время, явный запрос или упоминание услуги со словом "нужна/нужно/хочу"
    has_date_context = context.user_data.get('visit_time') or context.user_data.get('date')
    explicit_booking = any(word in user_text for word in ['записать', 'запишите', 'хочу записаться', 'нужна запись', 'нужна', 'нужно', 'хочу'])
    
    if not context.user_data.get('service') and (has_date_context or explicit_booking):
        if 'стрижк' in user_text:
            context.user_data['service'] = 'Стрижка'
        elif 'укладк' in user_text:
            context.user_data['service'] = 'Укладка'
        elif 'окраш' in user_text or 'колор' in user_text:
            context.user_data['service'] = 'Окрашивание'
        elif 'плетен' in user_text:
            context.user_data['service'] = 'Плетение'
        
        if context.user_data.get('service'):
            logger.info(f"[DEBUG] Распознана услуга: {context.user_data['service']}")
            _save_context_state(chat_id, context)

    # Извлекаем возраст ребёнка
    if not context.user_data.get('child_age'):
        child_age = parse_child_age(user_text_raw)
        if child_age:
            context.user_data['child_age'] = child_age
            _save_context_state(chat_id, context)
    
    # Извлекаем мастера из текста
    if not context.user_data.get('master_id'):
        master = get_master_by_name(user_text_raw)
        if master:
            context.user_data['master_id'] = master['id']
            context.user_data['master_name'] = master['name']
            logger.info(f"[DEBUG] Распознан мастер: {master['name']}")
            _save_context_state(chat_id, context)

    # --- ШАГ 1.5: ПРОВЕРЯЕМ ЗАНЯТОСТЬ ВРЕМЕНИ (ДО ОТПРАВКИ В LLM) ---
    # Если время только что было извлечено, сразу проверяем его занятость
    if context.user_data.get('visit_time') and not context.user_data.get('time_checked'):
        visit_time = context.user_data['visit_time']
        if not is_slot_free(visit_time):
            await send_chunked(context, chat_id, "Минутку, проверяю занятость времени...")
            
            # Уведомляем администратора
            name = context.user_data.get('client_name', 'Неизвестно')
            phone = context.user_data.get('client_phone', 'Неизвестно')
            service = context.user_data.get('service', 'Неизвестно')
            await send_chunked(
                context,
                ADMIN_CHAT_ID,
                f"⚠️ ПОПЫТКА ЗАПИСИ НА ЗАНЯТОЕ ВРЕМЯ!\n\n👤 {name}\n📱 {phone}\n🕐 {visit_time:%d.%m.%Y %H:%M}\n💇‍♀️ {service}\n\nВремя уже занято!"
            )
            
            # Получаем свободные слоты на эту дату
            same_date = visit_time.date()
            free_slots = get_free_slots(same_date)
            if free_slots:
                await send_chunked(context, chat_id, 
                    f"К сожалению, это время уже занято. Вот свободные варианты на {same_date.strftime('%d.%m.%Y')}: {', '.join(free_slots)}. Выберите другое время, пожалуйста.")
            else:
                await send_chunked(context, chat_id, 
                    "К сожалению, это время уже занято, и на эту дату нет свободных слотов. Предложите, пожалуйста, другую дату.")
            
            # Сбрасываем время, оставляем остальные данные
            context.user_data.pop('visit_time', None)
            context.user_data.pop('date', None)
            context.user_data.pop('time', None)
            context.user_data.pop('time_checked', None)
            _save_context_state(chat_id, context)
            return
        else:
            # Время свободно, отмечаем что проверили
            context.user_data['time_checked'] = True
            _save_context_state(chat_id, context)

    # --- ШАГ 2: ФОРМИРУЕМ КОНТЕКСТ ДЛЯ LLM ---
    # Если имя не сохранено, но есть в Telegram профиле, используем его
    if not context.user_data.get('client_name') and context.user_data.get('tg_first_name'):
        context.user_data['client_name'] = context.user_data['tg_first_name']
        _save_context_state(chat_id, context)
    
    # Проверяем, есть ли у клиента любимый мастер (если телефон уже известен и мастер не выбран)
    if context.user_data.get('client_phone') and not context.user_data.get('master_id') and not context.user_data.get('favorite_master_offered'):
        last_master_id = get_last_master_for_client(context.user_data['client_phone'])
        if last_master_id:
            master = get_master_by_id(last_master_id)
            if master:
                context.user_data['favorite_master_id'] = last_master_id
                context.user_data['favorite_master_name'] = master['name']
                context.user_data['favorite_master_offered'] = True
                _save_context_state(chat_id, context)
                logger.info(f"[DEBUG] Найден любимый мастер клиента: {master['name']}")
    
    context_info = []
    
    # Добавляем текущую дату и время для контекста LLM
    now = datetime.datetime.now(tz=TZ)
    current_hour = now.hour
    context_info.append(f"[ТЕКУЩЕЕ ВРЕМЯ: {now.strftime('%d.%m.%Y %H:%M')} - {now.strftime('%A')}]")
    
    # Добавляем информацию о дозаписи
    if is_additional_booking:
        context_info.append(f"[ДОЗАПИСЬ]: Клиент просит записать ещё одного человека к уже существующей записи")
        if context.user_data.get('last_visit_time'):
            context_info.append(f"[Время предыдущей записи: {context.user_data.get('last_visit_time')}]")
    
    if context.user_data.get('client_name'):
        context_info.append(f"[Имя клиента: {context.user_data['client_name']}]")
    if context.user_data.get('client_phone'):
        context_info.append(f"[Телефон клиента: {context.user_data['client_phone']}]")
    if context.user_data.get('visit_time'):
        vt = context.user_data['visit_time']
        context_info.append(f"[Выбранное время: {vt.strftime('%d.%m.%Y %H:%M')}]")
        
        # Получаем свободные слоты для этой даты
        free_slots_for_date = get_free_slots(vt.date())
        if free_slots_for_date:
            context_info.append(f"[СВОБОДНЫЕ СЛОТЫ НА {vt.strftime('%d.%m.%Y')}: {', '.join(free_slots_for_date)}]")
    elif context.user_data.get('date'):
        # Если есть только дата (без времени), получаем свободные слоты
        try:
            date_obj = datetime.date.fromisoformat(context.user_data['date'])
            free_slots_for_date = get_free_slots(date_obj)
            if free_slots_for_date:
                context_info.append(f"[СВОБОДНЫЕ СЛОТЫ НА {date_obj.strftime('%d.%m.%Y')}: {', '.join(free_slots_for_date)}]")
        except (ValueError, TypeError):
            pass
    
    # Если пользователь спрашивает про свободное время на конкретную дату (например, "на четверг")
    # проверим, упоминается ли в тексте дата без времени
    if not context.user_data.get('visit_time') and not context.user_data.get('date'):
        date_keywords = ['завтра', 'послезавтра', 'сегодня', 'понедельник', 'вторник', 'среду', 'четверг', 'пятницу', 'субботу', 'воскресенье']
        if any(keyword in user_text for keyword in date_keywords):
            # Попробуем распарсить дату
            parsed_date = dateparser.parse(
                user_text_raw, 
                languages=['ru'], 
                settings={
                    'PREFER_DATES_FROM': 'future',
                    'RELATIVE_BASE': now,
                    'TIMEZONE': 'Europe/Moscow',
                    'RETURN_AS_TIMEZONE_AWARE': True
                }
            )
            if parsed_date:
                free_slots_for_date = get_free_slots(parsed_date.date())
                if free_slots_for_date:
                    context_info.append(f"[СВОБОДНЫЕ СЛОТЫ НА {parsed_date.strftime('%d.%m.%Y')}: {', '.join(free_slots_for_date)}]")
    
    if context.user_data.get('service'):
        context_info.append(f"[Услуга: {context.user_data['service']}]")
    if context.user_data.get('child_age'):
        context_info.append(f"[Возраст ребёнка: {context.user_data['child_age']} лет]")
    if context.user_data.get('master_name'):
        context_info.append(f"[Выбранный мастер: {context.user_data['master_name']}]")
    elif context.user_data.get('favorite_master_name'):
        context_info.append(f"[В прошлый раз клиент был у мастера: {context.user_data['favorite_master_name']}. Можешь мягко предложить записаться снова к этому мастеру]")
    
    # Проверяем, подходит ли какая-то акция
    if not context.user_data.get('promotion_mentioned'):
        promo = check_promotion(
            service=context.user_data.get('service'),
            visit_time=context.user_data.get('visit_time'),
            child_age=context.user_data.get('child_age'),
            context_data=context.user_data
        )
        if promo:
            context_info.append(f"[АКЦИЯ]: {promo['message']}")
            context.user_data['promotion_mentioned'] = True
            context.user_data['promotion_id'] = promo['id']
            logger.info(f"[ПРОМО] Найдена подходящая акция: {promo['name']}")
            _save_context_state(chat_id, context)
    
    context_str = " ".join(context_info) if context_info else ""
    
    # --- ШАГ 3: ОТПРАВЛЯЕМ ЗАПРОС В LLM ДЛЯ "ЖИВОГО" ОТВЕТА ---
    try:
        await context.bot.send_chat_action(chat_id=chat_id, action="typing")
    except Exception as e:
        logger.warning(f"[WARNING] Не удалось отправить typing action: {e}")
    first_name = context.user_data.get('tg_first_name') or ''
    username = context.user_data.get('tg_username') or ''
    user_meta = f"(id:{user_id} {first_name} @{username})".strip()
    
    # Добавляем контекст в сообщение пользователя для LLM
    user_message_for_llm = f"{user_text_raw}"
    if context_str:
        user_message_for_llm = f"{context_str}\n\nКлиент: {user_text_raw}"
    
    history.append({"role": "user", "content": f"{user_meta}: {user_message_for_llm}"})
    context.user_data['history'] = history
    _save_context_state(chat_id, context)
    
    try:
        response = ask_deepseek(user_message_for_llm, history=history)
        logger.info(f"[ОТЛАДКА] Ответ ИИ: {response}")
        
        if response and response != "Извините, сейчас не могу ответить. Попробуйте позже.":
            history.append({"role": "assistant", "content": response})
            context.user_data['history'] = history
            _save_context_state(chat_id, context)
            
            # Проверяем, есть ли команда вызова администратора
            if '[ВЫЗОВ_АДМИНИСТРАТОРА]' in response:
                # Убираем команду из ответа клиенту
                response_clean = response.replace('[ВЫЗОВ_АДМИНИСТРАТОРА]', '').strip()
                await send_chunked(context, chat_id, response_clean)
                
                # Отправляем уведомление администратору
                client_name = context.user_data.get('client_name', first_name or 'Неизвестно')
                client_username = f"@{username}" if username else "нет username"
                admin_notification = (
                    f"🔔 КЛИЕНТ ПРОСИТ АДМИНИСТРАТОРА!\n\n"
                    f"👤 {client_name}\n"
                    f"📱 {client_username}\n"
                    f"💬 Chat ID: {chat_id}\n\n"
                    f"Сообщение клиента:\n{user_text_raw}\n\n"
                    f"Для ответа используйте: /reply {chat_id} <текст>"
                )
                await send_chunked(context, ADMIN_CHAT_ID, admin_notification)
                logger.info(f"[ВЫЗОВ АДМИНИСТРАТОРА] Отправлено уведомление от {client_name} (chat_id={chat_id})")
            else:
                await send_chunked(context, chat_id, response)
        else:
            # LLM недоступен — используем умный fallback
            logger.warning(f"[FALLBACK] LLM недоступен, используем шаблонный ответ")
            
            # Определяем тип сообщения: вопрос или запись?
            question_words = ['как', 'что', 'когда', 'где', 'можете', 'можно', 'посоветуйте', 'совет', 
                            'расскажите', 'сколько', 'стоит', 'цена', 'почему', 'какой', 'какая', 'какие',
                            'подскажите', '?']
            
            is_question = any(word in user_text for word in question_words)
            
            # Если это вопрос — отвечаем на вопрос, не пытаемся записать
            if is_question and not context.user_data.get('visit_time'):
                # Вопрос о ценах
                if 'сколько' in user_text or 'цена' in user_text or 'стоит' in user_text:
                    await update.message.reply_text(
                        "Наши цены:\n"
                        "• Детская стрижка — от 800₽\n"
                        "• Взрослая стрижка — от 800₽\n"
                        "• Укладка\n"
                        "• Плетение\n"
                        "• Окрашивание — зависит от длины\n\n"
                        "Хотите записаться? Скажите, когда вам удобно 😊"
                    )
                    return
                # Вопрос об уходе за волосами или общий вопрос
                elif 'уход' in user_text or 'волос' in user_text or 'совет' in user_text:
                    await update.message.reply_text(
                        "К сожалению, для детальной консультации по уходу за волосами "
                        "лучше обратиться к нашему мастеру при визите.\n\n"
                        "Но я могу записать вас на консультацию или стрижку! "
                        "Когда вам удобно подойти? 😊"
                    )
                    return
                # Вопрос о времени работы
                elif 'работа' in user_text or 'график' in user_text or 'когда открыт' in user_text:
                    await update.message.reply_text(
                        "Мы работаем ежедневно, включая выходные.\n"
                        "Запись по свободному времени.\n\n"
                        "Когда вам удобно записаться? 😊"
                    )
                    return
                # Общий вопрос
                else:
                    await update.message.reply_text(
                        "Извините, сейчас у меня ограниченные возможности ответа. "
                        "Могу помочь с записью на стрижку, укладку, окрашивание или плетение.\n\n"
                        "Или свяжитесь с администратором напрямую для консультации 😊"
                    )
                    return
            
            # Проверяем, что уже известно (если не вопрос)
            required_fields = ['client_name', 'client_phone', 'visit_time', 'service']
            missing = [f for f in required_fields if not context.user_data.get(f)]
            
            if not missing:
                # Ничего не спрашиваем, просто переходим к созданию записи
                pass
            elif context.user_data.get('visit_time') and ('client_name' in missing or 'client_phone' in missing):
                # Есть время, нет контактов
                vt = context.user_data['visit_time']
                service = context.user_data.get('service', 'Стрижка')
                await update.message.reply_text(
                    f"Отлично! {vt.strftime('%d.%m.%Y')} в {vt.strftime('%H:%M')}. {service} — от 800₽.\n\n"
                    "Пришлите, пожалуйста, имя и номер телефона в одном сообщении.\n"
                    "Например: Анна, +7 999 123-45-67"
                )
                return
            elif not context.user_data.get('visit_time'):
                # Нет времени — спрашиваем
                service = context.user_data.get('service', '')
                await update.message.reply_text(
                    f"Здравствуйте! 😊 Я — администратор салона «Непоседы». "
                    f"{service + ' — от 800₽. ' if service else ''}"
                    "На какой день и время вам удобно записаться?"
                )
                return
            else:
                # Что-то ещё не хватает
                await update.message.reply_text(
                    "Здравствуйте! 😊 Я — администратор салона «Непоседы». "
                    "Помогу вам с записью. Скажите, когда вам удобно подойти?"
                )
                return
        
        # --- ШАГ 4: ПРОВЕРЯЕМ, СОБРАНЫ ЛИ ВСЕ ДАННЫЕ ДЛЯ ЗАПИСИ ---
        # Телефон делаем необязательным — если клиент отказывается, создаём запись без него
        required_fields = ['client_name', 'visit_time', 'service']
        missing = [f for f in required_fields if not context.user_data.get(f)]
        
        # Проверяем, есть ли явный отказ от предоставления телефона
        phone_refused = context.user_data.get('phone_refused', False)
        if not context.user_data.get('client_phone') and not phone_refused:
            # Если телефона нет и клиент НЕ отказался явно, добавляем в недостающие
            missing.append('client_phone')
        
        logger.info(f"[DEBUG] Проверка полей для записи. Данные: name={context.user_data.get('client_name')}, phone={context.user_data.get('client_phone')}, time={context.user_data.get('visit_time')}, service={context.user_data.get('service')}, phone_refused={phone_refused}")
        logger.info(f"[DEBUG] Недостающие поля: {missing}")
        
        if not missing:
            # ВСЕ ДАННЫЕ СОБРАНЫ — ПРОВЕРЯЕМ ВЫБОР МАСТЕРА
            name = context.user_data['client_name']
            phone = context.user_data.get('client_phone', 'Не указан')  # Если телефона нет, используем заглушку
            visit_time = context.user_data['visit_time']
            service = context.user_data['service']
            
            # Время уже было проверено на ШАГе 1.5, но для безопасности делаем финальную проверку
            if not is_slot_free(visit_time):
                logger.warning(f"[ПРЕДУПРЕЖДЕНИЕ] Время {visit_time} стало занятым между проверками!")
                await send_chunked(context, chat_id, "К сожалению, это время только что заняли. Пожалуйста, выберите другое время.")
                # Сбрасываем время и флаг проверки
                context.user_data.pop('visit_time', None)
                context.user_data.pop('date', None)
                context.user_data.pop('time', None)
                context.user_data.pop('time_checked', None)
                _save_context_state(chat_id, context)
                return
            
            # Проверяем, выбран ли мастер
            master_id = context.user_data.get('master_id')
            master_selection_shown = context.user_data.get('master_selection_shown', False)
            
            # Если мастер не выбран и кнопки ещё не показывались, показываем их
            if not master_id and not master_selection_shown:
                context.user_data['master_selection_shown'] = True
                _save_context_state(chat_id, context)
                
                # Проверяем, есть ли любимый мастер
                favorite_master_name = context.user_data.get('favorite_master_name')
                if favorite_master_name:
                    message_text = f"Отлично! Хотите записаться снова к {favorite_master_name} или выберете другого мастера?"
                else:
                    message_text = "Отлично! Выберите, пожалуйста, мастера:"
                
                keyboard = create_master_selection_keyboard()
                await update.message.reply_text(message_text, reply_markup=keyboard)
                return
            
            # Определяем мастера и его ID (если не выбран, будет "Любой свободный мастер")
            master_id = context.user_data.get('master_id')
            master_name = context.user_data.get('master_name', 'Любой свободный мастер')
            
            # ВАЖНО: master_id обязателен для бронирования
            if not master_id:
                logger.error("[ОШИБКА] Попытка бронирования без выбора мастера")
                await update.message.reply_text("Ошибка: не выбран мастер. Пожалуйста, начните запись заново с /start")
                return
            
            try:
                event_id = book_slot(visit_time, {
                    'name': name,
                    'phone': phone,
                    'service': service,
                    'child_age': context.user_data.get('child_age', '—'),
                    'master': master_name
                }, master_id)
                client_id = upsert_client(name, phone)
                booking_id = add_booking(client_id, visit_time.isoformat(), service, event_id, master_id)
                schedule_reminders(application=context.application, chat_id=chat_id, visit_time=visit_time)
                schedule_monthly_reminder(application=context.application, chat_id=chat_id, visit_time=visit_time)
                
                # Планируем запрос оценки (если выбран мастер)
                if master_id:
                    schedule_rating_request(
                        application=context.application,
                        chat_id=chat_id,
                        visit_time=visit_time,
                        master_name=master_name,
                        booking_id=booking_id
                    )
                admin_message = (
                    f"📅 НОВАЯ ЗАПИСЬ!\n\n👤 {name}\n📱 {phone}\n🕐 {visit_time:%d.%m.%Y %H:%M}\n💇‍♀️ {service}\n✂️ Мастер: {master_name}"
                )
                await send_chunked(context, ADMIN_CHAT_ID, admin_message)
                
                master_info = f"\n✂️ Мастер: {master_name}" if master_name != 'Любой свободный мастер' else ""
                confirmation = (
                    f"✅ Готово! Вы записаны:\n\n"
                    f"👤 {name}\n"
                    f"📱 {phone}\n"
                    f"🕐 {visit_time.strftime('%d.%m.%Y %H:%M')}\n"
                    f"💇‍♀️ {service}{master_info}\n\n"
                    f"Напомню за день и за час до визита. До встречи в «Непоседах»!"
                )
                await send_chunked(context, chat_id, confirmation)
                
                # Сбрасываем контекст, но сохраняем информацию о клиенте для возможной дозаписи
                _reset_context(context, keep_client_info=True)
                _save_context_state(chat_id, context)
                logger.info(f"[ЗАПИСЬ СОЗДАНА] {name}, {phone}, {visit_time}, {service}")
            except Exception as e:
                logger.error(f"[ОШИБКА] При создании записи: {e}")
                await update.message.reply_text("Произошла ошибка при создании записи. Пожалуйста, попробуйте ещё раз или свяжитесь с администратором.")
            return
        
        # Данные ещё не полные — продолжаем диалог (LLM сам попросит недостающее)
        return
    except Exception as e:
        logger.error(f"[ОШИБКА] При обращении к DeepSeek: {e}")
        fallback_response = (
            "Извините, сейчас у меня технические проблемы. "
            "Пожалуйста, попробуйте ещё раз или свяжитесь с администратором напрямую."
        )
        await update.message.reply_text(fallback_response)
    return

async def reply_to_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_CHAT_ID:
        await update.message.reply_text('Нет доступа.')
        return
    try:
        user_id = int(context.args[0])
        text = ' '.join(context.args[1:])
        await context.bot.send_message(chat_id=user_id, text=f'Администратор: {text}')
        await update.message.reply_text('Ответ отправлен.')
    except Exception as e:
        await update.message.reply_text(f'Ошибка: {e}')

def create_master_selection_keyboard(show_any_master=False):
    """Создаёт клавиатуру для выбора мастера"""
    keyboard = []
    row = []
    all_masters = get_all_masters()
    masters_list = list(all_masters.values())
    
    for i, master in enumerate(masters_list):
        button = InlineKeyboardButton(
            f"{master['emoji']} {master['name']}", 
            callback_data=f"master_{master['id']}"
        )
        row.append(button)
        # По 2 кнопки в ряду
        if len(row) == 2 or i == len(masters_list) - 1:
            keyboard.append(row)
            row = []
    
    # ВАЖНО: Теперь выбор мастера обязателен, кнопка "Любой мастер" по умолчанию отключена
    # Можно включить, передав show_any_master=True
    if show_any_master:
        keyboard.append([InlineKeyboardButton("✨ Любой свободный мастер", callback_data="master_any")])
    
    return InlineKeyboardMarkup(keyboard)

async def handle_master_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик выбора мастера через Inline-кнопки"""
    query = update.callback_query
    await query.answer()
    
    chat_id = update.effective_chat.id
    _load_context_state(chat_id, context)
    
    callback_data = query.data
    
    if callback_data == "master_any":
        context.user_data['master_id'] = None
        context.user_data['master_name'] = "Любой свободный мастер"
        await query.edit_message_text("Хорошо, запишу к любому свободному мастеру! ✨")
    elif callback_data.startswith("master_"):
        # Извлекаем master_id (теперь это строка, например "master_1")
        master_id = callback_data  # "master_master_1" -> нужно взять всё после первого "master_"
        master_id = "_".join(callback_data.split("_")[1:])  # "master_1"
        master = get_master_by_id(master_id)
        if master:
            context.user_data['master_id'] = master_id
            context.user_data['master_name'] = master['name']
            await query.edit_message_text(f"Отлично! Записываю к {master['emoji']} {master['name']}")
        else:
            await query.edit_message_text("Произошла ошибка. Попробуйте ещё раз.")
            return
    
    _save_context_state(chat_id, context)
    
    logger.info(f"[DEBUG] Мастер выбран: {context.user_data.get('master_name', 'не указан')}")
    
    # Проверяем, есть ли все данные для создания записи
    name = context.user_data.get('client_name')
    phone = context.user_data.get('client_phone')
    visit_time = context.user_data.get('visit_time')
    service = context.user_data.get('service')
    master_id = context.user_data.get('master_id')
    master_name = context.user_data.get('master_name', 'Любой свободный мастер')
    
    # ВАЖНО: master_id обязателен для бронирования
    if not master_id:
        logger.error("[ОШИБКА] Попытка бронирования без выбора мастера")
        await query.message.reply_text("Ошибка: не выбран мастер. Пожалуйста, начните запись заново с /start")
        return
    
    if name and phone and visit_time and service:
        try:
            event_id = book_slot(visit_time, {
                'name': name,
                'phone': phone,
                'service': service,
                'child_age': context.user_data.get('child_age', '—'),
                'master': master_name
            }, master_id)
            client_id = upsert_client(name, phone)
            booking_id = add_booking(client_id, visit_time.isoformat(), service, event_id, master_id)
            schedule_reminders(application=context.application, chat_id=chat_id, visit_time=visit_time)
            schedule_monthly_reminder(application=context.application, chat_id=chat_id, visit_time=visit_time)
            
            # Планируем запрос оценки (если выбран мастер)
            if master_id:
                schedule_rating_request(
                    application=context.application,
                    chat_id=chat_id,
                    visit_time=visit_time,
                    master_name=master_name,
                    booking_id=booking_id
                )
            
            # Отправляем подтверждение администратору
            admin_chat_id = os.getenv('ADMIN_CHAT_ID')
            if admin_chat_id:
                confirmation = (
                    f"📅 НОВАЯ ЗАПИСЬ!\n\n"
                    f"👤 {name}\n"
                    f"📱 {phone}\n"
                    f"🕐 {visit_time.strftime('%d.%m.%Y %H:%M')}\n"
                    f"💇‍♀️ {service}\n"
                    f"✂️ Мастер: {master_name}"
                )
                await context.bot.send_message(
                    chat_id=admin_chat_id,
                    text=confirmation
                )
            
            # Сбрасываем контекст, но сохраняем информацию о клиенте для возможной дозаписи
            _reset_context(context, keep_client_info=True)
            _save_context_state(chat_id, context)
            logger.info(f"[ЗАПИСЬ СОЗДАНА] {name}, {phone}, {visit_time}, {service}")
            
            # Отправляем финальное подтверждение клиенту
            final_msg = (
                f"✅ Отлично! Вы записаны на {service.lower()} "
                f"{visit_time.strftime('%d.%m.%Y')} в {visit_time.strftime('%H:%M')}.\n\n"
                f"Мастер: {master_name}\n"
                f"Телефон для напоминания: {phone}\n\n"
                f"Ждём вас в «Непоседах»! 🌸"
            )
            await context.bot.send_message(chat_id=chat_id, text=final_msg)
            
        except Exception as e:
            logger.error(f"[ОШИБКА] При создании записи: {e}")
            await context.bot.send_message(
                chat_id=chat_id,
                text="Произошла ошибка при создании записи. Пожалуйста, попробуйте ещё раз или свяжитесь с администратором."
            )

async def handle_rating(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик оценки мастера"""
    query = update.callback_query
    await query.answer()
    
    try:
        # Формат callback_data: rate_{booking_id}_{rating}
        parts = query.data.split('_')
        booking_id = int(parts[1])
        rating = int(parts[2])
        
        # Сохраняем оценку в базу
        chat_id = update.effective_chat.id
        _load_context_state(chat_id, context)
        
        # Получаем client_id по телефону (если есть)
        client_phone = context.user_data.get('client_phone')
        if client_phone:
            client_id = upsert_client(context.user_data.get('client_name', 'Клиент'), client_phone)
        else:
            # Если телефона нет, используем telegram user_id
            client_id = update.effective_user.id
        
        # Получаем master_id из booking (предполагаем, что booking_id есть)
        # Здесь нужно добавить функцию get_booking_info в db.py, но пока используем временное решение
        # Для простоты сохраняем без привязки к конкретному booking
        
        # Добавляем оценку (пока без master_id, нужно доработать)
        logger.info(f"[RATING] Получена оценка {rating} звезд для booking_id={booking_id}")
        
        stars = "⭐" * rating
        await query.edit_message_text(
            f"Спасибо за вашу оценку! {stars}\n\nМы ценим ваше мнение и будем рады видеть вас снова! 😊"
        )
        
        # Уведомляем администратора
        await context.bot.send_message(
            ADMIN_CHAT_ID,
            f"📊 НОВАЯ ОЦЕНКА!\n\nОценка: {stars} ({rating}/5)\nЗапись ID: {booking_id}"
        )
    except Exception as e:
        logger.error(f"[ОШИБКА] При обработке оценки: {e}")
        await query.edit_message_text("Произошла ошибка при сохранении оценки. Спасибо за желание оценить нашу работу!")

def setup_handlers(app):
    app.add_handler(CommandHandler('start', start))
    app.add_handler(CallbackQueryHandler(handle_master_selection, pattern="^master_"))
    app.add_handler(CallbackQueryHandler(handle_rating, pattern="^rate_"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))
    app.add_handler(CommandHandler('reply', reply_to_user))
