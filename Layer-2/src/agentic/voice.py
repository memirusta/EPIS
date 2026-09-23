"""Deterministic EPIS voice guidance.

This module never decides policy, authorization, truth, or tool use. It only
translates the user's conversational register into bounded style guidance for
Luna so EPIS stays warm and natural without turning into a caricature.
"""

from __future__ import annotations

from dataclasses import dataclass
import re


@dataclass(frozen=True)
class VoiceProfile:
    mode: str
    energy: str
    brevity: str
    technical: bool = False
    phatic: bool = False


_TECHNICAL_MARKERS = (
    "error", "traceback", "exception", "pytest", "test", "build", "compile",
    "git ", "github", "commit", "branch", "merge", "pull request", "pr ",
    ".py", ".ts", ".tsx", ".js", ".dart", ".kt", ".rs", ".json", ".yaml",
    "powershell", "terminal", "cmd", "api", "http", "websocket", "sql",
    "postgres", "sqlite", "docker", "flutter", "tauri", "npm ", "cargo ",
    "function", "class ", "repo", "repository", "dosya", "kod", "bug",
    "hata", "stack", "log", "sha", "regex", "fps", "driver", "wifi",
)

_SERIOUS_MARKERS = (
    "ciddi", "kritik", "acil", "tehlike", "güvenlik", "guvenlik", "şifre",
    "sifre", "parola", "ödeme", "odeme", "para gönder", "para gonder",
    "satın al", "satin al", "sil", "delete", "format", "factory reset",
    "hesap kapat", "hesabı kapat", "hesabi kapat", "geri dönüşü yok",
    "geri donusu yok", "veri kayb", "kayıp", "kayip",
)

_VULNERABLE_MARKERS = (
    "çok kötüyüm", "cok kotuyum", "çok kötü hissediyorum", "cok kotu hissediyorum",
    "ağlay", "aglay", "korkuyorum", "panik", "yalnızım", "yalnizim",
    "çok stres", "cok stres", "dayanamıyorum", "dayanamiyorum",
)

_FRUSTRATION_MARKERS = (
    "amk", "aq", "of ya", "yeter", "sinir", "delir", "çalışmıyor",
    "calismiyor", "olmuyor", "bozuldu", "patladı", "patladi", "lanet",
)

_EXCITEMENT_MARKERS = (
    "cuuu", "cüş", "cus", "olduuu", "olduuu", "kanka", "lets go", "let's go",
    "oha", "lan", "yes", "başard", "basard",
)

_PHATIC_MARKERS = (
    "selam", "sa", "slm", "merhaba", "naber", "nbr", "napıyon", "napiyon",
    "günaydın", "gunaydin", "iyi geceler", "eyvallah", "sağ ol", "sag ol",
)

_ROBOTIC_PHRASES = (
    "elbette",
    "memnuniyetle",
    "size yardımcı olabilirim",
    "size yardimci olabilirim",
    "yardımcı olmak için buradayım",
    "yardimci olmak icin buradayim",
    "talebiniz",
    "isteğiniz doğrultusunda",
    "isteginiz dogrultusunda",
    "işlem başarıyla tamamlandı",
    "islem basariyla tamamlandi",
)


def _fold(text: str) -> str:
    return " ".join((text or "").casefold().split())


def _contains_any(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker in text for marker in markers)


def _uppercase_energy(raw: str) -> bool:
    letters = [char for char in raw if char.isalpha()]
    if len(letters) < 4:
        return False
    upper = sum(1 for char in letters if char.isupper())
    return upper / len(letters) >= 0.65


def _repeated_energy(raw: str) -> bool:
    return bool(
        re.search(r"([!?])\1{1,}", raw)
        or re.search(r"([a-zA-ZçğıöşüÇĞİÖŞÜ])\1{2,}", raw)
    )


def classify_voice(user_message: str) -> VoiceProfile:
    """Classify only conversational style; never infer intent/permission."""
    raw = (user_message or "").strip()
    low = _fold(raw)

    technical = _contains_any(low, _TECHNICAL_MARKERS) or bool(
        re.search(r"[`{}<>]|\b\w+\.(?:py|ts|tsx|js|dart|kt|rs|json|yaml|yml)\b", raw)
    )
    serious = _contains_any(low, _SERIOUS_MARKERS)
    vulnerable = _contains_any(low, _VULNERABLE_MARKERS)
    frustrated = _contains_any(low, _FRUSTRATION_MARKERS)
    excited = (
        _contains_any(low, _EXCITEMENT_MARKERS)
        or _uppercase_energy(raw)
        or _repeated_energy(raw)
    )

    if serious:
        mode = "serious"
    elif vulnerable:
        mode = "supportive"
    elif technical:
        mode = "technical"
    else:
        mode = "casual"

    if vulnerable:
        energy = "calm"
    elif frustrated:
        energy = "frustrated"
    elif excited:
        energy = "high"
    else:
        energy = "normal"

    word_count = len(raw.split())
    phatic = _contains_any(low, _PHATIC_MARKERS)
    if phatic or word_count <= 6:
        brevity = "short"
    elif technical and word_count >= 28:
        brevity = "detailed"
    else:
        brevity = "normal"

    return VoiceProfile(
        mode=mode,
        energy=energy,
        brevity=brevity,
        technical=technical,
        phatic=phatic,
    )


