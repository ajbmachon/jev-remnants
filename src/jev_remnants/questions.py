"""The two judgments jvr asks. One closed property each; the consumer combines them.

Never reword an executed question in place: a new wording is a new question id
(the name carries a hash of its wording), and stored answers keep their question.
"""

from jev_navigator.judgments.questions import Check, Criterion

REFERS_REMOVED = Check(
    name="refers_to_removed",
    instructions=(
        "Does the text in `{item}.mention.text` refer to the removed thing described in "
        "`{item}.removed`?"
    ),
    yes=Criterion(
        what=(
            "The mention is about the removed thing itself: it uses one of the names in "
            "`{item}.removed.names`, or describes behavior, configuration or interfaces the "
            "removed thing had, as described in `{item}.removed.description`."
        ),
        not_for=(
            "A different thing that happens to share a word with a name, such as an unrelated "
            "function of the same name in another module."
        ),
    ),
    no=Criterion(
        what=(
            "The mention is about something else entirely, and none of the names in "
            "`{item}.removed.names` appears as a name of the removed thing in the text."
        ),
    ),
)

LEADS_RECREATION = Check(
    name="leads_agent_to_recreate",
    instructions=(
        "Would an agent reading `{item}.mention.text` be led to use, call, configure or "
        "re-create the removed thing described in `{item}.removed`?"
    ),
    yes=Criterion(
        what=(
            "The mention instructs use: it shows how to call or configure the thing, presents it "
            "as current or recommended, gives an example to copy, asserts a behavior of it, or is "
            "a test that exercises it."
        ),
    ),
    no=Criterion(
        what=(
            "The mention only records history or absence: a changelog entry, a migration or "
            "deprecation note, a decision record, or prose that says the thing is gone."
        ),
        not_for=(
            "Prose that presents the thing as usable, even inside a document that also mentions "
            "its removal."
        ),
    ),
)
