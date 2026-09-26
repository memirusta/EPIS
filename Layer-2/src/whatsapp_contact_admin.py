#!/usr/bin/env python3
"""Local-only WhatsApp contact enrollment for EPIS.

Phone numbers are entered locally and sent only to the authenticated
127.0.0.1 Baileys bridge. They are never written to the EPIS vault,
stdout, cloud runtime or model context.
"""

from __future__ import annotations

import getpass
from pathlib import Path
import secrets
import sys


THIS_DIR = Path(
    __file__
).resolve().parent

sys.path.insert(
    0,
    str(THIS_DIR),
)


from agentic.whatsapp_outreach import (
    LocalWhatsappBridge,
    load_whatsapp_bridge_config,
)
from memory_vault import (
    LocalMemoryVault,
)


def main() -> int:
    config = (
        load_whatsapp_bridge_config()
    )

    url = str(
        config.get(
            "EPIS_WHATSAPP_BRIDGE_URL"
        )
        or ""
    ).strip()

    token = str(
        config.get(
            "EPIS_WHATSAPP_BRIDGE_TOKEN"
        )
        or ""
    ).strip()

    if not url or not token:
        print(
            "WhatsApp bridge config missing.",
            file=sys.stderr,
        )
        return 2

    bridge = LocalWhatsappBridge(
        url,
        token,
    )

    if not bridge.available():
        print(
            "WhatsApp bridge is not connected.",
            file=sys.stderr,
        )
        return 3

    name = input(
        "Canonical name (örn. Merve): "
    ).strip()

    if not name:
        print(
            "Name required.",
            file=sys.stderr,
        )
        return 4

    alias_text = input(
        "Aliases, comma separated "
        "(optional): "
    ).strip()

    aliases = [
        item.strip()
        for item
        in alias_text.split(",")
        if item.strip()
    ]

    phone = getpass.getpass(
        "WhatsApp number with country code "
        "(örn. +90...; hidden): "
    ).strip()

    if not phone:
        print(
            "Phone required.",
            file=sys.stderr,
        )
        return 5

    contact_ref = (
        "contact:"
        + secrets.token_hex(12)
    )

    enrolled = (
        bridge.enroll_contact(
            contact_ref,
            phone,
        )
    )

    # Forget the local Python reference as early as possible.
    phone = ""

    if enrolled.get("ok") is not True:
        print(
            "Enrollment failed: "
            + str(
                enrolled.get(
                    "error"
                )
                or "unknown"
            ),
            file=sys.stderr,
        )
        return 6

    vault = LocalMemoryVault()

    try:
        bound = (
            vault.configure_person_whatsapp(
                name,
                contact_ref=
                    contact_ref,
                aliases=
                    aliases,
                allowlisted=True,
            )
        )
    except Exception as exc:
        # Do not leave an orphan transport mapping when the
        # authoritative local vault update fails.
        bridge.remove_contact(
            contact_ref
        )

        print(
            "Vault binding failed; "
            "bridge mapping rolled back: "
            + type(exc).__name__,
            file=sys.stderr,
        )

        return 7

    previous = str(
        bound.get(
            "previous_contact_ref"
        )
        or ""
    ).strip()

    if (
        previous
        and previous
        != contact_ref
    ):
        bridge.remove_contact(
            previous
        )

    print("")
    print(
        "Contact enrolled successfully."
    )
    print(
        "Name:",
        name,
    )
    print(
        "Opaque contact_ref:",
        contact_ref,
    )
    print(
        "Allowlisted: True"
    )
    print(
        "Raw phone/JID was not stored "
        "in the EPIS vault."
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(
        main()
    )
