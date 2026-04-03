"""
Fix: Validate pending member locker selections
As of March 30, 2026: Members SELECT lockers during registration and keep them pending approval
This script VALIDATES (not removes) those selections
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from locker.db import connect

def fix_pending_locker_assignments():
    """
    VALIDATE pending member locker selections (NEW WORKFLOW)
    - Members SELECT lockers during registration → locker_id is saved
    - Members stay PENDING until admin approves
    - Admin approval assigns the SELECTED locker (keeps locker_id)
    
    This script verifies the workflow is correct and logs issues only.
    """
    conn = connect()
    
    print("🔍 Validating pending member locker selections...\n")
    
    # Find pending members with lockers (this is CORRECT behavior)
    pending_with_locker = conn.execute("""
        SELECT id, full_name, status, payment_status, locker_id
        FROM members
        WHERE locker_id IS NOT NULL 
        AND status = 'pending'
        AND payment_status = 'unpaid'
        ORDER BY id
    """).fetchall()
    
    if pending_with_locker:
        print(f"✅ Found {len(pending_with_locker)} pending members with selected lockers:\n")
        for member in pending_with_locker:
            m = dict(member)
            print(f"   ✓ {m['full_name']} (ID {m['id']}) - Selected Locker {m['locker_id']}")
        print("\n→ These members are correctly waiting for admin approval\n")
    else:
        print("ℹ️  No pending members with selected lockers\n")
    
    # Check for ANOMALIES (members that are pending but have lockers assigned to OTHER status)
    anomalies = conn.execute("""
        SELECT id, full_name, status, payment_status, locker_id
        FROM members
        WHERE (status != 'pending' OR payment_status != 'unpaid')
        AND locker_id IS NOT NULL
        AND status != 'approved'
    """).fetchall()
    
    if anomalies:
        print(f"⚠️  Found {len(anomalies)} members in inconsistent state:\n")
        for member in anomalies:
            m = dict(member)
            print(f"   ⚠️  {m['full_name']} (ID {m['id']})")
            print(f"      Status: {m['status']}, Payment: {m['payment_status']}")
            print(f"      Has Locker: {m['locker_id']}\n")
        print("→ These may need manual review (not auto-fixed)\n")
    else:
        print("✓ No inconsistent states found\n")
    
    # 2. Verify final state
    print("📍 Current locker assignment state:\n")
    lockers = conn.execute("""
        SELECT l.id, l.label, l.status, m.full_name, m.status as member_status, m.member_type
        FROM lockers l
        LEFT JOIN members m ON l.id = m.locker_id
        ORDER BY l.id
    """).fetchall()
    
    for locker in lockers:
        loc = dict(locker)
        if loc['full_name']:
            type_badge = "👥" if loc['member_type'] == 'regular' else "🎫"
            print(f"   Locker {loc['id']}: {loc['label']} → {type_badge} {loc['full_name']} ({loc['member_status']})")
        else:
            print(f"   Locker {loc['id']}: {loc['label']} - Available")
    
    conn.commit()
    conn.close()
    
    print("\n✅ Validation complete! Pending members with selected lockers are ready for admin approval.")

if __name__ == "__main__":
    fix_pending_locker_assignments()
