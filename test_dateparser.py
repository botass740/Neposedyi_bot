import dateparser
import datetime
import re
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Europe/Moscow')

test_phrases = [
    "в ближайшую среду",
    "ближайшая среда",
    "следующая среда",
    "в среду",
    "среда",
    "завтра",
    "послезавтра",
    "сегодня"
]

print("Тестируем обновленную логику обработки фраз:")
print("=" * 50)

# Логика из handlers.py
relative_day_patterns = {
    r'в ближайшую (понедельник|вторник|среда|четверг|пятница|суббота|воскресенье)': 'ближайший',
    r'ближайшая (понедельник|вторник|среда|четверг|пятница|суббота|воскресенье)': 'ближайший',
    r'следующая (понедельник|вторник|среда|четверг|пятница|суббота|воскресенье)': 'следующий',
    r'в следующую (понедельник|вторник|среда|четверг|пятница|суббота|воскресенье)': 'следующий'
}

day_names = {
    'понедельник': 0, 'вторник': 1, 'среда': 2, 'четверг': 3,
    'пятница': 4, 'суббота': 5, 'воскресенье': 6
}

for phrase in test_phrases:
    print(f"\nОбрабатываем: '{phrase}'")
    print(f"  Длина строки: {len(phrase)}")
    print(f"  ASCII коды: {[ord(c) for c in phrase]}")
    processed_text = phrase
    pattern_found = False
    
    for pattern, prefix in relative_day_patterns.items():
        print(f"  Проверяем паттерн: {pattern}")
        match = re.search(pattern, phrase)
        if match:
            pattern_found = True
            day_name = match.group(1)
            day_offset = day_names[day_name]
            
            # Вычисляем дату следующего указанного дня недели
            today = datetime.datetime.now(tz=TZ)
            days_ahead = day_offset - today.weekday()
            if days_ahead <= 0:  # Если день уже прошел на этой неделе
                days_ahead += 7
            
            target_date = today + datetime.timedelta(days=days_ahead)
            
            # Заменяем фразу на конкретную дату для dateparser
            processed_text = re.sub(pattern, target_date.strftime('%d.%m.%Y'), phrase)
            print(f"[ОТЛАДКА] Заменено '{match.group(0)}' на '{target_date.strftime('%d.%m.%Y')}'")
            print(f"[ОТЛАДКА] Обработанный текст: '{processed_text}'")
            break
        else:
            print(f"  Паттерн не совпал")
    
    if not pattern_found:
        print(f"[ОТЛАДКА] Паттерн не найден, используем оригинальный текст: '{processed_text}'")

    parsed = dateparser.parse(
        processed_text,
        languages=['ru'],
        settings={'PREFER_DATES_FROM': 'future', 'RELATIVE_BASE': datetime.datetime.now(tz=TZ)}
    )
    if parsed:
        print(f"'{phrase}' -> {parsed.strftime('%A %d.%m.%Y %H:%M')}")
    else:
        print(f"'{phrase}' -> НЕ РАСПОЗНАНО")

print("\nТекущее время:", datetime.datetime.now(tz=TZ).strftime('%A %d.%m.%Y %H:%M'))
