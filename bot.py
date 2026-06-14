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

# Доступные разделы (типы записей)
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

def tg_edit_message_text(chat_id, message_id, text, reply_markup=None, parse_mode=None):
    url = f"{TG_API_URL}/editMessageText"
    data = {'chat_id': chat_id, 'message_id': message_id, 'text': text}
    if reply_markup:
        data['reply_markup'] = reply_markup
    if parse_mode:
        data['parse_mode'] = parse_mode
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

def format_content_for_wp(text):
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
    """Загрузка фото в WordPress"""
    try:
        get_file_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile"
        file_response = requests.get(get_file_url, params={'file_id': file_id}, timeout=30)
        
        if file_response.status_code != 200:
            logger.error(f"Ошибка getFile: {file_response.status_code}")
            return None
        
        result = file_response.json().get('result')
        if not result:
            logger.error("Не получен result от Telegram")
            return None
        
        file_path = result.get('file_path')
        if not file_path:
            logger.error("Не получен file_path")
            return None
        
        photo_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"
        logger.info(f"Скачивание фото...")
        
        download_headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        
        photo_response = requests.get(photo_url, headers=download_headers, timeout=60)
        
        if photo_response.status_code != 200:
            logger.error(f"Ошибка скачивания фото: {photo_response.status_code}")
            return None
        
        content_type = photo_response.headers.get('content-type', 'image/jpeg')
        
        wp_headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'application/json',
            'Accept-Language': 'ru-RU,ru;q=0.9,en;q=0.8',
            'Content-Type': content_type,
            'Content-Disposition': f'attachment; filename="telegram_photo_{int(time.time())}.jpg"'
        }
        
        wp_response = wp_session.post(
            WP_MEDIA_URL,
            auth=(WP_USERNAME, WP_PASSWORD),
            headers=wp_headers,
            data=photo_response.content,
            timeout=60
        )
        
        if wp_response.status_code == 201:
            media_id = wp_response.json()['id']
            logger.info(f"✅ Фото загружено, ID: {media_id}")
            return media_id
        else:
            logger.error(f"Ошибка WP при загрузке фото: {wp_response.status_code}")
            return None
            
    except Exception as e:
        logger.error(f"Ошибка фото: {e}")
        return None

