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
    """Форматирование контента - вставляем видео после первого абзаца"""
    if not text:
        return ""
    
    # Разбиваем на абзацы
    paragraphs = text.split('\n')
    formatted = []
    
    for para in paragraphs:
        para = para.strip()
        if para:
            para = re.sub(r'(https?://[^\s]+)', r'<a href="\1">\1</a>', para)
            para = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', para)
            para = re.sub(r'\*(.+?)\*', r'<em>\1</em>', para)
            formatted.append(f'<p>{para}</p>')
    
    # Вставляем видео после первого абзаца
    if video_url and len(formatted) > 0:
        video_html = f'<video controls width="100%"><source src="{video_url}" type="video/mp4"></video>'
        formatted.insert(1, video_html)
        logger.info(f"🎬 Видео вставлено в контент: {video_url}")
    
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

def download_and_upload_video(file_id):
    """Скачивание и загрузка видео в WordPress"""
    try:
        logger.info(f"🎬 НАЧАЛО ЗАГРУЗКИ ВИДЕО: file_id={file_id}")
        logger.info(f"📎 Тип file_id: {type(file_id)}, длина: {len(file_id) if file_id else 0}")
        
        # Шаг 1: Получаем путь к файлу
        get_file_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile"
        logger.info(f"📤 Запрос к Telegram: {get_file_url}?file_id={file_id[:30]}...")
        
        file_response = requests.get(get_file_url, params={'file_id': file_id}, timeout=30)
        
        logger.info(f"📥 Ответ Telegram: статус {file_response.status_code}")
        
        if file_response.status_code != 200:
            logger.error(f"❌ Ошибка getFile: {file_response.status_code}")
            logger.error(f"Ответ: {file_response.text[:200]}")
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
        
        # Шаг 2: Скачиваем видео
        video_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"
        logger.info(f"📸 Скачиваю видео...")
        
        video_response = requests.get(video_url, timeout=120)
        if video_response.status_code != 200:
            logger.error(f"❌ Ошибка скачивания: {video_response.status_code}")
            return None, None
        
        logger.info(f"✅ Видео скачано, размер: {len(video_response.content)} байт")
        
        # Шаг 3: Загружаем в WordPress
        files = {
            'file': (f'video_{int(time.time())}.mp4', video_response.content, 'video/mp4')
        }
        
        logger.info(f"📸 Загружаю видео в WordPress...")
        
        wp_response = wp_session.post(
            WP_MEDIA_URL,
            auth=(WP_USERNAME, WP_PASSWORD),
            files=files,
            timeout=120
        )
        
        logger.info(f"📸 Ответ WP: статус {wp_response.status_code}")
        
        if wp_response.status_code == 201:
            media_id = wp_response.json()['id']
            source_url = wp_response.json()['source_url']
            logger.info(f"✅ Видео загружено! ID={media_id}, URL={source_url}")
            return media_id, source_url
        else:
            logger.error(f"❌ Ошибка WP: {wp_response.status_code}")
            logger.error(f"Ответ: {wp_response.text[:200]}")
            return None, None
            
    except Exception as e:
        logger.error(f"❌ Ошибка загрузки видео: {e}")
        import traceback
        traceback.print_exc()
        return None, None

def create_wp_post(title, content, post_type, media_id=None, video_url=None, publish=False):
    """Создание поста в WordPress с видео"""
    status = 'publish' if publish else 'draft'
    
    # Форматируем контент с видео
    final_content = content
    if video_url:
        final_content = format_content_for_wp(content, video_url)
        logger.info(f"🎬 Видео URL вставлен в контент: {video_url}")
    
    post_data = {
        'title': title,
        'content': final_content,
        'status': status,
        'type': post_type,
    }
    
    if media_id:
        post_data['featured_media'] = media_id
        logger.info(f"📎 Устанавливаю видео ID={media_id} как обложку")
    
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
                if post_data.get('is_video') and post_data.get('media_file_id'):
                    media_id, video_url = download_and_upload_video(post_data['media_file_id'])
                    if media_id:
                        logger.info(f"✅ Видео загружено! ID={media_id}, URL={video_url}")
                    else:
                        logger.error("❌ Видео НЕ загрузилось!")
                        tg_edit_message_text(chat_id, msg_id, "⚠️ Видео не загрузилось, публикую без видео")
                
                success, link = create_wp_post(
                    post_data['title'],
                    post_data['content'],
                    post_data['post_type'],
                    media_id,
                    video_url,
                    True
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
                if post_data.get('is_video') and post_data.get('media_file_id'):
                    media_id, video_url = download_and_upload_video(post_data['media_file_id'])
                
                success, link = create_wp_post(
                    post_data['title'],
                    post_data['content'],
                    post_data['post_type'],
                    media_id,
                    video_url,
                    False
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
    logger.info(f"🎬 Поддержка видео: ✅")
    
    requests.post(f"{TG_API_URL}/deleteWebhook")
    requests.post(f"{TG_API_URL}/setWebhook", json={'url': webhook_url})
    logger.info("✅ Вебхук установлен")
    
    port = int(os.getenv('PORT', 8000))
    app.run(host='0.0.0.0', port=port)
