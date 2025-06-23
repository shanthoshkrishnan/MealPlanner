from flask import Flask, request, jsonify
import google.generativeai as genai
import os
import requests
from PIL import Image
import io
import json
import logging
import time
from typing import Dict, Any, Optional
import uuid
import psycopg2
from psycopg2.extras import RealDictCursor
import boto3
from botocore.exceptions import ClientError
from datetime import datetime
import re
from urllib.parse import urlparse
from typing import List, Dict, Optional, Tuple, Any


# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Configuration with validation
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
WHATSAPP_TOKEN = os.getenv('WHATSAPP_TOKEN')
WHATSAPP_PHONE_NUMBER_ID = os.getenv('WHATSAPP_PHONE_NUMBER_ID')
VERIFY_TOKEN = os.getenv('WEBHOOK_VERIFY_TOKEN')

# Database Configuration
DATABASE_URL = os.getenv('DATABASE_URL')

# AWS S3 Configuration
AWS_ACCESS_KEY_ID = os.getenv('AWS_ACCESS_KEY_ID')
AWS_SECRET_ACCESS_KEY = os.getenv('AWS_SECRET_ACCESS_KEY')
AWS_S3_BUCKET = os.getenv('AWS_S3_BUCKET')
AWS_REGION = os.getenv('AWS_REGION', 'us-east-1')

# Validation
required_env_vars = [
    'GEMINI_API_KEY', 'WHATSAPP_TOKEN', 'WHATSAPP_PHONE_NUMBER_ID', 
    'WEBHOOK_VERIFY_TOKEN', 'DATABASE_URL', 'AWS_ACCESS_KEY_ID', 
    'AWS_SECRET_ACCESS_KEY', 'AWS_S3_BUCKET'
]

missing_vars = [var for var in required_env_vars if not os.getenv(var)]
if missing_vars:
    logger.error(f"Missing required environment variables: {missing_vars}")
    raise ValueError(f"Missing required environment variables: {missing_vars}")

# Configure Gemini API
try:
    genai.configure(api_key=GEMINI_API_KEY)
    logger.info("Gemini API configured successfully")
except Exception as e:
    logger.error(f"Failed to configure Gemini API: {e}")
    raise

# Configure AWS S3
try:
    s3_client = boto3.client(
        's3',
        aws_access_key_id=AWS_ACCESS_KEY_ID,
        aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
        region_name=AWS_REGION
    )
    logger.info("AWS S3 configured successfully")
except Exception as e:
    logger.error(f"Failed to configure AWS S3: {e}")
    raise

