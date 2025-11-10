import os
from googleapiclient.discovery import build
from google.oauth2 import service_account
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from masters_config import get_master_calendar_id, get_all_masters

SCOPES = ['https://www.googleapis.com/auth/calendar']
SERVICE_ACCOUNT_FILE = os.getenv('GOOGLE_SERVICE_ACCOUNT_JSON', 'credentials.json')
CALENDAR_ID = os.getenv('GOOGLE_CALENDAR_ID', 'your_calendar_id@group.calendar.google.com')  # Устаревший, оставлен для совместимости
TZ = ZoneInfo('Europe/Moscow')

def get_service():
    """Создать сервис для работы с Google Calendar API"""
    try:
        creds = service_account.Credentials.from_service_account_file(
            SERVICE_ACCOUNT_FILE, scopes=SCOPES)
        return build('calendar', 'v3', credentials=creds)
    except Exception as e:
        print(f"[ОШИБКА] Не удалось создать сервис Google Calendar: {e}")
        return None

def get_free_slots(date, master_id=None):
    """Получить свободные слоты на дату (часы по Москве, 09-19).
    
    Args:
        date: дата для проверки
        master_id: ID мастера (опционально). Если None, возвращает слоты, где хотя бы один мастер свободен.
    """
    service = get_service()
    if not service:
        return ['10:00', '11:00', '12:00', '13:00', '14:00', '15:00', '16:00', '17:00', '18:00']

    try:
        day_start = datetime.combine(date, datetime.min.time(), tzinfo=TZ)
        day_end = day_start + timedelta(days=1)
        
        # Если указан конкретный мастер, проверяем только его календарь
        if master_id:
            calendar_id = get_master_calendar_id(master_id)
            if not calendar_id:
                print(f"[ОШИБКА] Календарь для мастера {master_id} не найден")
                return ['10:00', '11:00', '12:00', '13:00']
            
            events_result = service.events().list(
                calendarId=calendar_id,
                timeMin=day_start.isoformat(),
                timeMax=day_end.isoformat(),
                singleEvents=True,
                orderBy='startTime'
            ).execute()
            
            events = events_result.get('items', [])
            busy = []
            for event in events:
                start_str = event['start'].get('dateTime')
                end_str = event['end'].get('dateTime')
                if start_str and end_str:
                    busy.append((datetime.fromisoformat(start_str), datetime.fromisoformat(end_str)))
        else:
            # Проверяем все календари мастеров и собираем занятые слоты
            all_masters = get_all_masters()
            busy = []
            
            for master_key, master_data in all_masters.items():
                calendar_id = master_data.get('calendar_id')
                if not calendar_id:
                    continue
                
                events_result = service.events().list(
                    calendarId=calendar_id,
                    timeMin=day_start.isoformat(),
                    timeMax=day_end.isoformat(),
                    singleEvents=True,
                    orderBy='startTime'
                ).execute()
                
                events = events_result.get('items', [])
                for event in events:
                    start_str = event['start'].get('dateTime')
                    end_str = event['end'].get('dateTime')
                    if start_str and end_str:
                        busy.append((datetime.fromisoformat(start_str), datetime.fromisoformat(end_str)))

        free_slots = []
        current_time = day_start.replace(hour=9, minute=0, second=0, microsecond=0)
        work_end = day_start.replace(hour=19, minute=0, second=0, microsecond=0)

        # Текущее время для фильтрации прошедших слотов
        now = datetime.now(tz=TZ)
        
        while current_time < work_end:
            slot_end = current_time + timedelta(hours=1)
            
            if master_id:
                # Для конкретного мастера: проверяем, не занят ли он
                overlap = any((current_time < b_end and slot_end > b_start) for b_start, b_end in busy)
                if not overlap and current_time > now:
                    free_slots.append(current_time.strftime('%H:%M'))
            else:
                # Для всех мастеров: проверяем, есть ли хотя бы один свободный
                # Считаем, сколько мастеров заняты в этот слот
                busy_count = sum(1 for b_start, b_end in busy if current_time < b_end and slot_end > b_start)
                # Если свободен хотя бы один мастер (всего 4)
                if busy_count < 4 and current_time > now:
                    free_slots.append(current_time.strftime('%H:%M'))
            
            current_time += timedelta(hours=1)

        return free_slots or ['10:00', '11:00', '12:00', '13:00']
    except Exception as e:
        print(f"[ОШИБКА] Не удалось получить свободные слоты: {e}")
        return ['10:00', '11:00', '12:00', '13:00', '14:00', '15:00', '16:00', '17:00', '18:00']

