import os
import requests
import logging
import re
import time
import json
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from collections import defaultdict
import threading
from datetime import datetime, timedelta
from PIL import Image, ImageEnhance, ImageFilter
import io
import random
import urllib3

# Отключаем предупреждения SSL
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

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
DEEPSEEK_API_KEY = os.getenv('DEEPSEEK_API_KEY')

# API DeepSeek
DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"

# WordPress API
WP_API_URL = f"{WP_URL}/wp-json/wp/v2"
WP_MEDIA_URL = f"{WP_URL}/wp-json/wp/v2/media"

# Доступные разделы
POST_TYPES = {
    "news": "📰 Новости",
    "auto": "🚗 Авто",
    "afisha": "🎭 Афиша",
    "realt": "🏠 Недвижимость",
    "sales": "🏷️ Скидки/Распродажи",
    "sport": "⚽ Спорт"
}

app = Flask(__name__)

# Создаем сессию с отключенной проверкой SSL и большими таймаутами
wp_session = requests.Session()
wp_session.verify = False
wp_session.timeout = 300

# Настраиваем адаптер с большими таймаутами
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

retry_strategy = Retry(
    total=5,
    backoff_factor=2,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["HEAD", "GET", "OPTIONS", "POST", "PUT", "PATCH"]
)

adapter = HTTPAdapter(
    max_retries=retry_strategy,
    pool_connections=100,
    pool_maxsize=100
)
wp_session.mount("http://", adapter)
wp_session.mount("https://", adapter)

# Хранилище
pending_posts = {}
media_groups = defaultdict(dict)
group_timers = {}
scheduled_posts = {}
scheduled_timers = {}
video_pending = {}
uploaded_media_cache = {}

# Базовый URL для Telegram API
TG_API_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

DEEPSEEK_PROMPT = """Ты редактор новостного сайта. Перепиши новость в строгом городском формате, объемом около 650 символов. Убери лишнюю воду, сделай интересный заголовок, никаких смайликов. Не используй символы # и ** в ответе. Сохрани главные факты. Расставь абзацы.

ВАЖНО: НЕ пиши слова "Заголовок:" и "Текст:". Просто напиши сначала заголовок, потом пустую строку, потом текст."""

def generate_seo_description(title, content, post_type=None):
    """Генерирует SEO-описание для Yoast"""
    try:
        clean_text = re.sub(r'<[^>]+>', '', content)
        clean_text = re.sub(r'\[video[^\]]*\]', '', clean_text)
        clean_text = re.sub(r'\[gallery[^\]]*\]', '', clean_text)
        clean_text = re.sub(r'\[[^\]]*\]', '', clean_text)
        clean_text = re.sub(r'https?://[^\s]+', '', clean_text)
        clean_text = ' '.join(clean_text.split())
        
        if len(clean_text) > 200:
            description = clean_text[:197] + "..."
        elif len(clean_text) > 50:
            description = clean_text
        else:
            description = f"Подробности в материале: {title}"
        
        title_keywords = title.split()
        if len(title_keywords) > 2:
            keywords = ' '.join(title_keywords[:2])
            if not description.startswith(keywords):
                description = f"{keywords}. {description}"
        
        context_map = {
            "news": "Новости",
            "auto": "Автомобильные новости",
            "afisha": "Афиша и события",
            "realt": "Недвижимость",
            "sales": "Скидки и распродажи",
            "sport": "Спортивные новости"
        }
        
        if post_type and post_type in context_map:
            context = context_map[post_type]
            if len(description) + len(context) + 20 < 160:
                description = f"{description} Читайте в разделе {context}."
        
        if len(description) > 160:
            description = description[:157] + "..."
        
        logger.info(f"✅ Сгенерировано SEO-описание: {description[:100]}...")
        return description
        
    except Exception as e:
        logger.error(f"❌ Ошибка генерации SEO-описания: {e}")
        return f"{title[:140]}..."

