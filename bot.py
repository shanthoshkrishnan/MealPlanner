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
from dotenv import load_dotenv

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
load_dotenv()

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
# Updated DatabaseManager class with simplified schema (no address)
class DatabaseManager:
    def __init__(self):
        self.database_url = DATABASE_URL
        self.init_database()
        self.migrate_database_schema()
    
    def get_connection(self):
        """Get database connection"""
        try:
            return psycopg2.connect(self.database_url)
        except Exception as e:
            logger.error(f"Database connection failed: {e}")
            raise
    
    def init_database(self):
        """Initialize database tables with simplified schema (no address)"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            
            # Create users table with auto-incrementing user_id (no address)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id SERIAL PRIMARY KEY,
                    phone_number VARCHAR(20) UNIQUE NOT NULL,
                    name VARCHAR(100),
                    preferred_language VARCHAR(10) DEFAULT 'en',
                    registration_status VARCHAR(20) DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            try:
                cursor.execute("""
                    ALTER TABLE nutrition_analysis DROP COLUMN IF EXISTS phone_number;
              """)
            except Exception:
                pass  # Column might not exist

            try:
                cursor.execute("ALTER TABLE users DROP COLUMN IF EXISTS address;")
                cursor.execute("ALTER TABLE users DROP COLUMN IF EXISTS email;")
            except Exception:
                pass
            
            # Create nutrition_analysis table (only user_id, no phone_number)
            cursor.execute("DROP TABLE IF EXISTS nutrition_analysis;")
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS nutrition_analysis (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                    file_location TEXT NOT NULL,
                    analysis_result TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            
            # Create user_registration_sessions table (simplified for name and language only)
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
            
            # Create indexes for better performance
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_users_phone ON users(phone_number);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_nutrition_user_id ON nutrition_analysis(user_id);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_sessions_phone ON user_registration_sessions(phone_number);")
            
            conn.commit()
            cursor.close()
            conn.close()
            logger.info("Database initialized successfully")
            
        except Exception as e:
            logger.error(f"Database initialization error: {e}")
            raise
    
    def get_user_by_phone(self, phone_number: str) -> Optional[Dict]:
        """Get user by phone number"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor(cursor_factory=RealDictCursor)
            
            cursor.execute(
                "SELECT * FROM users WHERE phone_number = %s",
                (phone_number,)
            )
            user = cursor.fetchone()
            
            cursor.close()
            conn.close()
            
            return dict(user) if user else None
            
        except Exception as e:
            logger.error(f"Error getting user by phone: {e}")
            return None
    
    def create_user(self, phone_number: str, name: str, language: str) -> bool:
        """Create new user (simplified - no address)"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            
            cursor.execute("""
                INSERT INTO users (phone_number, name, preferred_language, registration_status)
                VALUES (%s, %s, %s, 'completed')
                ON CONFLICT (phone_number) 
                DO UPDATE SET 
                    name = EXCLUDED.name,
                    preferred_language = EXCLUDED.preferred_language,
                    registration_status = 'completed',
                    updated_at = CURRENT_TIMESTAMP
                RETURNING user_id
            """, (phone_number, name, language))

            result = cursor.fetchone()
            if result:
                user_id = result[0]
                logger.info(f"User created/updated with user_id: {user_id}")
            
            conn.commit()
            cursor.close()
            conn.close()
            
            # Clean up registration session
            self.delete_registration_session(phone_number)
            
            return True
            
        except Exception as e:
            logger.error(f"Error creating user: {e}")
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
    
    def update_user_language(self, phone_number: str, language: str) -> bool:
        """Update user's preferred language using phone number"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            
            cursor.execute("""
                UPDATE users 
                SET preferred_language = %s, updated_at = CURRENT_TIMESTAMP 
                WHERE phone_number = %s
            """, (language, phone_number))
            
            updated_rows = cursor.rowcount
            conn.commit()
            cursor.close()
            conn.close()
            
            return updated_rows > 0
            
        except Exception as e:
            logger.error(f"Error updating user language: {e}")
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
    
    def save_nutrition_analysis(self, user_id: int, file_location: str, analysis_result: str) -> bool:
        """Save nutrition analysis to database using user_id only"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            
            cursor.execute("""
                INSERT INTO nutrition_analysis (user_id, file_location, analysis_result)
                VALUES (%s, %s, %s)
            """, (user_id, file_location, analysis_result))
            
            conn.commit()
            cursor.close()
            conn.close()
            
            return True
            
        except Exception as e:
            logger.error(f"Error saving nutrition analysis: {e}")
            return False

    def get_user_stats(self, user_id: int) -> Dict:
        """Get user analysis statistics using user_id"""
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
            
            cursor.close()
            conn.close()
            
            return {
                'total_analyses': total_result['total_analyses'] if total_result else 0,
                'recent_analyses': [dict(row) for row in recent_stats] if recent_stats else []
            }
            
        except Exception as e:
            logger.error(f"Error getting user stats: {e}")
            return {'total_analyses': 0, 'recent_analyses': []}

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

    def migrate_database_schema(self):
        """Migrate database schema to fix user_id issues"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
        
            # Check if users table exists and has correct structure
            cursor.execute("""
                SELECT column_name, data_type, is_nullable
                FROM information_schema.columns 
                WHERE table_name = 'users'
                ORDER BY ordinal_position;
            """)
        
            existing_columns = cursor.fetchall()
        
            # If users table doesn't have user_id as primary key, recreate it
            has_user_id_pk = False
            for col in existing_columns:
                if col[0] == 'user_id':
                    # Check if it's primary key
                    cursor.execute("""
                        SELECT tc.constraint_name
                        FROM information_schema.table_constraints tc
                        JOIN information_schema.key_column_usage kcu
                        ON tc.constraint_name = kcu.constraint_name
                        WHERE tc.table_name = 'users' 
                        AND tc.constraint_type = 'PRIMARY KEY'
                        AND kcu.column_name = 'user_id';
                    """)
                    if cursor.fetchone():
                        has_user_id_pk = True
                    break
        
            if not has_user_id_pk:
                logger.info("Recreating users table with proper schema...")
            
                # Backup existing data
                cursor.execute("SELECT * FROM users;")
                existing_users = cursor.fetchall()
            
                # Drop and recreate users table
                cursor.execute("DROP TABLE IF EXISTS nutrition_analysis;")
                cursor.execute("DROP TABLE IF EXISTS users;")
            
                # Create new users table with proper schema
                cursor.execute("""
                    CREATE TABLE users (
                    user_id SERIAL PRIMARY KEY,
                    phone_number VARCHAR(20) UNIQUE NOT NULL,
                    name VARCHAR(100),
                    preferred_language VARCHAR(10) DEFAULT 'en',
                    registration_status VARCHAR(20) DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
                """)
            
                # Restore data (excluding address and email if they existed)
                for user in existing_users:
                    cursor.execute("""
                        INSERT INTO users (phone_number, name, preferred_language, registration_status, created_at, updated_at)
                        VALUES (%s, %s, %s, %s, %s, %s)
                    """, (user[1], user[2], user[3], user[4], user[5], user[6]))  # Adjust indices based on your schema
            
                # Recreate nutrition_analysis table
                cursor.execute("""
                    CREATE TABLE nutrition_analysis (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                    file_location TEXT NOT NULL,
                    analysis_result TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
                """)
            
                logger.info("Database schema migration completed")
        
            # Remove address column if it exists
            try:
                cursor.execute("ALTER TABLE users DROP COLUMN IF EXISTS address;")
                cursor.execute("ALTER TABLE users DROP COLUMN IF EXISTS email;")
            except Exception:
                pass
        
            conn.commit()
            cursor.close()
            conn.close()
        
        except Exception as e:
            logger.error(f"Database migration error: {e}")
        