class DatabaseManager:
    def __init__(self):
        self.database_url = DATABASE_URL
        self.init_database()
    
    def get_connection(self):
        """Get database connection"""
        try:
            return psycopg2.connect(self.database_url)
        except Exception as e:
            logger.error(f"Database connection failed: {e}")
            raise
    
    def init_database(self):
        """Initialize normalized database tables"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            
            # Create languages table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS languages (
                    id SERIAL PRIMARY KEY,
                    code VARCHAR(5) UNIQUE NOT NULL,
                    name VARCHAR(50) NOT NULL,
                    native_name VARCHAR(50) NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            
            # Insert default languages
            languages_data = [
                ('en', 'English', 'English'),
                ('ta', 'Tamil', 'தமிழ்'),
                ('te', 'Telugu', 'తెలుగు'),
                ('hi', 'Hindi', 'हिन्दी'),
                ('kn', 'Kannada', 'ಕನ್ನಡ'),
                ('ml', 'Malayalam', 'മലയാളം'),
                ('mr', 'Marathi', 'मराठी'),
                ('gu', 'Gujarati', 'ગુજરાતી'),
                ('bn', 'Bengali', 'বাংলা')
            ]
            
            for code, name, native in languages_data:
                cursor.execute("""
                    INSERT INTO languages (code, name, native_name) 
                    VALUES (%s, %s, %s) 
                    ON CONFLICT (code) DO NOTHING
                """, (code, name, native))
            
            # Create users table with normalized structure
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY,
                    phone_number VARCHAR(20) UNIQUE NOT NULL,
                    name VARCHAR(100),
                    address TEXT,
                    language_id INTEGER REFERENCES languages(id) DEFAULT 1,
                    registration_status VARCHAR(20) DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            
            # Create nutrition_analysis table (normalized)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS nutrition_analysis (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                    file_location TEXT NOT NULL,
                    analysis_result TEXT,
                    language_id INTEGER REFERENCES languages(id),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            
            # Create user_registration_sessions table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS user_registration_sessions (
                    id SERIAL PRIMARY KEY,
                    phone_number VARCHAR(20) UNIQUE NOT NULL,
                    current_step VARCHAR(20) DEFAULT 'name',
                    temp_data JSONB DEFAULT '{}',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            
            # Create language_change_requests table for tracking language changes
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS language_change_requests (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                    old_language_id INTEGER REFERENCES languages(id),
                    new_language_id INTEGER REFERENCES languages(id),
                    changed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            
            # Create indexes for better performance
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_users_phone ON users(phone_number);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_users_language ON users(language_id);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_nutrition_user ON nutrition_analysis(user_id);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_nutrition_created ON nutrition_analysis(created_at);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_sessions_phone ON user_registration_sessions(phone_number);")
            
            # Add trigger to update updated_at automatically
            cursor.execute("""
                CREATE OR REPLACE FUNCTION update_updated_at_column()
                RETURNS TRIGGER AS $$
                BEGIN
                    NEW.updated_at = CURRENT_TIMESTAMP;
                    RETURN NEW;
                END;
                $$ language 'plpgsql';
            """)
            
            cursor.execute("""
                DROP TRIGGER IF EXISTS update_users_updated_at ON users;
                CREATE TRIGGER update_users_updated_at
                    BEFORE UPDATE ON users
                    FOR EACH ROW
                    EXECUTE FUNCTION update_updated_at_column();
            """)
            
            conn.commit()
            cursor.close()
            conn.close()
            logger.info("Normalized database initialized successfully")
            
        except Exception as e:
            logger.error(f"Database initialization error: {e}")
            raise
    
    def get_user_by_phone(self, phone_number: str) -> Optional[Dict]:
        """Get user by phone number with language info"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor(cursor_factory=RealDictCursor)
            
            cursor.execute("""
                SELECT u.*, l.code as language_code, l.name as language_name, l.native_name as language_native
                FROM users u
                LEFT JOIN languages l ON u.language_id = l.id
                WHERE u.phone_number = %s
            """, (phone_number,))
            
            user = cursor.fetchone()
            cursor.close()
            conn.close()
            
            return dict(user) if user else None
            
        except Exception as e:
            logger.error(f"Error getting user by phone: {e}")
            return None
    
    def get_user_by_id(self, user_id: int) -> Optional[Dict]:
        """Get user by ID with language info"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor(cursor_factory=RealDictCursor)
            
            cursor.execute("""
                SELECT u.*, l.code as language_code, l.name as language_name, l.native_name as language_native
                FROM users u
                LEFT JOIN languages l ON u.language_id = l.id
                WHERE u.id = %s
            """, (user_id,))
            
            user = cursor.fetchone()
            cursor.close()
            conn.close()
            
            return dict(user) if user else None
            
        except Exception as e:
            logger.error(f"Error getting user by ID: {e}")
            return None
    
    def create_user(self, phone_number: str, name: str, address: str, language_code: str) -> bool:
        """Create new user with language code"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            
            # Get language ID
            cursor.execute("SELECT id FROM languages WHERE code = %s", (language_code,))
            language_result = cursor.fetchone()
            language_id = language_result[0] if language_result else 1  # Default to English
            
            cursor.execute("""
                INSERT INTO users (phone_number, name, address, language_id, registration_status)
                VALUES (%s, %s, %s, %s, 'completed')
                ON CONFLICT (phone_number) 
                DO UPDATE SET 
                    name = EXCLUDED.name,
                    address = EXCLUDED.address,
                    language_id = EXCLUDED.language_id,
                    registration_status = 'completed'
            """, (phone_number, name, address, language_id))
            
            conn.commit()
            cursor.close()
            conn.close()
            
            # Clean up registration session
            self.delete_registration_session(phone_number)
            
            return True
            
        except Exception as e:
            logger.error(f"Error creating user: {e}")
            return False
    
    def update_user_language(self, user_id: int, language_code: str) -> bool:
        """Update user's language preference"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            
            # Get current language and new language IDs
            cursor.execute("SELECT language_id FROM users WHERE id = %s", (user_id,))
            current_result = cursor.fetchone()
            old_language_id = current_result[0] if current_result else None
            
            cursor.execute("SELECT id FROM languages WHERE code = %s", (language_code,))
            language_result = cursor.fetchone()
            new_language_id = language_result[0] if language_result else 1
            
            # Update user language
            cursor.execute("""
                UPDATE users SET language_id = %s WHERE id = %s
            """, (new_language_id, user_id))
            
            # Log language change
            if old_language_id and old_language_id != new_language_id:
                cursor.execute("""
                    INSERT INTO language_change_requests (user_id, old_language_id, new_language_id)
                    VALUES (%s, %s, %s)
                """, (user_id, old_language_id, new_language_id))
            
            conn.commit()
            cursor.close()
            conn.close()
            
            return True
            
        except Exception as e:
            logger.error(f"Error updating user language: {e}")
            return False
    
    def get_registration_session(self, phone_number: str) -> Optional[Dict]:
        """Get user registration session"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor(cursor_factory=RealDictCursor)
            
            cursor.execute(
                "SELECT * FROM user_registration_sessions WHERE phone_number = %s",
                (phone_number,)
            )
            session = cursor.fetchone()
            
            cursor.close()
            conn.close()
            
            return dict(session) if session else None
            
        except Exception as e:
            logger.error(f"Error getting registration session: {e}")
            return None
    
    def update_registration_session(self, phone_number: str, step: str, temp_data: Dict) -> bool:
        """Update user registration session"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            
            cursor.execute("""
                INSERT INTO user_registration_sessions (phone_number, current_step, temp_data)
                VALUES (%s, %s, %s)
                ON CONFLICT (phone_number)
                DO UPDATE SET 
                    current_step = EXCLUDED.current_step,
                    temp_data = EXCLUDED.temp_data,
                    updated_at = CURRENT_TIMESTAMP
            """, (phone_number, step, json.dumps(temp_data)))
            
            conn.commit()
            cursor.close()
            conn.close()
            
            return True
            
        except Exception as e:
            logger.error(f"Error updating registration session: {e}")
            return False
    
    def delete_registration_session(self, phone_number: str) -> bool:
        """Delete registration session"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            
            cursor.execute(
                "DELETE FROM user_registration_sessions WHERE phone_number = %s",
                (phone_number,)
            )
            
            conn.commit()
            cursor.close()
            conn.close()
            
            return True
            
        except Exception as e:
            logger.error(f"Error deleting registration session: {e}")
            return False
    
    def save_nutrition_analysis(self, user_id: int, file_location: str, analysis_result: str, language_code: str) -> bool:
        """Save nutrition analysis to database"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            
            # Get language ID
            cursor.execute("SELECT id FROM languages WHERE code = %s", (language_code,))
            language_result = cursor.fetchone()
            language_id = language_result[0] if language_result else 1
            
            cursor.execute("""
                INSERT INTO nutrition_analysis (user_id, file_location, analysis_result, language_id)
                VALUES (%s, %s, %s, %s)
            """, (user_id, file_location, analysis_result, language_id))
            
            conn.commit()
            cursor.close()
            conn.close()
            
            return True
            
        except Exception as e:
            logger.error(f"Error saving nutrition analysis: {e}")
            return False

    def get_user_stats(self, user_id: int) -> Dict:
        """Get user analysis statistics"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor(cursor_factory=RealDictCursor)
            
            cursor.execute("""
                SELECT COUNT(*) as total_analyses
                FROM nutrition_analysis 
                WHERE user_id = %s
            """, (user_id,))
            
            total_result = cursor.fetchone()
            
            cursor.execute("""
                SELECT DATE(created_at) as analysis_date, COUNT(*) as daily_count
                FROM nutrition_analysis 
                WHERE user_id = %s 
                GROUP BY DATE(created_at)
                ORDER BY analysis_date DESC
                LIMIT 7
            """, (user_id,))
            
            recent_stats = cursor.fetchall()
            
            # Get language usage stats
            cursor.execute("""
                SELECT l.name, l.native_name, COUNT(*) as usage_count
                FROM nutrition_analysis na
                JOIN languages l ON na.language_id = l.id
                WHERE na.user_id = %s
                GROUP BY l.id, l.name, l.native_name
                ORDER BY usage_count DESC
            """, (user_id,))
            
            language_stats = cursor.fetchall()
            
            cursor.close()
            conn.close()
            
            return {
                'total_analyses': total_result['total_analyses'] if total_result else 0,
                'recent_analyses': [dict(row) for row in recent_stats] if recent_stats else [],
                'language_usage': [dict(row) for row in language_stats] if language_stats else []
            }
            
        except Exception as e:
            logger.error(f"Error getting user stats: {e}")
            return {'total_analyses': 0, 'recent_analyses': [], 'language_usage': []}

    def get_all_languages(self) -> List[Dict]:
        """Get all available languages"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor(cursor_factory=RealDictCursor)
            
            cursor.execute("SELECT * FROM languages ORDER BY name")
            languages = cursor.fetchall()
            
            cursor.close()
            conn.close()
            
            return [dict(lang) for lang in languages] if languages else []
            
        except Exception as e:
            logger.error(f"Error getting languages: {e}")
            return []

    def cleanup_old_registration_sessions(self):
        """Clean up old registration sessions (older than 24 hours)"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            
            cursor.execute("""
                DELETE FROM user_registration_sessions 
                WHERE created_at < NOW() - INTERVAL '24 hours'
            """)
            
            deleted_count = cursor.rowcount
            conn.commit()
            cursor.close()
            conn.close()
            
            if deleted_count > 0:
                logger.info(f"Cleaned up {deleted_count} old registration sessions")
            
        except Exception as e:
            logger.error(f"Error cleaning up old sessions: {e}")

