import json
import os
from typing import Any, Dict, Optional

STATE_FILE = os.path.join(os.path.dirname(__file__), 'user_state.json')

def _read_all() -> Dict[str, Dict[str, Any]]:
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}

def _write_all(data: Dict[str, Dict[str, Any]]) -> None:
    tmp = STATE_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)

def get_user_state(chat_id: int) -> Optional[Dict[str, Any]]:
    all_data = _read_all()
    return all_data.get(str(chat_id))

def update_user_state(chat_id: int, state: Dict[str, Any]) -> None:
    all_data = _read_all()
    all_data[str(chat_id)] = state
    _write_all(all_data)