# Updated S3Manager class with simplified file paths
class S3Manager:
    def __init__(self):
        self.s3_client = s3_client
        self.bucket_name = AWS_S3_BUCKET
        self.base_prefix = "https://{}.s3.{}.amazonaws.com".format(AWS_S3_BUCKET, AWS_REGION)
    
    def upload_image(self, image_bytes: bytes, user_id: int) -> tuple[Optional[str], Optional[str]]:
        """Upload image to S3 and return full URL and file location path"""
        try:
            # Generate unique filename with simplified path
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"{timestamp}_{uuid.uuid4().hex[:8]}.jpg"
            file_location = f"/nutrition_images/{user_id}/{filename}"
            
            # Upload to S3 (remove leading slash for S3 key)
            s3_key = file_location.lstrip('/')
            
            self.s3_client.put_object(
                Bucket=self.bucket_name,
                Key=s3_key,
                Body=image_bytes,
                ContentType='image/jpeg'
            )
            
            # Generate full URL
            image_url = f"{self.base_prefix}{file_location}"
            
            return image_url, file_location
            
        except ClientError as e:
            logger.error(f"S3 upload error: {e}")
            return None, None
        except Exception as e:
            logger.error(f"Unexpected S3 error: {e}")
            return None, None
    
    def get_full_url(self, file_location: str) -> str:
        """Convert file location to full S3 URL"""
        return f"{self.base_prefix}{file_location}"
    
    def download_image(self, file_location: str) -> Optional[bytes]:
        """Download image from S3 using file location"""
        try:
            # Remove leading slash for S3 key
            s3_key = file_location.lstrip('/')
            
            response = self.s3_client.get_object(
                Bucket=self.bucket_name,
                Key=s3_key
            )
            
            return response['Body'].read()
            
        except ClientError as e:
            logger.error(f"S3 download error: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected S3 download error: {e}")
            return None

