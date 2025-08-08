import requests
from bs4 import BeautifulSoup
import json

PRICES_FILE = "prices.json"
SITE_URL = "https://neposedy-antichnyj-prospekt.clients.site/"

def fetch_prices():
    response = requests.get(SITE_URL)
    soup = BeautifulSoup(response.text, "html.parser")
    prices = {}
    # Пример парсинга: ищем все блоки с услугами и ценами
    # !!! ВАЖНО: если структура сайта другая, адаптируйте селекторы ниже !!!
    for block in soup.find_all(['li', 'div']):
        text = block.get_text(strip=True)
        # Пример: ищем строки вида "Стрижка детская — 1000 ₽"
        if '₽' in text and ('стрижк' in text.lower() or 'укладк' in text.lower() or 'плетен' in text.lower() or 'окрашив' in text.lower()):
            parts = text.split('—')
            if len(parts) == 2:
                name = parts[0].strip()
                price = parts[1].strip()
                prices[name] = price
    return prices

def update_prices():
    prices = fetch_prices()
    if prices:
        with open(PRICES_FILE, "w", encoding="utf-8") as f:
            json.dump(prices, f, ensure_ascii=False, indent=2)
    return prices