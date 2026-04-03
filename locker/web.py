from __future__ import annotations

from datetime import datetime, timedelta
from flask import Flask, redirect, render_template, request, session, url_for

from locker.db import connect, init_db
from locker.device import get_device_controller
from locker.admin import admin_bp


def create_app() -> Flask:
    app = Flask(__name__)
    # Simple secret key for session handling (admin login). Replace in production.
    app.secret_key = "locker-secret-change-me"
    init_db()
    device = get_device_controller()

    # Register blueprints
    app.register_blueprint(admin_bp)

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
        """Find MEMBER by fingerprint (requires approval + payment)."""
        with connect() as conn:
            return conn.execute(
                "SELECT * FROM members WHERE fingerprint_uid = ? AND member_type = 'regular' AND status = 'approved' AND payment_status = 'paid'",
                (fp_uid,),
            ).fetchone()

    @app.post("/device/rfid")
    def device_rfid():
        """Handle RFID card access for GUESTS (no approval needed)."""
        data = request.get_json(silent=True) or {}
        uid = (data.get("uid") or "").strip()
        if not uid:
            return {"error": "missing uid"}, 400

        # Find guest by RFID
        guest_row = _find_guest_by_rfid(uid)
        guest = dict(guest_row) if guest_row else None
        
        if not guest:
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
        device.unlock(int(locker_id))
        with connect() as conn:
            conn.execute(
                "INSERT INTO access_logs (actor_type, actor_ref, action, detail) VALUES (?,?,?,?)",
                ("guest", str(guest["id"]), "rfid_access_granted", f"locker_id={locker_id}; rfid={uid}"),
            )
        return {"status": "unlocked", "locker_id": locker_id}

    @app.post("/device/fingerprint")
    def device_fingerprint():
        """Handle fingerprint access for MEMBERS (requires approval + payment)."""
        data = request.get_json(silent=True) or {}
        uid = (data.get("uid") or "").strip()
        if not uid:
            return {"error": "missing uid"}, 400

        # Find member by fingerprint (must be approved and paid)
        member_row = _find_member_by_fingerprint(uid)
        member = dict(member_row) if member_row else None
        
        if not member:
            with connect() as conn:
                conn.execute(
                    "INSERT INTO access_logs (actor_type, actor_ref, action, detail) VALUES (?,?,?,?)",
                    ("fingerprint", uid, "access_denied", "unrecognized_fingerprint_or_not_approved_or_unpaid"),
                )
            return {"status": "denied"}, 403

        locker_id = member["locker_id"]
        if not locker_id:
            return {"status": "failed", "reason": "no locker assigned"}, 409

        device.unlock(int(locker_id))
        with connect() as conn:
            conn.execute(
                "INSERT INTO access_logs (actor_type, actor_ref, action, detail) VALUES (?,?,?,?)",
                ("member", str(member["id"]), "fingerprint_access_granted", f"locker_id={locker_id}; fingerprint={uid}"),
            )
        return {"status": "unlocked", "locker_id": locker_id}

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
        
        # Update locker status in database + log IR status update
        with connect() as conn:
            # Update locker status
            conn.execute(
                "UPDATE lockers SET status = ? WHERE id = ?",
                (status, locker_id),
            )
            # Log IR status update
            conn.execute(
                "INSERT INTO access_logs (actor_type, actor_ref, action, detail) VALUES (?,?,?,?)",
                ("ir_sensor", uid, "occupancy_report", f"status={status}; ir_1={data.get('ir_1')}; ir_2={data.get('ir_2')}"),
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
            "created_at": datetime.utcnow().isoformat(timespec="seconds"),
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

        fingerprint_uid = (request.form.get("fingerprint_uid") or "").strip()
        with connect() as conn:
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
                    (datetime.utcnow() + timedelta(days=30)).isoformat(),  # Membership valid 30 days after approval
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
        # Placeholder: later this will trigger fingerprint auth flow.
        return render_template("pages/user_access.html")

    return app


def main() -> None:
    app = create_app()
    # Listen on 0.0.0.0 to allow ESP32 hardware to connect from network
    app.run(host="0.0.0.0", port=5000, debug=True)


if __name__ == "__main__":
    main()

