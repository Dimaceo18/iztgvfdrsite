import os
import requests
import logging
import re
import time
import json
from flask import Flask, request, jsonify
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

# Рубрики для каждого раздела
CATEGORIES = {
    "news": {
        "v-mire": "🌍 В мире",
        "vlasti": "🏛️ Власти",
        "city": "🏙️ Город",
        "dengi": "💰 Деньги",
        "zakon": "⚖️ Закон",
        "proisshestviya": "🚨 Происшествия"
    },
    "sport": {
        "edinoborstva": "🥊 Единоборства",
        "zimnie_vidy": "⛷️ Зимние виды",
        "mirovoy_sport": "🌍 Мировой спорт",
        "sbornaya_belarusi": "🇧🇾 Сборная Беларуси",
        "tennis": "🎾 Теннис",
        "futbol": "⚽ Футбол",
        "hokkey": "🏒 Хоккей"
    },
    "realt": {
        "za_gorodom": "🌳 За городом",
        "kredity": "🏦 Кредиты",
        "novostroyki": "🏗️ Новостройки",
        "obzory": "📋 Обзоры",
        "remont": "🔨 Ремонт"
    },
    "auto": {
        "avarii-i-dtp": "🚗 Аварии и ДТП",
        "avtorynok": "🏪 Авторынок",
        "pdd": "📜 ПДД",
        "test-drayvy": "🚘 Тест-драйвы и обзоры"
    },
    "afisha": {
        "vecherinki": "🎉 Вечеринки",
        "vystavki": "🖼️ Выставки",
        "vyhodnye": "📅 Выходные",
        "detskaya_afisha": "🧒 Детская афиша",
        "kvesty": "🔍 Квесты",
        "kino": "🎬 Кино",
        "koncerty": "🎵 Концерты",
        "master-klassy": "🎨 Мастер-классы",
        "obzory": "📋 Обзоры",
        "obuchenie": "📚 Обучение",
        "rekomendacii": "💡 Рекомендации",
        "sobytiya": "📅 События",
        "spektakli": "🎭 Спектакли",
        "standap": "🎤 Стендап",
        "festivali": "🎪 Фестивали",
        "ekskursii": "🏛️ Экскурсии"
    },
    "sales": {
        "buklety": "📰 Буклеты",
        "novinki": "✨ Новинки",
        "obzory": "📋 Обзоры",
        "skidki": "🏷️ Скидки"
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

# Хранилище
pending_posts = {}

# Кэш категорий
category_cache = {}

# Базовый URL для Telegram API
TG_API_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

DEEPSEEK_PROMPT = """Ты редактор новостного сайта. Перепиши новость в строгом городском формате, объемом около 650 символов. Убери лишнюю воду, сделай интересный заголовок, никаких смайликов. Не используй символы # и ** в ответе. Сохрани главные факты. Расставь абзацы.

ВАЖНО: НЕ пиши слова "Заголовок:" и "Текст:". Просто напиши сначала заголовок, потом пустую строку, потом текст."""

def tg_send_message(chat_id, text, reply_markup=None):
    url = f"{TG_API_URL}/sendMessage"
    data = {'chat_id': chat_id, 'text': text}
    if reply_markup:
        data['reply_markup'] = reply_markup
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

def format_content_for_wp(text, media_html=None):
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
    if media_html and len(formatted) > 0:
        formatted.insert(1, media_html)
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

def download_and_upload_photo(file_id):
    """Рабочая загрузка фото"""
    try:
        get_file_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile"
        file_response = requests.get(get_file_url, params={'file_id': file_id}, timeout=30)
        if file_response.status_code != 200:
            return None
        result = file_response.json().get('result')
        if not result:
            return None
        file_path = result.get('file_path')
        if not file_path:
            return None
        photo_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"
        photo_response = requests.get(photo_url, timeout=60)
        if photo_response.status_code != 200:
            return None
        wp_headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Content-Disposition': f'attachment; filename="photo_{int(time.time())}.jpg"',
            'Content-Type': 'image/jpeg'
        }
        wp_response = wp_session.post(
            WP_MEDIA_URL,
            auth=(WP_USERNAME, WP_PASSWORD),
            headers=wp_headers,
            data=photo_response.content,
            timeout=60
        )
        if wp_response.status_code == 201:
            return wp_response.json()['id']
        return None
    except Exception as e:
        logger.error(f"Ошибка фото: {e}")
        return None

def download_and_upload_video(file_id):
    try:
        get_file_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile"
        file_response = requests.get(get_file_url, params={'file_id': file_id}, timeout=30)
        if file_response.status_code != 200:
            return None, None
        result = file_response.json().get('result')
        if not result:
            return None, None
        file_path = result.get('file_path')
        if not file_path:
            return None, None
        video_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"
        video_response = requests.get(video_url, timeout=120)
        if video_response.status_code != 200:
            return None, None
        wp_headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Content-Disposition': f'attachment; filename="video_{int(time.time())}.mp4"',
            'Content-Type': 'video/mp4'
        }
        wp_response = wp_session.post(
            WP_MEDIA_URL,
            auth=(WP_USERNAME, WP_PASSWORD),
            headers=wp_headers,
            data=video_response.content,
            timeout=120
        )
        if wp_response.status_code == 201:
            media_id = wp_response.json()['id']
            source_url = wp_response.json()['source_url']
            return media_id, source_url
        return None, None
    except Exception as e:
        logger.error(f"Ошибка видео: {e}")
        return None, None

