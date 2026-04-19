#!/usr/bin/env python3
"""
Test hostname-based connectivity for ESP32, Raspberry Pi, and backend system.

This script verifies that all components are reachable using hostnames
and that the system will work across different networks.
"""

import socket
import subprocess
import sys
from urllib.parse import urlparse

try:
    import requests
except ImportError:
    print("Error: requests library not installed")
    sys.exit(1)


def print_header(text):
    """Print a formatted header."""
    print(f"\n{'='*60}")
    print(f"  {text}")
    print(f"{'='*60}")


def test_hostname_resolution(hostname):
    """Test if a hostname can be resolved to an IP."""
    try:
        ip = socket.gethostbyname(hostname)
        print(f"✓ Hostname '{hostname}' resolved to {ip}")
        return ip
    except socket.gaierror:
        print(f"✗ Hostname '{hostname}' could not be resolved")
        return None


def test_ping(hostname):
    """Test if a hostname responds to ping."""
    try:
        if sys.platform == "win32":
            result = subprocess.run(
                ["ping", "-n", "1", hostname],
                capture_output=True,
                timeout=5
            )
        else:
            result = subprocess.run(
                ["ping", "-c", "1", hostname],
                capture_output=True,
                timeout=5
            )
        
        if result.returncode == 0:
            print(f"✓ {hostname} responds to ping")
            return True
        else:
            print(f"✗ {hostname} does not respond to ping")
            return False
    except Exception as e:
        print(f"✗ Ping test failed: {e}")
        return False


def test_http_endpoint(url, description=""):
    """Test if an HTTP endpoint is reachable."""
    try:
        response = requests.get(url, timeout=5)
        if response.status_code == 200:
            print(f"✓ {description or url} is reachable (HTTP {response.status_code})")
            return True
        else:
            print(f"✗ {description or url} returned HTTP {response.status_code}")
            return False
    except requests.exceptions.Timeout:
        print(f"✗ {description or url} timed out (check if service is running)")
        return False
    except requests.exceptions.ConnectionError as e:
        print(f"✗ {description or url} connection failed: {e}")
        return False
    except Exception as e:
        print(f"✗ {description or url} error: {e}")
        return False


def test_esp32_connection():
    """Test ESP32 connectivity via hostname."""
    print_header("Testing ESP32 Connectivity")
    
    hostname = "esp32-locker.local"
    results = []
    
    # Test hostname resolution
    ip = test_hostname_resolution(hostname)
    results.append(ip is not None)
    
    # Test ping
    results.append(test_ping(hostname))
    
    # Test HTTP endpoint
    endpoint = f"http://{hostname}/locker/1/status"
    results.append(test_http_endpoint(endpoint, "ESP32 /locker/1/status"))
    
    # Test fallback IP
    print("\n  Testing fallback IPs...")
    for fallback_ip in ["192.168.1.100", "192.168.2.104"]:
        endpoint = f"http://{fallback_ip}/locker/1/status"
        if test_http_endpoint(endpoint, f"Fallback {fallback_ip}"):
            print(f"  → Found ESP32 at {fallback_ip}")
            break
    
    return all(results)


def test_raspberry_pi_connection():
    """Test Raspberry Pi backend connectivity via hostname."""
    print_header("Testing Raspberry Pi Backend Connectivity")
    
    hostname = "raspberrypi-locker.local"
    results = []
    
    # Test hostname resolution
    ip = test_hostname_resolution(hostname)
    results.append(ip is not None)
    
    # Test ping
    results.append(test_ping(hostname))
    
    # Test HTTP endpoint
    endpoint = f"http://{hostname}:5000/"
    results.append(test_http_endpoint(endpoint, "Raspberry Pi Flask server"))
    
    # Test specific endpoints
    endpoints = [
        (f"http://{hostname}:5000/admin/dashboard", "Admin dashboard"),
        (f"http://{hostname}:5000/admin/list", "Admin list endpoint"),
    ]
    
    for url, description in endpoints:
        test_http_endpoint(url, description)
    
    # Test fallback IPs
    print("\n  Testing fallback IPs...")
    for fallback_ip in ["192.168.1.50", "192.168.2.1"]:
        endpoint = f"http://{fallback_ip}:5000/"
        if test_http_endpoint(endpoint, f"Fallback {fallback_ip}:5000"):
            print(f"  → Found Raspberry Pi at {fallback_ip}")
            break
    
    return all(results)


def test_network_environment():
    """Test the network environment and display current configuration."""
    print_header("Network Environment")
    
    # Get local IP
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
        print(f"✓ Local IP address: {local_ip}")
    except Exception as e:
        print(f"✗ Could not determine local IP: {e}")
        local_ip = None
    
    # Check hostname
    try:
        hostname = socket.gethostname()
        print(f"✓ Computer hostname: {hostname}")
    except Exception as e:
        print(f"✗ Could not determine hostname: {e}")
    
    # Try to resolve localhost
    try:
        localhost_ip = socket.gethostbyname("localhost")
        print(f"✓ localhost resolves to {localhost_ip}")
    except Exception as e:
        print(f"✗ localhost resolution failed: {e}")
    
    # Check DNS
    try:
        dns_ip = socket.gethostbyname("google.com")
        print(f"✓ DNS working (google.com → {dns_ip})")
    except Exception as e:
        print(f"✗ DNS not working: {e}")


def load_env_config():
    """Load ESP32 configuration from .env file."""
    print_header("Configuration from .env")
    
    try:
        with open(".env", "r") as f:
            config = {}
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    config[key.strip()] = value.strip()
            
            print(f"Device Mode: {config.get('LOCKER_DEVICE_MODE', 'N/A')}")
            print(f"ESP32 Base URL: {config.get('ESP32_BASE_URL', 'N/A')}")
            print(f"ESP32 Fallback URLs: {config.get('ESP32_FALLBACK_URLS', 'N/A')}")
            print(f"Connect Timeout: {config.get('ESP32_CONNECT_TIMEOUT', 'N/A')}s")
            print(f"Read Timeout: {config.get('ESP32_TIMEOUT', 'N/A')}s")
            print(f"Health Check Interval: {config.get('ESP32_HEALTH_CHECK_INTERVAL', 'N/A')}s")
            
            return config
    except FileNotFoundError:
        print("✗ .env file not found")
        return None
    except Exception as e:
        print(f"✗ Error reading .env: {e}")
        return None


def print_summary(results):
    """Print test summary."""
    print_header("Test Summary")
    
    total = len(results)
    passed = sum(1 for r in results if r)
    
    print(f"Passed: {passed}/{total}")
    
    if passed == total:
        print("\n✓ All tests passed! Your system is configured correctly.")
        print("  Hostname-based connectivity is working.")
        return True
    else:
        print(f"\n✗ {total - passed} test(s) failed.")
        print("  Check the configuration and try again.")
        return False


def main():
    """Run all tests."""
    print("\n" + "="*60)
    print("  Smart Locker System - Hostname Connectivity Test")
    print("="*60)
    
    results = []
    
    # Test network environment
    test_network_environment()
    
    # Load configuration
    load_env_config()
    
    # Test ESP32
    try:
        results.append(test_esp32_connection())
    except Exception as e:
        print(f"Error testing ESP32: {e}")
        results.append(False)
    
    # Test Raspberry Pi
    try:
        results.append(test_raspberry_pi_connection())
    except Exception as e:
        print(f"Error testing Raspberry Pi: {e}")
        results.append(False)
    
    # Print summary
    success = print_summary(results)
    
    print("\n" + "="*60 + "\n")
    
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