def is_slot_free(slot_time: datetime, master_id: str = None) -> bool:
    """Проверить, свободен ли слот (1 час с указанного времени).
    
    Args:
        slot_time: время слота
        master_id: ID мастера. Если None, проверяет, свободен ли хотя бы один мастер.
    """
    service = get_service()
    if not service:
        return True

    start = slot_time.replace(tzinfo=TZ)
    end = (slot_time + timedelta(hours=1)).replace(tzinfo=TZ)
    
    try:
        if master_id:
            # Проверяем календарь конкретного мастера
            calendar_id = get_master_calendar_id(master_id)
            if not calendar_id:
                print(f"[ОШИБКА] Календарь для мастера {master_id} не найден")
                return False
            
            events = service.events().list(
                calendarId=calendar_id,
                timeMin=start.isoformat(),
                timeMax=end.isoformat(),
                singleEvents=True,
            ).execute().get('items', [])
            return len(events) == 0
        else:
            # Проверяем все календари мастеров
            all_masters = get_all_masters()
            for master_key, master_data in all_masters.items():
                calendar_id = master_data.get('calendar_id')
                if not calendar_id:
                    continue
                
                events = service.events().list(
                    calendarId=calendar_id,
                    timeMin=start.isoformat(),
                    timeMax=end.isoformat(),
                    singleEvents=True,
                ).execute().get('items', [])
                
                # Если хотя бы один мастер свободен
                if len(events) == 0:
                    return True
            
            # Все мастера заняты
            return False
    except Exception as e:
        print(f"[ОШИБКА] Проверка занятости слота: {e}")
        return True


def book_slot(slot_time, client_data, master_id: str):
    """Создать событие в календаре мастера. Возвращает event_id или None.
    
    Args:
        slot_time: время записи
        client_data: данные клиента (name, phone, service, child_age)
        master_id: ID мастера (обязательно)
    """
    service = get_service()
    if not service:
        print("[ОШИБКА] Сервис Google Calendar недоступен")
        return None

    # Получаем календарь мастера
    calendar_id = get_master_calendar_id(master_id)
    if not calendar_id:
        print(f"[ОШИБКА] Календарь для мастера {master_id} не найден")
        return None

    # Проверяем, свободен ли мастер в это время
    if not is_slot_free(slot_time, master_id):
        print(f"[ПРЕДУПРЕЖДЕНИЕ] Мастер {master_id} занят в это время, создание отменено")
        return None

    try:
        start_dt = slot_time.replace(tzinfo=TZ)
        end_dt = (slot_time + timedelta(hours=1)).replace(tzinfo=TZ)
        
        from masters_config import get_master_name
        master_name = get_master_name(master_id)
        
        event = {
            'summary': f'Запись: {client_data["name"]}',
            'description': (
                f'Клиент: {client_data["name"]}\n'
                f'Телефон: {client_data["phone"]}\n'
                f'Услуга: {client_data.get("service", "Не указана")}\n'
                f'Возраст ребёнка: {client_data.get("child_age", "—")}\n'
                f'Мастер: {master_name} ({master_id})'
            ),
            'start': {
                'dateTime': start_dt.isoformat(),
                'timeZone': 'Europe/Moscow',
            },
            'end': {
                'dateTime': end_dt.isoformat(),
                'timeZone': 'Europe/Moscow',
            },
        }

        event = service.events().insert(calendarId=calendar_id, body=event).execute()
        print(f"[УСПЕХ] Создано событие в календаре мастера {master_name}: {event['id']}")
        return event['id']
    except Exception as e:
        print(f"[ОШИБКА] Не удалось создать событие в календаре: {e}")
        return None