class LanguageManager:
    def __init__(self):
        self.languages = {
            'en': 'English',
            'ta': 'Tamil (தமிழ்)',
            'te': 'Telugu (తెలుగు)',
            'hi': 'Hindi (हिन्दी)',
            'kn': 'Kannada (ಕನ್ನಡ)',
            'ml': 'Malayalam (മലയാളം)',
            'mr': 'Marathi (मराठी)',
            'gu': 'Gujarati (ગુજરાતી)',
            'bn': 'Bengali (বাংলা)'
        }
        
        self.messages = {
            'en': {
                'welcome': "👋 Hello! I'm your AI Nutrition Analyzer bot!\n\n📸 Send me a photo of any food and I'll provide:\n• Detailed nutritional information\n• Calorie count and macros\n• Health analysis and tips\n• Improvement suggestions\n\nJust take a clear photo of your meal and send it to me! 🍽️",
                'registration_name': "Welcome! I need to collect some basic information from you.\n\n📝 Please enter your full name:",
                'registration_language': "Great! Please select your preferred language for nutrition analysis:\n\n🌍 **Available Languages:**\n• **English**\n• **Tamil** (தமிழ்)\n• **Telugu** (తెలుగు)\n• **Hindi** (हिन्दी)\n• **Kannada** (ಕನ್ನಡ)\n• **Malayalam** (മലയാളം)\n• **Marathi** (मराठी)\n• **Gujarati** (ગુજરાતી)\n• **Bengali** (বাংলা)\n\n💬 Reply with the full language name (e.g., 'English', 'Tamil', 'Hindi')",
                'registration_complete': "✅ Registration completed successfully! You can now send me food photos for nutrition analysis.",
                'analyzing': "🔍 Analyzing your food image... This may take a few moments.",
                'help': "🆘 **How to use this bot:**\n\n1. Take a clear photo of your food\n2. Send the image to me\n3. Wait for the analysis (usually 10-30 seconds)\n4. Get detailed nutrition information!\n\n**Tips for best results:**\n• Take photos in good lighting\n• Show the food clearly from above\n• Include the whole serving if possible\n• One dish per photo works best\n\n**Language Commands:**\n• Type 'language' to change your preferred language\n• Use full names like 'English', 'Tamil', 'Hindi'\n\nSend me a food photo to get started! 📸"
            },
            'ta': {
                'welcome': "👋 வணக்கம்! நான் உங்கள் AI ஊட்டச்சத்து பகுப்பாய்வு பாட்!\n\n📸 எந்த உணவின் புகைப்படத்தையும் அனுப்புங்கள், நான் வழங்குவேன்:\n• விரிவான ஊட்டச்சத்து தகவல்\n• கலோரி எண்ணிக்கை மற்றும் மேக்ரோக்கள்\n• ஆரோக்கிய பகுப்பாய்வு மற்றும் குறிப்புகள்\n• மேம்படுத்தும் பரிந்துரைகள்\n\nஉங்கள் உணவின் தெளிவான புகைப்படத்தை எடுத்து அனுப்புங்கள! 🍽️",
                'analyzing': "🔍 உங்கள் உணவு படத்தை பகுப்பாய்வு செய்கிறேன்... இதற்கு சில நிமிடங்கள் ஆகலாம்.",
                'help': "🆘 **இந்த பாட்டை எப்படி பயன்படுத்துவது:**\n\n1. உங்கள் உணவின் தெளிவான புகைப்படத்தை எடுங்கள்\n2. படத்தை எனக்கு அனுப்புங்கள்\n3. பகுப்பாய்விற்காக காத்திருங்கள்\n4. விரிவான ஊட்டச்சத்து தகவலைப் பெறுங்கள்!\n\n**மொழி கட்டளைகள்:**\n• மொழி மாற்ற 'language' என்று தட்டச்சு செய்யவும்\n• 'Tamil', 'English' போன்ற முழு பெயர்களைப் பயன்படுத்தவும்\n\nதொடங்க எனக்கு உணவு புகைப்படம் ஒன்றை அனுப்புங்கள்! 📸"
            },
            'hi': {
                'welcome': "👋 नमस्ते! मैं आपका AI पोषण विश्लेषक बॉट हूँ!\n\n📸 मुझे किसी भी खाने की फोटो भेजें और मैं प्रदान करूंगा:\n• विस्तृत पोषण संबंधी जानकारी\n• कैलोरी गिनती और मैक्रोज़\n• स्वास्थ्य विश्लेषण और सुझाव\n• सुधार के सुझाव\n\nबस अपने भोजन की एक स्पष्ट तस्वीर लें और मुझे भेज दें! 🍽️",
                'analyzing': "🔍 आपकी खाने की तस्वीर का विश्लेषण कर रहा हूँ... इसमें कुछ समय लग सकता है।",
                'help': "🆘 **इस बॉट का उपयोग कैसे करें:**\n\n1. अपने खाने की स्पष्ट तस्वीर लें\n2. तस्वीर मुझे भेजें\n3. विश्लेषण का इंतजार करें\n4. विस्तृत पोषण जानकारी प्राप्त करें!\n\n**भाषा कमांड:**\n• भाषा बदलने के लिए 'language' टाइप करें\n• 'Hindi', 'English' जैसे पूरे नाम का उपयोग करें\n\nशुरू करने के लिए मुझे खाने की तस्वीर भेजें! 📸"
            }
        }
    
    def get_message(self, language: str, key: str) -> str:
        """Get message in specified language"""
        return self.messages.get(language, self.messages['en']).get(key, self.messages['en'][key])
    
    def get_language_name(self, code: str) -> str:
        """Get language name by code"""
        return self.languages.get(code, 'English')

    def get_language_options_text(self) -> str:
        """Get formatted language options for user selection using full names"""
        options = []
        for code, name in self.languages.items():
            options.append(f"• **{name.split(' (')[0]}**")  # Remove script part for cleaner display
        
        return "🌍 **Please select your preferred language:**\n\n" + "\n".join(options) + "\n\n💬 **Reply with the full language name** (e.g., English, Tamil, Hindi)"

