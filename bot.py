import os
import requests
import logging
import re
import time
import asyncio
from flask import Flask, request, jsonify
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, MessageHandler, filters, ConversationHandler
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

# Состояния для ConversationHandler
WAITING_FOR_ACTION = 1

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

def download_and_upload_photo(file_id):
    try:
        get_file_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile"
        file_response = requests.get(get_file_url, params={'file_id': file_id}, timeout=30)
        if file_response.status_code != 200:
            return None
        file_path = file_response.json().get('result', {}).get('file_path')
        if not file_path:
            return None
        photo_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"
        photo_response = requests.get(photo_url, timeout=60)
        if photo_response.status_code != 200:
            return None
        wp_response = wp_session.post(
            WP_MEDIA_URL,
            auth=(WP_USERNAME, WP_PASSWORD),
            headers={'Content-Disposition': f'attachment; filename="photo_{int(time.time())}.jpg"'},
            data=photo_response.content,
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
    """Получаем пост из канала"""
    try:
        channel_post = update.channel_post
        if not channel_post:
            return
        
        logger.info(f"📨 Получен пост из канала: {channel_post.message_id}")
        
        text = channel_post.caption or channel_post.text or ""
        title, content_text = extract_title_and_content(text)
        
        media_id = None
        if channel_post.photo:
            photo = channel_post.photo[-1]
            media_id = download_and_upload_photo(photo.file_id)
            if media_id:
                logger.info(f"✅ Фото загружено")
        
        formatted_content = format_content_for_wp(content_text)
        
        # Сохраняем во временное хранилище
        post_key = str(channel_post.message_id)
        pending_posts[post_key] = {
            'title': title,
            'content': formatted_content,
            'media_id': media_id
        }
        
        # Отправляем админу сообщение с кнопками выбора
        if ADMIN_ID:
            reply_keyboard = [
                [KeyboardButton("📝 В Черновики"), KeyboardButton("🚀 Опубликовать")]
            ]
            markup = ReplyKeyboardMarkup(reply_keyboard, resize_keyboard=True, one_time_keyboard=True)
            
            msg = f"📢 <b>Новый пост из канала!</b>\n\n"
            msg += f"<b>Заголовок:</b> {title[:100]}\n"
            msg += f"<b>Текст:</b> {content_text[:150]}...\n" if len(content_text) > 150 else f"<b>Текст:</b> {content_text}\n"
            msg += f"<b>Фото:</b> {'✅ есть' if media_id else '❌ нет'}\n\n"
            msg += f"<i>Выбери действие:</i>"
            
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=msg,
                parse_mode='HTML',
                reply_markup=markup
            )
            
            # Сохраняем post_key в context.user_data
            context.user_data['current_post_key'] = post_key
            logger.info(f"✉️ Отправлен запрос админу")
            
    except Exception as e:
        logger.error(f"Ошибка: {e}")

async def handle_admin_reply(update: Update, context):
    """Обрабатываем ответ админа (выбор действия)"""
    text = update.message.text
    user_id = update.effective_user.id
    
    # Проверяем, что это админ
    if str(user_id) != ADMIN_ID:
        return
    
    post_key = context.user_data.get('current_post_key')
    if not post_key:
        await update.message.reply_text("❌ Нет активного поста для публикации.")
        return
    
    post_data = pending_posts.get(post_key)
    if not post_data:
        await update.message.reply_text("❌ Пост не найден.")
        return
    
    if text == "📝 В Черновики":
        status = 'draft'
        status_text = "сохранен в Черновики"
        result_text = "📝 Пост сохранен в Черновики"
    elif text == "🚀 Опубликовать":
        status = 'publish'
        status_text = "опубликован"
        result_text = "🚀 Пост опубликован на сайте"
    else:
        return
    
    await update.message.reply_text(f"⏳ {status_text}...")
    
    success, link = create_wp_post(
        post_data['title'],
        post_data['content'],
        post_data['media_id'],
        status
    )
    
    if success:
        await update.message.reply_text(
            f"✅ {result_text}!\n\n"
            f"<b>Ссылка:</b> {link}",
            parse_mode='HTML'
        )
        logger.info(f"✅ Пост {status_text}: {post_data['title'][:50]}")
    else:
        await update.message.reply_text(f"❌ Ошибка! Не удалось {status_text} пост.")
        logger.error(f"❌ Ошибка при создании поста")
    
    # Очищаем кнопки
    await update.message.reply_text("Готово!", reply_markup=ReplyKeyboardMarkup([[]], resize_keyboard=True))
    
    # Удаляем пост из хранилища
    del pending_posts[post_key]
    del context.user_data['current_post_key']

# Создаем приложение
application = Application.builder().token(TELEGRAM_TOKEN).build()

# Обработчик постов из канала
application.add_handler(MessageHandler(
    filters.Chat(chat_id=CHANNEL_ID) & (filters.TEXT | filters.PHOTO | filters.CAPTION),
    handle_channel_post
))

# Обработчик ответов админа
application.add_handler(MessageHandler(
    filters.TEXT & filters.User(user_id=int(ADMIN_ID)) if ADMIN_ID else filters.ALL,
    handle_admin_reply
))

logger.info("✅ Обработчики добавлены")

wp_session.headers.update({'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'})

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
    
    async def setup():
        await application.initialize()
        await application.bot.delete_webhook()
        await application.bot.set_webhook(url=webhook_url)
        logger.info("✅ Вебхук установлен")
    
    asyncio.run(setup())
    
    port = int(os.getenv('PORT', 8000))
    app.run(host='0.0.0.0', port=port)
