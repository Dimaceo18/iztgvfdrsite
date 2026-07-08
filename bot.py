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
media_groups = defaultdict(dict)
group_timers = {}
scheduled_posts = {}
scheduled_timers = {}
video_pending = {}

# Базовый URL для Telegram API
TG_API_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

DEEPSEEK_PROMPT = """Ты редактор новостного сайта. Перепиши новость в строгом городском формате, объемом около 650 символов. Убери лишнюю воду, сделай интересный заголовок, никаких смайликов. Не используй символы # и ** в ответе. Сохрани главные факты. Расставь абзацы.

ВАЖНО: НЕ пиши слова "Заголовок:" и "Текст:". Просто напиши сначала заголовок, потом пустую строку, потом текст."""

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
            
            # Вставляем медиа ПОСЛЕ ПЕРВОГО абзаца
            if i == 0:
                if is_video and video_url:
                    # Для видео используем шорткод [video]
                    formatted.append(f'[video width="100%" height="auto" mp4="{video_url}"]')
                    logger.info(f"🎬 Видео вставлено в контент после 1-го абзаца: {video_url}")
                elif gallery_ids and len(gallery_ids) > 0:
                    # Для фото используем галерею
                    gallery_shortcode = '[gallery ids="' + ','.join(str(id) for id in gallery_ids) + '" size="full" columns="1"]'
                    formatted.append(gallery_shortcode)
                    logger.info(f"🖼️ Добавлена галерея из {len(gallery_ids)} фото после 1-го абзаца")
    
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

