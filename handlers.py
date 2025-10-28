import re
import os
import datetime
from telegram import Update
from telegram.ext import CommandHandler, MessageHandler, ContextTypes, filters
from deepseek import ask_deepseek
from reminder import schedule_reminders, schedule_monthly_reminder
from calendar_api import book_slot, list_events_for_date, delete_event, update_event_time, is_slot_free, merge_client_into_event
from dotenv import load_dotenv
from zoneinfo import ZoneInfo
from typing import Optional
import logging
import json
from state_store import get_user_state, update_user_state
from db import upsert_client, add_booking
from textwrap import wrap
import dateparser
from datetime import time as dtime
import json
from reminder import scheduler
from calendar_api import get_free_slots

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
    """Пытается привести номер к формату +7XXXXXXXXXX. Возвращает None, если номер некорректен."""
    if not raw_phone:
        return None
    
    # Убираем все нецифровые символы
    digits = re.sub(r'\D', '', raw_phone)
    
    # Проверяем длину и корректность
    if len(digits) == 11:
        # 11 цифр: 7XXXXXXXXXX или 8XXXXXXXXXX
        if digits[0] in ('7', '8'):
            return '+7' + digits[1:]
    elif len(digits) == 10:
        # 10 цифр: 9XXXXXXXXX или 8XXXXXXXXX (без кода страны)
        if digits[0] in ('9', '8'):
            return '+7' + digits
    elif len(digits) == 12:
        # 12 цифр: 89XXXXXXXXXX (с лишней 8)
        if digits[0:2] == '89':
            return '+7' + digits[2:]
    
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
        'pending_date': context.user_data.get('pending_date')
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

