"""One-time interactive bootstrap: run the OAuth2 password grant against Kiteworks.

Prints to stderr only (so this script is safe to share a tty with stdio MCPs).
On success, saves the token bundle to `BPD_TOKEN_FILE` (0600) and verifies via
`GET /rest/users/me`.

Usage:
    uv run bpd-bootstrap
"""

from __future__ import annotations

import asyncio
import getpass
import sys
from pathlib import Path

from rich.console import Console

from bpd_mcp.auth import AuthError, AuthManager
from bpd_mcp.client import KiteworksAPIError, KiteworksClient, make_http_client
from bpd_mcp.config import Settings, get_settings
from bpd_mcp.logging_setup import configure_logging


def _err(console: Console, msg: str) -> None:
    console.print(f"[red]{msg}[/red]", style="bold")


async def _run() -> int:
    console = Console(file=sys.stderr)
    settings: Settings = get_settings()
    settings.ensure_dirs()
    configure_logging(settings.bpd_log_level, settings.log_dir)

    console.print(f"[bold]bpd-bootstrap[/bold] — base_url=[cyan]{settings.base_url}[/cyan]")
    console.print(f"client_id=[cyan]{settings.kiteworks_client_id}[/cyan]")
    console.print(f"scope=[cyan]{settings.kiteworks_oauth_scope}[/cyan]")
    console.print(f"token file: [cyan]{settings.token_file}[/cyan]")

    # Prompt for username/password if missing.
    if not settings.kiteworks_username:
        username = input("Kiteworks email: ").strip()
        settings.kiteworks_username = username
    else:
        console.print(f"username=[cyan]{settings.kiteworks_username}[/cyan]")

    if not settings.kiteworks_password:
        # getpass writes its prompt to stderr by default — good.
        password = getpass.getpass("Kiteworks password: ")
        from pydantic import SecretStr

        settings.kiteworks_password = SecretStr(password)

    http = make_http_client(settings)
    auth = AuthManager(settings, http)
    client = KiteworksClient(settings, auth, http)

    try:
        bundle = await auth.password_grant()
        console.print(
            f"[green]✓[/green] OAuth password grant succeeded. "
            f"scope=[cyan]{bundle.scope}[/cyan] expires_at=[cyan]{bundle.expires_at.isoformat()}[/cyan]"
        )
    except AuthError as e:
        _err(console, f"OAuth failed: {e}")
        await http.aclose()
        return 2

    # Verify via /rest/users/me.
    try:
        me = await client.whoami()
        email = me.get("email") or me.get("userPrincipalName") or "(unknown)"
        name = me.get("name", "")
        console.print(f"[green]✓[/green] Verified /rest/users/me: {name} <{email}>")
    except KiteworksAPIError as e:
        _err(console, f"/rest/users/me returned HTTP {e.status}: {e.body or e}")
        await http.aclose()
        return 3

    console.print(f"[green]Saved token file:[/green] {settings.token_file}")
    # Show the resulting perms so the user can confirm 0600.
    if not sys.platform.startswith("win"):
        mode = oct(Path(settings.token_file).stat().st_mode & 0o777)
        console.print(f"token file mode: [cyan]{mode}[/cyan]")

    await http.aclose()
    return 0


def main() -> None:
    sys.exit(asyncio.run(_run()))


if __name__ == "__main__":
    main()