class NutritionAnalyzer:
    def __init__(self):
        try:
            self.model = genai.GenerativeModel('gemini-1.5-flash')
            logger.info("Nutrition analyzer initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize nutrition analyzer: {e}")
            raise
        
    def analyze_image(self, image: Image.Image, language: str = 'en') -> str:
        """Analyze food image and return nutrition information in specified language"""
        
        language_prompts = {
            'en': "Analyze this food image and provide detailed nutritional information in English.",
            'ta': "இந்த உணவு படத்தை பகுப்பாய்வு செய்து தமிழில் விரிவான ஊட்டச்சத்து தகவல்களை வழங்கவும்.",
            'te': "ఈ ఆహార చిత్రాన్ని విశ్లేషించి తెలుగులో వివరణాత్మక పోషకాహార సమాచారాన్ని అందించండి.",
            'hi': "इस भोजन की छवि का विश्लेषण करें और हिंदी में विस्तृत पोषण संबंधी जानकारी प्रदान करें।",
            'kn': "ಈ ಆಹಾರ ಚಿತ್ರವನ್ನು ವಿಶ್ಲೇಷಿಸಿ ಮತ್ತು ಕನ್ನಡದಲ್ಲಿ ವಿವರವಾದ ಪೋಷಣೆ ಮಾಹಿತಿಯನ್ನು ಒದಗಿಸಿ।",
            'ml': "ഈ ഭക്ഷണ ചിത്രം വിശകലനം ചെയ്യുകയും മലയാളത്തിൽ വിശദമായ പോഷകാഹാര വിവരങ്ങൾ നൽകുകയും ചെയ്യുക।"
        }
        
        base_prompt = """
        Please provide a clear, easy-to-read response with the following information:

        🍽️ **DISH IDENTIFICATION**
        - Name and description of the dish
        - Type of cuisine
        - Confidence level in identification

        📏 **SERVING SIZE**
        - Estimated serving size and weight

        🔥 **NUTRITION FACTS (per serving)**
        - Calories
        - Protein, Carbohydrates, Fat, Fiber, Sugar (in grams)
        - Key vitamins and minerals

        💪 **HEALTH ANALYSIS**
        - Overall health score (1-10)
        - Nutritional strengths
        - Areas of concern

        💡 **IMPROVEMENT SUGGESTIONS**
        - Ways to make it healthier
        - Foods to add or reduce
        - Better cooking methods

        🚨 **DIETARY INFORMATION**
        - Potential allergens
        - Suitable for: Vegetarian/Vegan/Gluten-free/etc.

        Please format your response in a clear, conversational way that's easy to read on a mobile device.
        If you cannot clearly identify the food, please indicate this and provide your best assessment.
        """
        
        language_instruction = language_prompts.get(language, language_prompts['en'])
        full_prompt = f"{language_instruction}\n\n{base_prompt}"
        
        try:
            response = self.model.generate_content([full_prompt, image])
            return response.text.strip()
            
        except Exception as e:
            logger.error(f"Gemini analysis error: {e}")
            return f"❌ Sorry, I couldn't analyze this image. Please try again with a clearer photo of your food."

class WhatsAppBot:
    def __init__(self, token: str, phone_number_id: str):
        self.token = token
        self.phone_number_id = phone_number_id
        self.base_url = f"https://graph.facebook.com/v17.0/{phone_number_id}"
        
    def send_message(self, to: str, message: str) -> bool:
        """Send text message to WhatsApp user"""
        url = f"{self.base_url}/messages"
        
        headers = {
            'Authorization': f'Bearer {self.token}',
            'Content-Type': 'application/json'
        }
        
        data = {
            'messaging_product': 'whatsapp',
            'to': to,
            'type': 'text',
            'text': {'body': message}
        }
        
        try:
            response = requests.post(url, headers=headers, json=data, timeout=30)
            if response.status_code == 200:
                logger.info(f"Message sent successfully to {to}")
                return True
            else:
                logger.error(f"Failed to send message: {response.status_code} - {response.text}")
                return False
        except Exception as e:
            logger.error(f"Error sending message: {e}")
            return False
    
    def download_media(self, media_id: str) -> bytes:
        """Download media file from WhatsApp"""
        try:
            # Get media URL
            url = f"https://graph.facebook.com/v17.0/{media_id}"
            headers = {'Authorization': f'Bearer {self.token}'}
            
            response = requests.get(url, headers=headers, timeout=30)
            if response.status_code != 200:
                raise Exception(f"Failed to get media URL: {response.status_code}")
            
            media_data = response.json()
            media_url = media_data.get('url')
            
            if not media_url:
                raise Exception("No media URL found")
            
            # Download the actual media file
            media_response = requests.get(media_url, headers=headers, timeout=60)
            if media_response.status_code != 200:
                raise Exception(f"Failed to download media: {media_response.status_code}")
            
            return media_response.content
            
        except Exception as e:
            logger.error(f"Error downloading media {media_id}: {e}")
            raise

