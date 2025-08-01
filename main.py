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
import random
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, WebDriverException, NoSuchElementException
from discord.ext import tasks

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Environment variables validation with comprehensive debugging
def validate_environment():
    """Validate required environment variables"""
    logger.info("=== COMPREHENSIVE ENVIRONMENT DEBUG ===")
    
    # Print ALL environment variables for debugging
    logger.info("ALL ENVIRONMENT VARIABLES:")
    env_vars = dict(os.environ)
    for key in sorted(env_vars.keys()):
        value = env_vars[key]
        # Hide sensitive values but show they exist
        if 'TOKEN' in key.upper() or 'SECRET' in key.upper() or 'KEY' in key.upper():
            display_value = f"***HIDDEN*** (length: {len(value)})"
        else:
            display_value = value
        logger.info(f"  {key} = {display_value}")
    
    logger.info("=" * 50)
    
    # Multiple methods to get variables
    methods = [
        ("os.environ.get", lambda k: os.environ.get(k)),
        ("os.getenv", lambda k: os.getenv(k)),
        ("direct os.environ", lambda k: os.environ[k] if k in os.environ else None),
    ]
    
    discord_token = None
    voice_channel_id = None
    
    for method_name, method_func in methods:
        logger.info(f"Trying {method_name}:")
        try:
            token = method_func("DISCORD_BOT_TOKEN")
            channel = method_func("VOICE_CHANNEL_ID")
            logger.info(f"  DISCORD_BOT_TOKEN: {'FOUND' if token else 'NOT FOUND'}")
            logger.info(f"  VOICE_CHANNEL_ID: {'FOUND' if channel else 'NOT FOUND'}")
            
            if token and not discord_token:
                discord_token = token
            if channel and not voice_channel_id:
                voice_channel_id = channel
                
        except Exception as e:
            logger.error(f"  Error with {method_name}: {e}")
    
    # Try reading from potential Railway config files
    potential_files = [
        "/app/.env",
        "/etc/environment",
        "/proc/self/environ"
    ]
    
    for file_path in potential_files:
        if os.path.exists(file_path):
            logger.info(f"Found config file: {file_path}")
            try:
                with open(file_path, 'r') as f:
                    content = f.read()
                    if 'DISCORD_BOT_TOKEN' in content:
                        logger.info(f"  DISCORD_BOT_TOKEN found in {file_path}")
                    if 'VOICE_CHANNEL_ID' in content:
                        logger.info(f"  VOICE_CHANNEL_ID found in {file_path}")
            except Exception as e:
                logger.warning(f"  Could not read {file_path}: {e}")
    
    # Final validation
    if not discord_token:
        logger.error("❌ DISCORD_BOT_TOKEN could not be found with any method!")
        logger.error("🔧 DEBUG STEPS:")
        logger.error("   1. Check if Railway variables are really set")
        logger.error("   2. Try deleting and re-adding the variables")
        logger.error("   3. Make sure you're deploying to the right service/environment")
        logger.error("   4. Check Railway documentation for environment variable issues")
        
        # Last resort: try to find any variable that might be the token
        logger.info("🔍 Searching for potential token variables:")
        for key, value in os.environ.items():
            if len(value) > 50 and ('.' in value or len(value) > 60):  # Token-like pattern
                logger.info(f"  Potential token found in {key} (length: {len(value)})")
        
        raise ValueError("DISCORD_BOT_TOKEN environment variable is required")
    
    if not voice_channel_id:
        logger.error("❌ VOICE_CHANNEL_ID could not be found with any method!")
        raise ValueError("VOICE_CHANNEL_ID environment variable is required")
    
    # Validate channel ID format
    try:
        voice_channel_id = int(voice_channel_id)
    except ValueError:
        logger.error(f"❌ VOICE_CHANNEL_ID must be a valid integer. Got: '{voice_channel_id}'")
        raise ValueError("VOICE_CHANNEL_ID must be a valid integer")
    
    logger.info("✅ Environment variables validated successfully")
    logger.info(f"✅ Discord token: {discord_token[:10]}...{discord_token[-4:]} (length: {len(discord_token)})")
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
fetch_stats = {"success": 0, "failures": 0, "consecutive_failures": 0}

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

def create_chrome_options(chrome_binary, user_agent_variant=None):
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
    
    # Anti-detection measures with optional user agent variation
    user_agents = [
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36"
    ]
    
    if user_agent_variant is not None and user_agent_variant < len(user_agents):
        selected_ua = user_agents[user_agent_variant]
    else:
        selected_ua = random.choice(user_agents)
    
    options.add_argument(f"--user-agent={selected_ua}")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option('useAutomationExtension', False)
    
    return options

