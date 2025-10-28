import os
import json
import logging
from openai import OpenAI
import requests

logger = logging.getLogger(__name__)

USE_LOCAL_LLM = os.getenv('USE_LOCAL_LLM', 'false').lower() == 'true'
LOCAL_LLM_URL = os.getenv('LOCAL_LLM_URL', 'http://localhost:11434')
LOCAL_LLM_MODEL = os.getenv('LOCAL_LLM_MODEL', 'deepseek-r1:1.5b')

USE_GIGACHAT = os.getenv('USE_GIGACHAT', 'false').lower() == 'true'

# Фиксированные цены (заглушка)
def get_prices_text():
    return (
        "– Детская стрижка: от 800₽\n"
        "– Взрослая стрижка: от 800₽\n"
        "– Укладка\n"
        "– Плетение\n"
        "– Окрашивание: зависит от длины и сложности, уточняется при записи"
    )

def get_services_block():
    return (
        'УСЛУГИ: стрижки детские, взрослые, мужские, женские, укладка, покраска волос\n'
        'ЦЕНЫ: стрижки от 800 руб.\n'
        'АКЦИИ: Скидка на детские стрижки и стрижки для пенсионеров с 10.00 до 13.00 20%.\n'
    )

def get_system_prompt():
    try:
        with open("system_prompt.txt", "r", encoding="utf-8") as f:
            template = f.read()
        return template.format(prices=get_prices_text(), services_block=get_services_block())
    except Exception as e:
        logger.error(f"[ОШИБКА] Не удалось прочитать system_prompt.txt: {e}")
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
    system_message = get_system_prompt()
    if not system_message:
        return "Извините, внутренний файл system_prompt.txt не найден. Обратитесь к администратору."
    
    # Формируем историю сообщений
    messages = []
    
    # Добавляем системный промпт ПЕРВЫМ
    messages.append({"role": "system", "content": system_message})
    
    # Добавляем историю (если есть), но БЕЗ системного промпта из истории
    if history:
        for msg in history:
            if msg.get("role") != "system":
                messages.append(msg)
    
    # НЕ добавляем prompt снова, т.к. он уже в history
    # messages.append({"role": "user", "content": prompt})  # <-- УБРАЛИ ЭТУ СТРОКУ
    
    try:
        # Проверяем, используем ли GigaChat
        if USE_GIGACHAT:
            logger.info("[GIGACHAT] Используем GigaChat")
            from gigachat_llm import ask_gigachat
            response = ask_gigachat(messages)
            if response:
                # Проверка безопасности
                if validate_response(response):
                    return response
                else:
                    logger.warning(f"[БЕЗОПАСНОСТЬ] Ответ GigaChat содержит подозрительные данные")
                    return "Извините, я не уверена в точности информации. Давайте я уточню у администратора."
            else:
                return "Извините, сейчас не могу ответить. Попробуйте позже."
        
        # Проверяем, используем ли локальный LLM
        if USE_LOCAL_LLM:
            logger.info(f"[LOCAL LLM] Используем локальную модель {LOCAL_LLM_MODEL}")
            return ask_local_llm(messages)
        
        # Иначе используем OpenRouter
        client = get_openrouter_client()
        model = os.getenv('OPENROUTER_MODEL', 'deepseek/deepseek-chat-v3-0324:free')
        
        completion = client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=0.8,  # Повышаем для более "живых" ответов
            timeout=15,  # Увеличиваем timeout
            max_tokens=300,  # Увеличиваем лимит для более полных ответов
            extra_headers={
                "HTTP-Referer": "https://t.me/Neposedyi_bot",
                "X-Title": "NeposediBot"
            },
            extra_body={}
        )
        response = completion.choices[0].message.content
        logger.info(f"[LLM] Запрос: {prompt[:100]}... | Ответ: {response[:100]}...")
        
        # Проверка безопасности: фильтруем подозрительные фразы
        if validate_response(response):
            return response
        else:
            logger.warning(f"[БЕЗОПАСНОСТЬ] Ответ LLM содержит подозрительные данные: {response}")
            return "Извините, я не уверена в точности информации. Давайте я уточню у администратора."
    except Exception as e:
        logger.error(f"[ОШИБКА] При обращении к LLM: {e}")
        logger.error(f"[DEBUG] Количество сообщений: {len(messages)}")
        return "Извините, сейчас не могу ответить. Попробуйте позже."

