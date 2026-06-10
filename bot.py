import os
import requests
import logging
import re
import asyncio
from flask import Flask, request, jsonify
from telegram import Update
from telegram.ext import Application, MessageHandler, filters
from dotenv import load_dotenv

load_dotenv()

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Конфигурация
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
CHANNEL_ID = os.getenv('CHANNEL_ID')
WP_URL = os.getenv('WP_URL')
WP_USERNAME = os.getenv('WP_USERNAME')
WP_PASSWORD = os.getenv('WP_PASSWORD')

# WordPress API
WP_API_URL = f"{WP_URL}/wp-json/wp/v2"
WP_MEDIA_URL = f"{WP_URL}/wp-json/wp/v2/media"

# Flask приложение
app = Flask(__name__)

def extract_title_and_content(text):
    """Извлечение заголовка из текста"""
    if not text:
        return "Новый пост из Telegram", ""
    
    lines = text.strip().split('\n')
    title = lines[0].strip() if lines else "Новый пост"
    
    if len(title) > 180:
        title = title[:177] + "..."
    
    content = '\n'.join(lines[1:]).strip() if len(lines) > 1 else ""
    return title, content

def format_content_for_wp(text):
    """Форматирование контента для WordPress"""
    if not text:
        return ""
    
    paragraphs = text.split('\n')
    formatted = []
    
    for para in paragraphs:
        para = para.strip()
        if para:
            para = re.sub(r'(https?://[^\s]+)', r'<a href="\1">\1</a>', para)
            para = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', para)
            para = re.sub(r'\*(.+?)\*', r'<em>\1</em>', para)
            formatted.append(f'<p>{para}</p>')
    
    return '\n'.join(formatted)

def upload_image_to_wp(image_url, filename):
    """Загрузка фото в WordPress"""
    try:
        response = requests.get(f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{image_url}", timeout=30)
        
        if response.status_code == 200:
            wp_response = requests.post(
                WP_MEDIA_URL,
                auth=(WP_USERNAME, WP_PASSWORD),
                headers={'Content-Disposition': f'attachment; filename="{filename}"'},
                data=response.content,
                timeout=30
            )
            
            if wp_response.status_code == 201:
                return wp_response.json()['id']
    except Exception as e:
        logger.error(f"Ошибка фото: {e}")
    return None

def create_wp_draft(title, content, media_id=None):
    """Создание черновика"""
    post_data = {
        'title': title,
        'content': content,
        'status': 'draft',
    }
    
    if media_id:
        post_data['featured_media'] = media_id
    
    try:
        response = requests.post(
            f"{WP_API_URL}/posts",
            auth=(WP_USERNAME, WP_PASSWORD),
            json=post_data,
            timeout=30
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

async def handle_channel_post(update: Update, context):
    """Обработка постов из канала"""
    try:
        logger.info("=" * 50)
        logger.info("🔍 ОБРАБОТЧИК ВЫЗВАН!")
        
        channel_post = update.channel_post
        if not channel_post:
            logger.warning("⚠️ Нет channel_post")
            return
        
        real_chat_id = channel_post.chat_id
        logger.info(f"📨 Новый пост: ID {channel_post.message_id}")
        logger.info(f"🔍 ID канала: {real_chat_id}")
        
        text = channel_post.caption or channel_post.text or ""
        title, content_text = extract_title_and_content(text)
        logger.info(f"📌 Заголовок: {title[:50]}...")
        
        media_id = None
        if channel_post.photo:
            try:
                photo = channel_post.photo[-1]
                photo_file = await context.bot.get_file(photo.file_id)
                media_id = upload_image_to_wp(photo_file.file_path, f"photo_{channel_post.message_id}.jpg")
                if media_id:
                    logger.info(f"📸 Фото загружено")
            except Exception as e:
                logger.error(f"Ошибка фото: {e}")
        
        formatted_content = format_content_for_wp(content_text)
        
        if channel_post.date:
            source_info = f'<p><small>Источник: Telegram | {channel_post.date.strftime("%d.%m.%Y %H:%M")}</small></p>'
            formatted_content += source_info
        
        success = create_wp_draft(title, formatted_content, media_id)
        
        if success:
            logger.info(f"✨ Пост сохранен")
        else:
            logger.error("❌ Ошибка")
            
        logger.info("=" * 50)
        
    except Exception as e:
        logger.error(f"❌ Ошибка: {e}")
        logger.exception("Детали:")

# Создаем приложение Telegram
application = Application.builder().token(TELEGRAM_TOKEN).build()

# Добавляем обработчик
application.add_handler(MessageHandler(
    (filters.TEXT | filters.PHOTO | filters.CAPTION),
    handle_channel_post
))
logger.info("✅ Обработчик добавлен")

@app.route('/webhook', methods=['POST'])
def webhook():
    """Endpoint для вебхуков Telegram"""
    try:
        # Получаем данные от Telegram
        json_data = request.get_json(force=True)
        logger.info("🔔 Получен вебхук от Telegram")
        
        # Создаем объект Update
        update = Update.de_json(json_data, application.bot)
        
        # Обрабатываем update через application
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
    
    if not render_url:
        logger.error("❌ RENDER_EXTERNAL_URL не задан!")
        render_url = f"http://localhost:{os.getenv('PORT', 8000)}"
    
    webhook_url = f"{render_url}/webhook"
    
    logger.info(f"🚀 Запуск бота...")
    logger.info(f"🔗 Вебхук URL: {webhook_url}")
    
    # Устанавливаем вебхук
    async def setup_webhook():
        try:
            await application.bot.delete_webhook()
            await application.bot.set_webhook(url=webhook_url)
            logger.info("✅ Вебхук установлен")
        except Exception as e:
            logger.error(f"❌ Ошибка: {e}")
    
    asyncio.run(setup_webhook())
    
    port = int(os.getenv('PORT', 8000))
    app.run(host='0.0.0.0', port=port)
