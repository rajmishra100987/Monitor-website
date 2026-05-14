from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
import threading
import time
import json
import os
import hashlib
import secrets
import gc
import psutil
from datetime import datetime, timedelta
from typing import Dict, List
import signal
import sys
import logging
from pathlib import Path

# ============================================================================
# CONFIGURATION
# ============================================================================

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', secrets.token_hex(32))

USERS_FILE = 'users.json'
MONITORS_DIR = 'monitors_data'
LOG_FILE = 'app.log'
MEMORY_CLEANUP_INTERVAL = 3600  # 1 hour
BROWSER_RESTART_HOURS = 6  # Browser restart every 6 hours

# Create directories
os.makedirs(MONITORS_DIR, exist_ok=True)

# Configure logging - SILENT for Render logs
logging.basicConfig(
    level=logging.WARNING,  # Only warnings and errors
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# Global variables
user_sessions = {}
monitor_threads = {}
threads_running = True

# Shared browser instance (NEW - single browser for all URLs)
shared_driver = None
shared_driver_lock = threading.Lock()
shared_driver_last_restart = time.time()

# Chromium paths for Render
CHROMIUM_PATHS = [
    '/usr/bin/chromium',
    '/usr/bin/chromium-browser', 
    '/usr/bin/google-chrome',
    '/usr/bin/chrome'
]

# ============================================================================
# SHARED BROWSER MANAGEMENT (NEW)
# ============================================================================

def get_chromium_path():
    """Find chromium/chrome binary path"""
    for path in CHROMIUM_PATHS:
        if os.path.exists(path):
            return path
    return None

def setup_shared_browser():
    """Setup single shared browser instance"""
    global shared_driver, shared_driver_last_restart
    
    try:
        options = Options()
        options.add_argument('--headless=new')
        options.add_argument('--no-sandbox')
        options.add_argument('--disable-dev-shm-usage')
        options.add_argument('--disable-gpu')
        options.add_argument('--window-size=1024,768')
        options.add_argument('--disable-software-rasterizer')
        options.add_argument('--disable-setuid-sandbox')
        options.add_argument('--disable-extensions')
        options.add_argument('--disable-plugins')
        options.add_argument('--disable-images')
        options.add_argument('--blink-settings=imagesEnabled=false')
        options.add_argument('--disable-blink-features=AutomationControlled')
        
        # Memory optimization flags
        options.add_argument('--memory-pressure-off')
        options.add_argument('--max_old_space_size=128')
        options.add_argument('--js-flags="--max-old-space-size=128"')
        
        # Set binary location
        chromium_path = get_chromium_path()
        if chromium_path:
            options.binary_location = chromium_path
        
        # Use webdriver-manager for automatic ChromeDriver
        service = Service(ChromeDriverManager().install())
        shared_driver = webdriver.Chrome(service=service, options=options)
        
        shared_driver_last_restart = time.time()
        logger.info("Shared browser setup completed successfully")
        return True
        
    except Exception as e:
        logger.error(f"Failed to setup shared browser: {e}")
        shared_driver = None
        return False

def restart_shared_browser_if_needed():
    """Check and restart shared browser if needed (crash or time-based)"""
    global shared_driver, shared_driver_last_restart
    
    should_restart = False
    
    # Check if browser crashed
    if shared_driver is None:
        should_restart = True
        logger.info("Shared browser is None, restarting...")
    else:
        try:
            shared_driver.current_url
        except Exception as e:
            should_restart = True
            logger.info(f"Shared browser crashed, restarting: {str(e)[:50]}")
    
    # Check time-based restart
    if not should_restart and (time.time() - shared_driver_last_restart) >= (BROWSER_RESTART_HOURS * 3600):
        should_restart = True
        logger.info(f"Time-based restart after {BROWSER_RESTART_HOURS} hours")
    
    if should_restart:
        if shared_driver:
            try:
                shared_driver.quit()
            except:
                pass
            shared_driver = None
        
        for attempt in range(3):
            if setup_shared_browser():
                return True
            time.sleep(2)
        return False
    
    return True

def open_url_in_browser_shared(url: str, timeout_seconds: int = 30) -> dict:
    """
    Open URL using shared browser (single instance, multiple tabs)
    """
    global shared_driver
    
    result = {
        'success': False,
        'title': '',
        'error': '',
        'timestamp': datetime.now().isoformat()
    }
    
    with shared_driver_lock:
        # Restart browser if needed
        if not restart_shared_browser_if_needed():
            result['error'] = 'Browser unavailable after restart attempts'
            return result
    
    try:
        with shared_driver_lock:
            # Open new tab
            shared_driver.execute_script("window.open('');")
            original_window = shared_driver.current_window_handle
            new_tab = shared_driver.window_handles[-1]
            shared_driver.switch_to.window(new_tab)
        
        # Load URL
        shared_driver.set_page_load_timeout(timeout_seconds)
        shared_driver.get(url)
        
        # Wait for body
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
        
        WebDriverWait(shared_driver, timeout_seconds).until(
            EC.presence_of_element_located((By.TAG_NAME, "body"))
        )
        
        result['success'] = True
        result['title'] = shared_driver.title[:100] if shared_driver.title else "No Title"
        
    except TimeoutException:
        result['error'] = f"Timeout after {timeout_seconds}s"
    except WebDriverException as e:
        result['error'] = f"Browser error: {str(e)[:80]}"
    except Exception as e:
        result['error'] = f"Error: {str(e)[:80]}"
    finally:
        try:
            with shared_driver_lock:
                # Close tab and switch back
                if len(shared_driver.window_handles) > 1:
                    shared_driver.close()
                    shared_driver.switch_to.window(original_window)
        except:
            pass
    
    return result

# Keep old function name for compatibility
open_url_in_browser = open_url_in_browser_shared

# ============================================================================
# MEMORY MANAGEMENT
# ============================================================================

def get_memory_usage():
    """Get current memory usage in MB"""
    process = psutil.Process(os.getpid())
    return process.memory_info().rss / 1024 / 1024

def cleanup_memory():
    """Force garbage collection and memory cleanup"""
    gc.collect()
    if hasattr(gc, 'collect'):
        gc.collect(2)
    logger.info(f"Memory cleanup completed. Current usage: {get_memory_usage():.1f} MB")

def start_memory_cleanup_thread():
    """Start background thread for periodic memory cleanup"""
    def cleanup_loop():
        while threads_running:
            time.sleep(MEMORY_CLEANUP_INTERVAL)
            cleanup_memory()
    
    thread = threading.Thread(target=cleanup_loop, daemon=True)
    thread.start()
    logger.info("Memory cleanup thread started")

# ============================================================================
# USER MANAGEMENT
# ============================================================================

def load_users():
    """Load users from JSON file"""
    if os.path.exists(USERS_FILE):
        try:
            with open(USERS_FILE, 'r') as f:
                return json.load(f)
        except:
            return {}
    return {}

def save_users(users):
    """Save users to JSON file"""
    with open(USERS_FILE, 'w') as f:
        json.dump(users, f, indent=2)

def hash_password(password):
    """Hash password using SHA-256"""
    return hashlib.sha256(password.encode()).hexdigest()

def load_user_monitors(username):
    """Load monitors for specific user"""
    user_file = os.path.join(MONITORS_DIR, f"{username}.json")
    if os.path.exists(user_file):
        try:
            with open(user_file, 'r') as f:
                return json.load(f)
        except:
            return {}
    return {}

def save_user_monitors(username, monitors):
    """Save monitors for specific user"""
    user_file = os.path.join(MONITORS_DIR, f"{username}.json")
    with open(user_file, 'w') as f:
        json.dump(monitors, f, indent=2)

# ============================================================================
# MONITOR THREAD - MODIFIED (no browser restart here, handled globally)
# ============================================================================

def schedule_monitor_task(username: str, monitor_id: str, name: str, url: str, 
                          interval_minutes: int, timeout_seconds: int = 30):
    """
    Background task for each monitor - MODIFIED to use shared browser
    """
    logger.info(f"Monitor '{name}' started for user {username}")
    
    while threads_running:
        try:
            # Check if monitor still exists
            user_monitors = load_user_monitors(username)
            if monitor_id not in user_monitors:
                break
            
            monitor = user_monitors[monitor_id]
            if not monitor.get('enabled', True):
                time.sleep(60)
                continue
            
            # Update status
            monitor['status'] = 'checking'
            monitor['last_check'] = datetime.now().isoformat()
            save_user_monitors(username, user_monitors)
            
            # Open URL in shared browser
            result = open_url_in_browser(url, timeout_seconds)
            
            # Update monitor status
            if result['success']:
                monitor['status'] = 'online'
                monitor['last_success'] = result['timestamp']
                monitor['last_title'] = result['title']
                monitor['error'] = ''
                monitor['uptime'] = monitor.get('uptime', 0) + interval_minutes
            else:
                monitor['status'] = 'offline'
                monitor['last_error'] = result['error']
                monitor['error'] = result['error']
                monitor['failures'] = monitor.get('failures', 0) + 1
            
            monitor['last_check'] = result['timestamp']
            save_user_monitors(username, user_monitors)
            
            # Small delay before next check
            interval_seconds = interval_minutes * 60
            for _ in range(min(interval_seconds, 3600)):
                if not threads_running or monitor_id not in load_user_monitors(username):
                    break
                time.sleep(1)
                
        except Exception as e:
            logger.error(f"Error in monitor '{name}': {e}")
            time.sleep(60)

# ============================================================================
# FLASK ROUTES (SAME AS ORIGINAL - NO CHANGES)
# ============================================================================

@app.route('/')
def index():
    """Login page or dashboard"""
    if 'username' not in session:
        return render_template('login.html')
    return render_template('dashboard.html', username=session['username'])

@app.route('/login', methods=['POST'])
def login():
    """User login"""
    data = request.json
    username = data.get('username', '').strip()
    password = data.get('password', '').strip()
    
    users = load_users()
    
    if username in users and users[username]['password'] == hash_password(password):
        session['username'] = username
        session.permanent = True
        
        # Start monitor threads for this user if not already running
        start_user_monitors(username)
        
        return jsonify({'success': True})
    
    # Create new user if doesn't exist
    if username not in users and len(username) >= 3 and len(password) >= 3:
        users[username] = {
            'password': hash_password(password),
            'created_at': datetime.now().isoformat()
        }
        save_users(users)
        session['username'] = username
        
        # Create empty monitors file
        save_user_monitors(username, {})
        
        return jsonify({'success': True, 'new': True})
    
    return jsonify({'success': False, 'error': 'Invalid credentials'})

@app.route('/logout')
def logout():
    """User logout"""
    session.pop('username', None)
    return redirect(url_for('index'))

@app.route('/api/monitors', methods=['GET'])
def api_get_monitors():
    """Get all monitors for logged-in user"""
    if 'username' not in session:
        return jsonify({'error': 'Not logged in'}), 401
    
    monitors = load_user_monitors(session['username'])
    return jsonify(monitors)

@app.route('/api/monitor/add', methods=['POST'])
def api_add_monitor():
    """Add a new monitor for logged-in user"""
    if 'username' not in session:
        return jsonify({'error': 'Not logged in'}), 401
    
    data = request.json
    username = session['username']
    monitors = load_user_monitors(username)
    
    import uuid
    monitor_id = str(uuid.uuid4())[:8]
    
    monitors[monitor_id] = {
        'id': monitor_id,
        'name': data.get('name', 'Untitled'),
        'url': data.get('url', ''),
        'interval_minutes': int(data.get('interval_minutes', 60)),
        'timeout': int(data.get('timeout', 30)),
        'enabled': True,
        'status': 'pending',
        'created_at': datetime.now().isoformat(),
        'last_check': '',
        'last_success': '',
        'last_title': '',
        'error': '',
        'failures': 0,
        'uptime': 0
    }
    
    save_user_monitors(username, monitors)
    
    # Start monitor thread
    monitor = monitors[monitor_id]
    thread = threading.Thread(
        target=schedule_monitor_task,
        args=(username, monitor_id, monitor['name'], monitor['url'], 
              monitor['interval_minutes'], monitor['timeout']),
        daemon=True
    )
    thread.start()
    
    # Store thread reference
    if username not in monitor_threads:
        monitor_threads[username] = {}
    monitor_threads[username][monitor_id] = thread
    
    return jsonify({'success': True, 'monitor_id': monitor_id})

@app.route('/api/monitor/delete/<monitor_id>', methods=['DELETE'])
def api_delete_monitor(monitor_id):
    """Delete a monitor (user can only delete their own)"""
    if 'username' not in session:
        return jsonify({'error': 'Not logged in'}), 401
    
    username = session['username']
    monitors = load_user_monitors(username)
    
    if monitor_id in monitors:
        del monitors[monitor_id]
        save_user_monitors(username, monitors)
        
        # Stop thread (thread will exit on its own)
        if username in monitor_threads and monitor_id in monitor_threads[username]:
            del monitor_threads[username][monitor_id]
        
        return jsonify({'success': True})
    
    return jsonify({'success': False, 'error': 'Monitor not found'}), 404

@app.route('/api/monitor/toggle/<monitor_id>', methods=['POST'])
def api_toggle_monitor(monitor_id):
    """Enable/disable a monitor"""
    if 'username' not in session:
        return jsonify({'error': 'Not logged in'}), 401
    
    username = session['username']
    monitors = load_user_monitors(username)
    
    if monitor_id in monitors:
        monitors[monitor_id]['enabled'] = not monitors[monitor_id].get('enabled', True)
        save_user_monitors(username, monitors)
        return jsonify({'success': True, 'enabled': monitors[monitor_id]['enabled']})
    
    return jsonify({'success': False, 'error': 'Monitor not found'}), 404

@app.route('/api/monitor/check/<monitor_id>', methods=['POST'])
def api_check_now(monitor_id):
    """Manually check a monitor immediately"""
    if 'username' not in session:
        return jsonify({'error': 'Not logged in'}), 401
    
    username = session['username']
    monitors = load_user_monitors(username)
    
    if monitor_id not in monitors:
        return jsonify({'success': False, 'error': 'Monitor not found'}), 404
    
    monitor = monitors[monitor_id]
    
    def manual_check():
        result = open_url_in_browser(monitor['url'], monitor.get('timeout', 30))
        
        if result['success']:
            monitor['status'] = 'online'
            monitor['last_success'] = result['timestamp']
            monitor['last_title'] = result['title']
            monitor['error'] = ''
        else:
            monitor['status'] = 'offline'
            monitor['error'] = result['error']
        
        monitor['last_check'] = result['timestamp']
        save_user_monitors(username, monitors)
    
    thread = threading.Thread(target=manual_check, daemon=True)
    thread.start()
    
    return jsonify({'success': True, 'message': 'Check started'})

@app.route('/api/stats', methods=['GET'])
def api_stats():
    """Get dashboard statistics for logged-in user"""
    if 'username' not in session:
        return jsonify({'error': 'Not logged in'}), 401
    
    monitors = load_user_monitors(session['username'])
    
    total = len(monitors)
    online = sum(1 for m in monitors.values() if m.get('status') == 'online')
    offline = sum(1 for m in monitors.values() if m.get('status') == 'offline')
    checking = sum(1 for m in monitors.values() if m.get('status') == 'checking')
    
    return jsonify({
        'total': total,
        'online': online,
        'offline': offline,
        'checking': checking,
        'memory_usage': f"{get_memory_usage():.1f} MB"
    })

@app.route('/health')
def health():
    """Health check endpoint"""
    return jsonify({
        'status': 'ok',
        'timestamp': datetime.now().isoformat(),
        'memory_mb': get_memory_usage()
    })

def start_user_monitors(username):
    """Start all monitor threads for a user"""
    monitors = load_user_monitors(username)
    
    if username not in monitor_threads:
        monitor_threads[username] = {}
    
    for monitor_id, monitor in monitors.items():
        if monitor.get('enabled', True) and monitor_id not in monitor_threads[username]:
            thread = threading.Thread(
                target=schedule_monitor_task,
                args=(username, monitor_id, monitor['name'], monitor['url'], 
                      monitor['interval_minutes'], monitor.get('timeout', 30)),
                daemon=True
            )
            thread.start()
            monitor_threads[username][monitor_id] = thread
            logger.info(f"Started monitor thread for user {username}: {monitor['name']}")

def shutdown_signal_handler(signum, frame):
    """Handle shutdown signals"""
    global threads_running, shared_driver
    logger.info("Received shutdown signal, stopping...")
    threads_running = False
    
    # Close shared browser
    if shared_driver:
        try:
            shared_driver.quit()
        except:
            pass
    
    cleanup_memory()
    sys.exit(0)

# ============================================================================
# MAIN
# ============================================================================

if __name__ == '__main__':
    signal.signal(signal.SIGINT, shutdown_signal_handler)
    signal.signal(signal.SIGTERM, shutdown_signal_handler)
    
    # Start shared browser
    logger.info("Starting shared browser...")
    setup_shared_browser()
    
    # Start memory cleanup thread
    start_memory_cleanup_thread()
    
    port = int(os.environ.get('PORT', 8080))
    
    logger.info("=" * 50)
    logger.info(f"🚀 Monitor Dashboard Started")
    logger.info(f"💾 Initial Memory: {get_memory_usage():.1f} MB")
    logger.info(f"🔄 Browser Restart: Every {BROWSER_RESTART_HOURS} hours")
    logger.info(f"🌐 Port: {port}")
    logger.info("=" * 50)
    
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
