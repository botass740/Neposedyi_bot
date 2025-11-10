# Конфигурация мастеров и их календарей

MASTERS = {
    "master_1": {
        "id": "master_1",
        "name": "Мастер 1",
        "emoji": "👩‍🦰",
        "calendar_id": "942b4c9bbb4fec7feb831fe66046303e401bed84accceba81990946412aca5c7@group.calendar.google.com"
    },
    "master_2": {
        "id": "master_2",
        "name": "Мастер 2",
        "emoji": "👱‍♀️",
        "calendar_id": "52b48e66b828f30bbb2cfb6123c5bffb644ea8a3d554d3e2349dcc84d2514bd6@group.calendar.google.com"
    },
    "master_3": {
        "id": "master_3",
        "name": "Мастер 3",
        "emoji": "👩‍🦳",
        "calendar_id": "252e7116af118ef30e6d49d1556a406530689dd47ae2d69ce44c6ae3badbbb3a@group.calendar.google.com"
    },
    "master_4": {
        "id": "master_4",
        "name": "Мастер 4",
        "emoji": "👩",
        "calendar_id": "2f58edccf50365926eb33635be9a4c2256629ff36725ed60518e99adc79a7f5f@group.calendar.google.com"
    }
}

def get_master_by_id(master_id: str):
    """Получить данные мастера по ID"""
    return MASTERS.get(master_id)

def get_all_masters():
    """Получить список всех мастеров"""
    return MASTERS

def get_master_calendar_id(master_id: str):
    """Получить Calendar ID мастера"""
    master = MASTERS.get(master_id)
    return master.get("calendar_id") if master else None

def get_master_name(master_id: str):
    """Получить имя мастера"""
    master = MASTERS.get(master_id)
    return master.get("name") if master else None

def get_master_by_name(name: str):
    """Найти мастера по имени (например, 'Мастер 1')"""
    name_lower = name.lower()
    for master_key, master_data in MASTERS.items():
        if master_data['name'].lower() == name_lower:
            return master_data
    return None
