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

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Конфигурация
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
CHANNEL_ID = os.getenv('CHANNEL_ID')
WP_URL = os.getenv('WP_URL')
WP_USERNAME = os.getenv('WP_USERNAME')
WP_PASSWORD = os.getenv('WP_PASSWORD')
YOUR_TELEGRAM_ID = os.getenv('YOUR_TELEGRAM_ID')  # Твой личный ID

# WordPress API
WP_API_URL = f"{WP_URL}/wp-json/wp/v2"
WP_MEDIA_URL = f"{WP_URL}/wp-json/wp/v2/media"

# Flask приложение
app = Flask(__name__)

# Создаём сессию для постоянного соединения с WordPress
wp_session = requests.Session()

# Временное хранилище для постов (ожидающих решения)
pending_posts = {}

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

def download_and_upload_photo(file_id):
    """Синхронная загрузка фото из Telegram в WordPress"""
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
        logger.info(f"Скачивание фото: {photo_url[:50]}...")
        
        download_headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        
        photo_response = requests.get(photo_url, headers=download_headers, timeout=60)
        
        if photo_response.status_code != 200:
            logger.error(f"Ошибка скачивания фото: {photo_response.status_code}")
            return None
        
        content_type = photo_response.headers.get('content-type', 'image/jpeg')
        
        wp_headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'application/json',
            'Accept-Language': 'ru-RU,ru;q=0.9,en;q=0.8',
            'Content-Type': content_type,
            'Content-Disposition': f'attachment; filename="telegram_photo_{int(time.time())}.jpg"'
        }
        
        wp_response = wp_session.post(
            WP_MEDIA_URL,
            auth=(WP_USERNAME, WP_PASSWORD),
            headers=wp_headers,
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

def create_wp_post(title, content, media_id=None, status='draft'):
    """Создание поста в WordPress с указанным статусом"""
    post_data = {
        'title': title,
        'content': content,
        'status': status,  # 'draft' или 'publish'
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
        
        logger.info(f"Отправка в WordPress (статус: {status}): {WP_API_URL}/news")
        
        response = wp_session.post(
            f"{WP_API_URL}/news",
            auth=(WP_USERNAME, WP_PASSWORD),
            json=post_data,
            headers=headers,
            timeout=60
        )
        
        if response.status_code == 201:
            post_link = response.json()['link']
            status_text = "ОПУБЛИКОВАН" if status == 'publish' else "сохранен как черновик"
            logger.info(f"✅ Пост {status_text}: {post_link}")
            return True, post_link
        else:
            logger.error(f"Ошибка: {response.status_code}")
            return False, None
            
    except Exception as e:
        logger.error(f"Ошибка: {e}")
        return False, None

async def handle_channel_post(update: Update, context):
    """Обработка постов из канала - отправляем на согласование"""
    try:
        channel_post = update.channel_post
        if not channel_post:
            return
        
        # Проверяем, что пост из нужного канала
        if str(channel_post.chat_id) != CHANNEL_ID:
            logger.warning(f"Не тот канал: {channel_post.chat_id}")
            return
        
        logger.info("=" * 60)
        logger.info(f"📨 Получен пост из канала: ID {channel_post.message_id}")
        
        # Извлекаем контент
        text = channel_post.caption or channel_post.text or ""
        title, content_text = extract_title_and_content(text)
        logger.info(f"📌 Заголовок: {title[:60]}...")
        
        # Обработка фото
        media_id = None
        if channel_post.photo:
            try:
                photo = channel_post.photo[-1]
                logger.info(f"📸 Обработка фото...")
                media_id = download_and_upload_photo(photo.file_id)
                if media_id:
                    logger.info(f"✅ Фото загружено")
            except Exception as e:
                logger.error(f"Ошибка фото: {e}")
        
        # Форматируем контент
        formatted_content = format_content_for_wp(content_text)
        
        # Сохраняем данные поста во временное хранилище
        post_id = str(channel_post.message_id)
        pending_posts[post_id] = {
            'title': title,
            'content': formatted_content,
            'media_id': media_id,
            'message_id': channel_post.message_id
        }
        
        # Создаём кнопки для выбора
        keyboard = [
            [
                InlineKeyboardButton("📝 Черновик", callback_data=f"draft_{post_id}"),
                InlineKeyboardButton("🚀 Опубликовать", callback_data=f"publish_{post_id}")
            ],
            [
                InlineKeyboardButton("❌ Отмена", callback_data=f"cancel_{post_id}")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        # Отправляем сообщение с кнопками ТЕБЕ в личку
        preview_text = f"📢 <b>Новый пост из канала!</b>\n\n"
        preview_text += f"<b>Заголовок:</b> {title[:100]}\n"
        preview_text += f"<b>Содержание:</b> {content_text[:150]}...\n" if len(content_text) > 150 else f"<b>Содержание:</b> {content_text}\n"
        preview_text += f"<b>Фото:</b> {'✅ есть' if media_id else '❌ нет'}\n\n"
        preview_text += f"<i>Выбери действие:</i>"
        
        if YOUR_TELEGRAM_ID:
            await context.bot.send_message(
                chat_id=YOUR_TELEGRAM_ID,
                text=preview_text,
                parse_mode='HTML',
                reply_markup=reply_markup
            )
            logger.info(f"✉️ Отправлен запрос на публикацию")
        else:
            logger.error("❌ YOUR_TELEGRAM_ID не задан! Пост не будет обработан.")
            # Если нет ID, сохраняем как черновик по умолчанию
            success, _ = create_wp_post(title, formatted_content, media_id, 'draft')
            if success:
                logger.info(f"✨ Пост сохранен как черновик (автоматически)")
        
        logger.info("=" * 60)
        
    except Exception as e:
        logger.error(f"❌ Ошибка: {e}")

async def handle_button_click(update: Update, context):
    """Обработка нажатия на кнопки"""
    query = update.callback_query
    await query.answer()
    
    data = query.data
    action, post_id = data.split('_')
    
    # Получаем данные поста
    post_data = pending_posts.get(post_id)
    if not post_data:
        await query.edit_message_text(
            "❌ Пост не найден. Возможно, время ожидания истекло.",
            parse_mode='HTML'
        )
        return
    
    if action == 'cancel':
        await query.edit_message_text(
            f"❌ Публикация отменена.\n\n<b>Заголовок:</b> {post_data['title'][:100]}",
            parse_mode='HTML'
        )
        del pending_posts[post_id]
        return
    
    # Определяем статус
    status = 'publish' if action == 'publish' else 'draft'
    status_text = "опубликован" if status == 'publish' else "сохранен как черновик"
    
    # Отправляем уведомление о начале
    await query.edit_message_text(
        f"⏳ <b>Обработка...</b>\n\nПост '{post_data['title'][:50]}...' {status_text}.",
        parse_mode='HTML'
    )
    
    # Создаём пост в WordPress
    success, post_link = create_wp_post(
        post_data['title'],
        post_data['content'],
        post_data['media_id'],
        status
    )
    
    if success:
        await query.edit_message_text(
            f"✅ <b>Готово!</b>\n\n"
            f"<b>Заголовок:</b> {post_data['title'][:100]}\n"
            f"<b>Статус:</b> {'Опубликован' if status == 'publish' else 'Черновик'}\n"
            f"<b>Ссылка:</b> {post_link}",
            parse_mode='HTML'
        )
        logger.info(f"✅ Пост {status_text}")
    else:
        await query.edit_message_text(
            f"❌ <b>Ошибка!</b>\n\nНе удалось {status_text} пост '{post_data['title'][:100]}'.",
            parse_mode='HTML'
        )
        logger.error(f"❌ Ошибка при создании поста")
    
    # Удаляем из временного хранилища
    del pending_posts[post_id]

# Создаем приложение Telegram
application = Application.builder().token(TELEGRAM_TOKEN).build()

# Добавляем обработчики
application.add_handler(MessageHandler(
    filters.Chat(chat_id=CHANNEL_ID) & (filters.TEXT | filters.PHOTO | filters.CAPTION),
    handle_channel_post
))
application.add_handler(CallbackQueryHandler(handle_button_click))

logger.info("✅ Обработчики сообщений добавлены")

# Настраиваем сессию для WordPress
wp_session.headers.update({
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
})

@app.route('/webhook', methods=['POST'])
def webhook():
    """Endpoint для вебхуков Telegram"""
    try:
        json_data = request.get_json(force=True)
        logger.info("🔔 Получен вебхук от Telegram")
        
        update = Update.de_json(json_data, application.bot)
        
        # Обрабатываем update
        asyncio.run(application.process_update(update))
        
        return jsonify({'status': 'ok'})
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return jsonify({'status': 'error'}), 500

@app.route('/health', methods=['GET'])
def health():
    """Health check для Render"""
    return jsonify({'status': 'healthy', 'service': 'Telegram to WordPress Bot'})

@app.route('/', methods=['GET'])
def index():
    return jsonify({
        'status': 'Bot is running',
        'message': 'Telegram to WordPress Bot',
        'post_type': 'news (custom post type)',
        'endpoints': {
            'webhook': '/webhook (POST)',
            'health': '/health (GET)'
        }
    })

if __name__ == '__main__':
    render_url = os.getenv('RENDER_EXTERNAL_URL')
    
    if not render_url:
        logger.error("❌ RENDER_EXTERNAL_URL не задан!")
        render_url = f"http://localhost:{os.getenv('PORT', 8000)}"
    
    webhook_url = f"{render_url}/webhook"
    
    logger.info(f"🚀 ЗАПУСК БОТА...")
    logger.info(f"🔗 Вебхук URL: {webhook_url}")
    logger.info(f"🌐 WordPress URL: {WP_URL}")
    logger.info(f"📝 Тип записи: news (Новости)")
    
    if YOUR_TELEGRAM_ID:
        logger.info(f"👤 Твой Telegram ID: {YOUR_TELEGRAM_ID}")
    else:
        logger.warning("⚠️ YOUR_TELEGRAM_ID не задан! Посты будут сохраняться как черновики автоматически.")
    
    # Настройка вебхука
    async def setup():
        await application.initialize()
        await application.bot.delete_webhook()
        await application.bot.set_webhook(url=webhook_url)
        logger.info("✅ Вебхук установлен успешно")
    
    asyncio.run(setup())
    
    port = int(os.getenv('PORT', 8000))
    logger.info(f"🎯 Сервер запущен на порту {port}")
    app.run(host='0.0.0.0', port=port)