def create_wp_post(title, content, post_type, featured_media_id=None, video_url=None, publish=False, is_video=False, gallery_ids=None, schedule_time=None):
    """Создание поста в WordPress с видео или галереей в контенте и SEO (Yoast)"""
    status = 'future' if schedule_time else ('publish' if publish else 'draft')
    
    # Форматируем контент с медиа (видео вставляется после первого абзаца)
    final_content = content
    if is_video and video_url:
        # Для видео - вставляем шорткод после первого абзаца
        final_content = format_content_for_wp(content, video_url, None, is_video=True)
        logger.info(f"🎬 Видео вставлено в контент")
    elif gallery_ids and len(gallery_ids) > 0:
        # Для фото - галерея после первого абзаца
        final_content = format_content_for_wp(content, None, gallery_ids, is_video=False)
        logger.info(f"🖼️ Галерея из {len(gallery_ids)} фото добавлена в контент")
    
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
    
    # Добавляем время публикации если есть
    if schedule_time:
        post_data['date'] = schedule_time.isoformat()
        logger.info(f"⏰ Запланирована публикация на {schedule_time.strftime('%d.%m.%Y %H:%M')}")
    
    # Устанавливаем обложку (featured_media)
    if featured_media_id:
        post_data['featured_media'] = featured_media_id
        logger.info(f"📎 Устанавливаю обложку (featured_media) WP ID={featured_media_id}")
    
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
        # Получаем данные
        is_video = post_data.get('is_video', False)
        featured_media_id = post_data.get('featured_media_id')  # WP ID обложки
        video_file_id = post_data.get('video_file_id')
        media_file_ids = post_data.get('media_file_ids', [])
        
        video_url = None
        gallery_ids = []
        
        # Загружаем видео
        if is_video and video_file_id:
            logger.info(f"🎬 Загрузка видео...")
            media_id, video_url = download_and_upload_media(video_file_id, True)
            if media_id:
                logger.info(f"✅ Видео загружено, URL={video_url}")
            else:
                logger.error("❌ Не удалось загрузить видео")
        
        # Загружаем остальные медиа (фото), если есть
        for file_id in media_file_ids:
            if file_id != video_file_id:
                logger.info(f"📸 Загрузка фото...")
                media_id, _ = download_and_upload_media(file_id, False)
                if media_id:
                    gallery_ids.append(media_id)
                    logger.info(f"✅ Фото загружено, ID={media_id}")
        
        success, link = create_wp_post(
            post_data['title'],
            post_data['content'],
            post_data['post_type'],
            featured_media_id,  # Передаем ID обложки
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
        
        # Удаляем из хранилища
        del scheduled_posts[post_key]
        if post_key in scheduled_timers:
            del scheduled_timers[post_key]
            
    except Exception as e:
        logger.error(f"Ошибка публикации по расписанию: {e}")
        tg_send_message(chat_id, f"❌ Ошибка публикации по расписанию: {str(e)[:100]}")

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
    
    post_key = str(int(time.time() * 1000))
    pending_posts[post_key] = {
        'original_text': text,
        'media_file_ids': media_file_ids,
        'is_video': is_video,
        'title': title,
        'content': content,
        'featured_media_id': None
    }
    
    # Клавиатура с выбором категории
    keyboard = get_post_type_keyboard(post_key)
    
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
                    section_name = POST_TYPES.get(post_type, post_type)
                    
                    # Проверяем, есть ли уже обложка
                    if post_data.get('featured_media_id'):
                        # Если обложка уже есть - показываем действия
                        keyboard = get_action_keyboard(post_key)
                        media_count = len(post_data.get('media_file_ids', []))
                        media_type = "видео" if post_data.get('is_video') else f"{media_count} фото" if media_count > 0 else "нет"
                        new_text = f"✅ Выбран раздел: {section_name}\n\n"
                        new_text += f"Заголовок: {post_data.get('title', 'Без заголовка')}\n\n"
                        new_text += f"Текст: {post_data.get('content', '')[:300]}...\n\n"
                        new_text += f"Медиа: {media_type}\n\n"
                        new_text += "Выбери действие:"
                        
                        tg_edit_message_text(chat_id, msg_id, new_text, json.dumps(keyboard))
                    else:
                        # Если это видео - запрашиваем фото для обложки
                        if post_data.get('is_video'):
                            # Запрашиваем фото для обложки
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
                            # Обычный пост с фото - показываем действия
                            keyboard = get_action_keyboard(post_key)
                            media_count = len(post_data.get('media_file_ids', []))
                            media_type = "видео" if post_data.get('is_video') else f"{media_count} фото" if media_count > 0 else "нет"
                            new_text = f"✅ Выбран раздел: {section_name}\n\n"
                            new_text += f"Заголовок: {post_data.get('title', 'Без заголовка')}\n\n"
                            new_text += f"Текст: {post_data.get('content', '')[:300]}...\n\n"
                            new_text += f"Медиа: {media_type}\n\n"
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
                
                # Рассчитываем время публикации
                schedule_time = datetime.now() + timedelta(minutes=minutes)
                time_str = schedule_time.strftime('%d.%m.%Y %H:%M')
                
                # Сохраняем в отложенные
                scheduled_posts[post_key] = {
                    'title': post_data['title'],
                    'content': post_data['content'],
                    'post_type': post_data['post_type'],
                    'media_file_ids': post_data['media_file_ids'],
                    'is_video': post_data.get('is_video', False),
                    'chat_id': chat_id,
                    'msg_id': msg_id,
                    'featured_media_id': post_data.get('featured_media_id'),
                    'video_file_id': post_data.get('video_file_id')
                }
                
                # Запускаем таймер
                timer = threading.Timer(minutes * 60, publish_scheduled_post, args=[post_key])
                scheduled_timers[post_key] = timer
                timer.start()
                
                # Удаляем из pending
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
                
                # Получаем данные
                is_video = post_data.get('is_video', False)
                featured_media_id = post_data.get('featured_media_id')  # WP ID обложки
                video_file_id = post_data.get('video_file_id')
                media_file_ids = post_data.get('media_file_ids', [])
                
                video_url = None
                gallery_ids = []
                
                # Загружаем видео
                if is_video and video_file_id:
                    logger.info(f"🎬 Загрузка видео...")
                    media_id, video_url = download_and_upload_media(video_file_id, True)
                    if media_id:
                        logger.info(f"✅ Видео загружено, URL={video_url}")
                    else:
                        logger.error("❌ Не удалось загрузить видео")
                        tg_edit_message_text(chat_id, msg_id, "❌ Ошибка загрузки видео")
                        return
                
                # Загружаем остальные медиа (фото), если есть
                for file_id in media_file_ids:
                    if file_id != video_file_id:
                        logger.info(f"📸 Загрузка фото...")
                        media_id, _ = download_and_upload_media(file_id, False)
                        if media_id:
                            gallery_ids.append(media_id)
                            logger.info(f"✅ Фото загружено, ID={media_id}")
                
                success, link = create_wp_post(
                    post_data['title'],
                    post_data['content'],
                    post_data['post_type'],
                    featured_media_id,  # Передаем ID обложки
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
                
                # Очищаем ожидание фото, если оно было
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
                
                # Получаем данные
                is_video = post_data.get('is_video', False)
                featured_media_id = post_data.get('featured_media_id')
                video_file_id = post_data.get('video_file_id')
                media_file_ids = post_data.get('media_file_ids', [])
                
                video_url = None
                gallery_ids = []
                
                # Загружаем видео
                if is_video and video_file_id:
                    logger.info(f"🎬 Загрузка видео...")
                    media_id, video_url = download_and_upload_media(video_file_id, True)
                    if media_id:
                        logger.info(f"✅ Видео загружено, URL={video_url}")
                    else:
                        logger.error("❌ Не удалось загрузить видео")
                        tg_edit_message_text(chat_id, msg_id, "❌ Ошибка загрузки видео")
                        return
                
                # Загружаем остальные медиа (фото), если есть
                for file_id in media_file_ids:
                    if file_id != video_file_id:
                        logger.info(f"📸 Загрузка фото...")
                        media_id, _ = download_and_upload_media(file_id, False)
                        if media_id:
                            gallery_ids.append(media_id)
                            logger.info(f"✅ Фото загружено, ID={media_id}")
                
                success, link = create_wp_post(
                    post_data['title'],
                    post_data['content'],
                    post_data['post_type'],
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
            
            # Проверяем наличие медиа
            has_photo = 'photo' in message
            has_video = 'video' in message
            
            # Если это медиа-группа (альбом)
            if media_group_id and has_photo:
                photos = message['photo']
                if photos and len(photos) > 0:
                    file_id = photos[-1]['file_id']
                    
                    if media_group_id not in media_groups:
                        media_groups[media_group_id] = {
                            'file_ids': [],
                            'text': '',
                            'is_video': False,
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
            
            # Одиночное фото (без media_group_id)
            if has_photo and not media_group_id:
                # Проверяем, ожидаем ли мы фото для видео
                video_key = None                
                for key in list(video_pending.keys()):
                    if video_pending[key].get('chat_id') == chat_id:
                        video_key = key
                        break
                
                if video_key:
                    # Это фото для обложки к видео
                    photos = message['photo']
                    if photos and len(photos) > 0:
                        file_id = photos[-1]['file_id']
                        
                        # Загружаем фото в WordPress сразу как обложку
                        logger.info(f"📸 Загружаю фото обложки в WordPress...")
                        featured_media_id, thumbnail_url = download_and_upload_media(file_id, False)
                        
                        if featured_media_id:
                            # Сохраняем WP ID фото для обложки
                            pending_data = video_pending[video_key]
                            post_key = pending_data['post_key']
                            msg_id = pending_data.get('msg_id')
                            
                            if post_key in pending_posts:
                                pending_posts[post_key]['featured_media_id'] = featured_media_id
                                # Добавляем фото в media_file_ids для загрузки в контент (если нужно)
                                if file_id not in pending_posts[post_key]['media_file_ids']:
                                    pending_posts[post_key]['media_file_ids'].append(file_id)
                                logger.info(f"📸 Получено фото для обложки, WP ID={featured_media_id}, post_key={post_key}")
                                
                                # Показываем сообщение об успехе и продолжаем обработку
                                keyboard = get_action_keyboard(post_key)
                                post_data = pending_posts[post_key]
                                section_name = POST_TYPES.get(post_data.get('post_type'), 'Не выбран')
                                
                                new_text = f"✅ Фото для обложки получено!\n\n"
                                new_text += f"🎬 Видео с обложкой\n\n"
                                new_text += f"✅ Выбран раздел: {section_name}\n\n"
                                new_text += f"Заголовок: {post_data.get('title', 'Без заголовка')}\n\n"
                                new_text += f"Текст: {post_data.get('content', '')[:200]}...\n\n"
                                new_text += "Выбери действие:"
                                
                                if msg_id:
                                    tg_edit_message_text(chat_id, msg_id, new_text, json.dumps(keyboard))
                                else:
                                    tg_send_message(chat_id, new_text, json.dumps(keyboard))
                                
                                # Удаляем из ожидания
                                del video_pending[video_key]
                            else:
                                tg_send_message(chat_id, "❌ Пост не найден. Попробуйте отправить видео заново.")
                                if video_key in video_pending:
                                    del video_pending[video_key]
                        else:
                            tg_send_message(chat_id, "❌ Не удалось загрузить фото обложки. Попробуйте другое фото.")
                    return
                else:
                    # Обычное одиночное фото
                    photos = message['photo']
                    if photos and len(photos) > 0:
                        # Загружаем фото в WordPress сразу
                        logger.info(f"📸 Загружаю фото в WordPress...")
                        featured_media_id, _ = download_and_upload_media(photos[-1]['file_id'], False)
                        
                        if featured_media_id:
                            # Обрабатываем как обычный пост с фото
                            if text:
                                title, content = extract_title_and_content(text)
                                
                                post_key = str(int(time.time() * 1000))
                                pending_posts[post_key] = {
                                    'original_text': text,
                                    'media_file_ids': [photos[-1]['file_id']],
                                    'is_video': False,
                                    'title': title,
                                    'content': content,
                                    'featured_media_id': featured_media_id  # Сохраняем ID обложки
                                }
                                
                                # Клавиатура с выбором категории
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
                                tg_send_message(chat_id, "❌ Отправьте текст новости.\nПервая строка будет заголовком.")
                        else:
                            tg_send_message(chat_id, "❌ Не удалось загрузить фото.")
                    return
            
            # Видео
            elif has_video:
                video_file_id = message['video']['file_id']
                media_file_ids = [video_file_id]
                is_video = True
                logger.info("🎬 Обнаружено ВИДЕО")
                
                # Если есть текст - обрабатываем
                if text:
                    title, content = extract_title_and_content(text)
                    
                    post_key = str(int(time.time() * 1000))
                    pending_posts[post_key] = {
                        'original_text': text,
                        'media_file_ids': media_file_ids,
                        'is_video': is_video,
                        'title': title,
                        'content': content,
                        'featured_media_id': None,  # Будет заполнено после получения фото
                        'video_file_id': video_file_id  # Сохраняем file_id видео отдельно!
                    }
                    
                    # Сначала запрашиваем раздел
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
                    # Нет текста - запрашиваем текст
                    tg_send_message(chat_id, "❌ Отправьте текст новости к видео.\nПервая строка будет заголовком.")
                    return
            
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
    
    app.run(host='0.0.0.0', port=5000)