def create_wp_post(title, content, post_type, media_id=None, publish=False):
    """Создание поста в WordPress в указанном разделе"""
    status = 'publish' if publish else 'draft'
    
    post_data = {
        'title': title,
        'content': content,
        'status': status,
        'type': post_type,
    }
    
    if media_id:
        post_data['featured_media'] = media_id
    
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'application/json',
            'Content-Type': 'application/json',
        }
        
        logger.info(f"📤 Отправка в WordPress: раздел={post_type}, статус={status}")
        
        response = wp_session.post(
            f"{WP_API_URL}/{post_type}",
            auth=(WP_USERNAME, WP_PASSWORD),
            json=post_data,
            headers=headers,
            timeout=60
        )
        
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
        # Обработка callback_query
        if 'callback_query' in update_json:
            callback = update_json['callback_query']
            data = callback['data']
            message = callback['message']
            callback_id = callback['id']
            chat_id = message['chat']['id']
            msg_id = message['message_id']
            
            tg_answer_callback_query(callback_id)
            
            parts = data.split('_')
            action = parts[0]
            
            # Выбор раздела
            if action == 'select_post_type':
                post_key = parts[1]
                post_type = parts[2]
                post_data = pending_posts.get(post_key)
                
                if post_data:
                    post_data['post_type'] = post_type
                    
                    # Показываем финальные кнопки
                    keyboard = {
                        "inline_keyboard": [
                            [{"text": "✅ Опубликовать на сайт", "callback_data": f"publish_{post_key}"}],
                            [{"text": "📝 В черновики", "callback_data": f"draft_{post_key}"}],
                            [{"text": "🔄 Выбрать другой раздел", "callback_data": f"back_to_sections_{post_key}"}],
                            [{"text": "🤖 Обработать через ИИ", "callback_data": f"ai_{post_key}"}]
                        ]
                    }
                    
                    section_name = POST_TYPES.get(post_type, post_type)
                    tg_edit_message_text(
                        chat_id, msg_id,
                        f"✅ Выбран раздел: {section_name}\n\n"
                        f"<b>{post_data.get('title', 'Без заголовка')}</b>\n\n"
                        f"{post_data.get('content', '')[:200]}...\n\n"
                        f"Фото: {'✅ есть' if post_data.get('photo_file_id') else '❌ нет'}\n\n"
                        f"<i>Выбери действие:</i>",
                        json.dumps(keyboard), 'HTML'
                    )
                return
            
            # Показать список разделов
            if action == 'back_to_sections':
                post_key = parts[1]
                post_data = pending_posts.get(post_key)
                
                if post_data:
                    keyboard = {
                        "inline_keyboard": []
                    }
                    for pt_key, pt_name in POST_TYPES.items():
                        keyboard["inline_keyboard"].append([{"text": pt_name, "callback_data": f"select_post_type_{post_key}_{pt_key}"}])
                    
                    tg_edit_message_text(
                        chat_id, msg_id,
                        f"📂 <b>Выбери раздел для публикации:</b>\n\n"
                        f"Заголовок: {post_data.get('title', 'Без заголовка')[:50]}",
                        json.dumps(keyboard), 'HTML'
                    )
                return
            
            # Обработка через ИИ
            if action == 'ai':
                post_key = parts[1]
                post_data = pending_posts.get(post_key)
                
                if post_data:
                    tg_edit_message_text(chat_id, msg_id, "🤖 Обрабатываю текст через ИИ...")
                    processed = process_text_with_deepseek(post_data['original_text'])
                    
                    if processed:
                        title, content = extract_title_and_content(processed)
                        formatted_content = format_content_for_wp(content)
                        post_data['title'] = title
                        post_data['content'] = formatted_content
                        
                        # Показываем выбор раздела после ИИ
                        keyboard = {
                            "inline_keyboard": []
                        }
                        for pt_key, pt_name in POST_TYPES.items():
                            keyboard["inline_keyboard"].append([{"text": pt_name, "callback_data": f"select_post_type_{post_key}_{pt_key}"}])
                        
                        tg_edit_message_text(
                            chat_id, msg_id,
                            f"🤖 Текст обработан!\n\n"
                            f"<b>{title}</b>\n\n"
                            f"{content[:300]}...\n\n"
                            f"📂 <b>Выбери раздел для публикации:</b>",
                            json.dumps(keyboard), 'HTML'
                        )
                    else:
                        tg_edit_message_text(chat_id, msg_id, "❌ Ошибка ИИ")
                return
            
            # Публикация или черновик
            if action == 'publish' or action == 'draft':
                post_key = parts[1]
                post_data = pending_posts.get(post_key)
                
                if not post_data:
                    tg_edit_message_text(chat_id, msg_id, "❌ Пост не найден.")
                    return
                
                if not post_data.get('post_type'):
                    # Если раздел не выбран, показываем список
                    keyboard = {
                        "inline_keyboard": []
                    }
                    for pt_key, pt_name in POST_TYPES.items():
                        keyboard["inline_keyboard"].append([{"text": pt_name, "callback_data": f"select_post_type_{post_key}_{pt_key}"}])
                    
                    tg_edit_message_text(
                        chat_id, msg_id,
                        f"📂 <b>Сначала выбери раздел!</b>\n\n"
                        f"Заголовок: {post_data.get('title', 'Без заголовка')[:50]}",
                        json.dumps(keyboard), 'HTML'
                    )
                    return
                
                is_publish = (action == 'publish')
                status_text = "опубликован на сайте" if is_publish else "сохранен в черновиках"
                
                tg_edit_message_text(chat_id, msg_id, f"⏳ {status_text}...")
                
                media_id = None
                if post_data.get('photo_file_id'):
                    media_id = download_and_upload_photo(post_data['photo_file_id'])
                    if media_id:
                        logger.info(f"✅ Фото загружено, ID: {media_id}")
                
                success, link = create_wp_post(
                    post_data['title'],
                    post_data['content'],
                    post_data['post_type'],
                    media_id,
                    is_publish
                )
                
                if success:
                    tg_edit_message_text(chat_id, msg_id, f"✅ {status_text} в разделе {POST_TYPES.get(post_data['post_type'], post_data['post_type'])}!\n\n{link}")
                else:
                    tg_edit_message_text(chat_id, msg_id, f"❌ Ошибка {status_text}")
                
                del pending_posts[post_key]
        
        # Обработка нового сообщения
        elif 'message' in update_json:
            message = update_json['message']
            chat_id = message['chat']['id']
            user_id = message['from']['id']
            
            if str(user_id) != ADMIN_ID:
                tg_send_message(chat_id, "❌ У вас нет прав.")
                return
            
            text = message.get('caption') or message.get('text', '')
            photo_file_id = message['photo'][-1]['file_id'] if 'photo' in message else None
            
            if not text:
                tg_send_message(chat_id, "❌ Отправьте текст новости.\nПервая строка будет заголовком.")
                return
            
            title, content = extract_title_and_content(text)
            formatted_content = format_content_for_wp(content)
            
            post_key = str(int(time.time() * 1000))
            pending_posts[post_key] = {
                'original_text': text,
                'photo_file_id': photo_file_id,
                'title': title,
                'content': formatted_content
            }
            
            # Показываем выбор раздела
            keyboard = {
                "inline_keyboard": []
            }
            for pt_key, pt_name in POST_TYPES.items():
                keyboard["inline_keyboard"].append([{"text": pt_name, "callback_data": f"select_post_type_{post_key}_{pt_key}"}])
            
            tg_send_message(
                chat_id,
                f"📢 <b>Пост получен!</b>\n\n"
                f"<b>{title}</b>\n\n"
                f"{content[:200]}...\n\n"
                f"Фото: {'✅ есть' if photo_file_id else '❌ нет'}\n\n"
                f"📂 <b>Выбери раздел для публикации:</b>",
                json.dumps(keyboard), 'HTML'
            )
            logger.info(f"✉️ Отправлен выбор раздела")
            
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
    
    requests.post(f"{TG_API_URL}/deleteWebhook")
    requests.post(f"{TG_API_URL}/setWebhook", json={'url': webhook_url})
    logger.info("✅ Вебхук установлен")
    
    port = int(os.getenv('PORT', 8000))
    app.run(host='0.0.0.0', port=port)
