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
ADMIN_ID = os.getenv('YOUR_TELEGRAM_ID')  # Твой Telegram ID

# WordPress API
WP_API_URL = f"{WP_URL}/wp-json/wp/v2"
WP_MEDIA_URL = f"{WP_URL}/wp-json/wp/v2/media"

app = Flask(__name__)
wp_session = requests.Session()

# Хранилище временных постов
pending_posts = {}

def extract_title_and_content(text):
    """Извлечение заголовка из текста"""
    if not text:
        return "Новый пост", ""
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

def download_and_upload_photo(file_id):
    """Загрузка фото в WordPress"""
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
    """Создание поста в WordPress"""
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

async def send_post_to_admin(context, title, content, media_id, source_id):
    """Отправка поста админу на утверждение"""
    post_key = str(int(time.time() * 1000))  # Уникальный ключ
    pending_posts[post_key] = {
        'title': title,
        'content': content,
        'media_id': media_id,
        'source_id': source_id
    }
    
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📝 В Черновики", callback_data=f"draft_{post_key}"),
            InlineKeyboardButton("🚀 Опубликовать", callback_data=f"publish_{post_key}")
        ]
    ])
    
    msg = f"📢 <b>Новый пост для публикации!</b>\n\n"
    msg += f"<b>Заголовок:</b> {title[:100]}\n"
    msg += f"<b>Текст:</b> {content[:150]}...\n" if len(content) > 150 else f"<b>Текст:</b> {content}\n"
    msg += f"<b>Фото:</b> {'✅ есть' if media_id else '❌ нет'}\n\n"
    msg += f"<i>Выбери действие:</i>"
    
    if ADMIN_ID:
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=msg,
            parse_mode='HTML',
            reply_markup=keyboard
        )
        logger.info(f"✉️ Отправлен запрос админу")
        return True
    return False

async def handle_channel_post(update: Update, context):
    """Обработка постов из канала"""
    try:
        channel_post = update.channel_post
        if not channel_post:
            return
        
        # Проверяем ID канала
        if str(channel_post.chat_id) != CHANNEL_ID:
            logger.warning(f"⚠️ Не тот канал: {channel_post.chat_id}")
            return
        
        logger.info(f"📨 Получен пост из канала: ID {channel_post.message_id}")
        
        # Извлекаем текст
        text = channel_post.caption or channel_post.text or ""
        title, content_text = extract_title_and_content(text)
        logger.info(f"📌 Заголовок: {title[:50]}...")
        
        # Обработка фото
        media_id = None
        if channel_post.photo:
            photo = channel_post.photo[-1]
            media_id = download_and_upload_photo(photo.file_id)
            if media_id:
                logger.info(f"📸 Фото загружено, ID: {media_id}")
        
        formatted_content = format_content_for_wp(content_text)
        
        # Отправляем админу на утверждение
        await send_post_to_admin(context, title, formatted_content, media_id, f"channel_{channel_post.message_id}")
        
    except Exception as e:
        logger.error(f"❌ Ошибка: {e}")

async def handle_private_message(update: Update, context):
    """Обработка репостов в личку бота"""
    try:
        message = update.message
        if not message:
            return
        
        # Проверяем, что сообщение от админа
        if str(message.from_user.id) != ADMIN_ID:
            logger.warning(f"⚠️ Сообщение не от админа: {message.from_user.id}")
            await message.reply_text("❌ У вас нет прав для использования этого бота.")
            return
        
        # Проверяем, есть ли репост
        forward_from = message.forward_from
        forward_from_chat = message.forward_from_chat
        
        text = message.caption or message.text or ""
        
        # Если это репост из канала или чата
        if forward_from_chat:
            logger.info(f"📨 Получен репост из чата: {forward_from_chat.id}")
            # У репостов текст может быть в caption или text
            if message.caption:
                text = message.caption
            elif message.text:
                text = message.text
        elif forward_from:
            logger.info(f"📨 Получен репост от пользователя: {forward_from.id}")
        else:
            # Обычное сообщение с текстом
            logger.info(f"📨 Получено текстовое сообщение")
        
        if not text:
            await message.reply_text("❌ Не удалось извлечь текст из репоста.")
            return
        
        # Извлекаем заголовок и контент
        title, content_text = extract_title_and_content(text)
        logger.info(f"📌 Заголовок: {title[:50]}...")
        
        # Обработка фото (если есть)
        media_id = None
        if message.photo:
            photo = message.photo[-1]
            media_id = download_and_upload_photo(photo.file_id)
            if media_id:
                logger.info(f"📸 Фото загружено, ID: {media_id}")
        
        formatted_content = format_content_for_wp(content_text)
        
        # Отправляем админу на утверждение (в данном случае самому себе)
        await send_post_to_admin(context, title, formatted_content, media_id, f"private_{message.message_id}")
        
        await message.reply_text("✅ Пост отправлен на утверждение! Скоро придут кнопки.")
        
    except Exception as e:
        logger.error(f"❌ Ошибка: {e}")

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
        status_text = "сохранен в Черновики"
        result_text = "📝 Пост сохранен в <b>Черновики</b>"
    else:
        status = 'publish'
        status_text = "опубликован"
        result_text = "🚀 Пост <b>опубликован</b> на сайте"
    
    await query.edit_message_text(f"⏳ {status_text}...")
    
    success, link = create_wp_post(
        post_data['title'],
        post_data['content'],
        post_data['media_id'],
        status
    )
    
    if success:
        await query.edit_message_text(
            f"✅ <b>Готово!</b>\n\n"
            f"{result_text}\n\n"
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

# Добавляем обработчики
application.add_handler(MessageHandler(
    filters.Chat(chat_id=CHANNEL_ID) & (filters.TEXT | filters.PHOTO | filters.CAPTION),
    handle_channel_post
))
application.add_handler(MessageHandler(
    filters.PRIVATE & (filters.TEXT | filters.PHOTO | filters.CAPTION | filters.FORWARDED),
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
