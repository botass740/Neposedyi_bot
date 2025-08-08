import os
from telegram.ext import Application
from handlers import setup_handlers
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')

if not TELEGRAM_TOKEN:
    raise ValueError('TELEGRAM_TOKEN не найден в переменных окружения')

async def on_startup(app):
    print('Бот запущен и готов к работе!')

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    setup_handlers(app)
    app.post_init = on_startup
    app.run_polling()

if __name__ == '__main__':
    main()