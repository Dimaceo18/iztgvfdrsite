import os
import requests
import logging
import re
import time
import asyncio
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
YOUR_ID = os.getenv('YOUR_TELEGRAM_ID')

# WordPress API
WP_API_URL = f"{WP_URL}/wp-json/wp/v2"
WP_MEDIA_URL = f"{WP_URL}/wp-json/wp/v2/media"

app = Flask(__name__)
wp_session = requests.Session()

# Хранилище временных постов
pending_posts = {}

def extract_title_and_content(text):
    if not text:
        return "Новый пост", ""
    lines = text.strip().split('\n')
    title = lines[0].strip() if lines else "Новый пост"
    if len(title) > 180:
        title = title[:177] + "..."
    content = '\n'.join(lines[1:]).strip() if len(lines) > 1 else ""
    return title, content

def format_content_for_wp(text):
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

def upload_photo(file_id):
    try:
        get_file = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile",
            params={'file_id': file_id},
            timeout=30
        )
        if get_file.status_code != 200:
            return None
        
        file_path = get_file.json().get('result', {}).get('file_path')
        if not file_path:
            return None
        
        photo_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"
        photo_data = requests.get(photo_url, timeout=60)
        if photo_data.status_code != 200:
            return None
        
        wp_response = wp_session.post(
            WP_MEDIA_URL,
            auth=(WP_USERNAME, WP_PASSWORD),
            headers={'Content-Disposition': f'attachment; filename="photo_{int(time.time())}.jpg"'},
            data=photo_data.content,
            timeout=60
        )
        
        if wp_response.status_code == 201:
            return wp_response.json()['id']
    except Exception as e:
        logger.error(f"Ошибка фото: {e}")
    return None

def create_post(title, content, media_id=None, status='draft'):
    """Создание поста в WordPress"""
    post_data = {
        'title': title,
        'content': content,
        'status': status,  # 'draft' или 'publish'
        'type': 'news',    # Тип записей "Новости"
    }
    if media_id:
        post_data['featured_media'] = media_id
    
    try:
        response = wp_session.post(
            f"{WP_API_URL}/news",
            auth=(WP_USERNAME, WP_PASSWORD),
            json=post_data,
            timeout=60
        )
        if response.status_code == 201:
            return True, response.json()['link']
        else:
            logger.error(f"Ошибка {response.status_code}: {response.text[:200]}")
            return False, None
    except Exception as e:
        logger.error(f"Ошибка: {e}")
        return False, None

async def handle_channel_post(update: Update, context):
    """Получаем пост из канала и отправляем запрос на публикацию"""
    try:
        channel_post = update.channel_post
        if not channel_post:
            return
        
        if str(channel_post.chat_id) != CHANNEL_ID:
            return
        
        logger.info(f"📨 Получен пост из канала: ID {channel_post.message_id}")
        
        text = channel_post.caption or channel_post.text or ""
        title, content_text = extract_title_and_content(text)
        
        media_id = None
        if channel_post.photo:
            photo = channel_post.photo[-1]
            media_id = upload_photo(photo.file_id)
            if media_id:
                logger.info(f"📸 Фото загружено")
        
        formatted_content = format_content_for_wp(content_text)
        
        post_key = str(channel_post.message_id)
        pending_posts[post_key] = {
            'title': title,
            'content': formatted_content,
            'media_id': media_id
        }
        
        # Кнопки выбора
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📝 В Черновики (Новости)", callback_data=f"draft_{post_key}"),
                InlineKeyboardButton("🚀 Опубликовать на сайт", callback_data=f"publish_{post_key}")
            ]
        ])
        
        msg = f"📢 <b>Новый пост из канала!</b>\n\n"
        msg += f"<b>Заголовок:</b> {title[:100]}\n"
        msg += f"<b>Текст:</b> {content_text[:150]}...\n" if len(content_text) > 150 else f"<b>Текст:</b> {content_text}\n"
        msg += f"<b>Фото:</b> {'✅ есть' if media_id else '❌ нет'}\n\n"
        msg += f"<i>Куда сохранить пост?</i>"
        
        await context.bot.send_message(
            chat_id=YOUR_ID,
            text=msg,
            parse_mode='HTML',
            reply_markup=keyboard
        )
        
        logger.info(f"✉️ Отправлен запрос: {title[:50]}")
        
    except Exception as e:
        logger.error(f"Ошибка: {e}")

async def handle_button(update: Update, context):
    """Обработка нажатия на кнопку"""
    query = update.callback_query
    await query.answer()
    
    action, post_key = query.data.split('_')
    
    post_data = pending_posts.get(post_key)
    if not post_data:
        await query.edit_message_text("❌ Пост не найден")
        return
    
    if action == 'draft':
        status = 'draft'
        status_text = "сохранен в Черновики (раздел Новости)"
        result_text = "📝 Пост сохранен в <b>Черновики</b> раздела <b>Новости</b>"
    else:
        status = 'publish'
        status_text = "опубликован на сайте"
        result_text = "🚀 Пост <b>опубликован</b> на сайте"
    
    await query.edit_message_text(f"⏳ {status_text}...")
    
    success, link = create_post(
        post_data['title'],
        post_data['content'],
        post_data['media_id'],
        status
    )
    
    if success:
        await query.edit_message_text(
            f"✅ <b>Готово!</b>\n\n"
            f"{result_text}\n\n"
            f"<b>Заголовок:</b> {post_data['title'][:100]}\n"
            f"<b>Ссылка:</b> {link}",
            parse_mode='HTML'
        )
        logger.info(f"✅ Пост {status_text}: {post_data['title'][:50]}")
    else:
        await query.edit_message_text(
            f"❌ <b>Ошибка!</b>\n\nНе удалось {status_text} пост.",
            parse_mode='HTML'
        )
        logger.error(f"❌ Ошибка при создании поста")
    
    del pending_posts[post_key]

# Создаем приложение
application = Application.builder().token(TELEGRAM_TOKEN).build()

application.add_handler(MessageHandler(
    filters.Chat(chat_id=CHANNEL_ID) & (filters.TEXT | filters.PHOTO | filters.CAPTION),
    handle_channel_post
))
application.add_handler(CallbackQueryHandler(handle_button))

logger.info("✅ Обработчики добавлены")

@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        json_data = request.get_json(force=True)
        logger.info("🔔 Вебхук получен")
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
    
    logger.info(f"🚀 Запуск бота...")
    logger.info(f"🔗 Вебхук: {webhook_url}")
    logger.info(f"📢 Канал: {CHANNEL_ID}")
    logger.info(f"👤 Твой ID: {YOUR_ID}")
    
    async def setup():
        await application.initialize()
        await application.bot.delete_webhook()
        await application.bot.set_webhook(url=webhook_url)
        logger.info("✅ Вебхук установлен")
    
    asyncio.run(setup())
    
    port = int(os.getenv('PORT', 8000))
    app.run(host='0.0.0.0', port=port)
