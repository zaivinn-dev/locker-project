"""
Database Diagnostic Functions
View-only tools to inspect database state
"""

from locker.db import connect

def show_tables():
    """List all tables in the database"""
    conn = connect()
    tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    print("📊 Database Tables:")
    for table in tables:
        count = conn.execute(f"SELECT COUNT(*) as c FROM {table[0]}").fetchone()["c"]
        print(f"  - {table[0]}: {count} records")
    conn.close()

def show_lockers():
    """Show all locker status and assignments"""
    conn = connect()
    lockers = conn.execute("""
        SELECT l.id, l.label, l.status,
               GROUP_CONCAT(m.full_name, ', ') as members
        FROM lockers l
        LEFT JOIN members m ON l.id = m.locker_id
        GROUP BY l.id
        ORDER BY l.id
    """).fetchall()
    print("\n🔐 Locker Status:")
    for locker in lockers:
        l = dict(locker)
        status_icon = "✓" if l['status'] == 'available' else "✗"
        member_info = f" → {l['members']}" if l['members'] else " [empty]"
        print(f"  {status_icon} {l['label']}: {l['status']}{member_info}")
    conn.close()

def show_members():
    """Show all members with details"""
    conn = connect()
    members = conn.execute("""
        SELECT id, full_name, member_type, locker_id, status, payment_status 
        FROM members 
        ORDER BY id DESC
    """).fetchall()
    print("\n👥 All Members:")
    for member in members:
        m = dict(member)
        locker_info = f"Locker {m['locker_id']}" if m['locker_id'] else "No locker"
        status_icon = "✅" if m['status'] == 'approved' and m['payment_status'] == 'paid' else "⏳"
        print(f"  {status_icon} ID {m['id']}: {m['full_name']:20} | {m['member_type']:8} | {m['status']:10} | {locker_info}")
    conn.close()

def show_access_logs(limit=20):
    """Show recent access logs"""
    conn = connect()
    logs = conn.execute("""
        SELECT id, actor_type, action, created_at 
        FROM access_logs 
        ORDER BY id DESC 
        LIMIT ?
    """, (limit,)).fetchall()
    print(f"\n📝 Recent Access Logs ({limit} entries):")
    for log in logs:
        l = dict(log)
        print(f"  [{l['id']}] {l['actor_type']:8} | {l['action']:25} | {l['created_at']}")
    conn.close()

def database_summary():
    """Show overall database statistics"""
    conn = connect()
    stats = {
        "Total Members": conn.execute("SELECT COUNT(*) as c FROM members").fetchone()["c"],
        "Pending": conn.execute("SELECT COUNT(*) as c FROM members WHERE status='pending'").fetchone()["c"],
        "Approved": conn.execute("SELECT COUNT(*) as c FROM members WHERE status='approved'").fetchone()["c"],
        "Total Lockers": conn.execute("SELECT COUNT(*) as c FROM lockers").fetchone()["c"],
        "Available": conn.execute("SELECT COUNT(*) as c FROM lockers WHERE status='available'").fetchone()["c"],
        "Assigned": conn.execute("SELECT COUNT(*) as c FROM lockers WHERE status='assigned'").fetchone()["c"],
    }
    conn.close()
    
    print("\n" + "=" * 50)
    print("DATABASE SUMMARY")
    print("=" * 50)
    for key, value in stats.items():
        print(f"  {key:20} {value:5}")
    print("=" * 50)

if __name__ == "__main__":
    database_summary()
    show_tables()
    show_lockers()
    show_members()
    show_access_logs()