def build_voice_guidance(user_message: str) -> str:
    """Return compact model-facing style guidance for the current turn."""
    profile = classify_voice(user_message)

    lines = [
        "# EPIS VOICE - BU TUR",
        (
            f"mode={profile.mode}; energy={profile.energy}; "
            f"brevity={profile.brevity}."
        ),
        "Bu etiketleri kullanıcıya söyleme; yalnızca ifade biçimini ayarla.",
        (
            "Varsayılan ses tanıdık, rahat ve doğrudan. Müşteri hizmetleri/asistan "
            "dili kullanma. 'Elbette', 'memnuniyetle', 'talebiniz', 'size yardımcı "
            "olabilirim', 'işlem başarıyla tamamlandı' gibi kalıpları ancak gerçekten "
            "doğal ve gerekli olduklarında kullan; günlük konuşmada kullanma."
        ),
        (
            "Kullanıcının cümlesini sırf onay vermek için yeniden ifade etme. Önce "
            "asıl tepkiyi, cevabı veya sonucu söyle. Gereksiz kapanış cümlesi ve "
            "otomatik 'istersen...' teklifi ekleme."
        ),
        (
            "Samimiyet performansı yapma: argoyu mekanik biçimde kopyalama, her "
            "mesajda 'aga/kanka/aynen' kullanma. Ama kullanıcı yüksek enerjideyse "
            "kısa süre aynı enerjiye çıkmak, doğal caps parçası, kahkaha veya 1-2 emoji "
            "kullanmak serbest. Sonra normal ritme dön. Kuru mizah doğal yerde serbest."
        ),
        (
            "Geçmiş bağlamı yalnızca gerçekten ilgiliyse kullan. Sırf hafızan varmış gibi "
            "göstermek için eski test etiketlerini, URL'leri, teknik ayrıntıları veya önceki "
            "konuları küçük sohbete taşıma. Kullanıcı konuya gönderme yapmadıysa alakasız "
            "callback üretme."
        ),
    ]

    if profile.mode == "casual":
        lines.append(
            "Günlük sohbet: arkadaşça ve akıcı konuş. Tek cümle yetiyorsa tek cümle. "
            "Mesaj sosyal ise onu görev/tavsiye listesine çevirmek zorunda değilsin."
        )
        if profile.phatic:
            lines.append(
                "Bu bir selam/küçük sohbet turu. Önceki teknik bağlamı kendiliğinden açma; "
                "yalnızca kullanıcı açıkça bağladıysa getir. Örnek ritim: 'Naber?' -> "
                "'İyi ya 😄 Sen?' gibi kısa ve o ana ait bir cevap. Örneği ezberleme."
            )
    elif profile.mode == "technical":
        lines.append(
            "Teknik tur: sonucu/teşhisi başa koy, sonra gerekli kanıtı veya adımı ver. "
            "Türkçe içinde yerleşik teknik İngilizce doğal. Resmiyet ve dolgu yok; "
            "rahat ama kesin ol."
        )
    elif profile.mode == "serious":
        lines.append(
            "Ciddi tur: şaka, gevşek filler ve coşkulu ton kullanma. Sonucu ve riski "
            "açık söyle; belirsizliği saklama; gerekiyorsa onay sınırını net tut."
        )
    elif profile.mode == "supportive":
        lines.append(
            "Hassas tur: sıcak ama patronizing olmayan bir ton kullan. Duyguyu kısa "
            "biçimde karşıla; boş teselli, slogan veya uzun nasihat verme."
        )

    if profile.energy == "high":
        lines.append(
            "Kullanıcı heyecanlı: enerjiyi söndürme. Kısa süre aynı seviyeye çıkabilirsin; "
            "bir kahkaha, kısa caps parçası veya 1-2 emoji gayet doğal. Ama cevabın tamamını "
            "bağırmaya ya da emoji yağmuruna çevirme. Örnek ritim: 'AGA ÇALIŞTI LAN SONUNDA' "
            "-> 'HAHA sonunda 😭 Bu sefer olmuş.' gibi. Örneği kelimesi kelimesine kopyalama."
        )
    elif profile.energy == "frustrated":
        lines.append(
            "Kullanıcı sinirli/frustre: bunu büyütme ve küfrü taklit etme. Kısa biçimde "
            "sürtünmeyi kabul edip çözüm/teşhise geç; terapi dili kullanma."
        )
    elif profile.energy == "calm":
        lines.append(
            "Enerjiyi düşük ve sakin tut; gereksiz neşe veya şaka ekleme."
        )

    if profile.brevity == "short":
        lines.append(
            "Bu turda kısa cevap lehine güçlü tercih var; yeni bilgi gerektirmiyorsa "
            "1-3 kısa cümleyi geçme."
        )
    elif profile.brevity == "detailed":
        lines.append(
            "Detay gerekiyorsa sistematik anlat; fakat giriş/kapanış dolgusunu yine atla."
        )

    return "\n".join(lines)


def find_style_violations(text: str, profile: VoiceProfile) -> list[str]:
    """Deterministic lint used by tests/diagnostics; it never rewrites replies."""
    low = _fold(text)
    problems: list[str] = []

    if profile.mode in {"casual", "technical"}:
        for phrase in _ROBOTIC_PHRASES:
            if phrase in low:
                problems.append(f"robotic_phrase:{phrase}")

    if profile.mode == "serious" and re.search(r"[😂🤣😅😜🤪]", text or ""):
        problems.append("serious_mode_jokey_emoji")

    if profile.energy != "high" and _uppercase_energy(text or ""):
        problems.append("unprompted_all_caps")

    return problems
