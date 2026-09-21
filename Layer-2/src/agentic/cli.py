"""Text-first EPIS 0.1 entry point; voice and UI intentionally remain later milestones."""

from __future__ import annotations

import os
from pathlib import Path

from context_builder import ContextBuilder
from epis_core import build_system_prompt
from memory_manager import MemoryManager
from privacy import PrivacyFilter

from .core import AgentCore
from .devices import DeviceRegistry, LocalDeviceAgent, UnavailableDeviceAgent
from .hot_memory import HotConversationStore
from .luna import OpenAILunaClient, OpenAISolClient
from .permissions import PermissionEngine
from .tools import build_local_registry
from .transport import StdioDeviceAgent
from .tasks import TaskStore
from .usage import OpenAIOrganizationUsageRepository, SQLiteUsageRepository


def create_core() -> AgentCore:
    cloud_mode = os.getenv("EPIS_DEPLOYMENT", "local").lower() == "cloud"
    mode = os.getenv(
        "EPIS_LUNA_CONTEXT_MODE",
        "minimal",
    ).lower()

    if mode not in {
        "minimal",
        "local",
    }:
        raise ValueError(
            "Unknown context mode"
        )

    memory = MemoryManager()

    # ------------------------------------------------------------------
    # HOT CONVERSATION MEMORY
    #
    # Daily/weekly/long-term memory sisteminden bağımsızdır.
    # Son birkaç saatlik doğal user <-> EPIS konuşmasını restart'lar
    # arasında korur.
    # ------------------------------------------------------------------
    hot_memory = HotConversationStore(
        os.path.join(
            memory.memory_dir,
            "agent_hot_conversation.jsonl",
        )
    )

    registry = build_local_registry()

    state_path = os.path.join(
        memory.memory_dir,
        "devices.json",
    )

    devices = DeviceRegistry(
        state_path
    )

    transport_mode = (
        "cloud"
        if cloud_mode
        else os.getenv(
            "EPIS_DEVICE_TRANSPORT",
            "stdio",
        ).lower()
    )

    if transport_mode not in {
        "stdio",
        "inprocess",
        "paired",
        "cloud",
    }:
        raise ValueError(
            "EPIS_DEVICE_TRANSPORT must be "
            "stdio, inprocess, paired or cloud"
        )

    tasks = TaskStore(
        os.path.join(
            memory.memory_dir,
            "agent_tasks.db",
        )
    )
    local_usage = SQLiteUsageRepository(
        os.getenv("EPIS_USAGE_DB")
        or os.path.join(memory.memory_dir, "model_usage.db")
    )
    usage = OpenAIOrganizationUsageRepository(local_usage)

    local_agent = None

    try:
        # In-process remains explicit compatibility,
        # never a silent fallback.
        if transport_mode == "cloud":
            local_agent = UnavailableDeviceAgent(
                devices
            )

        elif transport_mode == "paired":
            from .paired_transport import (
                PairedLocalAgent,
            )

            default_profile = (
                Path(__file__).resolve().parents[3]
                / ".epis-runtime"
                / "pairing"
            )

            local_agent = PairedLocalAgent(
                devices,
                os.getenv(
                    "EPIS_PAIRING_DIR"
                )
                or default_profile,
            )

        elif transport_mode == "stdio":
            local_agent = StdioDeviceAgent(
                devices
            )

        else:
            local_agent = LocalDeviceAgent(
                devices,
                registry.dispatch_capability,
                registry.capabilities(),
            )

        return AgentCore(
            luna=OpenAILunaClient(
                usage_repository=usage,
            ),
            system_prompt=build_system_prompt(
                protocol="agentic",
                include_private=(
                    mode == "local"
                ),
            ),
            context_builder=ContextBuilder(
                memory
            ),
            memory=memory,
            registry=registry,
            devices=devices,
            local_agent=local_agent,
            permissions=PermissionEngine(),
            sol=OpenAISolClient(
                PrivacyFilter(),
                usage_repository=usage,
            ),
            tasks=tasks,
            hot_memory=hot_memory,
            usage_repository=usage,
        )

    except Exception:
        if local_agent:
            local_agent.close()

        tasks.close()
        usage.close()

        raise


