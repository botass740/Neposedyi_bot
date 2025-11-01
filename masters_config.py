# -*- coding: utf-8 -*-
"""
Конфигурация мастеров салона "Непоседы"
"""

MASTERS = [
    {
        "id": 1,
        "name": "Мастер 1",
        "emoji": "👩‍🦰",
        "short_name": "М1",
        "specialization": "Универсальный мастер"
    },
    {
        "id": 2,
        "name": "Мастер 2",
        "emoji": "👱‍♀️",
        "short_name": "М2",
        "specialization": "Универсальный мастер"
    },
    {
        "id": 3,
        "name": "Мастер 3",
        "emoji": "👩‍🦳",
        "short_name": "М3",
        "specialization": "Универсальный мастер"
    },
    {
        "id": 4,
        "name": "Мастер 4",
        "emoji": "👩",
        "short_name": "М4",
        "specialization": "Универсальный мастер"
    }
]

# Вспомогательные функции
def get_master_by_id(master_id):
    """Получить мастера по ID"""
    for master in MASTERS:
        if master['id'] == master_id:
            return master
    return None

def get_master_by_name(name):
    """Получить мастера по имени (гибкий поиск)"""
    name_lower = name.lower()
    for master in MASTERS:
        if (master['name'].lower() in name_lower or 
            name_lower in master['name'].lower() or
            master['short_name'].lower() == name_lower):
            return master
    return None

def get_all_masters():
    """Получить список всех мастеров"""
    return MASTERS

def get_masters_text():
    """Получить текстовое представление списка мастеров для LLM"""
    masters_list = []
    for master in MASTERS:
        masters_list.append(f"{master['emoji']} {master['name']}")
    return ", ".join(masters_list)

