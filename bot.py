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
WP_URL = os.getenv('WP_URL')
WP_USERNAME = os.getenv('WP_USERNAME')
WP_PASSWORD = os.getenv('WP_PASSWORD')
ADMIN_ID = os.getenv('YOUR_TELEGRAM_ID')
DEEPSEEK_API_KEY = os.getenv('DEEPSEEK_API_KEY')
CHANNEL_ID = os.getenv('CHANNEL_ID')

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

# Базовый URL для Telegram API
TG_API_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

def tg_send_message(chat_id, text, reply_markup=None, parse_mode=None):
    """Синхронная отправка сообщения в Telegram"""
    url = f"{TG_API_URL}/sendMessage"
    data = {'chat_id': chat_id, 'text': text}
    if reply_markup:
        data['reply_markup'] = reply_markup
    if parse_mode:
        data['parse_mode'] = parse_mode
    return requests.post(url, json=data, timeout=30)

def tg_edit_message_text(chat_id, message_id, text, reply_markup=None, parse_mode=None):
    """Синхронное редактирование сообщения в Telegram"""
    url = f"{TG_API_URL}/editMessageText"
    data = {'chat_id': chat_id, 'message_id': message_id, 'text': text}
    if reply_markup:
        data['reply_markup'] = reply_markup
    if parse_mode:
        data['parse_mode'] = parse_mode
    return requests.post(url, json=data, timeout=30)

def tg_answer_callback_query(callback_id):
    """Синхронный ответ на callback query"""
    url = f"{TG_API_URL}/answerCallbackQuery"
    return requests.post(url, json={'callback_query_id': callback_id}, timeout=30)

def tg_send_photo(chat_id, photo_url, caption=None):
    """Синхронная отправка фото"""
    url = f"{TG_API_URL}/sendPhoto"
    data = {'chat_id': chat_id, 'photo': photo_url}
    if caption:
        data['caption'] = caption
        data['parse_mode'] = 'HTML'
    return requests.post(url, json=data, timeout=60)

def tg_send_video(chat_id, video_url, caption=None):
    """Синхронная отправка видео"""
    url = f"{TG_API_URL}/sendVideo"
    data = {'chat_id': chat_id, 'video': video_url}
    if caption:
        data['caption'] = caption
        data['parse_mode'] = 'HTML'
    return requests.post(url, json=data, timeout=60)

def process_text_with_deepseek(text):
    """Обработка текста через DeepSeek API (синхронно)"""
    if not DEEPSEEK_API_KEY:
        return "❌ API ключ DeepSeek не настроен."
    
    prompt = """Ты редактор новостного сайта. Перепиши новость в строгом городском формате, объемом около 650 символов. Убери лишнюю воду, сделай интересный заголовок, никаких смайликов. Не используй символы # и ** в ответе. Сохрани главные факты. Расставь абзацы.

Вот текст:"""
    
    try:
        response = requests.post(
            DEEPSEEK_API_URL,
            headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": "deepseek-chat", 
                "messages": [
                    {"role": "system", "content": "Ты редактор новостного сайта. Отвечай только готовым новостным текстом, без пояснений и вступлений. Не используй символы # и ** в ответе."}, 
                    {"role": "user", "content": f"{prompt}\n\n{text}"}
                ], 
                "temperature": 0.7, 
                "max_tokens": 1000
            },
            timeout=60
        )
        if response.status_code == 200:
            result = response.json()["choices"][0]["message"]["content"]
            # Очистка результата
            result = re.sub(r'^Вот обработанный новостной текст.*?:', '', result, flags=re.IGNORECASE)
            result = re.sub(r'^Вот.*?текст.*?:', '', result, flags=re.IGNORECASE)
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

def download_and_upload_media(file_id, is_video=False):
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
        content_type = 'video/mp4' if is_video else 'image/jpeg'
        
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
        logger.error(f"Ошибка медиа: {e}")
    return None

