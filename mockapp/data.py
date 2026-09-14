"""In-memory 'core banking' data for the mock back-office app.

Deliberately tiny. Certain member IDs are wired to exercise specific runtime
outcomes so that replay error handling can be demonstrated deterministically:

    12345  -> normal member, has a savings balance      (happy path)
    22222  -> normal member, different balance
    00000  -> no such member                            (business outcome: not found)
    99999  -> exists but caller lacks permission        (business outcome: permission denied)

None of this is real PII. Names are obviously fake.
"""

MEMBERS = {
    "12345": {
        "member_id": "12345",
        "name": "Jordan Testerson",
        "status": "Active",
        "savings_balance": "$4,210.55",
        "checking_balance": "$812.10",
        "permitted": True,
    },
    "22222": {
        "member_id": "22222",
        "name": "Alex Sampleman",
        "status": "Active",
        "savings_balance": "$19,004.00",
        "checking_balance": "$1,250.75",
        "permitted": True,
    },
    "99999": {
        "member_id": "99999",
        "name": "Restricted Account",
        "status": "Restricted",
        "savings_balance": "$0.00",
        "checking_balance": "$0.00",
        "permitted": False,  # caller is not permitted to view this record
    },
    "33333": {
        "member_id": "33333",
        "name": "Sam Example",
        "status": "Active",
        "savings_balance": "$7,500.00",
        "checking_balance": "$300.00",
        "permitted": True,
    },
}


def get_member(member_id: str):
    """Return (member_dict_or_None, reason). reason in {'ok','not_found','forbidden'}."""
    member_id = (member_id or "").strip()
    m = MEMBERS.get(member_id)
    if m is None:
        return None, "not_found"
    if not m["permitted"]:
        return None, "forbidden"
    return m, "ok"
