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
import numpy as np

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

# Рубрики для каждого раздела с ID
CATEGORIES = {
    "news": {
        "v-mire": {"name": "🌍 В мире", "id": 32},
        "vlasti": {"name": "🏛️ Власти", "id": 31},
        "city": {"name": "🏙️ Город", "id": 27},
        "dengi": {"name": "💰 Деньги", "id": 30},
        "zakon": {"name": "⚖️ Закон", "id": 87},
        "proisshestviya": {"name": "🚨 Происшествия", "id": 29}
    },
    "sport": {
        "edinoborstva": {"name": "🥊 Единоборства", "id": None},
        "zimnie_vidy": {"name": "⛷️ Зимние виды", "id": None},
        "mirovoy_sport": {"name": "🌍 Мировой спорт", "id": None},
        "sbornaya_belarusi": {"name": "🇧🇾 Сборная Беларуси", "id": None},
        "tennis": {"name": "🎾 Теннис", "id": None},
        "futbol": {"name": "⚽ Футбол", "id": None},
        "hokkey": {"name": "🏒 Хоккей", "id": None}
    },
    "realt": {
        "za_gorodom": {"name": "🌳 За городом", "id": None},
        "kredity": {"name": "🏦 Кредиты", "id": None},
        "novostroyki": {"name": "🏗️ Новостройки", "id": None},
        "obzory": {"name": "📋 Обзоры", "id": None},
        "remont": {"name": "🔨 Ремонт", "id": None}
    },
    "auto": {
        "avarii-i-dtp": {"name": "🚗 Аварии и ДТП", "id": None},
        "avtorynok": {"name": "🏪 Авторынок", "id": None},
        "pdd": {"name": "📜 ПДД", "id": None},
        "test-drayvy": {"name": "🚘 Тест-драйвы и обзоры", "id": None}
    },
    "afisha": {
        "vecherinki": {"name": "🎉 Вечеринки", "id": 70},
        "vystavki": {"name": "🖼️ Выставки", "id": 104},
        "vyhodnye": {"name": "📅 Выходные", "id": None},
        "detskaya_afisha": {"name": "🧒 Детская афиша", "id": None},
        "kvesty": {"name": "🔍 Квесты", "id": None},
        "kino": {"name": "🎬 Кино", "id": None},
        "koncerty": {"name": "🎵 Концерты", "id": None},
        "master-klassy": {"name": "🎨 Мастер-классы", "id": None},
        "obzory": {"name": "📋 Обзоры", "id": None},
        "obuchenie": {"name": "📚 Обучение", "id": None},
        "rekomendacii": {"name": "💡 Рекомендации", "id": None},
        "sobytiya": {"name": "📅 События", "id": None},
        "spektakli": {"name": "🎭 Спектакли", "id": None},
        "standap": {"name": "🎤 Стендап", "id": None},
        "festivali": {"name": "🎪 Фестивали", "id": None},
        "ekskursii": {"name": "🏛️ Экскурсии", "id": None}
    },
    "sales": {
        "buklety": {"name": "📰 Буклеты", "id": None},
        "novinki": {"name": "✨ Новинки", "id": None},
        "obzory": {"name": "📋 Обзоры", "id": None},
        "skidki": {"name": "🏷️ Скидки", "id": None}
    }
}

# Маппинг таксономий для каждого раздела
TAXONOMY_MAP = {
    "news": "news_category",
    "sport": "sport_category",
    "realt": "realt_category",
    "auto": "auto_category",
    "afisha": "afisha_category",
    "sales": "sales_category"
}

app = Flask(__name__)
wp_session = requests.Session()

# Хранилища
pending_posts = {}
media_groups = defaultdict(dict)
group_timers = {}
scheduled_posts = {}
scheduled_timers = {}
video_awaiting_photo = {}
telegram_preview = {}

# Базовый URL для Telegram API
TG_API_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

# ОБНОВЛЕННЫЙ ПРОМПТ: заголовок не более 150-200 символов
DEEPSEEK_PROMPT = """Ты редактор новостного сайта. Перепиши новость в строгом городском формате, объемом около 650 символов. Убери лишнюю воду, сделай интересный заголовок (НЕ БОЛЕЕ 150-200 СИМВОЛОВ), никаких смайликов. Не используй символы # и ** в ответе. Сохрани главные факты. Расставь абзацы.

ВАЖНО: НЕ пиши слова "Заголовок:" и "Текст:". Просто напиши сначала заголовок, потом пустую строку, потом текст."""

TELEGRAM_SHORT_PROMPT = """Напиши краткую версию новости ровно на 500 символов. Сохрани все главные факты и суть. Текст должен быть связным, логичным и заканчиваться законченной мыслью.

Важно:
- Ровно 500 символов
- Без троеточия
- Без смайликов
- Без символов # и **
- Без слов "Заголовок:" и "Текст:"
- Только готовый текст"""

TELEGRAM_REWRITE_PROMPT = """Перепиши этот текст для Telegram-канала по-другому, сохранив все главные факты и суть. Сделай текст ровно 500 символов. Он должен быть связным, логичным и заканчиваться законченной мыслью.

Важно:
- Ровно 500 символов
- Без троеточия
- Без смайликов
- Без символов # и **
- Другой стиль изложения
- Только готовый текст

Текст для переписывания:"""

# Словарь смайликов по тематикам
EMOJI_CATEGORIES = {
    "политика": "🏛️",
    "власть": "🏛️",
    "правительство": "🏛️",
    "президент": "👤",
    "выборы": "🗳️",
    "закон": "⚖️",
    "суд": "⚖️",
    "деньги": "💰",
    "финансы": "💰",
    "экономика": "📊",
    "бюджет": "💰",
    "цена": "💲",
    "курс": "💱",
    "происшествие": "🚨",
    "авария": "🚗💥",
    "дтп": "🚗💥",
    "пожар": "🔥",
    "взрыв": "💥",
    "чп": "⚠️",
    "город": "🏙️",
    "стройка": "🏗️",
    "ремонт": "🔨",
    "дорога": "🛣️",
    "транспорт": "🚌",
    "метро": "🚇",
    "здоровье": "💊",
    "медицина": "🏥",
    "больница": "🏥",
    "спорт": "⚽",
    "футбол": "⚽",
    "хоккей": "🏒",
    "теннис": "🎾",
    "бокс": "🥊",
    "культура": "🎭",
    "искусство": "🎨",
    "выставка": "🖼️",
    "музей": "🏛️",
    "театр": "🎭",
    "кино": "🎬",
    "концерт": "🎵",
    "фестиваль": "🎪",
    "школа": "🏫",
    "университет": "🎓",
    "образование": "📚",
    "наука": "🔬",
    "технологии": "💻",
    "интернет": "🌐",
    "погода": "🌤️",
    "дождь": "🌧️",
    "снег": "❄️",
    "солнце": "☀️",
    "новость": "📰",
    "событие": "📅",
    "выходной": "🎉",
    "праздник": "🎊",
}

def get_emoji_for_text(text):
    """Определяет подходящий смайлик для текста новости"""
    try:
        text_lower = text.lower()
        found_emojis = []
        for keyword, emoji in EMOJI_CATEGORIES.items():
            if keyword in text_lower:
                found_emojis.append(emoji)
                logger.info(f"🔍 Найдено ключевое слово '{keyword}' -> смайлик {emoji}")
        
        if found_emojis:
            return found_emojis[0]
        
        logger.info("🤖 Использую ИИ для определения смайлика")
        emoji_prompt = f"""Определи один подходящий смайлик (эмодзи) для этой новости. Ответь только смайликом, без пояснений.

Текст новости:
{text[:500]}"""

        response = requests.post(
            DEEPSEEK_API_URL,
            headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": "deepseek-chat",
                "messages": [
                    {"role": "system", "content": "Ты помощник. Определи один подходящий смайлик для текста. Ответь только смайликом."},
                    {"role": "user", "content": emoji_prompt}
                ],
                "temperature": 0.3,
                "max_tokens": 50
            },
            timeout=30
        )
        
        if response.status_code == 200:
            emoji = response.json()["choices"][0]["message"]["content"].strip()
            if emoji and len(emoji) <= 2:
                logger.info(f"✅ ИИ определил смайлик: {emoji}")
                return emoji
        
        return "📰"
        
    except Exception as e:
        logger.error(f"❌ Ошибка определения смайлика: {e}")
        return "📰"