def wait_for_page_load(driver, timeout=180):
    """Wait for JavaScript to complete and page to be fully loaded"""
    try:
        logger.info("⏳ Waiting for page to fully load...")
        
        # Wait for document ready state
        WebDriverWait(driver, timeout).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )
        
        # Wait for jQuery if it exists
        try:
            WebDriverWait(driver, 10).until(
                lambda d: d.execute_script("return typeof jQuery === 'undefined' || jQuery.active === 0")
            )
            logger.info("✅ jQuery operations completed")
        except TimeoutException:
            logger.info("ℹ️ No jQuery detected or still active")
        
        # Wait for React/Angular if they exist
        try:
            WebDriverWait(driver, 10).until(
                lambda d: d.execute_script("""
                    return (typeof React === 'undefined' || 
                           typeof angular === 'undefined' || 
                           (window.getAllAngularTestabilities && 
                            window.getAllAngularTestabilities().findIndex(x=>!x.isStable()) === -1))
                """)
            )
            logger.info("✅ React/Angular operations completed")
        except TimeoutException:
            logger.info("ℹ️ React/Angular still processing or not detected")
        
        # Additional wait for dynamic content
        time.sleep(10)
        logger.info("✅ Page load verification completed")
        return True
        
    except TimeoutException:
        logger.warning(f"⚠️ Page load timeout after {timeout}s, continuing anyway...")
        return False
    except Exception as e:
        logger.warning(f"⚠️ Page load verification error: {e}")
        return False

def fetch_price_attempt(attempt_num=1, max_attempts=3):
    """Single attempt to fetch price with comprehensive error handling"""
    driver = None
    
    try:
        logger.info(f"🔄 Price fetch attempt {attempt_num}/{max_attempts}")
        
        # Setup ChromeDriver and Chrome
        chromedriver_path, chrome_binary = setup_chromedriver_and_chrome()
        if not chromedriver_path or not chrome_binary:
            logger.error("❌ Chrome/ChromeDriver setup failed")
            return None
        
        # Create Chrome options with slight variations for retry attempts
        user_agent_variant = (attempt_num - 1) % 3  # Rotate user agents
        options = create_chrome_options(chrome_binary, user_agent_variant)
        
        # Create service
        service = Service(executable_path=chromedriver_path)
        
        # Initialize WebDriver
        logger.info("🚀 Starting Chrome WebDriver...")
        driver = webdriver.Chrome(service=service, options=options)
        
        # Set increased timeouts for better reliability
        driver.set_page_load_timeout(180)  # Increased from 90 to 180 seconds
        driver.implicitly_wait(30)
        
        logger.info("🌐 Loading Nirvana Finance realize page...")
        driver.get("https://mainnet.nirvana.finance/realize")
        
        # Comprehensive page load waiting
        if not wait_for_page_load(driver, timeout=180):
            logger.warning("⚠️ Page load verification failed but continuing...")
        
        # Wait for page elements with extended timeout
        logger.info("⏳ Waiting for price elements...")
        wait = WebDriverWait(driver, 120)  # Increased from 90 to 120 seconds
        
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
            ("XPATH", "//span[contains(text(), '$')]"),
            ("XPATH", "//div[contains(text(), 'USDC')]"),
        ]
        
        price_text = None
        successful_selector = None
        
        for selector_type, selector in selectors_to_try:
            try:
                logger.info(f"🔍 Trying {selector_type}: {selector}")
                
                # Use presence_of_element_located with extended wait
                if selector_type == "CLASS_NAME":
                    element = wait.until(EC.presence_of_element_located((By.CLASS_NAME, selector)))
                elif selector_type == "CSS_SELECTOR":
                    element = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, selector)))
                elif selector_type == "XPATH":
                    element = wait.until(EC.presence_of_element_located((By.XPATH, selector)))
                
                # Wait for element to be visible and have text
                WebDriverWait(driver, 30).until(EC.visibility_of(element))
                
                # Additional wait for dynamic content
                time.sleep(10)
                
                # Get text content
                price_text = element.text.strip()
                logger.info(f"📝 Found text with {selector_type} '{selector}': '{price_text}'")
                
                if price_text and len(price_text) > 0:
                    successful_selector = f"{selector_type}: {selector}"
                    break
                    
            except TimeoutException:
                logger.debug(f"⏰ {selector_type} '{selector}' timed out")
                continue
            except NoSuchElementException:
                logger.debug(f"🔍 {selector_type} '{selector}' element not found")
                continue
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
            
            # Enhanced debug information (skip screenshot to avoid timeout issues)
            try:
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
                    # Count occurrences
                    datapoint_count = driver.page_source.count("DataPoint")
                    logger.info(f"📊 DataPoint occurrences: {datapoint_count}")
                else:
                    logger.warning("⚠️ No 'DataPoint' found in page source")
                
                # Check for other potential price indicators
                price_indicators = ["$", "USDC", "price", "Price"]
                for indicator in price_indicators:
                    if indicator in driver.page_source:
                        count = driver.page_source.count(indicator)
                        logger.info(f"💰 Found '{indicator}' {count} times in page source")
                
            except Exception as debug_error:
                logger.warning(f"⚠️ Debug info failed: {debug_error}")
            
            return None
            
    except TimeoutException as e:
        logger.error(f"⏰ Timeout in attempt {attempt_num}: {str(e)}")
        return None
    except WebDriverException as e:
        logger.error(f"🌐 WebDriver error in attempt {attempt_num}: {str(e)}")
        return None
    except Exception as e:
        logger.error(f"❌ Unexpected error in attempt {attempt_num}: {str(e)}")
        logger.error(f"❌ Error type: {type(e).__name__}")
        return None
        
    finally:
        if driver:
            try:
                driver.quit()
                logger.info(f"🔄 Chrome WebDriver closed (attempt {attempt_num})")
            except Exception as close_error:
                logger.warning(f"⚠️ Error closing WebDriver: {close_error}")

