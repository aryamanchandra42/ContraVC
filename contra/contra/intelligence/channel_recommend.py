"""
Channel recommendation — suggests the best outreach channel for an LP.

Heuristic rules (no LLM required for v1):
  1. warm_intro  — warm path count > 0
  2. email       — verified / personal domain or work email present
  3. linkedin    — LinkedIn URL known, no email
  4. twitter     — X/Twitter URL only (low confidence cold outreach)
  5. none        — no channels found

Priority table (matches plan):
  LP type                                | Primary       | Secondary
  Institutional CIO/FO                  | Work email    | LinkedIn
  Individual / angel, personal domain   | Email         | LinkedIn
  LinkedIn URL only                     | LinkedIn      | —
  Twitter/X only                        | Twitter       | (warning)
  Warm intro path exists                | warm_intro    | (then apply above)

The function attaches a plain-language rationale string.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from contra.intelligence.contact_resolver import ContactProfile

# Domains that unambiguously belong to a personal email provider
_PERSONAL_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "icloud.com",
    "me.com", "protonmail.com", "pm.me", "fastmail.com", "hey.com",
    "aol.com", "live.com", "msn.com", "zoho.com",
}


def _is_personal_domain(email: str) -> bool:
    """True if the email is at a well-known personal provider."""
    domain = email.split("@")[-1].lower() if "@" in email else ""
    return domain in _PERSONAL_DOMAINS


def _is_own_domain(email: str, investor_name: str) -> bool:
    """Heuristic: email domain contains a word from the investor's name."""
    if not email or "@" not in email:
        return False
    domain = email.split("@")[1].lower()
    name_words = {w.lower() for w in investor_name.split() if len(w) > 2}
    return any(w in domain for w in name_words)


def recommend_channel(
    profile: "ContactProfile",
    warm_path_count: int = 0,
    investor_type: Optional[str] = None,
) -> "ContactProfile":
    """
    Attach recommended_channel and recommendation_rationale to a ContactProfile.
    Mutates the profile in place and returns it.

    warm_path_count — number of warm intro paths found in the DB (from v_warm_paths)
    investor_type   — optional allocator_type string (e.g. "individual", "family_office")
    """
    email = profile.best_email()
    linkedin = profile.best_linkedin()
    twitter = profile.best_twitter()

    is_individual = investor_type in (
        "individual", "angel", "founder_lp", "personal", None
    )

    # Rule 1 — warm intro always wins
    if warm_path_count > 0:
        profile.recommended_channel = "warm_intro"
        profile.recommendation_rationale = (
            f"Warm introduction path exists ({warm_path_count} bridge"
            f"{'s' if warm_path_count > 1 else ''} found). "
            "Prioritise a warm intro before any cold outreach."
        )
        profile.confidence = 0.95
        return profile

    # Rule 2 — email
    if email:
        is_personal = _is_personal_domain(email)
        is_custom = _is_own_domain(email, profile.investor_name)

        if is_custom:
            rationale = (
                f"Personal domain email ({email}) found in research — "
                "strongly suggests individual LP. Email first."
            )
            confidence = 0.90
        elif is_individual and is_personal:
            rationale = (
                f"Personal email ({email}) with individual LP profile. "
                "Email is preferred for personal outreach."
            )
            confidence = 0.80
        elif not is_personal:
            rationale = (
                f"Work/institutional email ({email}) available. "
                "Direct email outreach is most professional."
            )
            confidence = 0.88
        else:
            rationale = f"Email ({email}) available — use as primary channel."
            confidence = 0.75

        profile.recommended_channel = "email"
        profile.recommendation_rationale = rationale
        profile.confidence = confidence
        return profile

    # Rule 3 — LinkedIn
    if linkedin:
        profile.recommended_channel = "linkedin"
        profile.recommendation_rationale = (
            f"No email found. LinkedIn profile available ({linkedin}). "
            "Send a connection request or InMail."
        )
        profile.confidence = 0.70
        return profile

    # Rule 4 — Twitter / X (low confidence, cold)
    if twitter:
        profile.recommended_channel = "twitter"
        profile.recommendation_rationale = (
            f"No email or LinkedIn found. X/Twitter profile ({twitter}) available. "
            "Only recommended if the LP is publicly active as an investor on X."
        )
        profile.confidence = 0.40
        return profile

    # Rule 5 — no channels
    profile.recommended_channel = ""
    profile.recommendation_rationale = (
        "No contact channels found yet. "
        "Run a Phantombuster Sales Navigator search or add contacts manually."
    )
    profile.confidence = 0.0
    return profile


def enrich_profile_with_warm_paths(con, profile: "ContactProfile") -> "ContactProfile":
    """
    Look up warm path count from the DB and run channel recommendation.
    Returns the mutated profile.
    """
    warm_count = 0
    investor_type = None

    if profile.allocator_id:
        try:
            row = con.execute(
                "SELECT warm_path_count, allocator_type FROM v_lp_profile WHERE allocator_id = ? LIMIT 1",
                [profile.allocator_id],
            ).fetchone()
            if row:
                warm_count = int(row[0] or 0)
                investor_type = row[1]
        except Exception:
            pass

        if investor_type is None:
            try:
                row = con.execute(
                    "SELECT allocator_type FROM allocators WHERE CAST(allocator_id AS VARCHAR) = ? LIMIT 1",
                    [profile.allocator_id],
                ).fetchone()
                if row:
                    investor_type = row[0]
            except Exception:
                pass

    return recommend_channel(profile, warm_path_count=warm_count, investor_type=investor_type)