def unique_image(image_bytes, is_video_thumbnail=False):
    """Уникализация изображения"""
    try:
        image = Image.open(io.BytesIO(image_bytes))
        
        if image.mode in ('RGBA', 'LA', 'P'):
            image = image.convert('RGB')
        
        method = random.choice([
            'resize_sharpen',
            'color_adjust',
            'rotate_crop',
            'filter_sharpen',
            'contrast_brightness'
        ])
        
        if is_video_thumbnail:
            method = random.choice([
                'resize_sharpen',
                'contrast_brightness',
                'color_adjust'
            ])
        
        logger.info(f"🔄 Уникализация фото методом: {method}")
        
        width, height = image.size
        
        if method == 'resize_sharpen':
            scale = random.uniform(0.95, 1.05)
            new_width = int(width * scale)
            new_height = int(height * scale)
            image = image.resize((new_width, new_height), Image.Resampling.LANCZOS)
            enhancer = ImageEnhance.Sharpness(image)
            image = enhancer.enhance(random.uniform(1.1, 1.3))
            image = image.resize((width, height), Image.Resampling.LANCZOS)
        
        elif method == 'color_adjust':
            enhancer = ImageEnhance.Color(image)
            image = enhancer.enhance(random.uniform(0.9, 1.1))
            enhancer = ImageEnhance.Contrast(image)
            image = enhancer.enhance(random.uniform(0.95, 1.05))
            enhancer = ImageEnhance.Brightness(image)
            image = enhancer.enhance(random.uniform(0.97, 1.03))
        
        elif method == 'rotate_crop':
            angle = random.uniform(-2, 2)
            image = image.rotate(angle, expand=False, fillcolor='white')
            crop_percent = random.uniform(0, 0.02)
            crop_x = int(width * crop_percent)
            crop_y = int(height * crop_percent)
            if crop_x > 0 and crop_y > 0:
                image = image.crop((crop_x, crop_y, width - crop_x, height - crop_y))
                image = image.resize((width, height), Image.Resampling.LANCZOS)
        
        elif method == 'filter_sharpen':
            if random.random() < 0.3:
                image = image.filter(ImageFilter.GaussianBlur(radius=0.3))
            enhancer = ImageEnhance.Sharpness(image)
            image = enhancer.enhance(random.uniform(1.2, 1.5))
        
        elif method == 'contrast_brightness':
            enhancer = ImageEnhance.Contrast(image)
            image = enhancer.enhance(random.uniform(0.9, 1.1))
            enhancer = ImageEnhance.Brightness(image)
            image = enhancer.enhance(random.uniform(0.95, 1.05))
        
        buffer = io.BytesIO()
        format_type = 'JPEG' if image.mode == 'RGB' else 'PNG'
        quality = random.randint(85, 95)
        
        if format_type == 'JPEG':
            image.save(buffer, format=format_type, quality=quality, optimize=True)
        else:
            image.save(buffer, format=format_type, optimize=True)
        
        buffer.seek(0)
        unique_bytes = buffer.getvalue()
        
        logger.info(f"✅ Фото уникализировано: {len(unique_bytes)} байт")
        return unique_bytes
        
    except Exception as e:
        logger.error(f"❌ Ошибка уникализации: {e}")
        return image_bytes

def set_media_metadata(media_id, title, alt_text=None):
    """Установка метаданных для медиафайла в WordPress"""
    try:
        if alt_text is None:
            alt_text = title
        
        alt_text = re.sub(r'<[^>]+>', '', alt_text)
        title = re.sub(r'<[^>]+>', '', title)
        
        if len(alt_text) > 150:
            alt_text = alt_text[:147] + "..."
        if len(title) > 150:
            title = title[:147] + "..."
        
        meta_data = {
            'title': title,
            'alt_text': alt_text,
            'caption': '',
            'description': f"Изображение к статье: {title}"
        }
        
        logger.info(f"📝 Устанавливаю метаданные для медиа ID={media_id}")
        
        response = wp_session.post(
            f"{WP_MEDIA_URL}/{media_id}",
            auth=(WP_USERNAME, WP_PASSWORD),
            json=meta_data,
            timeout=60,
            verify=False
        )
        
        if response.status_code == 200:
            logger.info(f"✅ Метаданные медиа обновлены: ID={media_id}")
            return True
        else:
            logger.error(f"❌ Ошибка обновления метаданных: {response.status_code}")
            return False
            
    except Exception as e:
        logger.error(f"❌ Ошибка установки метаданных: {e}")
        return False

def get_action_keyboard(post_key):
    """Создает клавиатуру с действиями для поста"""
    return {
        "inline_keyboard": [
            [{"text": "🤖 Переделать текст через ИИ", "callback_data": f"ai|{post_key}"}],
            [{"text": "📝 Сохранить в черновики", "callback_data": f"draft|{post_key}"}],
            [{"text": "🌐 Опубликовать сейчас", "callback_data": f"publish|{post_key}"}],
            [
                {"text": "⏰ 15 мин", "callback_data": f"schedule|{post_key}|15"},
                {"text": "⏰ 30 мин", "callback_data": f"schedule|{post_key}|30"},
                {"text": "⏰ 1 час", "callback_data": f"schedule|{post_key}|60"}
            ],
            [
                {"text": "⏰ 2 часа", "callback_data": f"schedule|{post_key}|120"},
                {"text": "⏰ 3 часа", "callback_data": f"schedule|{post_key}|180"},
                {"text": "⏰ 4 часа", "callback_data": f"schedule|{post_key}|240"}
            ],
            [
                {"text": "⏰ 5 часов", "callback_data": f"schedule|{post_key}|300"},
                {"text": "⏰ 6 часов", "callback_data": f"schedule|{post_key}|360"}
            ]
        ]
    }

def get_post_type_keyboard(post_key):
    """Создает клавиатуру с выбором раздела"""
    keyboard = {
        "inline_keyboard": []
    }
    for pt_key, pt_name in POST_TYPES.items():
        keyboard["inline_keyboard"].append([{"text": pt_name, "callback_data": f"select_post_type|{post_key}|{pt_key}"}])
    return keyboard

def tg_send_message(chat_id, text, reply_markup=None, parse_mode=None):
    url = f"{TG_API_URL}/sendMessage"
    data = {'chat_id': chat_id, 'text': text}
    if reply_markup:
        data['reply_markup'] = reply_markup
    if parse_mode:
        data['parse_mode'] = parse_mode
    return requests.post(url, json=data, timeout=30)

def tg_edit_message_text(chat_id, message_id, text, reply_markup=None):
    url = f"{TG_API_URL}/editMessageText"
    data = {'chat_id': chat_id, 'message_id': message_id, 'text': text}
    if reply_markup:
        data['reply_markup'] = reply_markup
    return requests.post(url, json=data, timeout=30)

