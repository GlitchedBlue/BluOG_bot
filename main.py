import os
import re
import asyncio
import requests
from telebot.async_telebot import AsyncTeleBot

BOT_TOKEN = os.getenv("BOT_TOKEN")
bot = AsyncTeleBot(BOT_TOKEN)

UNIVERSAL_URL_REGEX = r'(https?://[^\s]+)'

@bot.message_handler(func=lambda message: True)
async def handle_messages(message):
    match = re.search(UNIVERSAL_URL_REGEX, message.text or "")
    if match:
        raw_url = match.group(1)
        url = raw_url.lower()
        
        valid_platforms = ['instagram.com', 'facebook.com', 'fb.watch', 'tiktok.com', 'youtube.com', 'shorts/', 'youtu.be']
        if not any(platform in url for platform in valid_platforms):
            return  
            
        await bot.send_chat_action(message.chat.id, 'upload_video')
        
        # 1. DIRECT DEDICATED PIPELINE FOR YOUTUBE VIDEOS & SHORTS
        if 'youtube.com' in url or 'youtu.be' in url or 'shorts/' in url:
            try:
                # Direct streaming video fetch engine skips proxy server bottlenecks
                payload = {
                    "url": raw_url,
                    "videoQuality": "480", # Optimized resolution bypasses Telegram file size limits
                    "filenamePattern": "basic",
                    "downloadMode": "auto"
                }
                headers = {"Accept": "application/json", "Content-Type": "application/json"}
                
                # Attempt direct bypass using alternative processing relays
                for yt_node in ["https://wuk.sh", "https://unbanned.co", "https://cobalt.tools"]:
                    try:
                        res = requests.post(yt_node, json=payload, headers=headers, timeout=8)
                        if res.status_code == 200:
                            data = res.json()
                            if data.get("status") in ["stream", "picker"]:
                                video_url = data.get("url")
                                with requests.get(video_url, stream=True, timeout=15) as video_stream:
                                    video_stream.raise_for_status()
                                    with open('downloaded_media.mp4', 'wb') as f:
                                        for chunk in video_stream.iter_content(chunk_size=8192):
                                            f.write(chunk)
                                
                                with open('downloaded_media.mp4', 'rb') as video:
                                    await bot.send_video(message.chat.id, video, reply_to_message_id=message.message_id)
                                os.remove('downloaded_media.mp4')
                                return
                    except Exception:
                        continue
            except Exception as e:
                print(f"Direct YouTube download attempt bypassed: {e}")

        # 2. DIRECT PIPELINE FOR TIKTOK
        if 'tiktok.com' in url or 'vm.tiktok' in url or 'vt.tiktok' in url:
            try:
                resolved_url = requests.head(raw_url, allow_redirects=True, timeout=8).url
                video_id_match = re.search(r'/video/(\d+)', resolved_url)
                if video_id_match:
                    video_id = video_id_match.group(1)
                    direct_api = f"https://tiktokv.com{video_id}"
                    res = requests.get(direct_api, timeout=10).json()
                    play_addr = res['aweme_list']['video']['play_addr']['url_list']
                    video_data = requests.get(play_addr, headers={'User-Agent': 'Mozilla/5.0'}, timeout=15).content
                    
                    with open('downloaded_media.mp4', 'wb') as f:
                        f.write(video_data)
                    with open('downloaded_media.mp4', 'rb') as video:
                        await bot.send_video(message.chat.id, video, reply_to_message_id=message.message_id)
                    os.remove('downloaded_media.mp4')
                    return
            except Exception:
                pass

        # 3. DIRECT PIPELINE FOR INSTAGRAM
        if 'instagram.com' in url and any(x in url for x in ['/reel/', '/p/', '/tv/']):
            try:
                clean_url = raw_url.split('?')[0]
                if not clean_url.endswith('/'):
                    clean_url += '/'
                embed_url = f"{clean_url}embed/captioned/"
                res = requests.get(embed_url, timeout=8)
                if res.status_code == 200:
                    match_mp4 = re.search(r'"video_url":"([^"]+)"', res.text)
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
                pass

        print("The request could not be fulfilled through the active data links.")

async def main():
    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except Exception:
        pass
    print("Bot is officially online, isolated, and running cleanly on Railway!")
    await bot.polling(non_stop=True, timeout=90)

if __name__ == "__main__":
    asyncio.run(main())
