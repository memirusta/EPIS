import os
import time
import logging
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", "..", "Layer-3", "keys.env"))

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - LAYER 3 - %(message)s')

MAX_RETRIES = 3
RETRY_DELAY = 2
MAX_TOKENS  = 4096

LAYER3_SYSTEM_PROMPT = (
    "You are a pure analysis and execution engine. "
    "Return only the requested output. "
    "No preamble, no meta-commentary, no personality. "
    "If the task is in Turkish, respond in Turkish."
)


class Layer3APIClient:

    def __init__(self):
        self.claude_key = os.getenv("CLAUDE_API_KEY")
        self.gemini_key = os.getenv("GEMINI_API_KEY")
        # Nightly / yerel: Ollama OpenAI-uyumlu (RunPod veya localhost)
        self.ollama_base = (
            os.getenv("NIGHTLY_OLLAMA_BASE_URL")
            or os.getenv("QWEN_BASE_URL")
            or "http://localhost:11434/v1"
        ).rstrip("/")
        self.ollama_key = os.getenv("QWEN_API_KEY", "not-needed")

        self._anthropic_client = None
        self._gemini_client    = None
        self._ollama_client    = None

        self._log_key_status()

    def _log_key_status(self):
        missing = []
        if not self._claude_key_usable():
            missing.append("CLAUDE_API_KEY (yok veya placeholder)")
        if not self.gemini_key:
            missing.append("GEMINI_API_KEY")

        if missing:
            logger.warning(f"Eksik/gecersiz API anahtarlari: {', '.join(missing)}")
        else:
            logger.info("Tum API anahtarlari yuklendi.")
        logger.info(f"Ollama/OpenAI endpoint: {self.ollama_base}")

    def _claude_key_usable(self) -> bool:
        k = (self.claude_key or "").strip()
        if not k:
            return False
        low = k.lower()
        if any(m in low for m in ("buraya_", "your_", "placeholder", "changeme", "xxx")):
            return False
        return True

    def _gemini_fallback_model(self) -> str:
        return os.getenv("LAYER3_GEMINI_FALLBACK", "gemini-3.5-flash")

    @property
    def ollama_client(self):
        if self._ollama_client is None:
            try:
                from openai import OpenAI
                self._ollama_client = OpenAI(
                    base_url=self.ollama_base,
                    api_key=self.ollama_key,
                    timeout=300.0,
                    max_retries=1,
                )
                logger.info("Ollama OpenAI client olusturuldu.")
            except ImportError:
                raise RuntimeError("'openai' yuklu degil. Cozum: pip install openai")
        return self._ollama_client

    @property
    def anthropic_client(self):
        if self._anthropic_client is None:
            if not self._claude_key_usable():
                raise RuntimeError("CLAUDE_API_KEY tanimli/gecerli degil")
            try:
                import anthropic
                self._anthropic_client = anthropic.Anthropic(api_key=self.claude_key)
                logger.info("Anthropic client olusturuldu.")
            except ImportError:
                raise RuntimeError("'anthropic' yuklu degil. Cozum: pip install anthropic")
        return self._anthropic_client

    @property
    def gemini_client(self):
        if self._gemini_client is None:
            try:
                from google import genai
                self._gemini_client = genai.Client(api_key=self.gemini_key)
                logger.info("Gemini client olusturuldu.")
            except ImportError:
                raise RuntimeError("'google-genai' yuklu degil. Cozum: pip install google-genai")
        return self._gemini_client

    def execute_task(self, target_model: str, payload: str) -> str:
        if not payload or not payload.strip():
            logger.error("Bos payload alindi.")
            return "HATA: Bos payload -- islem iptal."

        model_lower = target_model.lower()

        if "claude" in model_lower:
            if not self._claude_key_usable():
                fb = self._gemini_fallback_model()
                logger.warning(
                    "Claude anahtari yok/placeholder — %s ile devam (%s)",
                    fb,
                    target_model,
                )
                return self._call_gemini(fb, payload)
            return self._call_claude(target_model, payload)
        elif "gemini" in model_lower:
            return self._call_gemini(target_model, payload)
        else:
            # qwen2.5:14b, qwen3.5:9b, llama, ... → Ollama / vLLM
            return self._call_ollama(target_model, payload)

    def _call_ollama(self, model_name: str, payload: str) -> str:
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                logger.info(
                    f"[Ollama] {model_name} @ {self.ollama_base} --> istek "
                    f"(Deneme {attempt}/{MAX_RETRIES})"
                )
                t0 = time.time()
                response = self.ollama_client.chat.completions.create(
                    model=model_name,
                    messages=[
                        {"role": "system", "content": LAYER3_SYSTEM_PROMPT},
                        {"role": "user", "content": payload},
                    ],
                    max_tokens=MAX_TOKENS,
                    temperature=0.3,
                )
                elapsed = time.time() - t0
                result = (response.choices[0].message.content or "").strip()
                usage = getattr(response, "usage", None)
                if usage:
                    logger.info(
                        f"[Ollama] Yanit alindi -- {elapsed:.2f}s | "
                        f"{usage.prompt_tokens} giris / {usage.completion_tokens} cikis"
                    )
                else:
                    logger.info(f"[Ollama] Yanit alindi -- {elapsed:.2f}s | {len(result)} kar.")
                if not result:
                    return "HATA: Ollama bos yanit dondu."
                return result
            except Exception as e:
                delay = RETRY_DELAY ** attempt
                logger.warning(f"[Ollama] {type(e).__name__}: {e}. {delay}s bekleniyor...")
                if attempt == MAX_RETRIES:
                    return f"HATA: Ollama erisilemez -- {e}"
                time.sleep(delay)

        return "HATA: Ollama tum denemeler sonunda yanit vermedi."

    def _call_claude(self, model_name: str, payload: str) -> str:
        if not self._claude_key_usable():
            fb = self._gemini_fallback_model()
            logger.warning("Claude anahtari kullanilamaz — Gemini fallback: %s", fb)
            return self._call_gemini(fb, payload)

        import anthropic

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                logger.info(f"[Anthropic] {model_name} --> istek (Deneme {attempt}/{MAX_RETRIES})")
                t0 = time.time()

                message = self.anthropic_client.messages.create(
                    model=model_name,
                    max_tokens=MAX_TOKENS,
                    system=LAYER3_SYSTEM_PROMPT,
                    messages=[{"role": "user", "content": payload}],
                )

                elapsed = time.time() - t0
                result  = message.content[0].text
                logger.info(
                    f"[Anthropic] Yanit alindi -- {elapsed:.2f}s | "
                    f"{message.usage.input_tokens} giris / {message.usage.output_tokens} cikis"
                )
                return result

            except anthropic.RateLimitError:
                delay = RETRY_DELAY ** attempt
                logger.warning(f"[Anthropic] Rate limit. {delay}s bekleniyor...")
                time.sleep(delay)

            except anthropic.AuthenticationError:
                logger.error("[Anthropic] API anahtari gecersiz.")
                return "HATA: Anthropic kimlik dogrulamasi basarisiz."

            except anthropic.APIConnectionError as e:
                delay = RETRY_DELAY ** attempt
                logger.warning(f"[Anthropic] Baglanti hatasi: {e}. {delay}s bekleniyor...")
                if attempt == MAX_RETRIES:
                    return f"HATA: Claude API'ye baglanilmadi -- {e}"
                time.sleep(delay)

            except anthropic.APITimeoutError:
                delay = RETRY_DELAY ** attempt
                logger.warning(f"[Anthropic] Zaman asimi. {delay}s bekleniyor...")
                if attempt == MAX_RETRIES:
                    return "HATA: Claude API zaman asiymina ugradi."
                time.sleep(delay)

            except anthropic.APIStatusError as e:
                logger.error(f"[Anthropic] API durum hatasi {e.status_code}: {e.message}")
                return f"HATA: Claude API -- {e.status_code} {e.message}"

            except Exception as e:
                logger.error(f"[Anthropic] Beklenmeyen hata: {type(e).__name__}: {e}")
                return f"HATA: Beklenmeyen Claude hatasi -- {e}"

        return "HATA: Claude API tum denemeler sonunda yanit vermedi."

    def _call_gemini(self, model_name: str, payload: str) -> str:
        if not self.gemini_key:
            return "HATA: GEMINI_API_KEY tanimli degil."

        from google.genai import types

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                logger.info(f"[Google AI] {model_name} --> istek (Deneme {attempt}/{MAX_RETRIES})")
                t0 = time.time()

                response = self.gemini_client.models.generate_content(
                    model=model_name,
                    contents=payload,
                    config=types.GenerateContentConfig(
                        system_instruction=LAYER3_SYSTEM_PROMPT,
                    ),
                )

                elapsed = time.time() - t0
                result  = response.text
                logger.info(f"[Google AI] Yanit alindi -- {elapsed:.2f}s")
                return result

            except Exception as e:
                error_str  = str(e).lower()

                if any(k in error_str for k in ("quota", "rate", "resource exhausted", "429")):
                    delay = RETRY_DELAY ** attempt
                    logger.warning(f"[Google AI] Rate limit. {delay}s bekleniyor...")
                    if attempt == MAX_RETRIES:
                        return "HATA: Gemini API kota sinirina ulasildi."
                    time.sleep(delay)

                elif any(k in error_str for k in ("api key", "invalid", "unauthenticated", "401", "403")):
                    logger.error(f"[Google AI] Kimlik hatasi: {e}")
                    return "HATA: Gemini kimlik dogrulamasi basarisiz."

                elif any(k in error_str for k in ("not found", "404")):
                    logger.error(f"[Google AI] Model bulunamadi: {model_name}")
                    return f"HATA: Gemini model bulunamadi -- '{model_name}'"

                else:
                    delay = RETRY_DELAY ** attempt
                    logger.warning(f"[Google AI] {type(e).__name__}: {e}. {delay}s bekleniyor...")
                    if attempt == MAX_RETRIES:
                        return f"HATA: Gemini API erisilemez -- {e}"
                    time.sleep(delay)

        return "HATA: Gemini API tum denemeler sonunda yanit vermedi."

    def health_check(self) -> dict:
        results = {}

        if self.claude_key:
            try:
                import anthropic
                self.anthropic_client.messages.create(
                    model="claude-haiku-4-5",
                    max_tokens=8,
                    messages=[{"role": "user", "content": "ping"}],
                )
                results["claude"] = "OK"
                logger.info("[Health] Claude: OK")
            except Exception as e:
                results["claude"] = f"HATA: {e}"
                logger.error(f"[Health] Claude: {e}")
        else:
            results["claude"] = "ANAHTAR_YOK"

        if self.gemini_key:
            try:
                response = self.gemini_client.models.generate_content(
                    model="gemini-3.1-flash-lite",
                    contents="ping",
                )
                _ = response.text
                results["gemini"] = "OK"
                logger.info("[Health] Gemini: OK")
            except Exception as e:
                results["gemini"] = f"HATA: {e}"
                logger.error(f"[Health] Gemini: {e}")
        else:
            results["gemini"] = "ANAHTAR_YOK"

        return results
