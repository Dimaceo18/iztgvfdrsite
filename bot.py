import os
import requests
import logging
import re
import time
import json
from flask import Flask, request, jsonify
from telegram import Bot
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

app = Flask(__name__)
wp_session = requests.Session()
wp_session.headers.update({'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'})

# Хранилище временных постов
pending_posts = {}

# Создаём бота
bot = Bot(token=TELEGRAM_TOKEN)

# Базовый URL для Telegram API
TG_API_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

# Промпт для DeepSeek
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

def process_text_with_deepseek(text: str) -> str:
    """Обработка текста через DeepSeek"""
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
            result = result.strip()
            return result
        return None
    except Exception as e:
        logger.error(f"Ошибка DeepSeek: {e}")
        return None

def extract_title_and_content(text):
    """Извлечение заголовка (первая строка) и текста (остальное)"""
    if not text:
        return "Новый пост", ""
    lines = text.strip().split('\n')
    title = lines[0].strip() if lines else "Новый пост"
    if len(title) > 180:
        title = title[:177] + "..."
    content = '\n'.join(lines[1:]).strip() if len(lines) > 1 else ""
    return title, content

def format_content_for_wp(text):
    """Форматирование для WordPress с HTML"""
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
    """Загрузка фото в WordPress"""
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

def create_wp_post(title, content, media_id=None, status='draft'):
    """Создание поста в WordPress"""
    post_data = {
        'title': title,
        'content': content,
        'status': status,
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
            return True, response.json()['link']
        else:
            logger.error(f"Ошибка: {response.status_code}")
            return False, None
    except Exception as e:
        logger.error(f"Ошибка: {e}")
        return False, None

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
            
            tg_answer_callback_query(callback_id)
            
            parts = data.split('_')
            action = parts[0]
            post_key = parts[1] if len(parts) > 1 else None
            
            post_data = pending_posts.get(post_key)
            if not post_data:
                tg_edit_message_text(chat_id, msg_id, "❌ Пост не найден.")
                return
            
            # Обработка ИИ
            if action == 'ai':
                tg_edit_message_text(chat_id, msg_id, "🤖 Обрабатываю текст через ИИ...")
                processed = process_text_with_deepseek(post_data['original_text'])
                
                if processed:
                    title, content = extract_title_and_content(processed)
                    post_data['title'] = title
                    post_data['content'] = format_content_for_wp(content)
                    post_data['processed_text'] = processed
                    
                    keyboard = {
                        "inline_keyboard": [
                            [{"text": "✅ Опубликовать на сайт", "callback_data": f"publish_{post_key}"}],
                            [{"text": "📝 В черновики", "callback_data": f"draft_{post_key}"}],
                            [{"text": "🔄 Ещё раз через ИИ", "callback_data": f"ai_{post_key}"}]
                        ]
                    }
                    
                    msg = f"<b>{title}</b>\n\n{content}"
                    tg_edit_message_text(chat_id, msg_id, msg, json.dumps(keyboard), 'HTML')
                else:
                    tg_edit_message_text(chat_id, msg_id, "❌ Ошибка ИИ. Отправляю без обработки...")
                    # Отправляем без обработки
                    title, content = extract_title_and_content(post_data['original_text'])
                    post_data['title'] = title
                    post_data['content'] = format_content_for_wp(content)
                    keyboard = {
                        "inline_keyboard": [
                            [{"text": "✅ Опубликовать на сайт", "callback_data": f"publish_{post_key}"}],
                            [{"text": "📝 В черновики", "callback_data": f"draft_{post_key}"}]
                        ]
                    }
                    msg = f"<b>{title}</b>\n\n{content}"
                    tg_edit_message_text(chat_id, msg_id, msg, json.dumps(keyboard), 'HTML')
                return
            
            # Публикация на сайт
            if action == 'publish':
                tg_edit_message_text(chat_id, msg_id, "⏳ Публикую на сайт...")
                media_id = None
                if post_data.get('photo_file_id'):
                    media_id = download_and_upload_photo(post_data['photo_file_id'])
                success, link = create_wp_post(
                    post_data['title'],
                    post_data['content'],
                    media_id,
                    'publish'
                )
                if success:
                    tg_edit_message_text(chat_id, msg_id, f"✅ Опубликовано!\n\n{link}")
                else:
                    tg_edit_message_text(chat_id, msg_id, "❌ Ошибка публикации")
                del pending_posts[post_key]
            
            # Черновик
            elif action == 'draft':
                tg_edit_message_text(chat_id, msg_id, "⏳ Сохраняю в черновики...")
                media_id = None
                if post_data.get('photo_file_id'):
                    media_id = download_and_upload_photo(post_data['photo_file_id'])
                success, link = create_wp_post(
                    post_data['title'],
                    post_data['content'],
                    media_id,
                    'draft'
                )
                if success:
                    tg_edit_message_text(chat_id, msg_id, f"✅ Сохранено в черновиках!\n\n{link}")
                else:
                    tg_edit_message_text(chat_id, msg_id, "❌ Ошибка сохранения")
                del pending_posts[post_key]
        
        # Обработка нового сообщения
        elif 'message' in update_json:
            message = update_json['message']
            chat_id = message['chat']['id']
            user_id = message['from']['id']
            
            # Проверяем права
            if str(user_id) != ADMIN_ID:
                tg_send_message(chat_id, "❌ У вас нет прав.")
                return
            
            # Получаем текст
            text = message.get('caption') or message.get('text', '')
            photo_file_id = None
            
            if 'photo' in message:
                photo_file_id = message['photo'][-1]['file_id']
            
            if not text:
                tg_send_message(chat_id, "❌ Отправьте текст новости.\nПервая строка будет заголовком.")
                return
            
            # Сохраняем пост
            post_key = str(int(time.time() * 1000))
            pending_posts[post_key] = {
                'original_text': text,
                'title': None,
                'content': None,
                'photo_file_id': photo_file_id
            }
            
            # Кнопки выбора
            keyboard = {
                "inline_keyboard": [
                    [{"text": "🤖 Обработать через ИИ", "callback_data": f"ai_{post_key}"}],
                    [{"text": "📝 Без ИИ (в черновики)", "callback_data": f"draft_{post_key}"}]
                ]
            }
            
            preview = f"📢 <b>Новый пост получен!</b>\n\n"
            preview += f"<b>Текст:</b>\n{text[:300]}{'...' if len(text) > 300 else ''}\n\n"
            preview += f"<b>Фото:</b> {'✅ есть' if photo_file_id else '❌ нет'}\n\n"
            preview += f"<i>Выбери действие:</i>"
            
            tg_send_message(chat_id, preview, json.dumps(keyboard), 'HTML')
            logger.info(f"✉️ Отправлены кнопки выбора")
            
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
    logger.info(f"👤 Админ ID: {ADMIN_ID}")
    logger.info(f"🤖 DeepSeek: {'✅' if DEEPSEEK_API_KEY else '❌'}")
    
    # Установка вебхука
    requests.post(f"{TG_API_URL}/deleteWebhook")
    requests.post(f"{TG_API_URL}/setWebhook", json={'url': webhook_url})
    logger.info("✅ Вебхук установлен")
    
    port = int(os.getenv('PORT', 8000))
    app.run(host='0.0.0.0', port=port)
