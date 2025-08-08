import os
from googleapiclient.discovery import build
from google.oauth2 import service_account
from datetime import datetime, timedelta

SCOPES = ['https://www.googleapis.com/auth/calendar']
SERVICE_ACCOUNT_FILE = os.getenv('GOOGLE_SERVICE_ACCOUNT_JSON', 'credentials.json')
CALENDAR_ID = os.getenv('GOOGLE_CALENDAR_ID', 'your_calendar_id@group.calendar.google.com')

def get_service():
    """Создать сервис для работы с Google Calendar API"""
    try:
        creds = service_account.Credentials.from_service_account_file(
            SERVICE_ACCOUNT_FILE, scopes=SCOPES)
        return build('calendar', 'v3', credentials=creds)
    except Exception as e:
        print(f"[ОШИБКА] Не удалось создать сервис Google Calendar: {e}")
        return None

def get_free_slots(date):
    """Получить свободные слоты на дату"""
    service = get_service()
    if not service:
        return ['10:00', '11:00', '12:00', '13:00', '14:00', '15:00', '16:00', '17:00', '18:00']
    
    try:
        start_time = datetime.combine(date, datetime.min.time())
        end_time = start_time + timedelta(days=1)
        
        events_result = service.events().list(
            calendarId=CALENDAR_ID,
            timeMin=start_time.isoformat() + 'Z',
            timeMax=end_time.isoformat() + 'Z',
            singleEvents=True,
            orderBy='startTime'
        ).execute()
        
        events = events_result.get('items', [])
        busy_times = []
        for event in events:
            start = event['start'].get('dateTime', event['start'].get('date'))
            end = event['end'].get('dateTime', event['end'].get('date'))
            busy_times.append((start, end))
        
        # Генерируем свободные слоты (с 9:00 до 19:00)
        free_slots = []
        work_start = datetime.combine(date, datetime.min.time().replace(hour=9))
        work_end = datetime.combine(date, datetime.min.time().replace(hour=19))
        
        current_time = work_start
        while current_time < work_end:
            slot_end = current_time + timedelta(hours=1)
            is_free = True
            for busy_start, busy_end in busy_times:
                if current_time < busy_end and slot_end > busy_start:
                    is_free = False
                    break
            if is_free:
                free_slots.append(current_time.strftime('%H:%M'))
            current_time += timedelta(hours=1)
        
        return free_slots if free_slots else ['10:00', '11:00', '12:00', '13:00']
    except Exception as e:
        print(f"[ОШИБКА] Не удалось получить свободные слоты: {e}")
        return ['10:00', '11:00', '12:00', '13:00', '14:00', '15:00', '16:00', '17:00', '18:00']

def book_slot(slot_time, client_data):
    """Создать событие в календаре"""
    service = get_service()
    if not service:
        print("[ОШИБКА] Сервис Google Calendar недоступен")
        return None
    
    try:
        event = {
            'summary': f'Запись: {client_data["name"]}',
            'description': f'Клиент: {client_data["name"]}\nТелефон: {client_data["phone"]}\nУслуга: {client_data.get("service", "Не указана")}\nВозраст ребёнка: {client_data.get("child_age", "—")}',
            'start': {
                'dateTime': slot_time.isoformat(),
                'timeZone': 'Europe/Moscow',
            },
            'end': {
                'dateTime': (slot_time + timedelta(hours=1)).isoformat(),
                'timeZone': 'Europe/Moscow',
            },
        }
        
        event = service.events().insert(calendarId=CALENDAR_ID, body=event).execute()
        print(f"[УСПЕХ] Создано событие в календаре: {event['id']}")
        return event['id']
    except Exception as e:
        print(f"[ОШИБКА] Не удалось создать событие в календаре: {e}")
        return None