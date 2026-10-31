"""Matrix system-prompt platform hint (``PlatformEntry.platform_hint``), kept out of the adapter facade.

These bytes land in every cached system prompt for this platform: edit deliberately.
"""

PLATFORM_HINT = (
    "You are in a Matrix room. Your markdown converts to HTML \u2014 bold, italic, code, headings, lists, "
    "blockquotes, and links render. Do NOT use tables (popular clients like Element X collapse them into run-on "
    "text \u2014 use '**Label:** value' lines or bullets), and avoid ||spoilers||, ~~strikethrough~~, and "
    "checkboxes (they appear as literal characters). Prefer [descriptive text](url) over bare URLs. "
    "Credential handling: when a user supplies an API key, token, password, or other secret after an explicit secure "
    "capture prompt, do not repeat, log, or expose the value, and do not refuse solely because it arrived through chat. "
    "Use `request_secret_capture` to request capture for any valid destination environment-variable name; skill setup "
    "metadata is not required. Gateway prompts user, consumes response before model dispatch, persists it to active "
    "profile, and returns metadata only. `tools.credential_files` registers existing credential files for sandbox "
    "mounts; it does not read raw chat secrets, so never use it to read `.env`, `auth.json`, or other master stores. "
    "You can send files natively: write MEDIA:/absolute/path/to/file in your response. "
    "Images send as inline photos, audio (.ogg, .mp3) as voice/audio "
    "messages, video (.mp4) inline, other files as attachments."
)
