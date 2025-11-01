"""
Конфигурация акций и специальных предложений салона.
Акции упоминаются ботом автоматически, когда условия совпадают.
"""

from datetime import datetime, time
from typing import Dict, Any, List, Optional

# Список всех активных акций
PROMOTIONS = [
    {
        "id": 1,
        "name": "Детская стрижка по воскресеньям",
        "active": True,
        "conditions": {
            "weekday": [6],  # 0=понедельник, 6=воскресенье
            "service": ["Стрижка"]
            # Убрали child_age_required - срабатывает для всех стрижек в воскресенье
        },
        "discount": "10%",
        "message": "Кстати, в воскресенье действует скидка 10% на стрижки! 🎉"
    },
    {
        "id": 2,
        "name": "Утренняя скидка",
        "active": True,
        "conditions": {
            "time_range": ["09:00", "12:00"],  # с 9:00 до 12:00
            "service": ["Стрижка", "Укладка", "Окрашивание", "Плетение"]  # на все услуги
        },
        "discount": "10%",
        "message": "Отлично! В утренние часы (до 12:00) действует скидка 10% 🌅"
    },
    {
        "id": 3,
        "name": "Понедельник — день скидок",
        "active": True,
        "conditions": {
            "weekday": [0],  # понедельник
            "service": ["Стрижка"]
        },
        "discount": "15%",
        "message": "Отличный выбор! По понедельникам скидка 15% на взрослые стрижки 💇‍♀️"
    },
    {
        "id": 4,
        "name": "Вечерняя скидка",
        "active": True,
        "conditions": {
            "time_range": ["18:00", "20:00"],  # с 18:00 до 20:00
            "service": ["Стрижка", "Укладка"]
        },
        "discount": "5%",
        "message": "К слову, в вечерние часы (после 18:00) действует скидка 5% ✨"
    },
    {
        "id": 5,
        "name": "Укладка + окрашивание",
        "active": True,
        "conditions": {
            "service_combo": ["Укладка", "Окрашивание"]  # если обе услуги
        },
        "discount": "10%",
        "message": "Кстати, при комбинации укладки и окрашивания скидка 10% 💅"
    }
]


def check_promotion(
    service: Optional[str],
    visit_time: Optional[datetime],
    child_age: Optional[str],
    context_data: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """
    Проверяет, подходит ли какая-то акция для текущих условий.
    
    Args:
        service: Услуга (Стрижка, Укладка, и т.д.)
        visit_time: Дата и время визита
        child_age: Возраст ребёнка (если указан)
        context_data: Дополнительные данные из контекста
    
    Returns:
        Словарь с информацией об акции или None
    """
    if not service or not visit_time:
        return None
    
    for promo in PROMOTIONS:
        if not promo.get("active", True):
            continue
        
        conditions = promo.get("conditions", {})
        
        # Проверка дня недели
        if "weekday" in conditions:
            weekday = visit_time.weekday()
            if weekday not in conditions["weekday"]:
                continue
        
        # Проверка времени
        if "time_range" in conditions:
            time_start = datetime.strptime(conditions["time_range"][0], "%H:%M").time()
            time_end = datetime.strptime(conditions["time_range"][1], "%H:%M").time()
            visit_time_only = visit_time.time()
            
            if not (time_start <= visit_time_only < time_end):
                continue
        
        # Проверка услуги
        if "service" in conditions:
            if service not in conditions["service"]:
                continue
        
        # Проверка возраста ребёнка
        if conditions.get("child_age_required"):
            if not child_age or child_age == "—":
                continue
        
        # Проверка комбинации услуг (пока не реализовано, для будущего)
        if "service_combo" in conditions:
            # Это для сложных случаев, когда нужны две услуги
            # Пока пропускаем
            continue
        
        # Если все условия выполнены, возвращаем акцию
        return {
            "id": promo["id"],
            "name": promo["name"],
            "discount": promo["discount"],
            "message": promo["message"]
        }
    
    return None


def get_all_active_promotions() -> List[Dict[str, Any]]:
    """Возвращает список всех активных акций"""
    return [p for p in PROMOTIONS if p.get("active", True)]


def get_promotion_by_id(promo_id: int) -> Optional[Dict[str, Any]]:
    """Получить акцию по ID"""
    for promo in PROMOTIONS:
        if promo["id"] == promo_id:
            return promo
    return None