# ============ ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ============

def get_channel_id():
    """Получает правильный ID канала"""
    try:
        if str(CHANNEL_ID).startswith('-100'):
            return CHANNEL_ID
        
        url = f"{TG_API_URL}/getChat"
        params = {'chat_id': CHANNEL_ID}
        response = requests.get(url, params=params, timeout=30)
        
        if response.status_code == 200:
            chat_data = response.json().get('result', {})
            chat_id = chat_data.get('id')
            logger.info(f"✅ Получен ID канала: {chat_id}")
            return chat_id
        else:
            logger.error(f"❌ Ошибка получения ID канала: {response.status_code}")
            return CHANNEL_ID
    except Exception as e:
        logger.error(f"❌ Ошибка получения ID канала: {e}")
        return CHANNEL_ID

def extract_title_and_content(text):
    """Извлекает заголовок и контент из текста"""
    if not text:
        return "Новый пост из Telegram", ""
    lines = text.strip().split('\n')
    title = lines[0].strip() if lines else "Новый пост"
    # Ограничиваем заголовок 200 символами
    if len(title) > 200:
        title = title[:197] + "..."
    content = '\n'.join(lines[1:]).strip() if len(lines) > 1 else ""
    return title, content

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

def tg_answer_callback_query(callback_id):
    url = f"{TG_API_URL}/answerCallbackQuery"
    return requests.post(url, json={'callback_query_id': callback_id}, timeout=30)

def get_category_id(post_type, category_slug):
    """Получить ID категории из словаря"""
    categories = CATEGORIES.get(post_type, {})
    if category_slug in categories:
        category_id = categories[category_slug].get('id')
        if category_id:
            logger.info(f"📂 Найдена рубрика: {category_slug} (ID: {category_id})")
            return category_id
        else:
            logger.warning(f"⚠️ Для рубрики {category_slug} не указан ID")
    else:
        logger.warning(f"⚠️ Рубрика {category_slug} не найдена в словаре")
    return None

def set_post_categories(post_id, post_type, category_ids):
    """Установка категорий для поста через правильную таксономию"""
    try:
        taxonomy = TAXONOMY_MAP.get(post_type, "category")
        logger.info(f"📂 Устанавливаю рубрики для поста {post_id}")
        logger.info(f"   Таксономия: {taxonomy}")
        logger.info(f"   ID рубрик: {category_ids}")
        
        success_count = 0
        for cat_id in category_ids:
            try:
                # Используем правильный эндпоинт для терминов таксономии
                term_url = f"{WP_URL}/wp-json/wp/v2/{taxonomy}/{cat_id}"
                term_data = {'post': post_id}
                
                logger.info(f"📤 Отправка запроса к {term_url}")
                term_response = wp_session.post(
                    term_url,
                    auth=(WP_USERNAME, WP_PASSWORD),
                    json=term_data,
                    timeout=30
                )
                
                if term_response.status_code in [200, 201]:
                    logger.info(f"✅ Рубрика {cat_id} успешно добавлена")
                    success_count += 1
                else:
                    logger.warning(f"⚠️ Ошибка добавления рубрики {cat_id}: {term_response.status_code}")
                    logger.warning(f"Ответ: {term_response.text[:200]}")
            except Exception as e:
                logger.error(f"❌ Ошибка добавления рубрики {cat_id}: {e}")
        
        return success_count > 0
            
    except Exception as e:
        logger.error(f"❌ Ошибка установки рубрик: {e}")
        return False

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
        
        return description
        
    except Exception as e:
        logger.error(f"❌ Ошибка генерации SEO-описания: {e}")
        return f"{title[:140]}..."

def unique_image(image_bytes, is_video_thumbnail=False):
    """Уникализация изображения с добавлением шума 20%"""
    try:
        image = Image.open(io.BytesIO(image_bytes))
        
        if image.mode in ('RGBA', 'LA', 'P'):
            image = image.convert('RGB')
        
        # Добавляем шум 20%
        logger.info("📸 Добавляем 20% шума к изображению")
        img_array = np.array(image)
        noise = np.random.normal(0, 0.2 * 255, img_array.shape)  # 20% шума
        noisy_array = np.clip(img_array + noise, 0, 255).astype(np.uint8)
        image = Image.fromarray(noisy_array)
        
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
        
        logger.info(f"✅ Фото уникализировано с шумом 20%: {len(unique_bytes)} байт")
        return unique_bytes
        
    except Exception as e:
        logger.error(f"❌ Ошибка уникализации: {e}")
        return image_bytes

def download_and_upload_photo(file_id, is_video=False, is_thumbnail=False, title=None, alt_text=None):
    """Загрузка фото из Telegram в WordPress"""
    try:
        media_type = "видео" if is_video else "фото"
        logger.info(f"📸 НАЧАЛО ЗАГРУЗКИ {media_type.upper()}: file_id={file_id}")
        
        get_file_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile"
        file_response = requests.get(get_file_url, params={'file_id': file_id}, timeout=60)
        
        if file_response.status_code != 200:
            logger.error(f"❌ Ошибка getFile: {file_response.status_code}")
            return None
        
        result = file_response.json().get('result')
        if not result:
            logger.error("❌ Не получен result от Telegram")
            return None
        
        file_path = result.get('file_path')
        if not file_path:
            logger.error("❌ Не получен file_path")
            return None
        
        logger.info(f"✅ file_path получен: {file_path}")
        
        media_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"
        logger.info(f"📸 Скачиваю {media_type}...")
        
        download_headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        
        media_response = requests.get(media_url, headers=download_headers, timeout=120)
        if media_response.status_code != 200:
            logger.error(f"❌ Ошибка скачивания {media_type}: {media_response.status_code}")
            return None
        
        media_content = media_response.content
        logger.info(f"✅ {media_type.capitalize()} скачано, размер: {len(media_content)} байт")
        
        if not is_video:
            is_video_thumbnail = is_thumbnail
            media_content = unique_image(media_content, is_video_thumbnail)
            logger.info(f"✅ Фото уникализировано с шумом 20%, новый размер: {len(media_content)} байт")
        
        ext = 'mp4' if is_video else 'jpg'
        mime = 'video/mp4' if is_video else 'image/jpeg'
        
        if title:
            clean_title = re.sub(r'[^\w\s-]', '', title)
            clean_title = re.sub(r'[-\s]+', '-', clean_title)
            clean_title = clean_title[:100]
            clean_title = re.sub(r'[^a-zA-Z0-9\-]', '', clean_title)
            if not clean_title:
                clean_title = f"media_{int(time.time())}"
            filename = f"{clean_title}_{int(time.time())}.{ext}"
        else:
            filename = f'media_{int(time.time())}.{ext}'
        
        logger.info(f"📸 Загружаю {media_type} в WordPress как: {filename}")
        
        wp_headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'application/json',
            'Accept-Language': 'ru-RU,ru;q=0.9,en;q=0.8',
            'Content-Type': mime,
            'Content-Disposition': f'attachment; filename="{filename}"'
        }
        
        wp_response = wp_session.post(
            WP_MEDIA_URL,
            auth=(WP_USERNAME, WP_PASSWORD),
            headers=wp_headers,
            data=media_content,
            timeout=120
        )
        
        if wp_response.status_code == 201:
            media_id = wp_response.json()['id']
            source_url = wp_response.json().get('source_url', 'unknown')
            logger.info(f"✅ {media_type.capitalize()} загружено! ID={media_id}, URL={source_url}")
            
            if title:
                try:
                    meta_data = {
                        'title': title[:100],
                        'alt_text': title[:100],
                        'caption': '',
                        'description': f"Изображение к статье: {title[:100]}"
                    }
                    meta_response = wp_session.post(
                        f"{WP_MEDIA_URL}/{media_id}",
                        auth=(WP_USERNAME, WP_PASSWORD),
                        json=meta_data,
                        timeout=30
                    )
                    if meta_response.status_code == 200:
                        logger.info(f"✅ Метаданные медиа обновлены")
                except Exception as e:
                    logger.warning(f"⚠️ Ошибка обновления метаданных: {e}")
            
            return media_id
        else:
            logger.error(f"❌ Ошибка WP при загрузке {media_type}: {wp_response.status_code}")
            logger.error(f"Ответ: {wp_response.text[:200]}")
            return None
            
    except Exception as e:
        logger.error(f"❌ Ошибка загрузки медиа: {e}")
        return None

