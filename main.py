import os
import discord
import time
import asyncio
import logging
import subprocess
import requests
import zipfile
import stat
import shutil
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from discord.ext import tasks

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Environment variables validation with better error messages
def validate_environment():
    """Validate required environment variables"""
    logger.info("=== ENVIRONMENT VALIDATION ===")
    
    # Wait a moment for environment to be fully loaded (Railway timing issue)
    time.sleep(1)
    
    # Get environment variables with multiple attempts
    discord_token = None
    voice_channel_id = None
    
    for attempt in range(3):
        discord_token = os.environ.get("DISCORD_BOT_TOKEN") or os.getenv("DISCORD_BOT_TOKEN")
        voice_channel_id = os.environ.get("VOICE_CHANNEL_ID") or os.getenv("VOICE_CHANNEL_ID")
        
        if discord_token and voice_channel_id:
            break
        
        logger.warning(f"⚠️ Attempt {attempt + 1}: Some variables not found, retrying...")
        time.sleep(2)
    
    # Debug ALL environment variables
    logger.info("=== ALL ENVIRONMENT VARIABLES ===")
    for key, value in sorted(os.environ.items()):
        if any(keyword in key.upper() for keyword in ['TOKEN', 'BOT', 'DISCORD', 'CHANNEL', 'VOICE']):
            # Show first 10 chars of sensitive values
            display_value = f"{value[:10]}..." if len(value) > 10 and 'TOKEN' in key else value
            logger.info(f"  {key} = {display_value}")
    logger.info("=" * 40)
    
    # Validate Discord token
    if not discord_token:
        logger.error("❌ DISCORD_BOT_TOKEN is missing!")
        logger.error("🔧 Variables found in Railway but not accessible:")
        logger.error("   - Try redeploying the service")
        logger.error("   - Check if variables are set at service level (not shared)")
        logger.error("   - Verify the variable names match exactly")
        raise ValueError("DISCORD_BOT_TOKEN environment variable is required")
    
    # Validate voice channel ID
    if not voice_channel_id:
        logger.error("❌ VOICE_CHANNEL_ID is missing!")
        logger.error("🔧 Variables found in Railway but not accessible:")
        logger.error("   - Try redeploying the service")
        logger.error("   - Check if variables are set at service level (not shared)")
        logger.error("   - Verify the variable names match exactly")
        raise ValueError("VOICE_CHANNEL_ID environment variable is required")
    
    # Validate channel ID format
    try:
        voice_channel_id = int(voice_channel_id)
    except ValueError:
        logger.error(f"❌ VOICE_CHANNEL_ID must be a valid integer. Got: '{voice_channel_id}'")
        raise ValueError("VOICE_CHANNEL_ID must be a valid integer")
    
    logger.info("✅ Environment variables validated successfully")
    logger.info(f"✅ Discord token: {discord_token[:10]}..." if len(discord_token) > 10 else "✅ Discord token: SET")
    logger.info(f"✅ Voice channel ID: {voice_channel_id}")
    
    return discord_token, voice_channel_id

# Validate environment on import
try:
    DISCORD_BOT_TOKEN, VOICE_CHANNEL_ID = validate_environment()
except Exception as e:
    logger.error(f"❌ Environment validation failed: {e}")
    logger.error("❌ Bot cannot start without proper environment variables")
    raise

# Setup Discord client
intents = discord.Intents.default()
intents.guilds = True
intents.voice_states = True
client = discord.Client(intents=intents)

last_price = None

def find_chrome_binary():
    """Find Chrome/Chromium binary location"""
    # Check if Railway provides a Chrome binary path
    railway_chrome = os.environ.get("GOOGLE_CHROME_BIN")
    if railway_chrome and os.path.exists(railway_chrome):
        logger.info(f"✅ Found Railway Chrome binary: {railway_chrome}")
        return railway_chrome
    
    possible_paths = [
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/snap/bin/chromium",
        "/usr/bin/chrome",
        "/opt/google/chrome/google-chrome",
        "/app/.chrome-for-testing/chrome-linux64/chrome"  # Railway buildpack location
    ]
    
    for path in possible_paths:
        if os.path.exists(path):
            logger.info(f"✅ Found Chrome binary: {path}")
            return path
    
    logger.error("❌ No Chrome binary found")
    logger.error("🔧 For Railway deployment, make sure you have Chrome buildpack:")
    logger.error("   Add this to your Railway service build settings or use a Chrome buildpack")
    return None

