import os
from dotenv import load_dotenv
from prices_updater import update_prices

# Загрузим .env до импорта остальных модулей, чтобы переменные были доступны при их инициализации
load_dotenv()

from telegram.ext import Application
from reminder import scheduler
from handlers import setup_handlers
from db import init_db

TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')

if not TELEGRAM_TOKEN:
    raise ValueError('TELEGRAM_TOKEN не найден в переменных окружения')

async def on_startup(app):
    print('Бот запущен и готов к работе!')
    # Убедимся, что планировщик работает
    if not scheduler.running:
        scheduler.start()
    # Обновим прайс при запуске (быстро и кэшируемо)
    try:
        update_prices()
        print('Прайс обновлён при старте.')
    except Exception as e:
        print(f'[ПРЕДУПРЕЖДЕНИЕ] Не удалось обновить прайс при старте: {e}')

def main():
    init_db()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    setup_handlers(app)
    app.post_init = on_startup
    app.run_polling()

if __name__ == '__main__':
    main()