def format_content_for_wp(text, video_url=None, gallery_ids=None, is_video=False):
    """Форматирование контента для WordPress с вставкой видео или галереи"""
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
                    logger.info(f"🖼️ Добавлена галерея из {len(gallery_ids)} фото после 1-го абзаца")
    
    return '\n'.join(formatted)

def process_text_with_deepseek(text, prompt_type='full'):
    """Обработка текста через DeepSeek"""
    if not DEEPSEEK_API_KEY:
        return None
    try:
        if prompt_type == 'telegram':
            prompt = TELEGRAM_SHORT_PROMPT
            system_prompt = "Ты редактор новостного канала в Telegram. Напиши краткую версию новости ровно 500 символов. Ответь только готовым текстом."
            max_tokens = 700
        elif prompt_type == 'telegram_rewrite':
            prompt = TELEGRAM_REWRITE_PROMPT
            system_prompt = "Ты редактор новостного канала в Telegram. Перепиши текст по-другому, ровно 500 символов. Ответь только готовым текстом."
            max_tokens = 700
        else:
            prompt = DEEPSEEK_PROMPT
            system_prompt = "Ты редактор новостного сайта. Отвечай только готовым новостным текстом, без пояснений и вступлений. Заголовок должен быть не более 150-200 символов."
            max_tokens = 1000
        
        response = requests.post(
            DEEPSEEK_API_URL,
            headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": "deepseek-chat",
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": f"{prompt}\n\n{text}"}
                ],
                "temperature": 0.7 if prompt_type == 'telegram_rewrite' else 0.5,
                "max_tokens": max_tokens
            },
            timeout=60
        )
        if response.status_code == 200:
            result = response.json()["choices"][0]["message"]["content"]
            result = re.sub(r'^Вот обработанный новостной текст.*?:', '', result, flags=re.IGNORECASE)
            result = re.sub(r'^Вот.*?текст.*?:', '', result, flags=re.IGNORECASE)
            result = re.sub(r'^#+\s+', '', result, flags=re.MULTILINE)
            result = result.strip()
            
            # Для сайта проверяем длину заголовка
            if prompt_type == 'full':
                lines = result.split('\n')
                if lines:
                    title = lines[0].strip()
                    if len(title) > 200:
                        logger.info(f"📏 Заголовок {len(title)} символов, сокращаю до 200")
                        title = title[:197] + "..."
                        result = title + '\n' + '\n'.join(lines[1:])
            
            # Для Telegram проверяем длину
            if prompt_type in ['telegram', 'telegram_rewrite']:
                if len(result) > 520:
                    logger.info(f"📏 Текст {len(result)} символов, прошу ИИ сократить до 500")
                    retry_prompt = f"""Сократи следующий текст ровно до 500 символов. Сохрани все главные факты и суть. Текст должен быть законченным и логичным.

Текст для сокращения:
{result}"""
                    retry_response = requests.post(
                        DEEPSEEK_API_URL,
                        headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"},
                        json={
                            "model": "deepseek-chat",
                            "messages": [
                                {"role": "system", "content": "Ты редактор. Сократи текст ровно до 500 символов. Ответь только готовым текстом."},
                                {"role": "user", "content": retry_prompt}
                            ],
                            "temperature": 0.5,
                            "max_tokens": 600
                        },
                        timeout=60
                    )
                    if retry_response.status_code == 200:
                        result = retry_response.json()["choices"][0]["message"]["content"].strip()
                        result = re.sub(r'^#+\s+', '', result, flags=re.MULTILINE)
                        result = result.strip()
                
                if len(result) > 500:
                    cut_pos = result[:500].rfind('.')
                    if cut_pos > 450:
                        result = result[:cut_pos + 1]
                    else:
                        cut_pos = result[:500].rfind(' ')
                        if cut_pos > 450:
                            result = result[:cut_pos]
                        else:
                            result = result[:500]
                elif len(result) < 480:
                    logger.info(f"📏 Текст {len(result)} символов, прошу ИИ дополнить до 500")
                    retry_prompt = f"""Дополни следующий текст до 500 символов, сохранив стиль и смысл. Добавь важные детали.

Текст для дополнения:
{result}"""
                    retry_response = requests.post(
                        DEEPSEEK_API_URL,
                        headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"},
                        json={
                            "model": "deepseek-chat",
                            "messages": [
                                {"role": "system", "content": "Ты редактор. Дополни текст до 500 символов. Ответь только готовым текстом."},
                                {"role": "user", "content": retry_prompt}
                            ],
                            "temperature": 0.5,
                            "max_tokens": 600
                        },
                        timeout=60
                    )
                    if retry_response.status_code == 200:
                        result = retry_response.json()["choices"][0]["message"]["content"].strip()
                        result = re.sub(r'^#+\s+', '', result, flags=re.MULTILINE)
                        result = result.strip()
                        if len(result) > 500:
                            result = result[:500]
            
            return result
        return None
    except Exception as e:
        logger.error(f"Ошибка DeepSeek: {e}")
        return None

def create_wp_post(title, content, post_type, category_slug=None, media_id=None, publish=False, video_url=None, is_video=False, gallery_ids=None, schedule_time=None):
    """Создание поста в WordPress с правильной установкой рубрики"""
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
    
    # Получаем ID категории и добавляем в post_data
    category_id = None
    if category_slug:
        category_id = get_category_id(post_type, category_slug)
        if category_id:
            # Важно: для произвольных таксономий нужно использовать поле с именем таксономии
            taxonomy = TAXONOMY_MAP.get(post_type, "category")
            # Используем поле с именем таксономии (например, 'news_category' для новостей)
            post_data[taxonomy] = [category_id]
            logger.info(f"📂 Добавляю рубрику ID={category_id} в поле {taxonomy}")
        else:
            logger.warning(f"⚠️ Рубрика {category_slug} не найдена")
    
    if schedule_time:
        post_data['date'] = schedule_time.isoformat()
        logger.info(f"⏰ Запланирована публикация на {schedule_time.strftime('%d.%m.%Y %H:%M')}")
    
    if media_id and isinstance(media_id, int):
        post_data['featured_media'] = media_id
        logger.info(f"📎 Устанавливаю обложку WP ID={media_id}")
    
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'application/json',
            'Content-Type': 'application/json',
        }
        
        logger.info(f"📤 Отправка в WordPress: раздел={post_type}, статус={status}")
        logger.info(f"📂 Категория: {category_slug} (ID: {category_id})")
        logger.info(f"📂 Таксономия: {TAXONOMY_MAP.get(post_type, 'category')}")
        logger.info(f"🔍 SEO Заголовок: {seo_title}")
        logger.info(f"🔍 SEO Описание: {seo_description[:100]}...")
        
        response = wp_session.post(
            f"{WP_API_URL}/{post_type}",
            auth=(WP_USERNAME, WP_PASSWORD),
            json=post_data,
            headers=headers,
            timeout=120
        )
        
        if response.status_code == 201:
            post_id = response.json()['id']
            post_link = response.json()['link']
            logger.info(f"✅ Пост создан: {post_link} (ID: {post_id})")
            
            # Проверяем, что категория установлена
            if category_slug and category_id:
                taxonomy = TAXONOMY_MAP.get(post_type, "category")
                check_response = wp_session.get(
                    f"{WP_API_URL}/{post_type}/{post_id}",
                    auth=(WP_USERNAME, WP_PASSWORD),
                    timeout=30
                )
                if check_response.status_code == 200:
                    post_check = check_response.json()
                    categories = post_check.get(taxonomy, [])
                    if category_id in categories:
                        logger.info(f"✅ Категория {category_id} успешно установлена в таксономии {taxonomy}!")
                    else:
                        logger.warning(f"⚠️ Категория не установлена. Текущие категории в {taxonomy}: {categories}")
                        # Пробуем установить повторно
                        logger.info(f"🔄 Пробую установить категорию повторно...")
                        set_post_categories(post_id, post_type, [category_id])
            
            logger.info(f"✅ SEO данные добавлены в Yoast")
            return True, post_link, post_id
        else:
            logger.error(f"❌ Ошибка: {response.status_code}")
            logger.error(f"Ответ: {response.text[:500]}")
            return False, None, None
            
    except Exception as e:
        logger.error(f"❌ Ошибка: {e}")
        return False, None, None

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

