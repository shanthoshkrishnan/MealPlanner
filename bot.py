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
        """Initialize database tables"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            
            # Create users table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY,
                    phone_number VARCHAR(20) UNIQUE NOT NULL,
                    name VARCHAR(100),
                    address TEXT,
                    preferred_language VARCHAR(10) DEFAULT 'en',
                    registration_status VARCHAR(20) DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            
            # Create nutrition_analysis table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS nutrition_analysis (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER REFERENCES users(id),
                    phone_number VARCHAR(20) NOT NULL,
                    image_url TEXT NOT NULL,
                    s3_key TEXT NOT NULL,
                    analysis_result TEXT,
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
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_nutrition_phone ON nutrition_analysis(phone_number);")
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
    
    def create_user(self, phone_number: str, name: str, address: str, language: str) -> bool:
        """Create new user"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            
            cursor.execute("""
                INSERT INTO users (phone_number, name, address, preferred_language, registration_status)
                VALUES (%s, %s, %s, %s, 'completed')
                ON CONFLICT (phone_number) 
                DO UPDATE SET 
                    name = EXCLUDED.name,
                    address = EXCLUDED.address,
                    preferred_language = EXCLUDED.preferred_language,
                    registration_status = 'completed',
                    updated_at = CURRENT_TIMESTAMP
            """, (phone_number, name, address, language))
            
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
    
    def save_nutrition_analysis(self, phone_number: str, image_url: str, s3_key: str, analysis_result: str) -> bool:
        """Save nutrition analysis to database"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            
            # Get user_id
            user = self.get_user_by_phone(phone_number)
            user_id = user['id'] if user else None
            
            cursor.execute("""
                INSERT INTO nutrition_analysis (user_id, phone_number, image_url, s3_key, analysis_result)
                VALUES (%s, %s, %s, %s, %s)
            """, (user_id, phone_number, image_url, s3_key, analysis_result))
            
            conn.commit()
            cursor.close()
            conn.close()
            
            return True
            
        except Exception as e:
            logger.error(f"Error saving nutrition analysis: {e}")
            return False

    def get_user_stats(self, phone_number: str) -> Dict:
        """Get user analysis statistics"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor(cursor_factory=RealDictCursor)
            
            cursor.execute("""
                SELECT COUNT(*) as total_analyses
                FROM nutrition_analysis 
                WHERE phone_number = %s
            """, (phone_number,))
            
            total_result = cursor.fetchone()
            
            cursor.execute("""
                SELECT DATE(created_at) as analysis_date, COUNT(*) as daily_count
                FROM nutrition_analysis 
                WHERE phone_number = %s 
                GROUP BY DATE(created_at)
                ORDER BY analysis_date DESC
                LIMIT 7
            """, (phone_number,))
            
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

