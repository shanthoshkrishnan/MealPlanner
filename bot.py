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
            
            # Create nutrition_analysis table 
            cursor.execute("DROP TABLE IF EXISTS nutrition_analysis;")
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS nutrition_analysis (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                    file_location TEXT NOT NULL,
                    analysis_result TEXT,
                    nutrient_details JSONB,
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
        
        self.messages = self.load_messages()
    
    def load_messages(self) -> dict:
        """Load messages from messages.json file"""
        try:
            # Try to load from the same directory as the script
            messages_path = os.path.join(os.path.dirname(__file__), 'messages.json')
            if not os.path.exists(messages_path):
                # Try current directory
                messages_path = 'messages.json'
            
            with open(messages_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error loading messages.json: {e}")
            # Fallback to basic English messages
            return {
                'en': {
                    'welcome': "👋 Hello! I'm your AI Nutrition Analyzer bot! Send me a photo of any food for detailed nutritional analysis.",
                    'language_selection': "Please select your preferred language for nutrition analysis.",
                    'ask_name': "Please enter your full name:",
                    'registration_complete': "✅ Registration completed successfully! You can now send me food photos for nutrition analysis.",
                    'analyzing': "🔍 Analyzing your food image... This may take a few moments.",
                    'help': "Send me a food photo to get detailed nutrition analysis. Type 'language' to change your language preference.",
                    'language_changed': "✅ Language updated successfully!",
                    'language_change_failed': "❌ Failed to update language. Please try again.",
                    'invalid_language': "❌ Invalid language selection. Please select from the available options.",
                    'unsupported_message': "🤖 I can only process text messages and food images. Please send me a food photo for nutrition analysis!",
                    'registration_failed': "❌ Registration failed. Please try again by typing 'start'.",
                    'invalid_name': "📝 Please enter a valid name (at least 2 characters):",
                    'image_processing_error': "❌ Sorry, I couldn't analyze your image. Please try again with a clearer photo of your food.",
                    'followup_message': "📸 Send me another food photo for more analysis! Type 'help' for assistance.",
                    'no_registration_session': "❌ No registration session found. Please type 'start' to begin.",
                    'user_incomplete': "❌ User registration incomplete. Please type 'start' to re-register.",
                    'unknown_command': "❓ I didn't understand that command. Type 'help' for assistance or send me a food photo for analysis."
                }
            }
    
    def get_message(self, language: str, key: str) -> str:
        """Get message in specified language"""
        return self.messages.get(language, self.messages.get('en', {})).get(key, self.messages.get('en', {}).get(key, f"Message not found: {key}"))
    
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
        
        # Universal prompt structure that works in any language
        base_prompt = f"""
        {language_instruction}

        Analyze this food image and provide TWO responses:

        1. FIRST, provide a detailed user-friendly response in the requested language with this structure (NO asterisks or markdown formatting):

        🍽️ DISH IDENTIFICATION
        - Name and description of the dish
        - Type of cuisine
        - Confidence level in identification

        📏 SERVING SIZE
        - Estimated serving size and weight

        🔥 NUTRITION FACTS (per serving)
        - Calories
        - Protein, Carbohydrates, Fat, Fiber, Sugar (in grams)
        - Key vitamins and minerals

        💪 HEALTH ANALYSIS
        - Overall health score (1-10)
        - Nutritional strengths
        - Areas of concern

        💡 IMPROVEMENT SUGGESTIONS
        - Ways to make it healthier
        - Foods to add or reduce
        - Better cooking methods

        🚨 DIETARY INFORMATION
        - Potential allergens
        - Suitable for: Vegetarian/Vegan/Gluten-free/etc.

        2. THEN, provide the structured data in JSON format:

        JSON_DATA_START
        {{
            "dish_name": "name of the dish",
            "cuisine_type": "type of cuisine",
            "confidence_level": "high/medium/low",
            "serving_size": {{
                "weight_grams": 0,
                "description": "serving description"
            }},
            "nutrition_per_serving": {{
                "calories": 0,
                "protein_g": 0,
                "carbohydrates_g": 0,
                "fat_g": 0,
                "fiber_g": 0,
                "sugar_g": 0,
                "sodium_mg": 0,
                "vitamins": ["list of key vitamins"],
                "minerals": ["list of key minerals"]
            }},
            "health_analysis": {{
                "health_score": 0,
                "strengths": ["list of nutritional strengths"],
                "concerns": ["list of areas of concern"]
            }},
            "dietary_info": {{
                "allergens": ["list of potential allergens"],
                "dietary_tags": ["vegetarian", "vegan", "gluten-free", etc.]
            }},
            "improvement_suggestions": ["list of suggestions"]
        }}
        JSON_DATA_END

        IMPORTANT: 
        - Respond completely in the requested language for the user-friendly part
        - JSON should have English keys but values in requested language
        - Do NOT use asterisks, markdown formatting, or bold text in the user response
        - Keep the emoji structure but make text clean and readable
        - If you cannot identify the food clearly, indicate this in both parts
        """
        
        try:
            response = self.model.generate_content([base_prompt, image])
            full_response = response.text.strip()
            
            # Extract user-friendly response and JSON data
            user_response, nutrient_data = self._extract_response_and_json(full_response)
            
            return user_response, nutrient_data

            
        except Exception as e:
            logger.error(f"Gemini analysis error: {e}")
            
            # Return error message in user's preferred language
            error_messages = {
                'en': "❌ Sorry, I couldn't analyze this image. Please try again with a clearer photo of your food.",
                'ta': "❌ மன்னிக்கவும், இந்த படத்தை பகுப்பாய்வு செய்ய முடியவில்லை. உங்கள் உணவின் தெளிவான புகைப்படத்துடன் மீண்டும் முயற்சிக்கவும்.",
                'te': "❌ క్షమించండి, ఈ చిత్రాన్ని విశ్లేషించలేకపోయాను. దయచేసి మీ ఆహారం యొక్క స్పష్టమైన ఫోటోతో మళ్లీ ప్రయత్నించండి.",
                'hi': "❌ खुशी है, मैं इस छवि का विश्लेषण नहीं कर सका। कृपया अपने भोजन की स्पष्ट तस्वीर के साथ पुनः प्रयास करें।",
                'kn': "❌ ಕ್ಷಮಿಸಿ, ನಾನು ಈ ಚಿತ್ರವನ್ನು ವಿಶ್ಲೇಷಿಸಲು ಸಾಧ್ಯವಾಗಲಿಲ್ಲ. ದಯವಿಟ್ಟು ನಿಮ್ಮ ಆಹಾರದ ಸ್ಪಷ್ಟ ಫೋಟೋದೊಂದಿಗೆ ಮತ್ತೆ ಪ್ರಯತ್ನಿಸಿ.",
                'ml': "❌ ക്ഷമിക്കണം, ഈ ചിത്രം വിശകലനം ചെയ്യാൻ എനിക്ക് കഴിഞ്ഞില്ല. ദയവായി നിങ്ങളുടെ ഭക്ഷണത്തിന്റെ വ്യക്തമായ ഫോട്ടോ ഉപയോഗിച്ച് വീണ്ടും ശ്രമിക്കുക.",
                'mr': "❌ माफ करा, मी या प्रतिमेचे विश्लेषण करू शकलो नाही. कृपया आपल्या अन्नाच्या स्पष्ट फोटोसह पुन्हा प्रयत्न करा.",
                'gu': "❌ માફ કરશો, હું આ છબીનું વિશ્લેષણ કરી શક્યો નથી. કૃપા કરીને તમારા ખોરાકના સ્પષ્ટ ફોટો સાથે ફરીથી પ્રયાસ કરો.",
                'bn': "❌ দুঃখিত, আমি এই ছবিটি বিশ্লেষণ করতে পারিনি। দয়া করে আপনার খাবারের স্পষ্ট ফটো দিয়ে আবার চেষ্টা করুন।"
            }
            
            error_msg = error_messages.get(language, error_messages['en'])
     

    def _extract_response_and_json(self, full_response: str) -> tuple[str, dict]:
        """Extract user response and JSON data from the full response"""
        try:
            # Remove asterisks and clean up formatting
            cleaned_response = full_response.replace('*', '').replace('**', '')
            
            # Find JSON data between markers
            json_start = cleaned_response.find('JSON_DATA_START')
            json_end = cleaned_response.find('JSON_DATA_END')
            
            if json_start != -1 and json_end != -1:
                # Extract user response (everything before JSON)
                user_response = cleaned_response[:json_start].strip()
                
                # Extract and parse JSON
                json_str = cleaned_response[json_start + len('JSON_DATA_START'):json_end].strip()
                
                try:
                    nutrient_data = json.loads(json_str)
                except json.JSONDecodeError as e:
                    logger.warning(f"Failed to parse JSON: {e}")
                    nutrient_data = {}
                
                return user_response, nutrient_data
            else:
                # No JSON markers found, return cleaned response and empty dict
                return cleaned_response, {}
                
        except Exception as e:
            logger.error(f"Error extracting response and JSON: {e}")
            return full_response.replace('*', ''), {}   

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
    """Handle incoming image messages"""
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
            
            # Analyze image - now returns both user response and structured data
            analysis_result, nutrient_details = analyzer.analyze_image(image, user_language)

            # Save analysis with nutrient details
            success = db_manager.save_nutrition_analysis(
                user['user_id'], 
                file_location, 
                analysis_result,
                nutrient_details
            )
            
            if not success:
                logger.error(f"Failed to save nutrition analysis for user {user['user_id']}")
            
            # Send analysis result to user
            whatsapp_bot.send_message(sender, analysis_result)
            
            # Send follow-up message
            followup_message = language_manager.get_message(user_language, 'followup_message')
            whatsapp_bot.send_message(sender, followup_message)
            
        except Exception as e:
            logger.error(f"Error processing image: {e}")
            error_message = language_manager.get_message(user_language, 'image_processing_error')
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

