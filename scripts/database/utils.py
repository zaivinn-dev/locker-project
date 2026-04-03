#!/usr/bin/env python3
"""
CONSOLIDATED UTILITIES
All database utility functions in one organized module
For direct database inspection and repair
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from locker.db import connect
import sqlite3

# ============================================================================
# DIAGNOSTIC UTILITIES
# ============================================================================

def inspect_database():
    """Full database inspection"""
    conn = sqlite3.connect('locker.sqlite3')
    conn.row_factory = sqlite3.Row
    
    print("\n" + "=" * 80)
    print("DATABASE INSPECTION")
    print("=" * 80)
    
    # Tables
    print("\n📊 TABLES:")
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    for t in tables:
        count = conn.execute(f"SELECT COUNT(*) as c FROM {t['name']}").fetchone()['c']
        print(f"  • {t['name']:20} ({count:4} rows)")
    
    # Members
    print("\n👥 MEMBERS:")
    members = conn.execute("""
        SELECT id, full_name, member_type, status, payment_status, locker_id
        FROM members ORDER BY id
    """).fetchall()
    if members:
        for m in members:
            locker_str = f"Locker {m['locker_id']}" if m['locker_id'] else "No locker"
            print(f"  ID {m['id']}: {m['full_name']:25} | {m['member_type']:8} | "
                  f"{m['status']:10} | {m['payment_status']:8} | {locker_str}")
    else:
        print("  (No members)")
    
    # Lockers
    print("\n🔐 LOCKERS:")
    lockers = conn.execute("""
        SELECT l.id, l.label, l.status, 
               COUNT(m.id) as member_count,
               GROUP_CONCAT(m.full_name) as members
        FROM lockers l
        LEFT JOIN members m ON l.id = m.locker_id
        GROUP BY l.id ORDER BY l.id
    """).fetchall()
    for l in lockers:
        member_info = f" → {l['members']}" if l['members'] else " [empty]"
        print(f"  [{l['id']}] {l['label']:20} ({l['status']:10}){member_info}")
    
    # Stats
    print("\n📈 STATISTICS:")
    stats = {
        "Total Members": conn.execute("SELECT COUNT(*) as c FROM members").fetchone()['c'],
        "Pending Members": conn.execute("SELECT COUNT(*) as c FROM members WHERE status='pending'").fetchone()['c'],
        "Approved Members": conn.execute("SELECT COUNT(*) as c FROM members WHERE status='approved'").fetchone()['c'],
        "Members with Locker": conn.execute("SELECT COUNT(*) as c FROM members WHERE locker_id IS NOT NULL").fetchone()['c'],
        "Total Lockers": conn.execute("SELECT COUNT(*) as c FROM lockers").fetchone()['c'],
        "Available Lockers": conn.execute("SELECT COUNT(*) as c FROM lockers WHERE status='available'").fetchone()['c'],
        "Assigned Lockers": conn.execute("SELECT COUNT(*) as c FROM lockers WHERE status='assigned'").fetchone()['c'],
        "Access Logs": conn.execute("SELECT COUNT(*) as c FROM access_logs").fetchone()['c'],
        "Payments": conn.execute("SELECT COUNT(*) as c FROM payments").fetchone()['c'],
    }
    for key, value in stats.items():
        print(f"  {key:25} {value:5}")
    
    print("\n" + "=" * 80)
    conn.close()

# ============================================================================
# REPAIR UTILITIES
# ============================================================================

def fix_pending_member_assignments():
    """
    VALIDATE pending member locker selections (UPDATED WORKFLOW - March 30, 2026)
    
    NEW FLOW:
    - Members SELECT lockers during registration → locker_id saved
    - Members stay PENDING until admin approves
    - Admin approval keeps the SELECTED locker
    
    This function verifies the workflow is correct (READ-ONLY validation)
    """
    conn = sqlite3.connect('locker.sqlite3')
    conn.row_factory = sqlite3.Row
    
    print("\n🔍 VALIDATING PENDING MEMBER LOCKER SELECTIONS...\n")
    
    # Show pending members with selected lockers (CORRECT behavior)
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
        for m in pending_with_locker:
            print(f"   ✓ ID {m['id']}: {m['full_name']:25} → Locker {m['locker_id']}")
        print("\n→ These members are correctly awaiting admin approval\n")
    else:
        print("ℹ️  No pending members with selected lockers\n")
    
    # Check for anomalies
    anomalies = conn.execute("""
        SELECT id, full_name, status, payment_status, locker_id
        FROM members
        WHERE (status != 'pending' OR payment_status != 'unpaid')
        AND locker_id IS NOT NULL
        AND status != 'approved'
    """).fetchall()
    
    if anomalies:
        print(f"⚠️  Found {len(anomalies)} members in inconsistent state:\n")
        for m in anomalies:
            print(f"   ID {m['id']}: {m['full_name']} | {m['status']} | {m['payment_status']} | Locker {m['locker_id']}\n")
        print("→ Manual review recommended\n")
    else:
        print("✓ No inconsistent states found\n")
    
    conn.close()


def cleanup_lockers(keep_count=4):
    """Remove extra lockers beyond specified count"""
    conn = sqlite3.connect('locker.sqlite3')
    conn.row_factory = sqlite3.Row
    
    print(f"\n🧹 CLEANING LOCKERS (keeping {keep_count})...\n")
    
    # Count current
    current = conn.execute("SELECT COUNT(*) as c FROM lockers").fetchone()['c']
    if current <= keep_count:
        print(f"✓ Already at {keep_count} lockers (current: {current})\n")
        conn.close()
        return
    
    # Delete extras
    removed = current - keep_count
    conn.execute(f"DELETE FROM lockers WHERE id > {keep_count}")
    conn.execute("DELETE FROM sqlite_sequence WHERE name = 'lockers'")
    conn.execute(f"INSERT INTO sqlite_sequence (name, seq) VALUES ('lockers', {keep_count})")
    conn.commit()
    
    print(f"✓ Removed {removed} locker(s)\n")
    conn.close()

def remove_duplicate_members():
    """Remove duplicate member registrations"""
    conn = sqlite3.connect('locker.sqlite3')
    conn.row_factory = sqlite3.Row
    
    print("\n🧹 REMOVING DUPLICATE MEMBERS...\n")
    
    # Find duplicates
    dupes = conn.execute("""
        SELECT full_name, COUNT(*) as c, GROUP_CONCAT(id) as ids
        FROM members
        GROUP BY full_name
        HAVING c > 1
        ORDER BY c DESC
    """).fetchall()
    
    if not dupes:
        print("✓ No duplicates found\n")
        conn.close()
        return
    
    total_removed = 0
    for dup in dupes:
        ids = list(map(int, dup['ids'].split(',')))
        keep_id = min(ids)
        delete_ids = [id for id in ids if id != keep_id]
        
        print(f"  {dup['full_name']} (keeping ID {keep_id}, removing {len(delete_ids)})")
        for del_id in delete_ids:
            conn.execute("DELETE FROM members WHERE id = ?", (del_id,))
            total_removed += 1
    
    conn.commit()
    print(f"\n✓ Removed {total_removed} duplicate records\n")
    conn.close()

def reset_single_locker(locker_id):
    """Reset a specific locker to 'available'"""
    conn = sqlite3.connect('locker.sqlite3')
    
    try:
        locker_id = int(locker_id)
        conn.execute("UPDATE lockers SET status = 'available' WHERE id = ?", (locker_id,))
        conn.commit()
        print(f"\n✓ Locker {locker_id} status reset to 'available'\n")
    except ValueError:
        print(f"\n✗ Invalid locker ID: {locker_id}\n")
    finally:
        conn.close()

def fix_locker_status():
    """
    Fix deprecated 'assigned' locker status.
    
    Locker status should ONLY be 'available' or 'occupied' (IR sensor state).
    Assignment tracking is done via members.locker_id, not locker status.
    """
    conn = sqlite3.connect('locker.sqlite3')
    conn.row_factory = sqlite3.Row
    
    print("\n🔧 FIXING LOCKER STATUS...\n")
    
    # Get lockers with deprecated 'assigned' status
    assigned = conn.execute(
        "SELECT id, label FROM lockers WHERE status = 'assigned'"
    ).fetchall()
    
    if not assigned:
        print("✓ No lockers with deprecated 'assigned' status found\n")
        conn.close()
        return
    
    print(f"Found {len(assigned)} locker(s) with deprecated 'assigned' status:")
    for l in assigned:
        print(f"  - {l['label']} (ID: {l['id']})")
    
    # Fix them
    conn.execute("UPDATE lockers SET status = 'available' WHERE status = 'assigned'")
    conn.commit()
    
    print(f"\n✓ Fixed: Updated all to 'available' status")
    print("  Locker status now only tracks IR sensor state (available/occupied)")
    print("  Assignment is tracked via members.locker_id only\n")
    conn.close()

# ============================================================================
# MAIN MENU
# ============================================================================

def menu():
    print("\n" + "=" * 70)
    print("DATABASE UTILITIES - CONSOLIDATED")
    print("=" * 70)
    print("\nDIAGNOSTICS:")
    print("  1. Full database inspection")
    print("\nREPAIRS:")
    print("  2. Fix pending member assignments (remove lockers)")
    print("  3. Cleanup extra lockers (keep 4)")
    print("  4. Remove duplicate members")
    print("  5. Reset single locker to available")
    print("  6. Fix deprecated locker 'assigned' status")
    print("\n  0. Exit")
    print("\n" + "-" * 70)

if __name__ == "__main__":
    while True:
        menu()
        try:
            choice = input("Select option (0-6): ").strip()
            
            if choice == "1":
                inspect_database()
            elif choice == "2":
                fix_pending_member_assignments()
            elif choice == "3":
                cleanup_lockers()
            elif choice == "4":
                remove_duplicate_members()
            elif choice == "5":
                locker_id = input("Enter locker ID: ").strip()
                reset_single_locker(locker_id)
            elif choice == "6":
                fix_locker_status()
            elif choice == "0":
                print("\n✓ Goodbye!\n")
                break
            else:
                print("✗ Invalid option\n")
        except KeyboardInterrupt:
            print("\n\n✓ Exiting...\n")
            break
        except Exception as e:
            print(f"\n✗ Error: {e}\n")
