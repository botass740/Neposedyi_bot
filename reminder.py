from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from prices_updater import update_prices

scheduler = BackgroundScheduler(timezone='Europe/Moscow')
scheduler.add_job(update_prices, 'cron', hour=7, minute=0)  # Обновлять цены каждый день в 7:00
scheduler.start()

def schedule_reminders(application, chat_id, visit_time):
    """
    Планирует напоминания за 1 день и за 1 час до визита.
    visit_time — datetime.datetime
    """
    tz = ZoneInfo('Europe/Moscow')
    # Нормализуем visit_time к часовому поясу Москвы, если он naive
    if visit_time.tzinfo is None:
        visit_time = visit_time.replace(tzinfo=tz)
    now = datetime.now(tz=tz)

    # Напоминание за 1 день
    one_day_before = visit_time - timedelta(days=1)
    if one_day_before > now:
        scheduler.add_job(
            lambda: application.bot.send_message(
                chat_id, f"Напоминаем: завтра ждём вас в салоне 'Непоседы' в {visit_time.strftime('%H:%M')}!"
            ),
            'date', run_date=one_day_before
        )

    # Напоминание за 1 час
    one_hour_before = visit_time - timedelta(hours=1)
    if one_hour_before > now:
        scheduler.add_job(
            lambda: application.bot.send_message(
                chat_id, f"Через час ждём вас в салоне 'Непоседы' в {visit_time.strftime('%H:%M')}!"
            ),
            'date', run_date=one_hour_before
        )

def schedule_monthly_reminder(application, chat_id, visit_time):
    """
    Планирует напоминание через 1 месяц после визита.
    """
    tz = ZoneInfo('Europe/Moscow')
    if visit_time.tzinfo is None:
        visit_time = visit_time.replace(tzinfo=tz)
    month_later = visit_time + timedelta(days=30)
    now = datetime.now(tz=tz)
    if month_later > now:
        scheduler.add_job(
            lambda: application.bot.send_message(
                chat_id,
                "Прошел месяц с вашей последней стрижки! Может, пора освежить образ? Запишитесь в 'Непоседы' — всегда рады видеть вас снова 😊"
            ),
            'date', run_date=month_later
        )
