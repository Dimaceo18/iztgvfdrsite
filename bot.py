import os
import requests
import logging
import re
import time
import asyncio
import json
from flask import Flask, request, jsonify
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, MessageHandler, filters, CallbackQueryHandler
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Конфигурация
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
CHANNEL_ID = os.getenv('CHANNEL_ID')
WP_URL = os.getenv('WP_URL')
WP_USERNAME = os.getenv('WP_USERNAME')
WP_PASSWORD = os.getenv('WP_PASSWORD')
ADMIN_ID = os.getenv('YOUR_TELEGRAM_ID')

# WordPress API
WP_API_URL = f"{WP_URL}/wp-json/wp/v2"
WP_MEDIA_URL = f"{WP_URL}/wp-json/wp/v2/media"

# Flask приложение
app = Flask(__name__)

# Создаём сессию для постоянного соединения с WordPress
wp_session = requests.Session()

# Хранилище временных постов
pending_posts = {}

def extract_title_and_content(text):
    """Извлечение заголовка из текста (первая строка)"""
    if not text:
        return "Новый пост из Telegram", ""
    
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
    """Загрузка фото из Telegram в WordPress"""
    try:
        get_file_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile"
        file_response = requests.get(get_file_url, params={'file_id': file_id}, timeout=30)
        
        if file_response.status_code != 200:
            logger.error(f"Ошибка getFile: {file_response.status_code}")
            return None
        
        result = file_response.json().get('result')
        if not result:
            logger.error("Не получен result от Telegram")
            return None
        
        file_path = result.get('file_path')
        if not file_path:
            logger.error("Не получен file_path")
            return None
        
        photo_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"
        logger.info(f"Скачивание фото...")
        
        photo_response = requests.get(photo_url, timeout=60)
        
        if photo_response.status_code != 200:
            logger.error(f"Ошибка скачивания фото: {photo_response.status_code}")
            return None
        
        wp_response = wp_session.post(
            WP_MEDIA_URL,
            auth=(WP_USERNAME, WP_PASSWORD),
            headers={'Content-Disposition': f'attachment; filename="photo_{int(time.time())}.jpg"'},
            data=photo_response.content,
            timeout=60
        )
        
        if wp_response.status_code == 201:
            media_id = wp_response.json()['id']
            logger.info(f"✅ Фото загружено, ID: {media_id}")
            return media_id
        else:
            logger.error(f"Ошибка WP при загрузке фото: {wp_response.status_code}")
            return None
            
    except Exception as e:
        logger.error(f"Ошибка фото: {e}")
        return None

def create_wp_draft(title, content, media_id=None):
    """Создание черновика в WordPress"""
    post_data = {
        'title': title,
        'content': content,
        'status': 'draft',
        'type': 'news',
    }
    
    if media_id:
        post_data['featured_media'] = media_id
    
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'application/json',
            'Content-Type': 'application/json',
        }
        
        logger.info(f"Отправка в WordPress...")
        
        response = wp_session.post(
            f"{WP_API_URL}/news",
            auth=(WP_USERNAME, WP_PASSWORD),
            json=post_data,
            headers=headers,
            timeout=60
        )
        
        if response.status_code == 201:
            logger.info(f"✅ Черновик создан")
            return True
        else:
            logger.error(f"Ошибка: {response.status_code}")
            return False
            
    except Exception as e:
        logger.error(f"Ошибка: {e}")
        return False

