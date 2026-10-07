"""Fifth scenario: role gating on bot.admin handlers. A 'content' staffer
must not be able to /publish; a 'owner' staffer must; a non-staff user is
silently ignored rather than told about roles at all (this bot also serves
public visitor traffic on the same commands namespace).
"""
import asyncio
from types import SimpleNamespace

from db import db
from bot import admin


class FakeMessage:
    def __init__(self, user_id, text=""):
        self.from_user = SimpleNamespace(id=user_id)
        self.text = text
        self.sent = []

    async def answer(self, text, reply_markup=None):
        self.sent.append(text)


async def main():
    async with db.get_conn() as conn:
        await conn.execute(
            "INSERT OR IGNORE INTO staff_users (telegram_user_id, full_name, role) VALUES (?, ?, ?)",
            (2001, "Content Staffer", "content"),
        )
        await conn.execute(
            "INSERT OR IGNORE INTO staff_users (telegram_user_id, full_name, role) VALUES (?, ?, ?)",
            (2002, "Owner Staffer", "owner"),
        )
        await conn.commit()

    await db.create_exhibit(
        exhibit_id="DRY05", title_am="Հինգերորդ ցուցանմուշ", fact_sheet_am="Փաստեր։", created_by=2002,
    )
    await db.set_exhibit_status("DRY05", "review")

    # content role: /publish must be refused, status unchanged
    msg = FakeMessage(user_id=2001, text="/publish DRY05")
    await admin.publish_exhibit(msg, SimpleNamespace(args="DRY05"))
    print("content role /publish response:", msg.sent)
    exhibit = await db.get_exhibit("DRY05")
    print("status after content attempt (expect still 'review'):", exhibit["status"])
    assert exhibit["status"] == "review", "content role must not be able to publish"
    assert msg.sent and "owner" in msg.sent[0].lower()

    # content role: still allowed on the add/edit-exhibit flow
    allowed = await admin._require_role(FakeMessage(user_id=2001), admin.CONTENT_ROLES)
    assert allowed, "content role should be allowed to add/edit exhibits"

    # owner role: /publish succeeds
    msg = FakeMessage(user_id=2002, text="/publish DRY05")
    await admin.publish_exhibit(msg, SimpleNamespace(args="DRY05"))
    print("owner role /publish response:", msg.sent)
    exhibit = await db.get_exhibit("DRY05")
    print("status after owner publish (expect 'live'):", exhibit["status"])
    assert exhibit["status"] == "live"

    # non-staff: silently ignored, no message at all
    msg = FakeMessage(user_id=3001, text="/publish DRY05")
    await admin.publish_exhibit(msg, SimpleNamespace(args="DRY05"))
    assert msg.sent == [], "non-staff users should be silently ignored, not told about roles"

    print("\nROLE-CHECK DRY RUN PASSED")


if __name__ == "__main__":
    asyncio.run(main())
