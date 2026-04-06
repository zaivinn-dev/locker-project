from __future__ import annotations

from datetime import datetime, timedelta, timezone
from flask import Flask, redirect, render_template, request, session, url_for

try:
    from .db import connect, init_db
    from .device import get_device_controller
    from .admin import admin_bp
except ImportError:
    from db import connect, init_db
    from device import get_device_controller
    from admin import admin_bp

# Global device controller instance - lazy initialized
device = None

def get_device():
    global device
    if device is None:
        device = get_device_controller()
    return device

# Runtime enrollment state shared between browser and ESP32 polling
fingerprint_enrollment_state = {
    "pending": False,
    "enrolled_uid": None,
    "step": 0,
    "error": None,
    "active": False,  # New: indicates if enrollment is currently active
}

# Runtime access status shared between the fingerprint scanner and the browser access page
access_status_state = {
    "state": "waiting",
    "locker_id": None,
    "message": "Awaiting fingerprint scan",
    "updated_at": None,
}

# Runtime scan control - fingerprint scanning only enabled when user is actively on member access page
scan_control_state = {
    "enabled": False,
    "last_enabled": None,
    "timeout_seconds": 60,  # Disable scanning after 60 seconds of inactivity
}

def create_app() -> Flask:
    app = Flask(__name__)
    # Simple secret key for session handling (admin login). Replace in production.
    app.secret_key = "locker-secret-change-me"
    init_db()
    # device is now initialized globally

    # Register blueprints
    app.register_blueprint(admin_bp, url_prefix="/admin")

    # Track a single pending fingerprint enrollment command for ESP32 polling.
    # This avoids accidental enrollment starting unless the browser explicitly requested it.
    fingerprint_enrollment_state["pending"] = False
    fingerprint_enrollment_state["enrolled_uid"] = None
    fingerprint_enrollment_state["step"] = 0
    fingerprint_enrollment_state["error"] = None
    fingerprint_enrollment_state["active"] = False

    # Helper functions
    def _find_member_by_fingerprint(fp_uid: str):
        """Find MEMBER by fingerprint for locker access."""
        with connect() as conn:
            return conn.execute(
                "SELECT * FROM members WHERE fingerprint_uid = ? AND member_type = 'regular' AND status = 'approved' AND payment_status = 'paid'",
                (fp_uid,),
            ).fetchone()

    def _clear_registration_draft() -> None:
        session.pop("registration_draft", None)

    def _find_guest_by_rfid(rfid_uid: str):
        """Find GUEST by RFID (only returns active/non-expired guests)."""
        with connect() as conn:
            return conn.execute(
                "SELECT * FROM members WHERE rfid_uid = ? AND member_type = 'guest' AND expiry_date >= datetime('now')",
                (rfid_uid,),
            ).fetchone()

    def _find_member_by_fingerprint(fp_uid: str):
        """Find MEMBER by fingerprint for locker access."""
        with connect() as conn:
            return conn.execute(
                "SELECT * FROM members WHERE fingerprint_uid = ? AND member_type = 'regular' AND status = 'approved' AND payment_status = 'paid'",
                (fp_uid,),
            ).fetchone()

    @app.post("/device/rfid")
    def device_rfid():
        """Handle RFID card access for GUESTS ONLY (role security enforced)."""
        data = request.get_json(silent=True) or {}
        uid = (data.get("uid") or "").strip()
        if not uid:
            return {"error": "missing uid"}, 400

        # Find guest by RFID
        guest_row = _find_guest_by_rfid(uid)
        guest = dict(guest_row) if guest_row else None
        
        if not guest:
            # Check if this RFID belongs to a member (role violation)
            with connect() as conn:
                member_check = conn.execute(
                    "SELECT id, full_name FROM members WHERE rfid_uid = ? AND member_type = 'regular'",
                    (uid,),
                ).fetchone()
                
                if member_check:
                    # RFID belongs to a member - log security violation
                    conn.execute(
                        "INSERT INTO access_logs (actor_type, actor_ref, action, detail) VALUES (?,?,?,?)",
                        ("rfid", uid, "security_violation", f"member_rfid_used_as_guest; member_id={member_check['id']}; name={member_check['full_name']}"),
                    )
                    return {"status": "denied", "reason": "rfid_reserved_for_members"}, 403
            
            # RFID not found in system
            with connect() as conn:
                conn.execute(
                    "INSERT INTO access_logs (actor_type, actor_ref, action, detail) VALUES (?,?,?,?)",
                    ("rfid", uid, "access_denied", "unrecognized_rfid_or_not_guest"),
                )
            return {"status": "denied"}, 403

        # Check guest expiry date - deny access if current time is past expiry
        if guest.get("expiry_date"):
            from datetime import datetime
            try:
                expiry_time = datetime.fromisoformat(guest["expiry_date"])
                if datetime.now() > expiry_time:  # Simple check: if now is past expiry, deny
                    with connect() as conn:
                        conn.execute(
                            "INSERT INTO access_logs (actor_type, actor_ref, action, detail) VALUES (?,?,?,?)",
                            ("guest", str(guest["id"]), "access_denied", f"guest_access_expired; rfid={uid}; expiry={guest['expiry_date']}"),
                        )
                    return {"status": "denied", "reason": "access_expired"}, 403
            except (ValueError, TypeError):
                # Invalid expiry_date format - deny access to be safe
                with connect() as conn:
                    conn.execute(
                        "INSERT INTO access_logs (actor_type, actor_ref, action, detail) VALUES (?,?,?,?)",
                        ("guest", str(guest["id"]), "access_denied", f"invalid_expiry_date; rfid={uid}"),
                    )
                return {"status": "denied", "reason": "invalid_expiry_date"}, 403

        # Check if guest has locker assigned
        locker_id = guest["locker_id"]
        if not locker_id:
            return {"status": "failed", "reason": "no locker assigned"}, 409

        # Unlock the locker
        get_device().unlock(int(locker_id))
        with connect() as conn:
            conn.execute(
                "INSERT INTO access_logs (actor_type, actor_ref, action, detail) VALUES (?,?,?,?)",
                ("guest", str(guest["id"]), "rfid_access_granted", f"locker_id={locker_id}; rfid={uid}"),
            )
        return {"status": "unlocked", "locker_id": locker_id}

    @app.post("/device/fingerprint")
    def device_fingerprint():
        """Handle fingerprint access for MEMBERS ONLY (role security enforced)."""
        data = request.get_json(silent=True) or {}
        uid = (data.get("uid") or "").strip()
        if not uid:
            return {"error": "missing uid"}, 400

        # Check if fingerprint scanning is currently enabled
        last_enabled = scan_control_state.get("last_enabled")
        timeout_seconds = scan_control_state.get("timeout_seconds", 60)
        
        if not last_enabled or (datetime.now(timezone.utc) - last_enabled).total_seconds() >= timeout_seconds:
            print(f"[FINGERPRINT ACCESS] ✗ REJECTED - Fingerprint scanning not enabled (must use Member Access page)")
            return {"status": "rejected", "reason": "scanning_not_enabled"}, 403

        print(f"[FINGERPRINT ACCESS] Received fingerprint UID: {uid}")

        # Find member by fingerprint (must be approved and paid)
        member_row = _find_member_by_fingerprint(uid)
        member = dict(member_row) if member_row else None
        
        if not member:
            # Check if this fingerprint belongs to a guest (role violation)
            with connect() as conn:
                guest_check = conn.execute(
                    "SELECT id, full_name FROM members WHERE fingerprint_uid = ? AND member_type = 'guest'",
                    (uid,),
                ).fetchone()
                
                if guest_check:
                    # Fingerprint belongs to a guest - log security violation
                    conn.execute(
                        "INSERT INTO access_logs (actor_type, actor_ref, action, detail) VALUES (?,?,?,?)",
                        ("fingerprint", uid, "security_violation", f"guest_fingerprint_used_as_member; guest_id={guest_check['id']}; name={guest_check['full_name']}"),
                    )
                    return {"status": "denied", "reason": "fingerprint_reserved_for_guests"}, 403
            
            # Fingerprint not found in system
            with connect() as conn:
                conn.execute(
                    "INSERT INTO access_logs (actor_type, actor_ref, action, detail) VALUES (?,?,?,?)",
                    ("fingerprint", uid, "access_denied", "unrecognized_fingerprint_or_not_registered"),
                )
            print(f"[FINGERPRINT ACCESS] Fingerprint UID {uid} NOT FOUND or not registered for member access")
            # Store access status for polling in shared server state
            access_status_state["state"] = "denied"
            access_status_state["locker_id"] = None
            access_status_state["message"] = "Fingerprint not registered for member access"
            access_status_state["updated_at"] = datetime.now(timezone.utc)
            return {"status": "denied"}, 403

        locker_id = member["locker_id"]
        if not locker_id:
            return {"status": "failed", "reason": "no locker assigned"}, 409

        print(f"[FINGERPRINT ACCESS] ✓ Member {member['full_name']} (ID={member['id']}) matched - unlocking locker {locker_id}")
        get_device().unlock(int(locker_id))
        with connect() as conn:
            conn.execute(
                "INSERT INTO access_logs (actor_type, actor_ref, action, detail) VALUES (?,?,?,?)",
                ("member", str(member["id"]), "fingerprint_access_granted", f"locker_id={locker_id}; fingerprint={uid}"),
            )
        # Store access status for polling in shared server state
        access_status_state["state"] = "unlocked"
        access_status_state["locker_id"] = locker_id
        access_status_state["message"] = "Unlock command sent - opening locker"
        access_status_state["updated_at"] = datetime.now(timezone.utc)
        return {"status": "unlocked", "locker_id": locker_id}

    @app.post("/device/fingerprint/enroll")
    def device_fingerprint_enroll():
        """Handle fingerprint enrollment during registration."""
        data = request.get_json(silent=True) or {}
        uid = (data.get("uid") or "").strip()
        step = data.get("step", 1)  # 1=first scan, 2=second scan/enrolled
        
        if not uid:
            return {"error": "missing uid"}, 400

        if step == 1:
            # First scan completed
            fingerprint_enrollment_state["step"] = 1
            fingerprint_enrollment_state["error"] = None
            print(f"First fingerprint scan completed, uid={uid}")
            return {"status": "first_scan_complete", "fingerprint_uid": uid}
        else:
            # Enrollment completed (second scan)
            fingerprint_enrollment_state["enrolled_uid"] = uid
            fingerprint_enrollment_state["pending"] = False
            fingerprint_enrollment_state["active"] = False  # Clear active flag
            fingerprint_enrollment_state["step"] = 2
            fingerprint_enrollment_state["error"] = None
            print(f"Fingerprint enrolled from device, uid={uid}")

            with connect() as conn:
                conn.execute(
                    "INSERT INTO access_logs (actor_type, actor_ref, action, detail) VALUES (?,?,?,?)",
                    ("system", "enrollment", "fingerprint_enrolled", f"fingerprint_uid={uid}"),
                )
            
            return {"status": "enrolled", "fingerprint_uid": uid}

    @app.post("/device/fingerprint/request-enrollment")
    def device_fingerprint_request_enrollment():
        """Request fingerprint enrollment mode for the ESP32."""
        data = request.get_json(silent=True) or {}
        action = data.get("action", "start")
        
        if action == "cancel":
            # Cancel enrollment - reset all states
            _reset_enrollment_state()
            print("Enrollment cancelled by user")
            return {"status": "cancelled"}
        
        # Reset any previous enrollment state first
        _reset_enrollment_state()
        
        fingerprint_enrollment_state["pending"] = True
        fingerprint_enrollment_state["enrolled_uid"] = None
        fingerprint_enrollment_state["step"] = 0
        fingerprint_enrollment_state["error"] = None
        fingerprint_enrollment_state["active"] = False
        print("Frontend requested enrollment - flag set to True")
        print(f"Current enrollment state: pending={fingerprint_enrollment_state['pending']}, enrolled_uid={fingerprint_enrollment_state['enrolled_uid']}")
        return {"status": "pending"}

    @app.post("/device/fingerprint/start-enrollment")
    def device_fingerprint_start_enrollment():
        """Check if enrollment should be started (polled by ESP32)."""
        data = request.get_json(silent=True) or {}
        action = data.get("action", "check")
        
        if action == "stop":
            # ESP32 is stopping enrollment
            fingerprint_enrollment_state["active"] = False
            fingerprint_enrollment_state["pending"] = False
            print("ESP32 stopped enrollment")
            return {"status": "enrollment_stopped"}
        
        pending = fingerprint_enrollment_state.get("pending", False)
        print(f"ESP32 polling for enrollment - pending: {pending}")
        if pending:
            fingerprint_enrollment_state["pending"] = False  # Clear the flag
            fingerprint_enrollment_state["active"] = True   # Set active flag
            print("Returning enrollment_started to ESP32")
            return {"status": "enrollment_started"}
        print("Returning no_action to ESP32")
        return {"status": "no_action"}

    @app.get("/api/enrollment-status")
    def api_enrollment_status():
        """Check if fingerprint has been enrolled during registration."""
        enrolled_uid = fingerprint_enrollment_state.get("enrolled_uid")
        enrollment_step = fingerprint_enrollment_state.get("step", 0)
        enrollment_error = fingerprint_enrollment_state.get("error")
        enrollment_active = fingerprint_enrollment_state.get("active", False)
        
        return {
            "enrolled": bool(enrolled_uid), 
            "fingerprint_uid": enrolled_uid,
            "step": enrollment_step,  # 0=waiting, 1=first scan done, 2=enrolled
            "error": enrollment_error,
            "active": enrollment_active  # New: indicates if enrollment is currently active
        }

    @app.get("/api/access-status")
    def api_access_status():
        """Check for recent fingerprint access attempts."""
        updated_at = access_status_state.get("updated_at")
        if updated_at:
            age_seconds = (datetime.now(timezone.utc) - updated_at).total_seconds()
            if age_seconds < 30:
                return {
                    "state": access_status_state.get("state", "waiting"),
                    "locker_id": access_status_state.get("locker_id"),
                    "message": access_status_state.get("message", "Awaiting fingerprint scan"),
                }

        # Clear stale status after 30 seconds.
        access_status_state["state"] = "waiting"
        access_status_state["locker_id"] = None
        access_status_state["message"] = "Awaiting fingerprint scan"
        access_status_state["updated_at"] = None
        return {"state": "waiting", "locker_id": None, "message": "Awaiting fingerprint scan"}

    @app.post("/api/access/enable-scan")
    def api_enable_scan():
        """Enable fingerprint scanning when user enters member access page."""
        scan_control_state["enabled"] = True
        scan_control_state["last_enabled"] = datetime.now(timezone.utc)
        return {"status": "scan_enabled"}

    @app.post("/api/access/disable-scan")
    def api_disable_scan():
        """Disable fingerprint scanning when user leaves member access page."""
        scan_control_state["enabled"] = False
        scan_control_state["last_enabled"] = None
        return {"status": "scan_disabled"}

    @app.get("/api/access/scan-enabled")
    def api_scan_enabled():
        """Check if fingerprint scanning is currently enabled."""
        last_enabled = scan_control_state.get("last_enabled")
        timeout_seconds = scan_control_state.get("timeout_seconds", 60)
        
        if last_enabled:
            elapsed = (datetime.now(timezone.utc) - last_enabled).total_seconds()
            if elapsed < timeout_seconds:
                # Still within timeout period - scanning enabled
                return {"enabled": True, "elapsed_seconds": int(elapsed)}

        # Timeout reached or never enabled - disable scanning
        scan_control_state["enabled"] = False
        scan_control_state["last_enabled"] = None
        return {"enabled": False, "elapsed_seconds": 0}

    @app.post("/device/ir-status")
    def device_ir_status():
        """Handle IR sensor occupancy status updates."""
        data = request.get_json(silent=True) or {}
        uid = (data.get("uid") or "").strip()
        status = (data.get("status") or "").strip()
        
        if not uid or not status:
            return {"error": "missing uid or status"}, 400
        
        if status not in ["occupied", "available"]:
            return {"error": "invalid status (must be 'occupied' or 'available')"}, 400
        
        # Extract locker number from uid (e.g., "locker_1" → 1)
        try:
            locker_id = int(uid.split("_")[-1])
        except (ValueError, IndexError):
            return {"error": "invalid uid format"}, 400
        
        # Update locker status in database
        with connect() as conn:
            conn.execute(
                "UPDATE lockers SET status = ? WHERE id = ?",
                (status, locker_id),
            )
            conn.commit()
        
        return {"success": True, "uid": uid, "status": status}

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/static/favicon.ico")
    def favicon():
        return app.send_static_file("favicon.ico")

    @app.get("/user/register")
    def user_register_form():
        return render_template("pages/user_register.html")

    @app.post("/user/register")
    def user_register_submit():
        full_name = (request.form.get("full_name") or "").strip()
        address = (request.form.get("address") or "").strip()
        contact_number = (request.form.get("contact_number") or "").strip()
        age_raw = (request.form.get("age") or "").strip()
        category = (request.form.get("category") or "").strip()
        error = None
        if not full_name:
            error = "Full name is required."
        elif not address:
            error = "Address is required."
        elif not contact_number:
            error = "Contact number is required."

        age_val = None
        if age_raw:
            try:
                age_val = int(age_raw)
                if age_val <= 0:
                    raise ValueError
            except ValueError:
                error = "Age must be a positive number."

        if error:
            return render_template(
                "pages/user_register.html",
                error=error,
                form_data={
                    "full_name": full_name,
                    "address": address,
                    "contact_number": contact_number,
                    "age": age_raw,
                    "category": category,
                },
            )

        # Stage details in session; do not save to DB until fingerprint enrollment completes.
        session["registration_draft"] = {
            "full_name": full_name,
            "address": address,
            "contact_number": contact_number,
            "age": age_val,
            "category": category,
            "locker_id": None,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }

        return redirect(url_for("user_select_locker"))

    @app.get("/user/select-locker")
    def user_select_locker():
        draft = session.get("registration_draft")
        if not isinstance(draft, dict) or not draft.get("full_name"):
            return redirect(url_for("user_register_form"))

        with connect() as conn:
            # Get all 4 lockers with their status and check assignments (member or guest)
            lockers = conn.execute(
                """SELECT id, label, status, 
                   CASE WHEN EXISTS(
                     SELECT 1 FROM members 
                     WHERE locker_id = lockers.id 
                     AND member_type = 'regular' 
                     AND status = 'approved'
                   ) THEN 'member_assigned' 
                   WHEN EXISTS(
                     SELECT 1 FROM members 
                     WHERE locker_id = lockers.id 
                     AND member_type = 'guest' 
                     AND expiry_date > datetime('now')
                   ) THEN 'guest_assigned' 
                   ELSE NULL END as assignment_status
                   FROM lockers ORDER BY id LIMIT 4"""
            ).fetchall()

        return render_template(
            "pages/user_select_locker.html",
            draft=draft,
            lockers=lockers,
        )

    @app.post("/user/select-locker")
    def user_select_locker_submit():
        locker_id_raw = (request.form.get("locker_id") or "").strip()
        draft = session.get("registration_draft")
        if not isinstance(draft, dict) or not draft.get("full_name"):
            return redirect(url_for("user_register_form"))
        if not locker_id_raw.isdigit():
            return redirect(url_for("user_select_locker"))
        locker_id = int(locker_id_raw)

        with connect() as conn:
            # Ensure locker is available (not occupied by IR AND no member/guest assigned)
            locker = conn.execute(
                """SELECT id FROM lockers WHERE id = ? AND status = 'available'
                   AND NOT EXISTS(
                     SELECT 1 FROM members 
                     WHERE locker_id = ? 
                     AND (
                       (member_type = 'regular' AND status = 'approved')
                       OR (member_type = 'guest' AND expiry_date > datetime('now'))
                     )
                   )""",
                (locker_id, locker_id),
            ).fetchone()
            if locker is None:
                return redirect(url_for("user_select_locker"))

        draft["locker_id"] = locker_id
        session["registration_draft"] = draft
        return redirect(url_for("user_enroll_fingerprint"))

    @app.get("/user/enroll-fingerprint")
    def user_enroll_fingerprint():
        draft = session.get("registration_draft")
        if not isinstance(draft, dict) or not draft.get("full_name"):
            return redirect(url_for("user_register_form"))
        if not draft.get("locker_id"):
            return redirect(url_for("user_select_locker"))
        with connect() as conn:
            locker = conn.execute(
                "SELECT id, label FROM lockers WHERE id = ?",
                (int(draft["locker_id"]),),
            ).fetchone()
        return render_template("pages/user_enroll_fingerprint.html", draft=draft, locker=locker)

    @app.post("/user/enroll-fingerprint/complete")
    def user_enroll_fingerprint_complete():
        """
        Finalize registration ONLY after fingerprint enrollment succeeds.
        For now this is a 'complete' action; later wire to the real R307 enrollment result.
        """
        draft = session.get("registration_draft")
        if not isinstance(draft, dict) or not draft.get("full_name") or not draft.get("locker_id"):
            return redirect(url_for("user_register_form"))

        # Get fingerprint_uid from shared enrollment result state
        fingerprint_uid = fingerprint_enrollment_state.get("enrolled_uid")
        if not fingerprint_uid:
            # If no fingerprint enrolled yet, redirect back to enrollment page
            return redirect(url_for("user_enroll_fingerprint"))
        
        with connect() as conn:
            # Check for role conflict: prevent registering as member if person already has RFID as guest
            role_conflict = conn.execute(
                "SELECT id, member_type FROM members WHERE full_name = ? AND rfid_uid IS NOT NULL",
                (draft.get("full_name"),),
            ).fetchone()
            if role_conflict:
                # Log the role violation attempt
                conn.execute(
                    "INSERT INTO access_logs (actor_type, actor_ref, action, detail) VALUES (?,?,?,?)",
                    ("system", "unknown", "role_violation_attempt", f"name={draft.get('full_name')}; attempted=member_registration; conflict=guest_with_rfid"),
                )
                session["registration_error"] = "You are already registered as a guest with RFID access. Please contact admin if you need to change your access method."
                _clear_registration_draft()
                return redirect(url_for("user_register_form"))
            
            # Check if this person already has a pending registration
            existing = conn.execute(
                """
                SELECT id, status FROM members 
                WHERE full_name = ? AND (status = 'pending' OR status = 'approved')
                LIMIT 1
                """,
                (draft.get("full_name"),),
            ).fetchone()
            
            if existing:
                # Already registered, redirect to success or show message
                session["registration_success"] = {
                    "member_id": existing["id"],
                    "member_name": draft.get("full_name"),
                    "is_duplicate": True,
                }
                _clear_registration_draft()
                return redirect(url_for("user_registered_success"))
            
            # Create member with the locker they selected (pending approval)
            cur = conn.execute(
                """
                INSERT INTO members (full_name, address, contact_number, age, category, locker_id, fingerprint_uid, 
                                     status, payment_status, expiry_date, member_type)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    draft.get("full_name"),
                    draft.get("address"),
                    draft.get("contact_number"),
                    draft.get("age"),
                    draft.get("category"),
                    draft.get("locker_id"),  # Store the locker they selected during registration
                    fingerprint_uid if fingerprint_uid else None,
                    "pending",  # Status: pending approval
                    "unpaid",  # Payment: unpaid until admin confirms
                    (datetime.now(timezone.utc) + timedelta(days=30)).isoformat(),  # Membership valid 30 days after approval
                    "regular",  # Member type
                ),
            )
            member_id = cur.lastrowid
            
            # Log registration with selected locker
            conn.execute(
                "INSERT INTO access_logs (actor_type, actor_ref, action, detail) VALUES (?,?,?,?)",
                ("system", str(member_id), "member_registered", f"member_id={member_id}; status=pending; locker_id={draft.get('locker_id')}"),
            )
            conn.execute(
                "INSERT INTO access_logs (actor_type, actor_ref, action, detail) VALUES (?,?,?,?)",
                ("system", str(member_id), "fingerprint_enrolled", "r307=enrolled"),
            )

        # Store success data in session for the success page
        session["registration_success"] = {
            "member_id": member_id,
            "member_name": draft.get("full_name"),
        }
        _clear_registration_draft()
        session.pop("enrolled_fingerprint_uid", None)
        fingerprint_enrollment_state["enrolled_uid"] = None
        return redirect(url_for("user_registered_success"))

    @app.get("/user/registered-success")
    def user_registered_success():
        success_data = session.get("registration_success")
        if not success_data:
            return redirect(url_for("index"))
        return render_template(
            "pages/user_registered_success.html",
            member_name=success_data.get("member_name"),
            is_duplicate=success_data.get("is_duplicate", False),
        )

    @app.post("/user/register/cancel")
    def user_register_cancel():
        _clear_registration_draft()
        return redirect(url_for("index"))

    @app.get("/user/access")
    def user_access():
        # Enable fingerprint scanning whenever the member access page is rendered.
        scan_control_state["enabled"] = True
        scan_control_state["last_enabled"] = datetime.now(timezone.utc)
        return render_template("pages/user_access.html")

    print("About to return app")
    return app


def main() -> None:
    app = create_app()
    # Listen on 0.0.0.0 to allow ESP32 hardware to connect from network
    # Disable the Flask reloader so shared enrollment state remains consistent.
    app.run(host="0.0.0.0", port=5000, debug=True, use_reloader=True)


if __name__ == "__main__":
    main()