def list_events_for_date(date):
    """Список событий на дату (возвращает list[dict])."""
    service = get_service()
    if not service:
        return []

    start = datetime.combine(date, datetime.min.time(), tzinfo=TZ)
    end = start + timedelta(days=1)
    try:
        result = service.events().list(
            calendarId=CALENDAR_ID,
            timeMin=start.isoformat(),
            timeMax=end.isoformat(),
            singleEvents=True,
            orderBy='startTime',
        ).execute()
        return result.get('items', [])
    except Exception as e:
        print(f"[ОШИБКА] Не удалось получить список событий: {e}")
        return []


def delete_event(event_id: str) -> bool:
    service = get_service()
    if not service:
        return False
    try:
        service.events().delete(calendarId=CALENDAR_ID, eventId=event_id).execute()
        print(f"[УСПЕХ] Удалено событие: {event_id}")
        return True
    except Exception as e:
        print(f"[ОШИБКА] Не удалось удалить событие: {e}")
        return False


def update_event_time(event_id: str, new_start: datetime) -> bool:
    service = get_service()
    if not service:
        return False


def find_event_at(slot_time: datetime):
    """Вернуть первое событие, которое пересекается с указанным временем (1 час)."""
    service = get_service()
    if not service:
        return None
    start = slot_time.replace(tzinfo=TZ)
    end = (slot_time + timedelta(hours=1)).replace(tzinfo=TZ)
    try:
        events = service.events().list(
            calendarId=CALENDAR_ID,
            timeMin=start.isoformat(),
            timeMax=end.isoformat(),
            singleEvents=True,
            orderBy='startTime'
        ).execute().get('items', [])
        return events[0] if events else None
    except Exception as e:
        print(f"[ОШИБКА] Поиск события на время: {e}")
        return None


def merge_client_into_event(slot_time: datetime, client_data: dict) -> bool:
    """Добавить данные клиента в существующее событие (объединить).
    Обновляет summary и description, не меняя время.
    """
    service = get_service()
    if not service:
        return False
    event = find_event_at(slot_time)
    if not event:
        return False
    try:
        summary_old = event.get('summary', 'Запись')
        description_old = event.get('description', '') or ''
        summary_new = f"{summary_old} + {client_data.get('name', 'Клиент')}"
        description_append = (
            f"\n---\nКлиент: {client_data.get('name','—')}\n"
            f"Телефон: {client_data.get('phone','—')}\n"
            f"Услуга: {client_data.get('service','Не указана')}\n"
            f"Возраст ребёнка: {client_data.get('child_age','—')}"
        )
        body = {
            'summary': summary_new,
            'description': description_old + description_append,
        }
        service.events().patch(calendarId=CALENDAR_ID, eventId=event['id'], body=body).execute()
        print(f"[УСПЕХ] Объединено с событием: {event['id']}")
        return True
    except Exception as e:
        print(f"[ОШИБКА] Не удалось объединить событие: {e}")
        return False
    try:
        start_dt = new_start.replace(tzinfo=TZ)
        end_dt = (new_start + timedelta(hours=1)).replace(tzinfo=TZ)
        body = {
            'start': {'dateTime': start_dt.isoformat(), 'timeZone': 'Europe/Moscow'},
            'end': {'dateTime': end_dt.isoformat(), 'timeZone': 'Europe/Moscow'},
        }
        service.events().patch(calendarId=CALENDAR_ID, eventId=event_id, body=body).execute()
        print(f"[УСПЕХ] Перенесено событие: {event_id}")
        return True
    except Exception as e:
        print(f"[ОШИБКА] Не удалось перенести событие: {e}")
        return False