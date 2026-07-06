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
media_groups = defaultdict(dict)  # Для сбора фото из альбомов
group_timers = {}  # Таймеры для групп

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

def format_content_for_wp(text, video_url=None, gallery_ids=None):
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
                elif gallery_ids and len(gallery_ids) > 0:
                    gallery_shortcode = '[gallery ids="' + ','.join(str(id) for id in gallery_ids) + '" size="full" columns="1"]'
                    formatted.append(gallery_shortcode)
                    logger.info(f"🖼️ Добавлена галерея из {len(gallery_ids)} фото")
    
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

def download_and_upload_media(file_id, is_video=False):
    """Загрузка фото или видео в WordPress"""
    try:
        media_type = "видео" if is_video else "фото"
        logger.info(f"📸 НАЧАЛО ЗАГРУЗКИ {media_type.upper()}: file_id={file_id}")
        
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
        
        media_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"
        logger.info(f"📸 Скачиваю {media_type}...")
        
        media_response = requests.get(media_url, timeout=120)
        if media_response.status_code != 200:
            logger.error(f"❌ Ошибка скачивания {media_type}: {media_response.status_code}")
            return None, None
        
        logger.info(f"✅ {media_type.capitalize()} скачано, размер: {len(media_response.content)} байт")
        
        ext = 'mp4' if is_video else 'jpg'
        mime = 'video/mp4' if is_video else 'image/jpeg'
        files = {
            'file': (f'{media_type}_{int(time.time())}.{ext}', media_response.content, mime)
        }
        
        logger.info(f"📸 Загружаю {media_type} в WordPress...")
        
        wp_response = wp_session.post(
            WP_MEDIA_URL,
            auth=(WP_USERNAME, WP_PASSWORD),
            files=files,
            timeout=120
        )
        
        logger.info(f"📸 Ответ WP: статус {wp_response.status_code}")
        
        if wp_response.status_code == 201:
            media_id = wp_response.json()['id']
            source_url = wp_response.json().get('source_url', 'unknown')
            logger.info(f"✅ {media_type.capitalize()} загружено! ID={media_id}, URL={source_url}")
            return media_id, source_url
        else:
            logger.error(f"❌ Ошибка WP при загрузке {media_type}: {wp_response.status_code}")
            logger.error(f"Ответ: {wp_response.text[:200]}")
            return None, None
            
    except Exception as e:
        logger.error(f"❌ Ошибка загрузки медиа: {e}")
        return None, None

