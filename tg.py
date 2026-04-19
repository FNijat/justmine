import os
import random
import time
import threading
import json
from datetime import datetime, timedelta
from pathlib import Path
import logging

import telebot
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.api_core.exceptions import GoogleAPIError
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
import io


logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

class TelegramStorageBot:
    def __init__(self, bot_token, channel_id, google_drive_folder_id=None, local_path=None, items_per_day=1,
                 caption_text="View file", caption_link_format="https://drive.google.com/file/d/{file_id}/view",
                 sleep_between_posts=10):
        self.bot = telebot.TeleBot(bot_token)
        self.channel_id = channel_id
        self.google_drive_folder_id = google_drive_folder_id
        self.local_path = local_path
        self.items_per_day = items_per_day
        self.caption_text = caption_text
        self.caption_link_format = caption_link_format
        self.sleep_between_posts = sleep_between_posts
        self.drive_service = None
        self.posted_files_db = "posted_files.json"
        self.posted_files = self._load_posted_files()
        
        if google_drive_folder_id:
            self._setup_google_drive()
    
    def _setup_google_drive(self):
        SCOPES = ['https://www.googleapis.com/auth/drive']
        creds = None

        token_json_env = os.environ.get('TOKEN_JSON')
        if token_json_env:
            with open('token.json', 'w') as f:
                f.write(token_json_env)
            logger.info("Wrote token.json from TOKEN_JSON environment variable")

        if os.path.exists('token.json'):
            creds = Credentials.from_authorized_user_file('token.json', SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
                with open('token.json', 'w') as token:
                    token.write(creds.to_json())
                logger.info("Refreshed expired credentials")
            else:
                raise RuntimeError(
                    "No valid credentials found. "
                    "Run generate_token.py locally and set TOKEN_JSON env var on Railway."
                )

        self.drive_service = build('drive', 'v3', credentials=creds)
        logger.info("Google Drive API initialized")
    
    def _load_posted_files(self):
        if os.path.exists(self.posted_files_db):
            with open(self.posted_files_db, 'r') as f:
                return set(json.load(f))
        return set()
    
    def _save_posted_files(self):
        with open(self.posted_files_db, 'w') as f:
            json.dump(list(self.posted_files), f)
    
    def _mark_as_posted(self, file_id):
        self.posted_files.add(file_id)
        self._save_posted_files()
    
    def _get_local_files(self):
        if not self.local_path:
            return []
        
        path = Path(self.local_path)
        if not path.exists():
            logger.error(f"Local path does not exist: {self.local_path}")
            return []
        
        supported_formats = {'.jpg', '.jpeg', '.png', '.gif', '.mp4', '.webm', '.pdf', '.txt'}
        all_files = [f for f in path.iterdir() 
                if f.is_file() and f.suffix.lower() in supported_formats]
        
        unposted_files = [f for f in all_files if str(f) not in self.posted_files]
        
        if not unposted_files:
            logger.info("All local files posted. Resetting tracker.")
            self.posted_files.clear()
            self._save_posted_files()
            return all_files
        
        return unposted_files
    
    def _get_drive_files(self):
        if not self.drive_service:
            return []
        
        try:
            query = f"'{self.google_drive_folder_id}' in parents and trashed=false"
            results = self.drive_service.files().list(
                q=query,
                spaces='drive',
                fields='files(id, name, mimeType)',
                pageSize=100
            ).execute()
            
            all_files = results.get('files', [])
            unposted_files = [f for f in all_files if f['id'] not in self.posted_files]
            
            if not unposted_files:
                logger.info("All files posted. Resetting tracker.")
                self.posted_files.clear()
                self._save_posted_files()
                return all_files
            
            return unposted_files
        except GoogleAPIError as e:
            logger.error(f"Google Drive API error: {e}")
            return []
    
    def post_content(self):
        files = []
        
        if self.local_path:
            files = self._get_local_files()
        elif self.drive_service:
            files = self._get_drive_files()
        
        if not files:
            logger.warning("No files found in storage")
            return
        
        selected_files = random.sample(files, min(self.items_per_day, len(files)))
        
        for i, file_obj in enumerate(selected_files):
            try:
                if self.local_path:
                    self._post_local_file(file_obj)
                else:
                    self._post_drive_file(file_obj)
            except Exception as e:
                logger.error(f"Error posting file: {e}")
            
            if i < len(selected_files) - 1:
                logger.info(f"Waiting {self.sleep_between_posts} seconds before next post.")
                time.sleep(self.sleep_between_posts)

    def _post_local_file(self, file_path):
        file_path = Path(file_path)
        suffix = file_path.suffix.lower()
        link = self.caption_link_format.format(file_path=file_path.resolve())
        caption = f"[{self.caption_text}]({link})"
        
        try:
            with open(file_path, 'rb') as f:
                if suffix in {'.jpg', '.jpeg', '.png', '.gif'}:
                    self.bot.send_photo(chat_id=self.channel_id, photo=f, caption=caption, parse_mode='Markdown')
                elif suffix in {'.mp4', '.webm'}:
                    self.bot.send_video(chat_id=self.channel_id, video=f, caption=caption, parse_mode='Markdown', timeout=120)
                else:
                    self.bot.send_document(chat_id=self.channel_id, document=f, caption=caption, parse_mode='Markdown', timeout=120)
            
            self._mark_as_posted(str(file_path))
            logger.info(f"Posted: {file_path.name}")
        except telebot.apihelper.ApiException as e:
            logger.error(f"Telegram error posting {file_path.name}: {e}")
    
    def _post_drive_file(self, file_obj):
        file_id = file_obj['id']
        file_name = file_obj['name']
        max_retries = 3

        for attempt in range(max_retries):
            try:
                request = self.drive_service.files().get_media(fileId=file_id)
                file_content = io.BytesIO()
                downloader = MediaIoBaseDownload(file_content, request)

                done = False
                while not done:
                    status, done = downloader.next_chunk()

                file_content.seek(0)
                mime_type = file_obj.get('mimeType', '')
                link = self.caption_link_format.format(file_id=file_id)
                caption = f"[{self.caption_text}]({link})"

                if 'image' in mime_type:
                    self.bot.send_photo(chat_id=self.channel_id, photo=file_content, caption=caption, parse_mode='Markdown')
                elif 'video' in mime_type:
                    self.bot.send_video(chat_id=self.channel_id, video=file_content, caption=caption, parse_mode='Markdown', timeout=120)
                else:
                    self.bot.send_document(chat_id=self.channel_id, document=file_content, visible_file_name=file_name, caption=caption, parse_mode='Markdown', timeout=120)

                self._mark_as_posted(file_id)
                logger.info(f"Posted from Drive: {file_name}")

                try:
                    self.drive_service.files().delete(fileId=file_id).execute()
                    logger.info(f"Deleted from Drive: {file_name} ({file_id})")
                except GoogleAPIError as delete_err:
                    logger.error(f"Failed to delete {file_name} ({file_id}) from Drive: {delete_err}")

                break

            except (telebot.apihelper.ApiException, GoogleAPIError) as e:
                logger.error(f"Error posting {file_name} (attempt {attempt+1}/{max_retries}): {e}")
                if attempt < max_retries - 1:
                    time.sleep(5)
            except Exception as e:
                logger.error(f"Unexpected error posting {file_name} (attempt {attempt+1}/{max_retries}): {e}")
                if attempt < max_retries - 1:
                    time.sleep(5)

    def schedule_daily_posts(self, posting_times):
        while True:
            now = datetime.now()
            next_post_time = None
            
            for hour, minute in posting_times:
                target_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if now < target_time and (next_post_time is None or target_time < next_post_time):
                    next_post_time = target_time
            
            tomorrow = now + timedelta(days=1)
            for hour, minute in posting_times:
                target_time = tomorrow.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if next_post_time is None or target_time < next_post_time:
                    next_post_time = target_time
            
            wait_seconds = (next_post_time - now).total_seconds()
            logger.info(f"Next post scheduled at {next_post_time}. Waiting {wait_seconds} seconds.")
            
            time.sleep(wait_seconds)
            self.post_content()


def main():
    BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
    CHANNEL_ID = int(os.environ["TELEGRAM_CHANNEL_ID"])
    GOOGLE_DRIVE_FOLDER_ID = os.environ["GOOGLE_DRIVE_FOLDER_ID"]
    ITEMS_PER_DAY = int(os.environ.get("ITEMS_PER_DAY", 1))
    SLEEP_BETWEEN_POSTS = int(os.environ.get("SLEEP_BETWEEN_POSTS", 10))

    posting_times_raw = os.environ.get("POSTING_TIMES", "10:00,15:00,21:11")
    POSTING_TIMES = [
        (int(t.split(":")[0]), int(t.split(":")[1]))
        for t in posting_times_raw.split(",")
    ]
    logger.info(f"POSTING_TIMES raw value: {posting_times_raw}")
    logger.info(f"POSTING_TIMES parsed: {POSTING_TIMES}")

    LOCAL_PATH = None
    CAPTION_TEXT = os.environ.get("CAPTION_TEXT", "View file")
    CAPTION_LINK_FORMAT = os.environ.get(
        "CAPTION_LINK_FORMAT", "https://drive.google.com/file/d/{file_id}/view"
    )

    bot = TelegramStorageBot(
        bot_token=BOT_TOKEN,
        channel_id=CHANNEL_ID,
        google_drive_folder_id=GOOGLE_DRIVE_FOLDER_ID,
        local_path=LOCAL_PATH,
        items_per_day=ITEMS_PER_DAY,
        caption_text=CAPTION_TEXT,
        caption_link_format=CAPTION_LINK_FORMAT,
        sleep_between_posts=SLEEP_BETWEEN_POSTS,
    )
    
    scheduler_thread = threading.Thread(
        target=bot.schedule_daily_posts,
        args=(POSTING_TIMES,),
        daemon=True
    )
    scheduler_thread.start()
    
    logger.info("Bot started. Press Ctrl+C to stop.")
    
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Bot stopped.")


if __name__ == "__main__":
    main()