# ============ ФУНКЦИИ ДЛЯ TELEGRAM ============

def clean_html_for_telegram(text):
    """Очищает HTML теги и форматирует текст для Telegram"""
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'\[video[^\]]*\]', '', text)
    text = re.sub(r'\[gallery[^\]]*\]', '', text)
    text = re.sub(r'\[[^\]]*\]', '', text)
    text = re.sub(r'https?://[^\s]+', '', text)
    text = re.sub(r'\n\s*\n', '\n\n', text)
    text = text.strip()
    return text

def shorten_text_for_telegram(text, rewrite=False):
    """Создает краткую версию текста для Telegram через ИИ (ровно 500 символов)"""
    try:
        clean_text = clean_html_for_telegram(text)
        
        if len(clean_text) == 500 and not rewrite:
            logger.info(f"✅ Текст уже ровно 500 символов")
            return clean_text
        
        if len(clean_text) < 500 and not rewrite:
            logger.info(f"📏 Текст {len(clean_text)} символов, дополняю до 500")
            retry_prompt = f"""Дополни следующий текст до 500 символов, сохранив стиль и смысл. Добавь важные детали, если их не хватает. Текст должен быть связным и логичным.

Текст для дополнения (сейчас {len(clean_text)} символов):
{clean_text}"""
            
            for attempt in range(3):
                try:
                    response = requests.post(
                        DEEPSEEK_API_URL,
                        headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"},
                        json={
                            "model": "deepseek-chat",
                            "messages": [
                                {"role": "system", "content": "Ты редактор. Дополни текст до 500 символов. Ответь только готовым текстом без пояснений."},
                                {"role": "user", "content": retry_prompt}
                            ],
                            "temperature": 0.5,
                            "max_tokens": 600
                        },
                        timeout=60
                    )
                    if response.status_code == 200:
                        result = response.json()["choices"][0]["message"]["content"].strip()
                        result = re.sub(r'^#+\s+', '', result, flags=re.MULTILINE)
                        result = result.strip()
                        
                        if len(result) >= 480:
                            if len(result) > 500:
                                result = result[:500]
                            logger.info(f"✅ Текст дополнен до {len(result)} символов (попытка {attempt+1})")
                            return result
                    time.sleep(1)
                except Exception as e:
                    logger.error(f"Ошибка при дополнении текста: {e}")
            
            if len(clean_text) < 500:
                logger.warning("⚠️ Не удалось дополнить текст через ИИ, дополняю вручную")
                return clean_text + " " * (500 - len(clean_text))
        
        logger.info(f"🤖 Создаю {'новую' if rewrite else 'краткую'} версию текста для Telegram (сейчас {len(clean_text)} символов)")
        
        prompt_type = 'telegram_rewrite' if rewrite else 'telegram'
        result = process_text_with_deepseek(clean_text, prompt_type=prompt_type)
        
        if result:
            logger.info(f"✅ {'Переписанный' if rewrite else 'Краткий'} текст: {len(result)} символов")
            return result
        
        logger.warning("⚠️ Не удалось создать текст через ИИ, обрезаю вручную")
        if len(clean_text) > 500:
            return clean_text[:500]
        return clean_text
            
    except Exception as e:
        logger.error(f"❌ Ошибка обработки текста: {e}")
        clean_text = clean_html_for_telegram(text)
        if len(clean_text) > 500:
            return clean_text[:500]
        return clean_text

def send_text_only_to_telegram(text):
    """Отправляет только текст в Telegram канал"""
    try:
        chat_id = get_channel_id()
        send_url = f"{TG_API_URL}/sendMessage"
        data = {
            'chat_id': chat_id,
            'text': text,
            'parse_mode': 'HTML'
        }
        response = requests.post(send_url, json=data, timeout=60)
        
        if response.status_code in [200, 201]:
            logger.info(f"✅ Текст опубликован в Telegram канале")
            return True
        else:
            logger.error(f"❌ Ошибка публикации текста в канал: {response.status_code}")
            return False
    except Exception as e:
        logger.error(f"❌ Ошибка отправки текста: {e}")
        return False

def publish_to_telegram_channel(title, content, post_link, media_file_id=None, video_file_id=None, gallery_file_ids=None):
    """Публикует пост в Telegram канал с сокращением текста до 500 символов"""
    try:
        logger.info(f"📢 Начинаю публикацию в Telegram канал...")
        
        if not CHANNEL_ID:
            logger.error("❌ CHANNEL_ID не указан в переменных окружения")
            return False
        
        chat_id = get_channel_id()
        logger.info(f"📢 Использую chat_id: {chat_id}")
        
        emoji = get_emoji_for_text(title + " " + content)
        logger.info(f"🎯 Выбран смайлик: {emoji}")
        
        shortened_content = shorten_text_for_telegram(content)
        telegram_text = f"{emoji} <b>{title}</b>\n\n{shortened_content}\n\n📖 Читать статью полностью на нашем сайте: {post_link}"
        
        if video_file_id:
            logger.info(f"🎬 Отправляю видео в Telegram канал")
            media_to_send = video_file_id
            is_video = True
        else:
            media_to_send = media_file_id
            is_video = False
        
        if media_to_send:
            get_file_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile"
            file_response = requests.get(get_file_url, params={'file_id': media_to_send}, timeout=60)
            
            if file_response.status_code == 200:
                result = file_response.json().get('result')
                if result:
                    file_path = result.get('file_path')
                    if file_path:
                        media_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"
                        media_response = requests.get(media_url, timeout=120)
                        
                        if media_response.status_code == 200:
                            media_content = media_response.content
                            
                            if is_video:
                                send_url = f"{TG_API_URL}/sendVideo"
                                files = {'video': media_content}
                                data = {
                                    'chat_id': chat_id,
                                    'caption': telegram_text,
                                    'parse_mode': 'HTML',
                                    'supports_streaming': True
                                }
                                response = requests.post(send_url, files=files, data=data, timeout=60)
                            else:
                                send_url = f"{TG_API_URL}/sendPhoto"
                                files = {'photo': media_content}
                                data = {
                                    'chat_id': chat_id,
                                    'caption': telegram_text,
                                    'parse_mode': 'HTML'
                                }
                                response = requests.post(send_url, files=files, data=data, timeout=60)
                            
                            if response.status_code in [200, 201]:
                                logger.info(f"✅ Пост опубликован в Telegram канале с медиа")
                                return True
                            else:
                                logger.error(f"❌ Ошибка публикации в канал: {response.status_code}")
                                return send_text_only_to_telegram(telegram_text)
                        else:
                            logger.error(f"❌ Ошибка скачивания медиа: {media_response.status_code}")
                            return send_text_only_to_telegram(telegram_text)
                    else:
                        logger.error("❌ Не получен file_path")
                        return send_text_only_to_telegram(telegram_text)
                else:
                    logger.error("❌ Не получен result от Telegram")
                    return send_text_only_to_telegram(telegram_text)
            else:
                logger.error(f"❌ Ошибка getFile: {file_response.status_code}")
                return send_text_only_to_telegram(telegram_text)
        else:
            return send_text_only_to_telegram(telegram_text)
            
    except Exception as e:
        logger.error(f"❌ Ошибка публикации в Telegram канал: {e}")
        try:
            emoji = get_emoji_for_text(title + " " + content)
            shortened_content = shorten_text_for_telegram(content)
            telegram_text = f"{emoji} <b>{title}</b>\n\n{shortened_content}\n\n📖 Читать статью полностью на нашем сайте: {post_link}"
            return send_text_only_to_telegram(telegram_text)
        except:
            return False