def create_wp_post(title, content, post_type, media_id=None, video_url=None, publish=False, is_video=False, gallery_ids=None):
    """Создание поста в WordPress с видео или галереей в контенте и SEO (Yoast)"""
    status = 'publish' if publish else 'draft'
    
    # Форматируем контент с медиа
    final_content = content
    if gallery_ids and len(gallery_ids) > 0:
        final_content = format_content_for_wp(content, None, gallery_ids)
        logger.info(f"🖼️ Галерея из {len(gallery_ids)} фото добавлена в контент")
    elif video_url:
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
    
    if media_id:
        post_data['featured_media'] = media_id
        logger.info(f"📎 Устанавливаю медиа ID={media_id} как обложку")
    
    try:
        logger.info(f"📤 Отправка в WordPress: раздел={post_type}, статус={status}")
        logger.info(f"🔍 SEO Заголовок: {seo_title}")
        logger.info(f"🔍 SEO Описание: {seo_description[:50]}...")
        
        response = wp_session.post(
            f"{WP_API_URL}/{post_type}",
            auth=(WP_USERNAME, WP_PASSWORD),
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
            logger.error(f"Ответ: {response.text[:200]}")
            return False, None
            
    except Exception as e:
        logger.error(f"❌ Ошибка: {e}")
        return False, None

def process_media_group(media_group_id):
    """Обработка собранной медиа-группы"""
    if media_group_id not in media_groups:
        return
    
    group_data = media_groups[media_group_id]
    chat_id = group_data.get('chat_id')
    
    # Проверяем, есть ли текст
    if not group_data.get('text'):
        tg_send_message(chat_id, "❌ Отправьте текст новости.\nПервая строка будет заголовком.")
        del media_groups[media_group_id]
        return
    
    # Собираем все file_id
    media_file_ids = group_data.get('file_ids', [])
    is_video = group_data.get('is_video', False)
    text = group_data.get('text', '')
    
    if not media_file_ids:
        tg_send_message(chat_id, "❌ Нет медиа для публикации.")
        del media_groups[media_group_id]
        return
    
    logger.info(f"📸 Обработка группы {media_group_id}: {len(media_file_ids)} файлов")
    
    # Обрабатываем как обычный пост
    title, content = extract_title_and_content(text)
    formatted_content = format_content_for_wp(content, None, None)
    
    post_key = str(int(time.time() * 1000))
    pending_posts[post_key] = {
        'original_text': text,
        'media_file_ids': media_file_ids,
        'is_video': is_video,
        'title': title,
        'content': formatted_content,
        'media_uploaded': []
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
    
    # Удаляем группу
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
                media_ids = []
                gallery_ids = []
                video_url = None
                is_video = post_data.get('is_video', False)
                
                if post_data.get('media_file_ids'):
                    for idx, file_id in enumerate(post_data['media_file_ids']):
                        logger.info(f"📸 Загрузка медиа {idx + 1}/{len(post_data['media_file_ids'])}")
                        media_id, media_url = download_and_upload_media(file_id, is_video)
                        
                        if media_id:
                            media_ids.append(media_id)
                            if is_video and idx == 0:
                                video_url = media_url
                            elif not is_video:
                                gallery_ids.append(media_id)
                            logger.info(f"✅ Медиа {idx + 1} загружено успешно")
                        else:
                            logger.warning(f"⚠️ Не удалось загрузить медиа {idx + 1}")
                    
                    if media_ids:
                        logger.info(f"✅ Загружено {len(media_ids)} медиа")
                    else:
                        logger.error("❌ Медиа НЕ загрузились!")
                
                # Для видео используем только первый ID как обложку
                media_id = media_ids[0] if media_ids and is_video else None
                # Для фото используем первый ID как обложку, все ID для галереи
                if not is_video and media_ids:
                    media_id = media_ids[0]
                
                success, link = create_wp_post(
                    post_data['title'],
                    post_data['content'],
                    post_data['post_type'],
                    media_id,
                    video_url,
                    True,
                    is_video,
                    gallery_ids if not is_video and len(gallery_ids) > 1 else None
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
                media_ids = []
                gallery_ids = []
                video_url = None
                is_video = post_data.get('is_video', False)
                
                if post_data.get('media_file_ids'):
                    for idx, file_id in enumerate(post_data['media_file_ids']):
                        logger.info(f"📸 Загрузка медиа {idx + 1}/{len(post_data['media_file_ids'])}")
                        media_id, media_url = download_and_upload_media(file_id, is_video)
                        
                        if media_id:
                            media_ids.append(media_id)
                            if is_video and idx == 0:
                                video_url = media_url
                            elif not is_video:
                                gallery_ids.append(media_id)
                            logger.info(f"✅ Медиа {idx + 1} загружено успешно")
                        else:
                            logger.warning(f"⚠️ Не удалось загрузить медиа {idx + 1}")
                
                # Для видео используем только первый ID как обложку
                media_id = media_ids[0] if media_ids and is_video else None
                # Для фото используем первый ID как обложку, все ID для галереи
                if not is_video and media_ids:
                    media_id = media_ids[0]
                
                success, link = create_wp_post(
                    post_data['title'],
                    post_data['content'],
                    post_data['post_type'],
                    media_id,
                    video_url,
                    False,
                    is_video,
                    gallery_ids if not is_video and len(gallery_ids) > 1 else None
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
            media_group_id = message.get('media_group_id')
            
            # Проверяем наличие медиа
            has_photo = 'photo' in message
            has_video = 'video' in message
            
            # Если это медиа-группа (альбом)
            if media_group_id and has_photo:
                # Получаем file_id самого большого фото
                photos = message['photo']
                if photos and len(photos) > 0:
                    file_id = photos[-1]['file_id']
                    
                    # Создаем или обновляем группу
                    if media_group_id not in media_groups:
                        media_groups[media_group_id] = {
                            'file_ids': [],
                            'text': '',
                            'is_video': False,
                            'chat_id': chat_id
                        }
                    
                    # Добавляем file_id (если его еще нет)
                    if file_id not in media_groups[media_group_id]['file_ids']:
                        media_groups[media_group_id]['file_ids'].append(file_id)
                    
                    # Сохраняем текст (если есть)
                    if text and not media_groups[media_group_id]['text']:
                        media_groups[media_group_id]['text'] = text
                    
                    logger.info(f"📸 Добавлено фото в группу {media_group_id}, всего {len(media_groups[media_group_id]['file_ids'])} фото")
                    
                    # Отменяем предыдущий таймер
                    if media_group_id in group_timers:
                        group_timers[media_group_id].cancel()
                    
                    # Запускаем новый таймер на 3 секунды
                    timer = threading.Timer(3.0, process_media_group, args=[media_group_id])
                    group_timers[media_group_id] = timer
                    timer.start()
                    
                    logger.info(f"⏳ Запущен таймер для группы {media_group_id} на 3 секунды")
                return
            
            # Одиночное фото (без media_group_id)
            if has_photo and not media_group_id:
                photos = message['photo']
                if photos and len(photos) > 0:
                    media_file_ids = [photos[-1]['file_id']]
                else:
                    media_file_ids = []
                is_video = False
                logger.info(f"📸 Обнаружено 1 ФОТО (не альбом)")
            
            # Видео
            elif has_video:
                media_file_ids = [message['video']['file_id']]
                is_video = True
                logger.info("🎬 Обнаружено ВИДЕО")
            else:
                media_file_ids = []
                is_video = False
            
            # Если нет текста и нет медиа - пропускаем
            if not text and not media_file_ids:
                return
            
            # Если нет текста, но есть медиа
            if not text and media_file_ids:
                tg_send_message(chat_id, "❌ Отправьте текст новости.\nПервая строка будет заголовком.")
                return
            
            # Если есть текст, обрабатываем
            if text and media_file_ids:
                title, content = extract_title_and_content(text)
                formatted_content = format_content_for_wp(content, None, None)
                
                post_key = str(int(time.time() * 1000))
                pending_posts[post_key] = {
                    'original_text': text,
                    'media_file_ids': media_file_ids,
                    'is_video': is_video,
                    'title': title,
                    'content': formatted_content,
                    'media_uploaded': []
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
