"""A reply to an inbox mail may only use that mail's own placeholders (fictional data)."""
from agents.tools.comms_tools import _own_placeholders

EMAIL = "From: <SENDER>\nHello, I am <PERSON_1>. Which documents should I bring to my interview?"


def test_own_placeholders_are_kept():
    subject, body, bad = _own_placeholders(EMAIL, "Re: interview", "Hello <PERSON_1>,\nBring your ID.")
    assert (body, bad) == ("Hello <PERSON_1>,\nBring your ID.", [])


def test_square_bracket_twin_is_switched_to_the_email_placeholder():
    _, body, bad = _own_placeholders(EMAIL, "Re: interview", "Hello [PERSON_1],\nBring your ID.")
    assert body.startswith("Hello <PERSON_1>,") and bad == []


def test_placeholder_from_elsewhere_is_refused():
    _, _, bad = _own_placeholders(EMAIL, "Re: interview", "Hello <PERSON_1>, see [PERSON_7] and <EMAIL_2>.")
    assert bad == ["<EMAIL_2>", "[PERSON_7]"]


def test_subject_is_checked_too():
    subject, _, bad = _own_placeholders(EMAIL, "Re: [PERSON_1] [PHONE_3]", "Hello.")
    assert subject == "Re: <PERSON_1> [PHONE_3]" and bad == ["[PHONE_3]"]