def fetch_price():
    """Fetch prANA price with retry logic and fallback strategies"""
    global fetch_stats
    
    max_attempts = 3
    base_delay = 30  # Base delay between attempts
    
    for attempt in range(1, max_attempts + 1):
        try:
            logger.info(f"🎯 Starting price fetch attempt {attempt}/{max_attempts}")
            
            # Add random delay to avoid being too predictable
            if attempt > 1:
                delay = base_delay + random.randint(10, 30)
                logger.info(f"⏳ Waiting {delay} seconds before retry...")
                time.sleep(delay)
            
            # Attempt to fetch price
            price = fetch_price_attempt(attempt, max_attempts)
            
            if price:
                logger.info(f"✅ Price fetch succeeded on attempt {attempt}")
                fetch_stats["success"] += 1
                fetch_stats["consecutive_failures"] = 0
                return price
            else:
                logger.warning(f"❌ Price fetch failed on attempt {attempt}")
                fetch_stats["failures"] += 1
                fetch_stats["consecutive_failures"] += 1
                
                # If not the last attempt, continue to retry
                if attempt < max_attempts:
                    logger.info(f"🔄 Will retry... ({max_attempts - attempt} attempts remaining)")
                    continue
                else:
                    logger.error(f"❌ All {max_attempts} attempts failed")
                    break
                    
        except Exception as e:
            logger.error(f"❌ Critical error in attempt {attempt}: {e}")
            fetch_stats["failures"] += 1
            fetch_stats["consecutive_failures"] += 1
            
            if attempt < max_attempts:
                logger.info(f"🔄 Continuing to next attempt due to critical error...")
                continue
            else:
                logger.error(f"❌ Critical error on final attempt")
                break
    
    # Log statistics
    total_attempts = fetch_stats["success"] + fetch_stats["failures"]
    if total_attempts > 0:
        success_rate = (fetch_stats["success"] / total_attempts) * 100
        logger.info(f"📊 Fetch statistics - Success: {fetch_stats['success']}, "
                   f"Failures: {fetch_stats['failures']}, "
                   f"Success rate: {success_rate:.1f}%, "
                   f"Consecutive failures: {fetch_stats['consecutive_failures']}")
    
    return None

@tasks.loop(seconds=180)  # Increased from 120 to 180 seconds (3 minutes)
async def update_bot_status():
    """Update bot status and channel name with improved error handling"""
    global last_price
    
    if not client.is_ready():
        logger.info("⏳ Bot not ready, skipping update...")
        return
    
    try:
        logger.info("🔄 Starting price update cycle...")
        
        # Add adaptive delay based on consecutive failures
        if fetch_stats["consecutive_failures"] >= 3:
            additional_delay = min(fetch_stats["consecutive_failures"] * 30, 300)  # Max 5 min
            logger.info(f"⏳ Adding {additional_delay}s delay due to {fetch_stats['consecutive_failures']} consecutive failures...")
            await asyncio.sleep(additional_delay)
        
        # Fetch price in executor to avoid blocking
        loop = asyncio.get_event_loop()
        price = await loop.run_in_executor(None, fetch_price)
        
        if price:
            if price != last_price:
                logger.info(f"📈 Price update: {last_price} → {price}")
                
                # Update bot status
                try:
                    await client.change_presence(activity=discord.Game(name=f"📊prANA Price: ${price}"))
                    logger.info(f"✅ Bot status updated: 📊prANA Price: ${price}")
                except Exception as status_error:
                    logger.error(f"❌ Status update failed: {status_error}")
                
                # Update voice channel
                channel = client.get_channel(VOICE_CHANNEL_ID)
                if channel and isinstance(channel, discord.VoiceChannel):
                    try:
                        channel_name = f"📊prANA Price: ${price}"
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
            
            # Consider longer delay if many failures
            if fetch_stats["consecutive_failures"] >= 5:
                logger.info("🔄 Many consecutive failures, extending next cycle delay...")
                
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
    # Reset consecutive failures on reconnect
    fetch_stats["consecutive_failures"] = 0

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
