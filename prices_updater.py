import requests
from bs4 import BeautifulSoup
import json

PRICES_FILE = "prices.json"
SITE_URL = "https://neposedy-antichnyj-prospekt.clients.site/"

def fetch_prices():
    return {
        "Женская стрижка": "800₽",
        "Мужская стрижка": "800₽",
        "Детская стрижка": "800₽"
    }

def update_prices():
    prices = fetch_prices()
    if prices:
        with open(PRICES_FILE, "w", encoding="utf-8") as f:
            json.dump(prices, f, ensure_ascii=False, indent=2)
    return prices

def main():
    prices = update_prices()
    if prices:
        print(f"Обновлено {len(prices)} цен.")
    else:
        print("Цены не найдены или не удалось распарсить сайт.")

if __name__ == "__main__":
    main()