async def handle_private_message(update: Update, context):
    """Обработка сообщений из личного чата"""
    try:
        message = update.message
        if not message:
            logger.warning("Нет message в update")
            return
        
        logger.info("=" * 60)
        logger.info("🔍 ПОЛУЧЕНО СООБЩЕНИЕ В ЛИЧКУ")
        logger.info(f"From: {message.from_user.id} ({message.from_user.username})")
        logger.info(f"Text: {message.text if message.text else 'нет'}")
        logger.info(f"Caption: {message.caption if message.caption else 'нет'}")
        logger.info(f"Photo: {'есть' if message.photo else 'нет'}")
        logger.info(f"Forward: {'есть' if message.forward_from_chat or message.forward_from else 'нет'}")
        
        # Проверяем права
        if str(message.from_user.id) != ADMIN_ID:
            logger.warning(f"❌ Доступ запрещён: {message.from_user.id} != {ADMIN_ID}")
            await message.reply_text("❌ У вас нет прав для использования этого бота.")
            return
        
        # Получаем текст
        text = message.caption or message.text or ""
        
        if not text:
            await message.reply_text("❌ Отправьте текст новости.\nПервая строка будет заголовком.")
            return
        
        title, content_text = extract_title_and_content(text)
        logger.info(f"📌 Заголовок: {title[:50]}...")
        
        # Обработка фото
        media_id = None
        if message.photo:
            photo = message.photo[-1]
            await message.reply_text("⏳ Загружаю фото...")
            media_id = download_and_upload_photo(photo.file_id)
            if media_id:
                logger.info(f"📸 Фото загружено")
            else:
                await message.reply_text("⚠️ Фото не загрузилось, продолжу без фото.")
        
        formatted_content = format_content_for_wp(content_text)
        
        # Сохраняем во временное хранилище
        post_key = str(int(time.time() * 1000))
        pending_posts[post_key] = {
            'title': title,
            'content': formatted_content,
            'media_id': media_id
        }
        
        # Создаем кнопки
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📝 В Черновики", callback_data=f"draft_{post_key}"),
                InlineKeyboardButton("🚀 Опубликовать", callback_data=f"publish_{post_key}")
            ]
        ])
        
        preview = f"📢 <b>Предпросмотр новости</b>\n\n"
        preview += f"<b>Заголовок:</b> {title}\n\n"
        preview += f"<b>Текст:</b>\n{content_text[:300]}{'...' if len(content_text) > 300 else ''}\n\n"
        preview += f"<b>Фото:</b> {'✅ есть' if media_id else '❌ нет'}\n\n"
        preview += f"<i>Выбери действие:</i>"
        
        await message.reply_text(
            preview,
            parse_mode='HTML',
            reply_markup=keyboard
        )
        
        logger.info(f"✉️ Отправлены кнопки выбора")
        logger.info("=" * 60)
        
    except Exception as e:
        logger.error(f"❌ Ошибка: {e}")
        await message.reply_text(f"❌ Ошибка: {str(e)[:100]}")

async def handle_button(update: Update, context):
    """Обработка нажатия на кнопку"""
    query = update.callback_query
    await query.answer()
    
    logger.info(f"🔘 Нажата кнопка: {query.data}")
    
    action, post_key = query.data.split('_')
    
    post_data = pending_posts.get(post_key)
    if not post_data:
        await query.edit_message_text("❌ Пост не найден.")
        return
    
    if action == 'draft':
        status_text = "сохранен в черновиках"
        result_text = "📝 Пост сохранен в <b>Черновики</b>"
    else:
        status_text = "опубликован"
        result_text = "🚀 Пост <b>опубликован</b> на сайте"
    
    await query.edit_message_text(f"⏳ {status_text}...")
    
    success = create_wp_draft(
        post_data['title'],
        post_data['content'],
        post_data['media_id']
    )
    
    if success:
        await query.edit_message_text(
            f"✅ <b>Готово!</b>\n\n{result_text}\n\n"
            f"<b>Заголовок:</b> {post_data['title'][:100]}",
            parse_mode='HTML'
        )
        logger.info(f"✅ Пост {status_text}")
    else:
        await query.edit_message_text(
            f"❌ <b>Ошибка!</b>\n\nНе удалось {status_text} пост.\n\n"
            f"💡 Проверьте подключение к WordPress.",
            parse_mode='HTML'
        )
        logger.error(f"❌ Ошибка при создании поста")
    
    del pending_posts[post_key]

# Создаем приложение
application = Application.builder().token(TELEGRAM_TOKEN).build()

# Только обработчик личных сообщений (для теста)
application.add_handler(MessageHandler(
    filters.TEXT | filters.PHOTO | filters.CAPTION,
    handle_private_message
))
application.add_handler(CallbackQueryHandler(handle_button))

logger.info("✅ Обработчики добавлены")

wp_session.headers.update({'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'})

@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        json_data = request.get_json(force=True)
        logger.info("🔔 Вебхук получен")
        
        # Логируем тип update
        if 'message' in json_data:
            logger.info("📩 Тип: message")
        elif 'callback_query' in json_data:
            logger.info("🔘 Тип: callback_query")
        else:
            logger.info(f"📦 Другие типы: {list(json_data.keys())}")
        
        update = Update.de_json(json_data, application.bot)
        asyncio.run(application.process_update(update))
        return jsonify({'status': 'ok'})
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return jsonify({'status': 'error'}), 500

@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'healthy'})

@app.route('/', methods=['GET'])
def index():
    return jsonify({'status': 'Bot is running'})

if __name__ == '__main__':
    render_url = os.getenv('RENDER_EXTERNAL_URL')
    webhook_url = f"{render_url}/webhook"
    
    logger.info(f"🚀 ЗАПУСК БОТА...")
    logger.info(f"🔗 Вебхук: {webhook_url}")
    logger.info(f"👤 Админ ID: {ADMIN_ID}")
    
    async def setup():
        await application.initialize()
        await application.bot.delete_webhook()
        await application.bot.set_webhook(url=webhook_url)
        logger.info("✅ Вебхук установлен")
    
    asyncio.run(setup())
    
    port = int(os.getenv('PORT', 8000))
    app.run(host='0.0.0.0', port=port)