class S3Manager:
    def __init__(self):
        self.s3_client = s3_client
        self.bucket_name = AWS_S3_BUCKET
    
    def upload_image(self, image_bytes: bytes, user_id: int) -> Optional[str]:
        """Upload image to S3 and return file location path"""
        try:
            # Generate unique filename with user_id
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"{timestamp}_{uuid.uuid4().hex[:8]}.jpeg"
            file_location = f"/nutrition_images/{user_id}/{filename}"
            
            # S3 key (remove leading slash for S3)
            s3_key = file_location.lstrip('/')
            
            # Upload to S3
            self.s3_client.put_object(
                Bucket=self.bucket_name,
                Key=s3_key,
                Body=image_bytes,
                ContentType='image/jpeg'
            )
            
            logger.info(f"Image uploaded successfully to {file_location}")
            return file_location
            
        except ClientError as e:
            logger.error(f"S3 upload error: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected S3 error: {e}")
            return None
    
    def get_image_url(self, file_location: str) -> str:
        """Generate S3 URL from file location"""
        s3_key = file_location.lstrip('/')
        return f"https://{self.bucket_name}.s3.{AWS_REGION}.amazonaws.com/{s3_key}"

class LanguageManager:
    def __init__(self, db_manager: DatabaseManager):
        self.db_manager = db_manager
        self.languages = {}
        self.messages = {
            'en': {
                'welcome': "👋 Hello! I'm your AI Nutrition Analyzer bot!\n\n📸 Send me a photo of any food and I'll provide:\n• Detailed nutritional information\n• Calorie count and macros\n• Health analysis and tips\n• Improvement suggestions\n\nJust take a clear photo of your meal and send it to me! 🍽️\n\n🌍 *Change language anytime:* Type 'language' or '/lang'",
                'registration_name': "Welcome! I need to collect some basic information from you.\n\n📝 Please enter your full name:",
                'registration_address': "Thank you! Now please enter your address:",
                'registration_language': "Great! Please select your preferred language for nutrition analysis:",
                'registration_complete': "✅ Registration completed successfully! You can now send me food photos for nutrition analysis.\n\n🌍 *Change language anytime:* Type 'language' or '/lang'",
                'analyzing': "🔍 Analyzing your food image... This may take a few moments.",
                'help': "🆘 **How to use this bot:**\n\n1. Take a clear photo of your food\n2. Send the image to me\n3. Wait for the analysis (usually 10-30 seconds)\n4. Get detailed nutrition information!\n\n**Tips for best results:**\n• Take photos in good lighting\n• Show the food clearly from above\n• Include the whole serving if possible\n• One dish per photo works best\n\n**Commands:**\n• Type 'language' or '/lang' to change language\n• Type 'stats' to see your analysis history\n• Type 'profile' to view your information\n\nSend me a food photo to get started! 📸",
                'language_changed': "✅ Language successfully changed to {language_name}!\n\nYou can change your language anytime by typing 'language' or '/lang'",
                'language_selection': "🌍 **Select your preferred language:**\n\nReply with the language code (e.g., 'en' for English):",
                'invalid_language': "❌ Invalid language code. Please choose from the available options.",
                'language_change_prompt': "🌍 **Change Language**\n\nCurrent language: {current_language}\n\n**Available languages:**\n{language_list}\n\n💬 Reply with the language code (e.g., 'ta' for Tamil, 'hi' for Hindi)"
            },
            'ta': {
                'welcome': "👋 வணக்கம்! நான் உங்கள் AI ஊட்டச்சத்து பகுப்பாய்வு பாட்!\n\n📸 எந்த உணவின் புகைப்படத்தையும் அனுப்புங்கள், நான் வழங்குவேன்:\n• விரிவான ஊட்டச்சத்து தகவல்\n• கலோரி எண்ணிக்கை மற்றும் மேக்ரோக்கள்\n• ஆரோக்கிய பகுப்பாய்வு மற்றும் குறிப்புகள்\n• மேம்படுத்தும் பரிந்துரைகள்\n\nஉங்கள் உணவின் தெளிவான புகைப்படத்தை எடுத்து அனுப்புங்கள! 🍽️\n\n🌍 *மொழி மாற்ற:* 'language' அல்லது '/lang' என்று டைப் செய்யவும்",
                'analyzing': "🔍 உங்கள் உணவு படத்தை பகுப்பாய்வு செய்கிறேன்... இதற்கு சில நிமிடங்கள் ஆகலாம்.",
                'help': "🆘 **இந்த பாட்டை எப்படி பயன்படுத்துவது:**\n\n1. உங்கள் உணவின் தெளிவான புகைப்படத்தை எடுங்கள்\n2. படத்தை எனக்கு அனுப்புங்கள்\n3. பகுப்பாய்விற்காக காத்திருங்கள்\n4. விரிவான ஊட்டச்சத்து தகவலைப் பெறுங்கள்!\n\n**கட்டளைகள்:**\n• மொழி மாற்ற 'language' அல்லது '/lang' டைப் செய்யவும்\n• புள்ளிவிவரங்களுக்கு 'stats' டைப் செய்யவும்\n\nதொடங்க எனக்கு உணவு புகைப்படம் ஒன்றை அனுப்புங்கள்! 📸",
                'language_changed': "✅ மொழி வெற்றிகரமாக {language_name} ஆக மாற்றப்பட்டது!\n\n'language' அல்லது '/lang' டைப் செய்து எந்த நேரத்திலும் மொழியை மாற்றலாம்",
                'language_change_prompt': "🌍 **மொழி மாற்றவும்**\n\nதற்போதைய மொழி: {current_language}\n\n**கிடைக்கும் மொழிகள்:**\n{language_list}\n\n💬 மொழி குறியீட்டுடன் பதிலளிக்கவும் (எ.கா., 'en' ஆங்கிலத்திற்கு)"
            },
            'hi': {
                'welcome': "👋 नमस्ते! मैं आपका AI पोषण विश्लेषक बॉट हूँ!\n\n📸 मुझे किसी भी खाने की फोटो भेजें और मैं प्रदान करूंगा:\n• विस्तृत पोषण संबंधी जानकारी\n• कैलोरी गिनती और मैक्रोज़\n• स्वास्थ्य विश्लेषण और सुझाव\n• सुधार के सुझाव\n\nबस अपने भोजन की एक स्पष्ट तस्वीर लें और मुझे भेज दें! 🍽️\n\n🌍 *भाषा बदलें:* 'language' या '/lang' टाइप करें",
                'analyzing': "🔍 आपकी खाने की तस्वीर का विश्लेषण कर रहा हूँ... इसमें कुछ समय लग सकता है।",
                'help': "🆘 **इस बॉट का उपयोग कैसे करें:**\n\n1. अपने खाने की स्पष्ट तस्वीर लें\n2. तस्वीर मुझे भेजें\n3. विश्लेषण का इंतजार करें\n4. विस्तृत पोषण जानकारी प्राप्त करें!\n\n**कमांड्स:**\n• भाषा बदलने के लिए 'language' या '/lang' टाइप करें\n• आंकड़ों के लिए 'stats' टाइप करें\n\nशुरू करने के लिए मुझे खाने की तस्वीर भेजें! 📸",
                'language_changed': "✅ भाषा सफलतापूर्वक {language_name} में बदल दी गई!\n\n'language' या '/lang' टाइप करके किसी भी समय भाषा बदल सकते हैं",
                'language_change_prompt': "🌍 **भाषा बदलें**\n\nवर्तमान भाषा: {current_language}\n\n**उपलब्ध भाषाएं:**\n{language_list}\n\n💬 भाषा कोड के साथ उत्तर दें (जैसे, 'en' अंग्रेजी के लिए)"
            }
        }
        self._load_languages()
    
    def _load_languages(self):
        """Load languages from database"""
        languages = self.db_manager.get_all_languages()
        self.languages = {lang['code']: lang for lang in languages}
    
    def get_message(self, language: str, key: str, **kwargs) -> str:
        """Get message in specified language with formatting"""
        message = self.messages.get(language, self.messages['en']).get(key, self.messages['en'][key])
        return message.format(**kwargs) if kwargs else message
    
    def get_language_name(self, code: str) -> str:
        """Get language name by code"""
        lang = self.languages.get(code)
        return lang['native_name'] if lang else 'English'
    
    def get_language_options_text(self, current_language: str = 'en') -> str:
        """Get formatted language options for user selection"""
        options = []
        current_lang_name = self.get_language_name(current_language)
        
        for code, lang in self.languages.items():
            marker = "✓" if code == current_language else "•"
            options.append(f"{marker} *{code}* - {lang['native_name']}")
        
        language_list = "\n".join(options)
        
        return self.get_message(current_language, 'language_change_prompt',
                              current_language=current_lang_name,
                              language_list=language_list)
    
    def is_valid_language(self, code: str) -> bool:
        """Check if language code is valid"""
        return code in self.languages