class S3Manager:
    def __init__(self):
        self.s3_client = s3_client
        self.bucket_name = AWS_S3_BUCKET
    
    def upload_image(self, image_bytes: bytes, phone_number: str) -> tuple[Optional[str], Optional[str]]:
        """Upload image to S3 and return URL and key"""
        try:
            # Generate unique filename
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"nutrition_images/{phone_number}/{timestamp}_{uuid.uuid4().hex[:8]}.jpg"
            
            # Upload to S3
            self.s3_client.put_object(
                Bucket=self.bucket_name,
                Key=filename,
                Body=image_bytes,
                ContentType='image/jpeg'
            )
            
            # Generate URL
            image_url = f"https://{self.bucket_name}.s3.{AWS_REGION}.amazonaws.com/{filename}"
            
            return image_url, filename
            
        except ClientError as e:
            logger.error(f"S3 upload error: {e}")
            return None, None
        except Exception as e:
            logger.error(f"Unexpected S3 error: {e}")
            return None, None

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
                'registration_address': "Thank you! Now please enter your address:",
                'registration_language': "Great! Please select your preferred language for nutrition analysis:\n\n" + "\n".join([f"{code.upper()}. {name}" for code, name in [
                    ('en', 'English'),
                    ('ta', 'Tamil (தமிழ்)'),
                    ('te', 'Telugu (తెలుగు)'),
                    ('hi', 'Hindi (हिन्दी)'),
                    ('kn', 'Kannada (ಕನ್ನಡ)'),
                    ('ml', 'Malayalam (മലയാളം)')
                ]]) + "\n\nReply with the language code (e.g., 'EN' for English, 'TA' for Tamil)",
                'registration_complete': "✅ Registration completed successfully! You can now send me food photos for nutrition analysis.",
                'analyzing': "🔍 Analyzing your food image... This may take a few moments.",
                'help': "🆘 **How to use this bot:**\n\n1. Take a clear photo of your food\n2. Send the image to me\n3. Wait for the analysis (usually 10-30 seconds)\n4. Get detailed nutrition information!\n\n**Tips for best results:**\n• Take photos in good lighting\n• Show the food clearly from above\n• Include the whole serving if possible\n• One dish per photo works best\n\nSend me a food photo to get started! 📸"
            },
            'ta': {
                'welcome': "👋 வணக்கம்! நான் உங்கள் AI ஊட்டச்சத்து பகுப்பாய்வு பாட்!\n\n📸 எந்த உணவின் புகைப்படத்தையும் அனுப்புங்கள், நான் வழங்குவேன்:\n• விரிவான ஊட்டச்சத்து தகவல்\n• கலோரி எண்ணிக்கை மற்றும் மேக்ரோக்கள்\n• ஆரோக்கிய பகுப்பாய்வு மற்றும் குறிப்புகள்\n• மேம்படுத்தும் பரிந்துரைகள்\n\nஉங்கள் உணவின் தெளிவான புகைப்படத்தை எடுத்து அனுப்புங்கள! 🍽️",
                'analyzing': "🔍 உங்கள் உணவு படத்தை பகுப்பாய்வு செய்கிறேன்... இதற்கு சில நிமிடங்கள் ஆகலாம்.",
                'help': "🆘 **இந்த பாட்டை எப்படி பயன்படுத்துவது:**\n\n1. உங்கள் உணவின் தெளிவான புகைப்படத்தை எடுங்கள்\n2. படத்தை எனக்கு அனுப்புங்கள்\n3. பகுப்பாய்விற்காக காத்திருங்கள்\n4. விரிவான ஊட்டச்சத்து தகவலைப் பெறுங்கள்!\n\nதொடங்க எனக்கு உணவு புகைப்படம் ஒன்றை அனுப்புங்கள்! 📸"
            },
            'hi': {
                'welcome': "👋 नमस्ते! मैं आपका AI पोषण विश्लेषक बॉट हूँ!\n\n📸 मुझे किसी भी खाने की फोटो भेजें और मैं प्रदान करूंगा:\n• विस्तृत पोषण संबंधी जानकारी\n• कैलोरी गिनती और मैक्रोज़\n• स्वास्थ्य विश्लेषण और सुझाव\n• सुधार के सुझाव\n\nबस अपने भोजन की एक स्पष्ट तस्वीर लें और मुझे भेज दें! 🍽️",
                'analyzing': "🔍 आपकी खाने की तस्वीर का विश्लेषण कर रहा हूँ... इसमें कुछ समय लग सकता है।",
                'help': "🆘 **इस बॉट का उपयोग कैसे करें:**\n\n1. अपने खाने की स्पष्ट तस्वीर लें\n2. तस्वीर मुझे भेजें\n3. विश्लेषण का इंतजार करें\n4. विस्तृत पोषण जानकारी प्राप्त करें!\n\nशुरू करने के लिए मुझे खाने की तस्वीर भेजें! 📸"
            }
        }
    
    def get_message(self, language: str, key: str) -> str:
        """Get message in specified language"""
        return self.messages.get(language, self.messages['en']).get(key, self.messages['en'][key])
    
    def get_language_name(self, code: str) -> str:
        """Get language name by code"""
        return self.languages.get(code, 'English')

    def get_language_options_text(self) -> str:
        """Get formatted language options for user selection"""
        options = []
        for code, name in self.languages.items():
            options.append(f"*{code.upper()}* - {name}")
        
        return "🌍 *Please select your preferred language:*\n\n" + "\n".join(options) + "\n\n💬 *Reply with the language code* (e.g., EN, TA, HI)"

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
            unsupported_message = (
                "🤖 *I can only process:*\n"
                "📝 Text messages\n"
                "📸 Food images\n\n"
                "Please send me a *food photo* for nutrition analysis!\n\n"
                "Type '*help*' if you need assistance. 💡"
            )
            whatsapp_bot.send_message(sender, unsupported_message)
            
    except Exception as e:
        logger.error(f"Error processing message: {e}")
        try:
            whatsapp_bot.send_message(
                message.get('from', ''), 
                "❌ *Something went wrong!* Please try again in a moment. 🔄"
            )
        except:
            pass

