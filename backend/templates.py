"""WhatsApp template registry — proactive (business-initiated) messages
sent outside the 24-hour customer-service window MUST be pre-approved
templates in WhatsApp Business Platform.

In Phase 3 we maintain templates in code with multilingual variants and
substitute variables locally. In production, the actual rendering happens
inside Meta's infrastructure when you send a `contentSid`-based message.

Each template has:
  - name: stable identifier
  - variants: dict[ISO lang code] -> body with {variable} placeholders
  - variables: ordered list of variable names (for validation)
  - approved_languages: which langs Meta has approved (mock-true here)
  - category: utility | authentication | marketing (Meta categorisation)
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Template:
    name: str
    variables: list[str]
    variants: dict[str, str]   # lang -> body
    category: str = "utility"

    def render(self, *, language: str, variables: dict[str, str]) -> str:
        body = self.variants.get(language) or self.variants.get("en-IN") or next(iter(self.variants.values()))
        for k in self.variables:
            v = variables.get(k, "{" + k + "}")
            body = body.replace("{" + k + "}", str(v))
        return body


REGISTRY: dict[str, Template] = {}


def register(t: Template) -> None:
    REGISTRY[t.name] = t


def get(name: str) -> Template | None:
    return REGISTRY.get(name)


def render(name: str, language: str, variables: dict[str, str]) -> str:
    t = get(name)
    if not t:
        return ""
    return t.render(language=language, variables=variables)


# -- Seeded templates (multilingual where realistic) -------------------------

register(Template(
    name="patta_transfer_approved",
    variables=["name", "survey_no", "vao"],
    category="utility",
    variants={
        "en-IN": "Hello {name}, your Patta transfer for survey number {survey_no} has been approved. Please collect the certificate at your VAO office: {vao}.",
        "ta-IN": "வணக்கம் {name}, உங்கள் சர்வே எண் {survey_no}-க்கான பட்டா மாற்றம் ஒப்புதல் பெற்றுள்ளது. சான்றிதழை VAO அலுவலகத்தில் ({vao}) பெறவும்.",
        "hi-IN": "नमस्ते {name}, सर्वे संख्या {survey_no} के लिए आपका पट्टा हस्तांतरण स्वीकृत हो गया है। कृपया अपने VAO कार्यालय ({vao}) से प्रमाणपत्र प्राप्त करें।",
    },
))

register(Template(
    name="dl_renewal_due",
    variables=["name", "dl_number", "expiry_date", "booking_url"],
    category="utility",
    variants={
        "en-IN": "Hello {name}, your driving licence {dl_number} expires on {expiry_date}. Renew online at tnsta.gov.in or book a slot here: {booking_url}",
        "ta-IN": "வணக்கம் {name}, உங்கள் ஓட்டுநர் உரிமம் {dl_number} {expiry_date} அன்று காலாவதியாகும். tnsta.gov.in-ல் ஆன்லைனில் புதுப்பிக்கவும்: {booking_url}",
        "hi-IN": "नमस्ते {name}, आपका ड्राइविंग लाइसेंस {dl_number} {expiry_date} को समाप्त हो रहा है। tnsta.gov.in पर ऑनलाइन नवीनीकरण करें: {booking_url}",
    },
))

register(Template(
    name="ration_allocation_released",
    variables=["month", "rice_kg", "sugar_kg", "shop_id"],
    category="utility",
    variants={
        "en-IN": "Your {month} ration is released — Rice {rice_kg} kg, Sugar {sugar_kg} kg. Collect from PDS shop {shop_id}. Bring your smart card.",
        "ta-IN": "{month}-ம் மாத ரேஷன் வழங்கப்பட்டுள்ளது — அரிசி {rice_kg} கிலோ, சர்க்கரை {sugar_kg} கிலோ. PDS கடை {shop_id}-ல் பெறவும். ஸ்மார்ட் கார்டு கொண்டு வரவும்.",
        "hi-IN": "{month} का राशन जारी हुआ है — चावल {rice_kg} किलो, चीनी {sugar_kg} किलो। PDS दुकान {shop_id} से लें। स्मार्ट कार्ड लाएँ।",
    },
))

register(Template(
    name="complaint_status_update",
    variables=["complaint_id", "status", "next_step"],
    category="utility",
    variants={
        "en-IN": "Complaint update: ID {complaint_id} is now {status}. Next step: {next_step}",
        "ta-IN": "புகார் புதுப்பிப்பு: ஐடி {complaint_id} - நிலை: {status}. அடுத்த படி: {next_step}",
        "hi-IN": "शिकायत अपडेट: ID {complaint_id} - स्थिति: {status}. अगला कदम: {next_step}",
    },
))

register(Template(
    name="scheme_launch",
    variables=["scheme_name", "eligibility", "apply_url"],
    category="utility",
    variants={
        "en-IN": "📢 New scheme launched: {scheme_name}. Eligibility: {eligibility}. Apply at: {apply_url}",
        "ta-IN": "📢 புதிய திட்டம் தொடங்கப்பட்டது: {scheme_name}. தகுதி: {eligibility}. விண்ணப்பிக்க: {apply_url}",
        "hi-IN": "📢 नई योजना शुरू: {scheme_name}. पात्रता: {eligibility}. आवेदन: {apply_url}",
    },
))

register(Template(
    name="disaster_relief_eligible",
    variables=["name", "scheme", "amount", "deadline"],
    category="utility",
    variants={
        "en-IN": "Hello {name}, you are eligible for {scheme}: ₹{amount}. Submit application by {deadline} at your VAO.",
        "ta-IN": "வணக்கம் {name}, நீங்கள் {scheme}-க்கு தகுதியானவர்: ₹{amount}. {deadline}-க்குள் VAO-வில் விண்ணப்பிக்கவும்.",
        "hi-IN": "नमस्ते {name}, आप {scheme} के लिए पात्र हैं: ₹{amount}. {deadline} तक VAO में आवेदन जमा करें।",
    },
))


def list_templates() -> list[dict]:
    return [
        {
            "name": t.name, "variables": t.variables, "category": t.category,
            "languages": list(t.variants.keys()),
        }
        for t in REGISTRY.values()
    ]
