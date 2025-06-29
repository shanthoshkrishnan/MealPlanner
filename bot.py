from flask import Flask, request, jsonify
import urllib
import google.generativeai as genai
import os
import requests
from PIL import Image
import io
import json
import logging
from typing import Dict, Any, Optional
import uuid
import psycopg2
from psycopg2.extras import RealDictCursor
import boto3
from botocore.exceptions import ClientError
from datetime import datetime
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

# 11za Configuration
AUTH_TOKEN = os.environ.get("ELEVENZA_AUTH_TOKEN") or os.environ.get("AUTH_TOKEN")
ORIGIN_WEBSITE = os.environ.get("ELEVENZA_ORIGIN_WEBSITE") or os.environ.get("ORIGIN_WEBSITE")
SEND_MESSAGE_URL = os.environ.get("ELEVENZA_SEND_MESSAGE_URL", "https://api.11za.in/apis/sendMessage/sendMessages")

# Validation
required_env_vars = [
    'GEMINI_API_KEY', 'WHATSAPP_TOKEN', 'WHATSAPP_PHONE_NUMBER_ID', 
    'WEBHOOK_VERIFY_TOKEN', 'DATABASE_URL', 'AWS_ACCESS_KEY_ID', 
    'AWS_SECRET_ACCESS_KEY', 'AWS_S3_BUCKET','AUTH_TOKEN','ORIGIN_WEBSITE'
]

missing_vars = [var for var in required_env_vars if not os.getenv(var)]
if missing_vars:
    logger.error(f"Missing required environment variables: {missing_vars}")
    raise ValueError(f"Missing required environment variables: {missing_vars}")

# Validate 11za required environment variables
if not AUTH_TOKEN:
    raise ValueError("ELEVENZA_AUTH_TOKEN or AUTH_TOKEN environment variable is required")
if not ORIGIN_WEBSITE:
    raise ValueError("ELEVENZA_ORIGIN_WEBSITE or ORIGIN_WEBSITE environment variable is required")

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