class NutritionAnalyzer:
    def __init__(self):
        self.model_name = "gemini-1.5-flash"
        self.model = genai.GenerativeModel(self.model_name)
    
    def analyze_food_image(self, image_bytes: bytes, language: str = 'en') -> Optional[str]:
        """Analyze food image using Gemini AI with language-specific prompts"""
        try:
            # Language-specific prompts
            prompts = {
                'en': """Analyze this food image and provide detailed nutritional information in English. 
                Include:
                1. Food identification and ingredients
                2. Estimated portion size and calories
                3. Macronutrients (carbs, protein, fat) in grams
                4. Key vitamins and minerals
                5. Health benefits and considerations
                6. Suggestions for improvement or pairing
                
                Format the response in a clear, easy-to-read manner with emojis for better presentation.""",
                
                'ta': """இந்த உணவு படத்தை பகுப்பாய்வு செய்து தமிழில் விரிவான ஊட்டச்சத்து தகவல்களை வழங்கவும்.
                உள்ளடக்க வேண்டியவை:
                1. உணவு அடையாளம் மற்றும் பொருட்கள்
                2. மதிப்பிடப்பட்ட பகுதி அளவு மற்றும் கலோரிகள்
                3. முக்கிய ஊட்டச்சத்துக்கள் (கார்போஹைட்ரேட், புரதம், கொழுப்பு) கிராமில்
                4. முக்கிய வைட்டமின்கள் மற்றும் தாதுக்கள்
                5. ஆரோக்கிய நன்மைகள் மற்றும் கவனிக்க வேண்டியவை
                6. மேம்படுத்துவதற்கான பரிந்துரைகள்
                
                எமோஜிகளுடன் தெளிவாக படிக்கக்கூடிய வகையில் பதிலை வடிவமைக்கவும்.""",
                
                'hi': """इस खाने की तस्वीर का विश्लेषण करें और हिंदी में विस्तृत पोषण संबंधी जानकारी प्रदान करें।
                शामिल करें:
                1. भोजन की पहचान और सामग्री
                2. अनुमानित हिस्से का आकार और कैलोरी
                3. मुख्य पोषक तत्व (कार्ब्स, प्रोटीन, वसा) ग्राम में
                4. मुख्य विटामिन और खनिज
                5. स्वास्थ्य लाभ और विचारणीय बातें
                6. सुधार के सुझाव
                
                बेहतर प्रस्तुति के लिए इमोजी के साथ स्पष्ट, पढ़ने में आसान तरीके से जवाब को प्रारूपित करें।""",
                
                'te': """ఈ ఆహార చిత్రాన్ని విశ్లేషించి తెలుగులో వివరణాత్మక పోషకాహార సమాచారాన్ని అందించండి.
                కలిగి ఉండవలసినవి:
                1. ఆహార గుర్తింపు మరియు పదార్థాలు
                2. అంచనా వేసిన భాగం పరిమాణం మరియు కేలరీలు
                3. ప్రధాన పోషకాలు (కార్బోహైడ్రేట్స్, ప్రోటీన్, కొవ్వు) గ్రాములలో
                4. ముఖ్య విటమిన్లు మరియు మినరల్స్
                5. ఆరోగ్య ప్రయోజనాలు మరియు పరిగణనలు
                6. మెరుగుదల సూచనలు
                
                మెరుగైన ప్రజెంటేషన్ కోసం ఎమోజీలతో స్పష్టమైన, చదవడానికి సులభమైన రీతిలో ప్రతిస్పందనను రూపొందించండి.""",
                
                'kn': """ಈ ಆಹಾರ ಚಿತ್ರವನ್ನು ವಿಶ್ಲೇಷಿಸಿ ಮತ್ತು ಕನ್ನಡದಲ್ಲಿ ವಿವರವಾದ ಪೌಷ್ಟಿಕಾಂಶದ ಮಾಹಿತಿಯನ್ನು ಒದಗಿಸಿ.
                ಒಳಗೊಂಡಿರಬೇಕಾದವು:
                1. ಆಹಾರ ಗುರುತಿಸುವಿಕೆ ಮತ್ತು ಘಟಕಗಳು
                2. ಅಂದಾಜು ಭಾಗದ ಗಾತ್ರ ಮತ್ತು ಕ್ಯಾಲೊರಿಗಳು
                3. ಮುಖ್ಯ ಪೌಷ್ಟಿಕಾಂಶಗಳು (ಕಾರ್ಬೋಹೈಡ್ರೇಟ್ಸ್, ಪ್ರೋಟೀನ್, ಕೊಬ್ಬು) ಗ್ರಾಂಗಳಲ್ಲಿ
                4. ಮುಖ್ಯ ಜೀವಸತ್ವಗಳು ಮತ್ತು ಖನಿಜಗಳು
                5. ಆರೋಗ್ಯ ಪ್ರಯೋಜನಗಳು ಮತ್ತು ಪರಿಗಣನೆಗಳು
                6. ಸುಧಾರಣೆಯ ಸಲಹೆಗಳು
                
                ಉತ್ತಮ ಪ್ರಸ್ತುತಿಗಾಗಿ ಎಮೋಜಿಗಳೊಂದಿಗೆ ಸ್ಪಷ್ಟವಾದ, ಓದಲು ಸುಲಭವಾದ ರೀತಿಯಲ್ಲಿ ಪ್ರತಿಕ್ರಿಯೆಯನ್ನು ರೂಪಿಸಿ.""",
                
                'ml': """ഈ ഭക്ഷണ ചിത്രം വിശകലനം ചെയ്ത് മലയാളത്തിൽ വിശദമായ പോഷകാഹാര വിവരങ്ങൾ നൽകുക.
                ഉൾപ്പെടുത്തേണ്ടവ:
                1. ഭക്ഷണ തിരിച്ചറിയലും ചേരുവകളും
                2. കണക്കാക്കിയ ഭാഗത്തിന്റെ വലുപ്പവും കലോറിയും
                3. പ്രധാന പോഷകങ്ങൾ (കാർബോഹൈഡ്രേറ്റ്, പ്രോട്ടീൻ, കൊഴുപ്പ്) ഗ്രാമിൽ
                4. പ്രധാന വിറ്റാമിനുകളും ധാതുക്കളും
                5. ആരോഗ്യ ഗുണങ്ങളും പരിഗണനകളും
                6. മെച്ചപ്പെടുത്തുന്നതിനുള്ള നിർദ്ദേശങ്ങൾ
                
                മികച്ച അവതരണത്തിനായി ഇമോജികൾക്കൊപ്പം വ്യക്തമായ, വായിക്കാൻ എളുപ്പമുള്ള രീതിയിൽ പ്രതികരണം രൂപപ്പെടുത്തുക."""
            }
            
            # Get the appropriate prompt for the language
            prompt = prompts.get(language, prompts['en'])
            
            # Convert bytes to PIL Image
            image = Image.open(io.BytesIO(image_bytes))
            
            # Generate content with the image
            response = self.model.generate_content([prompt, image])
            
            if response and response.text:
                logger.info(f"Successfully analyzed food image in {language}")
                return response.text
            
            logger.warning("No response from Gemini AI")
            return None
            
        except Exception as e:
            logger.error(f"Error analyzing food image: {e}")
            return None

