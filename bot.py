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
telegram_preview = {}  # Хранит данные для предпросмотра перед публикацией в Telegram

# Базовый URL для Telegram API
TG_API_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

DEEPSEEK_PROMPT = """Ты редактор новостного сайта. Перепиши новость в строгом городском формате, объемом около 650 символов. Убери лишнюю воду, сделай интересный заголовок, никаких смайликов. Не используй символы # и ** в ответе. Сохрани главные факты. Расставь абзацы.

ВАЖНО: НЕ пиши слова "Заголовок:" и "Текст:". Просто напиши сначала заголовок, потом пустую строку, потом текст."""

def preview_telegram_post(title, content, post_link, chat_id, post_key, media_file_id=None, video_file_id=None, gallery_file_ids=None):
    """
    Показывает предпросмотр поста перед публикацией в Telegram канал
    """
    try:
        logger.info(f"📢 Показываю предпросмотр для публикации в Telegram...")
        
        # Формируем текст для предпросмотра
        clean_content = re.sub(r'<[^>]+>', '', content)
        clean_content = re.sub(r'\[video[^\]]*\]', '', clean_content)
        clean_content = re.sub(r'\[gallery[^\]]*\]', '', clean_content)
        clean_content = re.sub(r'\[[^\]]*\]', '', clean_content)
        clean_content = re.sub(r'https?://[^\s]+', '', clean_content)
        clean_content = ' '.join(clean_content.split())
        
        # Ограничиваем длину текста для предпросмотра
        if len(clean_content) > 1000:
            clean_content = clean_content[:997] + "..."
        
        preview_text = f"<b>📢 ПРЕДПРОСМОТР ПУБЛИКАЦИИ В КАНАЛ</b>\n\n"
        preview_text += f"<b>Заголовок:</b>\n{title}\n\n"
        preview_text += f"<b>Текст:</b>\n{clean_content}\n\n"
        preview_text += f"<b>Ссылка:</b>\n<a href=\"{post_link}\">Подробнее: ссылка на статью</a>\n\n"
        preview_text += f"<i>⬇️ Нажмите кнопку ниже для публикации в канал</i>"
        
        # Сохраняем данные для публикации
        telegram_preview[post_key] = {
            'title': title,
            'content': content,
            'post_link': post_link,
            'media_file_id': media_file_id,
            'video_file_id': video_file_id,
            'gallery_file_ids': gallery_file_ids,
            'chat_id': chat_id
        }
        
        # Создаем клавиатуру с кнопкой публикации
        keyboard = {
            "inline_keyboard": [
                [{"text": "📤 Опубликовать в канал", "callback_data": f"confirm_telegram|{post_key}"}],
                [{"text": "❌ Отмена", "callback_data": f"cancel_telegram|{post_key}"}]
            ]
        }
        
        # Показываем предпросмотр с медиа если есть
        if media_file_id:
            # Скачиваем медиа для предпросмотра
            get_file_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile"
            file_response = requests.get(get_file_url, params={'file_id': media_file_id}, timeout=60)
            
            if file_response.status_code == 200:
                result = file_response.json().get('result')
                if result and result.get('file_path'):
                    media_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{result['file_path']}"
                    media_response = requests.get(media_url, timeout=120)
                    
                    if media_response.status_code == 200:
                        media_content = media_response.content
                        
                        if video_file_id:
                            # Отправляем видео с предпросмотром
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
                            # Отправляем фото с предпросмотром
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
                            # Пробуем отправить без медиа
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
            # Отправляем только текст
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

def confirm_telegram_publish(post_key):
    """
    Подтверждение и публикация в Telegram канал
    """
    try:
        if post_key not in telegram_preview:
            logger.error(f"❌ Пост {post_key} не найден в предпросмотре")
            return False
        
        post_data = telegram_preview[post_key]
        chat_id = post_data.get('chat_id')
        
        # Публикуем в канал
        success = publish_to_telegram_channel(
            title=post_data['title'],
            content=post_data['content'],
            post_link=post_data['post_link'],
            media_file_id=post_data.get('media_file_id'),
            video_file_id=post_data.get('video_file_id'),
            gallery_file_ids=post_data.get('gallery_file_ids', [])
        )
        
        # Удаляем из хранилища
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