def tg_delete_message(chat_id, message_id):
    url = f"{TG_API_URL}/deleteMessage"
    data = {'chat_id': chat_id, 'message_id': message_id}
    return requests.post(url, json=data, timeout=30)

def tg_answer_callback_query(callback_id):
    url = f"{TG_API_URL}/answerCallbackQuery"
    return requests.post(url, json={'callback_query_id': callback_id}, timeout=30)

def extract_title_and_content(text):
    if not text:
        return "Новый пост из Telegram", ""
    lines = text.strip().split('\n')
    title = lines[0].strip() if lines else "Новый пост"
    if len(title) > 180:
        title = title[:177] + "..."
    content = '\n'.join(lines[1:]).strip() if len(lines) > 1 else ""
    return title, content

def format_content_for_wp(text, video_url=None, gallery_ids=None, is_video=False):
    """Форматирование контента для WordPress с вставкой видео или галереи после первого абзаца"""
    if not text:
        return ""
    
    paragraphs = text.split('\n')
    formatted = []
    
    for i, para in enumerate(paragraphs):
        para = para.strip()
        if para:
            para = re.sub(r'(https?://[^\s]+)', r'<a href="\1">\1</a>', para)
            para = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', para)
            para = re.sub(r'\*(.+?)\*', r'<em>\1</em>', para)
            formatted.append(f'<p>{para}</p>')
            
            if i == 0:
                if is_video and video_url:
                    formatted.append(f'[video width="100%" height="auto" mp4="{video_url}"]')
                    logger.info(f"🎬 Видео вставлено в контент после 1-го абзаца: {video_url}")
                elif gallery_ids and len(gallery_ids) > 0:
                    gallery_shortcode = '[gallery ids="' + ','.join(str(id) for id in gallery_ids) + '" size="full" columns="1" link="none"]'
                    formatted.append(gallery_shortcode)
                    logger.info(f"🖼️ Добавлена галерея из {len(gallery_ids)} фото после 1-го абзаца (без подписей)")
    
    return '\n'.join(formatted)

def process_text_with_deepseek(text):
    if not DEEPSEEK_API_KEY:
        return None
    try:
        response = requests.post(
            DEEPSEEK_API_URL,
            headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": "deepseek-chat",
                "messages": [
                    {"role": "system", "content": "Ты редактор новостного сайта. Отвечай только готовым новостным текстом, без пояснений и вступлений. Не используй символы # и ** в ответе."},
                    {"role": "user", "content": f"{DEEPSEEK_PROMPT}\n\n{text}"}
                ],
                "temperature": 0.7,
                "max_tokens": 1000
            },
            timeout=60
        )
        if response.status_code == 200:
            result = response.json()["choices"][0]["message"]["content"]
            result = re.sub(r'^Вот обработанный новостной текст.*?:', '', result, flags=re.IGNORECASE)
            result = re.sub(r'^Вот.*?текст.*?:', '', result, flags=re.IGNORECASE)
            result = re.sub(r'^#+\s+', '', result, flags=re.MULTILINE)
            return result.strip()
        return None
    except Exception as e:
        logger.error(f"Ошибка DeepSeek: {e}")
        return None