def preview_telegram_post(title, content, post_link, chat_id, post_key, media_file_id=None, video_file_id=None, gallery_file_ids=None):
    """Показывает предпросмотр поста перед публикацией в Telegram канал"""
    try:
        logger.info(f"📢 Показываю предпросмотр для публикации в Telegram...")
        
        emoji = get_emoji_for_text(title + " " + content)
        logger.info(f"🎯 Выбран смайлик: {emoji}")
        
        shortened_content = shorten_text_for_telegram(content)
        
        media_type = "🎬 Видео" if video_file_id else "📸 Фото" if media_file_id else "📝 Текст"
        
        preview_text = f"<b>📢 ПРЕДПРОСМОТР ПУБЛИКАЦИИ В КАНАЛ</b>\n\n"
        preview_text += f"<b>Смайлик:</b> {emoji}\n"
        preview_text += f"<b>Заголовок:</b>\n{title}\n\n"
        preview_text += f"<b>Текст (500 символов):</b>\n{shortened_content}\n\n"
        preview_text += f"<b>Медиа:</b> {media_type}\n"
        preview_text += f"<b>Ссылка:</b>\n{post_link}\n\n"
        preview_text += f"<i>⬇️ Выберите действие:</i>"
        
        telegram_preview[post_key] = {
            'title': title,
            'content': content,
            'post_link': post_link,
            'media_file_id': media_file_id,
            'video_file_id': video_file_id,
            'gallery_file_ids': gallery_file_ids,
            'chat_id': chat_id,
            'emoji': emoji
        }
        
        keyboard = {
            "inline_keyboard": [
                [{"text": "🔄 Переделать текст через ИИ", "callback_data": f"rewrite_telegram|{post_key}"}],
                [{"text": "📤 Опубликовать в канал", "callback_data": f"confirm_telegram|{post_key}"}],
                [{"text": "❌ Отмена", "callback_data": f"cancel_telegram|{post_key}"}]
            ]
        }
        
        media_to_show = video_file_id if video_file_id else media_file_id
        
        if media_to_show:
            get_file_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile"
            file_response = requests.get(get_file_url, params={'file_id': media_to_show}, timeout=60)
            
            if file_response.status_code == 200:
                result = file_response.json().get('result')
                if result and result.get('file_path'):
                    media_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{result['file_path']}"
                    media_response = requests.get(media_url, timeout=120)
                    
                    if media_response.status_code == 200:
                        media_content = media_response.content
                        
                        if video_file_id:
                            send_url = f"{TG_API_URL}/sendVideo"
                            files = {'video': media_content}
                            data = {
                                'chat_id': chat_id,
                                'caption': preview_text,
                                'parse_mode': 'HTML',
                                'supports_streaming': True,
                                'reply_markup': json.dumps(keyboard)
                            }
                            response = requests.post(send_url, files=files, data=data, timeout=60)
                        else:
                            send_url = f"{TG_API_URL}/sendPhoto"
                            files = {'photo': media_content}
                            data = {
                                'chat_id': chat_id,
                                'caption': preview_text,
                                'parse_mode': 'HTML',
                                'reply_markup': json.dumps(keyboard)
                            }
                            response = requests.post(send_url, files=files, data=data, timeout=60)
                        
                        if response.status_code in [200, 201]:
                            logger.info(f"✅ Предпросмотр с медиа отправлен")
                            return True
                        else:
                            logger.error(f"❌ Ошибка отправки предпросмотра с медиа: {response.status_code}")
                            return send_preview_without_media(chat_id, preview_text, keyboard)
                    else:
                        logger.error(f"❌ Ошибка скачивания медиа для предпросмотра: {media_response.status_code}")
                        return send_preview_without_media(chat_id, preview_text, keyboard)
                else:
                    logger.error("❌ Не получен file_path")
                    return send_preview_without_media(chat_id, preview_text, keyboard)
            else:
                logger.error(f"❌ Ошибка getFile: {file_response.status_code}")
                return send_preview_without_media(chat_id, preview_text, keyboard)
        else:
            return send_preview_without_media(chat_id, preview_text, keyboard)
            
    except Exception as e:
        logger.error(f"❌ Ошибка показа предпросмотра: {e}")
        return False

def send_preview_without_media(chat_id, preview_text, keyboard):
    """Отправляет предпросмотр без медиа"""
    try:
        send_url = f"{TG_API_URL}/sendMessage"
        data = {
            'chat_id': chat_id,
            'text': preview_text,
            'parse_mode': 'HTML',
            'reply_markup': json.dumps(keyboard)
        }
        response = requests.post(send_url, json=data, timeout=60)
        
        if response.status_code in [200, 201]:
            logger.info(f"✅ Предпросмотр без медиа отправлен")
            return True
        else:
            logger.error(f"❌ Ошибка отправки предпросмотра: {response.status_code}")
            return False
    except Exception as e:
        logger.error(f"❌ Ошибка отправки предпросмотра: {e}")
        return False

def rewrite_telegram_text(post_key, chat_id, message_id):
    """Переписывает текст для Telegram через ИИ"""
    try:
        if post_key not in telegram_preview:
            logger.error(f"❌ Пост {post_key} не найден в предпросмотре")
            return False
        
        post_data = telegram_preview[post_key]
        title = post_data['title']
        content = post_data['content']
        
        logger.info(f"🔄 Переписываю текст для Telegram через ИИ...")
        
        new_content = shorten_text_for_telegram(content, rewrite=True)
        
        if new_content:
            telegram_preview[post_key]['content'] = content
            telegram_preview[post_key]['rewritten_content'] = new_content
            
            emoji = post_data.get('emoji', get_emoji_for_text(title + " " + content))
            media_type = "🎬 Видео" if post_data.get('video_file_id') else "📸 Фото" if post_data.get('media_file_id') else "📝 Текст"
            
            preview_text = f"<b>📢 ПРЕДПРОСМОТР ПУБЛИКАЦИИ В КАНАЛ (НОВАЯ ВЕРСИЯ)</b>\n\n"
            preview_text += f"<b>Смайлик:</b> {emoji}\n"
            preview_text += f"<b>Заголовок:</b>\n{title}\n\n"
            preview_text += f"<b>Новый текст (500 символов):</b>\n{new_content}\n\n"
            preview_text += f"<b>Медиа:</b> {media_type}\n"
            preview_text += f"<b>Ссылка:</b>\n{post_data['post_link']}\n\n"
            preview_text += f"<i>⬇️ Выберите действие:</i>"
            
            keyboard = {
                "inline_keyboard": [
                    [{"text": "🔄 Еще раз переделать", "callback_data": f"rewrite_telegram|{post_key}"}],
                    [{"text": "📤 Опубликовать в канал", "callback_data": f"confirm_telegram|{post_key}"}],
                    [{"text": "❌ Отмена", "callback_data": f"cancel_telegram|{post_key}"}]
                ]
            }
            
            try:
                tg_edit_message_text(chat_id, message_id, preview_text, json.dumps(keyboard))
                logger.info(f"✅ Предпросмотр обновлен с новым текстом")
                return True
            except Exception as e:
                logger.error(f"❌ Ошибка обновления предпросмотра: {e}")
                return False
        else:
            tg_send_message(chat_id, "❌ Не удалось переделать текст через ИИ")
            return False
            
    except Exception as e:
        logger.error(f"❌ Ошибка переписывания текста: {e}")
        return False

def confirm_telegram_publish(post_key):
    """Подтверждение и публикация в Telegram канал"""
    try:
        if post_key not in telegram_preview:
            logger.error(f"❌ Пост {post_key} не найден в предпросмотре")
            return False
        
        post_data = telegram_preview[post_key]
        chat_id = post_data.get('chat_id')
        
        content_to_publish = post_data.get('rewritten_content', post_data['content'])
        
        success = publish_to_telegram_channel(
            title=post_data['title'],
            content=content_to_publish,
            post_link=post_data['post_link'],
            media_file_id=post_data.get('media_file_id'),
            video_file_id=post_data.get('video_file_id'),
            gallery_file_ids=post_data.get('gallery_file_ids', [])
        )
        
        if post_key in telegram_preview:
            del telegram_preview[post_key]
        
        if success:
            tg_send_message(chat_id, "✅ Пост успешно опубликован в Telegram канале!")
            return True
        else:
            tg_send_message(chat_id, "❌ Ошибка публикации в Telegram канале")
            return False
            
    except Exception as e:
        logger.error(f"❌ Ошибка подтверждения публикации: {e}")
        return False