class WhatsAppManager:
    def __init__(self, db_manager: DatabaseManager, s3_manager: S3Manager, nutrition_analyzer: NutritionAnalyzer, language_manager: LanguageManager):
        self.db_manager = db_manager
        self.s3_manager = s3_manager
        self.nutrition_analyzer = nutrition_analyzer
        self.language_manager = language_manager
    
    def send_message(self, to: str, message: str) -> bool:
        """Send text message via WhatsApp API"""
        try:
            url = f"https://graph.facebook.com/v18.0/{WHATSAPP_PHONE_NUMBER_ID}/messages"
            headers = {
                "Authorization": f"Bearer {WHATSAPP_TOKEN}",
                "Content-Type": "application/json"
            }
            data = {
                "messaging_product": "whatsapp",
                "to": to,
                "type": "text",
                "text": {"body": message}
            }
            
            response = requests.post(url, headers=headers, json=data)
            
            if response.status_code == 200:
                logger.info(f"Message sent successfully to {to}")
                return True
            else:
                logger.error(f"Failed to send message: {response.status_code}, {response.text}")
                return False
                
        except Exception as e:
            logger.error(f"Error sending WhatsApp message: {e}")
            return False
    
    def download_media(self, media_id: str) -> Optional[bytes]:
        """Download media from WhatsApp API"""
        try:
            # Get media URL
            url = f"https://graph.facebook.com/v18.0/{media_id}"
            headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
            
            response = requests.get(url, headers=headers)
            if response.status_code != 200:
                logger.error(f"Failed to get media URL: {response.status_code}")
                return None
            
            media_url = response.json().get('url')
            if not media_url:
                logger.error("No media URL found")
                return None
            
            # Download the actual media
            media_response = requests.get(media_url, headers=headers)
            if media_response.status_code == 200:
                logger.info("Media downloaded successfully")
                return media_response.content
            else:
                logger.error(f"Failed to download media: {media_response.status_code}")
                return None
                
        except Exception as e:
            logger.error(f"Error downloading media: {e}")
            return None
    
    def handle_text_message(self, from_phone: str, message_text: str) -> None:
        """Handle incoming text messages with language change functionality"""
        try:
            # Clean phone number
            phone_number = from_phone.replace('+', '').strip()
            
            # Normalize message text
            message_lower = message_text.lower().strip()
            
            # Check for language change commands
            if message_lower in ['language', '/lang', 'lang', 'भाषा', 'மொழி', 'భాష', 'ಭಾಷೆ', 'ഭാഷ']:
                self.handle_language_change_request(phone_number)
                return
            
            # Check if user is registered
            user = self.db_manager.get_user_by_phone(phone_number)
            
            if not user or user['registration_status'] != 'completed':
                # Handle registration process
                self.handle_registration_process(phone_number, message_text)
                return
            
            # Get user's language
            user_language = user.get('language_code', 'en')
            
            # Check if this is a language code selection
            if self.language_manager.is_valid_language(message_lower):
                success = self.db_manager.update_user_language(user['id'], message_lower)
                if success:
                    new_language_name = self.language_manager.get_language_name(message_lower)
                    message = self.language_manager.get_message(
                        message_lower, 'language_changed', 
                        language_name=new_language_name
                    )
                    self.send_message(phone_number, message)
                else:
                    self.send_message(phone_number, "❌ Failed to change language. Please try again.")
                return
            
            # Handle other commands
            if message_lower in ['help', 'start', '/start', '/help']:
                help_message = self.language_manager.get_message(user_language, 'help')
                self.send_message(phone_number, help_message)
                
            elif message_lower in ['stats', '/stats', 'statistics']:
                self.handle_stats_request(user, user_language)
                
            elif message_lower in ['profile', '/profile']:
                self.handle_profile_request(user, user_language)
                
            else:
                # General help message for unrecognized commands
                help_message = self.language_manager.get_message(user_language, 'help')
                self.send_message(phone_number, help_message)
                
        except Exception as e:
            logger.error(f"Error handling text message: {e}")
            self.send_message(from_phone, "❌ Sorry, I encountered an error processing your message. Please try again.")
    
    def handle_language_change_request(self, phone_number: str) -> None:
        """Handle language change request"""
        try:
            user = self.db_manager.get_user_by_phone(phone_number)
            if not user:
                # User not registered yet
                self.send_message(phone_number, "Please register first by sending your name.")
                return
            
            current_language = user.get('language_code', 'en')
            language_options = self.language_manager.get_language_options_text(current_language)
            self.send_message(phone_number, language_options)
            
        except Exception as e:
            logger.error(f"Error handling language change request: {e}")
            self.send_message(phone_number, "❌ Error changing language. Please try again.")
    
    def handle_registration_process(self, phone_number: str, message_text: str) -> None:
        """Handle user registration process"""
        try:
            session = self.db_manager.get_registration_session(phone_number)
            
            if not session:
                # Start new registration
                temp_data = {'name': message_text}
                self.db_manager.update_registration_session(phone_number, 'address', temp_data)
                self.send_message(phone_number, self.language_manager.get_message('en', 'registration_address'))
                
            elif session['current_step'] == 'address':
                # Collect address
                temp_data = session['temp_data']
                temp_data['address'] = message_text
                self.db_manager.update_registration_session(phone_number, 'language', temp_data)
                
                # Send language selection options
                language_options = self.language_manager.get_language_options_text('en')
                self.send_message(phone_number, language_options)
                
            elif session['current_step'] == 'language':
                # Validate and complete registration
                if self.language_manager.is_valid_language(message_text.lower()):
                    temp_data = session['temp_data']
                    success = self.db_manager.create_user(
                        phone_number, 
                        temp_data['name'], 
                        temp_data['address'], 
                        message_text.lower()
                    )
                    
                    if success:
                        completion_message = self.language_manager.get_message(
                            message_text.lower(), 'registration_complete'
                        )
                        self.send_message(phone_number, completion_message)
                    else:
                        self.send_message(phone_number, "❌ Registration failed. Please try again.")
                else:
                    invalid_message = self.language_manager.get_message('en', 'invalid_language')
                    self.send_message(phone_number, invalid_message)
                    
        except Exception as e:
            logger.error(f"Error in registration process: {e}")
            self.send_message(phone_number, "❌ Registration error. Please try again.")
    
    def handle_stats_request(self, user: Dict, language: str) -> None:
        """Handle user statistics request"""
        try:
            stats = self.db_manager.get_user_stats(user['id'])
            
            if language == 'ta':
                stats_message = f"📊 **உங்கள் புள்ளிவிவரங்கள்**\n\n"
                stats_message += f"🔍 மொத்த பகுப்பாய்வுகள்: {stats['total_analyses']}\n"
                if stats['language_usage']:
                    stats_message += f"\n📈 மொழி பயன்பாடு:\n"
                    for lang_stat in stats['language_usage']:
                        stats_message += f"• {lang_stat['native_name']}: {lang_stat['usage_count']}\n"
            elif language == 'hi':
                stats_message = f"📊 **आपकी आंकड़े**\n\n"
                stats_message += f"🔍 कुल विश्लेषण: {stats['total_analyses']}\n"
                if stats['language_usage']:
                    stats_message += f"\n📈 भाषा उपयोग:\n"
                    for lang_stat in stats['language_usage']:
                        stats_message += f"• {lang_stat['native_name']}: {lang_stat['usage_count']}\n"
            else:
                stats_message = f"📊 **Your Statistics**\n\n"
                stats_message += f"🔍 Total Analyses: {stats['total_analyses']}\n"
                if stats['language_usage']:
                    stats_message += f"\n📈 Language Usage:\n"
                    for lang_stat in stats['language_usage']:
                        stats_message += f"• {lang_stat['native_name']}: {lang_stat['usage_count']}\n"
            
            self.send_message(user['phone_number'], stats_message)
            
        except Exception as e:
            logger.error(f"Error handling stats request: {e}")
    
    def handle_profile_request(self, user: Dict, language: str) -> None:
        """Handle user profile request"""
        try:
            if language == 'ta':
                profile_message = f"👤 **உங்கள் சுயவிவரம்**\n\n"
                profile_message += f"📝 பெயர்: {user['name']}\n"
                profile_message += f"📍 முகவரி: {user['address']}\n"
                profile_message += f"🌍 மொழி: {user['language_native']}\n"
                profile_message += f"📅 பதிவு செய்யப்பட்ட தேதி: {user['created_at'].strftime('%Y-%m-%d')}"
            elif language == 'hi':
                profile_message = f"👤 **आपकी प्रोफ़ाइल**\n\n"
                profile_message += f"📝 नाम: {user['name']}\n"
                profile_message += f"📍 पता: {user['address']}\n"
                profile_message += f"🌍 भाषा: {user['language_native']}\n"
                profile_message += f"📅 पंजीकरण तिथि: {user['created_at'].strftime('%Y-%m-%d')}"
            else:
                profile_message = f"👤 **Your Profile**\n\n"
                profile_message += f"📝 Name: {user['name']}\n"
                profile_message += f"📍 Address: {user['address']}\n"
                profile_message += f"🌍 Language: {user['language_native']}\n"
                profile_message += f"📅 Registered: {user['created_at'].strftime('%Y-%m-%d')}"
            
            self.send_message(user['phone_number'], profile_message)
            
        except Exception as e:
            logger.error(f"Error handling profile request: {e}")
    
    def handle_image_message(self, from_phone: str, media_id: str) -> None:
        """Handle incoming image messages"""
        try:
            phone_number = from_phone.replace('+', '').strip()
            
            # Check if user is registered
            user = self.db_manager.get_user_by_phone(phone_number)
            if not user or user['registration_status'] != 'completed':
                welcome_message = self.language_manager.get_message('en', 'registration_name')
                self.send_message(phone_number, welcome_message)
                return
            
            user_language = user.get('language_code', 'en')
            
            # Send analyzing message
            analyzing_message = self.language_manager.get_message(user_language, 'analyzing')
            self.send_message(phone_number, analyzing_message)
            
            # Download image
            image_bytes = self.download_media(media_id)
            if not image_bytes:
                error_msg = "❌ Failed to download image. Please try again." if user_language == 'en' else "❌ படம் பதிவிறக்க முடியவில்லை. மீண்டும் முயற்சிக்கவும்."
                self.send_message(phone_number, error_msg)
                return
            
            # Upload to S3
            file_location = self.s3_manager.upload_image(image_bytes, user['id'])
            if not file_location:
                error_msg = "❌ Failed to process image. Please try again." if user_language == 'en' else "❌ படம் செயலாக்க முடியவில்லை. மீண்டும் முயற்சிக்கவும்."
                self.send_message(phone_number, error_msg)
                return
            
            # Analyze nutrition
            analysis_result = self.nutrition_analyzer.analyze_food_image(image_bytes, user_language)
            if not analysis_result:
                error_msg = "❌ Failed to analyze nutrition. Please try with a clearer image." if user_language == 'en' else "❌ ஊட்டச்சத்து பகுப்பாய்வு தோல்வியடைந்தது. தெளிவான படத்துடன் முயற்சிக்கவும்."
                self.send_message(phone_number, error_msg)
                return
            
            # Save to database
            self.db_manager.save_nutrition_analysis(user['id'], file_location, analysis_result, user_language)
            
            # Send analysis result
            self.send_message(phone_number, analysis_result)
            
        except Exception as e:
            logger.error(f"Error handling image message: {e}")
            error_msg = "❌ Sorry, I encountered an error analyzing your image. Please try again."
            self.send_message(from_phone, error_msg)