def publish_to_telegram_channel(title, content, post_link, media_file_id=None, video_file_id=None, gallery_file_ids=None):
    """
    Публикует пост в Telegram канал с заголовком, текстом, медиа и ссылкой на статью
    """
    try:
        logger.info(f"📢 Начинаю публикацию в Telegram канал...")
        
        if not CHANNEL_ID:
            logger.error("❌ CHANNEL_ID не указан в переменных окружения")
            return False
        
        # Формируем текст для Telegram
        clean_content = re.sub(r'<[^>]+>', '', content)
        clean_content = re.sub(r'\[video[^\]]*\]', '', clean_content)
        clean_content = re.sub(r'\[gallery[^\]]*\]', '', clean_content)
        clean_content = re.sub(r'\[[^\]]*\]', '', clean_content)
        clean_content = re.sub(r'https?://[^\s]+', '', clean_content)
        clean_content = ' '.join(clean_content.split())
        
        if len(clean_content) > 1000:
            clean_content = clean_content[:997] + "..."
        
        telegram_text = f"<b>{title}</b>\n\n{clean_content}\n\n<a href=\"{post_link}\">Подробнее: ссылка на статью</a>"
        
        # Проверяем, есть ли медиа для публикации
        if media_file_id:
            get_file_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile"
            file_response = requests.get(get_file_url, params={'file_id': media_file_id}, timeout=60)
            
            if file_response.status_code == 200:
                result = file_response.json().get('result')
                if result:
                    file_path = result.get('file_path')
                    if file_path:
                        media_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"
                        media_response = requests.get(media_url, timeout=120)
                        
                        if media_response.status_code == 200:
                            media_content = media_response.content
                            
                            if len(media_content) > 50 * 1024 * 1024:
                                logger.warning(f"⚠️ Файл слишком большой: {len(media_content)} байт")
                                return send_text_only_to_telegram(telegram_text)
                            
                            if video_file_id:
                                send_url = f"{TG_API_URL}/sendVideo"
                                files = {'video': media_content}
                                data = {
                                    'chat_id': CHANNEL_ID,
                                    'caption': telegram_text,
                                    'parse_mode': 'HTML',
                                    'supports_streaming': True
                                }
                                response = requests.post(send_url, files=files, data=data, timeout=60)
                            else:
                                send_url = f"{TG_API_URL}/sendPhoto"
                                files = {'photo': media_content}
                                data = {
                                    'chat_id': CHANNEL_ID,
                                    'caption': telegram_text,
                                    'parse_mode': 'HTML'
                                }
                                response = requests.post(send_url, files=files, data=data, timeout=60)
                            
                            if response.status_code in [200, 201]:
                                logger.info(f"✅ Пост опубликован в Telegram канале с медиа")
                                return True
                            else:
                                logger.error(f"❌ Ошибка публикации в канал: {response.status_code}")
                                logger.error(f"Ответ: {response.text[:200]}")
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
            return send_text_only_to_telegram(telegram_text)
        except:
            return False

def send_text_only_to_telegram(text):
    """Отправляет только текст в Telegram канал"""
    try:
        send_url = f"{TG_API_URL}/sendMessage"
        data = {
            'chat_id': CHANNEL_ID,
            'text': text,
            'parse_mode': 'HTML'
        }
        response = requests.post(send_url, json=data, timeout=60)
        
        if response.status_code in [200, 201]:
            logger.info(f"✅ Текст опубликован в Telegram канале")
            return True
        else:
            logger.error(f"❌ Ошибка публикации текста в канал: {response.status_code}")
            logger.error(f"Ответ: {response.text[:200]}")
            return False
    except Exception as e:
        logger.error(f"❌ Ошибка отправки текста: {e}")
        return False

# Остальные функции (get_category_id, set_post_categories, generate_seo_description, 
# unique_image, tg_send_message, tg_edit_message_text, tg_answer_callback_query,
# extract_title_and_content, format_content_for_wp, process_text_with_deepseek,
# download_and_upload_photo, create_wp_post, publish_scheduled_post, 
# get_action_keyboard, process_media_group) остаются без изменений

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
            
            # НОВЫЙ ОБРАБОТЧИК: Подтверждение публикации в Telegram
            if action == 'confirm_telegram' and len(parts) >= 2:
                post_key = parts[1]
                tg_send_message(chat_id, "⏳ Публикую в Telegram канал...")
                
                if confirm_telegram_publish(post_key):
                    # Удаляем сообщение с предпросмотром
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
            
            # НОВЫЙ ОБРАБОТЧИК: Отмена публикации в Telegram
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
                    processed = process_text_with_deepseek(post_data['original_text'])
                    
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
                    
                    # 🔥 ПОКАЗЫВАЕМ ПРЕДПРОСМОТР ПЕРЕД ПУБЛИКАЦИЕЙ В TELEGRAM
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
            # Обработка сообщений (остается без изменений)
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
