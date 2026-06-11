import os
import requests
import logging
import re
import time
import io
import json
from datetime import datetime
from flask import Flask, request, jsonify
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from dotenv import load_dotenv
from openai import AsyncOpenAI

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ==================== КОНФИГУРАЦИЯ ====================
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
ADMIN_ID = os.getenv('YOUR_TELEGRAM_ID')
DEEPSEEK_API_KEY = os.getenv('DEEPSEEK_API_KEY')
CHANNEL_ID = os.getenv('CHANNEL_ID')

# WordPress настройки
WP_URL = os.getenv('WP_URL')
WP_USERNAME = os.getenv('WP_USERNAME')
WP_PASSWORD = os.getenv('WP_PASSWORD')
WP_API_URL = f"{WP_URL}/wp-json/wp/v2"
WP_MEDIA_URL = f"{WP_URL}/wp-json/wp/v2/media"

# DeepSeek API
DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"

# Flask приложение
app = Flask(__name__)

# Сессия для WordPress
wp_session = requests.Session()
wp_session.headers.update({'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'})

# Бот и хранилища
bot = Bot(token=TELEGRAM_TOKEN)
pending_posts = {}
user_sessions = {}

# API URL для Telegram
TG_API_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

# Промпт для DeepSeek
DEEPSEEK_PROMPT = """Перепиши новость в формате на 600-650 символов.

Правила:
- Удали смайлики и рекламу
- Разбей на 2-3 абзаца (пустая строка между абзацами)
- Сохрани главные факты
- Заголовок короткий и информативный

ВАЖНО: НЕ пиши слова "Заголовок:" и "Текст:". Просто напиши сначала заголовок, потом пустую строку, потом текст."""

# ==================== ТЕЛЕГРАМ ФУНКЦИИ ====================
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

def remove_emojis(text: str) -> str:
    if not text:
        return ""
    emoji_pattern = re.compile(
        "["
        "\U0001F600-\U0001F64F"
        "\U0001F300-\U0001F5FF"
        "\U0001F680-\U0001F6FF"
        "\U0001F1E0-\U0001F1FF"
        "\U00002702-\U000027B0"
        "\U000024C2-\U0001F251"
        "\U0001F900-\U0001F9FF"
        "\U0001FA70-\U0001FAFF"
        "]+",
        flags=re.UNICODE
    )
    return emoji_pattern.sub(r'', text)

# ==================== ОБРАБОТКА ИИ ====================
def process_text_with_deepseek(text: str) -> str:
    """Синхронная обработка текста через DeepSeek"""
    if not DEEPSEEK_API_KEY:
        return "❌ API ключ DeepSeek не настроен."
    
    try:
        response = requests.post(
            DEEPSEEK_API_URL,
            headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": "deepseek-chat",
                "messages": [
                    {"role": "system", "content": DEEPSEEK_PROMPT},
                    {"role": "user", "content": f"Перепиши эту новость в формате на 600-650 символов:\n\n{text}"}
                ],
                "temperature": 0.7,
                "max_tokens": 1000
            },
            timeout=60
        )
        if response.status_code == 200:
            result = response.json()["choices"][0]["message"]["content"]
            result = re.sub(r'^Вот.*?:', '', result, flags=re.IGNORECASE)
            result = re.sub(r'^#+\s+', '', result, flags=re.MULTILINE)
            result = result.strip()
            return result
        return f"❌ Ошибка API: {response.status_code}"
    except Exception as e:
        return f"❌ Ошибка: {str(e)}"

def extract_title_and_content(text):
    if not text:
        return "Новый пост", ""
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

# ==================== WORDPRESS ФУНКЦИИ ====================
def upload_media_to_wp(file_id, is_video=False):
    """Загрузка медиа в WordPress"""
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
        
        media_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"
        media_data = requests.get(media_url, timeout=60)
        if media_data.status_code != 200:
            return None
        
        ext = 'mp4' if is_video else 'jpg'
        wp_response = wp_session.post(
            WP_MEDIA_URL,
            auth=(WP_USERNAME, WP_PASSWORD),
            headers={'Content-Disposition': f'attachment; filename="media_{int(time.time())}.{ext}"'},
            data=media_data.content,
            timeout=60
        )
        
        if wp_response.status_code == 201:
            return wp_response.json()['id']
    except Exception as e:
        logger.error(f"Ошибка загрузки медиа: {e}")
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
            logger.error(f"Ошибка {response.status_code}")
            return False, None
    except Exception as e:
        logger.error(f"Ошибка: {e}")
        return False, None

def publish_to_channel(chat_id, text, media_file_id=None, is_video=False):
    """Публикация в Telegram канал"""
    try:
        if media_file_id:
            get_file = requests.get(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile",
                params={'file_id': media_file_id},
                timeout=30
            )
            if get_file.status_code == 200:
                file_path = get_file.json().get('result', {}).get('file_path')
                if file_path:
                    media_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"
                    if is_video:
                        url = f"{TG_API_URL}/sendVideo"
                    else:
                        url = f"{TG_API_URL}/sendPhoto"
                    requests.post(url, json={'chat_id': chat_id, 'photo' if not is_video else 'video': media_url, 'caption': text, 'parse_mode': 'HTML'}, timeout=60)
                    return True
        tg_send_message(chat_id, text, parse_mode='HTML')
        return True
    except Exception as e:
        logger.error(f"Ошибка публикации: {e}")
        return False