def download_and_upload_media(file_id, is_video=False, is_thumbnail=False, title=None, alt_text=None):
    """Загрузка фото или видео в WordPress с уникализацией и метаданными"""
    cache_key = f"{file_id}_{is_video}_{title}"
    if cache_key in uploaded_media_cache:
        logger.info(f"📸 Использую кэшированное медиа: {uploaded_media_cache[cache_key]}")
        return uploaded_media_cache[cache_key]
    
    try:
        media_type = "видео" if is_video else "фото"
        logger.info(f"📸 НАЧАЛО ЗАГРУЗКИ {media_type.upper()}: file_id={file_id}")
        
        get_file_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile"
        file_response = requests.get(get_file_url, params={'file_id': file_id}, timeout=60)
        
        if file_response.status_code != 200:
            logger.error(f"❌ Ошибка getFile: {file_response.status_code}")
            logger.error(f"Ответ: {file_response.text}")
            return None, None
        
        result = file_response.json().get('result')
        if not result:
            logger.error("❌ Не получен result от Telegram")
            return None, None
        
        file_path = result.get('file_path')
        if not file_path:
            logger.error("❌ Не получен file_path")
            return None, None
        
        logger.info(f"✅ file_path получен: {file_path}")
        
        media_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"
        logger.info(f"📸 Скачиваю {media_type}...")
        
        media_response = requests.get(media_url, timeout=120)
        if media_response.status_code != 200:
            logger.error(f"❌ Ошибка скачивания {media_type}: {media_response.status_code}")
            return None, None
        
        media_content = media_response.content
        logger.info(f"✅ {media_type.capitalize()} скачано, размер: {len(media_content)} байт")
        
        if not is_video:
            is_video_thumbnail = is_thumbnail
            media_content = unique_image(media_content, is_video_thumbnail)
            logger.info(f"✅ Фото уникализировано, новый размер: {len(media_content)} байт")
        
        ext = 'mp4' if is_video else 'jpg'
        mime = 'video/mp4' if is_video else 'image/jpeg'
        
        if title:
            clean_title = re.sub(r'[^\w\s-]', '', title)
            clean_title = re.sub(r'[-\s]+', '-', clean_title)
            clean_title = clean_title[:100]
            filename = f"{clean_title}_{int(time.time())}.{ext}"
        else:
            filename = f'{media_type}_{int(time.time())}.{ext}'
        
        files = {
            'file': (filename, media_content, mime)
        }
        
        logger.info(f"📸 Загружаю {media_type} в WordPress как: {filename}")
        
        max_retries = 3
        for attempt in range(max_retries):
            try:
                wp_response = wp_session.post(
                    WP_MEDIA_URL,
                    auth=(WP_USERNAME, WP_PASSWORD),
                    files=files,
                    timeout=180,
                    verify=False
                )
                
                if wp_response.status_code == 201:
                    media_id = wp_response.json()['id']
                    source_url = wp_response.json().get('source_url', 'unknown')
                    logger.info(f"✅ {media_type.capitalize()} загружено! ID={media_id}, URL={source_url}")
                    
                    if title:
                        set_media_metadata(media_id, title, alt_text)
                    
                    uploaded_media_cache[cache_key] = (media_id, source_url)
                    return media_id, source_url
                else:
                    logger.error(f"❌ Ошибка WP при загрузке {media_type}: {wp_response.status_code}")
                    logger.error(f"Ответ: {wp_response.text[:200]}")
                    if attempt < max_retries - 1:
                        logger.info(f"🔄 Повторная попытка {attempt + 2}/{max_retries} через 5 секунд...")
                        time.sleep(5)
                    continue
                    
            except requests.exceptions.Timeout:
                logger.error(f"❌ Таймаут при загрузке {media_type} (попытка {attempt + 1}/{max_retries})")
                if attempt < max_retries - 1:
                    logger.info(f"🔄 Повторная попытка {attempt + 2}/{max_retries} через 10 секунд...")
                    time.sleep(10)
                continue
            except Exception as e:
                logger.error(f"❌ Ошибка при загрузке {media_type}: {e}")
                if attempt < max_retries - 1:
                    logger.info(f"🔄 Повторная попытка {attempt + 2}/{max_retries} через 5 секунд...")
                    time.sleep(5)
                continue
        
        logger.error(f"❌ Не удалось загрузить {media_type} после {max_retries} попыток")
        return None, None
            
    except Exception as e:
        logger.error(f"❌ Ошибка загрузки медиа: {e}")
        return None, None

def create_wp_post(title, content, post_type, featured_media_id=None, video_url=None, publish=False, is_video=False, gallery_ids=None, schedule_time=None):
    """Создание поста в WordPress с видео или галереей в контенте и SEO (Yoast)"""
    status = 'future' if schedule_time else ('publish' if publish else 'draft')
    
    final_content = content
    if is_video and video_url:
        final_content = format_content_for_wp(content, video_url, None, is_video=True)
        logger.info(f"🎬 Видео вставлено в контент")
    elif gallery_ids and len(gallery_ids) > 0:
        final_content = format_content_for_wp(content, None, gallery_ids, is_video=False)
        logger.info(f"🖼️ Галерея из {len(gallery_ids)} фото добавлена в контент")
    
    seo_title = title[:70]
    seo_description = generate_seo_description(title, content, post_type)
    
    post_data = {
        'title': title,
        'content': final_content,
        'status': status,
        'type': post_type,
        'meta': {
            '_yoast_wpseo_title': seo_title,
            '_yoast_wpseo_metadesc': seo_description,
            'yoast_wpseo_title': seo_title,
            'yoast_wpseo_metadesc': seo_description,
            '_wpseo_metadesc': seo_description,
            '_wpseo_title': seo_title
        }
    }
    
    if schedule_time:
        post_data['date'] = schedule_time.isoformat()
        logger.info(f"⏰ Запланирована публикация на {schedule_time.strftime('%d.%m.%Y %H:%M')}")
    
    if featured_media_id:
        post_data['featured_media'] = featured_media_id
        logger.info(f"📎 Устанавливаю обложку WP ID={featured_media_id}")
    
    try:
        logger.info(f"📤 Отправка в WordPress: раздел={post_type}, статус={status}")
        logger.info(f"🔍 SEO Заголовок: {seo_title}")
        logger.info(f"🔍 SEO Описание: {seo_description}")
        
        response = wp_session.post(
            f"{WP_API_URL}/{post_type}",
            auth=(WP_USERNAME, WP_PASSWORD),
            json=post_data,
            timeout=120,
            verify=False
        )
        
        logger.info(f"📤 Ответ WP: {response.status_code}")
        
        if response.status_code == 201:
            post_id = response.json()['id']
            post_link = response.json()['link']
            logger.info(f"✅ Пост создан: {post_link}")
            
            update_data = {
                'meta': {
                    '_yoast_wpseo_title': seo_title,
                    '_yoast_wpseo_metadesc': seo_description
                }
            }
            
            try:
                update_response = wp_session.post(
                    f"{WP_API_URL}/{post_type}/{post_id}",
                    auth=(WP_USERNAME, WP_PASSWORD),
                    json=update_data,
                    timeout=60,
                    verify=False
                )
                if update_response.status_code == 200:
                    logger.info("✅ Мета-данные обновлены")
            except Exception as e:
                logger.warning(f"⚠️ Не удалось обновить мета-данные: {e}")
            
            return True, post_link
        else:
            logger.error(f"❌ Ошибка: {response.status_code}")
            logger.error(f"Ответ: {response.text[:500]}")
            return False, None
            
    except Exception as e:
        logger.error(f"❌ Ошибка: {e}")
        return False, None