def main() -> int:
    if not (
        os.getenv("LUNA_API_KEY")
        or os.getenv("OPENAI_API_KEY")
    ):
        print(
            "EPIS: API anahtarı bulunamadı. "
            "--env-file ile keys.env dosyanı seç."
        )
        return 1

    from .shell_tools import (
        set_shell_enabled,
    )

    set_shell_enabled(False)

    core = create_core()

    try:
        return interact(
            core
        )

    finally:
        set_shell_enabled(False)
        core.close()


def interact(
    core,
) -> int:
    print(
        "EPIS 0.1 — Luna + Sol"
    )

    print(
        "Cihaz: "
        f"{core.local_agent.device.display_name}"
        " | Bağlantı: "
        f"{os.getenv('EPIS_DEVICE_TRANSPORT', 'stdio')}"
    )

    print(
        "Bu oturumda yazdıkların ve araç sonuçları "
        "model API'sine gönderilir."
    )

    mode = os.getenv(
        "EPIS_LUNA_CONTEXT_MODE",
        "minimal",
    ).lower()

    if mode == "minimal":
        print(
            "Bağlam: minimal — son 5 saat sohbet devamlılığı "
            "yüklenir; eski günlük/haftalık hafıza ve sensörler "
            "bu moda otomatik enjekte edilmez."
        )

    else:
        print(
            "Bağlam: local — yakın sohbet devamlılığı ve "
            "yerel hafıza bağlamı kullanılabilir."
        )

    restored = getattr(
        core,
        "restored_hot_messages",
        0,
    )

    if restored:
        print(
            "Sohbet devamlılığı: "
            f"{restored} yakın-geçmiş mesajı geri yüklendi."
        )
    else:
        print(
            "Sohbet devamlılığı: yakın geçmişte "
            "geri yüklenecek mesaj yok."
        )

    print(
        "Çıkış: quit | "
        "Onay: evet / hayır (120 sn) | "
        "/new /help /devices /tasks /tools /workspace"
    )

    while True:
        try:
            text = input(
                "Sen: "
            ).strip()

        except (
            EOFError,
            KeyboardInterrupt,
        ):
            print(
                "\nEPIS: Görüşürüz."
            )
            return 0

        if not text:
            continue

        lowered = text.lower()

        if lowered in {
            "quit",
            "exit",
            "çık",
            "cik",
            "çıkış",
            "cikis",
        }:
            print(
                "EPIS: Görüşürüz."
            )
            return 0

        # --------------------------------------------------------------
        # NEW CONVERSATION
        #
        # Sadece yakın sohbet context'ini sıfırlar.
        # Daily/weekly/identity/long-term memory korunur.
        # --------------------------------------------------------------
        if lowered == "/new":
            cleared = (
                core.new_conversation()
            )

            print(
                "EPIS: Yeni sohbet açıldı. "
                f"{cleared} aktif mesaj bağlamdan çıkarıldı; "
                "günlük, haftalık ve uzun vadeli hafıza korunuyor.\n"
            )

            continue

        if lowered == "/help":
            print(
                "Örnekler: "
                "Bilgisayarın durumu ne? | "
                "Spotify'ı duraklat | "
                "Sesi kapat | "
                "Parlaklığı 50 yap"
            )

            print(
                "Nebula'yı bul ve aç | "
                "Nebula penceresini küçült | "
                "İndirilenler klasörünü aç"
            )

            print(
                "EPIS çalışma klasöründe Deneme klasörü oluştur | "
                "Ses ayarlarını aç"
            )

            print(
                "/new: yakın konuşma bağlamını sıfırla; "
                "daily/weekly/uzun vadeli hafıza korunur"
            )

            print(
                "/workspace: dosya deneme alanı | "
                "/cancel: bekleyen onayı iptal et | "
                "quit: çık"
            )

            print(
                "Dosya: UTF-8 okuma, yeni dosya yazma, "
                "hash kontrollü kopyalama/taşıma. "
                "Üzerine yazma/silme aracı yok."
            )

            print(
                "/shell on | /shell off: genel PowerShell izni "
                "(her komut ayrıca onaylı, en fazla 20 sn)"
            )

            print(
                "/shell-output KIMLIK: komut çıktısını "
                "API'ye göndermeden yerelde gör (120 sn)"
            )

            continue

        if lowered in {
            "/shell on",
            "/shell off",
        }:
            from .shell_tools import (
                set_shell_enabled,
            )

            enabled = (
                lowered
                == "/shell on"
            )

            set_shell_enabled(
                enabled
            )

            print(
                (
                    "Shell 1 saatliğine açıldı. "
                    "Her komutu ayrıca onaylayacaksın. "
                    "Klasör sınırı YOK; komut dosya/ağ "
                    "erişimi ve silme yapabilir."
                )
                if enabled
                else "Shell kapatıldı."
            )

            continue

        if lowered.startswith(
            "/shell-output "
        ):
            output_id = text.split(
                maxsplit=1
            )[1]

            if (
                len(output_id) != 32
                or any(
                    c
                    not in "0123456789abcdef"
                    for c in output_id
                )
            ):
                print(
                    "Geçersiz çıktı kimliği."
                )
                continue

            result = (
                core.local_agent.execute(
                    "shell.output",
                    {
                        "output_id": (
                            output_id
                        )
                    },
                    confirmed=True,
                )
            )

            print(
                result.get(
                    "output",
                    result.get(
                        "error",
                        "Çıktı yok",
                    ),
                )
            )

            print(
                "Bu yerel görüntüleme model API'sine "
                "gönderilmedi."
            )

            continue

        if lowered == "/workspace":
            from .filesystem import (
                known_root,
            )

            print(
                "Yerel deneme alanı: "
                f"{known_root('workspace')}"
            )

            print(
                "İlk izinli klasör işleminde oluşturulur. "
                "Bu yol model API'sine gönderilmedi."
            )

            continue

        if lowered == "/cancel":
            print(
                "EPIS: "
                f"{core.reject_pending().message}"
            )

            continue

        if lowered in {
            "/devices",
            "/tasks",
            "/tools",
        }:
            if lowered == "/devices":
                for transport in (
                    core.transports.values()
                ):
                    transport.refresh()

                for device in (
                    core.devices.list_public()
                ):
                    print(
                        f"{device['display_name']} "
                        f"({device['device_id']}): "
                        f"{'online' if device['online'] else 'offline'} "
                        f"| {', '.join(device['capabilities'])}"
                    )

            elif lowered == "/tasks":
                rows = (
                    core.tasks.recent()
                )

                for row in rows:
                    print(
                        f"{row['task_id'][:8]} | "
                        f"{row['tool']} | "
                        f"{row['device']} | "
                        f"{row['state']}"
                    )

                if not rows:
                    print(
                        "Henüz cihaz işlemi yok."
                    )

            else:
                for spec in (
                    core.registry.specs()
                ):
                    print(
                        f"{spec.name} | "
                        f"{spec.capability} | "
                        f"{spec.risk_class}"
                    )

            continue

        try:
            if (
                core.pending
                and lowered
                in {
                    "evet",
                    "onay",
                    "yes",
                }
            ):
                turn = (
                    core.confirm_pending()
                )

            elif (
                core.pending
                and lowered
                in {
                    "hayır",
                    "hayir",
                    "iptal",
                    "no",
                }
            ):
                turn = (
                    core.reject_pending()
                )

            else:
                turn = core.handle(
                    text
                )

            print(
                f"EPIS: {turn.message}\n"
            )

        except Exception as exc:
            import logging

            logging.getLogger(
                "EPIS.AGENT"
            ).error(
                "Turn failed: %s",
                type(exc).__name__,
            )

            print(
                "EPIS: Bu tur tamamlanamadı. "
                "Hata türü epis.log dosyasına kaydedildi.\n"
            )


def _dispatch_capability(
    registry,
    capability: str,
    arguments: dict,
) -> dict:
    return registry.dispatch_capability(
        capability,
        arguments,
    )
