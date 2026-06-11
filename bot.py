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
ADMIN_ID = os.getenv('YOUR_TELEGRAM_ID')

# WordPress API
WP_API_URL = f"{WP_URL}/wp-json/wp/v2"
WP_MEDIA_URL = f"{WP_URL}/wp-json/wp/v2/media"

app = Flask(__name__)
wp_session = requests.Session()
wp_session.headers.update({'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'})

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

def download_and_upload_photo(file_id):
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

def create_wp_post(title, content, media_id=None, status='draft'):
    post_data = {
        'title': title,
        'content': content,
        'status': status,
        'type': 'news',
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
            logger.error(f"Ошибка: {response.status_code}")
            return False, None
    except Exception as e:
        logger.error(f"Ошибка: {e}")
        return False, None

async def handle_channel_post(update: Update, context):
    try:
        channel_post = update.channel_post
        if not channel_post or str(channel_post.chat_id) != CHANNEL_ID:
            return
        
        logger.info(f"📨 Получен пост из канала")
        text = channel_post.caption or channel_post.text or ""
        title, content_text = extract_title_and_content(text)
        
        media_id = None
        if channel_post.photo:
            photo = channel_post.photo[-1]
            media_id = download_and_upload_photo(photo.file_id)
        
        formatted_content = format_content_for_wp(content_text)
        
        post_key = str(channel_post.message_id)
        pending_posts[post_key] = {
            'title': title,
            'content': formatted_content,
            'media_id': media_id
        }
        
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📝 В Черновики", callback_data=f"draft_{post_key}"),
                InlineKeyboardButton("🚀 Опубликовать", callback_data=f"publish_{post_key}")
            ]
        ])
        
        msg = f"📢 НОВЫЙ ПОСТ ИЗ КАНАЛА!\n\n"
        msg += f"Заголовок: {title[:100]}\n"
        msg += f"Фото: {'есть' if media_id else 'нет'}\n\n"
        msg += f"Выбери действие:"
        
        if ADMIN_ID:
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=msg,
                reply_markup=keyboard
            )
            logger.info(f"✉️ Отправлен запрос админу")
            
    except Exception as e:
        logger.error(f"Ошибка: {e}")

async def handle_private_message(update: Update, context):
    try:
        message = update.message
        if not message or str(message.from_user.id) != ADMIN_ID:
            return
        
        logger.info(f"📨 Получено сообщение в личку")
        
        text = message.caption or message.text or ""
        if not text and not message.photo:
            await message.reply_text("❌ Отправьте текст или фото с подписью.")
            return
        
        title, content_text = extract_title_and_content(text)
        
        media_id = None
        if message.photo:
            photo = message.photo[-1]
            media_id = download_and_upload_photo(photo.file_id)
            if media_id:
                logger.info(f"✅ Фото загружено")
        
        formatted_content = format_content_for_wp(content_text)
        
        post_key = str(int(time.time() * 1000))
        pending_posts[post_key] = {
            'title': title,
            'content': formatted_content,
            'media_id': media_id
        }
        
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📝 В Черновики", callback_data=f"draft_{post_key}"),
                InlineKeyboardButton("🚀 Опубликовать", callback_data=f"publish_{post_key}")
            ]
        ])
        
        msg = f"📢 НОВЫЙ ПОСТ!\n\n"
        msg += f"Заголовок: {title[:100]}\n"
        msg += f"Фото: {'есть' if media_id else 'нет'}\n\n"
        msg += f"Выбери действие:"
        
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=msg,
            reply_markup=keyboard
        )
        await message.reply_text("✅ Пост отправлен на утверждение!")
        logger.info(f"✉️ Отправлен запрос админу")
        
    except Exception as e:
        logger.error(f"Ошибка: {e}")

async def handle_button(update: Update, context):
    query = update.callback_query
    await query.answer()
    
    action, post_key = query.data.split('_')
    post_data = pending_posts.get(post_key)
    
    if not post_data:
        await query.edit_message_text("❌ Пост не найден")
        return
    
    status = 'publish' if action == 'publish' else 'draft'
    status_text = "опубликован" if action == 'publish' else "сохранен в Черновики"
    result_text = "🚀 Пост опубликован на сайте" if action == 'publish' else "📝 Пост сохранен в Черновики"
    
    await query.edit_message_text(f"⏳ {status_text}...")
    success, link = create_wp_post(
        post_data['title'], 
        post_data['content'], 
        post_data['media_id'], 
        status
    )
    
    if success:
        await query.edit_message_text(f"✅ Готово!\n\n{result_text}\n\nСсылка: {link}")
        logger.info(f"✅ Пост {status_text}")
    else:
        await query.edit_message_text(f"❌ Ошибка!\n\nНе удалось {status_text} пост.\n\n💡 Проверьте подключение к WordPress.")
        logger.error(f"❌ Ошибка при создании поста")
    
    del pending_posts[post_key]

# Создаем приложение
application = Application.builder().token(TELEGRAM_TOKEN).build()

application.add_handler(MessageHandler(
    filters.Chat(chat_id=CHANNEL_ID) & (filters.TEXT | filters.PHOTO | filters.CAPTION),
    handle_channel_post
))
application.add_handler(MessageHandler(
    ~filters.ChatType.CHANNEL & (filters.TEXT | filters.PHOTO | filters.CAPTION),
    handle_private_message
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
    
    logger.info(f"🚀 ЗАПУСК БОТА...")
    logger.info(f"🔗 Вебхук: {webhook_url}")
    logger.info(f"📢 ID канала: {CHANNEL_ID}")
    logger.info(f"👤 ID админа: {ADMIN_ID}")
    
    async def setup():
        await application.initialize()
        await application.bot.delete_webhook()
        await application.bot.set_webhook(url=webhook_url)
        logger.info("✅ Вебхук установлен")
    
    asyncio.run(setup())
    
    port = int(os.getenv('PORT', 8000))
    app.run(host='0.0.0.0', port=port)