# ==================== КНОПКИ ====================
def get_main_keyboard():
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 Написать новость", callback_data="write_news")],
        [InlineKeyboardButton("📢 Показать последний пост", callback_data="show_last")]
    ])
    return keyboard

def get_post_preview_keyboard():
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🤖 Обработать текст ИИ", callback_data="ai_process")],
        [InlineKeyboardButton("✏️ Редактировать текст", callback_data="edit_text")],
        [InlineKeyboardButton("📢 Опубликовать в канал", callback_data="tochannel")],
        [InlineKeyboardButton("🌐 На сайт", callback_data="topublish")],
        [InlineKeyboardButton("📝 На сайт (Черновик)", callback_data="todraft")]
    ])
    return keyboard

def get_ai_result_keyboard(post_key):
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Опубликовать в канал", callback_data=f"tochannel_{post_key}")],
        [InlineKeyboardButton("🌐 На сайт", callback_data=f"topublish_{post_key}")],
        [InlineKeyboardButton("📝 На сайт (Черновик)", callback_data=f"todraft_{post_key}")],
        [InlineKeyboardButton("🔄 Переделать текст", callback_data=f"reprocess_{post_key}")],
        [InlineKeyboardButton("✏️ Редактировать вручную", callback_data=f"edit_{post_key}")]
    ])
    return keyboard