# Initialize components
try:
    db_manager = DatabaseManager()
    s3_manager = S3Manager()
    language_manager = LanguageManager()
    analyzer = NutritionAnalyzer()
    whatsapp_bot = WhatsAppBot(WHATSAPP_TOKEN, WHATSAPP_PHONE_NUMBER_ID)
    logger.info("All components initialized successfully")
except Exception as e:
    logger.error(f"Failed to initialize components: {e}")
    raise

@app.route('/', methods=['GET'])
def health():
    """Root endpoint for health check"""
    return jsonify({
        'status': 'healthy',
        'service': 'WhatsApp Nutrition Analyzer Bot',
        'timestamp': datetime.now().isoformat()
    }), 200

@app.route('/webhook', methods=['GET'])
def verify_webhook():
    """Verify WhatsApp webhook"""
    mode = request.args.get('hub.mode')
    token = request.args.get('hub.verify_token')
    challenge = request.args.get('hub.challenge')
    
    if mode == 'subscribe' and token == VERIFY_TOKEN:
        logger.info("Webhook verified successfully")
        return challenge
    else:
        logger.warning("Webhook verification failed")
        return 'Verification failed', 403

@app.route('/webhook', methods=['POST'])
def handle_webhook():
    """Handle incoming WhatsApp messages"""
    try:
        data = request.get_json()
        
        if not data:
            return jsonify({'status': 'no_data'}), 400
        
        # Check if this is a WhatsApp message
        if data.get('object') != 'whatsapp_business_account':
            return jsonify({'status': 'ignored'}), 200
        
        entries = data.get('entry', [])
        for entry in entries:
            changes = entry.get('changes', [])
            for change in changes:
                value = change.get('value', {})
                messages = value.get('messages', [])
                
                for message in messages:
                    process_message(message)
        
        return jsonify({'status': 'success'}), 200
        
    except Exception as e:
        logger.error(f"Webhook processing error: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

def process_message(message: Dict[str, Any]):
    """Process individual WhatsApp message"""
    try:
        message_type = message.get('type')
        sender = message.get('from')
        
        logger.info(f"Processing {message_type} message from {sender}")
        
        if message_type == 'text':
            handle_text_message(message)
        elif message_type == 'image':
            handle_image_message(message)
        else:
            # Handle other message types
            unsupported_message =(
                "🤖 *I can only process:*\n"
                "📝 Text messages\n"
                "📸 Food images\n\n"
                "Please send me a *food photo* for nutrition analysis!\n\n"
                "Type '*help*' if you need assistance."
            )
            whatsapp_bot.send_message(sender, unsupported_message)
            
    except Exception as e:
        logger.error(f"Error processing message: {e}")

def handle_text_message(message: Dict[str, Any]):
    """Handle incoming text messages"""
    try:
        sender = message.get('from')
        text_content = message.get('text', {}).get('body', '').strip().lower()
        
        logger.info(f"Text message from {sender}: {text_content}")
        
        # Check if user exists
        user = db_manager.get_user_by_phone(sender)
        
        # Handle different text commands
        if text_content in ['start', 'hello', 'hi', 'hey']:
            handle_start_command(sender, user)
        elif text_content == 'help':
            handle_help_command(sender, user)
        elif text_content == 'language':
            handle_language_command(sender)
        elif is_language_selection(text_content):
            handle_language_selection(sender, text_content, user)
        elif not user:
            # User doesn't exist, start registration
            handle_registration_flow(sender, text_content)
        else:
            # User exists but sent unrecognized text
            user_language = user.get('preferred_language', 'en') if user else 'en'
            help_message = language_manager.get_message(user_language, 'help')
            whatsapp_bot.send_message(sender, help_message)
            
    except Exception as e:
        logger.error(f"Error handling text message: {e}")

def handle_image_message(message: Dict[str, Any]):
    """Handle incoming image messages"""
    try:
        sender = message.get('from')
        image_data = message.get('image', {})
        media_id = image_data.get('id')
        
        logger.info(f"Image message from {sender}, media_id: {media_id}")
        
        # Check if user exists
        user = db_manager.get_user_by_phone(sender)
        if not user:
            # User doesn't exist, prompt registration
            registration_message = (
                "👋 Welcome! I need to get to know you first.\n\n"
                "📝 Please enter your full name to get started:"
            )
            whatsapp_bot.send_message(sender, registration_message)
            # Start registration session
            db_manager.update_registration_session(sender, 'name', {})
            return
        
        if 'user_id' not in user or user['user_id'] is None:
            logger.error(f"User {sender} does not have user_id: {user}")
            error_message = "❌ User registration incomplete. Please type 'start' to re-register."
            whatsapp_bot.send_message(sender, error_message)
            return
        
        user_language = user.get('preferred_language', 'en')
        
        # Send analysis started message
        analyzing_message = language_manager.get_message(user_language, 'analyzing')
        whatsapp_bot.send_message(sender, analyzing_message)
        
        # Download and process image
        try:
            # Download image from WhatsApp
            image_bytes = whatsapp_bot.download_media(media_id)
            
            # Upload to S3
            image_url, file_location = s3_manager.upload_image(image_bytes, user['user_id'])
            
            if not image_url or not file_location:
                error_message = "❌ Failed to process your image. Please try again with a different photo."
                whatsapp_bot.send_message(sender, error_message)
                return
            
            # Convert bytes to PIL Image for analysis
            image = Image.open(io.BytesIO(image_bytes))

            
            # Analyze image
            analysis_result = analyzer.analyze_image(image, user_language)

            success = db_manager.save_nutrition_analysis(
                user['user_id'], 
                file_location, 
                analysis_result
            )
            
            if not success:
                logger.error(f"Failed to save nutrition analysis for user {user['user_id']}")
            # Send analysis result

            whatsapp_bot.send_message(sender, analysis_result)
            
            # Send follow-up message
            followup_message = (
                "\n📸 Send me another food photo for more analysis!\n"
                "💬 Type 'help' for assistance or 'language' to change your language preference."
            )
            whatsapp_bot.send_message(sender, followup_message)
            
        except Exception as e:
            logger.error(f"Error processing image: {e}")
            error_message = (
                "❌ Sorry, I couldn't analyze your image. This might be because:\n\n"
                "• The image is not clear enough\n"
                "• No food is visible in the image\n"
                "• Technical processing error\n\n"
                "Please try again with a clear photo of your food! 📸"
            )
            whatsapp_bot.send_message(sender, error_message)
            
    except Exception as e:
        logger.error(f"Error handling image message: {e}")

def handle_start_command(sender: str, user: Optional[Dict]):
    """Handle start/welcome command"""
    try:
        if user:
            # Existing user
            user_language = user.get('preferred_language', 'en')
            welcome_message = language_manager.get_message(user_language, 'welcome')
        else:
            # New user - start registration
            welcome_message = (
                "👋 Welcome to the AI Nutrition Analyzer!\n\n"
                "I need to collect some basic information first.\n\n"
                "📝 Please enter your full name:"
            )
            # Start registration session
            db_manager.update_registration_session(sender, 'name', {})
        
        whatsapp_bot.send_message(sender, welcome_message)
        
    except Exception as e:
        logger.error(f"Error handling start command: {e}")

def handle_help_command(sender: str, user: Optional[Dict]):
    """Handle help command"""
    try:
        user_language = user.get('preferred_language', 'en') if user else 'en'
        help_message = language_manager.get_message(user_language, 'help')
        whatsapp_bot.send_message(sender, help_message)
        
    except Exception as e:
        logger.error(f"Error handling help command: {e}")

def handle_language_command(sender: str):
    """Handle language selection command"""
    try:
        language_options = language_manager.get_language_options_text()
        whatsapp_bot.send_message(sender, language_options)
        
    except Exception as e:
        logger.error(f"Error handling language command: {e}")

def is_language_selection(text: str) -> bool:
    """Check if text is a valid language selection"""
    language_names = {
        'english': 'en',
        'tamil': 'ta', 
        'telugu': 'te',
        'hindi': 'hi',
        'kannada': 'kn',
        'malayalam': 'ml',
        'marathi': 'mr',
        'gujarati': 'gu',
        'bengali': 'bn'
    }
    return text.lower() in language_names

def get_language_code(text: str) -> str:
    """Get language code from text input"""
    language_names = {
        'english': 'en',
        'tamil': 'ta',
        'telugu': 'te', 
        'hindi': 'hi',
        'kannada': 'kn',
        'malayalam': 'ml',
        'marathi': 'mr',
        'gujarati': 'gu',
        'bengali': 'bn'
    }
    return language_names.get(text.lower(), 'en')

def handle_language_selection(sender: str, text: str, user: Optional[Dict]):
    """Handle language selection from user"""
    try:
        language_code = get_language_code(text)
        language_name = language_manager.get_language_name(language_code)
        
        if user:
            # Update existing user's language
            success = db_manager.update_user_language(sender, language_code)
            if success:
                confirmation_message = f"✅ Language updated to {language_name}!\n\n📸 You can now send me food photos for nutrition analysis."
            else:
                confirmation_message = "❌ Failed to update language. Please try again."
        else:
            # Update registration session with language
            session = db_manager.get_registration_session(sender)
            if session:
                temp_data = session.get('temp_data', {})
                temp_data['language'] = language_code
                db_manager.update_registration_session(sender, 'completed', temp_data)
                
                # Complete registration if we have name
                if temp_data.get('name'):
                    success = db_manager.create_user(
                        sender, 
                        temp_data['name'], 
                        language_code
                    )
                    if success:
                        confirmation_message = f"✅ Registration completed! Language set to {language_name}.\n\n📸 You can now send me food photos for nutrition analysis."
                    else:
                        confirmation_message = "❌ Registration failed. Please try again by typing 'start'."
                else:
                    confirmation_message = f"✅ Language set to {language_name}!\n\n📝 Please enter your full name to complete registration:"
                    db_manager.update_registration_session(sender, 'name', temp_data)
            else:
                confirmation_message = "❌ No registration session found. Please type 'start' to begin."
        
        whatsapp_bot.send_message(sender, confirmation_message)
        
    except Exception as e:
        logger.error(f"Error handling language selection: {e}")

def handle_registration_flow(sender: str, text: str):
    """Handle user registration flow"""
    try:
        session = db_manager.get_registration_session(sender)
        
        if not session:
            # No session exists, start with name
            if len(text.strip()) < 2:
                whatsapp_bot.send_message(sender, "📝 Please enter a valid name (at least 2 characters):")
                return
            
            # Save name and ask for language
            temp_data = {'name': text.strip().title()}
            db_manager.update_registration_session(sender, 'language', temp_data)
            
            language_options = language_manager.get_language_options_text()
            whatsapp_bot.send_message(sender, f"👋 Nice to meet you, {temp_data['name']}!\n\n{language_options}")
            
        else:
            current_step = session.get('current_step', 'name')
            temp_data = session.get('temp_data', {})
            
            if current_step == 'name':
                # Validate and save name
                if len(text.strip()) < 2:
                    whatsapp_bot.send_message(sender, "📝 Please enter a valid name (at least 2 characters):")
                    return
                
                temp_data['name'] = text.strip().title()
                db_manager.update_registration_session(sender, 'language', temp_data)
                
                language_options = language_manager.get_language_options_text()
                whatsapp_bot.send_message(sender, f"👋 Nice to meet you, {temp_data['name']}!\n\n{language_options}")
                
            elif current_step == 'language':
                # Handle language selection
                if is_language_selection(text):
                    language_code = get_language_code(text)
                    temp_data['language'] = language_code
                    
                    # Complete registration
                    success = db_manager.create_user(
                        sender,
                        temp_data['name'],
                        language_code
                    )
                    
                    if success:
                        language_name = language_manager.get_language_name(language_code)
                        welcome_message = language_manager.get_message(language_code, 'welcome')
                        completion_message = f"✅ Registration completed successfully!\n\nLanguage: {language_name}\n\n{welcome_message}"
                        whatsapp_bot.send_message(sender, completion_message)
                    else:
                        whatsapp_bot.send_message(sender, "❌ Registration failed. Please try again by typing 'start'.")
                else:
                    language_options = language_manager.get_language_options_text()
                    whatsapp_bot.send_message(sender, f"❌ Invalid language selection.\n\n{language_options}")
    
    except Exception as e:
        logger.error(f"Error in registration flow: {e}")

@app.route('/health', methods=['GET'])
def health_check():
    """Comprehensive health check endpoint"""
    health_status = {
        'status': 'healthy',
        'service': 'WhatsApp Nutrition Analyzer Bot',
        'timestamp': datetime.now().isoformat(),
        'version': '1.0.0',
        'components': {}
    }
    
    overall_healthy = True
    
    # Check database connectivity
    try:
        conn = db_manager.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT 1")
        cursor.fetchone()
        cursor.close()
        conn.close()
        health_status['components']['database'] = {
            'status': 'healthy',
            'message': 'Database connection successful'
        }
    except Exception as e:
        health_status['components']['database'] = {
            'status': 'unhealthy',
            'message': f'Database connection failed: {str(e)}'
        }
        overall_healthy = False
    
    # Check AWS S3 connectivity
    try:
        s3_client.head_bucket(Bucket=AWS_S3_BUCKET)
        health_status['components']['s3'] = {
            'status': 'healthy',
            'message': 'S3 bucket accessible'
        }
    except Exception as e:
        health_status['components']['s3'] = {
            'status': 'unhealthy',
            'message': f'S3 connection failed: {str(e)}'
        }
        overall_healthy = False
    
    # Check Gemini API (basic configuration check)
    try:
        # Just check if the model is configured, don't make actual API call
        model = genai.GenerativeModel('gemini-1.5-flash')
        health_status['components']['gemini'] = {
            'status': 'healthy',
            'message': 'Gemini API configured'
        }
    except Exception as e:
        health_status['components']['gemini'] = {
            'status': 'unhealthy',
            'message': f'Gemini API configuration failed: {str(e)}'
        }
        overall_healthy = False
    
    # Check WhatsApp API configuration
    try:
        # Basic configuration check - don't make actual API call to avoid spam
        if WHATSAPP_TOKEN and WHATSAPP_PHONE_NUMBER_ID:
            health_status['components']['whatsapp'] = {
                'status': 'healthy',
                'message': 'WhatsApp API configured'
            }
        else:
            raise Exception("Missing WhatsApp configuration")
    except Exception as e:
        health_status['components']['whatsapp'] = {
            'status': 'unhealthy',
            'message': f'WhatsApp API configuration failed: {str(e)}'
        }
        overall_healthy = False
    
    # Check environment variables
    missing_vars = [var for var in required_env_vars if not os.getenv(var)]
    if missing_vars:
        health_status['components']['environment'] = {
            'status': 'unhealthy',
            'message': f'Missing environment variables: {missing_vars}'
        }
        overall_healthy = False
    else:
        health_status['components']['environment'] = {
            'status': 'healthy',
            'message': 'All required environment variables present'
        }
    
    # Add system metrics
    try:
        import psutil
        health_status['system'] = {
            'cpu_percent': psutil.cpu_percent(interval=1),
            'memory_percent': psutil.virtual_memory().percent,
            'disk_percent': psutil.disk_usage('/').percent
        }
    except ImportError:
        # psutil not available, skip system metrics
        pass
    except Exception as e:
        logger.warning(f"Could not get system metrics: {e}")
    
    # Set overall status
    if not overall_healthy:
        health_status['status'] = 'unhealthy'
    
    # Return appropriate HTTP status code
    status_code = 200 if overall_healthy else 503
    
    return jsonify(health_status), status_code

@app.route('/health/simple', methods=['GET'])
def simple_health_check():
    """Simple health check for load balancers"""
    return jsonify({
        'status': 'OK',
        'timestamp': datetime.now().isoformat()
    }), 200

@app.route('/health/ready', methods=['GET'])
def readiness_check():
    """Kubernetes readiness probe endpoint"""
    try:
        # Quick database connectivity check
        conn = db_manager.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT 1")
        cursor.close()
        conn.close()
        
        return jsonify({
            'status': 'ready',
            'timestamp': datetime.now().isoformat()
        }), 200
        
    except Exception as e:
        return jsonify({
            'status': 'not ready',
            'error': str(e),
            'timestamp': datetime.now().isoformat()
        }), 503

@app.route('/health/live', methods=['GET'])
def liveness_check():
    """Kubernetes liveness probe endpoint"""
    return jsonify({
        'status': 'alive',
        'timestamp': datetime.now().isoformat()
    }), 200

@app.route('/admin/stats', methods=['GET'])
def admin_stats():
    """Admin endpoint for system statistics"""
    try:
        conn = db_manager.get_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        # Get user statistics
        cursor.execute("SELECT COUNT(*) as total_users FROM users WHERE registration_status = 'completed'")
        total_users = cursor.fetchone()['total_users']
        
        cursor.execute("SELECT COUNT(*) as total_analyses FROM nutrition_analysis")
        total_analyses = cursor.fetchone()['total_analyses']
        
        cursor.execute("""
            SELECT DATE(created_at) as date, COUNT(*) as count 
            FROM nutrition_analysis 
            WHERE created_at >= NOW() - INTERVAL '7 days'
            GROUP BY DATE(created_at)
            ORDER BY date DESC
        """)
        recent_activity = cursor.fetchall()
        
        cursor.execute("""
            SELECT preferred_language, COUNT(*) as count
            FROM users 
            WHERE registration_status = 'completed'
            GROUP BY preferred_language
            ORDER BY count DESC
        """)
        language_stats = cursor.fetchall()
        
        cursor.close()
        conn.close()
        
        return jsonify({
            'total_users': total_users,
            'total_analyses': total_analyses,
            'recent_activity': [dict(row) for row in recent_activity],
            'language_distribution': [dict(row) for row in language_stats],
            'timestamp': datetime.now().isoformat()
        })
        
    except Exception as e:
        logger.error(f"Error getting admin stats: {e}")
        return jsonify({'error': 'Failed to fetch statistics'}), 500

@app.route('/admin/cleanup', methods=['POST'])
def admin_cleanup():
    """Admin endpoint to cleanup old data"""
    try:
        db_manager.cleanup_old_registration_sessions()
        return jsonify({'message': 'Cleanup completed successfully'})
    except Exception as e:
        logger.error(f"Cleanup error: {e}")
        return jsonify({'error': 'Cleanup failed'}), 500

@app.errorhandler(404)
def not_found(error):
    return jsonify({'error': 'Endpoint not found'}), 404

@app.errorhandler(500)
def internal_error(error):
    logger.error(f"Internal server error: {error}")
    return jsonify({'error': 'Internal server error'}), 500

if __name__ == '__main__':
    # Perform startup cleanup
    try:
        db_manager.cleanup_old_registration_sessions()
        logger.info("Startup cleanup completed")
    except Exception as e:
        logger.warning(f"Startup cleanup failed: {e}")
    
    # Start the Flask application
    port = int(os.getenv('PORT', 5000))
    debug_mode = os.getenv('FLASK_ENV') == 'development'
    
    logger.info(f"Starting WhatsApp Nutrition Analyzer Bot on port {port}")
    app.run(host='0.0.0.0', port=port, debug=debug_mode)