def ask_local_llm(messages):
    """Запрос к локальной модели через Ollama"""
    try:
        # Формируем промпт из истории сообщений
        full_prompt = ""
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role == "system":
                full_prompt += f"Системное сообщение: {content}\n\n"
            elif role == "user":
                full_prompt += f"Пользователь: {content}\n\n"
            elif role == "assistant":
                full_prompt += f"Ассистент: {content}\n\n"
        
        full_prompt += "Ассистент:"
        
        # Отправляем запрос к Ollama
        response = requests.post(
            f"{LOCAL_LLM_URL}/api/generate",
            json={
                "model": LOCAL_LLM_MODEL,
                "prompt": full_prompt,
                "stream": False,
                "options": {
                    "temperature": 0.8,
                    "num_predict": 300
                }
            },
            timeout=30
        )
        
        if response.status_code == 200:
            result = response.json()
            answer = result.get("response", "").strip()
            logger.info(f"[LOCAL LLM] Ответ получен: {answer[:100]}...")
            
            # Проверка безопасности
            if validate_response(answer):
                return answer
            else:
                logger.warning(f"[БЕЗОПАСНОСТЬ] Ответ локального LLM содержит подозрительные данные")
                return "Извините, я не уверена в точности информации. Давайте я уточню у администратора."
        else:
            logger.error(f"[LOCAL LLM] Ошибка: {response.status_code} - {response.text}")
            return "Извините, сейчас не могу ответить. Попробуйте позже."
            
    except requests.exceptions.ConnectionError:
        logger.error("[LOCAL LLM] Не удалось подключиться к Ollama. Убедитесь, что Ollama запущена.")
        return "Извините, сейчас не могу ответить. Попробуйте позже."
    except Exception as e:
        logger.error(f"[LOCAL LLM] Ошибка: {e}")
        return "Извините, сейчас не могу ответить. Попробуйте позже."

def validate_response(response: str) -> bool:
    """Проверяет ответ LLM на наличие подозрительных данных"""
    response_lower = response.lower()
    
    # Проверяем на чрезмерные скидки (больше 20%)
    suspicious_patterns = [
        r'скидк[аи].*?(\d{2,3})%',  # скидка 50%, 90% и т.д.
        r'(\d{2,3})%.*?скидк',
        'бесплатн',  # бесплатно (если это не часть акции)
        'даром',
        'в подарок',
    ]
    
    import re
    for pattern in suspicious_patterns:
        matches = re.findall(pattern, response_lower)
        for match in matches:
            if isinstance(match, str) and match.isdigit():
                discount = int(match)
                if discount > 20:
                    logger.warning(f"[БЕЗОПАСНОСТЬ] Обнаружена подозрительная скидка: {discount}%")
                    return False
            elif isinstance(match, tuple) and len(match) > 0 and match[0].isdigit():
                discount = int(match[0])
                if discount > 20:
                    logger.warning(f"[БЕЗОПАСНОСТЬ] Обнаружена подозрительная скидка: {discount}%")
                    return False
    
    # Проверяем на несуществующие услуги (не из нашего списка)
    known_services = ['стриж', 'уклад', 'окраш', 'плетен', 'покрас', 'колор']
    suspicious_services = ['мануал', 'педикюр', 'маникюр', 'массаж', 'тату', 'пирсинг', 'косметолог']
    
    for service in suspicious_services:
        if service in response_lower:
            logger.warning(f"[БЕЗОПАСНОСТЬ] Обнаружена несуществующая услуга: {service}")
            return False
    
    # Всё ок
    return True