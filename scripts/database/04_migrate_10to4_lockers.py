"""
Migration: Reduce lockers from 10 to 4
Removes lockers 5-10 and reassigns/cleans up members
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from locker.db import connect

def migrate_lockers_10_to_4():
    """
    Reduces database from 10 lockers to 4
    - Removes lockers 5-10
    - Unassigns any members on lockers 5-10
    - Resets locker counts
    """
    conn = connect()
    
    print("🔧 Migrating from 10 lockers to 4 lockers...\n")
    
    # 1. Find members assigned to lockers 5-10
    affected = conn.execute("""
        SELECT id, full_name, locker_id 
        FROM members 
        WHERE locker_id > 4
    """).fetchall()
    
    if affected:
        print(f"⚠️  Found {len(affected)} members on lockers 5-10:")
        for member in affected:
            m = dict(member)
            print(f"   - {m['full_name']} (ID {m['id']}) on Locker {m['locker_id']}")
        
        # Unassign them
        conn.execute("UPDATE members SET locker_id = NULL WHERE locker_id > 4")
        print(f"\n✓ Unassigned {len(affected)} members\n")
    else:
        print("✓ No members on lockers 5-10\n")
    
    # 2. Delete lockers 5-10
    conn.execute("DELETE FROM lockers WHERE id > 4")
    print("✓ Deleted lockers 5-10\n")
    
    # 3. Reset sqlite_sequence
    conn.execute("DELETE FROM sqlite_sequence WHERE name = 'lockers'")
    conn.execute("INSERT INTO sqlite_sequence (name, seq) VALUES ('lockers', 4)")
    print("✓ Reset locker sequence counter\n")
    
    # 4. Verify final state
    lockers = conn.execute("SELECT id, label, status FROM lockers ORDER BY id").fetchall()
    print("📍 Final locker state:")
    for locker in lockers:
        l = dict(locker)
        print(f"   Locker {l['id']}: {l['label']} ({l['status']})")
    
    conn.commit()
    conn.close()
    
    print("\n✅ Migration complete! Database now has 4 lockers.")

if __name__ == "__main__":
    migrate_lockers_10_to_4()
