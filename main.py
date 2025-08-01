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
import psutil
import gc
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

# Environment variables validation
def validate_environment():
    """Validate required environment variables"""
    logger.info("=== ENVIRONMENT VALIDATION ===")
    
    discord_token = os.environ.get("DISCORD_BOT_TOKEN")
    voice_channel_id = os.environ.get("VOICE_CHANNEL_ID")
    
    if not discord_token:
        raise ValueError("DISCORD_BOT_TOKEN environment variable is required")
    
    if not voice_channel_id:
        raise ValueError("VOICE_CHANNEL_ID environment variable is required")
    
    try:
        voice_channel_id = int(voice_channel_id)
    except ValueError:
        raise ValueError("VOICE_CHANNEL_ID must be a valid integer")
    
    logger.info("✅ Environment variables validated")
    return discord_token, voice_channel_id

# Validate environment
DISCORD_BOT_TOKEN, VOICE_CHANNEL_ID = validate_environment()

# Setup Discord client
intents = discord.Intents.default()
intents.guilds = True
intents.voice_states = True
client = discord.Client(intents=intents)

last_price = None
driver_instance = None

def log_system_resources():
    """Log current system resource usage"""
    try:
        memory = psutil.virtual_memory()
        cpu = psutil.cpu_percent(interval=1)
        logger.info(f"💾 Memory: {memory.percent:.1f}% used ({memory.used//1024//1024}MB/{memory.total//1024//1024}MB)")
        logger.info(f"🖥️ CPU: {cpu:.1f}%")
        
        # Count Chrome processes
        chrome_processes = []
        for proc in psutil.process_iter(['pid', 'name', 'memory_info']):
            try:
                if 'chrome' in proc.info['name'].lower() or 'chromium' in proc.info['name'].lower():
                    chrome_processes.append(proc)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        
        if chrome_processes:
            total_chrome_memory = sum(proc.info['memory_info'].rss for proc in chrome_processes)
            logger.info(f"🌐 Chrome processes: {len(chrome_processes)} (using {total_chrome_memory//1024//1024}MB)")
        
    except Exception as e:
        logger.warning(f"⚠️ Resource logging failed: {e}")

def cleanup_chrome_processes():
    """Aggressively cleanup Chrome processes"""
    try:
        logger.info("🧹 Cleaning up Chrome processes...")
        
        # Kill any remaining Chrome processes
        for proc in psutil.process_iter(['pid', 'name']):
            try:
                if any(name in proc.info['name'].lower() for name in ['chrome', 'chromium', 'chromedriver']):
                    logger.info(f"🔪 Killing {proc.info['name']} (PID: {proc.info['pid']})")
                    proc.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        
        # Wait for processes to die
        time.sleep(2)
        
        # Clean up temp directories
        temp_dirs = ['/tmp/chrome_temp', '/tmp/chromedriver_new', '/tmp/.com.google.Chrome.*']
        for temp_dir in temp_dirs:
            if os.path.exists(temp_dir):
                try:
                    shutil.rmtree(temp_dir)
                    logger.info(f"🗑️ Cleaned temp dir: {temp_dir}")
                except Exception as e:
                    logger.warning(f"⚠️ Failed to clean {temp_dir}: {e}")
        
        # Force garbage collection
        gc.collect()
        
        logger.info("✅ Chrome cleanup complete")
        
    except Exception as e:
        logger.warning(f"⚠️ Chrome cleanup failed: {e}")

def find_chrome_binary():
    """Find Chrome binary with Railway optimization"""
    # Check Railway environment variable first
    railway_chrome = os.environ.get("GOOGLE_CHROME_BIN")
    if railway_chrome and os.path.exists(railway_chrome):
        logger.info(f"✅ Using Railway Chrome: {railway_chrome}")
        return railway_chrome
    
    # Standard paths
    possible_paths = [
        "/usr/bin/google-chrome-stable",
        "/usr/bin/google-chrome",
        "/usr/bin/chromium-browser",
        "/usr/bin/chromium",
        "/opt/google/chrome/google-chrome",
    ]
    
    for path in possible_paths:
        if os.path.exists(path):
            logger.info(f"✅ Found Chrome binary: {path}")
            return path
    
    logger.error("❌ No Chrome binary found")
    return None