def handle_text_message(message: Dict[str, Any]):
    """Handle text messages including registration flow"""
    sender = message.get('from')
    text_content = message.get('text', {}).get('body', '').strip()
    
    # Get user from database
    user = db_manager.get_user_by_phone(sender)
    
    if not user:
        # User not registered, handle registration flow
        handle_registration_flow(sender, text_content)
        return
    
    # Handle commands for registered users
    text_lower = text_content.lower()
    user_language = user.get('preferred_language', 'en')
    
    if text_lower in ['help', 'h', '?', 'info']:
        help_message = language_manager.get_message(user_language, 'help')
        whatsapp_bot.send_message(sender, help_message)
        
    elif text_lower in ['stats', 'statistics', 'my stats']:
        handle_stats_request(sender, user_language)
        
    elif text_lower in ['profile', 'my profile', 'info']:
        handle_profile_request(sender, user, user_language)
        
    elif text_lower in ['language', 'change language', 'lang']:
        handle_language_change_request(sender)
        
    elif text_lower.startswith('lang:') or text_lower.startswith('language:'):
        # Handle language change
        lang_code = text_lower.split(':')[1].strip().lower()
        handle_language_update(sender, lang_code)
        
    else:
        # Default response for unrecognized text
        welcome_message = language_manager.get_message(user_language, 'welcome')
        whatsapp_bot.send_message(sender, welcome_message)

def handle_registration_flow(sender: str, text_content: str):
    """Handle user registration process"""
    session = db_manager.get_registration_session(sender)
    
    if not session:
        # Start registration
        welcome_msg = language_manager.get_message('en', 'registration_name')
        whatsapp_bot.send_message(sender, welcome_msg)
        db_manager.update_registration_session(sender, 'name', {})
        return
    
    current_step = session.get('current_step')
    temp_data = session.get('temp_data', {})
    
    if current_step == 'name':
        # Validate name
        if len(text_content) < 2 or len(text_content) > 50:
            whatsapp_bot.send_message(sender, "❌ Please enter a valid name (2-50 characters):")
            return
            
        temp_data['name'] = text_content
        address_msg = language_manager.get_message('en', 'registration_address')
        whatsapp_bot.send_message(sender, address_msg)
        db_manager.update_registration_session(sender, 'address', temp_data)
        
    elif current_step == 'address':
        # Validate address
        if len(text_content) < 5 or len(text_content) > 200:
            whatsapp_bot.send_message(sender, "❌ Please enter a valid address (5-200 characters):")
            return
            
        temp_data['address'] = text_content
        language_msg = language_manager.get_message('en', 'registration_language')
        whatsapp_bot.send_message(sender, language_msg)
        db_manager.update_registration_session(sender, 'language', temp_data)
        
    elif current_step == 'language':
        # Validate language selection
        lang_code = text_content.lower().strip()
        valid_languages = ['en', 'ta', 'te', 'hi', 'kn', 'ml', 'mr', 'gu', 'bn']
        
        if lang_code not in valid_languages:
            whatsapp_bot.send_message(
                sender, 
                "❌ Invalid language code. Please choose from: EN, TA, TE, HI, KN, ML, MR, GU, BN"
            )
            return
        
        temp_data['language'] = lang_code
        
        # Complete registration
        success = db_manager.create_user(
            sender, 
            temp_data['name'], 
            temp_data['address'], 
            temp_data['language']
        )
        
        if success:
            complete_msg = language_manager.get_message(lang_code, 'registration_complete')
            whatsapp_bot.send_message(sender, complete_msg)
            
            # Send welcome message in chosen language
            welcome_msg = language_manager.get_message(lang_code, 'welcome')
            whatsapp_bot.send_message(sender, welcome_msg)
        else:
            whatsapp_bot.send_message(sender, "❌ Registration failed. Please try again later.")

