# Debug Scripts

Utility scripts for debugging and inspecting the locker system database.

## Scripts

### `inspect_database.py`
**Purpose**: Comprehensive database inspection tool

**Usage**:
```bash
cd d:\project\locker-project
python scripts/debug/inspect_database.py
```

**Shows**:
- ✅ All guest records with expiry status
- ✅ Locker assignments and availability
- ✅ Auto-deletion event logs

### `test_auto_deletion.py`
**Purpose**: Test the auto-deletion functionality

**Usage**:
```bash
# Make sure Flask is running first
python -m locker.web &

# Then run the test
python scripts/debug/test_auto_deletion.py

# Check results
python scripts/debug/inspect_database.py
```

**Tests**:
- ✅ Makes request to `/admin/rfid` endpoint
- ✅ Triggers auto-deletion logic
- ✅ Logs success/failure

## Workflow

### 1. Check Current Status
```bash
python scripts/debug/inspect_database.py
```

### 2. Test Auto-Deletion
```bash
# Terminal 1: Start Flask
python -m locker.web

# Terminal 2: Run test
python scripts/debug/test_auto_deletion.py

# Check results
python scripts/debug/inspect_database.py
```

### 3. Verify Results
- Expired guests should show as "❌ EXPIRED"
- After running `test_auto_deletion.py`, they should be deleted
- Lockers should show as "✅ AVAILABLE"

## Example Output

```
============================================================
🔍 EXPIRED GUESTS CHECK
============================================================
Current local time: 2026-04-01 22:30:00.123456

Total guests: 2

✅ ACTIVE   | ID:  48 | vinzzz          | RFID: E49113BB | Expires: 2026-04-02T11:39:21.399931
✅ ACTIVE   | ID:  47 | marcel          | RFID: 98C357AD | Expires: 2026-04-02T11:38:51.032485

⚠️  Expired guests that should auto-delete: 0

============================================================
🗂️  LOCKER ASSIGNMENTS
============================================================

✅ Locker 1: available   | — EMPTY
✅ Locker 2: available   | — EMPTY
✅ Locker 3: available   | — EMPTY
✅ Locker 4: available   | — EMPTY

============================================================
📋 AUTO-DELETION LOGS
============================================================

[5604] 2026-04-01 14:26:58
     rfid=BB31D306 freed for reuse (expired at 2026-04-01T22:07:58.177887)
```

## Notes

- Database is at `d:\project\locker-project\locker.sqlite3`
- All times are in **local timezone** (Asia/Manila)
- Auto-deletion happens when admin accesses `/admin/rfid` or `/admin/lockers` pages
- Lockers automatically become "available" when guests expire
