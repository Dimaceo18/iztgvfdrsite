import os
import requests
import logging
import re
import time
import json
from flask import Flask, request, jsonify
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
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

# WordPress API
WP_API_URL = f"{WP_URL}/wp-json/wp/v2"
WP_MEDIA_URL = f"{WP_URL}/wp-json/wp/v2/media"

app = Flask(__name__)
wp_session = requests.Session()
wp_session.headers.update({'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'})

# Хранилище временных постов
pending_posts = {}

# Создаём бота
bot = Bot(token=TELEGRAM_TOKEN)

# Базовый URL для Telegram API
TG_API_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

def tg_send_message(chat_id, text, reply_markup=None, parse_mode=None):
    url = f"{TG_API_URL}/sendMessage"
    data = {'chat_id': chat_id, 'text': text}
    if reply_markup:
        data['reply_markup'] = reply_markup
    if parse_mode:
        data['parse_mode'] = parse_mode
    return requests.post(url, json=data, timeout=30)

def tg_send_photo(chat_id, photo_url, caption=None):
    url = f"{TG_API_URL}/sendPhoto"
    data = {'chat_id': chat_id, 'photo': photo_url}
    if caption:
        data['caption'] = caption
        data['parse_mode'] = 'HTML'
    return requests.post(url, json=data, timeout=60)

def tg_send_video(chat_id, video_url, caption=None):
    url = f"{TG_API_URL}/sendVideo"
    data = {'chat_id': chat_id, 'video': video_url}
    if caption:
        data['caption'] = caption
        data['parse_mode'] = 'HTML'
    return requests.post(url, json=data, timeout=60)

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

def download_and_upload_photo(file_id):
    try:
        get_file = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile",
            params={'file_id': file_id},
            timeout=30
        )
        if get_file.status_code != 200:
            return None
        file_path = get_file.json().get('result', {}).get('file_path')
        if not file_path:
            return None
        photo_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"
        photo_data = requests.get(photo_url, timeout=60)
        if photo_data.status_code != 200:
            return None
        wp_response = wp_session.post(
            WP_MEDIA_URL,
            auth=(WP_USERNAME, WP_PASSWORD),
            headers={'Content-Disposition': f'attachment; filename="photo_{int(time.time())}.jpg"'},
            data=photo_data.content,
            timeout=60
        )
        if wp_response.status_code == 201:
            return wp_response.json()['id']
    except Exception as e:
        logger.error(f"Ошибка фото: {e}")
    return None

def create_wp_draft(title, content, media_id=None):
    post_data = {
        'title': title,
        'content': content,
        'status': 'draft',
        'type': 'news',
    }
    if media_id:
        post_data['featured_media'] = media_id
    try:
        response = wp_session.post(
            f"{WP_API_URL}/news",
            auth=(WP_USERNAME, WP_PASSWORD),
            json=post_data,
            timeout=60
        )
        if response.status_code == 201:
            logger.info(f"✅ Черновик создан")
            return True
        else:
            logger.error(f"Ошибка: {response.status_code}")
            return False
    except Exception as e:
        logger.error(f"Ошибка: {e}")
        return False

def process_update(update_json):
    """Синхронная обработка update"""
    try:
        # Обработка callback_query (нажатие кнопки)
        if 'callback_query' in update_json:
            callback = update_json['callback_query']
            data = callback['data']
            message = callback['message']
            callback_id = callback['id']
            chat_id = message['chat']['id']
            msg_id = message['message_id']
            
            # Отвечаем на callback
            requests.post(f"{TG_API_URL}/answerCallbackQuery", json={'callback_query_id': callback_id}, timeout=30)
            
            action, post_key = data.split('_')
            post_data = pending_posts.get(post_key)
            
            if not post_data:
                requests.post(f"{TG_API_URL}/editMessageText", json={'chat_id': chat_id, 'message_id': msg_id, 'text': "❌ Пост не найден."}, timeout=30)
                return
            
            if action == 'draft':
                status_text = "сохранен в черновиках"
                result_text = "📝 Пост сохранен в Черновики"
            else:
                status_text = "опубликован"
                result_text = "🚀 Пост опубликован на сайте"
            
            requests.post(f"{TG_API_URL}/editMessageText", json={'chat_id': chat_id, 'message_id': msg_id, 'text': f"⏳ {status_text}..."}, timeout=30)
            
            success = create_wp_draft(
                post_data['title'],
                post_data['content'],
                post_data.get('media_id')
            )
            
            if success:
                requests.post(f"{TG_API_URL}/editMessageText", json={'chat_id': chat_id, 'message_id': msg_id, 'text': f"✅ Готово!\n\n{result_text}\n\nЗаголовок: {post_data['title'][:100]}"}, timeout=30)
                logger.info(f"✅ Пост {status_text}")
            else:
                requests.post(f"{TG_API_URL}/editMessageText", json={'chat_id': chat_id, 'message_id': msg_id, 'text': f"❌ Ошибка! Не удалось {status_text} пост.\n\n💡 Проверьте подключение к WordPress."}, timeout=30)
            
            del pending_posts[post_key]
        
        # Обработка сообщения
        elif 'message' in update_json:
            message = update_json['message']
            chat_id = message['chat']['id']
            user_id = message['from']['id']
            
            # Проверяем, что сообщение от админа
            if str(user_id) != ADMIN_ID:
                tg_send_message(chat_id, "❌ У вас нет прав.")
                return
            
            # Получаем текст
            text = message.get('caption') or message.get('text', '')
            
            if not text:
                tg_send_message(chat_id, "❌ Отправьте текст новости.\nПервая строка будет заголовком.")
                return
            
            # Обработка фото
            media_id = None
            if 'photo' in message:
                photo = message['photo'][-1]
                tg_send_message(chat_id, "⏳ Загружаю фото...")
                media_id = download_and_upload_photo(photo['file_id'])
            
            title, content_text = extract_title_and_content(text)
            formatted_content = format_content_for_wp(content_text)
            
            post_key = str(int(time.time() * 1000))
            pending_posts[post_key] = {
                'title': title,
                'content': formatted_content,
                'media_id': media_id
            }
            
            keyboard = {
                "inline_keyboard": [
                    [
                        {"text": "📝 В Черновики", "callback_data": f"draft_{post_key}"},
                        {"text": "🚀 Опубликовать", "callback_data": f"publish_{post_key}"}
                    ]
                ]
            }
            
            preview = f"📢 Предпросмотр новости\n\n"
            preview += f"Заголовок: {title}\n\n"
            preview += f"Текст:\n{content_text[:300]}{'...' if len(content_text) > 300 else ''}\n\n"
            preview += f"Фото: {'есть' if media_id else 'нет'}\n\n"
            preview += f"Выбери действие:"
            
            tg_send_message(chat_id, preview, json.dumps(keyboard))
            logger.info(f"✉️ Отправлены кнопки выбора")
            
    except Exception as e:
        logger.error(f"Ошибка обработки: {e}")

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
    logger.info(f"👤 Админ ID: {ADMIN_ID}")
    
    # Установка вебхука
    requests.post(f"{TG_API_URL}/deleteWebhook")
    requests.post(f"{TG_API_URL}/setWebhook", json={'url': webhook_url})
    logger.info("✅ Вебхук установлен")
    
    port = int(os.getenv('PORT', 8000))
    app.run(host='0.0.0.0', port=port)