def publish_scheduled_post(post_key):
    """Публикация отложенного поста"""
    if post_key not in scheduled_posts:
        return
    
    post_data = scheduled_posts[post_key]
    chat_id = post_data.get('chat_id')
    
    logger.info(f"⏰ Время публикации для поста {post_key}!")
    
    try:
        featured_media_id = post_data.get('featured_media_id')
        video_media_id = post_data.get('video_media_id')
        gallery_ids = post_data.get('gallery_ids', [])
        
        title = post_data.get('title', '')
        post_type = post_data.get('post_type', 'news')
        content = post_data.get('content', '')
        category_slug = post_data.get('category_slug')
        is_video = post_data.get('is_video', False)
        
        video_url = None
        
        if is_video and video_media_id:
            try:
                video_info = wp_session.get(
                    f"{WP_MEDIA_URL}/{video_media_id}",
                    auth=(WP_USERNAME, WP_PASSWORD),
                    timeout=30
                )
                if video_info.status_code == 200:
                    video_url = video_info.json().get('source_url')
                    logger.info(f"✅ Видео уже загружено, URL: {video_url}")
            except Exception as e:
                logger.error(f"❌ Ошибка получения URL видео: {e}")
        
        if is_video and not video_media_id:
            video_file_id = post_data.get('video_file_id')
            if video_file_id:
                logger.info(f"🎬 Загрузка видео из file_id...")
                video_media_id = download_and_upload_photo(video_file_id, is_video=True, title=title)
                if video_media_id:
                    try:
                        video_info = wp_session.get(
                            f"{WP_MEDIA_URL}/{video_media_id}",
                            auth=(WP_USERNAME, WP_PASSWORD),
                            timeout=30
                        )
                        if video_info.status_code == 200:
                            video_url = video_info.json().get('source_url')
                            logger.info(f"✅ Видео загружено, URL: {video_url}")
                    except Exception as e:
                        logger.error(f"❌ Ошибка получения URL видео: {e}")
        
        if not featured_media_id:
            media_file_id = post_data.get('media_file_id')
            if media_file_id:
                logger.info(f"📸 Загрузка обложки из file_id...")
                featured_media_id = download_and_upload_photo(media_file_id, is_video=False, title=title)
                if featured_media_id:
                    logger.info(f"✅ Обложка загружена ID={featured_media_id}")
        
        if not gallery_ids and post_data.get('gallery_file_ids'):
            logger.info(f"📸 Загрузка фото для галереи...")
            for file_id in post_data.get('gallery_file_ids', []):
                if file_id != post_data.get('video_file_id'):
                    photo_id = download_and_upload_photo(file_id, is_video=False, title=title)
                    if photo_id:
                        gallery_ids.append(photo_id)
                        logger.info(f"✅ Фото загружено ID={photo_id}")
        
        success, link, post_id = create_wp_post(
            title,
            content,
            post_type,
            category_slug,
            featured_media_id,
            True,
            video_url,
            is_video,
            gallery_ids if gallery_ids else None,
            None
        )
        
        if success:
            tg_send_message(chat_id, f"✅ Пост опубликован по расписанию!\n\n{link}")
            
            media_file_id = post_data.get('media_file_id')
            video_file_id = post_data.get('video_file_id')
            gallery_file_ids = post_data.get('gallery_file_ids', [])
            
            preview_telegram_post(
                title=title,
                content=content,
                post_link=link,
                chat_id=chat_id,
                post_key=post_key,
                media_file_id=media_file_id,
                video_file_id=video_file_id,
                gallery_file_ids=gallery_file_ids
            )
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
    formatted_content = format_content_for_wp(content)
    
    post_key = str(int(time.time() * 1000))
    
    pending_posts[post_key] = {
        'original_text': text,
        'media_file_id': media_file_ids[0] if media_file_ids else None,
        'is_video': False,
        'title': title,
        'content': formatted_content,
        'gallery_file_ids': media_file_ids[1:] if len(media_file_ids) > 1 else []
    }
    
    keyboard = {
        "inline_keyboard": []
    }
    for pt_key, pt_name in POST_TYPES.items():
        keyboard["inline_keyboard"].append([{"text": pt_name, "callback_data": f"select_post_type|{post_key}|{pt_key}"}])
    
    tg_send_message(
        chat_id,
        f"📸 Альбом получен! ({len(media_file_ids)} фото)\n\n"
        f"📌 {title}\n\n"
        f"📝 {content[:300]}...\n\n"
        f"📂 Выбери раздел для публикации:",
        json.dumps(keyboard)
    )
    
    if media_group_id in media_groups:
        del media_groups[media_group_id]
    if media_group_id in group_timers:
        del group_timers[media_group_id]

# ============ ОБРАБОТЧИК ОБНОВЛЕНИЙ ============

