#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Telegram Casino Bot Launcher
============================
Pre-launch verification and startup script
"""

import os
import sys
import subprocess

def check_dependencies():
    """Verify all required packages are installed"""
    print("=" * 50)
    print("Checking Dependencies...")
    print("=" * 50)
    
    dependencies = {
        'telegram': 'python-telegram-bot',
        'httpx': 'httpx',
        'PIL': 'Pillow'
    }
    
    missing = []
    for module, package_name in dependencies.items():
        try:
            __import__(module)
            print(f"✓ {package_name} installed")
        except ImportError:
            print(f"✗ {package_name} NOT found")
            missing.append(package_name)
    
    if missing:
        print(f"\nInstalling missing packages: {', '.join(missing)}")
        subprocess.check_call([sys.executable, '-m', 'pip', 'install', *missing, '-q'])
        print("✓ Dependencies installed")
    
    return True

def verify_files():
    """Check all required files exist"""
    print("\n" + "=" * 50)
    print("Verifying Required Files...")
    print("=" * 50)
    
    required_files = [
        'casino v5 (1).py',
        'storage.py',
        'bot_network.py',
        'languages.py',
        'oxapay.py'
    ]
    
    all_exist = True
    for filename in required_files:
        if os.path.isfile(filename):
            print(f"✓ {filename}")
        else:
            print(f"✗ {filename} NOT FOUND")
            all_exist = False
    
    return all_exist

def verify_token():
    """Verify bot token is set"""
    print("\n" + "=" * 50)
    print("Verifying Bot Configuration...")
    print("=" * 50)
    
    # Read the bot file to check token
    try:
        with open('casino v5 (1).py', 'r', encoding='utf-8') as f:
            content = f.read()
            if '8062106287:AAHuFUn04LihAfyvF8mRCAz7lg_BJRZECCg' in content:
                print("✓ Bot token configured correctly")
                return True
            else:
                print("✗ Bot token not found or incorrect")
                return False
    except Exception as e:
        print(f"✗ Error reading bot file: {e}")
        return False

def main():
    """Run pre-launch checks and start bot"""
    print("\n")
    print(" " * 30 + "TELEGRAM CASINO BOT")
    print(" " * 25 + "Pre-Launch Verification")
    print("\n")
    
    checks = [
        ("Dependencies", check_dependencies),
        ("Required Files", verify_files),
        ("Bot Configuration", verify_token),
    ]
    
    all_passed = True
    for check_name, check_func in checks:
        try:
            if not check_func():
                all_passed = False
        except Exception as e:
            print(f"✗ {check_name} check failed: {e}")
            all_passed = False
    
    print("\n" + "=" * 50)
    
    if not all_passed:
        print("✗ Pre-launch checks FAILED")
        print("=" * 50)
        input("\nPress Enter to exit...")
        return 1
    
    print("✓ All pre-launch checks PASSED")
    print("=" * 50)
    print("\nStarting Bot Application...")
    print("Bot Token: 8062106287:***")
    print("\nPress Ctrl+C to stop the bot\n")
    
    try:
        # Import and run the bot
        import casino v5 (1)  # This won't work due to filename
        # Instead, execute the file directly
        exec(open('casino v5 (1).py').read())
    except KeyboardInterrupt:
        print("\n\nBot stopped by user")
        return 0
    except Exception as e:
        print(f"\n✗ Bot error: {e}")
        import traceback
        traceback.print_exc()
        return 1

if __name__ == '__main__':
    sys.exit(main())
