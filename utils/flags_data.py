"""Country flag data for /giveaway guessflag.

FLAGS is a list of (iso2_code, display_name, [aliases]). The flag image is
https://flagcdn.com/w640/<code>.png (Discord fetches it, the bot never does).

normalize() lowercases, strips accents / punctuation and a leading "the", so
"The Netherlands", "netherlands" and "NETHERLANDS!" all match.
"""
import random
import re
import unicodedata

FLAGS = [
    ("af", "Afghanistan", []),
    ("al", "Albania", []),
    ("dz", "Algeria", []),
    ("ad", "Andorra", []),
    ("ao", "Angola", []),
    ("ag", "Antigua and Barbuda", ["antigua", "antigua & barbuda"]),
    ("ar", "Argentina", []),
    ("am", "Armenia", []),
    ("au", "Australia", []),
    ("at", "Austria", []),
    ("az", "Azerbaijan", []),
    ("bs", "Bahamas", ["the bahamas"]),
    ("bh", "Bahrain", []),
    ("bd", "Bangladesh", []),
    ("bb", "Barbados", []),
    ("by", "Belarus", []),
    ("be", "Belgium", []),
    ("bz", "Belize", []),
    ("bj", "Benin", []),
    ("bt", "Bhutan", []),
    ("bo", "Bolivia", []),
    ("ba", "Bosnia and Herzegovina", ["bosnia", "bosnia & herzegovina", "bosnia herzegovina"]),
    ("bw", "Botswana", []),
    ("br", "Brazil", ["brasil"]),
    ("bn", "Brunei", []),
    ("bg", "Bulgaria", []),
    ("bf", "Burkina Faso", []),
    ("bi", "Burundi", []),
    ("cv", "Cape Verde", ["cabo verde"]),
    ("kh", "Cambodia", []),
    ("cm", "Cameroon", []),
    ("ca", "Canada", []),
    ("cf", "Central African Republic", []),
    ("td", "Chad", []),
    ("cl", "Chile", []),
    ("cn", "China", ["peoples republic of china", "prc"]),
    ("co", "Colombia", []),
    ("km", "Comoros", []),
    ("cg", "Republic of the Congo", ["congo", "congo brazzaville", "republic of congo"]),
    ("cd", "Democratic Republic of the Congo", ["drc", "dr congo", "congo kinshasa", "democratic republic of congo"]),
    ("cr", "Costa Rica", []),
    ("ci", "Ivory Coast", ["cote divoire", "cote d ivoire"]),
    ("hr", "Croatia", []),
    ("cu", "Cuba", []),
    ("cy", "Cyprus", []),
    ("cz", "Czech Republic", ["czechia", "czech"]),
    ("dk", "Denmark", []),
    ("dj", "Djibouti", []),
    ("dm", "Dominica", []),
    ("do", "Dominican Republic", []),
    ("ec", "Ecuador", []),
    ("eg", "Egypt", []),
    ("sv", "El Salvador", []),
    ("gq", "Equatorial Guinea", []),
    ("er", "Eritrea", []),
    ("ee", "Estonia", []),
    ("sz", "Eswatini", ["swaziland"]),
    ("et", "Ethiopia", []),
    ("fj", "Fiji", []),
    ("fi", "Finland", []),
    ("fr", "France", []),
    ("ga", "Gabon", []),
    ("gm", "Gambia", ["the gambia"]),
    ("ge", "Georgia", []),
    ("de", "Germany", ["deutschland"]),
    ("gh", "Ghana", []),
    ("gr", "Greece", []),
    ("gd", "Grenada", []),
    ("gt", "Guatemala", []),
    ("gn", "Guinea", []),
    ("gw", "Guinea-Bissau", ["guinea bissau"]),
    ("gy", "Guyana", []),
    ("ht", "Haiti", []),
    ("hn", "Honduras", []),
    ("hu", "Hungary", []),
    ("is", "Iceland", []),
    ("in", "India", []),
    ("id", "Indonesia", []),
    ("ir", "Iran", []),
    ("iq", "Iraq", []),
    ("ie", "Ireland", []),
    ("il", "Israel", []),
    ("it", "Italy", []),
    ("jm", "Jamaica", []),
    ("jp", "Japan", []),
    ("jo", "Jordan", []),
    ("kz", "Kazakhstan", []),
    ("ke", "Kenya", []),
    ("ki", "Kiribati", []),
    ("xk", "Kosovo", []),
    ("kw", "Kuwait", []),
    ("kg", "Kyrgyzstan", []),
    ("la", "Laos", []),
    ("lv", "Latvia", []),
    ("lb", "Lebanon", []),
    ("ls", "Lesotho", []),
    ("lr", "Liberia", []),
    ("ly", "Libya", []),
    ("li", "Liechtenstein", []),
    ("lt", "Lithuania", []),
    ("lu", "Luxembourg", []),
    ("mg", "Madagascar", []),
    ("mw", "Malawi", []),
    ("my", "Malaysia", []),
    ("mv", "Maldives", []),
    ("ml", "Mali", []),
    ("mt", "Malta", []),
    ("mh", "Marshall Islands", []),
    ("mr", "Mauritania", []),
    ("mu", "Mauritius", []),
    ("mx", "Mexico", []),
    ("fm", "Micronesia", []),
    ("md", "Moldova", []),
    ("mc", "Monaco", []),
    ("mn", "Mongolia", []),
    ("me", "Montenegro", []),
    ("ma", "Morocco", []),
    ("mz", "Mozambique", []),
    ("mm", "Myanmar", ["burma"]),
    ("na", "Namibia", []),
    ("nr", "Nauru", []),
    ("np", "Nepal", []),
    ("nl", "Netherlands", ["holland", "the netherlands"]),
    ("nz", "New Zealand", []),
    ("ni", "Nicaragua", []),
    ("ne", "Niger", []),
    ("ng", "Nigeria", []),
    ("kp", "North Korea", ["dprk"]),
    ("mk", "North Macedonia", ["macedonia"]),
    ("no", "Norway", []),
    ("om", "Oman", []),
    ("pk", "Pakistan", []),
    ("pw", "Palau", []),
    ("ps", "Palestine", []),
    ("pa", "Panama", []),
    ("pg", "Papua New Guinea", ["png"]),
    ("py", "Paraguay", []),
    ("pe", "Peru", []),
    ("ph", "Philippines", ["the philippines"]),
    ("pl", "Poland", []),
    ("pt", "Portugal", []),
    ("qa", "Qatar", []),
    ("ro", "Romania", []),
    ("ru", "Russia", ["russian federation"]),
    ("rw", "Rwanda", []),
    ("kn", "Saint Kitts and Nevis", ["st kitts and nevis", "saint kitts", "st kitts"]),
    ("lc", "Saint Lucia", ["st lucia"]),
    ("vc", "Saint Vincent and the Grenadines", ["st vincent and the grenadines", "saint vincent", "st vincent"]),
    ("ws", "Samoa", []),
    ("sm", "San Marino", []),
    ("st", "Sao Tome and Principe", ["sao tome"]),
    ("sa", "Saudi Arabia", ["saudi"]),
    ("sn", "Senegal", []),
    ("rs", "Serbia", []),
    ("sc", "Seychelles", []),
    ("sl", "Sierra Leone", []),
    ("sg", "Singapore", []),
    ("sk", "Slovakia", []),
    ("si", "Slovenia", []),
    ("sb", "Solomon Islands", []),
    ("so", "Somalia", []),
    ("za", "South Africa", []),
    ("kr", "South Korea", ["korea", "republic of korea"]),
    ("ss", "South Sudan", []),
    ("es", "Spain", []),
    ("lk", "Sri Lanka", []),
    ("sd", "Sudan", []),
    ("sr", "Suriname", []),
    ("se", "Sweden", []),
    ("ch", "Switzerland", []),
    ("sy", "Syria", []),
    ("tw", "Taiwan", []),
    ("tj", "Tajikistan", []),
    ("tz", "Tanzania", []),
    ("th", "Thailand", []),
    ("tl", "Timor-Leste", ["east timor", "timor leste"]),
    ("tg", "Togo", []),
    ("to", "Tonga", []),
    ("tt", "Trinidad and Tobago", ["trinidad", "trinidad & tobago"]),
    ("tn", "Tunisia", []),
    ("tr", "Turkey", ["turkiye"]),
    ("tm", "Turkmenistan", []),
    ("tv", "Tuvalu", []),
    ("ug", "Uganda", []),
    ("ua", "Ukraine", []),
    ("ae", "United Arab Emirates", ["uae", "emirates"]),
    ("gb", "United Kingdom", ["uk", "britain", "great britain"]),
    ("us", "United States", ["usa", "america", "united states of america"]),
    ("uy", "Uruguay", []),
    ("uz", "Uzbekistan", []),
    ("vu", "Vanuatu", []),
    ("va", "Vatican City", ["vatican", "holy see"]),
    ("ve", "Venezuela", []),
    ("vn", "Vietnam", ["viet nam"]),
    ("ye", "Yemen", []),
    ("zm", "Zambia", []),
    ("zw", "Zimbabwe", []),
]

_PREFIX_RE = re.compile(r"^(it'?s|its|the flag is|flag is|this is|is it|i think its?|i think)\s+")


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", str(text or ""))
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9' ]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = _PREFIX_RE.sub("", text)
    text = text.replace("'", "")
    if text.startswith("the "):
        text = text[4:]
    return text.strip()


def answers_for(entry) -> set:
    code, name, aliases = entry
    return {normalize(name)} | {normalize(a) for a in aliases}


def flag_url(code: str) -> str:
    return f"https://flagcdn.com/w640/{code}.png"


def random_flag():
    return random.choice(FLAGS)


def find_by_code(code: str):
    for f in FLAGS:
        if f[0] == code:
            return f
    return None
