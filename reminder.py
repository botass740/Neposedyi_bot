from apscheduler.schedulers.background import BackgroundScheduler
from datetime import timedelta
from prices_updater import update_prices

scheduler = BackgroundScheduler()
scheduler.add_job(update_prices, 'cron', hour=7, minute=0)  # Обновлять цены каждый день в 7:00
scheduler.start()

def schedule_reminders(application, chat_id, visit_time):
    """
    Планирует напоминания за 1 день и за 1 час до визита.
    visit_time — datetime.datetime
    """
    # За 1 день
    scheduler.add_job(
        lambda: application.bot.send_message(chat_id, "Напоминаем: завтра ждём вас в салоне 'Непоседы'!"),
        'date', run_date=visit_time - timedelta(days=1)
    )
    # За 1 час
    scheduler.add_job(
        lambda: application.bot.send_message(chat_id, "Через час ждём вас в салоне 'Непоседы'!"),
        'date', run_date=visit_time - timedelta(hours=1)
    )