def handle_image_message(message: Dict[str, Any]):
    """Handle image messages for nutrition analysis"""
    sender = message.get('from')
    image_data = message.get('image', {})
    media_id = image_data.get('id')
    
    if not media_id:
        whatsapp_bot.send_message(sender, "❌ No image found. Please send a valid food image.")
        return
    
    # Check if user is registered
    user = db_manager.get_user_by_phone(sender)
    if not user:
        welcome_msg = language_manager.get_message('en', 'registration_name')
        whatsapp_bot.send_message(sender, welcome_msg)
        db_manager.update_registration_session(sender, 'name', {})
        return
    
    user_language = user.get('preferred_language', 'en')
    
    try:
        # Send analyzing message
        analyzing_msg = language_manager.get_message(user_language, 'analyzing')
        whatsapp_bot.send_message(sender, analyzing_msg)
        
        # Download image
        image_bytes = whatsapp_bot.download_media(media_id)
        
        # Convert to PIL Image
        image = Image.open(io.BytesIO(image_bytes))
        
        # Resize if too large (to manage API limits)
        max_size = 1024
        if max(image.size) > max_size:
            image.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)
            
            # Convert back to bytes
            img_byte_arr = io.BytesIO()
            image.save(img_byte_arr, format='JPEG', quality=85)
            image_bytes = img_byte_arr.getvalue()
        
        # Upload to S3
        image_url, s3_key = s3_manager.upload_image(image_bytes, sender)
        
        if not image_url:
            whatsapp_bot.send_message(sender, "❌ Failed to process image. Please try again.")
            return
        
        # Analyze with Gemini
        analysis_result = analyzer.analyze_image(image, user_language)
        
        # Save to database
        db_manager.save_nutrition_analysis(sender, image_url, s3_key, analysis_result)
        
        # Send analysis result
        whatsapp_bot.send_message(sender, analysis_result)
        
        # Send follow-up message
        followup_msg = get_followup_message(user_language)
        whatsapp_bot.send_message(sender, followup_msg)
        
    except Exception as e:
        logger.error(f"Error processing image from {sender}: {e}")
        error_msg = get_error_message(user_language)
        whatsapp_bot.send_message(sender, error_msg)

def handle_stats_request(sender: str, language: str):
    """Handle user statistics request"""
    stats = db_manager.get_user_stats(sender)
    
    if language == 'ta':
        stats_msg = f"""📊 **உங்கள் ஊட்டச்சத்து பகுப்பாய்வு புள்ளிவிவரங்கள்**

🔢 **மொத்த பகுப்பாய்வுகள்:** {stats['total_analyses']}

📅 **சமீபத்திய செயல்பாடு:**"""
    elif language == 'hi':
        stats_msg = f"""📊 **आपके पोषण विश्लेषण आंकड़े**

🔢 **कुल विश्लेषण:** {stats['total_analyses']}

📅 **हाल की गतिविधि:**"""
    else:
        stats_msg = f"""📊 **Your Nutrition Analysis Statistics**

🔢 **Total Analyses:** {stats['total_analyses']}

📅 **Recent Activity:**"""
    
    if stats['recent_analyses']:
        for day_stat in stats['recent_analyses'][:5]:
            date_str = day_stat['analysis_date'].strftime('%Y-%m-%d')
            count = day_stat['daily_count']
            stats_msg += f"\n• {date_str}: {count} analysis{'es' if count > 1 else ''}"
    else:
        no_data_msg = "No recent activity" if language == 'en' else "சமீபத்திய செயல்பாடு இல்லை" if language == 'ta' else "कोई हाल की गतिविधि नहीं"
        stats_msg += f"\n{no_data_msg}"
    
    whatsapp_bot.send_message(sender, stats_msg)

