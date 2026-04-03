"""
Database Maintenance Scripts
Fix data consistency issues
"""

from locker.db import connect
from datetime import datetime, timedelta
import json

def reset_locker(locker_id, status='available'):
    """
    Reset a locker to available status
    Usage: reset_locker(1, 'available')
    """
    conn = connect()
    conn.execute('UPDATE lockers SET status = ? WHERE id = ?', (status, locker_id))
    conn.commit()
    print(f"✓ Locker {locker_id} reset to '{status}'")
    conn.close()

def clear_expired_access():
    """
    Clear locker assignments for members/guests with expired access
    Returns the count of cleared assignments
    """
    conn = connect()
    now = datetime.utcnow().isoformat()
    
    # Find expired records
    expired = conn.execute(
        "SELECT id, full_name, locker_id FROM members WHERE expiry_date < ? AND locker_id IS NOT NULL",
        (now,)
    ).fetchall()
    
    if not expired:
        print("✓ No expired members found")
        conn.close()
        return 0
    
    cleared = 0
    for member in expired:
        m = dict(member)
        locker_id = m['locker_id']
        
        # Clear locker assignment
        conn.execute("UPDATE members SET locker_id = NULL WHERE id = ?", (m['id'],))
        
        # Mark locker as available
        conn.execute("UPDATE lockers SET status = 'available' WHERE id = ?", (locker_id,))
        
        print(f"✓ Cleared: {m['full_name']} from Locker {locker_id} (expired {m['expiry_date'][:10]})")
        cleared += 1
    
    conn.commit()
    conn.close()
    print(f"\n✓ Total cleared: {cleared} assignments")
    return cleared

def remove_duplicate_registrations():
    """
    Find and remove duplicate member registrations (keeps first, deletes rest)
    """
    conn = connect()
    
    # Find duplicates
    duplicates = conn.execute("""
        SELECT full_name, COUNT(*) as count, GROUP_CONCAT(id) as ids
        FROM members
        WHERE member_type = 'regular' AND (status = 'pending' OR status = 'approved')
        GROUP BY full_name
        HAVING count > 1
        ORDER BY count DESC
    """).fetchall()
    
    if not duplicates:
        print("✓ No duplicate registrations found")
        conn.close()
        return 0
    
    total_deleted = 0
    for dup in duplicates:
        dup_dict = dict(dup)
        ids = list(map(int, dup_dict['ids'].split(',')))
        
        # Keep lowest ID, delete the rest
        keep_id = min(ids)
        delete_ids = [id for id in ids if id != keep_id]
        
        print(f"\n  Found {dup_dict['count']} registrations for: {dup_dict['full_name']}")
        print(f"    Keeping ID: {keep_id}")
        
        for del_id in delete_ids:
            conn.execute("DELETE FROM members WHERE id = ?", (del_id,))
            print(f"    ✓ Deleted ID {del_id}")
            total_deleted += 1
    
    conn.commit()
    conn.close()
    print(f"\n✓ Total deleted: {total_deleted} duplicate records")
    return total_deleted

if __name__ == "__main__":
    print("=" * 60)
    print("DATABASE MAINTENANCE")
    print("=" * 60)
    
    print("\n1️⃣  Clearing expired access...")
    clear_expired_access()
    
    print("\n2️⃣  Removing duplicates...")
    remove_duplicate_registrations()
    
    print("\n" + "=" * 60)
