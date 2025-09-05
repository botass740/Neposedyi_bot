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
logging.basicConfig(level=logging.INFO)
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
    for key in ['visit_time', 'client_name', 'client_phone', 'service', 'child_age', 'date', 'time', 'pending_date', 'history']:
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

    if 'перенести запись' in user_text or 'изменить дату' in user_text or 'новое время' in user_text:
        parsed_dt = dateparser.parse(user_text_raw, languages=['ru'], settings={'PREFER_DATES_FROM': 'future', 'RELATIVE_BASE': datetime.datetime.now()})
        if parsed_dt:
            if parsed_dt.tzinfo is None:
                parsed_dt = parsed_dt.replace(tzinfo=TZ)
            context.user_data['visit_time'] = parsed_dt
            context.user_data['date'] = parsed_dt.date().isoformat()
            context.user_data['time'] = parsed_dt.strftime('%H:%M')
            _save_context_state(chat_id, context)
            await update.message.reply_text(f"Ваша запись перенесена на {parsed_dt.strftime('%d.%m.%Y %H:%M')}")
        else:
            await update.message.reply_text("Пожалуйста, укажите новую дату и время (например: 'перенести запись на 15 сентября к 14:00').")
        return

    # --- Фильтр намерения: запись или свободный вопрос ---
    intent_words = [
        "записаться", "записать", "хочу стрижку", "хочу укладку", "стрижку", "укладку", "постричься", "подстричься", "освежить",
        "стрижка", "укладка", "окрашивание", "окрасить", "парикмахер", "мастер", "парикмахерская", "салон", "услуга"
    ]
    if any(word in user_text for word in intent_words):
        # --- Пошаговый сбор данных и живой диалог (сценарий записи) ---
        # 1. Попробовать извлечь имя и телефон из сообщения
        name_phone_match = re.match(r'^\s*([А-Яа-яA-Za-zЁё\-\s]+)[,;\s]+(\+?\d[\d\s\-\(\)]{8,})\s*$', user_text_raw)
        if name_phone_match:
            context.user_data['client_name'] = name_phone_match.group(1).strip()
            phone_norm = normalize_ru_phone(name_phone_match.group(2).strip())
            if phone_norm:
                context.user_data['client_phone'] = phone_norm
            _save_context_state(chat_id, context)

        # 2. Попробовать извлечь только телефон
        phone_match = re.search(r'(\+?\d[\d\s\-\(\)]{8,})', user_text_raw)
        if phone_match and not context.user_data.get('client_phone'):
            phone_norm = normalize_ru_phone(phone_match.group(1))
            if phone_norm:
                context.user_data['client_phone'] = phone_norm
                _save_context_state(chat_id, context)

        # 3. Попробовать извлечь только имя (если нет телефона)
        if not context.user_data.get('client_name') and len(user_text_raw.split()) == 1 and user_text_raw.isalpha():
            context.user_data['client_name'] = user_text_raw.strip().capitalize()
            _save_context_state(chat_id, context)

        # 4. Попробовать извлечь дату/время (универсальный парсер)
        if not context.user_data.get('visit_time'):
            parsed_dt = dateparser.parse(user_text_raw, languages=['ru'], settings={'PREFER_DATES_FROM': 'future', 'RELATIVE_BASE': datetime.datetime.now()})
            if parsed_dt:
                if parsed_dt.tzinfo is None:
                    parsed_dt = parsed_dt.replace(tzinfo=TZ)
                context.user_data['visit_time'] = parsed_dt
                context.user_data['date'] = parsed_dt.date().isoformat()
                context.user_data['time'] = parsed_dt.strftime('%H:%M')
                _save_context_state(chat_id, context)

        # 5. Попробовать извлечь услугу
        if not context.user_data.get('service'):
            if 'стрижк' in user_text:
                context.user_data['service'] = 'Стрижка'
            elif 'укладк' in user_text:
                context.user_data['service'] = 'Укладка'
            elif 'окраш' in user_text or 'колор' in user_text:
                context.user_data['service'] = 'Окрашивание'
            _save_context_state(chat_id, context)

        # 6. Проверить, хватает ли всего для записи
        required_fields = ['client_name', 'client_phone', 'visit_time', 'service']
        missing = [f for f in required_fields if not context.user_data.get(f)]
        known = []
        if context.user_data.get('client_name'):
            known.append(f"Имя: {context.user_data['client_name']}")
        if context.user_data.get('client_phone'):
            known.append(f"Телефон: {context.user_data['client_phone']}")
        if context.user_data.get('visit_time'):
            known.append(f"Дата и время: {context.user_data['visit_time'].strftime('%d.%m.%Y %H:%M')}")
        if context.user_data.get('service'):
            known.append(f"Услуга: {context.user_data['service']}")

        if not missing:
            # Всё есть — создаём запись
            name = context.user_data['client_name']
            phone = context.user_data['client_phone']
            visit_time = context.user_data['visit_time']
            service = context.user_data['service']
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
                await send_chunked(
                    context,
                    chat_id,
                    f"Готово!\nИмя: {name}\nТелефон: {phone}\nУслуга: {service}\nКогда: {visit_time:%d.%m.%Y %H:%M}\n"
                    "Я напомню за день и за час до визита. До встречи в «Непоседах»!"
                )
                _reset_context(context)
                _save_context_state(chat_id, context)
            except Exception as e:
                print(f"[ОШИБКА] При создании записи (живой ассистент): {e}")
                await update.message.reply_text("Произошла ошибка при создании записи.")
            return
        else:
            # Не хватает чего-то — спрашиваем только это
            if 'client_name' in missing and 'client_phone' in missing:
                await update.message.reply_text(
                    f"{'; '.join(known)}\n\nПришлите, пожалуйста, имя и номер телефона в одном сообщении."
                )
                return
            if 'client_name' in missing:
                await update.message.reply_text(
                    f"{'; '.join(known)}\n\nПожалуйста, напишите ваше имя."
                )
                return
            if 'client_phone' in missing:
                await update.message.reply_text(
                    f"{'; '.join(known)}\n\nПожалуйста, напишите ваш номер телефона."
                )
                return
            if 'visit_time' in missing:
                await update.message.reply_text(
                    f"{'; '.join(known)}\n\nКогда вам удобно записаться? Назовите дату и время."
                )
                return
            if 'service' in missing:
                await update.message.reply_text(
                    f"{'; '.join(known)}\n\nКакую услугу вы хотите? (стрижка, укладка, окрашивание и т.д.)"
                )
                return
        # Если не удалось распознать ничего — fallback
        await update.message.reply_text(
            "Извините, я не совсем поняла. Пожалуйста, уточните, что вы хотите: дату, время, имя, телефон или услугу."
        )
        return
    else:
        # --- Свободный вопрос: ответ через DeepSeek (ИИ) ---
        await context.bot.send_chat_action(chat_id=chat_id, action="typing")
        first_name = context.user_data.get('tg_first_name') or ''
        username = context.user_data.get('tg_username') or ''
        user_meta = f"(id:{user_id} {first_name} @{username})".strip()
        history = context.user_data.get('history', [])[-8:]
        history.append({"role": "user", "content": f"{user_meta}: {user_text_raw}"})
        context.user_data['history'] = history
        _save_context_state(chat_id, context)
        try:
            response = ask_deepseek(user_text_raw, history=history)
            print(f"[ОТЛАДКА] Ответ ИИ: {response}")
            if response and response != "Извините, сейчас не могу ответить. Попробуйте позже.":
                history.append({"role": "assistant", "content": response})
                context.user_data['history'] = history
                await send_chunked(context, chat_id, response)
                return
            else:
                fallback_response = (
                    "Здравствуйте! 😊 Я — ассистент администратора салона «Непоседы». "
                    "Помогу вам с записью на стрижку, укладку или другие услуги. "
                    "На какой день и время вам удобно подойти?"
                )
                await update.message.reply_text(fallback_response)
        except Exception as e:
            print(f"[ОШИБКА] При обращении к DeepSeek: {e}")
            fallback_response = (
                "Здравствуйте! 😊 Я — ассистент администратора салона «Непоседы». "
                "Помогу вам с записью на стрижку, укладку или другие услуги. "
                "На какой день и время вам удобно подойти?"
            )
            await update.message.reply_text(fallback_response)
        return

    # --- Fallback: если пользователь просто здоровается, сразу отправить приветствие и завершить обработку ---
    greetings = [
        'привет', 'здравствуйте', 'добрый день', 'доброе утро', 'добрый вечер', 'хай', 'hello', 'hi'
    ]
    if user_text.strip() in greetings and not context.user_data.get('greeted', False):
        context.user_data['greeted'] = True
        _save_context_state(chat_id, context)
        greeting = (
            "Здравствуйте! 😊 Я — ассистент администратора салона «Непоседы». Чем могу помочь?"
        )
        await update.message.reply_text(greeting)
        return

    # Сохраняем идентификаторы Telegram для персонализации и админ-уведомлений
    tg_user = update.effective_user
    context.user_data['tg_user_id'] = tg_user.id
    context.user_data['tg_username'] = getattr(tg_user, 'username', None)
    context.user_data['tg_first_name'] = getattr(tg_user, 'first_name', None)
    _save_context_state(chat_id, context)

    # TTL 30 минут: сбрасываем контекст, если давно не общались
    last_iso = context.user_data.get('last_interaction')
    if last_iso:
        try:
            last_dt = datetime.datetime.fromisoformat(last_iso)
            if (datetime.datetime.now(tz=TZ) - last_dt).total_seconds() > 30 * 60:
                _reset_context(context)
        except Exception:
            pass
    _save_context_state(chat_id, context)

    # Обработка админ-команд
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
    # --- конец блока ---

    print(f"[ОТЛАДКА] Обрабатываем текст: '{user_text_raw}'")

    # 0) Раннее распознавание имени и телефона, чтобы не спутать телефон со временем
    early_np = re.match(r'^\s*([А-Яа-яA-Za-zЁё\-\s]+)[,;\s]+(\+?\d[\d\s\-\(\)]{8,})\s*$', user_text_raw)
    if early_np:
        name_early = early_np.group(1).strip()
        phone_early_raw = early_np.group(2).strip()
        phone_early_norm = normalize_ru_phone(phone_early_raw)
        if phone_early_norm:
            context.user_data['client_name'] = name_early
            context.user_data['client_phone'] = phone_early_norm
            _save_context_state(chat_id, context)
            vt_ctx = context.user_data.get('visit_time')
            if vt_ctx:
                service = context.user_data.get('service') or 'Стрижка'
                try:
                    event_id = book_slot(vt_ctx, {
                        'name': name_early,
                        'phone': phone_early_norm,
                        'service': service,
                        'child_age': context.user_data.get('child_age', '—')
                    })
                    client_id = upsert_client(name_early, phone_early_norm)
                    add_booking(client_id, vt_ctx.isoformat(), service, event_id)
                    schedule_reminders(application=context.application, chat_id=chat_id, visit_time=vt_ctx)
                    schedule_monthly_reminder(application=context.application, chat_id=chat_id, visit_time=vt_ctx)
                    admin_message = (
                        f"📅 НОВАЯ ЗАПИСЬ!\n\n👤 {name_early}\n📱 {phone_early_norm}\n🕐 {vt_ctx:%d.%m.%Y %H:%M}\n💇‍♀️ {service}"
                    )
                    await send_chunked(context, ADMIN_CHAT_ID, admin_message)
                    await send_chunked(
                        context,
                        chat_id,
                        (
                            f"Готово!\nИмя: {name_early}\nТелефон: {phone_early_norm}\nУслуга: {service}\nКогда: {vt_ctx:%d.%m.%Y %H:%M}\n\n"
                            "Я напомню за день и за час до визита. До встречи в «Непоседах»!"
                        )
                    )
                except Exception as e:
                    print(f"[ОШИБКА] При создании записи (ранний блок): {e}")
                    await update.message.reply_text("Произошла ошибка при создании записи.")
                return
            else:
                await update.message.reply_text(
                    "Похоже, номер указан некорректно. Укажите, пожалуйста, номер в формате +7 9ХХ ХХХ-ХХ-ХХ."
                )
                return

    visit_time: Optional[datetime.datetime] = None

    # 1) Парсинг относительных дней недели (ближайшая/следующая среда и т.п.)
    day_variant_to_weekday = {
        'понедельник': 0, 'вторник': 1, 'среда': 2, 'среду': 2, 'четверг': 3,
        'пятница': 4, 'пятницу': 4, 'суббота': 5, 'субботу': 5, 'воскресенье': 6
    }

    rel_day_regex = re.compile(
        r'\b(?:в|на)?\s*(ближайш\w*|следующ\w*)?\s*(?:в\s+)?'
        r'(понедельник|вторник|среда|среду|четверг|пятница|пятницу|суббота|субботу|воскресенье)\b'
    )

    # Новый блок: ищем явное указание "на следующей неделе"
    is_next_week = bool(re.search(r'на следующей неделе|следующая неделя', user_text))

    m = rel_day_regex.search(user_text)
    processed_text = user_text
    if m:
        prefix = m.group(1) or ''  # ближайший / следующий (может быть пусто)
        day_word = m.group(2)
        target_wd = day_variant_to_weekday.get(day_word)
        now = datetime.datetime.now(tz=TZ)
        today = now.date()
        current_wd = now.weekday()
        days_until = (target_wd - current_wd) % 7

        # Логика смещения
        if prefix.startswith('следующ') or is_next_week:
            days_until = (days_until or 7) + 7  # всегда на следующую неделю
        else:
            if days_until == 0:
                days_until = 7  # ближайший/простой: не сегодня, а следующая неделя

        target_date = today + datetime.timedelta(days=days_until)
        print(f"[ОТЛАДКА] Относительный день: '{prefix or 'просто'} {day_word}' => {target_date}")

        # Вытаскиваем время, если есть в сообщении (устойчиво к опечаткам "чаов")
        t = re.search(r'(\d{1,2})\s*(?:час(?:а|ов)?|чаов)?\s*(?:[:\.\-]?\s*(\d{2}))?', user_text)
        if t:
            hour = int(t.group(1))
            minute = int((t.group(2) or '0'))
            visit_time = datetime.datetime.combine(target_date, datetime.time(hour, minute), tzinfo=TZ)
            # Проверка занятости сразу, до обещаний
            if not is_slot_free(visit_time):
                await update.message.reply_text("Это время уже занято, подождите минутку, я уточню у мастера.")
                await send_chunked(
                    context,
                    ADMIN_CHAT_ID,
                    f"⚠️ Пересечение запроса: {visit_time:%d.%m.%Y %H:%M}. Клиент просит занятое время."
                )
                same_date = visit_time.date()
                context.user_data['pending_date'] = same_date.isoformat()
                _save_context_state(chat_id, context)
                alt = [s for s in suggest_time_slots(same_date) if s != f"{hour:02d}:{minute:02d}"]
                if not alt:
                    alt = suggest_time_slots(same_date)
                await update.message.reply_text(
                    f"Свободные варианты на {same_date.strftime('%d.%m.%Y')}: {', '.join(alt)}. Подойдёт что-то из этого?"
                )
                return
            context.user_data['visit_time'] = visit_time
            context.user_data['date'] = target_date.isoformat()
            context.user_data['time'] = f"{hour:02d}:{minute:02d}"
            _save_context_state(chat_id, context)
            print(f"[ОТЛАДКА] День+время => {visit_time}")
            # Немедленно просим имя и телефон, если дата и время уже выбраны
            if visit_time and context.user_data.get('date'):
                await update.message.reply_text(
                    f"Отлично! {visit_time.strftime('%d.%m.%Y')} в {visit_time.strftime('%H:%M')}.\n"
                    "Пришлите, пожалуйста, имя и номер телефона в одном сообщении.\n"
                    "Например: Анна, +7 999 123-45-67"
                )
                return
            # Немедленно просим имя и телефон, но без утверждения, что слот свободен
            await update.message.reply_text(
                f"Отлично! {target_date.strftime('%d.%m.%Y')} в {hour:02d}:{minute:02d}. "
                "Пришлите, пожалуйста, имя и номер телефона в одном сообщении.\n"
                "Например: Анна, +7 999 123-45-67"
            )
            return
        else:
            # Если время не найдено, просто запомнить дату и предложить выбрать время
            context.user_data['pending_date'] = target_date.isoformat()
            context.user_data['date'] = target_date.isoformat()
            _save_context_state(chat_id, context)
            pref = detect_time_preference(user_text)
            slots = suggest_time_slots(target_date, pref)
            await update.message.reply_text(
                f"Отлично! {target_date.strftime('%d.%m.%Y')} подойдёт. Во сколько вам удобно? Могу предложить: {', '.join(slots)}."
            )
            return
    else:
        print("[ОТЛАДКА] Не найден относительный день недели")
        # Не найден относительный день недели — не трогаем target_date, продолжаем дальше
        # Просто выходим из блока, чтобы не было UnboundLocalError
        pass

    # 2) Универсальный парсер дат как запасной вариант
    if visit_time is None:
        parsed_dt = dateparser.parse(
            processed_text,
            languages=['ru'],
            settings={
                'PREFER_DATES_FROM': 'future',
                'RELATIVE_BASE': datetime.datetime.now()
            }
        )
        print(f"[ОТЛАДКА] dateparser результат: {parsed_dt}")
        if parsed_dt:
            if parsed_dt.tzinfo is None:
                parsed_dt = parsed_dt.replace(tzinfo=TZ)
            if parsed_dt.time() == dtime(0, 0):
                context.user_data['pending_date'] = parsed_dt.date().isoformat()
                context.user_data['date'] = parsed_dt.date().isoformat()
                _save_context_state(chat_id, context)
                pref = detect_time_preference(user_text)
                slots = suggest_time_slots(parsed_dt.date(), pref)
                await update.message.reply_text(
                    f"Отлично! {parsed_dt.strftime('%d.%m.%Y')} подойдёт. Во сколько вам удобно? Могу предложить: {', '.join(slots)}."
                )
                return
            else:
                visit_time = parsed_dt
                if visit_time <= datetime.datetime.now(tz=TZ):
                    await update.message.reply_text("Это время уже прошло. Пожалуйста, выберите другое время для записи.")
                    return
                context.user_data['visit_time'] = visit_time
                context.user_data['date'] = visit_time.date().isoformat()
                context.user_data['time'] = visit_time.strftime('%H:%M')
                _save_context_state(chat_id, context)
        else:
            # Если дата не распознана — просим пользователя уточнить
            await update.message.reply_text(
                "Не удалось распознать дату и время. Пожалуйста, укажите, когда вам удобно записаться (например: 'в субботу утром', 'на 15 августа к 14:00')."
            )
        # конец блока universal dateparser

    # 3) Если ранее выбрана дата (pending_date) и сейчас пришло время
    if visit_time is None:
        pending_date_iso = context.user_data.get('pending_date')
        if pending_date_iso:
            t = re.search(r'(\d{1,2})\s*(?:час(?:а|ов)?|чаов)?\s*(?:[:\.\-]?\s*(\d{2}))?', user_text)
            if t:
                hour = int(t.group(1))
                minute = int((t.group(2) or '0'))
                pd = datetime.date.fromisoformat(pending_date_iso)
                visit_time = datetime.datetime.combine(pd, datetime.time(hour, minute), tzinfo=TZ)
                if visit_time <= datetime.datetime.now(tz=TZ):
                    await update.message.reply_text("Это время уже прошло. Пожалуйста, выберите другое время для записи.")
                    return
                context.user_data['visit_time'] = visit_time
                context.user_data.pop('pending_date', None)
                context.user_data['date'] = pd.isoformat()
                context.user_data['time'] = f"{hour:02d}:{minute:02d}"
                _save_context_state(chat_id, context)
                print(f"[ОТЛАДКА] Скомбинировали pending_date + время => {visit_time}")

    # 4) Если есть visit_time (либо из текущего сообщения, либо из памяти) — просим имя и телефон и создаём запись
    if visit_time is None:
        visit_time = context.user_data.get('visit_time')
        if visit_time:
            if visit_time <= datetime.datetime.now(tz=TZ):
                await update.message.reply_text("Пожалуйста, укажите время в будущем.")
                return
            # Запоминаем услугу по ключевым словам
            if 'стрижк' in user_text:
                context.user_data['service'] = 'Стрижка'
            elif 'укладк' in user_text:
                context.user_data['service'] = 'Укладка'
            # Если прислали имя+телефон сразу
            name_phone_match = re.match(r'^\s*([А-Яа-яA-Za-zЁё\-\s]+)[,;\s]+(\+?\d[\d\s\-\(\)]{8,})\s*$', user_text_raw)
            if name_phone_match:
                name = name_phone_match.group(1).strip()
                phone_raw = name_phone_match.group(2).strip()
                phone_norm = normalize_ru_phone(phone_raw)
                if not phone_norm:
                    await update.message.reply_text(
                        "Похоже, номер указан некорректно. Укажите, пожалуйста, номер в формате +7 9ХХ ХХХ-ХХ-ХХ."
                    )
                    return
                service = context.user_data.get('service') or 'Стрижка'
                try:
                    # Проверим занятость слота
                    if not is_slot_free(visit_time):
                        await update.message.reply_text("Это время уже занято, подождите минутку, я уточню у мастера.")
                        await send_chunked(
                            context,
                            ADMIN_CHAT_ID,
                            f"⚠️ Пересечение записи: {visit_time:%d.%m.%Y %H:%M}. Клиент {name}, {phone_norm}, услуга {service}"
                        )
                        # Сольём данные в существующее событие и предложим альтернативы клиенту
                        merge_client_into_event(visit_time, {
                            'name': name,
                            'phone': phone_norm,
                            'service': service,
                            'child_age': context.user_data.get('child_age', '—')
                        })
                        same_date = visit_time.date()
                        context.user_data['pending_date'] = same_date.isoformat()
                        _save_context_state(chat_id, context)
                        alt_slots = [s for s in suggest_time_slots(same_date) if s != visit_time.strftime('%H:%M')]
                        if not alt_slots:
                            alt_slots = suggest_time_slots(same_date)
                        await send_chunked(context, chat_id, f"Свободные варианты на {same_date.strftime('%d.%m.%Y')}: {', '.join(alt_slots)}. Подойдёт что-то из этого?")
                        return
                    event_id = book_slot(visit_time, {'name': name, 'phone': phone_norm, 'service': service,
                                                      'child_age': context.user_data.get('child_age', '—')})
                    client_id = upsert_client(name, phone_norm)
                    add_booking(client_id, visit_time.isoformat(), service, event_id)
                    schedule_reminders(application=context.application, chat_id=chat_id, visit_time=visit_time)
                    schedule_monthly_reminder(application=context.application, chat_id=chat_id, visit_time=visit_time)
                    admin_message = (
                        f"📅 НОВАЯ ЗАПИСЬ!\n\n👤 {name}\n📱 {phone_norm}\n🕐 {visit_time:%d.%m.%Y %H:%M}\n💇‍♀️ {service}"
                    )
                    await send_chunked(context, ADMIN_CHAT_ID, admin_message)
                    await send_chunked(context, chat_id,
                        f"Готово!\nИмя: {name}\nТелефон: {phone_norm}\nУслуга: {service}\nКогда: {visit_time:%d.%m.%Y %H:%M}\n"
                        "Адрес: Севастополь, Античный проспект, 26, корп. 4\n\nДо встречи в «Непоседах»!")
                    _reset_context(context)
                    _save_context_state(chat_id, context)
                except Exception as e:
                    print(f"[ОШИБКА] При создании записи: {e}")
                    await update.message.reply_text("Произошла ошибка при создании записи.")
                return
            await update.message.reply_text("Отлично! Пришлите, пожалуйста, имя и номер телефона в одном сообщении.\nНапример: Анна, +7 999 123-45-67")
            return

    # Если есть явное намерение записаться, но дата не распознана — уточнить дату и время
    intent_words = [
        "записаться", "записать", "хочу стрижку", "хочу укладку", "стрижку", "укладку", "постричься", "подстричься", "освежить"
    ]
    if visit_time is None and any(word in user_text for word in intent_words):
        await update.message.reply_text(
            "Когда вам удобно записаться? Назовите, пожалуйста, дату и примерное время (например: 'в субботу утром', 'на 15 августа к 14:00')."
        )
        return

    # 5) Иначе — обычный диалог через LLM
    # --- ДОБАВЛЯЕМ ФИЛЬТР ТЕМАТИКИ ---
    topic_keywords = [
        'волос', 'стриж', 'салон', 'уклад', 'цена', 'запис', 'мастер', 'парикмах', 'уход',
        'детск', 'взросл', 'плетен', 'окраш', 'красот', 'причес', 'причёск', 'консультац', 'совет'
    ]
    if not any(kw in user_text for kw in topic_keywords):
        await update.message.reply_text(
            'Я могу помочь только по вопросам салона красоты, стрижек и ухода за волосами. Чем могу быть полезна?'
        )
        return
    # --- КОНЕЦ ФИЛЬТРА ---
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

    # --- LLM (DeepSeek) — первый обработчик любого сообщения ---
    await context.bot.send_chat_action(chat_id=chat_id, action="typing")
    first_name = context.user_data.get('tg_first_name') or ''
    username = context.user_data.get('tg_username') or ''
    user_meta = f"(id:{user_id} {first_name} @{username})".strip()
    history.append({"role": "user", "content": f"{user_meta}: {user_text_raw}"})
    context.user_data['history'] = history
    _save_context_state(chat_id, context)
    try:
        response = ask_deepseek(user_text_raw, history=history)
        print(f"[ОТЛАДКА] Ответ ИИ: {response}")
        if response and response != "Извините, сейчас не могу ответить. Попробуйте позже.":
            history.append({"role": "assistant", "content": response})
            context.user_data['history'] = history
            await send_chunked(context, chat_id, response)
            # --- Здесь можно анализировать response на предмет явного намерения записаться ---
            # Например, если в ответе есть фраза "Давайте запишу вас" или "Когда вам удобно прийти?" — можно запустить сценарий записи
            # (Оставляем как задел для будущей доработки)
            return
        else:
            fallback_response = (
                "Здравствуйте! 😊 Я — ассистент администратора салона «Непоседы». "
                "Помогу вам с записью на стрижку, укладку или другие услуги. "
                "На какой день и время вам удобно подойти?"
            )
            await update.message.reply_text(fallback_response)
    except Exception as e:
        print(f"[ОШИБКА] При обращении к DeepSeek: {e}")
        fallback_response = (
            "Здравствуйте! 😊 Я — ассистент администратора салона «Непоседы». "
            "Помогу вам с записью на стрижку, укладку или другие услуги. "
            "На какой день и время вам удобно подойти?"
        )
        await update.message.reply_text(fallback_response)

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