def process_update(update_json):
    try:
        if 'callback_query' in update_json:
            callback = update_json['callback_query']
            data = callback['data']
            message = callback['message']
            callback_id = callback['id']
            chat_id = message['chat']['id']
            msg_id = message.get('message_id')
            
            logger.info(f"🔘 Получен callback: {data}")
            
            tg_answer_callback_query(callback_id)
            
            parts = data.split('|')
            action = parts[0]
            
            if action == 'rewrite_telegram' and len(parts) >= 2:
                post_key = parts[1]
                tg_send_message(chat_id, "🔄 Переписываю текст через ИИ...")
                
                if rewrite_telegram_text(post_key, chat_id, msg_id):
                    tg_send_message(chat_id, "✅ Текст переписан! Обновите предпросмотр.")
                else:
                    tg_send_message(chat_id, "❌ Ошибка переписывания текста")
                return
            
            if action == 'confirm_telegram' and len(parts) >= 2:
                post_key = parts[1]
                tg_send_message(chat_id, "⏳ Публикую в Telegram канал...")
                
                if confirm_telegram_publish(post_key):
                    try:
                        tg_edit_message_text(chat_id, msg_id, "✅ Пост опубликован в Telegram канале!")
                    except:
                        pass
                else:
                    try:
                        tg_edit_message_text(chat_id, msg_id, "❌ Ошибка публикации в Telegram канале")
                    except:
                        pass
                return
            
            if action == 'cancel_telegram' and len(parts) >= 2:
                post_key = parts[1]
                if post_key in telegram_preview:
                    del telegram_preview[post_key]
                try:
                    tg_edit_message_text(chat_id, msg_id, "❌ Публикация в Telegram канал отменена")
                except:
                    pass
                return
            
            if action == 'select_post_type' and len(parts) >= 3:
                post_key = parts[1]
                post_type = parts[2]
                post_data = pending_posts.get(post_key)
                
                if post_data:
                    post_data['post_type'] = post_type
                    
                    categories = CATEGORIES.get(post_type, {})
                    keyboard = {"inline_keyboard": []}
                    
                    row = []
                    for cat_slug, cat_data in categories.items():
                        cat_name = cat_data.get('name', cat_slug)
                        row.append({"text": f"📌 {cat_name}", "callback_data": f"select_category|{post_key}|{cat_slug}"})
                        if len(row) == 2:
                            keyboard["inline_keyboard"].append(row)
                            row = []
                    if row:
                        keyboard["inline_keyboard"].append(row)
                    
                    keyboard["inline_keyboard"].append([{"text": "⏩ Без рубрики", "callback_data": f"no_category|{post_key}"}])
                    
                    section_name = POST_TYPES.get(post_type, post_type)
                    new_text = f"✅ Выбран раздел: {section_name}\n\n📂 Выбери рубрику:"
                    
                    tg_send_message(chat_id, new_text, json.dumps(keyboard))
                return
            
            if action == 'select_category' and len(parts) >= 3:
                post_key = parts[1]
                category_slug = parts[2]
                post_data = pending_posts.get(post_key)
                
                if post_data:
                    post_data['category_slug'] = category_slug
                    
                    keyboard = get_action_keyboard(post_key)
                    
                    section_name = POST_TYPES.get(post_data['post_type'], post_data['post_type'])
                    cat_data = CATEGORIES.get(post_data['post_type'], {}).get(category_slug, {})
                    category_name = cat_data.get('name', category_slug)
                    media_type = "видео" if post_data.get('is_video') else "фото" if post_data.get('media_file_id') else "нет"
                    has_media = "есть" if post_data.get('media_file_id') else "нет"
                    
                    new_text = f"✅ Выбран раздел: {section_name}\n"
                    new_text += f"✅ Выбрана рубрика: {category_name}\n\n"
                    new_text += f"📌 {post_data.get('title', 'Без заголовка')}\n\n"
                    new_text += f"📝 {post_data.get('content', '')[:300]}...\n\n"
                    new_text += f"{media_type.capitalize()}: {has_media}\n\n"
                    new_text += "Выбери действие:"
                    
                    tg_send_message(chat_id, new_text, json.dumps(keyboard))
                return
            
            if action == 'no_category' and len(parts) >= 2:
                post_key = parts[1]
                post_data = pending_posts.get(post_key)
                
                if post_data:
                    post_data['category_slug'] = None
                    
                    keyboard = get_action_keyboard(post_key)
                    
                    section_name = POST_TYPES.get(post_data['post_type'], post_data['post_type'])
                    media_type = "видео" if post_data.get('is_video') else "фото" if post_data.get('media_file_id') else "нет"
                    has_media = "есть" if post_data.get('media_file_id') else "нет"
                    
                    new_text = f"✅ Выбран раздел: {section_name}\n"
                    new_text += f"⏩ Без рубрики\n\n"
                    new_text += f"📌 {post_data.get('title', 'Без заголовка')}\n\n"
                    new_text += f"📝 {post_data.get('content', '')[:300]}...\n\n"
                    new_text += f"{media_type.capitalize()}: {has_media}\n\n"
                    new_text += "Выбери действие:"
                    
                    tg_send_message(chat_id, new_text, json.dumps(keyboard))
                return
            
            if action == 'schedule' and len(parts) >= 3:
                post_key = parts[1]
                minutes = int(parts[2])
                post_data = pending_posts.get(post_key)
                
                if not post_data:
                    tg_send_message(chat_id, "❌ Пост не найден.")
                    return
                
                if not post_data.get('post_type'):
                    tg_send_message(chat_id, "❌ Раздел не выбран.")
                    return
                
                tg_send_message(chat_id, "⏳ Загружаю медиа в WordPress...")
                
                is_video = post_data.get('is_video', False)
                media_file_id = post_data.get('media_file_id')
                video_file_id = post_data.get('video_file_id')
                gallery_file_ids = post_data.get('gallery_file_ids', [])
                title = post_data.get('title', '')
                
                featured_media_id = None
                video_media_id = None
                gallery_ids = []
                
                if is_video and video_file_id:
                    logger.info(f"🎬 Загрузка видео для планирования...")
                    video_media_id = download_and_upload_photo(video_file_id, is_video=True, title=title)
                    if video_media_id:
                        logger.info(f"✅ Видео загружено ID={video_media_id}")
                    else:
                        logger.warning("⚠️ Видео не загрузилось")
                
                if media_file_id:
                    logger.info(f"📸 Загрузка обложки для планирования...")
                    featured_media_id = download_and_upload_photo(media_file_id, is_video=False, title=title)
                    if featured_media_id:
                        logger.info(f"✅ Обложка загружена ID={featured_media_id}")
                    else:
                        logger.warning("⚠️ Обложка не загрузилась")
                
                for file_id in gallery_file_ids:
                    if file_id != video_file_id:
                        logger.info(f"📸 Загрузка фото для галереи...")
                        photo_id = download_and_upload_photo(file_id, is_video=False, title=title)
                        if photo_id:
                            gallery_ids.append(photo_id)
                            logger.info(f"✅ Фото загружено ID={photo_id}")
                
                schedule_time = datetime.now() + timedelta(minutes=minutes)
                time_str = schedule_time.strftime('%d.%m.%Y %H:%M')
                
                scheduled_posts[post_key] = {
                    'title': post_data['title'],
                    'content': post_data['content'],
                    'post_type': post_data['post_type'],
                    'category_slug': post_data.get('category_slug'),
                    'chat_id': chat_id,
                    'msg_id': msg_id,
                    'is_video': is_video,
                    'featured_media_id': featured_media_id,
                    'video_media_id': video_media_id,
                    'gallery_ids': gallery_ids,
                    'media_file_id': media_file_id,
                    'video_file_id': video_file_id,
                    'gallery_file_ids': gallery_file_ids
                }
                
                timer = threading.Timer(minutes * 60, publish_scheduled_post, args=[post_key])
                scheduled_timers[post_key] = timer
                timer.start()
                
                del pending_posts[post_key]
                
                tg_send_message(
                    chat_id,
                    f"✅ Пост запланирован!\n\n"
                    f"⏰ Публикация: {time_str}\n"
                    f"📂 Раздел: {POST_TYPES.get(post_data['post_type'], post_data['post_type'])}\n"
                    f"📝 Заголовок: {post_data['title']}\n"
                    f"📸 Медиа: {len(gallery_ids) + (1 if featured_media_id else 0)} файлов загружено\n\n"
                    f"🕐 Через {minutes} минут пост будет опубликован автоматически."
                )
                
                logger.info(f"⏰ Пост {post_key} запланирован на {time_str} с медиа ID: обложка={featured_media_id}, видео={video_media_id}, галерея={gallery_ids}")
                return
            
            if action == 'ai' and len(parts) >= 2:
                post_key = parts[1]
                post_data = pending_posts.get(post_key)
                
                if post_data:
                    tg_send_message(chat_id, "🤖 Обрабатываю текст через ИИ...")
                    processed = process_text_with_deepseek(post_data['original_text'], prompt_type='full')
                    
                    if processed:
                        title, content = extract_title_and_content(processed)
                        formatted_content = format_content_for_wp(content)
                        post_data['title'] = title
                        post_data['content'] = formatted_content
                        
                        keyboard = get_action_keyboard(post_key)
                        
                        media_type = "видео" if post_data.get('is_video') else "фото" if post_data.get('media_file_id') else "нет"
                        has_media = "есть" if post_data.get('media_file_id') else "нет"
                        
                        tg_send_message(
                            chat_id,
                            f"📌 {title}\n\n{content}\n\n{media_type.capitalize()}: {has_media}",
                            json.dumps(keyboard)
                        )
                    else:
                        tg_send_message(chat_id, "❌ Ошибка ИИ")
                return
            
            if action == 'publish' and len(parts) >= 2:
                post_key = parts[1]
                post_data = pending_posts.get(post_key)
                
                if not post_data:
                    tg_send_message(chat_id, "❌ Пост не найден.")
                    return
                
                if not post_data.get('post_type'):
                    tg_send_message(chat_id, "❌ Раздел не выбран.")
                    return
                
                tg_send_message(chat_id, "⏳ Публикую на сайт...")
                
                is_video = post_data.get('is_video', False)
                media_file_id = post_data.get('media_file_id')
                video_file_id = post_data.get('video_file_id')
                gallery_file_ids = post_data.get('gallery_file_ids', [])
                title = post_data.get('title', '')
                post_type = post_data.get('post_type', 'news')
                content = post_data.get('content', '')
                category_slug = post_data.get('category_slug')
                
                video_url = None
                gallery_ids = []
                featured_media_id = None
                
                if is_video and video_file_id:
                    logger.info(f"🎬 Загрузка видео...")
                    video_media_id = download_and_upload_photo(video_file_id, is_video=True, title=title)
                    if video_media_id:
                        try:
                            video_info = wp_session.get(
                                f"{WP_MEDIA_URL}/{video_media_id}",
                                auth=(WP_USERNAME, WP_PASSWORD),
                                timeout=30
                            )
                            if video_info.status_code == 200:
                                video_url = video_info.json().get('source_url')
                                logger.info(f"✅ Видео загружено, URL: {video_url}")
                            else:
                                logger.error(f"❌ Не удалось получить URL видео")
                        except Exception as e:
                            logger.error(f"❌ Ошибка получения URL видео: {e}")
                    else:
                        logger.warning("⚠️ Видео не загрузилось")
                
                if media_file_id:
                    logger.info(f"📸 Загрузка фото для обложки...")
                    featured_media_id = download_and_upload_photo(media_file_id, is_video=False, title=title)
                    if featured_media_id:
                        logger.info(f"✅ Фото для обложки загружено ID={featured_media_id}")
                    else:
                        logger.warning("⚠️ Фото для обложки не загрузилось")
                
                for file_id in gallery_file_ids:
                    if file_id != video_file_id:
                        logger.info(f"📸 Загрузка фото для галереи...")
                        photo_id = download_and_upload_photo(file_id, is_video=False, title=title)
                        if photo_id:
                            gallery_ids.append(photo_id)
                            logger.info(f"✅ Фото загружено ID={photo_id}")
                
                success, link, post_id = create_wp_post(
                    title,
                    content,
                    post_type,
                    category_slug,
                    featured_media_id,
                    True,
                    video_url,
                    is_video,
                    gallery_ids if gallery_ids else None,
                    None
                )
                
                if success:
                    tg_send_message(chat_id, f"✅ Пост опубликован на сайте!\n\n{link}")
                    
                    logger.info(f"📢 Показываем предпросмотр для Telegram...")
                    preview_telegram_post(
                        title=title,
                        content=content,
                        post_link=link,
                        chat_id=chat_id,
                        post_key=post_key,
                        media_file_id=media_file_id,
                        video_file_id=video_file_id,
                        gallery_file_ids=gallery_file_ids
                    )
                else:
                    tg_send_message(chat_id, "❌ Ошибка публикации на сайте")
                
                if post_key in pending_posts:
                    del pending_posts[post_key]
                return
            
            if action == 'draft' and len(parts) >= 2:
                post_key = parts[1]
                post_data = pending_posts.get(post_key)
                
                if not post_data:
                    tg_send_message(chat_id, "❌ Пост не найден.")
                    return
                
                if not post_data.get('post_type'):
                    tg_send_message(chat_id, "❌ Раздел не выбран.")
                    return
                
                tg_send_message(chat_id, "⏳ Сохраняю в черновики...")
                
                is_video = post_data.get('is_video', False)
                media_file_id = post_data.get('media_file_id')
                video_file_id = post_data.get('video_file_id')
                gallery_file_ids = post_data.get('gallery_file_ids', [])
                title = post_data.get('title', '')
                post_type = post_data.get('post_type', 'news')
                content = post_data.get('content', '')
                category_slug = post_data.get('category_slug')
                
                video_url = None
                gallery_ids = []
                featured_media_id = None
                
                if is_video and video_file_id:
                    logger.info(f"🎬 Загрузка видео...")
                    video_media_id = download_and_upload_photo(video_file_id, is_video=True, title=title)
                    if video_media_id:
                        try:
                            video_info = wp_session.get(
                                f"{WP_MEDIA_URL}/{video_media_id}",
                                auth=(WP_USERNAME, WP_PASSWORD),
                                timeout=30
                            )
                            if video_info.status_code == 200:
                                video_url = video_info.json().get('source_url')
                                logger.info(f"✅ Видео загружено, URL: {video_url}")
                            else:
                                logger.error(f"❌ Не удалось получить URL видео")
                        except Exception as e:
                            logger.error(f"❌ Ошибка получения URL видео: {e}")
                    else:
                        logger.warning("⚠️ Видео не загрузилось")
                
                if media_file_id:
                    logger.info(f"📸 Загрузка фото для обложки...")
                    featured_media_id = download_and_upload_photo(media_file_id, is_video=False, title=title)
                    if featured_media_id:
                        logger.info(f"✅ Фото для обложки загружено ID={featured_media_id}")
                    else:
                        logger.warning("⚠️ Фото для обложки не загрузилось")
                
                for file_id in gallery_file_ids:
                    if file_id != video_file_id:
                        logger.info(f"📸 Загрузка фото для галереи...")
                        photo_id = download_and_upload_photo(file_id, is_video=False, title=title)
                        if photo_id:
                            gallery_ids.append(photo_id)
                            logger.info(f"✅ Фото загружено ID={photo_id}")
                
                success, link, post_id = create_wp_post(
                    title,
                    content,
                    post_type,
                    category_slug,
                    featured_media_id,
                    False,
                    video_url,
                    is_video,
                    gallery_ids if gallery_ids else None,
                    None
                )
                
                if success:
                    tg_send_message(chat_id, f"✅ Пост сохранен в черновиках!\n\n{link}")
                else:
                    tg_send_message(chat_id, "❌ Ошибка сохранения")
                
                if post_key in pending_posts:
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
            
            if has_photo and chat_id in video_awaiting_photo:
                video_data = video_awaiting_photo[chat_id]
                
                photos = message['photo']
                if photos and len(photos) > 0:
                    photo_file_id = photos[-1]['file_id']
                    
                    title, content = extract_title_and_content(video_data['text'])
                    formatted_content = format_content_for_wp(content)
                    
                    post_key = str(int(time.time() * 1000))
                    pending_posts[post_key] = {
                        'original_text': video_data['text'],
                        'media_file_id': photo_file_id,
                        'is_video': True,
                        'title': title,
                        'content': formatted_content,
                        'video_file_id': video_data['video_file_id'],
                        'gallery_file_ids': [photo_file_id]
                    }
                    
                    del video_awaiting_photo[chat_id]
                    
                    keyboard = {
                        "inline_keyboard": []
                    }
                    for pt_key, pt_name in POST_TYPES.items():
                        keyboard["inline_keyboard"].append([{"text": pt_name, "callback_data": f"select_post_type|{post_key}|{pt_key}"}])
                    
                    tg_send_message(
                        chat_id,
                        f"🎬 Видео с фото получено!\n\n"
                        f"📌 {title}\n\n"
                        f"📝 {content[:300]}...\n\n"
                        f"📸 Фото будет использовано как обложка\n"
                        f"🎬 Видео будет вставлено в статью\n\n"
                        f"📂 Выбери раздел для публикации:",
                        json.dumps(keyboard)
                    )
                    logger.info(f"✅ Видео + фото объединены в пост {post_key}")
                    return
            
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
            
            if has_video:
                video_file_id = message['video']['file_id']
                
                if not text:
                    tg_send_message(chat_id, "❌ Отправьте текст новости вместе с видео.\nПервая строка будет заголовком.")
                    return
                
                video_awaiting_photo[chat_id] = {
                    'video_file_id': video_file_id,
                    'text': text
                }
                
                tg_send_message(
                    chat_id,
                    "📸 Теперь отправьте ФОТО для этой новости.\n"
                    "Оно будет использовано как ОБЛОЖКА новости.\n"
                    "Видео будет вставлено в текст статьи.\n\n"
                    "Отправьте фото отдельным сообщением."
                )
                logger.info(f"🎬 Получено видео, ожидаем фото от {chat_id}")
                return
            
            if has_photo and not media_group_id:
                photos = message['photo']
                if photos and len(photos) > 0:
                    media_file_id = photos[-1]['file_id']
                    is_video = False
                    logger.info("📸 Обнаружено ФОТО")
                else:
                    media_file_id = None
                    is_video = False
            else:
                media_file_id = None
                is_video = False
            
            if not text and media_file_id:
                tg_send_message(chat_id, "❌ Отправьте текст новости.\nПервая строка будет заголовком.")
                return
            
            if not text:
                return
            
            title, content = extract_title_and_content(text)
            formatted_content = format_content_for_wp(content)
            
            post_key = str(int(time.time() * 1000))
            pending_posts[post_key] = {
                'original_text': text,
                'media_file_id': media_file_id,
                'is_video': is_video,
                'title': title,
                'content': formatted_content,
                'video_file_id': None,
                'gallery_file_ids': []
            }
            
            keyboard = {
                "inline_keyboard": []
            }
            for pt_key, pt_name in POST_TYPES.items():
                keyboard["inline_keyboard"].append([{"text": pt_name, "callback_data": f"select_post_type|{post_key}|{pt_key}"}])
            
            media_type = "видеo" if is_video else "фото" if media_file_id else "нет"
            tg_send_message(
                chat_id,
                f"📢 Пост получен!\n\n"
                f"📌 {title}\n\n"
                f"📝 {content[:300]}...\n\n"
                f"{media_type.capitalize()}: {'есть' if media_file_id else 'нет'}\n\n"
                f"📂 Выбери раздел для публикации:",
                json.dumps(keyboard)
            )
            logger.info(f"✉️ Отправлен выбор раздела, медиа={media_type}")
            
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
    logger.info(f"📂 Доступные разделы: {', '.join(POST_TYPES.values())}")
    
    try:
        requests.post(f"{TG_API_URL}/deleteWebhook")
        requests.post(f"{TG_API_URL}/setWebhook", json={'url': webhook_url})
        logger.info("✅ Вебхук установлен")
    except Exception as e:
        logger.error(f"⚠️ Ошибка установки вебхука: {e}")
    
    port = int(os.getenv('PORT', 8000))
    app.run(host='0.0.0.0', port=port)
