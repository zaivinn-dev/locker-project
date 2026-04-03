# 🗄️ Database Administration Scripts

Clean, organized database management tools for the Locker Project. All database utilities are consolidated in **one utility script**.

## 📁 Directory Structure

```
scripts/database/
├── 01_diagnose.py              # Database inspection functions
├── 02_maintenance.py            # Cleanup and maintenance tasks  
├── 03_locker_assignment.py     # Locker-to-member management (deprecated)
├── 04_migrate_10to4_lockers.py  # Migration: 10→4 lockers
├── 05_fix_pending_members.py   # Fix pending member assignments
├── utils.py                     # 🎯 CONSOLIDATED UTILITIES (main entry point)
└── README.md                    # This file
```

## ⚡ Quick Start

### Use the Consolidated Utils
```bash
python scripts/database/utils.py
```
**Single unified entry point** - all diagnosis and repair tools in ONE menu-driven interface.

---

## 🎯 CONSOLIDATED UTILITIES (utils.py)

All database utilities are now in **one convenient module**.

### Diagnostics
- **Full Database Inspection** - See all tables, members, lockers, statistics
- Clean output showing current state
- Identifies problems like pending members with lockers

### Repairs
- **Fix Pending Member Assignments** - Remove lockers from members not yet approved
- **Cleanup Extra Lockers** - Keep only 4 physical lockers
- **Remove Duplicate Members** - Clean up duplicate registrations
- **Reset Single Locker** - Mark a specific locker as available
- **Fix Locker Status** - Remove deprecated 'assigned' status (locker status should only be available/occupied)

---

## 📋 Module Reference

### 01_diagnose.py
Database inspection functions (read-only).

**Functions:**
- `show_tables()` - List all tables with row counts
- `show_lockers()` - Show all lockers and assignments
- `show_members()` - Display all members with details
- `show_access_logs(limit=20)` - Recent activity
- `database_summary()` - Statistics overview

### 02_maintenance.py
Cleanup and maintenance operations.

**Functions:**
- `reset_locker(locker_id)` - Reset locker to 'available'
- `clear_expired_access()` - Remove expired member access
- `remove_duplicate_registrations()` - Clean duplicates

### 03_locker_assignment.py  
**⚠️ DEPRECATED** - Do NOT auto-assign to pending members.
Lockers should only be assigned when admin approves via `/admin/members/<id>/approve`.

### 04_migrate_10to4_lockers.py
One-time migration script to reduce from 10 to 4 lockers.

### 05_fix_pending_members.py
One-time fix to unassign pending members from lockers.

### admin.py
Interactive master control panel - combines all utilities into one menu interface.

---

## ✅ Common Tasks

### Check Database Health
```bash
python utils.py
→ Option 1: Full database inspection
```

### Fix Pending Members with Lockers
```bash
python utils.py
→ Option 2: Fix pending member assignments
```

### Clean Up Extra Lockers
```bash
python utils.py
→ Option 3: Cleanup extra lockers (keep 4)
```

### Remove Duplicates
```bash
python utils.py
→ Option 4: Remove duplicate members
```

---

## 🗂️ Root Directory (Cleaned Up)

❌ **These old scattered scripts have been DELETED:**
- ~~fix_db.py~~ → Use `utils.py`
- ~~fix_locker.py~~ → Use `utils.py`
- ~~fix_expired.py~~ → Use `02_maintenance.py`
- ~~check_db.py~~ → Use `utils.py`
- ~~check_locker.py~~ → Use `utils.py`
- ~~clean_lockers.py~~ → Use `utils.py`
- ~~cleanup_duplicates.py~~ → Use `utils.py`
- ~~assign_lockers.py~~ → DEPRECATED

✅ **All functionality consolidated into organized scripts/database/**

---

## 📖 How Scripts Were Organized

**Before:** 8+ scattered files in project root (messy)
```
project-root/
├── fix_db.py
├── fix_locker.py
├── check_db.py
├── clean_lockers.py
├── ... (more scattered files)
└── locker/
    └── app.py
```

**After:** Everything organized (clean)
```
project-root/
├── scripts/
│   └── database/
│       ├── 01_diagnose.py
│       ├── 02_maintenance.py
│       ├── utils.py  ← All utilities consolidated here
│       ├── admin.py
│       └── README.md
└── locker/
    └── app.py
```

---

## 🎓 Best Practices

1. **Use `utils.py`** for quick diagnosis and fixes
2. **Use `admin.py`** for comprehensive menu interface
3. **Always diagnose first** (Option 1) before making repairs
4. **No admin approval?** Pending members get NO locker (by design)
5. **Only 4 lockers** → Physical hardware limitation

---

## 🚀 Database Rules (Enforced)

✅ **Approved members** → Can have locker (via admin approval)  
❌ **Pending members** → NO locker assigned (not approved yet)  
✅ **4 lockers only** → (04_migrate_10to4_lockers.py removes extras)  
✅ **No duplicates** → (02_maintenance.py checks & cleans)  

---

## 📞 Using These Tools in Python

```python
# Import directly from organized modules
from scripts.database.py01_diagnose import show_lockers, database_summary
from scripts.database.py02_maintenance import reset_locker, clear_expired_access
from scripts.database.utils import inspect_database, fix_pending_member_assignments

# Run diagnostics
database_summary()
show_lockers()

# Run repairs
fix_pending_member_assignments()
reset_locker(1)
```

---

**Last Updated:** 2024  
**All scattered files have been consolidated ✅**
