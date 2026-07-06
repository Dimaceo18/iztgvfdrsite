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

app = Flask(__name__)
wp_session = requests.Session()

# Хранилище
pending_posts = {}

# Базовый URL для Telegram API
TG_API_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

DEEPSEEK_PROMPT = """Ты редактор новостного сайта. Перепиши новость в строгом городском формате, объемом около 650 символов. Убери лишнюю воду, сделай интересный заголовок, никаких смайликов. Не используй символы # и ** в ответе. Сохрани главные факты. Расставь абзацы.

ВАЖНО: НЕ пиши слова "Заголовок:" и "Текст:". Просто напиши сначала заголовок, потом пустую строку, потом текст."""

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

def extract_title_and_content(text):
    if not text:
        return "Новый пост из Telegram", ""
    lines = text.strip().split('\n')
    title = lines[0].strip() if lines else "Новый пост"
    if len(title) > 180:
        title = title[:177] + "..."
    content = '\n'.join(lines[1:]).strip() if len(lines) > 1 else ""
    return title, content

def format_content_for_wp(text, video_url=None, gallery_images=None):
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
            
            # Вставляем медиа после первого абзаца
            if i == 0:
                if video_url:
                    formatted.append(f'[video width="100%" height="auto" mp4="{video_url}"]')
                elif gallery_images and len(gallery_images) > 0:
                    # Создаем галерею из фото
                    gallery_shortcode = '[gallery ids="' + ','.join(str(img['id']) for img in gallery_images) + '"]'
                    formatted.append(gallery_shortcode)
                    logger.info(f"🖼️ Добавлена галерея из {len(gallery_images)} фото")
    
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

def download_and_upload_media(file_id, is_video=False, max_retries=3):
    """Загрузка одного фото или видео в WordPress с повторными попытками"""
    try:
        media_type = "видео" if is_video else "фото"
        logger.info(f"📸 НАЧАЛО ЗАГРУЗКИ {media_type.upper()}: file_id={file_id}")
        
        # Получаем file_path от Telegram
        get_file_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile"
        file_response = requests.get(get_file_url, params={'file_id': file_id}, timeout=30)
        
        if file_response.status_code != 200:
            logger.error(f"❌ Ошибка getFile: {file_response.status_code}")
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
        
        # Скачиваем файл из Telegram
        media_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"
        logger.info(f"📸 Скачиваю {media_type}...")
        
        media_response = requests.get(media_url, timeout=120)
        if media_response.status_code != 200:
            logger.error(f"❌ Ошибка скачивания {media_type}: {media_response.status_code}")
            return None, None
        
        logger.info(f"✅ {media_type.capitalize()} скачано, размер: {len(media_response.content)} байт")
        
        # Подготовка данных для загрузки
        ext = 'mp4' if is_video else 'jpg'
        mime = 'video/mp4' if is_video else 'image/jpeg'
        filename = f'{media_type}_{int(time.time())}_{file_id[:8]}.{ext}'
        
        # Пробуем загрузить с повторными попытками
        for attempt in range(max_retries):
            try:
                logger.info(f"📸 Загружаю {media_type} в WordPress (попытка {attempt + 1}/{max_retries})...")
                
                # Используем НОВУЮ сессию для каждого запроса
                wp_upload_session = requests.Session()
                wp_upload_session.auth = (WP_USERNAME, WP_PASSWORD)
                
                # Важные заголовки для WordPress
                wp_upload_session.headers.update({
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                    'Accept': 'application/json',
                    'Connection': 'close'
                })
                
                files = {
                    'file': (filename, media_response.content, mime)
                }
                
                wp_response = wp_upload_session.post(
                    WP_MEDIA_URL,
                    files=files,
                    timeout=180,
                    allow_redirects=True,
                    verify=True
                )
                
                logger.info(f"📸 Ответ WP: статус {wp_response.status_code}")
                
                if wp_response.status_code == 201:
                    media_id = wp_response.json()['id']
                    source_url = wp_response.json().get('source_url', 'unknown')
                    logger.info(f"✅ {media_type.capitalize()} загружено! ID={media_id}, URL={source_url}")
                    return media_id, source_url
                elif wp_response.status_code == 500:
                    logger.warning(f"⚠️ WP вернул 500 ошибку, пробуем ещё раз...")
                    time.sleep(2 ** attempt)
                    continue
                else:
                    logger.error(f"❌ Ошибка WP при загрузке {media_type}: {wp_response.status_code}")
                    logger.error(f"Ответ: {wp_response.text[:500]}")
                    if attempt < max_retries - 1:
                        time.sleep(2 ** attempt)
                        continue
                    return None, None
                    
            except requests.exceptions.ConnectionError as e:
                logger.warning(f"⚠️ Ошибка соединения (попытка {attempt + 1}): {e}")
                if attempt < max_retries - 1:
                    time.sleep(3 ** attempt)
                    continue
                else:
                    logger.error(f"❌ Превышено число попыток: {e}")
                    return None, None
                    
            except requests.exceptions.Timeout as e:
                logger.warning(f"⚠️ Таймаут (попытка {attempt + 1}): {e}")
                if attempt < max_retries - 1:
                    time.sleep(3 ** attempt)
                    continue
                else:
                    logger.error(f"❌ Превышено число попыток: {e}")
                    return None, None
                    
            except Exception as e:
                logger.error(f"❌ Неизвестная ошибка: {e}")
                if attempt < max_retries - 1:
                    time.sleep(2 ** attempt)
                    continue
                else:
                    return None, None
        
        return None, None
            
    except Exception as e:
        logger.error(f"❌ Критическая ошибка загрузки медиа: {e}")
        return None, None

