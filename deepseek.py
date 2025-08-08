import os
import json
from openai import OpenAI

# Фиксированные цены (заглушка)
def get_prices_text():
    return (
        "– Детская стрижка: от 800₽\n"
        "– Взрослая стрижка: от 1200₽\n"
        "– Укладка: от 1000₽\n"
        "– Плетение: от 900₽\n"
        "– Окрашивание: зависит от длины и сложности, уточняется при записи"
    )

def get_system_prompt():
    try:
        with open("system_prompt.txt", "r", encoding="utf-8") as f:
            template = f.read()
        return template.format(prices=get_prices_text())
    except Exception as e:
        print("[ОШИБКА] Не удалось прочитать system_prompt.txt:", e)
        return None

# Инициализация клиента OpenAI для OpenRouter
def get_openrouter_client():
    api_key = os.getenv('DEEPSEEK_API_KEY')
    if not api_key:
        raise ValueError('DEEPSEEK_API_KEY не найден в переменных окружения')
    return OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=api_key,
    )

def ask_deepseek(prompt, history=None):
    client = get_openrouter_client()
    system_message = get_system_prompt()
    if not system_message:
        return "Извините, внутренний файл system_prompt.txt не найден. Обратитесь к администратору."
    messages = history or []
    if not messages or messages[0].get("role") != "system":
        messages = [{"role": "system", "content": system_message}] + messages
    messages.append({"role": "user", "content": prompt})
    try:
        completion = client.chat.completions.create(
            model="deepseek/deepseek-chat-v3-0324:free",  # возвращаем DeepSeek
            messages=messages,
            temperature=0.7,
            timeout=8,  # короткий timeout для быстрых ответов
            max_tokens=150,  # ограничиваем длину ответа
            extra_headers={
                "HTTP-Referer": "https://t.me/Neposedyi_bot",
                "X-Title": "NeposediBot"
            },
            extra_body={}
        )
        return completion.choices[0].message.content
    except Exception as e:
        print("[ОШИБКА] При обращении к OpenRouter через openai SDK:", e)
        print("[DEBUG] Данные запроса:", messages)
        return "Извините, сейчас не могу ответить. Попробуйте позже."