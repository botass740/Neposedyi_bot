import os
import logging
from gigachat import GigaChat
from gigachat.models import Chat, Messages, MessagesRole

logger = logging.getLogger(__name__)

def get_gigachat_client():
    """Создаёт клиент GigaChat"""
    credentials = os.getenv('GIGACHAT_API_KEY')
    if not credentials:
        raise ValueError('GIGACHAT_API_KEY не найден в переменных окружения')
    
    return GigaChat(
        credentials=credentials,
        verify_ssl_certs=False,  # Для работы в России
        scope="GIGACHAT_API_PERS"  # Персональный доступ
    )

def ask_gigachat(messages):
    """
    Отправляет запрос в GigaChat
    
    Args:
        messages: список сообщений в формате [{"role": "system|user|assistant", "content": "текст"}]
    
    Returns:
        str: ответ от GigaChat
    """
    try:
        logger.info(f"[GIGACHAT] Отправляем запрос, сообщений: {len(messages)}")
        
        # Конвертируем формат сообщений
        giga_messages = []
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            
            # Преобразуем роли в формат GigaChat
            if role == "system":
                giga_role = MessagesRole.SYSTEM
            elif role == "assistant":
                giga_role = MessagesRole.ASSISTANT
            else:
                giga_role = MessagesRole.USER
            
            giga_messages.append(
                Messages(role=giga_role, content=content)
            )
        
        # Создаём клиент и отправляем запрос
        with get_gigachat_client() as giga:
            response = giga.chat(Chat(
                messages=giga_messages,
                temperature=0.8,
                max_tokens=300
            ))
            
            answer = response.choices[0].message.content
            logger.info(f"[GIGACHAT] Ответ получен: {answer[:100]}...")
            
            return answer
            
    except Exception as e:
        logger.error(f"[GIGACHAT] Ошибка: {e}")
        return None