def create_wp_post(title, content, media_id=None, status='draft'):
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
    try:
        if media_file_id:
            # Получаем файл для публикации
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
                        tg_send_video(chat_id, media_url, text)
                    else:
                        tg_send_photo(chat_id, media_url, text)
                    return True
        tg_send_message(chat_id, text, parse_mode='HTML')
        return True
    except Exception as e:
        logger.error(f"Ошибка публикации в канал: {e}")
        return False

@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        update = request.get_json(force=True)
        logger.info("🔔 Вебхук получен")
        
        # Обработка callback_query (нажатие кнопки)
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
            post_key = parts[1]
            
            post_data = pending_posts.get(post_key)
            if not post_data:
                tg_edit_message_text(chat_id, msg_id, "❌ Пост не найден.")
                return jsonify({'status': 'ok'})
            
            # Кнопка "Переделать текст"
            if action == 'reprocess':
                tg_edit_message_text(chat_id, msg_id, "⏳ Обрабатываю текст через ИИ...")
                processed_text = process_text_with_deepseek(post_data['original_text'])
                
                if processed_text.startswith("❌"):
                    tg_edit_message_text(chat_id, msg_id, processed_text)
                    return jsonify({'status': 'ok'})
                
                new_title, new_content = extract_title_and_content(processed_text)
                pending_posts[post_key]['title'] = new_title
                pending_posts[post_key]['content'] = format_content_for_wp(new_content)
                pending_posts[post_key]['processed_text'] = processed_text
                
                keyboard = {
                    "inline_keyboard": [
                        [{"text": "🔄 Переделать текст еще раз", "callback_data": f"reprocess_{post_key}"}],
                        [{"text": "📢 Опубликовать в канал", "callback_data": f"tochannel_{post_key}"}, {"text": "🌐 На сайт", "callback_data": f"topublish_{post_key}"}],
                        [{"text": "📝 На сайт (Черновик)", "callback_data": f"todraft_{post_key}"}]
                    ]
                }
                
                msg = f"📢 <b>Новость после обработки ИИ</b>\n\n"
                msg += f"<b>Заголовок:</b> {new_title}\n\n"
                msg += f"<b>Текст:</b>\n{processed_text}\n\n"
                msg += f"<b>Медиа:</b> {'✅ есть' if post_data['media_id'] else '❌ нет'}\n\n"
                msg += f"<i>Выбери действие:</i>"
                
                tg_edit_message_text(chat_id, msg_id, msg, json.dumps(keyboard), 'HTML')
                return jsonify({'status': 'ok'})
            
            # Публикация в канал
            if action == 'tochannel':
                tg_edit_message_text(chat_id, msg_id, "⏳ Публикую в канал...")
                success = publish_to_channel(
                    CHANNEL_ID,
                    f"<b>{post_data['title']}</b>\n\n{post_data['processed_text']}",
                    post_data['raw_media_id'],
                    post_data.get('is_video', False)
                )
                if success:
                    tg_edit_message_text(chat_id, msg_id, "✅ Новость опубликована в канал!")
                else:
                    tg_edit_message_text(chat_id, msg_id, "❌ Ошибка публикации в канал")
                del pending_posts[post_key]
            
            # Публикация на сайт
            elif action == 'topublish':
                tg_edit_message_text(chat_id, msg_id, "⏳ Публикую на сайт...")
                success, link = create_wp_post(
                    post_data['title'],
                    post_data['content'],
                    post_data['media_id'],
                    'publish'
                )
                if success:
                    tg_edit_message_text(chat_id, msg_id, f"✅ Новость опубликована на сайте!\n\nСсылка: {link}")
                else:
                    tg_edit_message_text(chat_id, msg_id, "❌ Ошибка публикации на сайт")
                del pending_posts[post_key]
            
            # Черновик на сайте
            elif action == 'todraft':
                tg_edit_message_text(chat_id, msg_id, "⏳ Сохраняю в черновики...")
                success, link = create_wp_post(
                    post_data['title'],
                    post_data['content'],
                    post_data['media_id'],
                    'draft'
                )
                if success:
                    tg_edit_message_text(chat_id, msg_id, f"✅ Новость сохранена в черновиках!\n\nСсылка: {link}")
                else:
                    tg_edit_message_text(chat_id, msg_id, "❌ Ошибка сохранения в черновики")
                del pending_posts[post_key]
        
        # Обработка обычного сообщения
        elif 'message' in update:
            message = update['message']
            chat_id = message['chat']['id']
            user_id = message['from']['id']
            
            if str(user_id) != ADMIN_ID:
                tg_send_message(chat_id, "❌ У вас нет прав.")
                return jsonify({'status': 'ok'})
            
            original_text = message.get('caption') or message.get('text', '')
            
            media_id = None
            raw_media_id = None
            is_video = False
            
            if 'photo' in message:
                photo = message['photo'][-1]
                raw_media_id = photo['file_id']
                tg_send_message(chat_id, "⏳ Загружаю фото...")
                media_id = download_and_upload_media(raw_media_id, is_video=False)
            elif 'video' in message:
                video = message['video']
                raw_media_id = video['file_id']
                is_video = True
                tg_send_message(chat_id, "⏳ Загружаю видео...")
                media_id = download_and_upload_media(raw_media_id, is_video=True)
            
            if not original_text:
                tg_send_message(chat_id, "❌ Отправьте текст новости.\nПервая строка будет заголовком.")
                return jsonify({'status': 'ok'})
            
            tg_send_message(chat_id, "⏳ Обрабатываю текст через ИИ...")
            processed_text = process_text_with_deepseek(original_text)
            
            if processed_text.startswith("❌"):
                tg_send_message(chat_id, processed_text)
                return jsonify({'status': 'ok'})
            
            title, content_text = extract_title_and_content(processed_text)
            formatted_content = format_content_for_wp(content_text)
            
            post_key = str(int(time.time() * 1000))
            pending_posts[post_key] = {
                'title': title,
                'content': formatted_content,
                'processed_text': processed_text,
                'original_text': original_text,
                'media_id': media_id,
                'raw_media_id': raw_media_id,
                'is_video': is_video
            }
            
            keyboard = {
                "inline_keyboard": [
                    [{"text": "🔄 Переделать текст еще раз", "callback_data": f"reprocess_{post_key}"}],
                    [{"text": "📢 Опубликовать в канал", "callback_data": f"tochannel_{post_key}"}, {"text": "🌐 На сайт", "callback_data": f"topublish_{post_key}"}],
                    [{"text": "📝 На сайт (Черновик)", "callback_data": f"todraft_{post_key}"}]
                ]
            }
            
            msg = f"📢 <b>Новость после обработки ИИ</b>\n\n"
            msg += f"<b>Заголовок:</b> {title}\n\n"
            msg += f"<b>Текст:</b>\n{processed_text}\n\n"
            msg += f"<b>Медиа:</b> {'✅ есть' if media_id else '❌ нет'}\n\n"
            msg += f"<i>Выбери действие:</i>"
            
            tg_send_message(chat_id, msg, json.dumps(keyboard), 'HTML')
            logger.info(f"✉️ Отправлен запрос на публикацию")
        
        return jsonify({'status': 'ok'})
    except Exception as e:
        logger.error(f"Ошибка: {e}")
        return jsonify({'status': 'error'}), 500

@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'healthy'})

@app.route('/', methods=['GET'])
def index():
    return jsonify({'status': 'Bot is running', 'mode': 'AI news editor'})

if __name__ == '__main__':
    render_url = os.getenv('RENDER_EXTERNAL_URL')
    webhook_url = f"{render_url}/webhook"
    
    logger.info(f"🚀 ЗАПУСК БОТА...")
    logger.info(f"🔗 Вебхук: {webhook_url}")
    logger.info(f"🌐 WordPress: {WP_URL}")
    logger.info(f"📢 Канал: {CHANNEL_ID}")
    logger.info(f"🤖 DeepSeek: {'✅' if DEEPSEEK_API_KEY else '❌'}")
    
    # Установка вебхука через requests
    requests.post(f"{TG_API_URL}/deleteWebhook")
    requests.post(f"{TG_API_URL}/setWebhook", json={'url': webhook_url})
    logger.info("✅ Вебхук установлен")
    
    port = int(os.getenv('PORT', 8000))
    app.run(host='0.0.0.0', port=port)
