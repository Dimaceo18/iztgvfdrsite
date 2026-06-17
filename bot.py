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

def format_content_for_wp(text, video_url=None):
    """Форматирование контента для WordPress - убираем все пустые строки"""
    if not text:
        return ""
    
    # Убираем множественные переносы строк, оставляем только по одному
    text = re.sub(r'\n\s*\n', '\n', text)
    
    # Разбиваем на строки и убираем пустые
    lines = [line.strip() for line in text.split('\n') if line.strip()]
    
    formatted = []
    video_inserted = False
    
    for i, line in enumerate(lines):
        line = re.sub(r'(https?://[^\s]+)', r'<a href="\1">\1</a>', line)
        line = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', line)
        line = re.sub(r'\*(.+?)\*', r'<em>\1</em>', line)
        formatted.append(f'<p>{line}</p>')
        
        # Вставляем видео после первого абзаца
        if i == 0 and video_url and not video_inserted:
            formatted.append(f'[video width="100%" height="auto" mp4="{video_url}"]')
            video_inserted = True
    
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
        
        # Загружаем через multipart/form-data
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

def create_wp_post(title, content, post_type, media_id=None, video_url=None, publish=False, is_video=False):
    """Создание поста в WordPress с видео в контенте"""
    status = 'publish' if publish else 'draft'
    
    # Форматируем контент с видео
    final_content = content
    if is_video and video_url:
        final_content = format_content_for_wp(content, video_url)
        logger.info(f"🎬 Видео URL {video_url} вставлен в контент")
    
    post_data = {
        'title': title,
        'content': final_content,
        'status': status,
        'type': post_type,
    }
    
    # Если есть медиа ID, устанавливаем как обложку
    if media_id:
        post_data['featured_media'] = media_id
        media_type = "видео" if is_video else "фото"
        logger.info(f"📎 Устанавливаю {media_type} ID={media_id} как обложку")
    
    try:
        logger.info(f"📤 Отправка в WordPress: раздел={post_type}, статус={status}")
        
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
            if is_video:
                if media_id:
                    logger.info(f"🎬 Видео вставлено в контент, ID={media_id} как обложка")
                else:
                    logger.info(f"🎬 Видео вставлено в контент (шорткод)")
            return True, post_link
        else:
            logger.error(f"❌ Ошибка: {response.status_code}")
            logger.error(f"Ответ: {response.text[:200]}")
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
            
            # Выбор раздела
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
                    media_type = "видео" if post_data.get('is_video') else "фото"
                    new_text = f"✅ Выбран раздел: {section_name}\n\n"
                    new_text += f"Заголовок: {post_data.get('title', 'Без заголовка')}\n\n"
                    new_text += f"Текст: {post_data.get('content', '')[:300]}...\n\n"
                    new_text += f"{media_type.capitalize()}: {'есть' if post_data.get('media_file_id') else 'нет'}\n\n"
                    new_text += "Выбери действие:"
                    
                    tg_edit_message_text(chat_id, msg_id, new_text, json.dumps(keyboard))
                return
            
            # Обработка через ИИ
            if action == 'ai' and len(parts) >= 2:
                post_key = parts[1]
                post_data = pending_posts.get(post_key)
                
                if post_data:
                    tg_edit_message_text(chat_id, msg_id, "🤖 Обрабатываю текст через ИИ...")
                    processed = process_text_with_deepseek(post_data['original_text'])
                    
                    if processed:
                        title, content = extract_title_and_content(processed)
                        formatted_content = format_content_for_wp(content, None)
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
                        tg_edit_message_text(
                            chat_id, msg_id,
                            f"Заголовок: {title}\n\nТекст: {content}\n\n{media_type.capitalize()}: {'есть' if post_data.get('media_file_id') else 'нет'}",
                            json.dumps(keyboard)
                        )
                    else:
                        tg_edit_message_text(chat_id, msg_id, "❌ Ошибка ИИ")
                return
            
            # Публикация на сайт
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
                
                media_id = None
                video_url = None
                if post_data.get('media_file_id'):
                    media_id, video_url = download_and_upload_media(post_data['media_file_id'], post_data.get('is_video', False))
                    if media_id:
                        logger.info(f"✅ Медиа загружено с ID={media_id}, URL={video_url}")
                    else:
                        logger.error("❌ Медиа НЕ загрузилось!")
                else:
                    logger.info("📸 Нет медиа для загрузки")
                
                success, link = create_wp_post(
                    post_data['title'],
                    post_data['content'],
                    post_data['post_type'],
                    media_id,
                    video_url,
                    True,
                    post_data.get('is_video', False)
                )
                
                if success:
                    tg_edit_message_text(chat_id, msg_id, f"✅ Пост опубликован!\n\n{link}")
                else:
                    tg_edit_message_text(chat_id, msg_id, "❌ Ошибка публикации")
                
                del pending_posts[post_key]
                return
            
            # Черновик
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
                
                media_id = None
                video_url = None
                if post_data.get('media_file_id'):
                    media_id, video_url = download_and_upload_media(post_data['media_file_id'], post_data.get('is_video', False))
                
                success, link = create_wp_post(
                    post_data['title'],
                    post_data['content'],
                    post_data['post_type'],
                    media_id,
                    video_url,
                    False,
                    post_data.get('is_video', False)
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
            
            media_file_id = None
            is_video = False
            
            if 'photo' in message:
                media_file_id = message['photo'][-1]['file_id']
                is_video = False
                logger.info("📸 Обнаружено ФОТО")
            elif 'video' in message:
                media_file_id = message['video']['file_id']
                is_video = True
                logger.info("🎬 Обнаружено ВИДЕО")
            
            if not text:
                tg_send_message(chat_id, "❌ Отправьте текст новости.\nПервая строка будет заголовком.")
                return
            
            title, content = extract_title_and_content(text)
            formatted_content = format_content_for_wp(content, None)
            
            post_key = str(int(time.time() * 1000))
            pending_posts[post_key] = {
                'original_text': text,
                'media_file_id': media_file_id,
                'is_video': is_video,
                'title': title,
                'content': formatted_content
            }
            
            keyboard = {
                "inline_keyboard": []
            }
            for pt_key, pt_name in POST_TYPES.items():
                keyboard["inline_keyboard"].append([{"text": pt_name, "callback_data": f"select_post_type|{post_key}|{pt_key}"}])
            
            media_type = "видео" if is_video else "фото" if media_file_id else "нет"
            tg_send_message(
                chat_id,
                f"📢 Пост получен!\n\n"
                f"Заголовок: {title}\n\n"
                f"Текст: {content[:300]}...\n\n"
                f"{media_type.capitalize()}: {'есть' if media_file_id else 'нет'}\n\n"
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
    logger.info(f"🎬 Поддержка видео: ✅ (шорткод + обложка)")
    
    requests.post(f"{TG_API_URL}/deleteWebhook")
    requests.post(f"{TG_API_URL}/setWebhook", json={'url': webhook_url})
    logger.info("✅ Вебхук установлен")
    
    port = int(os.getenv('PORT', 8000))
    app.run(host='0.0.0.0', port=port)