def handle_profile_request(sender: str, user: Dict, language: str):
    """Handle user profile request"""
    name = user.get('name', 'Not set')
    address = user.get('address', 'Not set')
    lang_name = language_manager.get_language_name(user.get('preferred_language', 'en'))
    registration_date = user.get('created_at', '').strftime('%Y-%m-%d') if user.get('created_at') else 'Unknown'
    
    if language == 'ta':
        profile_msg = f"""👤 **உங்கள் சுயவிவரம்**

📛 **பெயர்:** {name}
📍 **முகவரி:** {address}
🌍 **மொழி:** {lang_name}
📅 **பதிவு தேதி:** {registration_date}

💡 மொழி மாற்ற 'language' என்று டைப் செய்யவும்"""
    elif language == 'hi':
        profile_msg = f"""👤 **आपकी प्रोफ़ाइल**

📛 **नाम:** {name}
📍 **पता:** {address}
🌍 **भाषा:** {lang_name}
📅 **पंजीकरण तिथि:** {registration_date}

💡 भाषा बदलने के लिए 'language' टाइप करें"""
    else:
        profile_msg = f"""👤 **Your Profile**

📛 **Name:** {name}
📍 **Address:** {address}
🌍 **Language:** {lang_name}
📅 **Registration Date:** {registration_date}

💡 Type 'language' to change your language preference"""
    
    whatsapp_bot.send_message(sender, profile_msg)

def handle_language_change_request(sender: str):
    """Handle language change request"""
    language_options = language_manager.get_language_options_text()
    instruction_msg = f"""{language_options}

💬 **Reply with:** `lang:CODE`
📝 **Example:** `lang:ta` for Tamil

Available codes: EN, TA, TE, HI, KN, ML, MR, GU, BN"""
    
    whatsapp_bot.send_message(sender, instruction_msg)

def handle_language_update(sender: str, lang_code: str):
    """Handle language preference update"""
    valid_languages = ['en', 'ta', 'te', 'hi', 'kn', 'ml', 'mr', 'gu', 'bn']
    
    if lang_code not in valid_languages:
        whatsapp_bot.send_message(sender, "❌ Invalid language code. Use: EN, TA, TE, HI, KN, ML, MR, GU, BN")
        return
    
    # Update user language in database
    try:
        conn = db_manager.get_connection()
        cursor = conn.cursor()
        
        cursor.execute(
            "UPDATE users SET preferred_language = %s, updated_at = CURRENT_TIMESTAMP WHERE phone_number = %s",
            (lang_code, sender)
        )
        
        conn.commit()
        cursor.close()
        conn.close()
        
        lang_name = language_manager.get_language_name(lang_code)
        success_msg = language_manager.get_message(lang_code, 'welcome')
        
        confirmation = f"✅ Language updated to {lang_name}!\n\n{success_msg}"
        whatsapp_bot.send_message(sender, confirmation)
        
    except Exception as e:
        logger.error(f"Error updating language for {sender}: {e}")
        whatsapp_bot.send_message(sender, "❌ Failed to update language. Please try again.")

def get_followup_message(language: str) -> str:
    """Get follow-up message after analysis"""
    messages = {
        'en': "✨ *Analysis complete!* Send another food photo anytime for more nutrition insights! 📸\n\nType '*help*' for assistance or '*stats*' to see your analysis history.",
        'ta': "✨ *பகுப்பாய்வு முடிந்தது!* மேலும் ஊட்டச்சத்து தகவல்களுக்கு எந்த நேரத்திலும் மற்றொரு உணவு புகைப்படத்தை அனுப்பவும்! 📸",
        'hi': "✨ *विश्लेषण पूरा!* अधिक पोषण जानकारी के लिए कभी भी दूसरी खाने की तस्वीर भेजें! 📸"
    }
    return messages.get(language, messages['en'])

def get_error_message(language: str) -> str:
    """Get error message in user's language"""
    messages = {
        'en': "❌ *Sorry, something went wrong!* 😔\n\n🔄 Please try again with:\n• A clearer photo\n• Better lighting\n• Food clearly visible\n\nType '*help*' if you need assistance!",
        'ta': "❌ *மன்னிக்கவும், ஏதோ தவறு நடந்தது!* 😔\n\n🔄 தயவுசெய்து மீண்டும் முயற்சிக்கவும்:\n• தெளிவான புகைப்படம்\n• சிறந்த வெளிச்சம்\n• உணவு தெளிவாக தெரியும்",
        'hi': "❌ *माफ़ करें, कुछ गलत हुआ!* 😔\n\n🔄 कृपया फिर से कोशिश करें:\n• स्पष्ट तस्वीर के साथ\n• बेहतर रोशनी में\n• खाना स्पष्ट रूप से दिखाई दे"
    }
    return messages.get(language, messages['en'])

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

