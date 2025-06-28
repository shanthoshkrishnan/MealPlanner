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
from typing import List, Tuple

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
            # Step 1: Create users table
            self._execute_sql_safely([
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id SERIAL PRIMARY KEY,
                    phone_number VARCHAR(20) UNIQUE NOT NULL,
                    name VARCHAR(100),
                    preferred_language VARCHAR(10) DEFAULT 'en',
                    registration_status VARCHAR(20) DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                """
            ])
        
            # Step 2: Create language_messages table
            self._execute_sql_safely([
                """
                CREATE TABLE IF NOT EXISTS language_messages (
                    id SERIAL PRIMARY KEY,
                    language_code VARCHAR(10) NOT NULL,
                    message_key VARCHAR(50) NOT NULL,
                    message_text TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(language_code, message_key)
                );
                """
           ])
        
            # Step 3: Drop problematic columns safely
            self._drop_columns_safely()
        
            # Step 4: Recreate nutrition_analysis table
            self._execute_sql_safely([
                "DROP TABLE IF EXISTS nutrition_analysis CASCADE;",
                """
                CREATE TABLE nutrition_analysis (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                    file_location TEXT NOT NULL,
                    analysis_result TEXT,
                    nutrient_details JSONB,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                """
            ])
        
            # Step 5: Create user_registration_sessions table
            self._execute_sql_safely([
                """
                CREATE TABLE IF NOT EXISTS user_registration_sessions (
                    id SERIAL PRIMARY KEY,
                    phone_number VARCHAR(20) UNIQUE NOT NULL,
                    current_step VARCHAR(20) DEFAULT 'name',
                    temp_data JSONB DEFAULT '{}',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                """
           ])
        
            # Step 6: Create indexes
            self._execute_sql_safely([
                "CREATE INDEX IF NOT EXISTS idx_users_phone ON users(phone_number);",
                "CREATE INDEX IF NOT EXISTS idx_nutrition_user_id ON nutrition_analysis(user_id);",
                "CREATE INDEX IF NOT EXISTS idx_sessions_phone ON user_registration_sessions(phone_number);",
                "CREATE INDEX IF NOT EXISTS idx_messages_lang_key ON language_messages(language_code, message_key);"
           ])

            logger.info("Database initialized successfully")
        
        except Exception as e:
            logger.error(f"Database initialization error: {e}")
            raise
    
    def _execute_sql_safely(self, sql_statements):
        """Execute SQL statements in a safe transaction"""
        conn = None
        cursor = None
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
        
            for sql in sql_statements:
                cursor.execute(sql)
        
            conn.commit()
        
        except Exception as e:
            if conn:
                conn.rollback()
            logger.error(f"Error executing SQL: {e}")
            raise
        finally:
            if cursor:
                cursor.close()
            if conn:
                conn.close()

    def _drop_columns_safely(self):
        """Safely drop columns that might not exist"""
        columns_to_drop = [
            ("nutrition_analysis", "phone_number"),
            ("users", "address"),
            ("users", "email")
        ]
    
        for table_name, column_name in columns_to_drop:
            conn = None
            cursor = None
            try:
                conn = self.get_connection()
                cursor = conn.cursor()
            
                # Check if column exists first
                cursor.execute("""
                    SELECT column_name 
                    FROM information_schema.columns 
                    WHERE table_name = %s AND column_name = %s
                """, (table_name, column_name))
            
                if cursor.fetchone():
                    cursor.execute(f"ALTER TABLE {table_name} DROP COLUMN {column_name};")
                    conn.commit()
                    logger.info(f"Dropped column {column_name} from {table_name}")
            
            except Exception as e:
                logger.warning(f"Could not drop {column_name} from {table_name}: {e}")
                if conn:
                    conn.rollback()
            finally:
                if cursor:
                    cursor.close()
                if conn:
                    conn.close()

    def get_language_message(self, language_code: str, message_key: str) -> Optional[str]:
        """Get language message from database"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            
            cursor.execute(
                "SELECT message_text FROM language_messages WHERE language_code = %s AND message_key = %s",
                (language_code, message_key)
            )
            result = cursor.fetchone()
            
            cursor.close()
            conn.close()
            
            return result[0] if result else None
            
        except Exception as e:
            logger.error(f"Error getting language message: {e}")
            return None
    
    def insert_language_messages(self, messages_data: dict) -> bool:
        """Insert or update language messages in bulk"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            
            for language_code, messages in messages_data.items():
                for message_key, message_text in messages.items():
                    cursor.execute("""
                        INSERT INTO language_messages (language_code, message_key, message_text)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (language_code, message_key)
                        DO UPDATE SET 
                            message_text = EXCLUDED.message_text,
                            updated_at = CURRENT_TIMESTAMP
                    """, (language_code, message_key, message_text))
            
            conn.commit()
            cursor.close()
            conn.close()
            logger.info("Language messages inserted/updated successfully")
            return True
            
        except Exception as e:
            logger.error(f"Error inserting language messages: {e}")
            return False
        
    def get_all_language_messages(self) -> dict:
        """Get all language messages from database"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            
            cursor.execute("SELECT language_code, message_key, message_text FROM language_messages")
            results = cursor.fetchall()
            
            cursor.close()
            conn.close()
            
            # Structure the data
            messages = {}
            for language_code, message_key, message_text in results:
                if language_code not in messages:
                    messages[language_code] = {}
                messages[language_code][message_key] = message_text
            
            return messages
            
        except Exception as e:
            logger.error(f"Error getting all language messages: {e}")
            return {}

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
    
    def save_nutrition_analysis(self, user_id: int, file_location: str, analysis_result: str, nutrient_details: dict = None) -> bool:
        """Save nutrition analysis to database using user_id only"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            
            cursor.execute("""
                INSERT INTO nutrition_analysis (user_id, file_location, analysis_result, nutrient_details)
                VALUES (%s, %s, %s, %s)
            """, (user_id, file_location, analysis_result, json.dumps(nutrient_details) if nutrient_details else None))
            
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
        conn = None
        cursor = None
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
    
            # Check if users table exists
            cursor.execute("""
                SELECT EXISTS (
                    SELECT FROM information_schema.tables 
                    WHERE table_name = 'users'
                );
            """)
        
            table_exists = cursor.fetchone()[0]
        
            if not table_exists:
                logger.info("Users table doesn't exist, will be created in init_database")
                return
    
            # Check if users table has user_id as primary key
            cursor.execute("""
                SELECT tc.constraint_name
                FROM information_schema.table_constraints tc
                JOIN information_schema.key_column_usage kcu
                ON tc.constraint_name = kcu.constraint_name
                WHERE tc.table_name = 'users' 
                AND tc.constraint_type = 'PRIMARY KEY'
                AND kcu.column_name = 'user_id';
            """)
            has_user_id_pk = cursor.fetchone() is not None
    
            if not has_user_id_pk:
                logger.info("Users table exists but needs migration - will be handled in init_database")
    
            conn.commit()
    
        except Exception as e:
            logger.warning(f"Database migration check error: {e}")
            # Don't raise here, let init_database handle the creation
            if conn:
                conn.rollback()
        finally:
            if cursor:
                cursor.close()
            if conn:
                conn.close()
     
    def get_user_nutrition_history(self, user_id: int, limit: int = 10) -> List[Dict]:
        """Get user's nutrition analysis history with nutrient details"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor(cursor_factory=RealDictCursor)
        
            cursor.execute("""
                SELECT id, file_location, analysis_result, nutrient_details, created_at
                FROM nutrition_analysis 
                WHERE user_id = %s 
                ORDER BY created_at DESC 
                LIMIT %s
            """, (user_id, limit))
        
            history = cursor.fetchall()
        
            cursor.close()
            conn.close()
        
            return [dict(row) for row in history] if history else []
        
        except Exception as e:
            logger.error(f"Error getting nutrition history: {e}")
            return []


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
    def __init__(self, db_manager):
        self.db_manager = db_manager
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

        self.initialize_messages()

    def initialize_messages(self):
        """Initialize messages from database, insert default if empty"""
        try:
            # Check if messages exist in database
            existing_messages = self.db_manager.get_all_language_messages()

            if not existing_messages:
                logger.info("No messages found in database, initializing with default messages")
                # HERE IS WHERE YOU SHOULD PASTE YOUR MESSAGE CONTENT
                # Replace this with your actual message data
                default_messages = {
                    "en": {
                        "welcome": "👋 Hello! I'm your AI Nutrition Analyzer bot! Send me a photo of any food for detailed nutritional analysis.",
                        "language_selection": "Please select your preferred language for nutrition analysis.",
                        "ask_name": "Please enter your full name:",
                        "registration_complete": "✅ Registration completed successfully! You can now send me food photos for nutrition analysis.",
                        "analyzing": "🔍 Analyzing your food image... This may take a few moments.",
                        "help": "Send me a food photo to get detailed nutrition analysis. Type 'language' to change your language preference.",
                        "language_changed": "✅ Language updated successfully!",
                        "language_change_failed": "❌ Failed to update language. Please try again.",
                        "invalid_language": "❌ Invalid language selection. Please select from the available options.",
                        "unsupported_message": "🤖 I can only process text messages and food images. Please send me a food photo for nutrition analysis!",
                        "registration_failed": "❌ Registration failed. Please try again by typing 'start'.",
                        "invalid_name": "📝 Please enter a valid name (at least 2 characters):",
                        "image_processing_error": "❌ Sorry, I couldn't analyze your image. Please try again with a clearer photo of your food.",
                        "followup_message": "📸 Send me another food photo for more analysis! Type 'help' for assistance.",
                        "no_registration_session": "❌ No registration session found. Please type 'start' to begin.",
                        "user_incomplete": "❌ User registration incomplete. Please type 'start' to re-register.",
                        "unknown_command": "❓ I didn't understand that command. Type 'help' for assistance or send me a food photo for analysis.",
                    }
                }

                # Insert default messages
                success = self.db_manager.insert_language_messages(default_messages)
                if success:
                    logger.info("Default messages inserted successfully")
                else:
                    logger.error("Failed to insert default messages")
            else:
                logger.info(f"Messages loaded from database with languages: {list(existing_messages.keys())}")

        except Exception as e:
            logger.error(f"Error initializing messages: {e}")

    def get_message(self, language: str, key: str) -> str:
        """Get message in specified language from database"""
        try:
            message = self.db_manager.get_language_message(language, key)

            if message:
                return message
            else:
                # Fallback to English
                fallback_message = self.db_manager.get_language_message('en', key)
                if fallback_message:
                    logger.warning(f"Using English fallback for language '{language}', key '{key}'")
                    return fallback_message
                else:
                    logger.error(f"Message not found for key '{key}' in any language")
                    return f"Message not found: {key}"

        except Exception as e:
            logger.error(f"Error getting message: {e}")
            return f"Error retrieving message: {key}"

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
        
    def analyze_image(self, image: Image.Image, language: str = 'en') -> tuple[str, dict]:
        """Analyze food image and return nutrition information in specified language"""
        
        # Language-specific instructions for Gemini
        language_instructions = {
            'en': "Please respond in English.",
            'ta': "Please respond in Tamil language (தமிழ் மொழியில் பதிலளிக்கவும்). Write everything in Tamil script.",
            'te': "Please respond in Telugu language (తెలుగు భాషలో సమాధానం ఇవ్వండి). Write everything in Telugu script.",
            'hi': "Please respond in Hindi language (हिंदी भाषा में उत्तर दें). Write everything in Hindi script.",
            'kn': "Please respond in Kannada language (ಕನ್ನಡ ಭಾಷೆಯಲ್ಲಿ ಉತ್ತರಿಸಿ). Write everything in Kannada script.",
            'ml': "Please respond in Malayalam language (മലയാളം ഭാഷയിൽ ഉത്തരം നൽകുക). Write everything in Malayalam script.",
            'mr': "Please respond in Marathi language (मराठी भाषेत उत्तर द्या). Write everything in Marathi script.",
            'gu': "Please respond in Gujarati language (ગુજરાતી ભાષામાં જવાબ આપો). Write everything in Gujarati script.",
            'bn': "Please respond in Bengali language (বাংলা ভাষায় উত্তর দিন). Write everything in Bengali script."
        }
        
        # Get language instruction
        language_instruction = language_instructions.get(language, language_instructions['en'])
        
        # Enhanced prompt for better JSON extraction
        enhanced_prompt = f"""
        {language_instruction}

        Analyze this food image and provide ONLY a JSON response with the following exact structure. 
        Do not include any other text, explanations, or formatting - ONLY the JSON:

        {{
            "dish_identification": {{
                "name": "name of the dish in requested language",
                "cuisine_type": "type of cuisine in requested language", 
                "confidence_level": "high/medium/low",
                "description": "brief description in requested language"
            }},
            "serving_info": {{
                "estimated_weight_grams": 0,
                "serving_description": "serving size description in requested language"
            }},
            "nutrition_facts": {{
                "calories": 0,
                "protein_g": 0.0,
                "carbohydrates_g": 0.0,
                "fat_g": 0.0,
                "fiber_g": 0.0,
                "sugar_g": 0.0,
                "sodium_mg": 0.0,
                "saturated_fat_g": 0.0,
                "key_vitamins": ["list of vitamins in requested language"],
                "key_minerals": ["list of minerals in requested language"]
            }},
            "health_analysis": {{
                "health_score": 0,
                "health_grade": "A/B/C/D/F",
                "nutritional_strengths": ["list of strengths in requested language"],
                "areas_of_concern": ["list of concerns in requested language"],
                "overall_assessment": "brief overall health assessment in requested language"
            }},
            "dietary_information": {{
                "potential_allergens": ["list of allergens in requested language"],
                "dietary_compatibility": {{
                    "vegetarian": true/false,
                    "vegan": true/false,
                    "gluten_free": true/false,
                    "dairy_free": true/false,
                    "keto_friendly": true/false,
                    "low_sodium": true/false
                }}
            }},
            "improvement_suggestions": {{
                "healthier_alternatives": ["list of alternatives in requested language"],
                "portion_recommendations": "portion advice in requested language",
                "cooking_modifications": ["cooking suggestions in requested language"],
                "nutritional_additions": ["foods to add in requested language"]
            }},
            "detailed_breakdown": {{
                "ingredients_identified": ["list of visible ingredients in requested language"],
                "cooking_method": "identified cooking method in requested language",
                "meal_category": "breakfast/lunch/dinner/snack in requested language"
            }}
        }}

        CRITICAL: Respond with ONLY the JSON object. No additional text, explanations, or formatting.
        """
        
        try:
            response = self.model.generate_content([enhanced_prompt, image])
            json_response = response.text.strip()
            
            # Clean the response to ensure it's valid JSON
            json_response = self._clean_json_response(json_response)
            
            # Parse JSON
            nutrition_data = json.loads(json_response)
            
            # Create user-friendly message from parsed JSON
            user_message = self._create_user_message(nutrition_data, language)
            
            return user_message, nutrition_data

        except json.JSONDecodeError as e:
            logger.error(f"JSON parsing error: {e}")
            logger.error(f"Raw response: {response.text}")
            # Fallback to original method or simple message
            return self._handle_json_error(language), {}
            
        except Exception as e:
            logger.error(f"Gemini analysis error: {e}")
            return self._get_error_message(language), {}
     
    def _clean_json_response(self, response: str) -> str:
        """Clean the JSON response to ensure it's valid"""
        try:
            # Remove any markdown formatting
            response = response.strip()
            if response.startswith('```json'):
                response = response[7:]
            if response.startswith('```'):
                response = response[3:]
            if response.endswith('```'):
                response = response[:-3]
            
            # Find the first { and last } to extract just the JSON
            start_idx = response.find('{')
            end_idx = response.rfind('}')
            
            if start_idx != -1 and end_idx != -1:
                response = response[start_idx:end_idx + 1]
            
            return response.strip()
            
        except Exception as e:
            logger.error(f"Error cleaning JSON response: {e}")
            return response
        
    def _create_user_message(self, nutrition_data: dict, language: str) -> str:
        """Create a formatted user message from parsed JSON data"""
        try:
            # Extract data with fallbacks
            dish_info = nutrition_data.get('dish_identification', {})
            serving_info = nutrition_data.get('serving_info', {})
            nutrition_facts = nutrition_data.get('nutrition_facts', {})
            health_analysis = nutrition_data.get('health_analysis', {})
            dietary_info = nutrition_data.get('dietary_information', {})
            improvements = nutrition_data.get('improvement_suggestions', {})
            
            # Language-specific emojis and formatting
            message_parts = []
            
            # Dish identification section
            message_parts.append("🍽️ DISH IDENTIFICATION")
            message_parts.append(f"• Name: {dish_info.get('name', 'Unknown dish')}")
            message_parts.append(f"• Cuisine: {dish_info.get('cuisine_type', 'Unknown')}")
            message_parts.append(f"• Confidence: {dish_info.get('confidence_level', 'Medium')}")
            if dish_info.get('description'):
                message_parts.append(f"• Description: {dish_info.get('description')}")
            message_parts.append("")
            
            # Serving size section
            message_parts.append("📏 SERVING SIZE")
            weight = serving_info.get('estimated_weight_grams', 0)
            if weight > 0:
                message_parts.append(f"• Weight: ~{weight}g")
            message_parts.append(f"• Size: {serving_info.get('serving_description', 'Standard serving')}")
            message_parts.append("")
            
            # Nutrition facts section
            message_parts.append("🔥 NUTRITION FACTS (per serving)")
            message_parts.append(f"• Calories: {nutrition_facts.get('calories', 0)}")
            message_parts.append(f"• Protein: {nutrition_facts.get('protein_g', 0)}g")
            message_parts.append(f"• Carbohydrates: {nutrition_facts.get('carbohydrates_g', 0)}g")
            message_parts.append(f"• Fat: {nutrition_facts.get('fat_g', 0)}g")
            message_parts.append(f"• Fiber: {nutrition_facts.get('fiber_g', 0)}g")
            message_parts.append(f"• Sugar: {nutrition_facts.get('sugar_g', 0)}g")
            message_parts.append(f"• Sodium: {nutrition_facts.get('sodium_mg', 0)}mg")
            
            # Vitamins and minerals
            vitamins = nutrition_facts.get('key_vitamins', [])
            minerals = nutrition_facts.get('key_minerals', [])
            if vitamins:
                message_parts.append(f"• Key Vitamins: {', '.join(vitamins)}")
            if minerals:
                message_parts.append(f"• Key Minerals: {', '.join(minerals)}")
            message_parts.append("")
            
            # Health analysis section
            message_parts.append("💪 HEALTH ANALYSIS")
            health_score = health_analysis.get('health_score', 0)
            health_grade = health_analysis.get('health_grade', 'N/A')
            message_parts.append(f"• Health Score: {health_score}/100 (Grade: {health_grade})")
            
            strengths = health_analysis.get('nutritional_strengths', [])
            if strengths:
                message_parts.append("• Nutritional Strengths:")
                for strength in strengths[:3]:  # Limit to top 3
                    message_parts.append(f"  - {strength}")
            
            concerns = health_analysis.get('areas_of_concern', [])
            if concerns:
                message_parts.append("• Areas of Concern:")
                for concern in concerns[:3]:  # Limit to top 3
                    message_parts.append(f"  - {concern}")
            
            if health_analysis.get('overall_assessment'):
                message_parts.append(f"• Assessment: {health_analysis.get('overall_assessment')}")
            message_parts.append("")
            
            # Improvement suggestions
            message_parts.append("💡 IMPROVEMENT SUGGESTIONS")
            alternatives = improvements.get('healthier_alternatives', [])
            if alternatives:
                message_parts.append("• Healthier Options:")
                for alt in alternatives[:2]:  # Limit to top 2
                    message_parts.append(f"  - {alt}")
            
            portion_rec = improvements.get('portion_recommendations')
            if portion_rec:
                message_parts.append(f"• Portion Advice: {portion_rec}")
            
            cooking_mods = improvements.get('cooking_modifications', [])
            if cooking_mods:
                message_parts.append("• Cooking Tips:")
                for mod in cooking_mods[:2]:  # Limit to top 2
                    message_parts.append(f"  - {mod}")
            message_parts.append("")
            
            # Dietary information
            message_parts.append("🚨 DIETARY INFORMATION")
            allergens = dietary_info.get('potential_allergens', [])
            if allergens:
                message_parts.append(f"• Potential Allergens: {', '.join(allergens)}")
            
            compatibility = dietary_info.get('dietary_compatibility', {})
            dietary_tags = []
            for diet_type, compatible in compatibility.items():
                if compatible:
                    dietary_tags.append(diet_type.replace('_', ' ').title())
            
            if dietary_tags:
                message_parts.append(f"• Suitable for: {', '.join(dietary_tags)}")
            
            return "\n".join(message_parts)
            
        except Exception as e:
            logger.error(f"Error creating user message: {e}")
            return self._get_fallback_message(nutrition_data, language)
        
    def _get_fallback_message(self, nutrition_data: dict, language: str) -> str:
        """Create a simple fallback message if detailed formatting fails"""
        try:
            dish_name = nutrition_data.get('dish_identification', {}).get('name', 'Unknown dish')
            calories = nutrition_data.get('nutrition_facts', {}).get('calories', 0)
            health_score = nutrition_data.get('health_analysis', {}).get('health_score', 0)
            
            fallback_messages = {
                'en': f"🍽️ Analyzed: {dish_name}\n🔥 Calories: {calories}\n💪 Health Score: {health_score}/10\n\n📸 Send another food photo for more analysis!",
                'ta': f"🍽️ பகுப்பாய்வு: {dish_name}\n🔥 கலோரிகள்: {calories}\n💪 ஆரோக்கிய மதிப்பெண்: {health_score}/10\n\n📸 மேலும் பகுப்பாய்வுக்கு மற்றொரு உணவு புகைப்படம் அனுப்பவும்!",
                'hi': f"🍽️ विश्लेषण: {dish_name}\n🔥 कैलोरी: {calories}\n💪 स्वास्थ्य स्कोर: {health_score}/10\n\n📸 अधिक विश्लेषण के लिए दूसरी खाना फोटो भेजें!"
            }
            
            return fallback_messages.get(language, fallback_messages['en'])
            
        except Exception:
            return self._get_error_message(language)
    def _handle_json_error(self, language: str) -> str:
        """Handle JSON parsing errors"""
        error_messages = {
            'en': "🤖 I analyzed your food but had trouble formatting the response. Please try again with another photo.",
            'ta': "🤖 உங்கள் உணவை பகுப்பாய்வு செய்தேன் ஆனால் பதிலை வடிவமைப்பதில் சிக்கல் ஏற்பட்டது. மற்றொரு புகைப்படத்துடன் மீண்டும் முயற்சிக்கவும்.",
            'hi': "🤖 मैंने आपके भोजन का विश्लेषण किया लेकिन उत्तर को प्रारूपित करने में परेशानी हुई। कृपया दूसरी तस्वीर के साथ पुनः प्रयास करें।"
        }
        return error_messages.get(language, error_messages['en'])
    def _get_error_message(self, language: str) -> str:
        """Get error message in specified language"""
        error_messages = {
            'en': "❌ Sorry, I couldn't analyze this image. Please try again with a clearer photo of your food.",
            'ta': "❌ மன்னிக்கவும், இந்த படத்தை பகுப்பாய்வு செய்ய முடியவில்லை. உங்கள் உணவின் தெளிவான புகைப்படத்துடன் மீண்டும் முயற்சிக்கவும்.",
            'te': "❌ క్షమించండి, ఈ చిత్రాన్ని విశ్లేషించలేకపోయాను. దయచేసి మీ ఆహారం యొక్క స్పష్టమైన ఫోటోతో మళ్లీ ప్రయత్నించండి.",
            'hi': "❌ क्षमा करें, मैं इस छवि का विश्लेषण नहीं कर सका। कृपया अपने भोजन की स्पष्ट तस्वीर के साथ पुनः प्रयास करें।",
            'kn': "❌ ಕ್ಷಮಿಸಿ, ನಾನು ಈ ಚಿತ್ರವನ್ನು ವಿಶ್ಲೇಷಿಸಲು ಸಾಧ್ಯವಾಗಲಿಲ್ಲ. ದಯವಿಟ್ಟು ನಿಮ್ಮ ಆಹಾರದ ಸ್ಪಷ್ಟ ಫೋಟೋದೊಂದಿಗೆ ಮತ್ತೆ ಪ್ರಯತ್ನಿಸಿ.",
            'ml': "❌ ക്ഷമിക്കണം, ഈ ചിത്രം വിശകലനം ചെയ്യാൻ എനിക്ക് കഴിഞ്ഞില്ല. ദയവായി നിങ്ങളുടെ ഭക്ഷണത്തിന്റെ വ്യക്തമായ ഫോട്ടോ ഉപയോഗിച്ച് വീണ്ടും ശ്രമിക്കുക.",
            'mr': "❌ माफ करा, मी या प्रतिमेचे विश्लेषण करू शकलो नाही. कृपया आपल्या अन्नाच्या स्पष्ट फोटोसह पुन्हा प्रयत्न करा.",
            'gu': "❌ માફ કરશો, હું આ છબીનું વિશ્લેષણ કરી શક્યો નથી. કૃપા કરીને તમારા ખોરાકના સ્પષ્ટ ફોટો સાથે ફરીથી પ્રયાસ કરો.",
            'bn': "❌ দুঃখিত, আমি এই ছবিটি বিশ্লেষণ করতে পারিনি। দয়া করে আপনার খাবারের স্পষ্ট ফটো দিয়ে আবার চেষ্টা করুন।"
        }
        return error_messages.get(language, error_messages['en'])
            
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
    language_manager = LanguageManager(db_manager)
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
            user = db_manager.get_user_by_phone(sender)
            user_language = user.get('preferred_language', 'en') if user else 'en'
            unsupported_message = language_manager.get_message(user_language, 'unsupported_message')
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
            unknown_messsage = language_manager.get_message(user_language, 'unknown_command')
            whatsapp_bot.send_message(sender, unknown_messsage)
            
    except Exception as e:
        logger.error(f"Error handling text message: {e}")

# Updated handle_image_message function
def handle_image_message(message: Dict[str, Any]):
    """Handle incoming image messages with enhanced JSON processing"""
    try:
        sender = message.get('from')
        image_data = message.get('image', {})
        media_id = image_data.get('id')
        
        logger.info(f"Image message from {sender}, media_id: {media_id}")
        
        # Check if user exists
        user = db_manager.get_user_by_phone(sender)
        if not user:
            # User doesn't exist, start registration with language
            welcome_message = language_manager.get_message('en', 'language_selection') + "\n\n" + language_manager.get_language_options_text()
            whatsapp_bot.send_message(sender, welcome_message)
            db_manager.update_registration_session(sender, 'language', {})
            return
        
        if 'user_id' not in user or user['user_id'] is None:
            logger.error(f"User {sender} does not have user_id: {user}")
            user_language = user.get('preferred_language', 'en')
            error_message = language_manager.get_message(user_language, 'user_incomplete')
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
                error_message = language_manager.get_message(user_language, 'image_processing_error')
                whatsapp_bot.send_message(sender, error_message)
                return
            
            # Convert bytes to PIL Image for analysis
            image = Image.open(io.BytesIO(image_bytes))
            
            # Analyze image - now returns formatted message and structured JSON
            user_message, nutrition_json = analyzer.analyze_image(image, user_language)
            
            # Enhanced logging of structured data
            if nutrition_json:
                dish_name = nutrition_json.get('dish_identification', {}).get('name', 'Unknown')
                calories = nutrition_json.get('nutrition_facts', {}).get('calories', 0)
                health_score = nutrition_json.get('health_analysis', {}).get('health_score', 0)
                logger.info(f"Analyzed: {dish_name}, Calories: {calories}, Health Score: {health_score}")
            
            # Save analysis with comprehensive nutrient details
            success = db_manager.save_nutrition_analysis(
                user['user_id'], 
                file_location, 
                user_message,  # The formatted message for display
                nutrition_json  # The complete structured data
            )
            
            if not success:
                logger.error(f"Failed to save nutrition analysis for user {user['user_id']}")
            else:
                logger.info(f"Successfully saved nutrition analysis for user {user['user_id']}")
            
            # Send the formatted analysis result to user
            whatsapp_bot.send_message(sender, user_message)
            
            # Optional: Send additional insights if health score is concerning
            if nutrition_json and nutrition_json.get('health_analysis', {}).get('health_score', 10) < 4:
                health_warning = get_health_warning_message(user_language)
                whatsapp_bot.send_message(sender, health_warning)
            
            # Send follow-up message
            followup_message = language_manager.get_message(user_language, 'followup_message')
            whatsapp_bot.send_message(sender, followup_message)
            
        except Exception as e:
            logger.error(f"Error processing image: {e}")
            error_message = language_manager.get_message(user_language, 'image_processing_error')
            whatsapp_bot.send_message(sender, error_message)
            
    except Exception as e:
        logger.error(f"Error handling image message: {e}")

def get_health_warning_message(language: str) -> str:
    """Get health warning message for low-scoring foods"""
    warnings = {
        'en': "⚠️ This food has a low health score. Consider balancing it with healthier options or eating smaller portions.",
        'ta': "⚠️ இந்த உணவு குறைந்த ஆரோக்கிய மதிப்பெண் கிடைத்துள்ளது. ஆரோக்கியமான விருப்பங்களுடன் சமநிலைப்படுத்த அல்லது சிறிய பகுதிகளை சாப்பிட பரிசீலிக்கவும்.",
        'hi': "⚠️ इस भोजन का स्वास्थ्य स्कोर कम है। इसे स्वस्थ विकल्पों के साथ संतुलित करने या छोटे हिस्से खाने पर विचार करें।"
    }
    return warnings.get(language, warnings['en'])

def handle_start_command(sender: str, user: Optional[Dict]):
    """Handle start/welcome command"""
    try:
        if user:
            # Existing user
            user_language = user.get('preferred_language', 'en')
            welcome_message = language_manager.get_message(user_language, 'welcome')
        else:
            # New user - start registration
            welcome_message = language_manager.get_message('en', 'language_selection') + "\n\n" + language_manager.get_language_options_text()
            # Start registration session with language step
            db_manager.update_registration_session(sender, 'language', {})
        
        whatsapp_bot.send_message(sender, welcome_message)
        
    except Exception as e:
        logger.error(f"Error handling start command: {e}")

# Update the handle_help_command function
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
                confirmation_message = language_manager.get_message(language_code, 'language_changed') + f" ({language_name})\n\n" + language_manager.get_message(language_code, 'welcome')
            else:
                confirmation_message = language_manager.get_message(user.get('preferred_language', 'en'), 'language_change_failed')
        else:
            # Handle registration flow
            session = db_manager.get_registration_session(sender)
            if session:
                temp_data = session.get('temp_data', {})
                temp_data['language'] = language_code
                
                # Move to name step
                db_manager.update_registration_session(sender, 'name', temp_data)
                confirmation_message = language_manager.get_message(language_code, 'ask_name')
            else:
                confirmation_message = language_manager.get_message('en', 'no_registration_session')
        
        whatsapp_bot.send_message(sender, confirmation_message)
        
    except Exception as e:
        logger.error(f"Error handling language selection: {e}")

def handle_registration_flow(sender: str, text: str):
    """Handle user registration flow - Language first, then name"""
    try:
        session = db_manager.get_registration_session(sender)
        
        if not session:
            # No session exists, check if it's a language selection
            if is_language_selection(text):
                handle_language_selection(sender, text, None)
            else:
                # Start with language selection
                welcome_message = language_manager.get_message('en', 'language_selection') + "\n\n" + language_manager.get_language_options_text()
                whatsapp_bot.send_message(sender, welcome_message)
                db_manager.update_registration_session(sender, 'language', {})
            return
        
        current_step = session.get('current_step', 'language')
        temp_data = session.get('temp_data', {})
        
        if current_step == 'language':
            # Handle language selection
            if is_language_selection(text):
                handle_language_selection(sender, text, None)
            else:
                invalid_message = language_manager.get_message('en', 'invalid_language') + "\n\n" + language_manager.get_language_options_text()
                whatsapp_bot.send_message(sender, invalid_message)
                
        elif current_step == 'name':
            # Handle name input
            if len(text.strip()) < 2:
                selected_language = temp_data.get('language', 'en')
                invalid_name_message = language_manager.get_message(selected_language, 'invalid_name')
                whatsapp_bot.send_message(sender, invalid_name_message)
                return
            
            # Save name and complete registration
            temp_data['name'] = text.strip().title()
            selected_language = temp_data.get('language', 'en')
            
            # Create user
            success = db_manager.create_user(
                sender,
                temp_data['name'],
                selected_language
            )
            
            if success:
                completion_message = language_manager.get_message(selected_language, 'registration_complete') + "\n\n" + language_manager.get_message(selected_language, 'welcome')
                whatsapp_bot.send_message(sender, completion_message)
            else:
                failed_message = language_manager.get_message(selected_language, 'registration_failed')
                whatsapp_bot.send_message(sender, failed_message)
    
    except Exception as e:
        logger.error(f"Error in registration flow: {e}")

@app.route('/bsp/analyze', methods=['POST'])
def bsp_analyze():
    """BSP endpoint for nutrition analysis using existing classes"""
    try:
        # Initialize variables
        data = {}
        files = request.files
        
        # Try to get data from different sources
        if request.is_json:
            # JSON request
            data = request.get_json() or {}
        elif request.form:
            # Form data request
            data = {
                'phone_number': request.form.get('phone_number'),
                'user_id': request.form.get('user_id'),
                'message': request.form.get('message', ''),
                'language': request.form.get('language', 'en')
            }
        else:
            # Try to parse as JSON anyway (fallback)
            try:
                data = request.get_json(force=True) or {}
            except:
                data = {}
        
        # Clean up data - remove None values and empty strings
        data = {k: v for k, v in data.items() if v is not None and v != ''}
        
        if not data and not files:
            return jsonify({
                'status': 'error',
                'message': 'No data or files provided'
            }), 400
        
        # Get user identifier (phone number or user_id)
        user_phone = data.get('phone_number')
        user_id = data.get('user_id')
        message_text = data.get('message', '').strip()
        language_code = data.get('language', 'en')
        
        # Handle file upload (image analysis)
        if 'image' in files:
            return handle_bsp_image_analysis(files['image'], user_phone, user_id, language_code)
        
        # Handle text message
        if message_text and user_phone:
            return handle_bsp_text_message(user_phone, message_text, language_code)
        
        return jsonify({
            'status': 'error',
            'message': 'Invalid request format. Provide either image file or text message with phone_number'
        }), 400
        
    except Exception as e:
        logger.error(f"BSP endpoint error: {e}")
        return jsonify({
            'status': 'error',
            'message': f'Internal server error: {str(e)}'
        }), 500

def handle_bsp_image_analysis(image_file, user_phone=None, user_id=None, language_code='en'):
    """Handle BSP image analysis using existing classes"""
    try:
        # Validate image file
        if not image_file or not hasattr(image_file, 'filename') or not image_file.filename:
            return jsonify({
                'status': 'error',
                'message': 'No valid image file provided'
            }), 400
        
        # Check file type
        allowed_extensions = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
        file_extension = image_file.filename.rsplit('.', 1)[1].lower() if '.' in image_file.filename else ''
        
        if file_extension not in allowed_extensions:
            return jsonify({
                'status': 'error',
                'message': f'Unsupported file type. Allowed: {", ".join(allowed_extensions)}'
            }), 400
        
        # Validate user identifier
        if not user_phone and not user_id:
            return jsonify({
                'status': 'error',
                'message': 'Either phone_number or user_id must be provided'
            }), 400
        
        # Get or create user
        user = None
        if user_phone:
            user = db_manager.get_user_by_phone(user_phone)
            if not user:
                # Create user with basic info for BSP
                try:
                    success = db_manager.create_user(
                        user_phone,
                        f"BSP_User_{user_phone[-4:]}",  # Generic name
                        language_code
                    )
                    if success:
                        user = db_manager.get_user_by_phone(user_phone)
                    else:
                        return jsonify({
                            'status': 'error',
                            'message': 'Failed to create user'
                        }), 500
                except Exception as e:
                    logger.error(f"User creation error: {e}")
                    return jsonify({
                        'status': 'error',
                        'message': 'Failed to create user'
                    }), 500
        elif user_id:
            # For direct user_id usage - validate it exists if possible
            # If you have a method to validate user_id, use it here
            user = {'user_id': user_id, 'preferred_language': language_code}
        
        if not user or 'user_id' not in user:
            return jsonify({
                'status': 'error',
                'message': 'Invalid user data'
            }), 400
        
        user_language = user.get('preferred_language', language_code)
        
        # Read image bytes with size validation
        image_file.seek(0)  # Reset file pointer
        image_bytes = image_file.read()
        
        if len(image_bytes) == 0:
            return jsonify({
                'status': 'error',
                'message': 'Empty image file'
            }), 400
        
        # Check file size (optional - add reasonable limit)
        max_file_size = 10 * 1024 * 1024  # 10MB
        if len(image_bytes) > max_file_size:
            return jsonify({
                'status': 'error',
                'message': f'File too large. Maximum size: {max_file_size // (1024*1024)}MB'
            }), 400
        
        # Convert bytes to PIL Image for validation BEFORE uploading
        try:
            image = Image.open(io.BytesIO(image_bytes))
            # Validate image dimensions
            if image.width < 50 or image.height < 50:
                return jsonify({
                    'status': 'error',
                    'message': 'Image too small. Minimum size: 50x50 pixels'
                }), 400
            
            # Convert to RGB if necessary (for analysis consistency)
            if image.mode != 'RGB':
                image = image.convert('RGB')
                
        except Exception as e:
            logger.error(f"PIL Image validation error: {e}")
            return jsonify({
                'status': 'error',
                'message': 'Invalid image format or corrupted image'
            }), 400
        
        # Upload to S3 using existing S3Manager
        try:
            image_url, file_location = s3_manager.upload_image(image_bytes, user['user_id'])
            
            if not image_url or not file_location:
                return jsonify({
                    'status': 'error',
                    'message': 'Failed to upload image to storage'
                }), 500
        except Exception as e:
            logger.error(f"S3 upload error: {e}")
            return jsonify({
                'status': 'error',
                'message': 'Image upload failed'
            }), 500
        
        # Analyze image using existing NutritionAnalyzer
        try:
            user_message, nutrition_json = analyzer.analyze_image(image, user_language)
            
            # Validate analysis results
            if not user_message and not nutrition_json:
                logger.warning(f"Empty analysis result for user {user['user_id']}")
                return jsonify({
                    'status': 'error',
                    'message': 'Could not analyze the image. Please try with a clearer image of food.'
                }), 400
                
        except Exception as e:
            logger.error(f"Nutrition analysis error: {e}")
            return jsonify({
                'status': 'error',
                'message': 'Failed to analyze image. Please try again with a clearer image.'
            }), 500
        
        # Save analysis using existing DatabaseManager
        analysis_saved = False
        try:
            success = db_manager.save_nutrition_analysis(
                user['user_id'], 
                file_location, 
                user_message,
                nutrition_json
            )
            
            if success:
                analysis_saved = True
            else:
                logger.warning(f"Failed to save nutrition analysis for user {user['user_id']}")
        except Exception as e:
            logger.error(f"Database save error: {e}")
            # Don't fail the request if saving fails, but log it
        
        # Prepare response
        response_data = {
            'status': 'success',
            'message': 'Image analyzed successfully',
            'data': {
                'user_message': user_message,
                'nutrition_analysis': nutrition_json,
                'image_url': image_url,
                'language': user_language,
                'analysis_saved': analysis_saved
            }
        }
        
        # Add health warning if needed
        try:
            if nutrition_json and nutrition_json.get('health_analysis', {}).get('health_score', 10) < 4:
                health_warning = get_health_warning_message(user_language)
                response_data['data']['health_warning'] = health_warning
        except Exception as e:
            logger.error(f"Health warning generation error: {e}")
            # Don't fail the request if health warning fails
        
        return jsonify(response_data), 200
        
    except Exception as e:
        logger.error(f"BSP image analysis error: {e}")
        return jsonify({
            'status': 'error',
            'message': f'Image analysis failed: {str(e)}'
        }), 500

def handle_bsp_text_message(user_phone, message_text, language_code='en'):
    """Handle BSP text messages using existing classes"""
    try:
        text_content = message_text.lower().strip()
        
        # Check if user exists
        user = db_manager.get_user_by_phone(user_phone)
        
        response_data = {
            'status': 'success',
            'message': 'Text message processed',
            'data': {
                'user_phone': user_phone,
                'language': language_code,
                'response_message': ''
            }
        }
        
        # Handle different text commands using existing logic
        if text_content in ['start', 'hello', 'hi', 'hey']:
            if user:
                user_language = user.get('preferred_language', language_code)
                welcome_message = language_manager.get_message(user_language, 'welcome')
                response_data['data']['response_message'] = welcome_message
                response_data['data']['user_exists'] = True
            else:
                welcome_message = (language_manager.get_message('en', 'language_selection') + 
                                 "\n\n" + language_manager.get_language_options_text())
                response_data['data']['response_message'] = welcome_message
                response_data['data']['user_exists'] = False
                response_data['data']['registration_needed'] = True
                
                # Start registration session
                db_manager.update_registration_session(user_phone, 'language', {})
                
        elif text_content == 'help':
            user_language = user.get('preferred_language', language_code) if user else language_code
            help_message = language_manager.get_message(user_language, 'help')
            response_data['data']['response_message'] = help_message
            
        elif text_content == 'language':
            language_options = language_manager.get_language_options_text()
            response_data['data']['response_message'] = language_options
            response_data['data']['action'] = 'language_selection'
            
        elif is_language_selection(text_content):
            language_code_selected = get_language_code(text_content)
            language_name = language_manager.get_language_name(language_code_selected)
            
            if user:
                # Update existing user's language
                success = db_manager.update_user_language(user_phone, language_code_selected)
                if success:
                    confirmation_message = (language_manager.get_message(language_code_selected, 'language_changed') + 
                                          f" ({language_name})\n\n" + 
                                          language_manager.get_message(language_code_selected, 'welcome'))
                    response_data['data']['response_message'] = confirmation_message
                    response_data['data']['language_updated'] = True
                else:
                    confirmation_message = language_manager.get_message(
                        user.get('preferred_language', 'en'), 'language_change_failed'
                    )
                    response_data['data']['response_message'] = confirmation_message
                    response_data['status'] = 'error'
            else:
                # Handle registration flow
                session = db_manager.get_registration_session(user_phone)
                if session:
                    temp_data = session.get('temp_data', {})
                    temp_data['language'] = language_code_selected
                    
                    # Move to name step
                    db_manager.update_registration_session(user_phone, 'name', temp_data)
                    confirmation_message = language_manager.get_message(language_code_selected, 'ask_name')
                    response_data['data']['response_message'] = confirmation_message
                    response_data['data']['registration_step'] = 'name'
                else:
                    confirmation_message = language_manager.get_message('en', 'no_registration_session')
                    response_data['data']['response_message'] = confirmation_message
                    response_data['status'] = 'error'
                    
        elif not user:
            # Handle registration flow for name input
            session = db_manager.get_registration_session(user_phone)
            
            if session and session.get('current_step') == 'name':
                if len(message_text.strip()) < 2:
                    temp_data = session.get('temp_data', {})
                    selected_language = temp_data.get('language', 'en')
                    invalid_name_message = language_manager.get_message(selected_language, 'invalid_name')
                    response_data['data']['response_message'] = invalid_name_message
                    response_data['status'] = 'error'
                else:
                    # Complete registration
                    temp_data = session.get('temp_data', {})
                    temp_data['name'] = message_text.strip().title()
                    selected_language = temp_data.get('language', 'en')
                    
                    # Create user
                    success = db_manager.create_user(
                        user_phone,
                        temp_data['name'],
                        selected_language
                    )
                    
                    if success:
                        completion_message = (language_manager.get_message(selected_language, 'registration_complete') + 
                                            "\n\n" + language_manager.get_message(selected_language, 'welcome'))
                        response_data['data']['response_message'] = completion_message
                        response_data['data']['registration_completed'] = True
                    else:
                        failed_message = language_manager.get_message(selected_language, 'registration_failed')
                        response_data['data']['response_message'] = failed_message
                        response_data['status'] = 'error'
            else:
                # Start registration
                welcome_message = (language_manager.get_message('en', 'language_selection') + 
                                 "\n\n" + language_manager.get_language_options_text())
                response_data['data']['response_message'] = welcome_message
                response_data['data']['registration_needed'] = True
                db_manager.update_registration_session(user_phone, 'language', {})
        else:
            # User exists but sent unrecognized text
            user_language = user.get('preferred_language', language_code)
            unknown_message = language_manager.get_message(user_language, 'unknown_command')
            response_data['data']['response_message'] = unknown_message
        
        return jsonify(response_data), 200
        
    except Exception as e:
        logger.error(f"BSP text message error: {e}")
        return jsonify({
            'status': 'error',
            'message': f'Text message processing failed: {str(e)}'
        }), 500

@app.route('/bsp/user/<phone_number>', methods=['GET'])
def bsp_get_user(phone_number):
    """BSP endpoint to get user information"""
    try:
        user = db_manager.get_user_by_phone(phone_number)
        
        if not user:
            return jsonify({
                'status': 'error',
                'message': 'User not found'
            }), 404
        
        # Get user's analysis history
        try:
            conn = db_manager.get_connection()
            cursor = conn.cursor(cursor_factory=RealDictCursor)
            
            cursor.execute("""
                SELECT analysis_id, file_location, analysis_result, created_at
                FROM nutrition_analysis 
                WHERE user_id = %s 
                ORDER BY created_at DESC 
                LIMIT 10
            """, (user['user_id'],))
            
            analyses = cursor.fetchall()
            cursor.close()
            conn.close()
            
            return jsonify({
                'status': 'success',
                'data': {
                    'user': dict(user),
                    'recent_analyses': [dict(analysis) for analysis in analyses]
                }
            }), 200
            
        except Exception as e:
            logger.error(f"Error fetching user analyses: {e}")
            return jsonify({
                'status': 'success',
                'data': {
                    'user': dict(user),
                    'recent_analyses': []
                }
            }), 200
        
    except Exception as e:
        logger.error(f"BSP get user error: {e}")
        return jsonify({
            'status': 'error',
            'message': f'Failed to get user: {str(e)}'
        }), 500

@app.route('/bsp/user/<phone_number>/language', methods=['PUT'])
def bsp_update_user_language(phone_number):
    """BSP endpoint to update user language"""
    try:
        # Handle different content types
        data = {}
        
        if request.is_json:
            # JSON request
            data = request.get_json() or {}
        elif request.form:
            # Form data request
            data = {
                'language': request.form.get('language')
            }
        else:
            # Try to parse as JSON anyway (fallback)
            try:
                data = request.get_json(force=True) or {}
            except:
                data = {}
        
        # Clean up data - remove None values and empty strings
        data = {k: v for k, v in data.items() if v is not None and v != ''}
        
        # Validate phone number
        if not phone_number or not phone_number.strip():
            return jsonify({
                'status': 'error',
                'message': 'Valid phone number required'
            }), 400
        
        # Validate language data
        if not data or 'language' not in data:
            return jsonify({
                'status': 'error',
                'message': 'Language code required'
            }), 400
        
        language_code = data['language'].strip()
        
        if not language_code:
            return jsonify({
                'status': 'error',
                'message': 'Language code cannot be empty'
            }), 400
        
        # Validate language code
        if language_code not in language_manager.languages:
            return jsonify({
                'status': 'error',
                'message': f'Invalid language code. Supported: {list(language_manager.languages.keys())}'
            }), 400
        
        # Check if user exists first
        try:
            user = db_manager.get_user_by_phone(phone_number)
            if not user:
                return jsonify({
                    'status': 'error',
                    'message': 'User not found'
                }), 404
        except Exception as e:
            logger.error(f"Error checking user existence: {e}")
            return jsonify({
                'status': 'error',
                'message': 'Failed to verify user'
            }), 500
        
        # Update user language
        try:
            success = db_manager.update_user_language(phone_number, language_code)
        except Exception as e:
            logger.error(f"Database update error: {e}")
            return jsonify({
                'status': 'error',
                'message': 'Failed to update language in database'
            }), 500
        
        if success:
            try:
                language_name = language_manager.get_language_name(language_code)
            except Exception as e:
                logger.error(f"Error getting language name: {e}")
                language_name = language_code  # Fallback to code
            
            return jsonify({
                'status': 'success',
                'message': f'Language updated to {language_name}',
                'data': {
                    'phone_number': phone_number,
                    'language_code': language_code,
                    'language_name': language_name,
                    'user_id': user.get('user_id') if user else None
                }
            }), 200
        else:
            return jsonify({
                'status': 'error',
                'message': 'Failed to update language'
            }), 500
        
    except Exception as e:
        logger.error(f"BSP update language error: {e}")
        return jsonify({
            'status': 'error',
            'message': f'Failed to update language: {str(e)}'
        }), 500

@app.route('/bsp/languages', methods=['GET'])
def bsp_get_supported_languages():
    """BSP endpoint to get supported languages"""
    try:
        return jsonify({
            'status': 'success',
            'data': {
                'supported_languages': language_manager.languages,
                'language_options_text': language_manager.get_language_options_text()
            }
        }), 200
        
    except Exception as e:
        logger.error(f"BSP get languages error: {e}")
        return jsonify({
            'status': 'error',
            'message': f'Failed to get languages: {str(e)}'
        }), 500

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

@app.route('/admin/messages', methods=['POST'])
def update_messages():
    """Admin endpoint to update language messages"""
    try:
        data = request.get_json()
        
        if not data:
            return jsonify({'error': 'No data provided'}), 400
        
        success = db_manager.insert_language_messages(data)
        
        if success:
            # Reinitialize language manager to pick up new messages
            global language_manager
            language_manager = LanguageManager(db_manager)
            
            return jsonify({'status': 'success', 'message': 'Messages updated successfully'}), 200
        else:
            return jsonify({'error': 'Failed to update messages'}), 500
            
    except Exception as e:
        logger.error(f"Error updating messages: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/admin/messages', methods=['GET'])
def get_messages():
    """Admin endpoint to get all language messages"""
    try:
        messages = db_manager.get_all_language_messages()
        return jsonify(messages), 200
        
    except Exception as e:
        logger.error(f"Error getting messages: {e}")
        return jsonify({'error': str(e)}), 500

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