def publish_scheduled_post(post_key):
    """Публикация отложенного поста"""
    if post_key not in scheduled_posts:
        return
    
    post_data = scheduled_posts[post_key]
    chat_id = post_data.get('chat_id')
    msg_id = post_data.get('msg_id')
    
    logger.info(f"⏰ Время публикации для поста {post_key}!")
    
    try:
        is_video = post_data.get('is_video', False)
        featured_media_id = post_data.get('featured_media_id')
        video_file_id = post_data.get('video_file_id')
        gallery_file_ids = post_data.get('gallery_file_ids', [])
        title = post_data.get('title', '')
        
        video_url = None
        gallery_ids = []
        
        if is_video and video_file_id:
            logger.info(f"🎬 Загрузка видео...")
            media_id, video_url = download_and_upload_media(video_file_id, True, is_thumbnail=False, title=title)
            if media_id:
                logger.info(f"✅ Видео загружено, URL={video_url}")
            else:
                logger.error("❌ Не удалось загрузить видео")
        
        for file_id in gallery_file_ids:
            if file_id != featured_media_id:
                logger.info(f"📸 Загрузка фото для галереи...")
                media_id, _ = download_and_upload_media(file_id, False, is_thumbnail=False, title=title)
                if media_id:
                    gallery_ids.append(media_id)
                    logger.info(f"✅ Фото загружено, ID={media_id}")
        
        success, link = create_wp_post(
            post_data['title'],
            post_data['content'],
            post_data['post_type'],
            featured_media_id,
            video_url,
            True,
            is_video,
            gallery_ids if gallery_ids else None,
            None
        )
        
        if success:
            tg_send_message(chat_id, f"✅ Пост опубликован по расписанию!\n\n{link}")
        else:
            tg_send_message(chat_id, "❌ Ошибка публикации по расписанию")
        
        del scheduled_posts[post_key]
        if post_key in scheduled_timers:
            del scheduled_timers[post_key]
            
    except Exception as e:
        logger.error(f"Ошибка публикации по расписанию: {e}")
        tg_send_message(chat_id, f"❌ Ошибка публикации по расписанию: {str(e)[:100]}")

def process_media_group(media_group_id):
    """Обработка собранной медиа-группы (альбома)"""
    if media_group_id not in media_groups:
        return
    
    group_data = media_groups[media_group_id]
    chat_id = group_data.get('chat_id')
    
    if not group_data.get('text'):
        tg_send_message(chat_id, "❌ Отправьте текст новости.\nПервая строка будет заголовком.")
        del media_groups[media_group_id]
        return
    
    media_file_ids = group_data.get('file_ids', [])
    text = group_data.get('text', '')
    
    if not media_file_ids:
        tg_send_message(chat_id, "❌ Нет медиа для публикации.")
        del media_groups[media_group_id]
        return
    
    logger.info(f"📸 Обработка группы {media_group_id}: {len(media_file_ids)} файлов")
    
    title, content = extract_title_and_content(text)
    
    post_key = str(int(time.time() * 1000))
    
    logger.info(f"📸 Загружаю первое фото как обложку...")
    featured_media_id, _ = download_and_upload_media(media_file_ids[0], False, is_thumbnail=False, title=title)
    
    gallery_file_ids = media_file_ids[1:] if len(media_file_ids) > 1 else []
    
    pending_posts[post_key] = {
        'original_text': text,
        'media_file_ids': media_file_ids,
        'gallery_file_ids': gallery_file_ids,
        'is_video': False,
        'title': title,
        'content': content,
        'featured_media_id': featured_media_id
    }
    
    keyboard = get_post_type_keyboard(post_key)
    
    tg_send_message(
        chat_id,
        f"📸 Альбом получен! ({len(media_file_ids)} фото)\n\n"
        f"Заголовок: {title}\n\n"
        f"Текст: {content[:300]}...\n\n"
        f"📂 Выбери раздел для публикации:",
        json.dumps(keyboard)
    )
    
    if media_group_id in media_groups:
        del media_groups[media_group_id]
    if media_group_id in group_timers:
        del group_timers[media_group_id]

