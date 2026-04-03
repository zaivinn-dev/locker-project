"""Debug utilities for locker system database inspection."""
import sqlite3
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent.parent / "locker.sqlite3"


def get_connection():
    """Get database connection."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def check_expired_guests():
    """Check for expired guests in the database."""
    print("\n" + "="*60)
    print("🔍 EXPIRED GUESTS CHECK")
    print("="*60)
    
    conn = get_connection()
    cursor = conn.cursor()
    
    # Current time
    now_local = datetime.now()
    print(f"Current local time: {now_local}")
    print(f"ISO format: {now_local.isoformat()}\n")
    
    # Get all guests
    cursor.execute('''
        SELECT id, full_name, rfid_uid, expiry_date, locker_id
        FROM members
        WHERE member_type = 'guest'
        ORDER BY expiry_date DESC
    ''')
    
    guests = cursor.fetchall()
    print(f"Total guests: {len(guests)}\n")
    
    expired_count = 0
    for guest in guests:
        expiry = datetime.fromisoformat(guest['expiry_date'].replace(' ', 'T'))
        is_expired = expiry < now_local
        
        status = "❌ EXPIRED" if is_expired else "✅ ACTIVE"
        print(f"{status} | ID: {guest['id']:3d} | {guest['full_name']:15s} | RFID: {guest['rfid_uid']} | Expires: {guest['expiry_date']}")
        
        if is_expired:
            expired_count += 1
    
    print(f"\n⚠️  Expired guests that should auto-delete: {expired_count}")
    conn.close()
    return expired_count


def check_locker_assignments():
    """Check locker assignments and availability."""
    print("\n" + "="*60)
    print("🗂️  LOCKER ASSIGNMENTS")
    print("="*60 + "\n")
    
    conn = get_connection()
    
    # Get locker assignments
    lockers = conn.execute('''
        SELECT 
            l.id,
            l.label,
            l.status,
            m.full_name,
            m.member_type,
            m.expiry_date
        FROM lockers l
        LEFT JOIN members m ON l.id = m.locker_id
        ORDER BY l.id
    ''').fetchall()
    
    for locker in lockers:
        status_icon = "✅" if locker['status'] == 'available' else "🔒"
        
        if locker['full_name']:
            assigned = f"{locker['full_name']} ({locker['member_type']})"
            if locker['expiry_date']:
                assigned += f" - Expires: {locker['expiry_date']}"
        else:
            assigned = "— EMPTY"
        
        print(f"{status_icon} Locker {locker['id']}: {locker['status']:10s} | {assigned}")
    
    conn.close()


def check_auto_deletion_logs():
    """Check access logs for auto-deletion events."""
    print("\n" + "="*60)
    print("📋 AUTO-DELETION LOGS")
    print("="*60 + "\n")
    
    conn = get_connection()
    
    logs = conn.execute('''
        SELECT id, actor_ref, detail, created_at
        FROM access_logs
        WHERE action = 'guest_auto_deleted'
        ORDER BY id DESC
        LIMIT 10
    ''').fetchall()
    
    if logs:
        for log in logs:
            print(f"[{log['id']}] {log['created_at']}")
            print(f"     {log['detail']}\n")
    else:
        print("No auto-deletion logs found yet.")
    
    conn.close()


def main():
    """Run all debug checks."""
    try:
        check_expired_guests()
        check_locker_assignments()
        check_auto_deletion_logs()
        print("\n" + "="*60)
    except Exception as e:
        print(f"❌ Error: {e}")


if __name__ == "__main__":
    main()
