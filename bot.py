import os
import requests
import asyncio
import logging
import re
from typing import List, Optional, Tuple
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes
from dotenv import load_dotenv

# Загружаем переменные окружения
load_dotenv()

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Конфигурация из переменных окружения
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
CHANNEL_ID = os.getenv('CHANNEL_ID')
WP_URL = os.getenv('WP_URL')
WP_USERNAME = os.getenv('WP_USERNAME')
WP_PASSWORD = os.getenv('WP_PASSWORD')

# WordPress API endpoints
WP_API_URL = f"{WP_URL}/wp-json/wp/v2"
WP_MEDIA_URL = f"{WP_URL}/wp-json/wp/v2/media"

# Настройки заголовка
MAX_TITLE_LENGTH = 180

def extract_title_and_content(text: str) -> Tuple[str, str]:
    """Извлечение заголовка из текста поста"""
    if not text:
        return "Новый пост из Telegram", ""
    
    lines = text.strip().split('\n')
    title = lines[0].strip() if lines else "Новый пост из Telegram"
    
    if len(title) > MAX_TITLE_LENGTH:
        title = title[:MAX_TITLE_LENGTH - 3] + "..."
    
    content = '\n'.join(lines[1:]).strip() if len(lines) > 1 else ""
    
    return title, content

def format_content_for_wp(text: str) -> str:
    """Форматирует текст для WordPress"""
    if not text:
        return ""
    
    paragraphs = text.split('\n')
    formatted = []
    
    for para in paragraphs:
        para = para.strip()
        if para:
            # Преобразуем ссылки
            para = re.sub(r'(https?://[^\s]+)', r'<a href="\1">\1</a>', para)
            # Преобразуем **жирный**
            para = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', para)
            # Преобразуем *курсив*
            para = re.sub(r'\*(.+?)\*', r'<em>\1</em>', para)
            formatted.append(f'<p>{para}</p>')
    
    return '\n'.join(formatted)

async def upload_image_to_wp(image_url: str, filename: str = "image.jpg") -> Optional[int]:
    """Загрузка изображения в WordPress"""
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
        logger.error(f"Ошибка загрузки фото: {e}")
    
    return None

async def create_wp_draft(title: str, content: str, media_ids: List[int] = None) -> bool:
    """Создание черновика в WordPress"""
    post_data = {
        'title': title,
        'content': content,
        'status': 'draft',
    }
    
    if media_ids and len(media_ids) > 0:
        post_data['featured_media'] = media_ids[0]
    
    try:
        response = requests.post(
            f"{WP_API_URL}/posts",
            auth=(WP_USERNAME, WP_PASSWORD),
            json=post_data,
            timeout=30
        )
        
        if response.status_code == 201:
            logger.info(f"✅ Черновик создан: {response.json()['link']}")
            return True
        else:
            logger.error(f"Ошибка: {response.status_code}")
            return False
    except Exception as e:
        logger.error(f"Ошибка: {e}")
        return False

async def handle_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка новых постов"""
    if not update.channel_post:
        return
    
    post = update.channel_post
    logger.info(f"📨 Новый пост: {post.message_id}")
    
    # Получаем текст
    full_text = post.caption or post.text or ""
    title, content_text = extract_title_and_content(full_text)
    
    # Обрабатываем фото
    media_ids = []
    if post.photo:
        photo_file = await post.photo[-1].get_file()
        media_id = await upload_image_to_wp(photo_file.file_path, f"photo_{post.message_id}.jpg")
        if media_id:
            media_ids.append(media_id)
    
    # Форматируем контент
    formatted_content = format_content_for_wp(content_text)
    
    # Создаем черновик
    await create_wp_draft(title, formatted_content, media_ids)

async def main():
    """Запуск бота"""
    if not all([TELEGRAM_TOKEN, CHANNEL_ID, WP_URL, WP_USERNAME, WP_PASSWORD]):
        logger.error("❌ Не все переменные окружения заданы!")
        return
    
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    
    channel_filter = filters.Chat(chat_id=CHANNEL_ID) & (filters.TEXT | filters.PHOTO | filters.CAPTION)
    application.add_handler(MessageHandler(channel_filter, handle_channel_post))
    
    logger.info(f"🚀 Бот запущен и слушает канал {CHANNEL_ID}")
    
    await application.run_polling()

if __name__ == '__main__':
    asyncio.run(main())