def process_update(update_json):
    try:
        if 'callback_query' in update_json:
            callback = update_json['callback_query']
            data = callback['data']
            message = callback['message']
            callback_id = callback['id']
            chat_id = message['chat']['id']
            msg_id = message['message_id']
            
            logger.info(f"🔘 Получен callback: {data}")
            
            tg_answer_callback_query(callback_id)
            
            parts = data.split('|')
            action = parts[0]
            
            if action == 'select_post_type' and len(parts) >= 3:
                post_key = parts[1]
                post_type = parts[2]
                post_data = pending_posts.get(post_key)
                
                if post_data:
                    post_data['post_type'] = post_type
                    section_name = POST_TYPES.get(post_type, post_type)
                    
                    if post_data.get('is_video') and not post_data.get('featured_media_id'):
                        video_pending[post_key] = {
                            'post_key': post_key,
                            'chat_id': chat_id,
                            'msg_id': msg_id
                        }
                        
                        new_text = f"✅ Выбран раздел: {section_name}\n\n"
                        new_text += f"🎬 Видео получено!\n\n"
                        new_text += f"📸 Теперь отправь фото для заглавной (обложки) этого видео.\n\n"
                        new_text += f"Это фото будет использовано как обложка новости.\n\n"
                        new_text += f"Заголовок: {post_data.get('title', 'Без заголовка')}\n\n"
                        new_text += f"Текст: {post_data.get('content', '')[:200]}..."
                        
                        tg_edit_message_text(chat_id, msg_id, new_text)
                    else:
                        keyboard = get_action_keyboard(post_key)
                        media_count = len(post_data.get('media_file_ids', []))
                        media_type = "видео" if post_data.get('is_video') else f"{media_count} фото" if media_count > 0 else "нет"
                        
                        seo_desc = generate_seo_description(
                            post_data.get('title', ''),
                            post_data.get('content', ''),
                            post_type
                        )
                        
                        new_text = f"✅ Выбран раздел: {section_name}\n\n"
                        new_text += f"Заголовок: {post_data.get('title', 'Без заголовка')}\n\n"
                        new_text += f"Текст: {post_data.get('content', '')[:300]}...\n\n"
                        new_text += f"Медиа: {media_type}\n\n"
                        new_text += f"🔍 SEO-описание: {seo_desc}\n\n"
                        new_text += "Выбери действие:"
                        
                        tg_edit_message_text(chat_id, msg_id, new_text, json.dumps(keyboard))
                return
            
            if action == 'schedule' and len(parts) >= 3:
                post_key = parts[1]
                minutes = int(parts[2])
                post_data = pending_posts.get(post_key)
                
                if not post_data:
                    tg_edit_message_text(chat_id, msg_id, "❌ Пост не найден.")
                    return
                
                if not post_data.get('post_type'):
                    tg_edit_message_text(chat_id, msg_id, "❌ Раздел не выбран.")
                    return
                
                schedule_time = datetime.now() + timedelta(minutes=minutes)
                time_str = schedule_time.strftime('%d.%m.%Y %H:%M')
                
                scheduled_posts[post_key] = {
                    'title': post_data['title'],
                    'content': post_data['content'],
                    'post_type': post_data['post_type'],
                    'gallery_file_ids': post_data.get('gallery_file_ids', []),
                    'is_video': post_data.get('is_video', False),
                    'chat_id': chat_id,
                    'msg_id': msg_id,
                    'featured_media_id': post_data.get('featured_media_id'),
                    'video_file_id': post_data.get('video_file_id')
                }
                
                timer = threading.Timer(minutes * 60, publish_scheduled_post, args=[post_key])
                scheduled_timers[post_key] = timer
                timer.start()
                
                del pending_posts[post_key]
                
                tg_edit_message_text(
                    chat_id, msg_id,
                    f"✅ Пост запланирован!\n\n"
                    f"⏰ Публикация: {time_str}\n"
                    f"📂 Раздел: {POST_TYPES.get(post_data['post_type'], post_data['post_type'])}\n"
                    f"📝 Заголовок: {post_data['title']}\n\n"
                    f"🕐 Через {minutes} минут пост будет опубликован автоматически."
                )
                
                logger.info(f"⏰ Пост {post_key} запланирован на {time_str}")
                return
            
            if action == 'ai' and len(parts) >= 2:
                post_key = parts[1]
                post_data = pending_posts.get(post_key)
                
                if post_data:
                    tg_edit_message_text(chat_id, msg_id, "🤖 Обрабатываю текст через ИИ...")
                    processed = process_text_with_deepseek(post_data['original_text'])
                    
                    if processed:
                        title, content = extract_title_and_content(processed)
                        post_data['title'] = title
                        post_data['content'] = content
                        
                        keyboard = get_action_keyboard(post_key)
                        
                        media_count = len(post_data.get('media_file_ids', []))
                        media_type = "видео" if post_data.get('is_video') else f"{media_count} фото" if media_count > 0 else "нет"
                        
                        seo_desc = generate_seo_description(
                            title,
                            content,
                            post_data.get('post_type', 'news')
                        )
                        
                        tg_edit_message_text(
                            chat_id, msg_id,
                            f"Заголовок: {title}\n\nТекст: {content}\n\nМедиа: {media_type}\n\n🔍 SEO-описание: {seo_desc}",
                            json.dumps(keyboard)
                        )
                    else:
                        tg_edit_message_text(chat_id, msg_id, "❌ Ошибка ИИ")
                return
            
            if action == 'publish' and len(parts) >= 2:
                post_key = parts[1]
                post_data = pending_posts.get(post_key)
                
                if not post_data:
                    tg_edit_message_text(chat_id, msg_id, "❌ Пост не найден.")
                    return
                
                if not post_data.get('post_type'):
                    tg_edit_message_text(chat_id, msg_id, "❌ Раздел не выбран.")
                    return
                
                tg_edit_message_text(chat_id, msg_id, "⏳ Публикую на сайт...")
                
                is_video = post_data.get('is_video', False)
                featured_media_id = post_data.get('featured_media_id')
                video_file_id = post_data.get('video_file_id')
                gallery_file_ids = post_data.get('gallery_file_ids', [])
                title = post_data.get('title', '')
                post_type = post_data.get('post_type', 'news')
                content = post_data.get('content', '')
                
                video_url = None
                gallery_ids = []
                
                if is_video and video_file_id:
                    logger.info(f"🎬 Загрузка видео...")
                    media_id, video_url = download_and_upload_media(video_file_id, True, is_thumbnail=False, title=title)
                    if media_id:
                        logger.info(f"✅ Видео загружено, URL={video_url}")
                    else:
                        logger.error("❌ Не удалось загрузить видео")
                        tg_edit_message_text(chat_id, msg_id, "❌ Ошибка загрузки видео")
                        return
                
                for file_id in gallery_file_ids:
                    if file_id != featured_media_id:
                        logger.info(f"📸 Загрузка фото для галереи...")
                        media_id, _ = download_and_upload_media(file_id, False, is_thumbnail=False, title=title)
                        if media_id:
                            gallery_ids.append(media_id)
                            logger.info(f"✅ Фото загружено, ID={media_id}")
                
                success, link = create_wp_post(
                    title,
                    content,
                    post_type,
                    featured_media_id,
                    video_url,
                    True,
                    is_video,
                    gallery_ids if gallery_ids else None,
                    None
                )
                
                if success:
                    tg_edit_message_text(chat_id, msg_id, f"✅ Пост опубликован!\n\n{link}")
                else:
                    tg_edit_message_text(chat_id, msg_id, "❌ Ошибка публикации")
                
                if post_key in video_pending:
                    del video_pending[post_key]
                
                del pending_posts[post_key]
                return
            
            if action == 'draft' and len(parts) >= 2:
                post_key = parts[1]
                post_data = pending_posts.get(post_key)
                
                if not post_data:
                    tg_edit_message_text(chat_id, msg_id, "❌ Пост не найден.")
                    return
                
                if not post_data.get('post_type'):
                    tg_edit_message_text(chat_id, msg_id, "❌ Раздел не выбран.")
                    return
                
                tg_edit_message_text(chat_id, msg_id, "⏳ Сохраняю в черновики...")
                
                is_video = post_data.get('is_video', False)
                featured_media_id = post_data.get('featured_media_id')
                video_file_id = post_data.get('video_file_id')
                gallery_file_ids = post_data.get('gallery_file_ids', [])
                title = post_data.get('title', '')
                post_type = post_data.get('post_type', 'news')
                content = post_data.get('content', '')
                
                video_url = None
                gallery_ids = []
                
                if is_video and video_file_id:
                    logger.info(f"🎬 Загрузка видео...")
                    media_id, video_url = download_and_upload_media(video_file_id, True, is_thumbnail=False, title=title)
                    if media_id:
                        logger.info(f"✅ Видео загружено, URL={video_url}")
                    else:
                        logger.error("❌ Не удалось загрузить видео")
                        tg_edit_message_text(chat_id, msg_id, "❌ Ошибка загрузки видео")
                        return
                
                for file_id in gallery_file_ids:
                    if file_id != featured_media_id:
                        logger.info(f"📸 Загрузка фото...")
                        media_id, _ = download_and_upload_media(file_id, False, is_thumbnail=False, title=title)
                        if media_id:
                            gallery_ids.append(media_id)
                            logger.info(f"✅ Фото загружено, ID={media_id}")
                
                success, link = create_wp_post(
                    title,
                    content,
                    post_type,
                    featured_media_id,
                    video_url,
                    False,
                    is_video,
                    gallery_ids if gallery_ids else None,
                    None
                )
                
                if success:
                    tg_edit_message_text(chat_id, msg_id, f"✅ Пост сохранен в черновиках!\n\n{link}")
                else:
                    tg_edit_message_text(chat_id, msg_id, "❌ Ошибка сохранения")
                
                if post_key in video_pending:
                    del video_pending[post_key]
                
                del pending_posts[post_key]
                return
        
        elif 'message' in update_json:
            message = update_json['message']
            chat_id = message['chat']['id']
            user_id = message['from']['id']
            
            if str(user_id) != ADMIN_ID:
                tg_send_message(chat_id, "❌ У вас нет прав.")
                return
            
            text = message.get('caption') or message.get('text', '')
            media_group_id = message.get('media_group_id')
            
            has_photo = 'photo' in message
            has_video = 'video' in message
            
            if media_group_id and has_photo:
                photos = message['photo']
                if photos and len(photos) > 0:
                    file_id = photos[-1]['file_id']
                    
                    if media_group_id not in media_groups:
                        media_groups[media_group_id] = {
                            'file_ids': [],
                            'text': '',
                            'chat_id': chat_id
                        }
                    
                    if file_id not in media_groups[media_group_id]['file_ids']:
                        media_groups[media_group_id]['file_ids'].append(file_id)
                    
                    if text and not media_groups[media_group_id]['text']:
                        media_groups[media_group_id]['text'] = text
                    
                    logger.info(f"📸 Добавлено фото в группу {media_group_id}, всего {len(media_groups[media_group_id]['file_ids'])} фото")
                    
                    if media_group_id in group_timers:
                        group_timers[media_group_id].cancel()
                    
                    timer = threading.Timer(3.0, process_media_group, args=[media_group_id])
                    group_timers[media_group_id] = timer
                    timer.start()
                    
                    logger.info(f"⏳ Запущен таймер для группы {media_group_id} на 3 секунды")
                return
            
            if has_photo and not media_group_id:
                video_key = None
                for key in list(video_pending.keys()):
                    if video_pending[key].get('chat_id') == chat_id:
                        video_key = key
                        break
                
                if video_key:
                    photos = message['photo']
                    if photos and len(photos) > 0:
                        file_id = photos[-1]['file_id']
                        
                        pending_data = video_pending[video_key]
                        post_key = pending_data['post_key']
                        post_data = pending_posts.get(post_key, {})
                        title = post_data.get('title', 'Видео')
                        
                        logger.info(f"📸 Загружаю фото обложки в WordPress...")
                        featured_media_id, thumbnail_url = download_and_upload_media(
                            file_id, 
                            False, 
                            is_thumbnail=True,
                            title=title,
                            alt_text=f"Обложка: {title}"
                        )
                        
                        if featured_media_id:
                            msg_id = pending_data.get('msg_id')
                            
                            if post_key in pending_posts:
                                pending_posts[post_key]['featured_media_id'] = featured_media_id
                                logger.info(f"📸 Получено фото для обложки, WP ID={featured_media_id}")
                                
                                keyboard = get_action_keyboard(post_key)
                                post_data = pending_posts[post_key]
                                section_name = POST_TYPES.get(post_data.get('post_type'), 'Не выбран')
                                
                                seo_desc = generate_seo_description(
                                    post_data.get('title', ''),
                                    post_data.get('content', ''),
                                    post_data.get('post_type', 'news')
                                )
                                
                                new_text = f"✅ Фото для обложки получено!\n\n"
                                new_text += f"🎬 Видео с обложкой\n\n"
                                new_text += f"✅ Выбран раздел: {section_name}\n\n"
                                new_text += f"Заголовок: {post_data.get('title', 'Без заголовка')}\n\n"
                                new_text += f"Текст: {post_data.get('content', '')[:200]}...\n\n"
                                new_text += f"🔍 SEO-описание: {seo_desc}\n\n"
                                new_text += "Выбери действие:"
                                
                                if msg_id:
                                    tg_edit_message_text(chat_id, msg_id, new_text, json.dumps(keyboard))
                                else:
                                    tg_send_message(chat_id, new_text, json.dumps(keyboard))
                                
                                del video_pending[video_key]
                            else:
                                tg_send_message(chat_id, "❌ Пост не найден.")
                                del video_pending[video_key]
                        else:
                            tg_send_message(chat_id, "❌ Не удалось загрузить фото обложки.")
                    return
                else:
                    photos = message['photo']
                    if photos and len(photos) > 0:
                        file_id = photos[-1]['file_id']
                        
                        if text:
                            title, content = extract_title_and_content(text)
                            
                            logger.info(f"📸 Загружаю фото как обложку...")
                            featured_media_id, _ = download_and_upload_media(
                                file_id, 
                                False, 
                                is_thumbnail=False,
                                title=title,
                                alt_text=title
                            )
                            
                            if featured_media_id:
                                post_key = str(int(time.time() * 1000))
                                pending_posts[post_key] = {
                                    'original_text': text,
                                    'media_file_ids': [file_id],
                                    'gallery_file_ids': [],
                                    'is_video': False,
                                    'title': title,
                                    'content': content,
                                    'featured_media_id': featured_media_id
                                }
                                
                                keyboard = get_post_type_keyboard(post_key)
                                
                                tg_send_message(
                                    chat_id,
                                    f"📢 Пост получен!\n\n"
                                    f"Заголовок: {title}\n\n"
                                    f"Текст: {content[:300]}...\n\n"
                                    f"📸 Фото загружено как обложка\n\n"
                                    f"📂 Выбери раздел для публикации:",
                                    json.dumps(keyboard)
                                )
                            else:
                                tg_send_message(chat_id, "❌ Не удалось загрузить фото.")
                        else:
                            tg_send_message(chat_id, "❌ Отправьте текст новости.\nПервая строка будет заголовком.")
                    return
            
            if has_video:
                video_file_id = message['video']['file_id']
                logger.info("🎬 Обнаружено ВИДЕО")
                
                if text:
                    title, content = extract_title_and_content(text)
                    
                    post_key = str(int(time.time() * 1000))
                    pending_posts[post_key] = {
                        'original_text': text,
                        'media_file_ids': [video_file_id],
                        'gallery_file_ids': [],
                        'is_video': True,
                        'title': title,
                        'content': content,
                        'featured_media_id': None,
                        'video_file_id': video_file_id
                    }
                    
                    keyboard = get_post_type_keyboard(post_key)
                    tg_send_message(
                        chat_id,
                        f"🎬 Видео получено!\n\n"
                        f"Заголовок: {title}\n\n"
                        f"Текст: {content[:300]}...\n\n"
                        f"📂 Сначала выбери раздел для публикации:",
                        json.dumps(keyboard)
                    )
                    return
                else:
                    tg_send_message(chat_id, "❌ Отправьте текст новости к видео.\nПервая строка будет заголовком.")
                    return
            
    except Exception as e:
        logger.error(f"Ошибка: {e}")
        import traceback
        traceback.print_exc()

@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        json_data = request.get_json(force=True)
        logger.info("🔔 Вебхук получен")
        process_update(json_data)
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
    logger.info(f"📢 Канал: {CHANNEL_ID}")
    logger.info(f"👤 Админ ID: {ADMIN_ID}")
    logger.info(f"🤖 DeepSeek: {'✅' if DEEPSEEK_API_KEY else '❌'}")
    
    app.run(host='0.0.0.0', port=5000)
