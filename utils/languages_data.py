"""Language data for /giveaway guessthelanguage.

LANGUAGES is a list of (key, display_name, [aliases], sample_sentence). The sample is a
short greeting written in that language; players have to name the language.

normalize() keeps non-Latin scripts intact (so "русский" or "日本語" work as answers),
lowercases, strips punctuation, a leading "it's / this is / i think" and a trailing
"language". answers_for() returns every accepted form (with and without accents).
"""
import random
import re
import unicodedata

LANGUAGES = [
    ("es", "Spanish", ["espanol", "español", "castellano"], "Hola, ¿cómo estás hoy?"),
    ("fr", "French", ["francais", "français"], "Bonjour, comment allez-vous aujourd'hui ?"),
    ("de", "German", ["deutsch"], "Hallo, wie geht es dir heute?"),
    ("it", "Italian", ["italiano"], "Ciao, come stai oggi?"),
    ("pt", "Portuguese", ["portugues", "português"], "Olá, como estás hoje?"),
    ("nl", "Dutch", ["nederlands"], "Hallo, hoe gaat het vandaag met je?"),
    ("sv", "Swedish", ["svenska"], "Hej, hur mår du idag?"),
    ("no", "Norwegian", ["norsk"], "Hei, hvordan har du det i dag? Jeg er glad for å se deg."),
    ("da", "Danish", ["dansk"], "Hej, hvordan har du det i dag? Jeg er glad for at se dig."),
    ("fi", "Finnish", ["suomi"], "Hei, mitä kuuluu tänään?"),
    ("is", "Icelandic", ["islenska", "íslenska"], "Halló, hvernig hefur þú það í dag?"),
    ("pl", "Polish", ["polski"], "Cześć, jak się dzisiaj masz?"),
    ("cs", "Czech", ["cestina", "čeština"], "Ahoj, jak se dnes máš?"),
    ("sk", "Slovak", ["slovencina", "slovenčina"], "Ahoj, ako sa dnes máš?"),
    ("sl", "Slovenian", ["slovene", "slovenscina", "slovenščina"], "Živjo, kako si danes?"),
    ("hr", "Croatian", ["hrvatski"], "Bok, kako si danas?"),
    ("sr", "Serbian", ["srpski", "српски"], "Здраво, како си данас? Драго ми је да те видим."),
    ("mk", "Macedonian", ["македонски"], "Здраво, како си денес? Мило ми е што те гледам."),
    ("hu", "Hungarian", ["magyar"], "Szia, hogy vagy ma?"),
    ("ro", "Romanian", ["romana", "română"], "Salut, ce mai faci azi?"),
    ("bg", "Bulgarian", ["български"], "Здравей, как си днес?"),
    ("ru", "Russian", ["русский"], "Привет, как твои дела сегодня?"),
    ("uk", "Ukrainian", ["українська"], "Привіт, як у тебе справи сьогодні?"),
    ("el", "Greek", ["ελληνικά"], "Γεια σου, πώς είσαι σήμερα;"),
    ("sq", "Albanian", ["shqip"], "Përshëndetje, si je sot?"),
    ("lt", "Lithuanian", ["lietuviu", "lietuvių"], "Labas, kaip sekasi šiandien?"),
    ("lv", "Latvian", ["latviesu", "latviešu"], "Sveiki, kā jums šodien klājas?"),
    ("et", "Estonian", ["eesti"], "Tere, kuidas sul täna läheb?"),
    ("ca", "Catalan", ["catala", "català"], "Hola, com estàs avui?"),
    ("eu", "Basque", ["euskara"], "Kaixo, zer moduz zaude gaur?"),
    ("gl", "Galician", ["galego"], "Ola, como estás hoxe?"),
    ("ga", "Irish", ["gaeilge", "irish gaelic"], "Dia duit, conas atá tú inniu?"),
    ("cy", "Welsh", ["cymraeg"], "Helo, sut wyt ti heddiw?"),
    ("mt", "Maltese", ["malti"], "Bonġu, kif int illum?"),
    ("la", "Latin", ["latina"], "Salve, quomodo te habes hodie?"),
    ("eo", "Esperanto", [], "Saluton, kiel vi fartas hodiaŭ?"),
    ("tr", "Turkish", ["turkce", "türkçe"], "Merhaba, bugün nasılsın?"),
    ("ar", "Arabic", ["العربية", "arabi"], "مرحبا، كيف حالك اليوم؟"),
    ("he", "Hebrew", ["עברית", "ivrit"], "שלום, מה שלומך היום?"),
    ("fa", "Persian", ["farsi", "فارسی"], "سلام، امروز حالت چطور است؟"),
    ("ur", "Urdu", ["اردو"], "السلام علیکم، آپ آج کیسے ہیں؟"),
    ("hi", "Hindi", ["हिन्दी", "हिंदी"], "नमस्ते, आज आप कैसे हैं?"),
    ("mr", "Marathi", ["मराठी"], "नमस्कार, आज तुम्ही कसे आहात?"),
    ("ne", "Nepali", ["नेपाली"], "नमस्ते, तपाईंलाई आज कस्तो छ?"),
    ("bn", "Bengali", ["bangla", "বাংলা"], "নমস্কার, আপনি আজ কেমন আছেন?"),
    ("gu", "Gujarati", ["ગુજરાતી"], "નમસ્તે, આજે તમે કેમ છો?"),
    ("pa", "Punjabi", ["panjabi", "ਪੰਜਾਬੀ"], "ਸਤ ਸ੍ਰੀ ਅਕਾਲ, ਤੁਸੀਂ ਅੱਜ ਕਿਵੇਂ ਹੋ?"),
    ("ta", "Tamil", ["தமிழ்"], "வணக்கம், இன்று நீங்கள் எப்படி இருக்கிறீர்கள்?"),
    ("te", "Telugu", ["తెలుగు"], "నమస్కారం, ఈ రోజు మీరు ఎలా ఉన్నారు?"),
    ("si", "Sinhala", ["sinhalese", "සිංහල"], "ආයුබෝවන්, ඔබ අද කොහොමද?"),
    ("th", "Thai", ["ไทย"], "สวัสดี วันนี้คุณเป็นอย่างไรบ้าง"),
    ("vi", "Vietnamese", ["tieng viet", "tiếng việt"], "Xin chào, hôm nay bạn khỏe không?"),
    ("id", "Indonesian", ["bahasa indonesia"], "Halo, apa kabar hari ini?"),
    ("ms", "Malay", ["bahasa melayu"], "Helo, apa khabar hari ini?"),
    ("tl", "Filipino", ["tagalog"], "Kumusta ka ngayong araw?"),
    ("ja", "Japanese", ["nihongo", "日本語"], "こんにちは、今日はお元気ですか？"),
    ("ko", "Korean", ["hangugeo", "한국어"], "안녕하세요, 오늘 어떻게 지내세요?"),
    ("zh", "Chinese", ["mandarin", "mandarin chinese", "中文", "普通话"], "你好，你今天好吗？"),
    ("mn", "Mongolian", ["монгол"], "Сайн байна уу, өнөөдөр сайн уу?"),
    ("kk", "Kazakh", ["қазақша"], "Сәлем, бүгін қалайсың?"),
    ("uz", "Uzbek", ["ozbek", "o'zbek"], "Salom, bugun qandaysiz?"),
    ("ka", "Georgian", ["ქართული"], "გამარჯობა, როგორ ხართ დღეს?"),
    ("hy", "Armenian", ["հայերեն"], "Բարև, ինչպես ես այսօր?"),
    ("sw", "Swahili", ["kiswahili"], "Habari, hujambo leo?"),
    ("af", "Afrikaans", [], "Hallo, hoe gaan dit vandag met jou?"),
    ("zu", "Zulu", ["isizulu"], "Sawubona, unjani namhlanje?"),
    ("am", "Amharic", ["አማርኛ"], "ሰላም፣ ዛሬ እንዴት ነህ?"),
    ("haw", "Hawaiian", ["olelo hawaii", "ʻōlelo hawaiʻi"], "Aloha, pehea ʻoe i kēia lā?"),
]

_PREFIX_RE = re.compile(r"^(it'?s|its|this is|is it|i think its?|i think|it is)\s+")
_SUFFIX_RE = re.compile(r"\s+language$")


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFC", str(text or "")).casefold()
    text = re.sub(r"[^\w' ]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = _PREFIX_RE.sub("", text)
    text = _SUFFIX_RE.sub("", text)
    return text.replace("'", "").strip()


def strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text)
                   if not unicodedata.combining(c))


def forms(text: str) -> set:
    """Every form of `text` we'd accept: normalised, with and without accents."""
    n = normalize(text)
    return {n, strip_accents(n)} - {""}


def answers_for(entry) -> set:
    _key, name, aliases, _sample = entry
    out = set(forms(name))
    for a in aliases:
        out |= forms(a)
    return out


def matches(entry_answers: set, text: str) -> bool:
    return bool(forms(text) & entry_answers)


def random_language():
    return random.choice(LANGUAGES)


def find_by_key(key: str):
    for e in LANGUAGES:
        if e[0] == key:
            return e
    return None
