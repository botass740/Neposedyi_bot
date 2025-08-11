import re

test_phrase = "в ближайшую среду"
pattern = r'в ближайшую (понедельник|вторник|среда|четверг|пятница|суббота|воскресенье)'

print(f"Тестируем: '{test_phrase}'")
print(f"Паттерн: {pattern}")

# Проверим каждое слово отдельно
words = test_phrase.split()
print(f"Слова: {words}")

# Проверим, содержит ли фраза нужные части
print(f"'в' in test_phrase: {'в' in test_phrase}")
print(f"'ближайшую' in test_phrase: {'ближайшую' in test_phrase}")
print(f"'среда' in test_phrase: {'среда' in test_phrase}")

match = re.search(pattern, test_phrase)
if match:
    print(f"СОВПАДЕНИЕ! Группа: {match.group(1)}")
else:
    print("НЕТ СОВПАДЕНИЯ")

# Попробуем более простой паттерн
simple_pattern = r'в ближайшую среда'
match2 = re.search(simple_pattern, test_phrase)
if match2:
    print(f"СОВПАДЕНИЕ с простым паттерном!")
else:
    print("НЕТ СОВПАДЕНИЯ с простым паттерном")