def get_chrome_version(chrome_path):
    """Get Chrome version and extract major version number"""
    try:
        result = subprocess.run([chrome_path, "--version"], 
                              capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            version_output = result.stdout.strip()
            logger.info(f"✅ Chrome version: {version_output}")
            
            # Extract version number (e.g., "Google Chrome 138.0.7204.183" -> "138.0.7204.183")
            version_parts = version_output.split()
            version_number = version_parts[-1]  # Get the last part which should be the version
            major_version = version_number.split('.')[0]  # Get major version (138)
            
            logger.info(f"✅ Chrome major version: {major_version}")
            return version_number, major_version
        else:
            logger.error(f"❌ Failed to get Chrome version: {result.stderr}")
            return None, None
    except Exception as e:
        logger.error(f"❌ Error getting Chrome version: {e}")
        return None, None

def download_compatible_chromedriver(major_version):
    """Download ChromeDriver compatible with Chrome version"""
    try:
        # Check if Railway provides chromedriver path
        railway_chromedriver = os.environ.get("CHROMEDRIVER_PATH")
        if railway_chromedriver and os.path.exists(railway_chromedriver):
            logger.info(f"✅ Using Railway ChromeDriver: {railway_chromedriver}")
            return railway_chromedriver
        
        # ChromeDriver directory
        driver_dir = "/tmp/chromedriver_new"
        driver_path = os.path.join(driver_dir, "chromedriver")
        
        # Remove old directory if exists
        if os.path.exists(driver_dir):
            shutil.rmtree(driver_dir)
        
        # Create fresh directory
        os.makedirs(driver_dir, exist_ok=True)
        
        logger.info(f"📥 Downloading ChromeDriver for Chrome {major_version}...")
        
        # Chrome 115+ uses new ChromeDriver API
        if int(major_version) >= 115:
            try:
                # Try to get the exact ChromeDriver version for this Chrome version
                api_url = f"https://googlechromelabs.github.io/chrome-for-testing/LATEST_RELEASE_{major_version}"
                logger.info(f"🔍 Checking API: {api_url}")
                
                response = requests.get(api_url, timeout=30)
                if response.status_code == 200:
                    driver_version = response.text.strip()
                    logger.info(f"✅ Found ChromeDriver version: {driver_version}")
                    download_url = f"https://storage.googleapis.com/chrome-for-testing-public/{driver_version}/linux64/chromedriver-linux64.zip"
                else:
                    logger.warning(f"⚠️ API returned {response.status_code}, using fallback version")
                    # Use a known working version for Chrome 138
                    if major_version == "138":
                        driver_version = "138.0.6906.100"
                    else:
                        driver_version = f"{major_version}.0.6000.0"
                    download_url = f"https://storage.googleapis.com/chrome-for-testing-public/{driver_version}/linux64/chromedriver-linux64.zip"
                    
            except Exception as e:
                logger.warning(f"⚠️ New API failed: {e}, using fallback")
                # Fallback version
                if major_version == "138":
                    driver_version = "138.0.6906.100"
                else:
                    driver_version = f"{major_version}.0.6000.0"
                download_url = f"https://storage.googleapis.com/chrome-for-testing-public/{driver_version}/linux64/chromedriver-linux64.zip"
        else:
            # Chrome 114 and below use old API
            api_url = f"https://chromedriver.storage.googleapis.com/LATEST_RELEASE_{major_version}"
            try:
                response = requests.get(api_url, timeout=30)
                if response.status_code == 200:
                    driver_version = response.text.strip()
                    download_url = f"https://chromedriver.storage.googleapis.com/{driver_version}/chromedriver_linux64.zip"
                else:
                    raise Exception(f"Old API returned status {response.status_code}")
            except Exception as e:
                logger.error(f"❌ Failed to get ChromeDriver version for Chrome {major_version}: {e}")
                return None
        
        logger.info(f"📥 Downloading ChromeDriver {driver_version} from: {download_url}")
        
        # Download ChromeDriver
        zip_path = os.path.join(driver_dir, "chromedriver.zip")
        
        try:
            response = requests.get(download_url, timeout=120)
            response.raise_for_status()
            
            with open(zip_path, 'wb') as f:
                f.write(response.content)
            
            logger.info("📂 Extracting ChromeDriver...")
            
            # Extract the zip file
            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                zip_ref.extractall(driver_dir)
            
            # Find the chromedriver executable in extracted files
            chromedriver_found = False
            for root, dirs, files in os.walk(driver_dir):
                for file in files:
                    if file == "chromedriver":
                        extracted_path = os.path.join(root, file)
                        # Move to expected location if not already there
                        if extracted_path != driver_path:
                            shutil.move(extracted_path, driver_path)
                        chromedriver_found = True
                        break
                if chromedriver_found:
                    break
            
            if not chromedriver_found:
                logger.error("❌ ChromeDriver executable not found in downloaded files")
                return None
            
            # Make executable
            os.chmod(driver_path, stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
            
            # Clean up zip file
            os.remove(zip_path)
            
            # Test the downloaded ChromeDriver
            logger.info("🧪 Testing downloaded ChromeDriver...")
            result = subprocess.run([driver_path, "--version"], 
                                  capture_output=True, text=True, timeout=10)
            if result.returncode == 0:
                logger.info(f"✅ ChromeDriver working: {result.stdout.strip()}")
                return driver_path
            else:
                logger.error(f"❌ Downloaded ChromeDriver test failed: {result.stderr}")
                return None
                
        except requests.RequestException as e:
            logger.error(f"❌ Failed to download ChromeDriver: {e}")
            return None
            
    except Exception as e:
        logger.error(f"❌ ChromeDriver download error: {e}")
        return None

def setup_chromedriver_and_chrome():
    """Setup ChromeDriver with automatic version matching"""
    try:
        # Find Chrome binary
        chrome_binary = find_chrome_binary()
        if not chrome_binary:
            logger.error("❌ Chrome binary not found")
            return None, None
        
        # Get Chrome version
        chrome_version, major_version = get_chrome_version(chrome_binary)
        if not chrome_version or not major_version:
            logger.error("❌ Could not determine Chrome version")
            return None, None
        
        # Always download a fresh ChromeDriver to ensure compatibility
        logger.info("📥 Downloading compatible ChromeDriver...")
        chromedriver_path = download_compatible_chromedriver(major_version)
        
        if not chromedriver_path:
            logger.error("❌ Could not download compatible ChromeDriver")
            return None, None
        
        logger.info(f"✅ ChromeDriver setup complete: {chromedriver_path}")
        return chromedriver_path, chrome_binary
            
    except Exception as e:
        logger.error(f"❌ ChromeDriver setup error: {e}")
        return None, None

def create_chrome_options(chrome_binary):
    """Create optimized Chrome options for Railway deployment"""
    options = Options()
    
    # Set binary location
    options.binary_location = chrome_binary
    
    # Essential options for headless operation in containerized environment
    options.add_argument("--headless=new")  # Use new headless mode
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-plugins")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--remote-debugging-port=9222")
    
    # Railway/Container specific options
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-software-rasterizer")
    options.add_argument("--disable-background-timer-throttling")
    options.add_argument("--disable-backgrounding-occluded-windows")
    options.add_argument("--disable-renderer-backgrounding")
    options.add_argument("--disable-features=TranslateUI")
    options.add_argument("--disable-features=VizDisplayCompositor")
    options.add_argument("--disable-ipc-flooding-protection")
    options.add_argument("--memory-pressure-off")
    
    # Reduce resource usage for Railway
    options.add_argument("--max_old_space_size=4096")
    options.add_argument("--disable-logging")
    options.add_argument("--disable-dev-tools")
    options.add_argument("--log-level=3")
    options.add_argument("--silent")
    
    # Anti-detection measures
    options.add_argument("--user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option('useAutomationExtension', False)
    
    return options

def fetch_price():
    """Fetch prANA price from Nirvana Finance"""
    driver = None
    
    try:
        # Setup ChromeDriver and Chrome
        chromedriver_path, chrome_binary = setup_chromedriver_and_chrome()
        if not chromedriver_path or not chrome_binary:
            logger.error("❌ Chrome/ChromeDriver setup failed")
            return None
        
        # Create Chrome options
        options = create_chrome_options(chrome_binary)
        
        # Create service
        service = Service(executable_path=chromedriver_path)
        
        # Initialize WebDriver
        logger.info("🚀 Starting Chrome WebDriver...")
        driver = webdriver.Chrome(service=service, options=options)
        
        # Set timeouts
        driver.set_page_load_timeout(90)
        driver.implicitly_wait(30)
        
        logger.info("🌐 Loading Nirvana Finance realize page...")
        driver.get("https://mainnet.nirvana.finance/realize")
        
        # Wait for page load
        logger.info("⏳ Waiting for page elements...")
        wait = WebDriverWait(driver, 90)
        
        # Try multiple selectors to find the price
        selectors_to_try = [
            ("CLASS_NAME", "DataPoint_dataPointValue__Bzf_E"),
            ("CSS_SELECTOR", "[class*='DataPoint_dataPointValue']"),
            ("CSS_SELECTOR", "[class*='dataPointValue']"),
            ("CSS_SELECTOR", "[data-testid*='price']"),
            ("CSS_SELECTOR", ".price-value"),
            ("CSS_SELECTOR", "[class*='price']"),
            ("XPATH", "//span[contains(@class, 'DataPoint')]"),
            ("XPATH", "//div[contains(@class, 'DataPoint')]//span"),
        ]
        
        price_text = None
        successful_selector = None
        
        for selector_type, selector in selectors_to_try:
            try:
                logger.info(f"🔍 Trying {selector_type}: {selector}")
                
                if selector_type == "CLASS_NAME":
                    element = wait.until(EC.presence_of_element_located((By.CLASS_NAME, selector)))
                elif selector_type == "CSS_SELECTOR":
                    element = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, selector)))
                elif selector_type == "XPATH":
                    element = wait.until(EC.presence_of_element_located((By.XPATH, selector)))
                
                # Wait a bit more for dynamic content
                time.sleep(5)
                
                # Get text content
                price_text = element.text.strip()
                logger.info(f"📝 Found text with {selector_type} '{selector}': '{price_text}'")
                
                if price_text:
                    successful_selector = f"{selector_type}: {selector}"
                    break
                    
            except Exception as e:
                logger.debug(f"⚠️ {selector_type} '{selector}' failed: {e}")
                continue
        
        # Process the found price text
        if price_text:
            logger.info(f"✅ Price found using {successful_selector}")
            
            # Clean the price text
            original_price = price_text
            cleaned_price = price_text.replace("USDC", "").replace("$", "").replace(",", "").strip()
            
            logger.info(f"🧹 Cleaned '{original_price}' to '{cleaned_price}'")
            
            if cleaned_price:
                try:
                    # Validate it's a valid number
                    price_float = float(cleaned_price)
                    logger.info(f"✅ Valid price extracted: {cleaned_price} (${price_float:.4f})")
                    return cleaned_price
                except ValueError:
                    logger.warning(f"⚠️ Invalid number format: '{cleaned_price}'")
                    return None
            else:
                logger.warning("⚠️ Price text empty after cleaning")
                return None
        else:
            logger.warning("⚠️ No price found with any selector")
            
            # Debug: take screenshot and log page info
            try:
                screenshot_path = "/tmp/debug_screenshot.png"
                driver.save_screenshot(screenshot_path)
                logger.info(f"📸 Debug screenshot: {screenshot_path}")
                
                # Check if page loaded correctly
                page_title = driver.title
                current_url = driver.current_url
                page_source_length = len(driver.page_source)
                
                logger.info(f"📄 Page title: '{page_title}'")
                logger.info(f"🔗 Current URL: {current_url}")
                logger.info(f"📊 Page source length: {page_source_length} chars")
                
                # Look for any DataPoint mentions in source
                if "DataPoint" in driver.page_source:
                    logger.info("✅ Found 'DataPoint' in page source")
                else:
                    logger.warning("⚠️ No 'DataPoint' found in page source")
                
            except Exception as debug_error:
                logger.warning(f"⚠️ Debug info failed: {debug_error}")
            
            return None
            
    except Exception as e:
        logger.error(f"❌ Price fetch failed: {str(e)}")
        logger.error(f"❌ Error type: {type(e).__name__}")
        return None
        
    finally:
        if driver:
            try:
                driver.quit()
                logger.info("🔄 Chrome WebDriver closed")
            except Exception as close_error:
                logger.warning(f"⚠️ Error closing WebDriver: {close_error}")

