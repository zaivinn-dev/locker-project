#!/usr/bin/env python3
"""
Database script for diagnosing and fixing fingerprint access issues.
Usage: python scripts/database/06_fix_fingerprint_uid.py
"""

import sqlite3
import sys
from pathlib import Path

# Add the locker directory to the path so we can import db.py
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "locker"))

from db import connect

def diagnose_fingerprint_issues():
    """Diagnose fingerprint-related issues in the database."""
    print("🔍 Diagnosing fingerprint access issues...")

    with connect() as conn:
        # Check members with fingerprint_uid
        members_with_fp = conn.execute("""
            SELECT id, full_name, fingerprint_uid, status, payment_status, member_type, locker_id
            FROM members
            WHERE fingerprint_uid IS NOT NULL AND fingerprint_uid != ''
            ORDER BY id
        """).fetchall()

        print(f"\n📊 Found {len(members_with_fp)} members with fingerprint data:")
        for member in members_with_fp:
            status_icon = "✅" if member['status'] == 'approved' and member['payment_status'] == 'paid' else "⏳"
            print(f"  {status_icon} ID {member['id']}: {member['full_name']} (FP: {member['fingerprint_uid']}, Status: {member['status']}/{member['payment_status']})")

        # Check for potential issues
        issues = []

        # Members with fingerprint but not approved
        unapproved_with_fp = [m for m in members_with_fp if m['status'] != 'approved' or m['payment_status'] != 'paid']
        if unapproved_with_fp:
            issues.append(f"⚠️  {len(unapproved_with_fp)} members have fingerprints but are not fully approved")

        # Check for duplicate fingerprint_uid
        fp_uids = [m['fingerprint_uid'] for m in members_with_fp]
        duplicates = [uid for uid in set(fp_uids) if fp_uids.count(uid) > 1]
        if duplicates:
            issues.append(f"🚨 Duplicate fingerprint_uids found: {duplicates}")

        if issues:
            print("\n🚨 Issues found:")
            for issue in issues:
                print(f"  {issue}")
        else:
            print("\n✅ No obvious issues found with fingerprint data")

def fix_fingerprint_uid(member_id: int, new_uid: str):
    """Update a member's fingerprint_uid."""
    print(f"🔧 Updating fingerprint_uid for member ID {member_id} to '{new_uid}'...")

    with connect() as conn:
        # Check if member exists
        member = conn.execute("SELECT id, full_name, fingerprint_uid FROM members WHERE id = ?", (member_id,)).fetchone()
        if not member:
            print(f"❌ Member ID {member_id} not found")
            return False

        old_uid = member['fingerprint_uid']
        conn.execute("UPDATE members SET fingerprint_uid = ? WHERE id = ?", (new_uid, member_id))

        print(f"✅ Updated {member['full_name']}'s fingerprint_uid from '{old_uid}' to '{new_uid}'")
        return True

def clear_all_fingerprints():
    """Clear all fingerprint_uid from members (use after sensor reset)."""
    print("🧹 Clearing all fingerprint_uid from database...")

    with connect() as conn:
        # Get count before clearing
        count_before = conn.execute("SELECT COUNT(*) FROM members WHERE fingerprint_uid IS NOT NULL AND fingerprint_uid != ''").fetchone()[0]
        
        # Clear all fingerprint_uid
        conn.execute("UPDATE members SET fingerprint_uid = NULL WHERE fingerprint_uid IS NOT NULL")
        
        print(f"✅ Cleared fingerprint_uid from {count_before} members")
        print("💡 Members will need to re-enroll fingerprints after sensor reset")

def main():
    """Main function with command-line interface."""
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python scripts/database/06_fix_fingerprint_uid.py diagnose")
        print("  python scripts/database/06_fix_fingerprint_uid.py fix <member_id> <new_fingerprint_uid>")
        print("  python scripts/database/06_fix_fingerprint_uid.py clear_all")
        return

    command = sys.argv[1].lower()

    if command == "diagnose":
        diagnose_fingerprint_issues()
    elif command == "fix" and len(sys.argv) == 4:
        try:
            member_id = int(sys.argv[2])
            new_uid = sys.argv[3]
            success = fix_fingerprint_uid(member_id, new_uid)
            if success:
                print("\n💡 Tip: Restart the web server if changes don't take effect immediately")
        except ValueError:
            print("❌ Invalid member ID - must be a number")
    elif command == "clear_all":
        clear_all_fingerprints()
    else:
        print("❌ Invalid command or arguments")
        print("Use 'diagnose' to check for issues, 'fix <id> <uid>' to update a fingerprint_uid, or 'clear_all' to reset all fingerprints")

if __name__ == "__main__":
    main()