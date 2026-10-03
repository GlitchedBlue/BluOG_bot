import os
import asyncio
import requests
from telebot.async_telebot import AsyncTeleBot

BOT_TOKEN = os.getenv("BOT_TOKEN")
bot = AsyncTeleBot(BOT_TOKEN)

@bot.message_handler(func=lambda message: True)
async def handle_messages(message):
    # Hardcoded test: triggers on ANY message sent to the chat
    await bot.send_chat_action(message.chat.id, 'upload_video')
    
    # Target video ID directly bypassing regex splits
    TARGET_VIDEO_ID = "gi5tv_4p0z0"
    
    # Try different public extraction relays sequentially
    processing_nodes = [
        "https://wuk.sh",
        "https://unbanned.co",
        "https://cobalt.tools"
    ]
    
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    payload = {
        "url": f"https://youtube.com{TARGET_VIDEO_ID}",
        "videoQuality": "480", # Forces a lightweight file to clear Telegram size caps
        "filenamePattern": "basic",
        "downloadMode": "auto"
    }
    
    video_stream_payload = None
    successful_node = None
    
    for node in processing_nodes:
        try:
            print(f"Testing connection hook with: {node}")
            response = requests.post(node, json=payload, headers=headers, timeout=8)
            print(f"Node response status code: {response.status_code}")
            
            if response.status_code == 200:
                node_data = response.json()
                print(f"Node API payload response data: {node_data}")
                
                if node_data.get("status") in ["stream", "picker"]:
                    video_url = node_data.get("url")
                    video_stream_payload = requests.get(video_url, stream=True, timeout=15)
                    successful_node = node
                    break
        except Exception as node_err:
            print(f"Node entry {node} failed: {node_err}")
            continue
            
    if not video_stream_payload:
        print("CRITICAL: All test extraction relays returned blocks or rate-limits.")
        await bot.reply_to(message, "Test failed: All cloud processing nodes are currently overloaded.")
        return

    print(f"Success! Pulling data block from streaming mirror found on: {successful_node}")

    try:
        with open('test_media.mp4', 'wb') as f:
            for chunk in video_stream_payload.iter_content(chunk_size=8192):
                f.write(chunk)
        
        print("File downloaded to local container memory safely. Transferring to Telegram...")
        
        try:
            with open('test_media.mp4', 'rb') as video:
                await bot.send_video(message.chat.id, video, reply_to_message_id=message.message_id)
            print("Video delivered successfully as an inline clip!")
        except Exception as upload_err:
            print(f"Inline upload failed, trying uncompressed file format attachment: {upload_err}")
            with open('test_media.mp4', 'rb') as video_file:
                await bot.send_document(message.chat.id, video_file, reply_to_message_id=message.message_id)
            print("Video delivered successfully as a file document attachment!")
                
        os.remove('test_media.mp4')
    except Exception as err:
        print(f"File writing process failed: {err}")
        if os.path.exists('test_media.mp4'):
            os.remove('test_media.mp4')

async def main():
    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except Exception:
        pass
    print("Test bot initialized. Send ANY message to fire the test download loop!")
    await bot.polling(non_stop=True, timeout=90)

if __name__ == "__main__":
    asyncio.run(main())
