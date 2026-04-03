"""Test script to verify auto-deletion functionality."""
import requests
import time
from datetime import datetime

def test_auto_deletion():
    """Trigger auto-deletion by accessing admin RFID page."""
    print("\n" + "="*60)
    print("🧪 TESTING AUTO-DELETION")
    print("="*60 + "\n")
    
    url = "http://localhost:5000/admin/rfid"
    
    print(f"Time: {datetime.now()}")
    print(f"Endpoint: {url}\n")
    
    print("📤 Sending request to trigger auto-deletion...")
    
    try:
        response = requests.get(url, timeout=5)
        print(f"✅ Response: {response.status_code}")
        
        if response.status_code == 200:
            print("✅ Auto-deletion logic executed successfully\n")
            return True
        else:
            print(f"❌ Unexpected status code: {response.status_code}\n")
            return False
    except requests.exceptions.ConnectionError:
        print("❌ Could not connect to server. Make sure Flask app is running:")
        print("   cd d:\\project\\locker-project")
        print("   python -m locker.web\n")
        return False
    except Exception as e:
        print(f"❌ Error: {e}\n")
        return False


if __name__ == "__main__":
    result = test_auto_deletion()
    
    if result:
        print("💡 Next steps:")
        print("   1. python scripts/debug/inspect_database.py")
        print("   2. Check if expired guests were deleted")
        print("   3. Verify lockers became available\n")