def get_category_id(post_type, category_slug):
    cache_key = f"{post_type}_{category_slug}"
    if cache_key in category_cache:
        return category_cache[cache_key]
    taxonomy = TAXONOMY_MAP.get(post_type, "category")
    try:
        cat_response = wp_session.get(
            f"{WP_URL}/wp-json/wp/v2/{taxonomy}",
            params={'slug': category_slug},
            timeout=10
        )
        if cat_response.status_code == 200 and cat_response.json():
            category_id = cat_response.json()[0]['id']
            category_cache[cache_key] = category_id
            return category_id
    except Exception as e:
        logger.warning(f"⚠️ Ошибка поиска рубрики {category_slug}: {e}")
    return None

def create_wp_post(title, content, post_type, category_slug=None, media_id=None, publish=False):
    status = 'publish' if publish else 'draft'
    
    seo_title = title[:70]
    seo_description = re.sub(r'<[^>]+>', '', content)
    seo_description = ' '.join(seo_description.split())
    seo_description = seo_description[:160]
    if len(seo_description) > 160:
        seo_description = seo_description[:157] + "..."
    
    post_data = {
        'title': title,
        'content': content,
        'status': status,
        'type': post_type,
        'meta': {
            '_yoast_wpseo_title': seo_title,
            '_yoast_wpseo_metadesc': seo_description
        }
    }
    
    if media_id:
        post_data['featured_media'] = media_id
    
    if category_slug:
        category_id = get_category_id(post_type, category_slug)
        if category_id:
            taxonomy = TAXONOMY_MAP.get(post_type, "category")
            post_data['tax_input'] = {
                taxonomy: [category_id]
            }
            logger.info(f"📂 Добавлена рубрика: {category_slug}")
    
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'application/json',
            'Content-Type': 'application/json',
        }
        response = wp_session.post(
            f"{WP_API_URL}/{post_type}",
            auth=(WP_USERNAME, WP_PASSWORD),
            json=post_data,
            headers=headers,
            timeout=60
        )
        if response.status_code == 201:
            logger.info(f"✅ Пост создан: {response.json()['link']}")
            return True, response.json()['link']
        else:
            logger.error(f"❌ Ошибка: {response.status_code}")
            return False, None
    except Exception as e:
        logger.error(f"❌ Ошибка: {e}")
        return False, None

