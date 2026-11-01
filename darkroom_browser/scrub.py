from __future__ import annotations

import re

REMOVED = "[removed]"
SCRIPT_BODY = re.compile(r"(?is)(<script\b[^>]*>).*?(</script>)")
INPUT_VALUE = re.compile(r"(?is)(<input\b[^>]*?\bvalue\s*=\s*)(\"[^\"]*\"|'[^']*'|[^\s>]+)")
SECRET_ATTRIBUTE = re.compile(r"(?is)\b((?:data-)?(?:[\w-]*token[\w-]*|[\w-]*secret[\w-]*|[\w-]*cookie[\w-]*|authuser|nonce|csrf[\w-]*|xsrf[\w-]*|sid|ssid|hsid|sapisid|apisid|at|f\.req))\s*=\s*(\"[^\"]*\"|'[^']*')")
META_SECRET = re.compile(r"(?is)(<meta\b[^>]*\b(?:token|csrf|xsrf|secret|nonce|session)[^>]*\bcontent\s*=\s*)(\"[^\"]*\"|'[^']*')")
URL_QUERY = re.compile(r"(https?://[^\s?#\"'<>]+)\?[^\s#\"'<>]*")
COOKIE_PAIR = re.compile(r"(?i)\b(SID|HSID|SSID|APISID|SAPISID|NID|OSID|__Secure-[\w-]+|__Host-[\w-]+|SIDCC|1P_JAR|AEC)=[^;\s\"'<>]+")
JWT = re.compile(r"\beyJ[\w-]{4,}\.[\w-]+\.[\w-]*")
LONG_OPAQUE = re.compile(r"(?<![\w/.-])[A-Za-z0-9_\-]{40,}(?![\w/.-])")
NOTICE = "<!-- scrubbed by Darkroom: scripts, form values, token-like attributes and query strings removed -->\n"


def scrub_html(html: str) -> str:
    text = SCRIPT_BODY.sub(rf"\1{REMOVED}\2", html)
    text = INPUT_VALUE.sub(rf'\1"{REMOVED}"', text)
    text = SECRET_ATTRIBUTE.sub(rf'\1="{REMOVED}"', text)
    text = META_SECRET.sub(rf'\1"{REMOVED}"', text)
    text = URL_QUERY.sub(rf"\1?{REMOVED}", text)
    text = COOKIE_PAIR.sub(rf"\1={REMOVED}", text)
    text = JWT.sub(REMOVED, text)
    text = LONG_OPAQUE.sub(REMOVED, text)
    return NOTICE + text