# Initialize managers
db_manager = DatabaseManager()
s3_manager = S3Manager()
nutrition_analyzer = NutritionAnalyzer()
language_manager = LanguageManager(db_manager)
whatsapp_manager = WhatsAppManager(db_manager, s3_manager, nutrition_analyzer, language_manager)

@app.route('/webhook', methods=['GET', 'POST'])
def webhook():
    """Handle WhatsApp webhook"""
    if request.method == 'GET':
        # Webhook verification
        mode = request.args.get('hub.mode')
        token = request.args.get('hub.verify_token')
        challenge = request.args.get('hub.challenge')
        
        if mode == 'subscribe' and token == VERIFY_TOKEN:
            logger.info("Webhook verified successfully")
            return challenge
        else:
            logger.warning("Webhook verification failed")
            return 'Verification failed', 403
    
    elif request.method == 'POST':
        # Handle incoming messages
        try:
            data = request.get_json()
            logger.info(f"Received webhook data: {json.dumps(data, indent=2)}")
            
            # Clean up old registration sessions periodically
            if random.randint(1, 100) == 1:  # 1% chance
                db_manager.cleanup_old_registration_sessions()
            
            if 'entry' in data:
                for entry in data['entry']:
                    if 'changes' in entry:
                        for change in entry['changes']:
                            if change.get('field') == 'messages':
                                if 'messages' in change['value']:
                                    for message in change['value']['messages']:
                                        from_phone = message['from']
                                        message_type = message.get('type')
                                        
                                        if message_type == 'text':
                                            text_body = message['text']['body']
                                            whatsapp_manager.handle_text_message(from_phone, text_body)
                                            
                                        elif message_type == 'image':
                                            media_id = message['image']['id']
                                            whatsapp_manager.handle_image_message(from_phone, media_id)
            
            return jsonify({'status': 'success'}), 200
            
        except Exception as e:
            logger.error(f"Error processing webhook: {e}")
            return jsonify({'error': 'Internal server error'}), 500