def setup_chromedriver():
    """Setup ChromeDriver with Railway optimization"""
    try:
        # Check if Railway provides chromedriver
        railway_chromedriver = os.environ.get("CHROMEDRIVER_PATH")
        if railway_chromedriver and os.path.exists(railway_chromedriver):
            logger.info(f"✅ Using Railway ChromeDriver: {railway_chromedriver}")
            return railway_chromedriver
        
        # Use system chromedriver if available
        system_paths = ["/usr/bin/chromedriver", "/usr/local/bin/chromedriver"]
        for path in system_paths:
            if os.path.exists(path):
                logger.info(f"✅ Using system ChromeDriver: {path}")
                return path
        
        logger.error("❌ No ChromeDriver found")
        return None
        
    except Exception as e:
        logger.error(f"❌ ChromeDriver setup error: {e}")
        return None

def create_ultra_lightweight_chrome_options(chrome_binary):
    """Create extremely lightweight Chrome options for Railway"""
    options = Options()
    
    # Set binary
    options.binary_location = chrome_binary
    
    # Headless and basic options
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    
    # Extreme memory optimization
    options.add_argument("--memory-pressure-off")
    options.add_argument("--max_old_space_size=512")
    options.add_argument("--disable-background-timer-throttling")
    options.add_argument("--disable-backgrounding-occluded-windows")
    options.add_argument("--disable-renderer-backgrounding")
    options.add_argument("--disable-features=TranslateUI,BlinkGenPropertyTrees,VizDisplayCompositor")
    
    # Disable everything possible
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-plugins")
    options.add_argument("--disable-images")
    options.add_argument("--disable-javascript")  # We'll enable this only if needed
    options.add_argument("--disable-css")
    options.add_argument("--disable-web-security")
    options.add_argument("--disable-features=VizDisplayCompositor")
    
    # Minimal window
    options.add_argument("--window-size=800,600")
    options.add_argument("--disable-logging")
    options.add_argument("--silent")
    options.add_argument("--log-level=3")
    
    # Single process mode to reduce memory
    options.add_argument("--single-process")
    options.add_argument("--disable-dev-tools")
    options.add_argument("--no-zygote")
    
    # Custom user data directory
    temp_dir = "/tmp/chrome_temp"
    if os.path.exists(temp_dir):
        shutil.rmtree(temp_dir)
    os.makedirs(temp_dir, exist_ok=True)
    options.add_argument(f"--user-data-dir={temp_dir}")
    
    # Anti-detection (minimal)
    options.add_argument("--user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option('useAutomationExtension', False)
    
    return options

def fetch_price_with_retry(max_retries=2):
    """Fetch price with retry logic and aggressive cleanup"""
    global driver_instance
    
    for attempt in range(max_retries):
        driver = None
        try:
            logger.info(f"🔄 Price fetch attempt {attempt + 1}/{max_retries}")
            
            # Log resources before starting
            log_system_resources()
            
            # Cleanup any existing processes
            cleanup_chrome_processes()
            
            # Setup Chrome
            chrome_binary = find_chrome_binary()
            chromedriver_path = setup_chromedriver()
            
            if not chrome_binary or not chromedriver_path:
                logger.error("❌ Chrome setup failed")
                continue
            
            # Create options
            options = create_ultra_lightweight_chrome_options(chrome_binary)
            service = Service(executable_path=chromedriver_path)
            
            # Set service timeout
            service.start_error_message = "ChromeDriver failed to start"
            
            logger.info("🚀 Starting Chrome (ultra-lightweight mode)...")
            driver = webdriver.Chrome(service=service, options=options)
            driver_instance = driver
            
            # Set aggressive timeouts
            driver.set_page_load_timeout(30)
            driver.implicitly_wait(10)
            
            logger.info("🌐 Loading Nirvana Finance...")
            driver.get("https://mainnet.nirvana.finance/realize")
            
            # Wait and look for price
            wait = WebDriverWait(driver, 20)
            
            # Try to find price element
            price_selectors = [
                (By.CLASS_NAME, "DataPoint_dataPointValue__Bzf_E"),
                (By.CSS_SELECTOR, "[class*='DataPoint_dataPointValue']"),
                (By.CSS_SELECTOR, "[class*='dataPointValue']"),
            ]
            
            price_text = None
            for selector_type, selector in price_selectors:
                try:
                    element = wait.until(EC.presence_of_element_located((selector_type, selector)))
                    time.sleep(3)  # Wait for dynamic content
                    price_text = element.text.strip()
                    if price_text:
                        break
                except:
                    continue
            
            if price_text:
                # Clean price
                cleaned_price = price_text.replace("USDC", "").replace("$", "").replace(",", "").strip()
                if cleaned_price:
                    try:
                        float(cleaned_price)  # Validate
                        logger.info(f"✅ Price found: {cleaned_price}")
                        return cleaned_price
                    except ValueError:
                        logger.warning(f"⚠️ Invalid price format: {cleaned_price}")
            
            logger.warning("⚠️ No valid price found")
            
        except Exception as e:
            logger.error(f"❌ Attempt {attempt + 1} failed: {e}")
            
        finally:
            # Aggressive cleanup after each attempt
            if driver:
                try:
                    driver.quit()
                    driver_instance = None
                except:
                    pass
            
            cleanup_chrome_processes()
            time.sleep(5)  # Wait between attempts
    
    logger.error("❌ All price fetch attempts failed")
    return None

@tasks.loop(seconds=180)  # Every 3 minutes (less frequent)
async def update_bot_status():
    """Update bot status with resource monitoring"""
    global last_price
    
    if not client.is_ready():
        return
    
    try:
        logger.info("🔄 Starting price update cycle...")
        log_system_resources()
        
        # Fetch price in executor
        loop = asyncio.get_event_loop()
        price = await loop.run_in_executor(None, fetch_price_with_retry)
        
        if price and price != last_price:
            logger.info(f"📈 Price update: {last_price} → {price}")
            
            # Update bot status
            await client.change_presence(activity=discord.Game(name=f"prANA: ${price}"))
            
            # Update voice channel (with rate limit protection)
            channel = client.get_channel(VOICE_CHANNEL_ID)
            if channel and isinstance(channel, discord.VoiceChannel):
                try:
                    await channel.edit(name=f"prANA: ${price}")
                    last_price = price
                    logger.info(f"✅ Updated to: ${price}")
                except discord.HTTPException as e:
                    if "rate limited" in str(e).lower():
                        logger.warning("⚠️ Rate limited, skipping channel update")
                    else:
                        raise
        elif price:
            logger.info(f"⏸️ Price unchanged: ${price}")
        else:
            logger.warning("⚠️ Price fetch failed")
            
    except Exception as e:
        logger.error(f"❌ Update cycle error: {e}")
    
    finally:
        # Always cleanup after update cycle
        cleanup_chrome_processes()
        log_system_resources()

@client.event
async def on_ready():
    """Bot ready event"""
    logger.info(f"✅ Bot ready: {client.user}")
    logger.info(f"🎯 Target channel: {VOICE_CHANNEL_ID}")
    
    # Verify channel
    channel = client.get_channel(VOICE_CHANNEL_ID)
    if channel and isinstance(channel, discord.VoiceChannel):
        logger.info(f"✅ Channel found: '{channel.name}' in '{channel.guild.name}'")
    else:
        logger.error(f"❌ Invalid channel: {VOICE_CHANNEL_ID}")
    
    # Log initial resources
    log_system_resources()
    
    # Start monitoring
    logger.info("🚀 Starting price monitoring (every 3 minutes)...")
    update_bot_status.start()

@client.event
async def on_disconnect():
    logger.warning("⚠️ Discord disconnected")
    cleanup_chrome_processes()

def main():
    """Main function with cleanup"""
    logger.info("🚀 prANA Price Bot Starting...")
    log_system_resources()
    
    try:
        client.run(DISCORD_BOT_TOKEN)
    except KeyboardInterrupt:
        logger.info("👋 Bot stopped by user")
    except Exception as e:
        logger.error(f"❌ Bot error: {e}")
    finally:
        cleanup_chrome_processes()
        logger.info("🧹 Final cleanup complete")

if __name__ == "__main__":
    main()
