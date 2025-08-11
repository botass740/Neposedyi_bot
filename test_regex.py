import re

test_phrase = "в ближайшую среду"
pattern = r'в\s+ближайшую\s+(понедельник|вторник|среда|четверг|пятница|суббота|воскресенье)'

print(f"Тестируем: '{test_phrase}'")
print(f"Паттерн: {pattern}")
print(f"Длина строки: {len(test_phrase)}")
print(f"Символы: {[c for c in test_phrase]}")
print(f"ASCII коды: {[ord(c) for c in test_phrase]}")

# Проверим каждый символ отдельно
for i, c in enumerate(test_phrase):
    print(f"  {i}: '{c}' (ASCII: {ord(c)})")

# Простые тесты
print(f"\nПростые тесты:")
print(f"'в' in test_phrase: {'в' in test_phrase}")
print(f"'ближайшую' in test_phrase: {'ближайшую' in test_phrase}")
print(f"'среда' in test_phrase: {'среда' in test_phrase}")

# Тест с простым паттерном
simple_pattern = r'в.*среда'
match_simple = re.search(simple_pattern, test_phrase)
if match_simple:
    print(f"СОВПАДЕНИЕ с простым паттерном: {match_simple.group(0)}")
else:
    print("НЕТ СОВПАДЕНИЯ с простым паттерном")

match = re.search(pattern, test_phrase)
if match:
    print(f"СОВПАДЕНИЕ! Группа: {match.group(1)}")
else:
    print("НЕТ СОВПАДЕНИЯ")

# Попробуем другой подход - без \s
pattern2 = r'в ближайшую (понедельник|вторник|среда|четверг|пятница|суббота|воскресенье)'
match2 = re.search(pattern2, test_phrase)
if match2:
    print(f"СОВПАДЕНИЕ с упрощенным паттерном! Группа: {match2.group(1)}")
else:
    print("НЕТ СОВПАДЕНИЯ с упрощенным паттерном")