@tasks.loop(seconds=120)  # Every 2 minutes
async def update_bot_status():
    """Update bot status and channel name"""
    global last_price
    
    if not client.is_ready():
        logger.info("⏳ Bot not ready, skipping update...")
        return
    
    try:
        logger.info("🔄 Starting price update...")
        
        # Fetch price in executor to avoid blocking
        loop = asyncio.get_event_loop()
        price = await loop.run_in_executor(None, fetch_price)
        
        if price:
            if price != last_price:
                logger.info(f"📈 Price update: {last_price} → {price}")
                
                # Update bot status
                try:
                    await client.change_presence(activity=discord.Game(name=f"prANA: ${price}"))
                    logger.info(f"✅ Bot status updated: prANA: ${price}")
                except Exception as status_error:
                    logger.error(f"❌ Status update failed: {status_error}")
                
                # Update voice channel
                channel = client.get_channel(VOICE_CHANNEL_ID)
                if channel and isinstance(channel, discord.VoiceChannel):
                    try:
                        channel_name = f"prANA: ${price}"
                        await channel.edit(name=channel_name)
                        logger.info(f"🔁 Channel updated: {channel_name}")
                        last_price = price
                    except discord.Forbidden:
                        logger.error("❌ No permission to edit channel")
                    except discord.HTTPException as http_error:
                        if "rate limited" in str(http_error).lower():
                            logger.warning("⚠️ Rate limited, will retry next cycle")
                        else:
                            logger.error(f"❌ Channel edit failed: {http_error}")
                    except Exception as channel_error:
                        logger.error(f"❌ Channel update error: {channel_error}")
                else:
                    logger.warning(f"⚠️ Channel {VOICE_CHANNEL_ID} not found or invalid")
            else:
                logger.info(f"⏸️ Price unchanged: ${price}")
        else:
            logger.warning("⏸️ Price fetch failed, will retry next cycle")
            
    except Exception as update_error:
        logger.error(f"⚠️ Update cycle error: {update_error}")