# ==================== ОБРАБОТЧИКИ ====================
@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        update = request.get_json(force=True)
        logger.info("🔔 Вебхук получен")
        
        # Обработка callback_query
        if 'callback_query' in update:
            callback = update['callback_query']
            data = callback['data']
            message = callback['message']
            callback_id = callback['id']
            chat_id = message['chat']['id']
            msg_id = message['message_id']
            
            tg_answer_callback_query(callback_id)
            
            parts = data.split('_')
            action = parts[0]
            
            # Обработка кнопок с постом
            if action == 'reprocess':
                post_key = parts[1]
                post_data = pending_posts.get(post_key)
                if post_data:
                    tg_edit_message_text(chat_id, msg_id, "⏳ Обрабатываю текст через ИИ...")
                    processed = process_text_with_deepseek(post_data['original_text'])
                    if processed.startswith("❌"):
                        tg_edit_message_text(chat_id, msg_id, processed)
                        return jsonify({'status': 'ok'})
                    
                    title, content = extract_title_and_content(processed)
                    post_data['title'] = title
                    post_data['content'] = format_content_for_wp(content)
                    post_data['processed_text'] = processed
                    
                    msg = f"📢 *Результат обработки ИИ*\n\n📰 *{title}*\n\n📝 {processed}\n\n📸 Фото: {'✅ есть' if post_data.get('media_file_id') else '❌ нет'}\n\nВыбери действие:"
                    tg_edit_message_text(chat_id, msg_id, msg, reply_markup=get_ai_result_keyboard(post_key).to_json(), parse_mode='Markdown')
            
            elif action == 'tochannel':
                post_key = parts[1]
                post_data = pending_posts.get(post_key)
                if post_data:
                    tg_edit_message_text(chat_id, msg_id, "⏳ Публикую в канал...")
                    success = publish_to_channel(
                        CHANNEL_ID,
                        f"<b>{post_data['title']}</b>\n\n{post_data.get('processed_text', post_data.get('original_text'))}",
                        post_data.get('media_file_id'),
                        post_data.get('is_video', False)
                    )
                    tg_edit_message_text(chat_id, msg_id, "✅ Опубликовано в канал!" if success else "❌ Ошибка")
                    pending_posts.pop(post_key, None)
            
            elif action == 'topublish':
                post_key = parts[1]
                post_data = pending_posts.get(post_key)
                if post_data:
                    tg_edit_message_text(chat_id, msg_id, "⏳ Публикую на сайт...")
                    
                    media_id = None
                    if post_data.get('media_file_id'):
                        media_id = upload_media_to_wp(post_data['media_file_id'], post_data.get('is_video', False))
                    
                    success, link = create_wp_post(
                        post_data['title'],
                        post_data['content'],
                        media_id,
                        'publish'
                    )
                    if success:
                        tg_edit_message_text(chat_id, msg_id, f"✅ Опубликовано на сайте!\n\nСсылка: {link}")
                    else:
                        tg_edit_message_text(chat_id, msg_id, "❌ Ошибка публикации на сайт")
                    pending_posts.pop(post_key, None)
            
            elif action == 'todraft':
                post_key = parts[1]
                post_data = pending_posts.get(post_key)
                if post_data:
                    tg_edit_message_text(chat_id, msg_id, "⏳ Сохраняю в черновики...")
                    
                    media_id = None
                    if post_data.get('media_file_id'):
                        media_id = upload_media_to_wp(post_data['media_file_id'], post_data.get('is_video', False))
                    
                    success, link = create_wp_post(
                        post_data['title'],
                        post_data['content'],
                        media_id,
                        'draft'
                    )
                    if success:
                        tg_edit_message_text(chat_id, msg_id, f"✅ Сохранено в черновиках!\n\nСсылка: {link}")
                    else:
                        tg_edit_message_text(chat_id, msg_id, "❌ Ошибка сохранения")
                    pending_posts.pop(post_key, None)
            
            elif action == 'ai_process':
                post_key = parts[1]
                post_data = pending_posts.get(post_key)
                if post_data:
                    tg_edit_message_text(chat_id, msg_id, "🤖 Обрабатываю текст через ИИ...")
                    processed = process_text_with_deepseek(post_data['original_text'])
                    if processed.startswith("❌"):
                        tg_edit_message_text(chat_id, msg_id, processed)
                        return jsonify({'status': 'ok'})
                    
                    title, content = extract_title_and_content(processed)
                    post_data['title'] = title
                    post_data['content'] = format_content_for_wp(content)
                    post_data['processed_text'] = processed
                    
                    msg = f"📢 *Результат обработки ИИ*\n\n📰 *{title}*\n\n📝 {processed}\n\n📸 Фото: {'✅ есть' if post_data.get('media_file_id') else '❌ нет'}\n\nВыбери действие:"
                    tg_edit_message_text(chat_id, msg_id, msg, reply_markup=get_ai_result_keyboard(post_key).to_json(), parse_mode='Markdown')
            
            elif action == 'write_news':
                tg_edit_message_text(chat_id, msg_id, "📝 Отправь мне текст новости (можно с фото или видео).\n\nПервая строка станет заголовком.")
                user_sessions[chat_id] = {"awaiting_news": True}
            
            elif action == 'show_last':
                tg_edit_message_text(chat_id, msg_id, "📢 Пока нет сохранённых постов.")
        
        # Обработка нового сообщения
        elif 'message' in update:
            message = update['message']
            chat_id = message['chat']['id']
            user_id = message['from']['id']
            
            if str(user_id) != ADMIN_ID:
                tg_send_message(chat_id, "❌ У вас нет прав.")
                return jsonify({'status': 'ok'})
            
            # Получаем текст и медиа
            text = message.get('caption') or message.get('text', '')
            media_file_id = None
            is_video = False
            
            if 'photo' in message:
                media_file_id = message['photo'][-1]['file_id']
            elif 'video' in message:
                media_file_id = message['video']['file_id']
                is_video = True
            
            if not text:
                tg_send_message(chat_id, "❌ Отправьте текст новости.\nПервая строка будет заголовком.")
                return jsonify({'status': 'ok'})
            
            # Обработка текста ИИ
            tg_send_message(chat_id, "🤖 Обрабатываю текст через ИИ (600-650 символов)...")
            processed = process_text_with_deepseek(text)
            
            if processed.startswith("❌"):
                tg_send_message(chat_id, processed)
                return jsonify({'status': 'ok'})
            
            title, content = extract_title_and_content(processed)
            formatted_content = format_content_for_wp(content)
            
            post_key = str(int(time.time() * 1000))
            pending_posts[post_key] = {
                'title': title,
                'content': formatted_content,
                'processed_text': processed,
                'original_text': text,
                'media_file_id': media_file_id,
                'is_video': is_video
            }
            
            msg = f"📢 *Результат обработки ИИ*\n\n📰 *{title}*\n\n📝 {processed}\n\n📸 Фото/видео: {'✅ есть' if media_file_id else '❌ нет'}\n\nВыбери действие:"
            tg_send_message(chat_id, msg, reply_markup=get_ai_result_keyboard(post_key).to_json(), parse_mode='Markdown')
        
        return jsonify({'status': 'ok'})
    except Exception as e:
        logger.error(f"Ошибка: {e}")
        return jsonify({'status': 'error'}), 500

@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'healthy'})

@app.route('/', methods=['GET'])
def index():
    return jsonify({'status': 'Bot is running', 'mode': 'AI news editor + WordPress'})

if __name__ == '__main__':
    render_url = os.getenv('RENDER_EXTERNAL_URL')
    webhook_url = f"{render_url}/webhook"
    
    logger.info(f"🚀 ЗАПУСК БОТА...")
    logger.info(f"🔗 Вебхук: {webhook_url}")
    logger.info(f"🌐 WordPress: {WP_URL}")
    logger.info(f"📢 Канал: {CHANNEL_ID}")
    logger.info(f"🤖 DeepSeek: {'✅' if DEEPSEEK_API_KEY else '❌'}")
    
    # Установка вебхука
    requests.post(f"{TG_API_URL}/deleteWebhook")
    requests.post(f"{TG_API_URL}/setWebhook", json={'url': webhook_url})
    logger.info("✅ Вебхук установлен")
    
    port = int(os.getenv('PORT', 8000))
    app.run(host='0.0.0.0', port=port)
