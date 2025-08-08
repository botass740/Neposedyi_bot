import os
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

ADMIN_CHAT_ID = int(os.getenv('ADMIN_CHAT_ID', '0'))

def notify_admin(application, booking_data):
    """
    Отправляет админу заявку с кнопкой "Ответить".
    booking_data — словарь с данными клиента.
    """
    if not ADMIN_CHAT_ID:
        return
    text = (
        f"Новая заявка:\n"
        f"Имя: {booking_data.get('name')}\n"
        f"Телефон: {booking_data.get('phone')}\n"
        f"Возраст ребёнка: {booking_data.get('child_age', '—')}\n"
        f"Пожелания: {booking_data.get('wishes', '—')}\n"
        f"Дата и время: {booking_data.get('datetime', '—')}"
    )
    keyboard = [[InlineKeyboardButton("Ответить", callback_data=f"reply_{booking_data.get('user_id')}")]]
    application.bot.send_message(chat_id=ADMIN_CHAT_ID, text=text, reply_markup=InlineKeyboardMarkup(keyboard))