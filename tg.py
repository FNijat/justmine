import os
import random
import time
import threading
import json
from datetime import datetime
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

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

class TelegramStorageBot:
    def __init__(self, bot_token, channel_id, google_drive_folder_id=None, local_path=None, items_per_day=1):
        self.bot = telebot.TeleBot(bot_token)
        self.channel_id = channel_id
        self.google_drive_folder_id = google_drive_folder_id
        self.local_path = local_path
        self.items_per_day = items_per_day
        self.drive_service = None
        self.posted_files_db = "posted_files.json"
        self.posted_files = self._load_posted_files()
        
        if google_drive_folder_id:
            self._setup_google_drive()
    
    def _setup_google_drive(self):
        """Initialize Google Drive API connection"""
        SCOPES = ['https://www.googleapis.com/auth/drive.readonly']
        creds = None
        
        if os.path.exists('token.json'):
            creds = Credentials.from_authorized_user_file('token.json', SCOPES)
        
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    'credentials.json', SCOPES)
                creds = flow.run_local_server(port=0)
            
            with open('token.json', 'w') as token:
                token.write(creds.to_json())
        
        self.drive_service = build('drive', 'v3', credentials=creds)
        logger.info("Google Drive API initialized")
    
    def _load_posted_files(self):
        """Load list of already posted file IDs"""
        if os.path.exists(self.posted_files_db):
            with open(self.posted_files_db, 'r') as f:
                return set(json.load(f))
        return set()
    
    def _save_posted_files(self):
        """Save posted file IDs to database"""
        with open(self.posted_files_db, 'w') as f:
            json.dump(list(self.posted_files), f)
    
    def _mark_as_posted(self, file_id):
        """Mark file as posted and save"""
        self.posted_files.add(file_id)
        self._save_posted_files()
    
    def _get_local_files(self):
        """Get files from local directory"""
        if not self.local_path:
            return []
        
        path = Path(self.local_path)
        if not path.exists():
            logger.error(f"Local path does not exist: {self.local_path}")
            return []
        
        supported_formats = {'.jpg', '.jpeg', '.png', '.gif', '.mp4', '.webm', '.pdf', '.txt'}
        all_files = [f for f in path.iterdir() 
                if f.is_file() and f.suffix.lower() in supported_formats]
        
        # Filter out already posted files (by full path)
        unposted_files = [f for f in all_files if str(f) not in self.posted_files]
        
        if not unposted_files:
            logger.info("All local files posted. Resetting tracker.")
            self.posted_files.clear()
            self._save_posted_files()
            return all_files
        
        return unposted_files
    
    def _get_drive_files(self):
        """Get files from Google Drive folder"""
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
            # Filter out already posted files
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
        """Select random files and post to Telegram"""
        files = []
        
        if self.local_path:
            files = self._get_local_files()
        elif self.drive_service:
            files = self._get_drive_files()
        
        if not files:
            logger.warning("No files found in storage")
            return
        
        selected_files = random.sample(files, min(self.items_per_day, len(files)))
        
        for file_obj in selected_files:
            try:
                if self.local_path:
                    self._post_local_file(file_obj)
                else:
                    self._post_drive_file(file_obj)
            except Exception as e:
                logger.error(f"Error posting file: {e}")
    
    def _post_local_file(self, file_path):
        """Post file from local storage"""
        file_path = Path(file_path)
        suffix = file_path.suffix.lower()
        
        try:
            with open(file_path, 'rb') as f:
                if suffix in {'.jpg', '.jpeg', '.png', '.gif'}:
                    self.bot.send_photo(chat_id=self.channel_id, photo=f)
                elif suffix in {'.mp4', '.webm'}:
                    self.bot.send_video(chat_id=self.channel_id, video=f)
                else:
                    self.bot.send_document(chat_id=self.channel_id, document=f)
            
            # Mark as posted
            self._mark_as_posted(str(file_path))
            logger.info(f"Posted: {file_path.name}")
        except telebot.apihelper.ApiException as e:
            logger.error(f"Telegram error posting {file_path.name}: {e}")
    
    def _post_drive_file(self, file_obj):
        """Post file from Google Drive"""
        file_id = file_obj['id']
        file_name = file_obj['name']
        
        try:
            request = self.drive_service.files().get_media(fileId=file_id)
            file_content = io.BytesIO()
            downloader = MediaIoBaseDownload(file_content, request)
            
            done = False
            while not done:
                status, done = downloader.next_chunk()
            
            file_content.seek(0)
            mime_type = file_obj.get('mimeType', '')
            
            if 'image' in mime_type:
                self.bot.send_photo(chat_id=self.channel_id, photo=file_content)
            elif 'video' in mime_type:
                self.bot.send_video(chat_id=self.channel_id, video=file_content)
            else:
                self.bot.send_document(chat_id=self.channel_id, document=file_content, visible_file_name=file_name)
            
            # Mark as posted
            self._mark_as_posted(file_id)
            logger.info(f"Posted from Drive: {file_name}")
        except (telebot.apihelper.ApiException, GoogleAPIError) as e:
            logger.error(f"Error posting {file_name}: {e}")
    
    def schedule_daily_posts(self, posting_times):
        """Schedule multiple daily postings at specific times
        posting_times: list of tuples (hour, minute)
        """
        while True:
            now = datetime.now()
            next_post_time = None
            
            # Find next posting time
            for hour, minute in posting_times:
                target_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if now < target_time and (next_post_time is None or target_time < next_post_time):
                    next_post_time = target_time
            
            # If no posting time found today, use first time tomorrow
            if next_post_time is None:
                hour, minute = posting_times[0]
                next_post_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                next_post_time = next_post_time.replace(day=next_post_time.day + 1)
            
            wait_seconds = (next_post_time - now).total_seconds()
            logger.info(f"Next post scheduled at {next_post_time}. Waiting {wait_seconds} seconds.")
            
            time.sleep(wait_seconds)
            self.post_content()


def main():
    # Configuration
    BOT_TOKEN = "8574806727:AAEGZWDFb0rmjHxY8TlB0WjuMOep8ynNoiE"
    CHANNEL_ID = -1003820108098
    ITEMS_PER_DAY = 4
    POSTING_TIMES = [(23, 24), (23, 19), (23, 22)]  # 10:00 AM, 3:00 PM, 9:00 PM
    
    LOCAL_PATH = None
    GOOGLE_DRIVE_FOLDER_ID = "1Ppqdn09d1mE9w4ntzlXfHR53iCL3FAyJ"
    
    # Initialize bot
    bot = TelegramStorageBot(
        bot_token=BOT_TOKEN,
        channel_id=CHANNEL_ID,
        google_drive_folder_id=GOOGLE_DRIVE_FOLDER_ID,
        local_path=LOCAL_PATH,
        items_per_day=ITEMS_PER_DAY
    )
    
    # Start scheduling in background thread
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