def _reset_context(context: ContextTypes.DEFAULT_TYPE) -> None:
    for key in ['visit_time', 'client_name', 'client_phone', 'service', 'child_age', 'date', 'time', 'pending_date', 'history', 'time_checked']:
        context.user_data.pop(key, None)
    context.user_data['greeted'] = False

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    context.user_data['greeted'] = True
    greeting = (
        "Здравствуйте! 😊 Я — ассистент администратора салона «Непоседы». Чем могу помочь?"
    )
    await update.message.reply_text(greeting)
    _save_context_state(update.effective_chat.id, context)

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
    history = context.user_data.get('history', [])[-8:]
    user_text_raw = update.message.text
    user_text = user_text_raw.lower()

    # Сохраняем идентификаторы Telegram для персонализации
    tg_user = update.effective_user
    context.user_data['tg_user_id'] = tg_user.id
    context.user_data['tg_username'] = getattr(tg_user, 'username', None)
    context.user_data['tg_first_name'] = getattr(tg_user, 'first_name', None)
    
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

    # Извлекаем только телефон
    phone_match = re.search(r'(\+?\d[\d\s\-\(\)]{8,})', user_text_raw)
    if phone_match and not context.user_data.get('client_phone'):
        phone_norm = normalize_ru_phone(phone_match.group(1))
        if phone_norm:
            context.user_data['client_phone'] = phone_norm
            _save_context_state(chat_id, context)

    # Извлекаем дату/время (универсальный парсер)
    if not context.user_data.get('visit_time'):
        # Сначала пробуем стандартный парсер
        parsed_dt = dateparser.parse(
            user_text_raw, 
            languages=['ru'], 
            settings={
                'PREFER_DATES_FROM': 'future', 
                'RELATIVE_BASE': datetime.datetime.now(tz=TZ),
                'TIMEZONE': 'Europe/Moscow',
                'RETURN_AS_TIMEZONE_AWARE': True
            }
        )
        
        # Если не сработало, пробуем ручной парсинг для "завтра/сегодня в/на ЧЧ:ММ"
        if not parsed_dt:
            time_patterns = [
                r'(завтра|сегодня|послезавтра)\s+(?:в|на)\s+(\d{1,2})[:\.](\d{2})',
                r'(завтра|сегодня|послезавтра)\s+(?:в|на)\s+(\d{1,2})',
                r'(?:в|на)\s+(\d{1,2})[:\.](\d{2})',
                r'(?:в|на)\s+(\d{1,2})\s*(?:час|ч)',
            ]
            for pattern in time_patterns:
                match = re.search(pattern, user_text, re.IGNORECASE)
                if match:
                    groups = match.groups()
                    now = datetime.datetime.now(tz=TZ)
                    
                    # Определяем дату
                    if groups[0] in ['завтра']:
                        target_date = now.date() + datetime.timedelta(days=1)
                    elif groups[0] in ['послезавтра']:
                        target_date = now.date() + datetime.timedelta(days=2)
                    elif groups[0] in ['сегодня']:
                        target_date = now.date()
                    else:
                        # Если день не указан, но есть время, берём ближайшее будущее
                        target_date = now.date()
                    
                    # Определяем время
                    if len(groups) >= 3 and groups[2]:
                        hour, minute = int(groups[1]), int(groups[2])
                    elif len(groups) >= 2 and groups[1]:
                        hour, minute = int(groups[1]), 0
                    else:
                        continue
                    
                    try:
                        parsed_dt = datetime.datetime.combine(target_date, datetime.time(hour, minute, tzinfo=TZ))
                        # Если время уже прошло сегодня, переносим на завтра
                        if parsed_dt <= now:
                            parsed_dt = parsed_dt + datetime.timedelta(days=1)
                        break
                    except ValueError:
                        continue
        
        if parsed_dt:
            if parsed_dt.tzinfo is None:
                parsed_dt = parsed_dt.replace(tzinfo=TZ)
            # Проверяем, что время не в прошлом
            if parsed_dt > datetime.datetime.now(tz=TZ):
                context.user_data['visit_time'] = parsed_dt
                context.user_data['date'] = parsed_dt.date().isoformat()
                context.user_data['time'] = parsed_dt.strftime('%H:%M')
                logger.info(f"[DEBUG] Успешно распознано время: {parsed_dt}")
                _save_context_state(chat_id, context)

    # Извлекаем услугу по ключевым словам (только если уже есть дата/время или явный запрос)
    has_date_context = context.user_data.get('visit_time') or context.user_data.get('date')
    explicit_booking = any(word in user_text for word in ['записать', 'запишите', 'хочу записаться', 'нужна запись'])
    
    if not context.user_data.get('service') and (has_date_context or explicit_booking):
        if 'стрижк' in user_text:
            context.user_data['service'] = 'Стрижка'
        elif 'укладк' in user_text:
            context.user_data['service'] = 'Укладка'
        elif 'окраш' in user_text or 'колор' in user_text:
            context.user_data['service'] = 'Окрашивание'
        elif 'плетен' in user_text:
            context.user_data['service'] = 'Плетение'
        _save_context_state(chat_id, context)

    # Извлекаем возраст ребёнка
    if not context.user_data.get('child_age'):
        child_age = parse_child_age(user_text_raw)
        if child_age:
            context.user_data['child_age'] = child_age
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
    
    context_info = []
    
    # Добавляем текущую дату и время для контекста LLM
    now = datetime.datetime.now(tz=TZ)
    current_hour = now.hour
    context_info.append(f"[ТЕКУЩЕЕ ВРЕМЯ: {now.strftime('%d.%m.%Y %H:%M')} - {now.strftime('%A')}]")
    
    if context.user_data.get('client_name'):
        context_info.append(f"[Имя клиента: {context.user_data['client_name']}]")
    if context.user_data.get('client_phone'):
        context_info.append(f"[Телефон клиента: {context.user_data['client_phone']}]")
    if context.user_data.get('visit_time'):
        vt = context.user_data['visit_time']
        context_info.append(f"[Выбранное время: {vt.strftime('%d.%m.%Y %H:%M')}]")
    if context.user_data.get('service'):
        context_info.append(f"[Услуга: {context.user_data['service']}]")
    if context.user_data.get('child_age'):
        context_info.append(f"[Возраст ребёнка: {context.user_data['child_age']} лет]")
    
    context_str = " ".join(context_info) if context_info else ""
    
    # --- ШАГ 3: ОТПРАВЛЯЕМ ЗАПРОС В LLM ДЛЯ "ЖИВОГО" ОТВЕТА ---
    await context.bot.send_chat_action(chat_id=chat_id, action="typing")
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
        required_fields = ['client_name', 'client_phone', 'visit_time', 'service']
        missing = [f for f in required_fields if not context.user_data.get(f)]
        
        logger.info(f"[DEBUG] Проверка полей для записи. Данные: name={context.user_data.get('client_name')}, phone={context.user_data.get('client_phone')}, time={context.user_data.get('visit_time')}, service={context.user_data.get('service')}")
        logger.info(f"[DEBUG] Недостающие поля: {missing}")
        
        if not missing:
            # ВСЕ ДАННЫЕ СОБРАНЫ — СОЗДАЁМ ЗАПИСЬ
            name = context.user_data['client_name']
            phone = context.user_data['client_phone']
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
            
            try:
                event_id = book_slot(visit_time, {
                    'name': name,
                    'phone': phone,
                    'service': service,
                    'child_age': context.user_data.get('child_age', '—')
                })
                client_id = upsert_client(name, phone)
                add_booking(client_id, visit_time.isoformat(), service, event_id)
                schedule_reminders(application=context.application, chat_id=chat_id, visit_time=visit_time)
                schedule_monthly_reminder(application=context.application, chat_id=chat_id, visit_time=visit_time)
                
                admin_message = (
                    f"📅 НОВАЯ ЗАПИСЬ!\n\n👤 {name}\n📱 {phone}\n🕐 {visit_time:%d.%m.%Y %H:%M}\n💇‍♀️ {service}"
                )
                await send_chunked(context, ADMIN_CHAT_ID, admin_message)
                
                confirmation = (
                    f"✅ Готово! Вы записаны:\n\n"
                    f"👤 {name}\n"
                    f"📱 {phone}\n"
                    f"🕐 {visit_time.strftime('%d.%m.%Y %H:%M')}\n"
                    f"💇‍♀️ {service}\n\n"
                    f"Напомню за день и за час до визита. До встречи в «Непоседах»!"
                )
                await send_chunked(context, chat_id, confirmation)
                
                _reset_context(context)
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

def setup_handlers(app):
    app.add_handler(CommandHandler('start', start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))
    app.add_handler(CommandHandler('reply', reply_to_user))