def process_update(update_json):
    try:
        if 'callback_query' in update_json:
            callback = update_json['callback_query']
            data = callback['data']
            message = callback['message']
            callback_id = callback['id']
            chat_id = message['chat']['id']
            
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
                    
                    categories = CATEGORIES.get(post_type, {})
                    keyboard = {"inline_keyboard": []}
                    
                    row = []
                    for cat_slug, cat_name in categories.items():
                        row.append({"text": cat_name, "callback_data": f"select_category|{post_key}|{cat_slug}"})
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
                    
                    keyboard = {
                        "inline_keyboard": [
                            [{"text": "🤖 Переделать текст через ИИ", "callback_data": f"ai|{post_key}"}],
                            [{"text": "📝 Опубликовать в черновики", "callback_data": f"draft|{post_key}"}],
                            [{"text": "🌐 Опубликовать на сайт", "callback_data": f"publish|{post_key}"}]
                        ]
                    }
                    
                    section_name = POST_TYPES.get(post_data['post_type'], post_data['post_type'])
                    category_name = CATEGORIES.get(post_data['post_type'], {}).get(category_slug, category_slug)
                    media_type = "видео" if post_data.get('is_video') else "фото"
                    
                    new_text = f"✅ Выбран раздел: {section_name}\n"
                    new_text += f"✅ Выбрана рубрика: {category_name}\n\n"
                    new_text += f"📌 {post_data.get('title', 'Без заголовка')}\n\n"
                    new_text += f"📝 {post_data.get('content', '')[:300]}...\n\n"
                    new_text += f"{media_type.capitalize()}: {'есть' if post_data.get('media_file_id') else 'нет'}\n\n"
                    new_text += "Выбери действие:"
                    
                    tg_send_message(chat_id, new_text, json.dumps(keyboard))
                return
            
            if action == 'no_category' and len(parts) >= 2:
                post_key = parts[1]
                post_data = pending_posts.get(post_key)
                
                if post_data:
                    post_data['category_slug'] = None
                    
                    keyboard = {
                        "inline_keyboard": [
                            [{"text": "🤖 Переделать текст через ИИ", "callback_data": f"ai|{post_key}"}],
                            [{"text": "📝 Опубликовать в черновики", "callback_data": f"draft|{post_key}"}],
                            [{"text": "🌐 Опубликовать на сайт", "callback_data": f"publish|{post_key}"}]
                        ]
                    }
                    
                    section_name = POST_TYPES.get(post_data['post_type'], post_data['post_type'])
                    media_type = "видео" if post_data.get('is_video') else "фото"
                    
                    new_text = f"✅ Выбран раздел: {section_name}\n"
                    new_text += f"⏩ Без рубрики\n\n"
                    new_text += f"📌 {post_data.get('title', 'Без заголовка')}\n\n"
                    new_text += f"📝 {post_data.get('content', '')[:300]}...\n\n"
                    new_text += f"{media_type.capitalize()}: {'есть' if post_data.get('media_file_id') else 'нет'}\n\n"
                    new_text += "Выбери действие:"
                    
                    tg_send_message(chat_id, new_text, json.dumps(keyboard))
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
                        
                        keyboard = {
                            "inline_keyboard": [
                                [{"text": "🤖 Переделать текст через ИИ", "callback_data": f"ai|{post_key}"}],
                                [{"text": "📝 Опубликовать в черновики", "callback_data": f"draft|{post_key}"}],
                                [{"text": "🌐 Опубликовать на сайт", "callback_data": f"publish|{post_key}"}]
                            ]
                        }
                        
                        media_type = "видео" if post_data.get('is_video') else "фото"
                        tg_send_message(
                            chat_id,
                            f"📌 {title}\n\n{content}\n\n{media_type.capitalize()}: {'есть' if post_data.get('media_file_id') else 'нет'}",
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
                
                # Загружаем медиа (с обработкой ошибок)
                featured_media_id = None
                media_html = []
                
                # Загружаем фото (только первое как обложку, остальные в контент)
                if post_data.get('photo_file_ids'):
                    for i, file_id in enumerate(post_data['photo_file_ids']):
                        media_id = download_and_upload_photo(file_id)
                        if media_id:
                            if i == 0:
                                featured_media_id = media_id
                            else:
                                media_html.append(f'<img src="{WP_URL}/wp-content/uploads/{media_id}.jpg" alt="">')
                
                # Загружаем видео
                if post_data.get('video_file_id'):
                    video_media_id, video_url = download_and_upload_video(post_data['video_file_id'])
                    if video_url:
                        media_html.append(f'<video controls width="100%"><source src="{video_url}" type="video/mp4"></video>')
                
                formatted_content = format_content_for_wp(post_data['content'], '\n'.join(media_html) if media_html else None)
                
                success, link = create_wp_post(
                    post_data['title'],
                    formatted_content,
                    post_data['post_type'],
                    post_data.get('category_slug'),
                    featured_media_id,
                    True
                )
                
                if success:
                    tg_send_message(chat_id, f"✅ Пост опубликован!\n\n{link}")
                else:
                    tg_send_message(chat_id, "❌ Ошибка публикации")
                
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
                
                featured_media_id = None
                media_html = []
                
                if post_data.get('photo_file_ids'):
                    for i, file_id in enumerate(post_data['photo_file_ids']):
                        media_id = download_and_upload_photo(file_id)
                        if media_id:
                            if i == 0:
                                featured_media_id = media_id
                            else:
                                media_html.append(f'<img src="{WP_URL}/wp-content/uploads/{media_id}.jpg" alt="">')
                
                if post_data.get('video_file_id'):
                    video_media_id, video_url = download_and_upload_video(post_data['video_file_id'])
                    if video_url:
                        media_html.append(f'<video controls width="100%"><source src="{video_url}" type="video/mp4"></video>')
                
                formatted_content = format_content_for_wp(post_data['content'], '\n'.join(media_html) if media_html else None)
                
                success, link = create_wp_post(
                    post_data['title'],
                    formatted_content,
                    post_data['post_type'],
                    post_data.get('category_slug'),
                    featured_media_id,
                    False
                )
                
                if success:
                    tg_send_message(chat_id, f"✅ Пост сохранен в черновиках!\n\n{link}")
                else:
                    tg_send_message(chat_id, "❌ Ошибка сохранения")
                
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
            
            photo_file_ids = []
            video_file_id = None
            
            if 'photo' in message:
                photo_file_ids = [photo['file_id'] for photo in message['photo']]
                logger.info(f"📸 Обнаружено {len(photo_file_ids)} фото")
            
            if 'video' in message:
                video_file_id = message['video']['file_id']
                logger.info("🎬 Обнаружено ВИДЕО")
            
            if not text:
                tg_send_message(chat_id, "❌ Отправьте текст новости.\nПервая строка будет заголовком.")
                return
            
            title, content = extract_title_and_content(text)
            formatted_content = format_content_for_wp(content)
            
            post_key = str(int(time.time() * 1000))
            pending_posts[post_key] = {
                'original_text': text,
                'photo_file_ids': photo_file_ids,
                'video_file_id': video_file_id,
                'title': title,
                'content': formatted_content
            }
            
            keyboard = {
                "inline_keyboard": []
            }
            for pt_key, pt_name in POST_TYPES.items():
                keyboard["inline_keyboard"].append([{"text": pt_name, "callback_data": f"select_post_type|{post_key}|{pt_key}"}])
            
            media_info = []
            if photo_file_ids:
                media_info.append(f"📸 {len(photo_file_ids)} фото")
            if video_file_id:
                media_info.append("🎬 видео")
            media_text = ", ".join(media_info) if media_info else "нет"
            
            tg_send_message(
                chat_id,
                f"📢 Пост получен!\n\n"
                f"📌 {title}\n\n"
                f"📝 {content[:300]}...\n\n"
                f"📎 Медиа: {media_text}\n\n"
                f"📂 Выбери раздел для публикации:",
                json.dumps(keyboard)
            )
            logger.info(f"✉️ Отправлен выбор раздела, медиа={media_text}")
            
    except Exception as e:
        logger.error(f"Ошибка: {e}")
        logger.exception("Детали:")

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
    logger.info(f"📋 Доступные рубрики: {sum(len(c) for c in CATEGORIES.values())}")
    
    requests.post(f"{TG_API_URL}/deleteWebhook")
    requests.post(f"{TG_API_URL}/setWebhook", json={'url': webhook_url})
    logger.info("✅ Вебхук установлен")
    
    port = int(os.getenv('PORT', 8000))
    app.run(host='0.0.0.0', port=port)
