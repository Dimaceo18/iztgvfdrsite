import os
import requests
import logging
import re
import time
import json
from flask import Flask, request, jsonify
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Bot
from telegram.ext import Application, MessageHandler, filters, CallbackQueryHandler
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Конфигурация
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
WP_URL = os.getenv('WP_URL')
WP_USERNAME = os.getenv('WP_USERNAME')
WP_PASSWORD = os.getenv('WP_PASSWORD')
ADMIN_ID = os.getenv('YOUR_TELEGRAM_ID')

# WordPress API
WP_API_URL = f"{WP_URL}/wp-json/wp/v2"
WP_MEDIA_URL = f"{WP_URL}/wp-json/wp/v2/media"

app = Flask(__name__)
wp_session = requests.Session()
wp_session.headers.update({'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'})

# Создаём бота синхронно
bot = Bot(token=TELEGRAM_TOKEN)

# Хранилище временных постов
pending_posts = {}

def extract_title_and_content(text):
    """Извлечение заголовка из текста (первая строка)"""
    if not text:
        return "Новый пост", ""
    lines = text.strip().split('\n')
    title = lines[0].strip() if lines else "Новый пост"
    if len(title) > 180:
        title = title[:177] + "..."
    content = '\n'.join(lines[1:]).strip() if len(lines) > 1 else ""
    return title, content

def format_content_for_wp(text):
    """Форматирование контента для WordPress с HTML тегами"""
    if not text:
        return ""
    paragraphs = text.split('\n')
    formatted = []
    for para in paragraphs:
        para = para.strip()
        if para:
            # Конвертируем ссылки
            para = re.sub(r'(https?://[^\s]+)', r'<a href="\1">\1</a>', para)
            # Конвертируем **жирный**
            para = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', para)
            # Конвертируем *курсив*
            para = re.sub(r'\*(.+?)\*', r'<em>\1</em>', para)
            formatted.append(f'<p>{para}</p>')
    return '\n'.join(formatted)

def download_and_upload_photo(file_id):
    """Загрузка фото в WordPress (синхронно)"""
    try:
        # Получаем путь к файлу
        get_file = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile",
            params={'file_id': file_id},
            timeout=30
        )
        if get_file.status_code != 200:
            logger.error(f"Ошибка getFile: {get_file.status_code}")
            return None
        
        file_path = get_file.json().get('result', {}).get('file_path')
        if not file_path:
            logger.error("Не получен file_path")
            return None
        
        # Скачиваем фото
        photo_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"
        logger.info(f"Скачивание фото...")
        photo_data = requests.get(photo_url, timeout=60)
        if photo_data.status_code != 200:
            logger.error(f"Ошибка скачивания: {photo_data.status_code}")
            return None
        
        # Загружаем в WordPress
        wp_response = wp_session.post(
            WP_MEDIA_URL,
            auth=(WP_USERNAME, WP_PASSWORD),
            headers={'Content-Disposition': f'attachment; filename="photo_{int(time.time())}.jpg"'},
            data=photo_data.content,
            timeout=60
        )
        
        if wp_response.status_code == 201:
            media_id = wp_response.json()['id']
            logger.info(f"✅ Фото загружено, ID: {media_id}")
            return media_id
        else:
            logger.error(f"Ошибка WP: {wp_response.status_code}")
            return None
            
    except Exception as e:
        logger.error(f"Ошибка фото: {e}")
        return None

def create_wp_post(title, content, media_id=None, status='draft'):
    """Создание поста в WordPress (синхронно)"""
    post_data = {
        'title': title,
        'content': content,
        'status': status,
        'type': 'news',
    }
    if media_id:
        post_data['featured_media'] = media_id
    
    try:
        logger.info(f"📤 Отправка в WordPress (статус: {status})...")
        response = wp_session.post(
            f"{WP_API_URL}/news",
            auth=(WP_USERNAME, WP_PASSWORD),
            json=post_data,
            timeout=60
        )
        
        if response.status_code == 201:
            link = response.json()['link']
            logger.info(f"✅ Пост создан: {link}")
            return True, link
        else:
            logger.error(f"Ошибка {response.status_code}: {response.text[:200]}")
            return False, None
    except Exception as e:
        logger.error(f"Ошибка: {e}")
        return False, None