def safe_json_serialize(obj):
    """Safely serialize objects for logging"""
    try:
        return json.dumps(obj, default=str)
    except (TypeError, ValueError) as e:
        return f"<Non-serializable object: {type(obj).__name__}>"
    
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
        
            self._execute_sql_safely([
            "DROP TABLE IF EXISTS nutrition_analysis CASCADE;",
            """
            CREATE TABLE nutrition_analysis (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                file_location TEXT NOT NULL,
                analysis_result TEXT,
                language TEXT NOT NULL DEFAULT 'en',  -- Changed from VARCHAR(100) to TEXT
                
                -- Dish identification
                dish_name VARCHAR(20000),
                cuisine_type VARCHAR(20000),
                confidence_level VARCHAR(200),
                dish_description TEXT,
                
                -- Serving information
                estimated_weight_grams INTEGER,
                serving_description VARCHAR(20000),
                
                -- Nutrition facts (per serving)
                calories INTEGER,
                protein_g DECIMAL(8,2),
                carbohydrates_g DECIMAL(8,2),
                fat_g DECIMAL(8,2),
                fiber_g DECIMAL(8,2),
                sugar_g DECIMAL(8,2),
                sodium_mg DECIMAL(8,2),
                saturated_fat_g DECIMAL(8,2),
                
                -- Vitamins and minerals (stored as arrays)
                key_vitamins TEXT[],
                key_minerals TEXT[],
                
                -- Health analysis
                health_score INTEGER,
                health_grade VARCHAR(5),
                nutritional_strengths TEXT[],
                areas_of_concern TEXT[],
                overall_assessment TEXT,
                
                -- Dietary information
                potential_allergens TEXT[],
                is_vegetarian BOOLEAN,
                is_vegan BOOLEAN,
                is_gluten_free BOOLEAN,
                is_dairy_free BOOLEAN,
                is_keto_friendly BOOLEAN,
                is_low_sodium BOOLEAN,
                
                -- Improvement suggestions
                healthier_alternatives TEXT[],
                portion_recommendations TEXT,
                cooking_modifications TEXT[],
                nutritional_additions TEXT[],
                
                -- Additional details
                ingredients_identified TEXT[],
                cooking_method VARCHAR(2000),
                meal_category VARCHAR(2000),
                
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
                "CREATE INDEX IF NOT EXISTS idx_nutrition_calories ON nutrition_analysis(calories);",
                "CREATE INDEX IF NOT EXISTS idx_nutrition_health_score ON nutrition_analysis(health_score);",
                "CREATE INDEX IF NOT EXISTS idx_nutrition_meal_category ON nutrition_analysis(meal_category);",
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
            user_id = None
            if result:
                user_id = result[0]
                logger.info(f"User created/updated with user_id: {user_id}")
            
            conn.commit()
            cursor.close()
            conn.close()
            
            # Clean up registration session
            self.delete_registration_session(phone_number)
            
            return user_id
            
        except Exception as e:
            logger.error(f"Error creating user: {e}")
            return None
    
    def get_or_create_user(self, phone_number: str, name: str = None, language: str = 'en') -> Optional[int]:
        """Get existing user or create new user, return user_id"""
        try:
            # First try to get existing user
            existing_user = self.get_user_by_phone(phone_number)
            if existing_user:
                logger.info(f"Found existing user with user_id: {existing_user['user_id']}")
                return existing_user['user_id']
        
            # If user doesn't exist and we have name, create new user
            if name:
                user_id = self.create_user(phone_number, name, language)
                if user_id:
                    logger.info(f"Created new user with user_id: {user_id}")
                    return user_id
        
            logger.warning(f"Could not get or create user for phone: {phone_number}")
            return None
        
        except Exception as e:
            logger.error(f"Error in get_or_create_user: {e}")
            return None

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
    def complete_user_registration(self, phone_number: str) -> Optional[int]:
        """Complete user registration from session data and return user_id"""
        try:
            # Get registration session
            session = self.get_registration_session(phone_number)
            if not session:
                logger.error(f"No registration session found for {phone_number}")
                return None
        
            temp_data = session.get('temp_data', {})
            name = temp_data.get('name')
            language = temp_data.get('language', 'en')
        
            if not name:
                logger.error(f"No name found in registration session for {phone_number}")
                return None
        
            # Create the user
            user_id = self.create_user(phone_number, name, language)
            if user_id:
                logger.info(f"Successfully completed registration for {phone_number} with user_id: {user_id}")
                return user_id
            else:
                logger.error(f"Failed to create user during registration completion for {phone_number}")
                return None
        
        except Exception as e:
            logger.error(f"Error completing user registration: {e}")
            return None

    def get_next_user_id(self) -> int:
        """Get the next available user_id (for reference only - PostgreSQL SERIAL handles this automatically)"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
        
            cursor.execute("SELECT COALESCE(MAX(user_id), 0) + 1 FROM users")
            next_id = cursor.fetchone()[0]
        
            cursor.close()
            conn.close()
        
            return next_id
        
        except Exception as e:
            logger.error(f"Error getting next user ID: {e}")
            return 1
    
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
    
    def save_nutrition_analysis(self, user_id: int, file_location: str, analysis_result: str, language: str = 'en', nutrition_data: dict = None) -> bool:
        """Save nutrition analysis to database with proper data extraction - ROBUST VERSION"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()

            # Helper function to safely truncate strings
            def safe_truncate(value, max_length, field_name="unknown"):
                if value is None:
                    return None
                str_value = str(value)
                if len(str_value) > max_length:
                    logger.warning(f"Truncating {field_name}: {len(str_value)} chars to {max_length}")
                    return str_value[:max_length]
                return str_value

            # Helper function to safely get numeric values
            def safe_numeric(value, default=None):
                if value is None:
                    return default
                try:
                    if isinstance(value, (int, float)):
                        return value
                    return float(value) if '.' in str(value) else int(value)
                except (ValueError, TypeError):
                    return default

            # Helper function to safely get boolean values
            def safe_boolean(value, default=None):
                if value is None:
                    return default
                if isinstance(value, bool):
                    return value
                if isinstance(value, str):
                    return value.lower() in ('true', '1', 'yes', 'on')
                return bool(value)

            # Helper function to safely get array values
            def safe_array(value, default=None):
                if value is None:
                    return default or []
                if isinstance(value, list):
                    return [str(item) for item in value if item is not None]
                return [str(value)] if value else []

            logger.debug(f"Starting nutrition analysis save for user_id: {user_id}")
            logger.debug(f"Language: '{language}' (length: {len(language)})")
            logger.debug(f"Analysis result length: {len(analysis_result)}")

            # Initialize default values
            default_values = {
                'user_id': user_id,
                'file_location': safe_truncate(file_location, 500, 'file_location'),
                'analysis_result': analysis_result,
                'language': language,
                'dish_name': None,
                'cuisine_type': None,
                'confidence_level': None,
                'dish_description': None,
                'estimated_weight_grams': None,
                'serving_description': None,
                'calories': None,
                'protein_g': None,
                'carbohydrates_g': None,
                'fat_g': None,
                'fiber_g': None,
                'sugar_g': None,
                'sodium_mg': None,
                'saturated_fat_g': None,
                'key_vitamins': [],
                'key_minerals': [],
                'health_score': None,
                'health_grade': None,
                'nutritional_strengths': [],
                'areas_of_concern': [],
                'overall_assessment': None,
                'potential_allergens': [],
                'is_vegetarian': None,
                'is_vegan': None,
                'is_gluten_free': None,
                'is_dairy_free': None,
                'is_keto_friendly': None,
                'is_low_sodium': None,
                'healthier_alternatives': [],
                'portion_recommendations': None,
                'cooking_modifications': [],
                'nutritional_additions': [],
                'ingredients_identified': [],
                'cooking_method': None,
                'meal_category': None
            }
 
            print("🔍 Raw nutrition_data:", json.dumps(nutrition_data, indent=2, ensure_ascii=False) if nutrition_data else "None")

            # Extract data from nutrient_details if provided
            if nutrition_data and isinstance(nutrition_data, dict) and nutrition_data.get('is_food', True):
                try:
                    logger.debug("Extracting data from already parsed nutrition_data...")
                    
                    language = nutrition_data.get('language', language)
                    default_values["language"] = safe_truncate(language, 10, 'language')
                    print(f"🗣️ Language set to: {default_values['language']}")

                    # Dish identification
                    dish_info = nutrition_data.get('dish_identification', {})
                    if dish_info:
                        default_values['dish_name'] = safe_truncate(dish_info.get('name'), 20000, 'dish_name')
                        default_values['cuisine_type'] = safe_truncate(dish_info.get('cuisine_type'), 20000, 'cuisine_type')
                        default_values['confidence_level'] = safe_truncate(dish_info.get('confidence_level'), 200, 'confidence_level')
                        default_values['dish_description'] = dish_info.get('description')
                        print(f"🍽️ Dish Name: {default_values['dish_name']}")
                        print(f"🌍 Cuisine: {default_values['cuisine_type']}")
                        print(f"✅ Confidence Level: {default_values['confidence_level']}")

                    # Serving information
                    serving_info = nutrition_data.get('serving_info', {})
                    if serving_info:
                        default_values['estimated_weight_grams'] = safe_numeric(serving_info.get('estimated_weight_grams'))
                        default_values['serving_description'] = safe_truncate(serving_info.get('serving_description'), 20000, 'serving_description')
                        print(f"📏 Weight (g): {default_values['estimated_weight_grams']}")
                        print(f"📦 Serving Description: {default_values['serving_description']}")

                    # Nutrition facts
                    nutrition_facts = nutrition_data.get('nutrition_facts', {})
                    if nutrition_facts:
                        default_values['calories'] = safe_numeric(nutrition_facts.get('calories'))
                        default_values['protein_g'] = safe_numeric(nutrition_facts.get('protein_g'))
                        default_values['carbohydrates_g'] = safe_numeric(nutrition_facts.get('carbohydrates_g'))
                        default_values['fat_g'] = safe_numeric(nutrition_facts.get('fat_g'))
                        default_values['fiber_g'] = safe_numeric(nutrition_facts.get('fiber_g'))
                        default_values['sugar_g'] = safe_numeric(nutrition_facts.get('sugar_g'))
                        default_values['sodium_mg'] = safe_numeric(nutrition_facts.get('sodium_mg'))
                        default_values['saturated_fat_g'] = safe_numeric(nutrition_facts.get('saturated_fat_g'))
                        default_values['key_vitamins'] = safe_array(nutrition_facts.get('key_vitamins'))
                        default_values['key_minerals'] = safe_array(nutrition_facts.get('key_minerals'))
                        print(f"🔥 Calories: {default_values['calories']}")
                        print(f"💪 Protein (g): {default_values['protein_g']}")
                        print(f"🍞 Carbs (g): {default_values['carbohydrates_g']}")
                        print(f"🥑 Fat (g): {default_values['fat_g']}")
                        print(f"🌾 Fiber (g): {default_values['fiber_g']}")
                        print(f"🍬 Sugar (g): {default_values['sugar_g']}")
                        print(f"🧂 Sodium (mg): {default_values['sodium_mg']}")
                        print(f"🧈 Sat. Fat (g): {default_values['saturated_fat_g']}")
                        print(f"🔑 Vitamins: {default_values['key_vitamins']}")
                        print(f"🔩 Minerals: {default_values['key_minerals']}")
                    # Health analysis
                    health_analysis = nutrition_data.get('health_analysis', {})
                    if health_analysis:
                        default_values['health_score'] = safe_numeric(health_analysis.get('health_score'))
                        default_values['health_grade'] = safe_truncate(health_analysis.get('health_grade'), 5, 'health_grade')
                        default_values['nutritional_strengths'] = safe_array(health_analysis.get('nutritional_strengths'))
                        default_values['areas_of_concern'] = safe_array(health_analysis.get('areas_of_concern'))
                        default_values['overall_assessment'] = health_analysis.get('overall_assessment')
                        print(f"❤️ Health Score: {default_values['health_score']}")
                        print(f"🎓 Health Grade: {default_values['health_grade']}")
                        print(f"💚 Strengths: {default_values['nutritional_strengths']}")
                        print(f"⚠️ Concerns: {default_values['areas_of_concern']}")
                        print(f"🧾 Assessment: {default_values['overall_assessment']}")

                    # Dietary information
                    dietary_info = nutrition_data.get('dietary_information', {})
                    if dietary_info:
                        default_values['potential_allergens'] = safe_array(dietary_info.get('potential_allergens'))
                    
                        # Dietary compatibility
                        dietary_compatibility = dietary_info.get('dietary_compatibility', {})
                        if dietary_compatibility:
                            default_values['is_vegetarian'] = safe_boolean(dietary_compatibility.get('vegetarian'))
                            default_values['is_vegan'] = safe_boolean(dietary_compatibility.get('vegan'))
                            default_values['is_gluten_free'] = safe_boolean(dietary_compatibility.get('gluten_free'))
                            default_values['is_dairy_free'] = safe_boolean(dietary_compatibility.get('dairy_free'))
                            default_values['is_keto_friendly'] = safe_boolean(dietary_compatibility.get('keto_friendly'))
                            default_values['is_low_sodium'] = safe_boolean(dietary_compatibility.get('low_sodium'))
                            print(f"🚨 Allergens: {default_values['potential_allergens']}")
                            print(f"🥬 Is Vegetarian? {default_values['is_vegetarian']}")
                            print(f"🌱 Is Vegan? {default_values['is_vegan']}")
                            print(f"🌾 Gluten-Free? {default_values['is_gluten_free']}")
                            print(f"🥛 Dairy-Free? {default_values['is_dairy_free']}")
                            print(f"🥩 Keto-Friendly? {default_values['is_keto_friendly']}")
                            print(f"🧂 Low Sodium? {default_values['is_low_sodium']}")
                    # Improvement suggestions
                    improvements = nutrition_data.get('improvement_suggestions', {})
                    if improvements:
                        default_values['healthier_alternatives'] = safe_array(improvements.get('healthier_alternatives'))
                        default_values['portion_recommendations'] = improvements.get('portion_recommendations')
                        default_values['cooking_modifications'] = safe_array(improvements.get('cooking_modifications'))
                        default_values['nutritional_additions'] = safe_array(improvements.get('nutritional_additions'))
                        print(f"🆗 Healthier Options: {default_values['healthier_alternatives']}")
                        print(f"📏 Portion Advice: {default_values['portion_recommendations']}")
                        print(f"👨‍🍳 Cooking Tips: {default_values['cooking_modifications']}")
                        print(f"🧪 Nutritional Additions: {default_values['nutritional_additions']}")

                    # Detailed breakdown
                    detailed_breakdown = nutrition_data.get('detailed_breakdown', {})
                    if detailed_breakdown:
                        default_values['ingredients_identified'] = safe_array(detailed_breakdown.get('ingredients_identified'))
                        default_values['cooking_method'] = safe_truncate(detailed_breakdown.get('cooking_method'), 2000, 'cooking_method')
                        default_values['meal_category'] = safe_truncate(detailed_breakdown.get('meal_category'), 2000, 'meal_category')
                        print(f"🧂 Ingredients Identified: {default_values['ingredients_identified']}")
                        print(f"🔥 Cooking Method: {default_values['cooking_method']}")
                        print(f"🍽️ Meal Category: {default_values['meal_category']}")

                    logger.debug("Data extraction from parsed JSON completed successfully")

                except Exception as e:
                    logger.error(f"Error extracting nutrient details: {e}")
                    logger.debug("Continuing with default values...")

            # Log some key values for debugging
            logger.debug(f"Final values - dish_name: {default_values['dish_name']}")
            logger.debug(f"Final values - calories: {default_values['calories']}")
            logger.debug(f"Final values - health_score: {default_values['health_score']}")

            # Execute the insert query
            sql = """
            INSERT INTO nutrition_analysis (
                user_id, file_location, analysis_result, language,
                dish_name, cuisine_type, confidence_level, dish_description,
                estimated_weight_grams, serving_description,
                calories, protein_g, carbohydrates_g, fat_g, fiber_g, sugar_g,
                sodium_mg, saturated_fat_g, key_vitamins, key_minerals,
                health_score, health_grade, nutritional_strengths, areas_of_concern, overall_assessment,
                potential_allergens, is_vegetarian, is_vegan, is_gluten_free, is_dairy_free,
                is_keto_friendly, is_low_sodium,
                healthier_alternatives, portion_recommendations, cooking_modifications, nutritional_additions,
                ingredients_identified, cooking_method, meal_category
            )
            VALUES (
                %(user_id)s, %(file_location)s, %(analysis_result)s, %(language)s,
                %(dish_name)s, %(cuisine_type)s, %(confidence_level)s, %(dish_description)s,
                %(estimated_weight_grams)s, %(serving_description)s,
                %(calories)s, %(protein_g)s, %(carbohydrates_g)s, %(fat_g)s, %(fiber_g)s, %(sugar_g)s,
                %(sodium_mg)s, %(saturated_fat_g)s, %(key_vitamins)s, %(key_minerals)s,
                %(health_score)s, %(health_grade)s, %(nutritional_strengths)s, %(areas_of_concern)s, %(overall_assessment)s,
                %(potential_allergens)s, %(is_vegetarian)s, %(is_vegan)s, %(is_gluten_free)s, %(is_dairy_free)s,
                %(is_keto_friendly)s, %(is_low_sodium)s,
                %(healthier_alternatives)s, %(portion_recommendations)s, %(cooking_modifications)s, %(nutritional_additions)s,
                %(ingredients_identified)s, %(cooking_method)s, %(meal_category)s
            )
            """
            print("✅ Final values ready for insert:")
            for k, v in default_values.items():
                print(f"  - {k}: {v}")

            cursor.execute(sql, default_values)


            conn.commit()
            cursor.close()
            conn.close()
        
            logger.info(f"Successfully saved nutrition analysis for user {user_id} with all nutrient details")
            return True

        except Exception as e:
            logger.error(f"Error saving nutrition analysis: {e}")
            logger.error(f"Error details - user_id: {user_id}, language: {language}")
            logger.exception("Full traceback:")
            if 'conn' in locals() and conn:
                conn.rollback()
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
        """Get user's nutrition analysis history with all nutrient details"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor(cursor_factory=RealDictCursor)
    
            cursor.execute("""
                SELECT 
                    id, file_location, analysis_result, language, created_at,
                    dish_name, cuisine_type, confidence_level, dish_description,
                    estimated_weight_grams, serving_description,
                    calories, protein_g, carbohydrates_g, fat_g, fiber_g, sugar_g, 
                    sodium_mg, saturated_fat_g, key_vitamins, key_minerals,
                    health_score, health_grade, nutritional_strengths, areas_of_concern, overall_assessment,
                    potential_allergens, is_vegetarian, is_vegan, is_gluten_free, is_dairy_free, 
                    is_keto_friendly, is_low_sodium,
                    healthier_alternatives, portion_recommendations, cooking_modifications, nutritional_additions,
                    ingredients_identified, cooking_method, meal_category
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
                    },
                    "ta": {
                        "welcome": "👋 வணக்கம்! நான் உங்கள் AI ஊட்டச்சத்து பகுப்பாய்வு பாட்!\n\n📸 எந்த உணவின் புகைப்படத்தையும் அனுப்புங்கள், நான் வழங்குவேன்:\n• விரிவான ஊட்டச்சத்து தகவல்\n• கலோரி எண்ணிக்கை மற்றும் மேக்ரோக்கள்\n• ஆரோக்கிய பகுப்பாய்வு மற்றும் குறிப்புகள்\n• மேம்படுத்தும் பரிந்துரைகள்\n\nஉங்கள் உணவின் தெளிவான புகைப்படத்தை எடுத்து அனுப்புங்கள! 🍽️",
                        "language_selection": "🌍 வணக்கம்! முதலில் உங்கள் விருப்பமான மொழியைத் தேர்ந்தெடுக்கவும்:\n\n• **English**\n• **Tamil** (தமிழ்)\n• **Telugu** (తెలుగు)\n• **Hindi** (हिन्दी)\n• **Kannada** (ಕನ್ನಡ)\n• **Malayalam** (മലയാളം)\n• **Marathi** (मराठी)\n• **Gujarati** (ગુજરાતી)\n• **Bengali** (বাংলা)\n\n💬 முழு மொழி பெயரைக் கொண்டு பதிலளியுங்கள் (எ.கா., 'Tamil', 'English', 'Hindi')",
                        "ask_name": "சிறப்பு! உங்கள் முழுப் பெயரை உள்ளிடவும்:",
                        "registration_complete": "✅ பதிவு வெற்றிகரமாக முடிந்தது! இப்போது நீங்கள் ஊட்டச்சத்து பகுப்பாய்விற்காக உணவு புகைப்படங்களை அனுப்பலாம்.",
                        "analyzing": "🔍 உங்கள் உணவு படத்தை பகுப்பாய்வு செய்கிறேன்... இதற்கு சில நிமிடங்கள் ஆகலாம்.",
                        "help": "🆘 **இந்த பாட்டை எப்படி பயன்படுத்துவது:**\n\n1. உங்கள் உணவின் தெளிவான புகைப்படத்தை எடுங்கள்\n2. படத்தை எனக்கு அனுப்புங்கள்\n3. பகுப்பாய்விற்காக காத்திருங்கள்\n4. விரிவான ஊட்டச்சத்து தகவலைப் பெறுங்கள்!\n\n**கிடைக்கும் கட்டளைகள்:**\n• 'help' என்று தட்டச்சு செய்யவும் - இந்த உதவி செய்தியைக் காட்டு\n• 'language' என்று தட்டச்சு செய்யவும் - உங்கள் விருப்பமான மொழியை மாற்றவும்\n• 'start' என்று தட்டச்சு செய்யவும் - பாட்டை மறுதொடக்கம் செய்யவும்\n\nதொடங்க எனக்கு உணவு புகைப்படம் ஒன்றை அனுப்புங்கள்! 📸",
                        "language_changed": "✅ மொழி வெற்றிகரமாக புதுப்பிக்கப்பட்டது! இப்போது நீங்கள் ஊட்டச்சத்து பகுப்பாய்விற்காக உணவு புகைப்படங்களை அனுப்பலாம்.",
                        "language_change_failed": "❌ மொழியை புதுப்பிக்க முடியவில்லை. மீண்டும் முயற்சிக்கவும்.",
                        "invalid_language": "❌ தவறான மொழி தேர்வு. கிடைக்கும் விருப்பங்களில் இருந்து தேர்ந்தெடுக்கவும்.",
                        "unsupported_message": "🤖 என்னால் செயல்படுத்த முடியும்:\n📝 உரை செய்திகள் (கட்டளைகள்)\n📸 உணவு படங்கள்\n\nஊட்டச்சத்து பகுப்பாய்விற்காக *உணவு புகைப்படம்* அனுப்பவும் அல்லது உதவிக்கு 'help' என்று தட்டச்சு செய்யவும்.",
                        "registration_failed": "❌ பதிவு தோல்வியடைந்தது. 'start' என்று தட்டச்சு செய்து மீண்டும் முயற்சிக்கவும்.",
                        "invalid_name": "📝 சரியான பெயரை உள்ளிடவும் (குறைந்தது 2 எழுத்துகள்):",
                        "image_processing_error": "❌ மன்னிக்கவும், உங்கள் படத்தை பகுப்பாய்வு செய்ய முடியவில்லை. இதற்கான காரணங்கள்:\n\n• படம் போதுமான அளவு தெளிவாக இல்லை\n• படத்தில் உணவு தெரியவில்லை\n• தொழில்நுட்ப செயலாக்க பிழை\n\nஉங்கள் உணவின் தெளிவான புகைப்படத்துடன் மீண்டும் முயற்சிக்கவும்! 📸",
                        "followup_message": "\n📸 மேலும் பகுப்பாய்விற்காக எனக்கு மற்றொரு உணவு புகைப்படத்தை அனுப்புங்கள்!\n💬 உதவிக்கு 'help' அல்லது மொழி மாற்ற 'language' என்று தட்டச்சு செய்யவும்.",
                        "no_registration_session": "❌ பதிவு அமர்வு கிடைக்கவில்லை. தொடங்க 'start' என்று தட்டச்சு செய்யவும்.",
                        "user_incomplete": "❌ பயனர் பதிவு முழுமையடையவில்லை. மீண்டும் பதிவு செய்ய 'start' என்று தட்டச்சு செய்யவும்.",
                        "unknown_command": "❌ அந்த கட்டளையை என்னால் புரிந்து கொள்ள முடியவில்லை. கிடைக்கும் கட்டளைகளைப் பார்க்க 'help' என்று தட்டச்சு செய்யவும் அல்லது பகுப்பாய்விற்காக உணவு புகைப்படம் அனுப்பவும்.",
                    },
                    "te": {
                        "welcome": "👋 నమస్కారం! నేను మీ AI పోషక విశ్లేషణ బాట్!\n\n📸 ఏదైనా ఆహారం యొక్క ఫోటోను పంపండి, నేను అందిస్తాను:\n• వివరణాత్మక పోషక సమాచారం\n• కేలరీ లెక్కింపు మరియు మాక్రోలు\n• ఆరోగ్య విశ్లేషణ మరియు చిట్కాలు\n• మెరుగుదల సూచనలు\n\nమీ భోజనం యొక్క స్పష్టమైన ఫోటో తీసి నాకు పంపండి! 🍽️",
                        "language_selection": "🌍 స్వాగతం! దయచేసి ముందుగా మీ ఇష్టపడే భాషను ఎంచుకోండి:\n\n• **English**\n• **Tamil** (தமிழ்)\n• **Telugu** (తెలుగు)\n• **Hindi** (हिन्दी)\n• **Kannada** (ಕನ್ನಡ)\n• **Malayalam** (മലയാളം)\n• **Marathi** (मराठी)\n• **Gujarati** (ગુજરાતી)\n• **Bengali** (বাংলা)\n\n💬 పూర్తి భాష పేరుతో ప్రత్యుత్తరం ఇవ్వండి (ఉదా., 'Telugu', 'English', 'Hindi')",
                        "ask_name": "గొప్పది! దయచేసి మీ పూర్తి పేరును నమోదు చేయండి:",
                        "registration_complete": "✅ నమోదీకరణ విజయవంతంగా పూర్తయింది! ఇప్పుడు మీరు పోషక విశ్లేషణ కోసం ఆహార ఫోటోలను పంపవచ్చు.",
                        "analyzing": "🔍 మీ ఆహార చిత్రాన్ని విశ్లేషిస్తున్నాను... దీనికి కొన్ని క్షణాలు పట్టవచ్చు.",
                        "help": "🆘 **ఈ బాట్‌ను ఎలా ఉపయోగించాలి:**\n\n1. మీ ఆహారం యొక్క స్పష్టమైన ఫోటో తీసుకోండి\n2. చిత్రాన్ని నాకు పంపండి\n3. విశ్లేషణ కోసం వేచి ఉండండి\n4. వివరణాత్మక పోషక సమాచారాన్ని పొందండి!\n\n**అందుబాటులో ఉన్న కమాండ్‌లు:**\n• 'help' అని టైప్ చేయండి - ఈ సహాయ సందేశాన్ని చూపించు\n• 'language' అని టైప్ చేయండి - మీ ఇష్టపడే భాషను మార్చండి\n• 'start' అని టైప్ చేయండి - బాట్‌ను పునఃప్రారంభించండి\n\nప్రారంభించడానికి నాకు ఆహార ఫోటో పంపండి! 📸",
                        "language_changed": "✅ భాష విజయవంతంగా నవీకరించబడింది! ఇప్పుడు మీరు పోషక విశ్లేషణ కోసం ఆహార ఫోటోలను పంపవచ్చు.",
                        "language_change_failed": "❌ భాషను నవీకరించడంలో విఫలమైంది. దయచేసి మళ్లీ ప్రయత్నించండి.",
                        "invalid_language": "❌ చెల్లని భాష ఎంపిక. దయచేసి అందుబాటులో ఉన్న ఎంపికల నుండి ఎంచుకోండి.",
                        "unsupported_message": "🤖 నేను ప్రాసెస్ చేయగలను:\n📝 టెక్స్ట్ సందేశాలు (కమాండ్‌లు)\n📸 ఆహార చిత్రాలు\n\nపోషక విశ్లేషణ కోసం *ఆహార ఫోటో* పంపండి లేదా సహాయం కోసం 'help' అని టైప్ చేయండి.",
                        "registration_failed": "❌ నమోదీకరణ విఫలమైంది. 'start' అని టైప్ చేసి మళ్లీ ప్రయత్నించండి.",
                        "invalid_name": "📝 దయచేసి చెల్లుబాటు అయ్యే పేరును నమోదు చేయండి (కనీసం 2 అక్షరాలు):",
                        "image_processing_error": "❌ క్షమించండి, మీ చిత్రాన్ని విశ్లేషించలేకపోయాను. దీనికి కారణాలు:\n\n• చిత్రం తగినంత స్పష్టంగా లేదు\n• చిత్రంలో ఆహారం కనిపించడం లేదు\n• సాంకేతిక ప్రాసెసింగ్ లోపం\n\nదయచేసి మీ ఆహారం యొక్క స్పష్టమైన ఫోటోతో మళ్లీ ప్రయత్నించండి! 📸",
                        "followup_message": "\n📸 మరింత విశ్లేషణ కోసం నాకు మరొక ఆహార ఫోటో పంపండి!\n💬 సహాయం కోసం 'help' లేదా భాష మార్చడానికి 'language' అని టైప్ చేయండి.",
                        "no_registration_session": "❌ నమోదీకరణ సెషన్ కనుగొనబడలేదు. ప్రారంభించడానికి 'start' అని టైప్ చేయండి.",
                        "user_incomplete": "❌ వినియోగదారు నమోదీకరణ అసంపూర్ణం. మళ్లీ నమోదు చేయడానికి 'start' అని టైప్ చేయండి.",
                        "unknown_command": "❌ ఆ కమాండ్ నాకు అర్థం కాలేదు. అందుబాటులో ఉన్న కమాండ్‌లను చూడటానికి 'help' అని టైప్ చేయండి లేదా విశ్లేషణ కోసం ఆహార ఫోటో పంపండి.",
                    },
                    "hi": {
                        "welcome": "👋 नमस्ते! मैं आपका AI पोषण विश्लेषक बॉट हूँ!\n\n📸 मुझे किसी भी खाने की फोटो भेजें और मैं प्रदान करूंगा:\n• विस्तृत पोषण संबंधी जानकारी\n• कैलोरी गिनती और मैक्रोज़\n• स्वास्थ्य विश्लेषण और सुझाव\n• सुधार के सुझाव\n\nबस अपने भोजन की एक स्पष्ट तस्वीर लें और मुझे भेज दें! 🍽️",
                        "language_selection": "🌍 स्वागत है! कृपया पहले अपनी पसंदीदा भाषा चुनें:\n\n• **English**\n• **Tamil** (தமிழ்)\n• **Telugu** (తెలుగు)\n• **Hindi** (हिन्दी)\n• **Kannada** (ಕನ್ನಡ)\n• **Malayalam** (മലയാളം)\n• **Marathi** (मराठी)\n• **Gujarati** (ગુજરાતી)\n• **Bengali** (বাংলা)\n\n💬 पूरे भाषा के नाम से जवाब दें (जैसे, 'Hindi', 'English', 'Tamil')",
                        "ask_name": "बहुत बढ़िया! कृपया अपना पूरा नाम दर्ज करें:",
                        "registration_complete": "✅ पंजीकरण सफलतापूर्वक पूरा हुआ! अब आप पोषण विश्लेषण के लिए खाने की फोटो भेज सकते हैं।",
                        "analyzing": "🔍 आपकी खाने की तस्वीर का विश्लेषण कर रहा हूँ... इसमें कुछ समय लग सकता है।",
                        "help": "🆘 **इस बॉट का उपयोग कैसे करें:**\n\n1. अपने खाने की स्पष्ट तस्वीर लें\n2. तस्वीर मुझे भेजें\n3. विश्लेषण का इंतजार करें\n4. विस्तृत पोषण जानकारी प्राप्त करें!\n\n**उपलब्ध कमांड:**\n• 'help' टाइप करें - यह सहायता संदेश दिखाएं\n• 'language' टाइप करें - अपनी पसंदीदा भाषा बदलें\n• 'start' टाइप करें - बॉट को पुनः आरंभ करें\n\nशुरू करने के लिए मुझे खाने की तस्वीर भेजें! 📸",
                        "language_changed": "✅ भाषा सफलतापूर्वक अपडेट हो गई! अब आप पोषण विश्लेषण के लिए खाने की फोटो भेज सकते हैं।",
                        "language_change_failed": "❌ भाषा अपडेट करने में विफल। कृपया पुनः प्रयास करें।",
                        "invalid_language": "❌ अमान्य भाषा का चयन। कृपया उपलब्ध विकल्पों में से चुनें।",
                        "unsupported_message": "🤖 मैं प्रोसेस कर सकता हूँ:\n📝 टेक्स्ट संदेश (कमांड)\n📸 खाने की तस्वीरें\n\nपोषण विश्लेषण के लिए *खाने की फोटो* भेजें या सहायता के लिए 'help' टाइप करें।",
                        "registration_failed": "❌ पंजीकरण विफल हुआ। 'start' टाइप करके पुनः प्रयास करें।",
                        "invalid_name": "📝 कृपया एक वैध नाम दर्ज करें (कम से कम 2 अक्षर):",
                        "image_processing_error": "❌ खुशी है, मैं आपकी तस्वीर का विश्लेषण नहीं कर सका। इसके कारण हो सकते हैं:\n\n• तस्वीर पर्याप्त स्पष्ट नहीं है\n• तस्वीर में खाना दिखाई नहीं दे रहा\n• तकनीकी प्रोसेसिंग त्रुटि\n\nकृपया अपने खाने की स्पष्ट तस्वीर के साथ पुनः प्रयास करें! 📸",
                        "followup_message": "\n📸 अधिक विश्लेषण के लिए मुझे खाने की और फोटो भेजें!\n💬 सहायता के लिए 'help' या भाषा बदलने के लिए 'language' टाइप करें।",
                        "no_registration_session": "❌ पंजीकरण सत्र नहीं मिला। शुरू करने के लिए 'start' टाइप करें।",
                        "user_incomplete": "❌ उपयोगकर्ता पंजीकरण अधूरा है। पुनः पंजीकरण के लिए 'start' टाइप करें।",
                        "unknown_command": "❌ मुझे वह कमांड समझ नहीं आया। उपलब्ध कमांड देखने के लिए 'help' टाइप करें या विश्लेषण के लिए खाने की फोटो भेजें।",
                    },
                    "kn": {
                        "welcome": "👋 ನಮಸ್ಕಾರ! ನಾನು ನಿಮ್ಮ AI ಪೋಷಣೆ ವಿಶ್ಲೇಷಕ ಬಾಟ್!\n\n📸 ಯಾವುದೇ ಆಹಾರದ ಫೋಟೋವನ್ನು ನನಗೆ ಕಳುಹಿಸಿ ಮತ್ತು ನಾನು ಒದಗಿಸುತ್ತೇನೆ:\n• ವಿವರವಾದ ಪೌಷ್ಟಿಕಾಂಶದ ಮಾಹಿತಿ\n• ಕ್ಯಾಲೋರಿ ಎಣಿಕೆ ಮತ್ತು ಮ್ಯಾಕ್ರೋಗಳು\n• ಆರೋಗ್ಯ ವಿಶ್ಲೇಷಣೆ ಮತ್ತು ಸಲಹೆಗಳು\n• ಸುಧಾರಣೆ ಸಲಹೆಗಳು\n\nನಿಮ್ಮ ಆಹಾರದ ಸ್ಪಷ್ಟ ಫೋಟೋವನ್ನು ತೆಗೆದು ನನಗೆ ಕಳುಹಿಸಿ! 🍽️",
                        "language_selection": "🌍 ಸ್ವಾಗತ! ದಯವಿಟ್ಟು ಮೊದಲು ನಿಮ್ಮ ಆದ್ಯತೆಯ ಭಾಷೆಯನ್ನು ಆಯ್ಕೆಮಾಡಿ:\n\n• **English**\n• **Tamil** (தமிழ்)\n• **Telugu** (తెలుగు)\n• **Hindi** (हिन्दी)\n• **Kannada** (ಕನ್ನಡ)\n• **Malayalam** (മലയാളം)\n• **Marathi** (मराठी)\n• **Gujarati** (ગુજરાતી)\n• **Bengali** (বাংলা)\n\n💬 ಪೂರ್ಣ ಭಾಷೆಯ ಹೆಸರಿನೊಂದಿಗೆ ಉತ್ತರಿಸಿ (ಉದಾ., 'Kannada', 'English', 'Hindi')",
                        "ask_name": "ಅದ್ಭುತ! ದಯವಿಟ್ಟು ನಿಮ್ಮ ಪೂರ್ಣ ಹೆಸರನ್ನು ನಮೂದಿಸಿ:",
                        "registration_complete": "✅ ನೋಂದಣಿ ಯಶಸ್ವಿಯಾಗಿ ಪೂರ್ಣಗೊಂಡಿದೆ! ಈಗ ನೀವು ಪೋಷಣೆ ವಿಶ್ಲೇಷಣೆಗಾಗಿ ಆಹಾರ ಫೋಟೋಗಳನ್ನು ಕಳುಹಿಸಬಹುದು.",
                        "analyzing": "🔍 ನಿಮ್ಮ ಆಹಾರ ಚಿತ್ರವನ್ನು ವಿಶ್ಲೇಷಿಸುತ್ತಿದ್ದೇನೆ... ಇದಕ್ಕೆ ಕೆಲವು ಕ್ಷಣಗಳು ಬೇಕಾಗಬಹುದು.",
                        "help": "🆘 **ಈ ಬಾಟ್ ಅನ್ನು ಹೇಗೆ ಬಳಸುವುದು:**\n\n1. ನಿಮ್ಮ ಆಹಾರದ ಸ್ಪಷ್ಟ ಫೋಟೋವನ್ನು ತೆಗೆದುಕೊಳ್ಳಿ\n2. ಚಿತ್ರವನ್ನು ನನಗೆ ಕಳುಹಿಸಿ\n3. ವಿಶ್ಲೇಷಣೆಗಾಗಿ ಕಾಯಿರಿ\n4. ವಿವರವಾದ ಪೋಷಣೆ ಮಾಹಿತಿಯನ್ನು ಪಡೆಯಿರಿ!\n\n**ಲಭ್ಯವಿರುವ ಆಜ್ಞೆಗಳು:**\n• 'help' ಎಂದು ಟೈಪ್ ಮಾಡಿ - ಈ ಸಹಾಯ ಸಂದೇಶವನ್ನು ತೋರಿಸಿ\n• 'language' ಎಂದು ಟೈಪ್ ಮಾಡಿ - ನಿಮ್ಮ ಆದ್ಯತೆಯ ಭಾಷೆಯನ್ನು ಬದಲಾಯಿಸಿ\n• 'start' ಎಂದು ಟೈಪ್ ಮಾಡಿ - ಬಾಟ್ ಅನ್ನು ಮರುಪ್ರಾರಂಭಿಸಿ\n\nಪ್ರಾರಂಭಿಸಲು ನನಗೆ ಆಹಾರ ಫೋಟೋವನ್ನು ಕಳುಹಿಸಿ! 📸",
                        "language_changed": "✅ ಭಾಷೆ ಯಶಸ್ವಿಯಾಗಿ ನವೀಕರಿಸಲಾಗಿದೆ! ಈಗ ನೀವು ಪೋಷಣೆ ವಿಶ್ಲೇಷಣೆಗಾಗಿ ಆಹಾರ ಫೋಟೋಗಳನ್ನು ಕಳುಹಿಸಬಹುದು.",
                        "language_change_failed": "❌ ಭಾಷೆಯನ್ನು ನವೀಕರಿಸಲು ವಿಫಲವಾಗಿದೆ. ದಯವಿಟ್ಟು ಮತ್ತೆ ಪ್ರಯತ್ನಿಸಿ.",
                        "invalid_language": "❌ ಅಮಾನ್ಯ ಭಾಷೆ ಆಯ್ಕೆ. ದಯವಿಟ್ಟು ಲಭ್ಯವಿರುವ ಆಯ್ಕೆಗಳಿಂದ ಆಯ್ಕೆಮಾಡಿ.",
                        "unsupported_message": "🤖 ನಾನು ಪ್ರಕ್ರಿಯೆಗೊಳಿಸಬಲ್ಲುದು:\n📝 ಪಠ್ಯ ಸಂದೇಶಗಳು (ಆಜ್ಞೆಗಳು)\n📸 ಆಹಾರ ಚಿತ್ರಗಳು\n\nಪೋಷಣೆ ವಿಶ್ಲೇಷಣೆಗಾಗಿ *ಆಹಾರ ಫೋಟೋ* ಕಳುಹಿಸಿ ಅಥವಾ ಸಹಾಯಕ್ಕಾಗಿ 'help' ಎಂದು ಟೈಪ್ ಮಾಡಿ.",
                        "registration_failed": "❌ ನೋಂದಣಿ ವಿಫಲವಾಗಿದೆ. 'start' ಎಂದು ಟೈಪ್ ಮಾಡಿ ಮತ್ತೆ ಪ್ರಯತ್ನಿಸಿ.",
                        "invalid_name": "📝 ದಯವಿಟ್ಟು ಮಾನ್ಯವಾದ ಹೆಸರನ್ನು ನಮೂದಿಸಿ (ಕನಿಷ್ಠ 2 ಅಕ್ಷರಗಳು):",
                        "image_processing_error": "❌ ಕ್ಷಮಿಸಿ, ನಿಮ್ಮ ಚಿತ್ರವನ್ನು ವಿಶ್ಲೇಷಿಸಲು ಸಾಧ್ಯವಾಗಲಿಲ್ಲ. ಇದಕ್ಕೆ ಕಾರಣಗಳು:\n\n• ಚಿತ್ರವು ಸಾಕಷ್ಟು ಸ್ಪಷ್ಟವಾಗಿಲ್ಲ\n• ಚಿತ್ರದಲ್ಲಿ ಆಹಾರ ಕಾಣಿಸುತ್ತಿಲ್ಲ\n• ತಾಂತ್ರಿಕ ಪ್ರಕ್ರಿಯೆ ದೋಷ\n\nದಯವಿಟ್ಟು ನಿಮ್ಮ ಆಹಾರದ ಸ್ಪಷ್ಟ ಫೋಟೋದೊಂದಿಗೆ ಮತ್ತೆ ಪ್ರಯತ್ನಿಸಿ! 📸",
                        "followup_message": "\n📸 ಹೆಚ್ಚಿನ ವಿಶ್ಲೇಷಣೆಗಾಗಿ ನನಗೆ ಇನ್ನೊಂದು ಆಹಾರ ಫೋಟೋ ಕಳುಹಿಸಿ!\n💬 ಸಹಾಯಕ್ಕಾಗಿ 'help' ಅಥವಾ ಭಾಷೆ ಬದಲಾಯಿಸಲು 'language' ಎಂದು ಟೈಪ್ ಮಾಡಿ.",
                        "no_registration_session": "❌ ನೋಂದಣಿ ಅಧಿವೇಶನ ಕಂಡುಬಂದಿಲ್ಲ. ಪ್ರಾರಂಭಿಸಲು 'start' ಎಂದು ಟೈಪ್ ಮಾಡಿ.",
                        "user_incomplete": "❌ ಬಳಕೆದಾರ ನೋಂದಣಿ ಅಪೂರ್ಣವಾಗಿದೆ. ಮರುನೋಂದಣಿಗಾಗಿ 'start' ಎಂದು ಟೈಪ್ ಮಾಡಿ.",
                        "unknown_command": "❌ ನನಗೆ ಆ ಆಜ್ಞೆ ಅರ್ಥವಾಗಲಿಲ್ಲ. ಲಭ್ಯವಿರುವ ಆಜ್ಞೆಗಳನ್ನು ನೋಡಲು 'help' ಎಂದು ಟೈಪ್ ಮಾಡಿ ಅಥವಾ ವಿಶ್ಲೇಷಣೆಗಾಗಿ ಆಹಾರ ಫೋಟೋ ಕಳುಹಿಸಿ.",
                    },
                    "ml": {
                        "welcome": "👋 നമസ്കാരം! ഞാൻ നിങ്ങളുടെ AI പോഷകാഹാര വിശകലന ബോട്ട്!\n\n📸 ഏതെങ്കിലും ഭക്ഷണത്തിന്റെ ഫോട്ടോ എനിക്ക് അയച്ചാൽ ഞാൻ നൽകും:\n• വിശദമായ പോഷകാഹാര വിവരങ്ങൾ\n• കലോറി എണ്ണവും മാക്രോകളും\n• ആരോഗ്യ വിശകലനവും നുറുങ്ങുകളും\n• മെച്ചപ്പെടുത്തൽ നിർദ്ദേശങ്ങൾ\n\nനിങ്ങളുടെ ഭക്ഷണത്തിന്റെ വ്യക്തമായ ഫോട്ടോ എടുത്ത് എനിക്ക് അയക്കുക! 🍽️",
                        "language_selection": "🌍 സ്വാഗതം! ദയവായി ആദ്യം നിങ്ങളുടെ ഇഷ്ട ഭാഷ തിരഞ്ഞെടുക്കുക:\n\n• **English**\n• **Tamil** (தமிழ்)\n• **Telugu** (తెలుగు)\n• **Hindi** (हिन्दी)\n• **Kannada** (ಕನ್ನಡ)\n• **Malayalam** (മലയാളം)\n• **Marathi** (मराठी)\n• **Gujarati** (ગુજરાતી)\n• **Bengali** (বাংলা)\n\n💬 പൂർണ്ണമായ ഭാഷാ നാമത്തോടെ മറുപടി നൽകുക (ഉദാ., 'Malayalam', 'English', 'Hindi')",
                        "ask_name": "മികച്ചു! ദയവായി നിങ്ങളുടെ പൂർണ്ണ നാമം നൽകുക:",
                        "registration_complete": "✅ രജിസ്ട്രേഷൻ വിജയകരമായി പൂർത്തിയായി! ഇപ്പോൾ നിങ്ങൾക്ക് പോഷകാഹാര വിശകലനത്തിനായി ഭക്ഷണ ഫോട്ടോകൾ അയക്കാം.",
                        "analyzing": "🔍 നിങ്ങളുടെ ഭക്ഷണ ചിത്രം വിശകലനം ചെയ്യുന്നു... ഇതിന് കുറച്ച് നിമിഷങ്ങൾ എടുത്തേക്കാം.",
                        "help": "🆘 **ഈ ബോട്ട് എങ്ങനെ ഉപയോഗിക്കാം:**\n\n1. നിങ്ങളുടെ ഭക്ഷണത്തിന്റെ വ്യക്തമായ ഫോട്ടോ എടുക്കുക\n2. ചിത്രം എനിക്ക് അയക്കുക\n3. വിശകലനത്തിനായി കാത്തിരിക്കുക\n4. വിശദമായ പോഷകാഹാര വിവരങ്ങൾ നേടുക!\n\n**ലഭ്യമായ കമാൻഡുകൾ:**\n• 'help' ടൈപ്പ് ചെയ്യുക - ഈ സഹായ സന്ദേശം കാണിക്കുക\n• 'language' ടൈപ്പ് ചെയ്യുക - നിങ്ങളുടെ ഇഷ്ട ഭാഷ മാറ്റുക\n• 'start' ടൈപ്പ് ചെയ്യുക - ബോട്ട് പുനരാരംഭിക്കുക\n\nആരംഭിക്കാൻ എനിക്ക് ഒരു ഭക്ഷണ ഫോട്ടോ അയക്കുക! 📸",
                        "language_changed": "✅ ഭാഷ വിജയകരമായി അപ്‌ഡേറ്റ് ചെയ്തു! ഇപ്പോൾ നിങ്ങൾക്ക് പോഷകാഹാര വിശകലനത്തിനായി ഭക്ഷണ ഫോട്ടോകൾ അയക്കാം.",
                        "language_change_failed": "❌ ഭാഷ അപ്‌ഡേറ്റ് ചെയ്യുന്നതിൽ പരാജയപ്പെട്ടു. ദയവായി വീണ്ടും ശ്രമിക്കുക.",
                        "invalid_language": "❌ അസാധുവായ ഭാഷാ തിരഞ്ഞെടുപ്പ്. ദയവായി ലഭ്യമായ ഓപ്‌ഷനുകളിൽ നിന്ന് തിരഞ്ഞെടുക്കുക.",
                        "unsupported_message": "🤖 എനിക്ക് പ്രോസസ്സ് ചെയ്യാൻ കഴിയും:\n📝 ടെക്‌സ്റ്റ് സന്ദേശങ്ങൾ (കമാൻഡുകൾ)\n📸 ഭക്ഷണ ചിത്രങ്ങൾ\n\nപോഷകാഹാര വിശകലനത്തിനായി *ഭക്ഷണ ഫോട്ടോ* അയക്കുക അല്ലെങ്കിൽ സഹായത്തിനായി 'help' ടൈപ്പ് ചെയ്യുക.",
                        "registration_failed": "❌ രജിസ്ട്രേഷൻ പരാജയപ്പെട്ടു. 'start' ടൈപ്പ് ചെയ്ത് വീണ്ടും ശ്രമിക്കുക.",
                        "invalid_name": "📝 ദയവായി സാധുവായ ഒരു നാമം നൽകുക (കുറഞ്ഞത് 2 അക്ഷരങ്ങൾ):",
                        "image_processing_error": "❌ ക്ഷമിക്കുക, നിങ്ങളുടെ ചിത്രം വിശകലനം ചെയ്യാൻ എനിക്ക് കഴിഞ്ഞില്ല. ഇതിന് കാരണങ്ങൾ:\n\n• ചിത്രം വേണ്ടത്ര വ്യക്തമല്ല\n• ചിത്രത്തിൽ ഭക്ഷണം കാണാനില്ല\n• സാങ്കേതിക പ്രോസസ്സിംഗ് പിശക്\n\nദയവായി നിങ്ങളുടെ ഭക്ഷണത്തിന്റെ വ്യക്തമായ ഫോട്ടോയുമായി വീണ്ടും ശ്രമിക്കുക! 📸",
                        "followup_message": "\n📸 കൂടുതൽ വിശകലനത്തിനായി എനിക്ക് മറ്റൊരു ഭക്ഷണ ഫോട്ടോ അയക്കുക!\n💬 സഹായത്തിനായി 'help' അല്ലെങ്കിൽ ഭാഷ മാറ്റാൻ 'language' ടൈപ്പ് ചെയ്യുക.",
                        "no_registration_session": "❌ രജിസ്ട്രേഷൻ സെഷൻ കണ്ടെത്തിയില്ല. ആരംഭിക്കാൻ 'start' ടൈപ്പ് ചെയ്യുക.",
                        "user_incomplete": "❌ ഉപയോക്താവിന്റെ രജിസ്ട്രേഷൻ അപൂർണ്ണമാണ്. വീണ്ടും രജിസ്റ്റർ ചെയ്യാൻ 'start' ടൈപ്പ് ചെയ്യുക.",
                        "unknown_command": "❌ ആ കമാൻഡ് എനിക്ക് മനസ്സിലായില്ല. ലഭ്യമായ കമാൻഡുകൾ കാണാൻ 'help' ടൈപ്പ് ചെയ്യുക അല്ലെങ്കിൽ വിശകലനത്തിനായി ഭക്ഷണ ഫോട്ടോ അയക്കുക.",
                    },
                    "mr": {
                        "welcome": "👋 नमस्कार! मी तुमचा AI पोषण विश्लेषक बॉट आहे!\n\n📸 मला कोणत्याही अन्नाचा फोटो पाठवा आणि मी प्रदान करीन:\n• तपशीलवार पोषण माहिती\n• कॅलरी मोजणी आणि मॅक्रोज\n• आरोग्य विश्लेषण आणि टिप्स\n• सुधारणा सूचना\n\nफक्त तुमच्या जेवणाचा स्पष्ट फोटो काढा आणि मला पाठवा! 🍽️",
                        "language_selection": "🌍 स्वागत आहे! कृपया प्रथम तुमची आवडती भाषा निवडा:\n\n• **English**\n• **Tamil** (தமிழ்)\n• **Telugu** (తెలుగు)\n• **Hindi** (हिन्दी)\n• **Kannada** (ಕನ್ನಡ)\n• **Malayalam** (മലയാളം)\n• **Marathi** (मराठी)\n• **Gujarati** (ગુજરાતી)\n• **Bengali** (বাংলা)\n\n💬 पूर्ण भाषेच्या नावाने उत्तर द्या (उदा., 'Marathi', 'English', 'Hindi')",
                        "ask_name": "उत्तम! कृपया तुमचे पूर्ण नाव प्रविष्ट करा:",
                        "registration_complete": "✅ नोंदणी यशस्वीरित्या पूर्ण झाली! आता तुम्ही पोषण विश्लेषणासाठी अन्न फोटो पाठवू शकता.",
                        "analyzing": "🔍 तुमच्या अन्न प्रतिमेचे विश्लेषण करत आहे... यास काही क्षण लागू शकतात.",
                        "help": "🆘 **हा बॉट कसा वापरावा:**\n\n1. तुमच्या अन्नाचा स्पष्ट फोटो काढा\n2. प्रतिमा मला पाठवा\n3. विश्लेषणाची प्रतीक्षा करा\n4. तपशीलवार पोषण माहिती मिळवा!\n\n**उपलब्ध आदेश:**\n• 'help' टाइप करा - हा मदत संदेश दाखवा\n• 'language' टाइप करा - तुमची आवडती भाषा बदला\n• 'start' टाइप करा - बॉट पुन्हा सुरू करा\n\nसुरुवात करण्यासाठी मला अन्न फोटो पाठवा! 📸",
                        "language_changed": "✅ भाषा यशस्वीरित्या अपडेट झाली! आता तुम्ही पोषण विश्लेषणासाठी अन्न फोटो पाठवू शकता.",
                        "language_change_failed": "❌ भाषा अपडेट करण्यात अयशस्वी. कृपया पुन्हा प्रयत्न करा.",
                        "invalid_language": "❌ अवैध भाषा निवड. कृपया उपलब्ध पर्यायांमधून निवडा.",
                        "unsupported_message": "🤖 मी प्रक्रिया करू शकतो:\n📝 मजकूर संदेश (आदेश)\n📸 अन्न प्रतिमा\n\nपोषण विश्लेषणासाठी *अन्न फोटो* पाठवा किंवा मदतीसाठी 'help' टाइप करा.",
                        "registration_failed": "❌ नोंदणी अयशस्वी झाली. 'start' टाइप करून पुन्हा प्रयत्न करा.",
                        "invalid_name": "📝 कृपया वैध नाव प्रविष्ट करा (किमान 2 अक्षरे):",
                        "image_processing_error": "❌ क्षमस्व, मी तुमची प्रतिमा विश्लेषित करू शकलो नाही. यासाठी कारणे:\n\n• प्रतिमा पुरेशी स्पष्ट नाही\n• प्रतिमेत अन्न दिसत नाही\n• तांत्रिक प्रक्रिया त्रुटी\n\nकृपया तुमच्या अन्नाच्या स्पष्ट फोटोसह पुन्हा प्रयत्न करा! 📸",
                        "followup_message": "\n📸 अधिक विश्लेषणासाठी मला दुसरा अन्न फोटो पाठवा!\n💬 मदतीसाठी 'help' किंवा भाषा बदलण्यासाठी 'language' टाइप करा.",
                        "no_registration_session": "❌ नोंदणी सत्र सापडले नाही. सुरुवात करण्यासाठी 'start' टाइप करा.",
                        "user_incomplete": "❌ वापरकर्त्याची नोंदणी अपूर्ण आहे. पुन्हा नोंदणी करण्यासाठी 'start' टाइप करा.",
                        "unknown_command": "❌ तो आदेश मला समजला नाही. उपलब्ध आदेश पाहण्यासाठी 'help' टाइप करा किंवा विश्लेषणासाठी अन्न फोटो पाठवा.",
                    },
                    "gu": {
                        "welcome": "👋 નમસ્કાર! હું તમારો AI પોષણ વિશ્લેષણ બોટ છું!\n\n📸 મને કોઈપણ ખોરાકનો ફોટો મોકલો અને હું આપીશ:\n• વિસ્તૃત પોષણ માહિતી\n• કેલરી ગણતરી અને મેક્રોઝ\n• આરોગ્ય વિશ્લેષણ અને સુઝાવો\n• સુધારણા માર્ગદર્શન\n\nફક્ત તમારા ખોરાકનો સ્પષ્ટ ફોટો લો અને મને મોકલો! 🍽️",
                        "language_selection": "🌍 સ્વાગત છે! કૃપા કરીને પહેલા તમારી પસંદીદા ભાષા પસંદ કરો:\n\n• **English**\n• **Tamil** (தமிழ்)\n• **Telugu** (తెలుగు)\n• **Hindi** (हिन्दी)\n• **Kannada** (ಕನ್ನಡ)\n• **Malayalam** (മലയാളം)\n• **Marathi** (मराठी)\n• **Gujarati** (ગુજરાતી)\n• **Bengali** (বাংলা)\n\n💬 સંપૂર્ણ ભાષાના નામ સાથે જવાબ આપો (દા.ત., 'Gujarati', 'English', 'Hindi')",
                        "ask_name": "ઉત્તમ! કૃપા કરીને તમારું સંપૂર્ણ નામ દાખલ કરો:",
                        "registration_complete": "✅ નોંધણી સફળતાપૂર્વક પૂર્ણ થઈ! હવે તમે પોષણ વિશ્લેષણ માટે ખોરાકના ફોટા મોકલી શકો છો.",
                        "analyzing": "🔍 તમારી ખોરાકની છબીનું વિશ્લેષણ કરી રહ્યો છું... આમાં થોડી ક્ષણો લાગી શકે છે.",
                        "help": "🆘 **આ બોટ કેવી રીતે ઉપયોગ કરવો:**\n\n1. તમારા ખોરાકનો સ્પષ્ટ ફોટો લો\n2. છબી મને મોકલો\n3. વિશ્લેષણની રાહ જુઓ\n4. વિસ્તૃત પોષણ માહિતી મેળવો!\n\n**ઉપલબ્ધ આદેશો:**\n• 'help' ટાઈપ કરો - આ સહાય સંદેશ બતાવો\n• 'language' ટાઈપ કરો - તમારી પસંદીદા ભાષા બદલો\n• 'start' ટાઈપ કરો - બોટ ફરીથી શરૂ કરો\n\nશરૂ કરવા માટે મને ખોરાકનો ફોટો મોકલો! 📸",
                        "language_changed": "✅ ભાષા સફળતાપૂર્વક અપડેટ થઈ! હવે તમે પોષણ વિશ્લેષણ માટે ખોરાકના ફોટા મોકલી શકો છો.",
                        "language_change_failed": "❌ ભાષા અપડેટ કરવામાં નિષ્ફળ. કૃપા કરીને ફરીથી પ્રયાસ કરો.",
                        "invalid_language": "❌ અમાન્ય ભાષા પસંદગી. કૃપા કરીને ઉપલબ્ધ વિકલ્પોમાંથી પસંદ કરો.",
                        "unsupported_message": "🤖 હું પ્રક્રિયા કરી શકું છું:\n📝 ટેક્સ્ટ સંદેશાઓ (આદેશો)\n📸 ખોરાકની છબીઓ\n\nપોષણ વિશ્લેષણ માટે *ખોરાકનો ફોટો* મોકલો અથવા સહાય માટે 'help' ટાઈપ કરો.",
                        "registration_failed": "❌ નોંધણી નિષ્ફળ. 'start' ટાઈપ કરીને ફરીથી પ્રયાસ કરો.",
                        "invalid_name": "📝 કૃપા કરીને માન્ય નામ દાખલ કરો (ઓછામાં ઓછા 2 અક્ષરો):",
                        "image_processing_error": "❌ માફ કરશો, હું તમારી છબીનું વિશ્લેષણ કરી શક્યો નહીં. આના કારણો:\n\n• છબી પૂરતી સ્પષ્ટ નથી\n• છબીમાં ખોરાક દેખાતો નથી\n• તકનીકી પ્રક્રિયા ભૂલ\n\nકૃપા કરીને તમારા ખોરાકના સ્પષ્ટ ફોટો સાથે ફરીથી પ્રયાસ કરો! 📸",
                        "followup_message": "\n📸 વધુ વિશ્લેષણ માટે મને બીજો ખોરાકનો ફોટો મોકલો!\n💬 સહાય માટે 'help' અથવા ભાષા બદલવા માટે 'language' ટાઈપ કરો.",
                        "no_registration_session": "❌ નોંધણી સત્ર મળ્યું નહીં. શરૂ કરવા માટે 'start' ટાઈપ કરો.",
                        "user_incomplete": "❌ વપરાશકર્તાની નોંધણી અધૂરી છે. ફરીથી નોંધણી કરવા માટે 'start' ટાઈપ કરો.",
                        "unknown_command": "❌ તે આદેશ મને સમજાયો નહીં. ઉપલબ્ધ આદેશો જોવા માટે 'help' ટાઈપ કરો અથવા વિશ્લેષણ માટે ખોરાકનો ફોટો મોકલો.",
                    },
                    "bn": {
                        "welcome": "👋 নমস্কার! আমি আপনার AI পুষ্টি বিশ্লেষণ বট!\n\n📸 আমাকে যেকোনো খাবারের ফটো পাঠান এবং আমি প্রদান করব:\n• বিস্তারিত পুষ্টি তথ্য\n• ক্যালোরি গণনা এবং ম্যাক্রো\n• স্বাস্থ্য বিশ্লেষণ এবং টিপস\n• উন্নতির সুপারিশ\n\nশুধু আপনার খাবারের স্পষ্ট ফটো তুলুন এবং আমাকে পাঠান! 🍽️",
                        "language_selection": "🌍 স্বাগতম! অনুগ্রহ করে প্রথমে আপনার পছন্দের ভাষা নির্বাচন করুন:\n\n• **English**\n• **Tamil** (தமிழ்)\n• **Telugu** (తెలుగు)\n• **Hindi** (हिन्दी)\n• **Kannada** (ಕನ್ನಡ)\n• **Malayalam** (മലയാളം)\n• **Marathi** (मराठी)\n• **Gujarati** (ગુજરાતી)\n• **Bengali** (বাংলা)\n\n💬 সম্পূর্ণ ভাষার নাম দিয়ে উত্তর দিন (যেমন, 'Bengali', 'English', 'Hindi')",
                        "ask_name": "চমৎকার! অনুগ্রহ করে আপনার সম্পূর্ণ নাম লিখুন:",
                        "registration_complete": "✅ নিবন্ধন সফলভাবে সম্পন্ন হয়েছে! এখন আপনি পুষ্টি বিশ্লেষণের জন্য খাবারের ফটো পাঠাতে পারেন।",
                        "analyzing": "🔍 আপনার খাবারের ছবি বিশ্লেষণ করছি... এতে কিছু মুহূর্ত লাগতে পারে।",
                        "help": "🆘 **এই বট কীভাবে ব্যবহার করবেন:**\n\n1. আপনার খাবারের স্পষ্ট ফটো তুলুন\n2. ছবিটি আমাকে পাঠান\n3. বিশ্লেষণের জন্য অপেক্ষা করুন\n4. বিস্তারিত পুষ্টি তথ্য পান!\n\n**উপলব্ধ কমান্ড:**\n• 'help' টাইপ করুন - এই সাহায্য বার্তা দেখান\n• 'language' টাইপ করুন - আপনার পছন্দের ভাষা পরিবর্তন করুন\n• 'start' টাইপ করুন - বট পুনরায় শুরু করুন\n\nশুরু করতে আমাকে একটি খাবারের ফটো পাঠান! 📸",
                        "language_changed": "✅ ভাষা সফলভাবে আপডেট হয়েছে! এখন আপনি পুষ্টি বিশ্লেষণের জন্য খাবারের ফটো পাঠাতে পারেন।",
                        "language_change_failed": "❌ ভাষা আপডেট করতে ব্যর্থ। অনুগ্রহ করে আবার চেষ্টা করুন।",
                        "invalid_language": "❌ অবৈধ ভাষা নির্বাচন। অনুগ্রহ করে উপলব্ধ বিকল্পগুলি থেকে নির্বাচন করুন।",
                        "unsupported_message": "🤖 আমি প্রক্রিয়া করতে পারি:\n📝 টেক্সট বার্তা (কমান্ড)\n📸 খাবারের ছবি\n\nপুষ্টি বিশ্লেষণের জন্য *খাবারের ফটো* পাঠান বা সাহায্যের জন্য 'help' টাইপ করুন।",
                        "registration_failed": "❌ নিবন্ধন ব্যর্থ হয়েছে। 'start' টাইপ করে আবার চেষ্টা করুন।",
                        "invalid_name": "📝 অনুগ্রহ করে একটি বৈধ নাম লিখুন (কমপক্ষে ২টি অক্ষর):",
                        "image_processing_error": "❌ দুঃখিত, আমি আপনার ছবি বিশ্লেষণ করতে পারিনি। এর কারণ:\n\n• ছবি যথেষ্ট স্পষ্ট নয়\n• ছবিতে খাবার দেখা যাচ্ছে না\n• প্রযুক্তিগত প্রক্রিয়াকরণ ত্রুটি\n\nঅনুগ্রহ করে আপনার খাবারের স্পষ্ট ফটো দিয়ে আবার চেষ্টা করুন! 📸",
                        "followup_message": "\n📸 আরও বিশ্লেষণের জন্য আমাকে আরেকটি খাবারের ফটো পাঠান!\n💬 সাহায্যের জন্য 'help' বা ভাষা পরিবর্তনের জন্য 'language' টাইপ করুন।",
                        "no_registration_session": "❌ নিবন্ধন সেশন পাওয়া যায়নি। শুরু করতে 'start' টাইপ করুন।",
                        "user_incomplete": "❌ ব্যবহারকারীর নিবন্ধন অসম্পূর্ণ। পুনরায় নিবন্ধনের জন্য 'start' টাইপ করুন।",
                        "unknown_command": "❌ সেই কমান্ড আমি বুঝতে পারিনি। উপলব্ধ কমান্ড দেখতে 'help' টাইপ করুন বা বিশ্লেষণের জন্য খাবারের ফটো পাঠান।",
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
            options.append(f"• {name.split(' (')[0]}")  # Remove script part for cleaner display

        return "🌍 Please select your preferred language:\n\n" + "\n".join(options) + "\n\n💬 Reply with the full language name (e.g., English, Tamil, Hindi)"

class NutritionAnalyzer:
    def __init__(self, language_manager):
        self.language_manager = language_manager
        
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

        FIRST, analyze this image carefully to determine if it contains food or a food dish.

        If this image does NOT contain food (e.g., it's a person, object, scenery, text, etc.), respond with ONLY this JSON structure:
        {{
            "is_food": false,
            "image_description": "brief description of what you see in the image in requested language",
            "message": "polite message asking user to send a food image in requested language"
        }}

        If this image DOES contain food, analyze it and provide ONLY a JSON response with the following exact structure:
        {{
            "is_food": true,
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
            
            # Check if it's a food image
            if not nutrition_data.get('is_food', True):
                # Handle non-food image
                user_message = self._create_non_food_message(nutrition_data, language)
                return user_message, {}

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
    
    def _create_non_food_message(self, response_data: dict, language: str) -> str:
        """Create message for non-food images"""
        try:
            image_description = response_data.get('image_description', '')
            ai_message = response_data.get('message', '')
        
            # Use the hardcoded messages instead of language_manager
            non_food_messages = {
                'en': f"🚫 This appears to be: {image_description}\n\n{ai_message}\n\nPlease send a clear photo of food for nutrition analysis! 📸🍽️",
                'ta': f"🚫 இது தோன்றுகிறது: {image_description}\n\n{ai_message}\n\nஊட்டச்சத்து பகுப்பாய்வுக்கு உணவின் தெளிவான புகைப்படத்தை அனுப்பவும்! 📸🍽️",
                'te': f"🚫 ఇది కనిపిస్తోంది: {image_description}\n\n{ai_message}\n\nపోషకాహార విశ్లేషణ కోసం ఆహారం యొక్క స్పష్టమైన ఫోటోను పంపండి! 📸🍽️",
                'hi': f"🚫 यह दिखाई दे रहा है: {image_description}\n\n{ai_message}\n\nपोषण विश्लेषण के लिए भोजन की स्पष्ट तस्वीर भेजें! 📸🍽️",
                'kn': f"🚫 ಇದು ಕಾಣಿಸುತ್ತದೆ: {image_description}\n\n{ai_message}\n\nಪೋಷಣೆ ವಿಶ್ಲೇಷಣೆಗಾಗಿ ಆಹಾರದ ಸ್ಪಷ್ಟ ಫೋಟೋವನ್ನು ಕಳುಹಿಸಿ! 📸🍽️",
                'ml': f"🚫 ഇത് കാണുന്നത്: {image_description}\n\n{ai_message}\n\nപോഷകാഹാര വിശകലനത്തിനായി ഭക്ഷണത്തിന്റെ വ്യക്തമായ ഫോട്ടോ അയയ്ക്കുക! 📸🍽️",
                'mr': f"🚫 हे दिसत आहे: {image_description}\n\n{ai_message}\n\nपोषण विश्लेषणासाठी अन्नाचा स्पष्ट फोटो पाठवा! 📸🍽️",
                'gu': f"🚫 આ દેખાય છે: {image_description}\n\n{ai_message}\n\nપોષણ વિશ્લેષણ માટે ખોરાકનો સ્પષ્ટ ફોટો મોકલો! 📸🍽️",
                'bn': f"🚫 এটি দেখা যাচ্ছে: {image_description}\n\n{ai_message}\n\nপুষ্টি বিশ্লেষণের জন্য খাবারের স্পষ্ট ছবি পাঠান! 📸🍽️"
            }
            
            return non_food_messages.get(language, non_food_messages['en'])
            
        except Exception as e:
            logger.error(f"Error creating non-food message: {e}")
            return self._get_non_food_fallback_message(language)
    
    def _get_non_food_fallback_message(self, language: str) -> str:
        """Simple fallback message with hardcoded messages"""
        try:
            fallback_messages = {
                'en': "🚫 This doesn't appear to be a food image. Please send a clear photo of food for nutrition analysis!",
                'ta': "🚫 இது உணவு படம் அல்ல. ஊட்டச்சத்து பகுப்பாய்வுக்கு உணவின் தெளிவான புகைப்படத்தை அனுப்பவும்!",
                'te': "🚫 ఇది ఆహార చిత్రం కాదు. పోషకాహార విశ్లేషణ కోసం ఆహారం యొక్క స్పష్టమైన ఫోటోను పంపండి!",
                'hi': "🚫 यह भोजन की तस्वीर नहीं लगती। कृपया पोषण विश्लेషण के लिए भोजन की स्पष्ट तस्वీर भेजें!",
                'kn': "🚫 ಇದು ಆಹಾರ ಚಿತ್ರವಲ್ಲ. ಪೋಷಣೆ ವಿಶ್ಲೇಷಣೆಗಾಗಿ ಆಹಾರದ ಸ್ಪಷ್ಟ ಫೋಟೋವನ್ನು ಕಳುಹಿಸಿ!",
                'ml': "🚫 ഇത് ഭക്ഷണ ചിത്രമല്ല. പോഷകാഹാര വിശകലനത്തിനായി ഭക്ഷണത്തിന്റെ വ്യക്തമായ ഫോട്ടോ അയയ്ക്കുക!",
                'mr': "🚫 हा अन्नाचा फोटो नाही. पोषण विश्लेषणासाठी अन्नाचा स्पष्ट फोटो पाठवा!",
                'gu': "🚫 આ ખોરાકનો ફોટો નથી. પોષણ વિશ્લેષણ માટે ખોરાકનો સ્પષ્ટ ફોટો મોકલો!",
                'bn': "🚫 এটি খাবারের ছবি নয়। পুষ্টি বিশ্লেষণের জন্য খাবারের স্পষ্ট ছবি পাঠান!"
            }
            
            return fallback_messages.get(language, fallback_messages['en'])
            
        except Exception as e:
            logger.error(f"Error getting fallback message: {e}")
            # Ultimate hardcoded fallback
            return "🚫 This doesn't appear to be a food image. Please send a clear photo of food for nutrition analysis!"


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

# 11za Bot class to handle message sending
class ElevenZABot:
    def __init__(self):
        self.auth_token = AUTH_TOKEN
        self.origin_website = ORIGIN_WEBSITE
        self.send_url = SEND_MESSAGE_URL
    
    def send_messages(self, to_number: str, message: str):
        """Send text message via 11za API"""
        try:
            payload = json.dumps({
                "sendto": to_number,
                "authToken": self.auth_token,
                "originWebsite": self.origin_website,
                "contentType": "text",
                "text": message
            }).encode("utf-8")
            
            req = urllib.request.Request(
                self.send_url,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            
            with urllib.request.urlopen(req) as resp:
                logger.info(f"Message sent to {to_number}: {resp.status}")
                return resp.status == 200
                
        except Exception as e:
            logger.error(f"Error sending message to {to_number}: {e}")
            return False
    
    def download_media(self, media_url: str) -> bytes:
        """Download media from 11za URL"""
        try:
            req = urllib.request.Request(media_url)
            with urllib.request.urlopen(req) as response:
                return response.read()
        except Exception as e:
            logger.error(f"Error downloading media from {media_url}: {e}")
            return None

# Initialize components
try:
    db_manager = DatabaseManager()
    s3_manager = S3Manager()
    language_manager = LanguageManager(db_manager)
    analyzer = NutritionAnalyzer(language_manager)
    whatsapp_bot = WhatsAppBot(WHATSAPP_TOKEN, WHATSAPP_PHONE_NUMBER_ID)
    elevenza_bot = ElevenZABot()
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
                json.dumps(nutrition_json) if nutrition_json else "{}"  # # Convert dict to JSON string
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

# Add this new route to your existing Flask app
@app.route('/webhook/11za', methods=['POST'])
def handle_11za_webhook():
    """Handle incoming 11za messages"""
    try:
        data = request.get_json()
        
        if not data:
            return jsonify({'status': 'no_data'}), 400
        
        logger.info(f"11za webhook received: {json.dumps(data)}")
        
        # Process the 11za message
        process_11za_message(data)
        
        return jsonify({'status': 'success'}), 200
        
    except Exception as e:
        logger.error(f"11za webhook processing error: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

def process_11za_message(data: Dict[str, Any]):
    """Process individual 11za message"""
    try:
        sender = data.get("from")
        content = data.get("content", {})
        content_type = content.get("contentType")
        
        logger.info(f"Processing {content_type} message from {sender}")
        
        if content_type == "text":
            handle_11za_text_message(sender, content)
        elif content_type == "media":
            handle_11za_media_message(sender, content)
        else:
            # Handle unsupported message types
            user = db_manager.get_user_by_phone(sender)
            user_language = user.get('preferred_language', 'en') if user else 'en'
            unsupported_message = language_manager.get_message(user_language, 'unsupported_message')
            elevenza_bot.send_messages(sender, unsupported_message)
            
    except Exception as e:
        logger.error(f"Error processing 11za message: {e}")

def handle_11za_text_message(sender: str, content: Dict[str, Any]):
    """Handle incoming text messages from 11za"""
    try:
        text_content = content.get("text", "").strip().lower()
        
        logger.info(f"11za text message from {sender}: {text_content}")
        
        # ENHANCED DEBUGGING - Check if user exists
        user = db_manager.get_user_by_phone(sender)
        
        # Add detailed logging for debugging - FIXED
        logger.info(f"DEBUG: User lookup for {sender}")
        logger.info(f"DEBUG: User data: {safe_json_serialize(user) if user else 'None'}")
        logger.info(f"DEBUG: User type: {type(user)}")
        logger.info(f"DEBUG: User truthiness: {bool(user)}")
        
        # Check for registration session
        session = db_manager.get_registration_session(sender)
        logger.info(f"DEBUG: Registration session: {safe_json_serialize(session) if session else 'None'}")
        
        # Handle different text commands
        if text_content in ['start', 'hello', 'hi', 'hey']:
            logger.info(f"DEBUG: Calling handle_11za_start_command with user: {user}")
            handle_11za_start_command(sender, user)
        elif text_content == 'help':
            handle_11za_help_command(sender, user)
        elif text_content == 'language':
            handle_11za_language_command(sender)
        elif is_language_selection(text_content):
            handle_11za_language_selection(sender, text_content, user)
        elif not user:
            # User doesn't exist, start registration
            logger.info(f"DEBUG: Starting registration flow for {sender}")
            handle_11za_registration_flow(sender, text_content)
        else:
            # User exists but sent unrecognized text
            logger.info(f"DEBUG: User exists, sending unknown command message")
            user_language = user.get('preferred_language', 'en') if user else 'en'
            unknown_message = language_manager.get_message(user_language, 'unknown_command')
            elevenza_bot.send_messages(sender, unknown_message)
            
    except Exception as e:
        logger.error(f"Error handling 11za text message: {e}")


def handle_11za_media_message(sender: str, content: Dict[str, Any]):
    """Handle incoming media messages from 11za"""
    try:
        media_info = content.get("media", {})
        media_type = media_info.get("type")
        media_url = media_info.get("url")
        
        logger.info(f"11za media message from {sender}, type: {media_type}, url: {media_url}")
        
        if media_type != "image":
            user = db_manager.get_user_by_phone(sender)
            user_language = user.get('preferred_language', 'en') if user else 'en'
            unsupported_message = language_manager.get_message(user_language, 'unsupported_message')
            elevenza_bot.send_messages(sender, unsupported_message)
            return
        
        # Check if user exists
        user = db_manager.get_user_by_phone(sender)
        if not user:
            # User doesn't exist, start registration with language
            welcome_message = language_manager.get_message('en', 'language_selection') + "\n\n" + language_manager.get_language_options_text()
            elevenza_bot.send_messages(sender, welcome_message)
            db_manager.update_registration_session(sender, 'language', {})
            return
        
        if 'user_id' not in user or user['user_id'] is None:
            logger.error(f"User {sender} does not have user_id: {user}")
            user_language = user.get('preferred_language', 'en')
            error_message = language_manager.get_message(user_language, 'user_incomplete')
            elevenza_bot.send_messages(sender, error_message)
            return
        
        user_language = user.get('preferred_language', 'en')
        
        # Send analysis started message
        analyzing_message = language_manager.get_message(user_language, 'analyzing')
        elevenza_bot.send_messages(sender, analyzing_message)
        
        # Download and process image
        try:
            # Download image from 11za
            image_bytes = elevenza_bot.download_media(media_url)
            
            if not image_bytes:
                error_message = language_manager.get_message(user_language, 'image_processing_error')
                elevenza_bot.send_messages(sender, error_message)
                return
            
            # Upload to S3
            image_url, file_location = s3_manager.upload_image(image_bytes, user['user_id'])
            
            if not image_url or not file_location:
                error_message = language_manager.get_message(user_language, 'image_processing_error')
                elevenza_bot.send_messages(sender, error_message)
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
                json.dumps(nutrition_json) if nutrition_json else "{}"  # The complete structured data
            )
            
            if not success:
                logger.error(f"Failed to save nutrition analysis for user {user['user_id']}")
            else:
                logger.info(f"Successfully saved nutrition analysis for user {user['user_id']}")
            
            # Send the formatted analysis result to user
            elevenza_bot.send_messages(sender, user_message)
            
            # Optional: Send additional insights if health score is concerning
            if nutrition_json and nutrition_json.get('health_analysis', {}).get('health_score', 10) < 4:
                health_warning = get_health_warning_message(user_language)
                elevenza_bot.send_messages(sender, health_warning)
            
            # Send follow-up message
            followup_message = language_manager.get_message(user_language, 'followup_message')
            elevenza_bot.send_messages(sender, followup_message)
            
        except Exception as e:
            logger.error(f"Error processing 11za image: {e}")
            user_language = user.get('preferred_language', 'en')
            error_message = language_manager.get_message(user_language, 'image_processing_error')
            elevenza_bot.send_messages(sender, error_message)
            
    except Exception as e:
        logger.error(f"Error handling 11za media message: {e}")

def handle_11za_start_command(sender: str, user: Optional[Dict]):
    """Handle start/welcome command for 11za"""
    try:
        logger.info(f"DEBUG: handle_11za_start_command called")
        logger.info(f"DEBUG: sender: {sender}")
        logger.info(f"DEBUG: user: {safe_json_serialize(user) if user else 'None'}")  # FIXED
        logger.info(f"DEBUG: user evaluation: {bool(user)}")
        
        if user:
            # Existing user
            logger.info(f"DEBUG: Treating as existing user")
            user_language = user.get('preferred_language', 'en')
            logger.info(f"DEBUG: User language: {user_language}")
            welcome_message = language_manager.get_message(user_language, 'welcome')
            logger.info(f"DEBUG: Welcome message: {welcome_message}")
        else:
            # New user - start registration
            logger.info(f"DEBUG: Treating as new user - starting registration")
            welcome_message = language_manager.get_message('en', 'language_selection') + "\n\n" + language_manager.get_language_options_text()
            # Start registration session with language step
            db_manager.update_registration_session(sender, 'language', {})
            logger.info(f"DEBUG: Registration session created")
        
        logger.info(f"DEBUG: Sending message via elevenza_bot: {welcome_message}")
        elevenza_bot.send_messages(sender, welcome_message)
        
    except Exception as e:
        logger.error(f"Error handling 11za start command: {e}")


def handle_11za_help_command(sender: str, user: Optional[Dict]):
    """Handle help command for 11za"""
    try:
        user_language = user.get('preferred_language', 'en') if user else 'en'
        help_message = language_manager.get_message(user_language, 'help')
        elevenza_bot.send_messages(sender, help_message)
        
    except Exception as e:
        logger.error(f"Error handling 11za help command: {e}")

def handle_11za_language_command(sender: str):
    """Handle language selection command for 11za"""
    try:
        language_options = language_manager.get_language_options_text()
        elevenza_bot.send_messages(sender, language_options)
        
    except Exception as e:
        logger.error(f"Error handling 11za language command: {e}")

def handle_11za_language_selection(sender: str, text: str, user: Optional[Dict]):
    """Handle language selection from user for 11za"""
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
        
        elevenza_bot.send_messages(sender, confirmation_message)
        
    except Exception as e:
        logger.error(f"Error handling 11za language selection: {e}")

def handle_11za_registration_flow(sender: str, text: str):
    """Handle user registration flow for 11za - Language first, then name"""
    try:
        session = db_manager.get_registration_session(sender)
        
        if not session:
            # No session exists, check if it's a language selection
            if is_language_selection(text):
                handle_11za_language_selection(sender, text, None)
            else:
                # Start with language selection
                welcome_message = language_manager.get_message('en', 'language_selection') + "\n\n" + language_manager.get_language_options_text()
                elevenza_bot.send_messages(sender, welcome_message)
                db_manager.update_registration_session(sender, 'language', {})
            return
        
        current_step = session.get('current_step', 'language')
        temp_data = session.get('temp_data', {})
        
        if current_step == 'language':
            # Handle language selection
            if is_language_selection(text):
                handle_11za_language_selection(sender, text, None)
            else:
                invalid_message = language_manager.get_message('en', 'invalid_language') + "\n\n" + language_manager.get_language_options_text()
                elevenza_bot.send_messages(sender, invalid_message)
                
        elif current_step == 'name':
            # Handle name input
            if len(text.strip()) < 2:
                selected_language = temp_data.get('language', 'en')
                invalid_name_message = language_manager.get_message(selected_language, 'invalid_name')
                elevenza_bot.send_messages(sender, invalid_name_message)
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
                elevenza_bot.send_messages(sender, completion_message)
            else:
                failed_message = language_manager.get_message(selected_language, 'registration_failed')
                elevenza_bot.send_messages(sender, failed_message)
    
    except Exception as e:
        logger.error(f"Error in 11za registration flow: {e}")

# Lambda handler for 11za (if you want to deploy this specific part as Lambda)
def lambda_handler(event, context):
    """AWS Lambda handler for 11za webhook"""
    try:
        body = json.loads(event["body"])
        logger.info(f"Lambda received: {json.dumps(body)}")
        
        # Process the 11za message using existing infrastructure
        process_11za_message(body)
        
        return {
            "statusCode": 200,
            "body": json.dumps({"status": "processed"})
        }
        
    except Exception as e:
        logger.error(f"Lambda processing error: {e}")
        return {
            "statusCode": 500,
            "body": json.dumps({"status": "error", "message": str(e)})
        }

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
