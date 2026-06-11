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
ADMIN_ID = os.getenv('YOUR_TELEGRAM_ID')
DEEPSEEK_API_KEY = os.getenv('DEEPSEEK_API_KEY')
CHANNEL_ID = os.getenv('CHANNEL_ID')

# API DeepSeek
DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"

app = Flask(__name__)

# Хранилище временных постов
pending_posts = {}

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

def process_text_with_deepseek(text):
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

def publish_to_channel(chat_id, text, media_file_id=None, is_video=False):
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
                post_data['title'] = new_title
                post_data['processed_text'] = processed_text
                
                keyboard = {
                    "inline_keyboard": [
                        [{"text": "🔄 Переделать текст еще раз", "callback_data": f"reprocess_{post_key}"}],
                        [{"text": "📢 Опубликовать в канал", "callback_data": f"tochannel_{post_key}"}]
                    ]
                }
                
                msg = f"📢 <b>Новость после обработки ИИ</b>\n\n"
                msg += f"<b>Заголовок:</b> {new_title}\n\n"
                msg += f"<b>Текст:</b>\n{processed_text}\n\n"
                msg += f"<b>Медиа:</b> {'✅ есть' if post_data['media_file_id'] else '❌ нет'}\n\n"
                msg += f"<i>Выбери действие:</i>"
                
                tg_edit_message_text(chat_id, msg_id, msg, json.dumps(keyboard), 'HTML')
                return jsonify({'status': 'ok'})
            
            # Публикация в канал
            if action == 'tochannel':
                tg_edit_message_text(chat_id, msg_id, "⏳ Публикую в канал...")
                success = publish_to_channel(
                    CHANNEL_ID,
                    f"<b>{post_data['title']}</b>\n\n{post_data['processed_text']}",
                    post_data.get('media_file_id'),
                    post_data.get('is_video', False)
                )
                if success:
                    tg_edit_message_text(chat_id, msg_id, "✅ Новость опубликована в канал!")
                else:
                    tg_edit_message_text(chat_id, msg_id, "❌ Ошибка публикации в канал")
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
            
            # Получаем медиа (фото или видео)
            media_file_id = None
            is_video = False
            
            if 'photo' in message:
                media_file_id = message['photo'][-1]['file_id']
            elif 'video' in message:
                media_file_id = message['video']['file_id']
                is_video = True
            
            if not original_text:
                tg_send_message(chat_id, "❌ Отправьте текст новости.\nПервая строка будет заголовком.")
                return jsonify({'status': 'ok'})
            
            tg_send_message(chat_id, "⏳ Обрабатываю текст через ИИ...")
            processed_text = process_text_with_deepseek(original_text)
            
            if processed_text.startswith("❌"):
                tg_send_message(chat_id, processed_text)
                return jsonify({'status': 'ok'})
            
            title, content_text = extract_title_and_content(processed_text)
            
            post_key = str(int(time.time() * 1000))
            pending_posts[post_key] = {
                'title': title,
                'processed_text': processed_text,
                'original_text': original_text,
                'media_file_id': media_file_id,
                'is_video': is_video
            }
            
            keyboard = {
                "inline_keyboard": [
                    [{"text": "🔄 Переделать текст еще раз", "callback_data": f"reprocess_{post_key}"}],
                    [{"text": "📢 Опубликовать в канал", "callback_data": f"tochannel_{post_key}"}]
                ]
            }
            
            msg = f"📢 <b>Новость после обработки ИИ</b>\n\n"
            msg += f"<b>Заголовок:</b> {title}\n\n"
            msg += f"<b>Текст:</b>\n{processed_text}\n\n"
            msg += f"<b>Медиа:</b> {'✅ есть' if media_file_id else '❌ нет'}\n\n"
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
    return jsonify({'status': 'Bot is running', 'mode': 'AI news editor to Telegram channel'})

if __name__ == '__main__':
    render_url = os.getenv('RENDER_EXTERNAL_URL')
    webhook_url = f"{render_url}/webhook"
    
    logger.info(f"🚀 ЗАПУСК БОТА...")
    logger.info(f"🔗 Вебхук: {webhook_url}")
    logger.info(f"📢 Канал: {CHANNEL_ID}")
    logger.info(f"🤖 DeepSeek: {'✅' if DEEPSEEK_API_KEY else '❌'}")
    
    requests.post(f"{TG_API_URL}/deleteWebhook")
    requests.post(f"{TG_API_URL}/setWebhook", json={'url': webhook_url})
    logger.info("✅ Вебхук установлен")
    
    port = int(os.getenv('PORT', 8000))
    app.run(host='0.0.0.0', port=port)
