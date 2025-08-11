import re
import os
import datetime
from telegram import Update
from telegram.ext import CommandHandler, MessageHandler, ContextTypes, filters
from deepseek import ask_deepseek
from reminder import schedule_reminders
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
    digits = re.sub(r'\D', '', raw_phone)
    # 11 цифр, начинается с 7 или 8 → нормализуем к 7
    if len(digits) == 11 and digits[0] in ('7', '8'):
        digits = '7' + digits[1:]
    # 10 цифр, начинается с 9 → добавим 7 (часто пишут без кода страны)
    elif len(digits) == 10 and digits[0] == '9':
        digits = '7' + digits
    else:
        return None
    return '+7' + digits[1:]

def suggest_time_slots(for_date: datetime.date, preference: Optional[str] = None) -> list[str]:
    """Возвращает 2–3 рекомендованных слота времени в зависимости от предпочтения."""
    morning = ["10:00", "11:30"]
    day = ["14:00", "15:30"]
    evening = ["18:00", "19:00"]
    if preference == 'morning':
        return morning + [day[0]]
    if preference == 'day':
        return day + [evening[0]]
    if preference == 'evening':
        return evening + [day[0]]
    return [morning[0], day[0], evening[0]]

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
        "Здравствуйте! 😊 Я — ассистент администратора салона «Непоседы». Помогу записаться на услуги для детей и взрослых: стрижка, укладка, плетение. "
        "Скажите, пожалуйста, на какой день и время вам удобно? Если не уверены, предложу 2–3 варианта."
    )
    await update.message.reply_text(greeting)
    _save_context_state(update.effective_chat.id, context)

async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    _load_context_state(chat_id, context)
    history = context.user_data.get('history', [])[-8:]
    user_text_raw = update.message.text
    user_text = user_text_raw.lower()

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

    # Приветствие при первом сообщении в текущем запуске (чтобы не зависеть от persist)
    if not context.chat_data.get('greeted_runtime'):
        context.chat_data['greeted_runtime'] = True
        first_name = context.user_data.get('tg_first_name')
        hello_name = f", {first_name}" if first_name else ""
        greeting = (
            f"Здравствуйте{hello_name}! 😊 Я — ассистент администратора салона «Непоседы». Помогу записаться на услуги для детей и взрослых: стрижка, укладка, плетение. "
            "Скажите, пожалуйста, на какой день и время вам удобно? Если не уверены, предложу 2–3 варианта."
        )
        await update.message.reply_text(greeting)
    # Персистентное приветствие (один раз на чат)
    if not context.user_data.get('greeted'):
        context.user_data['greeted'] = True
        _save_context_state(chat_id, context)
        # Не return — продолжаем обработку, если пользователь сразу прислал данные

    # Напоминание
    if re.search(r'напомн(и|ить|ание)', user_text):
        visit_time_ctx = context.user_data.get('visit_time')
        if visit_time_ctx:
            schedule_reminders(application=context.application, chat_id=chat_id, visit_time=visit_time_ctx)
            await update.message.reply_text("Я напомню вам за день и за час до визита.")
        else:
            await update.message.reply_text("Сначала укажите дату и время визита.")
        return

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
        if prefix.startswith('следующ'):
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
            # Немедленно просим имя и телефон, но без утверждения, что слот свободен
            await update.message.reply_text(
                (
                    f"Отлично! {target_date.strftime('%d.%m.%Y')} в {hour:02d}:{minute:02d}. "
                    "Пришлите, пожалуйста, имя и номер телефона в одном сообщении.\n"
                    "Например: Анна, +7 999 123-45-67"
                )
            )
            return
        else:
            # Время не указано — предложим 2–3 варианта и сохраним pending_date
            context.user_data['pending_date'] = target_date.isoformat()
            _save_context_state(chat_id, context)
            pref = detect_time_preference(user_text)
            slots = suggest_time_slots(target_date, pref)
        await update.message.reply_text(
                (
                    f"Поняла, {target_date.strftime('%d.%m.%Y')} удобно. Во сколько предпочитаете? "
                    f"Могу предложить: {', '.join(slots)}."
                )
        )
        return

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
                context.user_data['visit_time'] = visit_time
                context.user_data['date'] = visit_time.date().isoformat()
                context.user_data['time'] = visit_time.strftime('%H:%M')
                _save_context_state(chat_id, context)

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
                    # Сольём данные в существующее событие (по запросу) и предложим альтернативы клиенту
                    merge_client_into_event(visit_time, {
            'name': name,
                        'phone': phone_norm,
                        'service': service,
            'child_age': context.user_data.get('child_age', '—')
        })
                    # Предложим другие слоты на тот же день
                    same_date = visit_time.date()
                    context.user_data['pending_date'] = same_date.isoformat()
                    _save_context_state(chat_id, context)
                    alt_slots = [s for s in suggest_time_slots(same_date) if s != visit_time.strftime('%H:%M')]
                    if not alt_slots:
                        alt_slots = suggest_time_slots(same_date)
                    await send_chunked(
                        context,
                        chat_id,
                        f"Свободные варианты на {same_date.strftime('%d.%m.%Y')}: {', '.join(alt_slots)}. Подойдёт что-то из этого?"
                    )
                    return
                event_id = book_slot(visit_time, {'name': name, 'phone': phone_norm, 'service': service,
                                                  'child_age': context.user_data.get('child_age', '—')})
                client_id = upsert_client(name, phone_norm)
                add_booking(client_id, visit_time.isoformat(), service, event_id)
                schedule_reminders(application=context.application, chat_id=chat_id, visit_time=visit_time)
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

        # Если имя+телефон ещё не прислали — просим их
        await update.message.reply_text(
            "Отлично! Пришлите, пожалуйста, имя и номер телефона в одном сообщении.\n"
            "Например: Анна, +7 999 123-45-67"
        )
        return

    # 5) Иначе — обычный диалог через LLM
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

    # Убрано дублирование блока LLM — используется единый блок ниже

    # Остальной диалог через LLM (админ-команды уже обработаны выше)
    await context.bot.send_chat_action(chat_id=chat_id, action="typing")
    # Поддерживаем краткую историю диалога в памяти
    first_name = context.user_data.get('tg_first_name') or ''
    username = context.user_data.get('tg_username') or ''
    user_meta = f"(id:{user_id} {first_name} @{username})".strip()
    history.append({"role": "user", "content": f"{user_meta}: {user_text_raw}"})
    context.user_data['history'] = history
    _save_context_state(chat_id, context)
    try:
        # Передаём LLM сокращённый, но информативный контекст и системный стиль уже задан.
        response = ask_deepseek(user_text_raw, history=history)
        if response and response != "Извините, сейчас не могу ответить. Попробуйте позже.":
    history.append({"role": "assistant", "content": response})
    context.user_data['history'] = history
            await send_chunked(context, chat_id, response)
        else:
            fallback_response = (
                "Здравствуйте! 😊 Я — администратор салона «Непоседы». "
                "Помогу вам с записью на стрижку, укладку или другие услуги. "
                "На какой день и время вам удобно подойти?"
            )
            await update.message.reply_text(fallback_response)
    except Exception as e:
        print(f"[ОШИБКА] При обращении к DeepSeek: {e}")
        fallback_response = (
            "Здравствуйте! 😊 Я — администратор салона «Непоседы». "
            "Помогу вам с записью на стрижку, укладку или другие услуги. "
            "На какой день и время вам удобно подойти?"
        )
        await update.message.reply_text(fallback_response)

def setup_handlers(app):
    app.add_handler(CommandHandler('start', start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))