def handle_message(update: Update, context):
    """Обработка сообщений от админа (синхронно)"""
    try:
        message = update.effective_message
        if not message or str(message.from_user.id) != ADMIN_ID:
            return
        
        logger.info(f"📨 Получено сообщение от админа")
        
        # Получаем текст и фото
        text = message.caption or message.text or ""
        if not text and not message.photo:
            bot.send_message(chat_id=ADMIN_ID, text="❌ Отправьте текст новости (можно с фото).\n\nПервая строка будет заголовком.")
            return
        
        # Извлекаем заголовок и контент
        title, content_text = extract_title_and_content(text)
        logger.info(f"📌 Заголовок: {title[:50]}...")
        
        # Обработка фото
        media_id = None
        if message.photo:
            photo = message.photo[-1]
            bot.send_message(chat_id=ADMIN_ID, text="⏳ Загружаю фото...")
            media_id = download_and_upload_photo(photo.file_id)
            if media_id:
                logger.info(f"✅ Фото загружено, ID: {media_id}")
            else:
                bot.send_message(chat_id=ADMIN_ID, text="⚠️ Фото не загрузилось, продолжу без фото.")
        
        # Форматируем контент
        formatted_content = format_content_for_wp(content_text)
        
        # Сохраняем во временное хранилище
        post_key = str(int(time.time() * 1000))
        pending_posts[post_key] = {
            'title': title,
            'content': formatted_content,
            'media_id': media_id
        }
        
        # Создаем кнопки выбора
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📝 В Черновики", callback_data=f"draft_{post_key}"),
                InlineKeyboardButton("🚀 Опубликовать", callback_data=f"publish_{post_key}")
            ]
        ])
        
        # Отправляем сообщение с кнопками
        msg = f"📢 Новая новость!\n\n"
        msg += f"Заголовок: {title[:100]}\n"
        msg += f"Фото: {'есть' if media_id else 'нет'}\n\n"
        msg += f"Выбери действие:"
        
        bot.send_message(
            chat_id=ADMIN_ID,
            text=msg,
            reply_markup=keyboard
        )
        logger.info(f"✉️ Отправлен запрос на публикацию")
        
    except Exception as e:
        logger.error(f"❌ Ошибка: {e}")

def handle_button(update: Update, context):
    """Обработка нажатия на кнопку (синхронно)"""
    query = update.callback_query
    query.answer()
    
    action, post_key = query.data.split('_')
    post_data = pending_posts.get(post_key)
    
    if not post_data:
        query.edit_message_text("❌ Пост не найден.")
        return
    
    if action == 'draft':
        status = 'draft'
        status_text = "сохранен в Черновики"
        result_text = "📝 Пост сохранен в Черновики"
    else:
        status = 'publish'
        status_text = "опубликован"
        result_text = "🚀 Пост опубликован на сайте"
    
    query.edit_message_text(f"⏳ {status_text}...")
    
    success, link = create_wp_post(
        post_data['title'],
        post_data['content'],
        post_data['media_id'],
        status
    )
    
    if success:
        query.edit_message_text(
            f"✅ Готово!\n\n{result_text}\n\nСсылка: {link}"
        )
        logger.info(f"✅ Пост {status_text}: {post_data['title'][:50]}")
    else:
        query.edit_message_text(
            f"❌ Ошибка!\n\nНе удалось {status_text} пост.\n\n💡 Проверьте подключение к WordPress."
        )
        logger.error(f"❌ Ошибка при создании поста")
    
    # Удаляем пост из хранилища
    del pending_posts[post_key]

# Создаем приложение
application = Application.builder().token(TELEGRAM_TOKEN).build()

# Добавляем обработчики (синхронные)
application.add_handler(MessageHandler(
    filters.TEXT | filters.PHOTO | filters.CAPTION,
    handle_message
))
application.add_handler(CallbackQueryHandler(handle_button))

logger.info("✅ Обработчики добавлены")

@app.route('/webhook', methods=['POST'])
def webhook():
    """Обработка вебхука (синхронно)"""
    try:
        json_data = request.get_json(force=True)
        logger.info("🔔 Вебхук получен")
        update = Update.de_json(json_data, application.bot)
        application.process_update(update)  # Синхронный вызов!
        return jsonify({'status': 'ok'})
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return jsonify({'status': 'error'}), 500

@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'healthy'})

@app.route('/', methods=['GET'])
def index():
    return jsonify({'status': 'Bot is running', 'mode': 'private messages only'})

if __name__ == '__main__':
    render_url = os.getenv('RENDER_EXTERNAL_URL')
    webhook_url = f"{render_url}/webhook"
    
    logger.info(f"🚀 ЗАПУСК БОТА...")
    logger.info(f"🔗 Вебхук: {webhook_url}")
    logger.info(f"🌐 WordPress: {WP_URL}")
    logger.info(f"📝 Тип записей: news")
    logger.info(f"👤 ID админа: {ADMIN_ID}")
    
    # Установка вебхука (синхронно)
    bot.delete_webhook()
    bot.set_webhook(url=webhook_url)
    logger.info("✅ Вебхук установлен")
    
    port = int(os.getenv('PORT', 8000))
    app.run(host='0.0.0.0', port=port)
