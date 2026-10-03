import os
import re
import asyncio
from telebot.async_telebot import AsyncTeleBot
import requests

BOT_TOKEN = os.getenv("BOT_TOKEN")
bot = AsyncTeleBot(BOT_TOKEN)

# Catch-all regex to pull out any web links sent to the chat room
UNIVERSAL_URL_REGEX = r'(https?://[^\s]+)'

@bot.message_handler(func=lambda message: True)
async def handle_messages(message):
    match = re.search(UNIVERSAL_URL_REGEX, message.text or "")
    if match:
        raw_url = match.group(1)
        url = raw_url.lower()
        
        # Guard clause: immediately ignore links that aren't core video platforms
        valid_platforms = ['instagram.com', 'facebook.com', 'fb.watch', 'tiktok.com', 'youtube.com', 'youtu.be']
        if not any(platform in url for platform in valid_platforms):
            return  
            
        # Trigger the native "uploading video..." status animation in Telegram
        await bot.send_chat_action(message.chat.id, 'upload_video')
        
        # 1. SPECIAL INLINE PARSER FOR INSTAGRAM REELS (Bypasses overloaded servers)
        if 'instagram.com' in url and '/reel/' in url:
            try:
                clean_url = raw_url.split('?')[0]
                embed_url = f"{clean_url}embed/captioned/"
                res = requests.get(embed_url, timeout=8)
                if res.status_code == 200:
                    match_mp4 = re.search(r'video_url":"([^"]+)"', res.text)
                    if match_mp4:
                        direct_mp4 = match_mp4.group(1).replace('\\u0025', '%').replace('\\u0026', '&').replace('\\', '')
                        video_data = requests.get(direct_mp4, timeout=12).content
                        with open('downloaded_media.mp4', 'wb') as f:
                            f.write(video_data)
                        with open('downloaded_media.mp4', 'rb') as video:
                            await bot.send_video(message.chat.id, video, reply_to_message_id=message.message_id)
                        os.remove('downloaded_media.mp4')
                        return
            except Exception:
                pass # Fail silently and drop down to the public server cluster below

        # 2. ROTATING COMPATIBILITY ENGINE FOR YOUTUBE, FACEBOOK, AND TIKTOK
        processing_nodes = [
            "https://unbanned.co",
            "https://wuk.sh",
            "https://cobalt.tools"
        ]
        
        video_stream_payload = None
        for node in processing_nodes:
            try:
                headers = {"Accept": "application/json", "Content-Type": "application/json"}
                payload = {"url": raw_url, "videoQuality": "720", "filenamePattern": "basic"}
                response = requests.post(node, json=payload, headers=headers, timeout=6)
                if response.status_code == 200:
                    node_data = response.json()
                    if node_data.get("status") in ["stream", "picker"]:
                        video_stream_payload = requests.get(node_data.get("url"), stream=True, timeout=12)
                        break
            except Exception:
                continue # Skip to the backup server link if this one is busy
                
        if not video_stream_payload:
            print("Public cloud parsing pipelines are fully loaded right now.")
            return

        # 3. STREAM BINARY DATA SAFELY AND DELIVER AS AN ASSET
        try:
            with open('downloaded_media.mp4', 'wb') as f:
                for chunk in video_stream_payload.iter_content(chunk_size=8192):
                    f.write(chunk)
            
            try:
                with open('downloaded_media.mp4', 'rb') as video:
                    await bot.send_video(message.chat.id, video, reply_to_message_id=message.message_id)
            except Exception:
                # If Telegram rejects the compression layer, upload it as a clean uncompressed attachment document
                with open('downloaded_media.mp4', 'rb') as video_file:
                    await bot.send_document(message.chat.id, video_file, reply_to_message_id=message.message_id)
            
            os.remove('downloaded_media.mp4')
        except Exception as err:
            print(f"Local disk assembly handling error: {err}")
            if os.path.exists('downloaded_media.mp4'):
                os.remove('downloaded_media.mp4')

async def main():
    # Instantly cut off any hanging webhook processes tied to this token string
    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except Exception:
        pass
        
    print("Bot is officially online, isolated, and running cleanly on Railway!")
    await bot.polling(non_stop=True, timeout=90)

if __name__ == "__main__":
    asyncio.run(main())