@client.event
async def on_ready():
    """Bot ready event"""
    logger.info(f"✅ Bot logged in: {client.user}")
    logger.info(f"🎯 Target channel ID: {VOICE_CHANNEL_ID}")
    logger.info(f"🏠 Connected to {len(client.guilds)} servers")
    
    # Verify target channel
    channel = client.get_channel(VOICE_CHANNEL_ID)
    if channel:
        if isinstance(channel, discord.VoiceChannel):
            logger.info(f"✅ Target channel: '{channel.name}' in '{channel.guild.name}'")
        else:
            logger.error(f"❌ Channel {VOICE_CHANNEL_ID} is not a voice channel!")
    else:
        logger.error(f"❌ Channel {VOICE_CHANNEL_ID} not found!")
    
    # Test system setup
    logger.info("🧪 Testing system setup...")
    chrome_binary = find_chrome_binary()
    if chrome_binary:
        get_chrome_version(chrome_binary)
    
    # Start update loop
    logger.info("🚀 Starting price monitoring...")
    update_bot_status.start()

@client.event
async def on_disconnect():
    logger.warning("⚠️ Discord disconnected")

@client.event
async def on_resumed():
    logger.info("🔄 Discord reconnected")

@client.event
async def on_error(event, *args, **kwargs):
    logger.error(f"❌ Discord error in {event}")

def main():
    """Main function"""
    logger.info("🚀 prANA Price Bot Starting...")
    logger.info(f"🐍 Python: {os.sys.version}")
    logger.info(f"📁 Working dir: {os.getcwd()}")
    logger.info(f"🚂 Platform: Railway" if "RAILWAY_ENVIRONMENT" in os.environ else "🖥️ Platform: Local")
    
    # Environment is already validated during import
    logger.info("✅ Environment validation passed")
    
    # Start bot
    try:
        logger.info("🤖 Starting Discord bot...")
        client.run(DISCORD_BOT_TOKEN)
    except KeyboardInterrupt:
        logger.info("👋 Bot stopped by user")
    except Exception as start_error:
        logger.error(f"❌ Bot start failed: {start_error}")
        raise

if __name__ == "__main__":
    main()