@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    try:
        # Test database connection
        conn = db_manager.get_connection()
        conn.close()
        
        return jsonify({
            'status': 'healthy',
            'timestamp': datetime.now().isoformat(),
            'services': {
                'database': 'connected',
                'gemini_ai': 'configured',
                's3': 'configured'
            }
        }), 200
        
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        return jsonify({
            'status': 'unhealthy',
            'error': str(e),
            'timestamp': datetime.now().isoformat()
        }), 500

@app.route('/stats', methods=['GET'])
def get_system_stats():
    """Get system statistics endpoint"""
    try:
        conn = db_manager.get_connection()
        cursor = conn.cursor()
        
        # Get total users
        cursor.execute("SELECT COUNT(*) FROM users WHERE registration_status = 'completed'")
        total_users = cursor.fetchone()[0]
        
        # Get total analyses
        cursor.execute("SELECT COUNT(*) FROM nutrition_analysis")
        total_analyses = cursor.fetchone()[0]
        
        # Get analyses by language
        cursor.execute("""
            SELECT l.name, l.native_name, COUNT(*) as count
            FROM nutrition_analysis na
            JOIN languages l ON na.language_id = l.id
            GROUP BY l.id, l.name, l.native_name
            ORDER BY count DESC
        """)
        language_stats = cursor.fetchall()
        
        # Get recent activity (last 7 days)
        cursor.execute("""
            SELECT DATE(created_at) as date, COUNT(*) as analyses
            FROM nutrition_analysis 
            WHERE created_at >= NOW() - INTERVAL '7 days'
            GROUP BY DATE(created_at)
            ORDER BY date DESC
        """)
        recent_activity = cursor.fetchall()
        
        cursor.close()
        conn.close()
        
        return jsonify({
            'total_users': total_users,
            'total_analyses': total_analyses,
            'language_stats': [{'language': row[0], 'native_name': row[1], 'count': row[2]} for row in language_stats],
            'recent_activity': [{'date': row[0].isoformat(), 'analyses': row[1]} for row in recent_activity],
            'timestamp': datetime.now().isoformat()
        }), 200
        
    except Exception as e:
            logger.error(f"Error getting system stats: {e}")
            return jsonify({'error': 'Internal server error'}), 500

if __name__ == '__main__':
    # Initialize database tables on startup
    try:
        db_manager.create_tables()
        logger.info("Database tables initialized successfully")
    except Exception as e:
        logger.error(f"Failed to initialize database: {e}")
    
    # Configure Gemini AI
    if GEMINI_API_KEY:
        genai.configure(api_key=GEMINI_API_KEY)
        logger.info("Gemini AI configured successfully")
    else:
        logger.warning("Gemini API key not found")
    
    # Start Flask app
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
