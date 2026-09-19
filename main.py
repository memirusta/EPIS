#!/usr/bin/env python3
"""
EPIS -- Ana Giris Noktasi
=========================
Layer-1         : Qwen3.5-9B (Ollama) — fine-tune sonrasi qwen-epis
Layer-2         : router.py + api_clients.py  (tam entegre)
Layer-3         : Gemini & Claude API havuzu

Kullanim: python main.py
"""

import argparse
import os
import sys
import json
import logging
from datetime import datetime

# ---------------------------------------------------------------
# Path Kurulumu
# ---------------------------------------------------------------
EPIS_ROOT  = os.path.dirname(os.path.abspath(__file__))
LAYER2_SRC = os.path.join(EPIS_ROOT, "Layer-2", "src")
sys.path.insert(0, LAYER2_SRC)


# ---------------------------------------------------------------
# Loglama
# ---------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(
            os.path.join(EPIS_ROOT, "epis.log"), encoding="utf-8"
        ),
    ],
)
logger = logging.getLogger("EPIS.MAIN")


# ---------------------------------------------------------------
# Kairos Pending Teslimi
# ---------------------------------------------------------------
def _deliver_pending(engine):
    """
    pending.json'daki öğeleri EPIS'in kendi sesiyle formüle edip iletir.
    Ham 'context' Layer-1'e verilir, EPIS kendi üslubuyla yazar.
    """
    from proactive_delivery import format_kairos_epis_message
    pending_path = os.path.join(EPIS_ROOT, "Layer-1", "memory", "pending.json")
    if not os.path.exists(pending_path):
        return

    try:
        with open(pending_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return

    items     = data.get("items", [])
    delivered = False

    for item in items:
        if item.get("status") != "pending":
            continue

        context  = item.get("context", item.get("message", ""))
        priority = item.get("priority", "medium")
        t_type   = item.get("trigger_type", "")

        epis_msg = format_kairos_epis_message(engine, t_type, context)

        prefix = "⚡ " if priority in ("high", "urgent") else ""
        print(f"\n{prefix}EPIS: {epis_msg}")

        item["status"]       = "delivered"
        item["delivered_at"] = datetime.now().isoformat()
        delivered = True

    if delivered:
        data["last_updated"] = datetime.now().isoformat()
        with open(pending_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print()


# ---------------------------------------------------------------
# Ana Dongu
# ---------------------------------------------------------------
def legacy_main():
    from router import EpisRouter
    from memory_manager import MemoryManager
    from epis_core import build_system_prompt, Layer1Engine
    from context_builder import ContextBuilder
    logger.info("=" * 50)
    logger.info("EPIS baslatiliyor...")
    logger.info("=" * 50)

    try:
        router = EpisRouter()
        memory = MemoryManager()
    except Exception as e:
        logger.error(f"Router/Memory baslatilmadi: {e}")
        sys.exit(1)

    logger.info("API saglik kontrolu yapiliyor...")
    health    = router.api_client.health_check()
    gemini_ok = health.get("gemini") == "OK"
    claude_ok = health.get("claude") == "OK"

    logger.info("Identity dosyalari yukleniyor...")
    system_prompt = build_system_prompt()
    logger.info(f"System prompt hazir -- {len(system_prompt)} karakter")

    # Layer-2 baglam enjeksiyonu (RAG + zaman) -- her turda taze
    context_builder = ContextBuilder(memory)

    # Layer-1 engine'i config'e gore baslat (gemini | qwen)
    try:
        engine = Layer1Engine(
            system_prompt,
            gemini_key=router.api_client.gemini_key,
            context_provider=context_builder.build,
        )
    except Exception as e:
        logger.error(f"Layer-1 engine baslatilmadi: {e}")
        sys.exit(1)

    # Gemini backend Layer-1 icin Gemini'ye baglidir; yoksa baslatma.
    if engine.backend_name == "gemini" and not gemini_ok:
        logger.error("Gemini erisilemyor -- Layer-1 (gemini) icin gerekli. EPIS baslatilmiyor.")
        sys.exit(1)

    if engine.backend_name == "qwen":
        try:
            engine.backend.generate("Saglik kontrolu. Tek kelime: tamam", [])
            qwen_ok = True
        except Exception:
            qwen_ok = False
        if not qwen_ok:
            logger.error(
                "Qwen erisilemyor -- Ollama acik mi? scripts/start_qwen.ps1 veya install_qwen.ps1"
            )
            sys.exit(1)

    print("\n" + "=" * 55)
    print("  EPIS -- Aktif")
    print(f"  Layer-1 : {engine.model} ({engine.backend_name})")
    print(f"  Claude  : {'OK' if claude_ok else 'ANAHTAR EKSIK'}")
    print(f"  Gemini  : {'OK' if gemini_ok else 'HATA / YOK'}")
    print("  Cikmak icin: quit")
    print("=" * 55 + "\n")

    memory.update_current_state({"session_active": True})
    session_log = []

    _deliver_pending(engine)

    while True:
        try:
            user_input = input("Sen: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nEPIS kapatiliyor.")
            break

        if not user_input:
            continue

        if user_input.lower() in ("quit", "exit", "cik", "cikis"):
            print("EPIS: Görüşürüz.")
            break

        l1_response = engine.send(user_input)

        if l1_response.get("type") == "tool_call":
            task_type  = l1_response.get("task_type", "fast_tasks")
            payload    = l1_response.get("payload", user_input)
            bridge_msg = l1_response.get("bridge_message", "Bakiyorum...")

            print(f"EPIS: {bridge_msg}")
            logger.info(f"Tool Call tetiklendi: {task_type}")

            tool_call_data = {"task_type": task_type, "payload": payload}
            router_result  = router.intercept_tool_call(
                tool_call_data=tool_call_data,
                current_context={},
            )

            result_text = router_result.get("result", "")
            model_used  = router_result.get("model_used", "")
            logger.info(f"Layer-3 yaniti alindi: {model_used}")

            followup = (
                f"[GOREV SONUCU -- {task_type} / {model_used}]\n"
                f"{result_text}\n\n"
                "Yukaridaki sonucu kendi sesinde, EPIS olarak kullanıcıya ilet."
            )
            l1_final     = engine.send(followup)
            epis_message = l1_final.get("message", result_text)

            # Tool call'lar aninda kaydedilir
            memory.log_interaction(
                event_type="tool_call",
                raw_text=user_input,
                observation="",
                tags=[task_type, model_used],
            )
        else:
            epis_message = l1_response.get("message", "")

        session_log.append(f"kullanıcı: {user_input}")
        session_log.append(f"EPIS: {epis_message}")

        memory.update_current_state({
            "last_interaction": user_input[:200],
            "last_message_at": datetime.now().isoformat(),
        })
        print(f"EPIS: {epis_message}\n")

    # Oturum kapaninca ham konusmayi tek kayit olarak yaz
    # epis_observation nightly tarafindan doldurulacak
    if session_log:
        memory.log_interaction(
            event_type="session",
            raw_text="\n".join(session_log),
            observation="",
        )

    memory.update_current_state({"session_active": False})


def main(argv=None):
    """EPIS 0.1 defaults to the text agent; old CLI remains opt-in."""
    parser = argparse.ArgumentParser(description="EPIS")
    parser.add_argument("--env-file", help="Load an existing local environment file without copying secrets")
    parser.add_argument("--debug", action="store_true", help="Show diagnostic logs in the terminal")
    parser.add_argument("--device-transport", choices=["stdio", "paired", "inprocess"], help="Override device transport for this run")
    parser.add_argument("--pairing-dir", help="Existing local mTLS pairing profile")
    parser.add_argument(
        "--legacy",
        action="store_true",
        help="Run the pre-0.1 Layer-1/Layer-3 terminal loop.",
    )
    args = parser.parse_args(argv)
    from dotenv import load_dotenv
    env_path = args.env_file or os.path.join(EPIS_ROOT, "Layer-3", "keys.env")
    if args.env_file and not os.path.isfile(env_path):
        parser.error("Environment file not found")
    load_dotenv(env_path, override=False)
    if args.device_transport:
        os.environ["EPIS_DEVICE_TRANSPORT"] = args.device_transport
    if args.pairing_dir:
        os.environ["EPIS_PAIRING_DIR"] = os.path.abspath(args.pairing_dir)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    if not args.legacy:
        console = logging.StreamHandler()
        console.setLevel(logging.INFO if args.debug else logging.ERROR)
        logfile = logging.FileHandler(os.path.join(EPIS_ROOT, "epis.log"), encoding="utf-8")
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s",
                            handlers=[console, logfile], force=True)
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
    if args.legacy:
        legacy_main()
        return 0
    from agentic.cli import main as agentic_main
    try:
        return agentic_main()
    except Exception as exc:
        logger.error("Startup failed: %s", type(exc).__name__)
        print("EPIS başlatılamadı. Anahtar ve bağımlılık ayarlarını kontrol et.")
        return 1


# ---------------------------------------------------------------
if __name__ == "__main__":
    raise SystemExit(main())
