"""Matrix setup wizard and opt-in through the existing platform tools switch."""

def interactive_setup() -> None:
    """Interactive credential setup (setup_fn); CLI helpers are lazy-imported."""
    from hermes_cli.config import get_env_value, remove_env_value, save_env_value
    from hermes_cli.cli_output import prompt, prompt_yes_no, print_header, print_info, print_success, print_warning
    from hermes_cli.setup_platforms import declines_reconfigure
    print_header("Matrix")
    if declines_reconfigure("Matrix", "Reconfigure Matrix?", "MATRIX_ACCESS_TOKEN", "MATRIX_PASSWORD"):
        return
    for line in ("Works with any Matrix homeserver (Synapse, Conduit, Dendrite, or matrix.org).",
                 "   1. Create a bot user on your homeserver, or use your own account",
                 "   2. Get an access token from Element, or provide user ID + password"):
        print_info(line)
    def _ask(key: str, question: str, **kw) -> str:
        value = prompt(question, **kw)
        if value:
            save_env_value(key, value.rstrip("/") if key == "MATRIX_HOMESERVER" else value)
        return value
    _ask("MATRIX_HOMESERVER", "Homeserver URL (e.g. https://matrix.example.org)")
    print_info("Auth: provide an access token (recommended), or user ID + password.")
    token = _ask("MATRIX_ACCESS_TOKEN", "Access token (leave empty for password login)", password=True)
    if token:
        _ask("MATRIX_USER_ID", "User ID (@bot:server — optional, will be auto-detected)")
        print_success("Matrix access token saved")
    else:
        _ask("MATRIX_USER_ID", "User ID (@bot:server)")
        if _ask("MATRIX_PASSWORD", "Password", password=True):
            print_success("Matrix credentials saved")
    if token or get_env_value("MATRIX_PASSWORD"):
        want_e2ee = prompt_yes_no("Enable end-to-end encryption (E2EE)?", False)
        if want_e2ee:
            save_env_value("MATRIX_ENCRYPTION", "true")
            print_success("E2EE enabled")
        try:
            from pm import sync_venv

            print_info("Preparing Matrix dependencies...")
            sync_venv(["matrix"], explicit=True)
            print_success("Matrix dependencies prepared. Restart Hermes to use them.")
        except Exception as exc:
            print_warning(f"Matrix dependencies could not be prepared: {exc}")
            print_info("Run `hermes pm install`, then restart Hermes.")
        print_info("🔒 Security: Restrict who can use your bot")
        print_info("   Matrix user IDs look like @username:server")
        allowed_users = prompt("Allowed user IDs (comma-separated, leave empty for open access)")
        if allowed_users:
            save_env_value("MATRIX_ALLOWED_USERS", allowed_users.replace(" ", ""))
            print_success("Matrix allowlist configured")
        else:
            print_info("⚠️  No allowlist set - anyone who can message the bot can use it!")
        for line in ("📬 Home Room: where Hermes delivers cron job results and notifications.",
                     "   Room IDs look like !abc123:server (shown in Element room settings)",
                     "   You can also set this later by typing /set-home in a Matrix room.",
                     "Leave blank to clear a previously saved home room (cron / notifications)."):
            print_info(line)
        home_room = prompt("Home room ID (leave empty to set later with /set-home)").strip()
        if home_room:
            save_env_value("MATRIX_HOME_ROOM", home_room)
        elif remove_env_value("MATRIX_HOME_ROOM"):
            print_info("Home room cleared.")

        from hermes_cli.config import load_config
        from hermes_cli.tools_config import _get_platform_tools, _save_platform_tools
        config = load_config()
        enabled = _get_platform_tools(config, "matrix")
        want_ui = prompt_yes_no(
            "Enable interactive UI replies (Element Web labs flag feature_interactive_ui)?", False)
        if want_ui:
            enabled.add("interactive_ui")
        else:
            enabled.discard("interactive_ui")
            # Setup may run before plugin tools are registered; do not preserve this as an unknown entry.
            selections = config.get("platform_toolsets") or {}
            if isinstance(selections.get("matrix"), list):
                selections["matrix"] = [name for name in selections["matrix"] if name != "interactive_ui"]
        _save_platform_tools(config, "matrix", enabled)
        print_info("Interactive UI selection saved. Applies to the next session or /new.")