def download_and_upload_multiple_media(media_file_ids, is_video=False, max_retries=3):
    """Загрузка нескольких фото или видео в WordPress"""
    uploaded_media = []
    
    if not media_file_ids:
        return uploaded_media
    
    for idx, file_id in enumerate(media_file_ids):
        logger.info(f"📸 Загрузка медиа {idx + 1}/{len(media_file_ids)}")
        
        # Загружаем каждое медиа отдельно
        media_id, media_url = download_and_upload_media(file_id, is_video, max_retries)
        
        if media_id:
            uploaded_media.append({
                'id': media_id,
                'url': media_url,
                'index': idx
            })
        else:
            logger.warning(f"⚠️ Не удалось загрузить медиа {idx + 1}")
    
    logger.info(f"✅ Загружено {len(uploaded_media)} из {len(media_file_ids)} медиа")
    return uploaded_media

def create_wp_post(title, content, post_type, media_ids=None, video_url=None, publish=False, is_video=False, gallery_images=None):
    """Создание поста в WordPress с видео или галереей в контенте и SEO (Yoast)"""
    status = 'publish' if publish else 'draft'
    
    # Форматируем контент с медиа
    final_content = content
    if gallery_images and len(gallery_images) > 0:
        # Вставляем галерею в контент
        final_content = format_content_for_wp(content, None, gallery_images)
        logger.info(f"🖼️ Галерея из {len(gallery_images)} фото добавлена в контент")
    elif video_url:
        # Вставляем видео в контент
        final_content = format_content_for_wp(content, video_url, None)
        logger.info(f"🎬 Видео URL {video_url} вставлен в контент")
    
    # Генерируем SEO данные
    seo_title = title[:70]
    seo_description = re.sub(r'<[^>]+>', '', content)
    seo_description = re.sub(r'\[video[^\]]*\]', '', seo_description)
    seo_description = re.sub(r'\[gallery[^\]]*\]', '', seo_description)
    seo_description = ' '.join(seo_description.split())
    seo_description = seo_description[:160]
    if len(seo_description) > 160:
        seo_description = seo_description[:157] + "..."
    
    post_data = {
        'title': title,
        'content': final_content,
        'status': status,
        'type': post_type,
        'meta': {
            '_yoast_wpseo_title': seo_title,
            '_yoast_wpseo_metadesc': seo_description
        }
    }
    
    # Устанавливаем обложку (первое фото или видео)
    if media_ids and len(media_ids) > 0:
        # Первое медиа используем как обложку
        post_data['featured_media'] = media_ids[0]['id']
        logger.info(f"📎 Установлено первое медиа ID={media_ids[0]['id']} как обложка")
    
    try:
        logger.info(f"📤 Отправка в WordPress: раздел={post_type}, статус={status}")
        logger.info(f"🔍 SEO Заголовок: {seo_title}")
        logger.info(f"🔍 SEO Описание: {seo_description[:50]}...")
        
        # Используем новую сессию для поста
        wp_post_session = requests.Session()
        wp_post_session.auth = (WP_USERNAME, WP_PASSWORD)
        wp_post_session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'application/json'
        })
        
        response = wp_post_session.post(
            f"{WP_API_URL}/{post_type}",
            json=post_data,
            timeout=60
        )
        
        logger.info(f"📤 Ответ WP: {response.status_code}")
        
        if response.status_code == 201:
            post_link = response.json()['link']
            logger.info(f"✅ Пост создан: {post_link}")
            logger.info(f"✅ SEO данные добавлены (Yoast)")
            return True, post_link
        else:
            logger.error(f"❌ Ошибка: {response.status_code}")
            logger.error(f"Ответ: {response.text[:500]}")
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
                    
                    keyboard = {
                        "inline_keyboard": [
                            [{"text": "🤖 Переделать текст через ИИ", "callback_data": f"ai|{post_key}"}],
                            [{"text": "📝 Опубликовать в черновики", "callback_data": f"draft|{post_key}"}],
                            [{"text": "🌐 Опубликовать на сайт", "callback_data": f"publish|{post_key}"}]
                        ]
                    }
                    
                    section_name = POST_TYPES.get(post_type, post_type)
                    media_count = len(post_data.get('media_file_ids', []))
                    media_type = "видео" if post_data.get('is_video') else f"{media_count} фото" if media_count > 0 else "нет"
                    new_text = f"✅ Выбран раздел: {section_name}\n\n"
                    new_text += f"Заголовок: {post_data.get('title', 'Без заголовка')}\n\n"
                    new_text += f"Текст: {post_data.get('content', '')[:300]}...\n\n"
                    new_text += f"Медиа: {media_type}\n\n"
                    new_text += "Выбери действие:"
                    
                    tg_edit_message_text(chat_id, msg_id, new_text, json.dumps(keyboard))
                return
            
            if action == 'ai' and len(parts) >= 2:
                post_key = parts[1]
                post_data = pending_posts.get(post_key)
                
                if post_data:
                    tg_edit_message_text(chat_id, msg_id, "🤖 Обрабатываю текст через ИИ...")
                    processed = process_text_with_deepseek(post_data['original_text'])
                    
                    if processed:
                        title, content = extract_title_and_content(processed)
                        formatted_content = format_content_for_wp(content, None, None)
                        post_data['title'] = title
                        post_data['content'] = formatted_content
                        
                        keyboard = {
                            "inline_keyboard": [
                                [{"text": "🤖 Переделать текст через ИИ", "callback_data": f"ai|{post_key}"}],
                                [{"text": "📝 Опубликовать в черновики", "callback_data": f"draft|{post_key}"}],
                                [{"text": "🌐 Опубликовать на сайт", "callback_data": f"publish|{post_key}"}]
                            ]
                        }
                        
                        media_count = len(post_data.get('media_file_ids', []))
                        media_type = "видео" if post_data.get('is_video') else f"{media_count} фото" if media_count > 0 else "нет"
                        tg_edit_message_text(
                            chat_id, msg_id,
                            f"Заголовок: {title}\n\nТекст: {content}\n\nМедиа: {media_type}",
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
                
                # Загружаем все медиа
                uploaded_media = []
                video_url = None
                is_video = post_data.get('is_video', False)
                
                if post_data.get('media_file_ids'):
                    uploaded_media = download_and_upload_multiple_media(
                        post_data['media_file_ids'], 
                        is_video, 
                        max_retries=3
                    )
                    
                    if uploaded_media:
                        logger.info(f"✅ Загружено {len(uploaded_media)} медиа")
                        # Если это видео, берем URL первого
                        if is_video and len(uploaded_media) > 0:
                            video_url = uploaded_media[0]['url']
                    else:
                        logger.error("❌ Медиа НЕ загрузились!")
                
                # Создаем пост с галереей (если несколько фото) или с видео
                success, link = create_wp_post(
                    post_data['title'],
                    post_data['content'],
                    post_data['post_type'],
                    uploaded_media,  # Все загруженные медиа
                    video_url,
                    True,
                    is_video,
                    uploaded_media if not is_video and len(uploaded_media) > 1 else None  # Галерея только для фото (больше 1)
                )
                
                if success:
                    tg_edit_message_text(chat_id, msg_id, f"✅ Пост опубликован!\n\n{link}")
                else:
                    tg_edit_message_text(chat_id, msg_id, "❌ Ошибка публикации")
                
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
                
                # Загружаем все медиа
                uploaded_media = []
                video_url = None
                is_video = post_data.get('is_video', False)
                
                if post_data.get('media_file_ids'):
                    uploaded_media = download_and_upload_multiple_media(
                        post_data['media_file_ids'], 
                        is_video, 
                        max_retries=3
                    )
                    
                    if uploaded_media:
                        logger.info(f"✅ Загружено {len(uploaded_media)} медиа")
                        if is_video and len(uploaded_media) > 0:
                            video_url = uploaded_media[0]['url']
                
                # Создаем пост с галереей (если несколько фото) или с видео
                success, link = create_wp_post(
                    post_data['title'],
                    post_data['content'],
                    post_data['post_type'],
                    uploaded_media,
                    video_url,
                    False,
                    is_video,
                    uploaded_media if not is_video and len(uploaded_media) > 1 else None
                )
                
                if success:
                    tg_edit_message_text(chat_id, msg_id, f"✅ Пост сохранен в черновиках!\n\n{link}")
                else:
                    tg_edit_message_text(chat_id, msg_id, "❌ Ошибка сохранения")
                
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
            
            # Собираем все фото
            media_file_ids = []
            is_video = False
            
            if 'photo' in message:
                # Берем самое большое разрешение каждого фото
                photos = message['photo']
                if isinstance(photos, list):
                    # Если это массив фото (несколько фото в одном сообщении)
                    for photo in photos:
                        if isinstance(photo, list):
                            # Если photo - это массив размеров, берем последний (самый большой)
                            media_file_ids.append(photo[-1]['file_id'])
                        elif isinstance(photo, dict) and 'file_id' in photo:
                            media_file_ids.append(photo['file_id'])
                else:
                    # Одно фото
                    media_file_ids.append(photos[-1]['file_id'])
                is_video = False
                logger.info(f"📸 Обнаружено {len(media_file_ids)} ФОТО")
                
            elif 'video' in message:
                media_file_ids.append(message['video']['file_id'])
                is_video = True
                logger.info("🎬 Обнаружено ВИДЕО")
            
            if not text:
                tg_send_message(chat_id, "❌ Отправьте текст новости.\nПервая строка будет заголовком.")
                return
            
            title, content = extract_title_and_content(text)
            formatted_content = format_content_for_wp(content, None, None)
            
            post_key = str(int(time.time() * 1000))
            pending_posts[post_key] = {
                'original_text': text,
                'media_file_ids': media_file_ids,  # Массив ID файлов
                'is_video': is_video,
                'title': title,
                'content': formatted_content,
                'media_uploaded': []  # Будем хранить загруженные медиа ID
            }
            
            keyboard = {
                "inline_keyboard": []
            }
            for pt_key, pt_name in POST_TYPES.items():
                keyboard["inline_keyboard"].append([{"text": pt_name, "callback_data": f"select_post_type|{post_key}|{pt_key}"}])
            
            media_count = len(media_file_ids)
            media_type = "видео" if is_video else f"{media_count} фото" if media_count > 0 else "нет"
            tg_send_message(
                chat_id,
                f"📢 Пост получен!\n\n"
                f"Заголовок: {title}\n\n"
                f"Текст: {content[:300]}...\n\n"
                f"Медиа: {media_type}\n\n"
                f"📂 Выбери раздел для публикации:",
                json.dumps(keyboard)
            )
            logger.info(f"✉️ Отправлен выбор раздела, медиа={media_type}")
            
    except Exception as e:
        logger.error(f"Ошибка: {e}")

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
    logger.info(f"🖼️ Поддержка галереи: ✅ (несколько фото в галерею)")
    logger.info(f"🎬 Поддержка видео: ✅ (шорткод + обложка)")
    logger.info(f"🔍 SEO (Yoast): ✅ (автоматическое заполнение)")
    
    requests.post(f"{TG_API_URL}/deleteWebhook")
    requests.post(f"{TG_API_URL}/setWebhook", json={'url': webhook_url})
    logger.info("✅ Вебхук установлен")
    
    port = int(os.getenv('PORT', 8000))
    app.run(host='0.0.0.0', port=port)
