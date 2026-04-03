"""
Locker Assignment Scripts
Manage locker assignments to members
"""

from locker.db import connect

def auto_assign_lockers_to_pending():
    """
    DEPRECATED: Lockers should ONLY be assigned when admin approves a member.
    This function should NOT auto-assign to pending members.
    Use the /admin/members/<id>/approve endpoint instead.
    """
    print("⚠️  WARNING: Do NOT auto-assign lockers to pending members!")
    print("    Pending members have not been approved and may not be able to pay.")
    print("    Please use: /admin/members/<id>/approve endpoint instead.")
    print("    This will assign an available locker during approval.")
    return 0

def unassign_member_locker(member_id):
    """
    Remove locker assignment from a member and mark locker as available
    Usage: unassign_member_locker(13)
    """
    conn = connect()
    
    member = conn.execute("SELECT full_name, locker_id FROM members WHERE id = ?", (member_id,)).fetchone()
    
    if not member:
        print(f"✗ Member ID {member_id} not found")
        conn.close()
        return False
    
    m = dict(member)
    locker_id = m['locker_id']
    
    if not locker_id:
        print(f"✓ Member {m['full_name']} has no locker assigned")
        conn.close()
        return False
    
    # Clear assignment
    conn.execute("UPDATE members SET locker_id = NULL WHERE id = ?", (member_id,))
    conn.execute("UPDATE lockers SET status = 'available' WHERE id = ?", (locker_id,))
    
    conn.commit()
    conn.close()
    print(f"✓ Unassigned Locker {locker_id} from {m['full_name']}")
    return True

def show_locker_assignments():
    """
    Show all current locker assignments
    """
    conn = connect()
    
    print("\n📋 Locker Assignments:")
    assignments = conn.execute("""
        SELECT 
            l.id, l.label, l.status,
            COALESCE(m.full_name, 'Unassigned') as member,
            m.member_type,
            m.status as member_status
        FROM lockers l
        LEFT JOIN members m ON l.id = m.locker_id
        ORDER BY l.id
    """).fetchall()
    
    for assign in assignments:
        a = dict(assign)
        if a['member_status']:
            print(f"  Locker {a['id']} ({a['label']}): {a['member']} [{a['member_type']}] ({a['member_status']})")
        else:
            print(f"  Locker {a['id']} ({a['label']}): Available")
    
    conn.close()

if __name__ == "__main__":
    print("=" * 60)
    print("LOCKER ASSIGNMENT MANAGER")
    print("=" * 60)
    
    print("\n1️⃣  Auto-assigning lockers to pending members...")
    auto_assign_lockers_to_pending()
    
    print("\n2️⃣  Current assignments:")
    show_locker_assignments()
    
    print("\n" + "=" * 60)
