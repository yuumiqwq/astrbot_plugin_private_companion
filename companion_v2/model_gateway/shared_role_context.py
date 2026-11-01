"""Select role-text tasks that use the host conversation's model and context."""


MEDIA_TASKS = frozenset({
    "screen.interpretation", "conversation.image_narration", "conversation.video_narration",
    "conversation.context_image_caption", "conversation.image_review",
})
FOREGROUND_ROLE_TASKS = frozenset({
    "conversation.poke_reply", "role.expression.review", "role.expression.contact_review",
    "content.authoring.draft",
})


def shares_role_context(profile, request=None):
    if profile.kind in MEDIA_TASKS or set(getattr(profile, "capabilities", ("text",))) & {"image", "audio", "video", "embedding"}:
        return False
    if request is not None and (request.images or request.audios or request.videos):
        return False
    lane = getattr(profile, "lane", "foreground")
    return getattr(lane, "value", lane) == "background" or profile.kind in FOREGROUND_ROLE_TASKS


def has_host_history(request):
    return any(item.owner == "astrbot.conversation" and item.purpose == "recent_private_dialogue"
               for item in request.context.materials)
